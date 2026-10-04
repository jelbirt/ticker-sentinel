"""Twelve Data fallback: free-tier pacing, price basis, and history depth."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

from sentinel.data import prices


@pytest.fixture()
def fallback_env(monkeypatch):
    """Yahoo fully down, Twelve Data answering, sleeps recorded not slept.

    Yields a namespace with `naps` (recorded sleep durations) and `bars` (the
    bar count the fallback asked for, once per recovered ticker).
    """
    monkeypatch.setenv("TWELVEDATA_API_KEY", "k")
    monkeypatch.setattr(
        prices, "_yf_download", lambda tickers, period: (_ for _ in ()).throw(OSError("down"))
    )
    env = SimpleNamespace(naps=[], bars=[])

    def fake_series(ticker, api_key, bars=prices.DEFAULT_BARS):
        env.bars.append(bars)
        idx = pd.bdate_range(end="2026-08-05", periods=3)
        return (
            pd.Series([1.0, 2.0, 3.0], index=idx, name=ticker),
            pd.Series([10.0, 10.0, 10.0], index=idx, name=ticker),
        )

    monkeypatch.setattr(prices, "_twelvedata_series", fake_series)
    monkeypatch.setattr(prices.time, "sleep", env.naps.append)
    return env


def test_small_fallback_is_unpaced(fallback_env):
    tickers = [f"T{i}" for i in range(8)]
    close, volume, notes = prices.fetch_prices(tickers)
    assert fallback_env.naps == []
    assert set(close.columns) == set(tickers)


def test_large_fallback_paces_to_free_tier(fallback_env):
    tickers = [f"T{i}" for i in range(26)]
    close, volume, notes = prices.fetch_prices(tickers)
    # no sleep before the first call, one before each of the other 25
    assert len(fallback_env.naps) == 25
    assert all(nap == pytest.approx(60 / 8) for nap in fallback_env.naps)
    assert set(close.columns) == set(tickers)          # everything still recovered
    assert any("Twelve Data" in n for n in notes)


# --- history depth: the fallback must honour the period the run asked for -------------


def test_period_maps_to_a_bar_count():
    # ~252 trading days a year, plus slack for holidays and reporting lag
    assert prices._bars_for_period("1y") == 260
    assert prices._bars_for_period("2y") == 520


def test_unmapped_period_falls_back_to_the_shallow_depth(caplog):
    assert prices._bars_for_period("5y") == prices.DEFAULT_BARS
    assert "5y" in caplog.text


@pytest.mark.parametrize(("period", "expected"), [("1y", 260), ("2y", 520)])
def test_fallback_requests_the_depth_the_run_asked_for(fallback_env, period, expected):
    """A --deep run (2y) must not silently recover half the history."""
    prices.fetch_prices(["AAA", "BBB"], period=period)
    assert fallback_env.bars == [expected, expected]


# --- price basis: Twelve Data defaults to split-only, yfinance is total return --------


def test_series_request_asks_for_the_yfinance_price_basis(monkeypatch):
    """yfinance runs auto_adjust=True, so the fallback must ask for adjust=all.

    Twelve Data's default is adjust=splits, and it answers 200 with default-basis
    data when it does not honour the value, so the parameter must be sent
    explicitly and pinned here.
    """
    seen: dict = {}

    def fake_get(url, params, timeout):
        seen["url"] = url
        seen["params"] = params
        return SimpleNamespace(
            json=lambda: {
                "values": [
                    {"datetime": "2026-08-05", "close": "2.0", "volume": "10"},
                    {"datetime": "2026-08-04", "close": "1.0", "volume": "10"},
                ]
            }
        )

    monkeypatch.setattr(prices.requests, "get", fake_get)
    close, volume = prices._twelvedata_series("AAA", "k", bars=520)

    assert seen["params"]["adjust"] == "all"
    assert prices.TWELVEDATA_ADJUST == "all"
    assert seen["params"]["outputsize"] == 520
    assert seen["params"]["symbol"] == "AAA"
    assert seen["params"]["interval"] == "1day"
    assert list(close) == [1.0, 2.0]                   # returned oldest-first


def test_fallback_note_records_the_basis(fallback_env):
    _, _, notes = prices.fetch_prices(["AAA"])
    note = next(n for n in notes if "Twelve Data" in n)
    assert "split and dividend adjusted basis requested" in note
    assert "AAA" in note
    assert "—" not in note and "–" not in note        # no em or en dashes in output


def _frames(last_day: str):
    idx = pd.bdate_range(end=last_day, periods=3)
    close = pd.DataFrame({"AAA": [1.0, 2.0, 3.0], "SPY": [4.0, 5.0, 6.0]}, index=idx)
    return close, close * 10


def _ny(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp).replace(tzinfo=prices.MARKET_TZ)


def test_an_unfinished_same_day_bar_is_dropped_and_disclosed():
    close, volume = _frames("2026-10-02")
    close, volume, note = prices.drop_unfinished_session(
        close, volume, _ny("2026-10-02T12:46")
    )
    assert close.index[-1] == pd.Timestamp("2026-10-01")
    assert volume.index[-1] == pd.Timestamp("2026-10-01")
    assert note == (
        "today's unfinished session (2026-10-02, run at 12:46 New York time) "
        "dropped; technicals scored on the 2026-10-01 close"
    )
    assert "\u2014" not in note and "\u2013" not in note


@pytest.mark.parametrize(
    "last_day, now",
    [
        ("2026-10-02", "2026-10-02T16:30"),   # session settled: today's bar is the close
        ("2026-10-01", "2026-10-02T01:23"),   # pre-market: no same-day bar exists
    ],
)
def test_a_settled_or_absent_same_day_bar_is_kept(last_day, now):
    close, volume = _frames(last_day)
    kept, kept_volume, note = prices.drop_unfinished_session(close, volume, _ny(now))
    assert note is None
    assert kept.equals(close) and kept_volume.equals(volume)


def test_the_session_is_judged_in_new_york_time():
    # 01:00 UTC on 10-03 is 21:00 on 10-02 in New York: the 10-02 bar is settled
    close, volume = _frames("2026-10-02")
    now = datetime(2026, 10, 3, 1, 0, tzinfo=timezone.utc)
    assert prices.drop_unfinished_session(close, volume, now)[2] is None


def test_fetch_prices_applies_the_session_check(monkeypatch):
    close, volume = _frames("2026-10-02")
    monkeypatch.setattr(prices, "_yf_download", lambda tickers, period: (close, volume))
    got, _, notes = prices.fetch_prices(["AAA", "SPY"], now=_ny("2026-10-02T10:00"))
    assert got.index[-1] == pd.Timestamp("2026-10-01")
    assert any("unfinished session" in n for n in notes)
