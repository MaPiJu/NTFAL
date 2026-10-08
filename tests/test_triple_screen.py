"""Strategy tests on crafted uptrend / downtrend / range fixtures."""

from __future__ import annotations

import pandas as pd
import pytest

from indicators import ema
from strategy.params import StrategyParams
from strategy.triple_screen import (
    EMA_FAST,
    EMA_SLOW,
    Signal,
    TradeApgar,
    _divergence_for_indicator,
    _long_levels,
    _short_levels,
    average_adverse_noise,
    average_penetration,
    channel,
    data_warnings,
    detect_divergences,
    evaluate_asset,
    projected_ema,
    round_to_tick,
    safezone_initial_stop,
    safezone_stop_for_limit,
    select_best,
    tick_size,
    tide_trend,
    trade_apgar,
    value_zone_extension,
    value_zone_status,
)
from tests.conftest import make_ohlcv

WEEKLY_UP = make_ohlcv([100.0 + 2 * i for i in range(40)], freq="W")
WEEKLY_DOWN = make_ohlcv([300.0 - 2 * i for i in range(40)], freq="W")
WEEKLY_FLAT = make_ohlcv([100.0] * 40, freq="W")

# Realistic swing fixtures. A *healthy* pullback (or rally) is preceded by an
# earlier counter-move, so the entry bar's 2-day Force Index is NOT a fresh
# multi-week extreme — the case Elder's second screen actually trades (p.158).
DAILY_LONG = make_ohlcv(
    [100.0 + i for i in range(34)]
    + [130.0, 128.0]  # a normal prior pullback (sets the recent FI low)
    + [128.0 + i for i in range(1, 13)]  # uptrend resumes
    + [139.0]  # mild pullback to value -> 2-day Force Index dips below zero
)
DAILY_SHORT = make_ohlcv(
    [300.0 - 2 * i for i in range(34)]
    + [238.0, 241.0]  # a normal prior rally (sets the recent FI high)
    + [241.0 - 2 * i for i in range(1, 13)]  # downtrend resumes
    + [220.0]  # mild rally -> 2-day Force Index rises above zero
)
# Uptrend, then a sustained decline that turns the daily Impulse red. An early
# high-volume down spike keeps the Force Index off a new multi-week low, so the
# Impulse censor (not the new-extreme filter) is what forbids the long.
DAILY_RED = make_ohlcv(
    [100.0 + i for i in range(45)]
    + [140.0]
    + [142.0 + i for i in range(0, 7)]
    + [146.0, 143.0, 140.0, 137.0, 134.0],
    volumes=[1000.0] * 45 + [10000.0] + [1000.0] * 7 + [1000.0] * 5,
)
# A downtrend whose earlier rally pierces the daily EMA13, so the short also gets
# an average-penetration limit entry.
RALLY_SHORT = make_ohlcv(
    [300.0 - 2 * i for i in range(34)]
    + [238.0, 246.0]
    + [246.0 - 2 * i for i in range(1, 13)]
    + [226.0]
)


def _off_grid(bars: pd.DataFrame, k: float = 1.0137) -> pd.DataFrame:
    """The same bars scaled by an odd factor, so no level lands on the tick grid by luck."""
    bars = bars.copy()
    bars[["open", "high", "low", "close"]] *= k
    return bars


def test_tick_size():
    assert tick_size(159.5) == pytest.approx(0.01)
    assert tick_size(95000.0) == pytest.approx(1.0)
    assert tick_size(0.234) == pytest.approx(0.00001)
    with pytest.raises(ValueError):
        tick_size(0)


def test_tick_size_follows_hyperliquid_price_rules():
    # Hyperliquid perps: <= 5 significant figures, but integer prices are always
    # valid (so a tick never exceeds 1), and <= 6 - szDecimals decimals.
    assert tick_size(123_456.0) == pytest.approx(1.0)  # not 10: integers allowed
    assert tick_size(0.5, sz_decimals=2) == pytest.approx(1e-4)  # decimals cap binds
    assert tick_size(0.5) == pytest.approx(1e-5)  # unknown szDecimals: sig figs only
    # The six default xyz perps: 5 significant figures is the binding rule.
    assert tick_size(4126.0, sz_decimals=4) == pytest.approx(0.1)  # xyz:GOLD
    assert tick_size(91.81, sz_decimals=3) == pytest.approx(0.001)  # xyz:CL
    assert tick_size(30_967.0, sz_decimals=4) == pytest.approx(1.0)  # xyz:XYZ100


def test_entry_tick_respects_sz_decimals_cap():
    # With szDecimals=5 a perp may quote at most 1 decimal, so the buy-stop sits
    # 0.1 (not 0.01) above the prior high, even at a ~140 price.
    sig = evaluate_asset("X", WEEKLY_UP, DAILY_LONG, sz_decimals=5)
    prior_high = float(DAILY_LONG["high"].iloc[-1])
    assert sig.action == "long"
    assert sig.entry == pytest.approx(prior_high + 0.1)


def test_round_to_tick_on_the_requested_side():
    assert round_to_tick(4126.04, "up", sz_decimals=4) == 4126.1  # xyz:GOLD, tick 0.1
    assert round_to_tick(4126.04, "down", sz_decimals=4) == 4126.0
    # A price already on the grid stays put, float noise or not.
    assert round_to_tick(4126.3 + 0.1, "up", sz_decimals=4) == 4126.4
    assert round_to_tick(4126.3 + 0.1, "down", sz_decimals=4) == 4126.4
    # Integer prices are always valid (tick 1), and the 6 - szDecimals cap binds.
    assert round_to_tick(123_456.4, "up") == 123_457.0
    assert round_to_tick(123_456.4, "down") == 123_456.0
    assert round_to_tick(0.50004, "up", sz_decimals=2) == 0.5001
    assert round_to_tick(0.50004, "down", sz_decimals=2) == 0.5


