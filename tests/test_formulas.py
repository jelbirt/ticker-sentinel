"""Section 5 formulas against hand-computed values, incl. edge cases."""
from __future__ import annotations

import pytest
from pytest import approx

from sentinel.indicators import fundamentals as f


class TestGrowth:
    def test_basic(self):
        assert f.growth(980, 740) == approx(980 / 740 - 1)

    def test_decline(self):
        assert f.growth(212, 252) == approx(212 / 252 - 1)

    def test_missing_or_bad_prior(self):
        assert f.growth(None, 740) is None
        assert f.growth(980, None) is None
        assert f.growth(980, 0) is None
        assert f.growth(980, -5) is None


class TestMargins:
    def test_fcf_margin(self):
        assert f.fcf_margin(90, 10, 200) == approx(80 / 200)

    def test_fcf_margin_negative(self):
        assert f.fcf_margin(2, 10, 100) == approx(-0.08)

    def test_fcf_margin_missing(self):
        assert f.fcf_margin(None, 10, 200) is None
        assert f.fcf_margin(90, None, 200) is None
        assert f.fcf_margin(90, 10, None) is None
        assert f.fcf_margin(90, 10, 0) is None

    def test_ebitda_margin(self):
        assert f.ebitda_margin(50, 200) == approx(0.25)
        assert f.ebitda_margin(None, 200) is None

    def test_op_margin(self):
        assert f.op_margin(-8, 212) == approx(-8 / 212)

    def test_fcf_margin_ex_sbc(self):
        assert f.fcf_margin_ex_sbc(60, 20, 84, 400) == approx(-44 / 400)
        assert f.fcf_margin_ex_sbc(60, 20, None, 400) is None


class TestRuleOf40Family:
    def test_r40(self):
        assert f.r40(0.324324, 0.295918) == approx(0.620242)
        assert f.r40(None, 0.3) is None
        assert f.r40(0.3, None) is None

    def test_rule_of_x(self):
        assert f.rule_of_x(0.25, 0.10) == approx(0.60)
        assert f.rule_of_x(None, 0.10) is None


class TestGuards:
    def test_dilution(self):
        assert f.dilution(110, 100) == approx(0.10)
        assert f.dilution(90, 95) == approx(90 / 95 - 1)
        assert f.dilution(110, None) is None
        assert f.dilution(110, 0) is None

    def test_sbc_intensity(self):
        assert f.sbc_intensity(84, 400) == approx(0.21)
        assert f.sbc_intensity(None, 400) is None

    def test_enterprise_value_and_ev_revenue(self):
        ev = f.enterprise_value(12000, 500, 1500)
        assert ev == approx(11000)
        assert f.ev_revenue(ev, 980) == approx(11000 / 980)
        assert f.enterprise_value(None, 500, 1500) is None

    def test_fcf_yield(self):
        assert f.fcf_yield(330, 40, 12000) == approx(290 / 12000)
        assert f.fcf_yield(8, 16, 300) == approx(-8 / 300)
        assert f.fcf_yield(330, 40, None) is None


class TestAnnualGrowthFallback:
    def test_two_years(self):
        from datetime import date

        annual = {date(2026, 1, 31): 120.0, date(2025, 1, 31): 100.0}
        assert f._annual_growth(annual) == approx(0.20)

    def test_insufficient(self):
        from datetime import date

        assert f._annual_growth({}) is None
        assert f._annual_growth({date(2026, 1, 31): 120.0}) is None
