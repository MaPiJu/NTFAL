"""Append-only trading journal for daily scanner runs.

The scanner is read-only, so this journal records the analysis context and the
operator's pending/held trade decisions. It deliberately stores compact JSONL
entries instead of mutating a trade ledger automatically.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def append_journal_entry(snapshot: dict[str, Any], path: Path) -> None:
    """Append one compact scan summary to `path` as JSON Lines."""
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "generated_at": snapshot["generated_at"],
        "equity": snapshot["equity"],
        "risk_pct": snapshot["risk_pct"],
        "guard": snapshot["guard"],
        "top_pick": snapshot.get("top_pick"),
        "signals": [
            {
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
                "weekly_trend": s.get("weekly_trend"),
                "weekly_impulse": s.get("weekly_impulse"),
                "daily_impulse": s.get("daily_impulse"),
                "divergences": s.get("divergences", []),
            }
            for s in snapshot.get("signals", [])
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
                "verdict": p["verdict"],
                "reasons": p.get("reasons", []),
            }
            for p in snapshot.get("positions", [])
        ],
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
