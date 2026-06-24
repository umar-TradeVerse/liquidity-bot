"""
StrategyEngine — PDH/PDL fetch + signal detection for all 3 scenarios.

Signal flow per symbol per 1m candle:
  1. If scenario already fired → skip
  2. Check for sweep reversal (Scenario 1)
  3. Check for breakout (Scenario 2)
  4. If neither → inside day (Scenario 3, silent)
"""

import logging
from typing import Optional, Tuple
from core.state import BotState, SYMBOLS
from core.patterns import (
    is_rejection_candle_bearish,
    is_rejection_candle_bullish,
    pattern_name
)
from exchange.delta import DeltaClient

logger = logging.getLogger("strategy")

# SL buffer: 0.1% above/below rejection candle high/low
SL_BUFFER_PCT = 0.001


class Signal:
    def __init__(self, symbol, side, entry_price, sl_price, scenario, pattern, pdh, pdl):
        self.symbol = symbol
        self.side = side                  # 'BUY' or 'SELL'
        self.entry_price = entry_price
        self.sl_price = sl_price
        self.scenario = scenario          # 'sweep_reversal' or 'breakout'
        self.pattern = pattern            # candlestick pattern name or 'breakout'
        self.pdh = pdh
        self.pdl = pdl

    def __repr__(self):
        return (f"Signal({self.symbol} {self.side} @ {self.entry_price:.4f} "
                f"SL:{self.sl_price:.4f} [{self.scenario}])")


