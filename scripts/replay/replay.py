"""Candle-based replay of the deployed liquidity bot (SIMPLE_MODE).

Drives the REAL StrategyEngine, LiquidityMapAgent._step, RiskAgent and
context_agent from REPO, fed with CoinDCX 15m candles (fetch_candles.py).
Exits replicate trade_manager._simple_manage and the Liquidity Map
simple_exit branch. Daily levels = previous UTC day's high/low (05:30 IST
reset); entries only 05:30-23:00 IST; 50 USDT x10; INR tiers at 99.51;
fees 0.059% per side. Fills exactly at stop/target -- no slippage.

Usage: python scripts/replay/replay.py REPO CANDLE_DIR OUT_CSV
         [--from YYYY-MM-DD] [--to YYYY-MM-DD]
         [--exec-candles DIR]   fill entries + run exits on these candles (e.g. futures)
         [--fix-overnight]      block a new main entry while the coin is still in a trade
         [--only main|liqmap]   one strategy only
         [--set module:NAME=value]  override a constant, e.g. core.simple_mode:TP_R=2.0
         [--trace YYYY-MM-DD]   print the bot's own log lines for that UTC day
"""
import argparse
import csv
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ap = argparse.ArgumentParser()
ap.add_argument("repo"); ap.add_argument("candles"); ap.add_argument("out")
ap.add_argument("--from", dest="dfrom"); ap.add_argument("--to", dest="dto")
ap.add_argument("--fix-overnight", action="store_true",
                help="block a new main entry while the same symbol still has an open trade")
ap.add_argument("--only", choices=["main", "liqmap"])
ap.add_argument("--trace", help="print strategy log lines for this UTC date")
ap.add_argument("--exec-candles", help="fill entries and run exits on THESE candles (e.g. futures) while signals use the main candle dir")
ap.add_argument("--set", action="append", default=[], help="override module constant: module:NAME=value")
args = ap.parse_args()

os.environ["PERSIST_DIR"] = tempfile.mkdtemp(prefix="replay_")
os.environ["LOG_DIR"] = tempfile.mkdtemp(prefix="replay_logs_")
os.environ.setdefault("TRADE_SIZE_USD", "50")
os.environ.setdefault("SIMPLE_MODE", "on")
sys.path.insert(0, args.repo)

import json  # noqa: E402
from core import simple_mode  # noqa: E402
from core.state import BotState  # noqa: E402
from core import strategy as strat  # noqa: E402
from core.strategy import StrategyEngine, _classify_day  # noqa: E402
from core.agents import risk_agent as ra, context_agent  # noqa: E402
from core.agents.risk_agent import RiskAgent  # noqa: E402
from core.agents.liquidity_map_agent import LiquidityMapAgent  # noqa: E402
from core.trading_common import (MAX_CONCURRENT_POSITIONS, TRADE_LEVERAGE,  # noqa: E402
                                 SL_RISK_FULL_SIZE_MAX_INR, SL_RISK_STAGED_MAX_INR,
                                 STABILITY_MAX_COUNTER_CONFIRMS, SYMBOLS)

for s in args.set:
    mod, kv = s.split(":", 1); k, v = kv.split("=", 1)
    m = sys.modules[mod]; setattr(m, k, type(getattr(m, k))(v))

assert simple_mode.ON, "replay models SIMPLE_MODE only"
INR = ra.USD_TO_INR_RATE
MARGIN = float(os.environ["TRADE_SIZE_USD"])
D15 = 900 * 1000
IST = timedelta(hours=5, minutes=30)

# quiet the bot's loggers; optionally trace one day
logging.disable(logging.NOTSET)
trace_lines = []


class _Trace(logging.Handler):
    def emit(self, r):
        if SIM_DAY[0] == args.trace:
            trace_lines.append(f"{SIM_NOW[0]} | {r.name} | {r.getMessage()[:230]}")


for name in list(logging.root.manager.loggerDict):
    lg = logging.getLogger(name)
    lg.handlers = [_Trace()] if args.trace else []
    lg.propagate = False
SIM_DAY, SIM_NOW = [None], [None]
ra.trading_day = lambda: SIM_DAY[0]

# ── data ──────────────────────────────────────────────────────────────
C = {}
for sym in SYMBOLS + ["BTCUSD"]:
    with open(os.path.join(args.candles, f"{sym}.json")) as f:
        C[sym] = {c["time"]: c for c in json.load(f)}
times = sorted(set().union(*[set(v) for v in C.values()]))
EX = C
if args.exec_candles:
    EX = {}
    for sym in SYMBOLS:
        with open(os.path.join(args.exec_candles, f"{sym}.json")) as f:
            EX[sym] = {c["time"]: c for c in json.load(f)}


