#!/usr/bin/env python3
"""Refresh candles, compute signals, write the dashboard snapshot.

Usage:
    python run.py                     # one full refresh of every horizon
    python run.py --serve             # refresh, then serve the dashboard
    python run.py --serve --watch     # keep each horizon refreshed right after
                                      # each close of its entry bar (swing 4h,
                                      # scalp 15m, minutes 5m) while serving
    python run.py --horizon swing     # a quick analysis of one horizon only
    python run.py --horizon swing scalp --serve --watch
                                      # ...or of a few, kept fresh and served
    python run.py --all --horizon swing
                                      # every tradable perp on every dex, once

Read-only: this script only calls Hyperliquid's public info endpoint and
never places, signs, or cancels orders.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from app.cadence import first_run
from app.pipeline import (
    SNAPSHOT_FILENAME,
    build_snapshot,
    chosen_horizons,
    every_perp_watchlist,
    expand_watchlist,
    fetch_asset_contexts,
    liquid_markets,
    load_snapshot,
    refresh_horizon,
)
from config import Config, load_config
from data.hyperliquid import (
    CANDLE_REQUESTS_PER_MINUTE,
    HyperliquidClient,
    HyperliquidError,
    coin_dex,
)
from journal import append_journal_entry

# --watch runs one job per horizon on a thread pool, and their cadences line up
# (every swing refresh fires with a scalp and a micro one). A refresh loads,
# recomputes and rewrites the whole snapshot, so two at once would let the last
# writer put back what it loaded — the stop memory included. One at a time.
_REFRESH_LOCK = threading.Lock()


def num(x: float | None) -> str:
    return f"{x:,.6g}" if x is not None else "—"


def usd_short(x: float) -> str:
    """$12.3M, $480k: an open interest at a glance."""
    if x >= 1e9:
        return f"${x / 1e9:.3g}B"
    if x >= 1e6:
        return f"${x / 1e6:.3g}M"
    return f"${x / 1e3:.3g}k"


# Thin markets named by the CLI before the rest are only counted.
THIN_MARKETS_NAMED = 12


def pct(x: float | None, digits: int = 2) -> str:
    return f"{x:+.{digits}%}" if x is not None else "—"


def rate_pct(x: float | None) -> str:
    """An hourly funding rate in %, to two significant digits: rates of a few
    millionths an hour must not print as +0.0000%."""
    if x is None:
        return "—"
    pct_value = x * 100
    if pct_value == 0:
        return "+0.00%"
    decimals = max(2, 1 - math.floor(math.log10(abs(pct_value))))
    return f"{pct_value:+.{decimals}f}%"


def bar_time(iso: str | None) -> str:
    """A bar's open time, UTC, short: "Fri 10-02 20:00"."""
    if not iso:
        return "—"
    return datetime.fromisoformat(iso).astimezone(UTC).strftime("%a %m-%d %H:%M")


def apgar_detail(apgar: dict[str, Any]) -> str:
    """The Trade Apgar's five lines: question, observed answer, score."""
    lines = " · ".join(f"{x['question']} {x['answer']} {x['score']}" for x in apgar["lines"])
    verdict = "A-trade" if apgar["a_trade"] else "not an A-trade (needs >= 7 and no zero)"
    return f"apgar {apgar['total']}/10: {lines} — {verdict}"


