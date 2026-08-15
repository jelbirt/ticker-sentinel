"""Pure change-detection logic: snapshots (T3), diffs (T4), deterioration (T5)."""
from __future__ import annotations

import re
from datetime import date

from sentinel.config import ChangesCfg
from sentinel.indicators.fundamentals import Scorecard
from sentinel.indicators.signals import SignalSnapshot
from sentinel.indicators.technicals import TechnicalSnapshot
from sentinel.report.changes import (
    RunSnapshot,
    TickerSnapshot,
    baseline_ok,
    baseline_reference,
    deteriorating,
    deterioration_rows,
    diff_runs,
    select_baselines,
    snapshot_from_scorecards,
)

CFG = ChangesCfg()
ONE_SIGNAL = ChangesCfg(min_signals=1)


def _runs(prior_kw: dict, cur_kw: dict, ticker: str = "AAA"):
    prior = RunSnapshot("2026-08-05", "scheduled", {ticker: TickerSnapshot(**prior_kw)})
    cur = RunSnapshot("2026-08-06", "scheduled", {ticker: TickerSnapshot(**cur_kw)})
    return cur, prior


def _kinds(cs) -> list[str]:
    return [c.kind for c in cs.changes]


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


class TestDiffScoreAndRank:
    def test_composite_move_at_threshold(self):
        cur, prior = _runs({"composite": 50.0}, {"composite": 53.0})
        cs = diff_runs(cur, prior, CFG)
        assert _kinds(cs) == ["score"]
        assert cs.changes[0].direction == "up"

    def test_composite_move_below_threshold_silent(self):
        cur, prior = _runs({"composite": 50.0}, {"composite": 52.9})
        assert diff_runs(cur, prior, CFG).changes == []

    def test_composite_drop_direction_down(self):
        cur, prior = _runs({"composite": 53.0}, {"composite": 50.0})
        assert diff_runs(cur, prior, CFG).changes[0].direction == "down"

    def test_rank_move_at_threshold(self):
        cur, prior = _runs({"rank": 4}, {"rank": 2})
        cs = diff_runs(cur, prior, CFG)
        assert _kinds(cs) == ["rank"]
        assert cs.changes[0].direction == "up"
        assert "4" in cs.changes[0].detail and "2" in cs.changes[0].detail

    def test_rank_move_of_one_silent(self):
        cur, prior = _runs({"rank": 3}, {"rank": 2})
        assert diff_runs(cur, prior, CFG).changes == []

    def test_none_fields_fabricate_nothing(self):
        cur, prior = _runs({}, {"composite": 50.0, "rank": 1, "net_revisions_30d": 5})
        assert diff_runs(cur, prior, CFG).changes == []


class TestDiffFlagsAndTechnicals:
    def test_flag_set_and_cleared(self):
        cur, prior = _runs(
            {"flags": ["dilution", "stale_fundamentals"]},
            {"flags": ["death_cross", "dilution"]},
        )
        cs = diff_runs(cur, prior, CFG)
        assert sorted(_kinds(cs)) == ["flag_cleared", "flag_set"]
        details = {c.kind: c.detail for c in cs.changes}
        assert "death cross" in details["flag_set"]
        assert "stale fundamentals" in details["flag_cleared"]

    def test_r40_trend_sign_change(self):
        cur, prior = _runs({"r40_trend": 0.02}, {"r40_trend": -0.03})
        assert "r40_inflection" in _kinds(diff_runs(cur, prior, CFG))

    def test_r40_trend_crossing_deterioration_threshold(self):
        cur, prior = _runs({"r40_trend": -0.08}, {"r40_trend": -0.12})
        assert "r40_inflection" in _kinds(diff_runs(cur, prior, CFG))

    def test_r40_trend_same_side_silent(self):
        cur, prior = _runs({"r40_trend": -0.02}, {"r40_trend": -0.08})
        assert diff_runs(cur, prior, CFG).changes == []

    def test_trend_state_transition(self):
        cur, prior = _runs({"trend_state": "uptrend"}, {"trend_state": "mixed"})
        cs = diff_runs(cur, prior, CFG)
        assert _kinds(cs) == ["trend_state"]
        assert cs.changes[0].direction == "down"

    def test_new_death_cross(self):
        cur, prior = _runs({"death_cross": False}, {"death_cross": True})
        cs = diff_runs(cur, prior, CFG)
        assert _kinds(cs) == ["new_cross"]
        assert cs.changes[0].direction == "down"

    def test_persisting_cross_not_renoticed(self):
        cur, prior = _runs({"death_cross": True}, {"death_cross": True})
        assert diff_runs(cur, prior, CFG).changes == []


