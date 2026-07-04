"""
CoinDCX Futures API client for trading bot.
Corrected format with nested body structure and built-in SL mechanism.
Single order placement with integrated stop_loss_price (no separate SL order).
"""

import asyncio
import logging
import math
import time
import hmac
import hashlib
import json
from typing import Optional
import aiohttp
from datetime import datetime, timedelta
import pytz

from utils.logger import setup_logger
logger = setup_logger("coindcx")
IST = pytz.timezone("Asia/Kolkata")

COINDCX_BASE_URL = "https://api.coindcx.com"
COINDCX_PUBLIC_URL = "https://public.coindcx.com"

# Symbol mapping: Delta symbol → CoinDCX USDT futures symbol
SYMBOL_MAP = {
    "ETHUSD": "B-ETH_USDT",
    "SOLUSD": "B-SOL_USDT",
    "XRPUSD": "B-XRP_USDT",
    "TAOUSD": "B-TAO_USDT",
    "AEROUSD": "B-AERO_USDT",
    "BTCUSD": "B-BTC_USDT",
    "DOGEUSD": "B-DOGE_USDT",
    "ADAUSD": "B-ADA_USDT",
    "LINKUSD": "B-LINK_USDT",
    "AVAXUSD": "B-AVAX_USDT",
    "DOTUSD": "B-DOT_USDT",
    "LTCUSD": "B-LTC_USDT",
    "TRXUSD": "B-TRX_USDT",
    "SUIUSD": "B-SUI_USDT"
}


