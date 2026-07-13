"""
MarketMonitor — continuously polls 15-minute candles and routes signals to execution.
Runs all 7 days a week, from 5:30 AM to 11:00 PM IST.

Exit hierarchy for open positions, evaluated in strict priority order on
every closed candle — first true condition wins:
  1. Target reached (PDH for LONG / PDL for SHORT)           -> full close
  2. Rejection candle within REJECTION_PROXIMITY_PCT of target -> full close
  3. ROE (CoinDCX-reported) >= ROE_TARGET_PCT                 -> full close

Rule 3 — one automatic trade per symbol per day: once a symbol has had
one auto-placed trade today, any further clean setup on that symbol is
alert-only, never auto-placed, regardless of how that first trade closes.

BTC-regime filter: BTCUSD is never traded here (see core/state.py) but
its own price action relative to its PDH/PDL classifies a daily regime
(BULLISH/BEARISH/NEUTRAL) that blocks counter-regime auto-entries on the
other 9 symbols (alert-only instead).
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

TRADE_LEVERAGE = 5
MAX_CONCURRENT_POSITIONS = 2

# Priority 2 — how close to PDH/PDL counts as "approaching" the target.
REJECTION_PROXIMITY_PCT = 0.01  # 1.0%

# Priority 3 — CoinDCX's own reported ROE (% return on margin).
ROE_TARGET_PCT = 7.0


def _escape_md(text) -> str:
    text = str(text)
    for ch in ('_', '*', '`', '['):
        text = text.replace(ch, '\\' + ch)
    return text


def _is_bearish_rejection(candle: dict) -> bool:
    """Shooting Star / Bearish Pin Bar — long upper wick, small body near
    the bottom of the range, bearish close. Used to exit a LONG."""
    body = abs(candle['close'] - candle['open'])
    upper_wick = candle['high'] - max(candle['open'], candle['close'])
    lower_wick = min(candle['open'], candle['close']) - candle['low']
    if candle['close'] >= candle['open'] or body == 0:
        return False
    return upper_wick >= 2 * body and upper_wick >= 2 * lower_wick


def _is_bullish_rejection(candle: dict) -> bool:
    """Hammer / Bullish Pin Bar — mirror of the above. Used to exit a SHORT."""
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
        self._last_candle: dict = {}  # {symbol: last processed candle dict}, for engulfing checks
        self._last_regime_candle_time = None
        self._open_positions: dict = {}
        self._trailing: dict = {}  # {symbol: {side, entry, sl}} — open positions awaiting exit

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
        """Read-only — refreshes the BTC regime classification. BTC is
        never scanned for its own setups and never traded here."""
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
        """Priority 1 -> 2 -> 3, first true condition wins, checked in
        that exact order every closed candle."""
        tr = self._trailing.get(symbol)
        if not tr:
            return
        level = self.state.get_level(symbol)
        if not level:
            return

        side = tr["side"]
        target = level.pdh if side == 'BUY' else level.pdl

        # Priority 1 — target reached
        if side == 'BUY' and candle['high'] >= target:
            await self._exit_position(symbol, tr, exit_price=target, reason="target_achieved",
                                       label="Take Profit – Target Achieved")
            return
        if side == 'SELL' and candle['low'] <= target:
            await self._exit_position(symbol, tr, exit_price=target, reason="target_achieved",
                                       label="Take Profit – Target Achieved")
            return

        # Priority 2 — rejection candle, only while within proximity of target
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

            if rejection:
                await self._exit_position(symbol, tr, exit_price=candle['close'],
                                           reason="rejection_exit", label="Take Profit – Rejection Exit")
                return

        # Priority 3 — CoinDCX-reported ROE
        details = await self.coindcx.get_position_details(symbol)
        if details and details.get("roe") is not None and details["roe"] >= ROE_TARGET_PCT:
            await self._exit_position(symbol, tr, exit_price=candle['close'],
                                       reason="roe_protection", label="Take Profit – ROE Protection",
                                       roe=details["roe"])
            return

    async def _exit_position(self, symbol: str, tr: dict, exit_price: float, reason: str,
                              label: str, roe: Optional[float] = None):
        side = tr["side"]
        quantity = self._open_positions.get(symbol, 0)
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
            logger.info(f"{symbol} | Exited via {reason} at {exit_price:.4f}")
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

        # Rule 3 — one automatic trade per symbol per day.
        if level and level.auto_traded_today:
            logger.info(f"{symbol} | Fresh {signal.side} setup, but today's auto-trade "
                       f"already used — alert only")
            await self.telegram.send_alert(
                f"⚠️ *New Liquidity Setup Detected*\n\n"
                f"*Symbol:* {symbol}\n"
                f"*Time:* {datetime.now(IST).strftime('%H:%M IST')}\n"
                f"*Direction:* {'📈 LONG' if signal.side == 'BUY' else '📉 SHORT'}\n"
                f"*Entry:* {signal.entry_price:.4f}\n"
                f"*SL:* {signal.sl_price:.4f}\n\n"
                f"*Reason:* This symbol's one automatic trade for today has already been used.\n\n"
                f"No auto-entry executed. Review and enter manually on CoinDCX if you agree."
            )
            return

        # BTC-regime filter — block counter-regime auto-entries, alert only.
        regime = self.state.get_regime()
        if (signal.side == 'BUY' and regime == 'BEARISH') or (signal.side == 'SELL' and regime == 'BULLISH'):
            logger.info(f"{symbol} | {signal.side} setup, but BTC regime is {regime} — alert only")
            await self.telegram.send_alert(
                f"⚠️ *New Liquidity Setup Detected*\n\n"
                f"*Symbol:* {symbol}\n"
                f"*Time:* {datetime.now(IST).strftime('%H:%M IST')}\n"
                f"*Direction:* {'📈 LONG' if signal.side == 'BUY' else '📉 SHORT'}\n"
                f"*Entry:* {signal.entry_price:.4f}\n"
                f"*SL:* {signal.sl_price:.4f}\n\n"
                f"*Reason:* BTC is currently in a {regime.lower()} regime, running counter to "
                f"this {'LONG' if signal.side == 'BUY' else 'SHORT'} setup.\n\n"
                f"No auto-entry executed as per the BTC-regime filter. "
                f"Review and enter manually on CoinDCX if you agree with this setup."
            )
            return

        open_count = len(self._open_positions)
        if open_count >= MAX_CONCURRENT_POSITIONS:
            logger.info(f"SKIPPED {symbol} — {open_count}/{MAX_CONCURRENT_POSITIONS} positions already open")
            await self.telegram.send_alert(
                f"⏭️ *Setup Skipped* — Max concurrent positions reached ({open_count}/{MAX_CONCURRENT_POSITIONS})\n\n"
                f"*Symbol:* {symbol}\n*Side:* {signal.side}\n*Pattern:* {signal.pattern}\n"
                f"*Would-be Entry:* {signal.entry_price:.4f}\n*Would-be SL:* {signal.sl_price:.4f}"
            )
            return

        await self.telegram.send_alert(
            f"🔍 *Setup Detected*\n\n"
            f"*Symbol:* {symbol}\n*Side:* {'📈 LONG' if signal.side == 'BUY' else '📉 SHORT'}\n"
            f"*Pattern:* {signal.pattern}\n*Entry:* {signal.entry_price:.4f}\n*SL:* {signal.sl_price:.4f}\n"
            f"*PDH:* {signal.pdh:.4f} | *PDL:* {signal.pdl:.4f}\n\n⏳ Placing order..."
        )

        try:
            margin_usd = float(os.getenv('TRADE_SIZE_USD', 30))
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
                self._trailing[symbol] = {"side": signal.side, "entry": signal.entry_price, "sl": signal.sl_price}
                self._open_positions[symbol] = quantity_filled

                tp_price = signal.pdh if signal.side == 'BUY' else signal.pdl
                tp_set = await self.coindcx.update_position_tpsl(symbol, tp_price=tp_price)
                tp_note = (f"🎯 Resting take-profit set at {tp_price:.4f} (Priority 1 target)."
                          if tp_set else
                          f"⚠️ Could not set a resting take-profit order — Priority 1 will still "
                          f"be enforced by the bot on each closed candle, with possible minor "
                          f"slippage. Check CoinDCX manually if concerned.")

                msg = (
                    f"✅ *Trade Executed*\n\n"
                    f"*Symbol:* {symbol}\n*Side:* {'📈 LONG' if signal.side == 'BUY' else '📉 SHORT'}\n"
                    f"*Entry:* {signal.entry_price:.4f}\n*SL:* {signal.sl_price:.4f}\n"
                    f"*Target (PDH/PDL):* {tp_price:.4f}\n"
                    f"*Margin:* ${margin_usd:.2f}\n*Leverage:* {TRADE_LEVERAGE}x\n"
                    f"*Exposure:* ${margin_usd * TRADE_LEVERAGE:.2f}\n*Quantity:* {quantity_filled}\n"
                    f"*Order ID:* `{order_id}`\n*Open positions:* {len(self._open_positions)}/{MAX_CONCURRENT_POSITIONS}\n\n"
                    f"{tp_note}\n\n"
                    f"Exit hierarchy active: Target → Rejection Candle → 7% ROE.\n"
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

        except Exception as e:
            logger.error(f"{symbol} | Order exception: {e}", exc_info=True)
            await self.telegram.send_alert(
                f"❌ *Order Exception*\n\n*Symbol:* {symbol}\n*Error:* {_escape_md(e)}\n\n"
                f"⚠️ Manual intervention required."
            )
