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
  this is a single FRESH trend day, and the flip is applied:

  In DOWNTREND: the PDL (buy-side) sweep-reversal machinery still runs
  exactly as before and still requires all the same gates — but its
  resulting LONG signal is marked counter_trend and is alert-only, not
  auto-traded. The MOMENT that LONG signal confirms, its own
  confirmation candle's high becomes a candidate reference
  (trend_ref_high) for the sell-side liquidity SHORT hunt.

  *** FIX (2026-07-26) ***: this candidate reference does NOT
  automatically arm the PDH-side state machine into "SWEPT" anymore.
  Real cases (ETHUSD and KAITOUSD, both 2026-07-26) showed the old
  behaviour — force-setting pdh_state="SWEPT" directly from the seed —
  let a trade fire even when price never independently swept past that
  reference by any meaningful margin (ETH: seed 1876.53, actual peak
  only 1878.50, just 0.10% — far short of the same 0.2% bar every
  other sweep has to clear; KAITO: entry landed at essentially the
  seed price itself, 1.0106 vs seed 1.0108). Now the seed only updates
  trend_ref_high/trend_ref_low, which becomes the new effective_pdh/
  effective_pdl automatically — the EXISTING NONE-state sweep-arming
  logic (depth gate, inside-bar gate) then has to independently detect
  a real sweep past it on a LATER candle, exactly like any other
  sweep, before anything arms. Checked against real data: this change
  correctly still allows ICPUSD's validated win (real sweep, 0.70%
  past its seed) while blocking both disputed ETH and KAITO trades at
  the source.

  UPTREND is the exact mirror: the PDH (sell-side) machinery still
  runs, its resulting SHORT signal is counter_trend/alert-only, and
  the moment it confirms, its own low becomes a candidate reference
  (trend_ref_low) for the buy-side liquidity LONG hunt — same fix
  applies, no automatic arming.

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

SL_BUFFER_PCT = 0.002               # 0.2% buffer beyond the sweep extreme
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


class Signal:
    def __init__(self, symbol, side, entry_price, sl_price, pdh, pdl,
                 counter_trend=False, trend_mode=False, swept_level=None):
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
        # The level actually swept to produce this signal — the FIXED daily
        # PDH/PDL for a normal-regime trade, or the DYNAMIC re-anchored
        # trend_ref_high/trend_ref_low for a trend-flip trade. This is what
        # alerts should display, not always self.pdh/self.pdl, which stay
        # fixed even when a flip trade is actually hunting a different,
        # re-anchored level entirely.
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


