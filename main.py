"""
Liquidity Strategy Bot — CoinDCX
Entry point: starts scheduler + monitoring loop
"""
import asyncio
import logging
import sys
import os
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
from core.state import BotState
from core.strategy import StrategyEngine
from core.monitor import MarketMonitor
from notifications.telegram import TelegramBot
from exchange.coindcx import CoinDCXClient
from utils.logger import setup_logger
logger = setup_logger("main")
IST = pytz.timezone("Asia/Kolkata")

# TEMPORARY — set back to False once you're done weekend/off-hours testing.
# When True, the bot fetches PDH/PDL immediately on startup regardless of
# what time it is, instead of waiting for the 5:30 AM IST cron trigger.
TESTING_FORCE_IMMEDIATE_FETCH = True


async def daily_reset(state: BotState, engine: StrategyEngine, telegram: TelegramBot):
    """Runs at 5:30 AM IST — fetch PDH/PDL and reset daily counters."""
    logger.info("=== Daily Reset at 5:30 AM IST ===")
    state.reset_daily()
    success = await engine.fetch_and_set_levels()
    if not success:
        # Retry once
        logger.warning("PDH/PDL fetch failed — retrying in 30 seconds")
        await asyncio.sleep(30)
        success = await engine.fetch_and_set_levels()
        if not success:
            await telegram.send_alert(
                "⚠️ CRITICAL: Failed to fetch PDH/PDL after retry.\n"
                "Bot is paused for today. Manual intervention required."
            )
            state.paused = True
            return
    await telegram.send_alert(
        f"✅ Daily levels set:\n"
        f"{'='*30}\n" +
        "\n".join([
    f"*{sym}* | PDH: {lvl.pdh:.4f} | PDL: {lvl.pdl:.4f}"
    for sym, lvl in state.levels.items()
])
    )
    logger.info(f"Levels set: {state.levels}")


async def main():
    logger.info("Starting Liquidity Bot...")
    # Init components
    coindcx = CoinDCXClient(
        api_key=os.getenv('COINDCX_API_KEY'),
        api_secret=os.getenv('COINDCX_API_SECRET')
    )
    telegram = TelegramBot()
    state = BotState()
    engine = StrategyEngine(coindcx, state)
    monitor = MarketMonitor(coindcx, engine, state, telegram)
    # Test connections
    if not await telegram.test_connection():
        logger.error("Telegram connection failed — check BOT_TOKEN and CHAT_ID")
        sys.exit(1)
    await telegram.send_alert("🤖 Liquidity Bot started successfully.")
    # Scheduler for 5:30 AM IST daily reset
    scheduler = AsyncIOScheduler(timezone=IST)
    scheduler.add_job(
        daily_reset,
        CronTrigger(hour=5, minute=30, timezone=IST),
        args=[state, engine, telegram],
        id="daily_reset",
        replace_existing=True
    )
    scheduler.start()
    # Run initial fetch if bot starts after 5:30 AM (or if testing override is on)
    from datetime import datetime
    now = datetime.now(IST)
    started_after_530 = now.hour >= 5 and (now.hour > 5 or now.minute >= 30)
    if started_after_530:
        logger.info("Bot started after 5:30 AM — fetching today's levels immediately")
        await daily_reset(state, engine, telegram)
    elif TESTING_FORCE_IMMEDIATE_FETCH:
        logger.info("TESTING_FORCE_IMMEDIATE_FETCH is True — fetching levels immediately "
                    "even though it's before 5:30 AM IST")
        await daily_reset(state, engine, telegram)
    # Start monitoring loop
    try:
        await monitor.run()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    finally:
        scheduler.shutdown()
        await telegram.send_alert("🔴 Liquidity Bot stopped.")


if __name__ == "__main__":
    asyncio.run(main())
