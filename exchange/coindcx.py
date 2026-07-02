"""
CoinDCX Futures API client for trading bot.
Authenticated API - supports market orders and stop_market_order for SL.
No IP whitelisting restrictions.
"""

import asyncio
import logging
import time
import hmac
import hashlib
import json
from typing import Optional
import aiohttp
from datetime import datetime, timedelta
import pytz

logger = logging.getLogger("coindcx")
IST = pytz.timezone("Asia/Kolkata")

COINDCX_BASE_URL = "https://api.coindcx.com"
COINDCX_PUBLIC_URL = "https://public.coindcx.com"

# Symbol mapping: Delta symbol → CoinDCX futures symbol
SYMBOL_MAP = {
    "ETHUSD": "B-ETH_USDT",
    "SOLUSD": "B-SOL_USDT",
    "XRPUSD": "B-XRP_USDT",
    "TAOUSD": "B-TAO_USDT",
    "XAUTUSD": "B-XAU_USDT",
}


class CoinDCXClient:
    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = COINDCX_BASE_URL
        self.public_url = COINDCX_PUBLIC_URL
        self._session: Optional[aiohttp.ClientSession] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def _generate_signature(self, body: str) -> str:
        """Generate HMAC-SHA256 signature for request body."""
        secret_bytes = bytes(self.api_secret, encoding='utf-8')
        signature = hmac.new(secret_bytes, body.encode(), hashlib.sha256).hexdigest()
        return signature

    async def _post(self, endpoint: str, data: dict, authenticated: bool = True) -> Optional[dict]:
        """Make POST request to CoinDCX API."""
        session = await self._get_session()
        url = self.base_url + endpoint
        
        json_body = json.dumps(data, separators=(',', ':'))
        
        headers = {
            'Content-Type': 'application/json',
        }
        
        if authenticated:
            signature = self._generate_signature(json_body)
            headers['X-AUTH-APIKEY'] = self.api_key
            headers['X-AUTH-SIGNATURE'] = signature
        
        try:
            async with session.post(url, data=json_body, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status in [200, 201]:
                    return await resp.json()
                else:
                    text = await resp.text()
                    logger.error(f"POST {endpoint} → {resp.status}: {text}")
                    return None
        except asyncio.TimeoutError:
            logger.error(f"POST {endpoint} timeout")
            return None
        except Exception as e:
            logger.error(f"POST {endpoint} error: {e}")
            return None

    async def _get(self, path: str, params: dict = None) -> Optional[dict]:
        """Make GET request to public API."""
        session = await self._get_session()
        url = self.public_url + path
        try:
            async with session.get(url, params=params,
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

    async def get_previous_day_candle(self, symbol: str) -> Optional[dict]:
        """
        Fetch the last completed daily candle for a symbol.
        Returns the second-to-last candle to ensure it's fully completed.
        """
        coindcx_symbol = SYMBOL_MAP.get(symbol)
        if not coindcx_symbol:
            logger.error(f"Unknown symbol: {symbol}")
            return None

        now_ist = datetime.now(IST)
        # Look back 7 days to handle weekends
        start_ts = int((now_ist - timedelta(days=7)).timestamp() * 1000)
        end_ts = int(now_ist.timestamp() * 1000)

        params = {
            "pair": coindcx_symbol,
            "interval": "1d",
            "from": start_ts,
            "to": end_ts,
            "limit": 10
        }

        result = await self._get("/market_data/candles", params=params)
        if result and isinstance(result, list) and len(result) > 0:
            candles = result
            # Filter out candles with zero volume (non-trading days)
            active_candles = [c for c in candles if float(c.get("volume", 0)) > 0]

            if not active_candles:
                active_candles = candles

            if len(active_candles) >= 2:
                # Take second-to-last = last FULLY completed trading day
                c = active_candles[-2]
            elif len(active_candles) == 1:
                c = active_candles[-1]
            else:
                logger.error(f"{symbol} | No candle data returned")
                return None

            pdh = float(c["high"])
            pdl = float(c["low"])
            logger.info(f"{symbol} | Previous day candle → H:{pdh} L:{pdl} V:{c.get('volume', 0)}")

            return {
                "open": float(c["open"]),
                "high": pdh,
                "low": pdl,
                "close": float(c["close"]),
                "volume": float(c.get("volume", 0)),
                "time": c["time"]
            }

        logger.error(f"{symbol} | No daily candle data returned")
        return None

    async def get_latest_5m_candle(self, symbol: str) -> Optional[dict]:
        """
        Fetch the latest completed 5m candle.
        Returns the second-to-last candle to ensure it's fully completed.
        """
        coindcx_symbol = SYMBOL_MAP.get(symbol)
        if not coindcx_symbol:
            return None

        now_ts = int(time.time() * 1000)
        start_ts = now_ts - (900 * 1000)  # 15 minutes lookback (3 candles)

        params = {
            "pair": coindcx_symbol,
            "interval": "5m",
            "from": start_ts,
            "to": now_ts,
            "limit": 5
        }

        result = await self._get("/market_data/candles", params=params)
        if result and isinstance(result, list) and len(result) >= 2:
            candles = result
            # Take second-to-last = last FULLY completed 5m candle
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

    async def place_market_order(self, symbol: str, side: str, quantity: float) -> Optional[dict]:
        """
        Place a market entry order.
        Returns: order ID on success, None on failure
        """
        coindcx_symbol = SYMBOL_MAP.get(symbol)
        if not coindcx_symbol:
            logger.error(f"Unknown symbol: {symbol}")
            return None

        timestamp = int(time.time() * 1000)
        body = {
            "pair": coindcx_symbol,
            "side": side.lower(),  # buy or sell
            "order_type": "market_order",
            "total_quantity": quantity,
            "margin_currency_short_name": "USDT",
            "timestamp": timestamp
        }

        result = await self._post("/exchange/v1/derivatives/futures/orders/create", body)
        if result:
            order_id = result.get("id")
            logger.info(f"{symbol} {side} market order placed: {order_id}")
            return {
                "id": order_id,
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "type": "market"
            }
        
        logger.error(f"{symbol} | Failed to place market order")
        return None

    async def place_stop_market_order(self, symbol: str, side: str, quantity: float, 
                                     stop_price: float) -> Optional[dict]:
        """
        Place a stop-market SL order (triggered when price hits stop_price).
        Returns: order ID on success, None on failure
        """
        coindcx_symbol = SYMBOL_MAP.get(symbol)
        if not coindcx_symbol:
            logger.error(f"Unknown symbol: {symbol}")
            return None

        # For stop_market_order, opposite side (if long entry, short SL)
        sl_side = "sell" if side.lower() == "buy" else "buy"
        
        timestamp = int(time.time() * 1000)
        body = {
            "pair": coindcx_symbol,
            "side": sl_side,
            "order_type": "stop_market_order",
            "total_quantity": quantity,
            "stop_price": stop_price,  # Trigger price
            "margin_currency_short_name": "USDT",
            "timestamp": timestamp
        }

        result = await self._post("/exchange/v1/derivatives/futures/orders/create", body)
        if result:
            order_id = result.get("id")
            logger.info(f"{symbol} SL order placed at {stop_price}: {order_id}")
            return {
                "id": order_id,
                "symbol": symbol,
                "side": sl_side,
                "quantity": quantity,
                "stop_price": stop_price,
                "type": "stop_market"
            }
        
        logger.error(f"{symbol} | Failed to place SL order")
        return None

    async def get_open_orders(self, symbol: str) -> Optional[list]:
        """Fetch open orders for a symbol."""
        coindcx_symbol = SYMBOL_MAP.get(symbol)
        if not coindcx_symbol:
            return None

        timestamp = int(time.time() * 1000)
        body = {
            "pair": coindcx_symbol,
            "timestamp": timestamp
        }

        result = await self._post("/exchange/v1/derivatives/futures/orders/list", body)
        if result:
            return result.get("orders", [])
        return None

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel an open order."""
        coindcx_symbol = SYMBOL_MAP.get(symbol)
        if not coindcx_symbol:
            return False

        timestamp = int(time.time() * 1000)
        body = {
            "id": order_id,
            "pair": coindcx_symbol,
            "timestamp": timestamp
        }

        result = await self._post("/exchange/v1/derivatives/futures/orders/cancel", body)
        if result:
            logger.info(f"Order {order_id} cancelled")
            return True
        
        logger.error(f"Failed to cancel order {order_id}")
        return False
