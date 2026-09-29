"""Liquidity Pool Agent — extra liquidity pools, separate from the main flow.

Adds two pool types from the "5 liquidity patterns" framework, detected
independently of the existing yesterday's-high/low strategy:

  EQUAL   — two swing highs (or lows) within 0.1% of each other, at least
            4 candles apart, inside the last 4 days, never broken since.
            Stops cluster just beyond them.
  SESSION — the Asia range (00:00–07:00 UTC = 05:30–12:30 IST) high/low,
            swept during London/New York (07:00–21:00 UTC).

Flow per pool (same shape the backtest used, 2026-09-29):
  sweep   : wick beyond the level by >= 0.2%, candle closes back inside
  trigger : a rejection candle in the reversal direction within 24 candles
  delay   : ENTRY_DELAY_CANDLES more candles (same as main flow); if
            price is already beyond the SL at entry, the setup is skipped
  SL      : the sweep extreme (no buffer)
  target  : the opposite yesterday's high/low; reward:risk >= 1.0

Modes, set per pool with Railway variables (live / shadow / off):
  POOL_EQUAL_MODE    default live
  POOL_SESSION_MODE  default live
LIVE signals are returned to the monitor and go through the full existing
Risk -> Context -> Pattern -> INR tier -> execution chain. SHADOW signals
place nothing: the agent tracks them on real candles and logs the result.

Backtest evidence (10 weeks, 75 USDT, fees, full exit stack):
  EQUAL   35 trades, +Rs1,938, positive in BOTH halves (+34 / +1,904)
  SESSION 70 trades, +Rs499, unstable halves (-3,280 / +3,779)
Small samples — promising, not proven. Hence Session defaults to shadow.

Isolation: each pool has its OWN one-trade-per-symbol-per-day slot, so a
pool trade never consumes the main strategy's daily trade.
"""
import os
from datetime import datetime, timezone

from utils.logger import setup_logger
from core.strategy import Signal, ENTRY_DELAY_CANDLES, MIN_REWARD_RISK_RATIO

logger = setup_logger("pool_agent")

POOL_MODES = {
    "EQUAL": os.getenv("POOL_EQUAL_MODE", "live").strip().lower(),
    "SESSION": os.getenv("POOL_SESSION_MODE", "live").strip().lower(),   # default live (2026-09-30)
}
PATTERN_NAMES = {"EQUAL": "Equal Highs/Lows", "SESSION": "Session High/Low (Asia range)"}

SWEEP_MIN_PCT = 0.002      # wick must pierce the level by >= 0.2%
EQUAL_TOL_PCT = 0.001      # swings within 0.1% count as "equal"
EQUAL_MIN_GAP = 4          # candles between the two swings
EQUAL_LOOKBACK = 384       # 4 days of 15m candles
ARM_EXPIRY = 24            # candles to find a trigger after the sweep (6h)
HISTORY = 500              # candles kept per symbol
SHADOW_MAX_CANDLES = 192   # 48h shadow tracking cap
FEE = 0.059 / 100


def _rejection(c, side):
    o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
    b = abs(cl - o); up = h - max(o, cl); dn = min(o, cl) - l
    if side == "BUY":
        return cl > o and b > 0 and dn >= 2 * b and dn >= 2 * up
    return cl < o and b > 0 and up >= 2 * b and up >= 2 * dn


def _utc(c):
    return datetime.fromtimestamp(int(c["time"]) / 1000, timezone.utc)