def print_horizon_table(name: str, block: dict[str, Any]) -> None:
    """The signals table for one timeframe chain."""
    iv = block["intervals"]
    chain = f"{iv['tide']}/{iv['wave']}/{iv['entry']}"
    print(f"\n=== {block['label'].upper()} [{name}] — tide/wave/entry = {chain} ===")
    print(f"generated {block['generated_at']} · {len(block['signals'])} assets")

    best = next((s for s in block["signals"] if s.get("is_top_pick")), None)
    if best is not None:
        # A block a partial refresh carried over from an older snapshot may have
        # no Apgar yet: print what is there.
        apgar = best.get("apgar")
        rr = best.get("reward_risk")
        details = [
            f"Apgar {apgar['total']}/10" if apgar else None,
            f"R:R {rr:.2f}" if rr is not None else None,
            f"entry {num(best['entry'])}, stop {num(best['stop'])}, target {num(best['target'])}",
        ]
        print(
            f"★ BEST {name.upper()} TRADE: {best['asset']} {best['action']} "
            f"({', '.join(d for d in details if d)})"
        )

    # Best trade first: the pick, then the other A-trades, then the remaining
    # setups — each group by Trade Apgar, then R:R (desc) — then the stand-aside
    # rest by name.
    def sort_key(s: dict[str, Any]) -> tuple[int, int, int, int, float, str]:
        aside = s["action"] == "stand_aside"
        apgar = s.get("apgar") or {}
        return (
            1 if aside else 0,
            0 if s.get("is_top_pick") else 1,
            0 if apgar.get("a_trade") else 1,
            -apgar.get("total", 0),
            -(s["reward_risk"] or 0.0),
            s["asset"],
        )

    # 14-wide asset column: tradfi names like "xyz:BRENTOIL" are longer than tickers.
    header = (
        f"{'ASSET':<14} {'REGIME':<8} {'TIDE':<7} {'IMP T/W/E':<14} {'FI(2)':>14} {'ACTION':<13} "
        f"{'CLOSE':>12} {'BAR (UTC)':<15} {'MARK':>12} {'DRIFT':>7} {'ENTRY':>12} "
        f"{'STOP':>12} {'R:R':>7} "
        f"{'LIMIT':>12} {'LIM STOP':>12} {'LIM R:R':>7} {'TARGET':>12} "
        f"{'APGAR':>6} {'SIZE':>10} {'FUND/H':>11} {'FUND EST':>9}"
    )
    print("\n" + header)
    print("-" * len(header))
    for s in sorted(block["signals"], key=sort_key):
        rr = f"{s['reward_risk']:.2f}" if s["reward_risk"] is not None else "—"
        if s["reward_risk"] is not None and not s["rr_ok"]:
            rr += "⚠"
        lim_rr = f"{s['reward_risk_limit']:.2f}" if s.get("reward_risk_limit") is not None else "—"
        size = num(s["position_size"]["size"]) if s["position_size"] else "—"
        if s.get("size_warnings"):
            size += "⚠"
        apgar = s.get("apgar")
        score = f"{apgar['total']}{' A' if apgar['a_trade'] else ''}" if apgar else "—"
        action = (
            s["action"]
            + (" ⚠" if s.get("price_alert") else "")
            + (" !" if s.get("data_warnings") else "")
            + (" ★" if s.get("is_top_pick") else "")
        )
        impulses = "/".join([s["tide_impulse"], s["wave_impulse"], s.get("entry_impulse") or "—"])
        close, mark = s.get("last_close"), s.get("live_price")
        drift = f"{(mark / close - 1) * 100:+.1f}%" if close and mark else "—"
        print(
            f"{s['asset']:<14} {s.get('market_regime', '—'):<8} {s['tide_trend']:<7} "
            f"{impulses:<14} "
            f"{s['force_index_2']:>14,.4g} {action:<13} "
            f"{num(close):>12} {bar_time(s.get('last_bar_time')):<15} {num(mark):>12} {drift:>7} "
            f"{num(s['entry']):>12} {num(s['stop']):>12} {rr:>7} "
            f"{num(s['entry_limit']):>12} {num(s.get('entry_limit_stop')):>12} {lim_rr:>7} "
            f"{num(s['target']):>12} {score:>6} {size:>10} "
            f"{rate_pct(s.get('funding_rate')):>11} {pct(s.get('funding_cost')):>9}"
        )
    print()
    for s in block["signals"]:
        divs = s.get("divergences") or []
        suffix = f" · divergences: {', '.join(divs)}" if divs else ""
        vz = s.get("value_zone_status", "—")
        order = f" · order: {s['entry_order_plan']}" if s.get("entry_order_plan") else ""
        alert = f" · ⚠ {s['price_alert']}" if s.get("price_alert") else ""
        print(f"  {s['asset']}: {s['reason']} · value zone: {vz}{order}{alert}{suffix}")
        if s.get("apgar"):
            print(f"    {apgar_detail(s['apgar'])}")
        for w in s.get("data_warnings") or []:
            print(f"    ! data quality: {w}")
        for w in s.get("size_warnings") or []:
            print(f"    ! size: {w}")
        if s.get("funding_warning"):
            print(f"    ! funding: {s['funding_warning']}")
    if block.get("skipped"):
        print(f"\nskipped: {', '.join(block['skipped'])}")