@pytest.mark.parametrize("side", ["long", "short"])
def test_levels_round_to_the_tick_on_the_prudent_side(side):
    # Hyperliquid refuses off-grid prices. Each level is rounded the way that never
    # flatters the trade: a stop-entry further out (buy-stop up, sell-stop down), a
    # protective stop further away (long down, short up), a limit at a better price
    # (buy down, sell up), and the target toward the entry (reward never overstated).
    if side == "long":
        weekly, daily, levels = WEEKLY_UP, _off_grid(DAILY_LONG), _long_levels
        away, better = "up", "down"
    else:
        weekly, daily, levels = WEEKLY_DOWN, _off_grid(RALLY_SHORT), _short_levels
        away, better = "down", "up"
    tick = 0.1  # szDecimals 5 leaves one price decimal at these prices

    sig = evaluate_asset("X", weekly, daily, sz_decimals=5)
    assert sig.action == side and sig.entry_limit is not None

    raw_entry, raw_limit, raw_stop, raw_target = levels(weekly, daily, sz_decimals=5)
    expected = {
        "entry": (raw_entry, away),
        "entry_limit": (raw_limit, better),
        "stop": (raw_stop, better),
        "target": (raw_target, better),  # toward the entry: below a long's, above a short's
        "entry_limit_stop": (safezone_stop_for_limit(daily, side, sig.entry_limit), better),
    }
    for name, (raw, direction) in expected.items():
        value = getattr(sig, name)
        assert value / tick == pytest.approx(round(value / tick), abs=1e-6), name
        assert raw / tick != pytest.approx(round(raw / tick), abs=1e-6), name  # was off-grid
        if direction == "up":
            assert raw < value < raw + tick, name
        else:
            assert raw - tick < value < raw, name

    # Reward:risk is worked out from the rounded levels.
    assert sig.reward_risk == pytest.approx(abs(sig.target - sig.entry) / abs(sig.entry - sig.stop))
    assert sig.reward_risk_limit == pytest.approx(
        abs(sig.target - sig.entry_limit) / abs(sig.entry_limit - sig.entry_limit_stop)
    )


def test_tide_trend():
    assert tide_trend(WEEKLY_UP["close"]) == "up"
    assert tide_trend(WEEKLY_DOWN["close"]) == "down"
    assert tide_trend(WEEKLY_FLAT["close"]) == "neutral"


def test_uptrend_pullback_goes_long():
    # healthy uptrend with a prior pullback, then a mild pullback to value ->
    # 2-EMA Force Index dips below zero without printing a new multi-week low
    daily = DAILY_LONG

    sig = evaluate_asset("BTC", WEEKLY_UP, daily)

    assert sig.action == "long"
    assert sig.tide_trend == "up"
    assert sig.force_index_2 < 0
    assert sig.wave_impulse != "red"  # otherwise the veto would apply
    # buy-stop one tick above the prior day's high
    prior_high = float(daily["high"].iloc[-1])
    assert sig.entry == pytest.approx(prior_high + tick_size(prior_high))
    assert sig.stop == pytest.approx(safezone_initial_stop(daily, "long"))
    assert average_adverse_noise(daily, "long", 20) is not None
    assert sig.target is not None and sig.target > sig.entry
    assert sig.reward_risk is not None and sig.reward_risk > 0
    # the prior pullback pierced EMA13, so Elder's average-penetration limit is set
    assert sig.entry_limit is not None
    # the limit entry carries its own SafeZone stop & reward:risk, distinct from
    # the breakout pair, and consistent with each other (stop below the fill)
    assert sig.entry_limit_stop == pytest.approx(
        safezone_stop_for_limit(daily, "long", sig.entry_limit)
    )
    assert sig.entry_limit_stop < sig.entry_limit
    assert sig.reward_risk_limit == pytest.approx(
        (sig.target - sig.entry_limit) / (sig.entry_limit - sig.entry_limit_stop)
    )


def test_limit_stop_anchors_below_a_deep_pullback_fill():
    # A limit fill below the recent daily low must take its SafeZone stop below
    # the *fill*, not the (higher) breakout stop, so the stop stays usable.
    daily = DAILY_LONG
    deep_limit = float(daily["low"].iloc[-2:].min()) - 5.0  # below the recent low
    breakout_stop = safezone_initial_stop(daily, "long")
    limit_stop = safezone_stop_for_limit(daily, "long", deep_limit)
    assert limit_stop < deep_limit < breakout_stop
    # a limit inside the recent range collapses back to the breakout stop
    shallow_limit = float(daily["low"].iloc[-2:].min()) + 5.0
    assert safezone_stop_for_limit(daily, "long", shallow_limit) == pytest.approx(breakout_stop)


def _value_zone(wave: pd.DataFrame) -> tuple[float, float]:
    e13 = float(ema(wave["close"], EMA_FAST).iloc[-1])
    e26 = float(ema(wave["close"], EMA_SLOW).iloc[-1])
    return min(e13, e26), max(e13, e26)


def _close_through_the_channel(side: str) -> pd.DataFrame:
    """A gentle wave trend (0.05% a bar), a heavy-volume counter-move, then a
    light-volume bar that closes beyond the wave channel. FI(2) still reads as a
    pullback (long) / rally (short), but price has already run past the channel
    line — while staying within 3% of value, where 1h/15m bars almost always sit."""
    sign = 1.0 if side == "long" else -1.0
    closes = [1000.0 + sign * 0.5 * i for i in range(60)]
    last = closes[-1]
    closes += [last - sign * 3.0, last + sign * 8.0]
    return make_ohlcv(closes, volumes=[1000.0] * 60 + [20_000.0, 1000.0])


