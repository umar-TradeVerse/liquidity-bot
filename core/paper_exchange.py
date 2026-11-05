"""Paper trading switch (2026-11-05).

TRADING_MODE=live   (default) -- real orders on CoinDCX, exactly as before.
TRADING_MODE=paper  -- the whole bot runs unchanged (strategy, Liquidity Map,
                       Risk / Context checks, INR tiers, daily halt, exits,
                       Telegram, trades.jsonl), but every call that would
                       place, change or close an order is answered by this
                       simulated exchange instead. No order ever reaches
                       CoinDCX. Market data still comes from CoinDCX.
Any other value is treated as PAPER (a typo must never mean real money).

How it works: PaperExchange wraps the real CoinDCXClient. Order and
position calls (place_market_order, close_position_market, update_stop_loss,
update_position_tpsl, cancel_order, get_open_positions, get_position_details,
get_last_fill, get_open_orders, get_futures_wallet_balance) are simulated;
everything else (candles, order book, trade feed) passes straight through.
Simulated positions are saved to PERSIST_DIR/paper_positions.json so a
restart does not lose them.

Fills: entries fill at the close of the latest 15m candle the bot fetched
(the same price the strategy uses as its entry); exits are booked by the bot
at its own exit price (stop / target / close), exactly as in live mode.
No slippage is modelled -- live results will be somewhat worse.
"""
import json
import os
import time
from typing import Optional

from utils.logger import setup_logger

logger = setup_logger("paper")

_raw_mode = os.getenv("TRADING_MODE", "live").strip().lower()
TRADING_MODE = "live" if _raw_mode == "live" else "paper"
if _raw_mode not in ("live", "paper"):
    logger.warning(f"TRADING_MODE={_raw_mode!r} not recognised — running in PAPER mode (fail safe)")
PAPER = TRADING_MODE == "paper"

_PATH = os.path.join(os.getenv("PERSIST_DIR", "/data"), "paper_positions.json")


class PaperExchange:
    """Drop-in stand-in for CoinDCXClient in paper mode."""

    paper = True

    def __init__(self, real, path: str = _PATH):
        self._real = real
        self._path = path
        self._last_close: dict = {}     # symbol -> latest 15m close seen
        self.positions: dict = {}       # symbol -> {qty (signed), entry, sl, tp, opened_ms}
        self.fills: dict = {}           # symbol -> last fill {price, side, timestamp}
        self._n = 0
        # Belt and braces: even a real-client method this class does not
        # override can never send a signed (account) request in paper mode.
        real._post = self._blocked_post
        self._load()

    @staticmethod
    async def _blocked_post(endpoint, data=None):
        logger.error(f"PAPER | blocked a signed CoinDCX request to {endpoint} — paper mode never "
                     f"touches the account")
        return None

    # ── pass-through for market data and anything not simulated ───────────
    def __getattr__(self, name):
        if name in ("_post",):
            raise AttributeError("PaperExchange: signed POST calls are disabled in paper mode")
        return getattr(self._real, name)

    async def get_latest_15m_candle(self, symbol: str):
        c = await self._real.get_latest_15m_candle(symbol)
        if c:
            self._last_close[symbol] = float(c["close"])
        return c

    # ── persistence ───────────────────────────────────────────────────────
    def _load(self):
        try:
            with open(self._path) as f:
                d = json.load(f)
            self.positions, self.fills = d.get("positions", {}), d.get("fills", {})
            if self.positions:
                logger.info(f"PAPER: restored {len(self.positions)} simulated position(s): "
                            f"{list(self.positions)}")
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.error(f"PAPER: could not load {self._path}: {e}")

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"positions": self.positions, "fills": self.fills}, f)
            os.replace(tmp, self._path)
        except Exception as e:
            logger.error(f"PAPER: could not save {self._path}: {e}")

    def _fill(self, symbol, side, price):
        self.fills[symbol] = {"price": price, "side": side.lower(),
                              "timestamp": int(time.time() * 1000)}

    # ── simulated order / position calls ──────────────────────────────────
    async def place_market_order(self, symbol: str, side: str, quantity: float,
                                 sl_price: float, leverage: int = 5) -> Optional[dict]:
        price = self._last_close.get(symbol)
        if price is None:
            c = await self.get_latest_15m_candle(symbol)
            price = float(c["close"]) if c else None
        if price is None:
            return {"id": None, "error": "PAPER: no price available"}
        signed = quantity if side == "BUY" else -quantity
        pos = self.positions.get(symbol)
        if pos:                                   # mirror a one-way futures account
            signed += pos["qty"]
        self._n += 1
        order_id = f"paper-{int(time.time())}-{self._n}"
        if abs(signed) < 1e-12:
            self.positions.pop(symbol, None)
        else:
            self.positions[symbol] = {"qty": signed, "entry": price, "sl": sl_price, "tp": None,
                                      "opened_ms": int(time.time() * 1000), "leverage": leverage}
        self._fill(symbol, side, price)
        self._save()
        logger.info(f"PAPER | {symbol} {side} {quantity:.6g} filled at {price:.6g} "
                    f"(SL {sl_price:.6g}) — no real order sent")
        return {"id": order_id, "symbol": symbol, "side": side, "quantity": quantity,
                "sl_price": sl_price, "type": "market_with_sl", "leverage": leverage}

    async def close_position_market(self, symbol: str, side: str, quantity: float) -> bool:
        pos = self.positions.pop(symbol, None)
        if pos:
            px = self._last_close.get(symbol, pos["entry"])
            self._fill(symbol, "SELL" if pos["qty"] > 0 else "BUY", px)
            logger.info(f"PAPER | {symbol} position closed (simulated)")
        self._save()
        return True

    async def update_stop_loss(self, symbol: str, new_sl_price: float) -> bool:
        if symbol in self.positions:
            self.positions[symbol]["sl"] = new_sl_price
            self._save()
        return True

    async def update_position_tpsl(self, symbol: str, sl_price: Optional[float] = None,
                                   tp_price: Optional[float] = None) -> bool:
        pos = self.positions.get(symbol)
        if pos:
            if sl_price is not None:
                pos["sl"] = sl_price
            if tp_price is not None:
                pos["tp"] = tp_price
            self._save()
        return True

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        return True

    async def get_open_orders(self, symbol: str) -> Optional[list]:
        return []

    async def get_open_positions(self) -> dict:
        return {s: p["qty"] for s, p in self.positions.items()}

    async def get_position_details(self, symbol: str) -> Optional[dict]:
        pos = self.positions.get(symbol)
        if not pos:
            return None
        mark = self._last_close.get(symbol, pos["entry"])
        pnl = (mark - pos["entry"]) * pos["qty"]
        margin = abs(pos["qty"]) * pos["entry"] / (pos.get("leverage") or 1)
        return {"id": None, "active_pos": pos["qty"], "roe": pnl / margin * 100 if margin else None,
                "pnl": pnl, "mark_price": mark, "raw": {"paper": True}}

    async def get_last_fill(self, symbol: str) -> Optional[dict]:
        return self.fills.get(symbol)

    async def get_futures_wallet_balance(self) -> Optional[float]:
        return None
