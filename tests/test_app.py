"""Pipeline + dashboard integration test — fixtures only, no live network."""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.pipeline import (
    build_snapshot,
    fetch_open_positions,
    live_price_alert,
    load_snapshot,
    position_open_risk,
    refresh_horizon,
    size_warnings,
)
from config import (
    Config,
    HorizonConfig,
    JournalConfig,
    PositionsConfig,
    RiskConfig,
    ScannerConfig,
)
from data.hyperliquid import PerpSpec
from journal import append_journal_entry
from risk.sizing import position_size
from strategy.params import StrategyParams
from tests.conftest import make_clearinghouse_state, make_client

SWING = HorizonConfig(
    name="swing",
    label="Swing",
    tide="1w",
    wave="1d",
    entry="4h",
    lookback_tide=260,
    lookback_wave=400,
    lookback_entry=300,
    refresh_seconds=86_400,
    min_tide_bars=60,
)
SCALP = HorizonConfig(
    name="scalp",
    label="Scalp",
    tide="4h",
    wave="1h",
    entry="15m",
    lookback_tide=300,
    lookback_wave=400,
    lookback_entry=400,
    refresh_seconds=3_600,
    min_tide_bars=60,
)


def make_config(
    cache_dir,
    watchlist=("BTC",),
    address="",
    manual=(),
    horizons=(SWING,),
    positions_horizon="swing",
    strategy=None,
    **risk_overrides,
) -> Config:
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
            horizons=tuple(horizons),
            positions_horizon=positions_horizon,
        ),
        strategy=strategy or StrategyParams(),
        risk=RiskConfig(**risk),
        positions=PositionsConfig(address=address, manual=tuple(manual)),
        journal=JournalConfig(enabled=False, path=cache_dir / "journal.jsonl"),
        cache_dir=cache_dir,
    )


def swing(snapshot):
    return snapshot["horizons"]["swing"]


def test_live_price_alert_flags_chasing_and_void_setups():
    # Long setup: entry 105, stop 95, target 130 (computed on the closed bar).
    assert live_price_alert("long", 100.0, 105.0, 95.0, 130.0) is None  # still waiting
    # Live ran through the buy-stop -> entering now is chasing.
    assert "don't chase" in live_price_alert("long", 110.0, 105.0, 95.0, 130.0)
    # Live already at the target -> nothing left to capture.
    assert "spent" in live_price_alert("long", 130.0, 105.0, 95.0, 130.0)
    # Live collapsed past the stop -> the premise is void.
    assert "void" in live_price_alert("long", 90.0, 105.0, 95.0, 130.0)

    # Short setup: entry 95, stop 105, target 70 (mirror image).
    assert live_price_alert("short", 100.0, 95.0, 105.0, 70.0) is None
    assert "don't chase" in live_price_alert("short", 90.0, 95.0, 105.0, 70.0)
    assert "spent" in live_price_alert("short", 70.0, 95.0, 105.0, 70.0)
    assert "void" in live_price_alert("short", 110.0, 95.0, 105.0, 70.0)

    # Non-actionable rows and missing data never warn.
    assert live_price_alert("stand_aside", 100.0, None, None, None) is None
    assert live_price_alert("long", None, 105.0, 95.0, 130.0) is None


def test_build_snapshot_from_fixtures(tmp_path, btc_fixtures):
    cfg = make_config(tmp_path)
    client = make_client(btc_fixtures, tmp_path)
    snapshot = build_snapshot(cfg, client)

    assert snapshot["guard"]["blocked"] is False
    assert snapshot["horizon_order"] == ["swing"]
    block = swing(snapshot)
    assert block["intervals"] == {"tide": "1w", "wave": "1d", "entry": "4h"}
    assert "top_pick" in block  # name of the best trade on this horizon, or None

    (sig,) = block["signals"]
    assert sig["asset"] == "BTC"
    assert sig["action"] in {"long", "short", "stand_aside"}
    assert sig["tide_impulse"] in {"green", "red", "blue"}
    assert sig["horizon"] == "swing"
    # fields surfaced for the dashboard / ranking
    assert "last_close" in sig and "quality_score" in sig and "is_top_pick" in sig
    # live price (still-open wave bar) + a stale-price/chasing alert
    assert "live_price" in sig and "price_alert" in sig
    # data-quality flags travel with the signal, and so do size flags
    assert isinstance(sig["data_warnings"], list)
    assert sig["size_warnings"] == []  # nothing sized, nothing to flag
    # the top pick (if any) must be a tradable, R:R-passing setup
    if block["top_pick"] is not None:
        pick = next(s for s in block["signals"] if s["asset"] == block["top_pick"])
        assert pick["action"] != "stand_aside" and pick["rr_ok"] and pick["is_top_pick"]

    charts = block["charts"]["BTC"]
    for role in ("tide", "wave"):
        payload = charts[role]
        assert payload["candles"], f"no {role} candles"
        candle = payload["candles"][0]
        assert {"time", "open", "high", "low", "close", "color"} <= set(candle)
        assert payload["ema13"] and payload["ema26"]
        assert payload["macd_hist"] and payload["force_index_2"]