class TestDiffSignals:
    def test_revision_swing_at_threshold(self):
        cur, prior = _runs({"net_revisions_30d": 2}, {"net_revisions_30d": -1})
        cs = diff_runs(cur, prior, CFG)
        assert _kinds(cs) == ["revisions"]
        assert cs.changes[0].direction == "down"

    def test_revision_swing_below_threshold_silent(self):
        cur, prior = _runs({"net_revisions_30d": 2}, {"net_revisions_30d": 0})
        assert diff_runs(cur, prior, CFG).changes == []

    def test_short_interest_new_reading(self):
        cur, prior = _runs(
            {"shares_short": 9_000_000.0}, {"shares_short": 12_000_000.0}
        )
        cs = diff_runs(cur, prior, CFG)
        assert _kinds(cs) == ["short_interest"]
        # direction is a quality signal: shorts rising = worsening = "down"
        assert cs.changes[0].direction == "down"
        assert "33" in cs.changes[0].detail

    def test_short_interest_tiny_change_silent(self):
        cur, prior = _runs({"shares_short": 10_100_000.0}, {"shares_short": 10_000_000.0})
        assert diff_runs(cur, prior, CFG).changes == []


class TestDiffUniverseAndShape:
    def test_universe_added_and_removed(self):
        prior = RunSnapshot("2026-08-05", "scheduled", {"OLD": TickerSnapshot()})
        cur = RunSnapshot("2026-08-06", "scheduled", {"NEW": TickerSnapshot()})
        cs = diff_runs(cur, prior, CFG)
        assert sorted(_kinds(cs)) == ["universe_added", "universe_removed"]

    def test_quiet_day(self):
        cur, prior = _runs({"composite": 50.0}, {"composite": 51.0})
        cs = diff_runs(cur, prior, CFG)
        assert cs.quiet and cs.prior_date == "2026-08-05"

    def test_no_prior_is_not_quiet(self):
        cur = RunSnapshot("2026-08-06", "scheduled", {"AAA": TickerSnapshot()})
        cs = diff_runs(cur, None, CFG)
        assert cs.prior_date is None and cs.changes == [] and not cs.quiet

    def test_sorted_by_composite_move_magnitude(self):
        prior = RunSnapshot(
            "2026-08-05", "scheduled",
            {
                "AAA": TickerSnapshot(composite=50.0),
                "BBB": TickerSnapshot(composite=50.0),
            },
        )
        cur = RunSnapshot(
            "2026-08-06", "scheduled",
            {
                "AAA": TickerSnapshot(composite=54.0),
                "BBB": TickerSnapshot(composite=59.0),
            },
        )
        cs = diff_runs(cur, prior, CFG)
        assert [c.ticker for c in cs.changes] == ["BBB", "AAA"]

    def test_baseline_selection(self):
        runs = [
            RunSnapshot(f"2026-07-{d:02d}", "scheduled", {}) for d in range(1, 9)
        ]  # 8 runs, dates 07-01 .. 07-08
        prior, week_ago, span = select_baselines(runs, today="2026-07-09", week_window=5)
        assert prior.date == "2026-07-08"
        assert week_ago.date == "2026-07-04"  # today vs week_ago = exactly 5 run-steps
        assert span == 5

    def test_baseline_selection_same_day_rerun_skips_today(self):
        runs = [RunSnapshot("2026-08-05", "s", {}), RunSnapshot("2026-08-06", "s", {})]
        prior, week_ago, span = select_baselines(runs, today="2026-08-06", week_window=5)
        assert prior.date == "2026-08-05"
        assert week_ago.date == "2026-08-05" and span == 1  # short history: oldest

    def test_baseline_selection_empty(self):
        assert select_baselines([], today="2026-08-06", week_window=5) == (None, None, 0)