def _stretched_inside_the_channel(side: str) -> pd.DataFrame:
    """A steep wave trend whose last bar pulls back: the close is more than 3% from
    the value zone, yet still inside the wave channel."""
    sign = 1.0 if side == "long" else -1.0
    base = [(100.0 if side == "long" else 400.0) + sign * 3.0 * i for i in range(40)]
    swing = base[-1] - sign * 6.0  # an earlier, deeper counter-move
    closes = base + [swing] + [swing + sign * 3.0 * (i + 1) for i in range(12)]
    closes.append(closes[-1] - sign * 3.0)
    return make_ohlcv(closes)


def test_channel_veto_rejects_a_long_above_the_upper_wave_channel():
    # Elder (p.168): "never buy above the upper channel line or sell short below the
    # lower channel line". The second screen says buy (FI(2) < 0, not a new low,
    # Impulse not red), but the wave close sits above the upper wave channel line.
    daily = _close_through_the_channel("long")
    upper, _lower = channel(daily)
    close = float(daily["close"].iloc[-1])
    _value_low, value_high = _value_zone(daily)
    assert close > upper
    assert (close - value_high) / value_high < 0.03  # a fixed 3% tolerance never saw it

    sig = evaluate_asset("BTC", WEEKLY_UP, daily)

    assert sig.force_index_2 < 0 and sig.wave_impulse != "red"
    assert sig.action == "stand_aside"
    assert "upper" in sig.reason and "channel" in sig.reason and "chasing" in sig.reason
    assert sig.value_zone_status == "extended"
    assert sig.entry is None


def test_channel_veto_rejects_a_short_below_the_lower_wave_channel():
    daily = _close_through_the_channel("short")
    _upper, lower = channel(daily)
    close = float(daily["close"].iloc[-1])
    value_low, _value_high = _value_zone(daily)
    assert close < lower
    assert (value_low - close) / value_low < 0.03

    sig = evaluate_asset("ETH", WEEKLY_DOWN, daily)

    assert sig.force_index_2 > 0 and sig.wave_impulse != "green"
    assert sig.action == "stand_aside"
    assert "lower" in sig.reason and "channel" in sig.reason and "chasing" in sig.reason
    assert sig.value_zone_status == "extended"


@pytest.mark.parametrize(("side", "weekly"), [("long", WEEKLY_UP), ("short", WEEKLY_DOWN)])
def test_a_stretch_beyond_value_inside_the_wave_channel_is_not_vetoed(side, weekly):
    # The veto is the channel line, not a fixed distance from value: a close more
    # than 3% beyond the value zone, but still inside the wave channel, is tradable.
    daily = _stretched_inside_the_channel(side)
    upper, lower = channel(daily)
    close = float(daily["close"].iloc[-1])
    value_low, value_high = _value_zone(daily)
    if side == "long":
        assert (close - value_high) / value_high > 0.03 and close <= upper
    else:
        assert (value_low - close) / value_low > 0.03 and close >= lower

    sig = evaluate_asset("X", weekly, daily)

    assert sig.action == side
    assert sig.value_zone_status == "near_value"  # displayed, never a veto


def test_force_index_new_multiweek_low_blocks_long():
    # Gentle uptrend, then a sharp final dip that prints a fresh multi-week low of
    # the 2-day Force Index. Elder (p.158): a buy signal is void when the Force
    # Index falls to a new multi-week low — the decline is accelerating.
    closes = [100.0 + 0.7 * i for i in range(50)]
    closes.append(closes[-1] - 3.0)
    daily = make_ohlcv(closes)

    blocked = evaluate_asset("BTC", WEEKLY_UP, daily)
    assert blocked.action == "stand_aside"
    assert "multi-period low" in blocked.reason
    assert blocked.entry is None

    # Disable the caveat (lookback 0) and the very same bar is a tradable long,
    # proving the new-extreme filter — not Impulse or the value zone — blocked it.
    off = StrategyParams(force_index_extreme_lookback_bars=0)
    allowed = evaluate_asset("BTC", WEEKLY_UP, daily, params=off)
    assert allowed.action == "long"


def test_value_zone_veto_is_directional_long_below_value():
    # A pullback that dips just BELOW the value zone is an Elder bargain, not
    # chasing, so the directional veto must let the long through (its falling-knife
    # guard is the Force-Index new-low filter, which this bar clears). An earlier
    # deeper dip keeps today's Force Index off a new multi-week low.
    closes = [100.0 + 1.0 * i for i in range(40)] + [119.0]
    closes += [119.0 + i for i in range(1, 9)] + [124.0]
    daily = make_ohlcv(closes)

    assert value_zone_extension(daily) == "below"
    sig = evaluate_asset("BTC", WEEKLY_UP, daily)

    assert sig.action == "long"
    assert "pullback to buy" in sig.reason
    assert sig.entry is not None and sig.stop is not None


def test_value_zone_status_reads_the_wave_channel():
    # Display only: in the EMA13-EMA26 zone, near it (outside the zone but inside
    # the wave channel), or extended beyond the channel line.
    flat = make_ohlcv([100.0] * 60)
    assert value_zone_status(flat) == "in_value"
    assert value_zone_status(_stretched_inside_the_channel("long")) == "near_value"
    assert value_zone_status(_close_through_the_channel("long")) == "extended"
    assert value_zone_status(_close_through_the_channel("short")) == "extended"


