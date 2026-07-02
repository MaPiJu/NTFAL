"""Read-only client for Variational Omni public market data.

Two data surfaces, both public, keyless and unsigned (see CLAUDE.md hard
constraints — nothing here can place orders):

- **Documented**: ``GET {CLIENT_API}/metadata/stats`` — platform stats and the
  per-listing universe (ticker, mark price, funding, open interest, quotes).
  Rate-limited at 10 requests / 10 s / IP.
- **Undocumented** (reverse-engineered from the Omni web app, 2026-07): the
  same-origin chart endpoint
  ``GET https://omni.variational.io/api/candles?period=…&start=ISO&end=ISO&cex_asset=TICKER``.
  This is exactly the request the site's own chart makes. It sits behind
  Cloudflare bot management: from datacenter IPs it typically answers with an
  HTML challenge page instead of JSON. That case is detected and raised as
  :class:`VariationalBlockedError` — run the probe from a normal residential
  connection to check whether your setup passes:

      python -m data.variational ETH

Known caveats (verified against the app's JS bundles):

- Supported periods are ``1m 5m 15m 30m 1h 4h 1d`` — there is **no weekly
  period**. Weekly bars are resampled here from daily (Monday-anchored, UTC).
- The app's chart consumes only ``unix_time_ms/open/high/low/close``. If the
  API sends no volume, ``volume`` is filled with 0.0 and :func:`has_volume`
  returns False — Elder's Force Index cannot be computed from such data.
- Omni launched recently; history is much shorter than a CEX's. Check the
  probe output before relying on the weekly screen.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

DEFAULT_CLIENT_API_URL = "https://omni-client-api.prod.ap-northeast-1.variational.io"
DEFAULT_WEB_API_URL = "https://omni.variational.io/api"
RATE_LIMIT_RETRIES = 5

# Interval name (project convention, matching data.hyperliquid) -> API period.
# The chart endpoint has no weekly period; "1w" is resampled from "1d".
PERIODS: dict[str, str] = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
}

INTERVAL_MS: dict[str, int] = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
    "1w": 604_800_000,
}

CANDLE_COLUMNS = ("t", "T", "open", "high", "low", "close", "volume", "trades")


class VariationalError(RuntimeError):
    """Raised when a Variational endpoint returns unusable data."""


class VariationalBlockedError(VariationalError):
    """The candles endpoint answered with a Cloudflare challenge, not JSON.

    Cloudflare bot management scores the client IP + TLS fingerprint;
    datacenter/cloud IPs are usually challenged while normal residential
    connections tend to pass. Retrying from the same network will not help.
    """


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _empty_frame() -> pd.DataFrame:
    empty = pd.DataFrame({c: pd.Series(dtype="float64") for c in CANDLE_COLUMNS})
    empty.index = pd.DatetimeIndex([], tz="UTC", name="time")
    return empty


def parse_candles(raw: list[dict[str, Any]], interval: str) -> pd.DataFrame:
    """Parse a `/api/candles` payload into the project's candle frame shape.

    Same columns as ``data.hyperliquid.parse_candles`` so indicators and the
    pipeline can consume either source: t/T (open/close time, ms),
    open/high/low/close/volume (float), trades (int). Numeric fields may come
    back as strings (Variational preserves decimal precision that way).

    The API sends no close time; ``T`` is synthesized as
    ``t + interval - 1ms`` so ``completed_bars``-style filters keep working.
    Bars missing any of OHLC are dropped (the app's own chart does the same).
    """
    bar_ms = INTERVAL_MS[interval]
    ohlc = ("open", "high", "low", "close")
    rows = [r for r in raw if all(r.get(k) not in (None, "") for k in ohlc)]
    if not rows:
        return _empty_frame()

    src = pd.DataFrame(rows)
    t = src["unix_time_ms"].astype("int64")
    volume = (
        src["volume"].astype("float64")
        if "volume" in src.columns
        else pd.Series(0.0, index=src.index)
    )
    trades = (
        src["trades"].astype("int64")
        if "trades" in src.columns
        else pd.Series(0, index=src.index, dtype="int64")
    )
    df = pd.DataFrame(
        {
            "t": t,
            "T": t + bar_ms - 1,
            "open": src["open"].astype("float64"),
            "high": src["high"].astype("float64"),
            "low": src["low"].astype("float64"),
            "close": src["close"].astype("float64"),
            "volume": volume,
            "trades": trades,
        }
    )
    df = df.drop_duplicates(subset="t", keep="last").sort_values("t")
    df.index = pd.to_datetime(df["t"], unit="ms", utc=True)
    df.index.name = "time"
    return df


def has_volume(df: pd.DataFrame) -> bool:
    """True when the frame carries real volume (Force Index needs it)."""
    return bool(df["volume"].notna().any() and (df["volume"] != 0).any())


def resample_weekly(daily: pd.DataFrame) -> pd.DataFrame:
    """Aggregate daily bars into Monday-anchored (UTC) weekly bars.

    ``T`` is set to the *scheduled* week close (Monday open + 7d − 1ms), not
    the last seen daily close, so an in-progress week is correctly dropped by
    ``completed_bars``-style filters.
    """
    if daily.empty:
        return _empty_frame()
    week_ms = INTERVAL_MS["1w"]
    day = daily.index.tz_convert("UTC")
    week_start = (day - pd.to_timedelta(day.weekday, unit="D")).normalize()
    grouped = daily.groupby(week_start)
    df = pd.DataFrame(
        {
            "open": grouped["open"].first(),
            "high": grouped["high"].max(),
            "low": grouped["low"].min(),
            "close": grouped["close"].last(),
            "volume": grouped["volume"].sum(),
            "trades": grouped["trades"].sum(),
        }
    )
    df["t"] = df.index.as_unit("ns").astype("int64") // 1_000_000
    df["T"] = df["t"] + week_ms - 1
    df = df[list(CANDLE_COLUMNS)].sort_values("t")
    df.index.name = "time"
    return df


class VariationalClient:
    """Thin httpx wrapper around Omni's public endpoints, with a parquet cache."""

    def __init__(
        self,
        client_api_url: str | None = None,
        web_api_url: str | None = None,
        http: httpx.Client | None = None,
        cache_dir: Path = Path("cache"),
    ) -> None:
        self.client_api_url = client_api_url or os.environ.get(
            "VARIATIONAL_CLIENT_API_URL", DEFAULT_CLIENT_API_URL
        )
        self.web_api_url = web_api_url or os.environ.get(
            "VARIATIONAL_WEB_API_URL", DEFAULT_WEB_API_URL
        )
        self._http = http or httpx.Client(timeout=20.0)
        self.cache_dir = cache_dir

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> VariationalClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get_json(self, url: str, params: dict[str, str] | None = None) -> Any:
        for attempt in range(RATE_LIMIT_RETRIES):
            resp = self._http.get(url, params=params)
            if resp.status_code == 429 and attempt < RATE_LIMIT_RETRIES - 1:
                time.sleep(float(resp.headers.get("Retry-After", 2**attempt)))
                continue
            body = resp.text
            # Cloudflare bot management answers challenged requests with an
            # HTML interstitial (any status, often 403) instead of JSON.
            if body.lstrip()[:1] == "<" or "text/html" in resp.headers.get("content-type", ""):
                raise VariationalBlockedError(
                    f"{url} answered with a Cloudflare challenge page (HTTP "
                    f"{resp.status_code}) instead of JSON. This endpoint is bot-"
                    "protected; datacenter IPs are usually challenged. Run "
                    "`python -m data.variational <TICKER>` from your own machine "
                    "to check whether your connection passes."
                )
            resp.raise_for_status()
            return resp.json()
        raise VariationalError("unreachable")  # pragma: no cover

    # -- documented stats API -------------------------------------------------

    def market_stats(self) -> dict[str, Any]:
        """Full `/metadata/stats` payload (platform totals + per-listing stats)."""
        stats = self._get_json(f"{self.client_api_url}/metadata/stats")
        if not isinstance(stats, dict) or "listings" not in stats:
            raise VariationalError(f"unexpected stats payload: {json.dumps(stats)[:200]}")
        return stats

    def listings(self) -> dict[str, dict[str, Any]]:
        """Map of ticker -> listing entry (mark price, funding, OI, quotes…)."""
        return {entry["ticker"]: entry for entry in self.market_stats()["listings"]}

    def tradable_perps(self) -> list[str]:
        """Every listed Omni perp ticker, sorted."""
        return sorted(self.listings())

    # -- undocumented candles endpoint ----------------------------------------

    def fetch_candles(self, ticker: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        """OHLC candles for a ticker; `1w` is resampled from daily bars.

        Raises VariationalBlockedError when Cloudflare challenges the request
        and VariationalError on an unsupported interval.
        """
        if interval == "1w":
            daily = self.fetch_candles(ticker, "1d", start_ms, end_ms)
            return resample_weekly(daily)
        if interval not in PERIODS:
            raise VariationalError(f"unsupported interval: {interval}")
        raw = self._get_json(
            f"{self.web_api_url}/candles",
            params={
                "period": PERIODS[interval],
                "start": _iso(start_ms),
                "end": _iso(end_ms),
                "cex_asset": ticker.split("-", 1)[0].split("/", 1)[0],
            },
        )
        if not isinstance(raw, list):
            raise VariationalError(f"unexpected candles payload for {ticker}/{interval}")
        return parse_candles(raw, interval)

    # -- parquet cache (same layout as the Hyperliquid client) ----------------

    def _cache_path(self, ticker: str, interval: str) -> Path:
        return self.cache_dir / f"variational_{ticker.replace('/', '_')}_{interval}.parquet"

    def load_cached(self, ticker: str, interval: str) -> pd.DataFrame | None:
        path = self._cache_path(ticker, interval)
        if not path.exists():
            return None
        return pd.read_parquet(path)

    def refresh(
        self,
        ticker: str,
        interval: str,
        lookback_bars: int,
        now_ms: int | None = None,
    ) -> pd.DataFrame:
        """Incremental refresh: fetch only bars at/after the last cached open.

        The last cached bar is always refetched because it may have been open
        when it was cached. Result is merged, deduped, persisted to parquet.
        """
        if interval not in INTERVAL_MS:
            raise VariationalError(f"unsupported interval: {interval}")
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        bar_ms = INTERVAL_MS[interval]

        cached = self.load_cached(ticker, interval)
        if cached is None or cached.empty:
            start_ms = now_ms - lookback_bars * bar_ms
        else:
            start_ms = int(cached["t"].iloc[-1])

        fresh = self.fetch_candles(ticker, interval, start_ms, now_ms)
        if cached is not None and not cached.empty:
            merged = pd.concat([cached, fresh])
            merged = merged.drop_duplicates(subset="t", keep="last").sort_values("t")
        else:
            merged = fresh

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        merged.to_parquet(self._cache_path(ticker, interval))
        return merged


def _probe(ticker: str) -> int:  # pragma: no cover - manual network probe
    """Manual connectivity probe: `python -m data.variational ETH`."""
    days = 30
    now_ms = int(time.time() * 1000)
    with VariationalClient() as client:
        print(f"[1/2] documented stats API {client.client_api_url}/metadata/stats …")
        listings = client.listings()
        print(f"      OK — {len(listings)} listings")
        if ticker in listings:
            entry = listings[ticker]
            print(f"      {ticker}: mark={entry['mark_price']} funding={entry['funding_rate']}")
        else:
            print(f"      WARNING: {ticker!r} not listed on Omni")

        print(f"[2/2] chart candles {client.web_api_url}/candles ({ticker} 1d, {days}d) …")
        try:
            df = client.fetch_candles(ticker, "1d", now_ms - days * 86_400_000, now_ms)
        except VariationalBlockedError as exc:
            print(f"      BLOCKED — {exc}")
            return 1
        vol = "yes" if has_volume(df) else "NO (Force Index impossible from this source)"
        print(f"      OK — {len(df)} daily bars, volume present: {vol}")
        if not df.empty:
            print(f"      first bar {df.index[0]:%Y-%m-%d}, last bar {df.index[-1]:%Y-%m-%d}")
            weekly = resample_weekly(df)
            print(f"      resampled to {len(weekly)} weekly bars")
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    raise SystemExit(_probe(sys.argv[1] if len(sys.argv) > 1 else "ETH"))