def _det_sc(ticker="CCC", composite=40.0, **kw) -> Scorecard:
    sc = Scorecard(ticker=ticker)
    sc.composite = composite
    for k, v in kw.items():
        setattr(sc, k, v)
    return sc


def _prior_run(date_="2026-08-05", **ticker_kw) -> RunSnapshot:
    return RunSnapshot(date_, "scheduled", {"CCC": TickerSnapshot(**ticker_kw)})


class TestDeteriorationSignals:
    """Each negative signal in isolation (min_signals=1 config)."""

    def test_one_run_drop(self):
        rows = deterioration_rows(
            [_det_sc(composite=46.9)], _prior_run(composite=50.0), None, ONE_SIGNAL
        )
        assert len(rows) == 1 and "fell 3.1" in "; ".join(rows[0].reasons)

    def test_week_drop(self):
        rows = deterioration_rows(
            [_det_sc(composite=44.9)], _prior_run(composite=46.0),
            _prior_run("2026-07-30", composite=50.0), ONE_SIGNAL,
        )
        assert len(rows) == 1
        assert any("week" in r for r in rows[0].reasons)

    def test_r40_trend_level(self):
        rows = deterioration_rows(
            [_det_sc(r40_trend=-0.18)], _prior_run(composite=None), None, ONE_SIGNAL
        )
        assert len(rows) == 1 and any("R40" in r for r in rows[0].reasons)

    def test_technical_breakdown_new_death_cross(self):
        sc = _det_sc(tech=TechnicalSnapshot(trend_state="mixed", death_cross_recent=True))
        rows = deterioration_rows([sc], _prior_run(death_cross=False), None, ONE_SIGNAL)
        assert len(rows) == 1 and any("death cross" in r for r in rows[0].reasons)

    def test_technical_breakdown_downtrend_transition(self):
        sc = _det_sc(tech=TechnicalSnapshot(trend_state="downtrend"))
        rows = deterioration_rows([sc], _prior_run(trend_state="mixed"), None, ONE_SIGNAL)
        assert len(rows) == 1 and any("downtrend" in r for r in rows[0].reasons)

    def test_estimate_cuts(self):
        sc = _det_sc(signals=SignalSnapshot(ticker="CCC", eps_rev_up_30d=1, eps_rev_down_30d=4))
        rows = deterioration_rows([sc], _prior_run(), None, ONE_SIGNAL)
        assert len(rows) == 1 and any("estimate" in r for r in rows[0].reasons)

    def test_worsening_short_interest_mom(self):
        sc = _det_sc(signals=SignalSnapshot(
            ticker="CCC", shares_short=12_000_000, shares_short_prior=9_000_000,
        ))
        rows = deterioration_rows([sc], _prior_run(), None, ONE_SIGNAL)
        assert len(rows) == 1 and any("short interest" in r for r in rows[0].reasons)

    def test_week_baseline_same_as_prior_never_double_counts(self):
        # short history: select_baselines returns the same run as prior and
        # week_ago; one composite drop must yield exactly one reason
        prior = _prior_run(composite=50.0)
        rows = deterioration_rows([_det_sc(composite=44.0)], prior, prior, ONE_SIGNAL)
        assert len(rows) == 1
        assert sum("fell" in r for r in rows[0].reasons) == 1
        assert rows[0].delta_week is None

    def test_flags_null_in_stored_snapshot_tolerated(self):
        run = RunSnapshot.from_dict(
            {"date": "2026-08-05", "run_type": "s", "tickers": {"AAA": {"flags": None}}}
        )
        assert run.tickers["AAA"].flags == []
        cur = RunSnapshot("2026-08-06", "s", {"AAA": TickerSnapshot(flags=["dilution"])})
        assert _kinds(diff_runs(cur, run, CFG)) == ["flag_set"]

    def test_healthy_ticker_absent(self):
        rows = deterioration_rows(
            [_det_sc(composite=55.0, r40_trend=0.05)],
            _prior_run(composite=54.0), None, ONE_SIGNAL,
        )
        assert rows == []


