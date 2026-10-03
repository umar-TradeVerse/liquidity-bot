"""Breakout Sub-Agent — PAPER TRADING ONLY (2026-10-03). Never places an order.

Why it exists
  The liquidity strategy's most recurring loss pattern was the ACCEPTED SWEEP:
  price crossed yesterday's high/low and kept closing beyond it (e.g. ZAMAUSD
  03-Oct). The breakout guard now skips those. Tested on 10 weeks of candles,
  trading that pattern as a breakout lost in all 36 variants -- but volume was
  never available in that history, and ZAMA's real breakout came on 3.95x
  volume. This agent paper-trades breakouts live, recording volume/order-flow
  at entry, so we can test whether HIGH-VOLUME breakouts have an edge before
  anything goes live.

Rules (fixed, so results stay comparable)
  BREAK   first close beyond yesterday's high (LONG) / low (SHORT) today
  HOLD    the next candle also closes beyond it
  ENTRY   close of the hold candle
  STOP    structure: lowest low (LONG) / highest high (SHORT) of the two candles
  TARGET  3R; the best R reached (MFE) before exit is also recorded, so 1.5R
          or 2R targets can be evaluated later from the same records
  TIMEOUT 96 candles (24h) -> closed at market
  SIZING  same INR tiers as the bot (skip > Rs1,300, half size >= Rs800), fees
          included, at the current TRADE_SIZE_USD
  One breakout per symbol per side per day; one open paper trade per symbol.

Isolation
  Reads only: the shared candle history, today's levels, and the order-flow
  readings. It holds no exchange reference and cannot place, modify or close
  any order. It never changes another strategy's state. Results go to its own
  log lines and PERSIST_DIR/breakout_paper.jsonl.

Railway: BREAKOUT_MODE = paper (default) / off.
"""
import json
import os
from datetime import datetime, timezone

from utils.logger import setup_logger

logger = setup_logger("breakout_agent")

BREAKOUT_MODE = os.getenv("BREAKOUT_MODE", "paper").strip().lower()
TARGET_R = 3.0
TIMEOUT_CANDLES = 96
FEE = 0.059 / 100
_DIR = os.getenv("PERSIST_DIR", "/data")
_RESULTS = os.path.join(_DIR, "breakout_paper.jsonl")
_STATE = os.path.join(_DIR, "breakout_state.json")


def _day(ms):
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime("%Y-%m-%d")


def _ist(ms):
    from datetime import timedelta
    return (datetime.fromtimestamp(int(ms) / 1000, timezone.utc)
            + timedelta(hours=5, minutes=30)).strftime("%d-%b %I:%M %p IST")


