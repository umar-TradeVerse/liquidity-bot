"""Shared imports, constants and helpers for the monitor and its agents.

Split out of core/monitor.py on 2026-10-02 (no logic changes). Every name
here is re-exported, so code that imports from core.monitor keeps working."""
"""
MarketMonitor — continuously polls 15-minute candles and routes signals to execution.
Runs all 7 days a week, from 5:30 AM to 11:00 PM IST.

Exit hierarchy for open positions, evaluated in strict priority order on
every closed candle — first true condition wins:
  1. Target reached (PDH for LONG / PDL for SHORT) -> full close.
     SKIPPED for trend-mode (flip) trades — see note below.
  2. Rejection candle within REJECTION_PROXIMITY_PCT of target, AND the
     close is actually favorable versus entry -> full close. The
     profitability check was added after a real case (XRPUSD,
     2026-07-19) where this fired as a "Take Profit" while the position
     was actually underwater — the old logic only checked proximity and
     candle shape, never whether the trade was in profit at all.
     Also skipped for trend-mode trades (depends on the same target).
  3. ROE (CoinDCX-reported) >= ROE_TARGET_PCT -> full close.

Trend-mode trades (the sell-side/buy-side liquidity flip, see strategy.py)
deliberately skip priorities 1 and 2: their entry often already sits past
the fixed daily PDH/PDL by design, so a fixed-target check would trigger
a bogus near-immediate exit. These trades run on stop-loss + ROE only.

Rule 3 — one automatic trade per symbol per day: once a symbol has had
one auto-placed trade today, any further clean setup on that symbol is
alert-only, never auto-placed, regardless of how that first trade closes.

Counter-trend signals (fighting today's trend_bias) are always alert-only,
regardless of Rule 3.

BTC-regime is informational only, logged and surfaced in alerts, no longer
gates execution.

Margin cap: CoinDCX's futures wallet-balance endpoint has been confirmed
broken (404) since 2026-07-17, so the insufficient-funds pre-check no
longer depends on it. Instead the bot tracks its own committed margin
(positions already open x margin per trade) against an optional,
manually-configured TOTAL_ACCOUNT_MARGIN_USD env var. Unset or 0 disables
the check entirely.

Concurrency: all symbols are polled and processed concurrently via
asyncio.gather every cycle. Checking-then-reserving a MAX_CONCURRENT_POSITIONS
slot is guarded by self._position_lock so two symbols signaling in the
same poll cycle can't both slip past the cap before either registers.
"""

import asyncio
from datetime import datetime, time as dtime
from typing import Optional
import pytz
import os

from core.state import BotState, SYMBOLS, REGIME_SYMBOL, TradeRecord
from core.strategy import StrategyEngine, Signal, ENTRY_EXPIRY_MINUTES
from exchange.coindcx import CoinDCXClient
from notifications.telegram import TelegramBot
from core import persistence
from core.obi import compute_obi, summarise_book
from core.agents import context_agent
from core.agents.pattern_agent import PatternAgent, build_features
from core.agents.pool_agent import PoolAgent
from core.agents.liquidity_map_agent import LiquidityMapAgent
from core.agents.decision import DecisionRecord, Verdict
from core.agents.risk_agent import RiskAgent, DAILY_LOSS_LIMIT_USD, DAILY_LOSS_LIMIT_INR
from utils.logger import setup_logger
logger = setup_logger("monitor")
IST = pytz.timezone("Asia/Kolkata")

POLL_INTERVAL_SECONDS = 15
DAY_END_HOUR = 23
# 2026-10-02: in-trade OBI exit (see _obi_watch). Railway: OBI_EXIT_MODE=off disables
# the exit but keeps the logging.
OBI_EXIT_MODE = os.getenv('OBI_EXIT_MODE', 'live').strip().lower()
# Escalation rule (2026-10-02, designed by Umar): exit only when order-book
# pressure AGAINST the trade rises on OBI_RISE_CANDLES consecutive candles
# while the book is actually against the trade (pressure > OBI_EXIT_LEVEL,
# default 0 = no fixed level -- the sustained rise IS the signal).
# Any drop = pullback -> continue, count restarts. Applies to LONG and SHORT.
OBI_RISE_CANDLES = int(os.getenv('OBI_RISE_CANDLES', '4'))
OBI_EXIT_LEVEL = float(os.getenv('OBI_EXIT_LEVEL', '0'))
# Early-failure exit (first 3 candles): kept ON by default -- replay evidence
# 2026-10-02: removing it cost Rs1,317 over 10 weeks, worse in both halves.
EARLY_FAILURE_MODE = os.getenv('EARLY_FAILURE_MODE', 'on').strip().lower()
DAY_END_MINUTE = 0