class TestDeteriorationGate:
    def test_single_signal_excluded_at_default_min(self):
        rows = deterioration_rows(
            [_det_sc(composite=46.9)], _prior_run(composite=50.0), None, CFG
        )
        assert rows == []

    def test_two_signals_included(self):
        sc = _det_sc(
            composite=46.9,
            signals=SignalSnapshot(ticker="CCC", eps_rev_up_30d=0, eps_rev_down_30d=3),
        )
        rows = deterioration_rows([sc], _prior_run(composite=50.0), None, CFG)
        assert len(rows) == 1 and len(rows[0].reasons) == 2

    def test_plan6_combo_sufficient_alone(self):
        # R40 fell hard + downtrend confirmation: included even with min_signals=2
        # and no prior-run baseline (level signal alone would not clear the gate)
        sc = _det_sc(
            r40_trend=-0.15, tech=TechnicalSnapshot(trend_state="downtrend")
        )
        rows = deterioration_rows([sc], None, None, CFG)
        assert len(rows) == 1
        assert deteriorating(sc, CFG.deteriorating_r40_trend)

    def test_rows_sorted_most_signals_first(self):
        bad = _det_sc(
            ticker="BAD", composite=44.0, r40_trend=-0.2,
            signals=SignalSnapshot(ticker="BAD", eps_rev_up_30d=0, eps_rev_down_30d=3),
        )
        worse = _det_sc(
            ticker="WRS", composite=40.0, r40_trend=-0.2,
            tech=TechnicalSnapshot(trend_state="downtrend"),
            signals=SignalSnapshot(
                ticker="WRS", eps_rev_up_30d=0, eps_rev_down_30d=5,
                shares_short=13_000_000, shares_short_prior=9_000_000,
            ),
        )
        prior = RunSnapshot(
            "2026-08-05", "scheduled",
            {
                "BAD": TickerSnapshot(composite=48.0),
                "WRS": TickerSnapshot(composite=48.0, trend_state="mixed"),
            },
        )
        rows = deterioration_rows([bad, worse], prior, None, CFG)
        assert [r.ticker for r in rows] == ["WRS", "BAD"]

    def test_reasons_have_no_em_or_en_dashes(self):
        sc = _det_sc(
            composite=40.0, r40_trend=-0.2,
            tech=TechnicalSnapshot(trend_state="downtrend", death_cross_recent=True),
            signals=SignalSnapshot(
                ticker="CCC", eps_rev_up_30d=0, eps_rev_down_30d=5,
                shares_short=13_000_000, shares_short_prior=9_000_000,
            ),
        )
        rows = deterioration_rows(
            [sc], _prior_run(composite=50.0, trend_state="mixed", death_cross=False),
            _prior_run("2026-07-30", composite=52.0), CFG,
        )
        assert rows
        for reason in rows[0].reasons:
            assert not re.search(r"[–—]", reason), reason

    def test_details_have_no_em_or_en_dashes(self):
        cur, prior = _runs(
            {
                "composite": 40.0, "rank": 5, "flags": ["death_cross"],
                "r40_trend": -0.12, "trend_state": "downtrend", "death_cross": True,
                "net_revisions_30d": -4, "shares_short": 13_000_000.0,
            },
            {
                "composite": 50.0, "rank": 1, "flags": [],
                "r40_trend": 0.05, "trend_state": "uptrend", "death_cross": False,
                "net_revisions_30d": 2, "shares_short": 9_000_000.0,
            },
        )
        cs = diff_runs(cur, prior, CFG)
        assert len(cs.changes) >= 7
        for c in cs.changes:
            assert not re.search(r"[–—]", c.detail), c.detail