def test_short_tide_history_is_flagged_not_hidden(tmp_path, btc_fixtures):
    # The BTC weekly fixture has 30 bars, under the 60-bar convergence floor.
    cfg = make_config(tmp_path)
    client = make_client(btc_fixtures, tmp_path)
    (sig,) = swing(build_snapshot(cfg, client))["signals"]

    assert any("not converged" in w for w in sig["data_warnings"])
    assert sig["action"] in {"long", "short", "stand_aside"}  # still evaluated


def test_every_horizon_gets_its_own_block(tmp_path, chain_fixtures):
    cfg = make_config(tmp_path, horizons=(SWING, SCALP))
    client = make_client(chain_fixtures, tmp_path)
    progress: list[tuple[str, str]] = []
    snapshot = build_snapshot(cfg, client, on_progress=lambda h, c: progress.append((h, c)))

    assert snapshot["horizon_order"] == ["swing", "scalp"]
    assert set(snapshot["horizons"]) == {"swing", "scalp"}
    assert progress == [("swing", "BTC"), ("scalp", "BTC")]

    scalp = snapshot["horizons"]["scalp"]
    assert scalp["intervals"] == {"tide": "4h", "wave": "1h", "entry": "15m"}
    assert scalp["refresh_seconds"] == 3_600
    (sig,) = scalp["signals"]
    assert sig["horizon"] == "scalp"
    # the scalp chain has a usable third screen, so its entry chart exists
    assert "entry" in scalp["charts"]["BTC"]

    # Each horizon ranks its own setups — a pick is never borrowed from another.
    for name, block in snapshot["horizons"].items():
        if block["top_pick"] is not None:
            assert any(s["asset"] == block["top_pick"] for s in block["signals"]), name


def test_refresh_horizon_leaves_the_other_blocks_untouched(tmp_path, chain_fixtures):
    cfg = make_config(tmp_path, horizons=(SWING, SCALP))
    client = make_client(chain_fixtures, tmp_path)
    full = build_snapshot(cfg, client)
    swing_before = json.dumps(full["horizons"]["swing"], sort_keys=True)

    updated = refresh_horizon(cfg, client, "scalp", full)

    # The untouched horizon is byte-identical, timestamp included.
    assert json.dumps(updated["horizons"]["swing"], sort_keys=True) == swing_before
    assert updated["horizons"]["scalp"]["generated_at"] >= full["horizons"]["scalp"]["generated_at"]
    assert set(updated["horizons"]) == {"swing", "scalp"}
    # The merged snapshot is still JSON-serializable (no dataclasses leak through).
    json.dumps(updated)


def test_refresh_of_a_non_managing_horizon_keeps_positions(tmp_path, chain_fixtures):
    # Positions are managed on 'swing'; refreshing 'scalp' must not drop them.
    addr = "0x" + "ab" * 20
    cfg = make_config(tmp_path, horizons=(SWING, SCALP), address=addr)
    state = make_clearinghouse_state([{"coin": "BTC", "szi": "0.5", "entryPx": "50000.0"}])
    client = make_client(chain_fixtures, tmp_path, clearinghouse_states={addr: state})
    full = build_snapshot(cfg, client)
    assert full["positions"]

    updated = refresh_horizon(cfg, client, "scalp", full)
    assert updated["positions"] == full["positions"]
    assert updated["guard"] == full["guard"]