def test_divergence_requires_zero_line_crossover():
    # Two successively lower price lows with a shallower second indicator low is
    # the divergence *shape* — but Elder (p.87) requires the indicator to cross
    # back above its zero line between the two bottoms ("an absolute must").
    close = pd.Series(
        [110, 108, 106, 104, 102, 100, 102, 104, 106, 108,
         106, 104, 102, 100, 98, 96, 98, 100, 102, 104],
        dtype=float,
    )  # fmt: skip
    ind = pd.Series([-1.0] * 20)
    ind[5], ind[15] = -5.0, -2.0  # shallower second low, but never above zero

    # min_separation=0 isolates the zero-line rule from the 20-40 bar spacing gate.
    assert _divergence_for_indicator(close, ind, "TEST", min_separation=0) == []

    ind_crossed = ind.copy()
    ind_crossed[10] = 0.5  # a rally pokes the indicator above zero between the lows
    assert _divergence_for_indicator(close, ind_crossed, "TEST", min_separation=0) == [
        "bullish TEST divergence"
    ]


def test_divergence_needs_a_real_zero_line_cross_not_an_endpoint():
    # Elder (p.87, p.117): the indicator must *cross* its zero line between the two
    # extremes — down below zero at the first bottom, back above before the second.
    # An indicator already above zero at the first price low never crossed anything,
    # even though "some value in [first, second] is > 0" is trivially true.
    close = pd.Series(
        [110, 108, 106, 104, 102, 100, 102, 104, 106, 108,
         106, 104, 102, 100, 98, 96, 98, 100, 102, 104],
        dtype=float,
    )  # fmt: skip
    above = pd.Series([1.0] * 20)
    above[5], above[15] = 0.5, 2.0  # higher second "bottom", but never below zero
    assert _divergence_for_indicator(close, above, "TEST", min_separation=0) == []

    # Mirror image for tops: never above zero, so no bearish divergence either.
    tops = 200.0 - close  # higher high at bar 15
    below = pd.Series([-1.0] * 20)
    below[5], below[15] = -0.5, -2.0
    assert _divergence_for_indicator(tops, below, "TEST", min_separation=0) == []

    # A genuine bearish cross (above zero at the first top, below in between) counts.
    crossed = below.copy()
    crossed[5], crossed[10] = 3.0, -1.0
    assert _divergence_for_indicator(tops, crossed, "TEST", min_separation=0) == [
        "bearish TEST divergence"
    ]


def test_divergence_requires_minimum_separation():
    # Same valid bullish shape (crosses zero between the two lows), but the lows are
    # only 10 bars apart — below Elder/Lovvorn's 20-bar floor (p.88), so it is not
    # flagged. Lower the floor and the very same shape is flagged.
    close = pd.Series(
        [110, 108, 106, 104, 102, 100, 102, 104, 106, 108,
         106, 104, 102, 100, 98, 96, 98, 100, 102, 104],
        dtype=float,
    )  # fmt: skip
    ind = pd.Series([-1.0] * 20)
    ind[5], ind[15] = -5.0, -2.0
    ind[10] = 0.5  # crosses zero between the two lows (10 bars apart)

    assert _divergence_for_indicator(close, ind, "TEST", min_separation=20) == []
    assert _divergence_for_indicator(close, ind, "TEST", min_separation=5) == [
        "bullish TEST divergence"
    ]


@pytest.mark.parametrize(
    ("weekly", "daily", "move"),
    [(WEEKLY_UP, DAILY_LONG, "lower"), (WEEKLY_DOWN, DAILY_SHORT, "raise")],
)
def test_entry_order_plan_rolls_until_the_tide_or_impulse_cancels_it(weekly, daily, move):
    # Elder (p.161): "Keep lowering your buy-stop each day until stopped in or until
    # the weekly indicator reverses and cancels its buy signal" — no fixed expiry.
    sig = evaluate_asset(
        "BTC", weekly, daily, intervals={"tide": "1w", "wave": "1d", "entry": "4h"}
    )

    plan = sig.entry_order_plan
    assert plan is not None
    assert f"{move} it each 1d bar" in plan
    assert "1w tide" in plan and "Impulse" in plan
    assert "expire" not in plan


def test_uptrend_without_pullback_stands_aside():
    daily = make_ohlcv([100.0 + i for i in range(60)])  # FI(2) positive: chasing
    sig = evaluate_asset("BTC", WEEKLY_UP, daily)
    assert sig.action == "stand_aside"
    assert "chasing" in sig.reason
    assert sig.entry is None and sig.stop is None and sig.target is None


def test_downtrend_rally_goes_short():
    # downtrend with a prior rally, then a mild rally to value -> FI(2) rises above
    # zero without printing a new multi-week high
    daily = DAILY_SHORT

    sig = evaluate_asset("ETH", WEEKLY_DOWN, daily)

    assert sig.action == "short"
    assert sig.tide_trend == "down"
    assert sig.force_index_2 > 0
    assert sig.wave_impulse != "green"
    prior_low = float(daily["low"].iloc[-1])
    assert sig.entry == pytest.approx(prior_low - tick_size(prior_low))
    assert sig.stop == pytest.approx(safezone_initial_stop(daily, "short"))
    assert sig.target is not None
    # price is still below the weekly value zone here -> R:R below 2:1, flagged
    assert sig.rr_ok is False


def test_downtrend_without_rally_stands_aside():
    daily = make_ohlcv([300.0 - 2 * i for i in range(60)])
    sig = evaluate_asset("ETH", WEEKLY_DOWN, daily)
    assert sig.action == "stand_aside"
    assert sig.entry is None


def test_range_stands_aside():
    daily = make_ohlcv([100.0] * 60)
    sig = evaluate_asset("SOL", WEEKLY_FLAT, daily)
    assert sig.action == "stand_aside"
    assert sig.tide_trend == "neutral"
    assert "neutral" in sig.reason


