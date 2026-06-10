"""Risk module tests: 2% Iron Triangle + 6% monthly guard."""

from __future__ import annotations

import pytest

from risk.sizing import position_size, six_percent_guard


def test_basic_iron_triangle():
    ps = position_size(equity=10_000, entry=100.0, stop=95.0, risk_pct=0.01)
    assert ps.size == 20
    assert ps.risk_budget == pytest.approx(100.0)
    assert ps.risk_at_size == pytest.approx(100.0)
    assert ps.risk_per_unit == pytest.approx(5.0)
    assert ps.capped is False


def test_size_floors_to_asset_precision():
    ps = position_size(equity=10_000, entry=100.0, stop=97.0, risk_pct=0.01, sz_decimals=2)
    assert ps.size == pytest.approx(33.33)  # floor(33.333... * 100) / 100
    assert ps.risk_at_size <= ps.risk_budget


def test_hard_cap_never_silently_exceeded():
    ps = position_size(equity=10_000, entry=100.0, stop=95.0, risk_pct=0.05)
    assert ps.capped is True
    assert ps.risk_pct_used == 0.02
    assert ps.size == 40  # 2% of 10k = $200 budget, $5/unit
    assert ps.risk_budget == pytest.approx(200.0)


def test_short_side_sizing_uses_abs_distance():
    ps = position_size(equity=10_000, entry=95.0, stop=100.0, risk_pct=0.01)
    assert ps.size == 20


def test_invalid_inputs_raise():
    with pytest.raises(ValueError):
        position_size(equity=0, entry=100, stop=95)
    with pytest.raises(ValueError):
        position_size(equity=10_000, entry=100, stop=100)
    with pytest.raises(ValueError):
        position_size(equity=10_000, entry=100, stop=95, risk_pct=0)
    with pytest.raises(ValueError):
        position_size(equity=10_000, entry=-1, stop=95)


def test_six_percent_guard_trips():
    g = six_percent_guard(
        equity_at_month_start=10_000, month_realized_losses=400, open_trade_risk=250
    )
    assert g.blocked is True
    assert g.total_at_risk == pytest.approx(650.0)
    assert g.limit == pytest.approx(600.0)


def test_six_percent_guard_boundary_and_clear():
    assert six_percent_guard(10_000, 350, 250).blocked is True  # exactly 6% blocks
    assert six_percent_guard(10_000, 300, 250).blocked is False
    assert six_percent_guard(10_000, 0, 0).blocked is False
