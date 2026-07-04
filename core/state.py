"""
BotState — single source of truth for daily counters, levels, and trade tracking.
"""
from dataclasses import dataclass, field
from typing import Dict, Optional
from datetime import date
import threading

SYMBOLS = [
    "ETHUSD", "SOLUSD", "XRPUSD", "TAOUSD", "AEROUSD",
    "BTCUSD", "DOGEUSD", "ADAUSD", "LINKUSD", "AVAXUSD",
    "DOTUSD", "LTCUSD", "TRXUSD", "SUIUSD"
]


@dataclass
class DailyLevel:
    pdh: float
    pdl: float

    # Set to True after ANY signal fires for this symbol (sweep or breakout).
    # Once True, no further signals are generated for this symbol until the
    # next 5:30 AM daily reset. This enforces "one outcome per symbol per day."
    scenario_fired: bool = False

    # ── PDH side state machine ──
    # NONE     -> no candle has closed above PDH yet
    # SWEPT    -> a candle wicked above PDH but closed above it too (no rejection yet, watching next candle)
    # REJECTED -> a candle closed back below PDH after a sweep (watching for confirmation break of its low)
    pdh_state: str = "NONE"
    pdh_rejection: Optional[dict] = None  # the candle dict that formed the rejection

    # ── PDL side state machine (mirrored) ──
    pdl_state: str = "NONE"
    pdl_rejection: Optional[dict] = None


@dataclass
class TradeRecord:
    symbol: str
    side: str           # 'BUY' or 'SELL'
    entry_price: float
    sl_price: float
    order_id: str
    scenario: str       # 'sweep_reversal' or 'breakout'
    timestamp: str
    status: str = "open"  # open / sl_hit / manual_close


class BotState:
    def __init__(self):
        self._lock = threading.Lock()
        self.levels: Dict[str, DailyLevel] = {}
        self.trades_today: int = 0
        self.trade_records: list[TradeRecord] = []
        self.paused: bool = False
        self.today: date = date.today()

    def reset_daily(self):
        with self._lock:
            self.levels = {sym: None for sym in SYMBOLS}
            self.trades_today = 0
            self.trade_records = []
            self.paused = False
            self.today = date.today()

    def set_levels(self, symbol: str, pdh: float, pdl: float):
        with self._lock:
            self.levels[symbol] = DailyLevel(pdh=pdh, pdl=pdl)

    def get_level(self, symbol: str) -> Optional[DailyLevel]:
        return self.levels.get(symbol)

    def can_trade(self) -> bool:
        return self.trades_today < 2 and not self.paused

    def register_trade(self, record: TradeRecord):
        with self._lock:
            self.trades_today += 1
            self.trade_records.append(record)

    def mark_scenario_fired(self, symbol: str):
        with self._lock:
            if self.levels.get(symbol):
                self.levels[symbol].scenario_fired = True

    def levels_ready(self) -> bool:
        return bool(self.levels) and all(v is not None for v in self.levels.values())
