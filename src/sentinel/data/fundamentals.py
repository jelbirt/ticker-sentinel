"""Quarterly statements via yfinance: alias-mapped normalization, TTM builder, cache.

All yfinance I/O lives here. Row labels from yfinance drift between versions —
change ALIASES, never scatter string literals elsewhere.
"""
from __future__ import annotations

import logging
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


def build_ttm(stmts: pd.DataFrame | None, offset: int = 0) -> TTMWindow | None:
    """Sum of 4 consecutive quarters starting `offset` quarters back.

    Returns None when fewer than offset+4 quarters exist. A field is None unless
    all 4 quarters have it (never annualize silently). EBITDA falls back to
    OperatingIncome + D&A when not reported.
    """
    if stmts is None or stmts.shape[1] < offset + 4:
        return None
    window = stmts.iloc[:, offset : offset + 4]

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


def _column_at(stmts: pd.DataFrame, field: str, col: int) -> float | None:
    if field not in stmts.index or stmts.shape[1] <= col:
        return None
    val = stmts.loc[field].iloc[col]
    return float(val) if pd.notna(val) else None


def inputs_from_canonical(
    ticker: str,
    stmts: pd.DataFrame,
    market_cap: float | None,
    annual_revenue: dict[date, float] | None = None,
    notes: list[str] | None = None,
    company_name: str | None = None,
) -> FundamentalInputs:
    """Canonical statements frame -> FundamentalInputs for the indicator engine."""
    statement_date: date | None = None
    if "revenue" in stmts.index:
        rev = stmts.loc["revenue"]
        valid = rev.dropna()
        if not valid.empty:
            statement_date = valid.index.max().date()

    return FundamentalInputs(
        ticker=ticker,
        company_name=company_name,
        ttm_now=build_ttm(stmts, 0),
        ttm_minus_2q=build_ttm(stmts, 2),
        ttm_minus_4q=build_ttm(stmts, 4),
        ttm_minus_6q=build_ttm(stmts, 6),
        ttm_minus_8q=build_ttm(stmts, 8),
        diluted_shares_now=_column_at(stmts, "diluted_shares", 0)
        or _first_valid(stmts, "diluted_shares"),
        diluted_shares_1y_ago=_column_at(stmts, "diluted_shares", 4),
        market_cap=market_cap,
        total_debt=_first_valid(stmts, "total_debt"),
        cash=_first_valid(stmts, "cash"),
        statement_date=statement_date,
        annual_revenue=annual_revenue or {},
        data_notes=notes or [],
    )


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=20), reraise=True)
def _fetch_statements(ticker: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Network fetch: quarterly statements + annual revenue + market cap."""
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

    company_name: str | None = None
    try:
        company_name = t.info.get("shortName") or t.info.get("longName")
    except Exception:
        pass

    meta = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "market_cap": market_cap,
        "company_name": company_name,
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
    if not force_refresh and cache.is_fresh(cached_meta) and "company_name" in (cached_meta or {}):
        df, meta = cached_df, cached_meta
    else:
        try:
            fresh_df, meta = _fetch_statements(ticker)
            df = cache.merge_statements(cached_df, fresh_df)
            for carry in ("market_cap", "company_name"):
                if meta.get(carry) is None and cached_meta:
                    meta[carry] = cached_meta.get(carry)
            cache.save(ticker, df, meta)
        except Exception as exc:
            log.warning("fundamentals fetch failed for %s: %s", ticker, exc)
            if cached_df is not None:
                notes.append(f"{ticker}: fetch failed, using cached fundamentals ({exc})")
                df, meta = cached_df, cached_meta
            else:
                notes.append(f"{ticker}: fundamentals unavailable ({exc})")
                return None, notes

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
