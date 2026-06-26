"""
Delta Exchange API client.
Fixed trade size per trade, 5x leverage. No wallet balance fetch needed.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Optional
import aiohttp
import os
from datetime import datetime, timedelta
import pytz

logger = logging.getLogger("delta")
IST = pytz.timezone("Asia/Kolkata")

DELTA_BASE_URL = "https://api.india.delta.exchange"

SYMBOL_MAP = {
    "ETHUSD":  "ETHUSD",
    "SOLUSD":  "SOLUSD",
    "XRPUSD":  "XRPUSD",
    "TAOUSD":  "TAOUSD",
    "XAUTUSD": "XAUTUSD",
}

FIXED_LEVERAGE = 5
# Fixed trade size in USD — set via Railway environment variable TRADE_SIZE_USD
# Default: 30 USD per trade × 5x leverage = 150 USD notional position
TRADE_SIZE_USD = float(os.getenv("TRADE_SIZE_USD", "30"))


class DeltaClient:
    def __init__(self):
        self.api_key = os.getenv("DELTA_API_KEY", "")
        self.api_secret = os.getenv("DELTA_API_SECRET", "")
        self.base_url = DELTA_BASE_URL
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def _generate_signature(self, method: str, path: str, payload: str = "") -> dict:
        timestamp = str(int(time.time()))
        message = method + timestamp + path + payload
        signature = hmac.new(
            self.api_secret.encode(),
            message.encode(),
            hashlib.sha256
        ).hexdigest()
        return {
            "api-key": self.api_key,
            "timestamp": timestamp,
            "signature": signature,
            "Content-Type": "application/json"
        }

    async def _get(self, path: str, params: dict = None, auth: bool = False) -> Optional[dict]:
        session = await self._get_session()
        url = self.base_url + path
        headers = self._generate_signature("GET", path) if auth else {}
        try:
            async with session.get(url, params=params, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    text = await resp.text()
                    logger.error(f"GET {path} → {resp.status}: {text}")
                    return None
        except asyncio.TimeoutError:
            logger.error(f"GET {path} timeout")
            return None
        except Exception as e:
            logger.error(f"GET {path} error: {e}")
            return None

    async def _post(self, path: str, payload: dict) -> Optional[dict]:
        session = await self._get_session()
        url = self.base_url + path
        body = json.dumps(payload)
        headers = self._generate_signature("POST", path, body)
        try:
            async with session.post(url, data=body, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=15)) as resp:
                result = await resp.json()
                if resp.status in (200, 201):
                    return result
                else:
                    logger.error(f"POST {path} → {resp.status}: {result}")
                    return {"success": False, "error": str(result)}
        except asyncio.TimeoutError:
            logger.error(f"POST {path} timeout")
            return {"success": False, "error": "Request timeout"}
        except Exception as e:
            logger.error(f"POST {path} error: {e}")
            return {"success": False, "error": str(e)}

    async def get_previous_day_candle(self, symbol: str) -> Optional[dict]:
        delta_symbol = SYMBOL_MAP.get(symbol)
        if not delta_symbol:
            logger.error(f"Unknown symbol: {symbol}")
            return None

        now_ist = datetime.now(IST)
        yesterday_ist = (now_ist - timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        start_ts = int(yesterday_ist.timestamp())
        end_ts = start_ts + 86400

        params = {
            "resolution": "1d",
            "symbol": delta_symbol,
            "start": start_ts,
            "end": end_ts
        }

        result = await self._get("/v2/history/candles", params=params)
        if result and result.get("result"):
            candles = result["result"]
            if candles:
                c = candles[-1]
                return {
                    "open": float(c["open"]),
                    "high": float(c["high"]),
                    "low": float(c["low"]),
                    "close": float(c["close"]),
                    "volume": float(c.get("volume", 0)),
                    "time": c["time"]
                }
        logger.error(f"{symbol} | No daily candle data returned")
        return None

    async def get_latest_1m_candle(self, symbol: str) -> Optional[dict]:
        delta_symbol = SYMBOL_MAP.get(symbol)
        if not delta_symbol:
            return None

        now = int(time.time())
        start = now - 180

        params = {
            "resolution": "1m",
            "symbol": delta_symbol,
            "start": start,
            "end": now
        }

        result = await self._get("/v2/history/candles", params=params)
        if result and result.get("result"):
            candles = result["result"]
            if len(candles) >= 2:
                c = candles[-2]
                return {
                    "open": float(c["open"]),
                    "high": float(c["high"]),
                    "low": float(c["low"]),
                    "close": float(c["close"]),
                    "volume": float(c.get("volume", 0)),
                    "time": c["time"]
                }
        return None

    async def place_order(self, symbol: str, side: str, entry_price: float,
                          sl_price: float, leverage: int = 5) -> Optional[dict]:
        """
        Place a market order with stop loss.
        Position size = TRADE_SIZE_USD × leverage / entry_price
        Default: $30 × 5x = $150 notional
        """
        delta_symbol = SYMBOL_MAP.get(symbol)
        if not delta_symbol:
            return {"success": False, "error": f"Unknown symbol: {symbol}"}

        # Calculate quantity from fixed trade size
        notional = TRADE_SIZE_USD * leverage
        quantity = max(1, int(notional / entry_price))

        logger.info(
            f"{symbol} | Trade size: ${TRADE_SIZE_USD} | "
            f"Notional ({leverage}x): ${notional} | "
            f"Entry: {entry_price} | Qty: {quantity}"
        )

        # Set leverage
        await self._post("/v2/orders/leverage", {
            "product_symbol": delta_symbol,
            "leverage": str(leverage)
        })

        # Place market entry order
        order_payload = {
            "product_symbol": delta_symbol,
            "side": side.lower(),
            "order_type": "market_order",
            "size": quantity,
            "time_in_force": "ioc"
        }

        order_result = await self._post("/v2/orders", order_payload)
        if not order_result or order_result.get("success") is False:
            return {"success": False, "error": f"Entry order failed: {order_result}"}

        order_id = order_result.get("result", {}).get("id", "unknown")
        logger.info(f"{symbol} | Entry order placed: {order_id}")

        # Place stop loss order
        sl_side = "sell" if side == "BUY" else "buy"
        sl_payload = {
            "product_symbol": delta_symbol,
            "side": sl_side,
            "order_type": "stop_market_order",
            "size": quantity,
            "stop_price": str(round(sl_price, 4)),
            "reduce_only": True,
            "time_in_force": "gtc"
        }

        sl_result = await self._post("/v2/orders", sl_payload)
        if not sl_result or sl_result.get("success") is False:
            logger.error(f"{symbol} | SL order failed — retrying once")
            await asyncio.sleep(1)
            sl_result = await self._post("/v2/orders", sl_payload)
            if not sl_result or sl_result.get("success") is False:
                return {
                    "success": True,
                    "order_id": order_id,
                    "trade_usd": TRADE_SIZE_USD,
                    "quantity": quantity,
                    "warning": "SL order failed — manual SL placement required",
                    "sl_failed": True
                }

        sl_order_id = sl_result.get("result", {}).get("id", "unknown")
        logger.info(f"{symbol} | SL order placed: {sl_order_id}")

        return {
            "success": True,
            "order_id": order_id,
            "sl_order_id": sl_order_id,
            "trade_usd": TRADE_SIZE_USD,
            "quantity": quantity
        }
