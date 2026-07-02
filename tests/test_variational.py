"""Variational Omni data layer tests — recorded fixtures only, no live network."""

from __future__ import annotations

import json

import httpx
import pandas as pd
import pytest

from data.hyperliquid import completed_bars
from data.variational import (
    VariationalBlockedError,
    VariationalClient,
    VariationalError,
    has_volume,
    parse_candles,
    resample_weekly,
)

DAY_MS = 86_400_000

STATS = {
    "total_volume_24h": "1113656638.29",
    "num_markets": 3,
    "listings": [
        {"ticker": "ETH", "name": "Ethereum", "mark_price": "2500.1", "funding_rate": "0.1095"},
        {"ticker": "BTC", "name": "Bitcoin", "mark_price": "65000.2", "funding_rate": "0.1095"},
        {"ticker": "AIOZ", "name": "AIOZ Network", "mark_price": "0.05", "funding_rate": "0.1095"},
    ],
}

CLOUDFLARE_HTML = "<!DOCTYPE html><html><head><title>Just a moment...</title></head></html>"


def make_candle(t_ms: int, o: float, h: float, lo: float, c: float, **extra) -> dict:
    # The real API serves numbers as strings to preserve decimal precision.
    row = {
        "unix_time_ms": t_ms,
        "open": str(o),
        "high": str(h),
        "low": str(lo),
        "close": str(c),
    }
    row.update(extra)
    return row


def daily_fixture(start_ms: int, n: int) -> list[dict]:
    return [make_candle(start_ms + i * DAY_MS, 100 + i, 105 + i, 95 + i, 102 + i) for i in range(n)]


def make_client(
    candles: list[dict] | str,
    tmp_path,
    requests_log: list[httpx.Request] | None = None,
    candles_status: int = 200,
) -> VariationalClient:
    """Client backed by httpx.MockTransport serving canned payloads."""

    def handler(request: httpx.Request) -> httpx.Response:
        if requests_log is not None:
            requests_log.append(request)
        if request.url.path.endswith("/metadata/stats"):
            return httpx.Response(200, json=STATS)
        if request.url.path.endswith("/candles"):
            if isinstance(candles, str):
                return httpx.Response(
                    candles_status, text=candles, headers={"content-type": "text/html"}
                )
            return httpx.Response(200, json=candles)
        return httpx.Response(404)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return VariationalClient(http=http, cache_dir=tmp_path)


# -- parsing -------------------------------------------------------------------


def test_parse_candles_types_order_and_synthesized_close_time():
    raw = daily_fixture(1_700_000_000_000, 5)
    df = parse_candles(raw[::-1], "1d")  # reversed input must come back sorted

    assert list(df["t"]) == sorted(c["unix_time_ms"] for c in raw)
    assert (df["T"] == df["t"] + DAY_MS - 1).all()
    assert df["open"].dtype == "float64"
    assert df["close"].iloc[0] == 102.0  # parsed from string
    assert df.index.tz is not None


def test_parse_candles_no_volume_field():
    df = parse_candles(daily_fixture(1_700_000_000_000, 3), "1d")
    assert (df["volume"] == 0.0).all()
    assert not has_volume(df)


def test_parse_candles_with_volume_field():
    raw = [make_candle(1_700_000_000_000, 1, 2, 0.5, 1.5, volume="123.4")]
    df = parse_candles(raw, "1d")
    assert df["volume"].iloc[0] == 123.4
    assert has_volume(df)


def test_parse_candles_drops_incomplete_bars_and_empty():
    raw = daily_fixture(1_700_000_000_000, 2)
    raw.append({"unix_time_ms": 1_700_000_000_000 + 2 * DAY_MS, "close": "1"})  # no OHL
    assert len(parse_candles(raw, "1d")) == 2
    assert parse_candles([], "1d").empty


# -- weekly resample -----------------------------------------------------------


