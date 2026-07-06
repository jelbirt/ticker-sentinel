"""Fundamental score F and valuation labels — section 6, hand-computed expectations."""
from __future__ import annotations

import pytest
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


class TestTechnicalScore:
    def _snap(self, **kw):
        from sentinel.indicators.technicals import TechnicalSnapshot

        return TechnicalSnapshot(**kw)

    def test_perfect_technicals_hit_100(self):
        from sentinel.scoring import technical_score

        t = self._snap(
            trend_state="uptrend", rel_strength_3m=0.20, golden_cross_recent=True,
            dist_52w_high=0.0,
        )
        # 40 + 30 (rs clamped) + 15 + 15 = 100
        assert technical_score(t) == 100.0

    def test_breakdown_clamps_to_zero(self):
        from sentinel.scoring import technical_score

        t = self._snap(
            trend_state="downtrend", rel_strength_3m=-0.30, death_cross_recent=True,
            dist_52w_high=-0.50,
        )
        # −20 − 30 − 15 + 7.5 = −57.5 → 0
        assert technical_score(t) == 0.0

    def test_mixed_hand_computed(self):
        from sentinel.scoring import technical_score

        t = self._snap(trend_state="mixed", rel_strength_3m=0.075, dist_52w_high=-0.10)
        # 0 + 15 + 0 + 13.5 = 28.5
        assert technical_score(t) == pytest.approx(28.5)

    def test_no_trend_read_is_none(self):
        from sentinel.scoring import technical_score

        assert technical_score(None) is None
        assert technical_score(self._snap(trend_state=None)) is None


class TestComposite:
    def test_weighted_blend(self):
        from sentinel.scoring import composite_score

        assert composite_score(80.0, 50.0, 0.6, 0.4) == pytest.approx(68.0)
        assert composite_score(80.0, 50.0, 0.5, 0.5) == pytest.approx(65.0)  # weights from config

    def test_missing_technicals_falls_back_to_f(self):
        from sentinel.scoring import composite_score

        assert composite_score(80.0, None) == pytest.approx(80.0)
        assert composite_score(None, 50.0) is None

    def test_apply_scores_attaches_tech_and_cross_flags(self, fixture_inputs):
        from sentinel.indicators.fundamentals import compute_scorecard
        from sentinel.indicators.technicals import TechnicalSnapshot
        from sentinel.scoring import apply_scores
        from tests.conftest import FIXED_TODAY

        cards = [compute_scorecard(inp, today=FIXED_TODAY) for inp in fixture_inputs.values()]
        tech = {
            "ALFA": TechnicalSnapshot(trend_state="uptrend", golden_cross_recent=True),
            "CHRL": TechnicalSnapshot(trend_state="downtrend", death_cross_recent=True),
        }
        apply_scores(cards, tech, 0.6, 0.4)
        by = {sc.ticker: sc for sc in cards}
        assert "golden_cross" in by["ALFA"].flags
        assert "death_cross" in by["CHRL"].flags
        assert by["ALFA"].composite == pytest.approx(
            0.6 * by["ALFA"].score + 0.4 * by["ALFA"].technical_score
        )
        assert by["BRVO"].tech is None
        assert by["BRVO"].composite == pytest.approx(by["BRVO"].score)  # F fallback


def test_valuation_tags():
    assert valuation_tag(Scorecard(ticker="X", fcf_yield=0.05, ev_revenue=8)) == "cheap"
    assert (
        valuation_tag(Scorecard(ticker="X", fcf_yield=0.005, ev_revenue=15))
        == "priced-for-perfection"
    )
    assert valuation_tag(Scorecard(ticker="X", fcf_yield=0.02, ev_revenue=11)) == "fair"
    assert valuation_tag(Scorecard(ticker="X", fcf_yield=0.005, ev_revenue=None)) == "fair"
    assert valuation_tag(Scorecard(ticker="X")) is None
