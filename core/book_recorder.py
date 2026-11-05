"""Order-book recorder (2026-11-06) — LOG-ONLY research data.

Purpose: build the order-book history needed to test the "wall hunt" idea
(a large resting order near a level is where stop-hunts cluster: watch for
price to sweep through the wall, then trade the reversal). CoinDCX keeps no
book history, so nothing can be replayed until we record it ourselves.

What it does: on every monitor poll (~15 s), for each coin whose last close
is within NEAR_PCT of a level the bot tracks (PDH/PDL and the Liquidity Map's
active levels), it fetches the 50-level FUTURES book in the background and
appends one line to PERSIST_DIR/orderbook/YYYY-MM-DD.jsonl.gz (UTC date):

  {"t": ms, "s": "SOLUSD", "c": last close, "lv": [["PDH", 98.14], ["LM", 95.2], ...],
   "b": [[price, qty], ... best first], "a": [[price, qty], ... best first]}

It never trades, never vetoes, never blocks the main loop (fetches run as
background tasks, one per coin at a time), and any error is logged and
skipped. Files older than KEEP_DAYS are deleted. BOOK_RECORDER=off disables it.

Book reach (measured 5 Nov 2026): ETH ±0.02%, TAO ±0.23%, XRP/SOL/ZAMA/AERO/RIF
±0.4–0.9%, KAITO ±2.4% — walls are only visible close to price.
"""
import asyncio
import gzip
import json
import os
import time
from datetime import datetime, timedelta, timezone

from utils.logger import setup_logger

logger = setup_logger("book_recorder")

BOOK_RECORDER = os.getenv("BOOK_RECORDER", "on").strip().lower() != "off"
NEAR_PCT = 1.0          # record while price is within 1% of a tracked level
KEEP_DAYS = 45


class BookRecorder:
    def __init__(self, coindcx, persist_dir=None):
        self.cdx = coindcx
        self.dir = os.path.join(persist_dir or os.getenv("PERSIST_DIR", "/data"), "orderbook")
        self._busy = set()          # coins with a fetch in flight
        self._tasks = set()
        self._day = None
        self.count = 0              # snapshots written today
        self.failures = 0

    @staticmethod
    def levels_near(close, levels):
        """[(name, price)] of levels within NEAR_PCT of close."""
        return [(n, p) for n, p in levels
                if p and close and abs(close - p) / close * 100 <= NEAR_PCT]

    def maybe_record(self, symbol, close, levels):
        """Called from the monitor loop. Returns at once; the fetch and the
        write happen in a background task."""
        if not BOOK_RECORDER or symbol in self._busy:
            return
        near = self.levels_near(close, levels)
        if not near:
            return
        self._busy.add(symbol)
        t = asyncio.ensure_future(self._record(symbol, close, near))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _record(self, symbol, close, near):
        try:
            book = await self.cdx.get_futures_book(symbol)
            if not book:
                self.failures += 1
                return
            now = int(time.time() * 1000)
            row = {"t": now, "s": symbol, "c": close,
                   "lv": [[n, p] for n, p in near],
                   "b": [[p, q] for p, q in sorted(book["bids"].items(), reverse=True)],
                   "a": [[p, q] for p, q in sorted(book["asks"].items())]}
            self._write(now, row)
        except Exception as e:
            self.failures += 1
            logger.warning(f"{symbol} | order-book recorder skipped a snapshot ({e})")
        finally:
            self._busy.discard(symbol)

    def _write(self, now_ms, row):
        day = datetime.fromtimestamp(now_ms / 1000, timezone.utc).strftime("%Y-%m-%d")
        if day != self._day:
            if self._day is not None:
                logger.info(f"Order-book recorder: {self.count} snapshots on {self._day} "
                            f"({self.failures} failed fetches)")
            self._day, self.count, self.failures = day, 0, 0
            self._prune(now_ms)
        os.makedirs(self.dir, exist_ok=True)
        with gzip.open(os.path.join(self.dir, f"{day}.jsonl.gz"), "at") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
        self.count += 1

    def _prune(self, now_ms):
        cutoff = (datetime.fromtimestamp(now_ms / 1000, timezone.utc)
                  - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
        try:
            for name in os.listdir(self.dir):
                if name.endswith(".jsonl.gz") and name[:10] < cutoff:
                    os.remove(os.path.join(self.dir, name))
        except FileNotFoundError:
            pass
