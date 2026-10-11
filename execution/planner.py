"""What the bot should do — pure functions of the snapshot, the account and its state.

Three passes, in this order each cycle:

1. `reconcile` updates the state from what the exchange reports: a pending entry
   whose position appeared is now an owned position; an owned position that is
   gone was closed (stop hit, exit filled, or closed by hand).
2. `plan_cycle` (once per entry bar of the traded horizon) applies the Triple
   Screen: exits and partial profits on the owned positions' Elder verdicts,
   their trailing stop, and the one pending stop-entry — trailed while its signal
   holds, cancelled when it doesn't, placed for the best qualifying setup.
3. `plan_protection` (every minute, and after every cycle) makes sure each owned
   position carries exactly one protective stop on the exchange, at the stop the
   bot keeps — never looser —, sized to the position; it cancels the bot's
   orders that no longer belong (an orphan stop, an entry on a closed market).

The bot only manages positions it opened. A position opened by hand is never
touched, but it counts: no entry is placed on an asset already held.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from config import ExecutionConfig
from data.hyperliquid import MIN_ORDER_VALUE_USD, PerpSpec, coin_dex
from execution.account import AccountView
from execution.orders import floor_size, through_price
from execution.state import ExecutionState, OwnedPosition
from risk.sizing import position_size

# --- actions -----------------------------------------------------------------


@dataclass(frozen=True)
class PlaceEntry:
    asset: str
    side: str
    size: float
    trigger: float
    limit_px: float
    stop: float
    target: float | None
    leverage: int
    is_cross: bool
    risk: float
    reason: str


@dataclass(frozen=True)
class ModifyEntry:
    asset: str
    oid: int
    cloid: str  # kept: the bot finds its pending entry by it
    side: str
    size: float
    trigger: float
    limit_px: float
    stop: float
    target: float | None
    risk: float
    reason: str


@dataclass(frozen=True)
class CancelOrder:
    asset: str
    oid: int
    kind: str
    reason: str


@dataclass(frozen=True)
class PlaceStop:
    asset: str
    side: str  # the POSITION's side; the stop order is the opposite
    size: float
    trigger: float
    limit_px: float
    reason: str


@dataclass(frozen=True)
class ModifyStop:
    asset: str
    oid: int
    cloid: str
    side: str
    size: float
    trigger: float
    limit_px: float
    reason: str


@dataclass(frozen=True)
class ClosePosition:
    asset: str
    side: str
    size: float
    limit_px: float
    full: bool
    reason: str


@dataclass(frozen=True)
class SetStop:
    """Tighten the stop the bot keeps for a position (state only; the exchange
    order follows in `plan_protection`)."""

    asset: str
    stop: float
    reason: str


@dataclass(frozen=True)
class Note:
    """Something decided NOT to be done, worth a line in the report."""

    asset: str | None
    message: str


Action = (
    PlaceEntry | ModifyEntry | CancelOrder | PlaceStop | ModifyStop | ClosePosition | SetStop | Note
)

# --- helpers -----------------------------------------------------------------


def _sz_decimals(specs: Mapping[str, PerpSpec], asset: str) -> int | None:
    spec = specs.get(asset)
    return spec.sz_decimals if spec is not None else None


def tighter(side: str, a: float, b: float) -> float:
    """The more protective of two stops: the higher for a long, the lower for a short."""
    return max(a, b) if side == "long" else min(a, b)


def choose_leverage(entry: float, stop: float, max_leverage: int, spec: PerpSpec | None) -> int:
    """The leverage for a trade: at most 0.5 / (stop distance as a fraction of the
    entry), so the isolated-margin liquidation sits at least ~twice the stop distance
    away — never inside it, even after the maintenance margin — capped by
    `max_leverage` and the perp's own maximum."""
    distance = abs(entry - stop) / entry
    leverage = math.floor(0.5 / distance) if distance > 0 else 1
    cap = min(max_leverage, spec.max_leverage) if spec and spec.max_leverage else max_leverage
    return max(1, min(leverage, cap))


