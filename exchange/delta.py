"""
Delta Exchange API client.
Handles: candle fetching, wallet balance, order placement with SL.

Position sizing: 25% of total wallet balance per trade, 5x leverage.
Trading days: Monday to Friday only (checked in monitor.py).
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
    "ETHUSD": "ETHUSD",
    "SOLUSD": "SOLUSD",
    "XRPUSD": "XRPUSD",
    "TAOUSD": "TAOUSD",
    "ICPUSD": "ICPUSD",
}

FIXED_LEVERAGE = 5
WALLET_TRADE_PCT = 0.25  # 25% of total wallet balance per trade


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
        """Generate Delta Exchange API signature headers."""
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

    async def get_wallet_balance(self) -> Optional[float]:
        """
        Fetch total wallet balance in USD from Delta Exchange.
        Returns total balance as float, or None on failure.
        """
        result = await self._get("/v2/wallet/balances", auth=True)
        if result and result.get("result"):
            balances = result["result"]
            for asset in balances:
                # Delta returns balances per asset — look for USDT or USD
                if asset.get("asset_symbol") in ("USDT", "USD"):
                    total = float(asset.get("balance", 0))
                    logger.info(f"Wallet balance: ${total:.2f}")
                    return total
        logger.error("Could not fetch wallet balance")
        return None

    async def get_previous_day_candle(self, symbol: str) -> Optional[dict]:
        """Fetch the completed previous day candle (1d resolution)."""
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
        """Fetch the most recently completed 1-minute candle."""
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
        Place a market order with stop loss on Delta Exchange.
        Position size = 25% of total wallet balance × 5x leverage.

        Steps:
          1. Fetch wallet balance
          2. Calculate quantity
          3. Set leverage
          4. Place market order
          5. Place stop loss order
        """
        delta_symbol = SYMBOL_MAP.get(symbol)
        if not delta_symbol:
            return {"success": False, "error": f"Unknown symbol: {symbol}"}

        # Step 1: Fetch wallet balance
        wallet_balance = await self.get_wallet_balance()
        if not wallet_balance or wallet_balance <= 0:
            return {"success": False, "error": "Could not fetch wallet balance"}

        # Step 2: Calculate position size
        # 25% of wallet × 5x leverage = notional position value
        trade_usd = wallet_balance * WALLET_TRADE_PCT
        notional = trade_usd * leverage
        quantity = max(1, int(notional / entry_price))

        logger.info(
            f"{symbol} | Wallet: ${wallet_balance:.2f} | "
            f"Trade size (25%): ${trade_usd:.2f} | "
            f"Notional (5x): ${notional:.2f} | "
            f"Qty: {quantity}"
        )

        # Step 3: Set leverage
        await self._post("/v2/orders/leverage", {
            "product_symbol": delta_symbol,
            "leverage": str(leverage)
        })

        # Step 4: Place market entry order
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

        # Step 5: Place stop loss order
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
                    "trade_usd": trade_usd,
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
            "trade_usd": trade_usd,
            "quantity": quantity
        }
