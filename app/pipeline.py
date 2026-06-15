"""Build the dashboard snapshot: refresh candles, compute signals + chart data.

The snapshot is a single JSON document written by run.py and read by the
FastAPI app — the server itself never talks to the exchange.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from config import WATCHLIST_ALL, Config
from data.hyperliquid import HyperliquidClient, completed_bars
from indicators import ema, force_index, impulse_color, macd_histogram
from risk.sizing import position_size, six_percent_guard
from strategy.triple_screen import EMA_FAST, EMA_SLOW, Signal, evaluate_asset, select_best

SNAPSHOT_FILENAME = "snapshot.json"

IMPULSE_HEX = {"green": "#089981", "red": "#f23645", "blue": "#2962ff"}
HIST_UP_HEX = "#089981"
HIST_DOWN_HEX = "#f23645"


def _line_points(times: pd.Series, values: pd.Series) -> list[dict[str, Any]]:
    pts = []
    for t, v in zip(times, values, strict=True):
        if pd.notna(v):
            pts.append({"time": int(t) // 1000, "value": float(v)})
    return pts


def chart_payload(df: pd.DataFrame) -> dict[str, Any]:
    """Per-interval chart data for Lightweight Charts (times in epoch seconds)."""
    close, volume, t = df["close"], df["volume"], df["t"]
    colors = impulse_color(close)
    hist = macd_histogram(close)

    candles = []
    for i in range(len(df)):
        hex_color = IMPULSE_HEX[colors.iloc[i]]
        candles.append(
            {
                "time": int(t.iloc[i]) // 1000,
                "open": float(df["open"].iloc[i]),
                "high": float(df["high"].iloc[i]),
                "low": float(df["low"].iloc[i]),
                "close": float(close.iloc[i]),
                "color": hex_color,
                "borderColor": hex_color,
                "wickColor": hex_color,
            }
        )

    hist_points = []
    prev = None
    for ts, v in zip(t, hist, strict=True):
        if pd.isna(v):
            continue
        v = float(v)
        # Elder colors the histogram by slope: rising vs falling.
        up = prev is None or v >= prev
        hist_points.append(
            {
                "time": int(ts) // 1000,
                "value": v,
                "color": HIST_UP_HEX if up else HIST_DOWN_HEX,
            }
        )
        prev = v

    return {
        "candles": candles,
        "ema13": _line_points(t, ema(close, EMA_FAST)),
        "ema26": _line_points(t, ema(close, EMA_SLOW)),
        "macd_hist": hist_points,
        "force_index_2": _line_points(t, force_index(close, volume, span=2)),
        "force_index_13": _line_points(t, force_index(close, volume, span=13)),
    }


def expand_watchlist(watchlist: tuple[str, ...], client: HyperliquidClient) -> dict[str, int]:
    """Resolve watchlist entries to {coin: szDecimals}, sorted by name.

    "*" expands to every tradable native (crypto) perp; "<dex>:*" to every
    tradable perp of a HIP-3 builder dex (e.g. "xyz:*" = the tradfi universe:
    stocks, indices, gold, oil, forex…). Explicit coins are validated.
    """
    sz_decimals: dict[str, int] = {}
    explicit: list[str] = []
    for item in watchlist:
        if item == WATCHLIST_ALL:
            sz_decimals.update(client.tradable_perps())
        elif item.endswith(":" + WATCHLIST_ALL):
            sz_decimals.update(client.tradable_perps(item[: -len(":" + WATCHLIST_ALL)]))
        else:
            explicit.append(item)
    if explicit:
        sz_decimals.update(client.validate_watchlist(explicit))
    return dict(sorted(sz_decimals.items()))


def build_snapshot(
    cfg: Config,
    client: HyperliquidClient,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Full daily refresh for the watchlist -> dashboard snapshot dict.

    "*" in the watchlist expands to every tradable native perp, "<dex>:*" to a
    whole HIP-3 dex (e.g. "xyz:*" for the tradfi perps).
    """
    now_ms = int(time.time() * 1000)
    sz_decimals = expand_watchlist(cfg.scanner.watchlist, client)
    guard = six_percent_guard(
        cfg.risk.equity_at_month_start,
        cfg.risk.month_realized_losses,
        cfg.risk.open_trade_risk,
    )

    signals: list[dict[str, Any]] = []
    evaluated: list[Signal] = []  # parallel to `signals`, for cross-asset ranking
    charts: dict[str, Any] = {}
    skipped: list[str] = []
    for coin in sz_decimals:
        if on_progress is not None:
            on_progress(coin)
        weekly = completed_bars(
            client.refresh(coin, cfg.scanner.weekly_interval, cfg.scanner.lookback_weeks),
            now_ms,
        )
        daily = completed_bars(
            client.refresh(coin, cfg.scanner.daily_interval, cfg.scanner.lookback_days),
            now_ms,
        )

        # Fresh listings without two completed bars per timeframe can't be
        # evaluated (no slope, no prior-day levels) — report, don't crash.
        if len(weekly) < 2 or len(daily) < 2:
            skipped.append(coin)
            continue

        sig = evaluate_asset(coin, weekly, daily)
        evaluated.append(sig)
        row = asdict(sig)
        row["position_size"] = None
        if sig.action != "stand_aside" and sig.entry and sig.stop and not guard.blocked:
            row["position_size"] = asdict(
                position_size(
                    cfg.risk.equity,
                    sig.entry,
                    sig.stop,
                    cfg.risk.risk_pct,
                    sz_decimals=sz_decimals[coin],
                )
            )
        row["last_close"] = float(daily["close"].iloc[-1]) if not daily.empty else None
        signals.append(row)

        charts[coin] = {"weekly": chart_payload(weekly), "daily": chart_payload(daily)}

    # "Which trade do I take?" — rank the validated setups and flag the single
    # best. While the 6% guard is active no new entry is allowed, so no pick.
    best = None if guard.blocked else select_best(evaluated)
    best_asset = best.asset if best is not None else None
    for row in signals:
        row["is_top_pick"] = row["asset"] == best_asset

    return {
        "generated_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        "equity": cfg.risk.equity,
        "risk_pct": cfg.risk.risk_pct,
        "guard": asdict(guard),
        "top_pick": best_asset,
        "signals": signals,
        "skipped": skipped,
        "charts": charts,
    }
