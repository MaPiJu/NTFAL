"""Configuration loading: config.toml + optional .env overrides.

Secrets never live in the repo; equity / risk_pct may be overridden via
environment variables (see .env.example).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from data.hyperliquid import INTERVAL_MS
from data.sessions import WeekendClosure
from strategy.params import StrategyParams

DEFAULT_CONFIG_PATH = Path("config.toml")

# Watchlist wildcard: "*" scans every tradable native perp; "<dex>:*"
# (e.g. "xyz:*", the tradfi dex) scans a whole HIP-3 builder-dex universe.
WATCHLIST_ALL = "*"


@dataclass(frozen=True)
class HorizonConfig:
    """One Triple Screen timeframe chain (Elder's "factor of ~5" applied to a role).

    The three screens are named by *role*, not by a fixed interval, so the same
    Elder logic runs on a weekly tide or a 4h tide:
      - `tide`  = first screen, the strategic bias
      - `wave`  = second screen, the counter-trend oscillator that finds entries
      - `entry` = third screen, lower-timeframe entry timing

    `refresh_seconds` is how often this horizon is worth recomputing (a weekly
    tide does not change every five minutes); `min_tide_bars` is the history
    depth below which the slow EMA26 is not converged and the tide/Impulse are
    flagged as unreliable — surfaced, never silently trusted. `holding_hours` is
    a typical holding time for a trade on this chain, over which the current
    funding rate is turned into an estimated cost (None: no estimate).
    """

    name: str
    label: str
    tide: str
    wave: str
    entry: str
    lookback_tide: int
    lookback_wave: int
    lookback_entry: int
    refresh_seconds: int
    min_tide_bars: int
    holding_hours: float | None = None
    # Per-horizon strategy overrides merged over the global [strategy] block.
    params: StrategyParams = StrategyParams()

    @property
    def intervals(self) -> dict[str, str]:
        return {"tide": self.tide, "wave": self.wave, "entry": self.entry}


@dataclass(frozen=True)
class ScannerConfig:
    # Explicit coin names and/or wildcards ("*", "xyz:*") — see WATCHLIST_ALL.
    watchlist: tuple[str, ...]
    horizons: tuple[HorizonConfig, ...]
    # Horizon whose bars drive Elder trade management for OPEN positions.
    positions_horizon: str

    def horizon(self, name: str) -> HorizonConfig:
        for h in self.horizons:
            if h.name == name:
                return h
        raise KeyError(f"unknown horizon: {name}")


@dataclass(frozen=True)
class RiskConfig:
    equity: float
    risk_pct: float
    equity_at_month_start: float
    month_realized_losses: float
    open_trade_risk: float


@dataclass(frozen=True)
class ManualPosition:
    # An open position declared by hand — for a trade the configured public
    # address cannot see. The asset uses watchlist naming, e.g. "xyz:GOLD".
    asset: str
    side: str  # "long" | "short"
    size: float  # absolute units of the asset (always positive)
    entry: float


@dataclass(frozen=True)
class PositionsConfig:
    # Public wallet address used to read OPEN positions from Hyperliquid's public
    # clearinghouseState info endpoint. Read-only — no private key, no signing.
    # Empty string disables open-trade management.
    address: str
    # Manually declared open positions, merged with the ones read from
    # Hyperliquid for the same Elder exit analysis.
    manual: tuple[ManualPosition, ...] = ()


@dataclass(frozen=True)
class JournalConfig:
    enabled: bool
    path: Path


@dataclass(frozen=True)
class Config:
    scanner: ScannerConfig
    strategy: StrategyParams
    risk: RiskConfig
    positions: PositionsConfig
    journal: JournalConfig
    cache_dir: Path
    # dex -> weekend closure of its markets ([sessions.<dex>]); a dex without
    # one (the native crypto perps) trades 24/7.
    sessions: dict[str, WeekendClosure] = field(default_factory=dict)


class ConfigError(ValueError):
    """Raised when config.toml is structurally invalid."""


def _strategy_params(raw: dict[str, Any], base: StrategyParams) -> StrategyParams:
    """`base` with any keys present in `raw` overridden (unknown keys rejected)."""
    known = {f: getattr(base, f) for f in base.__dataclass_fields__}
    unknown = set(raw) - set(known)
    if unknown:
        raise ConfigError(f"unknown strategy setting(s): {', '.join(sorted(unknown))}")
    overrides = {k: type(known[k])(v) for k, v in raw.items()}
    return replace(base, **overrides)


def _horizon(raw: dict[str, Any], base: StrategyParams) -> HorizonConfig:
    try:
        name = str(raw["name"])
        tide, wave, entry = str(raw["tide"]), str(raw["wave"]), str(raw["entry"])
    except KeyError as exc:  # pragma: no cover - defensive
        raise ConfigError(f"horizon is missing required key {exc}") from exc

    for role, interval in (("tide", tide), ("wave", wave), ("entry", entry)):
        if interval not in INTERVAL_MS:
            raise ConfigError(
                f"horizon {name!r}: {role} interval {interval!r} is not a Hyperliquid "
                f"candle interval ({', '.join(INTERVAL_MS)})"
            )

    return HorizonConfig(
        name=name,
        label=str(raw.get("label", name)),
        tide=tide,
        wave=wave,
        entry=entry,
        lookback_tide=int(raw.get("lookback_tide", 260)),
        lookback_wave=int(raw.get("lookback_wave", 500)),
        lookback_entry=int(raw.get("lookback_entry", 300)),
        refresh_seconds=int(raw.get("refresh_seconds", 86_400)),
        min_tide_bars=int(raw.get("min_tide_bars", 60)),
        holding_hours=float(raw["holding_hours"]) if "holding_hours" in raw else None,
        params=_strategy_params(raw.get("strategy", {}), base),
    )


def _horizons(raw_list: list[dict[str, Any]], base: StrategyParams) -> tuple[HorizonConfig, ...]:
    if not raw_list:
        raise ConfigError("at least one [[scanner.horizons]] block is required")
    horizons = tuple(_horizon(h, base) for h in raw_list)
    names = [h.name for h in horizons]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise ConfigError(f"duplicate horizon name(s): {', '.join(sorted(duplicates))}")
    return horizons


def _sessions(raw: dict[str, Any]) -> dict[str, WeekendClosure]:
    out: dict[str, WeekendClosure] = {}
    for dex, table in raw.items():
        try:
            out[dex] = WeekendClosure.parse(
                table["weekend_close"], table["weekend_open"], table.get("timezone", "UTC")
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"[sessions.{dex}]: {exc}") from exc
    return out


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    load_dotenv()
    raw = tomllib.loads(path.read_text())

    s = raw["scanner"]
    r = raw["risk"]
    p = raw.get("positions", {})
    j = raw.get("journal", {})

    strategy = _strategy_params(raw.get("strategy", {}), StrategyParams())
    horizons = _horizons(s.get("horizons", []), strategy)

    positions_horizon = str(s.get("positions_horizon", horizons[0].name))
    if positions_horizon not in {h.name for h in horizons}:
        raise ConfigError(
            f"positions_horizon {positions_horizon!r} is not a defined horizon "
            f"({', '.join(h.name for h in horizons)})"
        )

    equity = float(os.environ.get("EQUITY", r["equity"]))
    risk_pct = float(os.environ.get("RISK_PCT", r["risk_pct"]))
    # A public address is not a secret, but allow an env override so it need not
    # be committed (see .env.example).
    address = str(os.environ.get("HL_ADDRESS", p.get("address", ""))).strip()

    manual = []
    for m in p.get("manual", []):
        side = str(m["side"]).lower()
        if side not in ("long", "short"):
            raise ConfigError(f"positions.manual side must be long|short, got {m['side']!r}")
        manual.append(
            ManualPosition(
                asset=str(m["asset"]),
                side=side,
                size=abs(float(m["size"])),
                entry=float(m["entry"]),
            )
        )

    return Config(
        scanner=ScannerConfig(
            watchlist=tuple(s["watchlist"]),
            horizons=horizons,
            positions_horizon=positions_horizon,
        ),
        strategy=strategy,
        risk=RiskConfig(
            equity=equity,
            risk_pct=risk_pct,
            equity_at_month_start=float(r.get("equity_at_month_start", equity)),
            month_realized_losses=float(r.get("month_realized_losses", 0.0)),
            open_trade_risk=float(r.get("open_trade_risk", 0.0)),
        ),
        positions=PositionsConfig(address=address, manual=tuple(manual)),
        journal=JournalConfig(
            enabled=bool(j.get("enabled", True)),
            path=Path(j.get("path", "cache/trading_journal.jsonl")),
        ),
        cache_dir=Path(raw.get("cache", {}).get("dir", "cache")),
        sessions=_sessions(raw.get("sessions", {})),
    )
