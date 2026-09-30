"""Risk Agent — can this trade be afforded?

Owns: hard-rule rejection (R:R, sweep depth — computed by Structure),
one-auto-trade-per-symbol-per-day, DAILY LOSS HALT, max concurrent
positions, INR risk tier sizing.

Daily loss halt (added 2026-09-28):
  * Counts REALISED losses only, fees included, from every close:
    full exits, partial TP/ROE closes, and exchange-side SL hits detected
    by reconciliation.
  * Trading day = UTC date, which starts at 05:30 IST — the same moment
    the bot's daily level reset runs, so "today" means the same thing
    everywhere.
  * When realised net P&L for the day <= -DAILY_LOSS_LIMIT_USD, NEW
    entries are blocked for the rest of the day. Open positions keep
    being managed normally to their own SL/TP.
  * Persisted to PERSIST_DIR/daily_pnl.json so a redeploy/restart
    mid-day cannot reset the counter.
"""
import json
import logging
import os
from datetime import datetime, timezone

from core.agents.decision import Verdict

from utils.logger import setup_logger
logger = setup_logger("risk_agent")   # bot's own logger -> visible in Railway
AGENT = "Risk"

DAILY_LOSS_LIMIT_INR = 2000
USD_TO_INR_RATE = 88.0                       # must match monitor.USD_TO_INR_RATE
DAILY_LOSS_LIMIT_USD = round(DAILY_LOSS_LIMIT_INR / USD_TO_INR_RATE, 2)   # 22.73
TAKER_FEE_PCT = 0.059                        # CoinDCX alt-perp taker, per side

_PERSIST_DIR = os.getenv("PERSIST_DIR", "/data")
_PNL_PATH = os.path.join(_PERSIST_DIR, "daily_pnl.json")


def trading_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class RiskAgent:
    def __init__(self):
        self.day = trading_day()
        self.realised_usd = 0.0
        self.halt_alerted = False
        self.seen = []            # keys of closes already booked (any day)
        self._load()

    # ── persistence ───────────────────────────────────────────────────
    def _load(self):
        try:
            with open(_PNL_PATH) as f:
                d = json.load(f)
            self.seen = list(d.get("seen", []))
            if d.get("day") == self.day:
                self.realised_usd = float(d.get("realised_usd", 0.0))
                self.halt_alerted = bool(d.get("halt_alerted", False))
                logger.info(f"Daily P&L restored for {self.day}: ${self.realised_usd:+.2f}")
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.error(f"Could not read {_PNL_PATH}: {e}")

    def _save(self):
        try:
            os.makedirs(_PERSIST_DIR, exist_ok=True)
            with open(_PNL_PATH, "w") as f:
                json.dump({"day": self.day, "realised_usd": round(self.realised_usd, 4),
                           "halt_alerted": self.halt_alerted, "seen": self.seen}, f)
        except Exception as e:
            logger.error(f"Could not write {_PNL_PATH}: {e}")

    def _roll(self):
        today = trading_day()
        if today != self.day:
            logger.info(f"Daily P&L rollover {self.day} -> {today} "
                        f"(closed at ${self.realised_usd:+.2f}) — halt cleared")
            self.day, self.realised_usd, self.halt_alerted = today, 0.0, False
            self._save()

    # ── P&L recording ─────────────────────────────────────────────────
    def record_close(self, symbol: str, side: str, entry, exit_price, qty,
                     closed_at_ms=None, key=None):
        """Record one realised close (full or partial). Returns net USD.

        closed_at_ms: the exchange fill time. If the close actually happened
        on an EARLIER trading day (e.g. detected after a restart or outage),
        it is NOT added to today's total -- so an old loss can never push
        today's daily halt. (2026-09-28, fix B)"""
        self._roll()
        # 2026-10-01 fix: the same close must never be booked twice (seen
        # after restarts replaying an out-of-date snapshot). Returns None.
        if key and key in self.seen:
            logger.info(f"{symbol} | Daily P&L: duplicate close ignored ({key})")
            return None
        if key and entry and exit_price and qty and qty > 0:
            self.seen.append(key); self.seen = self.seen[-300:]
        close_day = None
        if closed_at_ms:
            try:
                close_day = datetime.fromtimestamp(float(closed_at_ms) / 1000,
                                                   timezone.utc).strftime("%Y-%m-%d")
            except Exception:
                close_day = None
        if not entry or not exit_price or not qty or qty <= 0:
            logger.warning(f"{symbol} | Daily P&L: close not counted — missing "
                           f"entry/exit/qty (entry={entry}, exit={exit_price}, qty={qty})")
            return 0.0
        gross = (exit_price - entry) * qty if side == "BUY" else (entry - exit_price) * qty
        fees = (entry * qty + exit_price * qty) * TAKER_FEE_PCT / 100
        net = gross - fees
        if close_day and close_day != self.day:
            logger.info(f"{symbol} | Daily P&L: {net:+.2f} USD closed on {close_day} (earlier "
                        f"trading day) — not counted toward today's ({self.day}) halt")
            return net
        self.realised_usd += net
        self._save()
        logger.info(f"{symbol} | Daily P&L: {net:+.2f} USD (gross {gross:+.2f}, fees {fees:.2f}) "
                    f"-> day total ${self.realised_usd:+.2f} / limit -${DAILY_LOSS_LIMIT_USD:.2f}")
        return net

    @property
    def halted(self) -> bool:
        self._roll()
        return self.realised_usd <= -DAILY_LOSS_LIMIT_USD

    # ── verdicts ──────────────────────────────────────────────────────
    def pre_check(self, signal, level) -> Verdict:
        """Checks that run BEFORE the Context Agent (same order as before)."""
        if signal.reject_reason:
            return Verdict(AGENT, False, f"hard rule: {signal.reject_reason}")
        # Pool-agent signals have their own daily slot (tracked in pool_agent.py)
        if level and level.auto_traded_today and getattr(signal, 'source', '') != 'pool':
            return Verdict(AGENT, False, "today's auto-trade for this symbol already used")
        if self.halted:
            return Verdict(AGENT, False,
                           f"DAILY LOSS HALT — day P&L ${self.realised_usd:+.2f} "
                           f"<= -${DAILY_LOSS_LIMIT_USD:.2f} (₹{DAILY_LOSS_LIMIT_INR})")
        return Verdict(AGENT, True, f"day P&L ${self.realised_usd:+.2f}")

    @staticmethod
    def concurrency(open_count: int, max_positions: int) -> Verdict:
        if open_count >= max_positions:
            return Verdict(AGENT, False, f"{open_count}/{max_positions} positions open")
        return Verdict(AGENT, True)

    @staticmethod
    def inr_tier(entry, sl, margin_usd, leverage, full_max_inr, staged_max_inr, usd_inr):
        """Returns (tier, risk_usd, risk_inr); tier is FULL / STAGED / SKIP."""
        risk_usd = (abs(entry - sl) / entry) * margin_usd * leverage
        risk_inr = risk_usd * usd_inr
        if risk_inr > staged_max_inr:
            return "SKIP", risk_usd, risk_inr
        if risk_inr >= full_max_inr:
            return "STAGED", risk_usd, risk_inr
        return "FULL", risk_usd, risk_inr
