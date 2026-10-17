"""Context Agent — is the market environment right for this setup?

Owns: trend stability block, trend-bias (counter-trend) block, BTC regime
block. All three are LOG-ONLY vetoes: a vetoed setup is recorded in the
logs and the decision record, and no Telegram alert is sent.
Order-book walls/gaps are logged at sweep time by monitor._log_obi.
"""
from core.agents.decision import Verdict

AGENT = "Context"


def evaluate(signal, level, regime: str, stability_max_counter_confirms: int) -> Verdict:
    if level.counter_trend_confirms >= stability_max_counter_confirms:
        return Verdict(AGENT, False,
                       f"trend stability: {level.counter_trend_confirms} counter-trend "
                       f"confirms today, {level.trend_bias} no longer trusted")
    from core import simple_mode
    # 2026-10-05: trend-bias block OFF in SIMPLE_MODE -- redundant since the breakout guard
    if signal.counter_trend and not simple_mode.ON:
        return Verdict(AGENT, False, f"fights today's {level.trend_bias} bias")
    btc_counter = (signal.side == 'BUY' and regime == 'BEARISH') or \
                  (signal.side == 'SELL' and regime == 'BULLISH')
    if btc_counter:
        return Verdict(AGENT, False, f"BTC regime {regime} fights this direction")
    return Verdict(AGENT, True, f"bias={level.trend_bias} regime={regime}")
