"""
Candlestick pattern detection.
Only Pin Bar and Engulfing used — cleanest for automation.
Doji included as a special case of Pin Bar (tiny body).
"""


def candle_body(c: dict) -> float:
    return abs(c['close'] - c['open'])


def candle_range(c: dict) -> float:
    return c['high'] - c['low']


def upper_wick(c: dict) -> float:
    return c['high'] - max(c['open'], c['close'])


def lower_wick(c: dict) -> float:
    return min(c['open'], c['close']) - c['low']


def is_bearish(c: dict) -> bool:
    return c['close'] < c['open']


def is_bullish(c: dict) -> bool:
    return c['close'] > c['open']


def is_doji(c: dict) -> bool:
    """Body is <= 10% of total range."""
    r = candle_range(c)
    if r == 0:
        return True
    return candle_body(c) / r <= 0.10


def is_bearish_pin_bar(c: dict) -> bool:
    """
    Upper wick >= 2x body AND body in lower 40% of range.
    Catches shooting stars and bearish pins.
    Also catches doji near PDH (tiny body, big upper wick).
    """
    r = candle_range(c)
    if r == 0:
        return False
    body = candle_body(c)
    uw = upper_wick(c)
    # Doji case: tiny body with upper wick dominance
    if is_doji(c) and uw > r * 0.4:
        return True
    if body == 0:
        return False
    return uw >= 2 * body and (c['low'] + r * 0.4) >= min(c['open'], c['close'])


def is_bullish_pin_bar(c: dict) -> bool:
    """
    Lower wick >= 2x body AND body in upper 40% of range.
    Catches hammers and bullish pins.
    Also catches doji near PDL.
    """
    r = candle_range(c)
    if r == 0:
        return False
    body = candle_body(c)
    lw = lower_wick(c)
    if is_doji(c) and lw > r * 0.4:
        return True
    if body == 0:
        return False
    return lw >= 2 * body and (c['high'] - r * 0.4) <= max(c['open'], c['close'])


def is_bearish_engulfing(current: dict, previous: dict) -> bool:
    """Current bearish body fully engulfs previous candle body."""
    if not is_bearish(current):
        return False
    return (current['open'] >= max(previous['open'], previous['close']) and
            current['close'] <= min(previous['open'], previous['close']))


def is_bullish_engulfing(current: dict, previous: dict) -> bool:
    """Current bullish body fully engulfs previous candle body."""
    if not is_bullish(current):
        return False
    return (current['open'] <= min(previous['open'], previous['close']) and
            current['close'] >= max(previous['open'], previous['close']))


def is_rejection_candle_bearish(current: dict, previous: dict = None) -> bool:
    """
    Rejection candle after PDH break — signals SHORT.
    Must be: bearish pin bar OR bearish engulfing (if previous provided).
    """
    if is_bearish_pin_bar(current):
        return True
    if previous and is_bearish_engulfing(current, previous):
        return True
    return False


def is_rejection_candle_bullish(current: dict, previous: dict = None) -> bool:
    """
    Rejection candle after PDL break — signals LONG.
    Must be: bullish pin bar OR bullish engulfing (if previous provided).
    """
    if is_bullish_pin_bar(current):
        return True
    if previous and is_bullish_engulfing(current, previous):
        return True
    return False


def pattern_name(c: dict, previous: dict = None, side: str = "bearish") -> str:
    """Returns human-readable pattern name for Telegram notifications."""
    if side == "bearish":
        if previous and is_bearish_engulfing(c, previous):
            return "Bearish Engulfing"
        if is_doji(c):
            return "Doji (Bearish)"
        if is_bearish_pin_bar(c):
            return "Bearish Pin Bar"
    else:
        if previous and is_bullish_engulfing(c, previous):
            return "Bullish Engulfing"
        if is_doji(c):
            return "Doji (Bullish)"
        if is_bullish_pin_bar(c):
            return "Bullish Pin Bar"
    return "Rejection Candle"