def test_impulse_red_vetoes_long():
    # weekly tide up and FI(2) below zero (the table says long), but a sustained
    # daily decline turns the daily Impulse red. An earlier high-volume down spike
    # keeps FI off a new multi-week low, so it is the Impulse censor — not the
    # new-extreme filter — that vetoes the long.
    daily = DAILY_RED

    sig = evaluate_asset("BTC", WEEKLY_UP, daily)

    assert sig.wave_impulse == "red"
    assert sig.force_index_2 < 0
    assert sig.action == "stand_aside"
    assert "vetoed by Impulse" in sig.reason
    assert sig.entry is None


def test_channel_lower_band_stays_positive_after_a_crash():
    # Asset fell from ~120 to ~6 with deep weekly wicks. When price was high those
    # wicks pierced the EMA by large *absolute* amounts; an absolute channel offset
    # subtracts that stale distance from today's tiny EMA and goes negative — the
    # old source of negative short targets. The proportional channel must not.
    closes = [
        120.0, 90.0, 60.0, 40.0, 28.0, 20.0, 15.0, 12.0, 10.0, 9.0,
        8.5, 8.0, 7.5, 7.0, 6.8, 6.6, 6.4, 6.2, 6.1, 6.05,
        6.02, 6.0, 5.98, 5.96, 5.94, 5.92,
    ]  # fmt: skip
    lows = [c * 0.6 for c in closes]  # deep wicks -> big penetrations while price was high
    highs = [c * 1.05 for c in closes]
    weekly = make_ohlcv(closes, lows=lows, highs=highs, freq="W")

    upper, lower = channel(weekly)
    e26 = float(ema(weekly["close"], EMA_SLOW).iloc[-1])  # Elder's channel backbone

    assert lower > 0  # the bug was a negative lower band (negative short target)
    assert lower < e26 <= upper


# A new listing that pumped (one weekly high at 3.5x its EMA26) then crashed:
# its symmetric channel coefficient passes 100%.
PUMPED = [1.0, 1.05, 3.0, 2.6, 2.2, 1.9, 1.65, 1.45, 1.3, 1.18, 1.08, 1.0, 0.93, 0.87, 0.82]
WEEKLY_PUMP_CRASH = make_ohlcv(
    PUMPED,
    lows=[c * 0.98 for c in PUMPED],
    highs=[3.5 if i == 2 else c * 1.02 for i, c in enumerate(PUMPED)],
    freq="W",
)


def test_a_channel_past_100_percent_never_gives_a_negative_target():
    # One coefficient sets both lines, so highs more than doubling their EMA push
    # k past 1: the lower line is then no price at all. It is floored at zero and
    # a short gets no channel target (hence no R:R, no size, no Apgar credit)
    # rather than a negative target and a huge fake reward:risk.
    _upper, lower = channel(WEEKLY_PUMP_CRASH)
    assert lower == 0.0

    closes = [1.2 - 0.01 * i for i in range(40)]
    closes += [closes[-1] + 0.03, closes[-1] + 0.01]
    closes += [closes[-1] - 0.01 * i for i in range(1, 12)]
    closes.append(closes[-1] + 0.01)  # a mild rally to sell
    daily = make_ohlcv(closes, lows=[c - 0.005 for c in closes], highs=[c + 0.005 for c in closes])

    sig = evaluate_asset("X", WEEKLY_PUMP_CRASH, daily)

    assert sig.action == "short"
    assert sig.target is None and sig.reward_risk is None and not sig.rr_ok
    assert sig.apgar is not None and sig.apgar.lines[3].score == 0


def test_channel_backbone_is_slow_ema26():
    # Elder draws the channel parallel to the SLOW EMA26, not the fast EMA13: the
    # symmetrical channel is centered on it.
    weekly = make_ohlcv([100.0 + 2.0 * i for i in range(40)], freq="W")
    upper, lower = channel(weekly)
    e13 = float(ema(weekly["close"], EMA_FAST).iloc[-1])
    e26 = float(ema(weekly["close"], EMA_SLOW).iloc[-1])

    assert (upper + lower) / 2 == pytest.approx(e26)
    assert (upper + lower) / 2 != pytest.approx(e13)


def test_channel_is_one_symmetric_coefficient_around_the_slow_ema():
    # Elder (p.167): "Upper Channel Line = EMA + Channel Coefficient · EMA; Lower
    # Channel Line = EMA - Channel Coefficient · EMA" — one coefficient, adjusted
    # "until a channel contains approximately 95 percent of all price data for
    # the past 100 bars". Highs poke far above a flat EMA26 at 100, lows barely
    # below it: one coefficient still sets both lines.
    n = 100
    highs = [102.0 + (i % 10) * 0.3 for i in range(n)]  # +2.0% .. +4.7%, 10 bars each
    lows = [99.5] * n
    weekly = make_ohlcv([100.0] * n, lows=lows, highs=highs, freq="W")

    upper, lower = channel(weekly)

    assert upper - 100.0 == pytest.approx(100.0 - lower)
    # The smallest such coefficient: the top ten bars all poke out by 4.7%, and
    # keeping 95 of 100 inside needs some of them, hence all of them.
    assert upper == pytest.approx(104.7)


def test_channel_fits_the_last_100_bars_or_all_there_is():
    # Elder (p.79): a channel "should contain approximately 95% of all prices that
    # occurred during the past 100 bars". 11 spikes sit in the last 100 bars but
    # none in the last 26: more than the 5% budget, so the lines must reach them.
    assert StrategyParams().channel_lookback_bars == 100
    n = 130
    highs, lows = [101.0] * n, [99.0] * n
    for i in range(n - 100, n - 26, 7):
        highs[i] = 110.0
    assert sum(h == 110.0 for h in highs) == 11
    weekly = make_ohlcv([100.0] * n, lows=lows, highs=highs, freq="W")

    upper, lower = channel(weekly)
    assert (upper, lower) == (pytest.approx(110.0), pytest.approx(90.0))

    # Less history than that: every bar there is.
    short = make_ohlcv([100.0] * 40, lows=[99.0] * 40, highs=[101.0] * 39 + [103.0], freq="W")
    assert channel(short)[0] == pytest.approx(101.0)  # 1 bar of 40 left out


