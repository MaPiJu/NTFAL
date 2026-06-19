"""Elder trade-management tests on crafted open-position fixtures."""

from __future__ import annotations

import pytest

from strategy.trade_management import (
    OpenPosition,
    assess_position,
    parse_positions,
    safezone_stop,
)
from tests.conftest import make_clearinghouse_state, make_ohlcv

WEEKLY_UP = make_ohlcv([100.0 + 2 * i for i in range(40)], freq="W")
WEEKLY_DOWN = make_ohlcv([300.0 - 2 * i for i in range(40)], freq="W")


def _accel(start: float, n: int, step: float) -> list[float]:
    """Convex (accelerating) ramp: growing per-bar steps so the MACD-histogram
    keeps rising/falling — needed to produce a green/red Impulse (a linear ramp
    flattens the histogram and stays blue)."""
    out = [start]
    for j in range(1, n):
        out.append(out[-1] + j * step)
    return out


# Accelerating uptrend -> green Impulse on both the weekly and the daily.
WEEKLY_UP_GREEN = make_ohlcv(_accel(100.0, 40, 1.0), freq="W")


def test_parse_positions_signs_and_skips_flat():
    state = make_clearinghouse_state(
        [
            {"coin": "BTC", "szi": "0.5", "entryPx": "60000.0"},
            {"coin": "ETH", "szi": "-2.0", "entryPx": "3000.0"},
            {"coin": "SOL", "szi": "0.0", "entryPx": "150.0"},  # flat -> skipped
        ]
    )
    positions = parse_positions(state)
    assert [(p.asset, p.side, p.entry, p.size) for p in positions] == [
        ("BTC", "long", 60000.0, 0.5),
        ("ETH", "short", 3000.0, 2.0),
    ]


def test_parse_positions_empty():
    assert parse_positions({"assetPositions": []}) == []
    assert parse_positions({}) == []


def test_parse_positions_carries_live_price_and_pnl():
    state = make_clearinghouse_state(
        [
            {
                "coin": "NIL",
                "szi": "-4709.0",
                "entryPx": "0.0424",
                "positionValue": "189.0",
                "unrealizedPnl": "10.55",
            }
        ]
    )
    (pos,) = parse_positions(state)
    assert pos.unrealized_pnl == 10.55  # straight from the exchange
    assert pos.mark_price == pytest.approx(189.0 / 4709.0)  # positionValue / size


def test_live_and_elder_pnl_are_both_reported():
    daily = make_ohlcv([100.0 + i for i in range(60)])  # daily close 159
    pos = OpenPosition("BTC", "long", entry=120.0, size=2.0, mark_price=170.0, unrealized_pnl=99.0)
    tm = assess_position(pos, WEEKLY_UP_GREEN, daily)
    # Live view comes straight from the exchange.
    assert tm.live_price == 170.0
    assert tm.pnl_live == 99.0
    # Elder view uses the completed daily close (159), independent of the mark.
    assert tm.close_price == 159.0
    assert tm.pnl_elder == pytest.approx((159.0 - 120.0) * 2.0)
    assert tm.in_profit  # by the Elder PnL


def test_long_in_uptrend_with_green_impulse_holds():
    # accelerating climb -> EMA13 and MACD-hist both rising -> green; trend up.
    daily = make_ohlcv(_accel(100.0, 60, 0.1))  # ends ~277, still below weekly value
    pos = OpenPosition("BTC", "long", entry=150.0, size=1.0)

    tm = assess_position(pos, WEEKLY_UP_GREEN, daily)

    assert tm.weekly_trend == "up"
    assert tm.weekly_impulse == "green"
    assert tm.daily_impulse == "green"
    assert not tm.target_reached  # weekly value zone still overhead
    assert tm.verdict == "hold"
    assert tm.in_profit
    assert "hold" in tm.reasons[0]


def test_long_takes_profit_when_impulse_loses_green_in_profit():
    # climb, then a flat/soft top so the daily Impulse drops from green to blue
    # while the weekly tide is still up and the position is in profit.
    closes = [100.0 + i for i in range(50)] + [149.0, 148.5, 148.6, 148.4]
    daily = make_ohlcv(closes)
    pos = OpenPosition("BTC", "long", entry=120.0, size=1.0)

    tm = assess_position(pos, WEEKLY_UP, daily)

    assert tm.daily_impulse != "green"  # momentum stalled
    assert tm.in_profit
    assert tm.verdict == "take_profits"
    assert any("permission to take profits" in r for r in tm.reasons)


