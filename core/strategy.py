"""
StrategyEngine — Pure liquidity sweep reversal strategy, with rollover
reference candles. Nothing else.

BUY-SIDE SWEEP (sell-side liquidity taken -> bullish reversal -> LONG):
  1. A candle's low breaks below PDL -> this candle becomes the reference
     ("Liquidity N").
  2. Every candle after that, compare against the CURRENT reference:
       - If the new candle's high breaks above the reference's high,
         enter LONG immediately. SL = the reference's low.
       - If the new candle's low breaks below the reference's low, the
         market is continuing the sweep, not reversing -> ABANDON this
         reference entirely (reset to NONE). The bot goes back to
         watching for a completely fresh touch of PDL to start a new
         rollover — it does NOT keep tracking this candle as a new
         reference. This is what keeps every eventual entry anchored
         near the actual PDL, instead of drifting arbitrarily far from
         it through repeated rolling.
       - If neither happens, do nothing and keep waiting with the same
         reference.

SELL-SIDE SWEEP (buy-side liquidity taken -> bearish reversal -> SHORT):
  Mirrored exactly:
  1. A candle's high breaks above PDH -> becomes the reference.
  2. Each next candle:
       - New low below reference's low -> enter SHORT immediately.
         SL = the reference's high.
       - New high above reference's high -> continuation, not reversal
         -> ABANDON this reference (reset to NONE), watch for a fresh
         touch of PDH.

Entry is triggered by the candle's HIGH or LOW breaching the reference
(not its close) — but since the bot only sees fully-closed candles, the
actual execution price used is that candle's close, as the closest
available price to "the moment the breach was seen."

No indicators, no pattern matching, no proximity filters. If a symbol
never confirms, it simply doesn't trade — that is a valid, expected
outcome, not an error.

SL_BUFFER_PCT is the only numeric parameter beyond these conditions.
"""

import logging
from typing import Optional
from core.state import BotState, SYMBOLS
from exchange.coindcx import CoinDCXClient
from utils.logger import setup_logger
logger = setup_logger("strategy")

SL_BUFFER_PCT = 0.001


