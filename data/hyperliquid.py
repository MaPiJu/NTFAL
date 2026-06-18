"""Read-only client for Hyperliquid's public `info` endpoint.

Hard constraint (see CLAUDE.md): this module — and the whole project — only
POSTs *public* info requests: `meta`, `candleSnapshot`, and `clearinghouseState`
(open positions for a public address — a read-only account lookup, like a block
explorer). There is no wallet, no private key, no signing, no order path anywhere.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

DEFAULT_API_URL = "https://api.hyperliquid.xyz/info"
# The API serves only the most recent 5000 candles per (coin, interval).
MAX_CANDLES_PER_REQUEST = 5000
# Scanning the full universe fires hundreds of requests; back off on 429
# instead of dying mid-refresh.
RATE_LIMIT_RETRIES = 5

INTERVAL_MS: dict[str, int] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
    "3d": 259_200_000,
    "1w": 604_800_000,
}

CANDLE_COLUMNS = ("t", "T", "open", "high", "low", "close", "volume", "trades")


class HyperliquidError(RuntimeError):
    """Raised when the info endpoint returns unusable data or a coin is unknown."""


def coin_dex(coin: str) -> str:
    """Perp dex a coin belongs to: '' for native perps, the prefix for HIP-3
    builder-deployed perps (e.g. 'xyz:GOLD' -> 'xyz', the tradfi dex)."""
    return coin.split(":", 1)[0] if ":" in coin else ""


def parse_candles(raw: list[dict[str, Any]]) -> pd.DataFrame:
    """Parse a candleSnapshot payload (string OHLCV fields) into a typed frame.

    Columns: t/T (open/close time, ms), open/high/low/close/volume (float),
    trades (int). Index: UTC DatetimeIndex of the bar open time.
    """
    if not raw:
        empty = pd.DataFrame({c: pd.Series(dtype="float64") for c in CANDLE_COLUMNS})
        empty.index = pd.DatetimeIndex([], tz="UTC", name="time")
        return empty

    src = pd.DataFrame(raw)
    df = pd.DataFrame(
        {
            "t": src["t"].astype("int64"),
            "T": src["T"].astype("int64"),
            "open": src["o"].astype("float64"),
            "high": src["h"].astype("float64"),
            "low": src["l"].astype("float64"),
            "close": src["c"].astype("float64"),
            "volume": src["v"].astype("float64"),
            "trades": src["n"].astype("int64"),
        }
    )
    df = df.drop_duplicates(subset="t", keep="last").sort_values("t")
    df.index = pd.to_datetime(df["t"], unit="ms", utc=True)
    df.index.name = "time"
    return df


class HyperliquidClient:
    """Thin httpx wrapper around the public info endpoint, with a parquet cache."""

    def __init__(
        self,
        api_url: str | None = None,
        http: httpx.Client | None = None,
        cache_dir: Path = Path("cache"),
    ) -> None:
        self.api_url = api_url or os.environ.get("HL_API_URL", DEFAULT_API_URL)
        self._http = http or httpx.Client(timeout=20.0)
        self.cache_dir = cache_dir

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> HyperliquidClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _info(self, payload: dict[str, Any]) -> Any:
        for attempt in range(RATE_LIMIT_RETRIES):
            resp = self._http.post(self.api_url, json=payload)
            if resp.status_code != 429 or attempt == RATE_LIMIT_RETRIES - 1:
                resp.raise_for_status()
                return resp.json()
            time.sleep(float(resp.headers.get("Retry-After", 2**attempt)))
        raise HyperliquidError("unreachable")  # pragma: no cover

    # -- meta ---------------------------------------------------------------

    def perp_universe(self, dex: str = "") -> dict[str, dict[str, Any]]:
        """Map of perp name -> meta entry (incl. szDecimals) from the `meta` request.

        `dex` selects a perp dex: '' is the native (crypto) universe; HIP-3
        builder dexes like 'xyz' carry tradfi perps (stocks, indices, gold…)
        whose names come back already prefixed (e.g. 'xyz:GOLD').
        """
        payload: dict[str, Any] = {"type": "meta"}
        if dex:
            payload["dex"] = dex
        meta = self._info(payload)
        universe = meta.get("universe")
        if not isinstance(universe, list):
            raise HyperliquidError(f"unexpected meta payload: {json.dumps(meta)[:200]}")
        return {entry["name"]: entry for entry in universe}

    def validate_watchlist(self, coins: Sequence[str]) -> dict[str, int]:
        """Check every coin against its perp universe; return {coin: szDecimals}.

        Coins may mix dexes ('BTC' is native, 'xyz:GOLD' lives on the tradfi
        dex); one `meta` request is made per dex involved. Raises
        HyperliquidError listing any coin that is not a tradable perp.
        """
        universes: dict[str, dict[str, dict[str, Any]]] = {}
        for dex in {coin_dex(c) for c in coins}:
            universes[dex] = self.perp_universe(dex)
        unknown = [c for c in coins if c not in universes[coin_dex(c)]]
        if unknown:
            raise HyperliquidError(f"not in the Hyperliquid perp universe: {', '.join(unknown)}")
        return {c: int(universes[coin_dex(c)][c]["szDecimals"]) for c in coins}

    def tradable_perps(self, dex: str = "") -> dict[str, int]:
        """Every currently tradable perp of a dex -> szDecimals, sorted by name.

        Delisted assets stay in `meta` (flagged `isDelisted`) and are excluded.
        """
        universe = self.perp_universe(dex)
        return {
            name: int(entry["szDecimals"])
            for name, entry in sorted(universe.items())
            if not entry.get("isDelisted", False)
        }

    # -- account (read-only) ------------------------------------------------

    def clearinghouse_state(self, address: str) -> dict[str, Any]:
        """Public `clearinghouseState` for a wallet address: open positions + margin.

        This is a read-only account lookup against the public info endpoint — it
        takes only a public address, never a private key, and signs nothing. Used
        to surface the operator's OPEN positions for Elder trade management.
        """
        if not address:
            raise HyperliquidError("a public wallet address is required")
        state = self._info({"type": "clearinghouseState", "user": address})
        if not isinstance(state, dict):
            raise HyperliquidError(f"unexpected clearinghouseState payload for {address}")
        return state

    # -- candles ------------------------------------------------------------

    def fetch_candles(self, coin: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        raw = self._info(
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": coin,
                    "interval": interval,
                    "startTime": start_ms,
                    "endTime": end_ms,
                },
            }
        )
        if not isinstance(raw, list):
            raise HyperliquidError(f"unexpected candleSnapshot payload for {coin}/{interval}")
        return parse_candles(raw)

    def _cache_path(self, coin: str, interval: str) -> Path:
        # ':' in HIP-3 coin names (e.g. 'xyz:GOLD') is not filename-safe everywhere.
        return self.cache_dir / f"{coin.replace(':', '_')}_{interval}.parquet"

    def load_cached(self, coin: str, interval: str) -> pd.DataFrame | None:
        path = self._cache_path(coin, interval)
        if not path.exists():
            return None
        return pd.read_parquet(path)

    def refresh(
        self,
        coin: str,
        interval: str,
        lookback_bars: int,
        now_ms: int | None = None,
    ) -> pd.DataFrame:
        """Incremental refresh: fetch only bars at/after the last cached open.

        The last cached bar is always refetched because it may have been open
        when it was cached. Result is merged, deduped, persisted to parquet.
        """
        if interval not in INTERVAL_MS:
            raise HyperliquidError(f"unsupported interval: {interval}")
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        bar_ms = INTERVAL_MS[interval]
        lookback_bars = min(lookback_bars, MAX_CANDLES_PER_REQUEST)

        cached = self.load_cached(coin, interval)
        if cached is None or cached.empty:
            start_ms = now_ms - lookback_bars * bar_ms
        else:
            start_ms = int(cached["t"].iloc[-1])

        fresh = self.fetch_candles(coin, interval, start_ms, now_ms)
        if cached is not None and not cached.empty:
            merged = pd.concat([cached, fresh])
            merged = merged.drop_duplicates(subset="t", keep="last").sort_values("t")
        else:
            merged = fresh

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        merged.to_parquet(self._cache_path(coin, interval))
        return merged


def completed_bars(df: pd.DataFrame, now_ms: int | None = None) -> pd.DataFrame:
    """Drop a still-open trailing bar (close time in the future)."""
    if df.empty:
        return df
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    return df[df["T"] <= now_ms]