class TestScoreBasisGuard:
    """Audit fix: composite falls back to F alone when technicals are missing
    (scoring.composite_score), so diffing across that boundary fabricated big
    score and rank moves. Deltas must compare like for like."""

    def test_basis_flip_suppresses_composite_and_rank_fabrication(self):
        # the live CRWD shape: F 18.1, T 85.0, C 44.9; next day prices missing
        cur, prior = _runs(
            dict(composite=44.9, score=18.1, technical_score=85.0, rank=15),
            dict(composite=18.1, score=18.1, technical_score=None, rank=1),
        )
        cs = diff_runs(cur, prior, CFG)
        assert _kinds(cs) == ["score_basis"]
        assert cs.changes[0].direction == "info"
        assert "not comparable" in cs.changes[0].detail

    def test_basis_flip_still_reports_real_f_moves(self):
        cur, prior = _runs(
            dict(composite=44.9, score=18.1, technical_score=85.0, rank=15),
            dict(composite=10.0, score=10.0, technical_score=None, rank=8),
        )
        cs = diff_runs(cur, prior, CFG)
        assert _kinds(cs) == ["score"]
        change = cs.changes[0]
        assert "fundamental score" in change.detail
        assert change.direction == "down"

    def test_technicals_restored_is_info_not_a_gain(self):
        cur, prior = _runs(
            dict(composite=18.1, score=18.1, technical_score=None, rank=1),
            dict(composite=44.9, score=18.1, technical_score=85.0, rank=15),
        )
        cs = diff_runs(cur, prior, CFG)
        assert _kinds(cs) == ["score_basis"]
        assert "restored" in cs.changes[0].detail

    def test_same_basis_keeps_existing_behavior(self):
        cur, prior = _runs(
            dict(composite=50.0, score=50.0, technical_score=60.0, rank=1),
            dict(composite=44.0, score=44.0, technical_score=60.0, rank=4),
        )
        cs = diff_runs(cur, prior, CFG)
        assert set(_kinds(cs)) == {"score", "rank"}

    def test_universe_wide_flip_collapses_to_one_row(self):
        tickers_prior, tickers_cur = {}, {}
        for i, t in enumerate(["AAA", "BBB", "CCC", "DDD"]):
            tickers_prior[t] = TickerSnapshot(
                composite=50.0 + i, score=30.0, technical_score=80.0, rank=i + 1
            )
            tickers_cur[t] = TickerSnapshot(
                composite=30.0, score=30.0, technical_score=None, rank=i + 1
            )
        cs = diff_runs(
            RunSnapshot("2026-08-06", "scheduled", tickers_cur),
            RunSnapshot("2026-08-05", "scheduled", tickers_prior),
            CFG,
        )
        basis = [c for c in cs.changes if c.kind == "score_basis"]
        assert len(basis) == 1
        assert basis[0].ticker == "watchlist"
        assert "4 names" in basis[0].detail and "unavailable" in basis[0].detail

    def test_single_flip_stays_a_per_ticker_row(self):
        tickers_prior, tickers_cur = {}, {}
        for i, t in enumerate(["AAA", "BBB", "CCC", "DDD"]):
            flip = t == "AAA"
            tickers_prior[t] = TickerSnapshot(
                composite=50.0, score=30.0, technical_score=80.0, rank=i + 1
            )
            tickers_cur[t] = TickerSnapshot(
                composite=30.0 if flip else 50.0, score=30.0,
                technical_score=None if flip else 80.0, rank=i + 1,
            )
        cs = diff_runs(
            RunSnapshot("2026-08-06", "scheduled", tickers_cur),
            RunSnapshot("2026-08-05", "scheduled", tickers_prior),
            CFG,
        )
        basis = [c for c in cs.changes if c.kind == "score_basis"]
        assert len(basis) == 1 and basis[0].ticker == "AAA"


