"""Strategy tests on crafted uptrend / downtrend / range fixtures."""

from __future__ import annotations

import pytest

from indicators import ema
from strategy.triple_screen import (
    EMA_FAST,
    Signal,
    _long_levels,
    average_penetration,
    compute_quality_score,
    detect_divergences,
    evaluate_asset,
    impulse_confirmation,
    projected_ema,
    select_best,
    tick_size,
    weekly_channel,
    weekly_trend,
)
from tests.conftest import make_ohlcv

WEEKLY_UP = make_ohlcv([100.0 + 2 * i for i in range(40)], freq="W")
WEEKLY_DOWN = make_ohlcv([300.0 - 2 * i for i in range(40)], freq="W")
WEEKLY_FLAT = make_ohlcv([100.0] * 40, freq="W")


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
    # steady daily uptrend, last bar dips -> 2-EMA Force Index below zero
    closes = [100.0 + i for i in range(60)] + [156.0]
    daily = make_ohlcv(closes)

    sig = evaluate_asset("BTC", WEEKLY_UP, daily)

    assert sig.action == "long"
    assert sig.weekly_trend == "up"
    assert sig.force_index_2 < 0
    assert sig.daily_impulse != "red"  # otherwise the veto would apply
    # buy-stop one tick above the prior day's high
    prior_high = float(daily["high"].iloc[-1])
    assert sig.entry == pytest.approx(prior_high + tick_size(prior_high))
    # no EMA penetration in this synthetic trend: SafeZone falls back to one tick
    expected_stop = float(daily["low"].iloc[-2:].min()) - tick_size(prior_high)
    assert sig.stop == pytest.approx(expected_stop)
    assert sig.target is not None and sig.target > sig.entry
    assert sig.reward_risk is not None and sig.reward_risk > 0
    # strong trend: lows never pierce EMA13, so no penetration-based limit
    assert sig.entry_limit is None


def test_uptrend_without_pullback_stands_aside():
    daily = make_ohlcv([100.0 + i for i in range(60)])  # FI(2) positive: chasing
    sig = evaluate_asset("BTC", WEEKLY_UP, daily)
    assert sig.action == "stand_aside"
    assert "chasing" in sig.reason
    assert sig.entry is None and sig.stop is None and sig.target is None


def test_downtrend_rally_goes_short():
    # steady fall, then a two-bar rally -> FI(2) above zero
    closes = [300.0 - 2 * i for i in range(50)] + [205.0, 208.0]
    daily = make_ohlcv(closes)

    sig = evaluate_asset("ETH", WEEKLY_DOWN, daily)

    assert sig.action == "short"
    assert sig.weekly_trend == "down"
    assert sig.force_index_2 > 0
    assert sig.daily_impulse != "green"
    prior_low = float(daily["low"].iloc[-1])
    assert sig.entry == pytest.approx(prior_low - tick_size(prior_low))
    expected_stop = float(daily["high"].iloc[-2:].max()) + tick_size(prior_low)
    assert sig.stop == pytest.approx(expected_stop)
    assert sig.target is not None
    # in this steep synthetic fall, price is far below weekly value -> R:R flagged
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
    # weekly tide up, daily FI(2) negative -> table says long...
    closes = [100.0 + i for i in range(60)]
    for j in range(1, 16):  # accelerating sell-off: EMA13 and MACD-hist both fall
        closes.append(closes[-1] - j)
    daily = make_ohlcv(closes)

    sig = evaluate_asset("BTC", WEEKLY_UP, daily)

    # ...but the hard daily sell-off turns the daily Impulse red -> vetoed.
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
    ema_last = float(ema(weekly["close"], EMA_FAST).iloc[-1])

    assert lower > 0  # the bug was a negative lower band (negative short target)
    assert lower < ema_last <= upper


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
    daily = make_ohlcv([100.0 + i for i in range(60)] + [156.0])
    four_h = make_ohlcv([150.0 + i * 0.1 for i in range(30)], freq="4h")

    sig = evaluate_asset("BTC", WEEKLY_UP, daily, four_h)

    last_4h_high = float(four_h["high"].iloc[-1])
    assert sig.third_screen_impulse in {"green", "red", "blue"}
    assert sig.entry == pytest.approx(last_4h_high + tick_size(last_4h_high))

    falling_4h = make_ohlcv([180.0 - 0.1 * i * i for i in range(30)], freq="4h")
    vetoed = evaluate_asset("BTC", WEEKLY_UP, daily, falling_4h)
    assert vetoed.action == "stand_aside"
    assert "4h third-screen" in vetoed.reason


def test_safezone_initial_stop_uses_average_penetration():
    closes = [100.0] * 47
    lows = [100.0] * 47
    lows[-5], lows[-3], lows[-2] = 98.0, 97.0, 99.0
    highs = [101.0] * 47
    daily = make_ohlcv(closes, lows=lows, highs=highs)

    entry, _limit, stop, _target = _long_levels(WEEKLY_UP, daily)

    assert average_penetration(daily, "down") == pytest.approx(2.0)
    assert stop == pytest.approx(min(lows[-2:]) - 2.0)
    assert entry > stop


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
