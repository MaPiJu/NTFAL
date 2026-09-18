"""Elder's Triple Screen + Impulse, on any timeframe chain.

The three screens are named by *role*, never by a fixed interval, so the same
logic serves a weekly tide or a 4h one (see config.toml's `[[scanner.horizons]]`):

  - **tide**  (1st screen) — the strategic bias: slope of the tide EMA13.
  - **wave**  (2nd screen) — the counter-trend oscillator that finds entries
    *against* the short-term wave but *with* the tide.
  - **entry** (3rd screen) — lower-timeframe timing for the stop-entry order.

Decision table (CLAUDE.md, canonical):
  tide up   + wave FI(2) below zero  -> long
  tide up   + FI rising/above zero   -> stand aside (chasing)
  tide down + wave FI(2) above zero  -> short
  tide down + FI falling/below zero  -> stand aside

Impulse censorship is applied LAST and covers **screens 1 and 2 only**: any red
(tide or wave) forbids longs, any green forbids shorts. The third screen is an
*entry technique*, not a veto — the Triple Screen buys into a pullback, which is
exactly when the lower timeframe prints red, so censoring on it would cancel the
very setups the second screen just found. Its color is carried as context.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
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
DEFAULT_PARAMS = StrategyParams()
# Role -> interval labels, used only for human-readable reasons/plans.
DEFAULT_INTERVALS = {"tide": "tide", "wave": "wave", "entry": "entry"}


@dataclass(frozen=True)
class Signal:
    asset: str
    action: Action
    reason: str
    tide_trend: Trend
    tide_impulse: str
    wave_impulse: str
    force_index_2: float
    entry: float | None  # stop-entry trigger (1 tick beyond the prior bar's extreme)
    entry_limit: float | None  # pullback limit at projected EMA13 -/+ avg penetration
    stop: float | None
    target: float | None
    reward_risk: float | None
    rr_ok: bool
    tide_strength: float  # scale-free |slope| of the tide EMA13
    pullback_quality: float  # 0-1 depth of the latest wave Force-Index pullback
    quality_score: float | None  # composite Elder rank, None when standing aside
    market_regime: str  # trending / flat, derived from the tide EMA13 slope filter
    entry_impulse: str | None  # 3rd-screen Impulse — context for the operator, never a veto
    divergences: list[str]  # Elder MACD-Histogram / Force Index divergence warnings
    value_zone_status: str  # in_value / near_value / extended
    entry_order_plan: str | None  # how to roll/expire the theoretical stop-entry
    # Second entry technique: stop & reward:risk for the limit (pullback) fill,
    # which differ from the breakout entry's. None when there is no limit entry.
    entry_limit_stop: float | None = None
    reward_risk_limit: float | None = None
    # --- horizon + data-quality context ---------------------------------------
    horizon: str = ""  # which timeframe chain produced this signal
    intervals: dict[str, str] = field(default_factory=dict)  # role -> interval
    tide_bars: int = 0
    wave_bars: int = 0
    # Why this signal may be less trustworthy than it looks (short history, a
    # near-frozen market). Data checks, never trading indicators.
    data_warnings: list[str] = field(default_factory=list)


def tick_size(price: float) -> float:
    """One price tick, assuming 5-significant-figure Hyperliquid quoting."""
    if price <= 0:
        raise ValueError("price must be positive")
    return 10.0 ** (math.floor(math.log10(price)) - (PRICE_SIG_FIGS - 1))


def tide_trend(
    tide_close: pd.Series,
    span: int = EMA_FAST,
    min_slope_pct: float = DEFAULT_PARAMS.flat_trend_slope_pct,
) -> Trend:
    """First screen: the tide = slope of the tide-timeframe EMA13.

    Tiny EMA slopes are classified as neutral so sideways markets do not become
    accidental up/down trends because of floating-point noise or one small bar.
    """
    e = ema(tide_close, span)
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
    wave: pd.DataFrame,
    side: Literal["down", "up"],
    span: int = EMA_FAST,
    lookback: int = DEFAULT_PARAMS.penetration_lookback_bars,
) -> float | None:
    """Average distance pullbacks pierce the wave EMA13 over the last `lookback` bars.

    side="down": how far lows dip below the EMA (for longs in an uptrend);
    side="up":   how far highs poke above the EMA (for shorts in a downtrend).
    Bars without a penetration are ignored; returns None if there were none.
    """
    e = ema(wave["close"], span)
    raw = e - wave["low"] if side == "down" else wave["high"] - e
    pen = raw.clip(lower=0).iloc[-lookback:]
    pen = pen[pen > 0]
    if pen.empty:
        return None
    return float(pen.mean())


def force_index_new_extreme(
    fi2: pd.Series,
    side: Literal["long", "short"],
    lookback: int = DEFAULT_PARAMS.force_index_extreme_lookback_bars,
) -> bool:
    """Elder's second-screen caveat (book p.158): the wave Force Index signal is
    void when FI(2) prints a *new multi-period extreme*.

    A buy signal is valid only while the 2-bar Force Index dips below zero "as
    long as it doesn't fall to a new multi-week low" — a fresh low means the
    decline is accelerating, not a pullback to buy (p.131). Mirror image for
    shorts and new highs. Returns True when the latest FI(2) breaks the prior
    `lookback` bars' extreme, i.e. the signal should be skipped.
    """
    if len(fi2) < 2:
        return False
    now = float(fi2.iloc[-1])
    prior = fi2.iloc[-(lookback + 1) : -1]
    if prior.empty:
        return False
    return now < float(prior.min()) if side == "long" else now > float(prior.max())


def value_zone_status(wave: pd.DataFrame, max_distance_pct: float = 0.03) -> str:
    """Classify the last wave close versus Elder's wave EMA13-EMA26 value zone.

    The Triple Screen should enter on pullbacks to value, not after price has
    already run away. "Near" allows a small configurable overshoot so strong
    trends are not rejected for being a few ticks outside the zone.
    """
    close = float(wave["close"].iloc[-1])
    e13 = float(ema(wave["close"], EMA_FAST).iloc[-1])
    e26 = float(ema(wave["close"], EMA_SLOW).iloc[-1])
    low, high = sorted((e13, e26))
    if low <= close <= high:
        return "in_value"
    if close > high:
        return "near_value" if (close - high) / high <= max_distance_pct else "extended"
    return "near_value" if (low - close) / low <= max_distance_pct else "extended"


def value_zone_extension(wave: pd.DataFrame) -> Literal["above", "below", "inside"]:
    """Which side of the wave EMA13-EMA26 value zone the last close sits on.

    Makes the "extended" veto directional. Elder only warns against *chasing* —
    buying after price has run above value, or shorting after it has broken below
    value. A pullback extended the *other* way (a long far below value, a short
    far above) is a bargain, so it must not be vetoed by the value-zone filter.
    """
    close = float(wave["close"].iloc[-1])
    e13 = float(ema(wave["close"], EMA_FAST).iloc[-1])
    e26 = float(ema(wave["close"], EMA_SLOW).iloc[-1])
    low, high = sorted((e13, e26))
    if close > high:
        return "above"
    if close < low:
        return "below"
    return "inside"


def average_adverse_noise(
    wave: pd.DataFrame,
    side: Literal["long", "short"],
    lookback: int,
) -> float | None:
    """Elder SafeZone noise: average adverse one-bar extreme expansion.

    Longs care about downside noise (this bar's low undercuts the previous low).
    Shorts care about upside noise (this bar's high exceeds the previous high).
    Bars without adverse noise are ignored, matching SafeZone's intent to measure
    normal counter-trend noise instead of all volatility.
    """
    if side == "long":
        noise = (wave["low"].shift(1) - wave["low"]).clip(lower=0)
    else:
        noise = (wave["high"] - wave["high"].shift(1)).clip(lower=0)
    noise = noise.dropna().iloc[-lookback:]
    noise = noise[noise > 0]
    if noise.empty:
        return None
    return float(noise.mean())


def _safezone_stop_from_base(
    wave: pd.DataFrame,
    side: Literal["long", "short"],
    base: float,
    params: StrategyParams = DEFAULT_PARAMS,
) -> float:
    """SafeZone adverse-noise buffer applied below/above a given anchor `base`."""
    noise = average_adverse_noise(wave, side, params.safezone_lookback_bars)
    factor = params.safezone_factor_long if side == "long" else params.safezone_factor_short
    offset = (noise * factor) if noise is not None else tick_size(base)
    return base - offset if side == "long" else base + offset


def safezone_initial_stop(
    wave: pd.DataFrame,
    side: Literal["long", "short"],
    params: StrategyParams = DEFAULT_PARAMS,
) -> float:
    """Initial protective stop using Elder's SafeZone adverse-noise method.

    Anchored to the recent wave extreme (2-bar low for longs, high for shorts) —
    the stop that pairs with the buy-stop / sell-stop (breakout) entry.
    """
    if side == "long":
        base = float(wave["low"].iloc[-2:].min())
    else:
        base = float(wave["high"].iloc[-2:].max())
    return _safezone_stop_from_base(wave, side, base, params)


def safezone_stop_for_limit(
    wave: pd.DataFrame,
    side: Literal["long", "short"],
    limit: float,
    params: StrategyParams = DEFAULT_PARAMS,
) -> float:
    """SafeZone stop recalibrated to a limit (pullback) fill.

    A deep pullback can fill *below* the recent wave low (long) — beyond the
    breakout stop — so the SafeZone buffer is anchored to whichever is more
    protective, the recent extreme or the limit fill itself. This keeps the stop
    on the correct side of the limit entry, and collapses to the breakout stop
    when the limit sits inside the recent range.
    """
    if side == "long":
        base = min(float(wave["low"].iloc[-2:].min()), limit)
    else:
        base = max(float(wave["high"].iloc[-2:].max()), limit)
    return _safezone_stop_from_base(wave, side, base, params)


def theoretical_entry_order_plan(
    action: Action,
    entry: float | None,
    params: StrategyParams = DEFAULT_PARAMS,
    wave_label: str = "wave",
) -> str | None:
    """Human-readable lifecycle for the stop-entry order Elder would trail."""
    if action not in ("long", "short") or entry is None:
        return None
    direction = "buy-stop" if action == "long" else "sell-stop"
    return (
        f"Place a theoretical {direction} at {entry:.6g}; if not filled, roll it each "
        f"{wave_label} bar to the latest completed bar's "
        f"{'high + 1 tick' if action == 'long' else 'low - 1 tick'} "
        f"while the tide, Force Index pullback, value-zone filter and Impulse veto "
        f"remain valid; expire after {params.entry_order_expire_bars} completed "
        f"{wave_label} bars."
    )


def projected_ema(wave_close: pd.Series, span: int = EMA_FAST) -> float:
    """The next bar's EMA estimate: today_EMA + (today_EMA - yesterday_EMA)."""
    e = ema(wave_close, span)
    if len(e) < 2:
        return float(e.iloc[-1])
    return float(2 * e.iloc[-1] - e.iloc[-2])


def channel(
    tide: pd.DataFrame,
    params: StrategyParams = DEFAULT_PARAMS,
    span: int = EMA_SLOW,
) -> tuple[float, float]:
    """(upper, lower) tide channel around the slow EMA26 — Elder's percentage
    envelope (p.183), used as a fallback target when price already trades beyond
    the tide value zone.

    Elder draws the channel parallel to the *slower* EMA and widens it until it
    contains ~95% of recent bars. Each half-width is the `containment`-quantile of
    the **relative** excursion of tide highs above / lows below the EMA
    (penetration / EMA at that bar) over the lookback, projected onto the latest EMA.

    Measuring the excursion as a *ratio* (not an absolute price distance) keeps the
    channel proportional to the current price and the lower band strictly positive
    even for a market that has since crashed — a deliberate 24/7 adaptation that
    fits the two sides independently rather than as one symmetric coefficient.
    """
    e = ema(tide["close"], span)
    window = slice(-params.channel_lookback_bars, None)
    # Relative excursion of each bar's high above / low below the EMA (0 when the
    # bar doesn't poke out). The containment-quantile leaves ~(1-containment) of
    # bars outside the channel — Elder's "contains ~95% of bars" fit (p.183).
    up = ((tide["high"] - e) / e).clip(lower=0).iloc[window]
    down = ((e - tide["low"]) / e).clip(lower=0).iloc[window]
    last = float(e.iloc[-1])
    containment = params.channel_containment
    upper = last * (1.0 + (float(up.quantile(containment)) if not up.empty else 0.0))
    lower = last * (1.0 - (float(down.quantile(containment)) if not down.empty else 0.0))
    return upper, lower


def _long_levels(
    tide: pd.DataFrame, wave: pd.DataFrame, params: StrategyParams = DEFAULT_PARAMS
) -> tuple[float, float | None, float, float]:
    """(entry, entry_limit, stop, target) for a long setup."""
    prior_high = float(wave["high"].iloc[-1])
    tick = tick_size(prior_high)
    entry = prior_high + tick  # buy-stop 1 tick above the prior bar's high

    pen = average_penetration(wave, "down", lookback=params.penetration_lookback_bars)
    limit = projected_ema(wave["close"]) - pen if pen is not None else None

    stop = safezone_initial_stop(wave, "long", params)

    # Target: tide value zone (between EMA13 and EMA26); if price already
    # trades above value, fall back to the tide upper channel.
    e13 = float(ema(tide["close"], EMA_FAST).iloc[-1])
    e26 = float(ema(tide["close"], EMA_SLOW).iloc[-1])
    value_high = max(e13, e26)
    target = value_high if value_high > entry else channel(tide, params)[0]
    return entry, limit, stop, target


def _short_levels(
    tide: pd.DataFrame, wave: pd.DataFrame, params: StrategyParams = DEFAULT_PARAMS
) -> tuple[float, float | None, float, float]:
    """(entry, entry_limit, stop, target) for a short setup."""
    prior_low = float(wave["low"].iloc[-1])
    tick = tick_size(prior_low)
    entry = prior_low - tick  # sell-stop 1 tick below the prior bar's low

    pen = average_penetration(wave, "up", lookback=params.penetration_lookback_bars)
    limit = projected_ema(wave["close"]) + pen if pen is not None else None

    stop = safezone_initial_stop(wave, "short", params)

    e13 = float(ema(tide["close"], EMA_FAST).iloc[-1])
    e26 = float(ema(tide["close"], EMA_SLOW).iloc[-1])
    value_low = min(e13, e26)
    target = value_low if value_low < entry else channel(tide, params)[1]
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
    lookback: int = DEFAULT_PARAMS.divergence_lookback,
    min_separation: int = DEFAULT_PARAMS.divergence_min_separation,
    max_separation: int = DEFAULT_PARAMS.divergence_max_separation,
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
    wave: pd.DataFrame,
    lookback: int = DEFAULT_PARAMS.divergence_lookback,
    min_separation: int = DEFAULT_PARAMS.divergence_min_separation,
    max_separation: int = DEFAULT_PARAMS.divergence_max_separation,
) -> list[str]:
    """Recent Elder divergence warnings from MACD-Histogram and 13-EMA Force Index."""
    close = wave["close"]
    volume = wave["volume"]
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


def data_warnings(
    tide: pd.DataFrame,
    wave: pd.DataFrame,
    params: StrategyParams = DEFAULT_PARAMS,
    min_tide_bars: int = 0,
    intervals: dict[str, str] | None = None,
) -> list[str]:
    """Reasons this signal's *inputs* are weaker than they look.

    These are data checks, not trading indicators (CLAUDE.md: "less is more") —
    they never change an action, they only tell the operator how much to trust it:

    1. **Short tide history.** The EMA26 is seeded at the first bar (adjust=False),
       so with few bars a large share of it is still that seed: the tide direction
       and its Impulse are not yet meaningful. ~60 bars puts the seed under 1%.
    2. **Near-frozen market.** A tradfi perp over the weekend still prints bars,
       but on a fraction of normal volume and range. Force Index is volume-scaled,
       so those bars flatten every indicator; an intraday signal read off them is
       noise, not a market.
    """
    labels = intervals or DEFAULT_INTERVALS
    out: list[str] = []

    bars = len(tide)
    if min_tide_bars and bars < min_tide_bars:
        out.append(
            f"only {bars} {labels['tide']} bars of history (<{min_tide_bars}): the EMA26 "
            f"is not converged, so the tide and its Impulse are unreliable"
        )

    vol = wave["volume"].dropna()
    recent = vol.iloc[-params.low_volume_bars :]
    baseline = float(vol.iloc[-params.divergence_lookback :].median()) if len(vol) else 0.0
    if len(recent) and baseline > 0:
        ratio = float(recent.mean()) / baseline
        if ratio < params.low_volume_ratio:
            out.append(
                f"last {len(recent)} {labels['wave']} bars traded at {ratio:.0%} of normal "
                f"volume — market near-closed (weekend / out of session), signal unreliable"
            )
    return out


def _entry_screen_levels(
    action: Action, entry_frame: pd.DataFrame | None, fallback_entry: float
) -> tuple[float, str | None]:
    """Third screen: lower-timeframe trigger for the stop-entry order.

    When entry-timeframe bars are supplied, time the stop-entry from the latest
    completed bar there instead of the wave bar — a tighter trigger, closer to the
    stop. The bar's Impulse color is returned as *context only*: censorship is a
    screens-1-and-2 rule (see the module docstring), never applied here.
    """
    if entry_frame is None or entry_frame.empty:
        return fallback_entry, None
    imp = str(impulse_color(entry_frame["close"]).iloc[-1])
    if action == "long":
        px = float(entry_frame["high"].iloc[-1])
        return px + tick_size(px), imp
    if action == "short":
        px = float(entry_frame["low"].iloc[-1])
        return px - tick_size(px), imp
    return fallback_entry, imp


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def tide_slope_strength(tide_close: pd.Series, span: int = EMA_FAST) -> float:
    """Scale-free strength of the tide: |EMA13 now - previous| / EMA13.

    A ratio so a $0.05 alt and $60k BTC are comparable; 0 if the EMA is flat.
    """
    e = ema(tide_close, span)
    if len(e) < 2 or e.iloc[-1] == 0:
        return 0.0
    return abs(float(e.iloc[-1] - e.iloc[-2])) / abs(float(e.iloc[-1]))


def impulse_confirmation(action: Action, tide_impulse: str, wave_impulse: str) -> float:
    """Fraction of screens whose Impulse actively confirms the trade (0, .5, 1).

    Longs are confirmed by green Impulse, shorts by red, counted on tide + wave
    then averaged. (Censorship has already removed the vetoing color, so this only
    rewards positive agreement — a blue/neutral screen scores nothing.)
    """
    favorable = "green" if action == "long" else "red"
    return ((tide_impulse == favorable) + (wave_impulse == favorable)) / 2.0


def pullback_quality(wave: pd.DataFrame, params: StrategyParams = DEFAULT_PARAMS) -> float:
    """How stretched the latest wave Force-Index pullback is, scaled 0-1.

    |FI(2)| is measured against this asset's own recent average |FI(2)|, so the
    depth is comparable across assets. Elder enters on a pullback to value; a
    deeper-than-usual pullback (larger |FI|) is the better entry.
    """
    fi = force_index(wave["close"], wave["volume"], span=2)
    scale = float(fi.abs().iloc[-params.fi_scale_lookback :].mean())
    if scale <= 0:
        return 0.0
    return _clamp01(abs(float(fi.iloc[-1])) / scale)


def compute_quality_score(
    reward_risk: float | None,
    impulse_agreement: float,
    tide_strength: float,
    pullback_depth: float,
    params: StrategyParams = DEFAULT_PARAMS,
) -> float:
    """Composite 0-1 trade-quality score from Elder's "which setup?" criteria.

    Reward:risk dominates (the book's gatekeeper), then Impulse agreement across
    both screens, the strength of the tide, and how deep the entry pullback is.
    This only *ranks* setups already validated by the Triple Screen — it never
    creates or overrides a signal.
    """
    rr = _clamp01((reward_risk or 0.0) / params.rr_excellent)
    tide = _clamp01(tide_strength / params.strong_tide_slope)
    return (
        params.score_reward_risk_weight * rr
        + params.score_impulse_weight * impulse_agreement
        + params.score_tide_weight * tide
        + params.score_pullback_weight * _clamp01(pullback_depth)
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
    tide: pd.DataFrame,
    wave: pd.DataFrame,
    entry_frame: pd.DataFrame | None = None,
    params: StrategyParams = DEFAULT_PARAMS,
    *,
    horizon: str = "",
    intervals: dict[str, str] | None = None,
    min_tide_bars: int = 0,
) -> Signal:
    """Run the three screens + Impulse censorship for one asset on one horizon.

    `tide` and `wave` must be OHLCV frames of *completed* bars (open/high/low/
    close/volume columns, oldest first); `entry_frame` is the optional third
    screen's lower-timeframe bars.
    """
    labels = intervals or DEFAULT_INTERVALS
    t_imp = str(impulse_color(tide["close"]).iloc[-1])
    w_imp = str(impulse_color(wave["close"]).iloc[-1])
    trend = tide_trend(tide["close"], min_slope_pct=params.flat_trend_slope_pct)
    market_regime = "flat" if trend == "neutral" else "trending"
    fi2_series = force_index(wave["close"], wave["volume"], span=2)
    fi2 = float(fi2_series.iloc[-1])
    fi_lookback = params.force_index_extreme_lookback_bars
    divergences = detect_divergences(
        wave,
        lookback=params.divergence_lookback,
        min_separation=params.divergence_min_separation,
        max_separation=params.divergence_max_separation,
    )
    vz_status = value_zone_status(wave, params.value_zone_max_distance_pct)

    candidate: Action = "stand_aside"
    if trend == "up":
        if fi2 >= 0:
            reason = "tide up but Force Index not below zero — chasing, stand aside"
        elif force_index_new_extreme(fi2_series, "long", fi_lookback):
            reason = (
                "tide up, but the wave Force Index is at a new multi-period low — "
                "the decline is accelerating, not a pullback to buy; stand aside"
            )
        else:
            candidate = "long"
            reason = "tide up, wave 2-EMA Force Index below zero (pullback to buy)"
    elif trend == "down":
        if fi2 <= 0:
            reason = "tide down but Force Index not above zero — stand aside"
        elif force_index_new_extreme(fi2_series, "short", fi_lookback):
            reason = (
                "tide down, but the wave Force Index is at a new multi-period high — "
                "the rally is accelerating, not a pullback to sell; stand aside"
            )
        else:
            candidate = "short"
            reason = "tide down, wave 2-EMA Force Index above zero (rally to sell)"
    else:
        reason = "tide neutral — stand aside"

    # Impulse censorship overlay — applied last; it says what NOT to do. Scoped
    # to screens 1 and 2 (CLAUDE.md); the third screen never censors.
    if candidate == "long" and "red" in (t_imp, w_imp):
        candidate = "stand_aside"
        reason = f"long vetoed by Impulse (tide={t_imp}, wave={w_imp}: red forbids longs)"
    elif candidate == "short" and "green" in (t_imp, w_imp):
        candidate = "stand_aside"
        reason = f"short vetoed by Impulse (tide={t_imp}, wave={w_imp}: green forbids shorts)"
    elif candidate in ("long", "short") and vz_status == "extended":
        # The "extended" veto is directional (Elder only warns against *chasing*):
        # veto a long only when price is extended ABOVE value, a short only when
        # extended BELOW. A pullback the other way is a bargain — its falling-knife
        # guard is the Force-Index new-extreme filter above, not this veto.
        zone_side = value_zone_extension(wave)
        chasing = (candidate == "long" and zone_side == "above") or (
            candidate == "short" and zone_side == "below"
        )
        if chasing:
            vetoed = candidate
            candidate = "stand_aside"
            reason = (
                f"{vetoed} vetoed: the {labels['wave']} close is extended {zone_side} the "
                f"EMA13-EMA26 value zone (chasing)"
            )

    entry = limit = stop = target = rr = None
    entry_impulse = None
    if candidate == "long":
        entry, limit, stop, target = _long_levels(tide, wave, params)
        entry, entry_impulse = _entry_screen_levels(candidate, entry_frame, entry)
        if entry > stop:
            rr = (target - entry) / (entry - stop)
    elif candidate == "short":
        entry, limit, stop, target = _short_levels(tide, wave, params)
        entry, entry_impulse = _entry_screen_levels(candidate, entry_frame, entry)
        if stop > entry:
            rr = (entry - target) / (stop - entry)

    # Stop & reward:risk for the limit (pullback) entry — recalibrated to that
    # fill, since a deep pullback can clear the breakout stop.
    limit_stop = limit_rr = None
    if candidate in ("long", "short") and limit is not None and target is not None:
        limit_stop = safezone_stop_for_limit(wave, candidate, limit, params)
        if candidate == "long" and limit > limit_stop:
            limit_rr = (target - limit) / (limit - limit_stop)
        elif candidate == "short" and limit_stop > limit:
            limit_rr = (limit - target) / (limit_stop - limit)

    strength = tide_slope_strength(tide["close"])
    score = None
    pull = 0.0
    if candidate in ("long", "short"):
        pull = pullback_quality(wave, params)
        score = compute_quality_score(
            rr, impulse_confirmation(candidate, t_imp, w_imp), strength, pull, params
        )
    order_plan = theoretical_entry_order_plan(candidate, entry, params, labels["wave"])

    return Signal(
        asset=asset,
        action=candidate,
        reason=reason,
        tide_trend=trend,
        tide_impulse=t_imp,
        wave_impulse=w_imp,
        force_index_2=fi2,
        entry=entry,
        entry_limit=limit,
        stop=stop,
        target=target,
        reward_risk=rr,
        rr_ok=rr is not None and rr >= params.min_reward_risk,
        tide_strength=strength,
        pullback_quality=pull,
        quality_score=score,
        market_regime=market_regime,
        entry_impulse=entry_impulse,
        divergences=divergences,
        value_zone_status=vz_status,
        entry_order_plan=order_plan,
        entry_limit_stop=limit_stop,
        reward_risk_limit=limit_rr,
        horizon=horizon,
        intervals=dict(labels),
        tide_bars=len(tide),
        wave_bars=len(wave),
        data_warnings=data_warnings(tide, wave, params, min_tide_bars, labels),
    )
