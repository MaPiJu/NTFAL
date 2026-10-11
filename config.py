"""Configuration loading: config.toml + optional .env overrides.

Secrets never live in the repo or in config.toml: the execution layer's API
wallet key and the Telegram bot token are read from the environment (.env) by
bot.py, never stored on a Config. equity / risk_pct may be overridden via
environment variables too (see .env.example).
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from data.hyperliquid import INTERVAL_MS
from data.sessions import WeekendClosure
from strategy.params import StrategyParams
from strategy.triple_screen import LOW_VOLUME, MARKET_CLOSED, SHORT_HISTORY

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


EXECUTION_MODES = ("dry_run", "testnet", "mainnet")
DATA_FLAGS = (SHORT_HISTORY, LOW_VOLUME, MARKET_CLOSED)


@dataclass(frozen=True)
class ExecutionConfig:
    """The opt-in order layer ([execution]); see execution/ and CLAUDE.md.

    `mode`: "dry_run" computes and logs every order but sends none; "testnet"
    trades Hyperliquid's testnet (its data and its orders); "mainnet" sends real
    orders. Nothing runs unless `enabled` is true and bot.py is started.
    """

    enabled: bool = False
    mode: str = "dry_run"
    # The one horizon the bot trades. It must be the positions horizon, so a
    # position is managed on the chain that opened it.
    horizon: str = ""
    # Stop-limit entry: the limit sits this far beyond the trigger, capping the
    # fill; the size is computed from that worst fill, so the 2% Rule holds.
    entry_slippage_pct: float = 0.005
    # Market exits (EXIT verdict, take-profits): an IOC limit this far through the mark.
    exit_slippage_pct: float = 0.01
    # Protective stop (a stop-market trigger): the limit bounding its fill.
    stop_slippage_pct: float = 0.10
    # Share of the position closed on the first TAKE_PROFITS verdict.
    take_profit_fraction: float = 0.5
    # Leverage is chosen per trade so that liquidation sits at least twice the
    # stop distance away, capped here and by the perp's maxLeverage.
    max_leverage: int = 10
    margin: str = "isolated"  # or "cross" (isolated-only perps stay isolated)
    # How often the protective stops are checked (seconds).
    protect_seconds: int = 60
    # Data-quality flags that block a NEW entry (never an exit).
    block_on_flags: tuple[str, ...] = (LOW_VOLUME, MARKET_CLOSED)
    # Read equity, month-start equity and the month's realized losses from the
    # account instead of [risk].
    auto_equity: bool = True
    # While this file exists: no new entry, pending entries cancelled; open
    # positions keep their stops and their exits.
    halt_file: Path = Path("cache/HALT")


@dataclass(frozen=True)
class TelegramConfig:
    enabled: bool = False
    report_time: str = "21:15"  # HH:MM, daily, in `timezone`
    timezone: str = "Europe/Paris"
    # Instant message on fills, closes and errors, besides the daily report.
    alerts: bool = True


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
    execution: ExecutionConfig = ExecutionConfig()
    telegram: TelegramConfig = TelegramConfig()


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


def _execution(raw: dict[str, Any], positions_horizon: str) -> ExecutionConfig:
    base = ExecutionConfig()
    mode = str(raw.get("mode", base.mode))
    if mode not in EXECUTION_MODES:
        raise ConfigError(f"[execution] mode must be one of {', '.join(EXECUTION_MODES)}")
    horizon = str(raw.get("horizon", positions_horizon))
    if horizon != positions_horizon:
        raise ConfigError(
            f"[execution] horizon {horizon!r} must be scanner.positions_horizon "
            f"({positions_horizon!r}): a position is managed on the chain that opened it"
        )
    margin = str(raw.get("margin", base.margin))
    if margin not in ("isolated", "cross"):
        raise ConfigError("[execution] margin must be 'isolated' or 'cross'")
    flags = tuple(str(f) for f in raw.get("block_on_flags", base.block_on_flags))
    unknown = set(flags) - set(DATA_FLAGS)
    if unknown:
        raise ConfigError(
            f"[execution] unknown block_on_flags: {', '.join(sorted(unknown))} "
            f"(known: {', '.join(DATA_FLAGS)})"
        )
    out = ExecutionConfig(
        enabled=bool(raw.get("enabled", base.enabled)),
        mode=mode,
        horizon=horizon,
        entry_slippage_pct=float(raw.get("entry_slippage_pct", base.entry_slippage_pct)),
        exit_slippage_pct=float(raw.get("exit_slippage_pct", base.exit_slippage_pct)),
        stop_slippage_pct=float(raw.get("stop_slippage_pct", base.stop_slippage_pct)),
        take_profit_fraction=float(raw.get("take_profit_fraction", base.take_profit_fraction)),
        max_leverage=int(raw.get("max_leverage", base.max_leverage)),
        margin=margin,
        protect_seconds=int(raw.get("protect_seconds", base.protect_seconds)),
        block_on_flags=flags,
        auto_equity=bool(raw.get("auto_equity", base.auto_equity)),
        halt_file=Path(raw.get("halt_file", base.halt_file)),
    )
    for key in ("entry_slippage_pct", "exit_slippage_pct", "stop_slippage_pct"):
        if not 0 < getattr(out, key) <= 0.2:
            raise ConfigError(f"[execution] {key} must be in (0, 0.2]")
    if not 0 < out.take_profit_fraction <= 1:
        raise ConfigError("[execution] take_profit_fraction must be in (0, 1]")
    if out.max_leverage < 1:
        raise ConfigError("[execution] max_leverage must be at least 1")
    if out.protect_seconds < 10:
        raise ConfigError("[execution] protect_seconds must be at least 10")
    return out


def _telegram(raw: dict[str, Any]) -> TelegramConfig:
    base = TelegramConfig()
    out = TelegramConfig(
        enabled=bool(raw.get("enabled", base.enabled)),
        report_time=str(raw.get("report_time", base.report_time)),
        timezone=str(raw.get("timezone", base.timezone)),
        alerts=bool(raw.get("alerts", base.alerts)),
    )
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", out.report_time)
    if match is None or int(match[1]) > 23 or int(match[2]) > 59:
        raise ConfigError(f"[telegram] report_time must be HH:MM, got {out.report_time!r}")
    try:
        ZoneInfo(out.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"[telegram] unknown timezone {out.timezone!r}") from exc
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
        execution=_execution(raw.get("execution", {}), positions_horizon),
        telegram=_telegram(raw.get("telegram", {})),
    )
