"""Golden tests: hand-computed reference values + an independent recurrence."""

from __future__ import annotations

import pandas as pd
import pytest

from indicators import ema, force_index, impulse_color, macd_histogram


def ref_ema(values: list[float], span: int) -> list[float]:
    """Independent textbook recurrence: e_t = k*x_t + (1-k)*e_{t-1}, k = 2/(span+1)."""
    k = 2 / (span + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def test_ema_hand_computed_values():
    # span=3 -> k=0.5; values worked out by hand
    s = pd.Series([10.0, 11.0, 12.0, 11.0, 13.0])
    expected = [10.0, 10.5, 11.25, 11.125, 12.0625]
    assert ema(s, 3).tolist() == pytest.approx(expected)


def test_ema_rejects_bad_span():
    with pytest.raises(ValueError):
        ema(pd.Series([1.0]), 0)


def test_force_index_hand_computed_values():
    # raw force: [NaN, 110, 120, -130, 280]; EMA(2) with k=2/3 seeded at 110
    close = pd.Series([10.0, 11.0, 12.0, 11.0, 13.0])
    volume = pd.Series([100.0, 110.0, 120.0, 130.0, 140.0])
    fi = force_index(close, volume, span=2)
    assert pd.isna(fi.iloc[0])
    assert fi.iloc[1:].tolist() == pytest.approx([110.0, 116.6666667, -47.7777778, 170.7407407])


def test_macd_histogram_matches_independent_recurrence():
    closes = [10.0, 11.0, 13.0, 12.0, 14.0, 15.0, 14.0, 16.0, 18.0, 17.0, 19.0, 20.0]
    fast, slow, signal = 3, 6, 2
    macd_line = [f - s for f, s in zip(ref_ema(closes, fast), ref_ema(closes, slow), strict=True)]
    expected = [m - s for m, s in zip(macd_line, ref_ema(macd_line, signal), strict=True)]

    got = macd_histogram(pd.Series(closes), fast, slow, signal)
    assert got.tolist() == pytest.approx(expected)


def test_macd_histogram_default_params():
    closes = [100.0 + i + (3.0 if i % 7 == 0 else 0.0) for i in range(60)]
    macd_line = [f - s for f, s in zip(ref_ema(closes, 12), ref_ema(closes, 26), strict=True)]
    expected = [m - s for m, s in zip(macd_line, ref_ema(macd_line, 9), strict=True)]
    assert macd_histogram(pd.Series(closes)).tolist() == pytest.approx(expected)


def test_impulse_colors():
    # accelerating rise -> EMA13 and MACD-hist both rising -> green
    up = pd.Series([100.0 * 1.05**i for i in range(60)])
    assert impulse_color(up).iloc[-1] == "green"

    # accelerating fall -> both falling -> red
    down = pd.Series([100.0 * 0.95**i for i in range(60)])
    assert impulse_color(down).iloc[-1] == "red"

    # flat -> no slope either way -> blue (and the first bar is always blue)
    flat = pd.Series([100.0] * 60)
    assert (impulse_color(flat) == "blue").all()
    assert impulse_color(up).iloc[0] == "blue"


def test_impulse_mixed_is_blue():
    # long rise keeps EMA13 rising; one small dip turns the histogram down -> blue
    closes = [100.0 + 2 * i for i in range(50)]
    closes.append(closes[-1] - 1.0)
    s = pd.Series(closes)
    assert impulse_color(s).iloc[-1] == "blue"
