# CLAUDE.md — TradeVerse Liquidity Bot (handoff context)

> Context for Claude Code. Last updated: 5 Nov 2026 (late). Owner: Umar.
> **Never ask for, print, or commit secrets.**

---

## 1. What this is

An automated crypto **futures** trading bot on **CoinDCX** (USDT-margined perpetuals), trading
**liquidity sweeps** on 15-minute candles.

- **Repo:** `github.com/umar-TradeVerse/liquidity-bot` (public), branch **`main`**
- **Language:** Python 3.12 on the server (asyncio, aiohttp, apscheduler, pytz). Entry point: `python main.py`
- **Symbols (8):** ETHUSD, SOLUSD, XRPUSD, TAOUSD, AEROUSD, KAITOUSD, RIFUSD, ZAMAUSD.
  BTCUSD is used only as a **regime reference** (BULLISH / BEARISH / NEUTRAL)
- **Size:** `TRADE_SIZE_USD=50` margin per trade (set in the env file; code default is 40), **10× leverage**
- **⚠️ Currently in PAPER mode** (`TRADING_MODE=paper`, since 5 Nov 22:39 IST) — no real orders. See §5.

---

## 2. Owner preferences — follow these

- **Times in IST.** Server and log timestamps are **UTC** — always convert (IST = UTC + 5:30).
- **Evidence first.** Before changing trading logic, **replay it** (`scripts/replay/`) and report
  results for the **whole period AND both halves**. Don't adopt rules that only work in one half,
  or that come from a handful of trades. Several "obvious fixes" were tested and made things worse
  (latest: switching signals to futures candles — §6).