def qualifies(signal: Mapping[str, Any], ecfg: ExecutionConfig) -> str | None:
    """Why `signal` is not a setup the bot takes, or None when it is.

    The bot trades A-trades (Trade Apgar >= 7, no line at 0) with reward:risk at
    least the 2:1 floor, on inputs whose data-quality flags it was not told to
    distrust.
    """
    if signal.get("action") not in ("long", "short"):
        return f"no setup ({signal.get('reason', 'stand aside')})"
    if not (signal.get("apgar") or {}).get("a_trade"):
        return "not an A-trade"
    if not signal.get("rr_ok"):
        return "reward:risk under 2:1"
    blocked = [f for f in signal.get("data_flags", []) if f in ecfg.block_on_flags]
    if blocked:
        return f"data quality: {', '.join(blocked)}"
    if signal.get("entry") is None or signal.get("stop") is None:
        return "no entry/stop levels"
    return None


def _rank(signal: Mapping[str, Any]) -> tuple[int, float, str]:
    """select_best's order: Trade Apgar, then reward:risk, then name."""
    return (
        (signal.get("apgar") or {}).get("total", 0),
        signal.get("reward_risk") or 0.0,
        signal["asset"],
    )


@dataclass(frozen=True)
class EntryOrder:
    size: float
    trigger: float
    limit_px: float
    stop: float
    target: float | None
    risk: float


def entry_order(
    signal: Mapping[str, Any],
    ecfg: ExecutionConfig,
    spec: PerpSpec | None,
    equity_at_month_start: float,
    risk_pct: float,
) -> EntryOrder | str:
    """The stop-limit entry for `signal`, or why it cannot be placed.

    Trigger: the signal's stop-entry (already on the grid). The limit caps the
    fill `entry_slippage_pct` beyond it, and the Iron Triangle sizes from that
    worst fill — so even a fill at the limit risks no more than `risk_pct`.
    """
    is_buy = signal["action"] == "long"
    sz_decimals = spec.sz_decimals if spec is not None else 2
    trigger, stop = float(signal["entry"]), float(signal["stop"])
    limit_px = through_price(trigger, is_buy, ecfg.entry_slippage_pct, sz_decimals)
    if (is_buy and stop >= trigger) or (not is_buy and stop <= trigger):
        return "stop on the wrong side of the entry"
    sized = position_size(equity_at_month_start, limit_px, stop, risk_pct, sz_decimals)
    if sized.size <= 0:
        return "size rounds to zero"
    if sized.size * limit_px < MIN_ORDER_VALUE_USD:
        return f"order value under Hyperliquid's ${MIN_ORDER_VALUE_USD:.0f} minimum"
    return EntryOrder(
        size=sized.size,
        trigger=trigger,
        limit_px=limit_px,
        stop=stop,
        target=signal.get("target"),
        risk=sized.risk_at_size,
    )


# --- 1. reconcile ------------------------------------------------------------


def reconcile(state: ExecutionState, account: AccountView, now: datetime) -> list[dict[str, Any]]:
    """Bring the state in line with the exchange; return the events logged.

    Nothing is concluded about an asset on a dex whose lookup failed.
    """
    events: list[dict[str, Any]] = []
    stamp = now.isoformat(timespec="seconds")

    def readable(asset: str) -> bool:
        return coin_dex(asset) not in account.failed_dexes

    for asset, entry in list(state.entries.items()):
        if not readable(asset):
            continue
        pos = account.position(asset)
        if pos is not None and pos.side == entry.side and asset not in state.positions:
            state.positions[asset] = OwnedPosition(
                asset=asset,
                side=entry.side,
                entry=pos.entry,
                size=pos.size,
                stop=entry.stop,
                target=entry.target,
                opened_at=stamp,
            )
            del state.entries[asset]
            events.append(
                state.log(
                    "entry_filled",
                    f"{entry.side} {pos.size:g} {asset} filled at {pos.entry:g}; "
                    f"protective stop {entry.stop:g}",
                    asset=asset,
                    level="alert",
                    now=now,
                )
            )
        elif not any(o.cloid == entry.cloid for o in account.orders):
            del state.entries[asset]
            events.append(
                state.log(
                    "entry_gone",
                    f"pending {entry.side} entry on {asset} is no longer on the exchange "
                    f"(cancelled outside the bot, or rejected)",
                    asset=asset,
                    level="alert",
                    now=now,
                )
            )

    for asset, owned in list(state.positions.items()):
        if not readable(asset):
            continue
        pos = account.position(asset)
        if pos is None or pos.side != owned.side:
            del state.positions[asset]
            events.append(
                state.log(
                    "position_closed",
                    f"{owned.side} {asset} (entry {owned.entry:g}) is closed",
                    asset=asset,
                    level="alert",
                    now=now,
                )
            )
        else:
            owned.size = pos.size

    # A position carrying one of the bot's stops is the bot's, even if the state
    # file was lost: take it back, at that stop.
    for pos in account.positions:
        if pos.asset in state.positions or not readable(pos.asset):
            continue
        stops = [
            o for o in account.bot_orders(pos.asset, "stop") if o.is_buy == (pos.side == "short")
        ]
        if stops and stops[0].trigger_px:
            stop = stops[0].trigger_px
            for o in stops[1:]:
                stop = tighter(pos.side, stop, o.trigger_px or stop)
            state.positions[pos.asset] = OwnedPosition(
                asset=pos.asset,
                side=pos.side,
                entry=pos.entry,
                size=pos.size,
                stop=stop,
                target=None,
                opened_at=stamp,
            )
            events.append(
                state.log(
                    "position_adopted",
                    f"{pos.side} {pos.asset} carries a bot stop at {stop:g}: managed again",
                    asset=pos.asset,
                    level="alert",
                    now=now,
                )
            )
    return events


