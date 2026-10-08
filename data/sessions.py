"""Trading sessions of the tradfi perps: bars from a closed market are not a market.

Hyperliquid's perps print bars 24/7, but the markets behind the `xyz` dex
(gold, oil, indices…) close for the weekend: those bars carry a fraction of the
normal volume and range and flatten every EMA. Elder counts sessions (p.125:
five trading days a week), so a dex can be given a weekend closure: bars that
sit entirely inside it are dropped before any indicator is computed, and weekly
bars are rebuilt from the Monday-Friday daily bars — Hyperliquid's own 1w
candles open on Thursday (epoch alignment) and carry the weekend.

Weekday holidays are not in the calendar; the near-frozen-market data warning
covers them. All arithmetic is on the integer `t` / `T` columns (open time and
inclusive close time, ms UTC), independent of pandas' datetime resolution.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from data.hyperliquid import CANDLE_COLUMNS, INTERVAL_MS

DAY_MS = INTERVAL_MS["1d"]
MINUTE_MS = 60_000
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _week_start(t_ms: pd.Series) -> pd.Series:
    """Monday 00:00 UTC of the week each timestamp falls in (1970-01-01 was a Thursday)."""
    day = t_ms // DAY_MS
    return (day - (day + 3) % 7) * DAY_MS


@dataclass(frozen=True)
class WeekendClosure:
    """When a dex's markets are closed each week, as minutes after Monday 00:00 UTC."""

    close_minute: int
    open_minute: int

    @classmethod
    def parse(cls, close: str, reopen: str) -> WeekendClosure:
        """From "Fri 21:00"-style weekday + UTC time strings; the closure must not
        straddle Monday 00:00."""
        start, end = _minute_of_week(close), _minute_of_week(reopen)
        if start >= end:
            raise ValueError(f"the close ({close!r}) must come before the reopening ({reopen!r})")
        return cls(close_minute=start, open_minute=end)


def _minute_of_week(text: str) -> int:
    try:
        day, clock = text.strip().lower().split()
        hours, minutes = (int(x) for x in clock.split(":"))
    except ValueError as exc:
        raise ValueError(
            f"expected a weekday and a UTC time like 'Fri 21:00', got {text!r}"
        ) from exc
    if day not in WEEKDAYS:
        raise ValueError(f"unknown weekday {day!r} in {text!r} (use {', '.join(WEEKDAYS)})")
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        raise ValueError(f"not a time of day: {clock!r}")
    return WEEKDAYS.index(day) * 1440 + hours * 60 + minutes


def drop_closed_bars(bars: pd.DataFrame, closure: WeekendClosure | None) -> pd.DataFrame:
    """`bars` without those lying entirely inside the weekend closure.

    A bar that straddles the close or the reopening (a Friday daily bar, the
    Sunday 22:00 hourly bar) holds session trading and is kept. No closure: the
    frame is returned as is.
    """
    if closure is None or bars.empty:
        return bars
    week = _week_start(bars["t"])
    starts_closed = bars["t"] - week >= closure.close_minute * MINUTE_MS
    ends_closed = bars["T"] + 1 - week <= closure.open_minute * MINUTE_MS
    return bars[~(starts_closed & ends_closed)]


def weekly_from_weekdays(daily: pd.DataFrame) -> pd.DataFrame:
    """Weekly bars built from the Monday-Friday daily bars, one per week opening
    Monday 00:00 UTC and closing with Friday's bar (`T` = Friday 23:59:59.999, so
    `completed_bars` drops the week still in progress). Saturday and Sunday bars
    never reach a weekly bar.
    """
    if daily.empty:
        return daily
    week = _week_start(daily["t"])
    monday_to_friday = daily["t"] - week < 5 * DAY_MS
    weekdays = daily[monday_to_friday]
    groups = weekdays.groupby(week[monday_to_friday].to_numpy(), sort=True)
    weekly = pd.DataFrame(
        {
            "open": groups["open"].first(),
            "high": groups["high"].max(),
            "low": groups["low"].min(),
            "close": groups["close"].last(),
            "volume": groups["volume"].sum(),
            "trades": groups["trades"].sum(),
        }
    )
    t = weekly.index.to_numpy(dtype="int64")
    weekly["t"] = t
    weekly["T"] = t + 5 * DAY_MS - 1
    weekly.index = pd.to_datetime(t, unit="ms", utc=True)
    weekly.index.name = "time"
    return weekly[list(CANDLE_COLUMNS)]
