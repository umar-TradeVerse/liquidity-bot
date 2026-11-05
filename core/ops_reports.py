"""Telegram operations reports (2026-11-05) — run the bot from Telegram.

Built so day-to-day operation needs no server console:
  /logs [hours]  combined bot log for the last N hours (default 24, max 168)
  /status        running? since when, code version, open positions, today's P&L
  /settings      the active configuration — NEVER secrets (allow-list only)
  nightly        at 23:00 IST the last 24h of logs is sent automatically
"""
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone

from utils.logger import COMBINED_LOG

STARTED = time.time()
IST = timezone(timedelta(hours=5, minutes=30))

# Only these settings may ever be shown. Keys/secrets/tokens are never listed.
SETTINGS_SHOWN = (
    "TRADING_MODE", "CANDLE_SOURCE", "TRADE_SIZE_USD", "SIMPLE_MODE", "POOL_EQUAL_MODE", "POOL_SESSION_MODE",
    "LIQMAP_MODE", "LIQMAP_MIN_SWEEP_PCT", "BREAKOUT_MODE", "OBI_EXIT_MODE",
    "EARLY_FAILURE_MODE", "ORDERFLOW_SKIP", "USD_INR_RATE", "DEBUG",
)
_SECRET_HINTS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "CHAT_ID")


def build_log_file(hours: float = 24, now: datetime = None) -> str:
    """Write the combined log lines from the last `hours` into one file and
    return its path (None if there are no log files yet). Log timestamps are
    the server's clock (UTC), the same as the old Railway logs."""
    hours = max(1.0, min(float(hours), 168.0))
    now = now or datetime.utcnow()
    cutoff = (now - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    files = [f"{COMBINED_LOG}.{i}" for i in range(10, 0, -1)] + [COMBINED_LOG]   # oldest first
    files = [f for f in files if os.path.exists(f)]
    if not files:
        return None
    out = os.path.join(os.path.dirname(COMBINED_LOG) or ".",
                       f"logs_{now.strftime('%Y-%m-%d_%H%M')}_last{int(hours)}h.log")
    keep = False
    with open(out, "w", encoding="utf-8") as w:
        for f in files:
            with open(f, encoding="utf-8", errors="replace") as r:
                for line in r:
                    ts = line[:19]
                    if len(ts) == 19 and ts[4] == "-" and ts[13] == ":":
                        keep = ts >= cutoff          # continuation lines follow their entry
                    if keep:
                        w.write(line)
    return out


def _version() -> str:
    try:
        return subprocess.run(["git", "log", "-1", "--format=%h %s (%cr)"], capture_output=True,
                              text=True, timeout=5).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def status_text(monitor=None) -> str:
    up = timedelta(seconds=int(time.time() - STARTED))
    now = datetime.now(IST)
    hm = now.hour * 60 + now.minute
    window = "OPEN (05:30–23:00 IST)" if 5 * 60 + 30 <= hm < 23 * 60 else "closed — managing open trades only"
    from core.paper_exchange import TRADING_MODE
    lines = ["🟢 *Bot status*", f"Running for {up}",
             f"Trading: {'📝 PAPER — simulated, no real orders' if TRADING_MODE == 'paper' else '💰 LIVE — real orders'}",
             f"Entry hours: {window}", f"Code: `{_version()}`"]
    if monitor is not None:
        tr = getattr(monitor, "_trailing", {}) or {}
        if tr:
            lines.append(f"Open positions ({len(tr)}):")
            for sym, t in tr.items():
                lines.append(f"  • {sym} {'LONG' if t.get('side') == 'BUY' else 'SHORT'} @ {t.get('entry')} "
                             f"(SL {t.get('live_sl', t.get('sl'))}, TP {t.get('tp')})")
        else:
            lines.append("Open positions: none")
        risk = getattr(monitor, "risk", None)
        if risk is not None:
            try:
                from core.agents.risk_agent import USD_TO_INR_RATE, DAILY_LOSS_LIMIT_INR
                usd = float(getattr(risk, "realised_usd", 0.0))
                lines.append(f"Today's realised P&L: ${usd:+.2f} (₹{usd * USD_TO_INR_RATE:+,.0f})"
                             f" — daily halt at -₹{DAILY_LOSS_LIMIT_INR:,.0f}"
                             f"{' — ⛔ HALTED' if getattr(risk, 'halted', False) else ''}")
            except Exception:
                pass
    return "\n".join(lines)


def _candle_source() -> str:
    from exchange.coindcx import CANDLE_SOURCE
    return CANDLE_SOURCE


def settings_text() -> str:
    from core import simple_mode
    from core.agents.risk_agent import USD_TO_INR_RATE
    import core.strategy as S
    lines = ["⚙️ *Active settings* (secrets are never shown)"]
    for k in SETTINGS_SHOWN:
        if any(h in k for h in _SECRET_HINTS):
            continue                                   # defence in depth
        v = os.getenv(k)
        lines.append(f"`{k}` = {v if v is not None else '(default)'}")
    from core.paper_exchange import TRADING_MODE
    lines += ["", "*Effective values*",
              f"Trading: {TRADING_MODE.upper()}"
              f"{' — simulated, no real orders' if TRADING_MODE == 'paper' else ' — real orders'}",
              f"Price data: {_candle_source().upper()} candles + order book",
              f"Simple mode: {'ON' if simple_mode.ON else 'OFF'} — entry delay "
              f"{S.ENTRY_DELAY_CANDLES} candle(s), target {simple_mode.TP_R}R",
              f"Min sweep depth (main strategy): {S.MIN_LIQUIDITY_SWEEP_PCT * 100:.2f}% "
              f"(every sweep must still go {S.MIN_SWEEP_DEPTH_PCT * 100:.1f}% past the level)",
              f"USD→INR rate: {USD_TO_INR_RATE}",
              "", "_To change a setting: server console →_ `nano /etc/bots/liquidity-bot.env` "
              "_then_ `systemctl restart liquidity-bot`"]
    return "\n".join(lines)
