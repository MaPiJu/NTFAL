"""The two pillars of risk control (Elder): 2% Iron Triangle + 6% monthly guard."""

from __future__ import annotations

import math
from dataclasses import dataclass

DEFAULT_RISK_PCT = 0.01
HARD_CAP_RISK_PCT = 0.02  # never silently exceeded
MONTHLY_LIMIT_PCT = 0.06


@dataclass(frozen=True)
class PositionSize:
    size: float  # units of the asset, floored to sz_decimals
    risk_budget: float  # equity * risk_pct (after the hard cap)
    risk_at_size: float  # actual $ at risk after flooring: size * |entry - stop|
    risk_per_unit: float  # |entry - stop|
    risk_pct_used: float
    capped: bool  # True if the requested risk_pct exceeded the 2% hard cap


@dataclass(frozen=True)
class MonthlyGuard:
    blocked: bool
    total_at_risk: float  # month_realized_losses + open_trade_risk
    limit: float  # 6% of equity at month start


def position_size(
    equity: float,
    entry: float,
    stop: float,
    risk_pct: float = DEFAULT_RISK_PCT,
    sz_decimals: int = 0,
) -> PositionSize:
    """Iron Triangle: size = floor(max_risk / |entry - stop|), risk capped at 2%."""
    if equity <= 0:
        raise ValueError("equity must be positive")
    if entry <= 0 or stop <= 0:
        raise ValueError("entry and stop must be positive prices")
    if entry == stop:
        raise ValueError("stop must differ from entry (the risk per unit would be zero)")
    if risk_pct <= 0:
        raise ValueError("risk_pct must be positive")

    capped = risk_pct > HARD_CAP_RISK_PCT
    pct = min(risk_pct, HARD_CAP_RISK_PCT)
    budget = equity * pct
    per_unit = abs(entry - stop)
    factor = 10**sz_decimals
    size = math.floor(budget / per_unit * factor) / factor

    return PositionSize(
        size=size,
        risk_budget=budget,
        risk_at_size=size * per_unit,
        risk_per_unit=per_unit,
        risk_pct_used=pct,
        capped=capped,
    )


def six_percent_guard(
    equity_at_month_start: float,
    month_realized_losses: float,
    open_trade_risk: float,
) -> MonthlyGuard:
    """6% Rule: block all new entries once monthly losses + open risk reach 6%."""
    if equity_at_month_start <= 0:
        raise ValueError("equity_at_month_start must be positive")
    limit = MONTHLY_LIMIT_PCT * equity_at_month_start
    total = month_realized_losses + open_trade_risk
    return MonthlyGuard(blocked=total >= limit, total_at_risk=total, limit=limit)