def print_signals_tables(snapshot: dict[str, Any]) -> None:
    guard = snapshot["guard"]
    print(f"\nElder Triple Screen — generated {snapshot['generated_at']}")
    month_start = snapshot.get("equity_at_month_start", snapshot["equity"])
    print(
        f"equity ${snapshot['equity']:,.2f} · risk/trade {snapshot['risk_pct']:.1%} of "
        f"month-start equity ${month_start:,.2f} "
        f"· open risk ${snapshot.get('total_open_trade_risk', 0.0):,.2f}"
    )
    unread = snapshot.get("unread_dexes") or []
    if unread:
        where = ", ".join(f"dex '{d}'" if d else "the native clearinghouse" for d in unread)
        hidden = snapshot.get("hidden_positions") or []
        print(
            f"⚠ Positions could not be read on {where} this refresh: {len(hidden)} position(s) "
            f"from the previous refresh still count "
            f"${snapshot.get('hidden_open_trade_risk', 0.0):,.2f} of open risk toward the 6% rule."
        )
    if guard["blocked"]:
        print(
            f"⚠ 6% RULE ACTIVE: monthly losses + open risk ${guard['total_at_risk']:,.2f} "
            f">= limit ${guard['limit']:,.2f} — NO NEW ENTRIES this month."
        )
    thin = snapshot.get("thin_markets") or []
    if thin:
        named = ", ".join(
            f"{m['asset']} ({usd_short(m['open_interest_usd'])})" for m in thin[:THIN_MARKETS_NAMED]
        )
        more = (
            f" and {len(thin) - THIN_MARKETS_NAMED} more" if len(thin) > THIN_MARKETS_NAMED else ""
        )
        floor = usd_short(snapshot["min_open_interest_usd"])
        print(f"Not scanned, open interest under {floor} (thin book, wide spreads): {named}{more}")
    print(
        "BAR = open time (UTC) of the last completed wave bar, the one the signal is read off; "
        "CLOSE is its close."
    )
    print(
        "FUND/H = current hourly funding rate (+: longs pay shorts); FUND EST = funding "
        "over the horizon's holding time, % of notional (+: this trade pays)."
    )
    print(
        "⚠ Each horizon sizes its suggestion as a STANDALONE trade risking "
        f"{snapshot['risk_pct']:.1%}. Taking several at once multiplies your risk."
    )

    horizons = snapshot.get("horizons", {})
    for name in snapshot.get("horizon_order", list(horizons)):
        if name in horizons:
            print_horizon_table(name, horizons[name])
    print("\nInformational only — not financial advice; no orders are placed.\n")


VERDICT_LABEL = {"hold": "HOLD", "take_profits": "TAKE PROFITS", "exit": "EXIT"}