def test_channel_widens_with_containment():
    # Higher containment -> wider channel (Elder fits ~95%, p.167). Up-excursions
    # spike every 5th bar, so the 95th percentile sits well above the median.
    highs = [100.0 + (10.0 if i % 5 == 0 else 1.0) for i in range(40)]
    weekly = make_ohlcv([100.0] * 40, lows=[100.0] * 40, highs=highs, freq="W")

    narrow_upper, _ = channel(weekly, StrategyParams(channel_containment=0.50))
    wide_upper, _ = channel(weekly, StrategyParams(channel_containment=0.95))
    assert wide_upper > narrow_upper


def test_channel_contains_about_95_percent_of_bars():
    # Elder (p.79, p.167): a well-drawn channel contains ~95% of recent prices —
    # "between 90% and 95%" (p.226). The 5% left outside is split between BOTH
    # edges; fitting each edge to its own 95th percentile leaves ~5% above AND ~5%
    # below, i.e. only ~85-90% inside. Flat EMA26 at 100 with distinct excursions,
    # so containment is a plain count over the lookback window.
    n = 40
    highs = [100.0 + 1.0 + (i * 7 % n) / 10 for i in range(n)]
    lows = [100.0 - 1.0 - (i * 11 % n) / 10 for i in range(n)]
    weekly = make_ohlcv([100.0] * n, lows=lows, highs=highs, freq="W")
    params = StrategyParams()

    upper, lower = channel(weekly, params)
    window = weekly.iloc[-params.channel_lookback_bars :]
    inside = ((window["high"] <= upper) & (window["low"] >= lower)).mean()

    assert 0.90 <= inside < 1.0  # Elder's 90-95%, with only the extremes outside


def _scores(apgar: TradeApgar) -> list[int]:
    return [line.score for line in apgar.lines]


def test_trade_apgar_scores_a_long_pullback_to_value():
    # Elder (p.238-242): five questions, each scored 0/1/2, and "each strategy
    # demands its own Apgar". For this system's long: tide Impulse green 2 / blue 1
    # / red 0; wave Impulse blue 2 / green 1 / red 0; wave close below value 2 / in
    # the zone 1 / above 0; R:R >= 2 -> 2, 1-2 -> 1, < 1 -> 0; wave divergence
    # bullish 2 / none 1 / bearish 0.
    best = trade_apgar("long", "green", "blue", "below", 2.5, ["bullish Force Index divergence"])
    assert _scores(best) == [2, 2, 2, 2, 2] and best.total == 10 and best.a_trade
    assert [line.answer for line in best.lines] == [
        "green",
        "blue",
        "below value",
        "2.50",
        "bullish",
    ]
    middling = trade_apgar("long", "blue", "green", "inside", 1.5, [])
    assert _scores(middling) == [1, 1, 1, 1, 1] and middling.total == 5
    worst = trade_apgar("long", "red", "red", "above", 0.8, ["bearish MACD-Histogram divergence"])
    assert _scores(worst) == [0, 0, 0, 0, 0]
    # R:R edges: exactly 2 earns 2, exactly 1 earns 1; no R:R at all earns 0.
    assert _scores(trade_apgar("long", "green", "blue", "below", 2.0, []))[3] == 2
    assert _scores(trade_apgar("long", "green", "blue", "below", 1.0, []))[3] == 1
    assert _scores(trade_apgar("long", "green", "blue", "below", None, []))[3] == 0


def test_trade_apgar_mirrors_for_a_short():
    best = trade_apgar("short", "red", "blue", "above", 3.0, ["bearish Force Index divergence"])
    assert _scores(best) == [2, 2, 2, 2, 2]
    assert best.lines[2].answer == "above value"
    assert _scores(trade_apgar("short", "blue", "red", "inside", 1.0, [])) == [1, 1, 1, 1, 1]
    worst = trade_apgar(
        "short", "green", "green", "below", None, ["bullish MACD-Histogram divergence"]
    )
    assert _scores(worst) == [0, 0, 0, 0, 0]


def test_an_a_trade_needs_seven_points_and_no_zero_line():
    # Elder (p.239): "only healthy ideas whose score is 7 or higher, and not a
    # single line rated zero".
    seven = trade_apgar("long", "green", "green", "inside", 2.5, [])  # 2+1+1+2+1
    assert seven.total == 7 and seven.a_trade
    six = trade_apgar("long", "blue", "green", "inside", 2.5, [])  # 1+1+1+2+1
    assert six.total == 6 and not six.a_trade
    # 8 points, but an adverse divergence scores a zero: not an A-trade.
    zero = trade_apgar("long", "green", "blue", "below", 2.5, ["bearish Force Index divergence"])
    assert zero.total == 8 and not zero.a_trade
    # A divergence both ways is still an adverse one.
    mixed = ["bullish Force Index divergence", "bearish MACD-Histogram divergence"]
    assert _scores(trade_apgar("long", "green", "blue", "below", 2.5, mixed))[4] == 0


def _mk_signal(asset: str, action: str, rr: float | None, apgar: TradeApgar | None) -> Signal:
    return Signal(
        asset=asset,
        action=action,
        reason="",
        tide_trend="up",
        tide_impulse="green",
        wave_impulse="blue",
        force_index_2=-1.0,
        entry=10.0,
        entry_limit=None,
        stop=9.0,
        target=13.0,
        reward_risk=rr,
        rr_ok=rr is not None and rr >= 2.0,
        market_regime="trending",
        entry_impulse=None,
        divergences=[],
        value_zone_status="in_value",
        entry_order_plan=None,
        apgar=apgar,
    )


