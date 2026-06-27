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
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

import pandas as pd

from indicators import ema, force_index, impulse_color, macd_histogram
from strategy.params import StrategyParams

Action = Literal["long", "short", "stand_aside"]
Trend = Literal["up", "down", "neutral"]

EMA_FAST = 13
EMA_SLOW = 26
# Hyperliquid quotes prices to at most 5 significant figures.
PRICE_SIG_FIGS = 5
# "last ~4-6 weeks" of daily bars on a 24/7 market.
PENETRATION_LOOKBACK_DAYS = 35
# Elder's 2nd-screen caveat: a Force Index signal is void if FI(2) also prints a
# new multi-week extreme (accelerating move, not a pullback). ~3-4 weeks of bars.
FORCE_INDEX_EXTREME_LOOKBACK = 25
# Weekly channel: containment-quantile excursion of highs/lows beyond the slow
# EMA26 over this window (Elder's percentage envelope, p.183).
CHANNEL_LOOKBACK_WEEKS = 26
CHANNEL_CONTAINMENT = 0.95
MIN_REWARD_RISK = 2.0
# Treat tiny weekly EMA13 slopes as range/no-trend instead of a tradable tide.
FLAT_TREND_SLOPE_PCT = 0.001
DIVERGENCE_LOOKBACK = 60
# Elder/Lovvorn (p.104): the two divergence extremes should be 20-40 bars apart.
DIVERGENCE_MIN_SEPARATION = 20
DIVERGENCE_MAX_SEPARATION = 40

# --- Trade-quality ranking ("which setup is best?", Elder's selection logic) ---
# Reward:risk is Elder's gatekeeper (2:1 floor); 3:1 or better earns full credit.
RR_EXCELLENT = 3.0
# A weekly EMA13 sloping ~3% per bar is already a strong tide; cap the score there.
STRONG_WEEKLY_SLOPE = 0.03
# Window for scaling the daily Force-Index pullback into a 0-1 "depth".
FI_SCALE_LOOKBACK = 20
# Composite weights (sum to 1): reward:risk dominates, per the book.
SCORE_WEIGHTS = {"reward_risk": 0.40, "impulse": 0.25, "tide": 0.20, "pullback": 0.15}
DEFAULT_PARAMS = StrategyParams()


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
    weekly_trend_strength: float  # scale-free |slope| of the weekly EMA13
    pullback_quality: float  # 0-1 depth of today's daily Force-Index pullback
    quality_score: float | None  # composite Elder rank, None when standing aside
    market_regime: str  # trending / flat, derived from the weekly EMA13 slope filter
    third_screen_impulse: str | None  # optional lower-timeframe Impulse used for entry timing
    divergences: list[str]  # Elder MACD-Histogram / Force Index divergence warnings
    value_zone_status: str  # in_value / near_value / extended
    entry_order_plan: str | None  # how to roll/expire the theoretical stop-entry
    # Second entry technique: stop & reward:risk for the limit (pullback) fill,
    # which differ from the breakout entry's. None when there is no limit entry.
    entry_limit_stop: float | None = None
    reward_risk_limit: float | None = None


def tick_size(price: float) -> float:
    """One price tick, assuming 5-significant-figure Hyperliquid quoting."""
    if price <= 0:
        raise ValueError("price must be positive")
    return 10.0 ** (math.floor(math.log10(price)) - (PRICE_SIG_FIGS - 1))


def weekly_trend(
    weekly_close: pd.Series,
    span: int = EMA_FAST,
    min_slope_pct: float = FLAT_TREND_SLOPE_PCT,
) -> Trend:
    """First screen: the tide = slope of the weekly EMA13.

    Tiny EMA slopes are classified as neutral so sideways markets do not become
    accidental up/down trends because of floating-point noise or one small bar.
    """
    e = ema(weekly_close, span)
    if len(e) < 2:
        return "neutral"
    diff = float(e.iloc[-1] - e.iloc[-2])
    base = abs(float(e.iloc[-1]))
    if base == 0 or abs(diff) / base < min_slope_pct:
        return "neutral"
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


