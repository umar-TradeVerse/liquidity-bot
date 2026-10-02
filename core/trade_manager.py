"""Trade Manager Agent — manages every open position candle by candle:
breakeven, staged add, early failure, OBI escalation, trend trail,
TP ladder, ROE protection.

Moved verbatim out of core/monitor.py on 2026-10-02 (no logic changes)."""
from core.trading_common import (  # explicit, so lint can catch undefined names
    BREAKEVEN_STAGE1_R,
    BREAKEVEN_STAGE2_R,
    DOJI_BODY_RATIO_MAX,
    EARLY_FAILURE_MAX_CANDLES,
    EARLY_FAILURE_MODE,
    MIN_ROE_FOR_REJECTION_EXIT_PCT,
    OBI_EXIT_LEVEL,
    OBI_EXIT_MODE,
    OBI_RISE_CANDLES,
    Optional,
    REJECTION_PROXIMITY_PCT,
    ROE_TARGET_PCT,
    TRADE_LEVERAGE,
    TRAILING_STOP_R_BUFFER,
    TREND_TRAIL_ACTIVATE_R,
    TREND_TRAIL_BUFFER_R,
    _is_bearish_engulfing,
    _is_bearish_rejection,
    _is_bullish_engulfing,
    _is_bullish_rejection,
    compute_obi,
    logger,
    persistence,
)


