"""Fundamental score F and valuation labels — section 6, hand-computed expectations."""
from __future__ import annotations

from pytest import approx

from sentinel.indicators.fundamentals import Scorecard
from sentinel.scoring import fundamental_score, valuation_tag


def test_alfa_score(fixture_scorecards):
    # base 62.024×0.75 = 46.518; rule_of_x 94.457 → capped bonus 15; +10 (r40_ebitda ≥ 40);
    # trend +6.060 → 77.578
    assert fundamental_score(fixture_scorecards["ALFA"]) == approx(77.5779, abs=1e-3)


def test_brvo_score(fixture_scorecards):
    # base 35×0.75 = 26.25; rox 60 → +5; no ebitda bonus; trend +11.964; penalties −30 → 13.214
    assert fundamental_score(fixture_scorecards["BRVO"]) == approx(13.2143, abs=1e-3)


def test_chrl_score_clamped_to_zero(fixture_scorecards):
    # base −19.647×0.75 = −14.735; trend −5.948 → −20.68 → clamp 0
    assert fundamental_score(fixture_scorecards["CHRL"]) == approx(0.0)


def test_base_cap_at_80_points():
    sc = Scorecard(ticker="X", r40_fcf=1.20)  # 120 pts, capped at 80
    assert fundamental_score(sc) == approx(60.0)


def test_trend_clamped_both_ways():
    up = Scorecard(ticker="X", r40_fcf=0.40, r40_trend=0.50)
    down = Scorecard(ticker="X", r40_fcf=0.40, r40_trend=-0.50)
    assert fundamental_score(up) == approx(30.0 + 15.0)
    assert fundamental_score(down) == approx(30.0 - 15.0)


def test_unscorable_when_r40_missing_or_stale():
    assert fundamental_score(Scorecard(ticker="X")) is None
    assert fundamental_score(Scorecard(ticker="X", r40_fcf=0.40, stale=True)) is None


def test_all_three_penalties():
    sc = Scorecard(
        ticker="X", r40_fcf=0.40, r40_sbc_adj=0.10, dilution=0.05, sbc_intensity=0.20
    )
    assert fundamental_score(sc) == approx(0.0)  # 30 − 30 = 0


def test_valuation_tags():
    assert valuation_tag(Scorecard(ticker="X", fcf_yield=0.05, ev_revenue=8)) == "cheap"
    assert (
        valuation_tag(Scorecard(ticker="X", fcf_yield=0.005, ev_revenue=15))
        == "priced-for-perfection"
    )
    assert valuation_tag(Scorecard(ticker="X", fcf_yield=0.02, ev_revenue=11)) == "fair"
    assert valuation_tag(Scorecard(ticker="X", fcf_yield=0.005, ev_revenue=None)) == "fair"
    assert valuation_tag(Scorecard(ticker="X")) is None
