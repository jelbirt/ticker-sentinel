"""Quarterly statements via yfinance: alias-mapped normalization, TTM builder, cache.

All yfinance I/O lives here. Row labels from yfinance drift between versions —
change ALIASES, never scatter string literals elsewhere.
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, timezone
from typing import Any

import pandas as pd
from tenacity import retry, stop_after_attempt, wait_exponential

from sentinel.data import cache
from sentinel.indicators.fundamentals import FundamentalInputs, TTMWindow

log = logging.getLogger(__name__)

# canonical field -> (statement, [yfinance row-label aliases, first match wins])
ALIASES: dict[str, tuple[str, list[str]]] = {
    "revenue": ("income", ["Total Revenue", "Operating Revenue"]),
    "ebitda": ("income", ["EBITDA", "Normalized EBITDA"]),
    "operating_income": ("income", ["Operating Income", "Total Operating Income As Reported"]),
    "d_and_a": (
        "cashflow",
        ["Depreciation And Amortization", "Depreciation Amortization Depletion", "Depreciation"],
    ),
    "ocf": (
        "cashflow",
        ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities"],
    ),
    "capex": ("cashflow", ["Capital Expenditure", "Purchase Of PPE"]),
    "sbc": ("cashflow", ["Stock Based Compensation"]),
    "diluted_shares": ("income", ["Diluted Average Shares"]),
    "total_debt": ("balance", ["Total Debt"]),
    "cash": (
        "balance",
        ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"],
    ),
}

# yfinance reports these as negative outflows; we keep positive magnitudes
NEGATIVE_MAGNITUDE_FIELDS = {"capex"}

CANONICAL_FIELDS = list(ALIASES)

# The fields the headline metric (r40_fcf = growth + fcf_margin) cannot live
# without. Yahoo publishes a fresh quarter's statements piecemeal for a few
# days after earnings; a column carrying only, say, diluted_shares must not
# anchor the TTM windows, or the ticker silently drops out of scoring at the
# exact moment its numbers are most interesting.
CORE_FIELDS = ("revenue", "ocf", "capex")

# Piecemeal publishing lasts days, not quarters: skip at most this many
# leading partial columns. A deeper scan would let a drifted field alias
# (the repo's top gotcha) silently re-anchor scoring on arbitrarily old
# quarters instead of surfacing the missing field as degraded data.
MAX_ANCHOR_SKIP = 2

# Consecutive fiscal quarters are ~90 days apart (a 13-week retail calendar
# stretches to ~98). A wider gap means a quarter is missing from the source,
# and a "TTM" summed across it would silently span 15+ months.
MAX_QUARTER_GAP_DAYS = 120

# --- share-count integrity ---------------------------------------------------
# diluted_shares is the one row a bad cell corrupts invisibly: it drives
# dilution and (via run._reprice_market_cap) market cap, hence ev_revenue,
# fcf_yield and the valuation label. Two upstream failure modes have been seen
# on this watchlist, and neither is detectable from a single value:
#   1. Basis breaks. A split rebases every share count Yahoo serves, statements
#      included, retroactively (CRWD 4:1 on 2026-07-02, NOW 5:1 on 2025-12-18).
#      Yahoo only serves ~5 quarters, so quarters that live only in our cache
#      keep the pre-split basis and the row silently mixes the two.
#   2. Scale slips. On 2026-08-07 Yahoo served two of NOW's quarters in
#      thousands (1,034,334) next to neighbours in units (1,047,000,000).
# So the guard uses two independent checks: each cell against the provider's
# current shares outstanding, and each cell against its neighbour.

# Diluted average shares and shares outstanding differ by option overhang,
# buybacks and dual-class counting, never by an order of magnitude: measured
# across this watchlist the spread is 0.98x to 1.16x. The band is deliberately
# a magnitude check, wide enough that four years of honest issuance never trips
# it, tight enough to catch a 4:1 basis break or a units-for-thousands slip.
SHARE_COUNT_BAND = (0.33, 3.0)

# No company changes its diluted count by half in one quarter without a split;
# the widest genuine step on this watchlist is a stock-funded acquisition at
# +13%. A bigger step means everything older than it sits on an unknown basis.
MAX_QUARTER_SHARE_STEP = 1.5


def _anchor_offset(stmts: pd.DataFrame) -> int:
    """Index of the newest column where every core field is present.

    Scans only the first MAX_ANCHOR_SKIP + 1 columns; 0 when the newest
    column is already complete, or when no nearby column is (then the normal
    insufficient-data degradation applies unchanged).
    """
    for i, col in enumerate(stmts.columns[: MAX_ANCHOR_SKIP + 1]):
        if all(f in stmts.index and pd.notna(stmts.loc[f, col]) for f in CORE_FIELDS):
            return i
    return 0


def _contiguous(cols: list[pd.Timestamp]) -> bool:
    """True when consecutive (newest-first) quarter columns have no gap."""
    return all(
        (a - b).days <= MAX_QUARTER_GAP_DAYS for a, b in zip(cols[:-1], cols[1:])
    )


def normalize_statements(
    income: pd.DataFrame | None,
    cashflow: pd.DataFrame | None,
    balance: pd.DataFrame | None,
) -> pd.DataFrame:
    """Raw yfinance statement frames -> canonical frame.

    Rows: CANONICAL_FIELDS. Columns: quarter-end Timestamps, newest first.
    Missing fields become NaN rows; signs normalized to positive magnitudes.
    """
    sources = {"income": income, "cashflow": cashflow, "balance": balance}
    columns: set[pd.Timestamp] = set()
    for df in sources.values():
        if df is not None and not df.empty:
            columns.update(pd.to_datetime(df.columns))
    cols = sorted(columns, reverse=True)
    out = pd.DataFrame(index=CANONICAL_FIELDS, columns=cols, dtype="float64")

    for field, (stmt, aliases) in ALIASES.items():
        src = sources.get(stmt)
        if src is None or src.empty:
            continue
        src = src.copy()
        src.columns = pd.to_datetime(src.columns)
        for alias in aliases:
            if alias in src.index:
                series = pd.to_numeric(src.loc[alias], errors="coerce")
                if isinstance(series, pd.DataFrame):  # duplicated row label
                    series = series.iloc[0]
                if field in NEGATIVE_MAGNITUDE_FIELDS:
                    series = series.abs()
                out.loc[field, series.index.intersection(out.columns)] = series
                break
    return out


def sanitize_share_counts(
    ticker: str,
    stmts: pd.DataFrame,
    shares_outstanding: float | None = None,
    notes: list[str] | None = None,
) -> pd.DataFrame:
    """Blank diluted-share cells that cannot be on the current share basis.

    Returns a copy with the offending cells set to NaN and appends one data
    note per check that fired; the input frame is left alone and nothing here
    raises. Dropping degrades dilution and the repriced market cap to n/a,
    which is the point: a share count on the wrong basis is worse than no
    share count at all, because every number derived from it still looks
    perfectly reasonable.
    """
    notes = notes if notes is not None else []
    if "diluted_shares" not in stmts.index:
        return stmts
    out = stmts.copy()
    row = out.loc["diluted_shares"].copy()

    low, high = SHARE_COUNT_BAND
    if shares_outstanding is not None and shares_outstanding > 0:
        ratio = row / float(shares_outstanding)
        off_scale = row.notna() & ((ratio < low) | (ratio > high))
        if off_scale.any():
            quarters = ", ".join(c.date().isoformat() for c in row.index[off_scale])
            notes.append(
                f"{ticker}: diluted share count implausible against "
                f"{shares_outstanding:,.0f} shares outstanding; dropped ({quarters})"
            )
            row[off_scale] = float("nan")

    # neighbour check: a step splits the row into two internally consistent
    # sides on different bases. The shares-outstanding reference arbitrates
    # which side is on today's basis (recency alone cannot: a corrupt NEWEST
    # cell would win and the guard would destroy the correct history behind
    # it). Without a reference nothing can arbitrate, so every reading drops:
    # n/a beats a coin flip whose wrong side misprices the market cap.
    valid = row.dropna()
    for i in range(len(valid) - 1):
        newer, older = float(valid.iloc[i]), float(valid.iloc[i + 1])
        if newer <= 0 or older <= 0:
            step = float("inf")
        else:
            step = max(newer / older, older / newer)
        if step <= MAX_QUARTER_SHARE_STEP:
            continue
        if shares_outstanding is None or shares_outstanding <= 0:
            notes.append(
                f"{ticker}: diluted share count steps {older:,.0f} to "
                f"{newer:,.0f} at {valid.index[i].date().isoformat()} with no "
                "shares outstanding reference to arbitrate the basis; all "
                f"{len(valid)} share readings dropped"
            )
            row[list(valid.index)] = float("nan")
            break
        newer_side, older_side = valid.iloc[: i + 1], valid.iloc[i + 1 :]

        def _off_basis(side: pd.Series) -> float:
            return abs(math.log(float(side.median()) / float(shares_outstanding)))

        keep_newer = _off_basis(newer_side) <= _off_basis(older_side)
        dropped_side = older_side if keep_newer else newer_side
        which = "older" if keep_newer else "newer"
        label = "quarter" if len(dropped_side) == 1 else "quarters"
        notes.append(
            f"{ticker}: diluted share count steps {older:,.0f} to {newer:,.0f} "
            f"at {valid.index[i].date().isoformat()} (a split or a source "
            f"error, not issuance); {len(dropped_side)} {which} {label} dropped "
            "as a different share basis"
        )
        row[list(dropped_side.index)] = float("nan")
        break

    out.loc["diluted_shares"] = row
    return out


def build_ttm(stmts: pd.DataFrame | None, offset: int = 0) -> TTMWindow | None:
    """Sum of 4 consecutive quarters starting `offset` quarters back.

    Returns None when fewer than offset+4 quarters exist, or when the window's
    columns are not consecutive quarters (a missing quarter would make the
    "TTM" silently span 15+ months). A field is None unless all 4 quarters
    have it (never annualize silently). EBITDA falls back to
    OperatingIncome + D&A when not reported.
    """
    if stmts is None or stmts.shape[1] < offset + 4:
        return None
    window = stmts.iloc[:, offset : offset + 4]
    if not _contiguous(list(window.columns)):
        return None

    def ttm(field: str) -> float | None:
        if field not in window.index:
            return None
        vals = window.loc[field]
        if vals.isna().any():
            return None
        return float(vals.sum())

    ebitda = ttm("ebitda")
    if ebitda is None:
        oi, da = ttm("operating_income"), ttm("d_and_a")
        if oi is not None and da is not None:
            ebitda = oi + da

    return TTMWindow(
        revenue=ttm("revenue"),
        ebitda=ebitda,
        operating_income=ttm("operating_income"),
        ocf=ttm("ocf"),
        capex=ttm("capex"),
        sbc=ttm("sbc"),
    )


def _first_valid(stmts: pd.DataFrame, field: str, start: int = 0) -> float | None:
    """First non-NaN value for a row, scanning newest-first from column `start`."""
    if field not in stmts.index or stmts.shape[1] <= start:
        return None
    for val in stmts.loc[field].iloc[start:]:
        if pd.notna(val):
            return float(val)
    return None


def _paired_shares(stmts: pd.DataFrame) -> tuple[float | None, float | None]:
    """Diluted shares "now" and exactly 4 quarters earlier, from one anchor.

    If the newest quarter lacks the row, anchor on the first quarter that has
    it and take the prior reading 4 quarters behind THAT — the dilution ratio
    must always span a true year, never a mixed 3-quarter window.
    """
    if "diluted_shares" not in stmts.index:
        return None, None
    row = stmts.loc["diluted_shares"]
    for i in range(len(row)):
        if pd.notna(row.iloc[i]):
            prior = row.iloc[i + 4] if len(row) > i + 4 else None
            return (
                float(row.iloc[i]),
                float(prior) if prior is not None and pd.notna(prior) else None,
            )
    return None, None


def inputs_from_canonical(
    ticker: str,
    stmts: pd.DataFrame,
    market_cap: float | None,
    annual_revenue: dict[date, float] | None = None,
    notes: list[str] | None = None,
    company_name: str | None = None,
) -> FundamentalInputs:
    """Canonical statements frame -> FundamentalInputs for the indicator engine.

    Leading partially-populated quarters (core fields missing) are skipped, up
    to MAX_ANCHOR_SKIP columns, so the TTM windows anchor on the newest
    COMPLETE quarter with a data note: a ticker with usable history must
    degrade to "scored as of the prior quarter", never to insufficient_data.
    A gap in the quarterly history gets its own note (the affected TTM
    windows return None via the contiguity check in build_ttm).
    """
    notes = notes if notes is not None else []
    anchor = _anchor_offset(stmts)
    if anchor:
        skipped = ", ".join(c.date().isoformat() for c in stmts.columns[:anchor])
        stmts = stmts.iloc[:, anchor:]
        label = "quarter" if anchor == 1 else "quarters"
        notes.append(
            f"{ticker}: newest statement {label} incomplete at the source "
            f"({skipped}); scored as of {stmts.columns[0].date().isoformat()}"
        )
    # note any gap within the span the offset windows consume (offsets 0..8
    # need up to 12 columns): a deep gap silently degrades the trend windows
    # to None, which must read as a source gap, not "insufficient history"
    span = list(stmts.columns[:12])
    if len(span) >= 4 and not _contiguous(span):
        notes.append(
            f"{ticker}: gap in quarterly statement history; TTM windows "
            "spanning the gap are treated as insufficient data"
        )

    statement_date: date | None = None
    if "revenue" in stmts.index:
        rev = stmts.loc["revenue"]
        valid = rev.dropna()
        if not valid.empty:
            statement_date = valid.index.max().date()

    shares_now, shares_1y = _paired_shares(stmts)
    return FundamentalInputs(
        ticker=ticker,
        company_name=company_name,
        ttm_now=build_ttm(stmts, 0),
        ttm_minus_2q=build_ttm(stmts, 2),
        ttm_minus_4q=build_ttm(stmts, 4),
        ttm_minus_6q=build_ttm(stmts, 6),
        ttm_minus_8q=build_ttm(stmts, 8),
        diluted_shares_now=shares_now,
        diluted_shares_1y_ago=shares_1y,
        market_cap=market_cap,
        total_debt=_first_valid(stmts, "total_debt"),
        cash=_first_valid(stmts, "cash"),
        statement_date=statement_date,
        annual_revenue=annual_revenue or {},
        data_notes=notes,
    )


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=20), reraise=True)
def fetch_statements(ticker: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Network fetch: quarterly statements + annual revenue + market cap.

    Public because the one-time backfill tool (sentinel.backfill) needs the
    raw fetch without get_fundamentals' cache write: a dry run must be able to
    seed an in-memory overlap for an uncached bench name and write nothing.
    """
    import yfinance as yf

    t = yf.Ticker(ticker)
    stmts = normalize_statements(t.quarterly_income_stmt, t.quarterly_cashflow, t.quarterly_balance_sheet)

    annual_revenue: dict[str, float] = {}
    try:
        annual = t.income_stmt
        if annual is not None and not annual.empty and "Total Revenue" in annual.index:
            for col, val in annual.loc["Total Revenue"].items():
                if pd.notna(val):
                    annual_revenue[pd.to_datetime(col).date().isoformat()] = float(val)
    except Exception:  # annual revenue is a fallback nicety, never fatal
        pass

    market_cap: float | None = None
    try:
        market_cap = float(t.fast_info["market_cap"])
    except Exception:
        try:
            market_cap = float(t.info.get("marketCap"))
        except Exception:
            pass

    # the reference the share-count guard checks statement cells against: it
    # tracks the same split basis as fast_info's price and market cap, so a
    # statement row on any other basis stands out. "implied" shares first,
    # since sharesOutstanding counts one class only for dual-class names.
    shares_outstanding: float | None = None
    try:
        shares_outstanding = float(t.fast_info["shares"])
    except Exception:
        try:
            info = t.info
            shares_outstanding = float(
                info.get("impliedSharesOutstanding") or info.get("sharesOutstanding")
            )
        except Exception:
            pass

    company_name: str | None = None
    try:
        company_name = t.info.get("shortName") or t.info.get("longName")
    except Exception:
        pass

    next_earnings: str | None = None
    try:
        cal = t.calendar
        dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if dates:
            next_earnings = min(dates).isoformat()
    except Exception:  # calendar is a freshness nicety, never fatal
        pass

    meta = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "market_cap": market_cap,
        "shares_outstanding": shares_outstanding,
        "company_name": company_name,
        "next_earnings": next_earnings,
        "annual_revenue": annual_revenue,
        "source": "yfinance",
    }
    return stmts, meta