def utc_day(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


daily = {}   # sym -> {day: {open, high, low, close}}
for sym, cs in C.items():
    d = {}
    for t in sorted(cs):
        c, k = cs[t], utc_day(t)
        if k not in d:
            d[k] = dict(open=c["open"], high=c["high"], low=c["low"], close=c["close"], n=0)
        x = d[k]; x["high"] = max(x["high"], c["high"]); x["low"] = min(x["low"], c["low"])
        x["close"] = c["close"]; x["n"] += 1
    daily[sym] = d

# ── bot objects ───────────────────────────────────────────────────────
state = BotState()
engine = StrategyEngine(None, state)
risk = RiskAgent()
risk.realised_usd, risk.seen, risk.day_dir = 0.0, [], {}
liq = LiquidityMapAgent(None, store=object())
liq.levels = {}
hist = {s: [] for s in SYMBOLS}
open_tr = {}          # symbol -> trade dict
trades = []
overnight_collisions = []


def in_entry_hours(close_ms):
    t = (datetime.fromtimestamp(close_ms / 1000, timezone.utc) + IST).time()
    return (t.hour, t.minute) >= (5, 30) and (t.hour, t.minute) <= (23, 0)


def daily_reset(day):
    SIM_DAY[0] = day
    risk._roll()
    state.reset_daily()
    prev = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    ok = True
    for sym in SYMBOLS:
        pd = daily[sym].get(prev)
        if not pd or pd["n"] < 90:          # coin not listed yet / data gap: it sits out today
            continue
        state.set_levels(sym, pdh=pd["high"], pdl=pd["low"])
        lv = state.get_level(sym)
        recent = [daily[sym][k] for k in sorted(daily[sym]) if k < day][-strat.TREND_LOOKBACK_DAYS:]
        cl = [_classify_day(c) for c in recent]
        d1 = cl[-1] if cl else "SIDEWAYS"; d2 = cl[-2] if len(cl) >= 2 else None
        lv.trend_bias = ("NONE" if d1 in ("UP", "DOWN") and d2 == d1 else
                         "UPTREND" if d1 == "UP" else "DOWNTREND" if d1 == "DOWN" else "NONE")
    b = daily["BTCUSD"].get(prev)
    if b:
        state.set_regime_levels(pdh=b["high"], pdl=b["low"])
    return ok


def ist(ms):
    return (datetime.fromtimestamp(ms / 1000, timezone.utc) + IST).strftime("%Y-%m-%d %H:%M")


def close_trade(sym, tr, px, reason, t):
    net = risk.record_close(sym, tr["side"], tr["fill"], px, tr["qty"],
                            key=f"{sym}-{tr['t_open']}")
    R = abs(tr["entry"] - tr["sl"])
    r_mult = ((px - tr["entry"]) if tr["side"] == "BUY" else (tr["entry"] - px)) / R
    trades.append(dict(strategy=tr["strategy"], symbol=sym, side=tr["side"], opened_ist=ist(tr["t_open"] + D15),
                       closed_ist=ist(t + D15), entry=tr["entry"], sl=tr["sl"], tp=tr["tp"], exit=px,
                       reason=reason, size=tr["size"], r=round(r_mult, 3), net_usd=round(net, 4),
                       net_inr=round(net * INR, 2), day=tr["day"]))
    del open_tr[sym]
    lv = state.get_level(sym)
    if lv:
        lv.in_trade = False


def manage(sym, tr, c, t):
    side, e = tr["side"], tr["entry"]
    if tr["strategy"] == "liqmap":
        sl_hit = c["low"] <= tr["live_sl"] if side == "BUY" else c["high"] >= tr["live_sl"]
        tp_hit = c["high"] >= tr["tp"] if side == "BUY" else c["low"] <= tr["tp"]
        lvl = tr["inv"]
        thru = c["close"] < lvl if side == "BUY" else c["close"] > lvl
        if sl_hit:
            close_trade(sym, tr, tr["live_sl"], "sl_hit", t)
        elif thru:
            close_trade(sym, tr, c["close"], "level_invalidated", t)
        elif tp_hit:
            close_trade(sym, tr, tr["tp"], "target", t)
        return
    R = abs(e - tr["sl"]) or 1e-12
    tr["n"] += 1
    ls = tr["live_sl"]
    if (c["low"] <= ls) if side == "BUY" else (c["high"] >= ls):
        close_trade(sym, tr, ls, "be_stop" if abs(ls - e) < 1e-12 else "sl_hit", t); return
    tp = tr["tp"]
    if (c["high"] >= tp) if side == "BUY" else (c["low"] <= tp):
        close_trade(sym, tr, tp, "target", t); return
    fav = (c["high"] - e) if side == "BUY" else (e - c["low"])
    if not tr["be"] and fav >= simple_mode.BE_AT_R * R:
        tr["be"] = True; tr["live_sl"] = e
    if tr["n"] >= simple_mode.TIMEOUT_CANDLES:
        close_trade(sym, tr, c["close"], "timeout", t)


def handle(sig, t, strategy):
    sym = sig.symbol
    level = state.get_level(sym)
    if not risk.pre_check(sig, level).approved:
        return "risk"
    if not getattr(sig, "skip_context", False):
        if not context_agent.evaluate(sig, level, state.get_regime(), STABILITY_MAX_COUNTER_CONFIRMS).approved:
            return "context"
    if strategy == "main" and sym in open_tr:
        overnight_collisions.append((ist(t + D15), sym, sig.side, open_tr[sym]["side"]))
        if args.fix_overnight:
            return "already-open"
    if not RiskAgent.concurrency(len(open_tr), MAX_CONCURRENT_POSITIONS).approved:
        return "concurrency"
    if getattr(sig, "source", "") != "pool":
        state.mark_auto_traded(sym)
    tier, _, _ = RiskAgent.inr_tier(sig.entry_price, sig.sl_price, MARGIN, TRADE_LEVERAGE,
                                    SL_RISK_FULL_SIZE_MAX_INR, SL_RISK_STAGED_MAX_INR, INR)
    if tier == "SKIP":
        return "inr-skip"
    margin = MARGIN * 0.5 if tier == "STAGED" else MARGIN
    qty = margin * TRADE_LEVERAGE / sig.entry_price
    if strategy == "main":
        r = abs(sig.entry_price - sig.sl_price)
        tp = sig.entry_price + simple_mode.TP_R * r if sig.side == "BUY" else sig.entry_price - simple_mode.TP_R * r
    else:
        tp = sig.target
    if sym in open_tr:      # live bug path: exchange position is ADDED to/netted; we just replace tracking
        old = open_tr[sym]
        close_trade(sym, old, sig.entry_price, "overwritten_by_new_entry", t)
    state.mark_in_trade(sym)
    risk.record_direction(sym, sig.side)
    fx = EX.get(sym, {}).get(t)
    fill = fx["close"] if fx else sig.entry_price
    open_tr[sym] = dict(strategy=strategy, side=sig.side, entry=sig.entry_price, fill=fill, sl=sig.sl_price,
                        live_sl=sig.sl_price, tp=tp, qty=qty, size=tier, n=0, be=False,
                        inv=getattr(sig, "invalidate_level", None), t_open=t, day=SIM_DAY[0])
    return "trade"


# ── main loop ─────────────────────────────────────────────────────────
dfrom = args.dfrom or utc_day(times[0] + 6 * 86400000)     # need 5 days of liquidity map warm-up
dto = args.dto or utc_day(times[-1])
levels_ok = False
decisions = {}
for t in times:
    T = t + D15                          # processed when the candle closes
    if T % 86400000 == 0 or SIM_DAY[0] is None:
        day = utc_day(T)
        levels_ok = daily_reset(day) if dfrom <= day <= dto else False
        if day > dto and not open_tr:
            break
    SIM_NOW[0] = ist(T)
    b = C["BTCUSD"].get(t)
    if b and state.regime:
        state.update_regime_price(b["close"])
    allowed = in_entry_hours(T) and levels_ok and dfrom <= SIM_DAY[0] <= dto
    for sym in SYMBOLS:
        c = C[sym].get(t)
        if not c:
            continue
        hist[sym].append(c); del hist[sym][:-500]
        if sym in open_tr:
            ce = EX.get(sym, {}).get(t)
            if ce:
                manage(sym, open_tr[sym], ce, t)
        if not allowed:
            liq._step(sym, hist[sym], trade=False)
            continue
        if args.only != "liqmap":
            sig = engine.process_candle(sym, c)
            if sig:
                r = handle(sig, t, "main")
                decisions[("main", r)] = decisions.get(("main", r), 0) + 1
        liq._level[sym] = state.get_level(sym)
        msig = liq._step(sym, hist[sym], trade=True)
        if msig and args.only != "main":
            if sym in open_tr:
                r = "symbol-busy"
            else:
                r = handle(msig, t, "liqmap")
            decisions[("liqmap", r)] = decisions.get(("liqmap", r), 0) + 1

with open(args.out, "w", newline="") as f:
    if trades:
        w = csv.DictWriter(f, fieldnames=list(trades[0])); w.writeheader(); w.writerows(trades)
print(f"period {dfrom} .. {dto} | trades {len(trades)} | still open {len(open_tr)}")
print("decisions:", dict(sorted(decisions.items())))
print(f"main signals while same symbol already open (overnight collision): {len(overnight_collisions)}")
for x in overnight_collisions[:8]:
    print("   ", x)
if args.trace:
    print("\n".join(trace_lines))