class Signal:
    def __init__(self, symbol, side, entry_price, sl_price, pdh, pdl,
                 needs_review=False, review_reason=None):
        self.symbol = symbol
        self.side = side
        self.entry_price = entry_price
        self.sl_price = sl_price
        self.scenario = "sweep_reversal"
        self.pattern = "Liquidity Sweep"
        self.pdh = pdh
        self.pdl = pdl
        # needs_review=True means: don't auto-place this trade, just alert
        # Telegram and let the person decide manually. Used for setups where
        # the entry price ends up on the "wrong side" of the level it was
        # supposed to be rejecting from — e.g. a SHORT whose entry is below
        # PDH, because the reference candle's own low already dipped below
        # PDH from a single wide wick (no rolling/drift needed for this to
        # happen).
        self.needs_review = needs_review
        self.review_reason = review_reason

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

        return success_count == len(SYMBOLS)

    def process_candle(self, symbol: str, candle: dict) -> Optional[Signal]:
        """
        Process a new completed candle for a symbol.
        Returns a Signal only when a reference candle's high (LONG side)
        or low (SHORT side) is breached. Returns None otherwise —
        including while a reference is being tracked/rolled, which is
        normal and can persist and drift for as long as it takes.
        """
        level = self.state.get_level(symbol)
        if not level or level.in_trade:
            return None

        pdh = level.pdh
        pdl = level.pdl
        signal = None

        # ══════════════════════════════════════════════════════════════
        # PDH SIDE — sell-side sweep -> bearish reversal (SHORT)
        # ══════════════════════════════════════════════════════════════
        if level.pdh_state == "NONE":
            if candle['high'] > pdh:
                level.pdh_state = "TRACKING"
                level.pdh_reference = candle
                logger.info(f"{symbol} | PDH swept — reference set | "
                            f"H:{candle['high']:.4f} L:{candle['low']:.4f}")

        elif level.pdh_state == "TRACKING":
            ref = level.pdh_reference
            if candle['close'] < ref['low']:
                sl = ref['high'] * (1 + SL_BUFFER_PCT)
                entry = candle['close']
                if ref['low'] < pdh:
                    signal = Signal(symbol, 'SELL', entry, sl, pdh, pdl,
                                     needs_review=True,
                                     review_reason=f"The reference candle's own low "
                                                    f"({ref['low']:.4f}) already dipped below "
                                                    f"PDH ({pdh:.4f}) before entry.")
                    logger.info(f"{symbol} | SHORT signal FLAGGED FOR REVIEW "
                                f"(reference low {ref['low']:.4f} below PDH {pdh:.4f}) | "
                                f"Entry:{entry:.4f} SL:{sl:.4f}")
                else:
                    signal = Signal(symbol, 'SELL', entry, sl, pdh, pdl)
                    logger.info(f"{symbol} | SHORT signal (liquidity sweep) | "
                                f"Entry:{entry:.4f} SL:{sl:.4f}")
                level.pdh_state = "NONE"
                level.pdh_reference = None
            elif candle['high'] > ref['high']:
                logger.info(f"{symbol} | PDH continuation — reference abandoned "
                            f"(H:{candle['high']:.4f} broke ref H:{ref['high']:.4f}), "
                            f"watching for a fresh sweep")
                level.pdh_state = "NONE"
                level.pdh_reference = None
            elif candle['low'] < ref['low']:
                logger.debug(f"{symbol} | PDH low wicked below ref low but close "
                            f"({candle['close']:.4f}) didn't confirm — still waiting")
            # else: neither breached — keep waiting with the same reference

        # ══════════════════════════════════════════════════════════════
        # PDL SIDE — buy-side sweep -> bullish reversal (LONG)
        # Only evaluated if PDH side didn't already fire a signal this candle.
        # ══════════════════════════════════════════════════════════════
        if signal is None:
            if level.pdl_state == "NONE":
                if candle['low'] < pdl:
                    level.pdl_state = "TRACKING"
                    level.pdl_reference = candle
                    logger.info(f"{symbol} | PDL swept — reference set | "
                                f"L:{candle['low']:.4f} H:{candle['high']:.4f}")

            elif level.pdl_state == "TRACKING":
                ref = level.pdl_reference
                if candle['close'] > ref['high']:
                    sl = ref['low'] * (1 - SL_BUFFER_PCT)
                    entry = candle['close']
                    if ref['high'] > pdl:
                        signal = Signal(symbol, 'BUY', entry, sl, pdh, pdl,
                                         needs_review=True,
                                         review_reason=f"The reference candle's own high "
                                                        f"({ref['high']:.4f}) already poked above "
                                                        f"PDL ({pdl:.4f}) before entry.")
                        logger.info(f"{symbol} | LONG signal FLAGGED FOR REVIEW "
                                    f"(reference high {ref['high']:.4f} above PDL {pdl:.4f}) | "
                                    f"Entry:{entry:.4f} SL:{sl:.4f}")
                    else:
                        signal = Signal(symbol, 'BUY', entry, sl, pdh, pdl)
                        logger.info(f"{symbol} | LONG signal (liquidity sweep) | "
                                    f"Entry:{entry:.4f} SL:{sl:.4f}")
                    level.pdl_state = "NONE"
                    level.pdl_reference = None
                elif candle['low'] < ref['low']:
                    logger.info(f"{symbol} | PDL continuation — reference abandoned "
                                f"(L:{candle['low']:.4f} broke ref L:{ref['low']:.4f}), "
                                f"watching for a fresh sweep")
                    level.pdl_state = "NONE"
                    level.pdl_reference = None
                elif candle['high'] > ref['high']:
                    logger.debug(f"{symbol} | PDL high wicked above ref high but close "
                                f"({candle['close']:.4f}) didn't confirm — still waiting")
                # else: neither breached — keep waiting with the same reference

        return signal