def get_fundamentals(ticker: str, force_refresh: bool = False) -> tuple[FundamentalInputs | None, list[str]]:
    """Cache-aware fundamentals: (inputs, notes). Degrades to cache, then to None."""
    notes: list[str] = []
    cached_df, cached_meta = cache.load(ticker)

    # "company_name" in meta doubles as a cache-schema check: entries written
    # before the field existed get refreshed instead of served for a week
    fresh = (
        not force_refresh
        and cache.is_fresh(cached_meta)
        and "company_name" in (cached_meta or {})
    )
    # earnings-aware refresh: once a report date passes, refetch daily until the
    # new quarter shows up (cheaper than waiting out the weekly window)
    if fresh and (cached_meta or {}).get("next_earnings"):
        try:
            if date.today() >= date.fromisoformat(cached_meta["next_earnings"]):
                fresh = False
        except (TypeError, ValueError):
            pass
    refetched = False
    if fresh:
        df, meta = cached_df, cached_meta
    else:
        try:
            fresh_df, meta = fetch_statements(ticker)
            df = cache.merge_statements(cached_df, fresh_df)
            for carry in ("market_cap", "shares_outstanding", "company_name"):
                if meta.get(carry) is None and cached_meta:
                    meta[carry] = cached_meta.get(carry)
            refetched = True
        except Exception as exc:
            log.warning("fundamentals fetch failed for %s: %s", ticker, exc)
            if cached_df is not None:
                notes.append(f"{ticker}: fetch failed, using cached fundamentals ({exc})")
                df, meta = cached_df, cached_meta
            else:
                notes.append(f"{ticker}: fundamentals unavailable ({exc})")
                return None, notes

    # the cache keeps the UNSANITIZED merge: cells inside Yahoo's served
    # window self-correct via combine_first regardless, so persisting the
    # scrub could only ever destroy cache-only history, and a false positive
    # (a stale or wrong shares-outstanding reference) would destroy it
    # permanently. Scrubbing at read time protects every path just the same.
    if refetched:
        try:
            cache.save(ticker, df, meta)
        except Exception as exc:  # a disk problem must not cost us the report
            log.warning("cache save failed for %s: %s", ticker, exc)
            notes.append(f"{ticker}: cache not updated ({exc})")
    df = sanitize_share_counts(ticker, df, (meta or {}).get("shares_outstanding"), notes)

    annual_revenue = {
        date.fromisoformat(k): v for k, v in (meta or {}).get("annual_revenue", {}).items()
    }
    inputs = inputs_from_canonical(
        ticker,
        df,
        (meta or {}).get("market_cap"),
        annual_revenue,
        notes,
        company_name=(meta or {}).get("company_name"),
    )
    return inputs, notes
