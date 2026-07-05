"""Pure fundamental-metric formulas — PROJECT_PLAN.md section 5, exactly.

All ratios are fractions (0.40 means "40 points" / 40%). Inputs `capex` and `sbc`
are positive magnitudes (the data-layer alias code normalizes signs).
No I/O in this module: dataclasses in, values out.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

# Flag identifiers (rendered with labels/emoji by the report builder)
FLAG_INSUFFICIENT_DATA = "insufficient_data"      # <4 quarters: no TTM at all
FLAG_INSUFFICIENT_HISTORY = "insufficient_history"  # trend/growth window unavailable
FLAG_GROWTH_FROM_ANNUAL = "growth_from_annual"    # growth fell back to FY-over-FY
FLAG_SBC_INFLATED = "sbc_inflated"                # r40_fcf − r40_sbc_adj > 20 pts
FLAG_DILUTION = "dilution"                        # dilution > 3%/yr
FLAG_HIGH_SBC = "high_sbc"                        # sbc_intensity > 15%
FLAG_STALE = "stale_fundamentals"                 # statements > 200 days old
FLAG_ALL_R40 = "passes_all_r40"                   # all three R40 variants ≥ 40

STALE_DAYS = 200
DILUTION_LIMIT = 0.03
SBC_INTENSITY_LIMIT = 0.15
SBC_GAP_LIMIT = 0.20  # 20 points


@dataclass(frozen=True)
class TTMWindow:
    """Trailing-twelve-month sums as of one point in time."""
    revenue: float | None = None
    ebitda: float | None = None
    operating_income: float | None = None
    ocf: float | None = None
    capex: float | None = None
    sbc: float | None = None


@dataclass
class FundamentalInputs:
    """Everything compute_scorecard needs; the data layer builds this.

    ttm_minus_Nq are TTM windows shifted back N quarters. Computing r40 at −2Q/−4Q
    needs the growth denominator a further 4 quarters back, hence −6Q and −8Q.
    """
    ticker: str
    ttm_now: TTMWindow | None = None
    ttm_minus_2q: TTMWindow | None = None
    ttm_minus_4q: TTMWindow | None = None
    ttm_minus_6q: TTMWindow | None = None
    ttm_minus_8q: TTMWindow | None = None
    diluted_shares_now: float | None = None
    diluted_shares_1y_ago: float | None = None
    market_cap: float | None = None
    total_debt: float | None = None
    cash: float | None = None
    statement_date: date | None = None
    annual_revenue: dict[date, float] = field(default_factory=dict)  # growth fallback
    data_notes: list[str] = field(default_factory=list)


@dataclass
class Scorecard:
    ticker: str
    growth: float | None = None
    growth_source: str | None = None  # "ttm" | "annual"
    fcf_margin: float | None = None
    ebitda_margin: float | None = None
    op_margin: float | None = None
    fcf_margin_ex_sbc: float | None = None
    r40_fcf: float | None = None
    r40_ebitda: float | None = None
    r40_sbc_adj: float | None = None
    rule_of_x: float | None = None
    r40_fcf_2q: float | None = None   # sparkline point (Phase 2)
    r40_trend: float | None = None
    dilution: float | None = None
    sbc_intensity: float | None = None
    ev_revenue: float | None = None
    fcf_yield: float | None = None
    statement_date: date | None = None
    stale: bool = False
    flags: list[str] = field(default_factory=list)
    # filled in by sentinel.scoring:
    score: float | None = None
    valuation: str | None = None


def _div(num: float | None, den: float | None) -> float | None:
    if num is None or den is None or den == 0:
        return None
    return num / den


def _add(*vals: float | None) -> float | None:
    if any(v is None for v in vals):
        return None
    return sum(vals)  # type: ignore[arg-type]


def growth(rev_ttm: float | None, rev_ttm_prior: float | None) -> float | None:
    """(Rev_TTM / Rev_TTM_prior) − 1. None if prior revenue is missing or non-positive."""
    if rev_ttm is None or rev_ttm_prior is None or rev_ttm_prior <= 0:
        return None
    return rev_ttm / rev_ttm_prior - 1


def fcf_margin(ocf: float | None, capex: float | None, revenue: float | None) -> float | None:
    """(OCF − CapEx) / Rev_TTM"""
    if ocf is None or capex is None:
        return None
    return _div(ocf - capex, revenue)


def ebitda_margin(ebitda: float | None, revenue: float | None) -> float | None:
    """EBITDA / Rev_TTM"""
    return _div(ebitda, revenue)


def op_margin(operating_income: float | None, revenue: float | None) -> float | None:
    """OperatingIncome / Rev_TTM"""
    return _div(operating_income, revenue)


def fcf_margin_ex_sbc(
    ocf: float | None, capex: float | None, sbc: float | None, revenue: float | None
) -> float | None:
    """(OCF − CapEx − SBC) / Rev_TTM — treats SBC as a cash cost."""
    if ocf is None or capex is None or sbc is None:
        return None
    return _div(ocf - capex - sbc, revenue)


def r40(growth_val: float | None, margin: float | None) -> float | None:
    """growth + margin — shared shape of all three R40 variants."""
    return _add(growth_val, margin)


def rule_of_x(growth_val: float | None, fcf_margin_val: float | None) -> float | None:
    """2 × growth + fcf_margin (target ≥ 50 pts)."""
    if growth_val is None or fcf_margin_val is None:
        return None
    return 2 * growth_val + fcf_margin_val


def dilution(shares_now: float | None, shares_1y_ago: float | None) -> float | None:
    """(DilutedShares_now / DilutedShares_1y_ago) − 1"""
    if shares_now is None or shares_1y_ago is None or shares_1y_ago <= 0:
        return None
    return shares_now / shares_1y_ago - 1


def sbc_intensity(sbc: float | None, revenue: float | None) -> float | None:
    """SBC_TTM / Rev_TTM"""
    return _div(sbc, revenue)


def enterprise_value(
    market_cap: float | None, total_debt: float | None, cash: float | None
) -> float | None:
    """MarketCap + TotalDebt − Cash"""
    if market_cap is None or total_debt is None or cash is None:
        return None
    return market_cap + total_debt - cash


def ev_revenue(ev: float | None, revenue: float | None) -> float | None:
    """EnterpriseValue / Rev_TTM"""
    return _div(ev, revenue)


def fcf_yield(ocf: float | None, capex: float | None, market_cap: float | None) -> float | None:
    """(OCF − CapEx)_TTM / MarketCap"""
    if ocf is None or capex is None:
        return None
    return _div(ocf - capex, market_cap)


def _r40_fcf_at(window: TTMWindow | None, prior: TTMWindow | None) -> float | None:
    """r40_fcf at an arbitrary point: growth vs the window 4 quarters earlier + its FCF margin."""
    if window is None or prior is None:
        return None
    g = growth(window.revenue, prior.revenue)
    m = fcf_margin(window.ocf, window.capex, window.revenue)
    return r40(g, m)


def _annual_growth(annual_revenue: dict[date, float]) -> float | None:
    """FY-over-FY growth from the two most recent annual revenues (fallback path)."""
    if len(annual_revenue) < 2:
        return None
    ordered = sorted(annual_revenue.items(), key=lambda kv: kv[0], reverse=True)
    return growth(ordered[0][1], ordered[1][1])


def compute_scorecard(inp: FundamentalInputs, today: date | None = None) -> Scorecard:
    """Assemble the full section-5 scorecard. Missing data flags, never raises."""
    sc = Scorecard(ticker=inp.ticker, statement_date=inp.statement_date)
    now = inp.ttm_now

    if now is None or now.revenue is None:
        sc.flags.append(FLAG_INSUFFICIENT_DATA)
        return sc

    # growth: TTM vs TTM one year earlier; FY-over-FY fallback if history is short
    g = growth(now.revenue, inp.ttm_minus_4q.revenue if inp.ttm_minus_4q else None)
    if g is not None:
        sc.growth_source = "ttm"
    else:
        g = _annual_growth(inp.annual_revenue)
        if g is not None:
            sc.growth_source = "annual"
            sc.flags.append(FLAG_GROWTH_FROM_ANNUAL)
        else:
            sc.flags.append(FLAG_INSUFFICIENT_HISTORY)
    sc.growth = g

    # margins
    sc.fcf_margin = fcf_margin(now.ocf, now.capex, now.revenue)
    sc.ebitda_margin = ebitda_margin(now.ebitda, now.revenue)
    sc.op_margin = op_margin(now.operating_income, now.revenue)
    sc.fcf_margin_ex_sbc = fcf_margin_ex_sbc(now.ocf, now.capex, now.sbc, now.revenue)

    # rule-of-40 family
    sc.r40_fcf = r40(g, sc.fcf_margin)
    sc.r40_ebitda = r40(g, sc.ebitda_margin)
    sc.r40_sbc_adj = r40(g, sc.fcf_margin_ex_sbc)
    sc.rule_of_x = rule_of_x(g, sc.fcf_margin)

    # trend: r40_fcf(now) − r40_fcf(−4Q); −2Q point kept for the Phase 2 sparkline
    r40_m4 = _r40_fcf_at(inp.ttm_minus_4q, inp.ttm_minus_8q)
    sc.r40_fcf_2q = _r40_fcf_at(inp.ttm_minus_2q, inp.ttm_minus_6q)
    if sc.r40_fcf is not None and r40_m4 is not None:
        sc.r40_trend = sc.r40_fcf - r40_m4
    elif FLAG_INSUFFICIENT_HISTORY not in sc.flags:
        sc.flags.append(FLAG_INSUFFICIENT_HISTORY)

    # dilution & valuation guards
    sc.dilution = dilution(inp.diluted_shares_now, inp.diluted_shares_1y_ago)
    sc.sbc_intensity = sbc_intensity(now.sbc, now.revenue)
    ev = enterprise_value(inp.market_cap, inp.total_debt, inp.cash)
    sc.ev_revenue = ev_revenue(ev, now.revenue)
    sc.fcf_yield = fcf_yield(now.ocf, now.capex, inp.market_cap)

    # flags
    if sc.dilution is not None and sc.dilution > DILUTION_LIMIT:
        sc.flags.append(FLAG_DILUTION)
    if sc.sbc_intensity is not None and sc.sbc_intensity > SBC_INTENSITY_LIMIT:
        sc.flags.append(FLAG_HIGH_SBC)
    if (
        sc.r40_fcf is not None
        and sc.r40_sbc_adj is not None
        and sc.r40_fcf - sc.r40_sbc_adj > SBC_GAP_LIMIT
    ):
        sc.flags.append(FLAG_SBC_INFLATED)
    if all(v is not None and v >= 0.40 for v in (sc.r40_fcf, sc.r40_ebitda, sc.r40_sbc_adj)):
        sc.flags.append(FLAG_ALL_R40)

    today = today or date.today()
    if inp.statement_date is not None and (today - inp.statement_date).days > STALE_DAYS:
        sc.stale = True
        sc.flags.append(FLAG_STALE)

    return sc
