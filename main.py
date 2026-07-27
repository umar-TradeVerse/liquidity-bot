"""
Liquidity Strategy Bot — CoinDCX
Entry point: starts scheduler + monitoring loop
"""
import asyncio
import logging
import sys
import os
from typing import Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
from core.state import BotState, DailyLevel
from core.strategy import StrategyEngine
from core.monitor import MarketMonitor
from core import persistence
from notifications.telegram import TelegramBot
from exchange.coindcx import CoinDCXClient
from utils.logger import setup_logger
logger = setup_logger("main")
IST = pytz.timezone("Asia/Kolkata")

# Set to True only if you want to force an immediate PDH/PDL fetch on
# startup regardless of time (useful for off-hours testing).
TESTING_FORCE_IMMEDIATE_FETCH = False


async def daily_reset(state: BotState, engine: StrategyEngine, telegram: TelegramBot,
                       monitor: Optional["MarketMonitor"] = None):
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
    # Snapshot immediately so a restart later today never reloads yesterday's
    # (now stale) state instead of today's freshly-reset levels.
    if monitor is not None:
        persistence.save_state(state, monitor._trailing)
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
    # Scheduler for 5:30 AM IST daily reset (runs every day, weekends included)
    scheduler = AsyncIOScheduler(timezone=IST)
    scheduler.add_job(
        daily_reset,
        CronTrigger(hour=5, minute=30, timezone=IST),
        args=[state, engine, telegram, monitor],
        id="daily_reset",
        replace_existing=True
    )
    scheduler.start()
    # Restart recovery: if a same-day state snapshot exists (mounted volume,
    # see core/persistence.py), restore levels + in-flight trailing (SL/TP/
    # MFE-MAE tracking) BEFORE deciding whether to run daily_reset. This is
    # what stops a Railway restart mid-day from wiping sweep-in-progress
    # state and losing exit-condition tracking on any open position.
    restored = persistence.load_state()
    if restored:
        for sym, level_dict in restored["levels"].items():
            state.levels[sym] = DailyLevel(**level_dict) if level_dict is not None else None
        monitor.restore_trailing(restored["trailing"])
        await telegram.send_alert(
            f"♻️ Restored today's state from snapshot "
            f"({len(restored['trailing'])} open position(s) reattached)."
        )

    # Run initial fetch if bot starts after 5:30 AM (or if testing override is on)
    # and we didn't just restore a same-day snapshot — a successful restore
    # already has today's levels, so re-fetching would be redundant (and
    # harmless, but noisy) rather than wrong.
    from datetime import datetime
    now = datetime.now(IST)
    started_after_530 = now.hour >= 5 and (now.hour > 5 or now.minute >= 30)
    if restored and state.levels_ready():
        logger.info("Same-day snapshot restored — skipping immediate daily_reset fetch")
    elif started_after_530:
        logger.info("Bot started after 5:30 AM — fetching today's levels immediately")
        await daily_reset(state, engine, telegram, monitor)
    elif TESTING_FORCE_IMMEDIATE_FETCH:
        logger.info("TESTING_FORCE_IMMEDIATE_FETCH is True — fetching levels immediately "
                    "even though it's before 5:30 AM IST")
        await daily_reset(state, engine, telegram, monitor)
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
