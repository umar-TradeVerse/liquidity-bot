async def get_position_details(self, symbol: str) -> Optional[dict]:
        """
        Fetch this symbol's open position details, including CoinDCX's own
        ROE field (used for Priority-3 exits), rather than recomputing ROE
        manually from stored entry price.

        NOTE: the exact field name for ROE in CoinDCX's response has not
        been confirmed from available docs — "roe" is the best guess based
        on naming conventions. The raw entry is logged on first use so you
        can verify/correct the field name from real data if needed.
        """
        coindcx_symbol = SYMBOL_MAP.get(symbol)
        if not coindcx_symbol:
            return None

        timestamp = int(time.time() * 1000)
        body = {"timestamp": timestamp}
        result = await self._post("/exchange/v1/derivatives/futures/positions", body)
        entries = result if isinstance(result, list) else []

        for entry in entries:
            if not isinstance(entry, dict) or entry.get("pair") != coindcx_symbol:
                continue
            active = entry.get("active_pos", 0) or 0
            try:
                active = float(active)
            except (TypeError, ValueError):
                active = 0.0
            if active != 0:
                roe_raw = entry.get("roe")
                roe = None
                if roe_raw is not None:
                    try:
                        roe = float(roe_raw)
                    except (TypeError, ValueError):
                        roe = None
                if roe is None:
                    logger.warning(f"{symbol} | 'roe' field missing/unparsable in position "
                                   f"response — raw entry for verification: {entry}")
                return {"id": entry.get("id"), "active_pos": active, "roe": roe, "raw": entry}
        return None

    async def close_position_market(self, symbol: str, side: str, quantity: float) -> bool:
        """
        Close an open position via an opposite-side reduce-only market
        order. `side` = the ORIGINAL position's side ('BUY' or 'SELL') —
        this flips it automatically for the closing order.

        NOTE: "reduce_only" support/behavior on CoinDCX's order-create
        endpoint has not been confirmed from available docs. Watch the
        first live close closely — if it doesn't fully flatten the
        position (or opens opposite exposure instead), this needs a
        follow-up fix.
        """
        coindcx_symbol = SYMBOL_MAP.get(symbol)
        if not coindcx_symbol:
            logger.error(f"Unknown symbol: {symbol}")
            return False

        close_side = "sell" if side.upper() == "BUY" else "buy"

        instrument = await self._get_instrument_details(coindcx_symbol)
        if instrument and instrument["quantity_increment"] > 0:
            quantity = self._round_to_increment(quantity, instrument["quantity_increment"])

        timestamp = int(time.time() * 1000)
        body = {
            "timestamp": timestamp,
            "order": {
                "side": close_side,
                "pair": coindcx_symbol,
                "order_type": "market_order",
                "total_quantity": quantity,
                "notification": "email_notification",
                "time_in_force": "good_till_cancel",
                "hidden": False,
                "post_only": False,
                "reduce_only": True
            }
        }

        result = await self._post("/exchange/v1/derivatives/futures/orders/create", body)
        if result:
            logger.info(f"{symbol} | Position closed via reduce-only {close_side} market order")
            return True

        logger.error(f"{symbol} | Failed to close position: {self.last_error}")
        return False

    async def update_position_tpsl(self, symbol: str, sl_price: Optional[float] = None,
                                    tp_price: Optional[float] = None) -> bool:
        """
        Set/update stop-loss and/or take-profit on an open position via
        CoinDCX's create_tpsl endpoint. Pass only the price(s) you want to
        set — the take_profit object's shape is mirrored from the working
        stop_loss shape as the most likely format; NOT independently
        confirmed. Watch the first live take-profit call's response
        closely — if it errors or doesn't behave as expected, this needs
        a follow-up fix. Priority-1 exits are still enforced as a backup
        by the bot's own candle-close check either way (see monitor.py),
        so a failure here doesn't leave the position unmanaged.
        """
        if sl_price is None and tp_price is None:
            return False

        coindcx_symbol = SYMBOL_MAP.get(symbol)
        if not coindcx_symbol:
            logger.error(f"{symbol} | Unknown symbol, cannot update TP/SL")
            return False

        timestamp = int(time.time() * 1000)
        positions_result = await self._post(
            "/exchange/v1/derivatives/futures/positions",
            {"timestamp": timestamp}
        )
        entries = positions_result if isinstance(positions_result, list) else []

        position_id = None
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("pair") != coindcx_symbol:
                continue
            active = entry.get("active_pos", 0) or 0
            try:
                active = float(active)
            except (TypeError, ValueError):
                active = 0.0
            if active != 0:
                position_id = entry.get("id")
                break

        if not position_id:
            logger.error(f"{symbol} | Could not find an open position id — TP/SL not updated")
            return False

        instrument = await self._get_instrument_details(coindcx_symbol)
        if instrument and instrument["price_increment"] > 0:
            if sl_price is not None:
                sl_price = self._round_to_increment(sl_price, instrument["price_increment"], mode="nearest")
            if tp_price is not None:
                tp_price = self._round_to_increment(tp_price, instrument["price_increment"], mode="nearest")

        body = {"timestamp": timestamp, "id": position_id}
        if sl_price is not None:
            body["stop_loss"] = {"stop_price": sl_price, "limit_price": sl_price, "order_type": "stop_market"}
        if tp_price is not None:
            body["take_profit"] = {"stop_price": tp_price, "limit_price": tp_price, "order_type": "take_profit_market"}

        result = await self._post("/exchange/v1/derivatives/futures/positions/create_tpsl", body)
        if result:
            logger.info(f"{symbol} | TP/SL updated — SL:{sl_price} TP:{tp_price} (position {position_id})")
            return True

        logger.error(f"{symbol} | Failed to update TP/SL: {self.last_error}")
        return False