def print_positions_table(snapshot: dict[str, Any]) -> None:
    """Elder trade-management verdict for each OPEN position (read-only)."""
    positions = snapshot.get("positions") or []
    if not snapshot.get("position_address") and not positions:
        return  # trade management disabled (no public address configured)

    addr = snapshot.get("position_address")
    suffix = f"  (address {addr})" if addr else "  (manual positions)"
    horizon = snapshot.get("positions_horizon", "?")
    print(f"\nOpen positions — Elder trade management on the '{horizon}' horizon{suffix}")
    if not positions:
        print("  (none open, or held coins are too new to evaluate)\n")
        return

    header = (
        f"{'ASSET':<14} {'SIDE':<6} {'ENTRY':>12} {'CLOSE':>12} {'MARK':>12} "
        f"{'PnL ELDER':>12} {'PnL LIVE':>12} {'FUNDING PAID':>12} {'IMP T/W':<11} "
        f"{'TARGET':>12} {'TRAIL STOP':>12} {'OPEN RISK':>12} {'VERDICT':<13}"
    )
    print("\n" + header)
    print("-" * len(header))
    for p in positions:
        target = num(p["target"]) + ("✓" if p["target_reached"] else "")
        verdict = VERDICT_LABEL.get(p["verdict"], p["verdict"])
        # cumFunding.sinceOpen: paid since open (negative = received).
        funding = p.get("cum_funding")
        funding_cell = f"{funding:,.2f}" if funding is not None else "—"
        print(
            f"{p['asset']:<14} {p['side']:<6} {num(p['entry']):>12} "
            f"{num(p['close_price']):>12} {num(p['live_price']):>12} "
            f"{p['pnl_elder']:>12,.2f} {p['pnl_live']:>12,.2f} {funding_cell:>12} "
            f"{p['tide_impulse'] + '/' + p['wave_impulse']:<11} "
            f"{target:>12} {num(p['suggested_stop']):>12} {num(p.get('open_risk')):>12} "
            f"{verdict:<13}"
        )
    print()
    for p in positions:
        for reason in p["reasons"]:
            print(f"  {p['asset']} ({p['side']}): {reason}")
    print()


def snapshot_path(cfg: Config) -> Path:
    return cfg.cache_dir / SNAPSHOT_FILENAME


def write_snapshot(cfg: Config, snapshot: dict[str, Any]) -> Path:
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    out = snapshot_path(cfg)
    # Write aside, then swap in: a write cut short (or read halfway by the
    # dashboard) never leaves a truncated snapshot, which would load as empty
    # and lose the stop memory.
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(json.dumps(snapshot))
    os.replace(tmp, out)
    if cfg.journal.enabled:
        append_journal_entry(snapshot, cfg.journal.path)
    return out


def _progress(horizon: str, coin: str) -> None:
    print(f"  [{horizon}] refreshing {coin}…", flush=True)


def do_refresh(
    cfg: Config, horizon: str | None = None, scope: Sequence[str] | None = None
) -> dict[str, Any]:
    """Build a snapshot of the horizons in `scope` (every horizon by default), or
    refresh just `horizon` and merge it into the stored snapshot."""
    with _REFRESH_LOCK:
        # Both paths read the previous snapshot: besides the horizons a partial
        # refresh merges into, it holds the stop memory (a suggested stop never
        # moves back).
        previous = load_snapshot(snapshot_path(cfg))
        with HyperliquidClient(cache_dir=cfg.cache_dir) as client:
            if horizon is None or not previous.get("horizons"):
                # Nothing to merge into yet — a partial refresh would leave the
                # other horizons missing from the dashboard, so build the whole scope.
                snapshot = build_snapshot(
                    cfg, client, on_progress=_progress, previous=previous, horizons=scope
                )
            else:
                snapshot = refresh_horizon(cfg, client, horizon, previous, on_progress=_progress)
        out = write_snapshot(cfg, snapshot)
    picks = {n: b.get("top_pick") for n, b in snapshot.get("horizons", {}).items()}
    picked = ", ".join(f"{n}={p}" for n, p in picks.items() if p) or "no qualifying setup"
    print(f"snapshot written to {out} · best: {picked}")
    return snapshot


def add_watch_jobs(scheduler: Any, cfg: Config, scope: Sequence[str] | None = None) -> None:
    """One recurring refresh job per horizon in `scope` (every horizon by
    default), each on its own cadence: right after each close of its entry bar
    (or every `refresh_seconds`, a whole number of them)."""
    for h in chosen_horizons(cfg, scope):
        start = first_run(h.refresh_seconds)
        scheduler.add_job(
            do_refresh,
            "interval",
            seconds=h.refresh_seconds,
            start_date=start,
            args=[cfg, h.name, scope],
            id=f"refresh-{h.name}",
            max_instances=1,  # a slow refresh must not stack on itself
            coalesce=True,  # missed runs collapse into one
            misfire_grace_time=h.refresh_seconds // 2,  # late beats skipped
        )
        print(
            f"watching '{h.name}' every {h.refresh_seconds}s, after each "
            f"{h.entry} close (next {start:%H:%M:%S} UTC)"
        )


