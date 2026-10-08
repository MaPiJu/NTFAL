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
# Hyperliquid perp prices: at most 5 significant figures and at most
# MAX_PRICE_DECIMALS - szDecimals decimals; integer prices are always valid.
PRICE_SIG_FIGS = 5
MAX_PRICE_DECIMALS = 6
DEFAULT_PARAMS = StrategyParams()
# Role -> interval labels, used only for human-readable reasons/plans.
DEFAULT_INTERVALS = {"tide": "tide", "wave": "wave", "entry": "entry"}


@dataclass(frozen=True)
class ApgarLine:
    question: str  # what is graded, e.g. "tide Impulse"
    answer: str  # what was observed, e.g. "green", "below value", "2.40"
    score: int  # 0, 1 or 2


@dataclass(frozen=True)
class TradeApgar:
    """Elder's Trade Apgar (p.238-242) for this system — "pullback to value".

    Five questions scored 0/1/2; an **A-trade** totals 7 or more with no line at
    zero (p.239: "only healthy ideas whose score is 7 or higher, and not a single
    line rated zero"). It ranks setups the Triple Screen already validated; it
    never creates or vetoes one.
    """

    lines: list[ApgarLine]
    total: int
    a_trade: bool


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
    market_regime: str  # trending / flat, derived from the tide EMA13 slope filter
    entry_impulse: str | None  # 3rd-screen Impulse — context for the operator, never a veto
    divergences: list[str]  # Elder MACD-Histogram / Force Index divergence warnings
    value_zone_status: str  # in_value / near_value / extended (beyond the wave channel)
    entry_order_plan: str | None  # how to trail the theoretical stop-entry, and when to cancel it
    # Second entry technique: stop & reward:risk for the limit (pullback) fill,
    # which differ from the breakout entry's. None when there is no limit entry.
    entry_limit_stop: float | None = None
    reward_risk_limit: float | None = None
    # Trade Apgar — how this setup ranks; None when standing aside.
    apgar: TradeApgar | None = None
    # --- horizon + data-quality context ---------------------------------------
    horizon: str = ""  # which timeframe chain produced this signal
    intervals: dict[str, str] = field(default_factory=dict)  # role -> interval
    tide_bars: int = 0
    wave_bars: int = 0
    # Why this signal may be less trustworthy than it looks (short history, a
    # near-frozen market). Data checks, never trading indicators.
    data_warnings: list[str] = field(default_factory=list)


def tick_size(price: float, sz_decimals: int | None = None) -> float:
    """One price tick under Hyperliquid's perp price rules.

    At most 5 significant figures — but integer prices are always valid, so the
    tick never exceeds 1 — and, when the asset's `szDecimals` is known, at most
    6 - szDecimals decimals.
    """
    if price <= 0:
        raise ValueError("price must be positive")
    tick = min(10.0 ** (math.floor(math.log10(price)) - (PRICE_SIG_FIGS - 1)), 1.0)
    if sz_decimals is not None:
        tick = max(tick, 10.0 ** -(MAX_PRICE_DECIMALS - sz_decimals))
    return tick


def round_to_tick(
    price: float, direction: Literal["up", "down"], sz_decimals: int | None = None
) -> float:
    """`price` on Hyperliquid's price grid, rounded `direction`.

    A price already on the grid (up to float noise, e.g. 4126.3 + 0.1) stays put.
    Non-positive prices have no grid and are returned unchanged.
    """
    if price <= 0:
        return price
    tick = tick_size(price, sz_decimals)
    steps = price / tick
    nearest = round(steps)
    if abs(steps - nearest) < 1e-6:
        steps_on_grid = nearest
    else:
        steps_on_grid = math.ceil(steps) if direction == "up" else math.floor(steps)
    # The tick is a power of ten: round away the float noise of the product.
    return round(steps_on_grid * tick, max(0, round(-math.log10(tick))))


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
    """Average depth of the pullbacks through the wave EMA13 over the last `lookback` bars.

    side="down": how far lows dip below the EMA (for longs in an uptrend);
    side="up":   how far highs poke above the EMA (for shorts in a downtrend).
    Elder measures each pullback once (Fig. 39.3, p.159-160: occasions A-D): every
    run of consecutive piercing bars is one pullback, measured at its deepest bar,
    and those depths are averaged. Returns None if nothing pierced the EMA.
    """
    e = ema(wave["close"], span)
    raw = e - wave["low"] if side == "down" else wave["high"] - e
    pen = raw.clip(lower=0).iloc[-lookback:]
    pierced = pen > 0
    if not pierced.any():
        return None
    # A new pullback starts on each piercing bar that follows a non-piercing one.
    pullback_id = (pierced & ~pierced.shift(1, fill_value=False)).cumsum()
    return float(pen[pierced].groupby(pullback_id[pierced]).max().mean())


