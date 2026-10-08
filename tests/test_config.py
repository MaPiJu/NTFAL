"""config.toml parsing: horizons, strategy overrides, manual positions."""

from __future__ import annotations

import math

import pytest

from config import ConfigError, ManualPosition, load_config
from data.hyperliquid import INTERVAL_MS
from data.sessions import WeekendClosure

BASE = """
[scanner]
watchlist = ["xyz:GOLD"]
positions_horizon = "swing"

[[scanner.horizons]]
name = "swing"
label = "Swing"
tide = "1w"
wave = "1d"
entry = "4h"
refresh_seconds = 86400

[[scanner.horizons]]
name = "micro"
tide = "1h"
wave = "15m"
entry = "5m"
refresh_seconds = 300
holding_hours = 4
min_tide_bars = 90

[risk]
equity = 1000.0
risk_pct = 0.01
"""


def write(tmp_path, body: str):
    path = tmp_path / "config.toml"
    path.write_text(body)
    return path


def test_horizons_are_parsed_in_order(tmp_path):
    cfg = load_config(write(tmp_path, BASE))

    assert [h.name for h in cfg.scanner.horizons] == ["swing", "micro"]
    swing, micro = cfg.scanner.horizons
    assert (swing.tide, swing.wave, swing.entry) == ("1w", "1d", "4h")
    assert (micro.tide, micro.wave, micro.entry) == ("1h", "15m", "5m")
    assert micro.refresh_seconds == 300
    assert micro.min_tide_bars == 90
    assert swing.min_tide_bars == 60  # default
    assert micro.holding_hours == 4.0
    assert swing.holding_hours is None  # no funding-cost estimate unless configured
    assert micro.label == "micro"  # falls back to the name
    assert cfg.scanner.horizon("micro") is micro
    assert swing.intervals == {"tide": "1w", "wave": "1d", "entry": "4h"}


def test_unknown_horizon_lookup_raises(tmp_path):
    cfg = load_config(write(tmp_path, BASE))
    with pytest.raises(KeyError, match="nope"):
        cfg.scanner.horizon("nope")


def test_rejects_an_interval_hyperliquid_does_not_serve(tmp_path):
    body = BASE.replace('tide = "1h"', 'tide = "7m"')
    with pytest.raises(ConfigError, match="7m"):
        load_config(write(tmp_path, body))


def test_rejects_duplicate_horizon_names(tmp_path):
    body = BASE.replace('name = "micro"', 'name = "swing"')
    with pytest.raises(ConfigError, match="duplicate horizon"):
        load_config(write(tmp_path, body))


def test_rejects_a_positions_horizon_that_does_not_exist(tmp_path):
    body = BASE.replace('positions_horizon = "swing"', 'positions_horizon = "daytrade"')
    with pytest.raises(ConfigError, match="daytrade"):
        load_config(write(tmp_path, body))


def test_requires_at_least_one_horizon(tmp_path):
    body = """
[scanner]
watchlist = []
[risk]
equity = 1000.0
risk_pct = 0.01
"""
    with pytest.raises(ConfigError, match="at least one"):
        load_config(write(tmp_path, body))


def test_positions_horizon_defaults_to_the_first(tmp_path):
    body = BASE.replace('positions_horizon = "swing"\n', "")
    cfg = load_config(write(tmp_path, body))
    assert cfg.scanner.positions_horizon == "swing"


def test_horizon_inherits_then_overrides_global_strategy(tmp_path):
    body = BASE + """
[strategy]
min_reward_risk = 2.5
safezone_factor_short = 4.0
"""
    # Give only the micro horizon a tighter R:R floor.
    micro_override = "\n[scanner.horizons.strategy]\nmin_reward_risk = 1.5"
    body = body.replace("min_tide_bars = 90", "min_tide_bars = 90" + micro_override)
    cfg = load_config(write(tmp_path, body))
    swing, micro = cfg.scanner.horizons

    assert cfg.strategy.min_reward_risk == 2.5
    assert swing.params.min_reward_risk == 2.5  # inherits the global block
    assert micro.params.min_reward_risk == 1.5  # its own override
    assert micro.params.safezone_factor_short == 4.0  # still inherits the rest


