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
from journal import append_journal_entry


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

    best = next((s for s in snapshot["signals"] if s.get("is_top_pick")), None)
    if best is not None:
        print(
            f"★ BEST TRADE: {best['asset']} {best['action']} "
            f"(score {best['quality_score'] * 100:.0f}/100, R:R {best['reward_risk']:.2f}, "
            f"entry {best['entry']:,.6g}, stop {best['stop']:,.6g}, target {best['target']:,.6g})"
        )

    def num(x: float | None) -> str:
        return f"{x:,.6g}" if x is not None else "—"

    # Best trade first: tradable setups by Elder score (desc), then the rest by name.
    def sort_key(s: dict[str, Any]) -> tuple[int, float, str]:
        aside = s["action"] == "stand_aside"
        return (1 if aside else 0, -(s["quality_score"] or 0.0), s["asset"])

    # 14-wide asset column: tradfi names like "xyz:ALUMINIUM" are longer than tickers.
    header = (
        f"{'ASSET':<14} {'REGIME':<8} {'TIDE':<7} {'IMP W/D/4H':<14} {'FI(2)':>14} {'ACTION':<13} "
        f"{'PRICE':>12} {'ENTRY':>12} {'STOP':>12} {'R:R':>7} "
        f"{'LIMIT':>12} {'LIM STOP':>12} {'LIM R:R':>7} {'TARGET':>12} "
        f"{'SCORE':>6} {'SIZE':>10}"
    )
    print("\n" + header)
    print("-" * len(header))
    for s in sorted(snapshot["signals"], key=sort_key):
        rr = f"{s['reward_risk']:.2f}" if s["reward_risk"] is not None else "—"
        if s["reward_risk"] is not None and not s["rr_ok"]:
            rr += "⚠"
        lim_rr = f"{s['reward_risk_limit']:.2f}" if s.get("reward_risk_limit") is not None else "—"
        size = num(s["position_size"]["size"]) if s["position_size"] else "—"
        score = f"{s['quality_score'] * 100:.0f}" if s.get("quality_score") is not None else "—"
        action = s["action"] + (" ★" if s.get("is_top_pick") else "")
        impulses = "/".join(
            [s["weekly_impulse"], s["daily_impulse"], s.get("third_screen_impulse") or "—"]
        )
        print(
            f"{s['asset']:<14} {s.get('market_regime', '—'):<8} {s['weekly_trend']:<7} "
            f"{impulses:<14} "
            f"{s['force_index_2']:>14,.4g} {action:<13} "
            f"{num(s.get('last_close')):>12} "
            f"{num(s['entry']):>12} {num(s['stop']):>12} {rr:>7} "
            f"{num(s['entry_limit']):>12} {num(s.get('entry_limit_stop')):>12} {lim_rr:>7} "
            f"{num(s['target']):>12} {score:>6} {size:>10}"
        )
    print()
    for s in snapshot["signals"]:
        divs = s.get("divergences") or []
        suffix = f" · divergences: {', '.join(divs)}" if divs else ""
        vz = s.get("value_zone_status", "—")
        order = f" · order: {s['entry_order_plan']}" if s.get("entry_order_plan") else ""
        print(f"  {s['asset']}: {s['reason']} · value zone: {vz}{order}{suffix}")
    if snapshot.get("skipped"):
        print(f"\nskipped (not enough history yet): {', '.join(snapshot['skipped'])}")
    print("\nInformational only — not financial advice; no orders are placed.\n")


VERDICT_LABEL = {"hold": "HOLD", "take_profits": "TAKE PROFITS", "exit": "EXIT"}


def print_positions_table(snapshot: dict[str, Any]) -> None:
    """Elder trade-management verdict for each OPEN position (read-only)."""
    positions = snapshot.get("positions") or []
    if not snapshot.get("position_address") and not positions:
        return  # trade management disabled (no public address configured)

    print(
        f"\nOpen positions — Elder trade management  (address {snapshot.get('position_address')})"
    )
    if not positions:
        print("  (none open, or held coins are too new to evaluate)\n")
        return

    def num(x: float | None) -> str:
        return f"{x:,.6g}" if x is not None else "—"

    header = (
        f"{'ASSET':<14} {'SIDE':<6} {'ENTRY':>12} {'CLOSE':>12} {'MARK':>12} "
        f"{'PnL ELDER':>12} {'PnL LIVE':>12} {'IMP W/D':<11} {'TARGET':>12} "
        f"{'TRAIL STOP':>12} {'OPEN RISK':>12} {'VERDICT':<13}"
    )
    print("\n" + header)
    print("-" * len(header))
    for p in positions:
        target = num(p["target"]) + ("✓" if p["target_reached"] else "")
        verdict = VERDICT_LABEL.get(p["verdict"], p["verdict"])
        print(
            f"{p['asset']:<14} {p['side']:<6} {num(p['entry']):>12} "
            f"{num(p['close_price']):>12} {num(p['live_price']):>12} "
            f"{p['pnl_elder']:>12,.2f} {p['pnl_live']:>12,.2f} "
            f"{p['weekly_impulse'] + '/' + p['daily_impulse']:<11} "
            f"{target:>12} {num(p['suggested_stop']):>12} {num(p.get('open_risk')):>12} "
            f"{verdict:<13}"
        )
    print()
    for p in positions:
        for reason in p["reasons"]:
            print(f"  {p['asset']} ({p['side']}): {reason}")
    print()


def do_refresh(cfg: Config) -> dict[str, Any]:
    with HyperliquidClient(cache_dir=cfg.cache_dir) as client:
        snapshot = build_snapshot(
            cfg, client, on_progress=lambda coin: print(f"refreshing {coin}…", flush=True)
        )
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    out = cfg.cache_dir / SNAPSHOT_FILENAME
    out.write_text(json.dumps(snapshot))
    print(f"snapshot written to {out}")
    if cfg.journal.enabled:
        append_journal_entry(snapshot, cfg.journal.path)
        print(f"journal appended to {cfg.journal.path}")
    print_signals_table(snapshot)
    print_positions_table(snapshot)
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
