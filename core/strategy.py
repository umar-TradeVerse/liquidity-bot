"""
StrategyEngine — Liquidity sweep + trigger-candle reversal strategy.

Objective: not every breakout is traded. Only a liquidity sweep followed
by a genuine rejection (trigger) candle, confirmed by a later candle
breaking the trigger's range, qualifies.

BUY-SIDE SETUP (sweep of PDL -> bullish reversal -> LONG):
  1. Liquidity Sweep — a candle's low trades below PDL. This candle is
     NOT the entry candle and NOT the reference. It only marks that a
     sweep has occurred; the bot keeps monitoring.
  2. Trigger Candle — the FIRST candle after the sweep whose close is
     bullish (close > open) becomes the Trigger Candle. It does not need
     to touch PDL again. Trigger High / Trigger Low = that candle's
     high/low — this is the only reference from here on.
  3. Entry — only when a later candle's CLOSE breaks above Trigger High.
  4. Stop Loss — just below Trigger Low (not the sweep candle, not PDL).
  5. Invalidation — if a candle's CLOSE breaks below Trigger Low before
     any candle closes above Trigger High, the setup is scrapped. The
     bot immediately resumes watching for a completely fresh sweep (even
     if price is still below PDL) — this is not locked out.

SELL-SIDE SETUP (sweep of PDH -> bearish reversal -> SHORT): mirrored
exactly, using a bearish (close < open) Trigger Candle.

Once a signal actually FIRES (auto-traded or alert-only per Rule 3),
that side locks (event_active) until a candle closes back on the other
side of the level — this stops one continuous excursion beyond PDH/PDL
from being re-sliced into multiple "fresh" trades. This lock does NOT
apply to invalidation, which is meant to re-arm immediately.

No indicators, no pattern matching, no proximity filters. If a symbol
never produces a trigger candle, or the trigger never gets confirmed,
it simply doesn't trade — a valid, expected outcome, not an error.

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
        Returns a Signal only when a Trigger Candle's high (SHORT side, via
        a break below its low) or low (LONG side, via a break above its
        high) is confirmed by a later candle's CLOSE. Returns None
        otherwise — including while sweeping/waiting for a trigger, or
        while a trigger is set and neither confirmed nor invalidated yet,
        which is normal and can persist for as long as it takes.
        """
        level = self.state.get_level(symbol)
        if not level or level.in_trade:
            return None

        pdh = level.pdh
        pdl = level.pdl
        signal = None
        is_bullish = candle['close'] > candle['open']
        is_bearish = candle['close'] < candle['open']

        # Clear the lock once price genuinely returns to the normal range.
        # Only then is this side eligible for a brand new, independent sweep.
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
                # else: no bearish candle yet — keep waiting in SWEPT

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
                    level.pdh_event_active = True  # lock — no new PDH signal until price closes back below PDH
                elif candle['close'] > trig['high']:
                    logger.info(f"{symbol} | PDH trigger INVALIDATED — close "
                                f"{candle['close']:.4f} broke trigger high {trig['high']:.4f} "
                                f"before trigger low — resuming watch for a fresh sweep")
                    level.pdh_state = "NONE"
                    level.pdh_trigger = None
                    # no event_active lock — re-arm immediately, per spec
                # else: neither breached — keep waiting with the same trigger

        # ══════════════════════════════════════════════════════════════
        # PDL SIDE — sweep of PDL -> bullish trigger -> LONG
        # Only evaluated if PDH side didn't already fire a signal this candle.
        # ══════════════════════════════════════════════════════════════
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
                # else: no bullish candle yet — keep waiting in SWEPT

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
                    level.pdl_event_active = True  # lock — no new PDL signal until price closes back above PDL
                elif candle['close'] < trig['low']:
                    logger.info(f"{symbol} | PDL trigger INVALIDATED — close "
                                f"{candle['close']:.4f} broke trigger low {trig['low']:.4f} "
                                f"before trigger high — resuming watch for a fresh sweep")
                    level.pdl_state = "NONE"
                    level.pdl_trigger = None
                    # no event_active lock — re-arm immediately, per spec
                # else: neither breached — keep waiting with the same trigger

        return signal
