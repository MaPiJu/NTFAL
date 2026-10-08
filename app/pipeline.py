"""Build the dashboard snapshot: refresh candles, compute signals + chart data.

The snapshot is a single JSON document written by run.py and read by the
FastAPI app — the server itself never talks to the exchange.

One scan produces one block per **horizon** (timeframe chain). Horizons refresh
on their own cadence — a weekly tide does not move every five minutes — so
`refresh_horizon` rebuilds a single block and merges it into the existing
snapshot, leaving the others untouched.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

from config import WATCHLIST_ALL, Config, HorizonConfig
from data.hyperliquid import (
    MIN_ORDER_VALUE_USD,
    HyperliquidError,
    PerpSpec,
    coin_dex,
    completed_bars,
)
from data.provider import MarketDataProvider
from data.sessions import WeekendClosure, drop_closed_bars, weekly_from_weekdays
from indicators import ema, force_index, impulse_color, macd_histogram
from risk.sizing import MonthlyGuard, position_size, six_percent_guard
from strategy.trade_management import OpenPosition, assess_position, parse_positions
from strategy.triple_screen import EMA_FAST, EMA_SLOW, Signal, evaluate_asset, select_best

SNAPSHOT_FILENAME = "snapshot.json"

IMPULSE_HEX = {"green": "#089981", "red": "#f23645", "blue": "#2962ff"}
HIST_UP_HEX = "#089981"
HIST_DOWN_HEX = "#f23645"

# Indicators are computed on the FULL history (so the EMAs are converged), but
# only this many trailing bars are shipped to the browser. Without the cap an
# intraday horizon would put thousands of bars per chart into the snapshot.
CHART_BARS = 300
# Chart values are rounded before serializing: a raw EMA carries ~16 significant
# digits, which is ~40% of the snapshot's bytes and not a pixel of extra detail
# (Hyperliquid itself quotes 5 significant figures).
CHART_SIG_FIGS = 8


def _round(v: float) -> float:
    return float(f"{v:.{CHART_SIG_FIGS}g}")


def _line_points(times: pd.Series, values: pd.Series, bars: int) -> list[dict[str, Any]]:
    pts = []
    for t, v in zip(times.iloc[-bars:], values.iloc[-bars:], strict=True):
        if pd.notna(v):
            pts.append({"time": int(t) // 1000, "value": _round(float(v))})
    return pts


def chart_payload(df: pd.DataFrame, bars: int = CHART_BARS) -> dict[str, Any]:
    """Per-interval chart data for Lightweight Charts (times in epoch seconds).

    Indicators are computed over all of `df` and only then trimmed to the last
    `bars` points, so a trimmed chart shows the same EMA values as a full one.
    """
    close, volume, t = df["close"], df["volume"], df["t"]
    colors = impulse_color(close)
    hist = macd_histogram(close)

    candles = []
    for i in range(max(0, len(df) - bars), len(df)):
        # Body and wick carry the bar's Impulse color. The border is left to the
        # series (which draws none), since an Impulse-colored border on an
        # Impulse-colored body is invisible anyway.
        hex_color = IMPULSE_HEX[colors.iloc[i]]
        candles.append(
            {
                "time": int(t.iloc[i]) // 1000,
                "open": _round(float(df["open"].iloc[i])),
                "high": _round(float(df["high"].iloc[i])),
                "low": _round(float(df["low"].iloc[i])),
                "close": _round(float(close.iloc[i])),
                "color": hex_color,
                "wickColor": hex_color,
            }
        )

    hist_points = []
    prev = None
    for i, (ts, v) in enumerate(zip(t, hist, strict=True)):
        if pd.isna(v):
            continue
        v = float(v)
        # Elder colors the histogram by slope: rising vs falling.
        up = prev is None or v >= prev
        prev = v
        if i < len(df) - bars:
            continue  # slope needed the earlier bars, but don't ship them
        hist_points.append(
            {
                "time": int(ts) // 1000,
                "value": _round(v),
                "color": HIST_UP_HEX if up else HIST_DOWN_HEX,
            }
        )

    return {
        "candles": candles,
        "ema13": _line_points(t, ema(close, EMA_FAST), bars),
        "ema26": _line_points(t, ema(close, EMA_SLOW), bars),
        "macd_hist": hist_points,
        "force_index_2": _line_points(t, force_index(close, volume, span=2), bars),
        "force_index_13": _line_points(t, force_index(close, volume, span=13), bars),
    }


def live_price_alert(
    action: str,
    live: float | None,
    entry: float | None,
    stop: float | None,
    target: float | None,
) -> str | None:
    """Warn when the live price has already run past the setup's key levels.

    Signals are computed on the last *completed* bar; on a 24/7 market the live
    price (the still-open bar's close) can drift far from it before the next
    refresh. This flags when acting on the stale signal would mean chasing — the
    live price has already cleared the buy-stop/sell-stop entry — or when the
    setup is already spent (live beyond the target) or void (live beyond the
    stop). Returns None for non-actionable rows or when there is nothing to warn.
    """
    if action not in ("long", "short") or live is None:
        return None
    if action == "long":
        if target is not None and live >= target:
            return "target already reached — setup spent, do not enter"
        if stop is not None and live <= stop:
            return "live price below the stop — setup void"
        if entry is not None and live >= entry:
            return "entry already triggered live — don't chase, wait for a pullback"
    else:
        if target is not None and live <= target:
            return "target already reached — setup spent, do not enter"
        if stop is not None and live >= stop:
            return "live price above the stop — setup void"
        if entry is not None and live <= entry:
            return "entry already triggered live — don't chase, wait for a pullback"
    return None


def size_warnings(size: float, entry: float, equity: float, spec: PerpSpec | None) -> list[str]:
    """Why a suggested size may not be placeable as is — flagged, never capped.

    The Iron Triangle sizes on risk alone; Hyperliquid adds two limits: the
    notional (size x entry) can't exceed the perp's max leverage x equity, and an
    order under $10 is refused. Like the data-quality flags, these never change
    the action or the size: the operator decides.
    """
    out: list[str] = []
    notional = size * entry
    if spec is not None and spec.max_leverage and notional > spec.max_leverage * equity:
        margin = ", isolated margin only" if spec.only_isolated else ""
        out.append(
            f"size × entry = ${notional:,.0f} is {notional / equity:.1f}× equity, above "
            f"the {spec.max_leverage}× max leverage{margin} — not placeable at this size"
        )
    if notional < MIN_ORDER_VALUE_USD:
        out.append(
            f"size × entry = ${notional:,.2f} is under Hyperliquid's "
            f"${MIN_ORDER_VALUE_USD:.0f} minimum order value — not placeable"
        )
    return out


def fetch_funding_rates(
    coins: Mapping[str, PerpSpec], client: MarketDataProvider
) -> dict[str, float]:
    """Current hourly funding rate per watched coin — one `metaAndAssetCtxs`
    request per dex involved.

    Funding is context, never a trading input: a dex whose lookup fails (a bad
    payload, an HTTP error, a timeout) only leaves its coins without a rate.
    """
    rates: dict[str, float] = {}
    for dex in sorted({coin_dex(c) for c in coins}):
        try:
            by_coin = client.funding_rates(dex)
        except (HyperliquidError, httpx.HTTPError):
            continue
        rates.update({c: r for c, r in by_coin.items() if c in coins})
    return rates


def funding_cost(action: str, rate: float | None, hours: float | None) -> float | None:
    """Funding over `hours` as a fraction of notional: positive when the trade
    PAYS, negative when it is paid.

    Hyperliquid's rate is hourly; positive, longs pay shorts. None for a row
    with no side to hold, an unknown rate, or no holding time configured.
    """
    if action not in ("long", "short") or rate is None or not hours:
        return None
    return rate * hours * (1.0 if action == "long" else -1.0)


def _holding_label(hours: float) -> str:
    return f"{hours / 24:g} d" if hours >= 24 and hours % 24 == 0 else f"{hours:g} h"


def funding_warning(
    cost: float | None, entry: float | None, stop: float | None, hours: float | None
) -> str | None:
    """Flag a funding bill above half of what the trade risks, both as a fraction
    of notional (so the size doesn't matter). Informative: never changes the action."""
    if cost is None or cost <= 0 or entry is None or stop is None or entry <= 0 or not hours:
        return None
    risk = abs(entry - stop) / entry
    if cost <= 0.5 * risk:
        return None
    return (
        f"funding over {_holding_label(hours)} ≈ {cost:.2%} of notional, more than half "
        f"the trade's risk ({risk:.2%} from entry to stop) — it eats the edge if held "
        f"that long"
    )


def expand_watchlist(watchlist: tuple[str, ...], client: MarketDataProvider) -> dict[str, PerpSpec]:
    """Resolve watchlist entries to {coin: PerpSpec} (szDecimals, max leverage…),
    sorted by name.

    "*" expands to every tradable native (crypto) perp; "<dex>:*" to every
    tradable perp of a HIP-3 builder dex (e.g. "xyz:*" = the tradfi universe:
    stocks, indices, gold, oil, forex…). Explicit coins are validated.
    """
    specs: dict[str, PerpSpec] = {}
    explicit: list[str] = []
    for item in watchlist:
        if item == WATCHLIST_ALL:
            specs.update(client.tradable_perps())
        elif item.endswith(":" + WATCHLIST_ALL):
            specs.update(client.tradable_perps(item[: -len(":" + WATCHLIST_ALL)]))
        else:
            explicit.append(item)
    if explicit:
        specs.update(client.validate_watchlist(explicit))
    return dict(sorted(specs.items()))


def mask_address(address: str) -> str:
    """Show only the first 6 and last 4 chars of a public address for display."""
    return f"{address[:6]}…{address[-4:]}" if len(address) > 12 else address


def watchlist_dexes(watchlist: tuple[str, ...]) -> list[str]:
    """The perp dexes referenced by the watchlist, '' (native) first.

    `clearinghouseState` is per-dex, so to find every open position we query the
    native clearinghouse plus each HIP-3 builder dex the operator watches
    ("xyz:*" / "xyz:GOLD" -> "xyz"). Without this, positions on a builder dex
    (e.g. the tradfi "xyz" universe) are invisible.
    """
    dexes = {coin_dex(item) for item in watchlist}
    dexes.add("")  # always include the native clearinghouse
    return sorted(dexes)


def fetch_open_positions(cfg: Config, client: MarketDataProvider) -> list[OpenPosition]:
    """Open positions: read from Hyperliquid + declared manually.

    Hyperliquid positions are gathered across the native clearinghouse and
    every HIP-3 dex the watchlist references; a failure on one dex (a bad
    payload, an HTTP error, a timeout) doesn't drop the others.
    `[[positions.manual]]` entries cover a trade the configured address cannot
    see.
    """
    out: list[OpenPosition] = []
    if cfg.positions.address:
        for dex in watchlist_dexes(cfg.scanner.watchlist):
            try:
                state = client.clearinghouse_state(cfg.positions.address, dex=dex)
            except (HyperliquidError, httpx.HTTPError):
                continue
            out.extend(parse_positions(state))
    for m in cfg.positions.manual:
        out.append(OpenPosition(asset=m.asset, side=m.side, entry=m.entry, size=m.size))
    return out


def position_open_risk(position: dict[str, Any]) -> float:
    """Risk still open using the current Elder/SafeZone stop suggestion.

    Elder's 6% Rule (p.208-209): the distance from entry to the current stop,
    and zero once the stop is at or beyond break-even (it locks in profit).
    """
    entry, stop = float(position["entry"]), float(position["suggested_stop"])
    per_unit = entry - stop if position["side"] == "long" else stop - entry
    return max(0.0, per_unit) * float(position["size"])


def screen_bars(
    client: MarketDataProvider,
    coin: str,
    interval: str,
    lookback: int,
    now_ms: int,
    closure: WeekendClosure | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(completed bars the indicators may see, raw bars incl. the open one) for one
    screen of one coin.

    On a dex with a weekend `closure` (data/sessions.py), bars lying entirely
    inside it are dropped, and a weekly screen is rebuilt from the Monday-Friday
    daily bars instead of Hyperliquid's Thursday-anchored 1w candles. Without
    one, these are simply the completed bars.
    """
    if closure is not None and interval == "1w":
        raw = client.refresh(coin, "1d", lookback * 7)
        return completed_bars(weekly_from_weekdays(raw), now_ms), raw
    raw = client.refresh(coin, interval, lookback)
    return drop_closed_bars(completed_bars(raw, now_ms), closure), raw


def _position_frames(
    client: MarketDataProvider,
    horizon: HorizonConfig,
    coin: str,
    now_ms: int,
    closure: WeekendClosure | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame] | None:
    """Completed tide + wave bars for a held coin, or None if too new."""
    tide, _ = screen_bars(client, coin, horizon.tide, horizon.lookback_tide, now_ms, closure)
    wave, _ = screen_bars(client, coin, horizon.wave, horizon.lookback_wave, now_ms, closure)
    if len(tide) < 2 or len(wave) < 2:
        return None
    return tide, wave


def stop_memory_key(asset: str, side: str, entry: float) -> str:
    """One open position, for the stop memory: a new entry price is a new position."""
    return f"{asset}|{side}|{float(entry)!r}"


def stop_memory(positions: list[dict[str, Any]]) -> dict[str, float]:
    """The last suggested stop of every position just assessed, to store in the
    snapshot. Positions no longer held drop out, so their memory resets."""
    return {
        stop_memory_key(p["asset"], p["side"], p["entry"]): p["suggested_stop"] for p in positions
    }


def build_positions(
    client: MarketDataProvider,
    horizon: HorizonConfig,
    open_positions: list[OpenPosition],
    frames: dict[str, tuple[pd.DataFrame, pd.DataFrame]],
    now_ms: int,
    previous_stops: Mapping[str, float] | None = None,
    sessions: Mapping[str, WeekendClosure] | None = None,
) -> list[dict[str, Any]]:
    """Elder exit verdict per open position, reusing scan frames where available.

    A held coin outside the watchlist (so not already refreshed) gets its candles
    fetched on demand. Coins too new to evaluate are skipped silently.
    `previous_stops` (the previous snapshot's stop memory) keeps each suggested
    stop from moving back against its trade (Elder, p.224). `sessions` (dex ->
    weekend closure) shapes the bars of a held coin fetched on demand.
    """
    memory = previous_stops or {}
    out: list[dict[str, Any]] = []
    for pos in open_positions:
        tw = frames.get(pos.asset) or _position_frames(
            client, horizon, pos.asset, now_ms, (sessions or {}).get(coin_dex(pos.asset))
        )
        if tw is None:
            continue
        tide, wave = tw
        previous = memory.get(stop_memory_key(pos.asset, pos.side, pos.entry))
        verdict = asdict(assess_position(pos, tide, wave, horizon.params, previous))
        verdict["open_risk"] = position_open_risk(verdict)
        out.append(verdict)
    return out


def build_horizon(
    cfg: Config,
    client: MarketDataProvider,
    horizon: HorizonConfig,
    coins: dict[str, PerpSpec],
    now_ms: int,
    held: set[str],
    on_progress: Callable[[str, str], None] | None = None,
    funding: Mapping[str, float] | None = None,
) -> tuple[dict[str, Any], dict[str, tuple[pd.DataFrame, pd.DataFrame]]]:
    """One timeframe chain over the whole watchlist.

    Returns the horizon block (signals + charts, before sizing and ranking) and
    the tide/wave frames of any *held* coin, so trade management can reuse them.
    `funding` (coin -> hourly rate) adds each signal's estimated funding cost
    over the horizon's `holding_hours`.
    """
    params = horizon.params
    signals: list[dict[str, Any]] = []
    evaluated: list[Signal] = []
    charts: dict[str, Any] = {}
    skipped: list[str] = []
    held_frames: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {}

    for coin in coins:
        if on_progress is not None:
            on_progress(horizon.name, coin)
        # A tradfi perp's closed-market bars never reach an indicator (see
        # screen_bars). The raw wave frame keeps the still-open bar so we can read
        # a live price; the strategy itself only ever sees completed bars.
        closure = cfg.sessions.get(coin_dex(coin))
        tide, _ = screen_bars(client, coin, horizon.tide, horizon.lookback_tide, now_ms, closure)
        wave, wave_all = screen_bars(
            client, coin, horizon.wave, horizon.lookback_wave, now_ms, closure
        )
        entry_frame, _ = screen_bars(
            client, coin, horizon.entry, horizon.lookback_entry, now_ms, closure
        )

        # A market without two completed bars per screen can't be evaluated
        # (no slope, no prior-bar levels) — report, don't crash.
        if len(tide) < 2 or len(wave) < 2:
            skipped.append(f"{coin} — fewer than 2 completed {horizon.tide}/{horizon.wave} bars")
            continue

        if coin in held:
            held_frames[coin] = (tide, wave)

        sig = evaluate_asset(
            coin,
            tide,
            wave,
            entry_frame if len(entry_frame) >= 2 else None,
            params,
            horizon=horizon.name,
            intervals=horizon.intervals,
            min_tide_bars=horizon.min_tide_bars,
            sz_decimals=coins[coin].sz_decimals,
        )
        evaluated.append(sig)
        row = asdict(sig)
        row["position_size"] = None
        row["size_warnings"] = []
        row["last_close"] = float(wave["close"].iloc[-1])
        # Live price = the still-open wave bar's close (falls back to the last
        # completed close when there is no open bar). Lets the operator see how
        # far price has drifted from the basis the signal was computed on.
        row["live_price"] = float(wave_all["close"].iloc[-1]) if not wave_all.empty else None
        row["price_alert"] = live_price_alert(
            sig.action, row["live_price"], sig.entry, sig.stop, sig.target
        )
        # Funding: the current hourly rate, and what holding the trade for the
        # horizon's typical time would cost (positive = paid). Context only.
        rate = funding.get(coin) if funding else None
        cost = funding_cost(sig.action, rate, horizon.holding_hours)
        row["funding_rate"] = rate
        row["funding_hours"] = horizon.holding_hours
        row["funding_cost"] = cost
        row["funding_warning"] = funding_warning(cost, sig.entry, sig.stop, horizon.holding_hours)
        signals.append(row)

        charts[coin] = {
            "tide": chart_payload(tide),
            "wave": chart_payload(wave),
        }
        if len(entry_frame) >= 2:
            charts[coin]["entry"] = chart_payload(entry_frame)

    block = {
        "name": horizon.name,
        "label": horizon.label,
        "intervals": horizon.intervals,
        "refresh_seconds": horizon.refresh_seconds,
        "generated_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        "signals": signals,
        "charts": charts,
        "skipped": skipped,
        "top_pick": None,
        "_evaluated": evaluated,  # internal, removed by _finalize_block
    }
    return block, held_frames


def _finalize_block(
    block: dict[str, Any],
    cfg: Config,
    coins: dict[str, PerpSpec],
    guard_blocked: bool,
) -> dict[str, Any]:
    """Apply position sizing and Elder's "which trade?" ranking to one block.

    Kept separate from `build_horizon` because both depend on the 6% guard, which
    can only be computed once every horizon and every open position is known.
    """
    evaluated: list[Signal] = block.pop("_evaluated", [])

    for row in block["signals"]:
        # A non-positive reward:risk means the target sits on the wrong side of the
        # entry — price has already run through the whole value zone and channel, so
        # there is nothing left to capture and nothing to size. (Sub-2:1 setups are
        # still sized and merely flagged, per the spec: flag, don't hide.)
        tradable = (row["reward_risk"] or 0.0) > 0
        if row["action"] != "stand_aside" and tradable and not guard_blocked:
            spec = coins.get(row["asset"])
            # Elder's 2% Rule (p.204) is set once a month, from the equity on the
            # first day of the month — not from today's equity.
            sized = position_size(
                cfg.risk.equity_at_month_start,
                row["entry"],
                row["stop"],
                cfg.risk.risk_pct,
                sz_decimals=spec.sz_decimals if spec is not None else 2,
            )
            row["position_size"] = asdict(sized)
            # Margin is posted from the equity in the account now.
            row["size_warnings"] = size_warnings(sized.size, row["entry"], cfg.risk.equity, spec)

    # "Which trade do I take?" — ranked within the horizon, since setups are only
    # comparable against others on the same clock. While the 6% guard is active
    # no new entry is allowed, so no pick.
    best = None if guard_blocked else select_best(evaluated)
    block["top_pick"] = best.asset if best is not None else None
    for row in block["signals"]:
        row["is_top_pick"] = row["asset"] == block["top_pick"]
    return block


def build_snapshot(
    cfg: Config,
    client: MarketDataProvider,
    on_progress: Callable[[str, str], None] | None = None,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Full refresh of every horizon for the watchlist -> dashboard snapshot dict.

    `previous` is the snapshot on disk, if any: its stop memory keeps the open
    positions' suggested stops from moving back."""
    now_ms = int(time.time() * 1000)
    coins = expand_watchlist(cfg.scanner.watchlist, client)
    funding = fetch_funding_rates(coins, client)

    # Open positions (read-only) drive the Elder trade-management section.
    try:
        open_positions = fetch_open_positions(cfg, client)
    except HyperliquidError:
        open_positions = []
    held = {p.asset for p in open_positions}

    blocks: dict[str, Any] = {}
    position_frames: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {}
    for horizon in cfg.scanner.horizons:
        block, held_frames = build_horizon(
            cfg, client, horizon, coins, now_ms, held, on_progress, funding
        )
        blocks[horizon.name] = block
        if horizon.name == cfg.scanner.positions_horizon:
            position_frames = held_frames

    positions_horizon = cfg.scanner.horizon(cfg.scanner.positions_horizon)
    positions = build_positions(
        client,
        positions_horizon,
        open_positions,
        position_frames,
        now_ms,
        (previous or {}).get("stop_memory"),
        cfg.sessions,
    )
    guard, risks = _risk_summary(cfg, positions)

    snapshot: dict[str, Any] = {
        "generated_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        "equity": cfg.risk.equity,
        "equity_at_month_start": cfg.risk.equity_at_month_start,
        "risk_pct": cfg.risk.risk_pct,
        "guard": asdict(guard),
        **risks,
        "positions": positions,
        "stop_memory": stop_memory(positions),
        "positions_horizon": cfg.scanner.positions_horizon,
        "position_address": mask_address(cfg.positions.address) if cfg.positions.address else None,
        "horizons": {
            name: _finalize_block(block, cfg, coins, guard.blocked)
            for name, block in blocks.items()
        },
        "horizon_order": [h.name for h in cfg.scanner.horizons],
    }
    return snapshot


def _risk_summary(
    cfg: Config, positions: list[dict[str, Any]]
) -> tuple[MonthlyGuard, dict[str, float]]:
    """The 6% guard plus the open-risk figures it was computed from."""
    auto = sum(position_open_risk(p) for p in positions)
    total = cfg.risk.open_trade_risk + auto
    guard = six_percent_guard(
        cfg.risk.equity_at_month_start,
        cfg.risk.month_realized_losses,
        total,
    )
    return guard, {
        "manual_open_trade_risk": cfg.risk.open_trade_risk,
        "auto_open_trade_risk": auto,
        "total_open_trade_risk": total,
    }


def refresh_horizon(
    cfg: Config,
    client: MarketDataProvider,
    name: str,
    previous: dict[str, Any],
    on_progress: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    """Recompute ONE horizon and merge it into an existing snapshot.

    This is what makes per-horizon cadences cheap: a 5-minute "minutes" refresh
    refetches only 5m/15m/1h candles and leaves the swing block — and its
    `generated_at` — exactly as it was.

    Open positions and the 6% guard are global, so they are recomputed only when
    the refreshed horizon is the one that manages positions; otherwise the
    previous snapshot's values are carried over.
    """
    horizon = cfg.scanner.horizon(name)
    now_ms = int(time.time() * 1000)
    coins = expand_watchlist(cfg.scanner.watchlist, client)
    funding = fetch_funding_rates(coins, client)

    snapshot = dict(previous)
    snapshot.setdefault("horizons", {})
    snapshot["horizons"] = dict(snapshot["horizons"])

    try:
        open_positions = fetch_open_positions(cfg, client)
    except HyperliquidError:
        open_positions = []
    held = {p.asset for p in open_positions}

    block, held_frames = build_horizon(
        cfg, client, horizon, coins, now_ms, held, on_progress, funding
    )

    if name == cfg.scanner.positions_horizon:
        positions = build_positions(
            client,
            horizon,
            open_positions,
            held_frames,
            now_ms,
            previous.get("stop_memory"),
            cfg.sessions,
        )
        guard, risks = _risk_summary(cfg, positions)
        snapshot["positions"] = positions
        snapshot["stop_memory"] = stop_memory(positions)
        snapshot["guard"] = asdict(guard)
        snapshot.update(risks)
        blocked = guard.blocked
    else:
        blocked = bool(snapshot.get("guard", {}).get("blocked", False))

    snapshot["horizons"][name] = _finalize_block(block, cfg, coins, blocked)
    snapshot["generated_at"] = datetime.now(tz=UTC).isoformat(timespec="seconds")
    snapshot["equity"] = cfg.risk.equity
    snapshot["equity_at_month_start"] = cfg.risk.equity_at_month_start
    snapshot["risk_pct"] = cfg.risk.risk_pct
    snapshot["positions_horizon"] = cfg.scanner.positions_horizon
    snapshot["horizon_order"] = [h.name for h in cfg.scanner.horizons]
    snapshot["position_address"] = (
        mask_address(cfg.positions.address) if cfg.positions.address else None
    )
    return snapshot


def load_snapshot(path: Path) -> dict[str, Any]:
    """The snapshot on disk, or an empty dict when there is none (or it is corrupt)."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
