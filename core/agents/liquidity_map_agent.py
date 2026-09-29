"""Liquidity Map Agent — trades sweeps of PROVEN liquidity levels only.

Idea (from live observation, XRPUSD 30-Sep): a level price has already
respected is where stops pile up. When that proven level finally gets
swept — even shallowly — and price closes back, it tends to travel to the
next pool of liquidity on the other side.

No fixed sweep-depth percentage: the level's own history decides.

  LEVEL     a MAJOR swing high/low — the extreme of the surrounding 2 hours
            on both sides (8 x 15m candles each side). Kept for 5 days.
  ZONE      the pivot candle's wick (for a low: low -> body bottom).
  RESPECTED price re-enters the zone without breaking the level and closes
            back on the level's side. Each separate visit counts once.
  SWEPT     wick through the level, close back on its side. If the level
            was respected >= MIN_RESPECTED times -> trade signal.
  BROKEN    candle CLOSES beyond the level -> level retired (accepted).
  ENTRY     close of the sweep candle.   SL: the sweep extreme.
  TARGET    the nearest still-active opposite-side level (the next pool).
            Trade only if that pool is at least 1R away.
  EXITS     SIMPLE, as tested: SL or target only (no breakeven, trail, TP
            ladder or early-failure — those were not part of the test).

Backtest (10 weeks, 8 symbols, 75 USDT, fees, SL/target exits only):
  respected 2+ times : 165 trades, +Rs16,359 (both halves positive)
  never respected    : 287 trades, -Rs13,982
Hit rate is low (~19%): many small losses, fewer large wins.

Level memory is saved to PERSIST_DIR/liquidity_map.json so a restart keeps
every level's history. Mode: LIQMAP_MODE = live / off (default live).
"""
import json
import os
from datetime import datetime, timezone

from utils.logger import setup_logger
from core.strategy import Signal

logger = setup_logger("liquidity_map")

LIQMAP_MODE = os.getenv("LIQMAP_MODE", "live").strip().lower()
SWING = 8            # 2 hours each side on 15m = a MAJOR swing
MAX_AGE = 480        # 5 days of 15m candles
MIN_RESPECTED = 2
HISTORY = 500
_PATH = os.path.join(os.getenv("PERSIST_DIR", "/data"), "liquidity_map.json")


def _ist(ms):
    from datetime import timedelta
    return (datetime.fromtimestamp(ms / 1000, timezone.utc) + timedelta(hours=5, minutes=30)).strftime("%d-%b %I:%M %p IST")


