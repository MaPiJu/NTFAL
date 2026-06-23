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
    channel_lookback_weeks: int = 26
    min_reward_risk: float = 2.0
    divergence_lookback: int = 60
    rr_excellent: float = 3.0
    strong_weekly_slope: float = 0.03
    fi_scale_lookback: int = 20
    score_reward_risk_weight: float = 0.40
    score_impulse_weight: float = 0.25
    score_tide_weight: float = 0.20
    score_pullback_weight: float = 0.15
