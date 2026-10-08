"""Shared test helpers — no test ever hits the network (httpx.MockTransport only)."""

from __future__ import annotations

import json
import time
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
        # Delisted perps remain in `meta` but must never be scanned.
        {"name": "OLD", "szDecimals": 0, "maxLeverage": 3, "isDelisted": True},
    ]
}

# HIP-3 builder dex carrying tradfi perps; names come back already prefixed.
XYZ_META = {
    "universe": [
        {"name": "xyz:GOLD", "szDecimals": 4, "maxLeverage": 25},
        {"name": "xyz:SP500", "szDecimals": 4, "maxLeverage": 30},
        {"name": "xyz:RETIRED", "szDecimals": 1, "maxLeverage": 5, "isDelisted": True},
    ]
}

META_BY_DEX = {"": META, "xyz": XYZ_META}


def load_fixture(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text())


def make_clearinghouse_state(positions: list[dict]) -> dict:
    """A minimal clearinghouseState payload from (coin, szi, entryPx) dicts."""
    return {
        "assetPositions": [{"type": "oneWay", "position": p} for p in positions],
        "marginSummary": {"accountValue": "0"},
    }


def make_client(
    candle_fixtures: dict[tuple[str, str], list[dict]],
    cache_dir: Path,
    requests_log: list[dict] | None = None,
    clearinghouse_states: dict[str, dict] | None = None,
) -> HyperliquidClient:
    """Client backed by httpx.MockTransport serving recorded fixtures."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if requests_log is not None:
            requests_log.append(payload)
        if payload["type"] == "meta":
            dex = payload.get("dex", "")
            if dex not in META_BY_DEX:
                return httpx.Response(400, json={"error": f"unknown dex {dex}"})
            return httpx.Response(200, json=META_BY_DEX[dex])
        if payload["type"] == "candleSnapshot":
            req = payload["req"]
            candles = candle_fixtures.get((req["coin"], req["interval"]), [])
            served = [c for c in candles if req["startTime"] <= c["t"] <= req["endTime"]]
            return httpx.Response(200, json=served)
        if payload["type"] == "clearinghouseState":
            states = clearinghouse_states or {}
            user, dex = payload["user"], payload.get("dex", "")
            # States may be keyed by user (native clearinghouse) or by
            # (user, dex) for a HIP-3 builder dex like "xyz".
            state = states.get((user, dex))
            if state is None and not dex:
                state = states.get(user)
            return httpx.Response(200, json=state or {"assetPositions": []})
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


INTERVAL_MS = {
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
    "1w": 604_800_000,
}


def synthetic_candles(
    interval: str,
    count: int = 0,
    start: float = 100.0,
    step: float = 1.0,
    end_ms: int | None = None,
    volume: float = 1000.0,
    closes: list[float] | None = None,
) -> list[dict]:
    """candleSnapshot-shaped bars for an arbitrary interval, oldest first.

    Lets a test build a whole timeframe chain (e.g. 4h/1h/15m) without another
    recorded fixture file — the strategy only cares about the shape of the series.
    A straight line of `count` bars by default; pass `closes` to replay a crafted
    series (bars then match `make_ohlcv`: open = previous close, ±0.5 wicks).

    The series ends just before "now" by default: an intraday refresh asks for
    `now - lookback * bar`, so bars anchored to a fixed past date would fall
    outside the window and the asset would look unlisted.
    """
    bar = INTERVAL_MS[interval]
    if closes is None:
        closes = [start + step * i for i in range(count)]
    count = len(closes)
    if end_ms is None:
        # Last bar fully in the past, so completed_bars() keeps it.
        end_ms = (int(time.time() * 1000) // bar) * bar - 2 * bar
    out = []
    for i in range(count):
        t = end_ms - (count - 1 - i) * bar
        close = closes[i]
        prev = closes[i - 1] if i else close - step
        out.append(
            {
                "t": t,
                "T": t + bar - 1,
                "o": str(prev),
                "h": str(max(prev, close) + 0.5),
                "l": str(min(prev, close) - 0.5),
                "c": str(close),
                "v": str(volume),
                "n": 1,
            }
        )
    return out


@pytest.fixture
def btc_fixtures() -> dict[tuple[str, str], list[dict]]:
    return {
        ("BTC", "1w"): load_fixture("btc_1w.json"),
        ("BTC", "1d"): load_fixture("btc_1d.json"),
    }


@pytest.fixture
def chain_fixtures(btc_fixtures) -> dict[tuple[str, str], list[dict]]:
    """BTC bars on every interval the two-horizon test config uses."""
    return btc_fixtures | {
        ("BTC", "4h"): synthetic_candles("4h", 60, start=90_000.0, step=50.0),
        ("BTC", "1h"): synthetic_candles("1h", 80, start=94_000.0, step=10.0),
        ("BTC", "15m"): synthetic_candles("15m", 80, start=95_000.0, step=2.0),
    }


@pytest.fixture
def long_setup_fixtures() -> dict[tuple[str, str], list[dict]]:
    """A clean swing long for BTC — a weekly uptrend and a healthy daily pullback
    (the `DAILY_LONG` shape of test_triple_screen.py) — replayed through the pipeline."""
    daily = (
        [100.0 + i for i in range(34)]
        + [130.0, 128.0]
        + [128.0 + i for i in range(1, 13)]
        + [139.0]
    )
    return {
        ("BTC", "1w"): synthetic_candles("1w", closes=[100.0 + 2 * i for i in range(40)]),
        ("BTC", "1d"): synthetic_candles("1d", closes=daily),
    }
