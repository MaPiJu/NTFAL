"""Strategy tests on crafted uptrend / downtrend / range fixtures."""

from __future__ import annotations

import pytest

from strategy.triple_screen import (
    average_penetration,
    evaluate_asset,
    projected_ema,
    tick_size,
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
    # protective stop one tick below the lower of the last two daily lows
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


def test_average_penetration_and_projection():
    lows = [100.0] * 47
    lows[-5], lows[-3], lows[-2] = 98.0, 97.0, 99.0  # penetrations: 2, 3, 1
    highs = [100.0] * 47
    daily = make_ohlcv([100.0] * 47, lows=lows, highs=highs)

    assert average_penetration(daily, "down") == pytest.approx(2.0)
    assert average_penetration(daily, "up") is None  # highs never pierce the EMA
    assert projected_ema(daily["close"]) == pytest.approx(100.0)
