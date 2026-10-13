"""Order types shared by the planner, the gateways and the engine.

Every order the bot places carries a client order id (cloid) that says it is the
bot's and what it is for — an entry, a protective stop or a market close — so the
bot can find its own orders on the exchange after a restart and never touches an
order placed by hand.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from strategy.triple_screen import round_to_tick

OrderKind = Literal["entry", "stop", "close"]
Side = Literal["long", "short"]

# cloid = 0x + "NTFA" in hex + a kind byte + 11 bytes (time + randomness).
CLOID_PREFIX = "4e544641"
_KIND_CODE: dict[str, str] = {"entry": "01", "stop": "02", "close": "03"}
_CODE_KIND = {v: k for k, v in _KIND_CODE.items()}


def make_cloid(kind: OrderKind, now_ms: int | None = None) -> str:
    """A fresh 16-byte cloid tagged as the bot's `kind` order."""
    ms = int(time.time() * 1000) if now_ms is None else now_ms
    return f"0x{CLOID_PREFIX}{_KIND_CODE[kind]}{ms & (2**48 - 1):012x}{secrets.token_hex(5)}"


def cloid_kind(cloid: str | None) -> OrderKind | None:
    """The kind of a bot order, from its cloid; None for an order the bot did not place."""
    if not cloid or not cloid.lower().startswith("0x" + CLOID_PREFIX) or len(cloid) != 34:
        return None
    return _CODE_KIND.get(cloid[10:12].lower())  # type: ignore[return-value]


@dataclass(frozen=True)
class OrderRequest:
    """One order, in the bot's terms.

    `trigger_px` set: a trigger order — `kind="entry"` is a stop-LIMIT (fills at
    `limit_px` or better once the trigger trades), `kind="stop"` a stop-MARKET
    (`limit_px` only bounds its slippage). No trigger: an IOC limit, i.e. a
    market order capped at `limit_px`.
    """

    coin: str
    is_buy: bool
    size: float
    limit_px: float
    kind: OrderKind
    reduce_only: bool
    cloid: str
    trigger_px: float | None = None


@dataclass(frozen=True)
class OrderResult:
    ok: bool
    status: str  # "resting" | "filled" | "dry_run" | "error"
    oid: int | None = None
    filled_size: float = 0.0
    avg_price: float | None = None
    error: str | None = None


@dataclass(frozen=True)
class RestingOrder:
    """An open order as `frontendOpenOrders` reports it."""

    coin: str
    oid: int
    cloid: str | None
    is_buy: bool
    size: float
    limit_px: float
    trigger_px: float | None
    reduce_only: bool

    @property
    def kind(self) -> OrderKind | None:
        return cloid_kind(self.cloid)


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_open_orders(raw: list[Mapping[str, Any]]) -> list[RestingOrder]:
    """`frontendOpenOrders` entries -> RestingOrder (unparsable entries are skipped)."""
    out: list[RestingOrder] = []
    for o in raw:
        try:
            trigger = _float(o.get("triggerPx")) if o.get("isTrigger") else None
            out.append(
                RestingOrder(
                    coin=str(o["coin"]),
                    oid=int(o["oid"]),
                    cloid=o.get("cloid"),
                    is_buy=o.get("side") == "B",
                    size=float(o["sz"]),
                    limit_px=float(o["limitPx"]),
                    trigger_px=trigger if trigger else None,
                    reduce_only=bool(o.get("reduceOnly", False)),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def through_price(
    price: float, is_buy: bool, slippage: float, sz_decimals: int | None = None
) -> float:
    """`price` moved `slippage` against the order and put on the price grid away
    from it (a buy up, a sell down): the worst fill an order accepts."""
    moved = price * (1 + slippage) if is_buy else price * (1 - slippage)
    return round_to_tick(moved, "up" if is_buy else "down", sz_decimals)


def floor_size(size: float, sz_decimals: int) -> float:
    """`size` floored to the perp's size decimals (never rounded up)."""
    factor = 10**sz_decimals
    # The epsilon keeps 0.3 * 10 = 2.9999999999999996 from flooring to 2.
    return int(size * factor + 1e-9) / factor
