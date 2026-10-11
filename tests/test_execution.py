"""The execution layer — every test offline: fake accounts, the dry-run gateway,
or a stand-in for the SDK's Exchange. Nothing here can reach an exchange."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from config import ExecutionConfig
from data.hyperliquid import PerpSpec
from execution.account import AccountView, account_risk, realized_pnl
from execution.engine import Engine
from execution.gateway import DryRunGateway, parse_response
from execution.orders import (
    OrderRequest,
    OrderResult,
    RestingOrder,
    cloid_kind,
    floor_size,
    make_cloid,
    parse_open_orders,
    through_price,
)
from execution.planner import (
    CancelOrder,
    ClosePosition,
    ModifyEntry,
    ModifyStop,
    Note,
    PlaceEntry,
    PlaceStop,
    SetStop,
    choose_leverage,
    entry_order,
    plan_cycle,
    plan_protection,
    qualifies,
    reconcile,
)
from execution.state import ExecutionState, OwnedPosition, PendingEntry, load_state, save_state
from strategy.trade_management import OpenPosition
from tests.test_app import make_config

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)  # a Wednesday: xyz is open
ECFG = ExecutionConfig(enabled=True, horizon="swing")
SPECS = {
    "xyz:GOLD": PerpSpec(sz_decimals=4, max_leverage=25),
    "xyz:SILVER": PerpSpec(sz_decimals=2, max_leverage=20, only_isolated=True),
    "BTC": PerpSpec(sz_decimals=5, max_leverage=40),
}


def signal(asset: str = "xyz:GOLD", **over: Any) -> dict[str, Any]:
    """A swing signal row as the snapshot carries it: by default a long A-trade."""
    row = {
        "asset": asset,
        "action": "long",
        "reason": "tide up, wave 2-EMA Force Index below zero (pullback to buy)",
        "entry": 4012.3,
        "stop": 3950.1,
        "target": 4180.0,
        "reward_risk": 2.7,
        "rr_ok": True,
        "apgar": {"total": 8, "a_trade": True, "lines": []},
        "data_flags": [],
        "price_alert": None,
        "is_top_pick": True,
    }
    row.update(over)
    return row


def snapshot(*signals: dict[str, Any], positions=(), guard=None) -> dict[str, Any]:
    return {
        "guard": guard or {"blocked": False, "total_at_risk": 0.0, "limit": 600.0},
        "positions": list(positions),
        "horizons": {"swing": {"signals": list(signals), "intervals": {"entry": "4h"}}},
    }


def account(positions=(), orders=(), equity=10_000.0, failed=()) -> AccountView:
    return AccountView(
        positions=list(positions),
        orders=list(orders),
        equity=equity,
        failed_dexes=frozenset(failed),
    )


def position(asset="xyz:GOLD", side="long", entry=4000.0, size=1.5, mark=4100.0) -> OpenPosition:
    return OpenPosition(asset=asset, side=side, entry=entry, size=size, mark_price=mark)


def resting(coin, kind, *, is_buy, size, trigger=None, limit=1.0, oid=1, cloid=None):
    return RestingOrder(
        coin=coin,
        oid=oid,
        cloid=cloid or (make_cloid(kind) if kind else None),
        is_buy=is_buy,
        size=size,
        limit_px=limit,
        trigger_px=trigger,
        reduce_only=kind == "stop",
    )


def verdict(asset="xyz:GOLD", side="long", v="hold", stop=3990.0, close=4100.0) -> dict[str, Any]:
    return {
        "asset": asset,
        "side": side,
        "entry": 4000.0,
        "verdict": v,
        "suggested_stop": stop,
        "close_price": close,
        "live_price": close,
        "reasons": [f"{v} reason"],
    }


def cycle(snap, state, acct, *, entry_block=None, closed=(), ecfg=ECFG):
    return plan_cycle(
        snap,
        state,
        acct,
        ecfg=ecfg,
        specs=SPECS,
        closed_dexes=set(closed),
        entry_block=entry_block,
        equity_at_month_start=10_000.0,
        risk_pct=0.01,
    )


# --- orders ---------------------------------------------------------------------------


def test_a_cloid_says_whose_order_it_is_and_what_for():
    for kind in ("entry", "stop", "close"):
        cloid = make_cloid(kind)
        assert len(cloid) == 34 and cloid.startswith("0x")
        assert cloid_kind(cloid) == kind
    assert make_cloid("entry") != make_cloid("entry")
    assert cloid_kind(None) is None
    assert cloid_kind("0x" + "ab" * 16) is None  # an order placed by hand


def test_through_price_moves_against_the_order_onto_the_grid():
    assert through_price(4000.0, True, 0.005, 4) == 4020.0
    assert through_price(4000.0, False, 0.10, 4) == 3600.0
    # Off the grid: a buy's worst price rounds up, a sell's down.
    assert through_price(1.23456, True, 0.01, 0) == 1.2469
    assert through_price(1.23456, False, 0.01, 0) == 1.2222


def test_floor_size_never_rounds_up():
    assert floor_size(0.3, 1) == 0.3
    assert floor_size(1.23456, 2) == 1.23
    assert floor_size(0.75 * 0.5, 2) == 0.37


def test_open_orders_are_parsed_from_frontend_open_orders():
    raw = [
        {
            "coin": "xyz:GOLD",
            "oid": 7,
            "cloid": make_cloid("stop"),
            "side": "A",
            "sz": "1.5",
            "limitPx": "3600",
            "isTrigger": True,
            "triggerPx": "3990",
            "reduceOnly": True,
        },
        {"coin": "BTC", "oid": 8, "side": "B", "sz": "0.1", "limitPx": "90000"},
        {"coin": "broken"},
    ]
    stop, manual = parse_open_orders(raw)
    assert (stop.kind, stop.is_buy, stop.trigger_px, stop.reduce_only) == (
        "stop",
        False,
        3990.0,
        True,
    )
    assert (manual.kind, manual.trigger_px, manual.is_buy) == (None, None, True)


# --- sizing and leverage --------------------------------------------------------------


def test_the_entry_is_sized_from_its_worst_fill_so_the_2_percent_rule_holds():
    planned = entry_order(signal(), ECFG, SPECS["xyz:GOLD"], 10_000.0, 0.01)
    assert not isinstance(planned, str)
    assert planned.trigger == 4012.3
    assert planned.limit_px == pytest.approx(4012.3 * 1.005, abs=0.1)
    # Risk at the limit (the worst fill) stays within 1% of the month-start equity.
    assert planned.size * (planned.limit_px - planned.stop) <= 100.0 + 1e-9
    assert planned.risk == pytest.approx(planned.size * (planned.limit_px - planned.stop))


def test_an_order_under_the_10_dollar_minimum_is_not_placed():
    assert "minimum" in entry_order(signal(), ECFG, SPECS["xyz:GOLD"], 1.0, 0.01)


def test_leverage_keeps_liquidation_beyond_twice_the_stop_distance():
    # 2% stop: at most 25x, capped by the config's 10x.
    assert choose_leverage(100.0, 98.0, 10, PerpSpec(4, max_leverage=25)) == 10
    # 8% stop: 0.5 / 0.08 = 6.25 -> 6x, so 1/6 = 16.7% of margin >= 2 x 8%.
    assert choose_leverage(100.0, 92.0, 10, PerpSpec(4, max_leverage=25)) == 6
    # The perp's own maximum wins when lower.
    assert choose_leverage(100.0, 99.0, 10, PerpSpec(4, max_leverage=3)) == 3
    # A very wide stop still trades at 1x, never 0.
    assert choose_leverage(100.0, 40.0, 10, None) == 1


# --- qualification --------------------------------------------------------------------


def test_only_a_trades_at_2_to_1_on_trusted_data_qualify():
    assert qualifies(signal(), ECFG) is None
    assert "A-trade" in qualifies(signal(apgar={"total": 6, "a_trade": False}), ECFG)
    assert "2:1" in qualifies(signal(rr_ok=False, reward_risk=1.6), ECFG)
    assert "no setup" in qualifies(signal(action="stand_aside"), ECFG)
    assert "low_volume" in qualifies(signal(data_flags=["low_volume"]), ECFG)
    # Short tide history is not blocking by default (the xyz perps are young)...
    assert qualifies(signal(data_flags=["short_history"]), ECFG) is None
    # ...but can be made so.
    strict = replace(ECFG, block_on_flags=("short_history",))
    assert "short_history" in qualifies(signal(data_flags=["short_history"]), strict)


# --- new entries ----------------------------------------------------------------------


def test_the_best_qualifying_setup_gets_a_stop_limit_entry():
    silver = signal("xyz:SILVER", entry=50.12, stop=48.5, target=55.0, reward_risk=2.2)
    snap = snapshot(signal(), silver)
    (action,) = cycle(snap, ExecutionState(), account())
    assert isinstance(action, PlaceEntry)
    assert (action.asset, action.side, action.trigger) == ("xyz:GOLD", "long", 4012.3)
    assert action.stop == 3950.1 and action.target == 4180.0
    assert action.leverage >= 1 and not action.is_cross


def test_the_scanner_pick_under_2_to_1_gives_way_to_the_next_a_trade():
    snap = snapshot(
        signal(rr_ok=False, reward_risk=1.5, apgar={"total": 9, "a_trade": True}),
        signal("xyz:SILVER", entry=50.12, stop=48.5, target=55.0, is_top_pick=False),
    )
    (action,) = cycle(snap, ExecutionState(), account())
    assert isinstance(action, PlaceEntry) and action.asset == "xyz:SILVER"


def test_no_entry_on_an_asset_already_held_even_by_hand():
    snap = snapshot(signal())
    assert cycle(snap, ExecutionState(), account([position()])) == []


def test_no_entry_while_the_market_is_closed():
    assert cycle(snapshot(signal()), ExecutionState(), account(), closed={"xyz"}) == []


def test_no_entry_when_the_live_price_already_ran_through_it():
    snap = snapshot(signal(price_alert="entry already triggered live — don't chase"))
    (note,) = cycle(snap, ExecutionState(), account())
    assert isinstance(note, Note) and "chase" in note.message


def test_the_6_percent_rule_counts_the_new_trade_before_placing_it():
    snap = snapshot(signal(), guard={"blocked": False, "total_at_risk": 550.0, "limit": 600.0})
    (note,) = cycle(snap, ExecutionState(), account())
    assert isinstance(note, Note) and "6% limit" in note.message


def test_a_tripped_6_percent_rule_blocks_entries():
    snap = snapshot(signal(), guard={"blocked": True, "total_at_risk": 640.0, "limit": 600.0})
    (note,) = cycle(snap, ExecutionState(), account())
    assert isinstance(note, Note) and "6% Rule" in note.message


def test_not_enough_margin_skips_the_setup():
    snap = snapshot(signal())
    (note,) = cycle(snap, ExecutionState(), account(equity=50.0))
    assert isinstance(note, Note) and "margin" in note.message


# --- the pending entry ----------------------------------------------------------------


def pending_state(asset="xyz:GOLD", side="long", trigger=4012.3, stop=3950.1):
    state = ExecutionState()
    planned = entry_order(signal(entry=trigger, stop=stop), ECFG, SPECS[asset], 10_000.0, 0.01)
    state.entries[asset] = PendingEntry(
        asset=asset,
        side=side,
        cloid=make_cloid("entry"),
        size=planned.size,
        trigger=planned.trigger,
        limit_px=planned.limit_px,
        stop=planned.stop,
        target=4180.0,
        leverage=10,
        risk=planned.risk,
        placed_at=NOW.isoformat(),
    )
    order = resting(
        asset,
        "entry",
        is_buy=side == "long",
        size=planned.size,
        trigger=planned.trigger,
        oid=42,
        cloid=state.entries[asset].cloid,
    )
    return state, order


def test_the_pending_entry_is_trailed_to_the_new_bar_keeping_its_cloid():
    state, order = pending_state()
    snap = snapshot(signal(entry=4001.7, stop=3948.0))
    (action,) = cycle(snap, state, account(orders=[order]))
    assert isinstance(action, ModifyEntry)
    assert (action.oid, action.cloid, action.trigger) == (42, order.cloid, 4001.7)


def test_an_unchanged_pending_entry_is_left_alone():
    state, order = pending_state()
    assert cycle(snapshot(signal()), state, account(orders=[order])) == []


@pytest.mark.parametrize(
    ("snap_signals", "why"),
    [
        ((), "signal gone"),
        ((signal(action="stand_aside"),), "no setup"),
        ((signal(action="short", entry=3990.0, stop=4050.0, target=3800.0),), "changed side"),
        ((signal(apgar={"total": 6, "a_trade": False}),), "A-trade"),
    ],
)
def test_the_pending_entry_is_cancelled_when_its_signal_fails(snap_signals, why):
    state, order = pending_state()
    actions = cycle(snapshot(*snap_signals), state, account(orders=[order]))
    cancel = next(a for a in actions if isinstance(a, CancelOrder))
    assert cancel.oid == 42 and why in cancel.reason


def test_a_halt_cancels_the_pending_entry_and_places_none():
    state, order = pending_state()
    actions = cycle(snapshot(signal()), state, account(orders=[order]), entry_block="halt")
    assert isinstance(actions[0], CancelOrder) and actions[0].reason == "halt"
    assert not any(isinstance(a, PlaceEntry) for a in actions)


# --- owned positions ------------------------------------------------------------------


def owned_state(asset="xyz:GOLD", side="long", stop=3950.0, took=False) -> ExecutionState:
    state = ExecutionState()
    state.positions[asset] = OwnedPosition(
        asset=asset,
        side=side,
        entry=4000.0,
        size=1.5,
        stop=stop,
        target=4180.0,
        opened_at=NOW.isoformat(),
        took_profits=took,
    )
    return state


def test_an_exit_verdict_closes_the_whole_position_at_a_capped_price():
    snap = snapshot(positions=[verdict(v="exit")])
    (action,) = cycle(snap, owned_state(), account([position()]), entry_block="x")[:1]
    assert isinstance(action, ClosePosition)
    assert action.full and action.size == 1.5
    assert action.limit_px == through_price(4100.0, False, ECFG.exit_slippage_pct, 4)


def test_take_profits_closes_half_once():
    snap = snapshot(positions=[verdict(v="take_profits")])
    actions = cycle(snap, owned_state(), account([position()]), entry_block="x")
    close = next(a for a in actions if isinstance(a, ClosePosition))
    assert not close.full and close.size == 0.75
    again = cycle(snap, owned_state(took=True), account([position()]), entry_block="x")
    assert not any(isinstance(a, ClosePosition) for a in again)


def test_take_profits_on_a_dust_position_closes_it_all():
    snap = snapshot(positions=[verdict(v="take_profits")])
    tiny = position(size=0.004, mark=4100.0)  # $16: half would be under $10
    close = next(
        a
        for a in cycle(snap, owned_state(), account([tiny]), entry_block="x")
        if isinstance(a, ClosePosition)
    )
    assert close.full and close.size == 0.004


def test_the_trailing_stop_only_tightens():
    tighter = cycle(
        snapshot(positions=[verdict(stop=3990.0)]),
        owned_state(),
        account([position()]),
        entry_block="x",
    )
    assert any(isinstance(a, SetStop) and a.stop == 3990.0 for a in tighter)
    looser = cycle(
        snapshot(positions=[verdict(stop=3900.0)]),
        owned_state(),
        account([position()]),
        entry_block="x",
    )
    assert not any(isinstance(a, SetStop) for a in looser)


def test_a_short_trails_its_stop_down():
    snap = snapshot(positions=[verdict(side="short", stop=4060.0)])
    state = owned_state(side="short", stop=4100.0)
    actions = cycle(snap, state, account([position(side="short")]), entry_block="x")
    assert any(isinstance(a, SetStop) and a.stop == 4060.0 for a in actions)


def test_exits_wait_for_the_market_to_reopen():
    snap = snapshot(positions=[verdict(v="exit")])
    actions = cycle(snap, owned_state(), account([position()]), closed={"xyz"}, entry_block="x")
    assert not any(isinstance(a, ClosePosition) for a in actions)
    assert any(isinstance(a, Note) and "market closed" in a.message for a in actions)


def test_a_position_opened_by_hand_is_never_managed():
    snap = snapshot(positions=[verdict(v="exit")])
    actions = cycle(snap, ExecutionState(), account([position()]), entry_block="x")
    assert not any(isinstance(a, ClosePosition | SetStop) for a in actions)


# --- reconcile ------------------------------------------------------------------------


def test_a_filled_entry_becomes_an_owned_position_with_its_stop():
    state, _ = pending_state()
    events = reconcile(state, account([position(entry=4012.5, size=2.0)]), NOW)
    owned = state.positions["xyz:GOLD"]
    assert (owned.entry, owned.size, owned.stop) == (4012.5, 2.0, 3950.1)
    assert not state.entries
    assert events[0]["kind"] == "entry_filled" and events[0]["level"] == "alert"


def test_a_vanished_entry_is_forgotten():
    state, _ = pending_state()
    events = reconcile(state, account(), NOW)
    assert not state.entries and events[0]["kind"] == "entry_gone"


def test_a_closed_position_is_forgotten():
    state = owned_state()
    events = reconcile(state, account(), NOW)
    assert not state.positions and events[0]["kind"] == "position_closed"


def test_nothing_is_concluded_on_an_unreadable_dex():
    state = owned_state()
    assert reconcile(state, account(failed={"xyz"}), NOW) == []
    assert "xyz:GOLD" in state.positions


def test_a_position_carrying_a_bot_stop_is_adopted_after_a_lost_state():
    stop = resting("xyz:GOLD", "stop", is_buy=False, size=1.5, trigger=3975.0)
    state = ExecutionState()
    events = reconcile(state, account([position()], [stop]), NOW)
    assert state.positions["xyz:GOLD"].stop == 3975.0
    assert events[0]["kind"] == "position_adopted"


# --- protection -----------------------------------------------------------------------


def protect(state, acct, *, halted=False, closed=()):
    return plan_protection(
        state, acct, ecfg=ECFG, specs=SPECS, closed_dexes=set(closed), halted=halted
    )


def test_an_unprotected_position_gets_a_stop_market_order():
    (action,) = protect(owned_state(stop=3950.0), account([position()]))
    assert isinstance(action, PlaceStop)
    assert (action.side, action.size, action.trigger) == ("long", 1.5, 3950.0)
    assert action.limit_px == through_price(3950.0, False, ECFG.stop_slippage_pct, 4)


def test_a_resting_stop_is_never_loosened():
    exchange_stop = resting("xyz:GOLD", "stop", is_buy=False, size=1.5, trigger=3980.0)
    assert protect(owned_state(stop=3950.0), account([position()], [exchange_stop])) == []


def test_the_stop_follows_the_kept_level_and_the_position_size():
    exchange_stop = resting("xyz:GOLD", "stop", is_buy=False, size=1.5, trigger=3950.0, oid=9)
    (action,) = protect(owned_state(stop=3990.0), account([position(size=0.75)], [exchange_stop]))
    assert isinstance(action, ModifyStop)
    assert (action.oid, action.trigger, action.size) == (9, 3990.0, 0.75)
    assert action.cloid == exchange_stop.cloid


def test_duplicate_stops_keep_the_most_protective():
    loose = resting("xyz:GOLD", "stop", is_buy=False, size=1.5, trigger=3900.0, oid=1)
    tight = resting("xyz:GOLD", "stop", is_buy=False, size=1.5, trigger=3960.0, oid=2)
    actions = protect(owned_state(stop=3950.0), account([position()], [loose, tight]))
    assert [type(a) for a in actions] == [CancelOrder]
    assert actions[0].oid == 1


def test_orphan_bot_orders_are_cancelled_and_manual_ones_untouched():
    orphan = resting("xyz:SILVER", "stop", is_buy=False, size=3, trigger=40.0, oid=5)
    stranger = resting("xyz:SILVER", "entry", is_buy=True, size=3, trigger=60.0, oid=6)
    manual = resting("xyz:SILVER", None, is_buy=True, size=3, limit=45.0, oid=7)
    actions = protect(ExecutionState(), account(orders=[orphan, stranger, manual]))
    assert sorted(a.oid for a in actions) == [5, 6]
    assert all(isinstance(a, CancelOrder) for a in actions)


def test_the_pending_entry_is_pulled_on_halt_and_for_the_weekend():
    state, order = pending_state()
    assert protect(state, account(orders=[order])) == []
    (halt,) = protect(state, account(orders=[order]), halted=True)
    assert "halt" in halt.reason
    (weekend,) = protect(state, account(orders=[order]), closed={"xyz"})
    assert "closed" in weekend.reason


# --- account bookkeeping --------------------------------------------------------------


def test_the_month_start_equity_is_the_first_read_of_the_month():
    risk = make_config(Path(".")).risk
    state = ExecutionState()
    fills = [{"closedPnl": "-120.0", "fee": "3.0"}, {"closedPnl": "40.0", "fee": "1.0"}]
    first = account_risk(risk, account(equity=9_500.0), state, fills, NOW)
    assert first.equity_at_month_start == 9_500.0
    assert first.month_realized_losses == pytest.approx(84.0)
    later = account_risk(risk, account(equity=9_000.0), state, [], NOW)
    assert (later.equity, later.equity_at_month_start) == (9_000.0, 9_500.0)
    next_month = account_risk(
        risk, account(equity=9_000.0), state, [], datetime(2026, 11, 1, tzinfo=UTC)
    )
    assert next_month.equity_at_month_start == 9_000.0
    assert account_risk(risk, account(equity=None), state, [], NOW) is None
    assert realized_pnl([{"closedPnl": "10", "fee": "0.5"}]) == 9.5


# --- state ----------------------------------------------------------------------------


def test_state_round_trips_and_prunes_old_events(tmp_path):
    state, _ = pending_state()
    state.positions = owned_state().positions
    state.log("old", "long ago", now=datetime(2026, 9, 1, tzinfo=UTC))
    state.log("new", "today", now=NOW)
    path = tmp_path / "state.json"
    save_state(state, path, NOW)
    back = load_state(path)
    assert back.entries == state.entries and back.positions == state.positions
    assert [e["kind"] for e in back.events] == ["new"]


def test_a_corrupt_state_file_stops_the_bot_rather_than_forgetting(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    with pytest.raises(ValueError):
        load_state(path)


# --- gateways -------------------------------------------------------------------------


def test_the_dry_run_keeps_a_book_and_sends_nothing():
    state = ExecutionState()
    gw = DryRunGateway(state)
    req = OrderRequest("xyz:GOLD", True, 1.0, 4020.0, "entry", False, make_cloid("entry"), 4000.0)
    placed = gw.place(req)
    assert placed.status == "resting" and len(gw.open_orders("xyz")) == 1
    assert gw.open_orders("") == []
    assert gw.modify(placed.oid, replace(req, trigger_px=3990.0)).ok
    assert gw.open_orders("xyz")[0]["triggerPx"] == "3990.0"
    ioc = gw.place(replace(req, trigger_px=None, kind="close", reduce_only=True))
    assert ioc.status == "dry_run" and ioc.filled_size == 0
    assert gw.cancel("xyz:GOLD", placed.oid).ok and not gw.open_orders("xyz")
    assert not gw.cancel("xyz:GOLD", placed.oid).ok


def test_exchange_responses_are_parsed():
    def ok(status):
        return {"status": "ok", "response": {"type": "order", "data": {"statuses": [status]}}}

    assert parse_response(ok({"resting": {"oid": 12}})) == OrderResult(True, "resting", oid=12)
    filled = parse_response(ok({"filled": {"oid": 3, "totalSz": "0.75", "avgPx": "4101.5"}}))
    assert (filled.status, filled.filled_size, filled.avg_price) == ("filled", 0.75, 4101.5)
    assert parse_response(ok({"error": "Insufficient margin"})).error == "Insufficient margin"
    assert not parse_response({"status": "err", "response": "User or API Wallet does not exist"}).ok
    assert parse_response({"status": "ok", "response": {"type": "default"}}).ok
    assert parse_response(ok("success")).ok


class FakeExchange:
    """Stands in for hyperliquid.exchange.Exchange: records calls, sends nothing."""

    calls: list[tuple[str, tuple, dict]] = []

    def __init__(self, wallet, base_url, account_address=None, perp_dexs=None):
        FakeExchange.calls = [("init", (base_url, account_address, perp_dexs), {})]

    def order(self, *args, **kwargs):
        FakeExchange.calls.append(("order", args, kwargs))
        return {
            "status": "ok",
            "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 1}}]}},
        }

    def modify_order(self, *args, **kwargs):
        FakeExchange.calls.append(("modify", args, kwargs))
        return {"status": "ok", "response": {"type": "default"}}

    def cancel(self, *args):
        FakeExchange.calls.append(("cancel", args, {}))
        raise ConnectionError("network down")

    def update_leverage(self, *args):
        FakeExchange.calls.append(("leverage", args, {}))
        return {"status": "ok", "response": {"type": "default"}}


@pytest.fixture
def hl_gateway(monkeypatch):
    pytest.importorskip("hyperliquid")
    import hyperliquid.exchange
    from eth_account import Account

    from execution.gateway import HyperliquidGateway

    monkeypatch.setattr(hyperliquid.exchange, "Exchange", FakeExchange)
    agent = Account.create()
    main = Account.create()

    def build(account_address=main.address):
        return HyperliquidGateway(
            "0x" + bytes(agent.key).hex(),
            account_address,
            "https://example.invalid",
            None,
            ["", "xyz"],
            "testnet",
        )

    return build, agent


def test_the_gateway_refuses_the_accounts_own_key(hl_gateway):
    build, agent = hl_gateway
    with pytest.raises(ValueError, match="API wallet"):
        build(account_address=agent.address)


def test_the_gateway_maps_orders_to_sdk_order_types(hl_gateway):
    build, _ = hl_gateway
    gw = build()
    assert FakeExchange.calls[0][1][2] == ["", "xyz"]
    entry = OrderRequest("xyz:GOLD", True, 1.0, 4020.0, "entry", False, make_cloid("entry"), 4000.0)
    stop = OrderRequest("xyz:GOLD", False, 1.0, 3600.0, "stop", True, make_cloid("stop"), 3950.0)
    close = OrderRequest("xyz:GOLD", False, 0.5, 4050.0, "close", True, make_cloid("close"))
    assert gw.place(entry).oid == 1
    gw.place(stop)
    gw.place(close)
    (_, e_args, e_kw), (_, s_args, s_kw), (_, c_args, c_kw) = FakeExchange.calls[1:]
    # A stop-LIMIT entry, a stop-MARKET protective stop, an IOC close.
    assert e_args[4] == {"trigger": {"triggerPx": 4000.0, "isMarket": False, "tpsl": "sl"}}
    assert not e_kw["reduce_only"] and e_kw["cloid"].to_raw() == entry.cloid
    assert s_args[4] == {"trigger": {"triggerPx": 3950.0, "isMarket": True, "tpsl": "sl"}}
    assert s_kw["reduce_only"]
    assert c_args[4] == {"limit": {"tif": "Ioc"}} and c_kw["reduce_only"]
    # A transport failure is a failed call, not a crash.
    failed = gw.cancel("xyz:GOLD", 1)
    assert not failed.ok and "network down" in failed.error


# --- the engine, end to end, in a dry run ---------------------------------------------


class FakeInfo:
    """The keyless info reads the engine makes, served from memory."""

    def __init__(self, positions=(), equity="10000"):
        self.positions = list(positions)
        self.equity = equity

    def clearinghouse_state(self, address, dex=""):
        mine = [p for p in self.positions if (":" in p["coin"]) == bool(dex)]
        return {
            "assetPositions": [{"type": "oneWay", "position": p} for p in mine],
            "marginSummary": {"accountValue": self.equity if not dex else "0"},
        }

    def user_fills(self, address, start_ms, end_ms=None):
        return []

    def validate_watchlist(self, coins):
        return {c: SPECS[c] for c in coins}

    def tradable_perps(self, dex=""):
        return {}


def make_engine(tmp_path, snap, info=None, alerts=None):
    cfg = make_config(tmp_path, watchlist=("xyz:GOLD", "xyz:SILVER"), address="0x" + "1" * 40)
    # The halt file lives in the test's own directory, never the repo's cache/.
    cfg = replace(cfg, execution=replace(ECFG, halt_file=tmp_path / "HALT"), sessions={})
    snaps = snap if isinstance(snap, list) else [snap]
    engine = Engine(
        cfg,
        info or FakeInfo(),
        DryRunGateway,
        refresh=lambda c: snaps.pop(0) if len(snaps) > 1 else snaps[0],
        alert=(alerts.append if alerts is not None else None),
        now=lambda: NOW,
    )
    return engine


def test_a_dry_run_cycle_places_then_trails_the_entry(tmp_path):
    alerts: list[dict] = []
    engine = make_engine(
        tmp_path, [snapshot(signal()), snapshot(signal(entry=4001.7, stop=3948.0))], alerts=alerts
    )
    first = engine.cycle()
    assert any(isinstance(a, PlaceEntry) for a in first)
    state = load_state(engine.state_path)
    assert list(state.entries) == ["xyz:GOLD"]
    (book_order,) = state.dry_run_orders
    assert book_order["cloid"] == state.entries["xyz:GOLD"].cloid
    assert alerts and alerts[0]["kind"] == "entry_placed"

    second = engine.cycle()
    assert any(isinstance(a, ModifyEntry) for a in second)
    state = load_state(engine.state_path)
    assert state.entries["xyz:GOLD"].trigger == 4001.7
    assert state.dry_run_orders[0]["triggerPx"] == "4001.7"


def test_the_engine_protects_a_filled_entry_within_one_pass(tmp_path):
    engine = make_engine(tmp_path, snapshot(signal()))
    engine.cycle()
    # The entry fills on the exchange: the position appears, the order is gone.
    state = load_state(engine.state_path)
    state.dry_run_orders.clear()
    save_state(state, engine.state_path, NOW)
    engine.client.positions = [
        {"coin": "xyz:GOLD", "szi": "0.5", "entryPx": "4013.0", "positionValue": "2006.5"}
    ]
    actions = engine.protect()
    assert any(isinstance(a, PlaceStop) and a.trigger == 3950.1 for a in actions)
    state = load_state(engine.state_path)
    assert state.positions["xyz:GOLD"].size == 0.5
    stop = state.dry_run_orders[0]
    assert stop["reduceOnly"] and stop["isTrigger"] and cloid_kind(stop["cloid"]) == "stop"


def test_a_halt_file_blocks_entries(tmp_path):
    engine = make_engine(tmp_path, snapshot(signal()))
    (tmp_path / "HALT").write_text("")
    actions = engine.cycle()
    assert not any(isinstance(a, PlaceEntry) for a in actions)
    assert any(isinstance(a, Note) and "halt" in a.message for a in actions)


def test_an_unreadable_account_blocks_entries_but_not_management(tmp_path):
    engine = make_engine(tmp_path, snapshot(signal()), info=FakeInfo(equity="not a number"))
    actions = engine.cycle()
    assert not any(isinstance(a, PlaceEntry) for a in actions)


def test_a_dry_run_never_builds_a_signing_gateway(tmp_path):
    # The engine is handed DryRunGateway itself: nothing in a dry run can sign.
    engine = make_engine(tmp_path, snapshot(signal()))
    engine.cycle()
    state = load_state(engine.state_path)
    assert isinstance(engine._gateway_factory(state), DryRunGateway)


# --- telegram ---------------------------------------------------------------------------


def test_long_messages_are_split_on_line_boundaries():
    from notify.telegram import split_message

    lines = [f"line {i} " + "x" * 50 for i in range(200)]
    chunks = split_message("\n".join(lines), limit=1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert "\n".join(chunks) == "\n".join(lines)
    assert split_message("y" * 2500, limit=1000) == ["y" * 1000, "y" * 1000, "y" * 500]


def test_the_notifier_posts_html_and_never_raises(capsys):
    import json

    import httpx

    from notify.telegram import TelegramNotifier

    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append({"url": str(request.url), **json.loads(request.content)})
        return httpx.Response(200 if len(sent) == 1 else 500, json={"ok": True})

    bot = TelegramNotifier("123:SECRET", "42", httpx.Client(transport=httpx.MockTransport(handler)))
    assert bot.send("<b>hello</b>")
    assert sent[0]["url"] == "https://api.telegram.org/bot123:SECRET/sendMessage"
    assert (sent[0]["chat_id"], sent[0]["parse_mode"]) == ("42", "HTML")
    assert not bot.send("refused")

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}")

    offline = TelegramNotifier(
        "123:SECRET", "42", httpx.Client(transport=httpx.MockTransport(down))
    )
    assert not offline.send("hello")
    assert "SECRET" not in capsys.readouterr().err  # the token never reaches a log line
    with pytest.raises(ValueError):
        TelegramNotifier("", "42")


def test_an_alert_escapes_the_event_text():
    from notify.telegram import format_alert

    text = format_alert({"level": "error", "message": "stop <NOT> set & retried"}, "mainnet")
    assert text.startswith("🚨 <b>[MAINNET]</b>")
    assert "&lt;NOT&gt;" in text and "&amp;" in text


# --- the daily report -----------------------------------------------------------------------


def test_the_daily_report_covers_the_day(tmp_path):
    from datetime import timedelta

    from notify.report import build_report

    cfg = replace(make_config(tmp_path, watchlist=("xyz:GOLD",)), execution=ECFG)
    state, _ = pending_state()
    state.positions = owned_state().positions
    state.month = {"key": "2026-10", "equity": 9_800.0}
    state.log(
        "entry_filled", "long 1.5 xyz:GOLD filled at 4000", asset="xyz:GOLD", level="alert", now=NOW
    )
    state.log("note", "best setup skipped: <chasing>", asset="xyz:SILVER", now=NOW)
    state.log("stop_moved", "too old to report", now=NOW - timedelta(days=2))
    fills = [
        {
            "time": int(NOW.timestamp() * 1000),
            "coin": "xyz:GOLD",
            "dir": "Open Long",
            "sz": "1.5",
            "px": "4000",
            "fee": "1.2",
            "closedPnl": "0.0",
        },
    ]
    snap = snapshot(signal(), positions=[verdict()])
    snap["horizons"]["swing"]["generated_at"] = NOW.isoformat()
    text = build_report(
        cfg,
        now=NOW,
        since=NOW - timedelta(days=1),
        state=state,
        account=account([position(), position("BTC", size=0.1, entry=90_000.0, mark=91_000.0)]),
        fills=fills,
        month_fills=fills,
        snapshot=snap,
    )
    assert "DRY-RUN" in text and "swing" in text
    assert "10,000.00 $" in text and "+2.04%" in text  # equity vs the month start
    assert "Open Long" in text and "frais 1.20 $" in text
    assert "filled at 4000" in text and "too old" not in text
    assert "&lt;chasing&gt;" in text  # notes are escaped
    assert "géré par le bot" in text and "manuel — non géré" in text
    assert "Entrée en attente" in text and "stop-entry 4,012.3" in text
    assert "Meilleur trade : <b>xyz:GOLD long</b>" in text
    assert "Règle des 6 %" in text and "OK" in text


# --- config ------------------------------------------------------------------------------------


def patched_config(tmp_path, section: str, key: str, value: str) -> Path:
    """The shipped config.toml with `key = value` set in `[section]` only."""
    out, inside, done = [], False, False
    for line in Path("config.toml").read_text().split("\n"):
        if line.startswith("["):
            inside = line.strip() == f"[{section}]"
            out.append(line)
            if inside:
                out.append(f"{key} = {value}")
                done = True
            continue
        if inside and line.startswith(f"{key} = "):
            continue
        out.append(line)
    assert done
    path = tmp_path / "config.toml"
    path.write_text("\n".join(out))
    return path


def test_execution_is_off_and_dry_by_default():
    from config import load_config

    cfg = load_config()
    assert not cfg.execution.enabled and cfg.execution.mode == "dry_run"
    assert cfg.execution.horizon == cfg.scanner.positions_horizon == "swing"
    assert not cfg.telegram.enabled


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("mode", '"live"'),
        ("horizon", '"micro"'),
        ("block_on_flags", '["vibes"]'),
        ("entry_slippage_pct", "0.5"),
        ("take_profit_fraction", "0"),
        ("margin", '"portfolio"'),
    ],
)
def test_bad_execution_settings_are_refused(tmp_path, key, value):
    from config import ConfigError, load_config

    path = patched_config(tmp_path, "execution", key, value)
    message = "positions_horizon" if key == "horizon" else key
    with pytest.raises(ConfigError, match=message):
        load_config(path)


@pytest.mark.parametrize(
    ("key", "value"), [("report_time", '"25:00"'), ("timezone", '"Mars/Olympus"')]
)
def test_bad_telegram_settings_are_refused(tmp_path, key, value):
    from config import ConfigError, load_config

    with pytest.raises(ConfigError, match=key):
        load_config(patched_config(tmp_path, "telegram", key, value))


# --- data-quality codes -------------------------------------------------------------------------


def test_signals_carry_data_quality_codes_beside_the_messages():
    from strategy.triple_screen import LOW_VOLUME, SHORT_HISTORY, evaluate_asset
    from tests.test_triple_screen import DAILY_LONG, WEEKLY_UP

    sig = evaluate_asset("BTC", WEEKLY_UP.iloc[-20:], DAILY_LONG, min_tide_bars=60)
    assert SHORT_HISTORY in sig.data_flags and len(sig.data_flags) == len(sig.data_warnings)
    quiet = DAILY_LONG.copy()
    quiet.loc[quiet.index[-6:], "volume"] = 1.0
    assert LOW_VOLUME in evaluate_asset("BTC", WEEKLY_UP, quiet).data_flags
