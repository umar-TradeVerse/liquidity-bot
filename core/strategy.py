"""
StrategyEngine — Liquidity sweep + trigger-candle reversal strategy, with
three-way trend classification layered on top.

BASE MECHANICS, applied against "effective" levels that are either the
fixed daily PDH/PDL (trend_bias == NONE) or a dynamically re-anchored
reference (trend_bias set):

  1. Liquidity Sweep — a candle's low/high breaks new ground past the
     effective level and past today's deepest prior sweep on that side,
     clearing MIN_SWEEP_DEPTH_PCT. This gate is now enforced identically
     whether the effective level is the fixed daily PDH/PDL OR a
     trend-flip's dynamic reference — see the 2026-07-26 fix note below.
  2. Trigger Candle — the first opposite-colour candle after the sweep,
     UNLESS the sweep candle itself already shows a rejection shape
     (wick >= REJECTION_WICK_RATIO * body, and its own close already
     back on the correct side of the effective level) — in that case
     the sweep candle becomes the trigger immediately, no waiting.
  3. Entry — close breaks the trigger's high/low AND that candle is
     itself the confirming colour AND (for the ORIGINAL, non-flip
     sweep only) the close is back on the correct side of the FIXED
     daily PDH/PDL by at least MIN_RECLAIM_MARGIN_PCT — OR, for a
     "deep" sweep (see CISD section below), reclaims the CISD reference
     instead. This reclaim check is a no-op for trend-flip trades
     (price is already well past the fixed level by definition of
     being in a trend) while remaining a real filter for genuine
     sideways-market reversals.
  4. Stop Loss — beyond the sweep extreme for this cycle, buffered by
     SL_BUFFER_PCT.
  5. Invalidation — close breaks back through the trigger's other side
     first — scrapped, re-arms immediately.

  If price never breaks PDH or PDL at all — stays inside the previous
  day's range — no sweep ever arms on either side, and no trade is
  taken. This is inherent to the mechanics above, not a separate rule.

FRESH-SWEEP QUALITY GATES (apply to every NONE->SWEPT transition,
including a trend-flip's seeded second leg as of the 2026-07-26 fix):
  - MIN_SWEEP_DEPTH_PCT — the new extreme must clear the prior deepest
    sweep (or, for the very first sweep of a cycle, the effective
    level itself) by at least this fraction, not just by any tiny
    amount.
  - Inside-bar skip — a candle that's fully inside the previous
    candle's range never arms a fresh sweep (low conviction).

TREND-FLIP MECHANIC (validated against 2026-07-17 real trades — ETH,
SOL, XRP all traced to ~2.8-3R winners using this exact logic):

  trend_bias is classified once per symbol at the daily reset, from
  the last 3 COMPLETE daily candles (today's in-progress candle
  excluded). Each of the 3 days is classified UP / DOWN / SIDEWAYS by
  the same decisive-body-ratio test (TREND_BODY_RATIO_THRESHOLD).

  If the most recent complete day (day-1) is decisively UP or DOWN,
  AND the day before it (day-2) was the SAME direction — i.e. 2+
  consecutive same-direction trend days — the move is treated as
  likely maturing/exhausted, and trend_bias is set to NONE: the
  original dual-sided sweep-reversal logic handles today.

  If day-1 is decisive and day-2 differs (or isn't decisive itself),
  this is a single FRESH trend day.

  *** REMOVED (2026-08-20) ***: this used to seed a dynamic
  "trend_ref_high"/"trend_ref_low" re-anchored reference from a
  counter-trend bounce, which effective_pdh/effective_pdl would then
  hunt instead of the fixed daily PDH/PDL — the "trend-aligned flip"
  mechanism. Removed entirely after a real trade's rejection alert
  showed "Level swept: 196.00", far from the fixed PDH (201.42),
  causing confusion about what was actually being hunted. Since flip
  trades had already been alert-only (never auto-traded) since
  2026-07-29, this removal has zero impact on any auto-executed trade
  — SHORT signals now ONLY come from a genuine fixed-PDH sweep, LONG
  only from a genuine fixed-PDL sweep. effective_pdh/effective_pdl now
  always just equal the fixed pdh/pdl. The DailyLevel dataclass still
  has trend_ref_high/trend_ref_low fields (state.py) for persistence
  compatibility with old saved snapshots, but nothing sets them anymore.

CISD HYBRID (2026-07-22): for a "deep" sweep (sweep extreme sits at
least DEEP_SWEEP_THRESHOLD_PCT beyond the fixed PDH/PDL), the reclaim
check uses the CISD reference (the open of the candle immediately
before the trigger) instead of demanding a full reclaim of the fixed
level, which real cases (ICP, KAITO) showed could cost many hours of
delay on an otherwise valid entry. Shallow sweeps are unaffected and
still use the fixed-level reclaim + margin, unchanged — this is the
exact case that originally caught KAITO's very first bad SHORT entry
weeks ago (2.5% on the wrong side of the level). USE_CISD_FOR_DEEP_SWEEPS
is a single-line standby switch to fully revert to the old fixed-level
reclaim everywhere if needed, no other code changes required.

No indicators, no pattern matching beyond what's described above. No
breakout logic of any kind — this engine only ever trades liquidity
sweeps, on either the fixed daily level or a trend-re-anchored one.
"""

import logging
from typing import Optional
from core.state import BotState, SYMBOLS, REGIME_SYMBOL
from exchange.coindcx import CoinDCXClient
from utils.logger import setup_logger
logger = setup_logger("strategy")

SL_BUFFER_PCT = 0.01                # 1.0% buffer beyond the sweep extreme --
                                     # widened from 0.2% on 2026-08-20. Real
                                     # trade evidence (KAITOUSD, Aug 16-19)
                                     # showed SL sitting almost exactly at
                                     # the sweep wick was too tight -- a
                                     # shallow re-test of the same wick could
                                     # clip the stop before the real reversal
                                     # played out. This is an explicit,
                                     # deliberate number, not derived from a
                                     # backtested structural rule -- an
                                     # earlier attempt to find a clean,
                                     # non-percentage "structural" reference
                                     # (e.g. the pre-sweep candle) didn't
                                     # hold up against the real data checked.
