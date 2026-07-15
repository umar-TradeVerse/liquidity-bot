"""
StrategyEngine — Liquidity sweep + trigger-candle reversal strategy.

BUY-SIDE SETUP (sweep of PDL -> bullish reversal -> LONG):
  1. Liquidity Sweep — a candle's low trades below PDL. NOT the entry
     candle. A sweep only ARMS if it exceeds pdl_day_extreme (the
     deepest low swept below PDL so far today, across all prior
     cycles) — a shallow re-dip that doesn't break new ground is
     logged and ignored, not treated as a fresh sweep.
  2. Trigger Candle — the FIRST candle after the sweep whose close is
     bullish becomes the Trigger Candle.
  3. Entry — only when a later candle's CLOSE breaks above Trigger High.
  4. Stop Loss — below the SWEEP extreme for this cycle (the lowest low
     reached from sweep through to confirm), buffered by SL_BUFFER_PCT.
  5. Invalidation — close breaks Trigger Low before Trigger High —
     scrapped, re-arms immediately (still subject to the day-extreme
     check above).

SELL-SIDE SETUP (sweep of PDH -> bearish reversal -> SHORT): mirrored.

Once a signal fires, that side locks (event_active) until price closes
back on the other side of the level. Invalidation does NOT lock.

No indicators, no pattern matching, no proximity filters.
SL_BUFFER_PCT is the only numeric parameter beyond these conditions.
"""

import logging
from typing import Optional
from core.state import BotState, SYMBOLS, REGIME_SYMBOL
from exchange.coindcx import CoinDCXClient
from utils.logger import setup_logger
logger = setup_logger("strategy")

SL_BUFFER_PCT = 0.002  # 0.2% buffer beyond the sweep extreme


class Signal:
    def __init__(self, symbol, side, entry_price, sl_price, pdh, pdl):
        self.symbol = symbol
        self.side = side
        self.entry_price = entry_price
        self.sl_price = sl_price
        self.scenario = "sweep_reversal"
        self.pattern = "Liquidity Sweep"
        self.pdh = pdh
        self.pdl = pdl

    def __repr__(self):
        return (f"Signal({self.symbol} {self.side} @ {self.entry_price:.4f} "
                f"SL:{self.sl_price:.4f} [liquidity sweep])")


