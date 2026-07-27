# TradeVerse Liquidity Bot

An automated futures trading bot on **CoinDCX**, trading a liquidity-sweep
reversal strategy across 8 symbols (ETHUSD, SOLUSD, XRPUSD, TAOUSD, AEROUSD,
LTCUSD, ICPUSD, KAITOUSD), 15-minute candles, 5:30 AM–11:00 PM IST daily.

## Strategy summary

**Core setup — sweep + reversal:**
1. Price sweeps the previous day's high (PDH) or low (PDL).
2. Bot watches for the first opposite-colored trigger candle.
3. On confirmation, enters against the sweep direction, targeting the
   opposite fixed daily level (PDH for shorts, PDL for longs).

**CISD (Change In State of Delivery) reclaim** — for "deep" sweeps that move
well past the fixed PDH/PDL, the reclaim reference used to confirm entry is
the open of the candle just before the trigger, instead of demanding a full
reclaim back to the original (now distant) fixed level. Added after real
cases where the fixed-level requirement cost hours of unnecessary delay on
an already-valid entry.

**Trend bias** — classified once daily from the last 3 daily candles.
`UPTREND`/`DOWNTREND` bias favors one side (auto-traded) over the other
(alert-only); `NONE` covers sideways or an already-matured 2+ day trend,
keeping the original dual-sided logic.

**Trend-aligned flip trades** — when in a trend, a bounce off a
counter-trend confirmation seeds a dynamic re-anchored level for hunting
liquidity in the trend's direction. These trades often enter already past
the fixed daily PDH/PDL, so they deliberately skip the fixed-target and
rejection-candle exit priorities and run on stop-loss + ROE protection
(and breakeven-move, see below) only.

**Rule: one auto-trade per symbol per day.** Further clean setups on an
already-traded symbol are alert-only regardless of outcome. Counter-trend
signals are always alert-only, regardless of this rule.

## Exit logic (priority order, evaluated every closed candle)

1. **Target achieved** — PDH (long) / PDL (short) reached. Skipped for
   trend-mode trades.
2. **Rejection exit** — a rejection/engulfing candle near target, only if
   the position is already profitable and at ≥3% ROE. Skipped for
   trend-mode trades.
3. **ROE protection** — closes at ≥7% ROE regardless of the above.

**TP1/TP2/TP3 partial ladder** (non-trend-mode only): TP1 at 1.5R, TP2 at
2.5R, TP3 = the original single target above. Only seeded if TP1/TP2 sit
closer than TP3 — otherwise the trade behaves as a single-target trade,
unchanged. TP1/TP2 are bot-managed partial market closes, always sized off
a freshly-fetched live quantity (CoinDCX's `close_position_market` has no
`reduce_only` support, so trusting a locally-cached quantity risks flipping
the position instead of trimming it). TP3 stays a resting exchange-side
order — a dead-man's-switch full close if the bot itself is ever down.

**Breakeven stop-move**: once a trade's MFE reaches +0.5R (any trade type,
including trend-mode), the SL moves to entry price. Added after a 7-day
analysis showed most losing trades had moved meaningfully in the account's
favor before reversing to a full stop-out.

## State persistence & restart recovery

Railway containers restart often (redeploys, occasional crashes) — an
in-memory-only bot loses all same-day sweep-in-progress state and
in-flight SL/TP/MFE-MAE tracking on every restart. `core/persistence.py`
snapshots `state.levels` + the monitor's trailing dict to a mounted volume
(`/data/state_snapshot.json`) roughly every minute, and on startup, restores
same-day state before the daily 5:30 AM reset logic runs. A cross-check
against live exchange positions flags (once, not repeatedly) any position
CoinDCX shows open that has no local tracking record — this can still
happen for a position opened before this system existed, or in the narrow
window between order placement and the next snapshot.

## Trade history & Telegram commands

Every open/close/partial-close event is appended to `/data/trades.jsonl`
(one JSON object per line) — independent of Railway's log retention
entirely. From the bot's Telegram chat:
- `/trades` — sends the full trade history file
- `/state` — sends the current state snapshot (debugging aid)
- `/help` — lists commands

Trade-execution alerts include historical stats (occurrence count, win
rate, avg/largest move, avg hold time) per symbol once 5+ closed trades
are logged — shown as "not enough data yet" below that threshold rather
than a misleadingly precise number from a tiny sample.

## Known limitations / open items

- **CoinDCX futures wallet-balance endpoint returns 404** (confirmed since
  2026-07-17). Margin cap tracking uses the bot's own committed-margin
  count against an optional `TOTAL_ACCOUNT_MARGIN_USD` env var instead.
- **Breakeven move uses exact entry price**, not accounting for the taker
  fee — a breakeven-triggered exit is a small net loss after fees, not a
  true scratch.
- **Full backtest comparison of alternate TP methodologies** (PDH/PDL-based,
  swing-based, ATR-based, trailing) needs several weeks of clean
  `trades.jsonl` data to be trustworthy — the 7-day sample used to justify
  the current TP1/TP2/TP3 ladder had too many restart-related gaps for a
  confident multi-method comparison.

## Environment variables

- `COINDCX_API_KEY`, `COINDCX_API_SECRET`
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- `TOTAL_ACCOUNT_MARGIN_USD` (optional — enables the local margin cap)
- `PERSIST_DIR` (optional — defaults to `/data`, the Railway volume mount)
