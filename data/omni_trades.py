"""Reconstruct open Variational Omni positions from the official Trades CSV.

Omni has no public position lookup (and its portfolio API requires a wallet
signature to log in — off limits per the project's hard constraints). The one
*documented* way to get account data out is the portfolio's CSV download
(docs.variational.io → Technical documentation → Trade and Transfer History):
trade id, timestamp, side, underlying ticker, execution price, quantity, trade
type (trade / liquidation / settlement) and status, limited to 365 days and
10 000 rows per export.

This module replays that trade history chronologically to rebuild the open
positions — signed quantity per instrument, weighted-average entry while a
position grows, entry unchanged while it shrinks, restart at the fill price
when it flips through zero. The result feeds the same Elder trade-management
path as positions read from Hyperliquid.

Caveats (documented in README):
- a position opened more than 365 days ago cannot be fully reconstructed;
- the weighted-average entry is *our* accounting — the venue's own displayed
  entry may differ slightly around partial closes;
- the export headers are not documented, so columns are matched tolerantly by
  name; on failure the error lists the headers found so the mapping can be
  extended.
"""

from __future__ import annotations

import csv
from pathlib import Path

from strategy.trade_management import OpenPosition

# Candidate header spellings per logical column, matched after normalization
# (lowercase, spaces/dashes -> underscores). The Omni docs name the columns
# loosely ("timestamp", "side", "underlying asset ticker", "execution price",
# "quantity", "trade type", "status") without giving the exact header line.
COLUMN_CANDIDATES: dict[str, tuple[str, ...]] = {
    "time": ("timestamp", "time", "created_at", "date", "executed_at", "datetime"),
    "side": ("side", "direction"),
    "ticker": (
        "underlying_asset_ticker",
        "underlying_ticker",
        "underlying",
        "ticker",
        "asset",
        "instrument",
        "symbol",
        "market",
    ),
    "price": ("execution_price", "price", "fill_price", "avg_price", "trade_price"),
    "qty": ("quantity", "qty", "size", "amount"),
    "type": ("trade_type", "type"),
    "status": ("status", "state"),
    "id": ("trade_id", "id"),
}
REQUIRED = ("time", "side", "ticker", "price", "qty")

BUY_SIDES = {"buy", "long", "b", "bid"}
SELL_SIDES = {"sell", "short", "s", "ask"}
# Statuses that mean the fill did NOT happen; anything else changes the position
# (the docs say pending rows are excluded from exports anyway).
DEAD_STATUSES = {"cancelled", "canceled", "rejected", "failed", "pending", "expired"}

POSITION_EPSILON = 1e-12


class OmniTradesError(ValueError):
    """Raised when the Trades CSV cannot be parsed into positions."""


def _normalize(header: str) -> str:
    return header.strip().lower().replace(" ", "_").replace("-", "_")


def _resolve_columns(headers: list[str]) -> dict[str, str]:
    """Map logical column -> actual CSV header, or raise listing what was found."""
    normalized = {_normalize(h): h for h in headers}
    resolved: dict[str, str] = {}
    for logical, candidates in COLUMN_CANDIDATES.items():
        for cand in candidates:
            if cand in normalized:
                resolved[logical] = normalized[cand]
                break
    missing = [c for c in REQUIRED if c not in resolved]
    if missing:
        raise OmniTradesError(
            f"Trades CSV is missing required column(s) {missing}; headers found: "
            f"{headers}. If this is a genuine Omni export, please report the header "
            "line so the column mapping in data/omni_trades.py can be extended."
        )
    return resolved


def _base_ticker(raw: str) -> str:
    """'ETH', 'ETH-USDC', 'ETH/USDC' or 'ETH-PERP' -> 'ETH'."""
    return raw.strip().upper().replace("/", "-").split("-", 1)[0]


def _to_float(raw: str, column: str, line: int) -> float:
    try:
        return float(str(raw).replace(",", ""))
    except ValueError as exc:
        raise OmniTradesError(f"line {line}: cannot parse {column}={raw!r} as a number") from exc


def positions_from_trades_csv(path: Path) -> list[OpenPosition]:
    """Open positions ('omni:TICKER') rebuilt from an Omni Trades CSV export.

    Fully closed instruments net out to zero and are omitted. Trades are
    replayed in timestamp order (ties keep file order); duplicate trade ids
    (e.g. overlapping exports concatenated) are counted once.
    """
    if not path.exists():
        raise OmniTradesError(
            f"Omni trades CSV not found: {path}. Download it from the Omni portfolio "
            "(trades tab, download icon) or clear positions.omni_trades_csv in config.toml."
        )
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise OmniTradesError(f"{path} is empty — no CSV header line found")
        cols = _resolve_columns(list(reader.fieldnames))
        rows = list(reader)

    seen_ids: set[str] = set()
    fills: list[tuple[str, int, str, float, float]] = []  # (time, order, ticker, signed_qty, px)
    for line_no, row in enumerate(rows, start=2):
        if "status" in cols and _normalize(str(row[cols["status"]])) in DEAD_STATUSES:
            continue
        if "id" in cols:
            trade_id = str(row[cols["id"]]).strip()
            if trade_id and trade_id in seen_ids:
                continue
            seen_ids.add(trade_id)
        side = _normalize(str(row[cols["side"]]))
        if side in BUY_SIDES:
            sign = 1.0
        elif side in SELL_SIDES:
            sign = -1.0
        else:
            raise OmniTradesError(f"line {line_no}: unrecognized side {row[cols['side']]!r}")
        qty = abs(_to_float(row[cols["qty"]], "quantity", line_no))
        if qty == 0:
            continue
        price = _to_float(row[cols["price"]], "price", line_no)
        ticker = _base_ticker(str(row[cols["ticker"]]))
        # ISO timestamps sort correctly as strings; epoch numbers do too as
        # long as their digit count is uniform — keep file order for ties.
        fills.append((str(row[cols["time"]]).strip(), line_no, ticker, sign * qty, price))

    fills.sort(key=lambda f: (f[0], f[1]))

    net: dict[str, float] = {}  # signed position per ticker
    entry: dict[str, float] = {}  # weighted-average entry of the open lot
    for _, _, ticker, signed_qty, price in fills:
        pos = net.get(ticker, 0.0)
        new_pos = pos + signed_qty
        if pos == 0.0 or (pos > 0) == (signed_qty > 0):
            # opening or increasing: weighted-average the entry
            total = abs(pos) + abs(signed_qty)
            entry[ticker] = (entry.get(ticker, 0.0) * abs(pos) + price * abs(signed_qty)) / total
        elif (new_pos > 0) != (pos > 0) and abs(new_pos) > POSITION_EPSILON:
            # flipped through zero: the remainder is a fresh lot at this fill
            entry[ticker] = price
        # plain reduction keeps the existing entry
        net[ticker] = new_pos

    out: list[OpenPosition] = []
    for ticker in sorted(net):
        pos = net[ticker]
        if abs(pos) <= POSITION_EPSILON:
            continue
        out.append(
            OpenPosition(
                asset=f"omni:{ticker}",
                side="long" if pos > 0 else "short",
                entry=entry[ticker],
                size=abs(pos),
            )
        )
    return out
