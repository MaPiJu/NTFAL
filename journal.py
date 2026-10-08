"""Append-only trading journal for scanner runs.

The scanner is read-only, so this journal records the analysis context and the
operator's pending/held trade decisions. It deliberately stores compact JSONL
entries instead of mutating a trade ledger automatically.

One line per refresh; each signal carries the horizon it came from, so a later
review can tell a swing call apart from a five-minute one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _signal_entry(horizon: str, s: dict[str, Any]) -> dict[str, Any]:
    return {
        "horizon": horizon,
        "asset": s["asset"],
        "action": s["action"],
        "reason": s["reason"],
        "entry": s.get("entry"),
        "entry_limit": s.get("entry_limit"),
        "stop": s.get("stop"),
        "target": s.get("target"),
        "reward_risk": s.get("reward_risk"),
        "rr_ok": s.get("rr_ok"),
        "quality_score": s.get("quality_score"),
        "is_top_pick": s.get("is_top_pick", False),
        "tide_trend": s.get("tide_trend"),
        "tide_impulse": s.get("tide_impulse"),
        "wave_impulse": s.get("wave_impulse"),
        "divergences": s.get("divergences", []),
        "data_warnings": s.get("data_warnings", []),
    }


def append_journal_entry(snapshot: dict[str, Any], path: Path) -> None:
    """Append one compact scan summary to `path` as JSON Lines."""
    path.parent.mkdir(parents=True, exist_ok=True)
    horizons = snapshot.get("horizons", {})
    entry = {
        "generated_at": snapshot["generated_at"],
        "equity": snapshot["equity"],
        "risk_pct": snapshot["risk_pct"],
        "guard": snapshot["guard"],
        "top_picks": {name: block.get("top_pick") for name, block in horizons.items()},
        "signals": [
            _signal_entry(name, s)
            for name, block in horizons.items()
            for s in block.get("signals", [])
        ],
        "positions": [
            {
                "asset": p["asset"],
                "side": p["side"],
                "entry": p["entry"],
                "size": p["size"],
                "close_price": p["close_price"],
                "suggested_stop": p["suggested_stop"],
                "open_risk": p.get("open_risk"),
                "cum_funding": p.get("cum_funding"),
                "verdict": p["verdict"],
                "reasons": p.get("reasons", []),
            }
            for p in snapshot.get("positions", [])
        ],
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
