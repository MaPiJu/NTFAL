#!/usr/bin/env python3
"""Daily refresh: fetch candles, compute signals, write the dashboard snapshot.

Usage:
    python run.py                     # one manual refresh (the default workflow)
    python run.py --serve             # refresh, then serve the dashboard
    python run.py --serve --schedule 06:00   # also re-refresh daily at 06:00 UTC

Read-only: this script only calls Hyperliquid's public info endpoint and
never places, signs, or cancels orders.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from app.pipeline import SNAPSHOT_FILENAME, build_snapshot
from config import Config, load_config
from data.hyperliquid import HyperliquidClient


def print_signals_table(snapshot: dict[str, Any]) -> None:
    guard = snapshot["guard"]
    print(f"\nElder Triple Screen — generated {snapshot['generated_at']}")
    print(
        f"equity ${snapshot['equity']:,.2f} · risk/trade {snapshot['risk_pct']:.1%} "
        f"· {len(snapshot['signals'])} assets analyzed"
    )
    if guard["blocked"]:
        print(
            f"⚠ 6% RULE ACTIVE: monthly losses + open risk ${guard['total_at_risk']:,.2f} "
            f">= limit ${guard['limit']:,.2f} — NO NEW ENTRIES this month."
        )

    def num(x: float | None) -> str:
        return f"{x:,.6g}" if x is not None else "—"

    header = (
        f"{'ASSET':<6} {'TIDE':<7} {'IMP W/D':<11} {'FI(2)':>14} {'ACTION':<12} "
        f"{'ENTRY':>12} {'LIMIT':>12} {'STOP':>12} {'TARGET':>12} {'R:R':>7} {'SIZE':>10}"
    )
    print("\n" + header)
    print("-" * len(header))
    for s in snapshot["signals"]:
        rr = f"{s['reward_risk']:.2f}" if s["reward_risk"] is not None else "—"
        if s["reward_risk"] is not None and not s["rr_ok"]:
            rr += "⚠"
        size = num(s["position_size"]["size"]) if s["position_size"] else "—"
        print(
            f"{s['asset']:<6} {s['weekly_trend']:<7} "
            f"{s['weekly_impulse'] + '/' + s['daily_impulse']:<11} "
            f"{s['force_index_2']:>14,.4g} {s['action']:<12} "
            f"{num(s['entry']):>12} {num(s['entry_limit']):>12} {num(s['stop']):>12} "
            f"{num(s['target']):>12} {rr:>7} {size:>10}"
        )
    print()
    for s in snapshot["signals"]:
        print(f"  {s['asset']}: {s['reason']}")
    if snapshot.get("skipped"):
        print(f"\nskipped (not enough history yet): {', '.join(snapshot['skipped'])}")
    print("\nInformational only — not financial advice; no orders are placed.\n")


def do_refresh(cfg: Config) -> dict[str, Any]:
    with HyperliquidClient(cache_dir=cfg.cache_dir) as client:
        snapshot = build_snapshot(
            cfg, client, on_progress=lambda coin: print(f"refreshing {coin}…", flush=True)
        )
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    out = cfg.cache_dir / SNAPSHOT_FILENAME
    out.write_text(json.dumps(snapshot))
    print(f"snapshot written to {out}")
    print_signals_table(snapshot)
    return snapshot


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true", help="serve the dashboard after refreshing")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--schedule",
        metavar="HH:MM",
        help="optional: re-run the refresh daily at this UTC time (apscheduler)",
    )
    args = parser.parse_args(argv)

    cfg = load_config()
    do_refresh(cfg)

    if args.schedule:
        hour, minute = (int(x) for x in args.schedule.split(":"))
        if args.serve:
            from apscheduler.schedulers.background import BackgroundScheduler

            scheduler = BackgroundScheduler(timezone="UTC")
            scheduler.add_job(do_refresh, "cron", args=[cfg], hour=hour, minute=minute)
            scheduler.start()
        else:
            from apscheduler.schedulers.blocking import BlockingScheduler

            scheduler = BlockingScheduler(timezone="UTC")
            scheduler.add_job(do_refresh, "cron", args=[cfg], hour=hour, minute=minute)
            print(f"scheduler running — daily refresh at {args.schedule} UTC (Ctrl-C to stop)")
            scheduler.start()
            return 0

    if args.serve:
        import uvicorn

        uvicorn.run("app.main:app", host=args.host, port=args.port)

    return 0


if __name__ == "__main__":
    sys.exit(main())
