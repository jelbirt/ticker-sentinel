"""Phase 2.5 between-quarter signals — pure logic + offline parsing of source shapes."""
from __future__ import annotations

import pandas as pd
from pytest import approx

from sentinel.data.signals import (
    parse_eps_revisions,
    parse_insider_purchases,
    parse_recommendations,
    parse_short_info,
)
from sentinel.indicators.signals import (
    SignalSnapshot,
    net_revisions_30d,
    short_change_mom,
    signal_alerts,
)


class TestPureHelpers:
    def test_net_revisions(self):
        s = SignalSnapshot(ticker="X", eps_rev_up_30d=6, eps_rev_down_30d=2)
        assert net_revisions_30d(s) == 4
        assert net_revisions_30d(SignalSnapshot(ticker="X")) is None

    def test_short_change_mom(self):
        assert short_change_mom(12_000_000, 9_000_000) == approx(1 / 3)
        assert short_change_mom(None, 9e6) is None
        assert short_change_mom(1e6, 0) is None


class TestAlerts:
    def test_estimates_up_alert(self):
        s = SignalSnapshot(ticker="X", eps_rev_up_30d=6, eps_rev_down_30d=0)
        assert any("revised up by 6" in a for a in signal_alerts(s))

    def test_estimates_down_alert(self):
        s = SignalSnapshot(ticker="X", eps_rev_up_30d=0, eps_rev_down_30d=4)
        assert any("revised down" in a for a in signal_alerts(s))

    def test_no_alert_when_quiet(self):
        s = SignalSnapshot(
            ticker="X", eps_rev_up_30d=2, eps_rev_down_30d=1,
            shares_short=1_000_000, shares_short_prior=1_050_000, short_pct_float=0.03,
            insider_net_shares_6m=-50_000,
            rec_bullish=10, rec_bullish_prior=10,
        )
        assert signal_alerts(s) == []

    def test_short_spike_and_heavily_shorted(self):
        s = SignalSnapshot(
            ticker="X", shares_short=12_000_000, shares_short_prior=9_000_000,
            short_pct_float=0.12,
        )
        alerts = signal_alerts(s)
        assert any("short interest up 33%" in a for a in alerts)
        assert any("heavily shorted (12.0% of float)" in a for a in alerts)

    def test_insider_buying_alert(self):
        s = SignalSnapshot(ticker="X", insider_net_shares_6m=150_000)
        assert any("net-buying" in a for a in signal_alerts(s))
        assert signal_alerts(SignalSnapshot(ticker="X", insider_net_shares_6m=-150_000)) == []

    def test_bullishness_slipping(self):
        s = SignalSnapshot(ticker="X", rec_bullish=2, rec_bullish_prior=4)
        assert any("slipping (4 → 2" in a for a in signal_alerts(s))


class TestParsers:
    """Frames shaped exactly like yfinance 1.5.1 output (verified by live probe)."""

    def test_eps_revisions(self):
        df = pd.DataFrame(
            {
                "upLast7days": [1, 1], "upLast30days": [37, 36],
                "downLast30days": [0, 0], "downLast7Days": [0, 1],
            },
            index=pd.Index(["0q", "+1q"], name="period"),
        )
        out = parse_eps_revisions(df)
        assert out == {
            "eps_rev_up_7d": 1, "eps_rev_up_30d": 37,
            "eps_rev_down_30d": 0, "eps_rev_down_7d": 0,
        }
        assert parse_eps_revisions(None) == {}
        assert parse_eps_revisions(pd.DataFrame()) == {}

    def test_recommendations(self):
        df = pd.DataFrame(
            {
                "period": ["0m", "-1m", "-2m"],
                "strongBuy": [10, 10, 10], "buy": [33, 32, 34],
                "hold": [1, 2, 2], "sell": [1, 1, 1], "strongSell": [1, 2, 1],
            }
        )
        out = parse_recommendations(df)
        assert out["rec_bullish"] == 43
        assert out["rec_neutral"] == 1
        assert out["rec_bearish"] == 2
        assert out["rec_bullish_prior"] == 42
        assert out["rec_bearish_prior"] == 3

    def test_insider_purchases(self):
        df = pd.DataFrame(
            {
                "Insider Purchases Last 6m": [
                    "Purchases", "Sales", "Net Shares Purchased (Sold)",
                    "Total Insider Shares Held",
                ],
                "Shares": [2_993_963.0, 2_260_540.0, 733_423.0, 2_299_503.0],
                "Trans": [66, 67, 133, pd.NA],
            }
        )
        out = parse_insider_purchases(df)
        assert out["insider_net_shares_6m"] == approx(733_423.0)
        assert out["insider_buy_txns_6m"] == 66
        assert out["insider_sell_txns_6m"] == 67

    def test_short_info(self):
        out = parse_short_info(
            {
                "sharesShort": 14_614_591, "sharesShortPriorMonth": 15_608_515,
                "shortPercentOfFloat": 0.0509, "dateShortInterest": 1_781_481_600,
            }
        )
        assert out["shares_short"] == approx(14_614_591)
        assert out["short_pct_float"] == approx(0.0509)
        assert out["short_interest_date"].year == 2026
        assert parse_short_info(None) == {}
        assert parse_short_info({}) == {}  # falsy info short-circuits: nothing to merge
