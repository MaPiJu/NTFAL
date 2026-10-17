"""bot.py's scheduling: which horizons stay fresh, when, and what runs at startup.

No exchange is reached: execution stays disabled or in dry run, and the
refreshes are stubbed."""

from __future__ import annotations

from dataclasses import replace

import pytest

import bot
from app.cadence import BAR_CLOSE_DELAY_SECONDS, first_run
from config import ExecutionConfig
from tests.test_app import SCALP, SWING, make_config

HOUR = 3600


def test_first_run_lands_just_after_a_bar_close():
    # 4h bars close at 00:00, 04:00… UTC; the job runs a minute after.
    t0 = 1_700_000_000 - 1_700_000_000 % (4 * HOUR)  # a 4h close
    delay = BAR_CLOSE_DELAY_SECONDS
    assert first_run(4 * HOUR, now=t0 + 5).timestamp() == t0 + delay  # within the delay
    assert first_run(4 * HOUR, now=t0 + delay).timestamp() == t0 + 4 * HOUR + delay
    assert first_run(4 * HOUR, now=t0 - 1).timestamp() == t0 + delay
    assert first_run(900, now=t0 + 901).timestamp() == t0 + 900 + delay


class Scheduler:
    def __init__(self) -> None:
        self.jobs: dict[str, object] = {}

    def add_job(self, func, trigger, id, **kwargs) -> None:  # noqa: A002 - apscheduler's name
        self.jobs[id] = trigger


def make_bot(tmp_path, **execution) -> bot.Bot:
    cfg = make_config(tmp_path, horizons=(SWING, SCALP))
    return bot.Bot(replace(cfg, execution=ExecutionConfig(horizon="swing", **execution)))


def test_the_dashboard_keeps_every_horizon_fresh(tmp_path):
    # Analysis only: every horizon is refreshed by its own job...
    analysis = make_bot(tmp_path)
    assert analysis.refreshed_horizons(serve=True) == ["swing", "scalp"]
    # ...and without the dashboard the traded one still is, for the report.
    assert analysis.refreshed_horizons(serve=False) == ["swing"]

    # Trading: the trade cycle refreshes the traded horizon after every entry bar.
    trading = make_bot(tmp_path, enabled=True)
    assert trading.refreshed_horizons(serve=True) == ["scalp"]
    assert trading.refreshed_horizons(serve=False) == []

    scheduler = Scheduler()
    trading.schedule(scheduler, serve=True)
    assert set(scheduler.jobs) == {"cycle", "protect", "refresh-scalp"}
    cycle, scalp = scheduler.jobs["cycle"], scheduler.jobs["refresh-scalp"]
    assert cycle.interval.total_seconds() == 4 * HOUR
    assert cycle.start_date.timestamp() % (4 * HOUR) == BAR_CLOSE_DELAY_SECONDS
    assert scalp.interval.total_seconds() == SCALP.refresh_seconds
    assert scalp.start_date.timestamp() % SCALP.refresh_seconds == BAR_CLOSE_DELAY_SECONDS


@pytest.mark.parametrize(
    ("argv", "enabled", "first"),
    [
        (["--serve"], False, [None]),  # the dashboard: every horizon, now
        (["--serve"], True, [None]),
        ([], False, ["swing"]),  # reports only: the traded horizon's signals
        ([], True, []),  # the trade cycle refreshes it
        (["--once"], True, []),
    ],
)
def test_startup_fills_the_snapshot_without_waiting_for_a_bar(
    tmp_path, monkeypatch, argv, enabled, first
):
    cfg = make_config(tmp_path, horizons=(SWING, SCALP))
    cfg = replace(cfg, execution=ExecutionConfig(horizon="swing", enabled=enabled))
    monkeypatch.setattr(bot, "load_config", lambda: cfg)
    refreshed: list[str | None] = []
    monkeypatch.setattr(bot, "refresh", lambda c, client, name=None: refreshed.append(name))
    monkeypatch.setattr(bot.Bot, "protect", lambda self: None)
    monkeypatch.setattr(bot.Bot, "cycle", lambda self: None)
    started: list[bool] = []

    class NoScheduler:
        def __init__(self, timezone: str) -> None: ...

        def add_job(self, *args, **kwargs) -> None: ...

        def start(self) -> None:
            started.append(True)

        def shutdown(self, wait: bool = True) -> None: ...

    import apscheduler.schedulers.background as background
    import apscheduler.schedulers.blocking as blocking
    import uvicorn

    monkeypatch.setattr(background, "BackgroundScheduler", NoScheduler)
    monkeypatch.setattr(blocking, "BlockingScheduler", NoScheduler)
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(bot.signal, "signal", lambda *args: None)  # keep pytest's handlers

    assert bot.main(argv) == 0
    assert refreshed == first
    assert started == ([] if "--once" in argv else [True])