def test_parses_a_weekend_closure_per_dex(tmp_path):
    body = BASE + '\n[sessions.xyz]\nweekend_close = "Fri 21:00"\nweekend_open = "Sun 22:00"\n'
    cfg = load_config(write(tmp_path, body))
    assert cfg.sessions == {"xyz": WeekendClosure.parse("Fri 21:00", "Sun 22:00")}  # UTC
    zoned = body.replace("[sessions.xyz]", '[sessions.xyz]\ntimezone = "America/New_York"')
    assert load_config(write(tmp_path, zoned)).sessions == {
        "xyz": WeekendClosure.parse("Fri 21:00", "Sun 22:00", "America/New_York")
    }
    assert load_config(write(tmp_path, BASE)).sessions == {}  # 24/7 unless configured

    bad = BASE + '\n[sessions.xyz]\nweekend_close = "Friday"\nweekend_open = "Sun 22:00"\n'
    with pytest.raises(ConfigError, match="sessions.xyz"):
        load_config(write(tmp_path, bad))


def test_rejects_an_unknown_strategy_key(tmp_path):
    body = BASE + "\n[strategy]\nmagic_indicator = 3\n"
    with pytest.raises(ConfigError, match="magic_indicator"):
        load_config(write(tmp_path, body))


def test_parses_manual_positions_and_normalizes_them(tmp_path):
    body = BASE + """
[positions]
address = ""
[[positions.manual]]
asset = "xyz:GOLD"
side = "LONG"
size = -1.5
entry = 4300.0
"""
    cfg = load_config(write(tmp_path, body))
    assert cfg.positions.manual == (
        ManualPosition(asset="xyz:GOLD", side="long", size=1.5, entry=4300.0),
    )


def test_rejects_a_bad_manual_side(tmp_path):
    body = BASE + """
[[positions.manual]]
asset = "xyz:GOLD"
side = "hodl"
size = 1.0
entry = 1.0
"""
    with pytest.raises(ConfigError, match="hodl"):
        load_config(write(tmp_path, body))


def test_env_overrides_equity_risk_and_address(tmp_path, monkeypatch):
    monkeypatch.setenv("EQUITY", "25000")
    monkeypatch.setenv("RISK_PCT", "0.015")
    monkeypatch.setenv("HL_ADDRESS", "0x" + "ab" * 20)
    cfg = load_config(write(tmp_path, BASE))

    assert cfg.risk.equity == 25_000.0
    assert cfg.risk.risk_pct == 0.015
    assert cfg.positions.address == "0x" + "ab" * 20
    # equity_at_month_start falls back to the (overridden) equity
    assert cfg.risk.equity_at_month_start == 25_000.0


def test_shipped_config_is_valid_and_hyperliquid_only():
    """The config.toml in the repo must load and describe the intended setup."""
    cfg = load_config()

    assert cfg.scanner.watchlist == (
        "xyz:GOLD",
        "xyz:SILVER",
        "xyz:CL",
        "xyz:BRENTOIL",
        "xyz:SP500",
        "xyz:XYZ100",
    )
    assert [h.name for h in cfg.scanner.horizons] == ["swing", "scalp", "micro"]
    # Elder's factor of ~5 holds inside each chain, coarsest first.
    chains = [(h.tide, h.wave, h.entry) for h in cfg.scanner.horizons]
    assert chains == [("1w", "1d", "4h"), ("4h", "1h", "15m"), ("1h", "15m", "5m")]
    # Shorter horizons must refresh more often than longer ones.
    seconds = [h.refresh_seconds for h in cfg.scanner.horizons]
    assert seconds == sorted(seconds, reverse=True)
    assert cfg.scanner.positions_horizon == "swing"
    # Elder's 2:1 floor is the shipped default.
    assert cfg.strategy.min_reward_risk == 2.0
    # The xyz dex's tradfi markets close Friday 17:00 -> Sunday 18:00 New York
    # time (trade.xyz's external-price schedule): 21:00 -> 22:00 UTC in summer.
    assert cfg.sessions == {
        "xyz": WeekendClosure.parse("Fri 17:00", "Sun 18:00", "America/New_York")
    }
    # Funding is estimated over a typical holding time per horizon.
    assert [h.holding_hours for h in cfg.scanner.horizons] == [14 * 24, 2 * 24, 4]


def test_shipped_flat_tide_threshold_scales_with_the_tide_interval():
    # A "flat" tide is an EMA13 slope lost in the noise, and noise grows like the
    # square root of time: one threshold in %/bar cannot fit a weekly and an hourly
    # tide (at 0.1%/bar the 1h tide of xyz:SP500 read "neutral" 99% of the time).
    # Each horizon scales the swing (1w) threshold by sqrt(tide bar / 1 week).
    cfg = load_config()
    swing = cfg.scanner.horizon("swing")
    base = swing.params.flat_trend_slope_pct
    assert swing.tide == "1w"

    for h in cfg.scanner.horizons:
        expected = base * math.sqrt(INTERVAL_MS[h.tide] / INTERVAL_MS["1w"])
        assert h.params.flat_trend_slope_pct == pytest.approx(expected, rel=0.05), h.name
