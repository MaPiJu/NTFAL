"""Trading sessions of the tradfi perps: bars from a closed market are not a market.

Hyperliquid's perps print bars 24/7, but the markets behind the `xyz` dex
(gold, oil, indices…) close for the weekend: those bars carry a fraction of the
normal volume and range and flatten every EMA. Elder counts sessions (p.125:
five trading days a week), so a dex can be given a weekend closure: bars that
sit entirely inside it are dropped before any indicator is computed, and weekly
bars are rebuilt from the Monday-Friday daily bars — Hyperliquid's own 1w
candles open on Thursday (epoch alignment) and carry the weekend.

Weekday holidays are not in the calendar; the near-frozen-market data warning
covers them. While a dex is closed, `closure_at` says so: its perps keep trading
on Hyperliquid, so a signal read off the last session bar gets a data warning.
All arithmetic is on the integer `t` / `T` columns (open time and inclusive
close time, ms UTC), independent of pandas' datetime resolution.
"""

from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    """When a dex's markets are closed each week: minutes after Monday 00:00 in
    `timezone` (an IANA name), so a closure stated in New York time follows
    daylight saving — Friday 17:00 ET is 21:00 UTC in summer, 22:00 in winter."""

    close_minute: int
    open_minute: int
    timezone: str = "UTC"

    @classmethod
    def parse(cls, close: str, reopen: str, timezone: str = "UTC") -> WeekendClosure:
        """From "Fri 17:00"-style weekday + time-of-day strings in `timezone`; the
        closure must not straddle Monday 00:00."""
        start, end = _minute_of_week(close), _minute_of_week(reopen)
        if start >= end:
            raise ValueError(f"the close ({close!r}) must come before the reopening ({reopen!r})")
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown time zone {timezone!r} (use an IANA name)") from exc
        return cls(close_minute=start, open_minute=end, timezone=timezone)

    def label(self) -> str:
        """The closure as configured, e.g. "Fri 17:00 → Sun 18:00 America/New_York"."""
        close, reopen = _weekday_time(self.close_minute), _weekday_time(self.open_minute)
        return f"{close} → {reopen} {self.timezone}"


def _weekday_time(minute: int) -> str:
    day, rest = divmod(minute, 1440)
    return f"{WEEKDAYS[day].capitalize()} {rest // 60:02d}:{rest % 60:02d}"


def _minute_of_week(text: str) -> int:
    try:
        day, clock = text.strip().lower().split()
        hours, minutes = (int(x) for x in clock.split(":"))
    except ValueError as exc:
        raise ValueError(f"expected a weekday and a time like 'Fri 17:00', got {text!r}") from exc
    if day not in WEEKDAYS:
        raise ValueError(f"unknown weekday {day!r} in {text!r} (use {', '.join(WEEKDAYS)})")
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        raise ValueError(f"not a time of day: {clock!r}")
    return WEEKDAYS.index(day) * 1440 + hours * 60 + minutes


def drop_closed_bars(bars: pd.DataFrame, closure: WeekendClosure | None) -> pd.DataFrame:
    """`bars` without those lying entirely inside the weekend closure.

    A bar that straddles the close or the reopening (a Friday daily bar, the
    Sunday reopening hour) holds session trading and is kept. No closure: the
    frame is returned as is.
    """
    if closure is None or bars.empty:
        return bars
    close_ms, open_ms = _closure_bounds(bars["t"], closure)
    starts_closed = bars["t"].to_numpy() >= close_ms
    ends_closed = bars["T"].to_numpy() + 1 <= open_ms
    return bars[~(starts_closed & ends_closed)]


def closure_at(now_ms: int, closure: WeekendClosure | None) -> tuple[int, int] | None:
    """(close, reopening) in UTC ms of the weekend closure `now_ms` falls in, or
    None while the market is in session (or the dex trades 24/7). The close
    itself is closed; the reopening is in session again."""
    if closure is None:
        return None
    close, reopen = _closure_bounds(pd.Series([now_ms]), closure)
    start, end = int(close[0]), int(reopen[0])
    return (start, end) if start <= now_ms < end else None


def _closure_bounds(t_ms: pd.Series, closure: WeekendClosure) -> tuple:
    """(close, reopening) in UTC ms of the closure of the week each timestamp
    starts in — the week counted in the closure's time zone, so each week gets
    that week's UTC offset."""
    local = (
        pd.to_datetime(t_ms, unit="ms", utc=True)
        .dt.tz_convert(closure.timezone)
        .dt.tz_localize(None)
    )
    monday = local.dt.normalize() - pd.to_timedelta(local.dt.weekday, unit="D")

    def utc_ms(week: pd.Timestamp, minute: int) -> int:
        wall = week + pd.Timedelta(minutes=minute)
        at = wall.tz_localize(closure.timezone, ambiguous=True, nonexistent="shift_forward")
        return at.value // 1_000_000  # Timestamp.value is in nanoseconds

    weeks = [pd.Timestamp(w) for w in monday.unique()]
    close = {w: utc_ms(w, closure.close_minute) for w in weeks}
    reopen = {w: utc_ms(w, closure.open_minute) for w in weeks}
    return (
        monday.map(close).to_numpy(dtype="int64"),
        monday.map(reopen).to_numpy(dtype="int64"),
    )


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