class PoolAgent:
    def __init__(self, coindcx, margin_usd: float, leverage: float):
        self.cdx = coindcx
        self.margin, self.lev = margin_usd, leverage
        self.hist = {}       # symbol -> [candle dicts], oldest first
        self.armed = {}      # symbol -> {key: {...}}
        self.pending = {}    # symbol -> [pending entries]
        self.shadow = {}     # symbol -> [shadow positions]
        self.traded = set()  # (symbol, pool, utc_day)
        active = {k: v for k, v in POOL_MODES.items() if v in ("live", "shadow")}
        logger.info(f"Pool agent modes: {POOL_MODES} — active: {list(active)}")

    # ── history ────────────────────────────────────────────────────────
    async def _backfill(self, symbol):
        from exchange.coindcx import SYMBOL_MAP
        try:
            now = int(datetime.now(timezone.utc).timestamp() * 1000)
            res = await self.cdx._get("/market_data/candles", params={
                "pair": SYMBOL_MAP[symbol], "interval": "15m",
                "from": now - HISTORY * 900 * 1000, "to": now, "limit": HISTORY})
            cs = sorted(res or [], key=lambda c: int(c["time"]))[:-1]  # drop forming candle
            self.hist[symbol] = [{k: float(c[k]) for k in ("open", "high", "low", "close")}
                                 | {"time": int(c["time"])} for c in cs]
            logger.info(f"{symbol} | Pool agent history loaded: {len(self.hist[symbol])} candles")
        except Exception as e:
            self.hist[symbol] = []
            logger.error(f"{symbol} | Pool agent backfill failed ({e}) — building history live")

    # ── pools known BEFORE the current candle (no look-ahead) ─────────
    def _pools(self, cs):
        i = len(cs) - 1          # current candle index; pools use cs[:i]
        out = []
        if POOL_MODES["EQUAL"] in ("live", "shadow") and i > 10:
            lo = max(2, i - EQUAL_LOOKBACK)
            sh = [j for j in range(lo, i - 2) if cs[j]["high"] == max(x["high"] for x in cs[j-2:j+3])]
            sl = [j for j in range(lo, i - 2) if cs[j]["low"] == min(x["low"] for x in cs[j-2:j+3])]
            for arr, key, where, better in ((sh, "high", "high", max), (sl, "low", "low", min)):
                for a in range(len(arr)):
                    for b in range(a + 1, len(arr)):
                        x, y = cs[arr[a]][key], cs[arr[b]][key]
                        if abs(x - y) / x > EQUAL_TOL_PCT or arr[b] - arr[a] < EQUAL_MIN_GAP:
                            continue
                        lvl = better(x, y)
                        span = cs[arr[a]:i]
                        intact = all(c["high"] <= lvl for c in span) if where == "high" \
                            else all(c["low"] >= lvl for c in span)
                        if intact:
                            out.append(("EQUAL", where, lvl))
        if POOL_MODES["SESSION"] in ("live", "shadow"):
            now = _utc(cs[i])
            if 7 <= now.hour < 21:
                asia = [c for c in cs[max(0, i - 100):i]
                        if _utc(c).date() == now.date() and _utc(c).hour < 7]
                if len(asia) >= 20:
                    out.append(("SESSION", "high", max(c["high"] for c in asia)))
                    out.append(("SESSION", "low", min(c["low"] for c in asia)))
        return out

    # ── main entry: called once per closed candle ─────────────────────
    async def on_candle(self, symbol, candle, level):
        """Returns a list of LIVE Signals for the monitor to process."""
        # Reload full history on first use AND after any gap (e.g. the
        # 23:00–05:30 IST window when the agent isn't called), so a level
        # broken overnight can never wrongly look "untouched" next morning.
        last = self.hist.get(symbol)
        if not last or int(candle["time"]) - last[-1]["time"] > 900 * 1000 * 1.5:
            if last:
                logger.info(f"{symbol} | Pool agent: candle gap detected — reloading history")
            await self._backfill(symbol)
        cs = self.hist[symbol]
        if cs and int(candle["time"]) <= cs[-1]["time"]:
            return []
        cs.append({k: float(candle[k]) for k in ("open", "high", "low", "close")}
                  | {"time": int(candle["time"])})
        del cs[:-HISTORY]
        day = _utc(cs[-1]).strftime("%Y-%m-%d")
        self._manage_shadow(symbol, cs[-1])
        out = self._advance_pending(symbol, cs[-1], level, day)

        c = cs[-1]
        armed = self.armed.setdefault(symbol, {})
        for pool, where, lvl in self._pools(cs):
            key = (pool, where, round(lvl, 8))
            if key in armed:
                continue
            if where == "low" and c["low"] < lvl * (1 - SWEEP_MIN_PCT) and c["close"] > lvl:
                armed[key] = {"ext": c["low"], "age": 0, "lvl": lvl}
                logger.info(f"{symbol} | POOL {pool} low {lvl:.6g} swept (L:{c['low']:.6g}) "
                            f"[{POOL_MODES[pool]}] — watching for bullish trigger")
            elif where == "high" and c["high"] > lvl * (1 + SWEEP_MIN_PCT) and c["close"] < lvl:
                armed[key] = {"ext": c["high"], "age": 0, "lvl": lvl}
                logger.info(f"{symbol} | POOL {pool} high {lvl:.6g} swept (H:{c['high']:.6g}) "
                            f"[{POOL_MODES[pool]}] — watching for bearish trigger")

        for key, a in list(armed.items()):
            pool, where, _ = key
            side = "BUY" if where == "low" else "SELL"
            a["age"] += 1
            if a["age"] > ARM_EXPIRY:
                del armed[key]; continue
            if a["age"] > 1 and _rejection(c, side):
                del armed[key]
                if (symbol, pool, day) in self.traded or any(p["pool"] == pool for p in self.pending.get(symbol, [])):
                    continue
                self.pending.setdefault(symbol, []).append(
                    {"pool": pool, "side": side, "ext": a["ext"], "lvl": a["lvl"], "left": ENTRY_DELAY_CANDLES})
                logger.info(f"{symbol} | POOL {pool} {side} trigger confirmed — entry delayed "
                            f"{ENTRY_DELAY_CANDLES} candles | SL {a['ext']:.6g}")
        return out

    def _advance_pending(self, symbol, c, level, day):
        out, keep = [], []
        for p in self.pending.get(symbol, []):
            side = p["side"]
            p["left"] -= 1
            if p["left"] > 0:
                keep.append(p); continue
            e, sl = c["close"], p["ext"]
            pdh, pdl = (level.pdh, level.pdl) if level else (None, None)
            tgt = pdh if side == "BUY" else pdl
            reason = None
            if (side == "BUY" and e <= sl) or (side == "SELL" and e >= sl):
                reason = "entry beyond SL after delay"
            elif not tgt or (side == "BUY" and tgt <= e) or (side == "SELL" and tgt >= e):
                reason = "target not ahead of entry"
            elif abs(tgt - e) / abs(e - sl) < MIN_REWARD_RISK_RATIO:
                reason = f"reward:risk {abs(tgt - e) / abs(e - sl):.2f}x < {MIN_REWARD_RISK_RATIO:.1f}x"
            pool, mode = p["pool"], POOL_MODES[p["pool"]]
            if reason:
                logger.info(f"{symbol} | POOL {pool} {side} rejected at entry — {reason} "
                            f"| entry {e:.6g} SL {sl:.6g} — no trade")
                continue
            self.traded.add((symbol, pool, day))
            if mode == "shadow":
                from core.monitor import SL_RISK_FULL_SIZE_MAX_INR, SL_RISK_STAGED_MAX_INR, USD_TO_INR_RATE
                rinr = abs(e - sl) / e * self.margin * self.lev * USD_TO_INR_RATE
                if rinr > SL_RISK_STAGED_MAX_INR:
                    logger.info(f"{symbol} | POOL {pool} SHADOW skipped — INR risk ₹{rinr:.0f} > "
                                f"₹{SL_RISK_STAGED_MAX_INR}")
                    self.traded.discard((symbol, pool, day))
                    continue
                staged = rinr >= SL_RISK_FULL_SIZE_MAX_INR
                self.shadow.setdefault(symbol, []).append(
                    {"pool": pool, "side": side, "e": e, "sl": sl, "orig_sl": sl, "tgt": tgt,
                     "n": 0, "hw": 0.0, "tp": set(), "real": 0.0, "rem": 1.0, "staged": staged,
                     "qty": self.margin * self.lev * (0.5 if staged else 1) / e})
                logger.info(f"{symbol} | POOL {pool} SHADOW entry {side} {e:.6g} SL {sl:.6g} "
                            f"TP {tgt:.6g} — tracking only, no order")
                continue
            bias = getattr(level, "trend_bias", None)
            sig = Signal(symbol, side, e, sl, pdh, pdl,
                         counter_trend=(bias == "UPTREND" and side == "SELL") or
                                       (bias == "DOWNTREND" and side == "BUY"),
                         swept_level=p["lvl"])
            sig.pattern = PATTERN_NAMES[pool]
            sig.source = "pool"
            logger.info(f"{symbol} | POOL {pool} LIVE signal {side} entry {e:.6g} SL {sl:.6g} "
                        f"-> handing to Risk/Context/Pattern chain")
            out.append(sig)
        self.pending[symbol] = keep
        return out

    def _manage_shadow(self, symbol, c):
        """Same exit stack as the live Trade Manager: early failure (first 3
        candles), breakeven 0.4R/1.0R, trend trail 0.3R, TP ladder 1.5R/2.5R,
        target, 48h cap. Sized with the same INR tiers (skip/staged/full)."""
        keep = []
        for s in self.shadow.get(symbol, []):
            side, e, R = s["side"], s["e"], abs(s["e"] - s["orig_sl"])
            s["n"] += 1
            o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
            s["hw"] = max(s["hw"], ((h - e) if side == "BUY" else (e - l)) / R)
            exit_px = why = None
            if s["n"] <= 3:
                dj = s.get("doji") and ((cl < s["pc"]) if side == "BUY" else (cl > s["pc"]))
                if _rejection(c, "SELL" if side == "BUY" else "BUY") or dj:
                    exit_px, why = cl, "early_failure"
                s["doji"] = (h - l) > 0 and abs(cl - o) <= 0.15 * (h - l); s["pc"] = cl
            if exit_px is None:
                if s["hw"] >= 1.0:
                    s["sl"] = max(s["sl"], e) if side == "BUY" else min(s["sl"], e)
                    tr = e + (s["hw"] - 0.3) * R if side == "BUY" else e - (s["hw"] - 0.3) * R
                    s["sl"] = max(s["sl"], tr) if side == "BUY" else min(s["sl"], tr)
                elif s["hw"] >= 0.4:
                    half = e - 0.5 * R if side == "BUY" else e + 0.5 * R
                    s["sl"] = max(s["sl"], half) if side == "BUY" else min(s["sl"], half)
                if (l <= s["sl"]) if side == "BUY" else (h >= s["sl"]):
                    exit_px, why = s["sl"], "stop"
                else:
                    for lvl, w in ((1.5, .34), (2.5, .33)):
                        if lvl not in s["tp"] and s["hw"] >= lvl:
                            s["tp"].add(lvl)
                            px = e + lvl * R if side == "BUY" else e - lvl * R
                            s["real"] += self._pnl(s, px, w); s["rem"] -= w
                    if (h >= s["tgt"]) if side == "BUY" else (l <= s["tgt"]):
                        exit_px, why = s["tgt"], "target"
                    elif s["n"] >= SHADOW_MAX_CANDLES:
                        exit_px, why = cl, "timeout"
            if exit_px is None:
                keep.append(s); continue
            net = s["real"] + self._pnl(s, exit_px, s["rem"])
            logger.info(f"{symbol} | POOL {s['pool']} SHADOW RESULT {side} {why}: "
                        f"net ${net:+.2f} ({'staged 50%' if s['staged'] else 'full size'}) "
                        f"— no real order was placed")
        self.shadow[symbol] = keep

    def _pnl(self, s, px, frac):
        q = s["qty"] * frac
        g = (px - s["e"]) * q if s["side"] == "BUY" else (s["e"] - px) * q
        return g - (s["e"] + px) * q * FEE
