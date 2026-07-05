"""Alias layer: raw yfinance-shaped statement frames -> canonical frame."""
from __future__ import annotations

import numpy as np
import pandas as pd
from pytest import approx

from sentinel.data.cache import merge_statements
from sentinel.data.fundamentals import normalize_statements

DATES = pd.to_datetime(["2026-04-30", "2026-01-31", "2025-10-31", "2025-07-31"])


def _income():
    return pd.DataFrame(
        {
            d: {"Total Revenue": 100 + i, "Operating Income": 10 + i, "Diluted Average Shares": 50}
            for i, d in enumerate(DATES)
        }
    )


def _cashflow():
    return pd.DataFrame(
        {
            d: {
                "Operating Cash Flow": 20 + i,
                "Capital Expenditure": -5,  # yfinance reports outflows negative
                "Stock Based Compensation": 8,
                "Depreciation And Amortization": 4,
            }
            for i, d in enumerate(DATES)
        }
    )


def _balance():
    return pd.DataFrame({DATES[0]: {"Total Debt": 500, "Cash And Cash Equivalents": 1500}})


def test_normalize_maps_and_signs():
    out = normalize_statements(_income(), _cashflow(), _balance())
    assert list(out.columns) == sorted(DATES, reverse=True)
    assert out.loc["revenue", DATES[0]] == approx(100)
    assert out.loc["capex", DATES[0]] == approx(5)  # sign normalized to magnitude
    assert out.loc["sbc", DATES[2]] == approx(8)
    assert out.loc["total_debt", DATES[0]] == approx(500)
    assert np.isnan(out.loc["total_debt", DATES[1]])  # balance only has latest quarter
    assert out.loc["ebitda"].isna().all()  # not reported -> NaN row, TTM falls back


def test_alias_fallback_second_name_wins():
    income = _income().rename(index={"Total Revenue": "Operating Revenue"})
    out = normalize_statements(income, _cashflow(), _balance())
    assert out.loc["revenue", DATES[0]] == approx(100)


def test_missing_statement_leaves_nan():
    out = normalize_statements(_income(), None, None)
    assert out.loc["revenue", DATES[0]] == approx(100)
    assert out.loc["ocf"].isna().all()
    assert out.loc["total_debt"].isna().all()


def test_merge_accumulates_history_fresh_wins():
    old_dates = pd.to_datetime(["2025-10-31", "2025-07-31", "2025-04-30"])
    cached = pd.DataFrame({d: {"revenue": 90.0} for d in old_dates}).astype("float64")
    fresh = pd.DataFrame(
        {pd.Timestamp("2026-01-31"): {"revenue": 100.0}, pd.Timestamp("2025-10-31"): {"revenue": 95.0}}
    )
    merged = merge_statements(cached, fresh)
    assert list(merged.columns)[0] == pd.Timestamp("2026-01-31")  # newest first
    assert merged.shape[1] == 4  # union of quarters
    assert merged.loc["revenue", pd.Timestamp("2025-10-31")] == approx(95.0)  # fresh wins
    assert merged.loc["revenue", pd.Timestamp("2025-04-30")] == approx(90.0)  # history kept
