"""Alias layer: raw yfinance-shaped statement frames -> canonical frame."""
from __future__ import annotations

import numpy as np
import pandas as pd
from pytest import approx

from sentinel.data.cache import merge_statements
from sentinel.data.fundamentals import (
    inputs_from_canonical,
    normalize_statements,
    sanitize_share_counts,
)
from tests.conftest import make_canonical

DATES = pd.to_datetime(["2026-04-30", "2026-01-31", "2025-10-31", "2025-07-31"])


def _income():
    return pd.DataFrame(
        {
            d: {"Total Revenue": 100 + i, "Operating Income": 10 + i, "Diluted Average Shares": 50}
            for i, d in enumerate(DATES)
        }
    )


def _cashflow():
    return pd.DataFrame(
        {
            d: {
                "Operating Cash Flow": 20 + i,
                "Capital Expenditure": -5,  # yfinance reports outflows negative
                "Stock Based Compensation": 8,
                "Depreciation And Amortization": 4,
            }
            for i, d in enumerate(DATES)
        }
    )


def _balance():
    return pd.DataFrame({DATES[0]: {"Total Debt": 500, "Cash And Cash Equivalents": 1500}})


def test_normalize_maps_and_signs():
    out = normalize_statements(_income(), _cashflow(), _balance())
    assert list(out.columns) == sorted(DATES, reverse=True)
    assert out.loc["revenue", DATES[0]] == approx(100)
    assert out.loc["capex", DATES[0]] == approx(5)  # sign normalized to magnitude
    assert out.loc["sbc", DATES[2]] == approx(8)
    assert out.loc["total_debt", DATES[0]] == approx(500)
    assert np.isnan(out.loc["total_debt", DATES[1]])  # balance only has latest quarter
    assert out.loc["ebitda"].isna().all()  # not reported -> NaN row, TTM falls back


def test_alias_fallback_second_name_wins():
    income = _income().rename(index={"Total Revenue": "Operating Revenue"})
    out = normalize_statements(income, _cashflow(), _balance())
    assert out.loc["revenue", DATES[0]] == approx(100)


def test_missing_statement_leaves_nan():
    out = normalize_statements(_income(), None, None)
    assert out.loc["revenue", DATES[0]] == approx(100)
    assert out.loc["ocf"].isna().all()
    assert out.loc["total_debt"].isna().all()


def test_merge_accumulates_history_fresh_wins():
    old_dates = pd.to_datetime(["2025-10-31", "2025-07-31", "2025-04-30"])
    cached = pd.DataFrame({d: {"revenue": 90.0} for d in old_dates}).astype("float64")
    fresh = pd.DataFrame(
        {pd.Timestamp("2026-01-31"): {"revenue": 100.0}, pd.Timestamp("2025-10-31"): {"revenue": 95.0}}
    )
    merged = merge_statements(cached, fresh)
    assert list(merged.columns)[0] == pd.Timestamp("2026-01-31")  # newest first
    assert merged.shape[1] == 4  # union of quarters
    assert merged.loc["revenue", pd.Timestamp("2025-10-31")] == approx(95.0)  # fresh wins
    assert merged.loc["revenue", pd.Timestamp("2025-04-30")] == approx(90.0)  # history kept