MIN_SWEEP_DEPTH_PCT = 0.002          # UNVALIDATED placeholder — 0.2%
MIN_RECLAIM_MARGIN_PCT = 0.0015       # 0.15%, confirmed by user 2026-07-21 —
                                       # the reclaim check (close must be back
                                       # on the correct side of the fixed
                                       # PDH/PDL) now requires clearing it by
                                       # this margin, not just technically
                                       # passing. Real cases supporting this:
                                       # XRP (0.06% margin, loss), SOL (0.026%
                                       # margin, loss), LTC (0.17% margin, SL
                                       # hit) — all thin reclaims followed by
                                       # a stop-out.
REJECTION_WICK_RATIO = 2.0           # wick must be >= 2x body to fast-path the trigger
USE_CISD_FOR_DEEP_SWEEPS = True      # STANDBY SWITCH — flip to False to fully
                                       # revert to the fixed-level reclaim check
                                       # for every trade, no code changes needed
                                       # beyond this one line.
DEEP_SWEEP_THRESHOLD_PCT = 0.005      # UNVALIDATED placeholder — 0.5%. A sweep
                                       # whose extreme sits at least this far
                                       # beyond the fixed PDH/PDL is treated as
                                       # "deep" and uses CISD (reclaim the open
                                       # of the last opposite-colour candle
                                       # before the trigger) instead of the
                                       # fixed-level reclaim. Shallow sweeps
                                       # (below this threshold) keep the
                                       # existing fixed-level reclaim + margin
                                       # check unchanged — this is the exact
                                       # case that originally caught KAITO's
                                       # bad SHORT (entry 2.5% on the wrong
                                       # side of the level). Real cases behind
                                       # the deep-sweep side: ICP (sweep ~1%
                                       # past fixed PDL, CISD would have fired
                                       # ~8h earlier) and KAITO (sweep ~1.5%
                                       # past fixed PDL, CISD would have fired
                                       # ~1.5h earlier) — both 2026-07-22.
TREND_BODY_RATIO_THRESHOLD = 0.5     # UNVALIDATED placeholder — daily body must
                                     # cover >= 50% of the day's full range to
                                     # count as a decisive trend day
TREND_LOOKBACK_DAYS = 3              # how many complete daily candles to fetch
                                     # for classification (day-1/day-2 are the
                                     # ones actually used right now; day-3 is
                                     # fetched and available for future tuning)

# ══════════════════════════════════════════════════════════════════════════
# HARD RULES added 2026-08-13, following the 7-day no-funds audit. Each was
# validated against real historical setups before being added — see the
# audit conversation for the full impact tables. These are quality gates on
# an otherwise-confirmed entry, not new detection logic — they reject a
# setup that already passed every existing check, rather than changing how
# sweeps/triggers/reclaims are detected.
# ══════════════════════════════════════════════════════════════════════════

# Rule 1 — Maximum Stop Loss Distance. If the calculated SL is more than
# this far from entry, reject the trade outright (alert-only, no execution).
MAX_SL_DISTANCE_PCT = 0.03  # 3.0% — raised from 2.0% on 2026-08-27. The
                             # 2026-08-20 SL_BUFFER_PCT change (0.2% -> 1.0%)
                             # shifted the whole SL-distance distribution
                             # right, so the old 2.0% trigger was staging 97%
                             # of signals (32/33) instead of the ~30% it was
                             # designed for. Replay of all 45 R:R-passing
                             # historical trades at current sizing: 2.0% ->
                             # +$12.48, 3.0% -> +$24.03, staging 6/45 rather
                             # than 21/45. NOTE: the threshold sweep was noisy
                             # (3.25% -> +$29, 3.5% -> +$10), so 3.0% is chosen
                             # as a defensible round number in a reasonable
                             # range, NOT as a fitted optimum. Do not read the
                             # +$24 figure as a forecast.

# 2026-08-27 Rule 11 — minimum reward:risk. Reward is measured to the
# opposite daily level (the actual target the strategy trades toward),
# risk is entry->SL. Evidence: across all 53 matched historical trades,
# 8 had R:R < 1.0 and ALL 8 lost (avg return -0.73%), while the 45 with
# R:R >= 1.0 won 55.6% with positive average return. Zero winners sat
# below 1.0. Applying this rule to history moves total P&L from -$11.16
# to +$12.48 with no winning trade sacrificed. 1.0 is both the
# mathematically obvious floor (reward must at least equal risk) and the
# exact point where every historical failure fell below and every
# success above. Not raised beyond 1.0 because 1.5 would have rejected
# TAOUSD 08-27 (1.44x), a real winner.
MIN_REWARD_RISK_RATIO = 1.0

# Rules 2 & 3 — Minimum/Maximum Liquidity Sweep Distance. A sweep must clear
# the swept level (fixed PDH/PDL, or the dynamic trend-flip reference —
# whichever is actually being hunted) by at least MIN, and by no more than
# MAX, to count as the kind of liquidity event this strategy targets.
# MIN lowered from the originally-proposed 2.0% to 1.0% after the impact
# table showed 2.0% would have rejected 82% of all real signals this week,
# 89% of which were winners — 1.0% still filters shallow/marginal sweeps
# (e.g. the KAITO case that tapped PDH by only 0.3% and lost) without
# gutting the strategy's actual operating range.
MIN_LIQUIDITY_SWEEP_PCT = 0.005  # 0.5% — loosened from 1.0% on 2026-08-15.
                                  # Evidence review showed 1.0% blocked 93%
                                  # winners among rejections in the 20-trade
                                  # verified set; 0.5% is a middle ground —
                                  # some floor still has value (e.g. the
                                  # ZAMAUSD 0.58%-sweep trade that went on to
                                  # hit SL), but 1.0% was too destructive.
