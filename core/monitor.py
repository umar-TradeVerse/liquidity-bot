"""
MarketMonitor — continuously polls 15-minute candles and routes signals to execution.
Runs all 7 days a week, from 5:30 AM to 11:00 PM IST.

Exit hierarchy for open positions, evaluated in strict priority order on
every closed candle — first true condition wins:
  1. Target reached (PDH for LONG / PDL for SHORT) -> full close.
     SKIPPED for trend-mode (flip) trades — see note below.
  2. Rejection candle within REJECTION_PROXIMITY_PCT of target, AND the
     close is actually favorable versus entry -> full close. The
     profitability check was added after a real case (XRPUSD,
     2026-07-19) where this fired as a "Take Profit" while the position
     was actually underwater — the old logic only checked proximity and
     candle shape, never whether the trade was in profit at all.
     Also skipped for trend-mode trades (depends on the same target).
  3. ROE (CoinDCX-reported) >= ROE_TARGET_PCT -> full close.

Trend-mode trades (the sell-side/buy-side liquidity flip, see strategy.py)
deliberately skip priorities 1 and 2: their entry often already sits past
the fixed daily PDH/PDL by design, so a fixed-target check would trigger
a bogus near-immediate exit. These trades run on stop-loss + ROE only.

Rule 3 — one automatic trade per symbol per day: once a symbol has had
one auto-placed trade today, any further clean setup on that symbol is
alert-only, never auto-placed, regardless of how that first trade closes.

Counter-trend signals (fighting today's trend_bias) are always alert-only,
regardless of Rule 3.

BTC-regime is informational only, logged and surfaced in alerts, no longer
gates execution.

Margin cap: CoinDCX's futures wallet-balance endpoint has been confirmed
broken (404) since 2026-07-17, so the insufficient-funds pre-check no
longer depends on it. Instead the bot tracks its own committed margin
(positions already open x margin per trade) against an optional,
manually-configured TOTAL_ACCOUNT_MARGIN_USD env var. Unset or 0 disables
the check entirely.

Concurrency: all symbols are polled and processed concurrently via
asyncio.gather every cycle. Checking-then-reserving a MAX_CONCURRENT_POSITIONS
slot is guarded by self._position_lock so two symbols signaling in the
same poll cycle can't both slip past the cap before either registers.
"""

import asyncio
from datetime import datetime, time as dtime
from typing import Optional
import pytz
import os

from core.state import BotState, SYMBOLS, REGIME_SYMBOL, TradeRecord
from core.strategy import StrategyEngine, Signal
from exchange.coindcx import CoinDCXClient
from notifications.telegram import TelegramBot
from core import persistence
from utils.logger import setup_logger
logger = setup_logger("monitor")
IST = pytz.timezone("Asia/Kolkata")

POLL_INTERVAL_SECONDS = 15
DAY_END_HOUR = 23
DAY_END_MINUTE = 0

TRADE_LEVERAGE = 10
MAX_CONCURRENT_POSITIONS = 2

# Partial take-profit ladder (added per Umar's request after the 7-day MFE
# analysis showed avg MFE ~0.97R vs avg realized ~0.17R). Applies ONLY to
# non-trend-mode trades — trend-mode (flip) trades keep the existing
# SL + ROE-only behavior unchanged, since a fixed-target ladder doesn't fit
# an entry that's already past the daily PDH/PDL by design.
# TP3 is capped at the original single-target level (PDH/PDL) — it is NOT
# a new, more distant target. TP1/TP2 are interior partials closer than
# that existing target. If TP1's R-multiple would sit beyond TP3, the
# ladder silently collapses to the old single-TP behavior for that trade
# (no partial tiers, same as before this change).
TP_LADDER_R = (1.5, 2.5)  # TP1, TP2 — TP3 is always the original PDH/PDL target
TP_LADDER_WEIGHTS = (0.34, 0.33, 0.33)  # TP1, TP2, TP3 — must sum to 1.0
MIN_TRADES_FOR_HISTORICAL_STATS = 5  # below this, message shows "not enough
                                      # data yet" rather than a misleadingly
                                      # precise win rate from a tiny sample

# Breakeven stop-move — added after the 7-day loss analysis showed 6 of 9
# losing trades had moved 0.3R-0.8R in their favor before reversing all the
# way to full stop-loss. Triggered on raw MFE reaching an R-multiple
# (checked directly against candle highs/lows), NOT tied to TP1 filling —
# that keeps it independent of the TP ladder so it also protects trend-mode
# trades (which skip the ladder and TP1/TP2 entirely) and doesn't depend on
# a partial-close order having succeeded first.
#
# Went through two single-threshold versions before landing on a staged
# ratchet (2026-07-29):
#   0.5R (original) — ETHUSD moved to exact breakeven, then got clipped by
#     a small reversal wick right before the original move resumed hard.
#   1.0R (the fix for the above, 2026-07-27) — KAITOUSD then moved only
#     ~0.47R and reversed to a FULL loss, since 1.0R never triggered at all.
# There's no single number that avoids both failure modes — it's a genuine
# structural tradeoff, not a tuning problem. A staged ratchet reduces the
# SEVERITY of the failure instead: partial protection kicks in earlier (so
# a KAITOUSD-style reversal takes a smaller loss, not the full one), while
# full breakeven still requires a more convincing move (so an ETHUSD-style
# small wick is less likely to clip it).
BREAKEVEN_STAGE1_R = 0.4   # partial: cut remaining risk roughly in half
BREAKEVEN_STAGE2_R = 1.0   # full: move SL all the way to entry
STABILITY_MAX_COUNTER_CONFIRMS = 2  # Trend Stability — after this many
                                     # counter-trend confirmations on a
                                     # symbol today, the day's trend
                                     # classification is treated as no
                                     # longer reliable, and BOTH sides go
                                     # alert-only for the rest of the day,
                                     # not just the counter-trend one.

