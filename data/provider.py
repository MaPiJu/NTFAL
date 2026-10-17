"""What the pipeline needs from a market-data source.

The scanner reads everything from Hyperliquid's public `info` endpoint, so
`HyperliquidClient` is the only implementation. This Protocol exists so the
pipeline can be typed against the *capability* rather than the concrete client —
and so tests can hand it a stub without a live HTTP transport.

Everything here is read-only: candles, the perp dexes and their universe, each
perp's current market context (funding rate, open interest, mark), and a
public-address position lookup. No key, no signing, no order path (see CLAUDE.md).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

import pandas as pd

from data.hyperliquid import AssetContext, PerpSpec


class MarketDataProvider(Protocol):
    """Satisfied by HyperliquidClient."""

    def perp_dexs(self) -> list[str]: ...

    def tradable_perps(self, dex: str = "") -> dict[str, PerpSpec]: ...

    def validate_watchlist(self, coins: Sequence[str]) -> dict[str, PerpSpec]: ...

    def asset_contexts(self, dex: str = "") -> dict[str, AssetContext]: ...

    def refresh(
        self, coin: str, interval: str, lookback_bars: int, now_ms: int | None = None
    ) -> pd.DataFrame: ...

    def clearinghouse_state(self, address: str, dex: str = "") -> dict[str, Any]: ...
