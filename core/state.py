"""
BotState — single source of truth for daily levels and per-symbol setup state.
"""
from dataclasses import dataclass, field
from typing import Dict, Optional
from datetime import date
import threading

# BTCUSD is intentionally excluded — it's traded in a separate strategy.
# It is still fetched/monitored (see REGIME_SYMBOL below) purely as a
# read-only regime reference for the other symbols; never scanned for
# its own setups, never traded here.
SYMBOLS = [
    "ETHUSD", "SOLUSD", "XRPUSD", "TAOUSD", "AEROUSD",
    "LTCUSD",
    "AAVEUSD", "ICPUSD", "KAITOUSD"
]

# Read-only regime reference symbol — provides directional bias for the
# other symbols' LONG/SHORT signals. Never traded by this bot.
REGIME_SYMBOL = "BTCUSD"


@dataclass
class DailyLevel:
    pdh: float
    pdl: float

    # in_trade = True once an order has actually been placed for this
    # symbol. Blocks new signals until reset_symbol_watch() runs after
    # the position closes (via SL, or via one of the exit-priority checks).
    in_trade: bool = False

    # auto_traded_today = True once ONE automatic trade has been placed
    # for this symbol today (any outcome). Further clean setups today
    # are alert-only.
    auto_traded_today: bool = False

    # ── PDH side (buy-side sweep -> bearish reversal / SHORT) ──
    pdh_state: str = "NONE"
    pdh_trigger: Optional[dict] = None
    pdh_event_active: bool = False

    # ── PDL side (sell-side sweep -> bullish reversal / LONG), mirrored ──
    pdl_state: str = "NONE"
    pdl_trigger: Optional[dict] = None
    pdl_event_active: bool = False


@dataclass
class RegimeLevel:
    """
    Read-only daily PDH/PDL for the regime reference symbol (BTCUSD),
    plus the latest observed classification. Never used to trade BTCUSD
    itself — only to bias LONG/SHORT decisions on the other symbols.
    """
    pdh: float
    pdl: float
    last_close: Optional[float] = None
    # "BULLISH" -> latest close above PDH -> blocks new SHORT auto-entries elsewhere
    # "BEARISH" -> latest close below PDL -> blocks new LONG auto-entries elsewhere
    # "NEUTRAL" -> between PDL and PDH, or not yet established -> no filtering
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
    # open / target_achieved / rejection_exit / roe_protection / sl_hit / manual_close
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
        """Call with BTC's latest closed-candle close price to refresh
        the regime classification."""
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
        """Defaults to NEUTRAL (no filtering) if regime data isn't
        established yet, so a startup gap never silently blocks trading."""
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
                level.pdl_state = "NONE"
                level.pdl_trigger = None

    def levels_ready(self) -> bool:
        return bool(self.levels) and all(v is not None for v in self.levels.values())
