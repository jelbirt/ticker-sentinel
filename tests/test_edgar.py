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
    BACKFILL_FIELDS,
    Fact,
    canonical_from_companyfacts,
    classify_period,
    composite_values,
    facts_for_field,
    quarterly_values,
)

FIXTURE = Path(__file__).parent / "fixtures" / "companyfacts_sample.json"

Q1 = pd.Timestamp("2024-04-30")
Q2 = pd.Timestamp("2024-07-31")
Q3 = pd.Timestamp("2024-10-31")
Q4 = pd.Timestamp("2025-01-31")

PPE = "PaymentsToAcquirePropertyPlantAndEquipment"
PRODUCTIVE = "PaymentsToAcquireProductiveAssets"
DEVELOP_SW = "PaymentsToDevelopSoftware"
INTERNAL_SW = "PaymentsToCapitalizeInternalUseSoftware"
INTANGIBLES = "PaymentsToAcquireIntangibleAssets"

# fiscal quarter starts matching Q1..Q4 above
_STARTS = {
    Q1: "2024-02-01",
    Q2: "2024-05-01",
    Q3: "2024-08-01",
    Q4: "2024-11-01",
}


def fact(end: pd.Timestamp, value: float, start: str | None = None) -> dict:
    """One direct 3-month 10-Q entry, unless `start` widens it to a YTD period."""
    return {
        "start": start or _STARTS[end],
        "end": end.date().isoformat(),
        "val": value,
        "form": "10-Q",
        "filed": end.date().isoformat(),
    }


def tag_payload(tags: dict[str, list[dict]]) -> dict:
    return {
        "facts": {
            "us-gaap": {tag: {"units": {"USD": entries}} for tag, entries in tags.items()}
        }
    }


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


class TestNarrowedFieldSet:
    """Amendment 1 change 1: only the fields the history actually feeds."""

    def test_backfill_fields_are_the_r40_trend_inputs(self):
        assert BACKFILL_FIELDS == ["revenue", "ocf", "capex", "sbc", "diluted_shares"]

    def test_operating_income_and_d_and_a_are_never_derived(self, frame):
        """Both tags are present in the fixture; the frame still leaves them NaN."""
        for field in ("operating_income", "d_and_a"):
            assert frame.loc[field].isna().all()

    def test_their_tags_stay_mapped_for_inspection(self, payload):
        """TAG_MAP keeps the entries as documentation, nothing derives them."""
        assert facts_for_field(payload, "operating_income")
        assert facts_for_field(payload, "d_and_a")


