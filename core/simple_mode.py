"""SIMPLE MODE (2026-10-05) — one switch for the simplified trading design.

Why: over 10 weeks the main strategy with its full stack of competing exits
lost Rs4,437; the same entries with ONE stop and ONE target were about
break-even (best tested: 1-candle delay, 1.5R target, single breakeven at 1R
= +Rs991 over 56 trades). Live 7-day review also showed strategies trading
against each other on the same coin (Oct 4).

When ON (default):
  * Entry      : 1 candle after confirmation (was 3) — sharper entries
  * Stop       : the sweep extreme; only ever moved once, to entry at +1R
  * Target     : fixed 1.5R, set on the exchange at entry
  * Removed    : early failure, 0.4R breakeven, trend trail, TP ladder,
                 staged add-on, ROE partial close, near-target rejection,
                 OBI escalation EXIT (OBI is still logged)
  * Context    : trend-bias block OFF (redundant since the breakout guard);
                 BTC-regime and trend-stability blocks stay ON
  * Arbitration: one direction per coin per day — the first executed trade
                 decides; any later opposite signal on that coin is vetoed
The Liquidity Map keeps its own tested exits and is not affected.
Railway: SIMPLE_MODE = on (default) / off (old behaviour). Restart to apply.
"""
import os

ON = os.getenv("SIMPLE_MODE", "on").strip().lower() == "on"
TP_R = 1.5          # fixed target in R
BE_AT_R = 1.0       # single breakeven move
TIMEOUT_CANDLES = 96
