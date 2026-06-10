"""Data layer tests — recorded fixtures only, no live network."""

from __future__ import annotations

import pandas as pd
import pytest

from data.hyperliquid import (
    INTERVAL_MS,
    MAX_CANDLES_PER_REQUEST,
    HyperliquidError,
    completed_bars,
    parse_candles,
)
from tests.conftest import load_fixture, make_client


def test_parse_candles_types_and_order():
    raw = load_fixture("btc_1d.json")
    df = parse_candles(raw[::-1])  # reversed input must come back sorted

    assert list(df["t"]) == sorted(c["t"] for c in raw)
    assert df["open"].dtype == "float64"
    assert df["close"].dtype == "float64"
    assert df["volume"].dtype == "float64"
    assert df["trades"].dtype == "int64"
    assert df.index.tz is not None
    first = raw[0]
    assert df["close"].iloc[0] == float(first["c"])


def test_parse_candles_empty():
    df = parse_candles([])
    assert df.empty
    assert "close" in df.columns


def test_validate_watchlist(tmp_path, btc_fixtures):
    client = make_client(btc_fixtures, tmp_path)
    sz = client.validate_watchlist(["BTC", "ETH", "SOL", "HYPE"])
    assert sz == {"BTC": 5, "ETH": 4, "SOL": 2, "HYPE": 2}

    with pytest.raises(HyperliquidError, match="DOGEZILLA"):
        client.validate_watchlist(["BTC", "DOGEZILLA"])


def test_refresh_initial_fetch_and_cache(tmp_path, btc_fixtures):
    log: list[dict] = []
    client = make_client(btc_fixtures, tmp_path, requests_log=log)
    now_ms = btc_fixtures[("BTC", "1d")][-1]["t"] + 1000

    df = client.refresh("BTC", "1d", lookback_bars=400, now_ms=now_ms)
    assert len(df) == len(btc_fixtures[("BTC", "1d")])
    assert (tmp_path / "BTC_1d.parquet").exists()

    req = log[-1]["req"]
    assert req["startTime"] == now_ms - 400 * INTERVAL_MS["1d"]


def test_refresh_is_incremental(tmp_path, btc_fixtures):
    candles = btc_fixtures[("BTC", "1d")]
    log: list[dict] = []
    client = make_client(btc_fixtures, tmp_path, requests_log=log)
    now_ms = candles[-1]["t"] + 1000

    first = client.refresh("BTC", "1d", lookback_bars=400, now_ms=now_ms)

    # Second refresh: the last cached bar got revised and a new bar appeared.
    revised = dict(candles[-1], c=str(float(candles[-1]["c"]) + 100))
    new_bar = dict(candles[-1], t=candles[-1]["t"] + INTERVAL_MS["1d"], c="99999.0")
    new_bar["T"] = new_bar["t"] + INTERVAL_MS["1d"] - 1
    btc_fixtures[("BTC", "1d")] = candles[:-1] + [revised, new_bar]
    now2 = new_bar["t"] + 1000

    second = client.refresh("BTC", "1d", lookback_bars=400, now_ms=now2)
    assert len(second) == len(first) + 1
    assert second["t"].is_unique
    # incremental request started at the last cached open, not the full lookback
    assert log[-1]["req"]["startTime"] == candles[-1]["t"]
    # the revised close replaced the stale cached value
    assert second["close"].iloc[-2] == float(revised["c"])
    assert second["close"].iloc[-1] == 99999.0


def test_refresh_respects_5000_candle_limit(tmp_path, btc_fixtures):
    log: list[dict] = []
    client = make_client(btc_fixtures, tmp_path, requests_log=log)
    now_ms = btc_fixtures[("BTC", "1d")][-1]["t"] + 1000

    client.refresh("BTC", "1d", lookback_bars=999_999, now_ms=now_ms)
    req = log[-1]["req"]
    assert req["startTime"] == now_ms - MAX_CANDLES_PER_REQUEST * INTERVAL_MS["1d"]


def test_completed_bars_drops_open_bar():
    raw = load_fixture("btc_1d.json")
    df = parse_candles(raw)
    cutoff = int(df["T"].iloc[-1]) - 1  # pretend the last bar is still open
    trimmed = completed_bars(df, now_ms=cutoff)
    assert len(trimmed) == len(df) - 1
    assert completed_bars(df, now_ms=int(df["T"].iloc[-1])).equals(df)


def test_cache_roundtrip(tmp_path, btc_fixtures):
    client = make_client(btc_fixtures, tmp_path)
    now_ms = btc_fixtures[("BTC", "1w")][-1]["t"] + 1000
    df = client.refresh("BTC", "1w", lookback_bars=260, now_ms=now_ms)

    cached = client.load_cached("BTC", "1w")
    assert cached is not None
    pd.testing.assert_frame_equal(df, cached)