def test_refresh_horizon_on_an_empty_snapshot_still_produces_that_block(tmp_path, btc_fixtures):
    cfg = make_config(tmp_path)
    client = make_client(btc_fixtures, tmp_path)
    snapshot = refresh_horizon(cfg, client, "swing", {})
    assert swing(snapshot)["signals"]


def test_load_snapshot_handles_missing_and_corrupt_files(tmp_path):
    assert load_snapshot(tmp_path / "nope.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert load_snapshot(bad) == {}


def test_wildcard_watchlist_scans_whole_universe(tmp_path, btc_fixtures):
    cfg = make_config(tmp_path, watchlist=("*",))
    client = make_client(btc_fixtures, tmp_path)
    progress: list[str] = []
    snapshot = build_snapshot(cfg, client, on_progress=lambda h, c: progress.append(c))

    # Every tradable perp is scanned (delisted OLD is not), but only BTC has
    # enough recorded history to be evaluated — the rest are reported skipped.
    assert progress == ["BTC", "ETH", "HYPE", "SOL"]
    block = swing(snapshot)
    assert [s["asset"] for s in block["signals"]] == ["BTC"]
    assert [s.split(" — ")[0] for s in block["skipped"]] == ["ETH", "HYPE", "SOL"]
    assert list(block["charts"]) == ["BTC"]


def test_tradfi_dex_wildcard_scans_builder_universe(tmp_path, btc_fixtures):
    fixtures = dict(btc_fixtures)
    fixtures[("xyz:GOLD", "1w")] = btc_fixtures[("BTC", "1w")]
    fixtures[("xyz:GOLD", "1d")] = btc_fixtures[("BTC", "1d")]
    cfg = make_config(tmp_path, watchlist=("*", "xyz:*"))
    client = make_client(fixtures, tmp_path)
    progress: list[str] = []
    snapshot = build_snapshot(cfg, client, on_progress=lambda h, c: progress.append(c))

    # Native crypto universe + the whole tradfi ("xyz") dex, delisted excluded.
    assert progress == ["BTC", "ETH", "HYPE", "SOL", "xyz:GOLD", "xyz:SP500"]
    block = swing(snapshot)
    assert [s["asset"] for s in block["signals"]] == ["BTC", "xyz:GOLD"]
    assert "xyz:GOLD" in block["charts"]


def test_explicit_tradfi_coin_in_watchlist(tmp_path, btc_fixtures):
    fixtures = dict(btc_fixtures)
    fixtures[("xyz:GOLD", "1w")] = btc_fixtures[("BTC", "1w")]
    fixtures[("xyz:GOLD", "1d")] = btc_fixtures[("BTC", "1d")]
    cfg = make_config(tmp_path, watchlist=("BTC", "xyz:GOLD"))
    client = make_client(fixtures, tmp_path)
    snapshot = build_snapshot(cfg, client)
    assert [s["asset"] for s in swing(snapshot)["signals"]] == ["BTC", "xyz:GOLD"]


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
    assert snapshot["positions_horizon"] == "swing"
    (pos,) = snapshot["positions"]
    assert pos["asset"] == "BTC"
    assert pos["side"] == "long"
    assert pos["entry"] == 50000.0
    assert pos["verdict"] in {"hold", "take_profits", "exit"}
    assert pos["reasons"]
    assert pos["open_risk"] == max(0.0, pos["entry"] - pos["suggested_stop"]) * pos["size"]


def test_funding_paid_reaches_the_snapshot_cli_and_journal(tmp_path, btc_fixtures, capsys):
    addr = "0x" + "ab" * 20
    cfg = make_config(tmp_path, address=addr)
    position = {
        "coin": "BTC",
        "szi": "0.5",
        "entryPx": "50000.0",
        "cumFunding": {"allTime": "-3.0", "sinceOpen": "-1.25", "sinceChange": "-1.25"},
    }
    state = make_clearinghouse_state([position])
    client = make_client(btc_fixtures, tmp_path, clearinghouse_states={addr: state})
    snapshot = build_snapshot(cfg, client)

    (pos,) = snapshot["positions"]
    assert pos["cum_funding"] == -1.25  # received, not paid

    from run import print_positions_table

    print_positions_table(snapshot)
    out = capsys.readouterr().out
    assert "FUNDING PAID" in out and "-1.25" in out

    journal = tmp_path / "journal.jsonl"
    append_journal_entry(snapshot, journal)
    (entry,) = [json.loads(line) for line in journal.read_text().splitlines()]
    assert entry["positions"][0]["cum_funding"] == -1.25


def test_manual_position_merges_without_an_address(tmp_path, btc_fixtures):
    from config import ManualPosition

    cfg = make_config(
        tmp_path, manual=(ManualPosition(asset="BTC", side="long", size=1.5, entry=50_000.0),)
    )
    client = make_client(btc_fixtures, tmp_path)
    snapshot = build_snapshot(cfg, client)

    (pos,) = snapshot["positions"]
    assert (pos["asset"], pos["side"], pos["size"], pos["entry"]) == ("BTC", "long", 1.5, 50_000.0)


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

    assert [s["asset"] for s in swing(snapshot)["signals"]] == ["BTC"]  # ETH not scanned
    (pos,) = snapshot["positions"]
    assert pos["asset"] == "ETH" and pos["side"] == "short"


def test_position_on_builder_dex_is_found(tmp_path, btc_fixtures):
    # A position held on a HIP-3 builder dex ("xyz") lives in a *separate*
    # clearinghouse and is invisible to the native lookup; the watchlist's
    # "xyz:*" entry must drive a per-dex query so it still gets a verdict.
    fixtures = dict(btc_fixtures)
    fixtures[("xyz:GOLD", "1w")] = btc_fixtures[("BTC", "1w")]
    fixtures[("xyz:GOLD", "1d")] = btc_fixtures[("BTC", "1d")]
    addr = "0x" + "12" * 20
    cfg = make_config(tmp_path, watchlist=("*", "xyz:*"), address=addr)
    xyz_state = make_clearinghouse_state([{"coin": "xyz:GOLD", "szi": "2.0", "entryPx": "2000.0"}])
    client = make_client(fixtures, tmp_path, clearinghouse_states={(addr, "xyz"): xyz_state})
    snapshot = build_snapshot(cfg, client)

    (pos,) = snapshot["positions"]
    assert pos["asset"] == "xyz:GOLD"
    assert pos["side"] == "long"
    assert pos["entry"] == 2000.0
    assert pos["verdict"] in {"hold", "take_profits", "exit"}


def test_positions_survive_a_failing_dex(tmp_path):
    # clearinghouseState is queried once per dex; an HTTP error or timeout on one
    # dex (here the tradfi "xyz" one) must not drop the positions read on the
    # others, nor abort the whole refresh.
    addr = "0x" + "34" * 20
    cfg = make_config(tmp_path, watchlist=("BTC", "xyz:GOLD"), address=addr)

    class FlakyDex:
        def clearinghouse_state(self, address: str, dex: str = "") -> dict:
            if dex == "xyz":
                req = httpx.Request("POST", "https://api.hyperliquid.xyz/info")
                raise httpx.HTTPStatusError(
                    "502 Bad Gateway", request=req, response=httpx.Response(502, request=req)
                )
            return make_clearinghouse_state([{"coin": "BTC", "szi": "0.5", "entryPx": "50000.0"}])

    (pos,) = fetch_open_positions(cfg, FlakyDex())
    assert (pos.asset, pos.side, pos.entry) == ("BTC", "long", 50000.0)


def test_snapshot_reports_tripped_guard(tmp_path, chain_fixtures):
    cfg = make_config(tmp_path, horizons=(SWING, SCALP), month_realized_losses=700.0)
    client = make_client(chain_fixtures, tmp_path)
    snapshot = build_snapshot(cfg, client)
    assert snapshot["guard"]["blocked"] is True
    # The guard is global: no sizing and no pick on ANY horizon.
    for block in snapshot["horizons"].values():
        assert all(s["position_size"] is None for s in block["signals"])
        assert block["top_pick"] is None


def test_no_size_is_suggested_when_the_target_is_already_passed(tmp_path, chain_fixtures):
    # A non-positive reward:risk means price ran through the whole value zone and
    # channel: there is no reward left, so there is nothing to size. Sub-2:1 setups
    # are a different case and DO keep a size (flag, don't hide).
    cfg = make_config(tmp_path, horizons=(SWING, SCALP))
    client = make_client(chain_fixtures, tmp_path)
    snapshot = build_snapshot(cfg, client)

    for block in snapshot["horizons"].values():
        for s in block["signals"]:
            if s["position_size"] is not None:
                assert s["reward_risk"] > 0, s["asset"]


def test_open_risk_is_zero_once_the_stop_locks_in_profit():
    # Elder's 6% Rule (p.208-209): open risk is the distance from entry to the
    # current stop, and it is ZERO once the stop sits at or beyond break-even —
    # "nothing in stock A, because its stop is above breakeven". A stop that locks
    # in profit must not be counted as risk, or the guard trips on winning trades.
    def risk(side, entry, stop, size=10.0):
        return position_open_risk(
            {"side": side, "entry": entry, "suggested_stop": stop, "size": size}
        )

    assert risk("long", 100.0, 95.0) == 50.0  # stop below entry: real risk
    assert risk("long", 100.0, 105.0) == 0.0  # trailing stop above entry: profit locked
    assert risk("long", 100.0, 100.0) == 0.0  # at break-even
    assert risk("short", 100.0, 104.0) == 40.0
    assert risk("short", 100.0, 95.0) == 0.0  # short stop below entry: profit locked


def test_pipeline_passes_sz_decimals_to_the_strategy(tmp_path, btc_fixtures, monkeypatch):
    # The tick of an entry order depends on the asset's szDecimals (Hyperliquid:
    # at most 6 - szDecimals decimals), so the pipeline must hand it over.
    import app.pipeline as pipeline

    seen: dict[str, int | None] = {}
    real = pipeline.evaluate_asset

    def spy(asset, *args, **kwargs):
        seen[asset] = kwargs.get("sz_decimals")
        return real(asset, *args, **kwargs)

    monkeypatch.setattr(pipeline, "evaluate_asset", spy)
    build_snapshot(make_config(tmp_path), make_client(btc_fixtures, tmp_path))

    assert seen == {"BTC": 5}


def test_two_percent_rule_sizes_on_equity_at_month_start(tmp_path, long_setup_fixtures):
    # Elder (p.204): "Measure your account equity on the first day of each month" —
    # the 2% limit is set from that figure for the whole month, not from today's
    # equity, so a winning (or losing) streak doesn't resize trades mid-month.
    cfg = make_config(tmp_path, equity=12_000.0, equity_at_month_start=8_000.0)
    snapshot = build_snapshot(cfg, make_client(long_setup_fixtures, tmp_path))

    (sig,) = swing(snapshot)["signals"]
    assert sig["action"] == "long"
    ps = sig["position_size"]
    assert ps["risk_budget"] == pytest.approx(80.0)  # 1% of 8,000, not of 12,000
    expected = position_size(8_000.0, sig["entry"], sig["stop"], 0.01, sz_decimals=5)
    assert ps["size"] == expected.size


def test_size_is_worked_out_from_the_rounded_entry_and_stop(tmp_path, long_setup_fixtures):
    # The suggested size must be the one an order at the exchange's prices gives:
    # both the buy-stop and the stop sit on Hyperliquid's grid before sizing.
    snapshot = build_snapshot(make_config(tmp_path), make_client(long_setup_fixtures, tmp_path))

    (sig,) = swing(snapshot)["signals"]
    assert sig["action"] == "long"
    tick = 0.1  # BTC (szDecimals 5) quotes one price decimal
    for level in (sig["entry"], sig["stop"]):
        assert level / tick == pytest.approx(round(level / tick), abs=1e-6)
    ps = sig["position_size"]
    assert ps["risk_per_unit"] == pytest.approx(abs(sig["entry"] - sig["stop"]))
    assert ps["size"] == position_size(10_000.0, sig["entry"], sig["stop"], 0.01, 5).size


def test_size_warnings_flag_sizes_the_exchange_would_refuse():
    # The Iron Triangle sizes on risk alone; Hyperliquid adds two limits. Flag a
    # notional above the perp's max leverage x equity, or under the $10 minimum.
    gold = PerpSpec(sz_decimals=4, max_leverage=25)
    assert size_warnings(1.0, 4_000.0, 10_000.0, gold) == []  # $4,000 = 0.4x equity

    (over,) = size_warnings(100.0, 4_000.0, 10_000.0, gold)  # $400,000 = 40x equity
    assert "40.0× equity" in over and "25× max leverage" in over

    isolated = PerpSpec(sz_decimals=3, max_leverage=50, only_isolated=True)
    (over,) = size_warnings(100.0, 6_500.0, 10_000.0, isolated)  # 65x
    assert "50× max leverage" in over and "isolated margin only" in over

    (tiny,) = size_warnings(0.002, 4_000.0, 10_000.0, gold)  # $8
    assert "$10 minimum" in tiny
    # Unknown leverage cap: only the minimum order value can be checked.
    assert size_warnings(100.0, 4_000.0, 10_000.0, None) == []


def test_unexecutable_size_is_flagged_never_capped(tmp_path, long_setup_fixtures, capsys):
    # A $40 account: 1% risk buys a few hundredths of a unit at ~142, under
    # Hyperliquid's $10 minimum order value. The flag is information: the action
    # and the Iron-Triangle size stay exactly what Elder's rules give.
    cfg = make_config(tmp_path, equity=40.0, equity_at_month_start=40.0)
    snapshot = build_snapshot(cfg, make_client(long_setup_fixtures, tmp_path))

    (sig,) = swing(snapshot)["signals"]
    assert sig["action"] == "long"
    ps = sig["position_size"]
    assert ps["size"] == position_size(40.0, sig["entry"], sig["stop"], 0.01, 5).size
    assert 0 < ps["size"] * sig["entry"] < 10.0
    (warning,) = sig["size_warnings"]
    assert "$10 minimum" in warning

    from run import print_signals_tables

    print_signals_tables(snapshot)
    assert f"! size: {warning}" in capsys.readouterr().out


def test_guard_uses_automatic_open_position_risk(tmp_path, btc_fixtures):
    addr = "0x" + "ef" * 20
    cfg = make_config(tmp_path, address=addr, month_realized_losses=0.0)
    state = make_clearinghouse_state([{"coin": "BTC", "szi": "10.0", "entryPx": "50000.0"}])
    client = make_client(btc_fixtures, tmp_path, clearinghouse_states={addr: state})
    snapshot = build_snapshot(cfg, client)

    (pos,) = snapshot["positions"]
    assert snapshot["auto_open_trade_risk"] == pos["open_risk"]
    assert snapshot["total_open_trade_risk"] == snapshot["auto_open_trade_risk"]
    assert snapshot["guard"]["total_at_risk"] == snapshot["auto_open_trade_risk"]


def test_strategy_thresholds_are_configurable(tmp_path, btc_fixtures):
    cfg = make_config(tmp_path, strategy=StrategyParams(flat_trend_slope_pct=999.0))
    cfg = replace(
        cfg,
        scanner=replace(
            cfg.scanner,
            horizons=(replace(SWING, params=StrategyParams(flat_trend_slope_pct=999.0)),),
        ),
    )
    client = make_client(btc_fixtures, tmp_path)
    snapshot = build_snapshot(cfg, client)
    assert swing(snapshot)["signals"][0]["tide_trend"] == "neutral"


def test_per_horizon_strategy_overrides_are_independent(tmp_path, chain_fixtures):
    # Same bars, two horizons, different thresholds: only the overridden one flips.
    flat = replace(SCALP, params=StrategyParams(flat_trend_slope_pct=999.0))
    cfg = make_config(tmp_path, horizons=(SWING, flat))
    client = make_client(chain_fixtures, tmp_path)
    snapshot = build_snapshot(cfg, client)

    assert snapshot["horizons"]["scalp"]["signals"][0]["tide_trend"] == "neutral"
    assert snapshot["horizons"]["swing"]["signals"][0]["tide_trend"] != "neutral"


def test_journal_appends_compact_scan_entry(tmp_path, chain_fixtures):
    cfg = make_config(tmp_path, horizons=(SWING, SCALP))
    client = make_client(chain_fixtures, tmp_path)
    snapshot = build_snapshot(cfg, client)
    journal = tmp_path / "journal.jsonl"

    append_journal_entry(snapshot, journal)

    entry = json.loads(journal.read_text().splitlines()[0])
    assert entry["generated_at"] == snapshot["generated_at"]
    assert set(entry["top_picks"]) == {"swing", "scalp"}
    # Every signal is tagged with the horizon it came from.
    assert {s["horizon"] for s in entry["signals"]} == {"swing", "scalp"}
    assert all("reason" in s for s in entry["signals"])


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
    assert api.json()["horizons"]["swing"]["signals"][0]["asset"] == "BTC"

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
