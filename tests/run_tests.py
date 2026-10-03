"""Liquidity Bot test suite — run before EVERY deploy.

    python tests/run_tests.py

No network, no exchange, no Telegram: everything external is faked, and
all state files go to a throwaway temp folder. Exit code 0 = all passed.

Covers: imports, the full decision flow (every veto path and its alert
behaviour), INR sizing tiers, the daily loss halt, delayed entry, the
Liquidity Map exit rules, duplicate-close protection, snapshot saving,
and off-hours trade management.
"""
import asyncio
import os
import sys
import tempfile
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
os.environ["PERSIST_DIR"] = tempfile.mkdtemp(prefix="bot_tests_")
os.environ.setdefault("TRADE_SIZE_USD", "75")
for k in ("COINDCX_API_KEY", "COINDCX_API_SECRET", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
    os.environ.setdefault(k, "test")
for k in ("POOL_EQUAL_MODE", "POOL_SESSION_MODE", "LIQMAP_MODE"):
    os.environ.pop(k, None)

RESULTS = []


def test(fn):
    RESULTS.append(fn)
    return fn


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


# ── shared fakes ───────────────────────────────────────────────────────
class FakeTelegram:
    def __init__(self):
        self.sent = []

    async def send_alert(self, m):
        self.sent.append(m)


class FakeExchange:
    def __init__(self):
        self.orders, self.tp, self.sl = 0, None, None

    async def place_market_order(self, **k):
        self.orders += 1
        self.sl = k.get("sl_price")
        return {"id": "test", "quantity": k["quantity"]}

    async def update_position_tpsl(self, sym, tp_price=None, **k):
        self.tp = tp_price
        return True

    async def get_open_positions(self):
        return {}

    async def close_position_market(self, *a):
        return True


def make_monitor(regime="NEUTRAL", bias="NONE", confirms=0, auto=False, halted=False,
                 symbol="ETHUSD", pdh=2743.0, pdl=2600.0):
    from core.monitor import MarketMonitor
    from core.state import BotState, DailyLevel
    from core.agents.risk_agent import RiskAgent
    from core.agents.pattern_agent import PatternAgent
    m = MarketMonitor.__new__(MarketMonitor)
    m.state = BotState()
    lv = DailyLevel(pdh=pdh, pdl=pdl)
    lv.trend_bias, lv.counter_trend_confirms, lv.auto_traded_today = bias, confirms, auto
    m.state.levels[symbol] = lv
    m.state.get_regime = lambda: regime
    m.state.get_level = lambda s: m.state.levels.get(s)
    m.telegram, m.coindcx = FakeTelegram(), FakeExchange()
    m.risk = RiskAgent()
    m.risk.realised_usd = -30.0 if halted else 0.0
    m.pattern = PatternAgent()
    m._last_obi, m._open_positions, m._trailing = {}, {}, {}
    m._position_lock = asyncio.Lock()
    m._log_close = lambda *a, **k: None
    return m


def main_signal(side="SELL", reject=None, counter=False):
    from core.strategy import Signal
    return Signal("ETHUSD", side, 2700.0, 2716.0 if side == "SELL" else 2684.0, 2743.0, 2600.0,
                  counter_trend=counter, trend_mode=False, swept_level=2743.0,
                  reject_reason=reject, use_staged_entry=False)


def run(coro):
    return asyncio.run(coro)


# ── tests ──────────────────────────────────────────────────────────────
@test
def imports_and_startup_modules():
    import main  # noqa: F401
    from core.agents import context_agent, decision, risk_agent, pattern_agent  # noqa: F401
    from core.agents import pool_agent, liquidity_map_agent  # noqa: F401


@test
def no_undefined_names():
    try:
        import pyflakes.api, pyflakes.reporter, io
    except ImportError:
        print("      (pyflakes not installed — skipped; pip install pyflakes)")
        return
    out = io.StringIO()
    rep = pyflakes.reporter.Reporter(out, out)
    for d in ("core", "exchange", "utils"):
        pyflakes.api.checkRecursive([d], rep)
    pyflakes.api.checkPath("main.py", rep)
    bad = [l for l in out.getvalue().splitlines() if "undefined name" in l or "may be undefined" in l]
    check(not bad, "undefined names:\n" + "\n".join(bad))


@test
def decision_flow_every_path():
    cases = [  # name, monitor kwargs, signal kwargs, expected (alerts, order placed)
        ("hard-rule rejection", {}, {"reject": "R:R low"}, (1, False)),
        ("already traded today", {"auto": True}, {}, (1, False)),
        ("trend stability (log only)", {"bias": "UPTREND", "confirms": 2}, {}, (0, False)),
        ("counter-trend (log only)", {"bias": "UPTREND"}, {"counter": True}, (0, False)),
        ("BTC regime (log only)", {"regime": "BULLISH"}, {}, (0, False)),
        ("daily loss halt", {"halted": True}, {}, (1, False)),
        ("clean setup trades", {}, {}, (2, True)),
    ]
    for name, mk, sk, exp in cases:
        m = make_monitor(**mk)
        try:
            run(m._handle_signal(main_signal(**sk)))
        except Exception:
            pass
        got = (len(m.telegram.sent), m.coindcx.orders > 0)
        check(got == exp, f"{name}: expected (alerts, order)={exp}, got {got}")


@test
def inr_tiers():
    from core.agents.risk_agent import RiskAgent
    for e, sl, exp in ((0.5709, 0.5837, "SKIP"), (1.3727, 1.3947, "STAGED"), (100.0, 100.5, "FULL")):
        tier, _, inr = RiskAgent.inr_tier(e, sl, 80, 10, 800, 1300, 88.0)
        check(tier == exp, f"entry {e} sl {sl}: expected {exp}, got {tier} (Rs{inr:.0f})")


@test
def inr_rate_single_source():
    import core.monitor as M
    import core.agents.risk_agent as R
    check(R.USD_TO_INR_RATE == 99.51, f"rate is {R.USD_TO_INR_RATE}, expected CoinDCX 99.51")
    check(M.USD_TO_INR_RATE is R.USD_TO_INR_RATE, "monitor must use risk_agent's rate, not its own copy")
    # ZAMAUSD 02-Oct real fill: entry 0.07885, SL 0.07993, 75 USDT x10 -> CoinDCX showed Rs1,018
    tier, usd, inr = R.RiskAgent.inr_tier(0.07885, 0.07993, 75, 10, 800, 1300, R.USD_TO_INR_RATE)
    check(tier == "STAGED" and 1000 < inr < 1040, f"ZAMA fill must be STAGED near Rs1,018 (got {tier} Rs{inr:.0f})")


@test
def daily_halt_and_persistence():
    from core.agents.risk_agent import RiskAgent, DAILY_LOSS_LIMIT_USD
    check(DAILY_LOSS_LIMIT_USD == 20.10, f"limit is {DAILY_LOSS_LIMIT_USD} (Rs2000 / 99.51)")
    r = RiskAgent(); r.realised_usd = 0.0; r.seen = []
    for i, (e, x, q) in enumerate(((0.3545, 0.3612, 1128.3), (0.0906, 0.0897, 8830), (304.72, 306.59, 2.625))):
        side = "SELL" if i != 1 else "BUY"
        r.record_close("T", side, e, x, q, key=f"halt-test-{i}")
    check(r.halted, f"three losses (${r.realised_usd:.2f}) should halt")
    check(RiskAgent().halted, "halt must survive a restart")


@test
def duplicate_close_ignored():
    from core.monitor import MarketMonitor
    from core.agents.risk_agent import RiskAgent

    class P:
        n = 0

        def record(self, *a):
            P.n += 1
    tr = {"side": "SELL", "entry": 120.29, "sl": 120.51, "qty_open": 6.23,
          "opened_at": f"dup-{time.time()}", "features": {"sl": "tight<1%"}}
    totals = []
    for _ in range(3):   # original exit + two restart replays
        m = MarketMonitor.__new__(MarketMonitor)
        m.risk, m.pattern = RiskAgent(), P()
        m._log_close("SOLUSD", dict(tr), exit_price=119.466, reason="x", qty=6.23)
        totals.append(round(m.risk.realised_usd, 4))
    check(totals[0] == totals[1] == totals[2], f"P&L changed on replay: {totals}")
    check(P.n == 1, f"pattern memory learned {P.n}x, expected 1")


@test
def snapshot_saved_when_position_closes():
    import core.monitor as M
    saves, orig = [], M.persistence.save_state
    M.persistence.save_state = lambda st, trl: saves.append(sorted(trl))
    try:
        m = M.MarketMonitor.__new__(M.MarketMonitor)
        m.state, m._saved_open, m._loops_since_save, m._trailing = None, ["SOLUSD"], 1, {}
        m._save_if_positions_changed()
        m._save_if_positions_changed()
    finally:
        M.persistence.save_state = orig
    check(saves == [[]], f"expected exactly one save without SOLUSD, got {saves}")


@test
def delayed_entry_fires_on_configured_candle():
    from core.state import BotState, DailyLevel
    from core.strategy import StrategyEngine, ENTRY_DELAY_CANDLES
    st = BotState(); st.levels["ETHUSD"] = DailyLevel(pdh=2800.0, pdl=2600.0)
    eng = StrategyEngine(object(), st)
    st.levels["ETHUSD"].pending_entry = {
        "side": "SELL", "sl": 2789.0, "pdh": 2800.0, "pdl": 2600.0, "effective_level": 2800.0,
        "sweep_extreme": 2789.0, "target": 2600.0, "counter_trend": False,
        "pattern": "Liquidity Sweep", "candles_remaining": ENTRY_DELAY_CANDLES}
    fired = None
    for i in range(1, ENTRY_DELAY_CANDLES + 2):
        c = 2770 - i * 4
        if eng.process_candle("ETHUSD", {"time": i, "open": c + 2, "high": c + 5, "low": c - 5, "close": c}):
            fired = fired or i
    check(fired == ENTRY_DELAY_CANDLES, f"fired on candle {fired}, expected {ENTRY_DELAY_CANDLES}")


@test
def liquidity_map_option2_tao_example():
    import core.agents.liquidity_map_agent as LM
    from core.state import DailyLevel
    lv = DailyLevel(pdh=310.0, pdl=290.0)
    a = LM.LiquidityMapAgent(None); a._level["TAOUSD"] = lv
    a.levels = {"TAOUSD": [
        {"k": "low", "p": 298.92, "zone": 299.2, "t": 0, "touch": [1, 2], "inz": False},
        {"k": "low", "p": 296.50, "zone": 296.8, "t": 0, "touch": [], "inz": False},
        {"k": "high", "p": 305.86, "zone": 305.5, "t": 0, "touch": [], "inz": False}]}
    # a GENUINE sweep: wick to 297.90 = 0.34% past 298.92 (the real 30-Sep wick of
    # 0.11% is now skipped as noise -- see liquidity_map_min_sweep_depth)
    sig = a._step("TAOUSD", [{"open": 300.73, "high": 301.27, "low": 297.90, "close": 298.98, "time": 10 ** 7}])
    check(sig is not None, "no signal on the proven-level sweep")
    check(sig.sl_price == 296.50 and sig.target == 305.86 and sig.invalidate_level == 298.92,
          f"sl/target/invalidate = {sig.sl_price}/{sig.target}/{sig.invalidate_level}")
    m = make_monitor(symbol="TAOUSD", pdh=310.0, pdl=290.0, regime="BEARISH")
    run(m._handle_signal(sig))
    check("TAOUSD" in m._trailing, "Liquidity Map trade did not open (Context must be skipped)")
    check(m.coindcx.sl == 296.50 and m.coindcx.tp == 305.86, f"exchange SL/TP {m.coindcx.sl}/{m.coindcx.tp}")
    check("tp1_price" not in m._trailing["TAOUSD"], "TP ladder must not be seeded")

    async def exits():
        # wick to 297.46 but CLOSE 299.06 above the level -> stays open
        await m._check_exit_conditions("TAOUSD", {"open": 299.07, "high": 299.4, "low": 297.46, "close": 299.06, "time": 1}, None)
        check("TAOUSD" in m._trailing, "wick below the level must NOT exit")
        await m._check_exit_conditions("TAOUSD", {"open": 303.9, "high": 305.9, "low": 303.4, "close": 305.5, "time": 2}, None)
        check("TAOUSD" not in m._trailing, "reaching the pool must exit")
    run(exits())


@test
def off_hours_manages_but_never_enters():
    import core.monitor as M
    from core.state import BotState, DailyLevel
    calls = {"exit": 0, "engine": 0}

    class Eng:
        def process_candle(self, *a):
            calls["engine"] += 1

    class CDX:
        t = 0

        async def get_latest_15m_candle(self, sym):
            CDX.t += 900000
            return {"time": CDX.t, "open": 1, "high": 1, "low": 1, "close": 1}
    m = M.MarketMonitor.__new__(M.MarketMonitor)
    m.state = BotState(); m.state.levels["SOLUSD"] = DailyLevel(pdh=121.0, pdl=117.0)
    m.state.levels_ready = lambda: True
    m.coindcx, m.engine, m._offhours_logged, m._saved_open = CDX(), Eng(), False, None
    m._trailing = {"SOLUSD": {"side": "BUY", "entry": 119.0, "sl": 118.0, "live_sl": 118.0}}
    m._last_candle_time, m._last_candle, m._loops_since_save, m._SAVE_EVERY_N_LOOPS = {"SOLUSD": None}, {}, 0, 999
    m._is_trading_hours = lambda: False

    async def ex(*a):
        calls["exit"] += 1

    async def rc():
        pass
    m._check_exit_conditions, m._reconcile_positions = ex, rc
    orig_poll, orig_save = M.POLL_INTERVAL_SECONDS, M.persistence.save_state
    M.POLL_INTERVAL_SECONDS, M.persistence.save_state = 0.01, (lambda *a: None)

    async def go():
        try:
            await asyncio.wait_for(m.run(), timeout=0.1)
        except asyncio.TimeoutError:
            pass
    try:
        run(go())
    finally:
        M.POLL_INTERVAL_SECONDS, M.persistence.save_state = orig_poll, orig_save
    check(calls["exit"] > 0, "open trade not managed overnight")
    check(calls["engine"] == 0, "strategy engine ran overnight (new entries possible)")


@test
def trend_trail_no_same_candle_exit():
    """Regression: 01-Oct XRPUSD. A candle must never hit the trail it just
    created (its low came BEFORE its high). Exit only on a later candle."""
    m = make_monitor(symbol="XRPUSD", pdh=1.5443, pdl=1.4852)

    class X(FakeExchange):
        async def update_stop_loss(self, sym, new_sl_price=None):
            return True

        async def get_open_positions(self):
            return {"XRPUSD": 100.0}

        async def get_position_details(self, sym):
            return {"roe": 0.0}
    m.coindcx = X()
    m._trailing["XRPUSD"] = {"side": "BUY", "entry": 1.4814, "sl": 1.4744, "live_sl": 1.4744,
                             "mfe": 1.4814, "tp": 1.5443, "qty_open": 100, "opened_at": "t"}
    m._open_positions["XRPUSD"] = 100

    async def go():
        await m._check_exit_conditions("XRPUSD", {"open": 1.4814, "high": 1.4923, "low": 1.4810,
                                                  "close": 1.4918, "time": 1}, None)
        check("XRPUSD" in m._trailing, "exited on the same candle that set the trail (look-ahead bug)")
        check(abs(m._trailing["XRPUSD"].get("trail_sl", 0) - 1.4902) < 1e-4, "trail not set to 1.4902")
        await m._check_exit_conditions("XRPUSD", {"open": 1.4918, "high": 1.4920, "low": 1.4895,
                                                  "close": 1.4900, "time": 2}, None)
        check("XRPUSD" not in m._trailing, "a later candle through the trail must exit")
    run(go())


def _obi_monitor(obi_seq):
    """ZAMAUSD SHORT at 0.07916 (02-Oct), fake book returning OBI values in order."""
    m = make_monitor(symbol="ZAMAUSD", pdh=0.0850, pdl=0.0724)

    class X(FakeExchange):
        seq = list(obi_seq)

        next_obi = 0.0

        async def get_orderbook(self, sym, depth=20):
            v = self.next_obi
            if v is None:
                raise RuntimeError("api timeout")
            b, a = (1 + v) * 50, (1 - v) * 50          # (b-a)/(b+a) = v
            return {"bids": {0.0790: b}, "asks": {0.0791: a}}

        async def get_open_positions(self):
            return {"ZAMAUSD": 9474.0}

        async def get_position_details(self, sym):
            return {"roe": 0.0}

        async def update_stop_loss(self, *a, **k):
            return True
    m.coindcx = X()
    m._trailing["ZAMAUSD"] = {"side": "SELL", "entry": 0.07916, "sl": 0.07993, "live_sl": 0.07993,
                              "mfe": 0.07916, "tp": 0.0760, "qty_open": 9474, "opened_at": "t",
                              "simple_exit": True, "invalidate_level": 0.0795}
    m._open_positions["ZAMAUSD"] = 9474
    return m


def _candle(close, t):
    return {"open": close, "high": close + 0.00002, "low": close - 0.00002, "close": close, "time": t}


def _feed(m, sym, readings_and_closes):
    out = []
    for t, (obi, close) in enumerate(readings_and_closes, 1):
        m.coindcx.next_obi = obi
        run(m._check_exit_conditions(sym, _candle(close, t), None))
        out.append(sym in m._trailing)
    return out


@test
def obi_escalation_umars_example_exits():
    # SHORT at 0.07916; book pressure 0.46 -> 0.55 -> 0.60 -> 0.70 -> 0.82, trade losing
    m = _obi_monitor([])
    open_ = _feed(m, "ZAMAUSD", [(0.46, 0.07920), (0.55, 0.07925), (0.60, 0.07930),
                                 (0.70, 0.07935), (0.82, 0.07940)])
    check(open_ == [True, True, True, True, False],
          f"must stay open through the rise and exit when it passes 0.80 on the 4th rise: {open_}")


@test
def obi_escalation_pullback_continues():
    # 0.46 -> 0.55 -> 0.60 -> 0.70 -> 0.55 (drop = pullback) -> keep trading, count restarts
    m = _obi_monitor([])
    open_ = _feed(m, "ZAMAUSD", [(0.46, 0.07920), (0.55, 0.07925), (0.60, 0.07930),
                                 (0.70, 0.07935), (0.55, 0.07935), (0.82, 0.07940)])
    check(all(open_), f"a drop must reset the count, so 0.82 right after it must NOT exit: {open_}")
    check(m._trailing["ZAMAUSD"]["obi_rise"] == 1, "count must have restarted after the drop")


@test
def obi_escalation_needs_level_and_losing():
    m = _obi_monitor([])
    open_ = _feed(m, "ZAMAUSD", [(-0.60, 0.07920), (-0.50, 0.07925), (-0.40, 0.07930),
                                 (-0.30, 0.07935), (-0.20, 0.07940)])
    check(all(open_), "rising but book still ON the SHORT's side (negative) must not exit")
    m = _obi_monitor([])
    open_ = _feed(m, "ZAMAUSD", [(0.10, 0.07920), (0.20, 0.07925), (0.30, 0.07930), (0.40, 0.07935)])
    check(all(open_), "only 3 rises must not exit")
    m = _obi_monitor([])
    open_ = _feed(m, "ZAMAUSD", [(0.46, 0.0789), (0.55, 0.0788), (0.60, 0.0787),
                                 (0.70, 0.0786), (0.85, 0.0785)])
    check(all(open_), "a SHORT in profit must never be closed, however strong the book")


@test
def obi_escalation_long_mirror_and_safety():
    m = _obi_monitor([])
    m._trailing["ZAMAUSD"].update({"side": "BUY", "sl": 0.0784, "live_sl": 0.0784, "tp": 0.0820,
                                   "invalidate_level": 0.0785})
    open_ = _feed(m, "ZAMAUSD", [(-0.46, 0.0790), (-0.55, 0.0789), (-0.60, 0.0789),
                                 (-0.70, 0.0788), (-0.85, 0.0788)])
    check(open_[-1] is False, f"LONG: rising sell-side pressure past 0.80 while losing must exit: {open_}")
    m = _obi_monitor([])
    open_ = _feed(m, "ZAMAUSD", [(0.46, 0.07920), (None, 0.07925), (0.60, 0.07930)])
    check(all(open_), "an order-book API failure must never close the trade")
    import core.trade_manager as TM   # the OBI rule lives in the Trade Manager since the 02-Oct split
    orig = TM.OBI_EXIT_MODE; TM.OBI_EXIT_MODE = "off"
    try:
        m = _obi_monitor([])
        open_ = _feed(m, "ZAMAUSD", [(0.46, 0.07920), (0.55, 0.07925), (0.60, 0.07930),
                                     (0.70, 0.07935), (0.82, 0.07940)])
        check(all(open_), "OBI_EXIT_MODE=off must log only, never exit")
    finally:
        TM.OBI_EXIT_MODE = orig


@test
def candle_store_shared_and_failsafe():
    from core.candle_store import CandleStore
    import core.agents.pool_agent as PA
    import core.agents.liquidity_map_agent as LM

    class CDX:
        calls, fail = 0, False

        async def _get(self, path, params=None):
            CDX.calls += 1
            if CDX.fail:
                raise RuntimeError("api down")
            return [{"time": i * 900000, "open": 1, "high": 1, "low": 1, "close": 1} for i in range(1, 12)]
    cdx = CDX(); st = CandleStore(cdx)
    pa, lm = PA.PoolAgent(cdx, 75, 10, store=st), LM.LiquidityMapAgent(cdx, store=st)
    c = {"time": 11 * 900000, "open": 1, "high": 1, "low": 1, "close": 1}
    run(pa.on_candle("XRPUSD", c, None)); run(lm.on_candle("XRPUSD", c, None))
    check(CDX.calls == 1, f"two agents on the same candle must share ONE download (got {CDX.calls})")
    check(pa.store is lm.store, "both agents must use the same store")
    # a failed reload must never wipe the Liquidity Map's saved levels
    lm.levels["XRPUSD"] = [{"k": "low", "p": 0.9, "zone": 0.95, "t": 0, "touch": [1, 2], "inz": False}]
    CDX.fail = True
    gap = {"time": 40 * 900000, "open": 1, "high": 1, "low": 1, "close": 1}
    run(pa.on_candle("XRPUSD", gap, None)); run(lm.on_candle("XRPUSD", gap, None))
    check(len(lm.levels["XRPUSD"]) == 1, "a failed history download must keep existing levels")


@test
def liquidity_map_min_sweep_depth():
    import core.agents.liquidity_map_agent as LM
    from core.state import DailyLevel
    def agent(level, deeper, pool):
        a = LM.LiquidityMapAgent(None); a._level["X"] = DailyLevel(pdh=2.0, pdl=1.0)
        a.levels = {"X": [{"k": "high", "p": level, "zone": level * 0.999, "t": 0, "touch": [1, 2], "inz": False},
                          {"k": "high", "p": deeper, "zone": deeper * 0.999, "t": 0, "touch": [], "inz": False},
                          {"k": "low", "p": pool, "zone": pool * 1.001, "t": 0, "touch": [], "inz": False}]}
        return a
    # XRPUSD 02-Oct live loser: level 1.5094, wick to 1.5099 = 0.03% -> must be skipped
    a = agent(1.5094, 1.5200, 1.4700)
    sig = a._step("X", [{"open": 1.5080, "high": 1.5099, "low": 1.5070, "close": 1.5087, "time": 10 ** 7}])
    check(sig is None, "a 0.03% sweep must be skipped as noise")
    # RIFUSD 02-Oct: level 0.0837, wick to 0.0841 = 0.48% -> passes the depth rule
    a = agent(0.0837, 0.0850, 0.0800)
    sig = a._step("X", [{"open": 0.0836, "high": 0.0841, "low": 0.0834, "close": 0.0836, "time": 10 ** 7}])
    check(sig is not None, "a 0.48% sweep must be allowed")


@test
def candle_patterns_classified():
    from core.candle_patterns import classify
    cases = [
        ({"open": 10.0, "high": 10.05, "low": 9.00, "close": 10.02}, None, "hammer"),
        ({"open": 10.0, "high": 11.00, "low": 9.97, "close": 9.98}, None, "shooting_star"),
        ({"open": 10.0, "high": 10.50, "low": 9.50, "close": 10.01}, None, "doji"),
        ({"open": 10.0, "high": 11.02, "low": 9.99, "close": 11.00}, None, "bull_marubozu"),
        ({"open": 9.80, "high": 10.60, "low": 9.75, "close": 10.50}, {"open": 10.3, "high": 10.35, "low": 9.85, "close": 9.9}, "bull_engulfing"),
        ({"open": 10.0, "high": 10.20, "low": 9.90, "close": 10.15}, {"open": 9.8, "high": 10.5, "low": 9.6, "close": 10.3}, "inside_bar"),
    ]
    for c, prev, want in cases:
        got = classify(c, prev)
        check(want in got.split("+"), f"expected {want}, got {got} for {c}")


@test
def entry_candle_saved_on_trade():
    from core.strategy import Signal
    m = make_monitor()
    m._last_pattern = {"ETHUSD": "shooting_star"}
    run(m._handle_signal(main_signal()))
    check(m._trailing.get("ETHUSD", {}).get("entry_candle") == "shooting_star",
          "entry candle pattern must be saved on the trade record")


@test
def orderflow_volume_and_profile():
    from core.orderflow import VolumeTracker, volume_profile
    vt = VolumeTracker()
    for _ in range(5):
        vt.add("X", 100.0)
    check(abs(vt.add("X", 300.0) - 3.0) < 1e-9, "rvol of 300 vs average 100 must be 3.0x")
    check(VolumeTracker().add("Y", 50.0) is None, "rvol needs at least 5 earlier candles")
    cs = [{"high": 101, "low": 99, "volume": 1000}] * 6 + [{"high": 110, "low": 90, "volume": 50}] * 6
    vp = volume_profile(cs)
    check(vp and 99 <= vp["vpoc"] <= 101, f"VPOC must sit where most volume traded: {vp}")
    check(vp["val"] < vp["vpoc"] < vp["vah"], "value area must surround the VPOC")


@test
def orderflow_trade_feed_delta_cvd():
    from core.orderflow import TradeFlow
    T0 = 1790000000000 // 900000 * 900000

    class CDX:
        batch, base_works = [], True

        async def _get_base(self, path, params=None):
            return [dict(t) for t in CDX.batch] if CDX.base_works else None

        async def _get(self, path, params=None):
            return {"data": [dict(t) for t in CDX.batch]}
    tr = lambda ms, q, maker: {"timestamp": T0 + ms, "price": 1.5, "quantity": q, "is_maker": maker}
    f = TradeFlow(CDX())
    CDX.batch = [tr(1000, 10, False), tr(2000, 4, True)]            # buy 10, sell 4
    run(f.poll("XRPUSD"))
    CDX.batch = [tr(2000, 4, True), tr(3000, 6, False)]             # overlap + new buy 6
    run(f.poll("XRPUSD"))
    d = f.candle("XRPUSD", T0)
    check(d["n"] == 3 and abs(d["delta"] - 12) < 1e-9, f"dedupe/delta wrong: {d}")
    check(not d["gap"], "overlapping polls must not be flagged as a gap")
    CDX.batch = [tr(900000 + 5000, 5, True)]                         # next candle, but jumps past what we saw
    run(f.poll("XRPUSD"))
    CDX.batch = [tr(900000 + 600000, 1, True)]                       # oldest newer than last seen -> GAP
    run(f.poll("XRPUSD"))
    d2 = f.candle("XRPUSD", T0 + 900000)
    check(d2["gap"] and abs(d2["cvd"] - (12 - 6)) < 1e-9, f"gap flag / CVD wrong: {d2}")
    nxt = (T0 // 86400000 + 1) * 86400000
    CDX.batch = [{"timestamp": nxt + 1000, "price": 1.5, "quantity": 2, "is_maker": False}]
    run(f.poll("XRPUSD"))
    check(abs(f.candle("XRPUSD", nxt)["cvd"] - 2) < 1e-9, "CVD must reset on a new day")
    g = TradeFlow(CDX()); CDX.base_works = False; CDX.batch = [tr(1000, 3, False)]
    run(g.poll("XRPUSD"))
    check(g.endpoint == ("public", "/market_data/v3/trade_history"), "must fall back to the 2nd endpoint")

    class Dead:
        async def _get_base(self, *a, **k): return None
        async def _get(self, *a, **k): raise RuntimeError("404")
    h = TradeFlow(Dead())
    try:
        run(h.poll("XRPUSD"))
    except Exception:
        pass
    check(h.disabled_until > 0 and h.candle("XRPUSD", T0) is None,
          "no working endpoint must disable delta quietly, never crash")
    h.disabled_until = 0                         # an hour later: retry fails again...
    run(h.poll("XRPUSD"))
    check(h.disabled_until > time.time() + 3000, "a failed hourly retry must back off again")


@test
def orderflow_on_candle_line_and_trade_record():
    m = make_monitor()
    from core.orderflow import VolumeTracker, TradeFlow
    from core.candle_store import CandleStore
    m.vol, m.flow, m.candles, m._last_flow = VolumeTracker(), TradeFlow(None), CandleStore(None), {}
    base = 1790000000000 // 86400000 * 86400000          # start of a UTC day (05:30 IST)
    m.candles.hist["ETHUSD"] = [{"time": base + i * 900000, "open": 1, "high": 2, "low": 1, "close": 1.5,
                                 "volume": 100.0} for i in range(5)]
    for i in range(5, 11):
        txt = m._update_flow("ETHUSD", {"time": base + i * 900000, "open": 1, "high": 2,
                                        "low": 1, "close": 1.5, "volume": 100.0 * (3 if i == 10 else 1)})
    check("rvol 3.00x" in txt, f"candle line must carry volume and rvol: {txt}")
    check(m._last_flow["ETHUSD"]["vp"] is not None, "volume profile must be computed")
    m._last_pattern = {"ETHUSD": "hammer"}
    run(m._handle_signal(main_signal()))
    check(m._trailing["ETHUSD"].get("entry_flow", {}).get("rvol") is not None,
          "entry order-flow context must be saved on the trade record")


@test
def all_strategies_default_live():
    from core.agents.pool_agent import POOL_MODES
    import core.agents.liquidity_map_agent as LM
    from core.agents.pattern_agent import PATTERN_VETO_ENABLED
    check(POOL_MODES == {"EQUAL": "live", "SESSION": "live"}, f"pool modes {POOL_MODES}")
    check(LM.LIQMAP_MODE == "live", f"liquidity map mode {LM.LIQMAP_MODE}")
    check(PATTERN_VETO_ENABLED is False, "pattern agent must stay advisory")


# ── runner ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import logging
    logging.disable(logging.CRITICAL)
    failed = 0
    for fn in RESULTS:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL  {fn.__name__}: {e}")
            if not isinstance(e, AssertionError):
                traceback.print_exc()
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} passed")
    sys.exit(1 if failed else 0)