# --- 2. the Triple Screen cycle ------------------------------------------------


def plan_cycle(
    snapshot: Mapping[str, Any],
    state: ExecutionState,
    account: AccountView,
    *,
    ecfg: ExecutionConfig,
    specs: Mapping[str, PerpSpec],
    closed_dexes: set[str] | frozenset[str],
    entry_block: str | None,
    equity_at_month_start: float,
    risk_pct: float,
) -> list[Action]:
    """The orders the traded horizon's fresh snapshot calls for.

    `entry_block` (halt file, unreadable account…) stops new entries and cancels
    the pending one; the 6% Rule, read from the snapshot's guard, does the same.
    Open positions are managed regardless.
    """
    actions: list[Action] = []
    block = (snapshot.get("horizons") or {}).get(ecfg.horizon) or {}
    signals = {s["asset"]: s for s in block.get("signals", [])}
    verdicts = {p["asset"]: p for p in snapshot.get("positions", [])}
    guard = snapshot.get("guard") or {}
    if entry_block is None and guard.get("blocked"):
        entry_block = (
            f"6% Rule: monthly losses + open risk ${guard.get('total_at_risk', 0):,.0f} "
            f">= ${guard.get('limit', 0):,.0f}"
        )

    # --- owned positions: exit, take profits, trail ---------------------------
    for asset, owned in state.positions.items():
        pos = account.position(asset)
        verdict = verdicts.get(asset)
        if pos is None or verdict is None or verdict.get("side") != owned.side:
            continue
        sz_decimals = _sz_decimals(specs, asset)
        closing_buy = owned.side == "short"
        mark = pos.mark_price or verdict.get("live_price") or verdict["close_price"]
        if verdict["verdict"] in ("exit", "take_profits") and coin_dex(asset) in closed_dexes:
            actions.append(
                Note(asset, f"{verdict['verdict']} deferred: market closed; the stop stays on")
            )
        elif verdict["verdict"] == "exit":
            actions.append(
                ClosePosition(
                    asset,
                    owned.side,
                    pos.size,
                    through_price(mark, closing_buy, ecfg.exit_slippage_pct, sz_decimals),
                    full=True,
                    reason="; ".join(verdict.get("reasons", [])) or "exit",
                )
            )
            continue
        elif verdict["verdict"] == "take_profits" and not owned.took_profits:
            part = floor_size(pos.size * ecfg.take_profit_fraction, sz_decimals or 0)
            rest = pos.size - part
            full = part * mark < MIN_ORDER_VALUE_USD or rest * mark < MIN_ORDER_VALUE_USD
            actions.append(
                ClosePosition(
                    asset,
                    owned.side,
                    pos.size if full else part,
                    through_price(mark, closing_buy, ecfg.exit_slippage_pct, sz_decimals),
                    full=full,
                    reason="; ".join(verdict.get("reasons", [])) or "take profits",
                )
            )
            if full:
                continue
        new_stop = verdict.get("suggested_stop")
        if new_stop is not None and tighter(owned.side, new_stop, owned.stop) != owned.stop:
            actions.append(SetStop(asset, float(new_stop), "SafeZone trailing stop"))

    # --- the pending entry: trail it while its signal holds ---------------------
    # Elder (p.161) trails the stop-entry bar after bar "until stopped in or until
    # the weekly indicator reverses and cancels its buy signal". One at a time.
    kept = False
    for pending in list(state.entries.values()):
        order = next(iter(account.bot_orders(pending.asset, "entry")), None)
        if order is None:
            continue  # not on the exchange: reconcile drops it
        signal = signals.get(pending.asset)
        why_not = entry_block
        if why_not is None and coin_dex(pending.asset) in closed_dexes:
            why_not = "market closed"
        if why_not is None and signal is None:
            why_not = "signal gone"
        if why_not is None and signal is not None:
            why_not = qualifies(signal, ecfg)
            if why_not is None and signal["action"] != pending.side:
                why_not = "signal changed side"
        if why_not is None and kept:
            why_not = "another entry is already pending"
        planned = (
            entry_order(signal, ecfg, specs.get(pending.asset), equity_at_month_start, risk_pct)
            if why_not is None and signal is not None
            else why_not
        )
        if isinstance(planned, str):
            actions.append(CancelOrder(pending.asset, order.oid, "entry", planned))
            continue
        assert signal is not None
        kept = True
        if signal.get("price_alert"):
            # The live price already went through the new level: moving the order
            # there would fill at once — keep it where it is.
            actions.append(Note(pending.asset, f"entry not trailed: {signal['price_alert']}"))
        elif (planned.trigger, planned.limit_px, planned.size, planned.stop) != (
            pending.trigger,
            pending.limit_px,
            pending.size,
            pending.stop,
        ):
            entry_tf = block.get("intervals", {}).get("entry", "entry")
            actions.append(
                ModifyEntry(
                    pending.asset,
                    order.oid,
                    pending.cloid,
                    pending.side,
                    planned.size,
                    planned.trigger,
                    planned.limit_px,
                    planned.stop,
                    planned.target,
                    planned.risk,
                    f"trailed to the latest completed {entry_tf} bar",
                )
            )

    # --- a new entry: the best setup the bot can take ---------------------------
    if not kept and entry_block is None:
        actions.extend(
            _new_entry(
                signals.values(),
                state,
                account,
                ecfg=ecfg,
                specs=specs,
                closed_dexes=closed_dexes,
                guard=guard,
                equity_at_month_start=equity_at_month_start,
                risk_pct=risk_pct,
            )
        )
    elif not kept:
        actions.append(Note(None, f"no new entry: {entry_block}"))
    return actions


