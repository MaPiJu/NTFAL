"""Pure indicator functions on pandas Series — only what the Strategy Spec lists.

Adding more indicators is a regression, not a feature (see CLAUDE.md).
"""

from indicators.ema import ema
from indicators.force_index import force_index
from indicators.impulse import impulse_color
from indicators.macd import macd_histogram

__all__ = ["ema", "force_index", "impulse_color", "macd_histogram"]
