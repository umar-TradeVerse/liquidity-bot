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
    "DOTUSD", "LTCUSD", "BNBUSD", "TRXUSD", "SUIUSD"
]


@dataclass
class DailyLevel:
    pdh: float
    pdl: float
    # Scenario tracking — once one fires, ignore second signal for same symbol
    scenario_fired: bool = False
    # Breakout tracking
    pdh_broken: bool = False
    pdl_broken: bool = False
    # Sweep tracking
    pdh_swept: bool = False  # price closed above PDH
    pdl_swept: bool = False  # price closed below PDL
    # Rejection candle tracking
    rejection_candle: Optional[dict] = None  # {high, low, open, close, time}
    rejection_side: Optional[str] = None     # 'above_pdh' or 'below_pdl'


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

    def set_rejection_candle(self, symbol: str, candle: dict, side: str):
        with self._lock:
            if self.levels.get(symbol):
                self.levels[symbol].rejection_candle = candle
                self.levels[symbol].rejection_side = side

    def clear_rejection_candle(self, symbol: str):
        with self._lock:
            if self.levels.get(symbol):
                self.levels[symbol].rejection_candle = None
                self.levels[symbol].rejection_side = None

    def levels_ready(self) -> bool:
        return bool(self.levels) and all(v is not None for v in self.levels.values())
