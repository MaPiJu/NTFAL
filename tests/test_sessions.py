"""Trading sessions of the tradfi perps — fixtures only, no live network."""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import pytest

from data.hyperliquid import completed_bars, parse_candles
from data.sessions import WeekendClosure, drop_closed_bars, weekly_from_weekdays
from strategy.triple_screen import data_warnings
from tests.conftest import make_ohlcv, synthetic_candles

# A closure stated in UTC: Friday 21:00 to Sunday 22:00.
XYZ_WEEKEND = WeekendClosure.parse("Fri 21:00", "Sun 22:00")
# The xyz dex's actual schedule (trade.xyz's external price, the CME's hours):
# Friday 17:00 to Sunday 18:00 New York time — the window above in summer time.
NEW_YORK_WEEKEND = WeekendClosure.parse("Fri 17:00", "Sun 18:00", "America/New_York")


def ms(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp() * 1000)


def bars(interval: str, first: str, count: int, **kwargs) -> pd.DataFrame:
    """`count` real-shaped candles (T = t + interval - 1 ms) from `first` onward."""
    step = {"15m": 900_000, "1h": 3_600_000, "1d": 86_400_000}[interval]
    end = ms(first) + (count - 1) * step
    return parse_candles(synthetic_candles(interval, count, end_ms=end, **kwargs))


def starts(frame: pd.DataFrame) -> list[str]:
    return [datetime.fromtimestamp(t / 1000, UTC).strftime("%a %H:%M") for t in frame["t"]]


def test_closure_parses_weekday_and_time():
    assert (
        WeekendClosure(close_minute=4 * 1440 + 21 * 60, open_minute=6 * 1440 + 22 * 60)
        == XYZ_WEEKEND
    )
    with pytest.raises(ValueError, match="weekday"):
        WeekendClosure.parse("Fry 21:00", "Sun 22:00")
    with pytest.raises(ValueError, match="before"):
        WeekendClosure.parse("Sun 22:00", "Fri 21:00")


def test_hourly_bars_inside_the_weekend_close_are_dropped():
    # Mon 2026-09-28 00:00 to Mon 2026-10-05 23:00: 192 hourly bars. Those that sit
    # ENTIRELY inside Fri 21:00 -> Sun 22:00 go: Fri 21:00-23:00 (3), Saturday (24)
    # and Sunday 00:00-21:00 (22) = 49 bars.
    hourly = bars("1h", "2026-09-28T00:00", 192)
    kept = drop_closed_bars(hourly, XYZ_WEEKEND)

    assert len(hourly) - len(kept) == 49
    labels = starts(kept)
    assert "Fri 20:00" in labels  # ends at 21:00, the close: still a session bar
    assert "Fri 21:00" not in labels and "Sat 12:00" not in labels
    assert "Sun 21:00" not in labels
    assert "Sun 22:00" in labels  # the reopening


def test_a_new_york_closure_follows_daylight_saving():
    # Summer time (EDT, UTC-4): Fri 17:00 -> Sun 18:00 New York is exactly
    # Fri 21:00 -> Sun 22:00 UTC.
    summer = bars("1h", "2026-09-28T00:00", 192)
    assert starts(drop_closed_bars(summer, NEW_YORK_WEEKEND)) == starts(
        drop_closed_bars(summer, XYZ_WEEKEND)
    )

    # Winter time (EST, UTC-5, from 2026-11-01): the closure is an hour later in
    # UTC — Friday's 21:00 UTC hour is the last live CME hour, Sunday's 22:00 UTC
    # hour is still closed.
    winter = drop_closed_bars(bars("1h", "2026-11-02T00:00", 192), NEW_YORK_WEEKEND)
    labels = starts(winter)
    assert "Fri 21:00" in labels and "Fri 22:00" not in labels
    assert "Sun 22:00" not in labels and "Sun 23:00" in labels
    assert len(labels) == 192 - 49

    # The week DST ends (Sunday 2026-11-01 02:00): it closes Fri 21:00 UTC (EDT)
    # and reopens Sun 23:00 UTC (EST) — 50 hourly bars dropped.
    switch = bars("1h", "2026-10-26T00:00", 192)
    assert len(switch) - len(drop_closed_bars(switch, NEW_YORK_WEEKEND)) == 50