def test_resample_weekly_monday_anchored():
    # 2024-01-01 is a Monday; 10 daily bars span two Mondays + a partial week.
    start = int(pd.Timestamp("2024-01-01", tz="UTC").value // 1_000_000)
    df = parse_candles(daily_fixture(start, 10), "1d")
    weekly = resample_weekly(df)

    assert len(weekly) == 2
    assert weekly.index[0] == pd.Timestamp("2024-01-01", tz="UTC")
    assert weekly.index[1] == pd.Timestamp("2024-01-08", tz="UTC")
    # First week aggregates days 0..6: open of day 0, close of day 6, extremes.
    assert weekly["open"].iloc[0] == 100.0
    assert weekly["close"].iloc[0] == 108.0
    assert weekly["high"].iloc[0] == 111.0
    assert weekly["low"].iloc[0] == 95.0


def test_resample_weekly_in_progress_week_is_dropped_by_completed_bars():
    start = int(pd.Timestamp("2024-01-01", tz="UTC").value // 1_000_000)
    df = parse_candles(daily_fixture(start, 10), "1d")
    weekly = resample_weekly(df)

    # "now" = right after the 10th daily bar: week 2 is still in progress
    now_ms = start + 10 * DAY_MS
    done = completed_bars(weekly, now_ms=now_ms)
    assert len(done) == 1
    assert done.index[0] == pd.Timestamp("2024-01-01", tz="UTC")


# -- client --------------------------------------------------------------------


def test_listings_and_tradable_perps(tmp_path):
    client = make_client([], tmp_path)
    assert client.tradable_perps() == ["AIOZ", "BTC", "ETH"]
    assert client.listings()["ETH"]["mark_price"] == "2500.1"


def test_fetch_candles_request_shape(tmp_path):
    log: list[httpx.Request] = []
    client = make_client(daily_fixture(1_700_000_000_000, 3), tmp_path, requests_log=log)
    df = client.fetch_candles("ETH", "1d", 1_700_000_000_000, 1_700_000_000_000 + 3 * DAY_MS)

    assert len(df) == 3
    params = dict(httpx.QueryParams(log[-1].url.query))
    assert params["period"] == "1d"
    assert params["cex_asset"] == "ETH"
    assert params["start"].endswith("Z") and "T" in params["start"]


def test_fetch_candles_weekly_resamples_daily(tmp_path):
    start = int(pd.Timestamp("2024-01-01", tz="UTC").value // 1_000_000)
    log: list[httpx.Request] = []
    client = make_client(daily_fixture(start, 10), tmp_path, requests_log=log)
    weekly = client.fetch_candles("ETH", "1w", start, start + 10 * DAY_MS)

    assert len(weekly) == 2
    # the API itself was asked for daily bars (it has no weekly period)
    params = dict(httpx.QueryParams(log[-1].url.query))
    assert params["period"] == "1d"


def test_fetch_candles_unsupported_interval(tmp_path):
    client = make_client([], tmp_path)
    with pytest.raises(VariationalError, match="unsupported interval"):
        client.fetch_candles("ETH", "3d", 0, 1)


def test_cloudflare_challenge_raises_blocked_error(tmp_path):
    client = make_client(CLOUDFLARE_HTML, tmp_path, candles_status=403)
    with pytest.raises(VariationalBlockedError, match="Cloudflare"):
        client.fetch_candles("ETH", "1d", 0, DAY_MS)
    # stats API stays reachable even when the chart endpoint is challenged
    assert "ETH" in client.listings()


def test_refresh_caches_and_merges(tmp_path):
    start = 1_700_000_000_000
    client = make_client(daily_fixture(start, 5), tmp_path)
    now_ms = start + 5 * DAY_MS

    df = client.refresh("ETH", "1d", lookback_bars=5, now_ms=now_ms)
    assert len(df) == 5
    cached = client.load_cached("ETH", "1d")
    assert cached is not None and list(cached["t"]) == list(df["t"])

    # second refresh serves 2 newer bars; merge must dedupe and stay sorted
    client2 = make_client(daily_fixture(start + 3 * DAY_MS, 4), tmp_path)
    df2 = client2.refresh("ETH", "1d", lookback_bars=5, now_ms=now_ms + 2 * DAY_MS)
    assert len(df2) == 7
    assert list(df2["t"]) == sorted(df2["t"])


def test_bad_stats_payload(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"nope": True})

    client = VariationalClient(
        http=httpx.Client(transport=httpx.MockTransport(handler)), cache_dir=tmp_path
    )
    with pytest.raises(VariationalError, match="unexpected stats payload"):
        client.market_stats()
    assert "nope" in json.dumps({"nope": True})  # fixture sanity
