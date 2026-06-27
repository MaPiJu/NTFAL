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
    _divergence_for_indicator,
    _long_levels,
    average_adverse_noise,
    average_penetration,
    compute_quality_score,
    detect_divergences,
    evaluate_asset,
    impulse_confirmation,
    projected_ema,
    safezone_initial_stop,
    safezone_stop_for_limit,
    select_best,
    tick_size,
    value_zone_extension,
    value_zone_status,
    weekly_channel,
    weekly_channel_with_params,
    weekly_trend,
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


def test_tick_size():
    assert tick_size(159.5) == pytest.approx(0.01)
    assert tick_size(95000.0) == pytest.approx(1.0)
    assert tick_size(0.234) == pytest.approx(0.00001)
    with pytest.raises(ValueError):
        tick_size(0)


def test_weekly_trend():
    assert weekly_trend(WEEKLY_UP["close"]) == "up"
    assert weekly_trend(WEEKLY_DOWN["close"]) == "down"
    assert weekly_trend(WEEKLY_FLAT["close"]) == "neutral"


def test_uptrend_pullback_goes_long():
    # healthy uptrend with a prior pullback, then a mild pullback to value ->
    # 2-EMA Force Index dips below zero without printing a new multi-week low
    daily = DAILY_LONG

    sig = evaluate_asset("BTC", WEEKLY_UP, daily)

    assert sig.action == "long"
    assert sig.weekly_trend == "up"
    assert sig.force_index_2 < 0
    assert sig.daily_impulse != "red"  # otherwise the veto would apply
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


def test_value_zone_filter_rejects_extended_long_above_value():
    # Strong uptrend; the last bar dips (Force Index < 0) but price is still far
    # ABOVE the EMA13-EMA26 value zone — Elder calls that chasing. An earlier,
    # deeper dip keeps today's Force Index off a new multi-week low, so it is the
    # *value-zone* veto (not the new-extreme filter) that stands us aside.
    daily = make_ohlcv(
        [100.0 + 2 * i for i in range(40)] + [166.0] + [168.0 + 2 * i for i in range(12)] + [186.0]
    )
    params = StrategyParams(value_zone_max_distance_pct=0.0)

    assert value_zone_status(daily, max_distance_pct=0.0) == "extended"
    assert value_zone_extension(daily) == "above"
    sig = evaluate_asset("BTC", WEEKLY_UP, daily, params=params)

    assert sig.action == "stand_aside"
    assert sig.value_zone_status == "extended"
    assert "value zone" in sig.reason and "chasing" in sig.reason


def test_force_index_new_multiweek_low_blocks_long():
    # Gentle uptrend, then a sharp final dip that prints a fresh multi-week low of
    # the 2-day Force Index. Elder (p.158): a buy signal is void when the Force
    # Index falls to a new multi-week low — the decline is accelerating.
    closes = [100.0 + 0.7 * i for i in range(50)]
    closes.append(closes[-1] - 3.0)
    daily = make_ohlcv(closes)

    blocked = evaluate_asset("BTC", WEEKLY_UP, daily)
    assert blocked.action == "stand_aside"
    assert "multi-week low" in blocked.reason
    assert blocked.entry is None

    # Disable the caveat (lookback 0) and the very same bar is a tradable long,
    # proving the new-extreme filter — not Impulse or the value zone — blocked it.
    off = StrategyParams(force_index_extreme_lookback_days=0)
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
    params = StrategyParams(value_zone_max_distance_pct=0.0)

    assert value_zone_extension(daily) == "below"
    assert value_zone_status(daily, max_distance_pct=0.0) == "extended"
    sig = evaluate_asset("BTC", WEEKLY_UP, daily, params=params)

    assert sig.action == "long"
    assert "pullback to buy" in sig.reason
    assert sig.entry is not None and sig.stop is not None


def test_divergence_requires_zero_line_crossover():
    # Two successively lower price lows with a shallower second indicator low is
    # the divergence *shape* — but Elder (p.103) requires the indicator to cross
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


def test_divergence_requires_minimum_separation():
    # Same valid bullish shape (crosses zero between the two lows), but the lows are
    # only 10 bars apart — below Elder/Lovvorn's 20-bar floor (p.104), so it is not
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


def test_entry_order_plan_rolls_and_expires():
    sig = evaluate_asset("BTC", WEEKLY_UP, DAILY_LONG)

    assert sig.entry_order_plan is not None
    assert "roll it daily" in sig.entry_order_plan
    assert "expire after" in sig.entry_order_plan


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
    assert sig.weekly_trend == "down"
    assert sig.force_index_2 > 0
    assert sig.daily_impulse != "green"
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
    assert sig.weekly_trend == "neutral"
    assert "neutral" in sig.reason


def test_impulse_red_vetoes_long():
    # weekly tide up and FI(2) below zero (the table says long), but a sustained
    # daily decline turns the daily Impulse red. An earlier high-volume down spike
    # keeps FI off a new multi-week low, so it is the Impulse censor — not the
    # new-extreme filter — that vetoes the long.
    daily = DAILY_RED

    sig = evaluate_asset("BTC", WEEKLY_UP, daily)

    assert sig.daily_impulse == "red"
    assert sig.force_index_2 < 0
    assert sig.action == "stand_aside"
    assert "vetoed by Impulse" in sig.reason
    assert sig.entry is None


def test_weekly_channel_lower_band_stays_positive_after_a_crash():
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

    upper, lower = weekly_channel(weekly)
    e26 = float(ema(weekly["close"], EMA_SLOW).iloc[-1])  # Elder's channel backbone

    assert lower > 0  # the bug was a negative lower band (negative short target)
    assert lower < e26 <= upper