def run_watch(
    cfg: Config, serve: bool, host: str, port: int, scope: Sequence[str] | None = None
) -> int:
    """Keep every horizon in `scope` fresh, optionally while serving the dashboard."""
    if serve:
        from apscheduler.schedulers.background import BackgroundScheduler

        scheduler = BackgroundScheduler(timezone="UTC")
        add_watch_jobs(scheduler, cfg, scope)
        scheduler.start()

        import uvicorn

        uvicorn.run("app.main:app", host=host, port=port)
        return 0

    from apscheduler.schedulers.blocking import BlockingScheduler

    scheduler = BlockingScheduler(timezone="UTC")
    add_watch_jobs(scheduler, cfg, scope)
    print("watch mode running (Ctrl-C to stop)")
    scheduler.start()  # blocks until interrupted
    return 0


def with_every_perp(cfg: Config, scope: Sequence[str] | None) -> Config:
    """`cfg` with its watchlist replaced by every tradable perp on every dex
    (--all), and a word on how long that takes at the public API's rate limit."""
    with HyperliquidClient(cache_dir=cfg.cache_dir) as client:
        try:
            watchlist = every_perp_watchlist(client)
            perps = expand_watchlist(watchlist, client)
        except (HyperliquidError, httpx.HTTPError) as exc:
            sys.exit(f"--all: could not list Hyperliquid's perps ({exc})")
        floor = cfg.scanner.min_open_interest_usd
        liquid, _ = liquid_markets(perps, fetch_asset_contexts(perps, client), floor)
    horizons = chosen_horizons(cfg, scope)
    # At most three candle requests per perp and horizon: tide, wave and entry (a
    # screen whose cached bar is still open costs none).
    minutes = len(liquid) * 3 * len(horizons) / CANDLE_REQUESTS_PER_MINUTE
    # Dexes without a tradable perp today stay in the watchlist (a --watch run picks
    # up their listings) but are not worth naming.
    dexes = sorted({coin_dex(coin) for coin in perps})
    kept = (
        f", {len(liquid)} holding at least {usd_short(floor)} of open interest" if floor > 0 else ""
    )
    print(
        f"--all: {len(perps)} tradable perps on {len(dexes)} dexes "
        f"({', '.join(d or 'native' for d in dexes)}){kept} x {len(horizons)} horizon(s): "
        f"about {minutes:.0f} min at most, at the public API's "
        f"~{CANDLE_REQUESTS_PER_MINUTE} candle requests a minute"
    )
    return replace(cfg, scanner=replace(cfg.scanner, watchlist=watchlist))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true", help="serve the dashboard after refreshing")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--horizon",
        metavar="NAME",
        nargs="+",
        action="extend",
        help="analyse only these horizons (e.g. --horizon swing, --horizon swing scalp): "
        "only they are refreshed, printed, watched and shown on the dashboard",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="keep refreshing: one job per horizon, right after each close of its entry bar",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="this run scans every tradable perp on every Hyperliquid dex instead of the "
        "config watchlist (slow: best with --horizon swing)",
    )
    args = parser.parse_args(argv)

    cfg = load_config()
    # "--horizon swing scalp", "--horizon swing --horizon scalp" and
    # "--horizon swing,scalp" all name the same two horizons.
    scope = [name for arg in args.horizon or [] for name in arg.split(",") if name] or None
    try:
        chosen_horizons(cfg, scope)
    except KeyError as exc:
        parser.error(
            f"{exc.args[0]} — choose from {', '.join(h.name for h in cfg.scanner.horizons)}"
        )

    if args.all:
        cfg = with_every_perp(cfg, scope)

    snapshot = do_refresh(cfg, scope=scope)
    print_signals_tables(snapshot)
    print_positions_table(snapshot)

    if args.watch:
        return run_watch(cfg, args.serve, args.host, args.port, scope)

    if args.serve:
        import uvicorn

        uvicorn.run("app.main:app", host=args.host, port=args.port)

    return 0


if __name__ == "__main__":
    sys.exit(main())
