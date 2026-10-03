"""Order-flow measurements (2026-10-03) — LOG-ONLY, never makes or blocks a trade.

Built from the 'Complete Orderflow Trading Course' to collect the evidence
needed for the sweep-vs-breakout question:

PHASE 1 (data the bot already receives)
  * Relative volume  — this candle's volume vs the average of the last 20
                        ("trapped traders": heavy volume on the sweep wick)
  * Volume profile   — today's (UTC day = from 05:30 IST) VPOC / VAH / VAL,
                        built from 15m candles: each candle's volume is spread
                        evenly across its high-low range into price bins.

PHASE 2 (CoinDCX public futures trade feed — being VERIFIED live)
  * Delta per candle — aggressive buy volume minus aggressive sell volume
  * CVD              — running delta for the day (resets at 05:30 IST)
  The feed is polled every loop; trades are de-duplicated and bucketed into
  their 15m candle. Coverage is checked: if a poll's oldest trade is newer
  than the last trade already seen, trades were missed and the candle is
  marked 'gap'. A one-time PROBE line logs the raw response so the field
  meanings (especially which side was the aggressor) can be confirmed.
  Until confirmed, delta lines carry '[unverified]'.
"""
import time
from collections import deque

from utils.logger import setup_logger

logger = setup_logger("orderflow")

RVOL_LOOKBACK = 20
VP_BINS = 40
VALUE_AREA = 0.70
CANDLE_MS = 900 * 1000

# Candidate CoinDCX endpoints for the public futures trade feed. The first that
# returns a list of trades is used; the PROBE line records which one worked.
TRADE_ENDPOINTS = (
    ("base", "/exchange/v1/derivatives/futures/data/trades"),
    ("public", "/market_data/v3/trade_history"),
)


# ── Phase 1 ────────────────────────────────────────────────────────────
class VolumeTracker:
    def __init__(self):
        self.vols = {}   # symbol -> deque of recent candle volumes

    def seed(self, symbol, candles):
        if self.vols.get(symbol):
            return
        v = [c.get("volume") for c in candles if c.get("volume") is not None]
        if v:
            self.vols[symbol] = deque(v[-RVOL_LOOKBACK:], maxlen=RVOL_LOOKBACK)

    def add(self, symbol, volume):
        """Returns relative volume vs the PREVIOUS candles, then records this one."""
        d = self.vols.setdefault(symbol, deque(maxlen=RVOL_LOOKBACK))
        rvol = (volume / (sum(d) / len(d))) if len(d) >= 5 and sum(d) > 0 else None
        d.append(volume)
        return rvol


def volume_profile(candles):
    """VPOC / VAH / VAL for the given candles (each needs high, low, volume)."""
    cs = [c for c in candles if c.get("volume") and c["high"] > c["low"]]
    if len(cs) < 4:
        return None
    lo, hi = min(c["low"] for c in cs), max(c["high"] for c in cs)
    if hi <= lo:
        return None
    step = (hi - lo) / VP_BINS
    bins = [0.0] * VP_BINS
    for c in cs:
        a = int((c["low"] - lo) / step)
        b = min(VP_BINS - 1, int((c["high"] - lo) / step))
        share = c["volume"] / (b - a + 1)
        for i in range(a, b + 1):
            bins[i] += share
    total = sum(bins)
    poc = max(range(VP_BINS), key=lambda i: bins[i])
    lo_i = hi_i = poc
    acc = bins[poc]
    while acc < VALUE_AREA * total and (lo_i > 0 or hi_i < VP_BINS - 1):
        down = bins[lo_i - 1] if lo_i > 0 else -1
        up = bins[hi_i + 1] if hi_i < VP_BINS - 1 else -1
        if up >= down:
            hi_i += 1; acc += bins[hi_i]
        else:
            lo_i -= 1; acc += bins[lo_i]
    mid = lambda i: lo + (i + 0.5) * step
    return {"vpoc": mid(poc), "vah": lo + (hi_i + 1) * step, "val": lo + lo_i * step}


def fmt_vp(vp, price=None):
    if not vp:
        return "VP n/a"
    where = ""
    if price is not None:
        where = (" (price ABOVE value)" if price > vp["vah"] else
                 " (price BELOW value)" if price < vp["val"] else " (inside value)")
    return f"VPOC {vp['vpoc']:.6g} VAH {vp['vah']:.6g} VAL {vp['val']:.6g}{where}"


# ── Phase 2 ────────────────────────────────────────────────────────────
def _parse_trade(t):
    """-> (ts_ms, price, qty, aggressor 'buy'/'sell') or None.
    ASSUMPTION until verified: is_maker / m True means the BUYER was the maker,
    so the aggressor (taker) SOLD -- the common exchange convention."""
    try:
        ts = int(float(t.get("timestamp", t.get("T", t.get("t")))))
        if ts < 10 ** 12:
            ts *= 1000                       # seconds -> ms
        price = float(t.get("price", t.get("p")))
        qty = float(t.get("quantity", t.get("q", t.get("qty"))))
        maker = t.get("is_maker", t.get("m"))
        if maker is None:
            return None
        return ts, price, qty, ("sell" if bool(maker) else "buy")
    except Exception:
        return None


