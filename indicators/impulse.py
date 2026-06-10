"""Elder's Impulse system: EMA13 slope + MACD-histogram slope per bar."""

from __future__ import annotations

import pandas as pd

from indicators.ema import ema
from indicators.macd import macd_histogram

GREEN = "green"
RED = "red"
BLUE = "blue"


def impulse_color(
    close: pd.Series,
    ema_span: int = 13,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.Series:
    """Per-bar Impulse color.

    EMA rising AND MACD-hist rising -> green; both falling -> red; mixed -> blue.
    The first bar (no slope yet) is blue.
    """
    de = ema(close, ema_span).diff()
    dh = macd_histogram(close, fast, slow, signal).diff()
    colors = pd.Series(BLUE, index=close.index, dtype="object")
    colors[(de > 0) & (dh > 0)] = GREEN
    colors[(de < 0) & (dh < 0)] = RED
    return colors
