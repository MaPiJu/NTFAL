"""Runs the bot: read the account, plan, send, remember, alert.

`Engine.cycle()` is the Triple Screen pass (once per entry bar of the traded
horizon); `Engine.protect()` the frequent stop check; `Engine.report()` the daily
Telegram report. All three hold one lock, so they never interleave, and each
starts from what the exchange reports — a restart loses nothing but the timing.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from app.pipeline import SNAPSHOT_FILENAME, expand_watchlist, load_snapshot, watchlist_dexes
from config import Config
from data.hyperliquid import HyperliquidError, PerpSpec
from data.sessions import closure_at
from execution.account import AccountView, account_risk, month_start, read_account
from execution.gateway import OrderGateway
from execution.orders import OrderRequest, make_cloid
from execution.planner import (
    Action,
    CancelOrder,
    ClosePosition,
    ModifyEntry,
    ModifyStop,
    Note,
    PlaceEntry,
    PlaceStop,
    SetStop,
    plan_cycle,
    plan_protection,
    reconcile,
)
from execution.state import ExecutionState, PendingEntry, load_state, save_state

STATE_FILENAME = "execution_state.json"

GatewayFactory = Callable[[ExecutionState], OrderGateway]
Refresh = Callable[[Config], dict[str, Any]]
Alert = Callable[[dict[str, Any]], None]


class Engine:
    def __init__(
        self,
        cfg: Config,
        client: Any,
        gateway_factory: GatewayFactory,
        refresh: Refresh,
        alert: Alert | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    ) -> None:
        self.cfg = cfg
        self.client = client
        self._gateway_factory = gateway_factory
        self._refresh = refresh
        self._alert = alert
        self._now = now
        self._lock = threading.Lock()
        self._specs: dict[str, PerpSpec] = {}

    @property
    def state_path(self) -> Path:
        return self.cfg.cache_dir / STATE_FILENAME

    @property
    def address(self) -> str:
        return self.cfg.positions.address

    # -- reads ------------------------------------------------------------------

    def _dexes(self) -> list[str]:
        return watchlist_dexes(self.cfg.scanner.watchlist)

    def _read(self, gateway: OrderGateway) -> AccountView:
        return read_account(self.client, gateway, self.address, self._dexes())

    def specs(self) -> dict[str, PerpSpec]:
        """The watchlist's perp specs; the last known ones if `meta` fails."""
        with contextlib.suppress(HyperliquidError, httpx.HTTPError):
            self._specs = expand_watchlist(self.cfg.scanner.watchlist, self.client)
        return self._specs

    def closed_dexes(self, now: datetime) -> set[str]:
        now_ms = int(now.timestamp() * 1000)
        return {d for d, c in self.cfg.sessions.items() if closure_at(now_ms, c) is not None}

    def halted(self) -> bool:
        return self.cfg.execution.halt_file.exists()

    def _fills_since(self, since: datetime) -> list[dict[str, Any]] | None:
        if not self.address:
            return None
        try:
            return self.client.user_fills(self.address, int(since.timestamp() * 1000))
        except (HyperliquidError, httpx.HTTPError):
            return None

    # -- the two passes ------------------------------------------------------------

    def protect(self) -> list[Action]:
        """Reconcile with the exchange and make sure every owned position carries
        its stop; cancel the bot's orders that no longer belong."""
        with self._lock:
            now = self._now()
            state = load_state(self.state_path)
            seen = len(state.events)
            gateway = self._gateway_factory(state)
            actions = self._protect(state, gateway, now)
            save_state(state, self.state_path, now)
            self._send_alerts(state.events[seen:])
            return actions

    def _protect(self, state: ExecutionState, gateway: OrderGateway, now: datetime) -> list[Action]:
        account = self._read(gateway)
        reconcile(state, account, now)
        actions = plan_protection(
            state,
            account,
            ecfg=self.cfg.execution,
            specs=self._specs or self.specs(),
            closed_dexes=self.closed_dexes(now),
            halted=self.halted(),
        )
        self._apply(actions, state, gateway, now)
        return actions

    def cycle(self) -> list[Action]:
        """One Triple Screen pass on the traded horizon, then protection."""
        with self._lock:
            now = self._now()
            state = load_state(self.state_path)
            seen = len(state.events)
            gateway = self._gateway_factory(state)
            account = self._read(gateway)
            reconcile(state, account, now)
            specs = self.specs()

            ecfg = self.cfg.execution
            risk = self.cfg.risk
            entry_block: str | None = None
            if account.failed_dexes:
                entry_block = "positions unreadable on dex " + ", ".join(
                    f"'{d}'" if d else "native" for d in sorted(account.failed_dexes)
                )
            elif ecfg.auto_equity:
                read = account_risk(risk, account, state, self._fills_since(month_start(now)), now)
                if read is None:
                    entry_block = "account equity or this month's fills unreadable"
                else:
                    risk = read
            if self.halted():
                entry_block = f"halt file {ecfg.halt_file} present"

            snapshot = self._refresh(replace(self.cfg, risk=risk))
            actions = plan_cycle(
                snapshot,
                state,
                account,
                ecfg=ecfg,
                specs=specs,
                closed_dexes=self.closed_dexes(now),
                entry_block=entry_block,
                equity_at_month_start=risk.equity_at_month_start,
                risk_pct=risk.risk_pct,
            )
            self._apply(actions, state, gateway, now)
            actions += self._protect(state, gateway, now)
            save_state(state, self.state_path, now)
            self._send_alerts(state.events[seen:])
            return actions

    # -- the daily report -------------------------------------------------------------

    def report_inputs(self) -> dict[str, Any]:
        """Everything the daily report reads, gathered under the lock."""
        with self._lock:
            now = self._now()
            state = load_state(self.state_path)
            account = self._read(self._gateway_factory(state))
            since = now - timedelta(days=1)
            return {
                "now": now,
                "since": since,
                "state": state,
                "account": account,
                "fills": self._fills_since(since),
                "month_fills": self._fills_since(month_start(now)),
                "snapshot": load_snapshot(self.cfg.cache_dir / SNAPSHOT_FILENAME),
            }

    def mark_reported(self) -> None:
        with self._lock:
            now = self._now()
            state = load_state(self.state_path)
            state.last_report_at = now.isoformat(timespec="seconds")
            save_state(state, self.state_path, now)

    # -- sending ------------------------------------------------------------------------

    def _send_alerts(self, events: list[dict[str, Any]]) -> None:
        if self._alert is None:
            return
        for event in events:
            if event["level"] in ("alert", "error"):
                self._alert(event)

    def _apply(
        self, actions: list[Action], state: ExecutionState, gateway: OrderGateway, now: datetime
    ) -> None:
        for action in actions:
            self._apply_one(action, state, gateway, now)

    def _apply_one(
        self, a: Action, state: ExecutionState, gateway: OrderGateway, now: datetime
    ) -> None:
        def log(kind: str, message: str, level: str = "info") -> None:
            state.log(kind, message, asset=getattr(a, "asset", None), level=level, now=now)

        if isinstance(a, Note):
            log("note", a.message)
        elif isinstance(a, SetStop):
            owned = state.positions.get(a.asset)
            if owned is not None:
                log("stop_moved", f"{a.asset} stop {owned.stop:g} -> {a.stop:g} ({a.reason})")
                owned.stop = a.stop
        elif isinstance(a, PlaceEntry):
            lev = gateway.update_leverage(a.asset, a.leverage, a.is_cross)
            if not lev.ok:
                log("error", f"{a.asset}: leverage {a.leverage}x refused: {lev.error}", "error")
                return
            cloid = make_cloid("entry")
            result = gateway.place(
                OrderRequest(
                    a.asset,
                    a.side == "long",
                    a.size,
                    a.limit_px,
                    "entry",
                    reduce_only=False,
                    cloid=cloid,
                    trigger_px=a.trigger,
                )
            )
            if not result.ok:
                log("error", f"{a.asset}: {a.side} entry refused: {result.error}", "error")
                return
            state.entries[a.asset] = PendingEntry(
                asset=a.asset,
                side=a.side,
                cloid=cloid,
                size=a.size,
                trigger=a.trigger,
                limit_px=a.limit_px,
                stop=a.stop,
                target=a.target,
                leverage=a.leverage,
                risk=a.risk,
                placed_at=now.isoformat(timespec="seconds"),
            )
            target = f", target {a.target:g}" if a.target is not None else ""
            log(
                "entry_placed",
                f"{a.side} {a.size:g} {a.asset}: stop-entry {a.trigger:g} (limit {a.limit_px:g}), "
                f"stop {a.stop:g}{target}, {a.leverage}x, risk ${a.risk:,.2f} — {a.reason}",
                "alert",
            )
        elif isinstance(a, ModifyEntry):
            result = gateway.modify(
                a.oid,
                OrderRequest(
                    a.asset,
                    a.side == "long",
                    a.size,
                    a.limit_px,
                    "entry",
                    reduce_only=False,
                    cloid=a.cloid,
                    trigger_px=a.trigger,
                ),
            )
            pending = state.entries.get(a.asset)
            if not result.ok or pending is None:
                log("error", f"{a.asset}: entry not trailed: {result.error}", "error")
                return
            old = pending.trigger
            pending.size, pending.trigger, pending.limit_px = a.size, a.trigger, a.limit_px
            pending.stop, pending.target, pending.risk = a.stop, a.target, a.risk
            log(
                "entry_trailed",
                f"{a.asset} stop-entry {old:g} -> {a.trigger:g} (limit {a.limit_px:g}), "
                f"size {a.size:g}, stop {a.stop:g} — {a.reason}",
            )
        elif isinstance(a, CancelOrder):
            result = gateway.cancel(a.asset, a.oid)
            if not result.ok:
                log("error", f"{a.asset}: {a.kind} order not cancelled: {result.error}", "error")
                return
            if a.kind == "entry":
                state.entries.pop(a.asset, None)
            log("order_cancelled", f"{a.asset} {a.kind} order cancelled: {a.reason}")
        elif isinstance(a, PlaceStop | ModifyStop):
            req = OrderRequest(
                a.asset,
                a.side == "short",
                a.size,
                a.limit_px,
                "stop",
                reduce_only=True,
                cloid=a.cloid if isinstance(a, ModifyStop) else make_cloid("stop"),
                trigger_px=a.trigger,
            )
            result = gateway.modify(a.oid, req) if isinstance(a, ModifyStop) else gateway.place(req)
            if not result.ok:
                log(
                    "error",
                    f"{a.asset}: protective stop {a.trigger:g} NOT set ({result.error}) — "
                    f"retried every protection pass",
                    "error",
                )
                return
            log("stop_set", f"{a.asset} protective stop {a.trigger:g} for {a.size:g} ({a.reason})")
        elif isinstance(a, ClosePosition):
            self._close(a, state, gateway, log)

    def _close(
        self,
        a: ClosePosition,
        state: ExecutionState,
        gateway: OrderGateway,
        log: Callable[..., None],
    ) -> None:
        what = "exit" if a.full else "partial profit-taking"
        result = gateway.place(
            OrderRequest(
                a.asset,
                a.side == "short",
                a.size,
                a.limit_px,
                "close",
                reduce_only=True,
                cloid=make_cloid("close"),
            )
        )
        if not result.ok:
            log("error", f"{a.asset}: {what} refused: {result.error}", "error")
            return
        if result.status == "dry_run":
            log("close_sent", f"{a.asset}: {what} of {a.size:g} (dry run) — {a.reason}")
            return
        if result.filled_size <= 0:
            log(
                "close_unfilled",
                f"{a.asset}: {what} of {a.size:g} found no liquidity within the limit "
                f"{a.limit_px:g}; retried next cycle, the stop stays on",
                "alert",
            )
            return
        owned = state.positions.get(a.asset)
        if owned is not None and not a.full:
            owned.took_profits = True
        avg = f" at {result.avg_price:g}" if result.avg_price is not None else ""
        log(
            "closed", f"{a.asset}: {what}, {result.filled_size:g} closed{avg} — {a.reason}", "alert"
        )


def describe(actions: list[Action]) -> str:
    """One line per action, for the console."""
    lines = [f"  {type(a).__name__}: {a}" for a in actions]
    return "\n".join(lines) if lines else "  (nothing to do)"