def force_index_new_extreme(
    fi2: pd.Series,
    side: Literal["long", "short"],
    lookback: int = FORCE_INDEX_EXTREME_LOOKBACK,
) -> bool:
    """Elder's second-screen caveat (book p.158): the daily Force Index signal is
    void when FI(2) prints a *new multi-week extreme*.

    A buy signal is valid only while the 2-day Force Index dips below zero "as
    long as it doesn't fall to a new multi-week low" — a fresh low means the
    decline is accelerating, not a pullback to buy (p.131). Mirror image for
    shorts and new highs. Returns True when today's FI(2) breaks the prior
    `lookback` bars' extreme, i.e. the signal should be skipped.
    """
    if len(fi2) < 2:
        return False
    now = float(fi2.iloc[-1])
    prior = fi2.iloc[-(lookback + 1) : -1]
    if prior.empty:
        return False
    return now < float(prior.min()) if side == "long" else now > float(prior.max())


def value_zone_status(daily: pd.DataFrame, max_distance_pct: float = 0.03) -> str:
    """Classify the last daily close versus Elder's daily EMA13-EMA26 value zone.

    The Triple Screen should enter on pullbacks to value, not after price has
    already run away. "Near" allows a small configurable overshoot so strong
    trends are not rejected for being a few ticks outside the zone.
    """
    close = float(daily["close"].iloc[-1])
    e13 = float(ema(daily["close"], EMA_FAST).iloc[-1])
    e26 = float(ema(daily["close"], EMA_SLOW).iloc[-1])
    low, high = sorted((e13, e26))
    if low <= close <= high:
        return "in_value"
    if close > high:
        return "near_value" if (close - high) / high <= max_distance_pct else "extended"
    return "near_value" if (low - close) / low <= max_distance_pct else "extended"


def value_zone_extension(daily: pd.DataFrame) -> Literal["above", "below", "inside"]:
    """Which side of the daily EMA13-EMA26 value zone the last close sits on.

    Makes the "extended" veto directional. Elder only warns against *chasing* —
    buying after price has run above value, or shorting after it has broken below
    value. A pullback extended the *other* way (a long far below value, a short
    far above) is a bargain, so it must not be vetoed by the value-zone filter.
    """
    close = float(daily["close"].iloc[-1])
    e13 = float(ema(daily["close"], EMA_FAST).iloc[-1])
    e26 = float(ema(daily["close"], EMA_SLOW).iloc[-1])
    low, high = sorted((e13, e26))
    if close > high:
        return "above"
    if close < low:
        return "below"
    return "inside"


def average_adverse_noise(
    daily: pd.DataFrame,
    side: Literal["long", "short"],
    lookback: int,
) -> float | None:
    """Elder SafeZone noise: average adverse one-bar extreme expansion.

    Longs care about downside noise (today's low undercuts yesterday's low).
    Shorts care about upside noise (today's high exceeds yesterday's high).
    Bars without adverse noise are ignored, matching SafeZone's intent to measure
    normal counter-trend noise instead of all volatility.
    """
    if side == "long":
        noise = (daily["low"].shift(1) - daily["low"]).clip(lower=0)
    else:
        noise = (daily["high"] - daily["high"].shift(1)).clip(lower=0)
    noise = noise.dropna().iloc[-lookback:]
    noise = noise[noise > 0]
    if noise.empty:
        return None
    return float(noise.mean())


def _safezone_stop_from_base(
    daily: pd.DataFrame,
    side: Literal["long", "short"],
    base: float,
    params: StrategyParams = DEFAULT_PARAMS,
) -> float:
    """SafeZone adverse-noise buffer applied below/above a given anchor `base`."""
    if side == "long":
        noise = average_adverse_noise(daily, side, params.safezone_lookback_days)
        offset = (noise * params.safezone_factor_long) if noise is not None else tick_size(base)
        return base - offset
    noise = average_adverse_noise(daily, side, params.safezone_lookback_days)
    offset = (noise * params.safezone_factor_short) if noise is not None else tick_size(base)
    return base + offset


