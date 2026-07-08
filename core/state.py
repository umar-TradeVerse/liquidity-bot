"""
BotState — single source of truth for daily levels and per-symbol setup state.
"""
from dataclasses import dataclass, field
from typing import Dict, Optional
from datetime import date
import threading

SYMBOLS = [
    "ETHUSD", "SOLUSD", "XRPUSD", "TAOUSD", "AEROUSD",
    "BTCUSD", "DOGEUSD", "ADAUSD", "LINKUSD",
    "LTCUSD", "TRXUSD", "SUIUSD",
    "AAVEUSD", "ICPUSD", "NEARUSD", "RENDERUSD", "KAITOUSD"
]


@dataclass
class DailyLevel:
    pdh: float
    pdl: float

    # in_trade = True once a signal from this symbol has actually resulted in
    # a placed order. While True, no new signals are generated for this
    # symbol at all (either side). Cleared by BotState.reset_symbol_watch()
    # once a live position check confirms the position has closed.
    in_trade: bool = False

    # ── PDH side (buy-side sweep -> bearish reversal / SHORT) ──
    # NONE     -> no candle has broken above PDH yet
    # TRACKING -> a reference candle exists; the next candle either enters
    #             (low breaks reference's low, confirmed by CLOSE) or
    #             abandons this reference entirely (high breaks reference's
    #             high, treated as continuation, not reversal)
    pdh_state: str = "NONE"
    pdh_reference: Optional[dict] = None  # the current reference candle dict

    # Persistent extreme across the WHOLE continuous sweep sequence for
    # this side — survives abandon/re-reference cycles, only cleared once
    # a trade actually confirms (or the day resets). Used for SL instead
    # of just the latest (possibly shallower) reference, so the stop
    # reflects the true extent of the sweep, not just its final leg.
    pdh_session_high: Optional[float] = None

    # ── PDL side (sell-side sweep -> bullish reversal / LONG), mirrored ──
    pdl_state: str = "NONE"
    pdl_reference: Optional[dict] = None
    pdl_session_low: Optional[float] = None

    # Pure awareness — never affects trading. Set True once we've sent one
    # "hovering near the level without breaking it" alert for this side
    # today, so we don't spam the same near-miss every 15 minutes.
    pdh_near_alerted: bool = False
    pdl_near_alerted: bool = False


@dataclass
class TradeRecord:
    symbol: str
    side: str           # 'BUY' or 'SELL'
    entry_price: float
    sl_price: float
    order_id: str
    scenario: str       # 'sweep_reversal'
    timestamp: str
    status: str = "open"  # open / sl_hit / manual_close


class BotState:
    def __init__(self):
        self._lock = threading.Lock()
        self.levels: Dict[str, DailyLevel] = {}
        self.trade_records: list[TradeRecord] = []
        self.paused: bool = False
        self.today: date = date.today()

    def reset_daily(self):
        with self._lock:
            self.levels = {sym: None for sym in SYMBOLS}
            self.trade_records = []
            self.paused = False
            self.today = date.today()

    def set_levels(self, symbol: str, pdh: float, pdl: float):
        with self._lock:
            self.levels[symbol] = DailyLevel(pdh=pdh, pdl=pdl)

    def get_level(self, symbol: str) -> Optional[DailyLevel]:
        return self.levels.get(symbol)

    def register_trade(self, record: TradeRecord):
        with self._lock:
            self.trade_records.append(record)

    def mark_in_trade(self, symbol: str):
        """Call once an order has actually been placed for this symbol.
        Blocks new signals on this symbol (both sides) until
        reset_symbol_watch() is called after the position closes."""
        with self._lock:
            if self.levels.get(symbol):
                self.levels[symbol].in_trade = True

    def reset_symbol_watch(self, symbol: str):
        """Call once a live position check confirms this symbol's position
        has closed. Clears in_trade AND resets both sides' state machines
        back to NONE, so the symbol starts a completely fresh setup
        lifecycle."""
        with self._lock:
            level = self.levels.get(symbol)
            if level:
                level.in_trade = False
                level.pdh_state = "NONE"
                level.pdh_reference = None
                level.pdl_state = "NONE"
                level.pdl_reference = None

    def levels_ready(self) -> bool:
        return bool(self.levels) and all(v is not None for v in self.levels.values())
