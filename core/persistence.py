"""
persistence.py — JSON-file state snapshot + trade-event logging on a mounted volume.

Two responsibilities, kept deliberately simple (no DB, no ORM):

1. State snapshot (state_snapshot.json)
   Written periodically from the monitor loop. Captures state.levels (all
   DailyLevel fields — pure dataclasses, JSON-safe) and the monitor's
   in-memory _trailing dict (per-symbol entry/sl/tp/mfe/mae tracking).
   On startup, restore_state() loads this back IF the saved date matches
   today (IST) — a stale snapshot from a previous day is deliberately
   ignored, since the normal 5:30 AM daily_reset() already handles that
   transition correctly.

2. Trade event log (trades.jsonl)
   Append-only. One line per open/close event. Never overwritten, never
   truncated by the snapshot logic above — this is the durable trade
   history independent of Railway's log retention entirely.

Both files live under PERSIST_DIR (default /data, i.e. the Railway
volume mount). If the directory doesn't exist (e.g. running locally
without a volume), both functions degrade to no-ops with a warning
rather than crashing the bot.
"""
import os
import json
import dataclasses
from datetime import date, datetime
from typing import Optional
import pytz

from utils.logger import setup_logger

logger = setup_logger("persistence")
IST = pytz.timezone("Asia/Kolkata")

PERSIST_DIR = os.getenv("PERSIST_DIR", "/data")
STATE_SNAPSHOT_PATH = os.path.join(PERSIST_DIR, "state_snapshot.json")
TRADES_LOG_PATH = os.path.join(PERSIST_DIR, "trades.jsonl")

_dir_checked = False


def _ensure_dir_available() -> bool:
    """Checks PERSIST_DIR exists and is writable. Logs once, not every call."""
    global _dir_checked
    if os.path.isdir(PERSIST_DIR) and os.access(PERSIST_DIR, os.W_OK):
        return True
    if not _dir_checked:
        logger.warning(
            f"PERSIST_DIR '{PERSIST_DIR}' does not exist or isn't writable — "
            f"state persistence and trade logging are disabled this run. "
            f"On Railway, add a Volume mounted at {PERSIST_DIR}."
        )
        _dir_checked = True
    return False


def save_state(state, trailing: dict):
    """Write current levels + trailing snapshot. Safe to call every poll cycle."""
    if not _ensure_dir_available():
        return
    try:
        snapshot = {
            "saved_at_ist": datetime.now(IST).isoformat(),
            "date_ist": date.today().isoformat(),
            "levels": {
                sym: (dataclasses.asdict(lvl) if lvl is not None else None)
                for sym, lvl in state.levels.items()
            },
            "trailing": trailing,
        }
        tmp_path = STATE_SNAPSHOT_PATH + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(snapshot, f)
        os.replace(tmp_path, STATE_SNAPSHOT_PATH)
    except Exception as e:
        logger.error(f"Failed to save state snapshot: {e}", exc_info=True)


def load_state() -> Optional[dict]:
    """
    Returns {"levels": {...}, "trailing": {...}} if a same-day snapshot exists,
    else None. Caller is responsible for turning the raw level dicts back into
    DailyLevel objects (avoids a circular import between state.py and this module).
    """
    if not _ensure_dir_available():
        return None
    if not os.path.exists(STATE_SNAPSHOT_PATH):
        logger.info("No state snapshot found — starting fresh.")
        return None
    try:
        with open(STATE_SNAPSHOT_PATH, "r") as f:
            snapshot = json.load(f)
    except Exception as e:
        logger.error(f"Failed to read state snapshot, ignoring it: {e}", exc_info=True)
        return None

    if snapshot.get("date_ist") != date.today().isoformat():
        logger.info(
            f"State snapshot is from {snapshot.get('date_ist')}, not today "
            f"({date.today().isoformat()}) — ignoring, daily_reset will run normally."
        )
        return None

    logger.info(
        f"Restoring state snapshot saved at {snapshot.get('saved_at_ist')} "
        f"({len(snapshot.get('trailing', {}))} open position(s) tracked)"
    )
    return {"levels": snapshot.get("levels", {}), "trailing": snapshot.get("trailing", {})}


def get_pattern_stats(trend_mode: bool) -> dict:
    """
    Aggregates trades.jsonl by PATTERN TYPE (trend_mode True/False) across
    ALL symbols, rather than per-symbol. This answers "does this kind of
    setup tend to work" instead of "has this specific coin happened to
    work 5 times" — pools data across all 8 symbols so useful numbers
    accumulate much faster than a per-symbol breakdown would.

    trend_mode=False -> regular sweep-reversal setups (the core strategy)
    trend_mode=True  -> trend-aligned flip setups (the reactive, in-trend entries)

    Returns has_enough_data=False if there are fewer than
    MIN_TRADES_FOR_HISTORICAL_STATS resolved closes of this pattern type —
    the caller should show a plain "not enough history yet" line rather
    than a fabricated-looking precise percentage from a tiny sample.
    """
    from core.monitor import MIN_TRADES_FOR_HISTORICAL_STATS  # local import avoids a cycle

    empty = {
        "has_enough_data": False, "count": 0, "win_rate_pct": None,
        "avg_move_pct": None, "largest_move_pct": None, "avg_hold_minutes": None,
    }
    if not _ensure_dir_available() or not os.path.exists(TRADES_LOG_PATH):
        return empty

    closes = []
    try:
        with open(TRADES_LOG_PATH, "r") as f:
            for line in f:
                try:
                    event = json.loads(line)
                except Exception:
                    continue
                if event.get("event_type") == "close" and event.get("trend_mode") == trend_mode:
                    closes.append(event)
    except Exception as e:
        logger.error(f"Failed to read trades.jsonl for pattern stats: {e}", exc_info=True)
        return empty

    resolved = [c for c in closes if c.get("realized_rr") is not None]
    if len(resolved) < MIN_TRADES_FOR_HISTORICAL_STATS:
        return {**empty, "count": len(closes)}

    wins = [c for c in resolved if c["realized_rr"] > 0]
    move_pcts = []
    for c in resolved:
        entry, exit_price, side = c.get("entry"), c.get("exit_price"), c.get("side")
        if entry and exit_price:
            move = (exit_price - entry) if side == "BUY" else (entry - exit_price)
            move_pcts.append(100 * move / entry)
    durations = [c["duration_minutes"] for c in resolved if c.get("duration_minutes") is not None]

    return {
        "has_enough_data": True,
        "count": len(resolved),
        "win_rate_pct": round(100 * len(wins) / len(resolved), 0),
        "avg_move_pct": round(sum(move_pcts) / len(move_pcts), 2) if move_pcts else None,
        "largest_move_pct": round(max(move_pcts, key=abs), 2) if move_pcts else None,
        "avg_hold_minutes": round(sum(durations) / len(durations), 0) if durations else None,
    }


def log_trade_event(event: dict):
    """Append one JSON line to trades.jsonl. event should include at minimum:
    event_type ('open'|'close'), symbol, side, timestamp_ist.
    """
    if not _ensure_dir_available():
        return
    try:
        event = dict(event)
        event.setdefault("logged_at_ist", datetime.now(IST).isoformat())
        with open(TRADES_LOG_PATH, "a") as f:
            f.write(json.dumps(event) + "\n")
    except Exception as e:
        logger.error(f"Failed to append trade event: {e}", exc_info=True)
