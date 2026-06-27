"""Configurable knobs for the Elder strategy layer.

Defaults mirror the module constants used by the original implementation; callers
can override them from config.toml without changing code.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyParams:
    flat_trend_slope_pct: float = 0.001
    penetration_lookback_days: int = 35
    # Elder's 2nd-screen caveat: void the Force Index signal when FI(2) prints a
    # new multi-week extreme (accelerating move, not a pullback). ~3-4 weeks.
    force_index_extreme_lookback_days: int = 25
    channel_lookback_weeks: int = 26
    # Elder fits the channel so it contains ~95% of recent bars (p.183).
    channel_containment: float = 0.95
    min_reward_risk: float = 2.0
    divergence_lookback: int = 60
    # Elder/Lovvorn (p.104): the most tradable divergences span 20-40 bars.
    divergence_min_separation: int = 20
    divergence_max_separation: int = 40
    rr_excellent: float = 3.0
    strong_weekly_slope: float = 0.03
    fi_scale_lookback: int = 20
    value_zone_max_distance_pct: float = 0.03
    safezone_lookback_days: int = 20
    # Elder (p.220): shorts need wider stops (>=3) than longs (>=2), since
    # shorting near the highs is noisier and downtrends move faster.
    safezone_factor_long: float = 2.0
    safezone_factor_short: float = 3.0
    entry_order_expire_days: int = 2
    score_reward_risk_weight: float = 0.40
    score_impulse_weight: float = 0.25
    score_tide_weight: float = 0.20
    score_pullback_weight: float = 0.15