class StrategyEngine:
    def __init__(self, coindcx: CoinDCXClient, state: BotState):
        self.coindcx = coindcx
        self.state = state

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
                    logger.info(f"{symbol} | PDH: {prev_candle['high']} | PDL: {prev_candle['low']}")
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

        pdh = level.pdh
        pdl = level.pdl
        signal = None
        is_bullish = candle['close'] > candle['open']
        is_bearish = candle['close'] < candle['open']

        if level.pdh_event_active and candle['close'] < pdh:
            level.pdh_event_active = False
            logger.info(f"{symbol} | PDH event resolved — price closed back below "
                        f"PDH ({pdh:.4f}), ready for a fresh sweep")
        if level.pdl_event_active and candle['close'] > pdl:
            level.pdl_event_active = False
            logger.info(f"{symbol} | PDL event resolved — price closed back above "
                        f"PDL ({pdl:.4f}), ready for a fresh sweep")

        # ══════════════════════════════════════════════════════════════
        # PDH SIDE — sweep of PDH -> bearish trigger -> SHORT
        # ══════════════════════════════════════════════════════════════
        if not level.pdh_event_active:
            if level.pdh_state in ("SWEPT", "TRIGGERED"):
                if candle['high'] > level.pdh_sweep_extreme:
                    level.pdh_sweep_extreme = candle['high']
                if level.pdh_day_extreme is None or candle['high'] > level.pdh_day_extreme:
                    level.pdh_day_extreme = candle['high']

            if level.pdh_state == "NONE":
                if candle['high'] > pdh:
                    if level.pdh_day_extreme is None or candle['high'] > level.pdh_day_extreme:
                        level.pdh_state = "SWEPT"
                        level.pdh_sweep_extreme = candle['high']
                        level.pdh_day_extreme = candle['high']
                        logger.info(f"{symbol} | PDH swept (H:{candle['high']:.4f}) — "
                                    f"watching for the first bearish trigger candle")
                    else:
                        logger.info(f"{symbol} | PDH re-tested (H:{candle['high']:.4f}) but "
                                    f"did not exceed today's deepest sweep "
                                    f"({level.pdh_day_extreme:.4f}) — not a fresh sweep, ignoring")

            elif level.pdh_state == "SWEPT":
                if is_bearish:
                    level.pdh_state = "TRIGGERED"
                    level.pdh_trigger = candle
                    logger.info(f"{symbol} | Bearish trigger candle formed | "
                                f"Trigger H:{candle['high']:.4f} L:{candle['low']:.4f} "
                                f"(sweep extreme so far: {level.pdh_sweep_extreme:.4f})")

            elif level.pdh_state == "TRIGGERED":
                trig = level.pdh_trigger
                if candle['close'] < trig['low']:
                    entry = candle['close']
                    sl = level.pdh_sweep_extreme * (1 + SL_BUFFER_PCT)
                    signal = Signal(symbol, 'SELL', entry, sl, pdh, pdl)
                    logger.info(f"{symbol} | SHORT signal (trigger confirmed) | "
                                f"Entry:{entry:.4f} SL:{sl:.4f} "
                                f"(sweep extreme {level.pdh_sweep_extreme:.4f})")
                    level.pdh_state = "NONE"
                    level.pdh_trigger = None
                    level.pdh_sweep_extreme = None
                    level.pdh_event_active = True
                elif candle['close'] > trig['high']:
                    logger.info(f"{symbol} | PDH trigger INVALIDATED — close "
                                f"{candle['close']:.4f} broke trigger high {trig['high']:.4f} "
                                f"before trigger low — resuming watch for a fresh sweep")
                    level.pdh_state = "NONE"
                    level.pdh_trigger = None
                    level.pdh_sweep_extreme = None

        # ══════════════════════════════════════════════════════════════
        # PDL SIDE — sweep of PDL -> bullish trigger -> LONG
        # ══════════════════════════════════════════════════════════════
        if signal is None and not level.pdl_event_active:
            if level.pdl_state in ("SWEPT", "TRIGGERED"):
                if candle['low'] < level.pdl_sweep_extreme:
                    level.pdl_sweep_extreme = candle['low']
                if level.pdl_day_extreme is None or candle['low'] < level.pdl_day_extreme:
                    level.pdl_day_extreme = candle['low']

            if level.pdl_state == "NONE":
                if candle['low'] < pdl:
                    if level.pdl_day_extreme is None or candle['low'] < level.pdl_day_extreme:
                        level.pdl_state = "SWEPT"
                        level.pdl_sweep_extreme = candle['low']
                        level.pdl_day_extreme = candle['low']
                        logger.info(f"{symbol} | PDL swept (L:{candle['low']:.4f}) — "
                                    f"watching for the first bullish trigger candle")
                    else:
                        logger.info(f"{symbol} | PDL re-tested (L:{candle['low']:.4f}) but "
                                    f"did not exceed today's deepest sweep "
                                    f"({level.pdl_day_extreme:.4f}) — not a fresh sweep, ignoring")

            elif level.pdl_state == "SWEPT":
                if is_bullish:
                    level.pdl_state = "TRIGGERED"
                    level.pdl_trigger = candle
                    logger.info(f"{symbol} | Bullish trigger candle formed | "
                                f"Trigger H:{candle['high']:.4f} L:{candle['low']:.4f} "
                                f"(sweep extreme so far: {level.pdl_sweep_extreme:.4f})")

            elif level.pdl_state == "TRIGGERED":
                trig = level.pdl_trigger
                if candle['close'] > trig['high']:
                    entry = candle['close']
                    sl = level.pdl_sweep_extreme * (1 - SL_BUFFER_PCT)
                    signal = Signal(symbol, 'BUY', entry, sl, pdh, pdl)
                    logger.info(f"{symbol} | LONG signal (trigger confirmed) | "
                                f"Entry:{entry:.4f} SL:{sl:.4f} "
                                f"(sweep extreme {level.pdl_sweep_extreme:.4f})")
                    level.pdl_state = "NONE"
                    level.pdl_trigger = None
                    level.pdl_sweep_extreme = None
                    level.pdl_event_active = True
                elif candle['close'] < trig['low']:
                    logger.info(f"{symbol} | PDL trigger INVALIDATED — close "
                                f"{candle['close']:.4f} broke trigger low {trig['low']:.4f} "
                                f"before trigger high — resuming watch for a fresh sweep")
                    level.pdl_state = "NONE"
                    level.pdl_trigger = None
                    level.pdl_sweep_extreme = None

        return signal
