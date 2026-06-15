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

# --- Trade-quality ranking ("which setup is best?", Elder's selection logic) ---
# Reward:risk is Elder's gatekeeper (2:1 floor); 3:1 or better earns full credit.
RR_EXCELLENT = 3.0
# A weekly EMA13 sloping ~3% per bar is already a strong tide; cap the score there.
STRONG_WEEKLY_SLOPE = 0.03
# Window for scaling the daily Force-Index pullback into a 0-1 "depth".
FI_SCALE_LOOKBACK = 20
# Composite weights (sum to 1): reward:risk dominates, per the book.
SCORE_WEIGHTS = {"reward_risk": 0.40, "impulse": 0.25, "tide": 0.20, "pullback": 0.15}


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
    """(upper, lower) weekly channel around EMA13 — Elder's percentage envelope.

    Used as a fallback target when price already trades beyond the weekly value
    zone. The half-widths are the average **relative** excursion of weekly highs
    above / lows below the EMA (penetration / EMA at that bar) over the channel
    lookback, projected onto today's EMA.

    Measuring the excursion as a *ratio* (not an absolute price distance) keeps the
    channel proportional to today's price. An absolute offset, averaged over many
    weeks, mixes in penetrations from when the asset traded far higher; for a coin
    that has since crashed those stale, oversized distances could exceed today's
    small EMA and push the lower band — i.e. a short's target — below zero. A
    ratio whose mean is always < 1 keeps the lower band strictly positive.
    """
    e = ema(weekly["close"], span)
    window = slice(-CHANNEL_LOOKBACK_WEEKS, None)
    up = ((weekly["high"] - e) / e).clip(lower=0).iloc[window]
    down = ((e - weekly["low"]) / e).clip(lower=0).iloc[window]
    up = up[up > 0]
    down = down[down > 0]
    last = float(e.iloc[-1])
    upper = last * (1.0 + (float(up.mean()) if not up.empty else 0.0))
    lower = last * (1.0 - (float(down.mean()) if not down.empty else 0.0))
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

    tide_strength = weekly_slope_strength(weekly["close"])
    score = None
    pull = 0.0
    if candidate in ("long", "short"):
        pull = pullback_quality(daily)
        score = compute_quality_score(
            rr, impulse_confirmation(candidate, w_imp, d_imp), tide_strength, pull
        )

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
        weekly_trend_strength=tide_strength,
        pullback_quality=pull,
        quality_score=score,
    )
