"""Twelve Data fallback pacing: the free tier allows 8 requests/min."""
from __future__ import annotations

import pandas as pd
import pytest

from sentinel.data import prices


@pytest.fixture()
def fallback_env(monkeypatch):
    """Yahoo fully down, Twelve Data answering, sleeps recorded not slept."""
    monkeypatch.setenv("TWELVEDATA_API_KEY", "k")
    monkeypatch.setattr(
        prices, "_yf_download", lambda tickers, period: (_ for _ in ()).throw(OSError("down"))
    )

    def fake_series(ticker, api_key, bars=260):
        idx = pd.bdate_range(end="2026-08-05", periods=3)
        return (
            pd.Series([1.0, 2.0, 3.0], index=idx, name=ticker),
            pd.Series([10.0, 10.0, 10.0], index=idx, name=ticker),
        )

    monkeypatch.setattr(prices, "_twelvedata_series", fake_series)
    naps: list[float] = []
    monkeypatch.setattr(prices.time, "sleep", naps.append)
    return naps


def test_small_fallback_is_unpaced(fallback_env):
    tickers = [f"T{i}" for i in range(8)]
    close, volume, notes = prices.fetch_prices(tickers)
    assert fallback_env == []
    assert set(close.columns) == set(tickers)


def test_large_fallback_paces_to_free_tier(fallback_env):
    tickers = [f"T{i}" for i in range(26)]
    close, volume, notes = prices.fetch_prices(tickers)
    # no sleep before the first call, one before each of the other 25
    assert len(fallback_env) == 25
    assert all(nap == pytest.approx(60 / 8) for nap in fallback_env)
    assert set(close.columns) == set(tickers)          # everything still recovered
    assert any("Twelve Data" in n for n in notes)
