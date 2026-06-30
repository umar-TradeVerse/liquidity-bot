"""
StrategyEngine — PDH/PDL fetch + signal detection for all 3 scenarios.

SWEEP REVERSAL rule (tightened):
  - For SHORT: price must close above PDH AND rejection candle's HIGH must be
    above PDH AND within PROXIMITY_PCT of PDH (confirming an actual wick sweep
    right at the level, not a breakout that ran far away)
  - For LONG: price must close below PDL AND rejection candle's LOW must be
    below PDL AND within PROXIMITY_PCT of PDL

  CRITICAL FIX: if price moves more than PROXIMITY_PCT away from PDH/PDL
  without a rejection candle forming, the sweep watch EXPIRES and converts
  to breakout-eligible instead of waiting indefinitely for a rejection that
  will never be a genuine "sweep" (this was firing false sweep-reversal
  signals when price had already broken out and moved far away, e.g. SOL
  climbing from PDH 70.4 to 75 before a random bearish candle fired SHORT).

BREAKOUT rule:
  - No rejection candle formed near PDH/PDL
  - Two consecutive closes above PDH (LONG) or below PDL (SHORT)
"""

import logging
from typing import Optional
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

# Rejection candle must be within this % of PDH/PDL to count as a genuine sweep.
# Beyond this, it's no longer a "sweep" — it's a breakout that already happened.
PROXIMITY_PCT = 0.008  # 0.8%


class Signal:
    def __init__(self, symbol, side, entry_price, sl_price, scenario, pattern, pdh, pdl):
        self.symbol = symbol
        self.side = side
        self.entry_price = entry_price
        self.sl_price = sl_price
        self.scenario = scenario
        self.pattern = pattern
        self.pdh = pdh
        self.pdl = pdl

    def __repr__(self):
        return (f"Signal({self.symbol} {self.side} @ {self.entry_price:.4f} "
                f"SL:{self.sl_price:.4f} [{self.scenario}])")


class StrategyEngine:
    def __init__(self, delta: DeltaClient, state: BotState):
        self.delta = delta
        self.state = state
        self._prev_candles = {sym: None for sym in SYMBOLS}

    async def fetch_and_set_levels(self) -> bool:
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
        """
        level = self.state.get_level(symbol)
        if not level:
            return None

        if level.scenario_fired:
            return None

        pdh = level.pdh
        pdl = level.pdl
        prev = self._prev_candles.get(symbol)
        signal = None

        # ── SCENARIO 1: SWEEP REVERSAL — BEARISH (SHORT) ─────────────────────

        # Phase A: Price closes above PDH → sweep detected
        if not level.pdh_swept and candle['close'] > pdh:
            level.pdh_swept = True
            logger.info(f"{symbol} | PDH swept (close above PDH) — watching for rejection")

        # Phase B: Look for rejection candle
        # KEY FIX: rejection candle's HIGH must be above PDH AND within
        # PROXIMITY_PCT of PDH. If price has moved further away than that
        # without a rejection forming, the sweep watch expires (this was a
        # breakout, not a sweep) — pdh_swept is reset so Scenario 2 can fire.
        elif level.pdh_swept and level.rejection_candle is None:
            distance_from_pdh = (candle['close'] - pdh) / pdh
            if distance_from_pdh > PROXIMITY_PCT:
                level.pdh_swept = False
                logger.info(f"{symbol} | Sweep watch expired — price {distance_from_pdh*100:.2f}% "
                            f"above PDH with no rejection. Reverting to breakout-eligible.")
            elif (is_rejection_candle_bearish(candle, prev) and
                    candle['high'] > pdh and
                    (candle['high'] - pdh) / pdh <= PROXIMITY_PCT):
                self.state.set_rejection_candle(symbol, candle, side='above_pdh')
                pname = pattern_name(candle, prev, side='bearish')
                logger.info(f"{symbol} | Bearish rejection candle above PDH: {pname} | "
                            f"H:{candle['high']:.4f} L:{candle['low']:.4f}")

        # Phase C: Confirmation — next candle closes below rejection candle low → SHORT
        elif (level.pdh_swept and
              level.rejection_candle is not None and
              level.rejection_side == 'above_pdh'):
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
                self.state.clear_rejection_candle(symbol)
                logger.info(f"{symbol} | Rejection candle invalidated")

        # ── SCENARIO 1: SWEEP REVERSAL — BULLISH (LONG) ──────────────────────

        if not level.pdl_swept and candle['close'] < pdl:
            level.pdl_swept = True
            logger.info(f"{symbol} | PDL swept (close below PDL) — watching for rejection")

        elif level.pdl_swept and level.rejection_candle is None:
            distance_from_pdl = (pdl - candle['close']) / pdl
            if distance_from_pdl > PROXIMITY_PCT:
                level.pdl_swept = False
                logger.info(f"{symbol} | Sweep watch expired — price {distance_from_pdl*100:.2f}% "
                            f"below PDL with no rejection. Reverting to breakout-eligible.")
            elif (is_rejection_candle_bullish(candle, prev) and
                    candle['low'] < pdl and
                    (pdl - candle['low']) / pdl <= PROXIMITY_PCT):
                self.state.set_rejection_candle(symbol, candle, side='below_pdl')
                pname = pattern_name(candle, prev, side='bullish')
                logger.info(f"{symbol} | Bullish rejection candle below PDL: {pname}")

        elif (level.pdl_swept and
              level.rejection_candle is not None and
              level.rejection_side == 'below_pdl'):
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

        # ── SCENARIO 2: BREAKOUT ──────────────────────────────────────────────
        # Only when no sweep flags are set (clean break, no rejection)

        if signal is None and not level.pdh_swept and not level.pdl_swept:

            # Bullish breakout: two consecutive closes above PDH, no rejection
            if (not level.pdh_broken and
                    candle['close'] > pdh and
                    level.rejection_candle is None):
                if prev and prev['close'] > pdh:
                    level.pdh_broken = True
                    signal = Signal(
                        symbol=symbol,
                        side='BUY',
                        entry_price=candle['close'],
                        sl_price=pdh * (1 - SL_BUFFER_PCT),
                        scenario='breakout',
                        pattern='Bullish Breakout',
                        pdh=pdh,
                        pdl=pdl
                    )
                    logger.info(f"{symbol} | BULLISH BREAKOUT | Entry:{candle['close']:.4f}")

            # Bearish breakout: two consecutive closes below PDL, no rejection
            elif (not level.pdl_broken and
                    candle['close'] < pdl and
                    level.rejection_candle is None):
                if prev and prev['close'] < pdl:
                    level.pdl_broken = True
                    signal = Signal(
                        symbol=symbol,
                        side='SELL',
                        entry_price=candle['close'],
                        sl_price=pdl * (1 + SL_BUFFER_PCT),
                        scenario='breakout',
                        pattern='Bearish Breakout',
                        pdh=pdh,
                        pdl=pdl
                    )
                    logger.info(f"{symbol} | BEARISH BREAKOUT | Entry:{candle['close']:.4f}")

        self._prev_candles[symbol] = candle
        return signal

    def _candle_near_rejection(self, candle: dict, rejection: dict, side: str) -> bool:
        if side == 'above_pdh':
            distance = abs(candle['close'] - rejection['low']) / rejection['low']
        else:
            distance = abs(candle['close'] - rejection['high']) / rejection['high']
        return distance < 0.005
