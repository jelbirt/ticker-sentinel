"""run.py helpers: market-cap repricing, R40 trend warm-up note, market-holiday disclosure."""
from __future__ import annotations

from datetime import date

import pandas as pd
from pytest import approx

from sentinel.indicators.fundamentals import FundamentalInputs, Scorecard
from sentinel.run import _reprice_market_cap, _stale_session_note, _trend_warmup_note


def _close(ticker: str, price: float) -> pd.DataFrame:
    idx = pd.bdate_range(end="2026-07-07", periods=3)
    return pd.DataFrame({ticker: [price - 2, price - 1, price]}, index=idx)


def test_reprices_from_latest_close():
    inp = FundamentalInputs(ticker="AAA", diluted_shares_now=100.0, market_cap=1.0)
    stale = _reprice_market_cap([inp], _close("AAA", 50.0))
    assert stale == []
    assert inp.market_cap == approx(5000.0)


def test_missing_price_reports_stale_cached_cap():
    inp = FundamentalInputs(ticker="AAA", diluted_shares_now=100.0, market_cap=4200.0)
    stale = _reprice_market_cap([inp], None)
    assert stale == ["AAA"]
    assert inp.market_cap == approx(4200.0)  # cached value survives, but disclosed


def test_missing_shares_reports_stale_cached_cap():
    inp = FundamentalInputs(ticker="AAA", diluted_shares_now=None, market_cap=4200.0)
    stale = _reprice_market_cap([inp], _close("AAA", 50.0))
    assert stale == ["AAA"]


def test_no_cached_cap_nothing_to_disclose():
    # no price AND no cached cap: valuation will show as missing, not stale
    inp = FundamentalInputs(ticker="AAA", diluted_shares_now=100.0, market_cap=None)
    assert _reprice_market_cap([inp], None) == []


class TestTrendWarmupNote:
    """r40_trend needs 12 cached quarters; one aggregate note discloses the gap."""

    @staticmethod
    def _sc(ticker: str, score: float | None, trend: float | None) -> Scorecard:
        return Scorecard(ticker=ticker, score=score, r40_trend=trend)

    def test_counts_only_scored_names(self):
        cards = [
            self._sc("AAA", 60.0, None),
            self._sc("BBB", 55.0, 0.05),
            self._sc("CCC", None, None),  # unscored: has its own note already
        ]
        assert _trend_warmup_note(cards) == (
            "R40 trend warming up: n/a for 1 of 2 scored names "
            "(needs 12 cached quarters; the committed cache deepens by 4 per year)"
        )

    def test_silent_once_every_scored_name_has_a_trend(self):
        cards = [self._sc("AAA", 60.0, 0.02), self._sc("BBB", 55.0, -0.11)]
        assert _trend_warmup_note(cards) is None

    def test_silent_on_an_empty_scorecard_list(self):
        assert _trend_warmup_note([]) is None

    def test_silent_when_nothing_scored_at_all(self):
        assert _trend_warmup_note([self._sc("AAA", None, None)]) is None

    def test_reports_every_scored_name_when_none_have_a_trend(self):
        cards = [self._sc(t, 50.0, None) for t in ("AAA", "BBB", "CCC")]
        assert "n/a for 3 of 3 scored names" in _trend_warmup_note(cards)

    def test_note_has_no_em_or_en_dashes(self):
        note = _trend_warmup_note([self._sc("AAA", 50.0, None)])
        assert "—" not in note and "–" not in note


# --- market-holiday disclosure -------------------------------------------------


def _bench(last_bar: str, n: int = 5) -> pd.Series:
    idx = pd.bdate_range(end=last_bar, periods=n)
    return pd.Series(range(n), index=idx, dtype="float64")


def test_regular_weekday_run_says_nothing():
    # Tuesday 2026-07-07 run, Monday 2026-07-06 bar: 1 day
    assert _stale_session_note(_bench("2026-07-06"), date(2026, 7, 7)) is None


def test_saturday_run_over_a_normal_weekend_says_nothing():
    # the Tue-Sat cadence means Saturday runs see Friday's bar: 1 day
    assert _stale_session_note(_bench("2026-07-03"), date(2026, 7, 4)) is None


def test_monday_run_over_a_normal_weekend_says_nothing():
    # Friday bar seen on Monday is exactly 3 days: the widest normal gap
    assert _stale_session_note(_bench("2026-07-03"), date(2026, 7, 6)) is None


def test_tuesday_after_a_monday_holiday_discloses_the_stale_bar():
    # Monday 2026-07-06 closed, so Tuesday's run re-reports Friday's bar: 4 days
    note = _stale_session_note(_bench("2026-07-03"), date(2026, 7, 7))
    assert note == "no new market session since 2026-07-03 (market holiday?)"


def test_no_price_data_is_not_a_holiday_note():
    # missing prices are their own failure with their own notes; do not crash
    assert _stale_session_note(None, date(2026, 7, 7)) is None
    assert _stale_session_note(pd.Series(dtype="float64"), date(2026, 7, 7)) is None
    all_nan = pd.Series([float("nan")] * 3, index=pd.bdate_range(end="2026-07-03", periods=3))
    assert _stale_session_note(all_nan, date(2026, 7, 7)) is None


def test_trailing_nan_bars_do_not_hide_a_stale_close():
    # a feed that appends empty rows must not read as a fresh session
    idx = pd.bdate_range(end="2026-07-07", periods=3)   # Jul 3, 6, 7
    series = pd.Series([1.0, float("nan"), float("nan")], index=idx)
    note = _stale_session_note(series, date(2026, 7, 7))
    assert note == "no new market session since 2026-07-03 (market holiday?)"


def test_dry_run_does_not_claim_a_market_holiday(tmp_path):
    # fixture prices are frozen at a fixed date, so the check is skipped offline
    from sentinel.run import main

    assert main(["--dry-run", "--no-email", "--out-dir", str(tmp_path)]) == 0
    assert "market holiday" not in (tmp_path / "report.html").read_text()