def force_index_new_extreme(
    fi2: pd.Series,
    side: Literal["long", "short"],
    lookback: int = DEFAULT_PARAMS.force_index_extreme_lookback_bars,
) -> bool:
    """Elder's second-screen caveat (book p.158): the wave Force Index signal is
    void when FI(2) prints a *new multi-period extreme*.

    A buy signal is valid only while the 2-bar Force Index dips below zero "as
    long as it doesn't fall to a new multi-week low" — a fresh low means the
    decline is accelerating, not a pullback to buy (p.115). Mirror image for
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


def value_zone_status(wave: pd.DataFrame, params: StrategyParams = DEFAULT_PARAMS) -> str:
    """Classify the last wave close versus Elder's wave EMA13-EMA26 value zone.

    "in_value" inside the zone; "near_value" outside it but inside the wave
    channel; "extended" beyond the wave channel line. Context for the operator
    only — the chasing veto (`evaluate_asset`) reads the same channel line, but
    only in the trade's direction.
    """
    side = value_zone_extension(wave)
    if side == "inside":
        return "in_value"
    close = float(wave["close"].iloc[-1])
    upper, lower = channel(wave, params)
    beyond = close > upper if side == "above" else close < lower
    return "extended" if beyond else "near_value"


def value_zone_extension(wave: pd.DataFrame) -> Literal["above", "below", "inside"]:
    """Which side of the wave EMA13-EMA26 value zone the last close sits on.

    Elder only warns against *chasing* — buying after price has run above value,
    or shorting after it has broken below value. A pullback extended the *other*
    way (a long far below value, a short far above) is a bargain.
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
    wave_label: str = "wave",
    tide_label: str = "tide",
) -> str | None:
    """Human-readable lifecycle for the stop-entry order Elder would trail.

    Elder (p.161) keeps lowering the buy-stop each day "until stopped in or until
    the weekly indicator reverses and cancels its buy signal" — no fixed expiry:
    the order lives as long as the tide and the Impulse censorship allow the trade.
    """
    if action not in ("long", "short") or entry is None:
        return None
    if action == "long":
        direction, move, level, trend, color = "buy-stop", "lower", "high + 1 tick", "up", "red"
    else:
        direction, move, level, trend, color = "sell-stop", "raise", "low - 1 tick", "down", "green"
    return (
        f"Place a theoretical {direction} at {entry:.6g}; if not filled, {move} it each "
        f"{wave_label} bar to the latest completed bar's {level}. It stays valid until "
        f"filled as long as the {tide_label} tide stays {trend} and no {color} Impulse "
        f"(tide or wave) censors the {action}; cancel it when either fails."
    )


def projected_ema(wave_close: pd.Series, span: int = EMA_FAST) -> float:
    """The next bar's EMA estimate: today_EMA + (today_EMA - yesterday_EMA)."""
    e = ema(wave_close, span)
    if len(e) < 2:
        return float(e.iloc[-1])
    return float(2 * e.iloc[-1] - e.iloc[-2])


