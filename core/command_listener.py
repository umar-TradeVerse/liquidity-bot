"""
command_listener.py — lets you request files (trades.jsonl, state snapshot)
straight from Telegram instead of the Railway CLI or a terminal.

Supported commands (type these in your bot's Telegram chat):
  /trades   — sends the full trades.jsonl trade-history file
  /state    — sends the current state_snapshot.json (debugging aid)
  /help     — lists available commands

Runs as a background asyncio task (started from main.py alongside the
monitor loop). Uses Telegram long-polling (getUpdates) — no public
webhook URL or extra Railway networking config needed.

Security: only processes messages from the chat_id already configured
via TELEGRAM_CHAT_ID. Anyone else messaging the bot is silently ignored.
"""
import asyncio
from notifications.telegram import TelegramBot
from core import persistence
from utils.logger import setup_logger

logger = setup_logger("command_listener")

HELP_TEXT = (
    "🤖 *Available commands*\n\n"
    "/status — running? open positions, today's P&L, code version\n"
    "/logs — log file for the last 24h (or /logs 6 for 6 hours, max 168)\n"
    "/settings — active configuration (secrets never shown)\n"
    "/trades — sends the full trade history file (trades.jsonl)\n"
    "/state — sends the current state snapshot (for debugging)\n"
    "/help — shows this message\n\n"
    "📬 The last 24h of logs is also sent automatically every night at 11:00 pm IST."
)


async def send_logs(telegram: TelegramBot, hours: float = 24, label: str = ""):
    """Build the combined log file for the last `hours` and send it."""
    from core.ops_reports import build_log_file
    path = build_log_file(hours)
    if not path:
        await telegram.send_alert("⚠️ No log file yet — the bot may have just started.")
        return False
    ok = await telegram.send_document(path, caption=f"🧾 {label}Bot logs — last {int(hours)}h (times in UTC)")
    if not ok:
        await telegram.send_alert("⚠️ Couldn't send the log file (Telegram limit is 50 MB) — try fewer hours, e.g. /logs 12")
    return ok


async def run_command_listener(telegram: TelegramBot, monitor=None):
    logger.info("Telegram command listener started")
    offset = None
    while True:
        try:
            updates = await telegram.get_updates(offset=offset, timeout=25)
            for update in updates:
                offset = update["update_id"] + 1  # advance regardless of relevance
                message = update.get("message") or update.get("edited_message")
                if not message:
                    continue

                from_chat_id = str(message.get("chat", {}).get("id", ""))
                if from_chat_id != str(telegram.chat_id):
                    logger.warning(f"Ignoring command from unrecognized chat_id {from_chat_id}")
                    continue

                text = (message.get("text") or "").strip().lower()
                cmd, _, arg = text.partition(" ")
                cmd = cmd.split("@")[0]          # "/logs@MyBot" from group chats
                if cmd == "/logs":
                    try:
                        hours = float(arg) if arg else 24
                    except ValueError:
                        hours = 24
                    await send_logs(telegram, hours)
                elif cmd == "/status":
                    from core.ops_reports import status_text
                    await telegram.send_alert(status_text(monitor))
                elif cmd == "/settings":
                    from core.ops_reports import settings_text
                    await telegram.send_alert(settings_text())
                elif text == "/trades":
                    sent = await telegram.send_document(
                        persistence.TRADES_LOG_PATH,
                        caption="📊 Full trade history (one JSON line per open/close event)."
                    )
                    if not sent:
                        await telegram.send_alert(
                            "⚠️ Couldn't send trades.jsonl — it may not exist yet "
                            "(no trades logged since this feature was deployed) or "
                            "the volume isn't mounted."
                        )
                elif text == "/state":
                    await telegram.send_document(
                        persistence.STATE_SNAPSHOT_PATH,
                        caption="🗂 Current state snapshot."
                    )
                elif text == "/help":
                    await telegram.send_alert(HELP_TEXT)

        except Exception as e:
            logger.error(f"Command listener error: {e}", exc_info=True)
            await asyncio.sleep(5)  # back off before retrying getUpdates
