"""
Telegram notification bot.
Sends formatted alerts for all bot events.
"""
import asyncio
import os
import aiohttp
from typing import Optional
from utils.logger import setup_logger
logger = setup_logger("telegram")
TELEGRAM_API = "https://api.telegram.org"
class TelegramBot:
    def __init__(self):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    async def test_connection(self) -> bool:
        if not self.token or not self.chat_id:
            logger.error("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")
            return False
        result = await self.send_alert("🔌 Telegram connection test successful.")
        return result
    async def send_alert(self, message: str, parse_mode: str = "Markdown") -> bool:
        """Send a Telegram message. Returns True on success."""
        if not self.token or not self.chat_id:
            logger.warning(f"Telegram not configured. Message: {message}")
            return False
        url = f"{TELEGRAM_API}/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True
        }
        for attempt in range(3):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url, json=payload,
                        timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp:
                        if resp.status == 200:
                            return True
                        else:
                            text = await resp.text()
                            logger.error(f"Telegram send failed [{resp.status}]: {text}")
                            if resp.status == 400:
                                # Bad request — try without parse mode
                                payload["parse_mode"] = None
                                del payload["parse_mode"]
            except asyncio.TimeoutError:
                logger.warning(f"Telegram timeout (attempt {attempt+1})")
                await asyncio.sleep(2 ** attempt)
            except Exception as e:
                logger.error(f"Telegram error: {e}")
                await asyncio.sleep(2 ** attempt)
        return False
    async def send_sl_hit(self, symbol: str, side: str, entry: float,
                          sl: float, order_id: str):
        await self.send_alert(
            f"🛑 *Stop Loss Hit*\n\n"
            f"*Symbol:* {symbol}\n"
            f"*Side:* {side}\n"
            f"*Entry:* {entry:.4f}\n"
            f"*SL:* {sl:.4f}\n"
            f"*Order ID:* `{order_id}`\n\n"
            f"Position closed at stop loss."
        )
    async def send_manual_intervention(self, symbol: str, reason: str):
        await self.send_alert(
            f"⚠️ *Manual Intervention Required*\n\n"
            f"*Symbol:* {symbol}\n"
            f"*Reason:* {reason}\n\n"
            f"Please check your CoinDCX account."
        )

    async def get_updates(self, offset: Optional[int] = None, timeout: int = 25) -> list:
        """Long-poll for new incoming messages. Returns raw Telegram 'result' list."""
        if not self.token:
            return []
        url = f"{TELEGRAM_API}/bot{self.token}/getUpdates"
        params = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, params=params,
                    timeout=aiohttp.ClientTimeout(total=timeout + 10)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return data.get("result", [])
                    logger.warning(f"getUpdates returned {resp.status}")
        except asyncio.TimeoutError:
            pass  # normal for long-polling with no new messages
        except Exception as e:
            logger.error(f"getUpdates error: {e}")
        return []

    async def send_document(self, file_path: str, caption: Optional[str] = None) -> bool:
        """Send a file as a Telegram document. Returns True on success."""
        if not self.token or not self.chat_id:
            logger.warning("Telegram not configured — can't send document")
            return False
        if not os.path.exists(file_path):
            await self.send_alert(f"⚠️ Requested file doesn't exist yet: `{file_path}`")
            return False
        url = f"{TELEGRAM_API}/bot{self.token}/sendDocument"
        try:
            with open(file_path, "rb") as fh:
                data = aiohttp.FormData()
                data.add_field("chat_id", self.chat_id)
                if caption:
                    data.add_field("caption", caption)
                data.add_field("document", fh, filename=os.path.basename(file_path))
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        url, data=data, timeout=aiohttp.ClientTimeout(total=60)
                    ) as resp:
                        if resp.status == 200:
                            return True
                        text = await resp.text()
                        logger.error(f"sendDocument failed [{resp.status}]: {text}")
                        return False
        except Exception as e:
            logger.error(f"send_document error: {e}", exc_info=True)
            return False
