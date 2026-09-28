"""2026-09-03: Order Book Imbalance (OBI) — informational tracker only.

    OBI = (bid_volume - ask_volume) / (bid_volume + ask_volume)

Summed over the top N price levels on each side. Ranges from -1.0 (all ask,
no bid support) to +1.0 (all bid, no ask resistance). The idea being tested:
a genuine liquidity hunt should show real resting size defending the level
(high |OBI| in the direction of the eventual reversal), while a fake sweep
that simply continues has no such defense.

This is UNPROVEN. It exists purely to accumulate real evidence, the same
way earlier informational trackers did before any was ever
allowed to touch a trade decision -- and both of those turned out to need
real correction). Nothing here gates a signal. It is log-only.
"""


def compute_obi(bids: dict, asks: dict, levels: int = 5) -> float:
    """bids/asks are {price: quantity} dicts, as returned by
    CoinDCXClient.get_orderbook(). Sums quantity across the top `levels`
    price points on each side (highest bids, lowest asks -- i.e. closest
    to the touch, matching how depth is actually distributed on a real
    book) and returns the imbalance ratio. Returns None if either side is
    empty after taking the top `levels`, since the ratio is meaningless
    with zero volume on a side.
    """
    if not bids or not asks:
        return None

    top_bids = sorted(bids.keys(), reverse=True)[:levels]
    top_asks = sorted(asks.keys())[:levels]

    bid_volume = sum(bids[p] for p in top_bids)
    ask_volume = sum(asks[p] for p in top_asks)

    total = bid_volume + ask_volume
    if total <= 0:
        return None

    return (bid_volume - ask_volume) / total


# ── 2026-09-28: order-book WALLS and GAPS (log-only, Context Agent) ───
# Ported from the order-book analytics in the crypto-liquidity-ai-trading-bot
# fork (trade/engines/orderbookAnalytics.js: detectWalls / detectGaps),
# same default thresholds. Purpose: at the moment a PDH/PDL sweep arms,
# record WHERE resting size sits (a wall) and where the book is thin (a
# gap) -- data to test later against outcomes, exactly like OBI. Never
# gates a trade.

def detect_walls(bids: dict, asks: dict, levels: int = 12, share_pct: float = 18.0) -> list:
    """A wall = one price level holding >= share_pct of the visible depth
    on its own side (top `levels`). Returns [{side, price, qty, share}]."""
    walls = []
    for side, book, reverse in (("bid", bids, True), ("ask", asks, False)):
        if not book:
            continue
        top = sorted(book.keys(), reverse=reverse)[:levels]
        total = sum(book[p] for p in top)
        if total <= 0:
            continue
        for p in top:
            share = book[p] / total * 100
            if share >= share_pct:
                walls.append({"side": side, "price": p, "qty": book[p], "share": round(share, 1)})
    return walls


def detect_gaps(bids: dict, asks: dict, levels: int = 10, gap_pct: float = 0.4) -> list:
    """A gap = consecutive visible price levels more than gap_pct apart --
    a thin zone price can travel through quickly. Returns
    [{side, from, to, gap_pct}]."""
    gaps = []
    for side, book, reverse in (("bid", bids, True), ("ask", asks, False)):
        if not book:
            continue
        prices = sorted(book.keys(), reverse=reverse)[:levels]
        for a, b in zip(prices, prices[1:]):
            mid = (a + b) / 2
            if mid <= 0:
                continue
            jump = abs(b - a) / mid * 100
            if jump >= gap_pct:
                gaps.append({"side": side, "from": a, "to": b, "gap_pct": round(jump, 2)})
    return gaps


def summarise_book(bids: dict, asks: dict) -> str:
    """One compact log fragment for walls + gaps."""
    w = detect_walls(bids, asks)
    g = detect_gaps(bids, asks)
    ws = ", ".join(f"{x['side']} {x['price']}({x['share']}%)" for x in w) or "none"
    gs = ", ".join(f"{x['side']} {x['from']}->{x['to']}({x['gap_pct']}%)" for x in g) or "none"
    return f"walls: {ws} | gaps: {gs}"
