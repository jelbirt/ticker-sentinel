"""EDGAR companyfacts normalization: tags, period typing, quarterly derivation.

Offline only: every test reads the committed companyfacts-shaped fixture.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from pytest import approx

from sentinel.data.edgar import (
    Fact,
    canonical_from_companyfacts,
    classify_period,
    facts_for_field,
    quarterly_values,
)

FIXTURE = Path(__file__).parent / "fixtures" / "companyfacts_sample.json"

Q1 = pd.Timestamp("2024-04-30")
Q2 = pd.Timestamp("2024-07-31")
Q3 = pd.Timestamp("2024-10-31")
Q4 = pd.Timestamp("2025-01-31")


@pytest.fixture(scope="module")
def payload() -> dict:
    return json.loads(FIXTURE.read_text())


@pytest.fixture(scope="module")
def frame(payload) -> pd.DataFrame:
    return canonical_from_companyfacts(payload)


def quarters(payload, field: str) -> dict[pd.Timestamp, float]:
    from sentinel.data.edgar import NON_ADDITIVE_FIELDS

    return quarterly_values(
        facts_for_field(payload, field), additive=field not in NON_ADDITIVE_FIELDS
    )


class TestClassifyPeriod:
    def test_instant_when_no_start(self):
        assert classify_period(None, "2024-04-30") == "instant"

    def test_three_months_is_quarterly(self):
        assert classify_period("2024-02-01", "2024-04-30") == "quarterly"

    def test_fourteen_week_quarter_still_quarterly(self):
        assert classify_period("2024-02-01", "2024-05-08") == "quarterly"

    def test_six_and_nine_months_are_ytd(self):
        assert classify_period("2024-02-01", "2024-07-31") == "ytd"
        assert classify_period("2024-02-01", "2024-10-31") == "ytd"

    def test_fiscal_year_is_annual(self):
        assert classify_period("2024-02-01", "2025-01-31") == "annual"


class TestTagNormalization:
    def test_first_matching_tag_wins(self, payload):
        """`Revenues` is present, so `SalesRevenueNet` is never consulted."""
        assert quarters(payload, "revenue")[Q1] == approx(100000000)

    def test_falls_through_to_a_later_alias(self, payload):
        """d_and_a's first alias is absent; the second one carries the series."""
        assert quarters(payload, "d_and_a")[Q1] == approx(2000000)

    def test_missing_tag_yields_no_facts(self, payload):
        payload = {"facts": {"us-gaap": {}}}
        assert facts_for_field(payload, "revenue") == []

    def test_non_periodic_forms_are_ignored(self, payload):
        """An 8-K restating Q1 revenue to 1 must not win on filed date."""
        assert all(f.form != "8-K" for f in facts_for_field(payload, "revenue"))
        assert quarters(payload, "revenue")[Q1] == approx(100000000)

    def test_share_facts_read_the_shares_unit(self, payload):
        assert quarters(payload, "diluted_shares")[Q1] == approx(240000000)


class TestQuarterlyDerivation:
    def test_direct_quarterly_income_facts(self, payload):
        rev = quarters(payload, "revenue")
        assert (rev[Q1], rev[Q2], rev[Q3]) == approx((100000000, 110000000, 120000000))

    def test_q4_from_fiscal_year_minus_three_quarters(self, payload):
        """Revenue files no YTD Q3, so Q4 = FY - (Q1 + Q2 + Q3)."""
        assert quarters(payload, "revenue")[Q4] == approx(130000000)

    def test_q4_negative_operating_income_derives(self, payload):
        assert quarters(payload, "operating_income")[Q4] == approx(3000000)

    def test_ytd_q1_is_the_quarter_itself(self, payload):
        assert quarters(payload, "ocf")[Q1] == approx(40000000)

    def test_mid_year_ytd_differencing(self, payload):
        ocf = quarters(payload, "ocf")
        assert ocf[Q2] == approx(55000000)   # 95 - 40
        assert ocf[Q3] == approx(65000000)   # 160 - 95

    def test_q4_from_fiscal_year_minus_ytd_q3(self, payload):
        assert quarters(payload, "ocf")[Q4] == approx(80000000)  # 240 - 160

    def test_missing_intermediate_ytd_leaves_gap(self, payload):
        """capex has no 6-month YTD: Q2 and Q3 are underivable, Q4 still is."""
        capex = quarters(payload, "capex")
        assert Q2 not in capex
        assert Q3 not in capex
        assert capex[Q1] == approx(5000000)
        assert capex[Q4] == approx(8000000)  # 26 - 18

    def test_restatement_latest_filed_wins(self, payload):
        """Q1 SBC was 20M in the 10-Q and 22M in the later amendment."""
        assert quarters(payload, "sbc")[Q1] == approx(22000000)

    def test_weighted_averages_are_never_derived(self, payload):
        """Diluted shares is a mean: no differencing, no FY-minus-sum."""
        shares = quarters(payload, "diluted_shares")
        assert shares[Q3] == approx(242000000)
        assert Q4 not in shares

    def test_additive_flag_stops_after_direct_facts(self, payload):
        facts = facts_for_field(payload, "ocf")
        assert quarterly_values(facts, additive=False) == {Q1: approx(40000000)}

    def test_empty_facts_yield_empty_series(self):
        assert quarterly_values([]) == {}

    def test_instant_facts_never_become_quarters(self):
        fact = Fact(
            start=None,
            end=Q1,
            value=5.0,
            filed=pd.Timestamp("2024-06-05"),
            form="10-Q",
            period_type="instant",
        )
        assert quarterly_values([fact]) == {}


class TestCanonicalFrame:
    def test_columns_are_quarter_ends_newest_first(self, frame):
        assert list(frame.columns) == [Q4, Q3, Q2, Q1]

    def test_rows_match_the_cache_schema(self, frame):
        from sentinel.data.fundamentals import CANONICAL_FIELDS

        assert list(frame.index) == CANONICAL_FIELDS

    def test_unmapped_fields_stay_nan(self, frame):
        for field in ("ebitda", "total_debt", "cash"):
            assert frame.loc[field].isna().all()

    def test_capex_is_a_positive_magnitude(self, frame):
        assert frame.loc["capex", Q1] == approx(5000000)
        assert (frame.loc["capex"].dropna() > 0).all()

    def test_gap_quarters_are_nan_not_missing_columns(self, frame):
        assert pd.isna(frame.loc["capex", Q2])
        assert frame.loc["ocf", Q2] == approx(55000000)

    def test_empty_payload_yields_an_empty_frame(self):
        empty = canonical_from_companyfacts({"facts": {"us-gaap": {}}})
        assert empty.shape[1] == 0
