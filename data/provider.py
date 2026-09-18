"""What the pipeline needs from a market-data source.

The scanner reads everything from Hyperliquid's public `info` endpoint, so
`HyperliquidClient` is the only implementation. This Protocol exists so the
pipeline can be typed against the *capability* rather than the concrete client —
and so tests can hand it a stub without a live HTTP transport.

Everything here is read-only: candles, the perp universe, and a public-address
position lookup. No key, no signing, no order path (see CLAUDE.md).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

import pandas as pd


class MarketDataProvider(Protocol):
    """Satisfied by HyperliquidClient."""

    def tradable_perps(self, dex: str = "") -> dict[str, int]: ...

    def validate_watchlist(self, coins: Sequence[str]) -> dict[str, int]: ...

    def refresh(
        self, coin: str, interval: str, lookback_bars: int, now_ms: int | None = None
    ) -> pd.DataFrame: ...

    def clearinghouse_state(self, address: str, dex: str = "") -> dict[str, Any]: ...
