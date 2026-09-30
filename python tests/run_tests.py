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
    bad = [l for l in out.getvalue().splitlines() if "undefined name" in l]
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
def daily_halt_and_persistence():
    from core.agents.risk_agent import RiskAgent, DAILY_LOSS_LIMIT_USD
    check(DAILY_LOSS_LIMIT_USD == 22.73, f"limit is {DAILY_LOSS_LIMIT_USD}")
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
    sig = a._step("TAOUSD", [{"open": 300.73, "high": 301.27, "low": 298.58, "close": 298.98, "time": 10 ** 7}])
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
