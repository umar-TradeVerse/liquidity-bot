"""
StrategyEngine — Pure liquidity sweep reversal strategy. Nothing else.

Event-driven: each side (PDH / PDL) of a symbol resets itself back to NONE
immediately after producing a signal, regardless of whether that signal
actually resulted in an executed trade. This means a fresh sweep can be
caught right away rather than waiting for anything else to happen.

The one thing that DOES fully block a symbol is in_trade — set by the
caller (monitor.py) only once an order has actually been placed, and
cleared only once a live position check confirms that position has closed
(see BotState.reset_symbol_watch). This is what makes "one open position
at a time per symbol" hold, while still allowing unlimited independent
setups across the day once each position closes.

BUY-SIDE SWEEP (bearish reversal — SHORT):
  1. Price sweeps above PDH (a candle's high > PDH)
  2. Rejection candle forms (a candle closes back below PDH — this can be
     the same candle that swept, or a later one; the bot keeps watching
     for as long as it takes)
  3. Confirmation candle closes below the rejection candle's low
  -> Enter SHORT, SL just above the rejection candle's high

SELL-SIDE SWEEP (bullish reversal — LONG):
  1. Price sweeps below PDL (a candle's low < PDL)
  2. Rejection candle forms (a candle closes back above PDL)
  3. Confirmation candle closes above the rejection candle's high
  -> Enter LONG, SL just below the rejection candle's low

No indicators, no pattern matching, no proximity filters, no breakout
fallback. If a symbol never confirms, it simply doesn't trade — that is a
valid, expected outcome, not an error.

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
        Returns a Signal only when a liquidity sweep fully confirms.
        Returns None if a symbol is currently in_trade (position open), or
        if no sweep/rejection/confirmation sequence has completed yet.
        """
        level = self.state.get_level(symbol)
        if not level or level.in_trade:
            return None

        pdh = level.pdh
        pdl = level.pdl
        signal = None

        # ══════════════════════════════════════════════════════════════
        # PDH SIDE — buy-side sweep -> bearish reversal (SHORT)
        # ══════════════════════════════════════════════════════════════
        if level.pdh_state == "NONE":
            if candle['high'] > pdh:
                if candle['close'] < pdh:
                    level.pdh_state = "REJECTED"
                    level.pdh_rejection = candle
                    logger.info(f"{symbol} | PDH swept + rejected (same candle) | "
                                f"H:{candle['high']:.4f} C:{candle['close']:.4f}")
                else:
                    level.pdh_state = "SWEPT"
                    logger.info(f"{symbol} | PDH swept (closed above PDH) — watching for rejection")

        elif level.pdh_state == "SWEPT":
            if candle['close'] < pdh:
                level.pdh_state = "REJECTED"
                level.pdh_rejection = candle
                logger.info(f"{symbol} | PDH rejection candle formed | L:{candle['low']:.4f}")

        elif level.pdh_state == "REJECTED":
            rej = level.pdh_rejection
            if candle['close'] < rej['low']:
                sl = rej['high'] * (1 + SL_BUFFER_PCT)
                signal = Signal(symbol, 'SELL', candle['close'], sl, pdh, pdl)
                logger.info(f"{symbol} | SHORT signal (liquidity sweep) | "
                            f"Entry:{candle['close']:.4f} SL:{sl:.4f}")
                # This setup has concluded (fired) — reset immediately so a
                # fresh sweep can be caught right away regardless of what
                # happens to this signal (executed, skipped, or failed).
                level.pdh_state = "NONE"
                level.pdh_rejection = None

        # ══════════════════════════════════════════════════════════════
        # PDL SIDE — sell-side sweep -> bullish reversal (LONG)
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
                        logger.info(f"{symbol} | PDL swept (closed below PDL) — watching for rejection")

            elif level.pdl_state == "SWEPT":
                if candle['close'] > pdl:
                    level.pdl_state = "REJECTED"
                    level.pdl_rejection = candle
                    logger.info(f"{symbol} | PDL rejection candle formed | H:{candle['high']:.4f}")

            elif level.pdl_state == "REJECTED":
                rej = level.pdl_rejection
                if candle['close'] > rej['high']:
                    sl = rej['low'] * (1 - SL_BUFFER_PCT)
                    signal = Signal(symbol, 'BUY', candle['close'], sl, pdh, pdl)
                    logger.info(f"{symbol} | LONG signal (liquidity sweep) | "
                                f"Entry:{candle['close']:.4f} SL:{sl:.4f}")
                    level.pdl_state = "NONE"
                    level.pdl_rejection = None

        return signal
