"""Where the bot's orders go.

- `DryRunGateway` sends nothing: it keeps a simulated order book in the bot's
  state, so a dry run shows every order that would be placed, trailed or
  cancelled. Its entries never fill, so a dry run never opens a position.
- `HyperliquidGateway` signs with an **API wallet** (Hyperliquid → API: a key
  that can trade for the account but cannot withdraw) through the official
  `hyperliquid-python-sdk`. It refuses the account's own key.

Both report failures as `OrderResult(ok=False, …)` rather than raising: one
rejected order must not stop the cycle that protects the other positions.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from data.hyperliquid import coin_dex
from execution.orders import OrderRequest, OrderResult
from execution.state import ExecutionState

MAINNET_URL = "https://api.hyperliquid.xyz"
TESTNET_URL = "https://api.hyperliquid-testnet.xyz"


class OrderGateway(Protocol):
    mode: str

    def open_orders(self, dex: str) -> list[dict[str, Any]]: ...

    def update_leverage(self, coin: str, leverage: int, is_cross: bool) -> OrderResult: ...

    def place(self, req: OrderRequest) -> OrderResult: ...

    def modify(self, oid: int, req: OrderRequest) -> OrderResult: ...

    def cancel(self, coin: str, oid: int) -> OrderResult: ...


def _wire(req: OrderRequest, oid: int) -> dict[str, Any]:
    """An order as `frontendOpenOrders` reports one."""
    return {
        "coin": req.coin,
        "oid": oid,
        "cloid": req.cloid,
        "side": "B" if req.is_buy else "A",
        "sz": str(req.size),
        "limitPx": str(req.limit_px),
        "isTrigger": req.trigger_px is not None,
        "triggerPx": str(req.trigger_px) if req.trigger_px is not None else "0.0",
        "reduceOnly": req.reduce_only,
    }


class DryRunGateway:
    """Simulates the exchange's order book; sends nothing anywhere."""

    mode = "dry_run"

    def __init__(self, state: ExecutionState) -> None:
        self._state = state

    def open_orders(self, dex: str) -> list[dict[str, Any]]:
        return [o for o in self._state.dry_run_orders if coin_dex(o["coin"]) == dex]

    def update_leverage(self, coin: str, leverage: int, is_cross: bool) -> OrderResult:
        return OrderResult(ok=True, status="dry_run")

    def place(self, req: OrderRequest) -> OrderResult:
        if req.trigger_px is None:  # an IOC market order: nothing rests, nothing fills
            return OrderResult(ok=True, status="dry_run")
        oid = self._state.dry_run_next_oid
        self._state.dry_run_next_oid += 1
        self._state.dry_run_orders.append(_wire(req, oid))
        return OrderResult(ok=True, status="resting", oid=oid)

    def modify(self, oid: int, req: OrderRequest) -> OrderResult:
        for i, o in enumerate(self._state.dry_run_orders):
            if o["oid"] == oid:
                self._state.dry_run_orders[i] = _wire(req, oid)
                return OrderResult(ok=True, status="resting", oid=oid)
        return OrderResult(ok=False, status="error", error=f"unknown order {oid}")

    def cancel(self, coin: str, oid: int) -> OrderResult:
        before = len(self._state.dry_run_orders)
        self._state.dry_run_orders = [o for o in self._state.dry_run_orders if o["oid"] != oid]
        if len(self._state.dry_run_orders) == before:
            return OrderResult(ok=False, status="error", error=f"unknown order {oid}")
        return OrderResult(ok=True, status="dry_run")


