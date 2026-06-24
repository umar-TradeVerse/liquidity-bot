"""
MarketMonitor — continuously polls 1m candles and routes signals to execution.
Runs Monday to Friday only, from 5:30 AM IST.
"""

import asyncio
import logging
from datetime import datetime, time as dtime
import pytz

from core.state import BotState, SYMBOLS, TradeRecord
from core.strategy import StrategyEngine, Signal
from exchange.delta import DeltaClient
from notifications.telegram import TelegramBot

logger = logging.getLogger("monitor")
IST = pytz.timezone("Asia/Kolkata")

POLL_INTERVAL_SECONDS = 15
DAY_END_HOUR = 13
DAY_END_MINUTE = 0


class MarketMonitor:
    def __init__(self, delta: DeltaClient, engine: StrategyEngine,
                 state: BotState, telegram: TelegramBot):
        self.delta = delta
        self.engine = engine
        self.state = state
        self.telegram = telegram
        self._last_candle_time = {sym: None for sym in SYMBOLS}

    def _is_trading_day(self) -> bool:
        """Returns True only for Monday (0) to Friday (4)."""
        now = datetime.now(IST)
        weekday = now.weekday()  # 0=Monday, 6=Sunday
        return weekday < 5

    def _is_trading_hours(self) -> bool:
        """Returns True between 5:30 AM and 11:30 PM IST."""
        now = datetime.now(IST)
        current_time = now.time()
        return dtime(5, 30) <= current_time <= dtime(DAY_END_HOUR, DAY_END_MINUTE)

    async def run(self):
        """Main monitoring loop."""
        logger.info("Market monitor started")

        while True:
            try:
                # Weekend check
                if not self._is_trading_day():
                    now = datetime.now(IST)
                    logger.info(f"Weekend — skipping ({now.strftime('%A')})")
                    await asyncio.sleep(3600)  # Check again in 1 hour
                    continue

                # Levels not ready yet
                if not self.state.levels_ready() or self.state.paused:
                    await asyncio.sleep(30)
                    continue

                # Outside trading hours
                if not self._is_trading_hours():
                    await asyncio.sleep(60)
                    continue

                # Process all symbols concurrently
                tasks = [self._process_symbol(sym) for sym in SYMBOLS]
                await asyncio.gather(*tasks, return_exceptions=True)

            except Exception as e:
                logger.error(f"Monitor loop error: {e}", exc_info=True)

            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def _process_symbol(self, symbol: str):
        """Fetch latest 1m candle and run strategy check for one symbol."""
        try:
            candle = await self.delta.get_latest_1m_candle(symbol)
            if not candle:
                return

            if self._last_candle_time[symbol] == candle['time']:
                return
            self._last_candle_time[symbol] = candle['time']

            signal = self.engine.process_candle(symbol, candle)
            if signal:
                await self._handle_signal(signal)

        except Exception as e:
            logger.error(f"{symbol} | _process_symbol error: {e}", exc_info=True)

    async def _handle_signal(self, signal: Signal):
        """Execute or skip a signal based on daily trade limit."""
        symbol = signal.symbol

        if not self.state.can_trade():
            logger.info(f"SKIPPED {symbol} — max 2 trades reached today")
            await self.telegram.send_alert(
                f"⏭️ *Setup Skipped* — Max trades reached\n\n"
                f"*Symbol:* {symbol}\n"
                f"*Side:* {signal.side}\n"
                f"*Scenario:* {signal.scenario.replace('_', ' ').title()}\n"
                f"*Pattern:* {signal.pattern}\n"
                f"*Would-be Entry:* {signal.entry_price:.4f}\n"
                f"*Would-be SL:* {signal.sl_price:.4f}"
            )
            self.state.mark_scenario_fired(symbol)
            return

        await self.telegram.send_alert(
            f"🔍 *Setup Detected*\n\n"
            f"*Symbol:* {symbol}\n"
            f"*Side:* {'📈 LONG' if signal.side == 'BUY' else '📉 SHORT'}\n"
            f"*Scenario:* {signal.scenario.replace('_', ' ').title()}\n"
            f"*Pattern:* {signal.pattern}\n"
            f"*Entry:* {signal.entry_price:.4f}\n"
            f"*SL:* {signal.sl_price:.4f}\n"
            f"*PDH:* {signal.pdh:.4f} | *PDL:* {signal.pdl:.4f}\n\n"
            f"⏳ Placing order..."
        )

        try:
            order_result = await self.delta.place_order(
                symbol=symbol,
                side=signal.side,
                entry_price=signal.entry_price,
                sl_price=signal.sl_price,
                leverage=5
            )

            if order_result and order_result.get('success'):
                order_id = order_result.get('order_id', 'N/A')
                trade_usd = order_result.get('trade_usd', 0)
                quantity = order_result.get('quantity', 0)
                sl_warning = order_result.get('sl_failed', False)

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
                self.state.mark_scenario_fired(symbol)

                msg = (
                    f"✅ *Trade Executed*\n\n"
                    f"*Symbol:* {symbol}\n"
                    f"*Side:* {'📈 LONG' if signal.side == 'BUY' else '📉 SHORT'}\n"
                    f"*Entry:* {signal.entry_price:.4f}\n"
                    f"*SL:* {signal.sl_price:.4f}\n"
                    f"*Trade Size:* ${trade_usd:.2f} (25% of wallet)\n"
                    f"*Leverage:* 5x\n"
                    f"*Quantity:* {quantity}\n"
                    f"*Order ID:* `{order_id}`\n"
                    f"*Scenario:* {signal.scenario.replace('_', ' ').title()}\n"
                    f"*Trades today:* {self.state.trades_today}/2\n\n"
                    f"⚠️ TP is manual — monitor your position."
                )

                if sl_warning:
                    msg += "\n\n🚨 *SL order failed — place SL manually immediately!*"

                await self.telegram.send_alert(msg)
                logger.info(f"Trade executed: {record}")

            else:
                error_msg = order_result.get('error', 'Unknown') if order_result else 'No response'
                logger.error(f"{symbol} | Order failed: {error_msg}")
                await self.telegram.send_alert(
                    f"❌ *Order Failed* — NOT counted toward daily limit\n\n"
                    f"*Symbol:* {symbol}\n"
                    f"*Side:* {signal.side}\n"
                    f"*Error:* {error_msg}\n\n"
                    f"⚠️ Manual intervention may be required."
                )

        except Exception as e:
            logger.error(f"{symbol} | Order exception: {e}", exc_info=True)
            await self.telegram.send_alert(
                f"❌ *Order Exception* — NOT counted toward daily limit\n\n"
                f"*Symbol:* {symbol}\n"
                f"*Error:* {str(e)}\n\n"
                f"⚠️ Manual intervention required."
            )
