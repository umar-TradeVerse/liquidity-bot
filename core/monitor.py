from core.trading_common import (  # explicit, so lint can catch undefined names
    BotState,
    CoinDCXClient,
    DAY_END_HOUR,
    DAY_END_MINUTE,
    ENTRY_EXPIRY_MINUTES,
    IST,
    LiquidityMapAgent,
    POLL_INTERVAL_SECONDS,
    PatternAgent,
    PoolAgent,
    REGIME_SYMBOL,
    RiskAgent,
    SYMBOLS,
    StrategyEngine,
    TRADE_LEVERAGE,
    TelegramBot,
    asyncio,
    compute_obi,
    datetime,
    dtime,
    logger,
    os,
    persistence,
    summarise_book,
)
# Re-export every shared name so code importing from core.monitor keeps
# working (main.py, tests, agents). Done without 'import *' so lint can
# still detect undefined names in this file.
import core.trading_common as _common
globals().update({_n: getattr(_common, _n) for _n in _common.__all__ if _n not in globals()})
from core.execution import ExecutionMixin
from core.candle_store import CandleStore
from core.agents.breakout_agent import BreakoutAgent
from core.candle_patterns import classify as classify_candle
from core.orderflow import VolumeTracker, TradeFlow, volume_profile, fmt_flow, fmt_vp
from core.trade_manager import TradeManagerMixin