TRADE_LEVERAGE = 10  # 2026-09-08: reverted from 5x back to 10x per explicit
                      # request. Same margin ($90/trade) at double leverage
                      # means notional exposure and dollar risk both roughly
                      # double vs the 5x setup that's been running since
                      # 24-Aug -- worth knowing going in, same tradeoff
                      # flagged when 5x was first adopted, just reversed.
MAX_CONCURRENT_POSITIONS = 2

# 2026-09-14: INR-denominated risk-tier gate, per explicit request. Sizes
# each trade by how much it would actually lose at SL, in INR, rather than
# by a fixed %-distance rule.
#
#   risk < FULL_SIZE_MAX      -> full size (unchanged)
#   FULL_SIZE_MAX <= risk <= STAGED_MAX -> staged entry, 50% initial
#                                (reuses the existing staged-entry mechanism
#                                and its +1R confirmation-based addition)
#   risk > STAGED_MAX         -> do NOT trade. Logged only, same as every
#                                other rejection reason, so it's auditable.
#
# This replaces the pure %-distance staging trigger for the purpose of
# deciding SIZE. A flat, single-threshold $ cap (no staged middle tier) was
# tested earlier and explicitly rejected -- it either passed a trade at
# full size or blocked it outright with nothing in between, and blocked
# ~63% of all setups in backtesting. This tiered version keeps a genuine
# full-size lane for tight-SL trades and a reduced lane for medium risk,
# only fully skipping the setups whose SL risk is genuinely large.
#
# USD_TO_INR_RATE is a fixed approximation, not a live FX feed -- consistent
# with how this bot avoids external dependencies unless proven necessary.
# Revisit this number if USDT/INR drifts meaningfully from it.
SL_RISK_FULL_SIZE_MAX_INR = 800
SL_RISK_STAGED_MAX_INR = 1300
from core.agents.risk_agent import USD_TO_INR_RATE  # single source (CoinDCX rate, Railway USD_INR_RATE)

# Partial take-profit ladder (added per Umar's request after the 7-day MFE
# analysis showed avg MFE ~0.97R vs avg realized ~0.17R). Applies ONLY to
# non-trend-mode trades — trend-mode (flip) trades keep the existing
# SL + ROE-only behavior unchanged, since a fixed-target ladder doesn't fit
# an entry that's already past the daily PDH/PDL by design.
# TP3 is capped at the original single-target level (PDH/PDL) — it is NOT
# a new, more distant target. TP1/TP2 are interior partials closer than
# that existing target. If TP1's R-multiple would sit beyond TP3, the
# ladder silently collapses to the old single-TP behavior for that trade
# (no partial tiers, same as before this change).
TP_LADDER_R = (1.5, 2.5)  # TP1, TP2 — TP3 is always the original PDH/PDL target
TP_LADDER_WEIGHTS = (0.34, 0.33, 0.33)  # TP1, TP2, TP3 — must sum to 1.0
MIN_TRADES_FOR_HISTORICAL_STATS = 5  # below this, message shows "not enough
                                      # data yet" rather than a misleadingly
                                      # precise win rate from a tiny sample

# Breakeven stop-move — added after the 7-day loss analysis showed 6 of 9
# losing trades had moved 0.3R-0.8R in their favor before reversing all the
# way to full stop-loss. Triggered on raw MFE reaching an R-multiple
# (checked directly against candle highs/lows), NOT tied to TP1 filling —
# that keeps it independent of the TP ladder so it also protects trend-mode
# trades (which skip the ladder and TP1/TP2 entirely) and doesn't depend on
# a partial-close order having succeeded first.
#
# Went through two single-threshold versions before landing on a staged
# ratchet (2026-07-29):
#   0.5R (original) — ETHUSD moved to exact breakeven, then got clipped by
#     a small reversal wick right before the original move resumed hard.
#   1.0R (the fix for the above, 2026-07-27) — KAITOUSD then moved only
#     ~0.47R and reversed to a FULL loss, since 1.0R never triggered at all.
# There's no single number that avoids both failure modes — it's a genuine
# structural tradeoff, not a tuning problem. A staged ratchet reduces the
# SEVERITY of the failure instead: partial protection kicks in earlier (so
# a KAITOUSD-style reversal takes a smaller loss, not the full one), while
# full breakeven still requires a more convincing move (so an ETHUSD-style
# small wick is less likely to clip it).
BREAKEVEN_STAGE1_R = 0.4   # partial: cut remaining risk roughly in half
BREAKEVEN_STAGE2_R = 1.0   # full: move SL all the way to entry