class LiquidityMapAgent:
    def __init__(self, coindcx):
        self.cdx = coindcx
        self.hist = {}      # symbol -> candles
        self.levels = {}    # symbol -> [level dicts]
        self._level = {}    # symbol -> today's DailyLevel (for alert text)
        self._load()
        logger.info(f"Liquidity map mode: {LIQMAP_MODE} — {sum(len(v) for v in self.levels.values())} "
                    f"levels restored")

    # ── persistence ────────────────────────────────────────────────────
    def _load(self):
        try:
            with open(_PATH) as f:
                self.levels = json.load(f)
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.error(f"Liquidity map load failed: {e}")

    def _save(self):
        try:
            os.makedirs(os.path.dirname(_PATH), exist_ok=True)
            with open(_PATH, "w") as f:
                json.dump(self.levels, f)
        except Exception as e:
            logger.error(f"Liquidity map save failed: {e}")

    # ── history ────────────────────────────────────────────────────────
    async def _backfill(self, symbol):
        from exchange.coindcx import SYMBOL_MAP
        try:
            now = int(datetime.now(timezone.utc).timestamp() * 1000)
            res = await self.cdx._get("/market_data/candles", params={
                "pair": SYMBOL_MAP[symbol], "interval": "15m",
                "from": now - HISTORY * 900 * 1000, "to": now, "limit": HISTORY})
            cs = sorted(res or [], key=lambda c: int(c["time"]))[:-1]
            self.hist[symbol] = [{k: float(c[k]) for k in ("open", "high", "low", "close")}
                                 | {"time": int(c["time"])} for c in cs]
            self._rebuild(symbol)
            logger.info(f"{symbol} | Liquidity map: {len(self.hist[symbol])} candles, "
                        f"{len(self.levels.get(symbol, []))} active levels")
        except Exception as e:
            self.hist[symbol] = []
            logger.error(f"{symbol} | Liquidity map backfill failed ({e})")

    def _rebuild(self, symbol):
        """Replay stored history to rebuild levels exactly (no trades)."""
        cs = self.hist[symbol]
        self.levels[symbol] = []
        for i in range(len(cs)):
            self._step(symbol, cs[:i + 1], trade=False)
        self._save()

    # ── core: one candle ──────────────────────────────────────────────
    def _step(self, symbol, cs, trade=True):
        L = self.levels.setdefault(symbol, [])
        i = len(cs) - 1
        c = cs[i]
        j = i - SWING
        if j >= SWING:
            win = cs[j - SWING:j + SWING + 1]
            p = cs[j]
            if p["low"] == min(x["low"] for x in win):
                L.append({"k": "low", "p": p["low"], "zone": min(p["open"], p["close"]),
                          "t": p["time"], "touch": [], "inz": False})
            if p["high"] == max(x["high"] for x in win):
                L.append({"k": "high", "p": p["high"], "zone": max(p["open"], p["close"]),
                          "t": p["time"], "touch": [], "inz": False})
        L[:] = [v for v in L if c["time"] - v["t"] <= MAX_AGE * 900 * 1000]
        signal = None
        keep = []
        for v in L:
            if c["time"] - v["t"] <= SWING * 900 * 1000:
                keep.append(v); continue          # not confirmed yet
            state = "active"
            if v["k"] == "low":
                inz = v["p"] <= c["low"] <= v["zone"]
                if inz and c["close"] > v["p"] and not v["inz"]:
                    v["touch"].append(c["time"])
                v["inz"] = inz
                if c["low"] < v["p"] and c["close"] > v["p"]:
                    state = "swept"
                    if trade and signal is None and len(v["touch"]) >= MIN_RESPECTED:
                        signal = self._build(symbol, v, c, "BUY", L)
                elif c["close"] < v["p"]:
                    state = "broken"
            else:
                inz = v["zone"] <= c["high"] <= v["p"]
                if inz and c["close"] < v["p"] and not v["inz"]:
                    v["touch"].append(c["time"])
                v["inz"] = inz
                if c["high"] > v["p"] and c["close"] < v["p"]:
                    state = "swept"
                    if trade and signal is None and len(v["touch"]) >= MIN_RESPECTED:
                        signal = self._build(symbol, v, c, "SELL", L)
                elif c["close"] > v["p"]:
                    state = "broken"
            if state == "active":
                keep.append(v)
        L[:] = keep
        return signal

    def _build(self, symbol, v, c, side, L):
        e = c["close"]
        sl = c["low"] if side == "BUY" else c["high"]
        pools = sorted((x["p"] for x in L if x["k"] == ("high" if side == "BUY" else "low")
                        and x is not v and ((x["p"] > e) if side == "BUY" else (x["p"] < e))),
                       reverse=(side == "SELL"))
        story = (f"level {v['p']:.6g} formed {_ist(v['t'])}, respected {len(v['touch'])}x "
                 f"({', '.join(_ist(t) for t in v['touch'][-3:])}), swept {_ist(c['time'])} "
                 f"to {sl:.6g}, closed back {e:.6g}")
        if not pools:
            logger.info(f"{symbol} | LIQMAP {side} skipped — {story} — no pool ahead to target")
            return None
        tp, R = pools[0], abs(e - sl)
        if R <= 0 or abs(tp - e) < R:
            logger.info(f"{symbol} | LIQMAP {side} skipped — {story} — next pool {tp:.6g} "
                        f"is less than 1R away")
            return None
        lv = self._level.get(symbol)
        sig = Signal(symbol, side, e, sl, getattr(lv, "pdh", None) or tp,
                     getattr(lv, "pdl", None) or tp, swept_level=v["p"])
        sig.target = tp              # next pool; monitor uses this as the TP
        sig.pattern = f"Liquidity Map — proven level (respected {len(v['touch'])}x)"
        sig.source = "pool"          # own daily slot, like the pool agent
        sig.simple_exit = True       # SL / next-pool target only, as tested
        sig.liqmap_story = story
        sig.liqmap_pools = pools[:3]
        logger.info(f"{symbol} | LIQMAP {side} SIGNAL — {story} | target pool {tp:.6g} "
                    f"(stack: {', '.join(f'{p:.6g}' for p in pools[:3])})")
        return sig

    async def on_candle(self, symbol, candle, level=None):
        self._level[symbol] = level
        if LIQMAP_MODE != "live":
            return []
        last = self.hist.get(symbol)
        if not last or int(candle["time"]) - last[-1]["time"] > 900 * 1000 * 1.5:
            await self._backfill(symbol)
        cs = self.hist[symbol]
        if cs and int(candle["time"]) <= cs[-1]["time"]:
            return []
        cs.append({k: float(candle[k]) for k in ("open", "high", "low", "close")}
                  | {"time": int(candle["time"])})
        del cs[:-HISTORY]
        sig = self._step(symbol, cs)
        self._save()
        return [sig] if sig else []