class MarketMonitor(ExecutionMixin, TradeManagerMixin):
    def __init__(self, coindcx: CoinDCXClient, engine: StrategyEngine,
                 state: BotState, telegram: TelegramBot):
        self.coindcx = coindcx
        self.engine = engine
        self.state = state
        self.telegram = telegram
        self._last_candle_time = {sym: None for sym in SYMBOLS}
        self._last_candle: dict = {}
        self._last_regime_candle_time = None
        self._open_positions: dict = {}
        self._trailing: dict = {}
        self.risk = RiskAgent()   # multi-agent: owns daily loss halt
        self._offhours_logged = False
        # Liquidity Pool Agent (equal highs/lows, session highs/lows) — separate sub-agent
        # One shared 15m candle history for both sub-agents (2026-10-02)
        self.candles = CandleStore(coindcx)
        self.pool_agent = PoolAgent(coindcx, float(os.getenv('TRADE_SIZE_USD', 40)), TRADE_LEVERAGE,
                                    store=self.candles)
        # Liquidity Map Agent (proven-level sweeps) — separate sub-agent
        self.liqmap = LiquidityMapAgent(coindcx, store=self.candles)
        # Order-flow measurements (2026-10-03, log-only): volume, volume profile, delta/CVD
        self.vol = VolumeTracker()
        self.flow = TradeFlow(coindcx)
        self._last_flow: dict = {}
        # Breakout Sub-Agent (2026-10-03) — PAPER TRADING ONLY, never places orders
        self.breakout = BreakoutAgent(self.candles, float(os.getenv('TRADE_SIZE_USD', 40)), TRADE_LEVERAGE)
        self.pattern = PatternAgent()   # multi-agent: pattern memory (shadow mode)
        self._last_obi: dict = {}       # latest OBI per symbol, for pattern features
        self._position_lock = asyncio.Lock()
        self._loops_since_save = 0
        self._SAVE_EVERY_N_LOOPS = 4  # ~1 minute at POLL_INTERVAL_SECONDS=15
        self._saved_open = None       # open symbols in the last saved snapshot
        self._orphan_alerted: set = set()  # symbols already flagged this run —


    def restore_trailing(self, trailing: dict):
        """Called once from main.py at startup if a same-day state snapshot
        was found. Restores in-flight SL/TP/MFE/MAE tracking so the exit
        logic (_check_exit_conditions) resumes correctly after a restart,
        instead of going dark on any position that was open when the
        process stopped."""
        self._trailing = trailing or {}
        if self._trailing:
            logger.info(f"Restored trailing state for: {list(self._trailing.keys())}")


    def _save_if_positions_changed(self):
        """2026-10-01 fix: save the snapshot IMMEDIATELY whenever a position
        opens or closes. Previously a trade that closed overnight left the
        old snapshot (still showing it open) on disk; each later restart
        'rediscovered' the close and booked its P&L again."""
        now_open = sorted(self._trailing.keys())
        if now_open != self._saved_open:
            persistence.save_state(self.state, self._trailing)
            self._saved_open = now_open
            self._loops_since_save = 0


    def _is_trading_hours(self) -> bool:
        now = datetime.now(IST)
        return dtime(5, 30) <= now.time() <= dtime(DAY_END_HOUR, DAY_END_MINUTE)


    async def run(self):
        logger.info("Market monitor started")
        while True:
            try:
                if not self.state.levels_ready() or self.state.paused:
                    await asyncio.sleep(30)
                    continue

                if not self._is_trading_hours():
                    # 2026-09-28 (A): outside ENTRY hours (23:00–05:30 IST)
                    # open positions are still fully managed -- breakeven,
                    # trend trail, TP ladder, early failure, reconciliation.
                    # Only NEW entries stop. Previously the bot did nothing
                    # overnight and open trades relied solely on the
                    # exchange-side SL.
                    if self._trailing:
                        if not self._offhours_logged:
                            logger.info(f"Outside entry hours (23:00–05:30 IST) — managing "
                                        f"{len(self._trailing)} open position(s): "
                                        f"{list(self._trailing.keys())} — no new entries")
                            self._offhours_logged = True
                        await self._reconcile_positions()
                        tasks = [self._process_symbol(sym, entries_allowed=False)
                                 for sym in list(self._trailing.keys())]
                        await asyncio.gather(*tasks, return_exceptions=True)
                        self._loops_since_save += 1
                        if self._loops_since_save >= self._SAVE_EVERY_N_LOOPS:
                            persistence.save_state(self.state, self._trailing)
                            self._loops_since_save = 0
                        self._save_if_positions_changed()
                        await asyncio.sleep(POLL_INTERVAL_SECONDS)
                    else:
                        self._save_if_positions_changed()
                        await asyncio.sleep(60)
                    continue
                self._offhours_logged = False

                await self._reconcile_positions()
                await self._update_regime()

                # Union with self._trailing.keys(): if a symbol was removed
                # from SYMBOLS (watchlist restructuring) while it still had
                # an open position, it must keep being monitored until that
                # position actually closes — otherwise its breakeven-move,
                # TP ladder, and exit logic silently stop, and the position
                # becomes invisible to reconciliation even though it's still
                # live on the exchange. New signals still can't form for it
                # (strategy.py only evaluates symbols in SYMBOLS), so this
                # only affects winding down what's already open, never
                # opens anything new.
                active_symbols = set(SYMBOLS) | set(self._trailing.keys())
                tasks = [self._process_symbol(sym) for sym in active_symbols]
                await asyncio.gather(*tasks, return_exceptions=True)

                self._loops_since_save += 1
                if self._loops_since_save >= self._SAVE_EVERY_N_LOOPS:
                    persistence.save_state(self.state, self._trailing)
                    self._loops_since_save = 0
                self._save_if_positions_changed()

            except Exception as e:
                logger.error(f"Monitor loop error: {e}", exc_info=True)

            await asyncio.sleep(POLL_INTERVAL_SECONDS)


    async def _update_regime(self):
        try:
            candle = await self.coindcx.get_latest_15m_candle(REGIME_SYMBOL)
            if not candle or self._last_regime_candle_time == candle['time']:
                return
            self._last_regime_candle_time = candle['time']
            self.state.update_regime_price(candle['close'])
            logger.info(f"{REGIME_SYMBOL} (regime ref) | Close: {candle['close']:.4f} | "
                       f"Regime now: {self.state.get_regime()}")
        except Exception as e:
            logger.error(f"{REGIME_SYMBOL} (regime ref) | update error: {e}", exc_info=True)


    def _update_flow(self, symbol: str, candle: dict) -> str:
        """2026-10-03 order flow (log-only): relative volume, delta/CVD for this
        candle, and today's volume profile. Never raises."""
        try:
            hist = self.candles.hist.get(symbol, [])
            self.vol.seed(symbol, hist)
            vol = float(candle.get("volume") or 0)
            rvol = self.vol.add(symbol, vol)
            d = self.flow.candle(symbol, candle["time"])
            day_start = int(candle["time"]) // 86_400_000 * 86_400_000
            today = [c for c in hist if c["time"] >= day_start and c["time"] < int(candle["time"])]
            today.append({"high": candle["high"], "low": candle["low"], "volume": vol})
            vp = volume_profile(today)
            self._last_flow[symbol] = {"vol": vol, "rvol": rvol, "delta": d, "vp": vp}
            return fmt_flow(rvol, vol, d)
        except Exception as e:
            logger.warning(f"{symbol} | order-flow summary error ({e})")
            return "flow n/a"

    async def _log_obi(self, symbol: str, side: str, candle: dict, sweep_extreme):
        """2026-09-03 informational-only. See the wiring comment in
        _process_symbol for what this does and does not affect."""
        try:
            # depth=20 (was 5): OBI is still computed on the top 5 levels,
            # identical to before; the deeper book feeds walls/gaps.
            book = await self.coindcx.get_orderbook(symbol, depth=20)
        except Exception as e:
            logger.error(f"{symbol} | OBI fetch failed: {e}", exc_info=True)
            return
        if not book:
            return  # get_orderbook already logged why

        obi = compute_obi(book['bids'], book['asks'], levels=5)
        if obi is None:
            return

        self._last_obi[symbol] = obi
        sweep_str = f"{sweep_extreme:.4f}" if sweep_extreme is not None else "n/a"
        logger.info(f"{symbol} | OBI at {side} sweep-arm (extreme {sweep_str}): "
                   f"{obi:+.3f} (top-5 levels) | {summarise_book(book['bids'], book['asks'])}"
                   f" | {fmt_vp((self._last_flow.get(symbol) or {}).get('vp'), candle['close'])}")

        # 2026-09-08: Telegram alert removed per explicit request -- this
        # stays log-only. Still fully recorded in Railway logs for later
        # analysis, just no longer pings the phone for an unproven tracker.

    async def _process_symbol(self, symbol: str, entries_allowed: bool = True):
        try:
            candle = await self.coindcx.get_latest_15m_candle(symbol)
            if not candle:
                logger.debug(f"{symbol} | No candle data")
                return

            # Order flow: pull recent trades every loop so each candle's delta is complete
            try:
                await self.flow.poll(symbol)
            except Exception as e:
                logger.warning(f"{symbol} | order-flow poll error ({e})")

            if self._last_candle_time[symbol] == candle['time']:
                logger.debug(f"{symbol} | Candle already processed")
                return

            prev_candle = self._last_candle.get(symbol)
            self._last_candle_time[symbol] = candle['time']
            self._last_candle[symbol] = candle
            # 2026-10-03: candlestick pattern appended to every candle line (log-only)
            self._last_pattern = getattr(self, "_last_pattern", {})
            self._last_pattern[symbol] = classify_candle(candle, prev_candle)
            flow_txt = self._update_flow(symbol, candle)
            logger.info(f"{symbol} | Candle: O={candle['open']:.4f} H={candle['high']:.4f} "
                       f"L={candle['low']:.4f} C={candle['close']:.4f} | pattern: {self._last_pattern[symbol]}"
                       f" | {flow_txt}")

            if symbol in self._trailing:
                await self._check_exit_conditions(symbol, candle, prev_candle)

            if not entries_allowed:
                # Off-hours: manage the open trade only. The strategy engine
                # does not see this candle, exactly as before, so no setups
                # arm overnight and entry behaviour is unchanged.
                return

            signal = self.engine.process_candle(symbol, candle)

            # 2026-09-03: OBI (Order Book Imbalance) tracker — INFORMATIONAL
            # ONLY, exactly like the entry-drift and hunt/breakout trackers
            # before it. Fires once, at the exact candle a sweep freshly
            # arms (armed_at == this candle's time), fetches order book
            # depth, computes OBI, and alerts it. Never gates a trade,
            # never touches level state, never blocks the signal path
            # above or below this line. If the endpoint doesn't return
            # usable data (see get_orderbook docstring for why that's a
            # real possibility), it fails silently and trading continues
            # completely unaffected.
            level = self.state.get_level(symbol)
            if level:
                for side, armed_field, sweep_field in (
                    ('PDH', 'pdh_sweep_armed_at', 'pdh_sweep_extreme'),
                    ('PDL', 'pdl_sweep_armed_at', 'pdl_sweep_extreme'),
                ):
                    if getattr(level, armed_field, None) == candle['time']:
                        await self._log_obi(symbol, side, candle,
                                             getattr(level, sweep_field, None))

            # Rule 4 (2026-08-13) expiry alerts — drained here rather than
            # via the Signal return path, since an expiry was never a
            # confirmed entry. Filtered to this symbol since the engine's
            # queue is shared across all symbols processed concurrently.
            if self.engine.pending_expiry_alerts:
                remaining = []
                for ev in self.engine.pending_expiry_alerts:
                    if ev['symbol'] == symbol:
                        await self.telegram.send_alert(
                            f"⏱️ *Setup Expired — Entry Not Triggered in Time*\n\n"
                            f"*Symbol:* {symbol}\n*Side:* {ev['side']}\n"
                            f"*Elapsed:* {ev['elapsed_minutes']} min "
                            f"(limit: {ENTRY_EXPIRY_MINUTES} min)\n\n"
                            f"No entry confirmed within the window — waiting for a "
                            f"completely fresh liquidity sweep."
                        )
                    else:
                        remaining.append(ev)
                self.engine.pending_expiry_alerts = remaining

            if signal:
                await self._handle_signal(signal)

            # ── Liquidity Pool Agent (separate sub-agent, 2026-09-29) ──────
            # Runs AFTER the main strategy, in its own try, so it can never
            # break or delay the existing flow. Live pool signals go through
            # the same full Risk -> Context -> Pattern -> INR -> execution
            # chain as main-strategy signals.
            try:
                pool_signals = await self.pool_agent.on_candle(
                    symbol, candle, self.state.levels.get(symbol))
                for psig in pool_signals:
                    if symbol in self._trailing:
                        logger.info(f"{symbol} | POOL signal skipped — symbol already in a trade")
                        break
                    await self._handle_signal(psig)
            except Exception as e:
                logger.error(f"{symbol} | Pool agent error (main flow unaffected): {e}", exc_info=True)

            # ── Liquidity Map Agent (separate sub-agent, 2026-09-30) ───────
            # Same isolation as the pool agent: own try, never affects the
            # main flow; live signals use the full safety chain.
            try:
                for msig in await self.liqmap.on_candle(symbol, candle, self.state.levels.get(symbol)):
                    if symbol in self._trailing:
                        logger.info(f"{symbol} | LIQMAP signal skipped — symbol already in a trade")
                        break
                    await self._handle_signal(msig)
            except Exception as e:
                logger.error(f"{symbol} | Liquidity map error (main flow unaffected): {e}", exc_info=True)

            # ── Breakout Sub-Agent (paper only, 2026-10-03) ─────────────────
            # Own try: can never affect the liquidity strategies. It has no
            # exchange access and only records what it WOULD have traded.
            try:
                await self.breakout.on_candle(symbol, candle, self.state.levels.get(symbol),
                                              self._last_flow.get(symbol))
            except Exception as e:
                logger.error(f"{symbol} | Breakout agent error (liquidity strategies unaffected): {e}", exc_info=True)

        except Exception as e:
            logger.error(f"{symbol} | _process_symbol error: {e}", exc_info=True)