MAX_LIQUIDITY_SWEEP_PCT = 0.10   # 10.0%

# Rule 4 — Entry Expiry. The confirming candle must close beyond the
# trigger's high/low within this many minutes of the ORIGINAL sweep candle
# that first armed this side — not from any earlier candle that merely
# approached the level. If this window elapses while still SWEPT/TRIGGERED,
# the setup expires and the side resets to NONE, requiring a completely
# fresh sweep (not just a fresh trigger on the same old sweep).
ENTRY_EXPIRY_MINUTES = 90

# ══════════════════════════════════════════════════════════════════════════
# INFORMATIONAL-ONLY drift tracker, added 2026-08-18. Does NOT change
# ENTRY_EXPIRY_MINUTES, does NOT re-arm or extend real trading state, does
# NOT auto-trade anything. Purely observes: after a setup expires, does
# price EVENTUALLY do what would have confirmed it, and if so, how far had
# it drifted from the sweep extreme by then? Early evidence (4 traced
# cases) suggested low drift (<~1.5%) correlated with the eventual move
# being real, while high drift (>~2.5%) correlated with it reversing --
# but that's from 4 data points, nowhere near proven. This tracker exists
# to accumulate more real cases before ever considering acting on it.
POST_EXPIRY_DRIFT_OBSERVATION_MINUTES = 180  # how long to keep watching
                                              # after expiry before giving up
                                              # on this specific observation


class Signal:
    def __init__(self, symbol, side, entry_price, sl_price, pdh, pdl,
                 counter_trend=False, trend_mode=False, swept_level=None,
                 reject_reason=None, use_staged_entry=False,
                 sweep_closed_inside=None):
        self.symbol = symbol
        self.side = side
        self.entry_price = entry_price
        self.sl_price = sl_price
        self.scenario = "sweep_reversal"
        self.pattern = "Liquidity Sweep"
        self.pdh = pdh
        self.pdl = pdl
        self.counter_trend = counter_trend  # True = fights today's bias, alert-only
        self.trend_mode = trend_mode        # True = this IS the trend-aligned flip
                                             # trade — exit hierarchy skips the
                                             # fixed-target priority for these
        # Set when one of the 2026-08-13 hard rules (sweep depth bounds,
        # ambiguous data) rejects an otherwise-confirmed entry. monitor.py
        # checks this FIRST, before counter-trend/stability routing — a
        # rejected setup is always alert-only, never auto-traded, and gets
        # its own distinct alert message naming which rule fired.
        self.reject_reason = reject_reason
        # 2026-08-15: True when SL distance exceeds MAX_SL_DISTANCE_PCT.
        # This no longer rejects the trade — monitor.py instead places only
        # 50% of planned margin initially, adding the remaining 50% only on
        # the same +1R/confirmation logic used elsewhere, never a blind
        # timer or percentage trigger alone.
        self.use_staged_entry = use_staged_entry
        # 2026-08-25 informational only. True = sweep candle closed back
        # inside the level (genuine hunt). False = closed beyond it
        # (breakout risk). None = unknown. Surfaced in alerts so the
        # hunt-vs-breakout split can be evaluated on real trades before
        # it is ever allowed to gate an entry.
        self.sweep_closed_inside = sweep_closed_inside
        # The level actually swept to produce this signal -- the FIXED
        # daily PDH/PDL. (Prior to 2026-08-20 this could also be a dynamic
        # re-anchored trend-flip reference; that mechanism was removed
        # entirely, so this is now always just the fixed pdh/pdl.)
        self.swept_level = swept_level if swept_level is not None else (
            pdh if side == 'SELL' else pdl
        )

    def __repr__(self):
        return (f"Signal({self.symbol} {self.side} @ {self.entry_price:.4f} "
                f"SL:{self.sl_price:.4f} [liquidity sweep])")


def _is_inside_bar(candle: dict, prev_candle: Optional[dict]) -> bool:
    if prev_candle is None:
        return False
    return candle['high'] <= prev_candle['high'] and candle['low'] >= prev_candle['low']


def _classify_day(c: dict) -> str:
    """UP / DOWN / SIDEWAYS for one completed daily candle."""
    body = abs(c['close'] - c['open'])
    day_range = c['high'] - c['low']
    ratio = (body / day_range) if day_range > 0 else 0
    if c['close'] < c['open'] and ratio >= TREND_BODY_RATIO_THRESHOLD:
        return "DOWN"
    elif c['close'] > c['open'] and ratio >= TREND_BODY_RATIO_THRESHOLD:
        return "UP"
    return "SIDEWAYS"


