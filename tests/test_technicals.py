"""Section 5 technical overlay — hand-computed values on constructed series."""
from __future__ import annotations

import numpy as np
import pandas as pd
from pytest import approx

from sentinel.indicators import technicals as t


def _series(values) -> pd.Series:
    return pd.Series(values, index=pd.bdate_range(end="2026-07-02", periods=len(values)), dtype="float64")


class TestSmaAndTrend:
    def test_sma_last_value(self):
        s = _series(range(1, 61))  # 1..60
        assert t.sma(s, 50).iloc[-1] == approx(sum(range(11, 61)) / 50)  # mean of 11..60 = 35.5

    def test_sma_insufficient_is_nan(self):
        assert pd.isna(t.sma(_series(range(10)), 50).iloc[-1])

    def test_trend_states(self):
        assert t.trend_state(110, 105, 100) == "uptrend"
        assert t.trend_state(90, 105, 100) == "downtrend"
        assert t.trend_state(102, 105, 100) == "mixed"
        assert t.trend_state(None, 105, 100) is None
        assert t.trend_state(110, None, 100) is None


class TestCrosses:
    def test_golden_cross_recent(self):
        fast = _series([98, 99, 99.5, 100.5, 101])   # crosses above...
        slow = _series([100, 100, 100, 100, 100])    # ...a flat slow line
        assert t.recent_cross(fast, slow, lookback=10) == "golden"

    def test_death_cross_recent(self):
        fast = _series([102, 101, 100.5, 99.5, 99])
        slow = _series([100, 100, 100, 100, 100])
        assert t.recent_cross(fast, slow, lookback=10) == "death"

    def test_old_cross_outside_lookback_ignored(self):
        # cross happens at bar 2, then 15 bars of no crossing
        fast = _series([98, 99, 101] + [102] * 15)
        slow = _series([100] * 18)
        assert t.recent_cross(fast, slow, lookback=10) is None

    def test_no_cross(self):
        fast = _series([101, 102, 103, 104])
        slow = _series([100, 100, 100, 100])
        assert t.recent_cross(fast, slow) is None


class TestRsi:
    def test_all_gains_is_100(self):
        assert t.rsi14(_series(range(1, 40))) == approx(100.0)

    def test_all_losses_is_0(self):
        assert t.rsi14(_series(range(40, 1, -1))) == approx(0.0)

    def test_alternating_moves_oscillate_near_50(self):
        # Wilder smoothing biases toward the most recent bar's direction (~±5 at alpha=1/14)
        vals = [100 + (i % 2) for i in range(40)]
        assert t.rsi14(_series(vals)) == approx(50.0, abs=6.0)

    def test_insufficient_history(self):
        assert t.rsi14(_series(range(10))) is None


class TestRatios:
    def test_rel_strength_3m(self):
        p = _series([100.0] * 1 + [100.0] * 63 + [110.0])   # +10% over the window
        b = _series([100.0] * 1 + [100.0] * 63 + [105.0])   # +5%
        assert t.rel_strength_3m(p, b) == approx(1.10 / 1.05 - 1)

    def test_rel_strength_needs_benchmark(self):
        assert t.rel_strength_3m(_series(range(100)), None) is None

    def test_dist_52w_high_at_high_is_zero(self):
        s = _series(list(range(100, 400)))
        assert t.dist_52w_high(s) == approx(0.0)

    def test_dist_52w_high_below(self):
        s = _series([100.0] * 250 + [80.0])
        assert t.dist_52w_high(s) == approx(-0.20)

    def test_dist_52w_low(self):
        s = _series([100.0] * 250 + [80.0])
        assert t.dist_52w_low(s) == approx(0.0)

    def test_vol_ratio(self):
        v = _series([1_000_000.0] * 80 + [2_500_000.0] * 20)
        # slow window: (80×1.0 + 20×2.5)/100 = 1.3M; fast: 2.5M → 1.923×
        assert t.vol_ratio(v) == approx(2.5 / 1.3, abs=1e-3)

    def test_vol_ratio_insufficient(self):
        assert t.vol_ratio(_series([1e6] * 50)) is None


class TestComputeTechnicals:
    def test_empty_series_is_none(self):
        assert t.compute_technicals(pd.Series(dtype="float64")) is None
        assert t.compute_technicals(None) is None

    def test_synthetic_fixture_states(self):
        from sentinel.data.fixtures import synthetic_prices

        close, volume = synthetic_prices()
        bench = close["SPY"]
        alfa = t.compute_technicals(close["ALFA"], volume["ALFA"], bench)
        chrl = t.compute_technicals(close["CHRL"], volume["CHRL"], bench)
        assert alfa.trend_state == "uptrend"
        assert alfa.dist_52w_high == approx(0.0)          # linear riser is always at its high
        assert alfa.rel_strength_3m > 0
        assert chrl.trend_state == "downtrend"
        assert chrl.rel_strength_3m < 0
        assert chrl.vol_ratio > 1.5                        # engineered volume spike
