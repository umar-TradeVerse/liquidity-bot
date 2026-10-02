"""Candlestick pattern labels for every 15m candle (2026-10-03, log-only).

Recorded on each `Candle:` log line, on every in-trade OBI line, and on the
trade record at entry -- so pattern + order-book data can later be tested
against outcomes (e.g. for the sweep-vs-breakout work). Nothing here makes
or blocks a trade.

Shape rules use the candle's OWN proportions (no fixed price numbers):
  hammer           lower wick >= 2x body and >= 2x upper wick (bullish rejection)
  shooting_star    upper wick >= 2x body and >= 2x lower wick (bearish rejection)
  doji             body <= 10% of the range, wicks on both sides
  bull/bear_marubozu  body >= 90% of the range
  bull/bear_strong    body >= 60% of the range
  bull/bear_engulfing body fully covers the previous candle's opposite body
  inside_bar       whole range inside the previous candle's range
  bull/bear        anything else, by colour
"""


def classify(c, prev=None):
    o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
    rng = h - l
    if rng <= 0:
        return "flat"
    body = abs(cl - o)
    up, dn = h - max(o, cl), min(o, cl) - l
    bull = cl > o
    labels = []
    if prev:
        po, pc = prev["open"], prev["close"]
        if bull and pc < po and o <= pc and cl >= po and body > abs(pc - po):
            labels.append("bull_engulfing")
        elif not bull and pc > po and o >= pc and cl <= po and body > abs(pc - po):
            labels.append("bear_engulfing")
        if h <= prev["high"] and l >= prev["low"]:
            labels.append("inside_bar")
    # one-sided rejection wicks take priority over doji (a tiny body with only a
    # long lower wick is a hammer / dragonfly, not an indecision doji)
    if dn >= 2 * body and dn >= 2 * up:
        labels.append("hammer")
    elif up >= 2 * body and up >= 2 * dn:
        labels.append("shooting_star")
    elif body <= 0.10 * rng:
        labels.append("doji")
    elif body >= 0.90 * rng:
        labels.append("bull_marubozu" if bull else "bear_marubozu")
    elif body >= 0.60 * rng:
        labels.append("bull_strong" if bull else "bear_strong")
    if not labels:
        labels.append("bull" if bull else "bear")
    return "+".join(labels)
