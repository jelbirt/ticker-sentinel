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
