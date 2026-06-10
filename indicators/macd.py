"""MACD histogram. Only the slope (last vs previous bar) matters for Impulse."""

from __future__ import annotations

import pandas as pd

from indicators.ema import ema


def macd_histogram(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.Series:
    """MACD(fast, slow) line minus its `signal`-EMA."""
    macd_line = ema(close, fast) - ema(close, slow)
    return macd_line - ema(macd_line, signal)