# 2026-08-24: early-exit rules built from 7-day log analysis (Aug 16-23).
# EARLY_INVALIDATION: if a trade hasn't reached this R-multiple favorable
# within EARLY_INVALIDATION_CANDLES of entry, close it. Evidence: both
# catastrophic losses this week (RIFUSD -17%, SOLUSD -22.5%) peaked at
# only 0.07R and 0.19R and never recovered. Every winner this week reached
# 0.43R+ within 4 candles. Safe window confirmed: 0.19R (highest disaster)
# to 0.43R (lowest winner) -- 0.25R sits comfortably in the middle.
# Verified against all 3 winners: none cut. Verified against all disasters:
# both caught. Time window: all winners cleared in 4 candles max; giving 6
# (50% more) as breathing room for slower-starting setups.
#
# 2026-09-14: EARLY_INVALIDATION_R/CANDLES REMOVED entirely, per explicit
# request. That whole R-based cutoff mechanism (kept above in history for
# the record of what it was and why it changed over time) is replaced by
# EARLY_FAILURE_MAX_CANDLES + DOJI_BODY_RATIO_MAX below -- a pattern-based
# early exit instead of an R-multiple timer. See _check_early_failure_exit.
EARLY_FAILURE_MAX_CANDLES = 3   # 45 min at 15m candles. Past this, no
                                # further check of this kind applies -- the
                                # trade continues under normal SL/breakeven/
                                # trend_trail/TP-ladder management only.
DOJI_BODY_RATIO_MAX = 0.15      # body <= 15% of the candle's own high-low
                                # range counts as a doji for the "doji
                                # followed by weakness" condition. A doji
                                # alone never exits -- only if the NEXT
                                # candle continues adverse past its close.


# 2026-09-02: zone_reversal removed, replaced by TREND_TRAIL below. Backtest
# against all 3 real firings (ZAMAUSD, TAOUSD, RIFUSD) showed zone_reversal
# closed every trade correctly-but-early: 0.56R/0.42R/0.66R actual vs
# 1.85R/1.80R/0.66R available. A 0.4R-1.0R rejection-candle trigger was too
# early -- ordinary noise, not real reversals. Simulating a trail instead
# (activate at 1.0R, 0.3R behind the high-water mark) beat the actual
# result on ALL THREE cases simultaneously (+1.69R/+0.92R/+0.80R vs
# +0.56R/+0.42R/+0.66R, $63.23 vs $16.00 total) -- including the one case
# where zone_reversal had fired correctly. Activating at 1.0R deliberately
# matches BREAKEVEN_STAGE2_R: below that, the existing ratchet already
# protects the trade; this only takes over once full breakeven has
# already fired, extending it into a moving stop instead of a frozen one.
TREND_TRAIL_ACTIVATE_R = 1.0
TREND_TRAIL_BUFFER_R = 0.3
STABILITY_MAX_COUNTER_CONFIRMS = 2  # Trend Stability — after this many
                                     # counter-trend confirmations on a
                                     # symbol today, the day's trend
                                     # classification is treated as no
                                     # longer reliable, and BOTH sides go
                                     # alert-only for the rest of the day,
                                     # not just the counter-trend one.

REJECTION_PROXIMITY_PCT = 0.01  # 1.0%
ROE_TARGET_PCT = 7.0
# 2026-08-20 Fix 1: how far behind the best price the trailing stop sits
# for the remainder after an ROE-protection partial close, expressed as a
# fraction of the ORIGINAL entry-to-SL risk. 0.5 means it trails half an
# R-multiple behind the running high-water mark, ratcheting forward only.
TRAILING_STOP_R_BUFFER = 0.5
MIN_ROE_FOR_REJECTION_EXIT_PCT = 3.0  # JUDGMENT CALL, unvalidated — half of
                                       # ROE_TARGET_PCT. Priority 2 (rejection
                                       # exit) now requires the position to
                                       # already be at this much profit before
                                       # it's allowed to fire — added after a
                                       # real case (ETHUSD, 2026-07-20) where
                                       # a marginal-profit rejection candle
                                       # closed the trade just before a much
                                       # larger continuation move. This keeps
                                       # the reversal-protection intent while
                                       # requiring more conviction than "any
                                       # profit at all" before bailing early.


