"""
MarketMonitor — continuously polls 15-minute candles and routes signals to execution.
Runs all 7 days a week, from 5:30 AM to 11:00 PM IST.

Event-driven design: rather than a fixed daily trade count, the bot checks
CoinDCX's live open positions before every new trade (max 2 concurrent,
across all symbols). It also periodically reconciles which locally
"in_trade" symbols have actually closed on the exchange, and resets them
to start a completely fresh watch cycle.

Rule 3 — one automatic trade per symbol per day: once a symbol has had
one auto-placed trade today, any further clean setup on that symbol is
sent as an alert only, never auto-placed, regardless of how that first
trade closes (win or SL).
"""

import asyncio
from datetime import datetime, time as dtime
import pytz
import os

from core.state import BotState, SYMBOLS, TradeRecord
from core.strategy import StrategyEngine, Signal
from exchange.coindcx import CoinDCXClient
from notifications.telegram import TelegramBot
from utils.logger import setup_logger
logger = setup_logger("monitor")
IST = pytz.timezone("Asia/Kolkata")

POLL_INTERVAL_SECONDS = 15
DAY_END_HOUR = 23
DAY_END_MINUTE = 0

# Margin per trade in USD. This is MARGIN, not final position value —
# actual exposure = TRADE_MARGIN_USD * LEVERAGE.
TRADE_LEVERAGE = 5

# Max number of concurrent open positions across all symbols, checked live
# against the exchange before every new trade.
MAX_CONCURRENT_POSITIONS = 2

# Breakeven trailing: once a position's favorable move reaches this
# fraction of its own initial risk (entry-to-SL distance, "R"), the SL is
# moved to breakeven and held there — no further trailing beyond that.
BREAKEVEN_TRIGGER_R = 0.5

# Pure awareness, zero financial risk: if price gets within this % of
# PDH/PDL without actually breaking it, send one informational alert per
# symbol per side per day. Purely to give visibility into "noise" near
# the level — never triggers a trade either way. Safe to tune freely
# since nothing downstream depends on this number.
NEAR_LEVEL_THRESHOLD_PCT = 0.0015


def _escape_md(text) -> str:
    """
    Escape characters that Telegram's legacy Markdown parser treats as
    formatting (_, *, `, [) before embedding externally-sourced text
    (API error messages, exception text) into an alert. Our own literal
    template wording is fine as-is — this is only for text we didn't
    write ourselves, which could contain an unmatched special character
    and silently break delivery of the entire message.
    """
    text = str(text)
    for ch in ('_', '*', '`', '['):
        text = text.replace(ch, '\\' + ch)
    return text


