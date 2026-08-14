"""run.py helpers — market-cap repricing staleness disclosure."""
from __future__ import annotations

import pandas as pd
from pytest import approx

from sentinel.indicators.fundamentals import FundamentalInputs
from sentinel.run import _reprice_market_cap


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
    def _sc(self, score, trend):
        from sentinel.indicators.fundamentals import Scorecard

        sc = Scorecard(ticker="X")
        sc.score, sc.r40_trend = score, trend
        return sc

    def test_counts_only_scored_trendless_names(self):
        from sentinel.run import _trend_warmup_note

        cards = [
            self._sc(50.0, None),      # scored, no trend -> counted
            self._sc(60.0, 0.05),      # scored, trend present
            self._sc(None, None),      # unscored: not part of the disclosure
        ]
        note = _trend_warmup_note(cards)
        assert note is not None
        assert "1 of 2" in note and "n/a" in note

    def test_silent_when_every_scored_name_has_trend(self):
        from sentinel.run import _trend_warmup_note

        assert _trend_warmup_note([self._sc(50.0, 0.02)]) is None
        assert _trend_warmup_note([]) is None
