"""
BotState — single source of truth for daily levels and per-symbol setup state.
"""
from dataclasses import dataclass, field
from typing import Dict, Optional
from datetime import date
import threading

SYMBOLS = [
    "ETHUSD", "SOLUSD", "XRPUSD", "TAOUSD", "AEROUSD",
    "KAITOUSD",
    "RIFUSD", "ZAMAUSD"
    # DEXEUSD, LTCUSD removed 2026-09-08 per explicit request. Mapping kept
    # in exchange/coindcx.py's SYMBOL_MAP (same treatment as ICPUSD below)
    # so any already-open position on either can still be fetched/
    # monitored/closed properly -- no new signals will ever form for them.
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
    pdh_day_extreme: Optional[float] = None
    # Epoch-ms of the candle that FIRST armed this side into SWEPT state.
    # Used for the 90-minute entry-expiry rule (2026-08-13) — measured from
    # the actual confirmed sweep candle, not from any earlier candle that
    # merely approached the level, per the rule's explicit requirement.
    pdh_sweep_armed_at: Optional[int] = None
    # 2026-08-25 informational: did the sweep candle CLOSE back inside the
    # level (a genuine liquidity hunt — wick pierces, body rejects), or did
    # it close beyond the level (acceptance / breakout)? Captured at arm
    # time and carried through to the signal for alerting only.
    pdh_sweep_closed_inside: Optional[bool] = None
    pdl_state: str = "NONE"
    pdl_trigger: Optional[dict] = None
    pdl_event_active: bool = False
    pdl_sweep_extreme: Optional[float] = None
    pdl_day_extreme: Optional[float] = None
    pdl_sweep_armed_at: Optional[int] = None
    pdl_sweep_closed_inside: Optional[bool] = None
    # Trend bias, set once at the daily reset from the last 3 daily candles.
    # "NONE" = sideways OR a matured/exhausted trend (2+ consecutive same-
    # direction days) — keep the original dual-sided sweep-reversal logic.
    # "DOWNTREND" = a single fresh bearish day — hunt sell-side liquidity
    # (SHORT auto-trades, LONG alert-only).
    # "UPTREND" = a single fresh bullish day — hunt buy-side liquidity
    # (LONG auto-trades, SHORT alert-only).
    trend_bias: str = "NONE"
    # Dynamic re-anchored reference levels used only when trend_bias is set.
    # Seeded from the counter-trend side's own confirmation candle the moment
    # it fires, then tracked exactly like pdh_sweep_extreme/pdl_sweep_extreme.
    trend_ref_high: Optional[float] = None
    trend_ref_low: Optional[float] = None
    # CISD (Change In State of Delivery) reference — the open of the
    # candle immediately preceding the trigger candle. Used as the
    # reclaim reference INSTEAD OF the fixed PDH/PDL when a sweep is
    # "deep" (see DEEP_SWEEP_THRESHOLD_PCT in strategy.py). Added
    # 2026-07-22 after real cases (ICP, KAITO) where demanding a full
    # reclaim back to a fixed level that was already far from the
    # actual reversal cost hours of delay on a genuinely valid entry.
    pdh_cisd_ref: Optional[float] = None
    pdl_cisd_ref: Optional[float] = None
    # Trend Stability counter — counts how many times the COUNTER-trend side
    # has confirmed today. Two or more means the day's trend classification
    # has already been contradicted twice, which is real evidence the
    # classification itself may be wrong or the trend is reversing intraday
    # — not just noise. See monitor.py's STABILITY_MAX_COUNTER_CONFIRMS.
    counter_trend_confirms: int = 0

    # ── 2026-09-12: INSIDE BAR BREAKOUT — a SEPARATE, ADDITIONAL strategy,
    # deliberately distinct from the liquidity-sweep logic above.
    #
    # HONEST RECORD OF THE EVIDENCE, so future-you knows what this was
    # built on. Backtested across 699 day-pairs / 140 real inside bars
    # from this bot's own logged daily candles:
    #     broke DOWN only .......... 46 (33%)
    #     broke UP only ............ 39 (28%)
    #     BOTH sides broken ........ 30 (21%)  <- breakout entry likely loses
    #     never broke .............. 20 (14%)
    # Direction is near a coin flip and the 21% whipsaw rate is a
    # structural drag. This was implemented at explicit request AFTER that
    # analysis was presented and understood -- NOT because the data
    # supported it. Watch it closely; if live results match the backtest,
    # this rule costs money.
    #
    # NOTE ON CONFLICT: this is a CONTINUATION/breakout rule bolted onto a
    # MEAN-REVERSION bot. The two can fire opposite signals on the same
    # symbol. Precedence is handled in monitor.py: the liquidity-sweep
    # signal always wins if both are live, and only ONE inside-bar trade
    # per symbol per day is allowed.
    inside_bar_armed: bool = False        # yesterday was a true inside bar
    inside_bar_high: Optional[float] = None   # breakout level (long trigger)
    inside_bar_low: Optional[float] = None    # breakdown level (short trigger)
    inside_bar_traded_today: bool = False     # one attempt per symbol per day


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
                level.pdh_sweep_armed_at = None
                level.pdh_sweep_closed_inside = None
                level.pdl_state = "NONE"
                level.pdl_trigger = None
                level.pdl_sweep_extreme = None
                level.pdl_sweep_armed_at = None
                level.pdl_sweep_closed_inside = None
                # trend_bias / trend_ref_high / trend_ref_low deliberately NOT
                # reset here — they're a daily classification, not per-trade
                # state, and must persist across position closes within the
                # same day.

    def levels_ready(self) -> bool:
        return bool(self.levels) and all(v is not None for v in self.levels.values())
