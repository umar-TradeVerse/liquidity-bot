"""
BotState — single source of truth for daily levels and per-symbol setup state.
"""
from dataclasses import dataclass, field
from typing import Dict, Optional
from datetime import date
import threading

SYMBOLS = [
    "ETHUSD", "SOLUSD", "XRPUSD", "TAOUSD", "AEROUSD",
    "BTCUSD", "LTCUSD",
    "AAVEUSD", "ICPUSD", "KAITOUSD"
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

    # auto_traded_today = True once ONE automatic trade has been placed for
    # this symbol today (win or SL, doesn't matter). Once True, any further
    # clean setup on this symbol today is alert-only — never auto-placed.
    # Reset only by the next day's fresh DailyLevel (via reset_daily).
    auto_traded_today: bool = False

    # ── PDH side (buy-side sweep -> bearish reversal / SHORT) ──
    # NONE      -> no candle has broken above PDH yet
    # SWEPT     -> a candle's high broke above PDH; watching for the first
    #              bearish (close < open) candle to become the Trigger Candle
    # TRIGGERED -> a bearish Trigger Candle is set; watching for a later
    #              candle's close to break the trigger's low (entry) or
    #              its high (invalidation)
    pdh_state: str = "NONE"
    pdh_trigger: Optional[dict] = None  # the Trigger Candle dict

    # Once a trade has FIRED from this side (executed or alert-only per
    # Rule 3), this locks the side entirely: no new sweep/trigger detection
    # until a candle closes back on the other side of PDH. This stops one
    # continuous excursion beyond the level from being re-sliced into
    # multiple "fresh" trades every time a previous one closes.
    # NOTE: this lock is NOT set on invalidation (Trigger Low broken before
    # Trigger High) — per the strategy, invalidation should immediately
    # resume watching for a fresh sweep, even if price is still beyond PDH.
    pdh_event_active: bool = False

    # ── PDL side (sell-side sweep -> bullish reversal / LONG), mirrored ──
    pdl_state: str = "NONE"
    pdl_trigger: Optional[dict] = None
    pdl_event_active: bool = False

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

    def mark_auto_traded(self, symbol: str):
        """Call once an order has actually been placed for this symbol.
        Marks today's one-auto-trade-per-symbol allowance as used —
        stays True for the rest of the day regardless of position
        closes/reopens, unlike in_trade."""
        with self._lock:
            if self.levels.get(symbol):
                self.levels[symbol].auto_traded_today = True

    def reset_symbol_watch(self, symbol: str):
        """Call once a live position check confirms this symbol's position
        has closed. Clears in_trade AND resets both sides' state machines
        back to NONE, so the symbol starts a completely fresh setup
        lifecycle. Does NOT clear auto_traded_today — that persists for
        the rest of the day per Rule 3."""
        with self._lock:
            level = self.levels.get(symbol)
            if level:
                level.in_trade = False
                level.pdh_state = "NONE"
                level.pdh_trigger = None
                level.pdl_state = "NONE"
                level.pdl_trigger = None

    def levels_ready(self) -> bool:
        return bool(self.levels) and all(v is not None for v in self.levels.values())
