"""Multi-platform provider tests (Omni hybrid candles) — fixtures only, no network."""

from __future__ import annotations

import httpx
import pytest

from app.pipeline import expand_watchlist, fetch_open_positions, watchlist_dexes
from config import ManualPosition, load_config
from data.provider import OMNI_SZ_DECIMALS, OmniProvider, PlatformRouter, is_omni, platform_of
from data.variational import VariationalClient, VariationalError
from tests.conftest import make_client
from tests.test_app import make_config

DAY_MS = 86_400_000
NOW_MS = 1_700_000_000_000

# Omni universe for these tests: ETH overlaps Hyperliquid native, GOLD overlaps
# the xyz tradfi dex (as bare name), AIOZ and NOVOL are Omni-only.
OMNI_STATS = {
    "listings": [
        {"ticker": "ETH", "name": "Ethereum", "mark_price": "2500.1", "funding_rate": "0.1"},
        {"ticker": "GOLD", "name": "Gold", "mark_price": "2400.0", "funding_rate": "0.1"},
        {"ticker": "AIOZ", "name": "AIOZ Network", "mark_price": "0.05", "funding_rate": "0.1"},
        {"ticker": "NOVOL", "name": "No Volume", "mark_price": "1.0", "funding_rate": "0.1"},
    ]
}

CLOUDFLARE_HTML = "<!DOCTYPE html><html><head><title>Just a moment...</title></head></html>"


def hl_candles(n: int, interval_ms: int = DAY_MS) -> list[dict]:
    start = NOW_MS - n * interval_ms
    return [
        {
            "t": start + i * interval_ms,
            "T": start + (i + 1) * interval_ms - 1,
            "o": "100",
            "h": "105",
            "l": "95",
            "c": "102",
            "v": "1000",
            "n": 10,
        }
        for i in range(n)
    ]


def omni_candles(n: int, with_volume: bool) -> list[dict]:
    start = NOW_MS - n * DAY_MS
    rows = []
    for i in range(n):
        row = {"unix_time_ms": start + i * DAY_MS, "open": "1", "high": "2", "low": "0.5"}
        row["close"] = "1.5"
        if with_volume:
            row["volume"] = "50"
        rows.append(row)
    return rows