class TestUnscoredReasons:
    def test_universe_removed_carries_reason_when_known(self):
        cur = RunSnapshot("2026-08-06", "scheduled", {})
        prior = RunSnapshot("2026-08-05", "scheduled", {"GONE": TickerSnapshot()})
        cs = diff_runs(cur, prior, CFG, {"GONE": "insufficient data"})
        assert cs.changes[0].detail == "dropped from scored universe (insufficient data)"

    def test_bare_detail_when_reason_unknown(self):
        cur = RunSnapshot("2026-08-06", "scheduled", {})
        prior = RunSnapshot("2026-08-05", "scheduled", {"GONE": TickerSnapshot()})
        cs = diff_runs(cur, prior, CFG)
        assert cs.changes[0].detail == "dropped from scored universe"


class TestDeteriorationBasisGuard:
    def test_composite_drop_signals_suppressed_across_basis_change(self):
        sc = _det_sc(composite=18.1, score=18.1)  # technical_score None
        prior = _prior_run(composite=44.9, score=18.1, technical_score=85.0)
        assert deterioration_rows([sc], prior, None, ONE_SIGNAL) == []

    def test_week_drop_suppressed_across_basis_change(self):
        sc = _det_sc(composite=18.1, score=18.1)
        prior = _prior_run(composite=18.5)  # same basis: no 1-run signal
        week = RunSnapshot(
            "2026-08-01", "scheduled",
            {"CCC": TickerSnapshot(composite=44.9, technical_score=85.0)},
        )
        assert deterioration_rows([sc], prior, week, ONE_SIGNAL) == []

    def test_deltas_render_na_when_basis_differs_but_row_qualifies(self):
        sc = _det_sc(
            composite=30.0, r40_trend=-0.2,
            signals=SignalSnapshot(ticker="CCC", eps_rev_up_30d=0, eps_rev_down_30d=3),
        )
        prior = _prior_run(composite=44.0, technical_score=85.0)
        rows = deterioration_rows([sc], prior, None, CFG)
        assert len(rows) == 1
        assert rows[0].delta_1run is None  # renders n/a, not a fabricated -14


class TestBaselineOk:
    """The gate references the PRIOR baseline's tickers, so a watchlist
    expansion (new unscored names) passes while a wholesale outage fails."""

    def test_collapsed_run_rejected(self):
        prior = {f"T{i}" for i in range(20)}
        assert not baseline_ok({"T1", "T2", "T3"}, prior, 0.5)
        assert not baseline_ok(set(), prior, 0.5)

    def test_expansion_with_unscored_new_names_passes(self):
        prior = {f"T{i}" for i in range(12)}
        scored = prior  # all 12 old names scored; 15 new names unscored
        assert baseline_ok(scored, prior, 0.5)

    def test_boundary_exact_fraction_passes(self):
        prior = {f"T{i}" for i in range(20)}
        scored = {f"T{i}" for i in range(10)}
        assert baseline_ok(scored, prior, 0.5)

    def test_empty_reference_is_not_a_gate(self):
        assert baseline_ok(set(), set(), 0.5)