class TestShareCountGuard:
    """diluted_shares drives dilution AND the repriced market cap, so a cell on
    the wrong basis corrupts ev_revenue, fcf_yield and the valuation label
    while looking entirely reasonable. Both live failure modes are covered:
    the units-for-thousands slip Yahoo served for NOW on 2026-08-07, and the
    pre-split tail a split leaves behind in the cache (CRWD 4:1, 2026-07-02)."""

    def _row(self, frame):
        return list(frame.loc["diluted_shares"])

    def test_healthy_row_untouched(self):
        # PANW shape: a stock-funded acquisition steps the count 13% in one
        # quarter, which is issuance, not a basis change
        df = make_canonical({"diluted_shares": [801e6, 711e6, 709e6, 709e6, 707.4e6]})
        notes = []
        out = sanitize_share_counts("PANW", df, 815e6, notes)
        assert self._row(out) == approx(self._row(df))
        assert notes == []

    def test_off_scale_cells_dropped(self):
        # the committed NOW cache on 2026-08-07: two quarters served in
        # thousands sitting between neighbours served in units
        df = make_canonical(
            {"diluted_shares": [1_034_334.0, 1_039_884_000.0, 1_047_000_000.0, 1_046_608.0]}
        )
        notes = []
        out = sanitize_share_counts("NOW", df, 1_033_862_000.0, notes)
        row = self._row(out)
        assert np.isnan(row[0]) and np.isnan(row[3])
        assert row[1] == approx(1_039_884_000.0)
        assert row[2] == approx(1_047_000_000.0)
        assert len(notes) == 1
        assert "implausible against 1,033,862,000 shares outstanding" in notes[0]

    def test_pre_split_tail_dropped_when_basis_mixes(self):
        # a 2:1 split: the quarters the source still serves are restated, the
        # cache-only tail is not, and the tail stays inside the magnitude band,
        # so only the neighbour check can catch it
        df = make_canonical(
            {"diluted_shares": [1000e6, 995e6, 990e6, 980e6, 487e6, 484e6]}
        )
        notes = []
        out = sanitize_share_counts("X", df, 1_002_000_000.0, notes)
        row = self._row(out)
        assert row[:4] == approx([1000e6, 995e6, 990e6, 980e6])
        assert np.isnan(row[4]) and np.isnan(row[5])
        assert len(notes) == 1
        assert "2 older quarters dropped as a different share basis" in notes[0]

    def test_deep_pre_split_tail_also_fails_the_magnitude_band(self):
        # CRWD 4:1 shape: a tail that far off is caught by either check, so the
        # guard does not depend on which one happens to fire first
        df = make_canonical(
            {"diluted_shares": [1031.5e6, 1032.5e6, 1005.3e6, 999.6e6, 248.4e6, 247.0e6]}
        )
        notes = []
        row = self._row(sanitize_share_counts("CRWD", df, 1_018_259_280.0, notes))
        assert row[:4] == approx([1031.5e6, 1032.5e6, 1005.3e6, 999.6e6])
        assert np.isnan(row[4]) and np.isnan(row[5])
        assert notes and notes[0].startswith("CRWD: diluted share count implausible")

    def test_step_check_runs_without_a_reference(self):
        # meta written before shares_outstanding existed: the neighbour check
        # still has to fire on its own
        df = make_canonical({"diluted_shares": [400.0, 398.0, 100.0, 99.0]})
        notes = []
        out = sanitize_share_counts("X", df, None, notes)
        row = self._row(out)
        assert row[:2] == approx([400.0, 398.0])
        assert np.isnan(row[2]) and np.isnan(row[3])
        assert len(notes) == 1

    def test_gaps_do_not_count_as_steps(self):
        # a missing quarter must not make its neighbours look like a break
        df = make_canonical({"diluted_shares": [104.0, np.nan, 102.0, 101.0]})
        notes = []
        out = sanitize_share_counts("X", df, 103.0, notes)
        assert self._row(out)[0] == approx(104.0)
        assert self._row(out)[2] == approx(102.0)
        assert notes == []

    def test_missing_row_and_empty_row_are_noops(self):
        no_row = make_canonical({"revenue": [100.0, 99.0]}).drop(index="diluted_shares")
        assert "diluted_shares" not in sanitize_share_counts("X", no_row, 1e6, []).index
        all_nan = make_canonical({"revenue": [100.0], "diluted_shares": [np.nan]})
        notes = []
        sanitize_share_counts("X", all_nan, 1e6, notes)
        assert notes == []

    def test_input_frame_is_not_mutated(self):
        df = make_canonical({"diluted_shares": [1_034_334.0, 1_039_884_000.0]})
        sanitize_share_counts("NOW", df, 1_033_862_000.0, [])
        assert df.loc["diluted_shares"].iloc[0] == approx(1_034_334.0)

    def test_dropped_tail_degrades_dilution_to_none(self):
        # the payoff: no share count beats a 300% dilution reading invented by
        # comparing a post-split quarter with a pre-split one
        shares = [1031.5e6, 1032.5e6, 1005.3e6, 999.6e6, 248.4e6, 247.0e6]
        df = make_canonical({"revenue": [100.0] * 6, "diluted_shares": shares})
        notes = []
        clean = sanitize_share_counts("CRWD", df, 1_018_259_280.0, notes)
        inp = inputs_from_canonical("CRWD", clean, market_cap=None, notes=notes)
        assert inp.diluted_shares_now == approx(1031.5e6)
        assert inp.diluted_shares_1y_ago is None