def _check_hard_rules(side: str, entry: float, sl: float, swept_level: float,
                       sweep_extreme: float, target: float = None) -> tuple:
    """Rules 2/3 (2026-08-13, sweep depth bounds) and Rule 8 (ambiguous ->
    don't trade) still hard-reject here. Rule 1 (SL distance) no longer
    rejects — changed 2026-08-15: a wide SL usually just means the
    structural sweep extreme sits further from entry, not that the setup
    itself is bad. Rejecting those outright was found to block real
    winners disproportionately (evidence: RIF's 3.79%-SL trade that ran to
    8.27R). Instead, SL > MAX_SL_DISTANCE_PCT now triggers a STAGED entry
    (50% now, remaining 50% only added on the same +1R/confirmation logic
    already used for breakeven-ratchet math) — this keeps dollar risk
    controlled without discarding otherwise-valid structure.

    Returns (reject_reason, use_staged_entry). reject_reason is None unless
    a real hard-reject rule fires; use_staged_entry is True when SL
    distance exceeds MAX_SL_DISTANCE_PCT (irrelevant if reject_reason is set)."""
    if entry is None or sl is None or swept_level is None or sweep_extreme is None:
        return "AMBIGUOUS SETUP — missing entry/SL/level data", False
    if swept_level <= 0 or entry <= 0:
        return "AMBIGUOUS SETUP — non-positive price data", False

    risk = abs(entry - sl)
    if risk <= 0:
        return "AMBIGUOUS SETUP — entry and SL are identical (zero risk)", False

    # Rule 1 — SL distance: no longer a reject, just a staged-entry trigger
    sl_distance_pct = (risk / entry)
    use_staged_entry = sl_distance_pct > MAX_SL_DISTANCE_PCT

    # Rule 11 (2026-08-27) — minimum reward:risk. `target` is the opposite
    # daily level, i.e. what the strategy actually trades toward. Checked
    # before the sweep-depth rules because a setup offering less reward than
    # risk isn't worth taking regardless of how clean the sweep was.
    # `target` is optional, so any caller not yet passing it simply skips
    # this check rather than crashing.
    if target is not None and target > 0:
        reward = abs(target - entry)
        rr = reward / risk
        if rr < MIN_REWARD_RISK_RATIO:
            return (f"Reward:risk too low ({rr:.2f}x < "
                    f"{MIN_REWARD_RISK_RATIO:.1f}x) — reward to target "
                    f"({reward:.4f}) is smaller than risk to SL ({risk:.4f})",
                    use_staged_entry)

    # Rules 2 & 3 — sweep depth bounds, measured against the level actually
    # being hunted (fixed PDH/PDL, or the dynamic trend-flip reference —
    # whichever `swept_level` resolves to at the call site).
    if side == 'PDH':
        sweep_depth_pct = (sweep_extreme - swept_level) / swept_level
    else:
        sweep_depth_pct = (swept_level - sweep_extreme) / swept_level

    if sweep_depth_pct < MIN_LIQUIDITY_SWEEP_PCT:
        return (f"Sweep too shallow ({sweep_depth_pct*100:.2f}% < "
                f"{MIN_LIQUIDITY_SWEEP_PCT*100:.1f}%)", use_staged_entry)
    if sweep_depth_pct > MAX_LIQUIDITY_SWEEP_PCT:
        return (f"Sweep too deep ({sweep_depth_pct*100:.2f}% > "
                f"{MAX_LIQUIDITY_SWEEP_PCT*100:.1f}%)", use_staged_entry)

    return None, use_staged_entry


