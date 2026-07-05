"""
MarketMonitor — continuously polls 15-minute candles and routes signals to execution.
Runs Monday to Friday only, from 5:30 AM to 11:00 PM IST.

Event-driven design: rather than a fixed daily trade count, the bot checks
CoinDCX's live open positions before every new trade (max 2 concurrent,
across all symbols). It also periodically reconciles which locally
"in_trade" symbols have actually closed on the exchange, and resets them
to start a completely fresh watch cycle — so a symbol can trade multiple
independent times per day, not just once.
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

# TEMPORARY — set back to False once you're done testing over the weekend.
TESTING_IGNORE_WEEKENDS = True


class MarketMonitor:
    def __init__(self, coindcx: CoinDCXClient, engine: StrategyEngine,
                 state: BotState, telegram: TelegramBot):
        self.coindcx = coindcx
        self.engine = engine
        self.state = state
        self.telegram = telegram
        self._last_candle_time = {sym: None for sym in SYMBOLS}
        self._open_positions: dict = {}  # {symbol: active_pos}, refreshed each cycle

    def _is_trading_day(self) -> bool:
        if TESTING_IGNORE_WEEKENDS:
            return True
        now = datetime.now(IST)
        return now.weekday() < 5

    def _is_trading_hours(self) -> bool:
        now = datetime.now(IST)
        return dtime(5, 30) <= now.time() <= dtime(DAY_END_HOUR, DAY_END_MINUTE)

    async def run(self):
        logger.info("Market monitor started")
        while True:
            try:
                if not self._is_trading_day():
                    now = datetime.now(IST)
                    logger.info(f"Weekend — skipping ({now.strftime('%A')})")
                    await asyncio.sleep(3600)
                    continue

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

            signal = self.engine.process_candle(symbol, candle)
            if signal:
                await self._handle_signal(signal)

        except Exception as e:
            logger.error(f"{symbol} | _process_symbol error: {e}", exc_info=True)

    async def _handle_signal(self, signal: Signal):
        symbol = signal.symbol
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
                    f"🔄 This symbol will resume watching once the position closes."
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
                    f"*Error:* {error_msg}\n\n"
                    f"⚠️ Manual intervention may be required. This setup was NOT "
                    f"marked in_trade — the bot will keep watching for fresh sweeps."
                )

        except Exception as e:
            logger.error(f"{symbol} | Order exception: {e}", exc_info=True)
            await self.telegram.send_alert(
                f"❌ *Order Exception*\n\n"
                f"*Symbol:* {symbol}\n"
                f"*Error:* {str(e)}\n\n"
                f"⚠️ Manual intervention required. This setup was NOT marked "
                f"in_trade — the bot will keep watching for fresh sweeps."
            )