def _escape_md(text) -> str:
    text = str(text)
    for ch in ('_', '*', '`', '['):
        text = text.replace(ch, '\\' + ch)
    return text


def _close_dt_ist(fill_ms):
    """Actual close time from an exchange fill timestamp (ms), else now."""
    try:
        if fill_ms:
            return datetime.fromtimestamp(float(fill_ms) / 1000, IST)
    except Exception:
        pass
    return datetime.now(IST)


def _close_time_ist(fill_ms) -> str:
    return _close_dt_ist(fill_ms).isoformat()


def _is_bearish_rejection(candle: dict) -> bool:
    body = abs(candle['close'] - candle['open'])
    upper_wick = candle['high'] - max(candle['open'], candle['close'])
    lower_wick = min(candle['open'], candle['close']) - candle['low']
    if candle['close'] >= candle['open'] or body == 0:
        return False
    return upper_wick >= 2 * body and upper_wick >= 2 * lower_wick


def _is_bullish_rejection(candle: dict) -> bool:
    body = abs(candle['close'] - candle['open'])
    upper_wick = candle['high'] - max(candle['open'], candle['close'])
    lower_wick = min(candle['open'], candle['close']) - candle['low']
    if candle['close'] <= candle['open'] or body == 0:
        return False
    return lower_wick >= 2 * body and lower_wick >= 2 * upper_wick


def _is_bearish_engulfing(prev_candle: dict, candle: dict) -> bool:
    if not (prev_candle['close'] > prev_candle['open'] and candle['close'] < candle['open']):
        return False
    return candle['open'] >= prev_candle['close'] and candle['close'] <= prev_candle['open']


def _is_bullish_engulfing(prev_candle: dict, candle: dict) -> bool:
    if not (prev_candle['close'] < prev_candle['open'] and candle['close'] > candle['open']):
        return False
    return candle['open'] <= prev_candle['close'] and candle['close'] >= prev_candle['open']


# Everything above is shared with monitor.py, execution.py and trade_manager.py.
__all__ = [
    "BREAKEVEN_STAGE1_R",
    "BREAKEVEN_STAGE2_R",
    "BotState",
    "CoinDCXClient",
    "DAILY_LOSS_LIMIT_INR",
    "DAILY_LOSS_LIMIT_USD",
    "DAY_END_HOUR",
    "DAY_END_MINUTE",
    "DOJI_BODY_RATIO_MAX",
    "DecisionRecord",
    "EARLY_FAILURE_MAX_CANDLES",
    "EARLY_FAILURE_MODE",
    "ENTRY_EXPIRY_MINUTES",
    "IST",
    "LiquidityMapAgent",
    "MAX_CONCURRENT_POSITIONS",
    "MIN_ROE_FOR_REJECTION_EXIT_PCT",
    "MIN_TRADES_FOR_HISTORICAL_STATS",
    "OBI_EXIT_LEVEL",
    "OBI_EXIT_MODE",
    "OBI_RISE_CANDLES",
    "Optional",
    "POLL_INTERVAL_SECONDS",
    "PatternAgent",
    "PoolAgent",
    "REGIME_SYMBOL",
    "REJECTION_PROXIMITY_PCT",
    "ROE_TARGET_PCT",
    "RiskAgent",
    "SL_RISK_FULL_SIZE_MAX_INR",
    "SL_RISK_STAGED_MAX_INR",
    "STABILITY_MAX_COUNTER_CONFIRMS",
    "SYMBOLS",
    "Signal",
    "StrategyEngine",
    "TP_LADDER_R",
    "TP_LADDER_WEIGHTS",
    "TRADE_LEVERAGE",
    "TRAILING_STOP_R_BUFFER",
    "TREND_TRAIL_ACTIVATE_R",
    "TREND_TRAIL_BUFFER_R",
    "TelegramBot",
    "TradeRecord",
    "USD_TO_INR_RATE",
    "Verdict",
    "_close_dt_ist",
    "_close_time_ist",
    "_escape_md",
    "_is_bearish_engulfing",
    "_is_bearish_rejection",
    "_is_bullish_engulfing",
    "_is_bullish_rejection",
    "asyncio",
    "build_features",
    "compute_obi",
    "context_agent",
    "datetime",
    "dtime",
    "logger",
    "os",
    "persistence",
    "pytz",
    "setup_logger",
    "summarise_book",
]
