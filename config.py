"""Configuration loading: config.toml + optional .env overrides.

Secrets never live in the repo; equity / risk_pct may be overridden via
environment variables (see .env.example).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_CONFIG_PATH = Path("config.toml")

# Watchlist wildcard: "*" scans every tradable native perp; "<dex>:*"
# (e.g. "xyz:*", the tradfi dex) scans a whole HIP-3 builder-dex universe.
WATCHLIST_ALL = "*"


@dataclass(frozen=True)
class ScannerConfig:
    # Explicit coin names and/or wildcards ("*", "xyz:*") — see WATCHLIST_ALL.
    watchlist: tuple[str, ...]
    weekly_interval: str
    daily_interval: str
    third_screen_interval: str
    use_third_screen: bool
    lookback_weeks: int
    lookback_days: int
    lookback_third_screen: int


@dataclass(frozen=True)
class StrategyConfig:
    flat_trend_slope_pct: float = 0.001
    penetration_lookback_days: int = 35
    force_index_extreme_lookback_days: int = 25
    channel_lookback_weeks: int = 26
    min_reward_risk: float = 2.0
    divergence_lookback: int = 60
    rr_excellent: float = 3.0
    strong_weekly_slope: float = 0.03
    fi_scale_lookback: int = 20
    value_zone_max_distance_pct: float = 0.03
    safezone_lookback_days: int = 20
    safezone_factor: float = 2.0
    entry_order_expire_days: int = 2
    score_reward_risk_weight: float = 0.40
    score_impulse_weight: float = 0.25
    score_tide_weight: float = 0.20
    score_pullback_weight: float = 0.15


@dataclass(frozen=True)
class RiskConfig:
    equity: float
    risk_pct: float
    equity_at_month_start: float
    month_realized_losses: float
    open_trade_risk: float


@dataclass(frozen=True)
class PositionsConfig:
    # Public wallet address used to read OPEN positions from Hyperliquid's public
    # clearinghouseState info endpoint. Read-only — no private key, no signing.
    # Empty string disables open-trade management.
    address: str


@dataclass(frozen=True)
class JournalConfig:
    enabled: bool
    path: Path


@dataclass(frozen=True)
class Config:
    scanner: ScannerConfig
    strategy: StrategyConfig
    risk: RiskConfig
    positions: PositionsConfig
    journal: JournalConfig
    cache_dir: Path


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    load_dotenv()
    raw = tomllib.loads(path.read_text())

    s = raw["scanner"]
    st = raw.get("strategy", {})
    r = raw["risk"]
    p = raw.get("positions", {})
    j = raw.get("journal", {})

    equity = float(os.environ.get("EQUITY", r["equity"]))
    risk_pct = float(os.environ.get("RISK_PCT", r["risk_pct"]))
    # A public address is not a secret, but allow an env override so it need not
    # be committed (see .env.example).
    address = str(os.environ.get("HL_ADDRESS", p.get("address", ""))).strip()

    return Config(
        scanner=ScannerConfig(
            watchlist=tuple(s["watchlist"]),
            weekly_interval=s.get("weekly_interval", "1w"),
            daily_interval=s.get("daily_interval", "1d"),
            third_screen_interval=s.get("third_screen_interval", "4h"),
            use_third_screen=bool(s.get("use_third_screen", False)),
            lookback_weeks=int(s.get("lookback_weeks", 260)),
            lookback_days=int(s.get("lookback_days", 500)),
            lookback_third_screen=int(s.get("lookback_third_screen", 300)),
        ),
        strategy=StrategyConfig(
            flat_trend_slope_pct=float(st.get("flat_trend_slope_pct", 0.001)),
            penetration_lookback_days=int(st.get("penetration_lookback_days", 35)),
            force_index_extreme_lookback_days=int(st.get("force_index_extreme_lookback_days", 25)),
            channel_lookback_weeks=int(st.get("channel_lookback_weeks", 26)),
            min_reward_risk=float(st.get("min_reward_risk", 2.0)),
            divergence_lookback=int(st.get("divergence_lookback", 60)),
            rr_excellent=float(st.get("rr_excellent", 3.0)),
            strong_weekly_slope=float(st.get("strong_weekly_slope", 0.03)),
            fi_scale_lookback=int(st.get("fi_scale_lookback", 20)),
            value_zone_max_distance_pct=float(st.get("value_zone_max_distance_pct", 0.03)),
            safezone_lookback_days=int(st.get("safezone_lookback_days", 20)),
            safezone_factor=float(st.get("safezone_factor", 2.0)),
            entry_order_expire_days=int(st.get("entry_order_expire_days", 2)),
            score_reward_risk_weight=float(st.get("score_reward_risk_weight", 0.40)),
            score_impulse_weight=float(st.get("score_impulse_weight", 0.25)),
            score_tide_weight=float(st.get("score_tide_weight", 0.20)),
            score_pullback_weight=float(st.get("score_pullback_weight", 0.15)),
        ),
        risk=RiskConfig(
            equity=equity,
            risk_pct=risk_pct,
            equity_at_month_start=float(r.get("equity_at_month_start", equity)),
            month_realized_losses=float(r.get("month_realized_losses", 0.0)),
            open_trade_risk=float(r.get("open_trade_risk", 0.0)),
        ),
        positions=PositionsConfig(address=address),
        journal=JournalConfig(
            enabled=bool(j.get("enabled", True)),
            path=Path(j.get("path", "cache/trading_journal.jsonl")),
        ),
        cache_dir=Path(raw.get("cache", {}).get("dir", "cache")),
    )
