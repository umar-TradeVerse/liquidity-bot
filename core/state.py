"""
BotState — single source of truth for daily levels and per-symbol setup state.
"""
from dataclasses import dataclass, field
from typing import Dict, Optional
from datetime import date
import threading

SYMBOLS = [
    "ETHUSD", "SOLUSD", "XRPUSD", "TAOUSD", "AEROUSD",
    "LTCUSD",
    "AAVEUSD", "ICPUSD", "KAITOUSD"
]

REGIME_SYMBOL = "BTCUSD"


@dataclass
class DailyLevel:
    pdh: float
    pdl: float

    in_trade: bool = False
    auto_traded_today: bool = False

    pdh_state: str = "NONE"
    pdh_trigger: Optional[dict] = None
    pdh_event_active: bool = False
    pdh_sweep_extreme: Optional[float] = None

    pdl_state: str = "NONE"
    pdl_trigger: Optional[dict] = None
    pdl_event_active: bool = False
    pdl_sweep_extreme: Optional[float] = None


@dataclass
class RegimeLevel:
    pdh: float
    pdl: float
    last_close: Optional[float] = None
    regime: str = "NEUTRAL"


@dataclass
class TradeRecord:
    symbol: str
    side: str
    entry_price: float
    sl_price: float
    order_id: str
    scenario: str
    timestamp: str
    status: str = "open"


class BotState:
    def __init__(self):
        self._lock = threading.Lock()
        self.levels: Dict[str, DailyLevel] = {}
        self.regime: Optional[RegimeLevel] = None
        self.trade_records: list[TradeRecord] = []
        self.paused: bool = False
        self.today: date = date.today()

    def reset_daily(self):
        with self._lock:
            self.levels = {sym: None for sym in SYMBOLS}
            self.regime = None
            self.trade_records = []
            self.paused = False
            self.today = date.today()

    def set_levels(self, symbol: str, pdh: float, pdl: float):
        with self._lock:
            self.levels[symbol] = DailyLevel(pdh=pdh, pdl=pdl)

    def get_level(self, symbol: str) -> Optional[DailyLevel]:
        return self.levels.get(symbol)

    def set_regime_levels(self, pdh: float, pdl: float):
        with self._lock:
            self.regime = RegimeLevel(pdh=pdh, pdl=pdl)

    def update_regime_price(self, close: float):
        with self._lock:
            if not self.regime:
                return
            self.regime.last_close = close
            if close > self.regime.pdh:
                self.regime.regime = "BULLISH"
            elif close < self.regime.pdl:
                self.regime.regime = "BEARISH"
            else:
                self.regime.regime = "NEUTRAL"

    def get_regime(self) -> str:
        with self._lock:
            return self.regime.regime if self.regime else "NEUTRAL"

    def register_trade(self, record: TradeRecord):
        with self._lock:
            self.trade_records.append(record)

    def mark_in_trade(self, symbol: str):
        with self._lock:
            if self.levels.get(symbol):
                self.levels[symbol].in_trade = True

    def mark_auto_traded(self, symbol: str):
        with self._lock:
            if self.levels.get(symbol):
                self.levels[symbol].auto_traded_today = True

    def reset_symbol_watch(self, symbol: str):
        with self._lock:
            level = self.levels.get(symbol)
            if level:
                level.in_trade = False
                level.pdh_state = "NONE"
                level.pdh_trigger = None
                level.pdh_sweep_extreme = None
                level.pdl_state = "NONE"
                level.pdl_trigger = None
                level.pdl_sweep_extreme = None

    def levels_ready(self) -> bool:
        return bool(self.levels) and all(v is not None for v in self.levels.values())
