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
from utils.logger import setup_logger
logger = setup_logger("monitor")
IST = pytz.timezone("Asia/Kolkata")

POLL_INTERVAL_SECONDS = 15
DAY_END_HOUR = 23
DAY_END_MINUTE = 0

TRADE_LEVERAGE = 10
MAX_CONCURRENT_POSITIONS = 2
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

                tasks = [self._process_symbol(sym) for sym in SYMBOLS]
                await asyncio.gather(*tasks, return_exceptions=True)

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
        sl = tr.get("sl")
        tp = tr.get("tp")
        tolerance = 0.005

        if sl and abs(price - sl) / sl <= tolerance:
            return f"Stop Loss hit (fill ~{price:.4f} vs SL {sl:.4f})"
        if tp and abs(price - tp) / tp <= tolerance:
            return f"Take Profit hit (fill ~{price:.4f} vs TP {tp:.4f})"
        return f"unclear — fill ~{price:.4f} (entry {tr.get('entry')}, SL {sl}, TP {tp})"

    async def _reconcile_positions(self):
        try:
            positions = await self.coindcx.get_open_positions()
        except Exception as e:
            logger.error(f"Failed to fetch open positions: {e}", exc_info=True)
            return

        self._open_positions = positions

        for symbol in SYMBOLS:
            level = self.state.get_level(symbol)
            if level and level.in_trade and symbol not in positions:
                reason_line = await self._classify_reconciled_exit(symbol)
                self.state.reset_symbol_watch(symbol)
                self._trailing.pop(symbol, None)
                logger.info(f"{symbol} | Position closed ({reason_line}) — resuming watch for fresh setups")
                await self.telegram.send_alert(
                    f"🔄 *Position Closed*\n\n"
                    f"*Symbol:* {symbol}\n"
                    f"*Likely reason:* {reason_line}\n\n"
                    f"Resuming watch for fresh liquidity setups on this symbol."
                )

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

    async def _check_exit_conditions(self, symbol: str, candle: dict, prev_candle: Optional[dict]):
        tr = self._trailing.get(symbol)
        if not tr:
            return
        level = self.state.get_level(symbol)
        if not level:
            return

        side = tr["side"]
        target = level.pdh if side == 'BUY' else level.pdl
        skip_target_priorities = tr.get("trend_mode", False)
        details = None  # fetched at most once per candle, reused across priorities

        if not skip_target_priorities:
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
                self._trailing[symbol] = {"side": signal.side, "entry": signal.entry_price,
                                           "sl": signal.sl_price, "tp": tp_price,
                                           "trend_mode": signal.trend_mode}
                self._open_positions[symbol] = quantity_filled

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
                else:
                    tp_note = (f"🎯 Resting take-profit set at {tp_price:.4f} (Priority 1 target)."
                              if tp_set else
                              f"⚠️ Could not set a resting take-profit order — Priority 1 will still "
                              f"be enforced by the bot on each closed candle, with possible minor "
                              f"slippage. Check CoinDCX manually if concerned.")

                msg = (
                    f"✅ *Trade Executed*\n\n"
                    f"*Symbol:* {symbol}\n*Side:* {'📈 LONG' if signal.side == 'BUY' else '📉 SHORT'}\n"
                    f"*Entry:* {signal.entry_price:.4f}\n*SL:* {signal.sl_price:.4f}\n"
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