def channel(
    bars: pd.DataFrame,
    params: StrategyParams = DEFAULT_PARAMS,
    span: int = EMA_SLOW,
) -> tuple[float, float]:
    """(upper, lower) channel around the slow EMA26 — Elder's symmetrical channel
    (p.167). On the tide it is the fallback target when price already trades
    beyond the tide value zone; on the wave it is the chasing veto (p.168: never
    buy above the upper line, never sell short below the lower one).

    Elder draws the channel parallel to the *slower* EMA with ONE coefficient k:
    upper = EMA·(1 + k), lower = EMA·(1 − k), adjusted until it contains ~95% of
    the past 100 bars (p.79, p.167; "between 90% and 95%", p.226). k is the
    smallest coefficient that keeps at least `channel_containment` of the last
    `channel_lookback_bars` bars inside — all the history there is when shorter —
    each bar measured against its own EMA: high ≤ EMA·(1 + k) and low ≥ EMA·(1 − k).
    The lines are then drawn around the latest EMA.

    A ratio (not a price distance) keeps the channel proportional to price, and
    the lower line positive after a crash: a low below its EMA pokes out by less
    than 100% of it. Only highs more than doubling their own EMA — a parabolic
    market — can push k to 100% or more; the lower line is then floored at 0,
    which is no price: callers treat it as "no lower line".
    """
    e = ema(bars["close"], span)
    # How far each bar pokes out of its own EMA, either way, as a fraction of it
    # (0 when the bar sits inside).
    excursion = (
        pd.concat([(bars["high"] - e) / e, (e - bars["low"]) / e], axis=1)
        .max(axis=1)
        .clip(lower=0)
        .iloc[-params.channel_lookback_bars :]
    )
    last = float(e.iloc[-1])
    if excursion.empty:
        return last, last
    # Keeping m bars inside takes the m-th smallest excursion.
    inside = max(1, math.ceil(params.channel_containment * len(excursion) - 1e-9))
    k = float(excursion.sort_values().iloc[inside - 1])
    return last * (1.0 + k), last * max(0.0, 1.0 - k)


def _long_levels(
    tide: pd.DataFrame,
    wave: pd.DataFrame,
    params: StrategyParams = DEFAULT_PARAMS,
    sz_decimals: int | None = None,
) -> tuple[float, float | None, float, float]:
    """(entry, entry_limit, stop, target) for a long setup."""
    prior_high = float(wave["high"].iloc[-1])
    tick = tick_size(prior_high, sz_decimals)
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
    tide: pd.DataFrame,
    wave: pd.DataFrame,
    params: StrategyParams = DEFAULT_PARAMS,
    sz_decimals: int | None = None,
) -> tuple[float, float | None, float, float | None]:
    """(entry, entry_limit, stop, target) for a short setup; no target when price
    is already below value and the tide channel has no lower line."""
    prior_low = float(wave["low"].iloc[-1])
    tick = tick_size(prior_low, sz_decimals)
    entry = prior_low - tick  # sell-stop 1 tick below the prior bar's low

    pen = average_penetration(wave, "up", lookback=params.penetration_lookback_bars)
    limit = projected_ema(wave["close"]) + pen if pen is not None else None

    stop = safezone_initial_stop(wave, "short", params)

    e13 = float(ema(tide["close"], EMA_FAST).iloc[-1])
    e26 = float(ema(tide["close"], EMA_SLOW).iloc[-1])
    value_low = min(e13, e26)
    target = value_low if value_low < entry else channel(tide, params)[1]
    return entry, limit, stop, target if target > 0 else None


def _round_levels(
    side: Literal["long", "short"],
    entry: float,
    limit: float | None,
    stop: float,
    target: float | None,
    sz_decimals: int | None = None,
) -> tuple[float, float | None, float, float | None]:
    """Put a setup's levels on Hyperliquid's price grid, each on the prudent side.

    The stop-entry moves further out (buy-stop up, sell-stop down), the limit to a
    better price and the protective stop further away (long: both down; short:
    both up), the target toward the entry — rounding never flatters the trade.
    """
    if side == "long":
        entry_dir, limit_dir, stop_dir = "up", "down", "down"
    else:
        entry_dir, limit_dir, stop_dir = "down", "up", "up"
    target_dir = "down" if target is not None and target > entry else "up"
    return (
        round_to_tick(entry, entry_dir, sz_decimals),
        round_to_tick(limit, limit_dir, sz_decimals) if limit is not None else None,
        round_to_tick(stop, stop_dir, sz_decimals),
        round_to_tick(target, target_dir, sz_decimals) if target is not None else None,
    )


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
    must cross its zero line between the two extremes (p.87), and the extremes
    must sit `min_separation`-`max_separation` bars apart (p.88). Intentionally
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
        # Elder (p.87): the indicator MUST cross back above its zero line between
        # the two bottoms ("an absolute must"); and (p.88) the bottoms must be
        # 20-40 bars apart to be tradable. Either gate failing => no divergence.
        # A cross needs the first bottom below zero; otherwise any positive value
        # in the window (even the first bottom itself) would pass vacuously.
        crossed_zero = float(ind.loc[pi]) < 0 and bool((ind.loc[pi:ri] > 0).any())
        spaced = min_separation <= (ri - pi) <= max_separation
        if rc < pc and float(ind.loc[ri]) > float(ind.loc[pi]) and crossed_zero and spaced:
            out.append(f"bullish {name} divergence")

    prev_high = _last_pivot(prev["close"], kind="high")
    recent_high = _last_pivot(recent["close"], kind="high")
    if prev_high and recent_high:
        pi, pc = prev_high
        ri, rc = recent_high
        # Mirror image: the indicator must drop below its zero line between the
        # two tops (so the first top must be above zero), 20-40 bars apart.
        crossed_zero = float(ind.loc[pi]) > 0 and bool((ind.loc[pi:ri] < 0).any())
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
    baseline = float(vol.iloc[-params.low_volume_baseline_bars :].median()) if len(vol) else 0.0
    if len(recent) and baseline > 0:
        ratio = float(recent.mean()) / baseline
        if ratio < params.low_volume_ratio:
            out.append(
                f"last {len(recent)} {labels['wave']} bars traded at {ratio:.0%} of normal "
                f"volume — market near-closed (weekend / out of session), signal unreliable"
            )
    return out


