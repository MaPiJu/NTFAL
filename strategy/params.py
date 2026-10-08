"""Configurable knobs for the Elder strategy layer.

Defaults mirror the module constants used by the original implementation; callers
can override them from config.toml without changing code.

Every lookback is a count of **bars on the relevant screen's timeframe**, never a
wall-clock duration — that is what lets the same Triple Screen run on a weekly
tide or a 4h one (see config.toml's `[[scanner.horizons]]`).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyParams:
    flat_trend_slope_pct: float = 0.001
    penetration_lookback_bars: int = 35
    # Elder's 2nd-screen caveat: void the Force Index signal when FI(2) prints a
    # new multi-period extreme (accelerating move, not a pullback). ~3-4 weeks
    # of wave bars on the canonical daily wave.
    force_index_extreme_lookback_bars: int = 25
    # Elder fits the channel so it contains ~95% of the past 100 bars (p.79, p.167).
    channel_lookback_bars: int = 100
    channel_containment: float = 0.95
    # Elder's 2:1 floor: flags sub-2:1 setups and earns full marks on the
    # Trade Apgar's reward:risk line.
    min_reward_risk: float = 2.0
    divergence_lookback: int = 60
    # Elder/Lovvorn (p.88): the most tradable divergences span 20-40 bars.
    divergence_min_separation: int = 20
    divergence_max_separation: int = 40
    safezone_lookback_bars: int = 20
    # Elder (p.220): shorts need wider stops (>=3) than longs (>=2), since
    # shorting near the highs is noisier and downtrends move faster.
    safezone_factor_long: float = 2.0
    safezone_factor_short: float = 3.0
    # Data-quality gate, not a trading indicator: warn when the most recent
    # `low_volume_bars` wave bars average below this fraction of the recent
    # median volume (a tradfi perp over the weekend is a frozen oracle).
    low_volume_ratio: float = 0.25
    low_volume_bars: int = 6
    # Bars of wave volume whose median is "normal". Must span well over a week of
    # wave bars (1000 x 15m ~ 10 days), or a ~49h weekend becomes the baseline.
    low_volume_baseline_bars: int = 1000
