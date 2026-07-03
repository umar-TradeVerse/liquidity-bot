"""
MarketMonitor — continuously polls 5m candles and routes signals to execution.
Runs Monday to Friday only, from 5:30 AM to 1:00 PM IST.
"""

import asyncio
import logging
from datetime import datetime, time as dtime
import pytz
import os

from core.state import BotState, SYMBOLS, TradeRecord
from core.strategy import StrategyEngine, Signal
from exchange.coindcx import CoinDCXClient
from notifications.telegram import TelegramBot

logger = logging.getLogger("monitor")
IST = pytz.timezone("Asia/Kolkata")

POLL_INTERVAL_SECONDS = 15
DAY_END_HOUR = 13
DAY_END_MINUTE = 0


class MarketMonitor:
    def __init__(self, coindcx: CoinDCXClient, engine: StrategyEngine,
                 state: BotState, telegram: TelegramBot):
        self.coindcx = coindcx
        self.engine = engine
        self.state = state
        self.telegram = telegram
        self._last_candle_time = {sym: None for sym in SYMBOLS}

    def _is_trading_day(self) -> bool:
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

                tasks = [self._process_symbol(sym) for sym in SYMBOLS]
                await asyncio.gather(*tasks, return_exceptions=True)

            except Exception as e:
                logger.error(f"Monitor loop error: {e}", exc_info=True)

            await asyncio.sleep(POLL_INTERVAL_SECONDS)

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

        except Exception as e:
            logger.error(f"{symbol} | _process_symbol error: {e}", exc_info=True)

    async def _handle_signal(self, signal: Signal):
        symbol = signal.symbol

        if not self.state.can_trade():
            logger.info(f"SKIPPED {symbol} — max 2 trades reached today")
            self.state.mark_scenario_fired(symbol)
            await self.telegram.send_alert(
                f"⏭️ *Setup Skipped* — Max trades reached\n\n"
                f"*Symbol:* {symbol}\n"
                f"*Side:* {signal.side}\n"
                f"*Scenario:* {signal.scenario.replace('_', ' ').title()}\n"
                f"*Pattern:* {signal.pattern}\n"
                f"*Would-be Entry:* {signal.entry_price:.4f}\n"
                f"*Would-be SL:* {signal.sl_price:.4f}"
            )
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
            trade_size_usd = float(os.getenv('TRADE_SIZE_USD', 30))
            quantity = trade_size_usd / signal.entry_price

            # SINGLE order with built-in SL (CoinDCX Futures API)
            # No separate SL order needed — it's included in the order itself
            order_result = await self.coindcx.place_market_order(
                symbol=symbol,
                side=signal.side,
                quantity=quantity,
                sl_price=signal.sl_price
            )

            if order_result and order_result.get('id'):
                order_id = order_result.get('id', 'N/A')
                trade_usd = trade_size_usd
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
                self.state.mark_scenario_fired(symbol)

                msg = (
                    f"✅ *Trade Executed*\n\n"
                    f"*Symbol:* {symbol}\n"
                    f"*Side:* {'📈 LONG' if signal.side == 'BUY' else '📉 SHORT'}\n"
                    f"*Entry:* {signal.entry_price:.4f}\n"
                    f"*SL:* {signal.sl_price:.4f}\n"
                    f"*Trade Size:* ${trade_usd:.2f}\n"
                    f"*Leverage:* 5x\n"
                    f"*Quantity:* {quantity_filled}\n"
                    f"*Order ID:* `{order_id}`\n"
                    f"*Scenario:* {signal.scenario.replace('_', ' ').title()}\n"
                    f"*Trades today:* {self.state.trades_today}/2\n\n"
                    f"⚠️ TP is manual — monitor your position.\n"
                    f"✅ SL is built-in — automatically triggered."
                )

                await self.telegram.send_alert(msg)
                logger.info(f"Trade executed: {record}")

            else:
                error_msg = order_result.get('error', 'Unknown') if order_result else 'No response from API'
                logger.error(f"{symbol} | Order failed: {error_msg}")
                self.state.mark_scenario_fired(symbol)
                await self.telegram.send_alert(
                    f"❌ *Order Failed* — NOT counted toward daily limit\n\n"
                    f"*Symbol:* {symbol}\n"
                    f"*Side:* {signal.side}\n"
                    f"*Error:* {error_msg}\n\n"
                    f"⚠️ Manual intervention may be required."
                )

        except Exception as e:
            logger.error(f"{symbol} | Order exception: {e}", exc_info=True)
            self.state.mark_scenario_fired(symbol)
            await self.telegram.send_alert(
                f"❌ *Order Exception* — NOT counted toward daily limit\n\n"
                f"*Symbol:* {symbol}\n"
                f"*Error:* {str(e)}\n\n"
                f"⚠️ Manual intervention required."
            )
