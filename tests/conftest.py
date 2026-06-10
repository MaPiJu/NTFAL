"""Shared test helpers — no test ever hits the network (httpx.MockTransport only)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pandas as pd
import pytest

from data.hyperliquid import HyperliquidClient

FIXTURES = Path(__file__).parent / "fixtures"

META = {
    "universe": [
        {"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
        {"name": "ETH", "szDecimals": 4, "maxLeverage": 25},
        {"name": "SOL", "szDecimals": 2, "maxLeverage": 20},
        {"name": "HYPE", "szDecimals": 2, "maxLeverage": 10},
    ]
}


def load_fixture(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text())


def make_client(
    candle_fixtures: dict[tuple[str, str], list[dict]],
    cache_dir: Path,
    requests_log: list[dict] | None = None,
) -> HyperliquidClient:
    """Client backed by httpx.MockTransport serving recorded fixtures."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if requests_log is not None:
            requests_log.append(payload)
        if payload["type"] == "meta":
            return httpx.Response(200, json=META)
        if payload["type"] == "candleSnapshot":
            req = payload["req"]
            candles = candle_fixtures.get((req["coin"], req["interval"]), [])
            served = [c for c in candles if req["startTime"] <= c["t"] <= req["endTime"]]
            return httpx.Response(200, json=served)
        return httpx.Response(400, json={"error": "unexpected request"})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return HyperliquidClient(http=http, cache_dir=cache_dir)


def make_ohlcv(
    closes: list[float],
    volumes: list[float] | None = None,
    lows: list[float] | None = None,
    highs: list[float] | None = None,
    freq: str = "D",
) -> pd.DataFrame:
    """Synthetic OHLCV frame for strategy tests (oldest first, UTC index)."""
    n = len(closes)
    closes_s = pd.Series(closes, dtype="float64")
    opens = closes_s.shift(1).fillna(closes_s.iloc[0])
    high = (
        pd.Series(highs, dtype="float64")
        if highs is not None
        else pd.concat([opens, closes_s], axis=1).max(axis=1) + 0.5
    )
    low = (
        pd.Series(lows, dtype="float64")
        if lows is not None
        else pd.concat([opens, closes_s], axis=1).min(axis=1) - 0.5
    )
    volume = pd.Series(volumes if volumes is not None else [1000.0] * n, dtype="float64")
    index = pd.date_range("2024-01-01", periods=n, freq=freq, tz="UTC")
    t = (index.view("int64") // 1_000_000).astype("int64")
    df = pd.DataFrame(
        {
            "t": t,
            "T": t + 1,
            "open": opens.values,
            "high": high.values,
            "low": low.values,
            "close": closes_s.values,
            "volume": volume.values,
            "trades": [1] * n,
        },
        index=index,
    )
    df.index.name = "time"
    return df


@pytest.fixture
def btc_fixtures() -> dict[tuple[str, str], list[dict]]:
    return {
        ("BTC", "1w"): load_fixture("btc_1w.json"),
        ("BTC", "1d"): load_fixture("btc_1d.json"),
    }