def test_long_exits_when_daily_impulse_turns_red():
    # hard sell-off turns the daily Impulse red -> momentum reversed -> exit.
    closes = [100.0 + i for i in range(50)]
    for j in range(1, 16):
        closes.append(closes[-1] - j)
    daily = make_ohlcv(closes)
    pos = OpenPosition("BTC", "long", entry=120.0, size=1.0)

    tm = assess_position(pos, WEEKLY_UP, daily)

    assert tm.daily_impulse == "red"
    assert tm.verdict == "exit"
    assert any("momentum reversed" in r for r in tm.reasons)


def test_long_exits_when_weekly_tide_flips_down():
    # the strategic premise (weekly up) is gone -> exit regardless of daily.
    daily = make_ohlcv([100.0 + i for i in range(60)])
    pos = OpenPosition("BTC", "long", entry=120.0, size=1.0)

    tm = assess_position(pos, WEEKLY_DOWN, daily)

    assert tm.weekly_trend == "down"
    assert tm.verdict == "exit"
    assert any("tide flipped" in r for r in tm.reasons)


def test_short_in_downtrend_with_red_impulse_holds():
    # accelerating decline that stays high (ends ~285) so the daily Impulse is red
    # but price has not reached the weekly value-zone target below.
    daily = make_ohlcv(_accel(320.0, 60, -0.02))
    pos = OpenPosition("ETH", "short", entry=300.0, size=1.0)

    tm = assess_position(pos, WEEKLY_DOWN, daily)

    assert tm.weekly_trend == "down"
    assert tm.daily_impulse == "red"  # favorable for a short -> hold
    assert not tm.target_reached
    assert tm.verdict == "hold"
    assert tm.in_profit  # price ~285 < entry 300


def test_short_exits_when_weekly_tide_flips_up():
    daily = make_ohlcv([300.0 - 2 * i for i in range(60)])
    pos = OpenPosition("ETH", "short", entry=260.0, size=1.0)

    tm = assess_position(pos, WEEKLY_UP, daily)

    assert tm.verdict == "exit"
    assert any("tide flipped" in r for r in tm.reasons)


def test_pnl_sign_by_side():
    daily = make_ohlcv([100.0 + i for i in range(60)])  # last close 159
    long = assess_position(OpenPosition("BTC", "long", 120.0, 2.0), WEEKLY_UP, daily)
    assert long.pnl_elder == pytest.approx((159.0 - 120.0) * 2.0)
    assert long.return_pct_elder == pytest.approx(159.0 / 120.0 - 1.0)

    short = assess_position(OpenPosition("BTC", "short", 120.0, 2.0), WEEKLY_UP, daily)
    assert short.pnl_elder == pytest.approx((159.0 - 120.0) * 2.0 * -1.0)
    assert short.return_pct_elder == pytest.approx(1.0 - 159.0 / 120.0)


def test_safezone_stop_ratchets_to_breakeven_in_profit():
    daily = make_ohlcv([100.0 + i for i in range(60)])  # recent lows ~156
    # Entry just under the current price but ABOVE the SafeZone level, so the
    # break-even floor actually bites.
    pos = OpenPosition("BTC", "long", entry=158.0, size=1.0)
    in_profit = safezone_stop(pos, daily, in_profit=True)
    underwater = safezone_stop(pos, daily, in_profit=False)
    # In profit the long stop is lifted to at least break-even (no giving back).
    assert in_profit >= 158.0
    assert underwater < in_profit


def test_long_takes_profit_when_target_reached():
    # Weekly value zone sits in the 30s-50s; the daily has run far above it (~277),
    # so the profit target is behind us -> take profits even with momentum intact.
    weekly = make_ohlcv(_accel(30.0, 40, 0.04), freq="W")  # gentle up, value zone ~40s
    daily = make_ohlcv(_accel(100.0, 60, 0.1))  # ends ~277
    pos = OpenPosition("BTC", "long", entry=35.0, size=1.0)

    tm = assess_position(pos, weekly, daily)

    assert tm.weekly_trend == "up"
    assert tm.target_reached
    assert tm.verdict == "take_profits"
    assert any("target" in r for r in tm.reasons)