def _long(asset: str, rr: float, zone: str = "below", divs: list[str] | None = None) -> Signal:
    return _mk_signal(asset, "long", rr, trade_apgar("long", "green", "blue", zone, rr, divs or []))


def test_select_best_picks_the_highest_apgar_a_trade_tie_broken_by_reward_risk():
    bullish = ["bullish Force Index divergence"]
    eight = _long("A", 2.5, zone="inside")  # 2+2+1+2+1 = 8
    ten_low_rr = _long("B", 2.2, divs=bullish)  # 10
    ten_high_rr = _long("C", 3.0, divs=bullish)  # 10, better R:R -> the pick
    zero_line = _long("D", 4.0, divs=["bearish Force Index divergence"])  # 8, has a zero
    aside = _mk_signal("E", "stand_aside", None, None)
    assert select_best([eight, ten_low_rr, ten_high_rr, zero_line, aside]).asset == "C"
    assert select_best([eight, zero_line, aside]).asset == "A"
    # The R:R line grades the reward: a 1.5:1 A-trade (9 points) is eligible.
    assert select_best([_long("F", 1.5, divs=bullish)]).asset == "F"


def test_select_best_returns_none_without_an_a_trade():
    six = _mk_signal("A", "long", 2.5, trade_apgar("long", "blue", "green", "inside", 2.5, []))
    zero_line = _long("B", 4.0, divs=["bearish Force Index divergence"])
    aside = _mk_signal("C", "stand_aside", None, None)
    assert select_best([six, zero_line, aside]) is None


def test_signal_carries_its_trade_apgar():
    sig = evaluate_asset("BTC", WEEKLY_UP, DAILY_LONG)
    assert sig.action == "long"
    assert sig.apgar is not None and len(sig.apgar.lines) == 5
    assert sig.apgar.total == sum(_scores(sig.apgar))
    assert sig.apgar.lines[3].score == (2 if sig.reward_risk >= 2 else 1)
    assert (
        evaluate_asset("BTC", WEEKLY_UP, make_ohlcv([100.0 + i for i in range(60)])).apgar is None
    )


def test_average_penetration_and_projection():
    lows = [100.0] * 47
    # Two pullbacks below the (flat, 100) EMA: bar -5 alone (2), then bars -3/-2
    # (3, 1) — one pullback each, so the deepest bars average (2 + 3) / 2.
    lows[-5], lows[-3], lows[-2] = 98.0, 97.0, 99.0
    highs = [100.0] * 47
    daily = make_ohlcv([100.0] * 47, lows=lows, highs=highs)

    assert average_penetration(daily, "down") == pytest.approx(2.5)
    assert average_penetration(daily, "up") is None  # highs never pierce the EMA
    assert projected_ema(daily["close"]) == pytest.approx(100.0)


@pytest.mark.parametrize("side", ["down", "up"])
def test_average_penetration_counts_one_value_per_pullback(side):
    # Elder (Fig. 39.3, p.159-160) measures each pullback once — A, B, C, D, the
    # depth of each dip below the fast EMA — then averages them. Averaging every
    # pierced bar lets the shallow bars entering and leaving a dip dilute it.
    n = 47
    pen = [0.0] * n
    pen[-12:-9] = [1.0, 4.0, 2.0]  # first pullback, deepest bar 4
    pen[-5:-3] = [3.0, 6.0]  # second pullback, deepest bar 6
    sign = -1.0 if side == "down" else 1.0
    extremes = [100.0 + sign * p for p in pen]
    daily = make_ohlcv(
        [100.0] * n,  # flat close: the EMA13 stays exactly at 100
        lows=extremes if side == "down" else [100.0] * n,
        highs=extremes if side == "up" else [100.0] * n,
    )

    # Per bar: (1 + 4 + 2 + 3 + 6) / 5 = 3.2. Per pullback: (4 + 6) / 2 = 5.
    assert average_penetration(daily, side) == pytest.approx(5.0)


def test_tiny_weekly_slope_is_treated_as_flat_market():
    weekly = make_ohlcv([100.0 + 0.001 * i for i in range(40)], freq="W")
    daily = make_ohlcv([100.0 + i for i in range(60)] + [156.0])

    sig = evaluate_asset("BTC", weekly, daily)

    assert sig.action == "stand_aside"
    assert sig.tide_trend == "neutral"
    assert sig.market_regime == "flat"
    assert "neutral" in sig.reason


def test_third_screen_times_the_entry():
    # The third screen's job is to tighten the stop-entry trigger: it is read off
    # the latest completed lower-timeframe bar instead of the wave bar.
    daily = DAILY_LONG
    four_h = make_ohlcv([150.0 + i * 0.1 for i in range(30)], freq="4h")

    sig = evaluate_asset("BTC", WEEKLY_UP, daily, four_h)

    last_4h_high = float(four_h["high"].iloc[-1])
    assert sig.entry_impulse in {"green", "red", "blue"}
    assert sig.entry == pytest.approx(last_4h_high + tick_size(last_4h_high))


def test_third_screen_impulse_never_vetoes_the_trade():
    # Regression guard. The Triple Screen buys INTO a pullback, so the entry
    # timeframe is red exactly when the setup is valid — censoring on it would
    # cancel the very setups the second screen just found. CLAUDE.md scopes
    # Impulse censorship to screens 1 and 2; the third screen only times entries.
    daily = DAILY_LONG
    falling_4h = make_ohlcv([180.0 - 0.1 * i * i for i in range(30)], freq="4h")

    without = evaluate_asset("BTC", WEEKLY_UP, daily)
    with_red_entry = evaluate_asset("BTC", WEEKLY_UP, daily, falling_4h)

    assert without.action == "long"
    assert with_red_entry.entry_impulse == "red"
    assert with_red_entry.action == "long"  # NOT vetoed
    assert with_red_entry.entry is not None and with_red_entry.stop is not None
    # The color is still surfaced to the operator, just never acted on.
    assert "third-screen" not in with_red_entry.reason


