"""Between-quarter signals — Phase 2.5 (pure logic; no I/O).

Signals that move weekly or faster while statements are frozen between earnings:
analyst estimate revisions, recommendation trends, short interest, insider activity.
Informational only — deliberately NOT part of any score (scoring changes need
explicit sign-off per project rules).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

# alert thresholds (informational labels, not score inputs)
REVISION_ALERT_COUNT = 5        # ≥ N analysts revising up with none down (or vice versa)
SHORT_MOM_ALERT = 0.20          # |month-over-month change in shares short| > 20%
HEAVILY_SHORTED = 0.10          # short interest > 10% of float


@dataclass
class SignalSnapshot:
    ticker: str
    # analyst EPS estimate revisions, current quarter, analyst counts
    eps_rev_up_7d: int | None = None
    eps_rev_up_30d: int | None = None
    eps_rev_down_7d: int | None = None
    eps_rev_down_30d: int | None = None
    # analyst recommendations, latest month vs one month earlier
    rec_bullish: int | None = None     # strongBuy + buy
    rec_neutral: int | None = None     # hold
    rec_bearish: int | None = None     # sell + strongSell
    rec_bullish_prior: int | None = None
    rec_bearish_prior: int | None = None
    # short interest (FINRA twice-monthly cycle, via Yahoo)
    short_pct_float: float | None = None
    shares_short: float | None = None
    shares_short_prior: float | None = None   # prior month's reading
    short_interest_date: date | None = None
    # insider activity, trailing 6 months
    insider_net_shares_6m: float | None = None  # purchases − sales
    insider_buy_txns_6m: int | None = None
    insider_sell_txns_6m: int | None = None
    sources: list[str] = field(default_factory=list)


def net_revisions_30d(s: SignalSnapshot) -> int | None:
    if s.eps_rev_up_30d is None or s.eps_rev_down_30d is None:
        return None
    return s.eps_rev_up_30d - s.eps_rev_down_30d


def short_change_mom(shares_short: float | None, prior: float | None) -> float | None:
    """Month-over-month change in shares short, as a fraction."""
    if shares_short is None or prior is None or prior <= 0:
        return None
    return shares_short / prior - 1


def signal_alerts(s: SignalSnapshot) -> list[str]:
    """Noteworthy between-quarter events for the Movers & alerts section.

    Returned without the ticker prefix; the report builder adds it.
    """
    alerts: list[str] = []

    up, down = s.eps_rev_up_30d, s.eps_rev_down_30d
    if up is not None and down is not None:
        if up >= REVISION_ALERT_COUNT and down == 0:
            alerts.append(f"estimates revised up by {up} analysts in 30d, none down")
        elif down > up:
            alerts.append(f"estimates revised down (▼{down} vs ▲{up} analysts, 30d)")

    mom = short_change_mom(s.shares_short, s.shares_short_prior)
    if mom is not None and abs(mom) > SHORT_MOM_ALERT:
        direction = "up" if mom > 0 else "down"
        alerts.append(f"short interest {direction} {abs(mom) * 100:.0f}% month-over-month")
    if s.short_pct_float is not None and s.short_pct_float > HEAVILY_SHORTED:
        alerts.append(f"heavily shorted ({s.short_pct_float * 100:.1f}% of float)")

    if s.insider_net_shares_6m is not None and s.insider_net_shares_6m > 0:
        alerts.append(
            f"insiders net-buying (+{s.insider_net_shares_6m:,.0f} shares over 6m)"
        )

    if (
        s.rec_bullish is not None
        and s.rec_bullish_prior is not None
        and s.rec_bullish < s.rec_bullish_prior
    ):
        alerts.append(
            f"analyst bullishness slipping ({s.rec_bullish_prior} → {s.rec_bullish} buy-or-better)"
        )

    return alerts