class StrategyEngine:
    def __init__(self, delta: DeltaClient, state: BotState):
        self.delta = delta
        self.state = state
        # Per-symbol: store last candle for engulfing detection
        self._prev_candles = {sym: None for sym in SYMBOLS}

    async def fetch_and_set_levels(self) -> bool:
        """Fetch previous day OHLC for all symbols and store PDH/PDL."""
        success_count = 0
        for symbol in SYMBOLS:
            try:
                prev_candle = await self.delta.get_previous_day_candle(symbol)
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
        Process a new 1m candle for a symbol.
        Returns a Signal if an actionable setup is detected, else None.

        candle: {open, high, low, close, volume, time}
        """
        level = self.state.get_level(symbol)
        if not level:
            return None

        # Skip if a scenario already fired today for this symbol
        if level.scenario_fired:
            return None

        pdh = level.pdh
        pdl = level.pdl
        prev = self._prev_candles.get(symbol)
        signal = None

        # ── SCENARIO 1: SWEEP REVERSAL ──────────────────────────────────────

        # Phase A: Detect PDH break — price closed above PDH
        if not level.pdh_swept and candle['close'] > pdh:
            level.pdh_swept = True
            logger.info(f"{symbol} | PDH swept — watching for rejection candle")

        # Phase B: After PDH swept — look for rejection candle
        elif level.pdh_swept and level.rejection_candle is None:
            if is_rejection_candle_bearish(candle, prev):
                self.state.set_rejection_candle(symbol, candle, side='above_pdh')
                pname = pattern_name(candle, prev, side='bearish')
                logger.info(f"{symbol} | Rejection candle detected: {pname} | "
                            f"H:{candle['high']:.4f} L:{candle['low']:.4f}")

        # Phase C: Confirmation — next candle breaks rejection candle low → SHORT
        elif level.pdh_swept and level.rejection_candle is not None and level.rejection_side == 'above_pdh':
            rej = level.rejection_candle
            if candle['close'] < rej['low']:
                sl = rej['high'] * (1 + SL_BUFFER_PCT)
                pname = pattern_name(rej, side='bearish')
                signal = Signal(
                    symbol=symbol,
                    side='SELL',
                    entry_price=candle['close'],
                    sl_price=sl,
                    scenario='sweep_reversal',
                    pattern=pname,
                    pdh=pdh,
                    pdl=pdl
                )
                logger.info(f"{symbol} | SHORT signal | Entry:{candle['close']:.4f} SL:{sl:.4f}")
            elif not self._candle_near_rejection(candle, level.rejection_candle, side='above_pdh'):
                # Price moved away from rejection without confirming → invalidate
                self.state.clear_rejection_candle(symbol)
                logger.info(f"{symbol} | Rejection candle invalidated (price moved away)")

        # ── PDL SWEEP (mirror) ───────────────────────────────────────────────

        if not level.pdl_swept and candle['close'] < pdl:
            level.pdl_swept = True
            logger.info(f"{symbol} | PDL swept — watching for rejection candle")

        elif level.pdl_swept and level.rejection_candle is None:
            if is_rejection_candle_bullish(candle, prev):
                self.state.set_rejection_candle(symbol, candle, side='below_pdl')
                pname = pattern_name(candle, prev, side='bullish')
                logger.info(f"{symbol} | Bullish rejection candle: {pname}")

        elif level.pdl_swept and level.rejection_candle is not None and level.rejection_side == 'below_pdl':
            rej = level.rejection_candle
            if candle['close'] > rej['high']:
                sl = rej['low'] * (1 - SL_BUFFER_PCT)
                pname = pattern_name(rej, side='bullish')
                signal = Signal(
                    symbol=symbol,
                    side='BUY',
                    entry_price=candle['close'],
                    sl_price=sl,
                    scenario='sweep_reversal',
                    pattern=pname,
                    pdh=pdh,
                    pdl=pdl
                )
                logger.info(f"{symbol} | LONG signal | Entry:{candle['close']:.4f} SL:{sl:.4f}")
            elif not self._candle_near_rejection(candle, level.rejection_candle, side='below_pdl'):
                self.state.clear_rejection_candle(symbol)

        # ── SCENARIO 2: BREAKOUT (no sweep rejection) ───────────────────────

        # Only check breakout if no sweep was detected (no pdh_swept/pdl_swept)
        if signal is None and not level.pdh_swept and not level.pdl_swept:

            # Bullish breakout: clean close above PDH, no rejection candle formed
            if (not level.pdh_broken and
                    candle['close'] > pdh and
                    level.rejection_candle is None):
                # Need confirmation: check if prev candle also closed above PDH
                if prev and prev['close'] > pdh:
                    level.pdh_broken = True
                    signal = Signal(
                        symbol=symbol,
                        side='BUY',
                        entry_price=candle['close'],
                        sl_price=pdh * (1 - SL_BUFFER_PCT),  # SL just below PDH
                        scenario='breakout',
                        pattern='Bullish Breakout',
                        pdh=pdh,
                        pdl=pdl
                    )
                    logger.info(f"{symbol} | BULLISH BREAKOUT | Entry:{candle['close']:.4f}")

            # Bearish breakout: clean close below PDL
            elif (not level.pdl_broken and
                    candle['close'] < pdl and
                    level.rejection_candle is None):
                if prev and prev['close'] < pdl:
                    level.pdl_broken = True
                    signal = Signal(
                        symbol=symbol,
                        side='SELL',
                        entry_price=candle['close'],
                        sl_price=pdl * (1 + SL_BUFFER_PCT),  # SL just above PDL
                        scenario='breakout',
                        pattern='Bearish Breakout',
                        pdh=pdh,
                        pdl=pdl
                    )
                    logger.info(f"{symbol} | BEARISH BREAKOUT | Entry:{candle['close']:.4f}")

        # Store current candle as prev for next iteration
        self._prev_candles[symbol] = candle

        return signal

    def _candle_near_rejection(self, candle: dict, rejection: dict, side: str) -> bool:
        """
        Checks if the current candle is still 'near' the rejection candle.
        If price has moved more than 0.5% away from rejection zone, invalidate.
        """
        if side == 'above_pdh':
            distance = abs(candle['close'] - rejection['low']) / rejection['low']
        else:
            distance = abs(candle['close'] - rejection['high']) / rejection['high']
        return distance < 0.005  # within 0.5%