def safezone_initial_stop(
    daily: pd.DataFrame,
    side: Literal["long", "short"],
    params: StrategyParams = DEFAULT_PARAMS,
) -> float:
    """Initial protective stop using Elder's SafeZone adverse-noise method.

    Anchored to the recent daily extreme (2-bar low for longs, high for shorts) —
    the stop that pairs with the buy-stop / sell-stop (breakout) entry.
    """
    if side == "long":
        base = float(daily["low"].iloc[-2:].min())
    else:
        base = float(daily["high"].iloc[-2:].max())
    return _safezone_stop_from_base(daily, side, base, params)


def safezone_stop_for_limit(
    daily: pd.DataFrame,
    side: Literal["long", "short"],
    limit: float,
    params: StrategyParams = DEFAULT_PARAMS,
) -> float:
    """SafeZone stop recalibrated to a limit (pullback) fill.

    A deep pullback can fill *below* the recent daily low (long) — beyond the
    breakout stop — so the SafeZone buffer is anchored to whichever is more
    protective, the recent extreme or the limit fill itself. This keeps the stop
    on the correct side of the limit entry, and collapses to the breakout stop
    when the limit sits inside the recent range.
    """
    if side == "long":
        base = min(float(daily["low"].iloc[-2:].min()), limit)
    else:
        base = max(float(daily["high"].iloc[-2:].max()), limit)
    return _safezone_stop_from_base(daily, side, base, params)


def theoretical_entry_order_plan(
    action: Action,
    entry: float | None,
    params: StrategyParams = DEFAULT_PARAMS,
) -> str | None:
    """Human-readable lifecycle for the stop-entry order Elder would trail."""
    if action not in ("long", "short") or entry is None:
        return None
    direction = "buy-stop" if action == "long" else "sell-stop"
    return (
        f"Place a theoretical {direction} at {entry:.6g}; if not filled, roll it daily "
        f"to the latest completed bar's {'high + 1 tick' if action == 'long' else 'low - 1 tick'} "
        f"while the weekly tide, Force Index pullback, value-zone filter and Impulse veto "
        f"remain valid; expire after {params.entry_order_expire_days} completed daily bars."
    )


def _weights(params: StrategyParams) -> dict[str, float]:
    return {
        "reward_risk": params.score_reward_risk_weight,
        "impulse": params.score_impulse_weight,
        "tide": params.score_tide_weight,
        "pullback": params.score_pullback_weight,
    }


def projected_ema(daily_close: pd.Series, span: int = EMA_FAST) -> float:
    """Tomorrow's EMA estimate: today_EMA + (today_EMA - yesterday_EMA)."""
    e = ema(daily_close, span)
    if len(e) < 2:
        return float(e.iloc[-1])
    return float(2 * e.iloc[-1] - e.iloc[-2])


def weekly_channel(weekly: pd.DataFrame, span: int = EMA_SLOW) -> tuple[float, float]:
    """(upper, lower) weekly channel around the slow EMA26 — Elder's percentage
    envelope (p.183), used as a fallback target when price already trades beyond
    the weekly value zone.

    Elder draws the channel parallel to the *slower* EMA and widens it until it
    contains ~95% of recent bars. Each half-width is the `containment`-quantile of
    the **relative** excursion of weekly highs above / lows below the EMA
    (penetration / EMA at that bar) over the lookback, projected onto today's EMA.

    Measuring the excursion as a *ratio* (not an absolute price distance) keeps the
    channel proportional to today's price and the lower band strictly positive even
    for a coin that has since crashed — a deliberate 24/7 adaptation that fits the
    two sides independently rather than as one symmetric coefficient.
    """
    return _weekly_channel(weekly, span=span)


def weekly_channel_with_params(
    weekly: pd.DataFrame, params: StrategyParams, span: int = EMA_SLOW
) -> tuple[float, float]:
    return _weekly_channel(
        weekly,
        span=span,
        lookback=params.channel_lookback_weeks,
        containment=params.channel_containment,
    )