def _new_entry(
    signals: Iterable[Mapping[str, Any]],
    state: ExecutionState,
    account: AccountView,
    *,
    ecfg: ExecutionConfig,
    specs: Mapping[str, PerpSpec],
    closed_dexes: set[str] | frozenset[str],
    guard: Mapping[str, Any],
    equity_at_month_start: float,
    risk_pct: float,
) -> list[Action]:
    candidates = []
    for s in signals:
        asset = s["asset"]
        if qualifies(s, ecfg) is not None:
            continue
        if coin_dex(asset) in closed_dexes or coin_dex(asset) in account.failed_dexes:
            continue
        if account.position(asset) is not None or account.bot_orders(asset, "entry"):
            continue
        candidates.append(s)
    if not candidates:
        return []
    best = max(candidates, key=_rank)
    asset = best["asset"]
    if best.get("price_alert"):
        return [Note(asset, f"best setup skipped: {best['price_alert']}")]
    spec = specs.get(asset)
    planned = entry_order(best, ecfg, spec, equity_at_month_start, risk_pct)
    if isinstance(planned, str):
        return [Note(asset, f"best setup skipped: {planned}")]
    # The 6% Rule counts what this entry would add (Elder sizes the NEXT trade
    # against it), on top of the losses and open risk already in the guard.
    limit = float(guard.get("limit", 0.0))
    committed = float(guard.get("total_at_risk", 0.0))
    if committed + planned.risk > limit:
        return [
            Note(
                asset,
                f"best setup skipped: its ${planned.risk:,.0f} risk would take the month to "
                f"${committed + planned.risk:,.0f}, over the 6% limit ${limit:,.0f}",
            )
        ]
    leverage = choose_leverage(planned.limit_px, planned.stop, ecfg.max_leverage, spec)
    is_cross = ecfg.margin == "cross" and not (spec is not None and spec.only_isolated)
    margin = planned.size * planned.limit_px / leverage
    if account.equity is not None and margin > account.equity:
        return [
            Note(
                asset,
                f"best setup skipped: needs ${margin:,.0f} margin at {leverage}x, "
                f"account equity ${account.equity:,.0f}",
            )
        ]
    apgar = (best.get("apgar") or {}).get("total")
    return [
        PlaceEntry(
            asset,
            best["action"],
            planned.size,
            planned.trigger,
            planned.limit_px,
            planned.stop,
            planned.target,
            leverage,
            is_cross,
            planned.risk,
            f"A-trade (Apgar {apgar}/10, R:R {best.get('reward_risk') or 0:.2f}): {best['reason']}",
        )
    ]