def test_closure_rejects_an_unknown_timezone():
    with pytest.raises(ValueError, match="time zone"):
        WeekendClosure.parse("Fri 17:00", "Sun 18:00", "America/Atlantis")


def test_daily_bars_keep_friday_and_sunday_but_not_saturday():
    # Friday trades until 21:00 and Sunday reopens at 22:00: only Saturday's daily
    # bar sits entirely inside the closure.
    daily = bars("1d", "2026-09-28T00:00", 8)
    assert starts(drop_closed_bars(daily, XYZ_WEEKEND)) == [
        "Mon 00:00",
        "Tue 00:00",
        "Wed 00:00",
        "Thu 00:00",
        "Fri 00:00",
        "Sun 00:00",
        "Mon 00:00",
    ]


def test_no_closure_means_no_filter():
    hourly = bars("1h", "2026-09-28T00:00", 192)
    assert drop_closed_bars(hourly, None) is hourly


def test_weekly_tide_is_built_from_monday_to_friday_daily_bars():
    # Hyperliquid's 1w candles open on Thursday (epoch alignment) and carry the
    # weekend. The tide's weekly bars are rebuilt from Monday-Friday daily bars.
    # 25 daily bars, Mon 2026-09-14 to Thu 2026-10-08; Saturday spikes to 999 and
    # Sunday carries a huge volume, neither of which may reach a weekly bar.
    daily = bars("1d", "2026-09-14T00:00", 25)
    weekday = (pd.to_datetime(daily["t"], unit="ms", utc=True).dt.weekday).to_numpy()
    daily.loc[weekday == 5, "high"] = 999.0
    daily.loc[weekday == 6, "volume"] = 1e9

    weekly = weekly_from_weekdays(daily)

    assert starts(weekly) == ["Mon 00:00"] * 4
    assert weekly["t"].iloc[0] == ms("2026-09-14T00:00")
    first_week = daily.iloc[0:5]  # Mon-Fri
    row = weekly.iloc[0]
    assert row["open"] == first_week["open"].iloc[0]
    assert row["close"] == first_week["close"].iloc[-1]
    assert row["high"] == first_week["high"].max() < 999.0
    assert row["low"] == first_week["low"].min()
    assert row["volume"] == first_week["volume"].sum()
    # A weekly bar closes with Friday: the week in progress (Mon-Thu 2026-10-05..08)
    # is not a completed bar yet on Thursday.
    assert weekly["T"].iloc[0] == ms("2026-09-19T00:00") - 1
    done = completed_bars(weekly, now_ms=ms("2026-10-08T12:00"))
    assert len(done) == 3


def test_a_weekday_holiday_is_still_flagged_near_frozen():
    # The calendar only knows the weekend; a holiday (here a Wednesday printing at
    # 3% of normal volume) survives the filter and the near-frozen flag catches it.
    n = 1000
    closes = [100.0 + 0.01 * i for i in range(n)]
    quiet = make_ohlcv(closes, volumes=[1000.0] * (n - 6) + [30.0] * 6, freq="15min")
    quiet["t"] = ms("2026-09-23T12:00") - (n - 1 - pd.RangeIndex(n)) * 900_000
    quiet["T"] = quiet["t"] + 900_000 - 1

    kept = drop_closed_bars(quiet, XYZ_WEEKEND)
    assert kept["t"].iloc[-1] == quiet["t"].iloc[-1]  # Wednesday is a session day
    weekly = make_ohlcv([100.0 + 2 * i for i in range(40)], freq="W")
    assert any("near-closed" in w for w in data_warnings(weekly, kept))
