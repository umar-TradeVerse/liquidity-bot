"""
StrategyEngine — Pure liquidity sweep + breakout strategy.

Exactly 3 possible outcomes per symbol per day. No indicators, no pattern
matching, no proximity filters. Every decision is based solely on candle
open/high/low/close relative to PDH/PDL.

SCENARIO 1 — Liquidity Sweep Reversal
  Buy-side sweep (bearish reversal):
    1. Price sweeps above PDH (a candle's high > PDH)
    2. Rejection candle forms (a candle closes back below PDH)
    3. Confirmation candle closes below the rejection candle's low
    -> Enter SHORT

  Sell-side sweep (bullish reversal):
    1. Price sweeps below PDL (a candle's low < PDL)
    2. Rejection candle forms (a candle closes back above PDL)
    3. Confirmation candle closes above the rejection candle's high
    -> Enter LONG

SCENARIO 2 — Breakout Trade
  Bullish breakout:
    1. PDH is broken (candle closes above PDH)
    2. No rejection formed (the very next candle also closes above PDH,
       instead of closing back below it)
    -> Enter LONG

  Bearish breakout:
    1. PDL is broken (candle closes below PDL)
    2. No rejection formed (the very next candle also closes below PDL)
    -> Enter SHORT

SCENARIO 3 — Inside Day
  Price stays within PDH/PDL, or a sweep/breakout sequence never confirms.
  -> No trade. This is simply the absence of Scenario 1 or 2 — no explicit
  code path is needed for it.

SL placement: a small fixed buffer (SL_BUFFER_PCT) is applied beyond the
rejection candle's extreme (sweep reversal) or beyond PDH/PDL itself
(breakout). This is the only numeric parameter beyond the 3 conditions,
included purely so the stop isn't placed exactly on the level itself
(which price could touch and reverse from without invalidating the trade).
Set SL_BUFFER_PCT = 0 if you want the stop placed exactly at the level/
rejection extreme with no buffer at all.
"""

import logging
from typing import Optional
from core.state import BotState, SYMBOLS
from exchange.coindcx import CoinDCXClient
from utils.logger import setup_logger
logger = setup_logger("strategy")

# Buffer applied beyond the SL reference point. Set to 0 for an exact,
# literal implementation with no buffer at all.
SL_BUFFER_PCT = 0.001


class Signal:
    def __init__(self, symbol, side, entry_price, sl_price, scenario, pdh, pdl):
        self.symbol = symbol
        self.side = side
        self.entry_price = entry_price
        self.sl_price = sl_price
        self.scenario = scenario
        self.pattern = "Liquidity Sweep" if scenario == "sweep_reversal" else "Breakout"
        self.pdh = pdh
        self.pdl = pdl

    def __repr__(self):
        return (f"Signal({self.symbol} {self.side} @ {self.entry_price:.4f} "
                f"SL:{self.sl_price:.4f} [{self.scenario}])")


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
        Process a new completed 1m candle for a symbol.
        Returns a Signal if Scenario 1 or 2 confirms, else None (Scenario 3).
        """
        level = self.state.get_level(symbol)
        if not level or level.scenario_fired:
            return None

        pdh = level.pdh
        pdl = level.pdl
        signal = None

        # ══════════════════════════════════════════════════════════════
        # PDH SIDE — bearish sweep reversal (SHORT) / bullish breakout (LONG)
        # ══════════════════════════════════════════════════════════════
        if level.pdh_state == "NONE":
            if candle['high'] > pdh:
                if candle['close'] < pdh:
                    # Swept and rejected within the same candle
                    level.pdh_state = "REJECTED"
                    level.pdh_rejection = candle
                    logger.info(f"{symbol} | PDH swept + rejected (same candle) | "
                                f"H:{candle['high']:.4f} C:{candle['close']:.4f}")
                else:
                    level.pdh_state = "SWEPT"
                    logger.info(f"{symbol} | PDH swept (closed above PDH) — watching next candle")

        elif level.pdh_state == "SWEPT":
            if candle['close'] < pdh:
                level.pdh_state = "REJECTED"
                level.pdh_rejection = candle
                logger.info(f"{symbol} | PDH rejection candle formed | L:{candle['low']:.4f}")
            else:
                # No rejection — two effective closes above PDH — breakout confirmed
                sl = pdh * (1 - SL_BUFFER_PCT)
                signal = Signal(symbol, 'BUY', candle['close'], sl, 'breakout', pdh, pdl)
                level.scenario_fired = True
                logger.info(f"{symbol} | BULLISH BREAKOUT | Entry:{candle['close']:.4f} SL:{sl:.4f}")

        elif level.pdh_state == "REJECTED":
            rej = level.pdh_rejection
            if candle['close'] < rej['low']:
                sl = rej['high'] * (1 + SL_BUFFER_PCT)
                signal = Signal(symbol, 'SELL', candle['close'], sl, 'sweep_reversal', pdh, pdl)
                level.scenario_fired = True
                logger.info(f"{symbol} | SHORT signal (sweep reversal) | "
                            f"Entry:{candle['close']:.4f} SL:{sl:.4f}")
            # else: keep waiting for confirmation, no invalidation logic added

        # ══════════════════════════════════════════════════════════════
        # PDL SIDE — bullish sweep reversal (LONG) / bearish breakout (SHORT)
        # Only evaluated if PDH side didn't already fire a signal this candle.
        # ══════════════════════════════════════════════════════════════
        if signal is None:
            if level.pdl_state == "NONE":
                if candle['low'] < pdl:
                    if candle['close'] > pdl:
                        level.pdl_state = "REJECTED"
                        level.pdl_rejection = candle
                        logger.info(f"{symbol} | PDL swept + rejected (same candle) | "
                                    f"L:{candle['low']:.4f} C:{candle['close']:.4f}")
                    else:
                        level.pdl_state = "SWEPT"
                        logger.info(f"{symbol} | PDL swept (closed below PDL) — watching next candle")

            elif level.pdl_state == "SWEPT":
                if candle['close'] > pdl:
                    level.pdl_state = "REJECTED"
                    level.pdl_rejection = candle
                    logger.info(f"{symbol} | PDL rejection candle formed | H:{candle['high']:.4f}")
                else:
                    sl = pdl * (1 + SL_BUFFER_PCT)
                    signal = Signal(symbol, 'SELL', candle['close'], sl, 'breakout', pdh, pdl)
                    level.scenario_fired = True
                    logger.info(f"{symbol} | BEARISH BREAKOUT | Entry:{candle['close']:.4f} SL:{sl:.4f}")

            elif level.pdl_state == "REJECTED":
                rej = level.pdl_rejection
                if candle['close'] > rej['high']:
                    sl = rej['low'] * (1 - SL_BUFFER_PCT)
                    signal = Signal(symbol, 'BUY', candle['close'], sl, 'sweep_reversal', pdh, pdl)
                    level.scenario_fired = True
                    logger.info(f"{symbol} | LONG signal (sweep reversal) | "
                                f"Entry:{candle['close']:.4f} SL:{sl:.4f}")
                # else: keep waiting for confirmation, no invalidation logic added

        return signal