def test_weekly_channel_backbone_is_slow_ema26():
    # Elder draws the channel parallel to the SLOW EMA26, not the fast EMA13. In a
    # clean uptrend the lows stay above the slow EMA, so the lower band collapses
    # onto the backbone — pinning it to EMA26.
    weekly = make_ohlcv([100.0 + 2.0 * i for i in range(40)], freq="W")
    _upper, lower = weekly_channel(weekly)
    e13 = float(ema(weekly["close"], EMA_FAST).iloc[-1])
    e26 = float(ema(weekly["close"], EMA_SLOW).iloc[-1])

    assert lower == pytest.approx(e26)
    assert lower != pytest.approx(e13)


def test_weekly_channel_widens_with_containment():
    # Higher containment -> wider channel (Elder fits ~95%, p.183). Up-excursions
    # spike every 5th bar, so the 95th percentile sits well above the median.
    highs = [100.0 + (10.0 if i % 5 == 0 else 1.0) for i in range(40)]
    weekly = make_ohlcv([100.0] * 40, lows=[100.0] * 40, highs=highs, freq="W")

    narrow_upper, _ = weekly_channel_with_params(weekly, StrategyParams(channel_containment=0.50))
    wide_upper, _ = weekly_channel_with_params(weekly, StrategyParams(channel_containment=0.95))
    assert wide_upper > narrow_upper


def test_quality_score_rewards_better_reward_risk():
    base = dict(impulse_agreement=1.0, tide_strength=0.03, pullback_depth=1.0)
    better = compute_quality_score(reward_risk=3.0, **base)
    worse = compute_quality_score(reward_risk=2.0, **base)
    assert 0.0 <= worse < better <= 1.0


def test_impulse_confirmation_counts_agreeing_screens():
    assert impulse_confirmation("long", "green", "green") == 1.0
    assert impulse_confirmation("long", "green", "blue") == 0.5
    assert impulse_confirmation("long", "blue", "blue") == 0.0
    assert impulse_confirmation("short", "red", "red") == 1.0
    assert impulse_confirmation("short", "red", "blue") == 0.5


def _mk_signal(asset: str, action: str, rr: float | None, score: float | None, rr_ok: bool):
    return Signal(
        asset=asset,
        action=action,
        reason="",
        weekly_trend="up",
        weekly_impulse="green",
        daily_impulse="green",
        force_index_2=-1.0,
        entry=10.0,
        entry_limit=None,
        stop=9.0,
        target=13.0,
        reward_risk=rr,
        rr_ok=rr_ok,
        weekly_trend_strength=0.02,
        pullback_quality=0.5,
        quality_score=score,
        market_regime="trending",
        third_screen_impulse=None,
        divergences=[],
        value_zone_status="in_value",
        entry_order_plan=None,
    )


def test_select_best_picks_highest_quality_tradable_above_floor():
    a = _mk_signal("A", "long", 2.5, 0.60, rr_ok=True)
    b = _mk_signal("B", "long", 3.5, 0.90, rr_ok=True)  # best
    c = _mk_signal("C", "stand_aside", None, None, rr_ok=False)  # not tradable
    d = _mk_signal("D", "long", 1.5, 0.95, rr_ok=False)  # below the 2:1 floor
    assert select_best([a, b, c, d]).asset == "B"


def test_select_best_returns_none_when_nothing_qualifies():
    only_aside = _mk_signal("A", "stand_aside", None, None, rr_ok=False)
    sub_floor = _mk_signal("B", "long", 1.2, 0.9, rr_ok=False)
    assert select_best([only_aside, sub_floor]) is None


def test_average_penetration_and_projection():
    lows = [100.0] * 47
    lows[-5], lows[-3], lows[-2] = 98.0, 97.0, 99.0  # penetrations: 2, 3, 1
    highs = [100.0] * 47
    daily = make_ohlcv([100.0] * 47, lows=lows, highs=highs)

    assert average_penetration(daily, "down") == pytest.approx(2.0)
    assert average_penetration(daily, "up") is None  # highs never pierce the EMA
    assert projected_ema(daily["close"]) == pytest.approx(100.0)


def test_tiny_weekly_slope_is_treated_as_flat_market():
    weekly = make_ohlcv([100.0 + 0.001 * i for i in range(40)], freq="W")
    daily = make_ohlcv([100.0 + i for i in range(60)] + [156.0])

    sig = evaluate_asset("BTC", weekly, daily)

    assert sig.action == "stand_aside"
    assert sig.weekly_trend == "neutral"
    assert sig.market_regime == "flat"
    assert "neutral" in sig.reason


def test_optional_4h_third_screen_sets_entry_and_can_veto():
    daily = DAILY_LONG
    four_h = make_ohlcv([150.0 + i * 0.1 for i in range(30)], freq="4h")

    sig = evaluate_asset("BTC", WEEKLY_UP, daily, four_h)

    last_4h_high = float(four_h["high"].iloc[-1])
    assert sig.third_screen_impulse in {"green", "red", "blue"}
    assert sig.entry == pytest.approx(last_4h_high + tick_size(last_4h_high))

    falling_4h = make_ohlcv([180.0 - 0.1 * i * i for i in range(30)], freq="4h")
    vetoed = evaluate_asset("BTC", WEEKLY_UP, daily, falling_4h)
    assert vetoed.action == "stand_aside"
    assert "4h third-screen" in vetoed.reason


def test_safezone_initial_stop_uses_average_adverse_noise():
    closes = [100.0] * 47
    lows = [100.0] * 47
    lows[-5], lows[-3], lows[-2] = 98.0, 97.0, 99.0
    highs = [101.0] * 47
    daily = make_ohlcv(closes, lows=lows, highs=highs)

    entry, _limit, stop, _target = _long_levels(WEEKLY_UP, daily)

    assert average_penetration(daily, "down") == pytest.approx(2.0)
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