- **Be direct; no comforting.** State plainly when something won't work or the data doesn't support it.
- **Be proactive.** Front-load all checks and considerations rather than surfacing problems one at a time.
- **Code delivery: Claude commits and pushes to `main` itself** (owner's instruction, 5 Nov 2026).
  The owner's Mac has the clone at `/Users/umar/backtest/liquidity-bot` and `gh` logged in as
  `umar-TradeVerse`. Commit as `umar-TradeVerse` with the GitHub noreply email. Run the tests and
  pyflakes first (Python 3.10+ — the Mac's system 3.9 gives false "no current event loop" failures),
  push, then verify a fresh clone. Say what will change before pushing anything that alters trading.
- The owner uses a terminal only for pasting commands into the DigitalOcean **Web Console**
  (server settings). Give one command per block.
- **Secrets never pass through chat.** Scripts must prompt for secrets on the server. Never print
  keys, secrets, or tokens (mask them: length + last 4 characters).
- After a push, confirm the ✅ *Deployed* Telegram message / `botstatus` code version.
- Win-rate targets like "85–90%" were rejected as unrealistic; optimise expectancy, not win rate.

---

## 3. Deployment (DigitalOcean — Railway is retired)

Since **29 Oct 2026**, CoinDCX binds each API key to **ONE IP**, so the bot runs on a Droplet.

| Item | Value |
|---|---|
| Droplet | `tradeverse-bots` — Bangalore (BLR1), Ubuntu 24.04, 1 GB / $6/mo |
| Public IPv4 | **206.189.138.66** — the CoinDCX key is IP-bound to this. IPv6 OFF, IPv4 forced (`/etc/gai.conf`) |
| Code | `/opt/bots/liquidity-bot` (git clone of `main`), venv at `/opt/bots/liquidity-bot/venv` |
| Service | systemd `liquidity-bot.service`, runs as user `bot`, `Restart=always` |
| Settings / secrets | `/etc/bots/liquidity-bot.env` (root-only, `chmod 600`) |
| Persistent data | `PERSIST_DIR=/opt/bots/liquidity-bot/data` (snapshot, trades.jsonl, liquidity map, daily P&L, pattern memory, paper_positions.json) |
| Logs | `logs/bot.log` — one combined, time-ordered log (rotating, 10 MB × 10). Only since the move to the Droplet (4 Nov 20:32 UTC); **Railway logs are gone** |

### Auto-deploy
`bot-update@liquidity-bot.timer` runs `/usr/local/bin/bot-update` **every 5 min**: fetch → if
`origin/main` changed, stage + `pip install` + **`tests/run_tests.py`** → pass: switch, restart,
Telegram ✅ *Deployed …*; fail: keep the old version, Telegram ❌ *NOT deployed*. **The test suite
is the deployment gate.** Deploy takes ~5–8 min after a push.

### Server helper commands (Web Console)
- `botstatus` — service state, code version, outgoing IP
- `botlogs [bot] ["since"]` / `botlogs-send [bot] ["since"]`
- Change a setting: `nano /etc/bots/liquidity-bot.env` then `systemctl restart liquidity-bot`
- `droplet_setup.sh` (repo root) is an **older** copy without robust paste handling — any new setup
  script must re-ask on empty input and strip whitespace / bracketed-paste markers.

### Telegram
- Bot **username `@TradeWerse_Bot`** (with a W; display name "TradeVerse_Bot"), sends to the private
  chat of `umar_haleem`. Only `TELEGRAM_CHAT_ID` is obeyed.
- `/status` (mode, positions, P&L, version, uptime), `/logs [hours]`, `/settings` (allow-listed),
  `/trades`, `/state`, `/help` — `@BotName` suffixes work. Logs sent nightly at **23:00 IST**.
- In paper mode every message starts with **"📝 PAPER —"**.
- Each bot needs its **own** token (two pollers on one token → `getUpdates 409`; seen 4 Nov 01:21–01:30 IST).

---

## 4. Code layout

```
main.py                    startup, scheduler (05:30 IST reset, 23:00 IST logs), paper wrapping
core/monitor.py            main loop, per-symbol candle processing, sub-agent hooks
core/execution.py          orders, SL/TP, exits, reconciliation, close logging
core/trade_manager.py      exit checks; SIMPLE-mode manager (_simple_manage); OBI watch
core/paper_exchange.py     TRADING_MODE switch + PaperExchange (simulated account calls)
core/trading_common.py     shared constants/helpers (re-exported by monitor.py)
core/strategy.py           MAIN strategy: PDH/PDL sweep -> trigger -> confirm -> delayed entry
core/simple_mode.py        SIMPLE_MODE switch (default ON)
core/state.py              BotState, DailyLevel
core/persistence.py        state snapshot (+ last candle per symbol) + trades.jsonl (tagged mode)
core/candle_store.py       shared 15m candle history for sub-agents
core/orderflow.py, candle_patterns.py, obi.py   LOG-ONLY research data
core/ops_reports.py        /logs, /status, /settings text
core/command_listener.py   Telegram command polling
core/agents/               risk, context, liquidity_map, pool (off), breakout (off), pattern (advisory)
exchange/coindcx.py        CoinDCX client; CANDLE_SOURCE switch; PositionsUnavailable
notifications/telegram.py  send / getUpdates (with backoff) / sendDocument
scripts/replay/            candle downloader, replay, summary — see its README
tests/run_tests.py         58 tests, plain Python. Run: python tests/run_tests.py
```

---

## 5. Live logic (as deployed) and switches

### Main strategy — sweep of yesterday's high/low (PDH/PDL)
1. **Sweep** beyond PDH (→ short setup) / PDL (→ long). **Every sweep — the first one too — must go
   0.2% past the level** (`MIN_SWEEP_DEPTH_PCT`). So "no minimum sweep" (`MIN_LIQUIDITY_SWEEP_PCT=0`)
   is really a 0.2% minimum. Replay: removing the 0.2% is worse in both halves → **keep it**
   (this settles the old "0.2% re-test rule" question). Inside bars never arm a sweep.
2. **Trigger:** first opposite-colour candle (the sweep candle itself can count if it has a big wick).
   Invalidated by a close beyond the trigger's far side.
3. **Confirmation:** close beyond the trigger high/low, confirming colour, 0.15% back inside the level
   (or a CISD reclaim on sweeps > 0.5%). Setups expire 6 h after the sweep.
4. **Entry** 1 candle after confirmation (simple mode). **Breakout guard:** entry candle must close
   back inside the swept level, else cancelled.
5. **Stop** = sweep extreme; **target 1.5R**; **breakeven** at +1R from the next candle (retried on the
   exchange until it succeeds); **timeout** 96 candles. R:R to the opposite level ≥ 1.0; one main
   trade per symbol per day; last entry on the 22:45 candle close (23:00 IST cut-off).

### Liquidity Map agent
Swing highs/lows (2 h each side, 5 days), trades sweeps of levels respected ≥ 2× with depth ≥ 0.30%;
target next opposite level (≥ 1R); exit on a close back through the level or hard stop at the next
deeper level. Skips Context. **Replay says it loses even before fees** (§6).

### Risk
`USD_INR_RATE=99.51`; stop risk ≤ ₹800 full size, ₹800–1,300 half, > ₹1,300 skip; daily loss halt
₹2,000 realised (reset 05:30 IST); max 2 positions; one direction per coin per day.

### Switches (env file)
| Variable | Values | Meaning |
|---|---|---|
| `TRADING_MODE` | `live` (default if unset) / `paper` | paper = every account call simulated, signed requests blocked; market data live. **Any other value = paper** (typo-safe). Paper P&L counts toward the halt; pattern memory learns from paper trades. |
| `CANDLE_SOURCE` | `spot` (default) / `futures` | price feed for signals + OBI order book. Bot trades futures but has always read the spot (`B-`) feed. **futures replayed worse in both halves — not adopted.** |
| `SIMPLE_MODE` | on (default) / off | |
| `LIQMAP_MODE`, `POOL_*_MODE`, `BREAKOUT_MODE`, `OBI_EXIT_MODE` | | see code |

---

## 6. Evidence (replay over 8 Oct 2025 – 4 Nov 2026, 13 months)

Method: `scripts/replay/` — real strategy code on CoinDCX 15m candles (validated candle-for-candle
against the 5 Nov live log), spot signals with fills/exits on futures prices, fees 0.059%/side,
no slippage. Halves split 28 Apr 2026.

| Finding | Result |
|---|---|
| **Deployed system (main + Liquidity Map)** | 587 trades, **−₹11,179** (halves −₹12,242 / +₹1,063). **No edge.** |
| Main strategy only | 464 trades, −₹2,867 (−₹7,513 / +₹4,646). Before fees ≈ +0.05R/trade; fees ≈ 0.10R/trade (1R ≈ ₹500) |
| Liquidity Map only | 165 trades, −₹10,285 (−₹7,636 / −₹2,649) — negative even before fees. The old +₹9,065 (22 trades) did not hold |
| Signals on futures candles | main −₹30,252 (−₹17,813 / −₹12,439) — worse in both halves (noisier wicks) |
| Exit variants: TP 1R / 2R / 3R, no breakeven, TP 2R + no BE | none positive in both halves |
| Sweep minimum 0% / 0.5% (vs 0.2%) | both worse |
| Liquidity Map depth 0.5% / 1.0% | 66 / 8 trades, still negative |
| Older findings (10-week Railway-log replays) | simple mode > complex exits; breakout guard kept; breakout strategies and "trade winning patterns" rejected; BTC-regime early exit rejected |

---

## 7. Fixed on 5 Nov 2026 (each with a test that fails on the old code)

Failed positions call read as "nothing open" (dropped live trades, fake P&L) · failed market close
forgot the position (now retried) · second order on a coin already in a trade after the 05:30 reset ·
restart re-processed the last candle (early entry) · older candles processed twice · open trades
unmanaged when levels failed / paused · restart across 05:30 dropped positions · Telegram polling had
no backoff · breakeven not retried on the exchange · order-create timeout treated as reject (now
checks the exchange and adopts a real fill) · "Setup Detected" alert delayed the order · exactly ₹800
went half size · `/trades@Bot` didn't match · today's-trade flag saved ~1 min late · last closed
candle picked by position instead of time · `/status` uptime started at first `/status`.

---

## 8. Open items

1. **No profitable strategy yet.** Stay in paper until something passes the replay in both halves.
   The owner has a plan to discuss (expected to cover new strategy work).
2. **P&L uses planned prices, not fills** — the daily halt doesn't see slippage (owner: OK for now).
3. **SORB-15** (separate session-breakout bot, separate repo; BTC/Oil/NatGas) runs on the same Droplet
   as user `sorb15` from `/home/sorb15/sorb15-bot`:
   - `sorb15.service` = **paper** (env `.env`) + `sorb15-watchdog.timer` — **running**.
   - `sorb15-live.service` = **LIVE real money** (env `.env.live`) — was running until 5 Nov 23:3x IST;
     the owner had it **stopped**: disabled, its watchdog timer disabled, and locked off by
     `/etc/systemd/system/sorb15-live.service.d/off-switch.conf`
     (`ConditionPathExists=!/etc/sorb15-live.disabled`). `mask` is impossible (unit file sits in
     /etc/systemd/system). Re-enable ONLY on the owner's request:
     `rm /etc/sorb15-live.disabled && systemctl enable --now sorb15-live.service sorb15-live-watchdog.timer`.
   - Paper and live share ONE Telegram token (…hLcE, not the liquidity bot's) — 0 409s so far. Give
     paper its own @BotFather bot before ever running both again. Logs showed no live positions/orders
     in the 3 days before it was stopped.
   - Watchdogs were healthy (no restart loop); the earlier "no next run" was just a freshly enabled timer.
4. Volume vs delta mismatch on ZAMA/RIF/AERO (uninvestigated); order-book heatmap (proposed).
5. On 5 Nov 22:15 IST, before paper mode, live SOL and XRP longs were rejected by CoinDCX with
   "Insufficient funds" (as was KAITO at 17:00) — nothing filled.

---

## 9. Rules for making changes

- Behavioural changes: behind an **env switch** where sensible, **with tests**, replayed first.
  New data features start **log-only**.
- Keep `pyflakes core/ main.py exchange/ utils/ notifications/` clean. Explicit imports only.
- `tests/run_tests.py` must pass — the server won't deploy otherwise. Each change gets a test that
  **fails on the old code**.
- Don't edit the server's files by hand. Code goes through GitHub → auto-deploy. Server-only changes
  (env file, `/usr/local/bin` scripts) go to the owner as Web Console commands.