class CoinDCXClient:
    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = COINDCX_BASE_URL
        self.public_url = COINDCX_PUBLIC_URL
        self._session: Optional[aiohttp.ClientSession] = None
        self.last_error: Optional[str] = None
        self._instrument_cache: dict = {}  # symbol -> {"quantity_increment": ..., "min_quantity": ..., "min_notional": ...}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def _generate_signature(self, body: str) -> str:
        """Generate HMAC-SHA256 signature for request body."""
        secret_bytes = bytes(self.api_secret, encoding='utf-8')
        signature = hmac.new(secret_bytes, body.encode(), hashlib.sha256).hexdigest()
        return signature

    async def _post(self, endpoint: str, data: dict) -> Optional[dict]:
        """Make POST request to CoinDCX API with signature."""
        session = await self._get_session()
        url = self.base_url + endpoint

        json_body = json.dumps(data, separators=(',', ':'))

        signature = self._generate_signature(json_body)
        headers = {
            'Content-Type': 'application/json',
            'X-AUTH-APIKEY': self.api_key,
            'X-AUTH-SIGNATURE': signature
        }

        try:
            async with session.post(url, data=json_body, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status in [200, 201]:
                    self.last_error = None
                    return await resp.json()
                else:
                    text = await resp.text()
                    logger.error(f"POST {endpoint} → {resp.status}: {text}")
                    self.last_error = text
                    return None
        except asyncio.TimeoutError:
            logger.error(f"POST {endpoint} timeout")
            self.last_error = "Request timed out"
            return None
        except Exception as e:
            logger.error(f"POST {endpoint} error: {e}")
            self.last_error = str(e)
            return None

    async def _get(self, path: str, params: dict = None) -> Optional[dict]:
        """Make GET request to public API."""
        session = await self._get_session()
        url = self.public_url + path
        try:
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=30)) as resp:
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

    async def _get_base(self, path: str, params: dict = None) -> Optional[dict]:
        """Make an unauthenticated GET request against api.coindcx.com
        (as opposed to public.coindcx.com used by _get). Used for endpoints
        like instrument details that live on the main API domain."""
        session = await self._get_session()
        url = self.base_url + path
        try:
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=30)) as resp:
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
        Matches candles by actual IST calendar date instead of assuming
        array position/order — this avoids picking a stale candle if the
        API returns results in an unexpected order or with gaps.
        """
        coindcx_symbol = SYMBOL_MAP.get(symbol)
        if not coindcx_symbol:
            logger.error(f"Unknown symbol: {symbol}")
            return None

        now_ist = datetime.now(IST)
        yesterday_date = (now_ist - timedelta(days=1)).date()

        # Look back 10 days to comfortably cover weekends/gaps
        start_ts = int((now_ist - timedelta(days=10)).timestamp() * 1000)
        end_ts = int(now_ist.timestamp() * 1000)

        params = {
            "pair": coindcx_symbol,
            "interval": "1d",
            "from": start_ts,
            "to": end_ts,
            "limit": 15
        }

        result = await self._get("/market_data/candles", params=params)

        # DIAGNOSTIC: show every candle's date so ordering/gaps are visible
        if result and isinstance(result, list):
            debug_dates = [
                (datetime.fromtimestamp(int(c["time"]) / 1000, IST).date().isoformat(), c.get("high"), c.get("low"))
                for c in result
            ]
            logger.info(f"{symbol} | Daily candles returned (date, H, L): {debug_dates} "
                        f"| target=yesterday={yesterday_date.isoformat()}")

        if not result or not isinstance(result, list) or len(result) == 0:
            logger.error(f"{symbol} | No daily candle data returned")
            return None

        # Find the candle whose IST calendar date matches yesterday exactly
        match = None
        for c in result:
            candle_date = datetime.fromtimestamp(int(c["time"]) / 1000, IST).date()
            if candle_date == yesterday_date:
                match = c
                break

        if match is None:
            # Fallback: closest date before today, in case yesterday had zero volume
            # or wasn't returned (e.g. brand-new listing) — pick the most recent
            # candle that is strictly before today's date.
            candidates = [
                c for c in result
                if datetime.fromtimestamp(int(c["time"]) / 1000, IST).date() < now_ist.date()
            ]
            if candidates:
                match = max(candidates, key=lambda c: int(c["time"]))
                match_date = datetime.fromtimestamp(int(match["time"]) / 1000, IST).date()
                logger.warning(f"{symbol} | Exact 'yesterday' candle not found — "
                               f"using closest prior date {match_date.isoformat()} instead")
            else:
                logger.error(f"{symbol} | No usable prior-day candle found in response")
                return None

        pdh = float(match["high"])
        pdl = float(match["low"])
        logger.info(f"{symbol} | Previous day candle → H:{pdh} L:{pdl} V:{match.get('volume', 0)}")

        return {
            "open": float(match["open"]),
            "high": pdh,
            "low": pdl,
            "close": float(match["close"]),
            "volume": float(match.get("volume", 0)),
            "time": match["time"]
        }

    async def get_latest_15m_candle(self, symbol: str) -> Optional[dict]:
        """
        Fetch the latest completed 15-minute candle.
        Sorts candles explicitly by timestamp (ascending) before picking the
        second-to-last one — do not trust the API's return order.
        """
        coindcx_symbol = SYMBOL_MAP.get(symbol)
        if not coindcx_symbol:
            logger.error(f"{symbol} | Unknown symbol, no CoinDCX mapping")
            return None

        now_ts = int(time.time() * 1000)
        start_ts = now_ts - (7200 * 1000)  # 2 hours lookback (8 candles)

        params = {
            "pair": coindcx_symbol,
            "interval": "15m",
            "from": start_ts,
            "to": now_ts,
            "limit": 10
        }

        result = await self._get("/market_data/candles", params=params)

        if not result or not isinstance(result, list) or len(result) == 0:
            logger.warning(f"{symbol} | Empty/None response from 15m candles endpoint")
            return None

        # Sort explicitly by timestamp ascending — never trust API order
        try:
            candles = sorted(result, key=lambda c: int(c["time"]))
        except (KeyError, ValueError, TypeError) as e:
            logger.error(f"{symbol} | Failed to sort 15m candles: {e}")
            return None

        if len(candles) < 2:
            logger.warning(f"{symbol} | Only {len(candles)} candle(s) returned, need >=2")
            return None

        # Second-to-last after sorting ascending = most recently FULLY completed candle
        c = candles[-2]

        return {
            "open": float(c["open"]),
            "high": float(c["high"]),
            "low": float(c["low"]),
            "close": float(c["close"]),
            "volume": float(c.get("volume", 0)),
            "time": c["time"]
        }

    async def _get_instrument_details(self, coindcx_symbol: str) -> Optional[dict]:
        """
        Fetch and cache instrument details (quantity_increment, min_quantity,
        min_notional) for a symbol. CoinDCX requires order quantity to be an
        exact multiple of quantity_increment — this is fetched live instead
        of hardcoded so it stays correct for all symbols automatically.
        """
        if coindcx_symbol in self._instrument_cache:
            return self._instrument_cache[coindcx_symbol]

        result = await self._get_base(
            "/exchange/v1/derivatives/futures/data/instrument",
            params={"pair": coindcx_symbol}
        )

        if not result or "instrument" not in result:
            logger.warning(f"{coindcx_symbol} | Could not fetch instrument details — "
                            f"quantity will NOT be rounded to step size")
            return None

        inst = result["instrument"]
        # Sanity check: make sure the API actually returned details for the
        # symbol we asked for, not a mismatched/default one.
        if inst.get("pair") != coindcx_symbol:
            logger.warning(f"{coindcx_symbol} | Instrument details returned for "
                            f"mismatched pair {inst.get('pair')} — ignoring, "
                            f"quantity will NOT be rounded to step size")
            return None

        details = {
            "quantity_increment": float(inst.get("quantity_increment", 0) or 0),
            "min_quantity": float(inst.get("min_quantity", 0) or 0),
            "min_notional": float(inst.get("min_notional", 0) or 0),
            "price_increment": float(inst.get("price_increment", 0) or 0),
        }
        self._instrument_cache[coindcx_symbol] = details
        logger.info(f"{coindcx_symbol} | Instrument details cached: {details}")
        return details

    @staticmethod
    def _round_to_increment(quantity: float, increment: float, mode: str = "floor") -> float:
        """
        Round to the nearest valid multiple of increment.
        mode="floor"   -> round down (used for order quantity, so we never
                           request more than the calculated size)
        mode="nearest" -> round to closest (used for SL price, where over/under
                           by half a tick doesn't matter, just needs to be valid)
        """
        if not increment or increment <= 0:
            return quantity
        if mode == "nearest":
            steps = round(quantity / increment)
        else:
            steps = math.floor(quantity / increment + 1e-9)
        rounded = steps * increment
        # Determine decimal places from the increment itself to avoid
        # floating point noise like 0.30000000004
        inc_str = f"{increment:.10f}".rstrip('0')
        decimals = len(inc_str.split('.')[1]) if '.' in inc_str else 0
        return round(rounded, decimals)

    async def place_market_order(self, symbol: str, side: str, quantity: float,
                                sl_price: float) -> Optional[dict]:
        """
        Place a market order with integrated SL using stop_loss_price.
        This is a SINGLE order with built-in SL, not two separate orders.

        Returns: order response dict on success, None on failure
        """
        coindcx_symbol = SYMBOL_MAP.get(symbol)
        if not coindcx_symbol:
            logger.error(f"Unknown symbol: {symbol}")
            return None

        # Round quantity to the exchange's required step size before sending.
        instrument = await self._get_instrument_details(coindcx_symbol)
        if instrument and instrument["quantity_increment"] > 0:
            original_quantity = quantity
            quantity = self._round_to_increment(quantity, instrument["quantity_increment"])
            if quantity != original_quantity:
                logger.info(f"{symbol} | Quantity rounded from {original_quantity} to "
                            f"{quantity} (step size {instrument['quantity_increment']})")

            if quantity <= 0 or (instrument["min_quantity"] and quantity < instrument["min_quantity"]):
                logger.error(f"{symbol} | Rounded quantity {quantity} is below minimum "
                             f"{instrument['min_quantity']} — trade size too small for this "
                             f"symbol's step size, order not sent")
                return {"id": None, "error": f"Quantity {quantity} below exchange minimum "
                                             f"{instrument['min_quantity']} after rounding"}

            notional = quantity * sl_price  # rough check, entry price not available here
            if instrument["min_notional"] and notional < instrument["min_notional"]:
                logger.warning(f"{symbol} | Order notional ~{notional:.2f} may be below "
                               f"exchange minimum {instrument['min_notional']}")

            if instrument["price_increment"] > 0:
                original_sl = sl_price
                sl_price = self._round_to_increment(sl_price, instrument["price_increment"], mode="nearest")
                if sl_price != original_sl:
                    logger.info(f"{symbol} | SL price rounded from {original_sl} to {sl_price} "
                                f"(tick size {instrument['price_increment']})")

        timestamp = int(time.time() * 1000)

        # Nested body structure as per CoinDCX API
        body = {
            "timestamp": timestamp,
            "order": {
                "side": side.lower(),  # buy or sell
                "pair": coindcx_symbol,
                "order_type": "market_order",
                "total_quantity": quantity,
                "leverage": 5,
                "stop_loss_price": sl_price,  # Built-in SL
                "notification": "email_notification",
                "time_in_force": "good_till_cancel",
                "hidden": False,
                "post_only": False
            }
        }

        result = await self._post("/exchange/v1/derivatives/futures/orders/create", body)
        if result:
            # CoinDCX has been observed returning either a single order dict
            # OR a list containing one order dict for this endpoint. Normalize
            # to a dict before reading fields, instead of assuming one shape.
            if isinstance(result, list):
                if len(result) == 0:
                    logger.error(f"{symbol} | Order response was an empty list — "
                                 f"order may or may not have been placed, verify manually on CoinDCX")
                    return {"id": None, "error": "Empty list response from order create — verify manually"}
                order_obj = result[0]
                logger.warning(f"{symbol} | Order response was a list, not a dict — "
                               f"used first element. Raw response: {result}")
            else:
                order_obj = result

            order_id = order_obj.get("id")
            logger.info(f"{symbol} {side} market order placed with SL @ {sl_price}: {order_id}")
            return {
                "id": order_id,
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "sl_price": sl_price,
                "type": "market_with_sl",
                "leverage": 5
            }

        logger.error(f"{symbol} | Failed to place market order with SL: {self.last_error}")
        return {"id": None, "error": self.last_error or "Unknown error"}

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
        if isinstance(result, list):
            # This endpoint may return the orders list directly rather than
            # wrapped in {"orders": [...]}
            return result
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
