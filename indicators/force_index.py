"""Elder's Force Index: EMA of (close - prev_close) * volume."""

from __future__ import annotations

import pandas as pd

from indicators.ema import ema


def force_index(close: pd.Series, volume: pd.Series, span: int = 2) -> pd.Series:
    """EMA(span) of the raw force; span=2 for entries, span=13 for context."""
    raw = close.diff() * volume
    return ema(raw, span)
