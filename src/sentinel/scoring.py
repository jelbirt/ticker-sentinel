"""Scoring model — PROJECT_PLAN.md section 6.

Phase 1 implements the fundamental score F and the valuation label; the technical
score T and composite C = w_f·F + w_t·T arrive in Phase 2 (weights come from config).
Scorecard ratios are fractions; section 6 thresholds are in points, so convert ×100 here.
"""
from __future__ import annotations

from sentinel.indicators.fundamentals import Scorecard

VALUATION_CHEAP_FCF_YIELD = 0.04
VALUATION_RICH_EV_REVENUE = 12.0
VALUATION_RICH_FCF_YIELD = 0.01


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def fundamental_score(sc: Scorecard) -> float | None:
    """F (0–100) per section 6. None when unscorable (no r40_fcf, or stale >200d)."""
    if sc.r40_fcf is None or sc.stale:
        return None
    r40_pts = sc.r40_fcf * 100

    score = min(r40_pts, 80.0) / 80.0 * 60.0

    if sc.rule_of_x is not None:
        score += min(max(sc.rule_of_x * 100 - 50.0, 0.0), 30.0) * 0.5
    if sc.r40_ebitda is not None and sc.r40_ebitda * 100 >= 40.0:
        score += 10.0
    if sc.r40_trend is not None:
        score += 15.0 * clamp(sc.r40_trend * 100 / 15.0, -1.0, 1.0)

    if sc.dilution is not None and sc.dilution > 0.03:
        score -= 10.0
    if sc.sbc_intensity is not None and sc.sbc_intensity > 0.15:
        score -= 10.0
    if sc.r40_sbc_adj is not None and (sc.r40_fcf - sc.r40_sbc_adj) * 100 > 20.0:
        score -= 10.0

    return clamp(score, 0.0, 100.0)


def valuation_tag(sc: Scorecard) -> str | None:
    """Label, not a score input: cheap / fair / priced-for-perfection."""
    if sc.fcf_yield is None:
        return None
    if sc.fcf_yield > VALUATION_CHEAP_FCF_YIELD:
        return "cheap"
    if (
        sc.ev_revenue is not None
        and sc.ev_revenue > VALUATION_RICH_EV_REVENUE
        and sc.fcf_yield < VALUATION_RICH_FCF_YIELD
    ):
        return "priced-for-perfection"
    return "fair"


def apply_scores(scorecards: list[Scorecard]) -> list[Scorecard]:
    for sc in scorecards:
        sc.score = fundamental_score(sc)
        sc.valuation = valuation_tag(sc)
    return scorecards
