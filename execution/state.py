"""What the bot remembers between runs, in one JSON file.

The exchange is the source of truth for positions and resting orders; this file
only holds what the exchange cannot say: which positions the bot opened (it never
manages a position opened by hand), the stop it keeps for each, whether it already
took its partial profits, the pending entry's planned stop, the month-start
equity, and a log of what it did for the daily report.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Events older than this are dropped on save; the daily report reads the last 24h.
EVENT_RETENTION = timedelta(days=7)


@dataclass
class PendingEntry:
    """The bot's resting stop-entry for one asset."""

    asset: str
    side: str  # "long" | "short"
    cloid: str
    size: float
    trigger: float
    limit_px: float
    stop: float  # protective stop placed once filled
    target: float | None
    leverage: int
    risk: float  # $ at risk at the worst fill (limit_px to stop)
    placed_at: str


@dataclass
class OwnedPosition:
    """A position the bot opened (or re-adopted from its own stop order)."""

    asset: str
    side: str
    entry: float
    size: float
    stop: float  # the protective stop the bot keeps on the exchange; never loosened
    target: float | None
    opened_at: str
    took_profits: bool = False


@dataclass
class ExecutionState:
    entries: dict[str, PendingEntry] = field(default_factory=dict)
    positions: dict[str, OwnedPosition] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    # {"key": "YYYY-MM", "equity": float}: equity on the first read of the month.
    month: dict[str, Any] | None = None
    # The dry run's simulated order book (frontendOpenOrders-shaped).
    dry_run_orders: list[dict[str, Any]] = field(default_factory=list)
    dry_run_next_oid: int = 1
    last_report_at: str | None = None

    def log(
        self,
        kind: str,
        message: str,
        *,
        asset: str | None = None,
        level: str = "info",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Record one event; `level` "alert" or "error" also goes out as an instant alert."""
        event = {
            "time": (now or datetime.now(tz=UTC)).isoformat(timespec="seconds"),
            "kind": kind,
            "asset": asset,
            "level": level,
            "message": message,
        }
        self.events.append(event)
        return event

    def events_since(self, since: datetime) -> list[dict[str, Any]]:
        return [e for e in self.events if datetime.fromisoformat(e["time"]) >= since]


def load_state(path: Path) -> ExecutionState:
    """The stored state, or a fresh one when there is none.

    A corrupt file is NOT silently replaced: losing it would forget which
    positions the bot owns, so it raises and the bot stops.
    """
    if not path.exists():
        return ExecutionState()
    raw = json.loads(path.read_text())
    return ExecutionState(
        entries={k: PendingEntry(**v) for k, v in raw.get("entries", {}).items()},
        positions={k: OwnedPosition(**v) for k, v in raw.get("positions", {}).items()},
        events=list(raw.get("events", [])),
        month=raw.get("month"),
        dry_run_orders=list(raw.get("dry_run_orders", [])),
        dry_run_next_oid=int(raw.get("dry_run_next_oid", 1)),
        last_report_at=raw.get("last_report_at"),
    )


def save_state(state: ExecutionState, path: Path, now: datetime | None = None) -> None:
    """Write the state atomically (aside, then swapped in), pruning old events."""
    cutoff = (now or datetime.now(tz=UTC)) - EVENT_RETENTION
    state.events = [e for e in state.events if datetime.fromisoformat(e["time"]) >= cutoff]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(asdict(state), indent=1))
    os.replace(tmp, path)
