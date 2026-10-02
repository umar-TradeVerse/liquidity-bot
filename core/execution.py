"""Execution Agent — turns an approved signal into a real position and closes it:
order placement, SL/TP registration, exits, reconciliation, close logging.

Moved verbatim out of core/monitor.py on 2026-10-02 (no logic changes)."""
from core.trading_common import (  # explicit, so lint can catch undefined names
    DAILY_LOSS_LIMIT_INR,
    DAILY_LOSS_LIMIT_USD,
    DecisionRecord,
    IST,
    MAX_CONCURRENT_POSITIONS,
    MIN_TRADES_FOR_HISTORICAL_STATS,
    Optional,
    RiskAgent,
    SL_RISK_FULL_SIZE_MAX_INR,
    SL_RISK_STAGED_MAX_INR,
    STABILITY_MAX_COUNTER_CONFIRMS,
    SYMBOLS,
    Signal,
    TP_LADDER_R,
    TP_LADDER_WEIGHTS,
    TRADE_LEVERAGE,
    TradeRecord,
    USD_TO_INR_RATE,
    Verdict,
    _close_dt_ist,
    _close_time_ist,
    _escape_md,
    asyncio,
    build_features,
    context_agent,
    datetime,
    logger,
    os,
    persistence,
)


class ExecutionMixin:

    async def _classify_reconciled_exit(self, symbol: str) -> str:
        tr = self._trailing.get(symbol)
        if not tr:
            return "reason unknown (no trailing data)"

        try:
            fill = await self.coindcx.get_last_fill(symbol)
        except Exception as e:
            logger.error(f"{symbol} | Failed to fetch last fill for exit classification: {e}")
            return "reason unknown (fill lookup failed)"

        if not fill or fill.get("price") is None:
            return "reason unknown (no fill data returned)"

        price = fill["price"]
        tr["_last_fill_price"] = price   # used for realised P&L on reconciled closes
        tr["_last_fill_ts"] = fill.get("timestamp")   # actual exchange close time (ms)
        sl = tr.get("live_sl", tr.get("sl"))
        tp = tr.get("tp")
        tolerance = 0.005

        if sl and abs(price - sl) / sl <= tolerance:
            label = "Breakeven Stop hit" if tr.get("breakeven_moved") else "Stop Loss hit"
            return f"{label} (fill ~{price:.4f} vs SL {sl:.4f})"
        if tp and abs(price - tp) / tp <= tolerance:
            return f"Take Profit hit (fill ~{price:.4f} vs TP {tp:.4f})"
        return f"unclear — fill ~{price:.4f} (entry {tr.get('entry')}, SL {sl}, TP {tp})"


    def _log_close(self, symbol: str, tr: dict, exit_price: Optional[float], reason: str,
                    event_type: str = "close", qty: Optional[float] = None):
        """Writes one line to trades.jsonl using whatever we tracked in
        self._trailing for this symbol (entry/sl/tp/mfe/mae/opened_at).
        event_type='close' means the position is fully done (used for win-rate
        stats in get_pattern_stats). event_type='partial_close' is a TP1/TP2
        ladder fill — the trade is still open, and get_pattern_stats
        deliberately excludes these from win-rate/occurrence counting so a
        single trade with two partial fills doesn't get counted as three."""
        # Risk Agent: record realised P&L for the daily loss halt. Never
        # allowed to break close logging, so it has its own try.
        try:
            _q = qty if qty is not None else tr.get("qty_open")
            _closed_ms = tr.get("_last_fill_ts") if event_type == "close" else None
            # A trade is identified by symbol + its open time; partial closes
            # also by their reason. Same key again = duplicate -> ignored.
            _key = f"{symbol}|{tr.get('opened_at')}|{event_type}|{reason if event_type != 'close' else ''}"
            _net = self.risk.record_close(symbol, tr.get("side"), tr.get("entry"), exit_price, _q,
                                          closed_at_ms=_closed_ms, key=_key)
            if _net is None:
                return   # duplicate: not re-booked, not re-learned, not re-logged
            tr["realised_usd"] = tr.get("realised_usd", 0.0) + _net
            # Pattern memory learns from each FULLY closed real trade:
            # win = the trade's total realised result (partials included) > 0
            if event_type == "close" and exit_price is not None and tr.get("features"):
                self.pattern.record(tr["features"], tr["realised_usd"] > 0)
        except Exception as e:
            logger.error(f"{symbol} | Daily P&L record failed: {e}")
        try:
            entry = tr.get("entry")
            side = tr.get("side")
            opened_at = tr.get("opened_at")
            duration_minutes = None
            if opened_at:
                try:
                    opened_dt = datetime.fromisoformat(opened_at)
                    _end = _close_dt_ist(tr.get("_last_fill_ts") if event_type == "close" else None)
                    duration_minutes = round((_end - opened_dt).total_seconds() / 60, 1)
                except Exception:
                    pass

            rr = None
            if exit_price is not None and entry is not None and tr.get("sl") is not None:
                risk = abs(entry - tr["sl"])
                if risk > 0:
                    reward = (exit_price - entry) if side == "BUY" else (entry - exit_price)
                    rr = round(reward / risk, 3)

            persistence.log_trade_event({
                "event_type": event_type,
                "symbol": symbol,
                "side": side,
                "entry": entry,
                "sl": tr.get("sl"),
                "tp": tr.get("tp"),
                "trend_mode": tr.get("trend_mode", False),
                "exit_price": exit_price,
                "reason": reason,
                "mfe_price": tr.get("mfe"),
                "mae_price": tr.get("mae"),
                "opened_at_ist": opened_at,
                "entry_candle": tr.get("entry_candle"),
                "closed_at_ist": _close_time_ist(tr.get("_last_fill_ts") if event_type == "close" else None),
                "duration_minutes": duration_minutes,
                "realized_rr": rr,
            })
        except Exception as e:
            logger.error(f"{symbol} | Failed to log trade close event: {e}", exc_info=True)


    async def _reconcile_positions(self):
        try:
            positions = await self.coindcx.get_open_positions()
        except Exception as e:
            logger.error(f"Failed to fetch open positions: {e}", exc_info=True)
            return

        self._open_positions = positions

        # Same reasoning as the main loop: a symbol removed from SYMBOLS
        # (watchlist restructuring) must still be reconciled/closed-out
        # properly if it has an open position or restored level data —
        # otherwise its close would go completely undetected.
        active_symbols = set(SYMBOLS) | set(self._trailing.keys()) | set(positions.keys())
        for symbol in active_symbols:
            level = self.state.get_level(symbol)
            # BUG FIX (2026-07-29): was `if level and level.in_trade and symbol
            # not in positions`. This missed a real case — SOLUSD was closed
            # manually outside the bot, but the closure happened in the
            # window before a daily reset (which rebuilds level.in_trade to
            # False for everyone, correctly, since it has no memory of the
            # old trade). That reset wiping in_trade to False meant this
            # check never fired again, even though self._trailing still had
            # a stale entry — and _check_exit_conditions only checks
            # `symbol in self._trailing`, not level.in_trade, so the bot kept
            # trying to manage a position that no longer existed, forever.
            # self._trailing is the more reliable source of truth for "am I
            # tracking this as open" — checking it directly (not just
            # level.in_trade) closes this gap.
            locally_tracked_open = (level and level.in_trade) or (symbol in self._trailing)
            if locally_tracked_open and symbol not in positions:
                reason_line = await self._classify_reconciled_exit(symbol)
                tr = self._trailing.get(symbol)
                if tr:
                    self._log_close(symbol, tr, exit_price=tr.get("_last_fill_price"),
                                    reason=reason_line)
                self.state.reset_symbol_watch(symbol)
                self._trailing.pop(symbol, None)
                logger.info(f"{symbol} | Position closed ({reason_line}) — resuming watch for fresh setups")
                await self.telegram.send_alert(
                    f"🔄 *Position Closed*\n\n"
                    f"*Symbol:* {symbol}\n"
                    f"*Likely reason:* {reason_line}\n\n"
                    f"Resuming watch for fresh liquidity setups on this symbol."
                )

            # Orphan check: exchange shows an open position we have no local
            # trailing record for. Happens if the process restarted between
            # order placement and the next periodic save, or (before this
            # fix existed) after any restart at all. We can't reconstruct
            # the original SL/TP/scenario from the exchange alone, so this
            # is a manual-review flag, not an auto-fix.
            if symbol in positions and symbol not in self._trailing:
                if symbol not in self._orphan_alerted:
                    logger.warning(f"{symbol} | Exchange shows an open position with no local "
                                   f"tracking (orphaned after restart?) — flagging for manual review")
                    await self.telegram.send_alert(
                        f"⚠️ *Untracked Open Position*\n\n"
                        f"*Symbol:* {symbol}\n\n"
                        f"CoinDCX shows this position open, but the bot has no local SL/TP/entry "
                        f"record for it (likely a restart between order placement and the last "
                        f"state save). The bot's exit logic will NOT monitor this position until "
                        f"you either close it manually or it hits its exchange-side SL. "
                        f"This alert won't repeat — check CoinDCX when you can."
                    )
                    self._orphan_alerted.add(symbol)
            elif symbol in self._orphan_alerted:
                # Position resolved (closed manually, or trailing was restored) —
                # allow a fresh alert if it somehow becomes orphaned again later.
                self._orphan_alerted.discard(symbol)


    async def _exit_position(self, symbol: str, tr: dict, exit_price: float, reason: str,
                              label: str, roe: Optional[float] = None):
        side = tr["side"]

        positions_ok = True
        try:
            live_positions = await self.coindcx.get_open_positions()
        except Exception as e:
            logger.error(f"{symbol} | Failed to fetch live position before close: {e}", exc_info=True)
            live_positions = {}
            positions_ok = False

        quantity = abs(live_positions.get(symbol, 0))

        if quantity <= 0:
            logger.warning(f"{symbol} | No live position found on exchange at exit time "
                           f"(reason={reason}) — skipping close call, resetting local state only")
            # The exchange-side SL/TP most likely fired first. Price the
            # realised close from the last fill so the daily loss halt still
            # counts it -- but only when the positions call actually
            # succeeded, never on a guess after an API failure.
            _fill_px = None
            if positions_ok:
                try:
                    _fill = await self.coindcx.get_last_fill(symbol)
                    _fill_px = _fill.get("price") if _fill else None
                    if _fill: tr["_last_fill_ts"] = _fill.get("timestamp")
                except Exception as e:
                    logger.error(f"{symbol} | Last-fill lookup failed on already-closed exit: {e}")
            self._log_close(symbol, tr, exit_price=_fill_px, reason=f"{reason}_already_closed")
            self.state.reset_symbol_watch(symbol)
            self._trailing.pop(symbol, None)
            self._open_positions.pop(symbol, None)
            # 2026-09-08 fix: this branch returns early just like the success
            # path below, but was never marking position_closed -- meaning
            # any exit check called AFTER this one on the same candle (e.g.
            # _check_partial_tp_ladder right after _check_trend_trailing_stop)
            # would keep running against a tr dict that's already been
            # popped from self._trailing, using stale/incomplete fields.
            # Confirmed via direct test: this caused a real KeyError crash
            # on tp2_price. Setting the flag here closes that gap the same
            # way the success branch already does.
            tr['position_closed'] = True
            return

        success = await self.coindcx.close_position_market(symbol, side, quantity)
        tr['position_closed'] = True  # prevents any subsequent check this candle from re-attempting

        roe_line = f"\n*ROE:* {roe:.2f}%" if roe is not None else ""
        if success:
            await self.telegram.send_alert(
                f"✅ *{label}*\n\n"
                f"*Symbol:* {symbol}\n"
                f"*Side:* {'📈 LONG' if side == 'BUY' else '📉 SHORT'}\n"
                f"*Entry:* {tr['entry']:.4f}\n"
                f"*Exit:* {exit_price:.4f}{roe_line}\n\n"
                f"Position closed automatically."
            )
            logger.info(f"{symbol} | Exited via {reason} at {exit_price:.4f} (qty {quantity})")
        else:
            await self.telegram.send_alert(
                f"⚠️ *{label} — Close Failed*\n\n"
                f"*Symbol:* {symbol}\n"
                f"Tried to close automatically but the exchange call failed. "
                f"Please check and close manually on CoinDCX."
            )
            logger.error(f"{symbol} | Failed to auto-close on {reason} trigger")

        # qty=0 on a FAILED close so the Risk Agent never books P&L for a
        # position that is actually still open on the exchange.
        self._log_close(symbol, tr, exit_price=exit_price, reason=reason,
                        qty=quantity if success else 0)
        self.state.reset_symbol_watch(symbol)
        self._trailing.pop(symbol, None)
        self._open_positions.pop(symbol, None)


    async def _handle_signal(self, signal: Signal):
        symbol = signal.symbol
        level = self.state.get_level(symbol)

        # ── MULTI-AGENT DECISION FLOW (2026-09-28) ─────────────────────
        # Structure Agent (StrategyEngine) proposed this signal. Risk and
        # Context now approve or veto it, in the SAME order the checks ran
        # before the restructure. One DECISION line is logged per setup.
        record = DecisionRecord(symbol, signal.side, signal.entry_price, signal.sl_price)

        # Risk Agent — pre-checks (hard rules, one-per-day, daily loss halt)
        v = record.add(self.risk.pre_check(signal, level))
        if not v.approved:
            record.log("NO TRADE")
            if signal.reject_reason:
                also_note = ""
                if signal.trend_mode:
                    also_note = "\n\n_Note: this was also a trend-aligned flip setup — would have been alert-only for that reason too, independent of the hard-rule rejection above._"
                elif signal.counter_trend:
                    also_note = "\n\n_Note: this setup also fights today's trend bias — would have been alert-only as counter-trend too, independent of the hard-rule rejection above._"

                logger.info(f"{symbol} | {signal.side} setup REJECTED — {signal.reject_reason} — alert only"
                           f"{' (also counter-trend/flip)' if also_note else ''}")
                await self.telegram.send_alert(
                    f"🚫 *Setup Rejected — Hard Rule*\n\n"
                    f"*Symbol:* {symbol}\n*Direction:* {'📈 LONG' if signal.side=='BUY' else '📉 SHORT'}\n"
                    f"*Entry:* {signal.entry_price:.4f}\n*SL:* {signal.sl_price:.4f}\n"
                    f"*Level swept:* {signal.swept_level:.4f}\n\n"
                    f"*Reason:* {signal.reject_reason}\n\n"
                    f"No auto-entry executed. This setup was rejected by a hard rule, not by "
                    f"trend/stability routing — review manually on CoinDCX if you disagree."
                    f"{also_note}"
                )
            elif level and level.auto_traded_today and getattr(signal, 'source', '') != 'pool':
                logger.info(f"{symbol} | Fresh {signal.side} setup, but today's auto-trade "
                           f"already used — alert only")
                await self.telegram.send_alert(
                    f"⚠️ *New Liquidity Setup Detected*\n\n"
                    f"*Symbol:* {symbol}\n"
                    f"*Time:* {datetime.now(IST).strftime('%H:%M IST')}\n"
                    f"*Direction:* {'📈 LONG' if signal.side == 'BUY' else '📉 SHORT'}\n"
                    f"*Entry:* {signal.entry_price:.4f}\n"
                    f"*SL:* {signal.sl_price:.4f}\n"
                    f"*Level swept:* {signal.swept_level:.4f}\n\n"
                    f"*Reason:* This symbol's one automatic trade for today has already been used.\n\n"
                    f"No auto-entry executed. Review and enter manually on CoinDCX if you agree."
                )
            elif self.risk.halted and not self.risk.halt_alerted:
                # One alert when the halt first bites; later blocked setups
                # the same day are log-only (the DECISION line above).
                self.risk.halt_alerted = True
                self.risk._save()
                await self.telegram.send_alert(
                    f"🛑 *Daily Loss Limit Reached*\n\n"
                    f"Realised P&L today: *${self.risk.realised_usd:+.2f}* "
                    f"(limit -${DAILY_LOSS_LIMIT_USD:.2f} / ₹{DAILY_LOSS_LIMIT_INR}).\n\n"
                    f"No new entries until the 05:30 IST reset. Open positions "
                    f"are still managed to their SL/TP."
                )
            return

        # Context Agent — trend stability, trend bias, BTC regime. LOG ONLY.
        # 2026-09-30: Liquidity Map signals skip Context. They were tested
        # WITHOUT these blocks, and Context would have removed 59 of 183
        # replay trades worth +Rs10,392 (29% win rate vs 20% for the rest):
        # a proven level being swept often marks a turn AGAINST the trend.
        if getattr(signal, "skip_context", False):
            v = record.add(Verdict("Context", True, "skipped — Liquidity Map trades by level history"))
        else:
            v = record.add(context_agent.evaluate(signal, level, self.state.get_regime(),
                                                  STABILITY_MAX_COUNTER_CONFIRMS))
        if not v.approved:
            logger.info(f"{symbol} | {signal.side} setup BLOCKED by Context — {v.reason} "
                       f"— log only, no auto-entry")
            record.log("NO TRADE")
            return

        # Pattern Memory Agent — shadow mode unless explicitly enabled.
        signal.features = build_features(symbol, signal.side, signal.entry_price, signal.sl_price,
                                         self.state.get_regime(), getattr(level, "trend_bias", None),
                                         self._last_obi.get(symbol))
        v = record.add(self.pattern.evaluate(signal.features))
        if not v.approved:
            logger.info(f"{symbol} | {signal.side} setup BLOCKED by Pattern — {v.reason} — log only")
            record.log("NO TRADE")
            return

        async with self._position_lock:
            open_count = len(self._open_positions)
            v = record.add(self.risk.concurrency(open_count, MAX_CONCURRENT_POSITIONS))
            if not v.approved:
                record.log("NO TRADE")
                logger.info(f"SKIPPED {symbol} — {open_count}/{MAX_CONCURRENT_POSITIONS} positions already open")
                await self.telegram.send_alert(
                    f"⏭️ *Setup Skipped* — Max concurrent positions reached ({open_count}/{MAX_CONCURRENT_POSITIONS})\n\n"
                    f"*Symbol:* {symbol}\n*Side:* {signal.side}\n*Pattern:* {signal.pattern}\n"
                    f"*Would-be Entry:* {signal.entry_price:.4f}\n*Would-be SL:* {signal.sl_price:.4f}\n"
                    f"*Level swept:* {signal.swept_level:.4f}"
                )
                return
            self._open_positions[symbol] = 0

        trend_line = f"\n*Trend bias:* {level.trend_bias}" if level.trend_bias != "NONE" else ""

        try:
            margin_usd = float(os.getenv('TRADE_SIZE_USD', 40))

            # Margin cap — internally tracked, does NOT depend on the broken
            # CoinDCX wallet-balance endpoint. Set TOTAL_ACCOUNT_MARGIN_USD in
            # Railway env vars to enable; unset or 0 disables this check.
            total_margin_cap = float(os.getenv('TOTAL_ACCOUNT_MARGIN_USD', 0))
            if total_margin_cap > 0:
                committed_margin = len(self._open_positions) * margin_usd
                if committed_margin > total_margin_cap:
                    logger.warning(f"{symbol} | Skipping order — would exceed configured "
                                   f"margin cap (committed ${committed_margin:.2f} > "
                                   f"cap ${total_margin_cap:.2f})")
                    await self.telegram.send_alert(
                        f"⚠️ *Order Skipped — Margin Cap Reached*\n\n"
                        f"*Symbol:* {symbol}\n*Side:* {'📈 LONG' if signal.side == 'BUY' else '📉 SHORT'}\n"
                        f"*Committed if opened:* ${committed_margin:.2f}\n"
                        f"*Configured cap:* ${total_margin_cap:.2f}\n\n"
                        f"Setup was valid but would exceed your configured TOTAL_ACCOUNT_MARGIN_USD."
                    )
                    self._open_positions.pop(symbol, None)
                    return

            # Rule 10 fix (2026-08-13) — mark today's one-attempt-per-symbol
            # slot used HERE, the moment we actually commit to attempting an
            # order, not after the order succeeds. Previously this only
            # fired inside the success branch below, which meant every
            # order that failed (e.g. the whole no-funds audit week) never
            # set this flag at all — confirmed via real data: RIF fired 3
            # auto-attempts and TAO fired 2, all on the same symbol/day,
            # because every one of them failed and none ever marked the
            # day as used. This now fires regardless of what happens next,
            # matching what "one attempt per day" actually means.
            if getattr(signal, 'source', '') != 'pool':   # pool trades use their own daily slot
                self.state.mark_auto_traded(symbol)

            # 2026-09-14: INR risk-tier gate. Computed once, here, so both
            # the alert text below and the actual sizing agree. This
            # REPLACES the earlier %-distance-based use_staged_entry value
            # from the strategy engine -- signal.use_staged_entry is
            # overwritten below to reflect the INR-based decision instead.
            _tier, _risk_usd_at_full_size, _risk_inr_at_full_size = RiskAgent.inr_tier(
                signal.entry_price, signal.sl_price, margin_usd, TRADE_LEVERAGE,
                SL_RISK_FULL_SIZE_MAX_INR, SL_RISK_STAGED_MAX_INR, USD_TO_INR_RATE)

            if _tier == "SKIP":
                record.add(Verdict("Risk", False, f"INR tier SKIP ₹{_risk_inr_at_full_size:.0f}"))
                record.log("NO TRADE")
                logger.info(f"{symbol} | SKIPPED (INR risk tier) — full-size SL risk would be "
                           f"₹{_risk_inr_at_full_size:.0f} (${_risk_usd_at_full_size:.2f}), "
                           f"above the ₹{SL_RISK_STAGED_MAX_INR} ceiling. Recorded only, no order "
                           f"placed. Entry:{signal.entry_price:.4f} SL:{signal.sl_price:.4f}")
                self._open_positions.pop(symbol, None)
                return
            elif _tier == "STAGED":
                signal.use_staged_entry = True
                record.add(Verdict("Risk", True, f"INR tier STAGED ₹{_risk_inr_at_full_size:.0f}"))
                record.log("TRADE (staged 50%)")
                logger.info(f"{symbol} | INR risk tier: STAGED 50% — full-size SL risk would be "
                           f"₹{_risk_inr_at_full_size:.0f} (${_risk_usd_at_full_size:.2f}), "
                           f"between ₹{SL_RISK_FULL_SIZE_MAX_INR}-{SL_RISK_STAGED_MAX_INR}")
            else:
                signal.use_staged_entry = False
                record.add(Verdict("Risk", True, f"INR tier FULL ₹{_risk_inr_at_full_size:.0f}"))
                record.log("TRADE (full size)")
                logger.info(f"{symbol} | INR risk tier: FULL SIZE — SL risk ₹{_risk_inr_at_full_size:.0f} "
                           f"(${_risk_usd_at_full_size:.2f}), below ₹{SL_RISK_FULL_SIZE_MAX_INR}")

            level_line = (f"*Level swept:* {signal.swept_level:.4f} (dynamic re-anchor, "
                          f"not the fixed daily PDH/PDL below)\n*Fixed PDH:* {signal.pdh:.4f} | "
                          f"*Fixed PDL:* {signal.pdl:.4f}"
                          if signal.trend_mode else
                          f"*PDH:* {signal.pdh:.4f} | *PDL:* {signal.pdl:.4f}")


            await self.telegram.send_alert(
                f"🔍 *Setup Detected*\n\n"
                f"*Symbol:* {symbol}\n*Side:* {'📈 LONG' if signal.side == 'BUY' else '📉 SHORT'}\n"
                f"*Pattern:* {signal.pattern}{' (trend-aligned flip)' if signal.trend_mode else ''}"
                f"{' — STAGED ENTRY (wide SL, 50% initial)' if signal.use_staged_entry else ''}\n"
                f"*Entry:* {signal.entry_price:.4f}\n*SL:* {signal.sl_price:.4f}\n"
                f"{level_line}{trend_line}"
                + (f"\n*Why:* {signal.liqmap_story}\n*Target pool:* "
                   f"{', '.join(f'{p:.6g}' for p in signal.liqmap_pools)}"
                   if getattr(signal, 'liqmap_story', None) else "")
                + "\n\n⏳ Placing order..."
            )

            # 2026-08-15: SL distance beyond MAX_SL_DISTANCE_PCT no longer
            # rejects the trade — it opens at half planned margin instead,
            # with the remainder added only via _check_staged_addition's
            # +1R/confirmation check (never a blind timer or percentage).
            initial_margin = margin_usd * 0.5 if signal.use_staged_entry else margin_usd
            quantity = (initial_margin * TRADE_LEVERAGE) / signal.entry_price

            order_result = await self.coindcx.place_market_order(
                symbol=symbol, side=signal.side, quantity=quantity,
                sl_price=signal.sl_price, leverage=TRADE_LEVERAGE
            )

            if order_result and order_result.get('id'):
                order_id = order_result.get('id', 'N/A')
                quantity_filled = order_result.get('quantity', quantity)

                record = TradeRecord(
                    symbol=symbol, side=signal.side, entry_price=signal.entry_price,
                    sl_price=signal.sl_price, order_id=order_id, scenario=signal.scenario,
                    timestamp=datetime.now(IST).isoformat()
                )
                self.state.register_trade(record)
                self.state.mark_in_trade(symbol)
                # mark_auto_traded() already fired above, at the point we
                # committed to attempting this order — see the Rule 10 fix
                # comment there. Not repeated here anymore.

                tp_price = getattr(signal, 'target', None) or (signal.pdh if signal.side == 'BUY' else signal.pdl)
                opened_at_ist = datetime.now(IST).isoformat()
                risk = abs(signal.entry_price - signal.sl_price)

                tr = {"features": getattr(signal, "features", None),
                      "entry_candle": getattr(self, "_last_pattern", {}).get(symbol),
                      "side": signal.side, "entry": signal.entry_price,
                      "sl": signal.sl_price, "live_sl": signal.sl_price,
                      "breakeven_moved": False,
                      "breakeven_stage1_moved": False,
                      "tp": tp_price,
                      "simple_exit": getattr(signal, "simple_exit", False),
                      "invalidate_level": getattr(signal, "invalidate_level", None),
                      "trend_mode": signal.trend_mode,
                      "opened_at": opened_at_ist,
                      "mfe": signal.entry_price, "mae": signal.entry_price,
                      "staged_entry": signal.use_staged_entry,
                      "staged_stage2_added": False,
                      "planned_margin_usd": margin_usd,
                      "deployed_margin_usd": initial_margin}

                # Seed the TP1/TP2 partial ladder — only for non-trend-mode
                # trades, and only if each tier's R-multiple sits strictly
                # closer than the original single target (tp_price). If TP1
                # would already be beyond that target, skip the ladder
                # entirely for this trade — it behaves exactly as before.
                if not signal.trend_mode and risk > 0 and not getattr(signal, 'simple_exit', False):
                    candidate_prices = []
                    for r in TP_LADDER_R:
                        p = (signal.entry_price + r * risk if signal.side == 'BUY'
                             else signal.entry_price - r * risk)
                        candidate_prices.append(p)
                    within_target = all(
                        (p <= tp_price if signal.side == 'BUY' else p >= tp_price)
                        for p in candidate_prices
                    )
                    if within_target:
                        tr["tp1_price"] = candidate_prices[0]
                        tr["tp2_price"] = candidate_prices[1]
                        tr["tp1_filled"] = False
                        tr["tp2_filled"] = False
                        tr["tp1_weight"], tr["tp2_weight"], tr["tp3_weight"] = TP_LADDER_WEIGHTS

                tr["qty_open"] = quantity_filled
                self._trailing[symbol] = tr
                self._open_positions[symbol] = quantity_filled
                persistence.log_trade_event({
                    "event_type": "open", "symbol": symbol, "side": signal.side,
                    "entry": signal.entry_price, "sl": signal.sl_price, "tp": tp_price,
                    "trend_mode": signal.trend_mode, "scenario": signal.scenario,
                    "order_id": order_id, "opened_at_ist": opened_at_ist,
                })
                # Snapshot immediately after opening rather than waiting for the
                # next periodic save — a restart in the seconds right after
                # entry is exactly the window the orphan-check above exists for.
                persistence.save_state(self.state, self._trailing)

                tp_set = False
                if not signal.trend_mode:
                    for attempt in range(3):
                        if attempt > 0:
                            await asyncio.sleep(1.5)
                        tp_set = await self.coindcx.update_position_tpsl(symbol, tp_price=tp_price)
                        if tp_set:
                            break
                        logger.warning(f"{symbol} | TP set attempt {attempt + 1}/3 failed — "
                                       f"position may not be registered on the exchange yet")

                if signal.trend_mode:
                    tp_note = ("🎯 Trend-aligned trade — no fixed resting TP set (target would sit "
                              "behind entry). Exit runs on stop-loss + ROE protection only.")
                    tp_lines = ""
                    rr_lines = ""
                else:
                    tp_note = (f"🎯 Resting take-profit (dead-man's-switch) set at {tp_price:.4f} — "
                              f"fires if the bot itself ever goes down before managing exits."
                              if tp_set else
                              "⚠️ Could not set the resting take-profit safety order — the bot's own "
                              "candle-by-candle logic will still enforce all exits, but there's no "
                              "exchange-side backstop if the bot is down. Check CoinDCX manually if concerned.")

                    if "tp1_price" in tr:
                        tp_lines = (f"*TP1:* {tr['tp1_price']:.4f} (34%)\n"
                                   f"*TP2:* {tr['tp2_price']:.4f} (33%)\n"
                                   f"*TP3:* {tp_price:.4f} (33%, final target)\n")
                        rr_lines = (f"*Expected RR:*\n"
                                   f"1:{TP_LADDER_R[0]}\n1:{TP_LADDER_R[1]}\n"
                                   f"1:{round(abs(tp_price - signal.entry_price) / risk, 1) if risk else '—'}\n")
                    else:
                        tp_lines = f"*TP:* {tp_price:.4f} (single target — TP1/TP2 would've sat beyond it)\n"
                        rr_lines = (f"*Expected RR:* 1:{round(abs(tp_price - signal.entry_price) / risk, 1)}\n"
                                   if risk else "")

                pattern_label = "trend-aligned flip setups (all symbols)" if signal.trend_mode \
                                 else "sweep-reversal setups (all symbols)"
                stats = persistence.get_pattern_stats(trend_mode=signal.trend_mode)
                if stats["has_enough_data"]:
                    confidence = ("HIGH" if stats["win_rate_pct"] >= 60 else
                                  "MEDIUM" if stats["win_rate_pct"] >= 45 else "LOW")
                    stats_block = (
                        f"*Confidence:* {confidence} _(heuristic from historical win rate — not a guarantee)_\n"
                        f"*Historical Win Rate:* {stats['win_rate_pct']:.0f}% "
                        f"({stats['count']} {pattern_label})\n"
                        + (f"*Avg Hold Time:* {stats['avg_hold_minutes']:.0f} min\n" if stats['avg_hold_minutes'] else "")
                        + f"\nThis pattern has occurred {stats['count']} times across all symbols.\n"
                        + (f"Average move: {stats['avg_move_pct']:+.2f}%\n" if stats['avg_move_pct'] is not None else "")
                        + (f"Largest move: {stats['largest_move_pct']:+.2f}%\n" if stats['largest_move_pct'] is not None else "")
                    )
                else:
                    n = stats["count"]
                    stats_block = (
                        f"*Confidence:* N/A — not enough history yet ({n}/{MIN_TRADES_FOR_HISTORICAL_STATS} "
                        f"{pattern_label} logged)\n"
                        f"*Historical Win Rate:* N/A — will show once {MIN_TRADES_FOR_HISTORICAL_STATS}+ "
                        f"trades of this pattern are logged\n"
                    )

                staged_note = ""
                if signal.use_staged_entry:
                    staged_note = (
                        f"\n\n📊 *Staged Entry* — SL distance exceeded the normal cap, so this "
                        f"opened at 50% size (${initial_margin:.2f} of ${margin_usd:.2f} planned). "
                        f"Remaining ${margin_usd - initial_margin:.2f} adds automatically only if "
                        f"the trade reaches +1R with continued confirmation — never on a blind timer."
                    )

                msg = (
                    f"✅ *Trade Executed*\n\n"
                    f"*Symbol:* {symbol}\n*Side:* {'📈 LONG' if signal.side == 'BUY' else '📉 SHORT'}\n"
                    f"*Entry:* {signal.entry_price:.4f}\n*SL:* {signal.sl_price:.4f}\n"
                    f"{tp_lines}{rr_lines}\n"
                    f"{stats_block}\n"
                    f"*Margin:* ${initial_margin:.2f}\n*Leverage:* {TRADE_LEVERAGE}x\n"
                    f"*Exposure:* ${initial_margin * TRADE_LEVERAGE:.2f}\n*Quantity:* {quantity_filled}\n"
                    f"*Order ID:* `{order_id}`\n*Open positions:* {len(self._open_positions)}/{MAX_CONCURRENT_POSITIONS}\n\n"
                    f"{tp_note}"
                    f"{staged_note}\n\n"
                    f"🔄 Today's auto-trade for this symbol has now been used — further setups will be alert-only."
                )
                await self.telegram.send_alert(msg)
                logger.info(f"Trade executed: {record}")

            else:
                error_msg = order_result.get('error', 'Unknown') if order_result else 'No response from API'
                logger.error(f"{symbol} | Order failed: {error_msg}")
                await self.telegram.send_alert(
                    f"❌ *Order Failed*\n\n*Symbol:* {symbol}\n*Side:* {signal.side}\n"
                    f"*Error:* {_escape_md(error_msg)}\n\n⚠️ Manual intervention may be required."
                )
                self._open_positions.pop(symbol, None)

        except Exception as e:
            logger.error(f"{symbol} | Order exception: {e}", exc_info=True)
            await self.telegram.send_alert(
                f"❌ *Order Exception*\n\n*Symbol:* {symbol}\n*Error:* {_escape_md(e)}\n\n"
                f"⚠️ Manual intervention required."
            )
            self._open_positions.pop(symbol, None)
