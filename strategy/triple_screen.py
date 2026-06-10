"""Elder's Triple Screen + Impulse on weekly/daily candles.

Decision table (CLAUDE.md, canonical):
  weekly up   + daily FI(2) below zero  -> long
  weekly up   + FI rising/above zero    -> stand aside (chasing)
  weekly down + daily FI(2) above zero  -> short
  weekly down + FI falling/below zero   -> stand aside
Impulse censorship is applied LAST: any red (weekly or daily) forbids longs,
any green forbids shorts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import pandas as pd

from indicators import ema, force_index, impulse_color

Action = Literal["long", "short", "stand_aside"]
Trend = Literal["up", "down", "neutral"]

EMA_FAST = 13
EMA_SLOW = 26
# Hyperliquid quotes prices to at most 5 significant figures.
PRICE_SIG_FIGS = 5
# "last ~4-6 weeks" of daily bars on a 24/7 market.
PENETRATION_LOOKBACK_DAYS = 35
# Weekly channel: average excursion of highs/lows beyond EMA13 over this window.
CHANNEL_LOOKBACK_WEEKS = 26
MIN_REWARD_RISK = 2.0


@dataclass(frozen=True)
class Signal:
    asset: str
    action: Action
    reason: str
    weekly_trend: Trend
    weekly_impulse: str
    daily_impulse: str
    force_index_2: float
    entry: float | None  # stop-entry trigger (1 tick beyond prior day's extreme)
    entry_limit: float | None  # pullback limit at projected EMA13 -/+ avg penetration
    stop: float | None
    target: float | None
    reward_risk: float | None
    rr_ok: bool


def tick_size(price: float) -> float:
    """One price tick, assuming 5-significant-figure Hyperliquid quoting."""
    if price <= 0:
        raise ValueError("price must be positive")
    return 10.0 ** (math.floor(math.log10(price)) - (PRICE_SIG_FIGS - 1))


def weekly_trend(weekly_close: pd.Series, span: int = EMA_FAST) -> Trend:
    """First screen: the tide = slope of the weekly EMA13."""
    e = ema(weekly_close, span)
    if len(e) < 2:
        return "neutral"
    diff = float(e.iloc[-1] - e.iloc[-2])
    if diff > 0:
        return "up"
    if diff < 0:
        return "down"
    return "neutral"


def average_penetration(
    daily: pd.DataFrame,
    side: Literal["down", "up"],
    span: int = EMA_FAST,
    lookback: int = PENETRATION_LOOKBACK_DAYS,
) -> float | None:
    """Average distance pullbacks pierce the daily EMA13 over the last `lookback` days.

    side="down": how far lows dip below the EMA (for longs in an uptrend);
    side="up":   how far highs poke above the EMA (for shorts in a downtrend).
    Days without a penetration are ignored; returns None if there were none.
    """
    e = ema(daily["close"], span)
    raw = e - daily["low"] if side == "down" else daily["high"] - e
    pen = raw.clip(lower=0).iloc[-lookback:]
    pen = pen[pen > 0]
    if pen.empty:
        return None
    return float(pen.mean())


def projected_ema(daily_close: pd.Series, span: int = EMA_FAST) -> float:
    """Tomorrow's EMA estimate: today_EMA + (today_EMA - yesterday_EMA)."""
    e = ema(daily_close, span)
    if len(e) < 2:
        return float(e.iloc[-1])
    return float(2 * e.iloc[-1] - e.iloc[-2])


def weekly_channel(weekly: pd.DataFrame, span: int = EMA_FAST) -> tuple[float, float]:
    """(upper, lower) weekly channel around EMA13.

    Estimated from the average excursion of weekly highs above / lows below the
    EMA over the channel lookback — used as a fallback target when price already
    trades beyond the weekly value zone.
    """
    e = ema(weekly["close"], span)
    up = (weekly["high"] - e).clip(lower=0).iloc[-CHANNEL_LOOKBACK_WEEKS:]
    down = (e - weekly["low"]).clip(lower=0).iloc[-CHANNEL_LOOKBACK_WEEKS:]
    up = up[up > 0]
    down = down[down > 0]
    last = float(e.iloc[-1])
    upper = last + (float(up.mean()) if not up.empty else 0.0)
    lower = last - (float(down.mean()) if not down.empty else 0.0)
    return upper, lower


