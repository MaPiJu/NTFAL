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
    lookback_weeks: int
    lookback_days: int


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
class Config:
    scanner: ScannerConfig
    risk: RiskConfig
    positions: PositionsConfig
    cache_dir: Path


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    load_dotenv()
    raw = tomllib.loads(path.read_text())

    s = raw["scanner"]
    r = raw["risk"]
    p = raw.get("positions", {})

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
            lookback_weeks=int(s.get("lookback_weeks", 260)),
            lookback_days=int(s.get("lookback_days", 500)),
        ),
        risk=RiskConfig(
            equity=equity,
            risk_pct=risk_pct,
            equity_at_month_start=float(r.get("equity_at_month_start", equity)),
            month_realized_losses=float(r.get("month_realized_losses", 0.0)),
            open_trade_risk=float(r.get("open_trade_risk", 0.0)),
        ),
        positions=PositionsConfig(address=address),
        cache_dir=Path(raw.get("cache", {}).get("dir", "cache")),
    )
