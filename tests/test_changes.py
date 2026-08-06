"""Pure change-detection logic: snapshots (T3), diffs (T4), deterioration (T5)."""
from __future__ import annotations

from datetime import date

from sentinel.indicators.fundamentals import Scorecard
from sentinel.indicators.signals import SignalSnapshot
from sentinel.indicators.technicals import TechnicalSnapshot
from sentinel.report.changes import RunSnapshot, snapshot_from_scorecards


def _sc(ticker: str, **kw) -> Scorecard:
    sc = Scorecard(ticker=ticker)
    for k, v in kw.items():
        setattr(sc, k, v)
    return sc


class TestSnapshot:
    def test_full_capture(self):
        sc = _sc(
            "AAA",
            composite=71.2, score=68.0, technical_score=76.1,
            r40_fcf=0.52, r40_trend=0.03, valuation="fair",
            flags=["passes_all_r40"],
            tech=TechnicalSnapshot(trend_state="uptrend", golden_cross_recent=True),
            signals=SignalSnapshot(
                ticker="AAA", eps_rev_up_30d=6, eps_rev_down_30d=2,
                short_pct_float=0.021, shares_short=2_000_000,
            ),
        )
        run = snapshot_from_scorecards([sc], today=date(2026, 8, 6), run_type="scheduled")
        assert run.date == "2026-08-06" and run.run_type == "scheduled"
        t = run.tickers["AAA"]
        assert t.composite == 71.2 and t.rank == 1
        assert t.trend_state == "uptrend" and t.golden_cross and not t.death_cross
        assert t.net_revisions_30d == 4
        assert t.short_pct_float == 0.021 and t.shares_short == 2_000_000
        assert t.flags == ["passes_all_r40"]

    def test_rank_follows_list_order(self):
        run = snapshot_from_scorecards(
            [_sc("AAA", composite=70.0), _sc("BBB", composite=60.0)],
            today=date(2026, 8, 6), run_type="ad hoc",
        )
        assert run.tickers["AAA"].rank == 1
        assert run.tickers["BBB"].rank == 2

    def test_missing_everything_stays_none(self):
        run = snapshot_from_scorecards([_sc("AAA")], today=date(2026, 8, 6), run_type="dry")
        t = run.tickers["AAA"]
        assert t.composite is None and t.trend_state is None
        assert t.net_revisions_30d is None and t.shares_short is None
        assert t.golden_cross is False and t.death_cross is False

    def test_dict_round_trip_preserves_null_keys(self):
        run = snapshot_from_scorecards([_sc("AAA")], today=date(2026, 8, 6), run_type="dry")
        d = run.to_dict()
        # nulls are stored explicitly, never omitted (spec 2.2)
        assert d["tickers"]["AAA"]["composite"] is None
        back = RunSnapshot.from_dict(d)
        assert back == run

    def test_from_dict_tolerates_unknown_and_missing_keys(self):
        d = {
            "date": "2026-08-05",
            "run_type": "scheduled",
            "tickers": {"AAA": {"composite": 50.0, "someday_new_field": 1}},
        }
        run = RunSnapshot.from_dict(d)
        assert run.tickers["AAA"].composite == 50.0
        assert run.tickers["AAA"].rank is None
