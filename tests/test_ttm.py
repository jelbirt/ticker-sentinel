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


class TestPartialQuarterAnchoring:
    """A leading partially-populated quarter (Yahoo publishing statements
    piecemeal after earnings) must shift the TTM anchor back with a note,
    never unscore the ticker (the live TEAM case, 2026-08-14). The skip is
    bounded so a drifted field alias cannot silently re-anchor scoring on
    arbitrarily old quarters."""

    FULL = {
        "revenue": [260.0, 250, 240, 230, 200, 190, 180],
        "ocf": [90.0, 85, 80, 75, 70, 65, 60],
        "capex": [10.0, 10, 10, 10, 10, 10, 10],
        "diluted_shares": [100.0] * 7,
    }

    def _inputs(self, fields, notes=None):
        from sentinel.data.fundamentals import inputs_from_canonical

        return inputs_from_canonical("X", make_canonical(fields), market_cap=None, notes=notes)

    def _with_partial_leads(self, n: int, missing=("revenue", "ocf", "capex")):
        fields = {k: list(v) for k, v in self.FULL.items()}
        for f in missing:
            for i in range(n):
                fields[f][i] = np.nan
        return fields

    def test_complete_newest_quarter_anchors_at_zero_no_note(self):
        notes: list[str] = []
        inp = self._inputs(self.FULL, notes)
        assert inp.ttm_now.revenue == approx(980)
        assert notes == []

    def test_shares_only_lead_column_scores_as_of_prior_quarter(self):
        notes: list[str] = []
        inp = self._inputs(self._with_partial_leads(1), notes)
        assert inp.ttm_now is not None
        assert inp.ttm_now.revenue == approx(250 + 240 + 230 + 200)
        assert inp.ttm_now.ocf == approx(85 + 80 + 75 + 70)
        assert len(notes) == 1
        assert "scored as of" in notes[0] and "quarter " in notes[0]

    def test_cashflow_lagging_income_statement_also_anchors_back(self):
        notes: list[str] = []
        inp = self._inputs(self._with_partial_leads(1, missing=("ocf", "capex")), notes)
        assert inp.ttm_now.revenue == approx(250 + 240 + 230 + 200)
        assert len(notes) == 1

    def test_two_partial_leads_still_anchor(self):
        notes: list[str] = []
        inp = self._inputs(self._with_partial_leads(2), notes)
        assert inp.ttm_now.revenue == approx(240 + 230 + 200 + 190)
        assert len(notes) == 1
        assert "quarters" in notes[0]  # plural wording for a multi-column skip

    def test_three_partial_leads_exceed_the_bound_and_degrade(self):
        # a persistently missing core field (alias drift) must surface as
        # degraded data, not silently re-anchor on ever-older quarters
        notes: list[str] = []
        inp = self._inputs(self._with_partial_leads(3), notes)
        assert notes == []
        assert inp.ttm_now.revenue is None

    def test_no_complete_column_degrades_unchanged(self):
        fields = {"revenue": [260.0, 250, 240, 230], "diluted_shares": [100.0] * 4}
        notes: list[str] = []
        inp = self._inputs(fields, notes)
        assert notes == []
        assert inp.ttm_now.revenue == approx(980)
        assert inp.ttm_now.ocf is None


class TestQuarterContiguity:
    """A missing middle quarter must not let a 4-column window silently sum
    15+ months as a "TTM"."""

    def _frame_with_gap(self):
        import pandas as pd

        from sentinel.data.fundamentals import CANONICAL_FIELDS

        # 2025-12-31 missing: newest 4 columns span 15 months
        cols = pd.to_datetime(
            ["2026-06-30", "2026-03-31", "2025-09-30", "2025-06-30", "2025-03-31"]
        )
        df = pd.DataFrame(100.0, index=CANONICAL_FIELDS, columns=cols)
        return df

    def test_gap_spanning_window_is_none(self):
        assert build_ttm(self._frame_with_gap(), 0) is None

    def test_contiguous_window_behind_the_gap_still_builds(self):
        # offset 1 = [2026-03-31 .. 2025-03-31] minus one... still has the gap
        # between 2026-03-31 and 2025-09-30, so it is None too; a fully
        # contiguous synthetic frame builds fine (regression guard)
        assert build_ttm(self._frame_with_gap(), 1) is None
        df = make_canonical({"revenue": [260.0, 250, 240, 230]})
        assert build_ttm(df, 0).revenue == approx(980)

    def test_gap_note_emitted(self):
        from sentinel.data.fundamentals import inputs_from_canonical

        notes: list[str] = []
        inputs_from_canonical("X", self._frame_with_gap(), market_cap=None, notes=notes)
        assert any("gap in quarterly statement history" in n for n in notes)

    def test_13_week_retail_calendar_is_not_a_gap(self):
        import pandas as pd

        from sentinel.data.fundamentals import CANONICAL_FIELDS

        # 4-4-5 style quarter ends: spacing 91-98 days
        cols = pd.to_datetime(["2026-05-02", "2026-01-31", "2025-11-01", "2025-08-02"])
        df = pd.DataFrame(100.0, index=CANONICAL_FIELDS, columns=cols)
        assert build_ttm(df, 0).revenue == approx(400)
