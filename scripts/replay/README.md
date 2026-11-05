# Replay — test a rule on history before it goes live

Runs the bot's **real** strategy code (`StrategyEngine`, Liquidity Map, Risk and
Context agents) over downloaded CoinDCX 15m candles, candle by candle, and
simulates SIMPLE_MODE exits. On 5 Nov 2026 it reproduced the live log exactly
(KAITO sweep → trigger → invalidation → trigger → confirm 16:45 → entry 17:00 at
0.2251 / SL 0.2217; AERO breakout cancel 15:15 IST).

Needs Python 3.10+ (the server's 3.12 is fine) and `pip install -r requirements.txt`.

```bash
# 1. download ~400 days (public data, no API key). spot = what the bot reads.
python scripts/replay/fetch_candles.py replay_data/spot 400
python scripts/replay/fetch_candles.py replay_data/futures 400 --source futures

# 2. replay: spot signals, fills/exits on futures prices (closest to reality)
python scripts/replay/replay.py . replay_data/spot out.csv --to 2026-11-04 \
    --exec-candles replay_data/futures --fix-overnight

# 3. summary: whole period AND both halves (always report both)
python scripts/replay/summary.py out.csv --by strategy
```

Try a rule: `--set core.simple_mode:TP_R=2.0`, `--set core.strategy:MIN_SWEEP_DEPTH_PCT=0.0`,
`--set core.agents.liquidity_map_agent:MIN_SWEEP_PCT=0.5`, `--only main`.

**Limits:** fills exactly at stop/target (no slippage) and entry at the candle
close; order-book / order-flow features are not replayed (they are log-only);
coins sit out days before their data starts (AERO spot from 17 Jul 2026,
ZAMA spot from Feb 2026, ZAMA futures from Jun 2026).

## Results so far (8 Oct 2025 – 4 Nov 2026, deployed rules, fees included)

| Setup | Trades | Total | 1st half | 2nd half |
|---|---|---|---|---|
| Both strategies, spot signals, futures exits | 587 | −₹11,179 | −₹12,242 | +₹1,063 |
| Main only, spot signals, futures exits | 464 | −₹2,867 | −₹7,513 | +₹4,646 |
| Liquidity Map only, spot signals, futures exits | 165 | −₹10,285 | −₹7,636 | −₹2,649 |
| Main only, **futures** signals | 547 | −₹30,252 | −₹17,813 | −₹12,439 |

Fees are ~0.10R per trade (1R ≈ ₹500, round trip ≈ ₹50). Main strategy before
fees ≈ +0.05R/trade — not enough. No exit/sweep variant tested (TP 1R/2R/3R, no
breakeven, sweep min 0 / 0.5%, Liquidity Map depth 0.5% / 1.0%) was positive
in both halves. Removing the 0.2% minimum sweep was worse in both halves.
