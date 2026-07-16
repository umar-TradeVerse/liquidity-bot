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

SYMBOL_MAP = {
    "ETHUSD": "B-ETH_USDT",
    "SOLUSD": "B-SOL_USDT",
    "XRPUSD": "B-XRP_USDT",
    "TAOUSD": "B-TAO_USDT",
    "AEROUSD": "B-AERO_USDT",
    "BTCUSD": "B-BTC_USDT",
    "LTCUSD": "B-LTC_USDT",
    "ICPUSD": "B-ICP_USDT",
    "KAITOUSD": "B-KAITO_USDT"
}

REVERSE_SYMBOL_MAP = {v: k for k, v in SYMBOL_MAP.items()}


class CoinDCXClient:
    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = COINDCX_BASE_URL
        self.public_url = COINDCX_PUBLIC_URL
        self._session: Optional[aiohttp.ClientSession] = None
        self.last_error: Optional[str] = None
        self._instrument_cache: dict = {}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def _generate_signature(self, body: str) -> str:
        secret_bytes = bytes(self.api_secret, encoding='utf-8')
        signature = hmac.new(secret_bytes, body.encode(), hashlib.sha256).hexdigest()
        return signature

    async def _post(self, endpoint: str, data: dict) -> Optional[dict]:
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
        coindcx_symbol = SYMBOL_MAP.get(symbol)
        if not coindcx_symbol:
            logger.error(f"Unknown symbol: {symbol}")
            return None

        now_ist = datetime.now(IST)
        yesterday_date = (now_ist - timedelta(days=1)).date()
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

        match = None
        for c in result:
            candle_date = datetime.fromtimestamp(int(c["time"]) / 1000, IST).date()
            if candle_date == yesterday_date:
                match = c
                break

        if match is None:
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
        coindcx_symbol = SYMBOL_MAP.get(symbol)
        if not coindcx_symbol:
            logger.error(f"{symbol} | Unknown symbol, no CoinDCX mapping")
            return None

        now_ts = int(time.time() * 1000)
        start_ts = now_ts - (7200 * 1000)

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

        try:
            candles = sorted(result, key=lambda c: int(c["time"]))
        except (KeyError, ValueError, TypeError) as e:
            logger.error(f"{symbol} | Failed to sort 15m candles: {e}")
            return None

        if len(candles) < 2:
            logger.warning(f"{symbol} | Only {len(candles)} candle(s) returned, need >=2")
            return None

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
        if not increment or increment <= 0:
            return quantity
        if mode == "nearest":
            steps = round(quantity / increment)
        else:
            steps = math.floor(quantity / increment + 1e-9)
        rounded = steps * increment
        inc_str = f"{increment:.10f}".rstrip('0')
        decimals = len(inc_str.split('.')[1]) if '.' in inc_str else 0
        return round(rounded, decimals)

    async def get_futures_wallet_balance(self) -> Optional[float]:
        """
        Fetch available (free/unused) margin balance in USDT from the
        futures wallet. Used as a pre-flight check before placing new
        orders so a valid signal isn't wasted on an order that will
        fail with 'Insufficient funds' (observed repeatedly on TAOUSD).

        NOTE: field names below (available_balance / balance,
        currency_short_name / currency) are a best-effort guess at
        CoinDCX's futures wallet response shape and have NOT been
        verified against a live response. If this logs the "could not
        fetch" warning on every call, the parsing below needs to be
        corrected to match the actual JSON keys CoinDCX returns —
        check a raw response once and adjust. Until then this fails
        open (returns None -> pre-check is skipped, trading proceeds
        as before), it never blocks trading due to a parsing mismatch.
        """
        timestamp = int(time.time() * 1000)
        body = {"timestamp": timestamp}
        result = await self._post("/exchange/v1/derivatives/futures/wallets", body)

        if not result:
            logger.warning("Could not fetch futures wallet balance — "
                           "proceeding without an insufficient-funds pre-check")
            return None

        if isinstance(result, list):
            entries = result
        elif isinstance(result, dict):
            entries = result.get("wallets", result.get("data", []))
        else:
            entries = []

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            currency = entry.get("currency_short_name") or entry.get("currency") or entry.get("asset")
            if currency and currency.upper() == "USDT":
                for key in ("available_balance", "available_margin", "free_balance", "balance"):
                    if key in entry and entry[key] is not None:
                        try:
                            return float(entry[key])
                        except (TypeError, ValueError):
                            continue
                logger.warning(f"USDT wallet entry found but no recognizable balance field: {entry}")
                return None

        logger.warning("USDT wallet entry not found in futures wallet response — "
                       "proceeding without an insufficient-funds pre-check")
        return None

    async def place_market_order(self, symbol: str, side: str, quantity: float,
                                sl_price: float, leverage: int = 5) -> Optional[dict]:
        coindcx_symbol = SYMBOL_MAP.get(symbol)
        if not coindcx_symbol:
            logger.error(f"Unknown symbol: {symbol}")
            return None

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

            notional = quantity * sl_price
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

        body = {
            "timestamp": timestamp,
            "order": {
                "side": side.lower(),
                "pair": coindcx_symbol,
                "order_type": "market_order",
                "total_quantity": quantity,
                "leverage": leverage,
                "stop_loss_price": sl_price,
                "notification": "email_notification",
                "time_in_force": "good_till_cancel",
                "hidden": False,
                "post_only": False
            }
        }

        result = await self._post("/exchange/v1/derivatives/futures/orders/create", body)
        if result:
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
            logger.info(f"{symbol} {side} market order placed with SL @ {sl_price} "
                       f"leverage={leverage}x: {order_id}")
            return {
                "id": order_id,
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "sl_price": sl_price,
                "type": "market_with_sl",
                "leverage": leverage
            }

        logger.error(f"{symbol} | Failed to place market order with SL: {self.last_error}")
        return {"id": None, "error": self.last_error or "Unknown error"}

    async def get_open_positions(self) -> dict:
        timestamp = int(time.time() * 1000)
        body = {"timestamp": timestamp}

        result = await self._post("/exchange/v1/derivatives/futures/positions", body)

        if isinstance(result, list):
            entries = result
        elif isinstance(result, dict) and "positions" in result:
            entries = result["positions"]
        else:
            if result is not None:
                logger.warning(f"Unexpected positions response shape: {type(result)}")
            entries = []

        positions = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            pair = entry.get("pair")
            active = entry.get("active_pos", 0) or 0
            try:
                active = float(active)
            except (TypeError, ValueError):
                active = 0.0
            internal_symbol = REVERSE_SYMBOL_MAP.get(pair)
            if internal_symbol and active != 0:
                positions[internal_symbol] = active

        return positions

    async def get_position_details(self, symbol: str) -> Optional[dict]:
        """
        Fetch this symbol's open position and compute ROE manually.
        CoinDCX's position response does NOT include a pre-computed ROE
        field (confirmed from live data) — it must be derived the same
        way CoinDCX's own UI does:
            pnl = (mark_price - avg_price) * active_pos          [LONG]
            pnl = (avg_price - mark_price) * active_pos          [SHORT]
            roe_pct = (pnl / locked_user_margin) * 100
        Verified against a live CoinDCX position screenshot — matches
        CoinDCX's own displayed ROE% exactly.
        """
        coindcx_symbol = SYMBOL_MAP.get(symbol)
        if not coindcx_symbol:
            return None

        timestamp = int(time.time() * 1000)
        body = {"timestamp": timestamp}
        result = await self._post("/exchange/v1/derivatives/futures/positions", body)
        entries = result if isinstance(result, list) else []

        for entry in entries:
            if not isinstance(entry, dict) or entry.get("pair") != coindcx_symbol:
                continue
            active = entry.get("active_pos", 0) or 0
            try:
                active = float(active)
            except (TypeError, ValueError):
                active = 0.0
            if active == 0:
                continue

            try:
                avg_price = float(entry.get("avg_price", 0) or 0)
                mark_price = float(entry.get("mark_price", 0) or 0)
                margin = float(entry.get("locked_user_margin", 0) or 0)
            except (TypeError, ValueError):
                logger.warning(f"{symbol} | Could not parse avg_price/mark_price/margin "
                               f"for ROE calc — raw entry: {entry}")
                return {"id": entry.get("id"), "active_pos": active, "roe": None, "raw": entry}

            if margin <= 0:
                logger.warning(f"{symbol} | locked_user_margin is zero/missing — "
                               f"cannot compute ROE, raw entry: {entry}")
                return {"id": entry.get("id"), "active_pos": active, "roe": None, "raw": entry}

            if active > 0:
                pnl = (mark_price - avg_price) * active
            else:
                pnl = (avg_price - mark_price) * abs(active)

            roe = (pnl / margin) * 100

            return {"id": entry.get("id"), "active_pos": active, "roe": roe,
                    "pnl": pnl, "mark_price": mark_price, "raw": entry}

        return None

    async def get_open_orders(self, symbol: str) -> Optional[list]:
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
            return result
        if result:
            return result.get("orders", [])
        return None

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
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

    async def close_position_market(self, symbol: str, side: str, quantity: float) -> bool:
        coindcx_symbol = SYMBOL_MAP.get(symbol)
        if not coindcx_symbol:
            logger.error(f"Unknown symbol: {symbol}")
            return False

        close_side = "sell" if side.upper() == "BUY" else "buy"

        instrument = await self._get_instrument_details(coindcx_symbol)
        if instrument and instrument["quantity_increment"] > 0:
            quantity = self._round_to_increment(quantity, instrument["quantity_increment"])

        timestamp = int(time.time() * 1000)
        body = {
            "timestamp": timestamp,
            "order": {
                "side": close_side,
                "pair": coindcx_symbol,
                "order_type": "market_order",
                "total_quantity": quantity,
                "notification": "email_notification",
                "time_in_force": "good_till_cancel",
                "hidden": False,
                "post_only": False
                # reduce_only intentionally omitted: CoinDCX rejects
                # reduce_only on market orders with 400 "Reduce Only Order
                # is only applicable for Limit Order". Caller is now
                # responsible for passing an accurate, freshly-fetched
                # quantity (see monitor.py _exit_position) since there's
                # no exchange-side cap protecting against overshoot.
            }
        }

        result = await self._post("/exchange/v1/derivatives/futures/orders/create", body)
        if result:
            logger.info(f"{symbol} | Position closed via {close_side} market order (qty {quantity})")
            return True

        logger.error(f"{symbol} | Failed to close position: {self.last_error}")
        return False

    async def update_stop_loss(self, symbol: str, new_sl_price: float) -> bool:
        coindcx_symbol = SYMBOL_MAP.get(symbol)
        if not coindcx_symbol:
            logger.error(f"{symbol} | Unknown symbol, cannot update SL")
            return False

        timestamp = int(time.time() * 1000)

        positions_result = await self._post(
            "/exchange/v1/derivatives/futures/positions",
            {"timestamp": timestamp}
        )
        entries = positions_result if isinstance(positions_result, list) else []

        position_id = None
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("pair") != coindcx_symbol:
                continue
            active = entry.get("active_pos", 0) or 0
            try:
                active = float(active)
            except (TypeError, ValueError):
                active = 0.0
            if active != 0:
                position_id = entry.get("id")
                break

        if not position_id:
            logger.error(f"{symbol} | Could not find an open position id — SL not updated")
            return False

        instrument = await self._get_instrument_details(coindcx_symbol)
        if instrument and instrument["price_increment"] > 0:
            new_sl_price = self._round_to_increment(
                new_sl_price, instrument["price_increment"], mode="nearest"
            )

        body = {
            "timestamp": timestamp,
            "id": position_id,
            "stop_loss": {
                "stop_price": new_sl_price,
                "limit_price": new_sl_price,
                "order_type": "stop_market"
            }
        }

        result = await self._post(
            "/exchange/v1/derivatives/futures/positions/create_tpsl", body
        )
        if result:
            logger.info(f"{symbol} | Stop-loss updated to {new_sl_price} "
                       f"(position {position_id})")
            return True

        logger.error(f"{symbol} | Failed to update stop-loss: {self.last_error}")
        return False

    async def update_position_tpsl(self, symbol: str, sl_price: Optional[float] = None,
                                    tp_price: Optional[float] = None) -> bool:
        if sl_price is None and tp_price is None:
            return False

        coindcx_symbol = SYMBOL_MAP.get(symbol)
        if not coindcx_symbol:
            logger.error(f"{symbol} | Unknown symbol, cannot update TP/SL")
            return False

        timestamp = int(time.time() * 1000)
        positions_result = await self._post(
            "/exchange/v1/derivatives/futures/positions",
            {"timestamp": timestamp}
        )
        entries = positions_result if isinstance(positions_result, list) else []

        position_id = None
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("pair") != coindcx_symbol:
                continue
            active = entry.get("active_pos", 0) or 0
            try:
                active = float(active)
            except (TypeError, ValueError):
                active = 0.0
            if active != 0:
                position_id = entry.get("id")
                break

        if not position_id:
            logger.error(f"{symbol} | Could not find an open position id — TP/SL not updated")
            return False

        instrument = await self._get_instrument_details(coindcx_symbol)
        if instrument and instrument["price_increment"] > 0:
            if sl_price is not None:
                sl_price = self._round_to_increment(sl_price, instrument["price_increment"], mode="nearest")
            if tp_price is not None:
                tp_price = self._round_to_increment(tp_price, instrument["price_increment"], mode="nearest")

        body = {"timestamp": timestamp, "id": position_id}
        if sl_price is not None:
            body["stop_loss"] = {"stop_price": sl_price, "limit_price": sl_price, "order_type": "stop_market"}
        if tp_price is not None:
            body["take_profit"] = {"stop_price": tp_price, "limit_price": tp_price, "order_type": "take_profit_market"}

        result = await self._post("/exchange/v1/derivatives/futures/positions/create_tpsl", body)
        if result:
            logger.info(f"{symbol} | TP/SL updated — SL:{sl_price} TP:{tp_price} (position {position_id})")
            return True

        logger.error(f"{symbol} | Failed to update TP/SL: {self.last_error}")
        return False