class BreakoutAgent:
    def __init__(self, store, margin_usd: float, leverage: float):
        self.store = store                 # shared candle history (read-only use)
        self.margin, self.lev = margin_usd, leverage
        self.open = {}                     # symbol -> paper position
        self.done = set()                  # (symbol, day, side) already taken
        self.flow_at = {}                  # symbol -> {candle_time: flow summary}
        self._load()
        logger.info(f"Breakout agent mode: {BREAKOUT_MODE} (paper trading only — never places orders) | "
                    f"{len(self.open)} open paper trade(s) restored")

    # ── persistence ────────────────────────────────────────────────────
    def _load(self):
        try:
            with open(_STATE) as f:
                d = json.load(f)
            self.open = d.get("open", {})
            self.done = {tuple(x) for x in d.get("done", [])}
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.error(f"Breakout state load failed: {e}")

    def _save(self):
        try:
            os.makedirs(_DIR, exist_ok=True)
            keep = [list(x) for x in sorted(self.done, key=lambda x: x[1])][-500:]   # newest 500
            with open(_STATE, "w") as f:
                json.dump({"open": self.open, "done": keep}, f)
        except Exception as e:
            logger.error(f"Breakout state save failed: {e}")

    def _record(self, row):
        try:
            os.makedirs(_DIR, exist_ok=True)
            with open(_RESULTS, "a") as f:
                f.write(json.dumps(row) + "\n")
        except Exception as e:
            logger.error(f"Breakout result write failed: {e}")

    # ── sizing (same tiers as the live bot) ────────────────────────────
    def _size(self, e, sl):
        from core.agents.risk_agent import USD_TO_INR_RATE
        risk_inr = abs(e - sl) / e * self.margin * self.lev * USD_TO_INR_RATE
        if risk_inr > 1300:
            return None, risk_inr
        return (0.5 if risk_inr >= 800 else 1.0), risk_inr

    # ── main entry: once per closed candle ────────────────────────────
    async def on_candle(self, symbol, candle, level, flow=None):
        if BREAKOUT_MODE != "paper":
            return
        # keep the shared history current itself (safe if another agent already did)
        cs = await self.store.update(symbol, candle) or []
        if flow is not None:   # record EVERY candle's flow, even before 3 candles exist
            fa = self.flow_at.setdefault(symbol, {})
            fa[int(candle["time"])] = flow
            for k in [k for k in fa if k < int(candle["time"]) - 8 * 900_000]:
                del fa[k]
        if len(cs) < 3:
            return
        self._manage(symbol, cs)
        if level is None or symbol in self.open:
            return
        a, b, c = cs[-3], cs[-2], cs[-1]            # c = hold candle, b = break candle
        if not (_day(a["time"]) == _day(b["time"]) == _day(c["time"])):
            return                                  # whole pattern must be inside today
        day = _day(c["time"])
        for side, lvl in (("BUY", level.pdh), ("SELL", level.pdl)):
            if (symbol, day, side) in self.done or not lvl:
                continue
            beyond = (lambda k: k["close"] > lvl) if side == "BUY" else (lambda k: k["close"] < lvl)
            if beyond(a) or not (beyond(b) and beyond(c)):
                continue                            # needs a FRESH break plus one hold
            self.done.add((symbol, day, side))
            e = c["close"]
            sl = min(b["low"], c["low"]) if side == "BUY" else max(b["high"], c["high"])
            R = abs(e - sl)
            if R <= 0:
                continue
            size, risk_inr = self._size(e, sl)
            bf = (self.flow_at.get(symbol) or {}).get(b["time"]) or {}
            hf = (self.flow_at.get(symbol) or {}).get(c["time"]) or {}
            if size is None:
                logger.info(f"{symbol} | BREAKOUT PAPER skipped — {side} through {lvl:.6g}, "
                            f"risk ₹{risk_inr:.0f} > ₹1,300")
                self._save()
                return
            from core.candle_patterns import classify
            pos = {"side": side, "entry": e, "sl": sl, "tp": e + TARGET_R * R if side == "BUY" else e - TARGET_R * R,
                   "risk": R, "level": lvl, "size": size, "opened": c["time"], "last_t": c["time"],
                   "mfe_r": 0.0, "n": 0,
                   "break_rvol": bf.get("rvol"), "break_vol": bf.get("vol"),
                   "break_delta": (bf.get("delta") or {}).get("delta"),
                   "hold_rvol": hf.get("rvol"), "hold_delta": (hf.get("delta") or {}).get("delta"),
                   "vp": hf.get("vp"),
                   "break_pattern": classify(b, a), "hold_pattern": classify(c, b)}
            self.open[symbol] = pos
            rv = pos["break_rvol"]
            logger.info(f"{symbol} | BREAKOUT PAPER ENTRY {side} @ {e:.6g} | broke {lvl:.6g} "
                        f"({'yesterday high' if side == 'BUY' else 'yesterday low'}) at {_ist(b['time'])}, held "
                        f"| SL {sl:.6g} TP {pos['tp']:.6g} (3R) | {'half' if size < 1 else 'full'} size "
                        f"| break candle rvol {f'{rv:.2f}x' if rv is not None else 'n/a'}, "
                        f"pattern {pos['break_pattern']} — PAPER ONLY, no order placed")
            self._save()
            return

    def _manage(self, symbol, cs):
        pos = self.open.get(symbol)
        if not pos:
            return
        side, e, R = pos["side"], pos["entry"], pos["risk"]
        for k in [k for k in cs if k["time"] > pos["last_t"]]:   # includes candles missed overnight
            pos["last_t"] = k["time"]; pos["n"] += 1
            fav = (k["high"] - e) if side == "BUY" else (e - k["low"])
            px = why = None
            if (k["low"] <= pos["sl"]) if side == "BUY" else (k["high"] >= pos["sl"]):
                px, why = pos["sl"], "stop"                       # stop checked first (conservative)
            else:
                pos["mfe_r"] = max(pos["mfe_r"], fav / R)
                if (k["high"] >= pos["tp"]) if side == "BUY" else (k["low"] <= pos["tp"]):
                    px, why = pos["tp"], "target"
                elif pos["n"] >= TIMEOUT_CANDLES:
                    px, why = k["close"], "timeout"
            if px is None:
                continue
            qty = self.margin * self.lev * pos["size"] / e
            gross = (px - e) * qty if side == "BUY" else (e - px) * qty
            net = gross - (e + px) * qty * FEE
            r = ((px - e) if side == "BUY" else (e - px)) / R
            from core.agents.risk_agent import USD_TO_INR_RATE
            logger.info(f"{symbol} | BREAKOUT PAPER RESULT {side} {why}: {r:+.2f}R, "
                        f"${net:+.2f} (₹{net * USD_TO_INR_RATE:+.0f}) | best reached {pos['mfe_r']:.2f}R "
                        f"| break rvol {pos['break_rvol']} — PAPER ONLY")
            self._record({**pos, "symbol": symbol, "exit": px, "why": why, "r": r, "net_usd": net,
                          "closed": k["time"]})
            del self.open[symbol]
            self._save()
            return
        self._save()
