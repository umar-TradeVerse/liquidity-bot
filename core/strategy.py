"""
StrategyEngine — Liquidity sweep + trigger-candle reversal strategy, with
three-way trend classification layered on top.

BASE MECHANICS (unchanged from the original design), applied against
"effective" levels that are either the fixed daily PDH/PDL (trend_bias
== NONE) or a dynamically re-anchored reference (trend_bias set):

  1. Liquidity Sweep — a candle's low/high breaks new ground past the
     effective level and past today's deepest prior sweep on that side.
  2. Trigger Candle — the first opposite-colour candle after the sweep,
     UNLESS the sweep candle itself already shows a rejection shape
     (wick >= REJECTION_WICK_RATIO * body, and its own close already
     back on the correct side of the effective level) — in that case
     the sweep candle becomes the trigger immediately, no waiting.
  3. Entry — close breaks the trigger's high/low AND that candle is
     itself the confirming colour AND (for the ORIGINAL, non-flip
     sweep only) the close is back on the correct side of the FIXED
     daily PDH/PDL — the "reclaim" requirement. This reclaim check is
     against the fixed level always, which makes it a no-op for
     trend-flip trades (price is already well past the fixed level by
     definition of being in a trend) while remaining a real filter for
     genuine sideways-market reversals.
  4. Stop Loss — beyond the sweep extreme for this cycle, buffered by
     SL_BUFFER_PCT.
  5. Invalidation — close breaks back through the trigger's other side
     first — scrapped, re-arms immediately.

FRESH-SWEEP QUALITY GATES (apply only to the NONE->SWEPT transition,
not to the seeding of a trend-flip, which bypasses these deliberately
since it's a direct hand-off from an already-validated confirmation):
  - MIN_SWEEP_DEPTH_PCT — the new extreme must clear the prior deepest
    sweep by at least this fraction, not just by any tiny amount.
  - Inside-bar skip — a candle that's fully inside the previous
    candle's range never arms a fresh sweep (low conviction).

TREND-FLIP MECHANIC (validated against 2026-07-17 real trades — ETH,
SOL, XRP all traced to ~2.8-3R winners using this exact logic, and the
mirror shape confirmed visually against LTC's 2026-07-14 uptrend):

  trend_bias is classified once per symbol at the daily reset, from
  yesterday's own daily candle body relative to its full range
  (TREND_BODY_RATIO_THRESHOLD). A decisively bearish daily candle sets
  DOWNTREND; decisively bullish sets UPTREND; anything smaller-bodied
  stays NONE (sideways — original dual-sided logic, untouched).

  In DOWNTREND: the PDL (buy-side) sweep-reversal machinery still runs
  exactly as before and still requires all the same gates — but its
  resulting LONG signal is marked counter_trend and is alert-only, not
  auto-traded (see monitor.py). The MOMENT that LONG signal confirms,
  its own confirmation candle's high seeds trend_ref_high, and the
  PDH-side (sell-side liquidity / SHORT) state machine is manually
  kick-started into SWEPT against that seed — no separate "sweep
  beyond a level" step needed, since the seed candle, by definition,
  just made a fresh high. From there the SHORT side runs the identical
  trigger/confirm logic as a normal PDH sweep, just anchored dynamically.

  UPTREND is the exact mirror: the PDH (sell-side) machinery still runs,
  its resulting SHORT signal is counter_trend/alert-only, and the
  moment it confirms, its own low seeds trend_ref_low, kick-starting the
  PDL-side (buy-side liquidity / LONG) hunt from there.

No indicators, no pattern matching beyond what's described above.
"""

import logging
from typing import Optional
from core.state import BotState, SYMBOLS, REGIME_SYMBOL
from exchange.coindcx import CoinDCXClient
from utils.logger import setup_logger
logger = setup_logger("strategy")

