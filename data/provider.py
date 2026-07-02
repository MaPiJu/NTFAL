"""Multi-platform market data: provider protocol + the Omni/Hyperliquid router.

The scanner was born Hyperliquid-only; Variational Omni is added as a second
*execution venue* behind the same read-only data surface. Watchlist entries
are namespaced exactly like HIP-3 dexes: ``omni:ETH`` is Omni's ETH perp,
``omni:*`` scans the whole Omni universe (everything else stays Hyperliquid).

Omni exposes no reliable public OHLCV API (its chart endpoint sits behind
Cloudflare bot management — see data/variational.py), but Omni is an
oracle-priced venue: its marks track external index prices. So candles for an
``omni:`` asset are sourced **hybrid**:

1. ticker listed as a native Hyperliquid perp  -> Hyperliquid candles
2. bare ticker listed on Hyperliquid's ``xyz`` tradfi dex -> those candles
3. otherwise, best-effort native Variational candles — used only when they
   actually carry volume (Elder's Force Index needs it); a Cloudflare block is
   remembered for the rest of the run so 300+ long-tail tickers fail fast.

Assets with no usable candle source are *skipped with a reason*, never
evaluated on bogus data. Everything here is read-only public GETs — no key,
no signing, no order path (see CLAUDE.md hard constraints).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

import httpx
import pandas as pd

from data.hyperliquid import HyperliquidClient, HyperliquidError
from data.variational import (
    VariationalBlockedError,
    VariationalClient,
    VariationalError,
    _empty_frame,
    has_volume,
)

# Watchlist namespace for Variational Omni assets ("omni:ETH", "omni:*").
OMNI_PREFIX = "omni"
# Omni's stats API exposes no size-decimals metadata; suggested sizes for
# omni: assets are floored to this many decimals (fractional sizing is the
# norm there — the operator rounds to what the UI accepts when entering).
OMNI_SZ_DECIMALS = 4


class MarketDataProvider(Protocol):
    """What the pipeline consumes — satisfied by HyperliquidClient and PlatformRouter."""

    def tradable_perps(self, dex: str = "") -> dict[str, int]: ...

    def validate_watchlist(self, coins: Sequence[str]) -> dict[str, int]: ...

    def refresh(
        self, coin: str, interval: str, lookback_bars: int, now_ms: int | None = None
    ) -> pd.DataFrame: ...

    def clearinghouse_state(self, address: str, dex: str = "") -> dict[str, Any]: ...


def is_omni(coin: str) -> bool:
    return coin.startswith(OMNI_PREFIX + ":")


def platform_of(coin: str) -> str:
    """Execution venue of a watchlist asset, for display/journal purposes."""
    return "variational" if is_omni(coin) else "hyperliquid"


class OmniProvider:
    """Variational Omni universe + hybrid candles (Hyperliquid-first).

    All coins in and out of this class carry the ``omni:`` prefix; the bare
    Omni ticker only exists internally. ``notes`` collects, per prefixed coin,
    why an asset could not be given usable candles (surfaced as skip reasons).
    """

    def __init__(self, variational: VariationalClient, hyperliquid: HyperliquidClient) -> None:
        self.variational = variational
        self.hyperliquid = hyperliquid
        self.notes: dict[str, str] = {}
        self._listings: dict[str, dict[str, Any]] | None = None
        self._hl_native: set[str] | None = None
        self._hl_xyz: set[str] | None = None
        self._native_blocked = False

    # -- universe -------------------------------------------------------------

    def listings(self) -> dict[str, dict[str, Any]]:
        if self._listings is None:
            self._listings = self.variational.listings()
        return self._listings

    def tradable_perps(self) -> dict[str, int]:
        """Every Omni listing as 'omni:TICKER' -> assumed size decimals."""
        return {f"{OMNI_PREFIX}:{t}": OMNI_SZ_DECIMALS for t in sorted(self.listings())}

    def validate_watchlist(self, coins: Sequence[str]) -> dict[str, int]:
        unknown = [c for c in coins if c.split(":", 1)[1] not in self.listings()]
        if unknown:
            raise VariationalError(f"not in the Omni perp universe: {', '.join(unknown)}")
        return {c: OMNI_SZ_DECIMALS for c in coins}

    # -- hybrid candles ---------------------------------------------------------

    def _hl_source(self, ticker: str) -> str | None:
        """Hyperliquid coin whose candles proxy this Omni ticker, if any.

        Omni marks to external index prices, so for a ticker also listed on
        Hyperliquid (natively, or as bare name on the 'xyz' tradfi dex) the
        Hyperliquid candles are the same underlying price series — with real
        volume and deep history, neither of which Omni's chart endpoint offers.
        """
        if self._hl_native is None:
            self._hl_native = set(self.hyperliquid.tradable_perps())
        if ticker in self._hl_native:
            return ticker
        if self._hl_xyz is None:
            try:
                self._hl_xyz = set(self.hyperliquid.tradable_perps("xyz"))
            except (HyperliquidError, httpx.HTTPError):  # dex absent (e.g. test setup)
                self._hl_xyz = set()
        xyz_coin = f"xyz:{ticker}"
        return xyz_coin if xyz_coin in self._hl_xyz else None

    def refresh(
        self, coin: str, interval: str, lookback_bars: int, now_ms: int | None = None
    ) -> pd.DataFrame:
        """Candles for an 'omni:TICKER' asset via the hybrid source chain.

        Returns an empty frame (recording the reason in ``notes``) when no
        usable source exists, so the pipeline skips the asset cleanly.
        """
        ticker = coin.split(":", 1)[1]
        hl_coin = self._hl_source(ticker)
        if hl_coin is not None:
            # Shared parquet cache: omni:BTC and BTC reuse the same HL candles.
            return self.hyperliquid.refresh(hl_coin, interval, lookback_bars, now_ms)

        if self._native_blocked:
            self.notes.setdefault(coin, "native Omni candles blocked by Cloudflare")
            return _empty_frame()
        try:
            df = self.variational.refresh(ticker, interval, lookback_bars, now_ms)
        except VariationalBlockedError:
            # Bot management scores the network, not the ticker: once one
            # request is challenged, every later one will be — stop trying.
            self._native_blocked = True
            self.notes.setdefault(coin, "native Omni candles blocked by Cloudflare")
            return _empty_frame()
        if not df.empty and not has_volume(df):
            self.notes.setdefault(coin, "no volume in native Omni candles (Force Index needs it)")
            return _empty_frame()
        return df


class PlatformRouter:
    """Single MarketDataProvider facade over Hyperliquid + Variational Omni.

    Dispatch is by namespace: 'omni:*'/'omni:TICKER' go to the OmniProvider,
    everything else (native coins, HIP-3 'xyz:GOLD'…) to the HyperliquidClient.
    Account lookups (clearinghouseState) exist only on Hyperliquid; Omni
    positions are declared manually in config.toml instead.
    """

    def __init__(self, hyperliquid: HyperliquidClient, omni: OmniProvider) -> None:
        self.hyperliquid = hyperliquid
        self.omni = omni

    def close(self) -> None:
        self.hyperliquid.close()
        self.omni.variational.close()

    def __enter__(self) -> PlatformRouter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def notes(self) -> dict[str, str]:
        return self.omni.notes

    def tradable_perps(self, dex: str = "") -> dict[str, int]:
        if dex == OMNI_PREFIX:
            return self.omni.tradable_perps()
        return self.hyperliquid.tradable_perps(dex)

    def validate_watchlist(self, coins: Sequence[str]) -> dict[str, int]:
        omni_coins = [c for c in coins if is_omni(c)]
        hl_coins = [c for c in coins if not is_omni(c)]
        out: dict[str, int] = {}
        if hl_coins:
            out.update(self.hyperliquid.validate_watchlist(hl_coins))
        if omni_coins:
            out.update(self.omni.validate_watchlist(omni_coins))
        return out

    def refresh(
        self, coin: str, interval: str, lookback_bars: int, now_ms: int | None = None
    ) -> pd.DataFrame:
        if is_omni(coin):
            return self.omni.refresh(coin, interval, lookback_bars, now_ms)
        return self.hyperliquid.refresh(coin, interval, lookback_bars, now_ms)

    def clearinghouse_state(self, address: str, dex: str = "") -> dict[str, Any]:
        return self.hyperliquid.clearinghouse_state(address, dex)
