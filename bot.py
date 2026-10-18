#!/usr/bin/env python3
"""Trade ONE horizon's Triple Screen automatically, with a daily Telegram report.

Usage:
    python bot.py              # run forever (the VPS service): a trade cycle after
                               # every entry bar, the stop check every minute, the
                               # daily report
    python bot.py --once       # one trade cycle (+ stop check), then exit
    python bot.py --report     # send the daily report now, then exit
    python bot.py --serve      # also keep every horizon fresh and serve the dashboard
                               # (the VPS service: deploy/ntfal-bot.service)

Nothing is ever sent unless config.toml has [execution] enabled = true, and the
default mode "dry_run" logs every order without sending it. "testnet" and
"mainnet" need an API wallet key in .env (HL_API_PRIVATE_KEY) — a key that can
trade for the account but cannot withdraw — and the account's public address
(HL_ADDRESS). See README, "Automated execution".
"""

from __future__ import annotations

import argparse
import contextlib
import os
import signal
import sys
import traceback
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from app.cadence import BAR_CLOSE_DELAY_SECONDS, first_run
from app.pipeline import build_snapshot, load_snapshot, refresh_horizon, watchlist_dexes
from config import Config, load_config
from data.hyperliquid import INTERVAL_MS, HyperliquidClient
from execution.engine import Engine, describe
from execution.gateway import (
    MAINNET_URL,
    TESTNET_URL,
    DryRunGateway,
    HyperliquidGateway,
    OrderGateway,
)
from execution.state import ExecutionState
from notify.report import build_report
from notify.telegram import TelegramNotifier, format_alert
from run import _REFRESH_LOCK, snapshot_path, write_snapshot


def bot_config(cfg: Config) -> Config:
    """Testnet data and state live apart from mainnet's: their prices differ. Its
    open interest is play money too, far under any mainnet floor: there the floor
    is off, or the testnet run would scan nothing."""
    if cfg.execution.mode != "testnet":
        return cfg
    cache = cfg.cache_dir / "testnet"
    return replace(
        cfg,
        cache_dir=cache,
        journal=replace(cfg.journal, path=cache / "journal.jsonl"),
        scanner=replace(cfg.scanner, min_open_interest_usd=0.0),
    )


def make_client(cfg: Config) -> HyperliquidClient:
    url = f"{TESTNET_URL}/info" if cfg.execution.mode == "testnet" else None
    return HyperliquidClient(api_url=url, cache_dir=cfg.cache_dir)


def make_gateway_factory(
    cfg: Config, client: HyperliquidClient
) -> Callable[[ExecutionState], OrderGateway]:
    mode = cfg.execution.mode
    if not cfg.execution.enabled or mode == "dry_run":
        return DryRunGateway
    key = os.environ.get("HL_API_PRIVATE_KEY", "").strip()
    if not key:
        raise SystemExit(f"mode {mode!r} needs HL_API_PRIVATE_KEY (an API wallet key) in .env")
    if not cfg.positions.address:
        raise SystemExit(f"mode {mode!r} needs HL_ADDRESS, the account's public address, in .env")
    gateway = HyperliquidGateway(
        key,
        cfg.positions.address,
        TESTNET_URL if mode == "testnet" else MAINNET_URL,
        client,
        perp_dexs=watchlist_dexes(cfg.scanner.watchlist),
        mode=mode,
    )
    print(f"API wallet {gateway.agent_address} trading for {cfg.positions.address}")
    return lambda _state: gateway


def make_notifier(cfg: Config) -> TelegramNotifier | None:
    if not cfg.telegram.enabled:
        return None
    try:
        return TelegramNotifier(
            os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
        )
    except ValueError as exc:
        raise SystemExit(f"[telegram] enabled but {exc} (in .env)") from exc


