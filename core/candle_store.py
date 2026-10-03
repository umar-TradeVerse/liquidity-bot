"""Shared 15-minute candle history for the sub-agents (2026-10-02).

Before this, the Pool Agent and the Liquidity Map each downloaded and kept
their own 500-candle history per symbol. Now one store does it once:
half the API calls, and both agents always see exactly the same candles.

Rules (identical to what each agent did on its own):
  * first use, or a gap of more than 1.5 candles (e.g. after the
    23:00-05:30 IST window), reloads the last HISTORY closed candles
  * the forming candle returned by the exchange is dropped
  * a live candle is appended only if newer than the last one stored

Consumers detect a reload through `gen[symbol]` (it increases on every
reload attempt), and use `from_backfill` / `ok` to decide what to do.
"""
from datetime import datetime, timezone

from utils.logger import setup_logger

logger = setup_logger("candle_store")

HISTORY = 500
GAP_MS = 900 * 1000 * 1.5


def _norm(c):
    return ({k: float(c[k]) for k in ("open", "high", "low", "close")}
            | {"time": int(c["time"]), "volume": float(c.get("volume") or 0)})


class CandleStore:
    def __init__(self, coindcx):
        self.cdx = coindcx
        self.hist = {}            # symbol -> [candles], oldest first
        self.gen = {}             # symbol -> reload counter
        self.ok = {}              # symbol -> did the last reload succeed?
        self.from_backfill = {}   # symbol -> was the latest candle already in the reload?

    async def _backfill(self, symbol):
        from exchange.coindcx import SYMBOL_MAP
        self.gen[symbol] = self.gen.get(symbol, 0) + 1
        try:
            now = int(datetime.now(timezone.utc).timestamp() * 1000)
            res = await self.cdx._get("/market_data/candles", params={
                "pair": SYMBOL_MAP[symbol], "interval": "15m",
                "from": now - HISTORY * 900 * 1000, "to": now, "limit": HISTORY})
            cs = sorted(res or [], key=lambda c: int(c["time"]))[:-1]   # drop forming candle
            self.hist[symbol] = [_norm(c) for c in cs]
            self.ok[symbol] = True
            logger.info(f"{symbol} | Candle history loaded: {len(self.hist[symbol])} candles (shared)")
        except Exception as e:
            self.hist[symbol] = []
            self.ok[symbol] = False
            logger.error(f"{symbol} | Candle history backfill failed ({e}) — building history live")

    async def update(self, symbol, candle):
        """Make sure history is current up to `candle`; returns the list.
        Safe to call from several agents for the same candle."""
        t = int(candle["time"])
        last = self.hist.get(symbol)
        if not last or t - last[-1]["time"] > GAP_MS:
            if last:
                logger.info(f"{symbol} | Candle gap detected — reloading shared history")
            await self._backfill(symbol)
            cs = self.hist[symbol]
            self.from_backfill[symbol] = bool(cs) and t <= cs[-1]["time"]
        cs = self.hist[symbol]
        if not cs or t > cs[-1]["time"]:
            cs.append(_norm(candle))
            del cs[:-HISTORY]
        return cs
