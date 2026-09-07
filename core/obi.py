"""2026-09-03: Order Book Imbalance (OBI) — informational tracker only.

    OBI = (bid_volume - ask_volume) / (bid_volume + ask_volume)

Summed over the top N price levels on each side. Ranges from -1.0 (all ask,
no bid support) to +1.0 (all bid, no ask resistance). The idea being tested:
a genuine liquidity hunt should show real resting size defending the level
(high |OBI| in the direction of the eventual reversal), while a fake sweep
that simply continues has no such defense.

This is UNPROVEN. It exists purely to accumulate real evidence, the same
way the entry-drift and hunt/breakout trackers did before either was ever
allowed to touch a trade decision -- and both of those turned out to need
real correction (entry-drift never validated beyond 4 cases; the hunt/
breakout classifier flagged ~85% of sweeps as breakout and had to be
questioned). Nothing here gates a signal. It only gets logged and alerted.
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