class TestCompositeCapex:
    """Amendment 1 change 2: capex = base tag plus software capitalization."""

    def test_components_are_summed_per_quarter(self):
        values = composite_values(
            tag_payload(
                {
                    PPE: [fact(Q1, 5_000_000), fact(Q2, 6_000_000)],
                    DEVELOP_SW: [fact(Q1, 3_000_000), fact(Q2, 3_500_000)],
                    INTERNAL_SW: [fact(Q1, 1_000_000), fact(Q2, 1_000_000)],
                    INTANGIBLES: [fact(Q1, 500_000), fact(Q2, 500_000)],
                }
            ),
            "capex",
        )
        assert values[Q1] == approx(9_500_000)
        assert values[Q2] == approx(11_000_000)

    def test_base_only_filer_is_unchanged(self):
        values = composite_values(tag_payload({PPE: [fact(Q1, 5_000_000)]}), "capex")
        assert values == {Q1: approx(5_000_000)}

    def test_a_quarter_missing_from_the_base_is_absent(self):
        """No base, no quarter: the addend alone is not a capex number."""
        values = composite_values(
            tag_payload(
                {
                    PPE: [fact(Q1, 5_000_000)],
                    DEVELOP_SW: [fact(Q1, 3_000_000), fact(Q2, 3_500_000)],
                }
            ),
            "capex",
        )
        assert Q2 not in values
        assert values[Q1] == approx(8_000_000)

    def test_an_unfiled_addend_quarter_counts_as_zero(self):
        """A quarter that capitalized no software simply omits the tag."""
        values = composite_values(
            tag_payload(
                {
                    PPE: [fact(Q1, 5_000_000), fact(Q2, 6_000_000)],
                    DEVELOP_SW: [fact(Q1, 3_000_000)],
                }
            ),
            "capex",
        )
        assert values[Q1] == approx(8_000_000)
        assert values[Q2] == approx(6_000_000)

    def test_a_filed_but_underivable_addend_quarter_drops_the_quarter(self):
        """A 6-month YTD with no Q1 point to difference: unknown, not zero."""
        values = composite_values(
            tag_payload(
                {
                    PPE: [fact(Q1, 5_000_000), fact(Q2, 6_000_000)],
                    DEVELOP_SW: [fact(Q2, 7_000_000, start=_STARTS[Q1])],
                }
            ),
            "capex",
        )
        assert Q2 not in values
        assert values[Q1] == approx(5_000_000)

    def test_productive_assets_is_an_alternative_base_not_an_addend(self):
        """Both tags filed: PP&E wins and ProductiveAssets is not added on top."""
        values = composite_values(
            tag_payload(
                {
                    PPE: [fact(Q1, 5_000_000)],
                    PRODUCTIVE: [fact(Q1, 7_000_000)],
                }
            ),
            "capex",
        )
        assert values[Q1] == approx(5_000_000)

    def test_productive_assets_carries_the_base_when_ppe_is_absent(self):
        values = composite_values(
            tag_payload(
                {
                    PRODUCTIVE: [fact(Q1, 7_000_000)],
                    DEVELOP_SW: [fact(Q1, 1_000_000)],
                }
            ),
            "capex",
        )
        assert values[Q1] == approx(8_000_000)

    def test_no_base_tag_at_all_yields_nothing(self):
        assert composite_values(tag_payload({DEVELOP_SW: [fact(Q1, 1_000_000)]}), "capex") == {}

    def test_components_are_derived_before_summing(self):
        """PP&E files YTD, software files per quarter: differencing comes first."""
        values = composite_values(
            tag_payload(
                {
                    PPE: [
                        fact(Q1, 5_000_000),
                        fact(Q2, 11_000_000, start=_STARTS[Q1]),
                    ],
                    DEVELOP_SW: [fact(Q1, 1_000_000), fact(Q2, 2_000_000)],
                }
            ),
            "capex",
        )
        assert values[Q1] == approx(6_000_000)
        assert values[Q2] == approx(8_000_000)  # (11 - 5) + 2

    def test_the_composite_reaches_the_canonical_frame(self):
        frame = canonical_from_companyfacts(
            tag_payload(
                {
                    PPE: [fact(Q1, 5_000_000)],
                    INTERNAL_SW: [fact(Q1, 4_000_000)],
                }
            )
        )
        assert frame.loc["capex", Q1] == approx(9_000_000)


class TestTagShadowing:
    """Verification-pass regression (live case: PANW). A tag can carry facts
    yet derive zero quarters (annual-only "Revenues" shells); precedence must
    sit on derived quarters or the shell tag shadows the tag with the real
    quarterly coverage and the field silently vanishes from the frame."""

    def _annual_shell(self, value: float) -> dict:
        return {
            "start": "2024-02-01", "end": "2025-01-31", "val": value,
            "form": "10-K", "filed": "2025-03-15",
        }

    def _payload(self):
        return tag_payload({
            "Revenues": [self._annual_shell(8_000.0)],
            "RevenueFromContractWithCustomerExcludingAssessedTax": [
                fact(Q1, 2_000.0), fact(Q2, 2_100.0),
            ],
        })

    def test_field_values_skips_shell_tag(self):
        from sentinel.data.edgar import field_values

        values = field_values(self._payload(), "revenue")
        assert values == {Q1: 2_000.0, Q2: 2_100.0}

    def test_canonical_frame_carries_the_field(self):
        frame = canonical_from_companyfacts(self._payload())
        assert pd.notna(frame.loc["revenue", Q2])

    def test_composite_base_skips_shell_tag(self):
        payload = tag_payload({
            PPE: [self._annual_shell(400.0)],
            PRODUCTIVE: [fact(Q1, 90.0)],
        })
        assert composite_values(payload, "capex") == {Q1: 90.0}