def parse_response(resp: Any) -> OrderResult:
    """An `/exchange` response -> OrderResult (the first status of a batch)."""
    if not isinstance(resp, Mapping) or resp.get("status") != "ok":
        detail = resp.get("response") if isinstance(resp, Mapping) else resp
        return OrderResult(ok=False, status="error", error=str(detail))
    data = (resp.get("response") or {}).get("data") or {}
    statuses = data.get("statuses") if isinstance(data, Mapping) else None
    if not statuses:
        return OrderResult(ok=True, status="ok")
    first = statuses[0]
    if isinstance(first, Mapping):
        if "error" in first:
            return OrderResult(ok=False, status="error", error=str(first["error"]))
        if "resting" in first:
            return OrderResult(ok=True, status="resting", oid=int(first["resting"]["oid"]))
        if "filled" in first:
            filled = first["filled"]
            return OrderResult(
                ok=True,
                status="filled",
                oid=int(filled["oid"]) if "oid" in filled else None,
                filled_size=float(filled.get("totalSz", 0.0)),
                avg_price=float(filled["avgPx"]) if "avgPx" in filled else None,
            )
    return OrderResult(ok=True, status=str(first))


class HyperliquidGateway:
    """Real orders, signed by an API wallet, for `account_address`.

    Reads (open orders) go through the keyless info client; only orders, cancels
    and leverage changes are signed.
    """

    def __init__(
        self,
        private_key: str,
        account_address: str,
        base_url: str,
        info_client: Any,
        perp_dexs: list[str],
        mode: str,
    ) -> None:
        # Imported here so the scanner never needs the signing stack installed.
        from eth_account import Account
        from hyperliquid.exchange import Exchange

        wallet = Account.from_key(private_key)
        if wallet.address.lower() == account_address.lower():
            raise ValueError(
                "HL_API_PRIVATE_KEY is the account's own key, which can withdraw funds. "
                "Create an API wallet (app.hyperliquid.xyz → API) and use its key instead."
            )
        self.mode = mode
        self.agent_address = wallet.address
        self._address = account_address
        self._info = info_client
        self._exchange = Exchange(
            wallet, base_url, account_address=account_address, perp_dexs=perp_dexs
        )

    def open_orders(self, dex: str) -> list[dict[str, Any]]:
        return self._info.open_orders(self._address, dex)

    def _order_type(self, req: OrderRequest) -> dict[str, Any]:
        if req.trigger_px is None:
            return {"limit": {"tif": "Ioc"}}
        # "sl" triggers a buy when the price rises to the trigger and a sell when it
        # falls to it: a buy-stop/sell-stop entry, or a protective stop.
        return {
            "trigger": {"triggerPx": req.trigger_px, "isMarket": req.kind == "stop", "tpsl": "sl"}
        }

    def update_leverage(self, coin: str, leverage: int, is_cross: bool) -> OrderResult:
        try:
            return parse_response(self._exchange.update_leverage(leverage, coin, is_cross))
        except Exception as exc:  # noqa: BLE001 - any SDK/transport failure is a failed call
            return OrderResult(ok=False, status="error", error=repr(exc))

    def place(self, req: OrderRequest) -> OrderResult:
        from hyperliquid.utils.types import Cloid

        try:
            resp = self._exchange.order(
                req.coin,
                req.is_buy,
                req.size,
                req.limit_px,
                self._order_type(req),
                reduce_only=req.reduce_only,
                cloid=Cloid.from_str(req.cloid),
            )
            return parse_response(resp)
        except Exception as exc:  # noqa: BLE001
            return OrderResult(ok=False, status="error", error=repr(exc))

    def modify(self, oid: int, req: OrderRequest) -> OrderResult:
        from hyperliquid.utils.types import Cloid

        try:
            resp = self._exchange.modify_order(
                oid,
                req.coin,
                req.is_buy,
                req.size,
                req.limit_px,
                self._order_type(req),
                reduce_only=req.reduce_only,
                cloid=Cloid.from_str(req.cloid),
            )
            return parse_response(resp)
        except Exception as exc:  # noqa: BLE001
            return OrderResult(ok=False, status="error", error=repr(exc))

    def cancel(self, coin: str, oid: int) -> OrderResult:
        try:
            return parse_response(self._exchange.cancel(coin, oid))
        except Exception as exc:  # noqa: BLE001
            return OrderResult(ok=False, status="error", error=repr(exc))