def _weekly_channel(
    weekly: pd.DataFrame,
    span: int = EMA_SLOW,
    lookback: int = CHANNEL_LOOKBACK_WEEKS,
    containment: float = CHANNEL_CONTAINMENT,
) -> tuple[float, float]:
    e = ema(weekly["close"], span)
    window = slice(-lookback, None)
    # Relative excursion of each bar's high above / low below the EMA (0 when the
    # bar doesn't poke out). The containment-quantile leaves ~(1-containment) of
    # bars outside the channel — Elder's "contains ~95% of bars" fit (p.183).
    up = ((weekly["high"] - e) / e).clip(lower=0).iloc[window]
    down = ((e - weekly["low"]) / e).clip(lower=0).iloc[window]
    last = float(e.iloc[-1])
    upper = last * (1.0 + (float(up.quantile(containment)) if not up.empty else 0.0))
    lower = last * (1.0 - (float(down.quantile(containment)) if not down.empty else 0.0))
    return upper, lower


def _long_levels(
    weekly: pd.DataFrame, daily: pd.DataFrame, params: StrategyParams = DEFAULT_PARAMS
) -> tuple[float, float | None, float, float]:
    """(entry, entry_limit, stop, target) for a long setup."""
    prior_high = float(daily["high"].iloc[-1])
    tick = tick_size(prior_high)
    entry = prior_high + tick  # buy-stop 1 tick above prior day's high

    pen = average_penetration(daily, "down", lookback=params.penetration_lookback_days)
    limit = projected_ema(daily["close"]) - pen if pen is not None else None

    stop = safezone_initial_stop(daily, "long", params)

    # Target: weekly value zone (between EMA13 and EMA26); if price already
    # trades above value, fall back to the weekly upper channel.
    e13 = float(ema(weekly["close"], EMA_FAST).iloc[-1])
    e26 = float(ema(weekly["close"], EMA_SLOW).iloc[-1])
    value_high = max(e13, e26)
    target = value_high if value_high > entry else weekly_channel_with_params(weekly, params)[0]
    return entry, limit, stop, target


def _short_levels(
    weekly: pd.DataFrame, daily: pd.DataFrame, params: StrategyParams = DEFAULT_PARAMS
) -> tuple[float, float | None, float, float]:
    """(entry, entry_limit, stop, target) for a short setup."""
    prior_low = float(daily["low"].iloc[-1])
    tick = tick_size(prior_low)
    entry = prior_low - tick  # sell-stop 1 tick below prior day's low

    pen = average_penetration(daily, "up", lookback=params.penetration_lookback_days)
    limit = projected_ema(daily["close"]) + pen if pen is not None else None

    stop = safezone_initial_stop(daily, "short", params)

    e13 = float(ema(weekly["close"], EMA_FAST).iloc[-1])
    e26 = float(ema(weekly["close"], EMA_SLOW).iloc[-1])
    value_low = min(e13, e26)
    target = value_low if value_low < entry else weekly_channel_with_params(weekly, params)[1]
    return entry, limit, stop, target


def _last_pivot(values: pd.Series, *, kind: Literal["low", "high"]) -> tuple[int, float] | None:
    if values.empty or values.isna().all():
        return None
    idx = int(values.idxmin() if kind == "low" else values.idxmax())
    return idx, float(values.loc[idx])