def make_router(
    tmp_path,
    native_candles: dict[str, list[dict]] | None = None,
    blocked: bool = False,
    variational_log: list[httpx.Request] | None = None,
) -> PlatformRouter:
    """Router over a mocked Hyperliquid client + a mocked Variational client."""
    hl = make_client(
        {
            ("ETH", "1d"): hl_candles(10),
            ("xyz:GOLD", "1d"): hl_candles(10),
        },
        tmp_path,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if variational_log is not None:
            variational_log.append(request)
        if request.url.path.endswith("/metadata/stats"):
            return httpx.Response(200, json=OMNI_STATS)
        if request.url.path.endswith("/candles"):
            if blocked:
                headers = {"content-type": "text/html"}
                return httpx.Response(403, text=CLOUDFLARE_HTML, headers=headers)
            ticker = dict(httpx.QueryParams(request.url.query))["cex_asset"]
            return httpx.Response(200, json=(native_candles or {}).get(ticker, []))
        return httpx.Response(404)

    variational = VariationalClient(
        http=httpx.Client(transport=httpx.MockTransport(handler)), cache_dir=tmp_path
    )
    return PlatformRouter(hl, OmniProvider(variational, hl))


# -- naming ---------------------------------------------------------------------


def test_is_omni_and_platform_of():
    assert is_omni("omni:ETH") and not is_omni("ETH") and not is_omni("xyz:GOLD")
    assert platform_of("omni:ETH") == "variational"
    assert platform_of("BTC") == platform_of("xyz:GOLD") == "hyperliquid"


# -- watchlist expansion ----------------------------------------------------------


def test_expand_watchlist_omni_wildcard(tmp_path):
    router = make_router(tmp_path)
    got = expand_watchlist(("omni:*",), router)
    assert got == {
        "omni:AIOZ": OMNI_SZ_DECIMALS,
        "omni:ETH": OMNI_SZ_DECIMALS,
        "omni:GOLD": OMNI_SZ_DECIMALS,
        "omni:NOVOL": OMNI_SZ_DECIMALS,
    }


def test_expand_watchlist_mixed_platforms(tmp_path):
    router = make_router(tmp_path)
    got = expand_watchlist(("BTC", "xyz:GOLD", "omni:ETH"), router)
    assert set(got) == {"BTC", "xyz:GOLD", "omni:ETH"}
    assert got["BTC"] == 5  # from the Hyperliquid meta fixture
    assert got["omni:ETH"] == OMNI_SZ_DECIMALS


def test_validate_watchlist_unknown_omni_ticker(tmp_path):
    router = make_router(tmp_path)
    with pytest.raises(VariationalError, match="omni:DOGEZILLA"):
        expand_watchlist(("omni:DOGEZILLA",), router)


def test_watchlist_dexes_excludes_the_omni_platform():
    assert watchlist_dexes(("*", "xyz:*", "omni:*", "omni:ETH")) == ["", "xyz"]


# -- hybrid candle routing ---------------------------------------------------------


def test_omni_candles_delegate_to_native_hyperliquid(tmp_path):
    log: list[httpx.Request] = []
    router = make_router(tmp_path, variational_log=log)
    df = router.refresh("omni:ETH", "1d", 10, now_ms=NOW_MS)

    assert len(df) == 10
    assert df["volume"].iloc[0] == 1000.0  # Hyperliquid candles carry volume
    # shared cache: stored under the Hyperliquid coin name, not a variational_ file
    assert (tmp_path / "ETH_1d.parquet").exists()
    assert not any(r.url.path.endswith("/candles") for r in log)


def test_omni_candles_delegate_to_xyz_tradfi_dex(tmp_path):
    router = make_router(tmp_path)
    df = router.refresh("omni:GOLD", "1d", 10, now_ms=NOW_MS)
    assert len(df) == 10
    assert (tmp_path / "xyz_GOLD_1d.parquet").exists()


def test_omni_only_ticker_uses_native_candles_when_they_have_volume(tmp_path):
    router = make_router(tmp_path, native_candles={"AIOZ": omni_candles(10, with_volume=True)})
    df = router.refresh("omni:AIOZ", "1d", 10, now_ms=NOW_MS)
    assert len(df) == 10
    assert (tmp_path / "variational_AIOZ_1d.parquet").exists()
    assert router.notes == {}


def test_omni_native_candles_without_volume_are_unusable(tmp_path):
    router = make_router(tmp_path, native_candles={"NOVOL": omni_candles(10, with_volume=False)})
    df = router.refresh("omni:NOVOL", "1d", 10, now_ms=NOW_MS)
    assert df.empty
    assert "volume" in router.notes["omni:NOVOL"]


def test_cloudflare_block_is_remembered_across_tickers(tmp_path):
    log: list[httpx.Request] = []
    router = make_router(tmp_path, blocked=True, variational_log=log)

    assert router.refresh("omni:AIOZ", "1d", 10, now_ms=NOW_MS).empty
    assert router.refresh("omni:NOVOL", "1d", 10, now_ms=NOW_MS).empty
    assert "Cloudflare" in router.notes["omni:AIOZ"]
    assert "Cloudflare" in router.notes["omni:NOVOL"]
    # only the first ticker hit the network; the block is memoized after that
    assert sum(r.url.path.endswith("/candles") for r in log) == 1


def test_non_omni_coins_go_straight_to_hyperliquid(tmp_path):
    router = make_router(tmp_path)
    df = router.refresh("ETH", "1d", 10, now_ms=NOW_MS)
    assert len(df) == 10


# -- manual positions ---------------------------------------------------------------


def test_manual_positions_merge_without_an_address(tmp_path):
    cfg = make_config(
        tmp_path, manual=(ManualPosition(asset="omni:ETH", side="long", size=1.5, entry=2400.0),)
    )
    router = make_router(tmp_path)
    positions = fetch_open_positions(cfg, router)
    assert [(p.asset, p.side, p.size, p.entry) for p in positions] == [
        ("omni:ETH", "long", 1.5, 2400.0)
    ]


def test_load_config_parses_manual_positions(tmp_path):
    (tmp_path / "config.toml").write_text("""
[scanner]
watchlist = ["omni:ETH"]
[risk]
equity = 1000.0
risk_pct = 0.01
[positions]
address = ""
[[positions.manual]]
asset = "omni:ETH"
side = "LONG"
size = -1.5
entry = 2400.0
""")
    cfg = load_config(tmp_path / "config.toml")
    assert cfg.positions.manual == (
        ManualPosition(asset="omni:ETH", side="long", size=1.5, entry=2400.0),
    )


def test_load_config_rejects_bad_manual_side(tmp_path):
    (tmp_path / "config.toml").write_text("""
[scanner]
watchlist = []
[risk]
equity = 1000.0
risk_pct = 0.01
[[positions.manual]]
asset = "omni:ETH"
side = "hodl"
size = 1.0
entry = 1.0
""")
    with pytest.raises(ValueError, match="hodl"):
        load_config(tmp_path / "config.toml")
