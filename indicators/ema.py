"""Exponential moving average."""

from __future__ import annotations

import pandas as pd


def ema(series: pd.Series, span: int) -> pd.Series:
    """EMA with multiplier 2/(span+1), seeded at the first value (adjust=False)."""
    if span < 1:
        raise ValueError("span must be >= 1")
    return series.ewm(span=span, adjust=False).mean()