def refresh(cfg: Config, client: HyperliquidClient, name: str | None = None) -> dict[str, Any]:
    """Refresh one horizon into the snapshot — every horizon when `name` is None or
    the snapshot does not hold it yet."""
    with _REFRESH_LOCK:
        previous = load_snapshot(snapshot_path(cfg))
        if name is None or name not in previous.get("horizons", {}):
            snapshot = build_snapshot(cfg, client, previous=previous)
        else:
            snapshot = refresh_horizon(cfg, client, name, previous)
        write_snapshot(cfg, snapshot)
    return snapshot


class Bot:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.client = make_client(cfg)
        self.notifier = make_notifier(cfg)
        self.mode = cfg.execution.mode if cfg.execution.enabled else "disabled"
        self.engine = Engine(
            cfg,
            self.client,
            make_gateway_factory(cfg, self.client),
            refresh=lambda c: refresh(c, self.client, cfg.execution.horizon),
            alert=self.alert,
        )

    def say(self, text: str) -> None:
        print(text, flush=True)
        if self.notifier is not None:
            self.notifier.send(text)

    def alert(self, event: dict[str, Any]) -> None:
        print(f"[{event['time']}] {event['level'].upper()} {event['message']}", flush=True)
        if self.notifier is not None and self.cfg.telegram.alerts:
            self.notifier.send(format_alert(event, self.mode))

    def guarded(self, name: str, job: Callable[[], Any]) -> Callable[[], None]:
        """`job`, with any crash reported instead of silently logged by the scheduler."""

        def run() -> None:
            try:
                job()
            except Exception as exc:  # a job must never take the scheduler down
                traceback.print_exc()
                self.say(f"🚨 [{self.mode.upper()}] {name} failed: {type(exc).__name__}: {exc}")

        return run

    def cycle(self) -> None:
        print(f"[{datetime.now(tz=UTC):%Y-%m-%d %H:%M:%S}] trade cycle ({self.mode})", flush=True)
        print(describe(self.engine.cycle()), flush=True)

    def protect(self) -> None:
        actions = self.engine.protect()
        if actions:
            print(describe(actions), flush=True)

    def report(self) -> None:
        inputs = self.engine.report_inputs()
        text = build_report(self.cfg, halted=self.engine.halted(), **inputs)
        if self.notifier is None:
            print(text)
            return
        if self.notifier.send(text):
            self.engine.mark_reported()

    def refreshed_horizons(self, serve: bool) -> list[str]:
        """The horizons the refresh jobs keep fresh: every one with the dashboard,
        else the traded one when no trade cycle refreshes it (the report reads its
        signals). The trade cycle refreshes the traded horizon itself."""
        ecfg = self.cfg.execution
        return [
            h.name
            for h in self.cfg.scanner.horizons
            if (serve or h.name == ecfg.horizon) and not (ecfg.enabled and h.name == ecfg.horizon)
        ]

    def schedule(self, scheduler: Any, serve: bool) -> None:
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger

        ecfg = self.cfg.execution
        horizon = self.cfg.scanner.horizon(ecfg.horizon)
        common = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 300}
        if ecfg.enabled:
            bar_s = INTERVAL_MS[horizon.entry] // 1000
            scheduler.add_job(
                self.guarded("trade cycle", self.cycle),
                IntervalTrigger(seconds=bar_s, start_date=first_run(bar_s)),
                id="cycle",
                **common,
            )
            scheduler.add_job(
                self.guarded("stop check", self.protect),
                IntervalTrigger(seconds=ecfg.protect_seconds),
                id="protect",
                **common,
            )
            delay = BAR_CLOSE_DELAY_SECONDS
            print(f"trade cycle after every {horizon.entry} bar on '{horizon.name}' (+{delay}s)")
        if self.notifier is not None:
            hour, minute = (int(x) for x in self.cfg.telegram.report_time.split(":"))
            scheduler.add_job(
                self.guarded("daily report", self.report),
                CronTrigger(hour=hour, minute=minute, timezone=self.cfg.telegram.timezone),
                id="report",
                **common,
            )
            print(f"daily report at {self.cfg.telegram.report_time} {self.cfg.telegram.timezone}")
        for name in self.refreshed_horizons(serve):
            h = self.cfg.scanner.horizon(name)
            scheduler.add_job(
                self.guarded(f"refresh {name}", lambda n=name: refresh(self.cfg, self.client, n)),
                IntervalTrigger(seconds=h.refresh_seconds, start_date=first_run(h.refresh_seconds)),
                id=f"refresh-{name}",
                **common,
            )
            print(f"refresh '{name}' every {h.refresh_seconds}s, after each {h.entry} close")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--once", action="store_true", help="one trade cycle, then exit")
    parser.add_argument(
        "--report", action="store_true", help="send the daily report now, then exit"
    )
    parser.add_argument("--serve", action="store_true", help="also serve the dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    cfg = bot_config(load_config())
    bot = Bot(cfg)
    ecfg = cfg.execution

    if args.report:
        bot.report()
        return 0
    if not ecfg.enabled:
        print("[execution] enabled = false: no order is planned or sent (reports only).")
    elif ecfg.mode == "mainnet":
        print("!" * 72 + "\nMAINNET — this bot places REAL orders with REAL money.\n" + "!" * 72)
    if ecfg.enabled:
        bot.protect()
    if not args.once and bot.refreshed_horizons(args.serve):
        # The jobs' first runs wait for a bar close (up to 4h on swing): fill the
        # dashboard (every horizon) or the report's signals (the traded one) now.
        # The trade cycle below refreshes the traded horizon again, at almost no
        # cost: its bars are cached.
        first = None if args.serve else ecfg.horizon
        bot.guarded("first refresh", lambda: refresh(bot.cfg, bot.client, first))()
    if ecfg.enabled:
        bot.cycle()
    if args.once:
        return 0

    bot.say(
        f"🤖 NTFAL bot started — mode {bot.mode.upper()}, horizon '{ecfg.horizon}'"
        + (f", halt file {ecfg.halt_file} present" if bot.engine.halted() else "")
    )
    # systemd stops the service with SIGTERM: leave through the normal path below, so
    # the "stopped" message goes out (uvicorn re-raises SIGTERM after its own shutdown).
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    if args.serve:
        from apscheduler.schedulers.background import BackgroundScheduler

        scheduler = BackgroundScheduler(timezone="UTC")
        bot.schedule(scheduler, serve=True)
        scheduler.start()

        import uvicorn

        os.environ.setdefault("SNAPSHOT_PATH", str(snapshot_path(cfg)))
        # SIGTERM ends here with code 0; a dashboard that cannot start (port taken,
        # import error) makes uvicorn exit non-zero, which must reach systemd so it
        # restarts the whole bot.
        code: int | str | None = 0
        try:
            uvicorn.run("app.main:app", host=args.host, port=args.port)
        except KeyboardInterrupt:
            pass
        except SystemExit as exc:
            code = exc.code
        scheduler.shutdown(wait=False)
        why = "" if code in (0, None) else f" (the dashboard failed to run: exit {code})"
        bot.say(
            f"🛑 NTFAL bot stopped — mode {bot.mode.upper()}{why}; exchange stops stay in place"
        )
        return 0 if code is None else code if isinstance(code, int) else 1

    from apscheduler.schedulers.blocking import BlockingScheduler

    scheduler = BlockingScheduler(timezone="UTC")
    bot.schedule(scheduler, serve=False)
    print("bot running (Ctrl-C to stop)", flush=True)
    with contextlib.suppress(KeyboardInterrupt, SystemExit):
        scheduler.start()
    bot.say(f"🛑 NTFAL bot stopped — mode {bot.mode.upper()}; exchange stops stay in place")
    return 0


if __name__ == "__main__":
    sys.exit(main())
