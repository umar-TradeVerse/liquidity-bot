"""
StrategyEngine — Pure liquidity sweep reversal strategy, with a rolling
reference candle. Nothing else.

BUY-SIDE SWEEP (sell-side liquidity taken -> bullish reversal -> LONG):
  1. A candle's low breaks below PDL -> this candle becomes the reference.
  2. Every candle after that, compare against the CURRENT reference:
       - If the new candle's low breaks below the reference's low,
         the sweep continues -> that candle BECOMES the new reference
         (roll forward). There is no limit on how many times this can
         roll, or how far the reference drifts from the original PDL.
       - If the new candle's high breaks above the reference's high,
         enter LONG immediately. SL = the reference's low.
       - If neither happens, do nothing and keep waiting with the same
         reference.

SELL-SIDE SWEEP (buy-side liquidity taken -> bearish reversal -> SHORT):
  Mirrored exactly:
  1. A candle's high breaks above PDH -> becomes the reference.
  2. Each next candle:
       - New high above reference's high -> rolls the reference forward.
       - New low below reference's low -> enter SHORT immediately.
         SL = the reference's high.

Entry is triggered by the candle's HIGH or LOW breaching the reference
(not its close) — but since the bot only sees fully-closed candles, the
actual execution price used is that candle's close, as the closest
available price to "the moment the breach was seen."

No indicators, no pattern matching, no proximity filters, no cap on
reference drift. If a symbol never confirms, it simply doesn't trade —
that is a valid, expected outcome, not an error.

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
            if candle['low'] < ref['low']:
                sl = ref['high'] * (1 + SL_BUFFER_PCT)
                signal = Signal(symbol, 'SELL', candle['close'], sl, pdh, pdl)
                logger.info(f"{symbol} | SHORT signal (liquidity sweep) | "
                            f"Entry:{candle['close']:.4f} SL:{sl:.4f}")
                level.pdh_state = "NONE"
                level.pdh_reference = None
            elif candle['high'] > ref['high']:
                level.pdh_reference = candle
                logger.info(f"{symbol} | PDH reference rolled | "
                            f"new H:{candle['high']:.4f} L:{candle['low']:.4f}")
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
                if candle['high'] > ref['high']:
                    sl = ref['low'] * (1 - SL_BUFFER_PCT)
                    signal = Signal(symbol, 'BUY', candle['close'], sl, pdh, pdl)
                    logger.info(f"{symbol} | LONG signal (liquidity sweep) | "
                                f"Entry:{candle['close']:.4f} SL:{sl:.4f}")
                    level.pdl_state = "NONE"
                    level.pdl_reference = None
                elif candle['low'] < ref['low']:
                    level.pdl_reference = candle
                    logger.info(f"{symbol} | PDL reference rolled | "
                                f"new L:{candle['low']:.4f} H:{candle['high']:.4f}")
                # else: neither breached — keep waiting with the same reference

        return signal
