"""compute_scorecard end-to-end on the three fixture profiles + degradation paths.

Expected values are hand-computed from the fixture JSON quarters (see each assert).
"""
from __future__ import annotations

from datetime import date

from pytest import approx

from sentinel.indicators import fundamentals as f
from sentinel.indicators.fundamentals import (
    FLAG_DILUTION,
    FLAG_GROWTH_FROM_ANNUAL,
    FLAG_HIGH_SBC,
    FLAG_INSUFFICIENT_DATA,
    FLAG_INSUFFICIENT_HISTORY,
    FLAG_SBC_INFLATED,
    FLAG_STALE,
    FLAG_ALL_R40,
    FundamentalInputs,
    TTMWindow,
    compute_scorecard,
)
from tests.conftest import FIXED_TODAY


class TestAlfaStrongGrower:
    def test_metrics(self, fixture_scorecards):
        sc = fixture_scorecards["ALFA"]
        assert sc.growth == approx(980 / 740 - 1)          # 32.4%
        assert sc.growth_source == "ttm"
        assert sc.fcf_margin == approx(290 / 980)
        assert sc.ebitda_margin == approx(250 / 980)
        assert sc.op_margin == approx(148 / 980)
        assert sc.fcf_margin_ex_sbc == approx(210 / 980)
        assert sc.r40_fcf == approx(980 / 740 - 1 + 290 / 980)      # 62.0 pts
        assert sc.r40_ebitda == approx(980 / 740 - 1 + 250 / 980)   # 57.9 pts
        assert sc.r40_sbc_adj == approx(980 / 740 - 1 + 210 / 980)  # 53.9 pts
        assert sc.rule_of_x == approx(2 * (980 / 740 - 1) + 290 / 980)

    def test_trend_and_sparkline_point(self, fixture_scorecards):
        sc = fixture_scorecards["ALFA"]
        r40_now = 980 / 740 - 1 + 290 / 980
        r40_m4 = 740 / 580 - 1 + 210 / 740
        assert sc.r40_trend == approx(r40_now - r40_m4)  # +6.1 pts
        assert sc.r40_fcf_2q == approx(860 / 660 - 1 + 250 / 860)

    def test_guards(self, fixture_scorecards):
        sc = fixture_scorecards["ALFA"]
        assert sc.dilution == approx(0.029)         # below the 3% limit: no flag
        assert sc.sbc_intensity == approx(80 / 980)
        assert sc.ev_revenue == approx(11000 / 980)
        assert sc.fcf_yield == approx(290 / 12000)
        assert FLAG_DILUTION not in sc.flags
        assert FLAG_HIGH_SBC not in sc.flags
        assert FLAG_SBC_INFLATED not in sc.flags
        assert FLAG_ALL_R40 in sc.flags
        assert not sc.stale


class TestBrvoSbcHeavy:
    def test_metrics(self, fixture_scorecards):
        sc = fixture_scorecards["BRVO"]
        assert sc.growth == approx(0.25)
        assert sc.r40_fcf == approx(0.35)
        assert sc.r40_ebitda == approx(0.325)
        assert sc.r40_sbc_adj == approx(0.25 - 44 / 400)  # 14 pts
        assert sc.rule_of_x == approx(0.60)
        assert sc.r40_trend == approx(0.35 - (320 / 280 - 1 + 28 / 320))

    def test_penalty_flags(self, fixture_scorecards):
        sc = fixture_scorecards["BRVO"]
        assert sc.dilution == approx(0.10)
        assert sc.sbc_intensity == approx(0.21)
        assert FLAG_DILUTION in sc.flags
        assert FLAG_HIGH_SBC in sc.flags
        assert FLAG_SBC_INFLATED in sc.flags  # 35 − 14 = 21 pts gap > 20
        assert FLAG_ALL_R40 not in sc.flags


class TestChrlDeteriorating:
    def test_metrics(self, fixture_scorecards):
        sc = fixture_scorecards["CHRL"]
        assert sc.growth == approx(212 / 252 - 1)  # −15.9%
        assert sc.fcf_margin == approx(-8 / 212)
        assert sc.r40_fcf == approx(212 / 252 - 1 - 8 / 212)   # −19.6 pts
        assert sc.r40_trend == approx((212 / 252 - 1 - 8 / 212) - (252 / 292 - 1 + 0))
        assert sc.dilution == approx(90 / 95 - 1)  # buyback, no flag
        assert FLAG_DILUTION not in sc.flags
        assert sc.fcf_yield == approx(-8 / 300)


class TestDegradationPaths:
    def test_no_ttm_at_all(self):
        sc = compute_scorecard(FundamentalInputs(ticker="X"), today=FIXED_TODAY)
        assert FLAG_INSUFFICIENT_DATA in sc.flags
        assert sc.r40_fcf is None

    def test_annual_growth_fallback(self):
        inp = FundamentalInputs(
            ticker="X",
            ttm_now=TTMWindow(revenue=980, ocf=330, capex=40, sbc=80, ebitda=250),
            annual_revenue={date(2026, 1, 31): 120.0, date(2025, 1, 31): 100.0},
            statement_date=date(2026, 4, 30),
        )
        sc = compute_scorecard(inp, today=FIXED_TODAY)
        assert sc.growth == approx(0.20)
        assert sc.growth_source == "annual"
        assert FLAG_GROWTH_FROM_ANNUAL in sc.flags
        assert sc.r40_fcf == approx(0.20 + 290 / 980)
        assert sc.r40_trend is None
        assert FLAG_INSUFFICIENT_HISTORY in sc.flags

    def test_no_growth_source_at_all(self):
        inp = FundamentalInputs(
            ticker="X", ttm_now=TTMWindow(revenue=980, ocf=330, capex=40)
        )
        sc = compute_scorecard(inp, today=FIXED_TODAY)
        assert sc.growth is None
        assert sc.r40_fcf is None
        assert sc.fcf_margin == approx(290 / 980)  # margins still reported
        assert FLAG_INSUFFICIENT_HISTORY in sc.flags

    def test_missing_sbc_degrades_sbc_metrics_only(self):
        inp = FundamentalInputs(
            ticker="X",
            ttm_now=TTMWindow(revenue=980, ocf=330, capex=40, ebitda=250),
            ttm_minus_4q=TTMWindow(revenue=740),
            statement_date=date(2026, 4, 30),
        )
        sc = compute_scorecard(inp, today=FIXED_TODAY)
        assert sc.r40_fcf is not None
        assert sc.r40_sbc_adj is None
        assert sc.sbc_intensity is None
        assert FLAG_SBC_INFLATED not in sc.flags
        assert FLAG_ALL_R40 not in sc.flags  # can't claim all 3 with one unknown

    def test_stale_statements_flagged(self):
        inp = FundamentalInputs(
            ticker="X",
            ttm_now=TTMWindow(revenue=980, ocf=330, capex=40),
            ttm_minus_4q=TTMWindow(revenue=740),
            statement_date=date(2025, 6, 30),  # 370 days before FIXED_TODAY
        )
        sc = compute_scorecard(inp, today=FIXED_TODAY)
        assert sc.stale
        assert FLAG_STALE in sc.flags
