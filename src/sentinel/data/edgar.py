"""SEC EDGAR companyfacts XBRL client for the one-time history backfill.

Adapted from the owner's split-signal project
(`src/split_signal/data/edgar.py`), which contributed the CIK lookup, the
throttled session, the tag-alias normalization and the
quarterly/ytd/annual period typing. Two deliberate divergences from it:

* Restatements: the LATEST filed value wins here, because this cache mirrors
  yfinance's restated view. split-signal keeps the EARLIEST filing for
  point-in-time research discipline.
* Output shape: this module emits the canonical statements frame the rest of
  the repo already speaks (rows = CANONICAL_FIELDS, columns = quarter-end
  Timestamps, newest first), not a long-format fact table.

This module is never imported by the scheduled run. Steady-state fundamentals
stay on yfinance; `sentinel.backfill` is a one-time tool (see
`tasks/spec-history-backfill.md`).

SEC fair access allows up to 10 requests/second with a declared User-Agent;
the default throttle here sits far below that.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import pandas as pd
import requests

from sentinel.data.fundamentals import CANONICAL_FIELDS, NEGATIVE_MAGNITUDE_FIELDS

COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

# SEC fair access requires a declared identity plus a contact route. Override
# with SEC_USER_AGENT when running this from anywhere but the owner's machine.
DEFAULT_USER_AGENT = (
    "ticker-sentinel/0.1 (personal research; jelbirt.consult@gmail.com)"
)
THROTTLE_S = 0.2  # ~5 req/s, half the SEC fair-access ceiling
TIMEOUT_S = 30

# Amendment 1: capex is a COMPOSITE field, not a first-match-wins alias list.
# yfinance's "Capital Expenditure" for SaaS filers bundles capitalized software
# development with plain PP&E purchases, so the single
# PaymentsToAcquirePropertyPlantAndEquipment tag reads 7 to 97 percent low and
# fails the verification gate on names that are otherwise clean.
#
# BASE tags are alternatives to each other (first present wins);
# PaymentsToAcquireProductiveAssets is a broader BASE some filers use INSTEAD of
# PP&E, never an addend on top of it, so preferring PP&E cannot double count.
CAPEX_BASE_TAGS = [
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsToAcquireProductiveAssets",
]
# ADDEND tags are summed on top of the base when the filer reports them.
CAPEX_ADDEND_TAGS = [
    "PaymentsToDevelopSoftware",
    "PaymentsToCapitalizeInternalUseSoftware",
    "PaymentsToAcquireIntangibleAssets",
]

# canonical field -> us-gaap tags, first tag with any usable facts wins
# (same alias semantics as the yfinance ALIASES map in data/fundamentals.py).
# `capex` carries only its BASE tags here; its addends go through
# COMPOSITE_TAGS below.
TAG_MAP: dict[str, list[str]] = {
    "revenue": [
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
    ],
    "operating_income": ["OperatingIncomeLoss"],
    "d_and_a": [
        "DepreciationDepletionAndAmortization",
        "DepreciationAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
    ],
    "ocf": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ],
    "capex": CAPEX_BASE_TAGS,
    "sbc": ["ShareBasedCompensation"],
    "diluted_shares": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
}

# canonical field -> (base tag alternatives, addend tags summed on top)
COMPOSITE_TAGS: dict[str, tuple[list[str], list[str]]] = {
    "capex": (CAPEX_BASE_TAGS, CAPEX_ADDEND_TAGS),
}

# Amendment 1 (approved 2026-08-15) narrows the backfilled set. Historical
# quarters exist only to feed r40_fcf trend and growth, which read revenue,
# ocf and capex (plus sbc for the SBC-adjusted level and diluted_shares for
# dilution). `operating_income` and `d_and_a` are read only from the NEWEST
# quarter, which always comes from yfinance, so backfilling them bought
# nothing while their as-filed-vs-normalized mismatches rejected whole
# tickers. Their TAG_MAP entries stay as documentation of the EDGAR tags (and
# keep `facts_for_field` usable for ad-hoc inspection); nothing derives them.
BACKFILL_FIELDS = ["revenue", "ocf", "capex", "sbc", "diluted_shares"]

# Deliberately NOT backfilled: `ebitda` (build_ttm's OpInc + D&A fallback
# computes it from the newest quarter), `operating_income` and `d_and_a` (see
# above), `total_debt` and `cash` (EV uses only the newest reading). All of
# them keep NaN in backfilled quarters.

# Weighted averages are not flows: differencing a year-to-date figure or
# subtracting three quarters from a fiscal year produces nonsense for them.
# These fields accept DIRECT 3-month facts only, and are left NaN otherwise.
NON_ADDITIVE_FIELDS = {"diluted_shares"}

# preferred XBRL unit per field; falls back to whatever unit is present
_UNITS = {"diluted_shares": "shares"}

# periodic reports (amendments included); everything else is comparative noise
_PERIODIC_FORMS = {"10-K", "10-Q", "10-K/A", "10-Q/A"}

# duration classification bounds, in days
_QUARTER_MIN_DAYS = 60
_QUARTER_MAX_DAYS = 120
_ANNUAL_MIN_DAYS = 300


@dataclass(frozen=True)
class Fact:
    """One XBRL fact: a value over a period, as of a filing."""

    start: pd.Timestamp | None
    end: pd.Timestamp
    value: float
    filed: pd.Timestamp
    form: str
    period_type: str


def classify_period(start: str | pd.Timestamp | None, end: str | pd.Timestamp) -> str:
    """Duration class: instant (no start), quarterly (~3mo), annual (>10mo), else ytd.

    A 10-K reports both the fiscal-year figure and, for some line items, the
    Q4 figure with the same period_end, so duration is the only reliable
    discriminator (this rule comes from split-signal).
    """
    if start is None:
        return "instant"
    days = (pd.Timestamp(end) - pd.Timestamp(start)).days
    if days > _ANNUAL_MIN_DAYS:
        return "annual"
    if _QUARTER_MIN_DAYS <= days <= _QUARTER_MAX_DAYS:
        return "quarterly"
    return "ytd"


def _entries_for_tag(payload: dict[str, Any], tag: str, field: str) -> list[dict[str, Any]]:
    units = payload.get("facts", {}).get("us-gaap", {}).get(tag, {}).get("units", {})
    if not units:
        return []
    preferred = _UNITS.get(field, "USD")
    if preferred in units:
        return units[preferred]
    return next(iter(units.values()))


def facts_for_tag(payload: dict[str, Any], tag: str, field: str) -> list[Fact]:
    """Periodic-report, non-instant facts for one us-gaap tag."""
    facts = [
        Fact(
            start=pd.Timestamp(e["start"]) if e.get("start") else None,
            end=pd.Timestamp(e["end"]),
            value=float(e["val"]),
            filed=pd.Timestamp(e["filed"]),
            form=str(e.get("form", "")),
            period_type=classify_period(e.get("start"), e["end"]),
        )
        for e in _entries_for_tag(payload, tag, field)
        if e.get("form") in _PERIODIC_FORMS and e.get("val") is not None
    ]
    return [f for f in facts if f.period_type != "instant"]


def facts_for_field(payload: dict[str, Any], field: str) -> list[Fact]:
    """Periodic-report facts for one canonical field; first matching tag wins.

    Ad-hoc inspection helper. Derivation goes through field_values(), whose
    precedence test is stricter: see there for why facts alone are not enough.
    """
    for tag in TAG_MAP[field]:
        facts = facts_for_tag(payload, tag, field)
        if facts:
            return facts
    return []


def field_values(payload: dict[str, Any], field: str) -> dict[pd.Timestamp, float]:
    """Per-quarter values for a plain field: first tag that DERIVES quarters wins.

    The precedence test must sit on derived quarters, not on raw facts: a tag
    can carry facts yet derive zero quarters (PANW files 18 annual-only
    "Revenues" shells), and returning it would shadow a later tag holding the
    real quarterly coverage (PANW's contract-revenue tag has 35 derivable
    quarters). Alias precedence is unchanged; only the non-emptiness test
    moves from facts to quarters.
    """
    additive = field not in NON_ADDITIVE_FIELDS
    for tag in TAG_MAP[field]:
        values = quarterly_values(facts_for_tag(payload, tag, field), additive=additive)
        if values:
            return values
    return {}


def _latest_filed(facts: list[Fact]) -> dict[tuple[pd.Timestamp | None, pd.Timestamp], Fact]:
    """Restatement rule: the latest filed value wins per (start, end) period."""
    out: dict[tuple[pd.Timestamp | None, pd.Timestamp], Fact] = {}
    for fact in facts:
        key = (fact.start, fact.end)
        prior = out.get(key)
        if prior is None or fact.filed > prior.filed:
            out[key] = fact
    return out


def quarterly_values(facts: list[Fact], additive: bool = True) -> dict[pd.Timestamp, float]:
    """Per-quarter values keyed by fiscal period_end.

    Three passes, in precedence order:

    1. Direct 3-month facts (10-Q income-statement lines, and the Q1 figure of
       any year-to-date series, which is itself 3 months long).
    2. Cumulative differencing inside a fiscal year: quarter n =
       YTD(n) - YTD(n-1), which also yields Q4 = FY - YTD Q3. This is how
       cash-flow lines, filed year-to-date in 10-Qs, become quarters. A
       missing intermediate YTD leaves that quarter absent (the pair either
       side of the hole is more than a quarter apart, so it is not
       differenced), which is exactly the NaN the TTM all-4 rule expects.
    3. Q4 = FY - (Q1 + Q2 + Q3) when a fiscal-year figure has no YTD Q3 to
       difference against but all three earlier quarters are known.

    `additive=False` (weighted averages such as diluted shares) stops after
    pass 1: differencing a mean is arithmetically meaningless.
    """
    deduped = _latest_filed(facts)
    out: dict[pd.Timestamp, float] = {}

    # pass 1: direct 3-month facts (latest filed already resolved per period)
    direct_filed: dict[pd.Timestamp, pd.Timestamp] = {}
    for fact in deduped.values():
        if fact.period_type != "quarterly":
            continue
        if fact.end not in out or fact.filed > direct_filed[fact.end]:
            out[fact.end] = fact.value
            direct_filed[fact.end] = fact.filed
    if not additive:
        return out

    # cumulative groups keyed by period start; a 3-month fact is a cumulative
    # point too, so Q2 = YTD(6mo) - Q1 falls out of the same differencing
    groups: dict[pd.Timestamp, dict[pd.Timestamp, float]] = {}
    for fact in deduped.values():
        if fact.start is None:
            continue
        groups.setdefault(fact.start, {})[fact.end] = fact.value

    # pass 2: difference consecutive cumulative points one quarter apart
    for points in groups.values():
        ends = sorted(points)
        for prev, end in zip(ends, ends[1:]):
            if end in out:
                continue
            if _QUARTER_MIN_DAYS <= (end - prev).days <= _QUARTER_MAX_DAYS:
                out[end] = points[end] - points[prev]

    # pass 3: Q4 = FY - (Q1 + Q2 + Q3) when the YTD Q3 point is missing
    for start, points in groups.items():
        for end, value in points.items():
            if end in out or (end - start).days <= _ANNUAL_MIN_DAYS:
                continue
            inside = sorted(q for q in out if start <= q < end)
            if len(inside) == 3:
                out[end] = value - sum(out[q] for q in inside)

    return out


def composite_values(payload: dict[str, Any], field: str) -> dict[pd.Timestamp, float]:
    """Per-quarter values for a COMPOSITE field: base tag plus its addends.

    Each component tag gets its own independent quarterly derivation (the same
    YTD differencing as any other field), and the components are summed per
    quarter. Deriving first and summing second is the only correct order: a
    filer can report PP&E purchases per quarter while filing capitalized
    software year-to-date, so summing the raw facts would mix period types.

    Two rules decide what a missing quarter means, and they are deliberately
    asymmetric:

    * BASE missing for a quarter -> that quarter is absent (NaN downstream).
      The base is the bulk of the number; without it there is nothing to
      report.
    * ADDEND missing for a quarter -> treated as 0, but ONLY when the addend
      tag files nothing at all ending on that period end. A filer that
      capitalizes no software in a quarter simply omits the tag, and reading
      that as 0 is what the cash flow statement means. When the tag DOES file
      for that period end and the quarter still could not be derived (a
      missing intermediate YTD point), the addend is unknown rather than zero,
      so the whole quarter is dropped instead of silently understated.

    The verification gate stays the arbiter either way: a composite that does
    not reconcile with the cached yfinance value inside the D2 tolerance still
    rejects the ticker.
    """
    base_tags, addend_tags = COMPOSITE_TAGS[field]
    out: dict[pd.Timestamp, float] = {}
    # alternatives, not addends: the first base tag that DERIVES quarters wins
    # (same rule as field_values: facts alone can be annual-only shells that
    # would shadow a base with real quarterly coverage)
    for tag in base_tags:
        values = quarterly_values(facts_for_tag(payload, tag, field))
        if values:
            out = {end: abs(v) for end, v in values.items()}
            break
    if not out:
        return {}

    for tag in addend_tags:
        facts = facts_for_tag(payload, tag, field)
        if not facts:
            continue  # tag never filed: contributes 0 to every quarter
        values = quarterly_values(facts)
        filed_ends = {f.end for f in facts}
        for quarter in list(out):
            if quarter in values:
                out[quarter] += abs(values[quarter])
            elif quarter in filed_ends:
                del out[quarter]  # filed for this period but underivable
    return out


def canonical_from_companyfacts(payload: dict[str, Any]) -> pd.DataFrame:
    """companyfacts JSON -> canonical statements frame (newest quarter first).

    Rows are CANONICAL_FIELDS so the frame merges directly with a cached
    yfinance frame; fields outside BACKFILL_FIELDS stay NaN. Signs follow the
    cache convention (capex as a positive magnitude).
    """
    series: dict[str, dict[pd.Timestamp, float]] = {}
    for field in BACKFILL_FIELDS:
        if field in COMPOSITE_TAGS:
            series[field] = composite_values(payload, field)
            continue
        series[field] = field_values(payload, field)

    columns = sorted({end for values in series.values() for end in values}, reverse=True)
    frame = pd.DataFrame(index=CANONICAL_FIELDS, columns=columns, dtype="float64")
    for field, values in series.items():
        for end, value in values.items():
            frame.loc[field, end] = abs(value) if field in NEGATIVE_MAGNITUDE_FIELDS else value
    return frame


# --- network ---------------------------------------------------------------


def user_agent() -> str:
    return os.environ.get("SEC_USER_AGENT") or DEFAULT_USER_AGENT


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = user_agent()
    session.headers["Accept-Encoding"] = "gzip, deflate"
    return session


def _get_json(url: str, session: requests.Session, throttle_s: float) -> Any:
    if throttle_s:
        time.sleep(throttle_s)
    response = session.get(url, timeout=TIMEOUT_S)
    response.raise_for_status()
    return response.json()


def fetch_cik_map(
    session: requests.Session | None = None, throttle_s: float = THROTTLE_S
) -> dict[str, int]:
    """Ticker -> CIK, from the SEC's public company_tickers.json."""
    session = session or make_session()
    data = _get_json(COMPANY_TICKERS_URL, session, throttle_s)
    rows = data.values() if isinstance(data, dict) else data
    return {str(row["ticker"]).upper(): int(row["cik_str"]) for row in rows}


def fetch_companyfacts(
    cik: int, session: requests.Session | None = None, throttle_s: float = THROTTLE_S
) -> dict[str, Any]:
    session = session or make_session()
    return _get_json(COMPANYFACTS_URL.format(cik=cik), session, throttle_s)
