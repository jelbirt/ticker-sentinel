"""run.py helpers - market-cap repricing disclosure, R40 trend warm-up note."""
from __future__ import annotations

import pandas as pd
from pytest import approx

from sentinel.indicators.fundamentals import FundamentalInputs, Scorecard
from sentinel.run import _reprice_market_cap, _trend_warmup_note


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