class StrategyEngine:
    def __init__(self, coindcx: CoinDCXClient, state: BotState):
        self.coindcx = coindcx
        self.state = state
        self._last_candle: dict = {}  # {symbol: last candle}, needed for inside-bar check
        # Rule 4 (2026-08-13) expiry events, drained by monitor.py after each
        # process_candle() call. process_candle()'s return type stays
        # Optional[Signal] — an expiry isn't a signal, there was never a
        # confirmed entry to alert on in the normal sense, so this is a
        # separate small side-channel rather than overloading Signal for it.
        self.pending_expiry_alerts: list = []
        # 2026-08-18 informational drift tracker -- see constant comment
        # above. drift_observations holds setups currently being watched
        # post-expiry; pending_drift_results holds completed observations
        # (confirmed-late or timed-out) waiting to be drained into an
        # alert by monitor.py. Neither ever touches level.pdh_state /
        # level.pdl_state or any real trading field.
        self.drift_observations: list = []
        self.pending_drift_results: list = []

    async def fetch_and_set_levels(self) -> bool:
        success_count = 0
        for symbol in SYMBOLS:
            try:
                prev_candle = await self.coindcx.get_previous_day_candle(symbol)
                if prev_candle:
                    self.state.set_levels(
                        symbol,
                        pdh=prev_candle['high'],
                        pdl=prev_candle['low']
                    )
                    level = self.state.get_level(symbol)

                    recent_days = await self.coindcx.get_recent_daily_candles(
                        symbol, n=TREND_LOOKBACK_DAYS
                    )
                    classifications = [_classify_day(c) for c in recent_days]
                    day1 = classifications[-1] if len(classifications) >= 1 else "SIDEWAYS"
                    day2 = classifications[-2] if len(classifications) >= 2 else None

                    if day1 in ("UP", "DOWN") and day2 == day1:
                        # 2+ consecutive same-direction trend days — likely
                        # maturing/exhausted. Revert to NONE so the ordinary
                        # dual-sided liquidity-sweep logic handles today,
                        # rather than extrapolating the trend forward again.
                        level.trend_bias = "NONE"
                        bias_note = f"{day1},{day1} consecutive — treating as matured/exhausted, reverting to NONE"
                    elif day1 == "UP":
                        level.trend_bias = "UPTREND"
                        bias_note = "single fresh UP day — flip active"
                    elif day1 == "DOWN":
                        level.trend_bias = "DOWNTREND"
                        bias_note = "single fresh DOWN day — flip active"
                    else:
                        level.trend_bias = "NONE"
                        bias_note = "day-1 not decisive — sideways"

                    logger.info(f"{symbol} | PDH: {prev_candle['high']} | PDL: {prev_candle['low']} "
                                f"| Trend bias: {level.trend_bias} ({bias_note}) "
                                f"| last {len(classifications)} days: {classifications}")
                    success_count += 1
                else:
                    logger.error(f"{symbol} | Failed to get previous day candle")
            except Exception as e:
                logger.error(f"{symbol} | fetch_and_set_levels error: {e}")

        try:
            btc_candle = await self.coindcx.get_previous_day_candle(REGIME_SYMBOL)
            if btc_candle:
                self.state.set_regime_levels(pdh=btc_candle['high'], pdl=btc_candle['low'])
                logger.info(f"{REGIME_SYMBOL} (regime ref) | PDH: {btc_candle['high']} | "
                           f"PDL: {btc_candle['low']}")
            else:
                logger.warning(f"{REGIME_SYMBOL} (regime ref) | Failed to get previous day "
                               f"candle — regime filter defaults to NEUTRAL today")
        except Exception as e:
            logger.error(f"{REGIME_SYMBOL} (regime ref) | fetch error: {e} — "
                         f"regime filter defaults to NEUTRAL today")

        return success_count == len(SYMBOLS)

    def process_candle(self, symbol: str, candle: dict) -> Optional[Signal]:
        level = self.state.get_level(symbol)
        if not level or level.in_trade:
            return None

        prev_candle = self._last_candle.get(symbol)
        self._last_candle[symbol] = candle

        pdh = level.pdh
        pdl = level.pdl
        # 2026-08-20: dynamic trend-flip re-anchoring removed entirely, per
        # explicit request -- SHORT signals now ONLY come from a genuine
        # fixed-PDH sweep, LONG only from a genuine fixed-PDL sweep. Flip
        # trades were already alert-only (never auto-traded), so this has
        # zero impact on auto-executed trades -- it only removes the
        # confusing "swept a level far from the fixed PDH/PDL" alerts.
        effective_pdh = pdh
        effective_pdl = pdl

        signal = None
        is_bullish = candle['close'] > candle['open']
        is_bearish = candle['close'] < candle['open']
        inside_bar = _is_inside_bar(candle, prev_candle)

        if level.pdh_event_active and candle['close'] < effective_pdh:
            level.pdh_event_active = False
            logger.info(f"{symbol} | PDH-side event resolved — price closed back below "
                        f"{effective_pdh:.4f}, ready for a fresh sweep")
        if level.pdl_event_active and candle['close'] > effective_pdl:
            level.pdl_event_active = False
            logger.info(f"{symbol} | PDL-side event resolved — price closed back above "
                        f"{effective_pdl:.4f}, ready for a fresh sweep")

        # ══════════════════════════════════════════════════════════════
        # PDH SIDE — sweep of effective_pdh -> bearish trigger -> SHORT
        # ══════════════════════════════════════════════════════════════
        if not level.pdh_event_active:
            pdh_expired_this_candle = False
            if level.pdh_state in ("SWEPT", "TRIGGERED"):
                if candle['high'] > level.pdh_sweep_extreme:
                    level.pdh_sweep_extreme = candle['high']
                if level.pdh_day_extreme is None or candle['high'] > level.pdh_day_extreme:
                    level.pdh_day_extreme = candle['high']

                # Rule 4 — Entry Expiry (2026-08-13). Measured from the
                # ORIGINAL sweep candle that armed this side, not from any
                # later re-trigger — per the rule's explicit requirement.
                if level.pdh_sweep_armed_at is not None:
                    elapsed_minutes = (candle['time'] - level.pdh_sweep_armed_at) / 1000 / 60
                    if elapsed_minutes > ENTRY_EXPIRY_MINUTES:
                        logger.info(f"{symbol} | PDH-side setup EXPIRED — "
                                    f"{elapsed_minutes:.0f} min since sweep armed, no confirmed "
                                    f"entry within {ENTRY_EXPIRY_MINUTES} min — resetting, "
                                    f"waiting for a completely fresh sweep")
                        # Informational-only snapshot, taken BEFORE the real
                        # state resets below. Read-only from here on.
                        # 2026-08-25 FIX: a setup can expire while still in
                        # SWEPT state (no trigger candle ever formed), leaving
                        # pdh_trigger as None. Reading ['high'] on it raised
                        # TypeError and killed the whole process_candle call —
                        # meaning that symbol silently stopped being evaluated
                        # for the rest of the run. Only record a drift
                        # observation when a trigger actually exists.
                        if level.pdh_trigger is not None:
                            self.drift_observations.append({
                            'symbol': symbol, 'side': 'PDH', 'direction': 'SHORT',
                            'trigger_high': level.pdh_trigger['high'],
                            'trigger_low': level.pdh_trigger['low'],
                            'sweep_extreme': level.pdh_sweep_extreme,
                            'sweep_armed_at': level.pdh_sweep_armed_at,
                            'expired_at': candle['time'],
                            })
                        level.pdh_state = "NONE"
                        level.pdh_trigger = None
                        level.pdh_sweep_extreme = None
                        level.pdh_sweep_armed_at = None
                        level.pdh_sweep_closed_inside = None
                        level.pdh_cisd_ref = None
                        level.pdh_event_active = True  # prevents re-arming on THIS same
                                                        # candle too, via the guard below
                        pdh_expired_this_candle = True
                        self.pending_expiry_alerts.append({
                            'symbol': symbol, 'side': 'PDH',
                            'elapsed_minutes': round(elapsed_minutes),
                        })

            if not pdh_expired_this_candle and level.pdh_state == "NONE":
                if candle['high'] > effective_pdh and not inside_bar:
                    # FIX (2026-07-21): the day's very first sweep attempt now
                    # must also clear MIN_SWEEP_DEPTH_PCT, measured against
                    # effective_pdh itself when no day_extreme exists yet —
                    # previously this check was skipped entirely on the first
                    # attempt. (effective_pdh always equals the fixed pdh
                    # since 2026-08-20 — see module docstring.)
                    baseline = level.pdh_day_extreme if level.pdh_day_extreme is not None else effective_pdh
                    deep_enough = candle['high'] >= baseline * (1 + MIN_SWEEP_DEPTH_PCT)
                    if deep_enough:
                        level.pdh_state = "SWEPT"
                        level.pdh_sweep_extreme = candle['high']
                        level.pdh_day_extreme = candle['high']
                        level.pdh_sweep_armed_at = candle['time']
                        # 2026-08-25 informational classification: a genuine
                        # liquidity hunt pierces the level with the wick but
                        # CLOSES back inside it. A close beyond the level is
                        # acceptance — i.e. a breakout, where a reversal
                        # short is fighting real momentum rather than fading
                        # a stop-hunt. Alert-only; does not gate anything.
                        level.pdh_sweep_closed_inside = candle['close'] < effective_pdh
                        _char = "HUNT (closed back inside)" if level.pdh_sweep_closed_inside \
                                else "BREAKOUT RISK (closed beyond level)"
                        logger.info(f"{symbol} | PDH-side swept (H:{candle['high']:.4f}, "
                                    f"C:{candle['close']:.4f} vs level {effective_pdh:.4f}) "
                                    f"— {_char} — watching for the first bearish trigger candle")

                        body = abs(candle['close'] - candle['open'])
                        upper_wick = candle['high'] - max(candle['open'], candle['close'])
                        is_rejection = (body > 0 and upper_wick >= REJECTION_WICK_RATIO * body
                                        and candle['close'] < effective_pdh)
                        if is_rejection:
                            level.pdh_state = "TRIGGERED"
                            level.pdh_trigger = candle
                            logger.info(f"{symbol} | Sweep candle itself shows rejection "
                                        f"(wick {upper_wick:.4f} vs body {body:.4f}) — "
                                        f"using it as the trigger immediately")
                    else:
                        logger.info(f"{symbol} | PDH-side re-tested (H:{candle['high']:.4f}) but "
                                    f"did not clear the {MIN_SWEEP_DEPTH_PCT*100:.1f}% minimum "
                                    f"depth past {baseline:.4f} — ignoring")
                elif candle['high'] > effective_pdh and inside_bar:
                    logger.info(f"{symbol} | PDH-side breach on an inside bar — skipping, "
                                f"no fresh sweep armed this candle")

            elif level.pdh_state == "SWEPT":
                if is_bearish:
                    level.pdh_state = "TRIGGERED"
                    level.pdh_trigger = candle
                    level.pdh_cisd_ref = prev_candle['open'] if prev_candle is not None else None
                    logger.info(f"{symbol} | Bearish trigger candle formed | "
                                f"Trigger H:{candle['high']:.4f} L:{candle['low']:.4f} "
                                f"(sweep extreme so far: {level.pdh_sweep_extreme:.4f})")

            elif level.pdh_state == "TRIGGERED":
                trig = level.pdh_trigger
                if candle['close'] < trig['low'] and is_bearish:
                    sweep_depth_from_pdh = ((level.pdh_sweep_extreme - pdh) / pdh) if pdh > 0 else 0
                    is_deep_sweep = sweep_depth_from_pdh >= DEEP_SWEEP_THRESHOLD_PCT
                    if USE_CISD_FOR_DEEP_SWEEPS and is_deep_sweep and level.pdh_cisd_ref is not None:
                        reclaim_ok = level.trend_bias == "DOWNTREND" or candle['close'] <= level.pdh_cisd_ref
                        reclaim_ref_used = level.pdh_cisd_ref
                        reclaim_ref_label = "CISD reference"
                    else:
                        reclaim_ok = level.trend_bias == "DOWNTREND" or candle['close'] <= pdh * (1 - MIN_RECLAIM_MARGIN_PCT)
                        reclaim_ref_used = pdh * (1 - MIN_RECLAIM_MARGIN_PCT)
                        reclaim_ref_label = "fixed PDH"
                    if reclaim_ok:
                        entry = candle['close']
                        sl = level.pdh_sweep_extreme * (1 + SL_BUFFER_PCT)
                        # 2026-08-20: flip mechanism removed entirely (see
                        # module docstring / effective_pdh comment above).
                        # counter_trend now purely reflects whether this
                        # SHORT fights today's UPTREND bias -- no more flip
                        # concept at all. As of today, counter_trend no
                        # longer forces alert-only either (see Fix 3 in
                        # monitor.py) -- it now triggers reduced/staged
                        # sizing instead, same mechanism as a wide SL.
                        counter = level.trend_bias == "UPTREND"

                        reject_reason, use_staged_entry = _check_hard_rules(
                            'PDH', entry, sl, effective_pdh, level.pdh_sweep_extreme,
                            target=pdl)

                        signal = Signal(symbol, 'SELL', entry, sl, pdh, pdl,
                                         counter_trend=counter, trend_mode=False,
                                         swept_level=effective_pdh, reject_reason=reject_reason,
                                         use_staged_entry=use_staged_entry,
                                         sweep_closed_inside=level.pdh_sweep_closed_inside)
                        if reject_reason:
                            logger.info(f"{symbol} | SHORT setup confirmed but REJECTED — "
                                        f"{reject_reason} | Entry:{entry:.4f} SL:{sl:.4f} "
                                        f"(sweep extreme {level.pdh_sweep_extreme:.4f})")
                        else:
                            logger.info(f"{symbol} | SHORT signal (trigger confirmed) | "
                                        f"Entry:{entry:.4f} SL:{sl:.4f} "
                                        f"(sweep extreme {level.pdh_sweep_extreme:.4f})"
                                        f"{' [STAGED ENTRY - wide SL]' if use_staged_entry else ''}")
                        level.pdh_state = "NONE"
                        level.pdh_trigger = None
                        level.pdh_sweep_extreme = None
                        level.pdh_sweep_armed_at = None
                        level.pdh_sweep_closed_inside = None
                        level.pdh_event_active = True
                        level.pdh_cisd_ref = None
                        if counter and not reject_reason:
                            level.counter_trend_confirms += 1
                    else:
                        logger.info(f"{symbol} | PDH close {candle['close']:.4f} broke trigger "
                                    f"but did not reclaim below the {reclaim_ref_label} "
                                    f"{reclaim_ref_used:.4f} — rejecting this confirmation")
                elif candle['close'] < trig['low'] and not is_bearish:
                    logger.info(f"{symbol} | PDH close {candle['close']:.4f} broke trigger low "
                                f"{trig['low']:.4f} but candle closed bullish — rejecting this "
                                f"confirmation, still waiting for a bearish close")
                elif candle['close'] > trig['high']:
                    logger.info(f"{symbol} | PDH trigger INVALIDATED — close "
                                f"{candle['close']:.4f} broke trigger high {trig['high']:.4f} "
                                f"before trigger low — resuming watch for a fresh sweep")
                    level.pdh_state = "NONE"
                    level.pdh_trigger = None
                    level.pdh_sweep_extreme = None
                    level.pdh_cisd_ref = None

        # ══════════════════════════════════════════════════════════════
        # PDL SIDE — sweep of effective_pdl -> bullish trigger -> LONG
        # ══════════════════════════════════════════════════════════════
        if signal is None and not level.pdl_event_active:
            pdl_expired_this_candle = False
            if level.pdl_state in ("SWEPT", "TRIGGERED"):
                if candle['low'] < level.pdl_sweep_extreme:
                    level.pdl_sweep_extreme = candle['low']
                if level.pdl_day_extreme is None or candle['low'] < level.pdl_day_extreme:
                    level.pdl_day_extreme = candle['low']

                if level.pdl_sweep_armed_at is not None:
                    elapsed_minutes = (candle['time'] - level.pdl_sweep_armed_at) / 1000 / 60
                    if elapsed_minutes > ENTRY_EXPIRY_MINUTES:
                        logger.info(f"{symbol} | PDL-side setup EXPIRED — "
                                    f"{elapsed_minutes:.0f} min since sweep armed, no confirmed "
                                    f"entry within {ENTRY_EXPIRY_MINUTES} min — resetting, "
                                    f"waiting for a completely fresh sweep")
                        # 2026-08-25 FIX: a setup can expire while still in
                        # SWEPT state (no trigger candle ever formed), leaving
                        # pdl_trigger as None. Reading ['high'] on it raised
                        # TypeError and killed the whole process_candle call —
                        # meaning that symbol silently stopped being evaluated
                        # for the rest of the run. Only record a drift
                        # observation when a trigger actually exists.
                        if level.pdl_trigger is not None:
                            self.drift_observations.append({
                            'symbol': symbol, 'side': 'PDL', 'direction': 'LONG',
                            'trigger_high': level.pdl_trigger['high'],
                            'trigger_low': level.pdl_trigger['low'],
                            'sweep_extreme': level.pdl_sweep_extreme,
                            'sweep_armed_at': level.pdl_sweep_armed_at,
                            'expired_at': candle['time'],
                            })
                        level.pdl_state = "NONE"
                        level.pdl_trigger = None
                        level.pdl_sweep_extreme = None
                        level.pdl_sweep_armed_at = None
                        level.pdl_sweep_closed_inside = None
                        level.pdl_cisd_ref = None
                        level.pdl_event_active = True
                        pdl_expired_this_candle = True
                        self.pending_expiry_alerts.append({
                            'symbol': symbol, 'side': 'PDL',
                            'elapsed_minutes': round(elapsed_minutes),
                        })

            if not pdl_expired_this_candle and level.pdl_state == "NONE":
                if candle['low'] < effective_pdl and not inside_bar:
                    # See matching PDH-side comment above — same fix.
                    baseline = level.pdl_day_extreme if level.pdl_day_extreme is not None else effective_pdl
                    deep_enough = candle['low'] <= baseline * (1 - MIN_SWEEP_DEPTH_PCT)
                    if deep_enough:
                        level.pdl_state = "SWEPT"
                        level.pdl_sweep_extreme = candle['low']
                        level.pdl_day_extreme = candle['low']
                        level.pdl_sweep_armed_at = candle['time']
                        # Mirror of the PDH-side classification above.
                        level.pdl_sweep_closed_inside = candle['close'] > effective_pdl
                        _char = "HUNT (closed back inside)" if level.pdl_sweep_closed_inside \
                                else "BREAKOUT RISK (closed beyond level)"
                        logger.info(f"{symbol} | PDL-side swept (L:{candle['low']:.4f}, "
                                    f"C:{candle['close']:.4f} vs level {effective_pdl:.4f}) "
                                    f"— {_char} — watching for the first bullish trigger candle")

                        body = abs(candle['close'] - candle['open'])
                        lower_wick = min(candle['open'], candle['close']) - candle['low']
                        is_rejection = (body > 0 and lower_wick >= REJECTION_WICK_RATIO * body
                                        and candle['close'] > effective_pdl)
                        if is_rejection:
                            level.pdl_state = "TRIGGERED"
                            level.pdl_trigger = candle
                            logger.info(f"{symbol} | Sweep candle itself shows rejection "
                                        f"(wick {lower_wick:.4f} vs body {body:.4f}) — "
                                        f"using it as the trigger immediately")
                    else:
                        logger.info(f"{symbol} | PDL-side re-tested (L:{candle['low']:.4f}) but "
                                    f"did not clear the {MIN_SWEEP_DEPTH_PCT*100:.1f}% minimum "
                                    f"depth past {baseline:.4f} — ignoring")
                elif candle['low'] < effective_pdl and inside_bar:
                    logger.info(f"{symbol} | PDL-side breach on an inside bar — skipping, "
                                f"no fresh sweep armed this candle")

            elif level.pdl_state == "SWEPT":
                if is_bullish:
                    level.pdl_state = "TRIGGERED"
                    level.pdl_trigger = candle
                    level.pdl_cisd_ref = prev_candle['open'] if prev_candle is not None else None
                    logger.info(f"{symbol} | Bullish trigger candle formed | "
                                f"Trigger H:{candle['high']:.4f} L:{candle['low']:.4f} "
                                f"(sweep extreme so far: {level.pdl_sweep_extreme:.4f})")

            elif level.pdl_state == "TRIGGERED":
                trig = level.pdl_trigger
                if candle['close'] > trig['high'] and is_bullish:
                    sweep_depth_from_pdl = ((pdl - level.pdl_sweep_extreme) / pdl) if pdl > 0 else 0
                    is_deep_sweep = sweep_depth_from_pdl >= DEEP_SWEEP_THRESHOLD_PCT
                    if USE_CISD_FOR_DEEP_SWEEPS and is_deep_sweep and level.pdl_cisd_ref is not None:
                        reclaim_ok = level.trend_bias == "UPTREND" or candle['close'] >= level.pdl_cisd_ref
                        reclaim_ref_used = level.pdl_cisd_ref
                        reclaim_ref_label = "CISD reference"
                    else:
                        reclaim_ok = level.trend_bias == "UPTREND" or candle['close'] >= pdl * (1 + MIN_RECLAIM_MARGIN_PCT)
                        reclaim_ref_used = pdl * (1 + MIN_RECLAIM_MARGIN_PCT)
                        reclaim_ref_label = "fixed PDL"
                    if reclaim_ok:
                        entry = candle['close']
                        sl = level.pdl_sweep_extreme * (1 - SL_BUFFER_PCT)
                        # 2026-08-20: flip mechanism removed entirely -- see
                        # the mirrored comment in the PDH-side block above.
                        counter = level.trend_bias == "DOWNTREND"

                        reject_reason, use_staged_entry = _check_hard_rules(
                            'PDL', entry, sl, effective_pdl, level.pdl_sweep_extreme,
                            target=pdh)

                        signal = Signal(symbol, 'BUY', entry, sl, pdh, pdl,
                                         counter_trend=counter, trend_mode=False,
                                         swept_level=effective_pdl, reject_reason=reject_reason,
                                         use_staged_entry=use_staged_entry,
                                         sweep_closed_inside=level.pdl_sweep_closed_inside)
                        if reject_reason:
                            logger.info(f"{symbol} | LONG setup confirmed but REJECTED — "
                                        f"{reject_reason} | Entry:{entry:.4f} SL:{sl:.4f} "
                                        f"(sweep extreme {level.pdl_sweep_extreme:.4f})")
                        else:
                            logger.info(f"{symbol} | LONG signal (trigger confirmed) | "
                                        f"Entry:{entry:.4f} SL:{sl:.4f} "
                                        f"(sweep extreme {level.pdl_sweep_extreme:.4f})"
                                        f"{' [STAGED ENTRY - wide SL]' if use_staged_entry else ''}")
                        level.pdl_state = "NONE"
                        level.pdl_trigger = None
                        level.pdl_sweep_extreme = None
                        level.pdl_sweep_armed_at = None
                        level.pdl_sweep_closed_inside = None
                        level.pdl_event_active = True
                        level.pdl_cisd_ref = None
                        if counter and not reject_reason:
                            level.counter_trend_confirms += 1
                    else:
                        logger.info(f"{symbol} | PDL close {candle['close']:.4f} broke trigger "
                                    f"but did not reclaim above the {reclaim_ref_label} "
                                    f"{reclaim_ref_used:.4f} — rejecting this confirmation")
                elif candle['close'] > trig['high'] and not is_bullish:
                    logger.info(f"{symbol} | PDL close {candle['close']:.4f} broke trigger high "
                                f"{trig['high']:.4f} but candle closed bearish — rejecting this "
                                f"confirmation, still waiting for a bullish close")
                elif candle['close'] < trig['low']:
                    logger.info(f"{symbol} | PDL trigger INVALIDATED — close "
                                f"{candle['close']:.4f} broke trigger low {trig['low']:.4f} "
                                f"before trigger high — resuming watch for a fresh sweep")
                    level.pdl_state = "NONE"
                    level.pdl_trigger = None
                    level.pdl_cisd_ref = None

        # Informational-only drift observation check -- entirely read-only,
        # never touches level.pdh_state/pdl_state or `signal`. See the
        # POST_EXPIRY_DRIFT_OBSERVATION_MINUTES comment above.
        self._check_drift_observations(symbol, candle, level)

        return signal

    def _check_drift_observations(self, symbol: str, candle: dict, level) -> None:
        """Watches previously-expired setups to see if price eventually
        does what would have confirmed them, purely for later analysis.
        Never re-arms real state, never produces a Signal, never affects
        auto-trading. Drops an observation if it goes stale (too old) or
        if a genuinely fresh sweep has since re-armed that same side,
        since a new real cycle supersedes the old informational watch."""
        still_watching = []
        for obs in self.drift_observations:
            if obs['symbol'] != symbol:
                still_watching.append(obs)
                continue

            elapsed_min = (candle['time'] - obs['expired_at']) / 1000 / 60
            if elapsed_min > POST_EXPIRY_DRIFT_OBSERVATION_MINUTES:
                continue  # too old, drop silently -- no verdict either way

            fresh_state = level.pdh_state if obs['side'] == 'PDH' else level.pdl_state
            if fresh_state != "NONE":
                continue  # a genuinely new cycle has started, old watch is stale

            if obs['direction'] == 'SHORT':
                confirmed = candle['close'] < obs['trigger_low'] and candle['close'] < candle['open']
            else:
                confirmed = candle['close'] > obs['trigger_high'] and candle['close'] > candle['open']

            if confirmed:
                drift_pct = abs(candle['close'] - obs['sweep_extreme']) / obs['sweep_extreme'] * 100
                total_elapsed_min = (candle['time'] - obs['sweep_armed_at']) / 1000 / 60
                self.pending_drift_results.append({
                    'symbol': symbol, 'side': obs['side'], 'direction': obs['direction'],
                    'would_be_entry': candle['close'], 'elapsed_minutes': round(total_elapsed_min),
                    'drift_pct': round(drift_pct, 2),
                })
                continue  # found it, drop the observation

            still_watching.append(obs)

        self.drift_observations = still_watching
