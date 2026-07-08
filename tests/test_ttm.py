"""TTM builder: 4-quarter windows, offsets, insufficient data, EBITDA fallback."""
from __future__ import annotations

import numpy as np
from pytest import approx

from sentinel.data.fundamentals import build_ttm
from tests.conftest import make_canonical


def test_ttm_now_and_offsets():
    df = make_canonical({"revenue": [260, 250, 240, 230, 200, 190, 180, 170]})
    assert build_ttm(df, 0).revenue == approx(980)
    assert build_ttm(df, 2).revenue == approx(240 + 230 + 200 + 190)
    assert build_ttm(df, 4).revenue == approx(740)


def test_fewer_than_four_quarters_is_none():
    df = make_canonical({"revenue": [260, 250, 240]})
    assert build_ttm(df, 0) is None


def test_offset_beyond_history_is_none():
    df = make_canonical({"revenue": [260, 250, 240, 230, 200]})
    assert build_ttm(df, 0) is not None
    assert build_ttm(df, 2) is None
    assert build_ttm(df, 4) is None


def test_missing_field_in_one_quarter_nulls_that_field_only():
    df = make_canonical(
        {"revenue": [260, 250, 240, 230], "sbc": [20, np.nan, 20, 20], "ocf": [90, 85, 80, 75]}
    )
    w = build_ttm(df, 0)
    assert w.revenue == approx(980)
    assert w.ocf == approx(330)
    assert w.sbc is None  # never annualize / partial-sum silently


def test_missing_sbc_row_entirely():
    df = make_canonical({"revenue": [260, 250, 240, 230]})
    w = build_ttm(df, 0)
    assert w.revenue == approx(980)
    assert w.sbc is None
    assert w.ocf is None


def test_ebitda_fallback_from_operating_income_plus_da():
    df = make_canonical(
        {
            "revenue": [260, 250, 240, 230],
            "operating_income": [40, 38, 36, 34],
            "d_and_a": [10, 10, 10, 10],
        }
    )
    w = build_ttm(df, 0)
    assert w.ebitda == approx(148 + 40)


def test_reported_ebitda_wins_over_fallback():
    df = make_canonical(
        {
            "revenue": [260, 250, 240, 230],
            "ebitda": [70, 65, 60, 55],
            "operating_income": [40, 38, 36, 34],
            "d_and_a": [10, 10, 10, 10],
        }
    )
    assert build_ttm(df, 0).ebitda == approx(250)


def test_none_frame():
    assert build_ttm(None, 0) is None


class TestPairedShares:
    """Dilution windows must always span a true year from one anchor quarter."""

    def _inputs(self, shares):
        from sentinel.data.fundamentals import inputs_from_canonical

        df = make_canonical({"revenue": [100.0] * len(shares), "diluted_shares": shares})
        return inputs_from_canonical("X", df, market_cap=None)

    def test_plain_case_uses_columns_0_and_4(self):
        inp = self._inputs([108, 106, 104, 102, 100, 99])
        assert inp.diluted_shares_now == approx(108)
        assert inp.diluted_shares_1y_ago == approx(100)

    def test_missing_newest_quarter_shifts_both_ends(self):
        # anchor moves to col 1; the prior must move to col 5 — never a
        # mixed window (col 1 vs col 4 would be only 3 quarters apart)
        inp = self._inputs([np.nan, 106, 104, 102, 100, 99])
        assert inp.diluted_shares_now == approx(106)
        assert inp.diluted_shares_1y_ago == approx(99)

    def test_prior_beyond_history_is_none(self):
        inp = self._inputs([108, 106, 104, 102])
        assert inp.diluted_shares_now == approx(108)
        assert inp.diluted_shares_1y_ago is None

    def test_all_nan_row(self):
        inp = self._inputs([np.nan, np.nan, np.nan, np.nan, np.nan])
        assert inp.diluted_shares_now is None
        assert inp.diluted_shares_1y_ago is None