def _divergence_for_indicator(
    close: pd.Series,
    indicator: pd.Series,
    name: str,
    lookback: int = DIVERGENCE_LOOKBACK,
    min_separation: int = DIVERGENCE_MIN_SEPARATION,
    max_separation: int = DIVERGENCE_MAX_SEPARATION,
) -> list[str]:
    """Detect simple Elder-style price/indicator divergences on recent swings.

    Bullish: latest price low undercuts a prior low while the indicator makes a
    higher low. Bearish: latest price high exceeds a prior high while the
    indicator makes a lower high. Two Elder validity gates apply: the indicator
    must cross its zero line between the two extremes (p.103), and the extremes
    must sit `min_separation`-`max_separation` bars apart (p.104). Intentionally
    conservative and warning-only; it never creates trades by itself.
    """
    df = pd.DataFrame({"close": close, "indicator": indicator}).dropna().tail(lookback)
    if len(df) < 10:
        return []
    df = df.reset_index(drop=True)
    split = max(3, len(df) // 2)
    prev = df.iloc[:split]
    recent = df.iloc[split:]
    ind = df["indicator"]
    out: list[str] = []

    prev_low = _last_pivot(prev["close"], kind="low")
    recent_low = _last_pivot(recent["close"], kind="low")
    if prev_low and recent_low:
        pi, pc = prev_low
        ri, rc = recent_low
        # Elder (p.103): the indicator MUST cross back above its zero line between
        # the two bottoms ("an absolute must"); and (p.104) the bottoms must be
        # 20-40 bars apart to be tradable. Either gate failing => no divergence.
        crossed_zero = bool((ind.loc[pi:ri] > 0).any())
        spaced = min_separation <= (ri - pi) <= max_separation
        if rc < pc and float(ind.loc[ri]) > float(ind.loc[pi]) and crossed_zero and spaced:
            out.append(f"bullish {name} divergence")

    prev_high = _last_pivot(prev["close"], kind="high")
    recent_high = _last_pivot(recent["close"], kind="high")
    if prev_high and recent_high:
        pi, pc = prev_high
        ri, rc = recent_high
        # Mirror image: the indicator must drop below its zero line between the
        # two tops, and the tops must be 20-40 bars apart.
        crossed_zero = bool((ind.loc[pi:ri] < 0).any())
        spaced = min_separation <= (ri - pi) <= max_separation
        if rc > pc and float(ind.loc[ri]) < float(ind.loc[pi]) and crossed_zero and spaced:
            out.append(f"bearish {name} divergence")
    return out


def detect_divergences(
    daily: pd.DataFrame,
    lookback: int = DIVERGENCE_LOOKBACK,
    min_separation: int = DIVERGENCE_MIN_SEPARATION,
    max_separation: int = DIVERGENCE_MAX_SEPARATION,
) -> list[str]:
    """Recent Elder divergence warnings from MACD-Histogram and 13-EMA Force Index."""
    close = daily["close"]
    volume = daily["volume"]
    out: list[str] = []
    for series, label in (
        (macd_histogram(close), "MACD-Histogram"),
        (force_index(close, volume, span=13), "Force Index"),
    ):
        out.extend(
            _divergence_for_indicator(
                close, series, label, lookback, min_separation, max_separation
            )
        )
    return out


def _third_screen_levels(
    action: Action, lower_timeframe: pd.DataFrame | None, fallback_entry: float
) -> tuple[float, str | None]:
    """Optional lower-timeframe trigger for the third screen.

    When 4h bars are supplied, time the stop-entry from the latest completed 4h
    bar instead of the daily bar; otherwise preserve the daily trigger.
    """
    if lower_timeframe is None or lower_timeframe.empty:
        return fallback_entry, None
    imp = str(impulse_color(lower_timeframe["close"]).iloc[-1])
    if action == "long":
        px = float(lower_timeframe["high"].iloc[-1])
        return px + tick_size(px), imp
    if action == "short":
        px = float(lower_timeframe["low"].iloc[-1])
        return px - tick_size(px), imp
    return fallback_entry, imp


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def weekly_slope_strength(weekly_close: pd.Series, span: int = EMA_FAST) -> float:
    """Scale-free strength of the weekly tide: |EMA13 today - yesterday| / EMA13.

    A ratio so a $0.05 alt and $60k BTC are comparable; 0 if the EMA is flat.
    """
    e = ema(weekly_close, span)
    if len(e) < 2 or e.iloc[-1] == 0:
        return 0.0
    return abs(float(e.iloc[-1] - e.iloc[-2])) / abs(float(e.iloc[-1]))


def impulse_confirmation(action: Action, weekly_impulse: str, daily_impulse: str) -> float:
    """Fraction of screens whose Impulse actively confirms the trade (0, .5, 1).

    Longs are confirmed by green Impulse, shorts by red, counted on weekly + daily
    then averaged. (Censorship has already removed the vetoing color, so this only
    rewards positive agreement — a blue/neutral screen scores nothing.)
    """
    favorable = "green" if action == "long" else "red"
    return ((weekly_impulse == favorable) + (daily_impulse == favorable)) / 2.0


def pullback_quality(daily: pd.DataFrame) -> float:
    """How stretched today's daily Force-Index pullback is, scaled 0-1.

    |FI(2)| is measured against this asset's own recent average |FI(2)|, so the
    depth is comparable across assets. Elder enters on a pullback to value; a
    deeper-than-usual pullback (larger |FI|) is the better entry.
    """
    fi = force_index(daily["close"], daily["volume"], span=2)
    scale = float(fi.abs().iloc[-FI_SCALE_LOOKBACK:].mean())
    if scale <= 0:
        return 0.0
    return _clamp01(abs(float(fi.iloc[-1])) / scale)


def pullback_quality_with_params(daily: pd.DataFrame, params: StrategyParams) -> float:
    fi = force_index(daily["close"], daily["volume"], span=2)
    scale = float(fi.abs().iloc[-params.fi_scale_lookback :].mean())
    if scale <= 0:
        return 0.0
    return _clamp01(abs(float(fi.iloc[-1])) / scale)


def compute_quality_score(
    reward_risk: float | None,
    impulse_agreement: float,
    tide_strength: float,
    pullback_depth: float,
) -> float:
    """Composite 0-1 trade-quality score from Elder's "which setup?" criteria.

    Reward:risk dominates (the book's gatekeeper), then Impulse agreement across
    both screens, the strength of the weekly tide, and how deep the entry pullback
    is. This only *ranks* setups already validated by the Triple Screen — it never
    creates or overrides a signal.
    """
    rr = _clamp01((reward_risk or 0.0) / RR_EXCELLENT)
    tide = _clamp01(tide_strength / STRONG_WEEKLY_SLOPE)
    w = SCORE_WEIGHTS
    return (
        w["reward_risk"] * rr
        + w["impulse"] * impulse_agreement
        + w["tide"] * tide
        + w["pullback"] * _clamp01(pullback_depth)
    )


def compute_quality_score_with_params(
    reward_risk: float | None,
    impulse_agreement: float,
    tide_strength: float,
    pullback_depth: float,
    params: StrategyParams,
) -> float:
    rr = _clamp01((reward_risk or 0.0) / params.rr_excellent)
    tide = _clamp01(tide_strength / params.strong_weekly_slope)
    w = _weights(params)
    return (
        w["reward_risk"] * rr
        + w["impulse"] * impulse_agreement
        + w["tide"] * tide
        + w["pullback"] * _clamp01(pullback_depth)
    )


def select_best(signals: Sequence[Signal]) -> Signal | None:
    """Elder's "which one do I take?": the highest-quality tradable setup that
    clears the 2:1 reward:risk floor. Returns None if nothing qualifies."""
    candidates = [
        s for s in signals if s.action != "stand_aside" and s.rr_ok and s.quality_score is not None
    ]
    if not candidates:
        return None
    # Tie-break on reward:risk, then asset name, for a stable, explainable pick.
    return max(
        candidates,
        key=lambda s: (s.quality_score, s.reward_risk or 0.0, s.asset),
    )


def evaluate_asset(
    asset: str,
    weekly: pd.DataFrame,
    daily: pd.DataFrame,
    lower_timeframe: pd.DataFrame | None = None,
    params: StrategyParams = DEFAULT_PARAMS,
) -> Signal:
    """Run the three screens + Impulse censorship for one asset.

    `weekly` and `daily` must be OHLCV frames of *completed* bars
    (open/high/low/close/volume columns, oldest first).
    """
    w_imp = str(impulse_color(weekly["close"]).iloc[-1])
    d_imp = str(impulse_color(daily["close"]).iloc[-1])
    trend = weekly_trend(weekly["close"], min_slope_pct=params.flat_trend_slope_pct)
    market_regime = "flat" if trend == "neutral" else "trending"
    fi2_series = force_index(daily["close"], daily["volume"], span=2)
    fi2 = float(fi2_series.iloc[-1])
    fi_lookback = params.force_index_extreme_lookback_days
    divergences = detect_divergences(
        daily,
        lookback=params.divergence_lookback,
        min_separation=params.divergence_min_separation,
        max_separation=params.divergence_max_separation,
    )
    vz_status = value_zone_status(daily, params.value_zone_max_distance_pct)

    candidate: Action = "stand_aside"
    if trend == "up":
        if fi2 >= 0:
            reason = "weekly tide up but Force Index not below zero — chasing, stand aside"
        elif force_index_new_extreme(fi2_series, "long", fi_lookback):
            reason = (
                "weekly tide up, but daily Force Index is at a new multi-week low — "
                "the decline is accelerating, not a pullback to buy; stand aside"
            )
        else:
            candidate = "long"
            reason = "weekly tide up, daily 2-EMA Force Index below zero (pullback to buy)"
    elif trend == "down":
        if fi2 <= 0:
            reason = "weekly tide down but Force Index not above zero — stand aside"
        elif force_index_new_extreme(fi2_series, "short", fi_lookback):
            reason = (
                "weekly tide down, but daily Force Index is at a new multi-week high — "
                "the rally is accelerating, not a pullback to sell; stand aside"
            )
        else:
            candidate = "short"
            reason = "weekly tide down, daily 2-EMA Force Index above zero (rally to sell)"
    else:
        reason = "weekly tide neutral — stand aside"

    # Impulse censorship overlay — applied last; it says what NOT to do.
    if candidate == "long" and "red" in (w_imp, d_imp):
        candidate = "stand_aside"
        reason = f"long vetoed by Impulse (weekly={w_imp}, daily={d_imp}: red forbids longs)"
    elif candidate == "short" and "green" in (w_imp, d_imp):
        candidate = "stand_aside"
        reason = f"short vetoed by Impulse (weekly={w_imp}, daily={d_imp}: green forbids shorts)"
    elif candidate in ("long", "short") and vz_status == "extended":
        # The "extended" veto is directional (Elder only warns against *chasing*):
        # veto a long only when price is extended ABOVE value, a short only when
        # extended BELOW. A pullback the other way is a bargain — its falling-knife
        # guard is the Force-Index new-extreme filter above, not this veto.
        zone_side = value_zone_extension(daily)
        chasing = (candidate == "long" and zone_side == "above") or (
            candidate == "short" and zone_side == "below"
        )
        if chasing:
            vetoed = candidate
            candidate = "stand_aside"
            reason = (
                f"{vetoed} vetoed: daily close is extended {zone_side} the EMA13-EMA26 "
                f"value zone (chasing)"
            )

    entry = limit = stop = target = rr = None
    third_impulse = None
    if candidate == "long":
        entry, limit, stop, target = _long_levels(weekly, daily, params)
        entry, third_impulse = _third_screen_levels(candidate, lower_timeframe, entry)
        if third_impulse == "red":
            candidate = "stand_aside"
            reason = "long vetoed by 4h third-screen Impulse red"
            entry = limit = stop = target = rr = None
        elif entry > stop:
            rr = (target - entry) / (entry - stop)
    elif candidate == "short":
        entry, limit, stop, target = _short_levels(weekly, daily, params)
        entry, third_impulse = _third_screen_levels(candidate, lower_timeframe, entry)
        if third_impulse == "green":
            candidate = "stand_aside"
            reason = "short vetoed by 4h third-screen Impulse green"
            entry = limit = stop = target = rr = None
        elif stop > entry:
            rr = (entry - target) / (stop - entry)

    # Stop & reward:risk for the limit (pullback) entry — recalibrated to that
    # fill, since a deep pullback can clear the breakout stop.
    limit_stop = limit_rr = None
    if candidate in ("long", "short") and limit is not None and target is not None:
        limit_stop = safezone_stop_for_limit(daily, candidate, limit, params)
        if candidate == "long" and limit > limit_stop:
            limit_rr = (target - limit) / (limit - limit_stop)
        elif candidate == "short" and limit_stop > limit:
            limit_rr = (limit - target) / (limit_stop - limit)

    tide_strength = weekly_slope_strength(weekly["close"])
    score = None
    pull = 0.0
    if candidate in ("long", "short"):
        pull = pullback_quality_with_params(daily, params)
        score = compute_quality_score_with_params(
            rr, impulse_confirmation(candidate, w_imp, d_imp), tide_strength, pull, params
        )
    order_plan = theoretical_entry_order_plan(candidate, entry, params)

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
        rr_ok=rr is not None and rr >= params.min_reward_risk,
        weekly_trend_strength=tide_strength,
        pullback_quality=pull,
        quality_score=score,
        market_regime=market_regime,
        third_screen_impulse=third_impulse,
        divergences=divergences,
        value_zone_status=vz_status,
        entry_order_plan=order_plan,
        entry_limit_stop=limit_stop,
        reward_risk_limit=limit_rr,
    )