class TradeManagerMixin:

    async def _check_breakeven_move(self, symbol: str, tr: dict):
        """Two-stage SL ratchet based on MFE:
          Stage 1 (BREAKEVEN_STAGE1_R): move SL partway — halfway between the
            original SL and entry — cutting max remaining loss roughly in half.
          Stage 2 (BREAKEVEN_STAGE2_R): move SL the rest of the way to entry.
        Runs for every trade type, including trend-mode. Uses the ORIGINAL
        sl (tr['sl']) for the risk calculation — tr['sl'] is never mutated,
        so R-multiple analytics (_log_close, ladder capping) stay correct
        even after the live stop has moved. tr['live_sl'] is the actual
        current protective price, used for reconciled-exit classification.
        Each stage only ever fires once, and stage 2 can fire directly
        without stage 1 having happened first if price gapped past both
        thresholds in a single candle."""
        entry, sl = tr.get("entry"), tr.get("sl")
        risk = abs(entry - sl) if entry is not None and sl is not None else 0
        if risk <= 0:
            return

        mfe = tr.get("mfe", entry)
        mfe_R = (mfe - entry) / risk if tr["side"] == "BUY" else (entry - mfe) / risk

        # Stage 2 first — if price already moved far enough to skip straight
        # past stage 1, go directly to full breakeven rather than parking at
        # a partial level that's already stale.
        if not tr.get("breakeven_moved") and mfe_R >= BREAKEVEN_STAGE2_R:
            success = await self.coindcx.update_stop_loss(symbol, new_sl_price=entry)
            if success:
                tr["live_sl"] = entry
                tr["breakeven_moved"] = True
                tr["breakeven_stage1_moved"] = True  # stage 1 is superseded, mark done
                logger.info(f"{symbol} | SL moved to full breakeven ({entry:.4f}) — MFE reached "
                           f"{mfe_R:.2f}R (stage 2 trigger: {BREAKEVEN_STAGE2_R}R)")
                await self.telegram.send_alert(
                    f"🛡️ *SL Moved to Breakeven*\n\n"
                    f"*Symbol:* {symbol}\n*New SL:* {entry:.4f} (entry price)\n\n"
                    f"This trade reached +{BREAKEVEN_STAGE2_R}R — worst case from here is now "
                    f"scratch, not a full loss."
                )
            else:
                logger.warning(f"{symbol} | Failed to move SL to full breakeven — will retry "
                               f"next candle if MFE condition still holds")
            return

        if not tr.get("breakeven_stage1_moved") and mfe_R >= BREAKEVEN_STAGE1_R:
            partial_sl = (entry + sl) / 2  # halfway between original SL and entry
            success = await self.coindcx.update_stop_loss(symbol, new_sl_price=partial_sl)
            if success:
                tr["live_sl"] = partial_sl
                tr["breakeven_stage1_moved"] = True
                logger.info(f"{symbol} | SL moved to partial breakeven ({partial_sl:.4f}) — MFE "
                           f"reached {mfe_R:.2f}R (stage 1 trigger: {BREAKEVEN_STAGE1_R}R)")
                await self.telegram.send_alert(
                    f"🛡️ *SL Moved to Partial Breakeven*\n\n"
                    f"*Symbol:* {symbol}\n*New SL:* {partial_sl:.4f} (halfway to entry)\n\n"
                    f"This trade reached +{BREAKEVEN_STAGE1_R}R — remaining risk is now roughly "
                    f"half of the original. Full breakeven locks in at +{BREAKEVEN_STAGE2_R}R."
                )
            else:
                logger.warning(f"{symbol} | Failed to move SL to partial breakeven — will retry "
                               f"next candle if MFE condition still holds")


    async def _check_staged_addition(self, symbol: str, tr: dict, candle: dict, target):
        """2026-08-15 — Rule 1 replacement: trades with SL distance beyond
        MAX_SL_DISTANCE_PCT open at only 50% of planned margin (see
        _handle_signal). The remaining 50% is added here, ONLY when ALL of:
          - MFE has reached BREAKEVEN_STAGE2_R (+1R) — same threshold
            already used for the breakeven ratchet, reused rather than
            inventing a second number to tune.
          - The CURRENT candle shows no rejection candle against the
            position's direction (reusing the same _is_bearish_rejection /
            _is_bullish_rejection shape checks already used for exit logic —
            not new subjective criteria).
          - Price hasn't already covered most of the distance to target
            (adding late, with little room left, isn't worth the extra risk).
        Never adds on a blind timer or percentage alone, and NEVER adds
        while the trade is underwater — mfe_R must already be >= 1.0, which
        by definition means the trade is profitable at the moment of adding.
        """
        if not tr.get("staged_entry") or tr.get("staged_stage2_added"):
            return

        entry, sl, side = tr.get("entry"), tr.get("sl"), tr["side"]
        risk = abs(entry - sl) if entry is not None and sl is not None else 0
        if risk <= 0:
            return

        mfe = tr.get("mfe", entry)
        mfe_R = (mfe - entry) / risk if side == "BUY" else (entry - mfe) / risk
        if mfe_R < BREAKEVEN_STAGE2_R:
            return

        rejection_against = (_is_bearish_rejection(candle) if side == "BUY"
                              else _is_bullish_rejection(candle))
        if rejection_against:
            logger.info(f"{symbol} | Staged addition skipped this candle — rejection "
                       f"candle against position direction")
            return

        if target:
            total_distance = abs(target - entry)
            covered = abs(mfe - entry)
            if total_distance > 0 and (covered / total_distance) >= 0.90:
                logger.info(f"{symbol} | Staged addition skipped — already {covered/total_distance*100:.0f}% "
                           f"of the way to target, not enough room left to justify adding")
                return

        remaining_margin = tr.get("planned_margin_usd", 0) - tr.get("deployed_margin_usd", 0)
        if remaining_margin <= 0:
            return

        quantity_to_add = (remaining_margin * TRADE_LEVERAGE) / candle['close']
        order_result = await self.coindcx.place_market_order(
            symbol=symbol, side=side, quantity=quantity_to_add,
            sl_price=tr.get("live_sl", sl), leverage=TRADE_LEVERAGE
        )

        if order_result and order_result.get('id'):
            added_qty = order_result.get('quantity', quantity_to_add)
            tr["deployed_margin_usd"] = tr.get("planned_margin_usd", 0)
            tr["staged_stage2_added"] = True
            self._open_positions[symbol] = self._open_positions.get(symbol, 0) + added_qty
            tr["qty_open"] = (tr.get("qty_open") or 0) + added_qty
            logger.info(f"{symbol} | Staged addition filled — remaining 50% (${remaining_margin:.2f} "
                       f"margin) added at {candle['close']:.4f}, MFE was {mfe_R:.2f}R")
            persistence.log_trade_event({
                "event_type": "staged_addition", "symbol": symbol, "side": side,
                "add_price": candle['close'], "margin_added": remaining_margin,
                "mfe_r_at_add": round(mfe_R, 3),
            })
            await self.telegram.send_alert(
                f"➕ *Staged Entry — Remaining 50% Added*\n\n"
                f"*Symbol:* {symbol}\n*Add price:* {candle['close']:.4f}\n"
                f"*Margin added:* ${remaining_margin:.2f}\n*Trade now at:* +{mfe_R:.2f}R\n\n"
                f"Position is now at full planned size."
            )
        else:
            logger.warning(f"{symbol} | Staged addition order failed — will retry next candle "
                           f"if conditions still hold")


    async def _check_early_failure_exit(self, symbol: str, tr: dict, candle: dict):
        """2026-09-14 — REPLACES early_invalidation entirely, per explicit
        request. Monitors only the first EARLY_FAILURE_MAX_CANDLES candles
        (45 min at 15m) after entry. Exits immediately the moment a clear
        adverse rejection appears; does nothing at all once that window
        has passed -- no R-based cutoff of any kind remains after this.

        Two conditions qualify, deliberately reusing existing, already-
        proven pattern detection rather than inventing new arbitrary
        thresholds:
          1. A genuine rejection candle against the position
             (_is_bearish_rejection for LONG, _is_bullish_rejection for
             SHORT -- the same wick:body shape check already used
             elsewhere for entries: shooting star / hammer / strong
             reversal are all this shape).
          2. A doji (body <= DOJI_BODY_RATIO_MAX of its own range) followed
             by the NEXT candle continuing adverse past the doji's close --
             "a meaningful doji followed by weakness", not a doji alone.

        Never touches SL, the breakeven ratchet, TP ladder, or trend_trail
        -- this is purely an additional, independent early check."""
        if tr.get('early_failure_checked_done'):
            return

        n = tr.get('early_failure_candles', 0) + 1
        tr['early_failure_candles'] = n

        entry, sl, side = tr.get('entry'), tr.get('sl'), tr['side']
        risk = abs(entry - sl) if entry and sl else 0
        if risk <= 0:
            tr['early_failure_checked_done'] = True
            return

        rejection = (_is_bearish_rejection(candle) if side == 'BUY'
                     else _is_bullish_rejection(candle))

        doji_followup = False
        if tr.get('early_failure_prior_was_doji'):
            prior_close = tr.get('early_failure_prior_close')
            if prior_close is not None:
                if side == 'BUY' and candle['close'] < prior_close:
                    doji_followup = True
                elif side == 'SELL' and candle['close'] > prior_close:
                    doji_followup = True

        if rejection or doji_followup:
            reason_bits = []
            if rejection: reason_bits.append("rejection candle against the position")
            if doji_followup: reason_bits.append("doji followed by continued weakness")
            reason_text = " + ".join(reason_bits)
            logger.info(f"{symbol} | Early failure exit — {reason_text} within "
                       f"{n} candle(s) of entry")
            tr['early_failure_checked_done'] = True
            await self._exit_position(
                symbol, tr, exit_price=candle['close'], reason="early_failure_exit",
                label=(f"⚠️ *Position Closed — Early Failure Exit*\n\n"
                       f"Clear adverse rejection detected within {n} candle(s) "
                       f"(~{n*15} min) of entry: {reason_text}.\n"
                       f"Exiting now rather than risk the full SL play out.")
            )
            return

        # Track doji state for next candle's follow-up check.
        rng = candle['high'] - candle['low']
        body = abs(candle['close'] - candle['open'])
        tr['early_failure_prior_was_doji'] = (rng > 0 and body <= DOJI_BODY_RATIO_MAX * rng)
        tr['early_failure_prior_close'] = candle['close']

        if n >= EARLY_FAILURE_MAX_CANDLES:
            tr['early_failure_checked_done'] = True
            logger.info(f"{symbol} | Early failure window closed ({EARLY_FAILURE_MAX_CANDLES} "
                       f"candles / 45 min) with no clear rejection -- trade continues normally, "
                       f"no further check of this kind applies.")



    async def _obi_watch(self, symbol: str, tr: dict, candle: dict) -> bool:
        """2026-10-02: OBI logged on EVERY candle of an open trade, plus the
        ESCALATION exit (rule designed by Umar).

        Pressure = OBI against the trade (OBI for a SHORT, -OBI for a LONG).
        If pressure RISES on OBI_RISE_CANDLES consecutive candles while the
        book is actually against the trade (pressure > OBI_EXIT_LEVEL,
        default 0), the reversal is real -> exit at market.
        Example (SHORT): 0.46 -> 0.55 -> 0.60 -> 0.70 -> 0.82 = exit.
        Mirrored for a LONG: -0.46 -> -0.55 -> ... = rising SELL pressure.
        Any drop (e.g. 0.70 -> 0.55) = a pullback -> continue; the count
        restarts from that candle.
        Safeguard: only exits a trade that is LOSING on that candle -- a
        winner is never closed by this rule. Any API failure is skipped.
        Live but UNTESTED when added (no in-trade OBI history existed)."""
        try:
            book = await self.coindcx.get_orderbook(symbol, depth=20)
            obi = compute_obi(book["bids"], book["asks"]) if book else None
        except Exception as e:
            logger.warning(f"{symbol} | OBI in-trade fetch failed ({e}) — skipped this candle")
            return False
        if obi is None:
            return False
        side, entry = tr["side"], tr["entry"]
        pressure = obi if side == "SELL" else -obi
        prev = tr.get("obi_prev")
        rise = tr.get("obi_rise", 0)
        rise = rise + 1 if (prev is not None and pressure > prev) else 0
        tr["obi_prev"], tr["obi_rise"] = pressure, rise
        losing = candle["close"] < entry if side == "BUY" else candle["close"] > entry
        risk = abs(entry - tr.get("sl", entry)) or 1
        r_now = ((candle["close"] - entry) if side == "BUY" else (entry - candle["close"])) / risk
        trigger = rise >= OBI_RISE_CANDLES and pressure > OBI_EXIT_LEVEL
        logger.info(f"{symbol} | OBI in-trade {obi:+.3f} (pressure against {side}: {pressure:+.3f}, "
                    f"rising {rise}/{OBI_RISE_CANDLES} candles) | {r_now:+.2f}R"
                    f"{' | ESCALATION reached' if trigger else ''}")
        if not (trigger and losing):
            return False
        if OBI_EXIT_MODE != "live":
            logger.info(f"{symbol} | OBI escalation exit WOULD fire (OBI_EXIT_MODE=off — logging only)")
            return False
        await self._exit_position(
            symbol, tr, exit_price=candle["close"], reason="obi_escalation",
            label=(f"📕 *Position Closed — Order Book Escalation*\n\n"
                   f"Pressure against the {'LONG' if side == 'BUY' else 'SHORT'} rose on "
                   f"{rise} consecutive candles to {pressure:+.2f} "
                   f"while the trade was losing ({r_now:+.2f}R).\n"
                   f"Treated as a real reversal, not a pullback."))
        return True


    async def _check_trend_trailing_stop(self, symbol: str, tr: dict, candle: dict):
        """2026-09-02 — replaces zone_reversal. Once MFE reaches
        TREND_TRAIL_ACTIVATE_R (1.0R, same threshold as the stage-2
        breakeven ratchet), starts trailing the SL TREND_TRAIL_BUFFER_R
        behind the running high-water mark instead of leaving it frozen at
        entry. Ratchets forward only, never loosens. Below 1.0R this does
        nothing -- the existing _check_breakeven_move ratchet is the only
        thing protecting the trade, same as before this change.
        Deliberately does NOT reuse TRAILING_STOP_R_BUFFER (0.5) from the
        post-ROE remainder trail -- that buffer activates only after a much
        larger move (7%+ ROE) and would sit ABOVE breakeven if applied here
        at a 1.0R activation point, causing an immediate false stop-out."""


        entry, sl, side = tr.get('entry'), tr.get('sl'), tr['side']
        risk = abs(entry - sl) if entry and sl else 0
        if risk <= 0:
            return

        mfe = tr.get('mfe', entry)
        mfe_R = (mfe - entry) / risk if side == 'BUY' else (entry - mfe) / risk

        if mfe_R < TREND_TRAIL_ACTIVATE_R:
            return  # not yet earned trailing -- stage-2 ratchet already covers this trade

        # 2026-10-02 FIX (look-ahead bug): the high-water mark already includes
        # THIS candle's high, so a trail built from it and then tested against
        # this same candle's low was "hit" by moves that happened BEFORE the
        # high (e.g. XRPUSD 01-Oct 10:45 pm: O 1.4814 L 1.4810 H 1.4923
        # C 1.4918 -> exited at 1.4902 although price never came back down).
        # Now a candle can only hit a trail that existed BEFORE it formed;
        # a new trail level takes effect from the next candle (and is sent
        # to the exchange immediately, which enforces it in real time).
        prev_trail = tr.get('trail_sl')
        hit = prev_trail is not None and (
            (candle['low'] <= prev_trail) if side == 'BUY' else (candle['high'] >= prev_trail))
        trail_sl = mfe - TREND_TRAIL_BUFFER_R * risk if side == 'BUY' else mfe + TREND_TRAIL_BUFFER_R * risk
        current_live_sl = tr.get('live_sl', sl)
        improved = (trail_sl > current_live_sl) if side == 'BUY' else (trail_sl < current_live_sl)

        if hit:
            trail_sl = prev_trail
            logger.info(f"{symbol} | Trend trail hit at {mfe_R:.2f}R MFE — trail was "
                       f"{trail_sl:.4f} ({TREND_TRAIL_BUFFER_R}R behind high-water)")
            await self._exit_position(
                symbol, tr, exit_price=trail_sl,
                reason="trend_trail",
                label=(f"📉 *Position Closed — Trend Trail*\n\n"
                       f"Price pulled back {TREND_TRAIL_BUFFER_R}R from its best point "
                       f"({mfe_R:.2f}R MFE) after already clearing full breakeven.\n"
                       f"Locked in the trailed level rather than risk further giveback.")
            )
            return

        if improved:
            # remembered for the NEXT candle's hit check, even if the exchange
            # update fails (the bot then still enforces it itself)
            tr['trail_sl'] = trail_sl if prev_trail is None else (
                max(prev_trail, trail_sl) if side == 'BUY' else min(prev_trail, trail_sl))
            success = await self.coindcx.update_stop_loss(symbol, new_sl_price=trail_sl)
            if success:
                tr['live_sl'] = trail_sl
                logger.info(f"{symbol} | Trend trail moved to {trail_sl:.4f} "
                           f"({TREND_TRAIL_BUFFER_R}R behind {mfe_R:.2f}R high-water)")


    async def _check_partial_tp_ladder(self, symbol: str, tr: dict, candle: dict):
        """Checks TP1/TP2 (interior partials, closer than the original single
        target) and executes a partial market close on whichever tier the
        candle has reached, in order. TP3 is NOT handled here — it's the
        original target/rejection/ROE priority logic below, unchanged, which
        closes whatever quantity remains at that point.

        Silently does nothing for trades where the ladder was never seeded
        (tp1_price missing) — that happens when TP1's R-multiple would have
        sat beyond the original single target at entry time, in which case
        _handle_signal deliberately skipped seeding the ladder and this
        trade behaves exactly as it would have before this feature existed."""
        if "tp1_price" not in tr:
            return
        side = tr["side"]

        for tier in (1, 2):
            price_key, filled_key, weight_key = f"tp{tier}_price", f"tp{tier}_filled", f"tp{tier}_weight"
            if tr.get(filled_key):
                continue
            tp_price = tr[price_key]
            hit = (candle['high'] >= tp_price) if side == 'BUY' else (candle['low'] <= tp_price)
            if not hit:
                continue

            try:
                live_positions = await self.coindcx.get_open_positions()
            except Exception as e:
                logger.error(f"{symbol} | Failed to fetch live position for TP{tier} partial close: {e}",
                             exc_info=True)
                return  # try again next candle rather than guessing at quantity

            live_qty = abs(live_positions.get(symbol, 0))
            if live_qty <= 0:
                # Position already fully gone (closed some other way) — nothing to partial-close.
                tr[filled_key] = True
                continue

            # Portion of the CURRENT remaining quantity, not the original
            # entry quantity — this is what keeps it safe against the
            # missing reduce_only guarantee (see coindcx.py comment):
            # always close a fraction of what's actually live right now.
            remaining_weight = sum(tr.get(f"tp{t}_weight", 0) for t in (1, 2, 3)
                                    if not tr.get(f"tp{t}_filled", False))
            tier_weight = tr[weight_key]
            close_qty = live_qty * (tier_weight / remaining_weight) if remaining_weight > 0 else live_qty

            success = await self.coindcx.close_position_market(symbol, side, close_qty)
            if success:
                tr[filled_key] = True
                self._log_close(symbol, tr, exit_price=tp_price, reason=f"tp{tier}_partial",
                                 event_type="partial_close", qty=close_qty)
                tr["qty_open"] = max(0.0, (tr.get("qty_open") or live_qty) - close_qty)
                pct_of_position = round(100 * tier_weight, 0)
                await self.telegram.send_alert(
                    f"🎯 *TP{tier} Hit — Partial Close*\n\n"
                    f"*Symbol:* {symbol}\n*Price:* {tp_price:.4f}\n"
                    f"*Closed:* ~{pct_of_position:.0f}% of remaining position\n\n"
                    f"Remainder still running toward "
                    f"{'TP' + str(tier + 1) if tier == 1 and 'tp2_price' in tr and not tr.get('tp2_filled') else 'final target'}."
                )
                logger.info(f"{symbol} | TP{tier} partial close at {tp_price:.4f} (qty {close_qty:.6f})")
            else:
                logger.error(f"{symbol} | TP{tier} partial close order failed — will retry next candle "
                             f"if price still qualifies")
                return  # don't mark filled; try again next candle


    async def _check_exit_conditions(self, symbol: str, candle: dict, prev_candle: Optional[dict]):
        tr = self._trailing.get(symbol)
        if not tr:
            return
        level = self.state.get_level(symbol)
        if not level:
            return

        side = tr["side"]

        # 2026-10-02: in-trade order-book watch (all strategies, 24/7).
        if await self._obi_watch(symbol, tr, candle):
            return

        # 2026-09-30: Liquidity Map trades use the SIMPLE exit they were
        # tested with -- stop at the sweep extreme, target at the next pool.
        # No breakeven, trailing, TP ladder, staged add or early failure.
        # (SL and TP are also set on the exchange; this is the bot-side check.)
        if tr.get("simple_exit"):
            sl_hit = candle['low'] <= tr["live_sl"] if side == 'BUY' else candle['high'] >= tr["live_sl"]
            tp_hit = candle['high'] >= tr["tp"] if side == 'BUY' else candle['low'] <= tr["tp"]
            lvl = tr.get("invalidate_level")
            closed_through = lvl is not None and (
                candle['close'] < lvl if side == 'BUY' else candle['close'] > lvl)
            if sl_hit:
                await self._exit_position(symbol, tr, exit_price=tr["live_sl"], reason="sl_hit",
                                          label="Hard Stop Hit (Liquidity Map)")
            elif closed_through:
                await self._exit_position(symbol, tr, exit_price=candle['close'], reason="level_invalidated",
                                          label=f"Level broken — candle closed "
                                                f"{'below' if side == 'BUY' else 'above'} {lvl:.6g} (Liquidity Map)")
            elif tp_hit:
                await self._exit_position(symbol, tr, exit_price=tr["tp"], reason="target_achieved",
                                          label="Take Profit — next liquidity pool reached")
            return

        # Track max favorable / max adverse excursion every candle, regardless
        # of exit priority outcome below — this is what makes the "if I'd
        # held longer" TP analysis possible without re-deriving it from raw
        # candle logs after the fact.
        if side == 'BUY':
            tr["mfe"] = max(tr.get("mfe", tr["entry"]), candle['high'])
            tr["mae"] = min(tr.get("mae", tr["entry"]), candle['low'])
        else:
            tr["mfe"] = min(tr.get("mfe", tr["entry"]), candle['low'])
            tr["mae"] = max(tr.get("mae", tr["entry"]), candle['high'])

        target = level.pdh if side == 'BUY' else level.pdl
        skip_target_priorities = tr.get("trend_mode", False)
        details = None  # fetched at most once per candle, reused across priorities

        # Runs regardless of trend_mode — this is the fix for the "worked
        # then reversed" loss pattern, and trend-mode trades (which skip the
        # target/rejection priorities entirely) need this protection even
        # more, since they otherwise only have SL + ROE.
        await self._check_breakeven_move(symbol, tr)
        await self._check_staged_addition(symbol, tr, candle, target)

        # 2026-09-14: early_invalidation REMOVED, replaced by
        # _check_early_failure_exit (pattern-based, first 3 candles / 45
        # min only, no R-multiple timer). _check_trend_trailing_stop is
        # unchanged -- replaces zone_reversal (removed 2026-09-02),
        # activates past full breakeven (1.0R), trails 0.3R behind
        # high-water instead of closing on a single rejection candle.
        if not tr.get('position_closed'):
            if EARLY_FAILURE_MODE == 'on':
                await self._check_early_failure_exit(symbol, tr, candle)
        if not tr.get('position_closed'):
            await self._check_trend_trailing_stop(symbol, tr, candle)

        if not skip_target_priorities and not tr.get('position_closed'):
            await self._check_partial_tp_ladder(symbol, tr, candle)

            if side == 'BUY' and candle['high'] >= target:
                await self._exit_position(symbol, tr, exit_price=target, reason="target_achieved",
                                           label="Take Profit – Target Achieved")
                return
            if side == 'SELL' and candle['low'] <= target:
                await self._exit_position(symbol, tr, exit_price=target, reason="target_achieved",
                                           label="Take Profit – Target Achieved")
                return

            near_target = (candle['high'] >= target * (1 - REJECTION_PROXIMITY_PCT) if side == 'BUY'
                           else candle['low'] <= target * (1 + REJECTION_PROXIMITY_PCT))

            if near_target:
                rejection = False
                if side == 'BUY':
                    rejection = _is_bearish_rejection(candle) or (
                        prev_candle is not None and _is_bearish_engulfing(prev_candle, candle))
                else:
                    rejection = _is_bullish_rejection(candle) or (
                        prev_candle is not None and _is_bullish_engulfing(prev_candle, candle))

                # Only fire as a profit-taking exit if the close is actually
                # favorable versus entry — otherwise it's just a rejection
                # candle near the target while underwater, not a real "Take
                # Profit" event. Fall through to ROE/stop-loss instead.
                is_profitable = (candle['close'] > tr['entry'] if side == 'BUY'
                                  else candle['close'] < tr['entry'])

                if rejection and is_profitable:
                    # Additionally require a meaningful ROE before allowing
                    # this early exit — see MIN_ROE_FOR_REJECTION_EXIT_PCT
                    # comment above. A marginal-profit rejection candle no
                    # longer bails out of a trade that might still run.
                    details = await self.coindcx.get_position_details(symbol)
                    roe = details.get("roe") if details else None
                    if roe is not None and roe >= MIN_ROE_FOR_REJECTION_EXIT_PCT:
                        await self._exit_position(symbol, tr, exit_price=candle['close'],
                                                   reason="rejection_exit",
                                                   label="Take Profit – Rejection Exit")
                        return
                    else:
                        logger.info(f"{symbol} | Rejection-exit conditions met but ROE "
                                   f"({roe if roe is not None else 'unknown'}%) is below the "
                                   f"{MIN_ROE_FOR_REJECTION_EXIT_PCT}% minimum — holding for more "
                                   f"confirmation instead of exiting early")

        # 2026-07-29: skip the blanket ROE-protection check once the TP
        # ladder has actually started filling (TP1 done). Confirmed via a
        # real AERO trade that ROE_TARGET_PCT (7%) sits BELOW TP1's own
        # ROE-equivalent for typical risk setups (~14-15%) — meaning ROE-
        # protection was firing on the exact same candle as TP1 and sweeping
        # up the entire remainder before TP2/TP3 ever got a chance, even
        # though price was still moving favorably, not reversing. Once TP1
        # has filled, the remainder's downside is already capped at
        # breakeven-or-better (via the staged ratchet above), so letting it
        # run toward TP2/TP3 without a lower-bar safety net cutting it short
        # is a deliberate choice, not an oversight — accepted tradeoff:
        # a reversal before TP2 now closes at breakeven instead of the
        # smaller-but-locked-in profit ROE-protection used to guarantee.
        ladder_already_running = tr.get("tp1_filled", False)

        # 2026-08-20 Fix 1: ROE-protection now partial-closes instead of
        # closing the whole position. Real evidence (a KAITOUSD trade)
        # showed price continuing well past the full-close point before
        # eventually reversing -- a full close has no way to participate
        # in that kind of continuation. Now: close 50% at the ROE
        # threshold (banking real profit, same as before), and let the
        # remainder ride behind a trailing stop instead of exiting
        # entirely. Skipped once already triggered (roe_partial_done) --
        # from then on _check_roe_trailing_stop (below) owns the exit.
        if not ladder_already_running and not tr.get("roe_partial_done", False):
            if details is None:
                details = await self.coindcx.get_position_details(symbol)
            if details and details.get("roe") is not None and details["roe"] >= ROE_TARGET_PCT:
                try:
                    live_positions = await self.coindcx.get_open_positions()
                except Exception as e:
                    logger.error(f"{symbol} | Failed to fetch live position for ROE partial close: {e}",
                                 exc_info=True)
                    return
                live_qty = abs(live_positions.get(symbol, 0))
                if live_qty <= 0:
                    self._log_close(symbol, tr, exit_price=None, reason="roe_protection_already_closed")
                    self.state.reset_symbol_watch(symbol)
                    self._trailing.pop(symbol, None)
                    self._open_positions.pop(symbol, None)
                    return
                close_qty = live_qty * 0.5
                success = await self.coindcx.close_position_market(symbol, side, close_qty)
                if success:
                    tr["roe_partial_done"] = True
                    tr["trailing_high_water"] = candle['close']
                    self._log_close(symbol, tr, exit_price=candle['close'], reason="roe_protection_partial",
                                     event_type="partial_close", qty=close_qty)
                    tr["qty_open"] = max(0.0, (tr.get("qty_open") or live_qty) - close_qty)
                    await self.telegram.send_alert(
                        f"✅ *Take Profit – ROE Protection (Partial)*\n\n"
                        f"*Symbol:* {symbol}\n*Side:* {'📈 LONG' if side == 'BUY' else '📉 SHORT'}\n"
                        f"*Entry:* {tr['entry']:.4f}\n*Exit price:* {candle['close']:.4f}\n"
                        f"*ROE:* {details['roe']:.2f}%\n*Closed:* ~50% of position\n\n"
                        f"Remainder now running behind a trailing stop instead of closing "
                        f"entirely, so a further favorable move can still be captured."
                    )
                    logger.info(f"{symbol} | ROE-protection partial close at {candle['close']:.4f} "
                               f"(qty {close_qty:.6f}, ROE {details['roe']:.2f}%)")
                else:
                    logger.error(f"{symbol} | ROE-protection partial close order failed — "
                                f"will retry next candle if ROE still qualifies")
                return

        if tr.get("roe_partial_done", False):
            await self._check_roe_trailing_stop(symbol, tr, candle)


    async def _check_roe_trailing_stop(self, symbol: str, tr: dict, candle: dict):
        """Owns the exit for the remainder left after an ROE-protection
        partial close. Trails behind the best price seen since the partial
        close by TRAILING_STOP_R_BUFFER (a fraction of the ORIGINAL entry
        risk) -- ratchets forward only, never loosens. Closes the full
        remainder if price closes back through the trailing level."""
        side = tr["side"]
        entry, sl = tr["entry"], tr["sl"]
        original_risk = abs(entry - sl)
        if original_risk <= 0:
            return

        high_water = tr.get("trailing_high_water", candle['close'])
        if side == 'BUY':
            if candle['close'] > high_water:
                high_water = candle['close']
            trail_sl = high_water - (TRAILING_STOP_R_BUFFER * original_risk)
            hit = candle['close'] < trail_sl
        else:
            if candle['close'] < high_water:
                high_water = candle['close']
            trail_sl = high_water + (TRAILING_STOP_R_BUFFER * original_risk)
            hit = candle['close'] > trail_sl

        tr["trailing_high_water"] = high_water

        if hit:
            await self._exit_position(symbol, tr, exit_price=candle['close'],
                                       reason="roe_trailing_stop",
                                       label="Position Closed — Trailing Stop (post-ROE remainder)")
            return

        new_trail_sl = trail_sl
        if new_trail_sl != tr.get("live_sl"):
            try:
                success = await self.coindcx.update_stop_loss(symbol, new_sl_price=new_trail_sl)
                if success:
                    tr["live_sl"] = new_trail_sl
                    logger.info(f"{symbol} | Trailing stop (post-ROE remainder) moved to {new_trail_sl:.4f}")
            except Exception as e:
                logger.error(f"{symbol} | Failed to update trailing stop: {e}", exc_info=True)