class TradeFlow:
    def __init__(self, coindcx):
        self.cdx = coindcx
        self.endpoint = None          # the working endpoint once found
        self.probed = False
        self.disabled_until = 0.0
        self.last_ts = {}             # symbol -> newest trade ts seen
        self.seen = {}                # symbol -> recent trade keys (dedupe)
        self.buckets = {}             # symbol -> {candle_start: {buy, sell, n, gap}}
        self.cvd = {}                 # symbol -> (utc_day, value)

    async def _fetch(self, symbol):
        from exchange.coindcx import SYMBOL_MAP
        pair = SYMBOL_MAP[symbol]
        tries = [self.endpoint] if self.endpoint else list(TRADE_ENDPOINTS)
        for host, path in tries:
            getter = self.cdx._get_base if host == "base" else self.cdx._get
            try:
                res = await getter(path, params={"pair": pair})
            except Exception as e:          # an erroring endpoint = try the next one
                logger.debug(f"{symbol} | trade feed {host}{path} failed ({e})")
                res = None
            trades = res if isinstance(res, list) else (res or {}).get("data") if isinstance(res, dict) else None
            if trades:
                if not self.probed:
                    self.probed = True
                    self.endpoint = (host, path)
                    logger.info(f"ORDERFLOW PROBE | {symbol} | {host}{path} returned {len(trades)} trades | "
                                f"fields: {sorted(trades[0].keys())} | sample: {trades[:2]}")
                return trades
        if self.endpoint is None:
            # nothing works yet: back off for an hour EVERY time, so a dead feed is
            # retried hourly rather than every loop for every symbol
            if not self.probed:
                self.probed = True
                logger.warning("ORDERFLOW PROBE | no trade-feed endpoint returned trades — "
                               "delta/CVD unavailable (retrying hourly); volume and volume "
                               "profile still logged")
            self.disabled_until = time.time() + 3600
        return None

    async def poll(self, symbol):
        """Called every loop: pull recent trades and bucket new ones by candle."""
        if time.time() < self.disabled_until:
            return
        try:
            raw = await self._fetch(symbol)
        except Exception as e:
            logger.warning(f"{symbol} | trade feed poll failed ({e})")
            return
        if not raw:
            return
        trades = [p for p in (_parse_trade(t) for t in raw) if p]
        if not trades:
            return
        trades.sort()
        seen = self.seen.setdefault(symbol, deque(maxlen=2000))
        seen_set = set(seen)
        last = self.last_ts.get(symbol)
        gap = last is not None and trades[0][0] > last   # oldest returned is newer than what we had
        bk = self.buckets.setdefault(symbol, {})
        for ts, price, qty, side in trades:
            key = (ts, price, qty, side)
            if key in seen_set:
                continue
            seen.append(key); seen_set.add(key)
            b = bk.setdefault(ts // CANDLE_MS * CANDLE_MS, {"buy": 0.0, "sell": 0.0, "n": 0, "gap": False})
            b[side] += qty
            b["n"] += 1
        if gap:
            bk.setdefault(trades[0][0] // CANDLE_MS * CANDLE_MS,
                          {"buy": 0.0, "sell": 0.0, "n": 0, "gap": False})["gap"] = True
        self.last_ts[symbol] = max(last or 0, trades[-1][0])
        for k in [k for k in bk if k < trades[-1][0] - 8 * CANDLE_MS]:
            del bk[k]

    def candle(self, symbol, candle_start_ms):
        """Delta summary for one closed candle; also advances the day's CVD."""
        b = (self.buckets.get(symbol) or {}).get(int(candle_start_ms))
        if not b or b["n"] == 0:
            return None
        delta = b["buy"] - b["sell"]
        day = time.strftime("%Y-%m-%d", time.gmtime(candle_start_ms / 1000))
        d, v = self.cvd.get(symbol, (day, 0.0))
        v = (v if d == day else 0.0) + delta
        self.cvd[symbol] = (day, v)
        tot = b["buy"] + b["sell"]
        return {"delta": delta, "buy_pct": b["buy"] / tot * 100 if tot else 0, "n": b["n"],
                "gap": b["gap"], "cvd": v}


def fmt_flow(rvol, vol, d):
    s = f"vol {vol:.6g} (rvol {rvol:.2f}x)" if rvol is not None else f"vol {vol:.6g} (rvol n/a)"
    if d:
        s += (f" | delta {d['delta']:+.6g} (buy {d['buy_pct']:.0f}%, {d['n']} trades"
              f"{', GAP' if d['gap'] else ''}) | CVD {d['cvd']:+.6g} [unverified]")
    return s
