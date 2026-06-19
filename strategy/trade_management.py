"""Elder trade management for an OPEN position (book ch. on exits & the Impulse system).

The Triple Screen tells you *when to enter*; this module answers the operator's
other daily question — "I'm already in this trade, do I hold, take profits, or
get out?" — using only Elder's own exit tools, no new indicators:

- **Impulse censorship, used for exits.** Elder's Impulse system is at its best as
  a censorship system. While you are long it *forbids selling* only as long as the
  Impulse is **green**; the moment green is lost (the bar turns **blue**) the
  prohibition is lifted — that is your *permission to take profits*. A **red**
  Impulse goes further: momentum has actively reversed → get out. Mirror image for
  shorts (red permits holding, blue lifts it, green means reverse → exit).
- **Premise invalidated ("the trade no longer earns its risk").** If the weekly
  tide — the reason you took the trade — flips against the position, the strategic
  case is gone → exit.
- **Profit target.** Take profits when price reaches the weekly value zone
  (between EMA13 and EMA26) or, if price already trades beyond value, the weekly
  channel — exactly the targets the entry logic projects.
- **Trailing stop (SafeZone).** Suggest tightening the protective stop behind the
  recent daily extreme by the average penetration of the fast EMA, ratcheted to at
  least break-even once the trade is in profit. Never widen risk.

Verdict precedence: EXIT (premise dead) > TAKE_PROFITS (target hit or Impulse
permission while in profit) > HOLD. As everywhere in this project the output is
informational only — a human decides and places any order manually.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

import pandas as pd

from indicators import ema, impulse_color
from strategy.triple_screen import (
    EMA_FAST,
    EMA_SLOW,
    Trend,
    average_penetration,
    tick_size,
    weekly_channel,
    weekly_trend,
)

Side = Literal["long", "short"]
Verdict = Literal["hold", "take_profits", "exit"]


@dataclass(frozen=True)
class OpenPosition:
    asset: str
    side: Side
    entry: float
    size: float  # absolute units of the asset (always positive)
    # Live values from the exchange (read-only), used for the displayed price/PnL
    # so they match what Hyperliquid shows. None falls back to the daily close.
    mark_price: float | None = None
    unrealized_pnl: float | None = None


@dataclass(frozen=True)
class TradeManagement:
    asset: str
    side: Side
    entry: float
    size: float
    # Two views of the same position, by design:
    #  - "Elder": the last *completed* daily close — the basis for the verdict.
    #  - "live": the exchange mark price / unrealizedPnl — matches Hyperliquid now.
    close_price: float  # last completed daily close (Elder basis)
    live_price: float  # live mark price (falls back to close_price if unknown)
    pnl_elder: float  # PnL at the daily close: (close - entry) * size, signed by side
    pnl_live: float  # live unrealized PnL from the exchange (or from the mark price)
    return_pct_elder: float  # signed return at the close, in the trade's favor
    return_pct_live: float  # signed live return, in the trade's favor
    weekly_trend: Trend
    weekly_impulse: str
    daily_impulse: str
    in_profit: bool  # by the Elder (close) PnL — keeps the verdict on completed bars
    target: float  # weekly value-zone edge (or channel) in the trade's direction
    target_reached: bool
    suggested_stop: float  # SafeZone trailing stop, ratcheted to >= break-even in profit
    verdict: Verdict
    reasons: list[str]


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_positions(state: Mapping[str, Any]) -> list[OpenPosition]:
    """Extract open perp positions from a Hyperliquid `clearinghouseState` payload.

    `szi` is the signed position size (negative = short); a zero size means the
    position is closed and is skipped. The live `unrealizedPnl` and a mark price
    derived from `positionValue` are carried through so the displayed PnL matches
    the exchange. Read-only: this only *reads* public account state — no key, no
    signing, no order is ever involved.
    """
    out: list[OpenPosition] = []
    for ap in state.get("assetPositions", []):
        p = ap.get("position") or {}
        szi = _to_float(p.get("szi"))
        if not szi:  # None or zero -> not an open position
            continue
        size = abs(szi)
        pos_value = _to_float(p.get("positionValue"))
        mark_price = pos_value / size if pos_value is not None and size else None
        out.append(
            OpenPosition(
                asset=str(p["coin"]),
                side="long" if szi > 0 else "short",
                entry=float(p["entryPx"]),
                size=size,
                mark_price=mark_price,
                unrealized_pnl=_to_float(p.get("unrealizedPnl")),
            )
        )
    return out


def _profit_target(pos: OpenPosition, weekly: pd.DataFrame) -> float:
    """Weekly value-zone edge in the trade's direction, or the weekly channel
    band when price already trades beyond value (mirrors the entry targets)."""
    e13 = float(ema(weekly["close"], EMA_FAST).iloc[-1])
    e26 = float(ema(weekly["close"], EMA_SLOW).iloc[-1])
    if pos.side == "long":
        value_edge = max(e13, e26)
        return value_edge if value_edge > pos.entry else weekly_channel(weekly)[0]
    value_edge = min(e13, e26)
    return value_edge if value_edge < pos.entry else weekly_channel(weekly)[1]


def safezone_stop(pos: OpenPosition, daily: pd.DataFrame, *, in_profit: bool) -> float:
    """Elder SafeZone-style trailing stop: place it behind the recent daily extreme
    by the average EMA penetration, ratcheted to break-even once the trade profits.

    Long: below the lower of the last two daily lows, minus the average downside
    penetration of EMA13; short: above the higher of the last two highs, plus the
    average upside penetration. Reuses the project's `average_penetration` (the same
    basis the entry logic uses) so no new indicator is introduced.
    """
    if pos.side == "long":
        base = float(daily["low"].iloc[-2:].min())
        pen = average_penetration(daily, "down")
        offset = pen if pen is not None else tick_size(base)
        stop = base - offset
        return max(stop, pos.entry) if in_profit else stop
    base = float(daily["high"].iloc[-2:].max())
    pen = average_penetration(daily, "up")
    offset = pen if pen is not None else tick_size(base)
    stop = base + offset
    return min(stop, pos.entry) if in_profit else stop


def assess_position(
    pos: OpenPosition, weekly: pd.DataFrame, daily: pd.DataFrame
) -> TradeManagement:
    """Elder exit verdict for one open position from completed weekly + daily bars.

    `weekly`/`daily` are OHLCV frames of completed bars (oldest first), the same
    shape the Triple Screen consumes. The verdict (trend/Impulse/target) is computed
    on those *completed* bars — the "Elder" view, using the last daily close. The
    result also carries a "live" price and PnL from the exchange (mark price /
    `unrealizedPnl`) so the displayed numbers match Hyperliquid in real time.
    """
    w_imp = str(impulse_color(weekly["close"]).iloc[-1])
    d_imp = str(impulse_color(daily["close"]).iloc[-1])
    trend = weekly_trend(weekly["close"])

    direction = 1.0 if pos.side == "long" else -1.0
    close_price = float(daily["close"].iloc[-1])
    live_price = pos.mark_price if pos.mark_price and pos.mark_price > 0 else close_price

    pnl_elder = (close_price - pos.entry) * pos.size * direction
    pnl_live = (
        pos.unrealized_pnl
        if pos.unrealized_pnl is not None
        else (live_price - pos.entry) * pos.size * direction
    )
    return_pct_elder = (close_price / pos.entry - 1.0) * direction if pos.entry else 0.0
    return_pct_live = (live_price / pos.entry - 1.0) * direction if pos.entry else 0.0
    # Verdict is on completed bars, so "in profit" uses the Elder (close) PnL.
    in_profit = pnl_elder > 0

    target = _profit_target(pos, weekly)
    target_reached = close_price >= target if pos.side == "long" else close_price <= target
    suggested_stop = safezone_stop(pos, daily, in_profit=in_profit)

    favorable_trend: Trend = "up" if pos.side == "long" else "down"
    favorable_imp = "green" if pos.side == "long" else "red"
    adverse_imp = "red" if pos.side == "long" else "green"

    reasons: list[str] = []
    verdict: Verdict = "hold"

    # --- EXIT: the strategic premise is dead -------------------------------
    if trend not in (favorable_trend, "neutral"):
        verdict = "exit"
        reasons.append(
            f"weekly tide flipped to {trend} — the reason for this {pos.side} is gone; exit"
        )
    if w_imp == adverse_imp or d_imp == adverse_imp:
        verdict = "exit"
        screen = "weekly" if w_imp == adverse_imp else "daily"
        reasons.append(
            f"{screen} Impulse turned {adverse_imp} — momentum reversed against the "
            f"{pos.side}; exit"
        )

    # --- TAKE PROFITS: target hit, or Impulse permission while in profit ----
    if verdict != "exit":
        if target_reached:
            verdict = "take_profits"
            reasons.append(
                f"daily close {close_price:.6g} reached the weekly value/channel "
                f"target {target:.6g} — take profits"
            )
        # Permission to take profits once NEITHER screen still shows favorable
        # momentum (the green/red is gone on both the tide and the wave) and the
        # trade is in profit. While even one screen keeps its color, hold.
        lost_favor = w_imp != favorable_imp and d_imp != favorable_imp
        if lost_favor and in_profit:
            verdict = "take_profits"
            reasons.append(
                f"neither weekly ({w_imp}) nor daily ({d_imp}) Impulse is still "
                f"{favorable_imp} — momentum stalling; permission to take profits"
            )

    if not reasons:
        reasons.append(
            "weekly tide and Impulse still favor the trade — hold; trail the stop to "
            f"{suggested_stop:.6g}"
        )

    return TradeManagement(
        asset=pos.asset,
        side=pos.side,
        entry=pos.entry,
        size=pos.size,
        close_price=close_price,
        live_price=live_price,
        pnl_elder=pnl_elder,
        pnl_live=pnl_live,
        return_pct_elder=return_pct_elder,
        return_pct_live=return_pct_live,
        weekly_trend=trend,
        weekly_impulse=w_imp,
        daily_impulse=d_imp,
        in_profit=in_profit,
        target=target,
        target_reached=target_reached,
        suggested_stop=suggested_stop,
        verdict=verdict,
        reasons=reasons,
    )