REJECTION_PROXIMITY_PCT = 0.01  # 1.0%
ROE_TARGET_PCT = 7.0
MIN_ROE_FOR_REJECTION_EXIT_PCT = 3.0  # JUDGMENT CALL, unvalidated — half of
                                       # ROE_TARGET_PCT. Priority 2 (rejection
                                       # exit) now requires the position to
                                       # already be at this much profit before
                                       # it's allowed to fire — added after a
                                       # real case (ETHUSD, 2026-07-20) where
                                       # a marginal-profit rejection candle
                                       # closed the trade just before a much
                                       # larger continuation move. This keeps
                                       # the reversal-protection intent while
                                       # requiring more conviction than "any
                                       # profit at all" before bailing early.


def _escape_md(text) -> str:
    text = str(text)
    for ch in ('_', '*', '`', '['):
        text = text.replace(ch, '\\' + ch)
    return text


def _is_bearish_rejection(candle: dict) -> bool:
    body = abs(candle['close'] - candle['open'])
    upper_wick = candle['high'] - max(candle['open'], candle['close'])
    lower_wick = min(candle['open'], candle['close']) - candle['low']
    if candle['close'] >= candle['open'] or body == 0:
        return False
    return upper_wick >= 2 * body and upper_wick >= 2 * lower_wick


def _is_bullish_rejection(candle: dict) -> bool:
    body = abs(candle['close'] - candle['open'])
    upper_wick = candle['high'] - max(candle['open'], candle['close'])
    lower_wick = min(candle['open'], candle['close']) - candle['low']
    if candle['close'] <= candle['open'] or body == 0:
        return False
    return lower_wick >= 2 * body and lower_wick >= 2 * upper_wick


def _is_bearish_engulfing(prev_candle: dict, candle: dict) -> bool:
    if not (prev_candle['close'] > prev_candle['open'] and candle['close'] < candle['open']):
        return False
    return candle['open'] >= prev_candle['close'] and candle['close'] <= prev_candle['open']


def _is_bullish_engulfing(prev_candle: dict, candle: dict) -> bool:
    if not (prev_candle['close'] < prev_candle['open'] and candle['close'] > candle['open']):
        return False
    return candle['open'] <= prev_candle['close'] and candle['close'] >= prev_candle['open']


