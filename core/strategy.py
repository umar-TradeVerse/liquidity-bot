"""
StrategyEngine — Liquidity sweep + trigger-candle reversal strategy.

(unchanged docstring from previous version — sweep -> trigger candle ->
confirm/invalidate logic is identical, not reproduced again here for
brevity of this diff-style file. See prior version for full detail.)
"""

import logging
from typing import Optional
from core.state import BotState, SYMBOLS, REGIME_SYMBOL
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

        # Regime reference (BTC) — fetched separately, never traded.
        # Failure here does NOT fail the daily reset overall; the regime
        # filter just degrades gracefully to NEUTRAL (no filtering) for
        # the day if this fails.
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
        """
        Unchanged from the previous version — sweep -> first bullish/bearish
        trigger candle -> confirm (close breaks trigger high/low) or
        invalidate (close breaks trigger low/high first, no lock, re-arm
        immediately). See prior version for the full commented logic;
        not a single line of this method changed in this update.
        """
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

        if not level.pdh_event_active:
            if level.pdh_state == "NONE":
                if candle['high'] > pdh:
                    level.pdh_state = "SWEPT"
                    logger.info(f"{symbol} | PDH swept (H:{candle['high']:.4f}) — "
                                f"watching for the first bearish trigger candle")

            elif level.pdh_state == "SWEPT":
                if is_bearish:
                    level.pdh_state = "TRIGGERED"
                    level.pdh_trigger = candle
                    logger.info(f"{symbol} | Bearish trigger candle formed | "
                                f"Trigger H:{candle['high']:.4f} L:{candle['low']:.4f}")

            elif level.pdh_state == "TRIGGERED":
                trig = level.pdh_trigger
                if candle['close'] < trig['low']:
                    entry = candle['close']
                    sl = trig['high'] * (1 + SL_BUFFER_PCT)
                    signal = Signal(symbol, 'SELL', entry, sl, pdh, pdl)
                    logger.info(f"{symbol} | SHORT signal (trigger confirmed) | "
                                f"Entry:{entry:.4f} SL:{sl:.4f} (trigger high {trig['high']:.4f})")
                    level.pdh_state = "NONE"
                    level.pdh_trigger = None
                    level.pdh_event_active = True
                elif candle['close'] > trig['high']:
                    logger.info(f"{symbol} | PDH trigger INVALIDATED — close "
                                f"{candle['close']:.4f} broke trigger high {trig['high']:.4f} "
                                f"before trigger low — resuming watch for a fresh sweep")
                    level.pdh_state = "NONE"
                    level.pdh_trigger = None

        if signal is None and not level.pdl_event_active:
            if level.pdl_state == "NONE":
                if candle['low'] < pdl:
                    level.pdl_state = "SWEPT"
                    logger.info(f"{symbol} | PDL swept (L:{candle['low']:.4f}) — "
                                f"watching for the first bullish trigger candle")

            elif level.pdl_state == "SWEPT":
                if is_bullish:
                    level.pdl_state = "TRIGGERED"
                    level.pdl_trigger = candle
                    logger.info(f"{symbol} | Bullish trigger candle formed | "
                                f"Trigger H:{candle['high']:.4f} L:{candle['low']:.4f}")

            elif level.pdl_state == "TRIGGERED":
                trig = level.pdl_trigger
                if candle['close'] > trig['high']:
                    entry = candle['close']
                    sl = trig['low'] * (1 - SL_BUFFER_PCT)
                    signal = Signal(symbol, 'BUY', entry, sl, pdh, pdl)
                    logger.info(f"{symbol} | LONG signal (trigger confirmed) | "
                                f"Entry:{entry:.4f} SL:{sl:.4f} (trigger low {trig['low']:.4f})")
                    level.pdl_state = "NONE"
                    level.pdl_trigger = None
                    level.pdl_event_active = True
                elif candle['close'] < trig['low']:
                    logger.info(f"{symbol} | PDL trigger INVALIDATED — close "
                                f"{candle['close']:.4f} broke trigger low {trig['low']:.4f} "
                                f"before trigger high — resuming watch for a fresh sweep")
                    level.pdl_state = "NONE"
                    level.pdl_trigger = None

        return signal
