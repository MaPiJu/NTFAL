"""The trading account as the exchange reports it — public, keyless reads.

Positions and margin come from `clearinghouseState`, one request per perp dex
(a HIP-3 dex like `xyz` has its own clearinghouse); resting orders from the
gateway (the exchange's `frontendOpenOrders`, or the dry run's own book); the
month's realized PnL from `userFillsByTime`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

import httpx

from config import RiskConfig
from data.hyperliquid import HyperliquidError
from execution.gateway import OrderGateway
from execution.orders import RestingOrder, parse_open_orders
from execution.state import ExecutionState
from strategy.trade_management import OpenPosition, parse_positions


@dataclass(frozen=True)
class AccountView:
    positions: list[OpenPosition]
    orders: list[RestingOrder]
    equity: float | None  # sum of accountValue over the dexes read; None if any failed
    failed_dexes: frozenset[str] = field(default_factory=frozenset)

    def position(self, asset: str) -> OpenPosition | None:
        return next((p for p in self.positions if p.asset == asset), None)

    def bot_orders(self, asset: str, kind: str) -> list[RestingOrder]:
        return [o for o in self.orders if o.coin == asset and o.kind == kind]


def read_account(
    client: Any, gateway: OrderGateway, address: str, dexes: Sequence[str]
) -> AccountView:
    """Positions, equity and resting orders across `dexes`.

    A dex whose lookup fails is reported in `failed_dexes`: its positions and
    orders are unknown this run (not absent), so nothing is decided about them.
    Without an address (a dry run may have none) the account is empty.
    """
    positions: list[OpenPosition] = []
    orders: list[RestingOrder] = []
    equity = 0.0
    failed: set[str] = set()
    for dex in dexes:
        try:
            if address:
                state = client.clearinghouse_state(address, dex=dex)
                dex_positions = parse_positions(state)
                value = float((state.get("marginSummary") or {}).get("accountValue", 0.0))
            else:
                dex_positions, value = [], 0.0
            dex_orders = parse_open_orders(gateway.open_orders(dex))
        except (HyperliquidError, httpx.HTTPError, TypeError, KeyError, ValueError):
            failed.add(dex)
            continue
        positions.extend(dex_positions)
        orders.extend(dex_orders)
        equity += value
    return AccountView(
        positions=positions,
        orders=orders,
        equity=equity if address and not failed else None,
        failed_dexes=frozenset(failed),
    )


def month_key(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m")


def month_start(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def realized_pnl(fills: Sequence[dict[str, Any]]) -> float:
    """Closed PnL net of fees over `fills` (positive = gain)."""
    return sum(float(f.get("closedPnl", 0.0)) - float(f.get("fee", 0.0)) for f in fills)


def account_risk(
    risk: RiskConfig,
    account: AccountView,
    state: ExecutionState,
    month_fills: Sequence[dict[str, Any]] | None,
    now: datetime,
) -> RiskConfig | None:
    """[risk] with the equity figures read off the account (auto_equity).

    Elder sets the 2% and 6% limits from the equity on the first day of the month
    (p.204): the first equity read in a new month is stored as its start. The
    month's realized losses are the net loss of its fills (0 when net positive).
    None when the equity or the fills could not be read: the bot then cannot size
    a trade safely and takes no new entry.
    """
    if account.equity is None or month_fills is None:
        return None
    key = month_key(now)
    if state.month is None or state.month.get("key") != key:
        state.month = {"key": key, "equity": account.equity}
    return replace(
        risk,
        equity=account.equity,
        equity_at_month_start=float(state.month["equity"]),
        month_realized_losses=max(0.0, -realized_pnl(month_fills)),
    )
