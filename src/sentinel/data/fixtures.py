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


def _target_latest_close(ticker: str) -> float | None:
    """Latest close implied by the fixture's market_cap / diluted_shares_now, so
    _reprice_market_cap() lands back on the fixture's own market_cap instead of
    a distorted one built from raw (unscaled) synthetic price levels."""
    path = FIXTURES_DIR / f"{ticker}.json"
    if not path.exists():
        return None
    raw = json.loads(path.read_text())
    market_cap = raw.get("market_cap")
    shares = raw.get("fields", {}).get("diluted_shares")
    if market_cap is None or not shares:
        return None
    return float(market_cap) / float(shares[0])


def synthetic_prices() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Deterministic daily close/volume for fixture tickers + benchmark (no randomness).

    ALFA: steady uptrend. BRVO: drifting/oscillating (ambiguous trend).
    CHRL: flat then breaking down, with a recent volume spike.

    Each ticker's series is scaled so its latest close, times the fixture's
    diluted_shares_now, reproduces the fixture's own market_cap - preserving
    the trend/oscillation shape while keeping valuation labels sane.
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
    for ticker in close.columns:
        if ticker == FIXTURE_BENCHMARK:
            continue
        target = _target_latest_close(ticker)
        if target is not None:
            close[ticker] *= target / close[ticker].iloc[-1]
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


def fixture_signals() -> dict[str, "SignalSnapshot"]:
    """Deterministic between-quarter signals matching the three fixture profiles."""
    from sentinel.indicators.signals import SignalSnapshot

    return {
        "ALFA": SignalSnapshot(  # loved: estimates up, insiders buying
            ticker="ALFA",
            eps_rev_up_7d=2, eps_rev_up_30d=6, eps_rev_down_7d=0, eps_rev_down_30d=0,
            rec_bullish=20, rec_neutral=3, rec_bearish=1,
            rec_bullish_prior=18, rec_bearish_prior=1,
            short_pct_float=0.02, shares_short=2_000_000, shares_short_prior=2_100_000,
            insider_net_shares_6m=150_000, insider_buy_txns_6m=12, insider_sell_txns_6m=3,
            sources=["fixture"],
        ),
        "BRVO": SignalSnapshot(  # short-squeeze bait: shorts piling in
            ticker="BRVO",
            eps_rev_up_7d=0, eps_rev_up_30d=1, eps_rev_down_7d=1, eps_rev_down_30d=1,
            rec_bullish=8, rec_neutral=6, rec_bearish=2,
            rec_bullish_prior=8, rec_bearish_prior=2,
            short_pct_float=0.12, shares_short=12_000_000, shares_short_prior=9_000_000,
            insider_net_shares_6m=-400_000, insider_buy_txns_6m=1, insider_sell_txns_6m=14,
            sources=["fixture"],
        ),
        "CHRL": SignalSnapshot(  # deteriorating: estimates cut, analysts bailing
            ticker="CHRL",
            eps_rev_up_7d=0, eps_rev_up_30d=0, eps_rev_down_7d=2, eps_rev_down_30d=4,
            rec_bullish=2, rec_neutral=5, rec_bearish=4,
            rec_bullish_prior=4, rec_bearish_prior=3,
            short_pct_float=0.08, shares_short=7_000_000, shares_short_prior=6_500_000,
            insider_net_shares_6m=-900_000, insider_buy_txns_6m=0, insider_sell_txns_6m=20,
            sources=["fixture"],
        ),
    }


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