class StrategyEngine:
    def __init__(self, coindcx: CoinDCXClient, state: BotState):
        self.coindcx = coindcx
        self.state = state
        self._last_candle: dict = {}  # {symbol: last candle}, needed for inside-bar check

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
        effective_pdh = level.trend_ref_high if (level.trend_bias == "DOWNTREND" and level.trend_ref_high is not None) else pdh
        effective_pdl = level.trend_ref_low if (level.trend_bias == "UPTREND" and level.trend_ref_low is not None) else pdl

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
            if level.pdh_state in ("SWEPT", "TRIGGERED"):
                if candle['high'] > level.pdh_sweep_extreme:
                    level.pdh_sweep_extreme = candle['high']
                if level.pdh_day_extreme is None or candle['high'] > level.pdh_day_extreme:
                    level.pdh_day_extreme = candle['high']

            if level.pdh_state == "NONE":
                if candle['high'] > effective_pdh and not inside_bar:
                    # FIX (2026-07-21): the day's very first sweep attempt now
                    # must also clear MIN_SWEEP_DEPTH_PCT, measured against
                    # effective_pdh itself when no day_extreme exists yet —
                    # previously this check was skipped entirely on the first
                    # attempt. This same check now also governs a trend-flip's
                    # seeded reference (effective_pdh resolves to
                    # trend_ref_high automatically), per the 2026-07-26 fix —
                    # see module docstring.
                    baseline = level.pdh_day_extreme if level.pdh_day_extreme is not None else effective_pdh
                    deep_enough = candle['high'] >= baseline * (1 + MIN_SWEEP_DEPTH_PCT)
                    if deep_enough:
                        level.pdh_state = "SWEPT"
                        level.pdh_sweep_extreme = candle['high']
                        level.pdh_day_extreme = candle['high']
                        logger.info(f"{symbol} | PDH-side swept (H:{candle['high']:.4f}) — "
                                    f"watching for the first bearish trigger candle")

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
                    else:
                        reclaim_ok = level.trend_bias == "DOWNTREND" or candle['close'] <= pdh * (1 - MIN_RECLAIM_MARGIN_PCT)
                    if reclaim_ok:
                        entry = candle['close']
                        sl = level.pdh_sweep_extreme * (1 + SL_BUFFER_PCT)
                        is_flip = level.trend_bias == "DOWNTREND"
                        counter = level.trend_bias == "UPTREND"
                        signal = Signal(symbol, 'SELL', entry, sl, pdh, pdl,
                                         counter_trend=counter, trend_mode=is_flip,
                                         swept_level=effective_pdh)
                        logger.info(f"{symbol} | SHORT signal (trigger confirmed) | "
                                    f"Entry:{entry:.4f} SL:{sl:.4f} "
                                    f"(sweep extreme {level.pdh_sweep_extreme:.4f})"
                                    f"{' [trend-aligned flip]' if is_flip else ''}")
                        level.pdh_state = "NONE"
                        level.pdh_trigger = None
                        level.pdh_sweep_extreme = None
                        level.pdh_event_active = True
                        level.pdh_cisd_ref = None
                        if counter:
                            level.counter_trend_confirms += 1

                        # UPTREND mirror: this counter-trend SHORT confirmation
                        # sets a CANDIDATE reference for the buy-side liquidity
                        # LONG hunt. FIX (2026-07-26): no longer force-arms
                        # pdl_state — a later candle must independently sweep
                        # past this reference through the normal NONE-state
                        # gates (depth + inside-bar) before anything arms.
                        # See module docstring for the real cases that made
                        # this necessary.
                        if counter and level.trend_bias == "UPTREND":
                            seed_low = candle['low']
                            if level.trend_ref_low is None or seed_low < level.trend_ref_low:
                                level.trend_ref_low = seed_low
                            logger.info(f"{symbol} | UPTREND — buy-side reference set at "
                                        f"{level.trend_ref_low:.4f} from this dip; still needs "
                                        f"a genuine sweep past it to arm")
                    else:
                        logger.info(f"{symbol} | PDH close {candle['close']:.4f} broke trigger "
                                    f"but did not reclaim below the fixed PDH {pdh:.4f} — "
                                    f"rejecting this confirmation")
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
            if level.pdl_state in ("SWEPT", "TRIGGERED"):
                if candle['low'] < level.pdl_sweep_extreme:
                    level.pdl_sweep_extreme = candle['low']
                if level.pdl_day_extreme is None or candle['low'] < level.pdl_day_extreme:
                    level.pdl_day_extreme = candle['low']

            if level.pdl_state == "NONE":
                if candle['low'] < effective_pdl and not inside_bar:
                    # See matching PDH-side comment above — same fix, and
                    # this now also governs a trend-flip's seeded reference
                    # for the same reason (effective_pdl resolves to
                    # trend_ref_low automatically).
                    baseline = level.pdl_day_extreme if level.pdl_day_extreme is not None else effective_pdl
                    deep_enough = candle['low'] <= baseline * (1 - MIN_SWEEP_DEPTH_PCT)
                    if deep_enough:
                        level.pdl_state = "SWEPT"
                        level.pdl_sweep_extreme = candle['low']
                        level.pdl_day_extreme = candle['low']
                        logger.info(f"{symbol} | PDL-side swept (L:{candle['low']:.4f}) — "
                                    f"watching for the first bullish trigger candle")

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
                    else:
                        reclaim_ok = level.trend_bias == "UPTREND" or candle['close'] >= pdl * (1 + MIN_RECLAIM_MARGIN_PCT)
                    if reclaim_ok:
                        entry = candle['close']
                        sl = level.pdl_sweep_extreme * (1 - SL_BUFFER_PCT)
                        is_flip = level.trend_bias == "UPTREND"
                        counter = level.trend_bias == "DOWNTREND"
                        signal = Signal(symbol, 'BUY', entry, sl, pdh, pdl,
                                         counter_trend=counter, trend_mode=is_flip,
                                         swept_level=effective_pdl)
                        logger.info(f"{symbol} | LONG signal (trigger confirmed) | "
                                    f"Entry:{entry:.4f} SL:{sl:.4f} "
                                    f"(sweep extreme {level.pdl_sweep_extreme:.4f})"
                                    f"{' [trend-aligned flip]' if is_flip else ''}")
                        level.pdl_state = "NONE"
                        level.pdl_trigger = None
                        level.pdl_sweep_extreme = None
                        level.pdl_event_active = True
                        level.pdl_cisd_ref = None
                        if counter:
                            level.counter_trend_confirms += 1

                        # DOWNTREND: this counter-trend LONG confirmation sets
                        # a CANDIDATE reference for the sell-side liquidity
                        # SHORT hunt. FIX (2026-07-26): no longer force-arms
                        # pdh_state — see module docstring and the mirrored
                        # comment in the PDH-side block above.
                        if counter and level.trend_bias == "DOWNTREND":
                            seed_high = candle['high']
                            if level.trend_ref_high is None or seed_high > level.trend_ref_high:
                                level.trend_ref_high = seed_high
                            logger.info(f"{symbol} | DOWNTREND — sell-side reference set at "
                                        f"{level.trend_ref_high:.4f} from this bounce; still "
                                        f"needs a genuine sweep past it to arm")
                    else:
                        logger.info(f"{symbol} | PDL close {candle['close']:.4f} broke trigger "
                                    f"but did not reclaim above the fixed PDL {pdl:.4f} — "
                                    f"rejecting this confirmation")
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

        return signal