class MarketMonitor:
    def __init__(self, coindcx: CoinDCXClient, engine: StrategyEngine,
                 state: BotState, telegram: TelegramBot):
        self.coindcx = coindcx
        self.engine = engine
        self.state = state
        self.telegram = telegram
        self._last_candle_time = {sym: None for sym in SYMBOLS}
        self._open_positions: dict = {}  # {symbol: active_pos}, refreshed each cycle
        self._trailing: dict = {}  # {symbol: {side, entry, initial_sl, peak, breakeven_done}}

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

                tasks = [self._process_symbol(sym) for sym in SYMBOLS]
                await asyncio.gather(*tasks, return_exceptions=True)

            except Exception as e:
                logger.error(f"Monitor loop error: {e}", exc_info=True)

            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def _reconcile_positions(self):
        """
        Refresh live open positions from the exchange. Any symbol currently
        marked in_trade locally, but no longer showing an open position on
        the exchange, gets reset to start a fresh watch cycle.
        """
        try:
            positions = await self.coindcx.get_open_positions()
        except Exception as e:
            logger.error(f"Failed to fetch open positions: {e}", exc_info=True)
            return  # keep last known state rather than assume anything

        self._open_positions = positions

        for symbol in SYMBOLS:
            level = self.state.get_level(symbol)
            if level and level.in_trade and symbol not in positions:
                self.state.reset_symbol_watch(symbol)
                self._trailing.pop(symbol, None)
                logger.info(f"{symbol} | Position closed — resuming watch for fresh setups")
                await self.telegram.send_alert(
                    f"🔄 *Position Closed*\n\n"
                    f"*Symbol:* {symbol}\n"
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

            self._last_candle_time[symbol] = candle['time']
            logger.info(f"{symbol} | Candle: O={candle['open']:.4f} H={candle['high']:.4f} L={candle['low']:.4f} C={candle['close']:.4f}")

            await self._check_near_miss(symbol, candle)

            if symbol in self._trailing:
                await self._check_trailing_stop(symbol, candle)

            signal = self.engine.process_candle(symbol, candle)
            if signal:
                await self._handle_signal(signal)

        except Exception as e:
            logger.error(f"{symbol} | _process_symbol error: {e}", exc_info=True)

    async def _check_near_miss(self, symbol: str, candle: dict):
        """
        Pure awareness — never affects trading. If price gets close to
        PDH/PDL without actually breaking it, send one alert per symbol
        per side per day so there's visibility into "noise" near the
        level. Only checked while no sweep is currently being tracked on
        that side.
        """
        level = self.state.get_level(symbol)
        if not level:
            return

        if (level.pdh_state == "NONE" and not level.pdh_near_alerted
                and candle['high'] <= level.pdh
                and candle['high'] >= level.pdh * (1 - NEAR_LEVEL_THRESHOLD_PCT)):
            level.pdh_near_alerted = True
            logger.info(f"{symbol} | Near miss: high {candle['high']:.4f} close to "
                       f"PDH {level.pdh:.4f} without breaking it")
            await self.telegram.send_alert(
                f"ℹ️ *Near PDH, No Break*\n\n"
                f"*Symbol:* {symbol}\n"
                f"*Candle High:* {candle['high']:.4f}\n"
                f"*PDH:* {level.pdh:.4f}\n\n"
                f"Price touched close to PDH without actually breaking it. "
                f"No sweep registered, no trade — just for your awareness."
            )

        if (level.pdl_state == "NONE" and not level.pdl_near_alerted
                and candle['low'] >= level.pdl
                and candle['low'] <= level.pdl * (1 + NEAR_LEVEL_THRESHOLD_PCT)):
            level.pdl_near_alerted = True
            logger.info(f"{symbol} | Near miss: low {candle['low']:.4f} close to "
                       f"PDL {level.pdl:.4f} without breaking it")
            await self.telegram.send_alert(
                f"ℹ️ *Near PDL, No Break*\n\n"
                f"*Symbol:* {symbol}\n"
                f"*Candle Low:* {candle['low']:.4f}\n"
                f"*PDL:* {level.pdl:.4f}\n\n"
                f"Price touched close to PDL without actually breaking it. "
                f"No sweep registered, no trade — just for your awareness."
            )

    async def _check_trailing_stop(self, symbol: str, candle: dict):
        """
        Breakeven-then-hold trailing: once the position's favorable move
        reaches BREAKEVEN_TRIGGER_R times its own initial risk, move the
        SL to entry price and stop — no further trailing after that.
        """
        tr = self._trailing.get(symbol)
        if not tr or tr["breakeven_done"]:
            return

        entry = tr["entry"]
        initial_sl = tr["initial_sl"]
        r = abs(entry - initial_sl)
        if r <= 0:
            return

        if tr["side"] == "BUY":
            tr["peak"] = max(tr["peak"], candle["high"])
            trigger_price = entry + BREAKEVEN_TRIGGER_R * r
            reached = tr["peak"] >= trigger_price
        else:
            tr["peak"] = min(tr["peak"], candle["low"])
            trigger_price = entry - BREAKEVEN_TRIGGER_R * r
            reached = tr["peak"] <= trigger_price

        if reached:
            logger.info(f"{symbol} | Breakeven trigger hit (peak {tr['peak']:.4f} vs "
                       f"target {trigger_price:.4f}) — moving SL to entry {entry:.4f}")
            success = await self.coindcx.update_stop_loss(symbol, entry)
            tr["breakeven_done"] = True  # don't retry every cycle even if it failed

            if success:
                await self.telegram.send_alert(
                    f"🔒 *Breakeven Triggered*\n\n"
                    f"*Symbol:* {symbol}\n"
                    f"*New SL:* {entry:.4f} (breakeven)\n\n"
                    f"Position can no longer close at a loss from here."
                )
            else:
                await self.telegram.send_alert(
                    f"⚠️ *Breakeven Update Failed*\n\n"
                    f"*Symbol:* {symbol}\n"
                    f"Tried to move SL to {entry:.4f} but the exchange call failed. "
                    f"Original SL ({initial_sl:.4f}) is still in place — check manually."
                )

    async def _handle_signal(self, signal: Signal):
        symbol = signal.symbol
        level = self.state.get_level(symbol)

        # Rule 3 — one automatic trade per symbol per day. If this symbol
        # has already had its one auto-trade today, every further clean
        # setup is alert-only, never auto-placed.
        if level and level.auto_traded_today:
            logger.info(f"{symbol} | Fresh {signal.side} setup detected, but this symbol's "
                       f"one auto-trade for today has already been used — alert only")
            await self.telegram.send_alert(
                f"⚠️ *New Liquidity Setup Detected*\n\n"
                f"*Symbol:* {symbol}\n"
                f"*Time:* {datetime.now(IST).strftime('%H:%M IST')}\n"
                f"*Direction:* {'📈 LONG' if signal.side == 'BUY' else '📉 SHORT'}\n"
                f"*Entry:* {signal.entry_price:.4f}\n"
                f"*SL:* {signal.sl_price:.4f}\n\n"
                f"*Reason:* Fresh {'buy' if signal.side == 'BUY' else 'sell'}-side liquidity "
                f"sweep detected after this symbol's automatic trade for today has already "
                f"been used.\n\n"
                f"No auto-entry executed as per the one-trade-per-symbol-per-day rule. "
                f"Review and enter manually on CoinDCX if you agree with this setup."
            )
            return

        open_count = len(self._open_positions)

        if open_count >= MAX_CONCURRENT_POSITIONS:
            logger.info(f"SKIPPED {symbol} — {open_count}/{MAX_CONCURRENT_POSITIONS} "
                       f"positions already open")
            await self.telegram.send_alert(
                f"⏭️ *Setup Skipped* — Max concurrent positions reached ({open_count}/{MAX_CONCURRENT_POSITIONS})\n\n"
                f"*Symbol:* {symbol}\n"
                f"*Side:* {signal.side}\n"
                f"*Pattern:* {signal.pattern}\n"
                f"*Would-be Entry:* {signal.entry_price:.4f}\n"
                f"*Would-be SL:* {signal.sl_price:.4f}"
            )
            return

        await self.telegram.send_alert(
            f"🔍 *Setup Detected*\n\n"
            f"*Symbol:* {symbol}\n"
            f"*Side:* {'📈 LONG' if signal.side == 'BUY' else '📉 SHORT'}\n"
            f"*Pattern:* {signal.pattern}\n"
            f"*Entry:* {signal.entry_price:.4f}\n"
            f"*SL:* {signal.sl_price:.4f}\n"
            f"*PDH:* {signal.pdh:.4f} | *PDL:* {signal.pdl:.4f}\n\n"
            f"⏳ Placing order..."
        )

        try:
            margin_usd = float(os.getenv('TRADE_SIZE_USD', 30))
            # TRADE_SIZE_USD is MARGIN, not final position value.
            # Actual exposure = margin * leverage.
            quantity = (margin_usd * TRADE_LEVERAGE) / signal.entry_price

            order_result = await self.coindcx.place_market_order(
                symbol=symbol,
                side=signal.side,
                quantity=quantity,
                sl_price=signal.sl_price,
                leverage=TRADE_LEVERAGE
            )

            if order_result and order_result.get('id'):
                order_id = order_result.get('id', 'N/A')
                quantity_filled = order_result.get('quantity', quantity)

                record = TradeRecord(
                    symbol=symbol,
                    side=signal.side,
                    entry_price=signal.entry_price,
                    sl_price=signal.sl_price,
                    order_id=order_id,
                    scenario=signal.scenario,
                    timestamp=datetime.now(IST).isoformat()
                )
                self.state.register_trade(record)
                self.state.mark_in_trade(symbol)
                self.state.mark_auto_traded(symbol)
                self._trailing[symbol] = {
                    "side": signal.side,
                    "entry": signal.entry_price,
                    "initial_sl": signal.sl_price,
                    "peak": signal.entry_price,
                    "breakeven_done": False,
                }
                # Optimistically reflect the new position immediately so a
                # second signal in the same poll cycle doesn't overshoot the
                # cap before the next reconciliation refreshes it for real.
                self._open_positions[symbol] = quantity_filled

                msg = (
                    f"✅ *Trade Executed*\n\n"
                    f"*Symbol:* {symbol}\n"
                    f"*Side:* {'📈 LONG' if signal.side == 'BUY' else '📉 SHORT'}\n"
                    f"*Entry:* {signal.entry_price:.4f}\n"
                    f"*SL:* {signal.sl_price:.4f}\n"
                    f"*Margin:* ${margin_usd:.2f}\n"
                    f"*Leverage:* {TRADE_LEVERAGE}x\n"
                    f"*Exposure:* ${margin_usd * TRADE_LEVERAGE:.2f}\n"
                    f"*Quantity:* {quantity_filled}\n"
                    f"*Order ID:* `{order_id}`\n"
                    f"*Open positions:* {len(self._open_positions)}/{MAX_CONCURRENT_POSITIONS}\n\n"
                    f"⚠️ TP is manual — monitor your position.\n"
                    f"✅ SL is built-in — automatically triggered.\n"
                    f"🔄 This symbol will resume watching once the position closes, but today's "
                    f"auto-trade for this symbol has now been used — further setups will be alert-only."
                )

                await self.telegram.send_alert(msg)
                logger.info(f"Trade executed: {record}")

            else:
                error_msg = order_result.get('error', 'Unknown') if order_result else 'No response from API'
                logger.error(f"{symbol} | Order failed: {error_msg}")
                await self.telegram.send_alert(
                    f"❌ *Order Failed*\n\n"
                    f"*Symbol:* {symbol}\n"
                    f"*Side:* {signal.side}\n"
                    f"*Error:* {_escape_md(error_msg)}\n\n"
                    f"⚠️ Manual intervention may be required. This setup was NOT "
                    f"marked in-trade — the bot will keep watching for fresh sweeps."
                )

        except Exception as e:
            logger.error(f"{symbol} | Order exception: {e}", exc_info=True)
            await self.telegram.send_alert(
                f"❌ *Order Exception*\n\n"
                f"*Symbol:* {symbol}\n"
                f"*Error:* {_escape_md(e)}\n\n"
                f"⚠️ Manual intervention required. This setup was NOT marked "
                f"in-trade — the bot will keep watching for fresh sweeps."
            )