class TestBaselineReference:
    """Review fix: the gate references prior-baseline names restricted to the
    intended universe, so a deliberate watchlist shrink cannot deadlock the
    gate (rejected runs are never saved, so an unrestricted reference could
    never shrink)."""

    def test_deliberate_shrink_does_not_deadlock(self):
        prior = {f"T{i}" for i in range(12)}
        intended = {"T0", "T1", "T2", "T3", "T4"}  # owner cut 12 -> 5
        ref = baseline_reference(prior, intended)
        assert baseline_ok(intended, ref, 0.5)  # all 5 scored: gate passes

    def test_outage_still_fails(self):
        prior = {f"T{i}" for i in range(20)}
        intended = prior  # watchlist unchanged; fetches collapsed
        ref = baseline_reference(prior, intended)
        assert not baseline_ok({"T1"}, ref, 0.5)

    def test_expansion_keeps_old_names_as_reference(self):
        prior = {f"T{i}" for i in range(12)}
        intended = prior | {f"N{i}" for i in range(15)}
        ref = baseline_reference(prior, intended)
        assert ref == prior
        assert baseline_ok(prior, ref, 0.5)  # old names scored, new ones not yet

    def test_first_run_falls_back_to_intended(self):
        assert baseline_reference(None, {"A", "B"}) == {"A", "B"}
        assert baseline_reference(set(), {"A", "B"}) == {"A", "B"}


class TestBasisCollapseMixedDirections:
    def _flip_runs(self, gone: int, restored: int, stable: int):
        prior_t, cur_t = {}, {}
        names = [f"G{i}" for i in range(gone)] + [f"R{i}" for i in range(restored)] \
            + [f"S{i}" for i in range(stable)]
        for i, t in enumerate(names):
            if t.startswith("G"):
                prior_t[t] = TickerSnapshot(composite=50.0, score=30.0,
                                            technical_score=80.0, rank=i + 1)
                cur_t[t] = TickerSnapshot(composite=30.0, score=30.0,
                                          technical_score=None, rank=i + 1)
            elif t.startswith("R"):
                prior_t[t] = TickerSnapshot(composite=30.0, score=30.0,
                                            technical_score=None, rank=i + 1)
                cur_t[t] = TickerSnapshot(composite=50.0, score=30.0,
                                          technical_score=80.0, rank=i + 1)
            else:
                prior_t[t] = TickerSnapshot(composite=40.0, score=40.0,
                                            technical_score=60.0, rank=i + 1)
                cur_t[t] = TickerSnapshot(composite=40.0, score=40.0,
                                          technical_score=60.0, rank=i + 1)
        return (RunSnapshot("2026-08-06", "scheduled", cur_t),
                RunSnapshot("2026-08-05", "scheduled", prior_t))

    def test_mixed_day_collapses_majority_only(self):
        cur, prior = self._flip_runs(gone=5, restored=2, stable=1)
        cs = diff_runs(cur, prior, CFG)
        basis = [c for c in cs.changes if c.kind == "score_basis"]
        agg = [c for c in basis if c.ticker == "watchlist"]
        assert len(agg) == 1
        assert "unavailable for 5 names" in agg[0].detail  # never counts the 2 restored
        assert sum(1 for c in basis if c.ticker.startswith("R")) == 2

    def test_flipped_rows_rank_by_f_move_not_composite_artifact(self):
        # one real F crash on a same-basis name must outrank a mechanical
        # basis-flip shift (which diff_runs itself labeled not comparable)
        prior_t = {
            "REAL": TickerSnapshot(composite=60.0, score=60.0, technical_score=60.0, rank=1),
            "FLIP": TickerSnapshot(composite=44.9, score=18.1, technical_score=85.0, rank=2),
        }
        cur_t = {
            "REAL": TickerSnapshot(composite=45.0, score=45.0, technical_score=60.0, rank=2),
            "FLIP": TickerSnapshot(composite=18.1, score=18.1, technical_score=None, rank=1),
        }
        cs = diff_runs(
            RunSnapshot("2026-08-06", "scheduled", cur_t),
            RunSnapshot("2026-08-05", "scheduled", prior_t), CFG,
        )
        assert cs.changes[0].ticker == "REAL"
