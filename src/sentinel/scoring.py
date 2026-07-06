"""Scoring model — PROJECT_PLAN.md section 6.

Fundamental score F, technical score T, composite C = w_f·F + w_t·T (weights from
config, never hardcoded). Scorecard ratios are fractions; section 6 thresholds are
in points, so convert ×100 here.

T interpretation (section 6 prose, made exact): trend +40/0/−20, relative strength
±30 (full credit at ±15% vs SPY over 3m), golden/death cross ±15, 52-week-high
proximity 0..+15 via 15 × clamp(1 + dist_52w_high, 0, 1); sum clamped to [0, 100].
The maxima sum to exactly 100, so no further rescaling is applied.
"""
from __future__ import annotations

from sentinel.indicators.fundamentals import Scorecard
from sentinel.indicators.technicals import TREND_DOWN, TREND_UP, TechnicalSnapshot

FLAG_GOLDEN_CROSS = "golden_cross"
FLAG_DEATH_CROSS = "death_cross"

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


def technical_score(t: TechnicalSnapshot | None) -> float | None:
    """T (0–100) per section 6. None when there is no price data / no trend read."""
    if t is None or t.trend_state is None:
        return None
    pts = {TREND_UP: 40.0, TREND_DOWN: -20.0}.get(t.trend_state, 0.0)
    if t.rel_strength_3m is not None:
        pts += 30.0 * clamp(t.rel_strength_3m / 0.15, -1.0, 1.0)
    if t.golden_cross_recent:
        pts += 15.0
    if t.death_cross_recent:
        pts -= 15.0
    if t.dist_52w_high is not None:
        pts += 15.0 * clamp(1.0 + t.dist_52w_high, 0.0, 1.0)
    return clamp(pts, 0.0, 100.0)


def composite_score(
    f: float | None,
    t: float | None,
    fundamentals_weight: float = 0.6,
    technicals_weight: float = 0.4,
) -> float | None:
    """C = w_f·F + w_t·T. Falls back to F alone when T is unavailable."""
    if f is None:
        return None
    if t is None:
        return f
    return fundamentals_weight * f + technicals_weight * t


def apply_scores(
    scorecards: list[Scorecard],
    technicals: dict[str, TechnicalSnapshot | None] | None = None,
    fundamentals_weight: float = 0.6,
    technicals_weight: float = 0.4,
) -> list[Scorecard]:
    for sc in scorecards:
        sc.score = fundamental_score(sc)
        sc.valuation = valuation_tag(sc)
        sc.tech = (technicals or {}).get(sc.ticker)
        sc.technical_score = technical_score(sc.tech)
        sc.composite = composite_score(
            sc.score, sc.technical_score, fundamentals_weight, technicals_weight
        )
        if sc.tech is not None:
            if sc.tech.golden_cross_recent:
                sc.flags.append(FLAG_GOLDEN_CROSS)
            if sc.tech.death_cross_recent:
                sc.flags.append(FLAG_DEATH_CROSS)
    return scorecards