def test_third_screen_impulse_never_vetoes_a_short():
    # Mirror image: a short is sold into a bounce, so the entry timeframe is green.
    daily = DAILY_SHORT
    rising_4h = make_ohlcv([200.0 + 0.1 * i * i for i in range(30)], freq="4h")

    with_green_entry = evaluate_asset("ETH", WEEKLY_DOWN, daily, rising_4h)

    assert with_green_entry.entry_impulse == "green"
    assert with_green_entry.action == "short"  # NOT vetoed


def test_signal_carries_its_horizon_and_interval_labels():
    intervals = {"tide": "4h", "wave": "1h", "entry": "15m"}
    sig = evaluate_asset("BTC", WEEKLY_UP, DAILY_LONG, horizon="scalp", intervals=intervals)

    assert sig.horizon == "scalp"
    assert sig.intervals == intervals
    assert sig.tide_bars == len(WEEKLY_UP)
    assert sig.wave_bars == len(DAILY_LONG)
    # Reasons and the order plan speak the horizon's own timeframes.
    assert "1h bar" in (sig.entry_order_plan or "")


def test_data_warning_on_short_tide_history():
    # The EMA26 is seeded at the first bar, so a short tide series is mostly seed:
    # the tide direction and its Impulse are not yet meaningful. Flag, don't block.
    short_tide = make_ohlcv([100.0 + 2 * i for i in range(28)], freq="W")

    warns = data_warnings(short_tide, DAILY_LONG, min_tide_bars=60)
    assert any("not converged" in w for w in warns)
    assert any("28" in w for w in warns)

    # Enough history -> no history warning.
    assert not any("not converged" in w for w in data_warnings(WEEKLY_UP, DAILY_LONG))


def test_data_warning_on_a_near_frozen_market():
    # A tradfi perp over the weekend still prints bars, on a fraction of normal
    # volume. Force Index is volume-scaled, so those bars flatten everything.
    closes = [100.0 + 0.5 * i for i in range(60)]
    volumes = [1000.0] * 54 + [30.0] * 6  # last 6 bars at 3% of normal
    quiet = make_ohlcv(closes, volumes=volumes)

    warns = data_warnings(WEEKLY_UP, quiet)
    assert any("near-closed" in w for w in warns)

    assert not any("near-closed" in w for w in data_warnings(WEEKLY_UP, DAILY_LONG))


def test_data_warning_survives_a_whole_frozen_weekend():
    # A tradfi weekend lasts ~49h: ~196 bars on a 15m wave. The "normal volume"
    # baseline must span far more than that, or by Saturday it is itself made of
    # weekend bars and the near-frozen flag switches off (live xyz:GOLD 15m: only
    # 5 of 192 weekend bars flagged with a 60-bar baseline).
    n_week, n_weekend = 1000, 150
    closes = [100.0 + 0.01 * i for i in range(n_week + n_weekend)]
    volumes = [1000.0] * n_week + [30.0] * n_weekend  # weekend at 3% of normal
    frozen = make_ohlcv(closes, volumes=volumes, freq="15min")

    warns = data_warnings(WEEKLY_UP, frozen)
    assert any("near-closed" in w for w in warns)


def test_data_warnings_do_not_change_the_action():
    # Flags describe the inputs; they never censor. Same bars, same verdict.
    loud = evaluate_asset("BTC", WEEKLY_UP, DAILY_LONG, min_tide_bars=9999)

    assert loud.data_warnings  # the flag fired
    assert loud.action == evaluate_asset("BTC", WEEKLY_UP, DAILY_LONG).action


def test_safezone_initial_stop_uses_average_adverse_noise():
    closes = [100.0] * 47
    lows = [100.0] * 47
    lows[-5], lows[-3], lows[-2] = 98.0, 97.0, 99.0
    highs = [101.0] * 47
    daily = make_ohlcv(closes, lows=lows, highs=highs)

    entry, _limit, stop, _target = _long_levels(WEEKLY_UP, daily)

    # Two pullbacks below the EMA: 2, then max(3, 1).
    assert average_penetration(daily, "down") == pytest.approx(2.5)
    assert average_adverse_noise(daily, "long", 20) == pytest.approx(2.5)
    assert stop == pytest.approx(min(lows[-2:]) - 2.5 * 2.0)
    assert entry > stop


def test_safezone_short_stop_uses_wider_factor():
    # Elder p.220: shorts use a wider SafeZone factor (3) than longs (2), since
    # shorting near the highs is noisier and downtrends move faster.
    highs = [100.0] * 47
    highs[-5], highs[-3], highs[-2] = 102.0, 103.0, 101.0  # upside noise 2 and 3 -> avg 2.5
    daily = make_ohlcv([100.0] * 47, lows=[99.0] * 47, highs=highs)

    assert average_adverse_noise(daily, "short", 20) == pytest.approx(2.5)
    base = max(highs[-2:])  # SafeZone trails behind the recent two-bar high
    assert safezone_initial_stop(daily, "short") == pytest.approx(base + 2.5 * 3.0)


def test_detect_divergences_flags_bullish_force_index_divergence():
    closes = (
        [120.0 - i for i in range(30)]
        + [91.0 + 0.5 * i for i in range(10)]
        + [95.0 - i for i in range(10)]
    )
    # The second lower low prints on much lighter volume, so Force Index improves.
    volumes = [1000.0] * 30 + [900.0] * 10 + [100.0] * 10
    daily = make_ohlcv(closes, volumes=volumes)

    divs = detect_divergences(daily)

    assert any(d.startswith("bullish") for d in divs)
