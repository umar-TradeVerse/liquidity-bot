"""Download CoinDCX 15m candles (public endpoints, no API key) for the replay.

    python scripts/replay/fetch_candles.py OUT_DIR [DAYS] [--source spot|futures]

spot    = /market_data/candles ("B-" pairs) -- the feed the bot reads by default
futures = /market_data/candlesticks?pcode=f -- CoinDCX futures (CANDLE_SOURCE=futures)
Writes OUT_DIR/<SYMBOL>.json: [{time (ms, candle open), open, high, low, close, volume}, ...]
oldest first. A failed chunk is skipped and reported, never silently filled.
"""
import json
import os
import sys
import time
import urllib.request

PAIRS = {
    "ETHUSD": "B-ETH_USDT", "SOLUSD": "B-SOL_USDT", "XRPUSD": "B-XRP_USDT",
    "TAOUSD": "B-TAO_USDT", "AEROUSD": "B-AERO_USDT", "KAITOUSD": "B-KAITO_USDT",
    "RIFUSD": "B-RIF_USDT", "ZAMAUSD": "B-ZAMA_USDT", "BTCUSD": "B-BTC_USDT",
}
SPOT = "https://public.coindcx.com/market_data/candles?pair={p}&interval=15m&startTime={f}&endTime={t}&limit=1000"
FUT = "https://public.coindcx.com/market_data/candlesticks?pair={p}&from={f}&to={t}&resolution=15&pcode=f"


def fetch(source, pair, f, t):
    url = (SPOT.format(p=pair, f=f * 1000, t=t * 1000 - 1) if source == "spot"
           else FUT.format(p=pair, f=f, t=t))
    # Python's default user-agent is refused (403); curl's is not.
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8.7.1"})
    last = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.load(r)
            if source == "spot":
                return d if isinstance(d, list) else []
            return (d.get("data") or []) if isinstance(d, dict) else []
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 + attempt * 3)
    print(f"  WARN {pair} {f}-{t}: {last} — chunk skipped", flush=True)
    return []


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    source = sys.argv[sys.argv.index("--source") + 1] if "--source" in sys.argv else "spot"
    if source in args:
        args.remove(source)
    out = args[0]
    days = int(args[1]) if len(args) > 1 else 400
    chunk = (10 if source == "spot" else 30) * 86400      # spot replies are capped at 1000 candles
    os.makedirs(out, exist_ok=True)
    now = int(time.time())
    for sym, pair in PAIRS.items():
        rows = {}
        f = now - days * 86400
        while f < now:
            t = min(f + chunk, now)
            for c in fetch(source, pair, f, t):
                rows[int(c["time"])] = {k: float(c[k]) for k in ("open", "high", "low", "close", "volume")}
            f = t
            time.sleep(0.25)
        # drop the still-forming candle
        candles = [dict(time=k, **v) for k, v in sorted(rows.items()) if k + 900 * 1000 <= now * 1000]
        with open(os.path.join(out, f"{sym}.json"), "w") as fh:
            json.dump(candles, fh)
        first = time.strftime("%Y-%m-%d", time.gmtime(candles[0]["time"] / 1000)) if candles else "-"
        print(f"{sym}: {len(candles)} candles from {first}", flush=True)


if __name__ == "__main__":
    main()
