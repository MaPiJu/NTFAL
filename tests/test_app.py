"""Pipeline + dashboard integration test — fixtures only, no live network."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.main import app
from app.pipeline import build_snapshot
from config import Config, PositionsConfig, RiskConfig, ScannerConfig
from tests.conftest import make_clearinghouse_state, make_client


def make_config(cache_dir, watchlist=("BTC",), address="", **risk_overrides) -> Config:
    risk = {
        "equity": 10_000.0,
        "risk_pct": 0.01,
        "equity_at_month_start": 10_000.0,
        "month_realized_losses": 0.0,
        "open_trade_risk": 0.0,
    } | risk_overrides
    return Config(
        scanner=ScannerConfig(
            watchlist=watchlist,
            weekly_interval="1w",
            daily_interval="1d",
            third_screen_interval="4h",
            use_third_screen=False,
            lookback_weeks=260,
            lookback_days=400,
            lookback_third_screen=300,
        ),
        risk=RiskConfig(**risk),
        positions=PositionsConfig(address=address),
        cache_dir=cache_dir,
    )


def test_build_snapshot_from_fixtures(tmp_path, btc_fixtures):
    cfg = make_config(tmp_path)
    client = make_client(btc_fixtures, tmp_path)
    snapshot = build_snapshot(cfg, client)

    assert snapshot["guard"]["blocked"] is False
    assert "top_pick" in snapshot  # name of the best trade, or None
    (sig,) = snapshot["signals"]
    assert sig["asset"] == "BTC"
    assert sig["action"] in {"long", "short", "stand_aside"}
    assert sig["weekly_impulse"] in {"green", "red", "blue"}
    # new fields surfaced for the dashboard / ranking
    assert "last_close" in sig and "quality_score" in sig and "is_top_pick" in sig
    # the top pick (if any) must be a tradable, R:R-passing setup
    if snapshot["top_pick"] is not None:
        pick = next(s for s in snapshot["signals"] if s["asset"] == snapshot["top_pick"])
        assert pick["action"] != "stand_aside" and pick["rr_ok"] and pick["is_top_pick"]

    charts = snapshot["charts"]["BTC"]
    for interval in ("weekly", "daily"):
        payload = charts[interval]
        assert payload["candles"], f"no {interval} candles"
        candle = payload["candles"][0]
        assert {"time", "open", "high", "low", "close", "color"} <= set(candle)
        assert payload["ema13"] and payload["ema26"]
        assert payload["macd_hist"] and payload["force_index_2"]


def test_wildcard_watchlist_scans_whole_universe(tmp_path, btc_fixtures):
    cfg = make_config(tmp_path, watchlist=("*",))
    client = make_client(btc_fixtures, tmp_path)
    progress: list[str] = []
    snapshot = build_snapshot(cfg, client, on_progress=progress.append)

    # Every tradable perp is scanned (delisted OLD is not), but only BTC has
    # enough recorded history to be evaluated — the rest are reported skipped.
    assert progress == ["BTC", "ETH", "HYPE", "SOL"]
    assert [s["asset"] for s in snapshot["signals"]] == ["BTC"]
    assert snapshot["skipped"] == ["ETH", "HYPE", "SOL"]
    assert list(snapshot["charts"]) == ["BTC"]


def test_tradfi_dex_wildcard_scans_builder_universe(tmp_path, btc_fixtures):
    fixtures = dict(btc_fixtures)
    fixtures[("xyz:GOLD", "1w")] = btc_fixtures[("BTC", "1w")]
    fixtures[("xyz:GOLD", "1d")] = btc_fixtures[("BTC", "1d")]
    cfg = make_config(tmp_path, watchlist=("*", "xyz:*"))
    client = make_client(fixtures, tmp_path)
    progress: list[str] = []
    snapshot = build_snapshot(cfg, client, on_progress=progress.append)

    # Native crypto universe + the whole tradfi ("xyz") dex, delisted excluded.
    assert progress == ["BTC", "ETH", "HYPE", "SOL", "xyz:GOLD", "xyz:SP500"]
    assert [s["asset"] for s in snapshot["signals"]] == ["BTC", "xyz:GOLD"]
    assert snapshot["skipped"] == ["ETH", "HYPE", "SOL", "xyz:SP500"]
    assert "xyz:GOLD" in snapshot["charts"]


def test_explicit_tradfi_coin_in_watchlist(tmp_path, btc_fixtures):
    fixtures = dict(btc_fixtures)
    fixtures[("xyz:GOLD", "1w")] = btc_fixtures[("BTC", "1w")]
    fixtures[("xyz:GOLD", "1d")] = btc_fixtures[("BTC", "1d")]
    cfg = make_config(tmp_path, watchlist=("BTC", "xyz:GOLD"))
    client = make_client(fixtures, tmp_path)
    snapshot = build_snapshot(cfg, client)
    assert [s["asset"] for s in snapshot["signals"]] == ["BTC", "xyz:GOLD"]


def test_no_address_means_no_positions(tmp_path, btc_fixtures):
    cfg = make_config(tmp_path)
    client = make_client(btc_fixtures, tmp_path)
    snapshot = build_snapshot(cfg, client)
    assert snapshot["positions"] == []
    assert snapshot["position_address"] is None


def test_open_position_gets_management_verdict(tmp_path, btc_fixtures):
    addr = "0x" + "ab" * 20
    cfg = make_config(tmp_path, address=addr)
    state = make_clearinghouse_state([{"coin": "BTC", "szi": "0.5", "entryPx": "50000.0"}])
    client = make_client(btc_fixtures, tmp_path, clearinghouse_states={addr: state})
    snapshot = build_snapshot(cfg, client)

    assert snapshot["position_address"].startswith("0xabab") and "…" in snapshot["position_address"]
    (pos,) = snapshot["positions"]
    assert pos["asset"] == "BTC"
    assert pos["side"] == "long"
    assert pos["entry"] == 50000.0
    assert pos["verdict"] in {"hold", "take_profits", "exit"}
    assert pos["reasons"]


def test_held_coin_outside_watchlist_is_fetched_on_demand(tmp_path, btc_fixtures):
    # The watchlist scans nothing, yet a held coin still gets its candles fetched
    # and a management verdict produced.
    fixtures = dict(btc_fixtures)
    fixtures[("ETH", "1w")] = btc_fixtures[("BTC", "1w")]
    fixtures[("ETH", "1d")] = btc_fixtures[("BTC", "1d")]
    addr = "0x" + "cd" * 20
    cfg = make_config(tmp_path, watchlist=("BTC",), address=addr)
    state = make_clearinghouse_state([{"coin": "ETH", "szi": "-1.0", "entryPx": "4000.0"}])
    client = make_client(fixtures, tmp_path, clearinghouse_states={addr: state})
    snapshot = build_snapshot(cfg, client)

    assert [s["asset"] for s in snapshot["signals"]] == ["BTC"]  # ETH not scanned
    (pos,) = snapshot["positions"]
    assert pos["asset"] == "ETH" and pos["side"] == "short"


def test_snapshot_reports_tripped_guard(tmp_path, btc_fixtures):
    cfg = make_config(tmp_path, month_realized_losses=700.0)
    client = make_client(btc_fixtures, tmp_path)
    snapshot = build_snapshot(cfg, client)
    assert snapshot["guard"]["blocked"] is True
    # no sizing suggestions while the 6% guard is active
    assert all(s["position_size"] is None for s in snapshot["signals"])


def test_dashboard_endpoints(tmp_path, btc_fixtures, monkeypatch):
    cfg = make_config(tmp_path)
    client = make_client(btc_fixtures, tmp_path)
    snapshot = build_snapshot(cfg, client)
    snapshot_file = tmp_path / "snapshot.json"
    snapshot_file.write_text(json.dumps(snapshot))
    monkeypatch.setenv("SNAPSHOT_PATH", str(snapshot_file))

    web = TestClient(app)
    api = web.get("/api/snapshot")
    assert api.status_code == 200
    assert api.json()["signals"][0]["asset"] == "BTC"

    page = web.get("/")
    assert page.status_code == 200
    assert "TradingView" in page.text  # required attribution
    assert "not financial advice" in page.text


def test_snapshot_missing_returns_404(monkeypatch, tmp_path):
    monkeypatch.setenv("SNAPSHOT_PATH", str(tmp_path / "missing.json"))
    web = TestClient(app)
    resp = web.get("/api/snapshot")
    assert resp.status_code == 404
    assert "run.py" in resp.json()["error"]