def _entry_screen_levels(
    action: Action,
    entry_frame: pd.DataFrame | None,
    fallback_entry: float,
    sz_decimals: int | None = None,
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
        return px + tick_size(px, sz_decimals), imp
    if action == "short":
        px = float(entry_frame["low"].iloc[-1])
        return px - tick_size(px, sz_decimals), imp
    return fallback_entry, imp


def trade_apgar(
    action: Literal["long", "short"],
    tide_impulse: str,
    wave_impulse: str,
    zone_side: Literal["above", "below", "inside"],
    reward_risk: float | None,
    divergences: Sequence[str],
    min_reward_risk: float = DEFAULT_PARAMS.min_reward_risk,
) -> TradeApgar:
    """Trade Apgar for a "pullback to value" setup — Elder's scoring (p.238-242)
    with this system's own five questions ("each strategy demands its own Apgar").

    For a long (mirror image for a short, red <-> green, below <-> above):
      a. tide Impulse: green 2 — the tide's momentum is behind the trade —,
         blue 1, red 0;
      b. wave Impulse: blue 2 — the pullback is losing force, Elder's "blue after
         red" —, green 1, red 0;
      c. wave close vs the EMA13-EMA26 value zone: below 2 (a bargain), inside 1,
         above 0;
      d. reward:risk: >= `min_reward_risk` (2) -> 2, >= 1 -> 1, below 1 or none -> 0;
      e. wave divergence: bullish 2, none 1, bearish (even alongside a bullish
         one) 0.
    """
    long = action == "long"
    with_tide = "green" if long else "red"
    bargain, chasing = ("below", "above") if long else ("above", "below")
    favorable, adverse = ("bullish", "bearish") if long else ("bearish", "bullish")

    def impulse(color: str, two: str, one: str) -> int:
        return 2 if color == two else 1 if color == one else 0

    if reward_risk is not None and reward_risk >= min_reward_risk:
        rr_score = 2
    elif reward_risk is not None and reward_risk >= 1.0:
        rr_score = 1
    else:
        rr_score = 0
    kinds = {d.split(" ", 1)[0] for d in divergences}
    if adverse in kinds:
        div_answer, div_score = adverse, 0
    elif favorable in kinds:
        div_answer, div_score = favorable, 2
    else:
        div_answer, div_score = "none", 1
    zone_answer = {"inside": "in value zone", "above": "above value", "below": "below value"}

    lines = [
        ApgarLine("tide Impulse", tide_impulse, impulse(tide_impulse, with_tide, "blue")),
        ApgarLine("wave Impulse", wave_impulse, impulse(wave_impulse, "blue", with_tide)),
        ApgarLine(
            "wave close vs value",
            zone_answer[zone_side],
            2 if zone_side == bargain else 0 if zone_side == chasing else 1,
        ),
        ApgarLine(
            "reward:risk",
            f"{reward_risk:.2f}" if reward_risk is not None else "none",
            rr_score,
        ),
        ApgarLine("wave divergence", div_answer, div_score),
    ]
    total = sum(line.score for line in lines)
    return TradeApgar(
        lines=lines, total=total, a_trade=total >= 7 and all(line.score for line in lines)
    )


def select_best(signals: Sequence[Signal]) -> Signal | None:
    """Elder's "which one do I take?": the A-trade with the best Trade Apgar,
    ties broken on reward:risk (then asset name, for a stable pick). Returns None
    when no setup is an A-trade."""
    candidates = [
        s for s in signals if s.action != "stand_aside" and s.apgar is not None and s.apgar.a_trade
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda s: (s.apgar.total, s.reward_risk or 0.0, s.asset))


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
    sz_decimals: int | None = None,
) -> Signal:
    """Run the three screens + Impulse censorship for one asset on one horizon.

    `tide` and `wave` must be OHLCV frames of *completed* bars (open/high/low/
    close/volume columns, oldest first); `entry_frame` is the optional third
    screen's lower-timeframe bars. `sz_decimals` (from `meta`) caps the decimals
    of every price level, per Hyperliquid's tick rules; each level is rounded to
    that grid on the prudent side (see `_round_levels`).
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
    vz_status = value_zone_status(wave, params)

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
    elif candidate in ("long", "short"):
        # Elder (p.168): "never buy above the upper channel line or sell short below
        # the lower channel line" — on the wave, where the entry is decided. The veto
        # is directional: a pullback the other way is a bargain, and its falling-knife
        # guard is the Force-Index new-extreme filter above, not this veto.
        upper, lower = channel(wave, params)
        close = float(wave["close"].iloc[-1])
        if candidate == "long" and close > upper:
            candidate = "stand_aside"
            reason = (
                f"long vetoed: the {labels['wave']} close {close:.6g} is above the upper "
                f"{labels['wave']} channel line {upper:.6g} (chasing)"
            )
        elif candidate == "short" and close < lower:
            candidate = "stand_aside"
            reason = (
                f"short vetoed: the {labels['wave']} close {close:.6g} is below the lower "
                f"{labels['wave']} channel line {lower:.6g} (chasing)"
            )

    entry = limit = stop = target = rr = None
    entry_impulse = None
    # Every level is put on the exchange's price grid before reward:risk (and,
    # downstream, the size) is worked out from it.
    if candidate == "long":
        entry, limit, stop, target = _long_levels(tide, wave, params, sz_decimals)
        entry, entry_impulse = _entry_screen_levels(candidate, entry_frame, entry, sz_decimals)
        entry, limit, stop, target = _round_levels("long", entry, limit, stop, target, sz_decimals)
        if entry > stop:
            rr = (target - entry) / (entry - stop)
    elif candidate == "short":
        entry, limit, stop, target = _short_levels(tide, wave, params, sz_decimals)
        entry, entry_impulse = _entry_screen_levels(candidate, entry_frame, entry, sz_decimals)
        entry, limit, stop, target = _round_levels("short", entry, limit, stop, target, sz_decimals)
        if stop > entry and target is not None:
            rr = (entry - target) / (stop - entry)

    # Stop & reward:risk for the limit (pullback) entry — recalibrated to that
    # fill, since a deep pullback can clear the breakout stop.
    limit_stop = limit_rr = None
    if candidate in ("long", "short") and limit is not None and target is not None:
        limit_stop = round_to_tick(
            safezone_stop_for_limit(wave, candidate, limit, params),
            "down" if candidate == "long" else "up",
            sz_decimals,
        )
        if candidate == "long" and limit > limit_stop:
            limit_rr = (target - limit) / (limit - limit_stop)
        elif candidate == "short" and limit_stop > limit:
            limit_rr = (limit - target) / (limit_stop - limit)

    apgar = None
    if candidate in ("long", "short"):
        apgar = trade_apgar(
            candidate,
            t_imp,
            w_imp,
            value_zone_extension(wave),
            rr,
            divergences,
            params.min_reward_risk,
        )
    order_plan = theoretical_entry_order_plan(candidate, entry, labels["wave"], labels["tide"])

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
        market_regime=market_regime,
        entry_impulse=entry_impulse,
        divergences=divergences,
        value_zone_status=vz_status,
        entry_order_plan=order_plan,
        entry_limit_stop=limit_stop,
        reward_risk_limit=limit_rr,
        apgar=apgar,
        horizon=horizon,
        intervals=dict(labels),
        tide_bars=len(tide),
        wave_bars=len(wave),
        data_warnings=data_warnings(tide, wave, params, min_tide_bars, labels),
    )