def _long_levels(
    weekly: pd.DataFrame, daily: pd.DataFrame
) -> tuple[float, float | None, float, float]:
    """(entry, entry_limit, stop, target) for a long setup."""
    prior_high = float(daily["high"].iloc[-1])
    tick = tick_size(prior_high)
    entry = prior_high + tick  # buy-stop 1 tick above prior day's high

    pen = average_penetration(daily, "down")
    limit = projected_ema(daily["close"]) - pen if pen is not None else None

    stop = float(daily["low"].iloc[-2:].min()) - tick  # daily stop below recent lows

    # Target: weekly value zone (between EMA13 and EMA26); if price already
    # trades above value, fall back to the weekly upper channel.
    e13 = float(ema(weekly["close"], EMA_FAST).iloc[-1])
    e26 = float(ema(weekly["close"], EMA_SLOW).iloc[-1])
    value_high = max(e13, e26)
    target = value_high if value_high > entry else weekly_channel(weekly)[0]
    return entry, limit, stop, target


def _short_levels(
    weekly: pd.DataFrame, daily: pd.DataFrame
) -> tuple[float, float | None, float, float]:
    """(entry, entry_limit, stop, target) for a short setup."""
    prior_low = float(daily["low"].iloc[-1])
    tick = tick_size(prior_low)
    entry = prior_low - tick  # sell-stop 1 tick below prior day's low

    pen = average_penetration(daily, "up")
    limit = projected_ema(daily["close"]) + pen if pen is not None else None

    stop = float(daily["high"].iloc[-2:].max()) + tick

    e13 = float(ema(weekly["close"], EMA_FAST).iloc[-1])
    e26 = float(ema(weekly["close"], EMA_SLOW).iloc[-1])
    value_low = min(e13, e26)
    target = value_low if value_low < entry else weekly_channel(weekly)[1]
    return entry, limit, stop, target


def evaluate_asset(asset: str, weekly: pd.DataFrame, daily: pd.DataFrame) -> Signal:
    """Run the three screens + Impulse censorship for one asset.

    `weekly` and `daily` must be OHLCV frames of *completed* bars
    (open/high/low/close/volume columns, oldest first).
    """
    w_imp = str(impulse_color(weekly["close"]).iloc[-1])
    d_imp = str(impulse_color(daily["close"]).iloc[-1])
    trend = weekly_trend(weekly["close"])
    fi2 = float(force_index(daily["close"], daily["volume"], span=2).iloc[-1])

    candidate: Action = "stand_aside"
    if trend == "up":
        if fi2 < 0:
            candidate = "long"
            reason = "weekly tide up, daily 2-EMA Force Index below zero (pullback to buy)"
        else:
            reason = "weekly tide up but Force Index not below zero — chasing, stand aside"
    elif trend == "down":
        if fi2 > 0:
            candidate = "short"
            reason = "weekly tide down, daily 2-EMA Force Index above zero (rally to sell)"
        else:
            reason = "weekly tide down but Force Index not above zero — stand aside"
    else:
        reason = "weekly tide neutral — stand aside"

    # Impulse censorship overlay — applied last; it says what NOT to do.
    if candidate == "long" and "red" in (w_imp, d_imp):
        candidate = "stand_aside"
        reason = f"long vetoed by Impulse (weekly={w_imp}, daily={d_imp}: red forbids longs)"
    elif candidate == "short" and "green" in (w_imp, d_imp):
        candidate = "stand_aside"
        reason = f"short vetoed by Impulse (weekly={w_imp}, daily={d_imp}: green forbids shorts)"

    entry = limit = stop = target = rr = None
    if candidate == "long":
        entry, limit, stop, target = _long_levels(weekly, daily)
        if entry > stop:
            rr = (target - entry) / (entry - stop)
    elif candidate == "short":
        entry, limit, stop, target = _short_levels(weekly, daily)
        if stop > entry:
            rr = (entry - target) / (stop - entry)

    return Signal(
        asset=asset,
        action=candidate,
        reason=reason,
        weekly_trend=trend,
        weekly_impulse=w_imp,
        daily_impulse=d_imp,
        force_index_2=fi2,
        entry=entry,
        entry_limit=limit,
        stop=stop,
        target=target,
        reward_risk=rr,
        rr_ok=rr is not None and rr >= MIN_REWARD_RISK,
    )
