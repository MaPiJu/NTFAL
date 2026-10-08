"""Elder trade management for an OPEN position (book ch. on exits & the Impulse system).

The Triple Screen tells you *when to enter*; this module answers the operator's
other question — "I'm already in this trade, do I hold, take profits, or get
out?" — using only Elder's own exit tools, no new indicators:

- **Impulse censorship, used for exits.** Elder's Impulse system is at its best as
  a censorship system. While you are long it *forbids selling* only as long as the
  Impulse is **green**; the moment green is lost (the bar turns **blue**) the
  prohibition is lifted — that is your *permission to take profits*. A **red**
  Impulse goes further: momentum has actively reversed → get out. Mirror image for
  shorts (red permits holding, blue lifts it, green means reverse → exit).
- **Premise invalidated ("the trade no longer earns its risk").** If the tide —
  the reason you took the trade — flips against the position, the strategic case
  is gone → exit.
- **Profit target.** Take profits when price reaches the tide value zone (between
  EMA13 and EMA26) or, if price already trades beyond value, the tide channel —
  exactly the targets the entry logic projects.
- **Trailing stop (SafeZone).** Suggest tightening the protective stop behind the
  recent wave extreme by the average adverse noise, ratcheted to at least
  break-even once the trade is in profit. Never widen risk: given the last
  suggestion for the same position, a new one only moves in the trade's direction.

Both screens come from the horizon configured as `positions_horizon` — a held
position is strategic, so it is judged on the same chain that would have entered it.

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
from strategy.params import StrategyParams
from strategy.triple_screen import (
    EMA_FAST,
    EMA_SLOW,
    Trend,
    average_adverse_noise,
    channel,
    tick_size,
    tide_trend,
)

DEFAULT_PARAMS = StrategyParams()

Side = Literal["long", "short"]
Verdict = Literal["hold", "take_profits", "exit"]


@dataclass(frozen=True)
class OpenPosition:
    asset: str
    side: Side
    entry: float
    size: float  # absolute units of the asset (always positive)
    # Live values from the exchange (read-only), used for the displayed price/PnL
    # so they match what Hyperliquid shows. None falls back to the wave close.
    mark_price: float | None = None
    unrealized_pnl: float | None = None
    # Hyperliquid's cumFunding.sinceOpen: USD of funding PAID since the position
    # opened (negative = received). None when unknown (e.g. a manual position).
    cum_funding: float | None = None


@dataclass(frozen=True)
class TradeManagement:
    asset: str
    side: Side
    entry: float
    size: float
    # Two views of the same position, by design:
    #  - "Elder": the last *completed* wave close — the basis for the verdict.
    #  - "live": the exchange mark price / unrealizedPnl — matches Hyperliquid now.
    close_price: float  # last completed wave close (Elder basis)
    live_price: float  # live mark price (falls back to close_price if unknown)
    pnl_elder: float  # PnL at the wave close: (close - entry) * size, signed by side
    pnl_live: float  # live unrealized PnL from the exchange (or from the mark price)
    return_pct_elder: float  # signed return at the close, in the trade's favor
    return_pct_live: float  # signed live return, in the trade's favor
    tide_trend: Trend
    tide_impulse: str
    wave_impulse: str
    in_profit: bool  # by the Elder (close) PnL — keeps the verdict on completed bars
    target: float  # tide value-zone edge (or channel) in the trade's direction
    target_reached: bool
    suggested_stop: float  # SafeZone trailing stop, ratcheted to >= break-even in profit
    verdict: Verdict
    reasons: list[str]
    cum_funding: float | None = None  # funding paid since open (negative = received)


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
    the exchange, with `cumFunding.sinceOpen` — the funding paid since the position
    opened (negative = received), a holding cost the price PnL leaves out.
    Read-only: this only *reads* public account state — no key, no signing, no
    order is ever involved.
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
        funding = p.get("cumFunding") or {}
        out.append(
            OpenPosition(
                asset=str(p["coin"]),
                side="long" if szi > 0 else "short",
                entry=float(p["entryPx"]),
                size=size,
                mark_price=mark_price,
                unrealized_pnl=_to_float(p.get("unrealizedPnl")),
                cum_funding=_to_float(funding.get("sinceOpen")),
            )
        )
    return out


def _profit_target(
    pos: OpenPosition, tide: pd.DataFrame, params: StrategyParams = DEFAULT_PARAMS
) -> float:
    """Tide value-zone edge in the trade's direction, or the tide channel band
    when price already trades beyond value (mirrors the entry targets)."""
    e13 = float(ema(tide["close"], EMA_FAST).iloc[-1])
    e26 = float(ema(tide["close"], EMA_SLOW).iloc[-1])
    if pos.side == "long":
        value_edge = max(e13, e26)
        if value_edge > pos.entry:
            return value_edge
        return channel(tide, params)[0]
    value_edge = min(e13, e26)
    if value_edge < pos.entry:
        return value_edge
    return channel(tide, params)[1]


def safezone_stop(
    pos: OpenPosition,
    wave: pd.DataFrame,
    *,
    in_profit: bool,
    params: StrategyParams = DEFAULT_PARAMS,
) -> float:
    """Elder SafeZone trailing stop behind the recent wave extreme.

    Longs subtract average downside noise (lows undercutting prior lows); shorts
    add average upside noise (highs exceeding prior highs). The stop is ratcheted
    to break-even once the completed wave close puts the trade in profit.
    """
    if pos.side == "long":
        base = float(wave["low"].iloc[-2:].min())
        noise = average_adverse_noise(wave, "long", params.safezone_lookback_bars)
        offset = (noise * params.safezone_factor_long) if noise is not None else tick_size(base)
        stop = base - offset
        return max(stop, pos.entry) if in_profit else stop
    base = float(wave["high"].iloc[-2:].max())
    noise = average_adverse_noise(wave, "short", params.safezone_lookback_bars)
    offset = (noise * params.safezone_factor_short) if noise is not None else tick_size(base)
    stop = base + offset
    return min(stop, pos.entry) if in_profit else stop


def assess_position(
    pos: OpenPosition,
    tide: pd.DataFrame,
    wave: pd.DataFrame,
    params: StrategyParams = DEFAULT_PARAMS,
    previous_stop: float | None = None,
) -> TradeManagement:
    """Elder exit verdict for one open position from completed tide + wave bars.

    `tide`/`wave` are OHLCV frames of completed bars (oldest first), the same
    shape the Triple Screen consumes. The verdict (trend/Impulse/target) is computed
    on those *completed* bars — the "Elder" view, using the last wave close. The
    result also carries a "live" price and PnL from the exchange (mark price /
    `unrealizedPnl`) so the displayed numbers match Hyperliquid in real time.

    `previous_stop` is the last stop suggested for this same position: the new
    suggestion never moves back past it (Elder, p.224: "move your stop only in
    the direction of your trade").
    """
    t_imp = str(impulse_color(tide["close"]).iloc[-1])
    w_imp = str(impulse_color(wave["close"]).iloc[-1])
    trend = tide_trend(tide["close"], min_slope_pct=params.flat_trend_slope_pct)

    direction = 1.0 if pos.side == "long" else -1.0
    close_price = float(wave["close"].iloc[-1])
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

    target = _profit_target(pos, tide, params)
    target_reached = close_price >= target if pos.side == "long" else close_price <= target
    suggested_stop = safezone_stop(pos, wave, in_profit=in_profit, params=params)
    if previous_stop is not None:
        pick = max if pos.side == "long" else min
        suggested_stop = pick(suggested_stop, previous_stop)

    favorable_trend: Trend = "up" if pos.side == "long" else "down"
    favorable_imp = "green" if pos.side == "long" else "red"
    adverse_imp = "red" if pos.side == "long" else "green"

    reasons: list[str] = []
    verdict: Verdict = "hold"

    # --- EXIT: the strategic premise is dead -------------------------------
    if trend not in (favorable_trend, "neutral"):
        verdict = "exit"
        reasons.append(f"tide flipped to {trend} — the reason for this {pos.side} is gone; exit")
    if t_imp == adverse_imp or w_imp == adverse_imp:
        verdict = "exit"
        screen = "tide" if t_imp == adverse_imp else "wave"
        reasons.append(
            f"{screen} Impulse turned {adverse_imp} — momentum reversed against the "
            f"{pos.side}; exit"
        )

    # --- TAKE PROFITS: target hit, or Impulse permission while in profit ----
    if verdict != "exit":
        if target_reached:
            verdict = "take_profits"
            reasons.append(
                f"wave close {close_price:.6g} reached the tide value/channel "
                f"target {target:.6g} — take profits"
            )
        # Permission to take profits once NEITHER screen still shows favorable
        # momentum (the green/red is gone on both the tide and the wave) and the
        # trade is in profit. While even one screen keeps its color, hold.
        lost_favor = t_imp != favorable_imp and w_imp != favorable_imp
        if lost_favor and in_profit:
            verdict = "take_profits"
            reasons.append(
                f"neither tide ({t_imp}) nor wave ({w_imp}) Impulse is still "
                f"{favorable_imp} — momentum stalling; permission to take profits"
            )

    if not reasons:
        reasons.append(
            "tide and Impulse still favor the trade — hold; trail the stop to "
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
        tide_trend=trend,
        tide_impulse=t_imp,
        wave_impulse=w_imp,
        in_profit=in_profit,
        target=target,
        target_reached=target_reached,
        suggested_stop=suggested_stop,
        verdict=verdict,
        reasons=reasons,
        cum_funding=pos.cum_funding,
    )
