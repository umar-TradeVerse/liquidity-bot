# Liquidity Strategy Bot — Delta Exchange

Automated crypto futures trading bot based on a custom PDH/PDL liquidity strategy.

---

## Strategy Summary

**Every day at 5:30 AM IST:**
- Fetches previous day's High (PDH) and Low (PDL) for all symbols

**Throughout the day (1m candles):**

### Scenario 1 — Sweep Reversal (Primary Edge)
| Direction | Condition | Entry |
|-----------|-----------|-------|
| SHORT | Price closes above PDH → rejection candle forms → next candle closes below rejection candle low | SELL at close |
| LONG | Price closes below PDL → rejection candle forms → next candle closes above rejection candle high | BUY at close |

**Stop Loss:** Above rejection candle high (SHORT) / Below rejection candle low (LONG) + 0.1% buffer

### Scenario 2 — Breakout (Clean break, no rejection)
| Direction | Condition | Entry |
|-----------|-----------|-------|
| LONG | Price closes above PDH, no rejection, next candle also closes above PDH | BUY |
| SHORT | Price closes below PDL, no rejection, next candle also closes below PDL | SELL |

**Stop Loss:** Just below PDH (LONG) / Just above PDL (SHORT) + 0.1% buffer

### Scenario 3 — Inside Day
- Price stays within PDH/PDL all day → **No trade**

---

## Symbols
- ETHUSD, SOLUSD, XRPUSD, TAOUSD, ICPUSD (Delta Exchange perpetual futures)

## Rules
- Max **2 trades per day** (resets at 5:30 AM IST)
- If symbol already triggered one scenario, ignore second signal for that symbol
- Failed orders do **not** count toward daily limit
- TP is **manual** — bot only handles entry + SL

---

## Project Structure

```
liquidity-bot/
├── main.py                  # Entry point + scheduler
├── core/
│   ├── state.py             # Bot state, daily counters, levels
│   ├── strategy.py          # Signal detection logic
│   ├── monitor.py           # 1m candle polling loop
│   └── patterns.py          # Candlestick pattern detection
├── exchange/
│   └── delta.py             # Delta Exchange API client
├── notifications/
│   └── telegram.py          # Telegram alerts
├── utils/
│   └── logger.py            # Rotating file logger
├── logs/                    # Auto-created
├── requirements.txt
├── .env.example
├── railway.toml
├── Procfile
└── .gitignore
```

---

## Setup

### 1. Clone & install
```bash
git clone https://github.com/YOUR_USERNAME/liquidity-bot.git
cd liquidity-bot
pip install -r requirements.txt
```

### 2. Configure environment
```bash
cp .env.example .env
# Edit .env with your credentials
```

Required variables:
```
DELTA_API_KEY=        # From Delta Exchange → Account → API
DELTA_API_SECRET=     # From Delta Exchange → Account → API
TELEGRAM_BOT_TOKEN=   # From @BotFather on Telegram
TELEGRAM_CHAT_ID=     # From @userinfobot on Telegram
TRADE_SIZE_USD=100    # USD per trade (position = this × 5x leverage)
```

### 3. Run locally
```bash
python main.py
```

---

## GitHub Setup

```bash
git init
git add .
git commit -m "Initial commit — liquidity bot"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/liquidity-bot.git
git push -u origin main
```

---

## Railway Deployment (24/7)

1. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub
2. Select your `liquidity-bot` repository
3. Go to **Variables** tab and add all `.env` values:
   - `DELTA_API_KEY`
   - `DELTA_API_SECRET`
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
   - `TRADE_SIZE_USD`
4. Railway will auto-detect `Procfile` and start `python main.py`
5. Monitor logs in Railway dashboard

**Important:** Set Railway region to **EU West** or **US East** for lowest latency to Delta Exchange.

---

## Delta Exchange API Setup

1. Login to [delta.exchange](https://www.delta.exchange)
2. Go to Account → API Keys → Create New Key
3. Permissions needed: **Read + Trade** (do NOT enable withdrawal)
4. Copy API Key and Secret to your `.env`

---

## Telegram Bot Setup

1. Open Telegram → search `@BotFather`
2. Send `/newbot` → follow instructions → copy the token
3. Open `@userinfobot` → send any message → copy your Chat ID
4. Add both to `.env`

---

## Candlestick Patterns Used

| Pattern | Type | Detection Rule |
|---------|------|----------------|
| Bearish Pin Bar | Rejection after PDH sweep | Upper wick ≥ 2× body, body in lower 40% of range |
| Bullish Pin Bar | Rejection after PDL sweep | Lower wick ≥ 2× body, body in upper 40% of range |
| Doji (Bearish) | Rejection after PDH sweep | Body ≤ 10% of range, upper wick dominant |
| Doji (Bullish) | Rejection after PDL sweep | Body ≤ 10% of range, lower wick dominant |
| Bearish Engulfing | Rejection after PDH sweep | Current bearish body fully engulfs previous body |
| Bullish Engulfing | Rejection after PDL sweep | Current bullish body fully engulfs previous body |

---

## Risk Management

- **Leverage:** Fixed 5x
- **SL Buffer:** 0.1% beyond rejection candle high/low
- **Position size:** `TRADE_SIZE_USD × 5 / entry_price` contracts
- **Max daily loss:** 2 trades × your defined trade size
- Note: Strategy may stop out 2-3 times before the real move (tight SL by design)

---

## Telegram Alerts Reference

| Event | Alert |
|-------|-------|
| Bot started | 🤖 Bot started |
| Daily levels set | ✅ PDH/PDL for all symbols |
| PDH/PDL fetch failed | ⚠️ CRITICAL alert |
| Setup detected | 🔍 Symbol, side, pattern, entry, SL |
| Trade executed | ✅ Full trade details + order ID |
| Trade skipped (max limit) | ⏭️ Skipped setup details |
| Order failed | ❌ Error details |
| Manual intervention needed | ⚠️ Action required |

---

## Edge Cases Handled

| Scenario | Handling |
|----------|----------|
| PDH/PDL fetch fails | Retry once → Telegram alert → bot paused for day |
| Order placement fails | Alert sent, NOT counted toward daily limit |
| SL order fails after entry | Retry once → manual intervention alert |
| Bot restarts after 5:30 AM | Immediately fetches today's levels |
| Same symbol fires twice | Second signal ignored (scenario_fired flag) |
| More than 2 signals in a day | 3rd+ skipped with Telegram notification |
| Rejection candle invalidated | Cleared if price moves >0.5% away |