class MarketMonitor:
    def __init__(self, coindcx: CoinDCXClient, engine: StrategyEngine,
                 state: BotState, telegram: TelegramBot):
        self.coindcx = coindcx
        self.engine = engine
        self.state = state
        self.telegram = telegram
        self._last_candle_time = {sym: None for sym in SYMBOLS}
        self._last_candle: dict = {}
        self._last_regime_candle_time = None
        self._open_positions: dict = {}
        self._trailing: dict = {}
        self._position_lock = asyncio.Lock()
        self._loops_since_save = 0
        self._SAVE_EVERY_N_LOOPS = 4  # ~1 minute at POLL_INTERVAL_SECONDS=15
        self._orphan_alerted: set = set()  # symbols already flagged this run —

    def restore_trailing(self, trailing: dict):
        """Called once from main.py at startup if a same-day state snapshot
        was found. Restores in-flight SL/TP/MFE/MAE tracking so the exit
        logic (_check_exit_conditions) resumes correctly after a restart,
        instead of going dark on any position that was open when the
        process stopped."""
        self._trailing = trailing or {}
        if self._trailing:
            logger.info(f"Restored trailing state for: {list(self._trailing.keys())}")

    def _is_trading_hours(self) -> bool:
        now = datetime.now(IST)
        return dtime(5, 30) <= now.time() <= dtime(DAY_END_HOUR, DAY_END_MINUTE)

    async def run(self):
        logger.info("Market monitor started")
        while True:
            try:
                if not self.state.levels_ready() or self.state.paused:
                    await asyncio.sleep(30)
                    continue

                if not self._is_trading_hours():
                    await asyncio.sleep(60)
                    continue

                await self._reconcile_positions()
                await self._update_regime()

                # Union with self._trailing.keys(): if a symbol was removed
                # from SYMBOLS (watchlist restructuring) while it still had
                # an open position, it must keep being monitored until that
                # position actually closes — otherwise its breakeven-move,
                # TP ladder, and exit logic silently stop, and the position
                # becomes invisible to reconciliation even though it's still
                # live on the exchange. New signals still can't form for it
                # (strategy.py only evaluates symbols in SYMBOLS), so this
                # only affects winding down what's already open, never
                # opens anything new.
                active_symbols = set(SYMBOLS) | set(self._trailing.keys())
                tasks = [self._process_symbol(sym) for sym in active_symbols]
                await asyncio.gather(*tasks, return_exceptions=True)

                self._loops_since_save += 1
                if self._loops_since_save >= self._SAVE_EVERY_N_LOOPS:
                    persistence.save_state(self.state, self._trailing)
                    self._loops_since_save = 0

            except Exception as e:
                logger.error(f"Monitor loop error: {e}", exc_info=True)

            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def _update_regime(self):
        try:
            candle = await self.coindcx.get_latest_15m_candle(REGIME_SYMBOL)
            if not candle or self._last_regime_candle_time == candle['time']:
                return
            self._last_regime_candle_time = candle['time']
            self.state.update_regime_price(candle['close'])
            logger.info(f"{REGIME_SYMBOL} (regime ref) | Close: {candle['close']:.4f} | "
                       f"Regime now: {self.state.get_regime()}")
        except Exception as e:
            logger.error(f"{REGIME_SYMBOL} (regime ref) | update error: {e}", exc_info=True)

    async def _classify_reconciled_exit(self, symbol: str) -> str:
        tr = self._trailing.get(symbol)
        if not tr:
            return "reason unknown (no trailing data)"

        try:
            fill = await self.coindcx.get_last_fill(symbol)
        except Exception as e:
            logger.error(f"{symbol} | Failed to fetch last fill for exit classification: {e}")
            return "reason unknown (fill lookup failed)"

        if not fill or fill.get("price") is None:
            return "reason unknown (no fill data returned)"

        price = fill["price"]
        sl = tr.get("live_sl", tr.get("sl"))
        tp = tr.get("tp")
        tolerance = 0.005

        if sl and abs(price - sl) / sl <= tolerance:
            label = "Breakeven Stop hit" if tr.get("breakeven_moved") else "Stop Loss hit"
            return f"{label} (fill ~{price:.4f} vs SL {sl:.4f})"
        if tp and abs(price - tp) / tp <= tolerance:
            return f"Take Profit hit (fill ~{price:.4f} vs TP {tp:.4f})"
        return f"unclear — fill ~{price:.4f} (entry {tr.get('entry')}, SL {sl}, TP {tp})"

    def _log_close(self, symbol: str, tr: dict, exit_price: Optional[float], reason: str,
                    event_type: str = "close"):
        """Writes one line to trades.jsonl using whatever we tracked in
        self._trailing for this symbol (entry/sl/tp/mfe/mae/opened_at).
        event_type='close' means the position is fully done (used for win-rate
        stats in get_symbol_stats). event_type='partial_close' is a TP1/TP2
        ladder fill — the trade is still open, and get_symbol_stats
        deliberately excludes these from win-rate/occurrence counting so a
        single trade with two partial fills doesn't get counted as three."""
        try:
            entry = tr.get("entry")
            side = tr.get("side")
            opened_at = tr.get("opened_at")
            duration_minutes = None
            if opened_at:
                try:
                    opened_dt = datetime.fromisoformat(opened_at)
                    duration_minutes = round((datetime.now(IST) - opened_dt).total_seconds() / 60, 1)
                except Exception:
                    pass

            rr = None
            if exit_price is not None and entry is not None and tr.get("sl") is not None:
                risk = abs(entry - tr["sl"])
                if risk > 0:
                    reward = (exit_price - entry) if side == "BUY" else (entry - exit_price)
                    rr = round(reward / risk, 3)

            persistence.log_trade_event({
                "event_type": event_type,
                "symbol": symbol,
                "side": side,
                "entry": entry,
                "sl": tr.get("sl"),
                "tp": tr.get("tp"),
                "trend_mode": tr.get("trend_mode", False),
                "exit_price": exit_price,
                "reason": reason,
                "mfe_price": tr.get("mfe"),
                "mae_price": tr.get("mae"),
                "opened_at_ist": opened_at,
                "duration_minutes": duration_minutes,
                "realized_rr": rr,
            })
        except Exception as e:
            logger.error(f"{symbol} | Failed to log trade close event: {e}", exc_info=True)

    async def _reconcile_positions(self):
        try:
            positions = await self.coindcx.get_open_positions()
        except Exception as e:
            logger.error(f"Failed to fetch open positions: {e}", exc_info=True)
            return

        self._open_positions = positions

        # Same reasoning as the main loop: a symbol removed from SYMBOLS
        # (watchlist restructuring) must still be reconciled/closed-out
        # properly if it has an open position or restored level data —
        # otherwise its close would go completely undetected.
        active_symbols = set(SYMBOLS) | set(self._trailing.keys()) | set(positions.keys())
        for symbol in active_symbols:
            level = self.state.get_level(symbol)
            if level and level.in_trade and symbol not in positions:
                reason_line = await self._classify_reconciled_exit(symbol)
                tr = self._trailing.get(symbol)
                if tr:
                    self._log_close(symbol, tr, exit_price=None, reason=reason_line)
                self.state.reset_symbol_watch(symbol)
                self._trailing.pop(symbol, None)
                logger.info(f"{symbol} | Position closed ({reason_line}) — resuming watch for fresh setups")
                await self.telegram.send_alert(
                    f"🔄 *Position Closed*\n\n"
                    f"*Symbol:* {symbol}\n"
                    f"*Likely reason:* {reason_line}\n\n"
                    f"Resuming watch for fresh liquidity setups on this symbol."
                )

            # Orphan check: exchange shows an open position we have no local
            # trailing record for. Happens if the process restarted between
            # order placement and the next periodic save, or (before this
            # fix existed) after any restart at all. We can't reconstruct
            # the original SL/TP/scenario from the exchange alone, so this
            # is a manual-review flag, not an auto-fix.
            if symbol in positions and symbol not in self._trailing:
                if symbol not in self._orphan_alerted:
                    logger.warning(f"{symbol} | Exchange shows an open position with no local "
                                   f"tracking (orphaned after restart?) — flagging for manual review")
                    await self.telegram.send_alert(
                        f"⚠️ *Untracked Open Position*\n\n"
                        f"*Symbol:* {symbol}\n\n"
                        f"CoinDCX shows this position open, but the bot has no local SL/TP/entry "
                        f"record for it (likely a restart between order placement and the last "
                        f"state save). The bot's exit logic will NOT monitor this position until "
                        f"you either close it manually or it hits its exchange-side SL. "
                        f"This alert won't repeat — check CoinDCX when you can."
                    )
                    self._orphan_alerted.add(symbol)
            elif symbol in self._orphan_alerted:
                # Position resolved (closed manually, or trailing was restored) —
                # allow a fresh alert if it somehow becomes orphaned again later.
                self._orphan_alerted.discard(symbol)

    async def _process_symbol(self, symbol: str):
        try:
            candle = await self.coindcx.get_latest_15m_candle(symbol)
            if not candle:
                logger.debug(f"{symbol} | No candle data")
                return

            if self._last_candle_time[symbol] == candle['time']:
                logger.debug(f"{symbol} | Candle already processed")
                return

            prev_candle = self._last_candle.get(symbol)
            self._last_candle_time[symbol] = candle['time']
            self._last_candle[symbol] = candle
            logger.info(f"{symbol} | Candle: O={candle['open']:.4f} H={candle['high']:.4f} "
                       f"L={candle['low']:.4f} C={candle['close']:.4f}")

            if symbol in self._trailing:
                await self._check_exit_conditions(symbol, candle, prev_candle)

            signal = self.engine.process_candle(symbol, candle)
            if signal:
                await self._handle_signal(signal)

        except Exception as e:
            logger.error(f"{symbol} | _process_symbol error: {e}", exc_info=True)

    async def _check_breakeven_move(self, symbol: str, tr: dict):
        """Two-stage SL ratchet based on MFE:
          Stage 1 (BREAKEVEN_STAGE1_R): move SL partway — halfway between the
            original SL and entry — cutting max remaining loss roughly in half.
          Stage 2 (BREAKEVEN_STAGE2_R): move SL the rest of the way to entry.
        Runs for every trade type, including trend-mode. Uses the ORIGINAL
        sl (tr['sl']) for the risk calculation — tr['sl'] is never mutated,
        so R-multiple analytics (_log_close, ladder capping) stay correct
        even after the live stop has moved. tr['live_sl'] is the actual
        current protective price, used for reconciled-exit classification.
        Each stage only ever fires once, and stage 2 can fire directly
        without stage 1 having happened first if price gapped past both
        thresholds in a single candle."""
        entry, sl = tr.get("entry"), tr.get("sl")
        risk = abs(entry - sl) if entry is not None and sl is not None else 0
        if risk <= 0:
            return

        mfe = tr.get("mfe", entry)
        mfe_R = (mfe - entry) / risk if tr["side"] == "BUY" else (entry - mfe) / risk

        # Stage 2 first — if price already moved far enough to skip straight
        # past stage 1, go directly to full breakeven rather than parking at
        # a partial level that's already stale.
        if not tr.get("breakeven_moved") and mfe_R >= BREAKEVEN_STAGE2_R:
            success = await self.coindcx.update_stop_loss(symbol, new_sl_price=entry)
            if success:
                tr["live_sl"] = entry
                tr["breakeven_moved"] = True
                tr["breakeven_stage1_moved"] = True  # stage 1 is superseded, mark done
                logger.info(f"{symbol} | SL moved to full breakeven ({entry:.4f}) — MFE reached "
                           f"{mfe_R:.2f}R (stage 2 trigger: {BREAKEVEN_STAGE2_R}R)")
                await self.telegram.send_alert(
                    f"🛡️ *SL Moved to Breakeven*\n\n"
                    f"*Symbol:* {symbol}\n*New SL:* {entry:.4f} (entry price)\n\n"
                    f"This trade reached +{BREAKEVEN_STAGE2_R}R — worst case from here is now "
                    f"scratch, not a full loss."
                )
            else:
                logger.warning(f"{symbol} | Failed to move SL to full breakeven — will retry "
                               f"next candle if MFE condition still holds")
            return

        if not tr.get("breakeven_stage1_moved") and mfe_R >= BREAKEVEN_STAGE1_R:
            partial_sl = (entry + sl) / 2  # halfway between original SL and entry
            success = await self.coindcx.update_stop_loss(symbol, new_sl_price=partial_sl)
            if success:
                tr["live_sl"] = partial_sl
                tr["breakeven_stage1_moved"] = True
                logger.info(f"{symbol} | SL moved to partial breakeven ({partial_sl:.4f}) — MFE "
                           f"reached {mfe_R:.2f}R (stage 1 trigger: {BREAKEVEN_STAGE1_R}R)")
                await self.telegram.send_alert(
                    f"🛡️ *SL Moved to Partial Breakeven*\n\n"
                    f"*Symbol:* {symbol}\n*New SL:* {partial_sl:.4f} (halfway to entry)\n\n"
                    f"This trade reached +{BREAKEVEN_STAGE1_R}R — remaining risk is now roughly "
                    f"half of the original. Full breakeven locks in at +{BREAKEVEN_STAGE2_R}R."
                )
            else:
                logger.warning(f"{symbol} | Failed to move SL to partial breakeven — will retry "
                               f"next candle if MFE condition still holds")

    async def _check_partial_tp_ladder(self, symbol: str, tr: dict, candle: dict):
        """Checks TP1/TP2 (interior partials, closer than the original single
        target) and executes a partial market close on whichever tier the
        candle has reached, in order. TP3 is NOT handled here — it's the
        original target/rejection/ROE priority logic below, unchanged, which
        closes whatever quantity remains at that point.

        Silently does nothing for trades where the ladder was never seeded
        (tp1_price missing) — that happens when TP1's R-multiple would have
        sat beyond the original single target at entry time, in which case
        _handle_signal deliberately skipped seeding the ladder and this
        trade behaves exactly as it would have before this feature existed."""
        if "tp1_price" not in tr:
            return
        side = tr["side"]

        for tier in (1, 2):
            price_key, filled_key, weight_key = f"tp{tier}_price", f"tp{tier}_filled", f"tp{tier}_weight"
            if tr.get(filled_key):
                continue
            tp_price = tr[price_key]
            hit = (candle['high'] >= tp_price) if side == 'BUY' else (candle['low'] <= tp_price)
            if not hit:
                continue

            try:
                live_positions = await self.coindcx.get_open_positions()
            except Exception as e:
                logger.error(f"{symbol} | Failed to fetch live position for TP{tier} partial close: {e}",
                             exc_info=True)
                return  # try again next candle rather than guessing at quantity

            live_qty = abs(live_positions.get(symbol, 0))
            if live_qty <= 0:
                # Position already fully gone (closed some other way) — nothing to partial-close.
                tr[filled_key] = True
                continue

            # Portion of the CURRENT remaining quantity, not the original
            # entry quantity — this is what keeps it safe against the
            # missing reduce_only guarantee (see coindcx.py comment):
            # always close a fraction of what's actually live right now.
            remaining_weight = sum(tr.get(f"tp{t}_weight", 0) for t in (1, 2, 3)
                                    if not tr.get(f"tp{t}_filled", False))
            tier_weight = tr[weight_key]
            close_qty = live_qty * (tier_weight / remaining_weight) if remaining_weight > 0 else live_qty

            success = await self.coindcx.close_position_market(symbol, side, close_qty)
            if success:
                tr[filled_key] = True
                self._log_close(symbol, tr, exit_price=tp_price, reason=f"tp{tier}_partial",
                                 event_type="partial_close")
                pct_of_position = round(100 * tier_weight, 0)
                await self.telegram.send_alert(
                    f"🎯 *TP{tier} Hit — Partial Close*\n\n"
                    f"*Symbol:* {symbol}\n*Price:* {tp_price:.4f}\n"
                    f"*Closed:* ~{pct_of_position:.0f}% of remaining position\n\n"
                    f"Remainder still running toward "
                    f"{'TP' + str(tier + 1) if tier == 1 and 'tp2_price' in tr and not tr.get('tp2_filled') else 'final target'}."
                )
                logger.info(f"{symbol} | TP{tier} partial close at {tp_price:.4f} (qty {close_qty:.6f})")
            else:
                logger.error(f"{symbol} | TP{tier} partial close order failed — will retry next candle "
                             f"if price still qualifies")
                return  # don't mark filled; try again next candle

    async def _check_exit_conditions(self, symbol: str, candle: dict, prev_candle: Optional[dict]):
        tr = self._trailing.get(symbol)
        if not tr:
            return
        level = self.state.get_level(symbol)
        if not level:
            return

        side = tr["side"]

        # Track max favorable / max adverse excursion every candle, regardless
        # of exit priority outcome below — this is what makes the "if I'd
        # held longer" TP analysis possible without re-deriving it from raw
        # candle logs after the fact.
        if side == 'BUY':
            tr["mfe"] = max(tr.get("mfe", tr["entry"]), candle['high'])
            tr["mae"] = min(tr.get("mae", tr["entry"]), candle['low'])
        else:
            tr["mfe"] = min(tr.get("mfe", tr["entry"]), candle['low'])
            tr["mae"] = max(tr.get("mae", tr["entry"]), candle['high'])

        target = level.pdh if side == 'BUY' else level.pdl
        skip_target_priorities = tr.get("trend_mode", False)
        details = None  # fetched at most once per candle, reused across priorities

        # Runs regardless of trend_mode — this is the fix for the "worked
        # then reversed" loss pattern, and trend-mode trades (which skip the
        # target/rejection priorities entirely) need this protection even
        # more, since they otherwise only have SL + ROE.
        await self._check_breakeven_move(symbol, tr)

        if not skip_target_priorities:
            await self._check_partial_tp_ladder(symbol, tr, candle)

            if side == 'BUY' and candle['high'] >= target:
                await self._exit_position(symbol, tr, exit_price=target, reason="target_achieved",
                                           label="Take Profit – Target Achieved")
                return
            if side == 'SELL' and candle['low'] <= target:
                await self._exit_position(symbol, tr, exit_price=target, reason="target_achieved",
                                           label="Take Profit – Target Achieved")
                return

            near_target = (candle['high'] >= target * (1 - REJECTION_PROXIMITY_PCT) if side == 'BUY'
                           else candle['low'] <= target * (1 + REJECTION_PROXIMITY_PCT))

            if near_target:
                rejection = False
                if side == 'BUY':
                    rejection = _is_bearish_rejection(candle) or (
                        prev_candle is not None and _is_bearish_engulfing(prev_candle, candle))
                else:
                    rejection = _is_bullish_rejection(candle) or (
                        prev_candle is not None and _is_bullish_engulfing(prev_candle, candle))

                # Only fire as a profit-taking exit if the close is actually
                # favorable versus entry — otherwise it's just a rejection
                # candle near the target while underwater, not a real "Take
                # Profit" event. Fall through to ROE/stop-loss instead.
                is_profitable = (candle['close'] > tr['entry'] if side == 'BUY'
                                  else candle['close'] < tr['entry'])

                if rejection and is_profitable:
                    # Additionally require a meaningful ROE before allowing
                    # this early exit — see MIN_ROE_FOR_REJECTION_EXIT_PCT
                    # comment above. A marginal-profit rejection candle no
                    # longer bails out of a trade that might still run.
                    details = await self.coindcx.get_position_details(symbol)
                    roe = details.get("roe") if details else None
                    if roe is not None and roe >= MIN_ROE_FOR_REJECTION_EXIT_PCT:
                        await self._exit_position(symbol, tr, exit_price=candle['close'],
                                                   reason="rejection_exit",
                                                   label="Take Profit – Rejection Exit")
                        return
                    else:
                        logger.info(f"{symbol} | Rejection-exit conditions met but ROE "
                                   f"({roe if roe is not None else 'unknown'}%) is below the "
                                   f"{MIN_ROE_FOR_REJECTION_EXIT_PCT}% minimum — holding for more "
                                   f"confirmation instead of exiting early")

        if details is None:
            details = await self.coindcx.get_position_details(symbol)
        if details and details.get("roe") is not None and details["roe"] >= ROE_TARGET_PCT:
            await self._exit_position(symbol, tr, exit_price=candle['close'],
                                       reason="roe_protection", label="Take Profit – ROE Protection",
                                       roe=details["roe"])
            return

    async def _exit_position(self, symbol: str, tr: dict, exit_price: float, reason: str,
                              label: str, roe: Optional[float] = None):
        side = tr["side"]

        try:
            live_positions = await self.coindcx.get_open_positions()
        except Exception as e:
            logger.error(f"{symbol} | Failed to fetch live position before close: {e}", exc_info=True)
            live_positions = {}

        quantity = abs(live_positions.get(symbol, 0))

        if quantity <= 0:
            logger.warning(f"{symbol} | No live position found on exchange at exit time "
                           f"(reason={reason}) — skipping close call, resetting local state only")
            self._log_close(symbol, tr, exit_price=None, reason=f"{reason}_already_closed")
            self.state.reset_symbol_watch(symbol)
            self._trailing.pop(symbol, None)
            self._open_positions.pop(symbol, None)
            return

        success = await self.coindcx.close_position_market(symbol, side, quantity)

        roe_line = f"\n*ROE:* {roe:.2f}%" if roe is not None else ""
        if success:
            await self.telegram.send_alert(
                f"✅ *{label}*\n\n"
                f"*Symbol:* {symbol}\n"
                f"*Side:* {'📈 LONG' if side == 'BUY' else '📉 SHORT'}\n"
                f"*Entry:* {tr['entry']:.4f}\n"
                f"*Exit:* {exit_price:.4f}{roe_line}\n\n"
                f"Position closed automatically."
            )
            logger.info(f"{symbol} | Exited via {reason} at {exit_price:.4f} (qty {quantity})")
        else:
            await self.telegram.send_alert(
                f"⚠️ *{label} — Close Failed*\n\n"
                f"*Symbol:* {symbol}\n"
                f"Tried to close automatically but the exchange call failed. "
                f"Please check and close manually on CoinDCX."
            )
            logger.error(f"{symbol} | Failed to auto-close on {reason} trigger")

        self._log_close(symbol, tr, exit_price=exit_price, reason=reason)
        self.state.reset_symbol_watch(symbol)
        self._trailing.pop(symbol, None)
        self._open_positions.pop(symbol, None)

    async def _handle_signal(self, signal: Signal):
        symbol = signal.symbol
        level = self.state.get_level(symbol)

        if level and level.auto_traded_today:
            logger.info(f"{symbol} | Fresh {signal.side} setup, but today's auto-trade "
                       f"already used — alert only")
            await self.telegram.send_alert(
                f"⚠️ *New Liquidity Setup Detected*\n\n"
                f"*Symbol:* {symbol}\n"
                f"*Time:* {datetime.now(IST).strftime('%H:%M IST')}\n"
                f"*Direction:* {'📈 LONG' if signal.side == 'BUY' else '📉 SHORT'}\n"
                f"*Entry:* {signal.entry_price:.4f}\n"
                f"*SL:* {signal.sl_price:.4f}\n"
                f"*Level swept:* {signal.swept_level:.4f}\n\n"
                f"*Reason:* This symbol's one automatic trade for today has already been used.\n\n"
                f"No auto-entry executed. Review and enter manually on CoinDCX if you agree."
            )
            return

        if level.counter_trend_confirms >= STABILITY_MAX_COUNTER_CONFIRMS:
            logger.info(f"{symbol} | {signal.side} setup — {level.counter_trend_confirms} "
                       f"counter-trend confirmations already seen today, day's "
                       f"{level.trend_bias} classification no longer trusted — alert only")
            await self.telegram.send_alert(
                f"⚠️ *New Liquidity Setup Detected*\n\n"
                f"*Symbol:* {symbol}\n*Direction:* {'📈 LONG' if signal.side=='BUY' else '📉 SHORT'}\n"
                f"*Entry:* {signal.entry_price:.4f}\n*SL:* {signal.sl_price:.4f}\n"
                f"*Level swept:* {signal.swept_level:.4f}\n\n"
                f"*Reason:* This symbol has had {level.counter_trend_confirms} counter-trend "
                f"confirmations today — today's {level.trend_bias} bias is no longer treated "
                f"as reliable. No auto-entry executed."
            )
            return

        if signal.counter_trend:
            logger.info(f"{symbol} | {signal.side} setup fights today's {level.trend_bias} "
                       f"bias — alert only")
            await self.telegram.send_alert(
                f"⚠️ *New Liquidity Setup Detected*\n\n"
                f"*Symbol:* {symbol}\n*Direction:* {'📈 LONG' if signal.side=='BUY' else '📉 SHORT'}\n"
                f"*Entry:* {signal.entry_price:.4f}\n*SL:* {signal.sl_price:.4f}\n"
                f"*Level swept:* {signal.swept_level:.4f}\n\n"
                f"*Reason:* Today's bias is {level.trend_bias} — this setup fights that bias. "
                f"No auto-entry executed."
            )
            return

        regime = self.state.get_regime()
        btc_counter = (signal.side == 'BUY' and regime == 'BEARISH') or \
                      (signal.side == 'SELL' and regime == 'BULLISH')
        if btc_counter:
            logger.info(f"{symbol} | {signal.side} setup — BTC regime is {regime} "
                       f"(informational only, proceeding with entry)")

        async with self._position_lock:
            open_count = len(self._open_positions)
            if open_count >= MAX_CONCURRENT_POSITIONS:
                logger.info(f"SKIPPED {symbol} — {open_count}/{MAX_CONCURRENT_POSITIONS} positions already open")
                await self.telegram.send_alert(
                    f"⏭️ *Setup Skipped* — Max concurrent positions reached ({open_count}/{MAX_CONCURRENT_POSITIONS})\n\n"
                    f"*Symbol:* {symbol}\n*Side:* {signal.side}\n*Pattern:* {signal.pattern}\n"
                    f"*Would-be Entry:* {signal.entry_price:.4f}\n*Would-be SL:* {signal.sl_price:.4f}\n"
                    f"*Level swept:* {signal.swept_level:.4f}"
                )
                return
            self._open_positions[symbol] = 0

        trend_line = f"\n*Trend bias:* {level.trend_bias}" if level.trend_bias != "NONE" else ""

        try:
            margin_usd = float(os.getenv('TRADE_SIZE_USD', 40))

            # Margin cap — internally tracked, does NOT depend on the broken
            # CoinDCX wallet-balance endpoint. Set TOTAL_ACCOUNT_MARGIN_USD in
            # Railway env vars to enable; unset or 0 disables this check.
            total_margin_cap = float(os.getenv('TOTAL_ACCOUNT_MARGIN_USD', 0))
            if total_margin_cap > 0:
                committed_margin = len(self._open_positions) * margin_usd
                if committed_margin > total_margin_cap:
                    logger.warning(f"{symbol} | Skipping order — would exceed configured "
                                   f"margin cap (committed ${committed_margin:.2f} > "
                                   f"cap ${total_margin_cap:.2f})")
                    await self.telegram.send_alert(
                        f"⚠️ *Order Skipped — Margin Cap Reached*\n\n"
                        f"*Symbol:* {symbol}\n*Side:* {'📈 LONG' if signal.side == 'BUY' else '📉 SHORT'}\n"
                        f"*Committed if opened:* ${committed_margin:.2f}\n"
                        f"*Configured cap:* ${total_margin_cap:.2f}\n\n"
                        f"Setup was valid but would exceed your configured TOTAL_ACCOUNT_MARGIN_USD."
                    )
                    self._open_positions.pop(symbol, None)
                    return

            level_line = (f"*Level swept:* {signal.swept_level:.4f} (dynamic re-anchor, "
                          f"not the fixed daily PDH/PDL below)\n*Fixed PDH:* {signal.pdh:.4f} | "
                          f"*Fixed PDL:* {signal.pdl:.4f}"
                          if signal.trend_mode else
                          f"*PDH:* {signal.pdh:.4f} | *PDL:* {signal.pdl:.4f}")

            await self.telegram.send_alert(
                f"🔍 *Setup Detected*\n\n"
                f"*Symbol:* {symbol}\n*Side:* {'📈 LONG' if signal.side == 'BUY' else '📉 SHORT'}\n"
                f"*Pattern:* {signal.pattern}{' (trend-aligned flip)' if signal.trend_mode else ''}\n"
                f"*Entry:* {signal.entry_price:.4f}\n*SL:* {signal.sl_price:.4f}\n"
                f"{level_line}{trend_line}\n\n⏳ Placing order..."
            )

            quantity = (margin_usd * TRADE_LEVERAGE) / signal.entry_price

            order_result = await self.coindcx.place_market_order(
                symbol=symbol, side=signal.side, quantity=quantity,
                sl_price=signal.sl_price, leverage=TRADE_LEVERAGE
            )

            if order_result and order_result.get('id'):
                order_id = order_result.get('id', 'N/A')
                quantity_filled = order_result.get('quantity', quantity)

                record = TradeRecord(
                    symbol=symbol, side=signal.side, entry_price=signal.entry_price,
                    sl_price=signal.sl_price, order_id=order_id, scenario=signal.scenario,
                    timestamp=datetime.now(IST).isoformat()
                )
                self.state.register_trade(record)
                self.state.mark_in_trade(symbol)
                self.state.mark_auto_traded(symbol)

                tp_price = signal.pdh if signal.side == 'BUY' else signal.pdl
                opened_at_ist = datetime.now(IST).isoformat()
                risk = abs(signal.entry_price - signal.sl_price)

                tr = {"side": signal.side, "entry": signal.entry_price,
                      "sl": signal.sl_price, "live_sl": signal.sl_price,
                      "breakeven_moved": False,
                      "breakeven_stage1_moved": False,
                      "tp": tp_price,
                      "trend_mode": signal.trend_mode,
                      "opened_at": opened_at_ist,
                      "mfe": signal.entry_price, "mae": signal.entry_price}

                # Seed the TP1/TP2 partial ladder — only for non-trend-mode
                # trades, and only if each tier's R-multiple sits strictly
                # closer than the original single target (tp_price). If TP1
                # would already be beyond that target, skip the ladder
                # entirely for this trade — it behaves exactly as before.
                if not signal.trend_mode and risk > 0:
                    candidate_prices = []
                    for r in TP_LADDER_R:
                        p = (signal.entry_price + r * risk if signal.side == 'BUY'
                             else signal.entry_price - r * risk)
                        candidate_prices.append(p)
                    within_target = all(
                        (p <= tp_price if signal.side == 'BUY' else p >= tp_price)
                        for p in candidate_prices
                    )
                    if within_target:
                        tr["tp1_price"] = candidate_prices[0]
                        tr["tp2_price"] = candidate_prices[1]
                        tr["tp1_filled"] = False
                        tr["tp2_filled"] = False
                        tr["tp1_weight"], tr["tp2_weight"], tr["tp3_weight"] = TP_LADDER_WEIGHTS

                self._trailing[symbol] = tr
                self._open_positions[symbol] = quantity_filled
                persistence.log_trade_event({
                    "event_type": "open", "symbol": symbol, "side": signal.side,
                    "entry": signal.entry_price, "sl": signal.sl_price, "tp": tp_price,
                    "trend_mode": signal.trend_mode, "scenario": signal.scenario,
                    "order_id": order_id, "opened_at_ist": opened_at_ist,
                })
                # Snapshot immediately after opening rather than waiting for the
                # next periodic save — a restart in the seconds right after
                # entry is exactly the window the orphan-check above exists for.
                persistence.save_state(self.state, self._trailing)

                tp_set = False
                if not signal.trend_mode:
                    for attempt in range(3):
                        if attempt > 0:
                            await asyncio.sleep(1.5)
                        tp_set = await self.coindcx.update_position_tpsl(symbol, tp_price=tp_price)
                        if tp_set:
                            break
                        logger.warning(f"{symbol} | TP set attempt {attempt + 1}/3 failed — "
                                       f"position may not be registered on the exchange yet")

                if signal.trend_mode:
                    tp_note = ("🎯 Trend-aligned trade — no fixed resting TP set (target would sit "
                              "behind entry). Exit runs on stop-loss + ROE protection only.")
                    tp_lines = ""
                    rr_lines = ""
                else:
                    tp_note = (f"🎯 Resting take-profit (dead-man's-switch) set at {tp_price:.4f} — "
                              f"fires if the bot itself ever goes down before managing exits."
                              if tp_set else
                              f"⚠️ Could not set the resting take-profit safety order — the bot's own "
                              f"candle-by-candle logic will still enforce all exits, but there's no "
                              f"exchange-side backstop if the bot is down. Check CoinDCX manually if concerned.")

                    if "tp1_price" in tr:
                        tp_lines = (f"*TP1:* {tr['tp1_price']:.4f} (34%)\n"
                                   f"*TP2:* {tr['tp2_price']:.4f} (33%)\n"
                                   f"*TP3:* {tp_price:.4f} (33%, final target)\n")
                        rr_lines = (f"*Expected RR:*\n"
                                   f"1:{TP_LADDER_R[0]}\n1:{TP_LADDER_R[1]}\n"
                                   f"1:{round(abs(tp_price - signal.entry_price) / risk, 1) if risk else '—'}\n")
                    else:
                        tp_lines = f"*TP:* {tp_price:.4f} (single target — TP1/TP2 would've sat beyond it)\n"
                        rr_lines = (f"*Expected RR:* 1:{round(abs(tp_price - signal.entry_price) / risk, 1)}\n"
                                   if risk else "")

                pattern_label = "trend-aligned flip setups (all symbols)" if signal.trend_mode \
                                 else "sweep-reversal setups (all symbols)"
                stats = persistence.get_pattern_stats(trend_mode=signal.trend_mode)
                if stats["has_enough_data"]:
                    confidence = ("HIGH" if stats["win_rate_pct"] >= 60 else
                                  "MEDIUM" if stats["win_rate_pct"] >= 45 else "LOW")
                    stats_block = (
                        f"*Confidence:* {confidence} _(heuristic from historical win rate — not a guarantee)_\n"
                        f"*Historical Win Rate:* {stats['win_rate_pct']:.0f}% "
                        f"({stats['count']} {pattern_label})\n"
                        + (f"*Avg Hold Time:* {stats['avg_hold_minutes']:.0f} min\n" if stats['avg_hold_minutes'] else "")
                        + f"\nThis pattern has occurred {stats['count']} times across all symbols.\n"
                        + (f"Average move: {stats['avg_move_pct']:+.2f}%\n" if stats['avg_move_pct'] is not None else "")
                        + (f"Largest move: {stats['largest_move_pct']:+.2f}%\n" if stats['largest_move_pct'] is not None else "")
                    )
                else:
                    n = stats["count"]
                    stats_block = (
                        f"*Confidence:* N/A — not enough history yet ({n}/{MIN_TRADES_FOR_HISTORICAL_STATS} "
                        f"{pattern_label} logged)\n"
                        f"*Historical Win Rate:* N/A — will show once {MIN_TRADES_FOR_HISTORICAL_STATS}+ "
                        f"trades of this pattern are logged\n"
                    )

                msg = (
                    f"✅ *Trade Executed*\n\n"
                    f"*Symbol:* {symbol}\n*Side:* {'📈 LONG' if signal.side == 'BUY' else '📉 SHORT'}\n"
                    f"*Entry:* {signal.entry_price:.4f}\n*SL:* {signal.sl_price:.4f}\n"
                    f"{tp_lines}{rr_lines}\n"
                    f"{stats_block}\n"
                    f"*Margin:* ${margin_usd:.2f}\n*Leverage:* {TRADE_LEVERAGE}x\n"
                    f"*Exposure:* ${margin_usd * TRADE_LEVERAGE:.2f}\n*Quantity:* {quantity_filled}\n"
                    f"*Order ID:* `{order_id}`\n*Open positions:* {len(self._open_positions)}/{MAX_CONCURRENT_POSITIONS}\n\n"
                    f"{tp_note}\n\n"
                    f"🔄 Today's auto-trade for this symbol has now been used — further setups will be alert-only."
                )
                await self.telegram.send_alert(msg)
                logger.info(f"Trade executed: {record}")

            else:
                error_msg = order_result.get('error', 'Unknown') if order_result else 'No response from API'
                logger.error(f"{symbol} | Order failed: {error_msg}")
                await self.telegram.send_alert(
                    f"❌ *Order Failed*\n\n*Symbol:* {symbol}\n*Side:* {signal.side}\n"
                    f"*Error:* {_escape_md(error_msg)}\n\n⚠️ Manual intervention may be required."
                )
                self._open_positions.pop(symbol, None)

        except Exception as e:
            logger.error(f"{symbol} | Order exception: {e}", exc_info=True)
            await self.telegram.send_alert(
                f"❌ *Order Exception*\n\n*Symbol:* {symbol}\n*Error:* {_escape_md(e)}\n\n"
                f"⚠️ Manual intervention required."
            )
            self._open_positions.pop(symbol, None)