SL_BUFFER_PCT = 0.002               # 0.2% buffer beyond the sweep extreme
MIN_SWEEP_DEPTH_PCT = 0.002          # UNVALIDATED placeholder — 0.2%
REJECTION_WICK_RATIO = 2.0           # wick must be >= 2x body to fast-path the trigger
TREND_BODY_RATIO_THRESHOLD = 0.5     # UNVALIDATED placeholder — daily body must
                                     # cover >= 50% of the day's full range to
                                     # count as a decisive trend day


class Signal:
    def __init__(self, symbol, side, entry_price, sl_price, pdh, pdl,
                 counter_trend=False, trend_mode=False):
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

    def __repr__(self):
        return (f"Signal({self.symbol} {self.side} @ {self.entry_price:.4f} "
                f"SL:{self.sl_price:.4f} [liquidity sweep])")


def _is_inside_bar(candle: dict, prev_candle: Optional[dict]) -> bool:
    if prev_candle is None:
        return False
    return candle['high'] <= prev_candle['high'] and candle['low'] >= prev_candle['low']


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

                    body = abs(prev_candle['close'] - prev_candle['open'])
                    day_range = prev_candle['high'] - prev_candle['low']
                    body_ratio = (body / day_range) if day_range > 0 else 0

                    if prev_candle['close'] < prev_candle['open'] and body_ratio >= TREND_BODY_RATIO_THRESHOLD:
                        level.trend_bias = "DOWNTREND"
                    elif prev_candle['close'] > prev_candle['open'] and body_ratio >= TREND_BODY_RATIO_THRESHOLD:
                        level.trend_bias = "UPTREND"
                    else:
                        level.trend_bias = "NONE"

                    logger.info(f"{symbol} | PDH: {prev_candle['high']} | PDL: {prev_candle['low']} "
                                f"| Trend bias: {level.trend_bias} (body_ratio={body_ratio:.2f})")
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
        # Effective levels: dynamic re-anchored reference when a trend is
        # active on that side, otherwise the fixed daily level.
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
                    deep_enough = (
                        level.pdh_day_extreme is None or
                        candle['high'] >= level.pdh_day_extreme * (1 + MIN_SWEEP_DEPTH_PCT)
                    )
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
                                    f"depth past {level.pdh_day_extreme:.4f} — ignoring")
                elif candle['high'] > effective_pdh and inside_bar:
                    logger.info(f"{symbol} | PDH-side breach on an inside bar — skipping, "
                                f"no fresh sweep armed this candle")

            elif level.pdh_state == "SWEPT":
                if is_bearish:
                    level.pdh_state = "TRIGGERED"
                    level.pdh_trigger = candle
                    logger.info(f"{symbol} | Bearish trigger candle formed | "
                                f"Trigger H:{candle['high']:.4f} L:{candle['low']:.4f} "
                                f"(sweep extreme so far: {level.pdh_sweep_extreme:.4f})")

            elif level.pdh_state == "TRIGGERED":
                trig = level.pdh_trigger
                if candle['close'] < trig['low'] and is_bearish:
                    reclaim_ok = level.trend_bias == "DOWNTREND" or candle['close'] < pdh
                    if reclaim_ok:
                        entry = candle['close']
                        sl = level.pdh_sweep_extreme * (1 + SL_BUFFER_PCT)
                        is_flip = level.trend_bias == "DOWNTREND"
                        counter = level.trend_bias == "UPTREND"
                        signal = Signal(symbol, 'SELL', entry, sl, pdh, pdl,
                                         counter_trend=counter, trend_mode=is_flip)
                        logger.info(f"{symbol} | SHORT signal (trigger confirmed) | "
                                    f"Entry:{entry:.4f} SL:{sl:.4f} "
                                    f"(sweep extreme {level.pdh_sweep_extreme:.4f})"
                                    f"{' [trend-aligned flip]' if is_flip else ''}")
                        level.pdh_state = "NONE"
                        level.pdh_trigger = None
                        level.pdh_sweep_extreme = None
                        level.pdh_event_active = True

                        # UPTREND mirror: this counter-trend SHORT confirmation
                        # is the seed for the buy-side liquidity LONG hunt.
                        # The confirming candle just made a fresh low by
                        # definition — kick-start the PDL-side state machine
                        # against it directly, bypassing the normal fresh-sweep
                        # depth/inside-bar gates (hand-off, not a new sweep).
                        if counter and level.trend_bias == "UPTREND":
                            seed_low = candle['low']
                            if level.trend_ref_low is None or seed_low < level.trend_ref_low:
                                level.trend_ref_low = seed_low
                            level.pdl_state = "SWEPT"
                            level.pdl_sweep_extreme = level.trend_ref_low
                            level.pdl_day_extreme = level.trend_ref_low
                            level.pdl_trigger = None
                            level.pdl_event_active = False
                            logger.info(f"{symbol} | UPTREND — seeding buy-side liquidity "
                                        f"hunt at {level.trend_ref_low:.4f} from this dip")
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
                    deep_enough = (
                        level.pdl_day_extreme is None or
                        candle['low'] <= level.pdl_day_extreme * (1 - MIN_SWEEP_DEPTH_PCT)
                    )
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
                                    f"depth past {level.pdl_day_extreme:.4f} — ignoring")
                elif candle['low'] < effective_pdl and inside_bar:
                    logger.info(f"{symbol} | PDL-side breach on an inside bar — skipping, "
                                f"no fresh sweep armed this candle")

            elif level.pdl_state == "SWEPT":
                if is_bullish:
                    level.pdl_state = "TRIGGERED"
                    level.pdl_trigger = candle
                    logger.info(f"{symbol} | Bullish trigger candle formed | "
                                f"Trigger H:{candle['high']:.4f} L:{candle['low']:.4f} "
                                f"(sweep extreme so far: {level.pdl_sweep_extreme:.4f})")

            elif level.pdl_state == "TRIGGERED":
                trig = level.pdl_trigger
                if candle['close'] > trig['high'] and is_bullish:
                    reclaim_ok = level.trend_bias == "UPTREND" or candle['close'] > pdl
                    if reclaim_ok:
                        entry = candle['close']
                        sl = level.pdl_sweep_extreme * (1 - SL_BUFFER_PCT)
                        is_flip = level.trend_bias == "UPTREND"
                        counter = level.trend_bias == "DOWNTREND"
                        signal = Signal(symbol, 'BUY', entry, sl, pdh, pdl,
                                         counter_trend=counter, trend_mode=is_flip)
                        logger.info(f"{symbol} | LONG signal (trigger confirmed) | "
                                    f"Entry:{entry:.4f} SL:{sl:.4f} "
                                    f"(sweep extreme {level.pdl_sweep_extreme:.4f})"
                                    f"{' [trend-aligned flip]' if is_flip else ''}")
                        level.pdl_state = "NONE"
                        level.pdl_trigger = None
                        level.pdl_sweep_extreme = None
                        level.pdl_event_active = True

                        # DOWNTREND: this counter-trend LONG confirmation is the
                        # seed for the sell-side liquidity SHORT hunt. The
                        # confirming candle just made a fresh high by
                        # definition — kick-start the PDH-side state machine
                        # against it directly, bypassing the normal fresh-sweep
                        # depth/inside-bar gates (hand-off, not a new sweep).
                        if counter and level.trend_bias == "DOWNTREND":
                            seed_high = candle['high']
                            if level.trend_ref_high is None or seed_high > level.trend_ref_high:
                                level.trend_ref_high = seed_high
                            level.pdh_state = "SWEPT"
                            level.pdh_sweep_extreme = level.trend_ref_high
                            level.pdh_day_extreme = level.trend_ref_high
                            level.pdh_trigger = None
                            level.pdh_event_active = False
                            logger.info(f"{symbol} | DOWNTREND — seeding sell-side liquidity "
                                        f"hunt at {level.trend_ref_high:.4f} from this bounce")
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
                    level.pdl_sweep_extreme = None

        return signal
