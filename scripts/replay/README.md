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

## ICT Killzones & Pivots [TFO] levels (tested 5 Nov 2026)

Same main-strategy engine, spot signals, futures fills/exits, fees included,
8 Oct 2025 – 4 Nov 2026, halves split 28 Apr 2026. TFO defaults: sessions in
New York time (DST-aware) — Asia 20:00–00:00, London 02:00–05:00, NY AM 09:30–11:00,
NY PM 13:30–16:00; a killzone's high/low is traded until that killzone's next
session starts. Weeks start Monday 00:00 UTC, months the 1st 00:00 UTC.
Options: `--levels weekly|monthly|kz:ASIA|kz:LNDN|kz:NYAM|kz:NYPM`, `--kz-filter`, `--target weekly`.

| Test | Trades | avg R | Total | 1st half | 2nd half |
|---|---|---|---|---|---|
| Baseline: PDH/PDL | 464 | +0.03 | −₹2,867 | −₹7,513 | +₹4,646 |
| A. PWH/PWL sweeps | 129 | +0.08 | −₹557 | +₹4,820 | −₹5,377 |
| B. PMH/PML sweeps | 49 | −0.21 | −₹7,733 | −₹3,431 | −₹4,302 |
| C1. Asia high/low | 578 | +0.02 | −₹19,969 | −₹20,475 | +₹506 |
| C2. London high/low | 484 | −0.04 | −₹39,668 | −₹12,424 | −₹27,244 |
| C3. NY AM high/low | 355 | +0.13 | +₹3,194 | +₹4,434 | −₹1,241 |
| C4. NY PM high/low | 461 | −0.09 | −₹42,709 | −₹14,727 | −₹27,982 |
| D. PDH/PDL, sweep inside a killzone only | 317 | +0.05 | −₹1,562 | −₹3,456 | +₹1,894 |
| E. PDH/PDL, target = opposite weekly level | 444 | −0.11 | −₹33,298 | −₹23,135 | −₹10,163 |

None is positive in both halves. NY AM is the only one with a positive average R in
both halves (+0.14 / +0.12) but fees take it to about break-even (2nd half −₹1,241);
with 8 variants tested, one near-miss is expected by chance. NY PM (23:00–01:30 IST
summer) lies entirely outside the bot's 05:30–23:00 IST entry hours.