# --- 3. protection --------------------------------------------------------------


def plan_protection(
    state: ExecutionState,
    account: AccountView,
    *,
    ecfg: ExecutionConfig,
    specs: Mapping[str, PerpSpec],
    closed_dexes: set[str] | frozenset[str],
    halted: bool,
) -> list[Action]:
    """One protective stop per owned position, at the kept stop, sized to it; and
    no bot order left that does not belong."""
    actions: list[Action] = []
    for asset, owned in state.positions.items():
        pos = account.position(asset)
        if pos is None or coin_dex(asset) in account.failed_dexes:
            continue
        sz_decimals = _sz_decimals(specs, asset)
        closing_buy = owned.side == "short"
        stops = [o for o in account.bot_orders(asset, "stop") if o.is_buy == closing_buy]
        if not stops:
            actions.append(
                PlaceStop(
                    asset,
                    owned.side,
                    pos.size,
                    owned.stop,
                    through_price(owned.stop, closing_buy, ecfg.stop_slippage_pct, sz_decimals),
                    "position without a protective stop",
                )
            )
            continue
        # Keep the most protective one; never loosen it to the kept level.
        keep = max(stops, key=lambda o: (o.trigger_px or 0) * (1 if owned.side == "long" else -1))
        for extra in stops:
            if extra is not keep:
                actions.append(CancelOrder(asset, extra.oid, "stop", "duplicate stop"))
        trigger = tighter(owned.side, owned.stop, keep.trigger_px or owned.stop)
        if trigger != keep.trigger_px or not math.isclose(keep.size, pos.size, rel_tol=1e-9):
            actions.append(
                ModifyStop(
                    asset,
                    keep.oid,
                    keep.cloid or "",
                    owned.side,
                    pos.size,
                    trigger,
                    through_price(trigger, closing_buy, ecfg.stop_slippage_pct, sz_decimals),
                    (
                        "stop trailed"
                        if trigger != keep.trigger_px
                        else "stop resized to the position"
                    ),
                )
            )

    for order in account.orders:
        if order.kind is None or coin_dex(order.coin) in account.failed_dexes:
            continue
        if order.kind == "stop" and order.coin not in state.positions:
            actions.append(
                CancelOrder(order.coin, order.oid, "stop", "no position left to protect")
            )
        elif order.kind == "entry":
            pending = state.entries.get(order.coin)
            if order.coin in state.positions or account.position(order.coin) is not None:
                # Partly filled (or the asset is held): the trade is on, at the size
                # filled so far — no adding to it.
                actions.append(
                    CancelOrder(order.coin, order.oid, "entry", "position open — rest cancelled")
                )
            elif pending is None or pending.cloid != order.cloid:
                actions.append(CancelOrder(order.coin, order.oid, "entry", "unknown bot entry"))
            elif halted:
                actions.append(CancelOrder(order.coin, order.oid, "entry", "halt file present"))
            elif coin_dex(order.coin) in closed_dexes:
                actions.append(CancelOrder(order.coin, order.oid, "entry", "market closed"))
    return actions
