from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from sentinel.data.fixtures import load_fixture_inputs
from sentinel.data.fundamentals import CANONICAL_FIELDS
from sentinel.indicators.fundamentals import FundamentalInputs, compute_scorecard

FIXED_TODAY = date(2026, 7, 5)  # keeps staleness checks deterministic


def make_canonical(fields: dict[str, list[float]], n_quarters: int | None = None) -> pd.DataFrame:
    """Canonical statements frame from per-field quarterly lists (latest first)."""
    n = n_quarters or max(len(v) for v in fields.values())
    latest = pd.Timestamp("2026-04-30")
    cols = pd.DatetimeIndex([latest - pd.DateOffset(months=3 * i) for i in range(n)])
    df = pd.DataFrame(index=CANONICAL_FIELDS, columns=cols, dtype="float64")
    for field, values in fields.items():
        df.loc[field, cols[: len(values)]] = values
    return df


@pytest.fixture(scope="session")
def fixture_inputs() -> dict[str, FundamentalInputs]:
    return {inp.ticker: inp for inp in load_fixture_inputs()}


@pytest.fixture(scope="session")
def fixture_scorecards(fixture_inputs):
    return {
        ticker: compute_scorecard(inp, today=FIXED_TODAY)
        for ticker, inp in fixture_inputs.items()
    }
