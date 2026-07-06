"""Fixture data loader for --dry-run and tests: no network, deterministic inputs."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from sentinel.data.fundamentals import CANONICAL_FIELDS, inputs_from_canonical
from sentinel.indicators.fundamentals import FundamentalInputs

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"

FIXTURE_BENCHMARK = "SPY"
_PRICE_BARS = 300
_PRICE_END = pd.Timestamp("2026-07-02")


def synthetic_prices() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Deterministic daily close/volume for fixture tickers + benchmark (no randomness).

    ALFA: steady uptrend. BRVO: drifting/oscillating (ambiguous trend).
    CHRL: flat then breaking down, with a recent volume spike.
    """
    idx = pd.bdate_range(end=_PRICE_END, periods=_PRICE_BARS)
    i = np.arange(_PRICE_BARS, dtype="float64")
    close = pd.DataFrame(
        {
            "ALFA": 100.0 * (1 + 0.002 * i),
            "BRVO": 100.0 + 0.05 * i + 4.0 * np.sin(i / 6.0),
            "CHRL": np.where(i < 240, 100.0, 100.0 - 0.5 * (i - 240)),
            FIXTURE_BENCHMARK: 100.0 * (1 + 0.0005 * i),
        },
        index=idx,
    )
    volume = pd.DataFrame(1_000_000.0, index=idx, columns=close.columns)
    volume.iloc[-20:, volume.columns.get_loc("CHRL")] = 2_500_000.0  # unusual-volume alert
    return close, volume


def canonical_from_fixture(raw: dict) -> pd.DataFrame:
    cols = pd.to_datetime(raw["dates"])
    df = pd.DataFrame(index=CANONICAL_FIELDS, columns=cols, dtype="float64")
    for field, values in raw["fields"].items():
        df.loc[field] = [float(v) for v in values]
    # balance-sheet point values only exist for the latest quarter in fixtures
    if raw.get("total_debt") is not None:
        df.loc["total_debt"] = np.nan
        df.iloc[df.index.get_loc("total_debt"), 0] = float(raw["total_debt"])
    if raw.get("cash") is not None:
        df.loc["cash"] = np.nan
        df.iloc[df.index.get_loc("cash"), 0] = float(raw["cash"])
    return df


def load_fixture_inputs(fixtures_dir: Path = FIXTURES_DIR) -> list[FundamentalInputs]:
    inputs = []
    for path in sorted(fixtures_dir.glob("*.json")):
        raw = json.loads(path.read_text())
        df = canonical_from_fixture(raw)
        inputs.append(
            inputs_from_canonical(
                raw["ticker"],
                df,
                raw.get("market_cap"),
                notes=[f"{raw['ticker']}: fixture data ({raw.get('profile', '')})"],
                company_name=raw.get("name"),
            )
        )
    return inputs
