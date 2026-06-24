"""
MarketMonitor — continuously polls 1m candles and routes signals to execution.
Runs from 5:30 AM IST until end of day.
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

POLL_INTERVAL_SECONDS = 15   # Check every 15 seconds
DAY_END_HOUR = 23            # Stop monitoring at 11 PM IST (before next 5:30 AM)
DAY_END_MINUTE = 30


class MarketMonitor:
    def __init__(self, delta: DeltaClient, engine: StrategyEngine,
                 state: BotState, telegram: TelegramBot):
        self.delta = delta
        self.engine = engine
        self.state = state
        self.telegram = telegram
        # Track last processed candle timestamp per symbol
        self._last_candle_time = {sym: None for sym in SYMBOLS}

    async def run(self):
        """Main monitoring loop."""
        logger.info("Market monitor started")

        while True:
            try:
                now = datetime.now(IST)

                # Wait until levels are ready
                if not self.state.levels_ready() or self.state.paused:
                    await asyncio.sleep(30)
                    continue

                # Check if within trading hours (5:30 AM to 11:30 PM IST)
                current_time = now.time()
                start_time = dtime(5, 30)
                end_time = dtime(DAY_END_HOUR, DAY_END_MINUTE)

                if not (start_time <= current_time <= end_time):
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

            # Skip if we already processed this candle
            if self._last_candle_time[symbol] == candle['time']:
                return
            self._last_candle_time[symbol] = candle['time']

            # Run strategy
            signal = self.engine.process_candle(symbol, candle)
            if signal:
                await self._handle_signal(signal)

        except Exception as e:
            logger.error(f"{symbol} | _process_symbol error: {e}", exc_info=True)

    async def _handle_signal(self, signal: Signal):
        """Execute or skip a signal based on daily trade limit."""
        symbol = signal.symbol

        if not self.state.can_trade():
            # Max trades reached — notify and skip
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

        # Notify: setup detected
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

        # Execute order
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

                await self.telegram.send_alert(
                    f"✅ *Trade Executed*\n\n"
                    f"*Symbol:* {symbol}\n"
                    f"*Side:* {'📈 LONG' if signal.side == 'BUY' else '📉 SHORT'}\n"
                    f"*Entry:* {signal.entry_price:.4f}\n"
                    f"*SL:* {signal.sl_price:.4f}\n"
                    f"*Leverage:* 5x\n"
                    f"*Order ID:* `{order_id}`\n"
                    f"*Scenario:* {signal.scenario.replace('_', ' ').title()}\n"
                    f"*Trades today:* {self.state.trades_today}/2\n\n"
                    f"⚠️ TP is manual — monitor your position."
                )
                logger.info(f"Trade executed: {record}")

            else:
                error_msg = order_result.get('error', 'Unknown error') if order_result else 'No response'
                logger.error(f"{symbol} | Order failed: {error_msg}")
                # Do NOT count failed order toward daily limit
                await self.telegram.send_alert(
                    f"❌ *Order Failed* — NOT counted toward daily limit\n\n"
                    f"*Symbol:* {symbol}\n"
                    f"*Side:* {signal.side}\n"
                    f"*Error:* {error_msg}\n\n"
                    f"⚠️ Manual intervention may be required."
                )

        except Exception as e:
            logger.error(f"{symbol} | Order execution exception: {e}", exc_info=True)
            await self.telegram.send_alert(
                f"❌ *Order Exception* — NOT counted toward daily limit\n\n"
                f"*Symbol:* {symbol}\n"
                f"*Error:* {str(e)}\n\n"
                f"⚠️ Manual intervention required."
            )
