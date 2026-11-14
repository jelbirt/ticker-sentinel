"""Weekly refresh digest: window aggregation, attention gating, rendering."""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pytest

from sentinel.config import ChangesCfg
from sentinel.digest import (
    BUSINESS_FLAGS,
    CALIBRATION_REFRESHES,
    DATA_QUALITY_FLAGS,
    build_digest,
    classify_flags,
    main,
    render_json,
    render_markdown,
    snapshot_decaying,
)
from sentinel.report.changes import RunSnapshot, TickerSnapshot

CFG = ChangesCfg()


def _run(day: int, tickers: dict[str, TickerSnapshot]) -> RunSnapshot:
    return RunSnapshot(f"2026-08-{day:02d}", "scheduled", tickers)


def _healthy(rank: int = 1, composite: float = 70.0) -> TickerSnapshot:
    return TickerSnapshot(
        composite=composite, rank=rank, r40_trend=0.05, trend_state="uptrend",
    )


def _decaying(rank: int = 2, composite: float = 40.0) -> TickerSnapshot:
    return TickerSnapshot(
        composite=composite, rank=rank, r40_trend=-0.20, trend_state="downtrend",
        flags=["high_sbc"],
    )


class TestDecayGate:
    def test_needs_both_r40_fall_and_technical_confirmation(self):
        assert snapshot_decaying(_decaying(), CFG.deteriorating_r40_trend)
        no_tech = TickerSnapshot(r40_trend=-0.20, trend_state="uptrend")
        assert not snapshot_decaying(no_tech, CFG.deteriorating_r40_trend)
        no_fall = TickerSnapshot(r40_trend=0.02, trend_state="downtrend")
        assert not snapshot_decaying(no_fall, CFG.deteriorating_r40_trend)

    def test_death_cross_confirms_without_downtrend(self):
        snap = TickerSnapshot(r40_trend=-0.20, trend_state="mixed", death_cross=True)
        assert snapshot_decaying(snap, CFG.deteriorating_r40_trend)

    def test_unknown_r40_never_decays(self):
        snap = TickerSnapshot(r40_trend=None, trend_state="downtrend")
        assert not snapshot_decaying(snap, CFG.deteriorating_r40_trend)


class TestAttentionList:
    def test_persistent_decliner_makes_the_list_with_streak_count(self):
        runs = [
            _run(10, {"AAA": _healthy(), "BBB": _decaying(composite=42.0)}),
            _run(11, {"AAA": _healthy(), "BBB": _decaying(composite=41.0)}),
            _run(12, {"AAA": _healthy(), "BBB": _decaying(composite=40.0)}),
        ]
        digest = build_digest(runs, ["AAA", "BBB"], [], CFG)
        assert [w.ticker for w in digest.attention] == ["BBB"]
        week = digest.attention[0]
        assert week.decay_hits == 3 and week.runs_seen == 3
        assert week.composite_delta is not None and abs(week.composite_delta + 2.0) < 1e-9

    def test_single_bad_run_stays_off_the_list(self):
        runs = [
            _run(11, {"BBB": _healthy(rank=2, composite=70.0)}),
            _run(12, {"BBB": _decaying(composite=68.0)}),  # small drop, one gate hit
        ]
        digest = build_digest(runs, ["BBB"], [], CFG)
        assert digest.attention == []

    def test_week_scale_composite_drop_qualifies_without_decay_gate(self):
        runs = [
            _run(11, {"CCC": _healthy(composite=70.0)}),
            _run(12, {"CCC": _healthy(composite=70.0 - CFG.week_drop_pts)}),
        ]
        digest = build_digest(runs, ["CCC"], [], CFG)
        assert [w.ticker for w in digest.attention] == ["CCC"]

    def test_degraded_none_composite_run_does_not_null_week_drop_evidence(self):
        # a rate-limited run persists composite=None; the week delta must
        # anchor on the observed values around it, not go silent
        runs = [
            _run(10, {"DDD": TickerSnapshot(composite=None, rank=1)}),
            _run(11, {"DDD": _healthy(composite=70.0)}),
            _run(12, {"DDD": _healthy(composite=70.0 - CFG.week_drop_pts - 1.0)}),
        ]
        digest = build_digest(runs, ["DDD"], [], CFG)
        assert [w.ticker for w in digest.attention] == ["DDD"]
        assert digest.attention[0].composite_delta is not None

    def test_window_bounded_by_config(self):
        cfg = ChangesCfg(week_window_runs=2)
        runs = [
            _run(9, {"BBB": _decaying()}),   # outside the 2-run window
            _run(11, {"BBB": _healthy()}),
            _run(12, {"BBB": _healthy()}),
        ]
        digest = build_digest(runs, ["BBB"], [], cfg)
        assert digest.runs_in_window == 2 and digest.window_start == "2026-08-11"
        assert digest.attention == []


class TestChangeActivityAndCoverage:
    def test_change_counts_aggregate_across_window_pairs(self):
        runs = [
            _run(11, {"AAA": TickerSnapshot(composite=70.0, rank=1)}),
            _run(12, {"AAA": TickerSnapshot(composite=60.0, rank=1)}),  # -10 crossing
            _run(13, {"AAA": TickerSnapshot(composite=50.0, rank=1)}),  # -10 crossing
        ]
        digest = build_digest(runs, ["AAA"], [], CFG)
        assert digest.change_counts.get("score") == 2
        assert digest.busiest[0] == ("AAA", 2)
        assert digest.attention[0].down_changes == 2

    def test_coverage_flags_missing_and_unconfigured_tickers(self):
        runs = [
            _run(11, {"AAA": _healthy(), "GONE": _healthy(rank=2)}),
            _run(12, {"AAA": _healthy()}),
        ]
        digest = build_digest(runs, ["AAA", "GONE", "NEVER"], [], CFG)
        gaps = " ".join(g.text for g in digest.coverage_gaps)
        assert "NEVER: configured but absent from every run" in gaps
        assert "GONE: missing from the latest run (2026-08-12)" in gaps

    def test_stale_history_ticker_reported(self):
        runs = [_run(12, {"AAA": _healthy(), "OLD": _healthy(rank=2)})]
        digest = build_digest(runs, ["AAA"], [], CFG)
        assert any(g.text.startswith("OLD: in run history") for g in digest.coverage_gaps)

    def test_empty_history_degrades_to_note_not_crash(self):
        digest = build_digest([], ["AAA", "BBB"], ["WDAY"], CFG)
        assert digest.runs_in_window == 0
        assert "no run history yet" in digest.coverage_gaps[0].text


class TestCoverageUniverse:
    """build_from_files measures coverage against the scored (r40) names only."""

    def _files(self, tmp_path: Path, universe: str) -> tuple[Path, Path]:
        cfg_path = tmp_path / "watchlist.yaml"
        cfg_path.write_text("universe:\n" + universe)
        hist = tmp_path / "run_history.json"
        hist.write_text(json.dumps({
            "version": 1,
            "runs": [
                _run(11, {"CRWD": _healthy()}).to_dict(),
                _run(12, {"CRWD": _healthy()}).to_dict(),
            ],
        }))
        return cfg_path, hist

    def test_tech_only_ticker_is_not_a_coverage_gap(self, tmp_path: Path):
        from sentinel.digest import build_from_files

        # XOM is configured but not r40-tagged, so it is never scored and never
        # reaches run history: flagging it weekly would be a standing false alarm
        cfg_path, hist = self._files(
            tmp_path,
            "  - ticker: CRWD\n    tags: [software, r40]\n"
            "  - ticker: XOM\n    tags: [energy]\n",
        )
        digest, _ = build_from_files(cfg_path, hist)
        assert digest.coverage_gaps == []

    def test_scored_ticker_absent_from_history_is_still_a_gap(self, tmp_path: Path):
        from sentinel.digest import build_from_files

        cfg_path, hist = self._files(
            tmp_path,
            "  - ticker: CRWD\n    tags: [software, r40]\n"
            "  - ticker: DDOG\n    tags: [software, r40]\n",
        )
        digest, _ = build_from_files(cfg_path, hist)
        assert any(
            g.text.startswith("DDOG: configured but absent from every run")
            for g in digest.coverage_gaps
        )


class TestCoverageStreaks:
    """SPEC 7.0.1 round-1 gap 1: a 2-run absence and a 1-run blip must read
    differently, in line with digest_decay_runs."""

    def _gap(self, digest, ticker):
        return next(g for g in digest.coverage_gaps if g.ticker == ticker)

    def test_one_run_blip_reads_as_a_blip(self):
        runs = [
            _run(10, {"AAA": _healthy(), "TEAM": _healthy(rank=2)}),
            _run(11, {"AAA": _healthy(), "TEAM": _healthy(rank=2)}),
            _run(12, {"AAA": _healthy()}),
        ]
        gap = self._gap(build_digest(runs, ["AAA", "TEAM"], [], CFG), "TEAM")
        assert gap.kind == "missing_streak" and gap.streak == 1
        assert gap.text == "TEAM: missing from the latest run (2026-08-12)"

    def test_two_run_streak_reads_as_a_streak(self):
        runs = [
            _run(10, {"AAA": _healthy(), "TEAM": _healthy(rank=2)}),
            _run(11, {"AAA": _healthy()}),
            _run(12, {"AAA": _healthy()}),
        ]
        gap = self._gap(build_digest(runs, ["AAA", "TEAM"], [], CFG), "TEAM")
        assert gap.streak == 2 and gap.runs_seen == 1 and gap.runs_in_window == 3
        assert "missing from the latest 2 runs (2026-08-11 to 2026-08-12)" in gap.text
        assert "seen in 1 of 3 runs" in gap.text

    def test_an_earlier_gap_that_healed_is_not_reported(self):
        runs = [
            _run(10, {"AAA": _healthy()}),
            _run(11, {"AAA": _healthy(), "TEAM": _healthy(rank=2)}),
            _run(12, {"AAA": _healthy(), "TEAM": _healthy(rank=2)}),
        ]
        digest = build_digest(runs, ["AAA", "TEAM"], [], CFG)
        assert digest.coverage_gaps == []

    def test_absent_from_every_run_carries_the_window_length(self):
        runs = [_run(11, {"AAA": _healthy()}), _run(12, {"AAA": _healthy()})]
        gap = self._gap(build_digest(runs, ["AAA", "NEVER"], [], CFG), "NEVER")
        assert gap.kind == "absent_all" and gap.streak == 2 and gap.runs_seen == 0
        assert "(2 runs), check listing status" in gap.text


class TestDecayStreak:
    def test_consecutive_hits_are_a_streak(self):
        runs = [
            _run(10, {"BBB": _decaying(composite=44.0)}),
            _run(11, {"BBB": _decaying(composite=43.0)}),
            _run(12, {"BBB": _decaying(composite=42.0)}),
        ]
        week = build_digest(runs, ["BBB"], [], CFG).attention[0]
        assert week.decay_hits == 3 and week.decay_streak == 3

    def test_scattered_hits_are_not_a_streak(self):
        # same hit count, very different evidence: the gate still counts hits,
        # the streak is what tells the owner it is persistent
        runs = [
            _run(10, {"BBB": _decaying(composite=44.0)}),
            _run(11, {"BBB": _healthy(rank=2, composite=44.0)}),
            _run(12, {"BBB": _decaying(composite=44.0)}),
        ]
        week = build_digest(runs, ["BBB"], [], CFG).attention[0]
        assert week.decay_hits == 2 and week.decay_streak == 1

    def test_a_missing_run_breaks_the_streak(self):
        # an absence is not evidence the gate held
        runs = [
            _run(10, {"BBB": _decaying(composite=44.0)}),
            _run(11, {"AAA": _healthy()}),
            _run(12, {"BBB": _decaying(composite=44.0)}),
        ]
        week = next(
            w for w in build_digest(runs, ["AAA", "BBB"], [], CFG).attention
            if w.ticker == "BBB"
        )
        assert week.decay_hits == 2 and week.decay_streak == 1


class TestFlagClassification:
    def test_data_quality_and_business_flags_are_split(self):
        quality, business = classify_flags(
            ["insufficient_history", "high_sbc", "growth_from_annual", "dilution"]
        )
        assert quality == ["insufficient_history", "growth_from_annual"]
        assert business == ["high_sbc", "dilution"]

    def test_unknown_flags_land_in_the_business_column(self):
        assert classify_flags(["brand_new_flag"]) == ([], ["brand_new_flag"])

    def test_every_flag_the_codebase_emits_is_classified(self):
        # guard: a new flag constant must be classified deliberately, not
        # silently default into the business column
        import sentinel.indicators.fundamentals as f
        import sentinel.scoring as s

        emitted = {
            getattr(mod, name) for mod in (f, s)
            for name in dir(mod) if name.startswith("FLAG_")
        }
        assert emitted, "no FLAG_ constants found; the guard would be vacuous"
        classified = DATA_QUALITY_FLAGS | BUSINESS_FLAGS
        assert emitted <= classified, sorted(emitted - classified)

    def test_attention_table_separates_the_two_columns(self):
        decaying_mixed = TickerSnapshot(
            composite=40.0, rank=2, r40_trend=-0.20, trend_state="downtrend",
            flags=["high_sbc", "insufficient_history"],
        )
        runs = [
            _run(11, {"BBB": decaying_mixed}),
            _run(12, {"BBB": decaying_mixed}),
        ]
        digest = build_digest(runs, ["BBB"], [], CFG)
        week = digest.attention[0]
        assert week.flags_business == ["high_sbc"]
        assert week.flags_data_quality == ["insufficient_history"]
        text = render_markdown(digest, 1, date(2026, 8, 15))
        assert "| business flags | data-quality flags |" in text
        assert "| high sbc | insufficient history |" in text


class TestBenchSection:
    def _bench_runs(self):
        return [
            RunSnapshot("2026-08-11", "scheduled", {"AAA": _healthy()},
                        {"WDAY": TickerSnapshot(composite=61.0, flags=["dilution"])}),
            RunSnapshot("2026-08-12", "scheduled", {"AAA": _healthy()},
                        {"WDAY": TickerSnapshot(composite=57.0, flags=["dilution"])}),
        ]

    def test_bench_week_is_first_vs_last_in_window(self):
        digest = build_digest(self._bench_runs(), ["AAA"], ["WDAY"], CFG)
        assert len(digest.bench_weeks) == 1
        b = digest.bench_weeks[0]
        assert b.ticker == "WDAY" and b.runs_seen == 2
        assert b.composite_first == 61.0 and b.composite_last == 57.0
        assert b.composite_delta == pytest.approx(-4.0)
        assert b.flags_latest == ["dilution"]

    def test_bench_table_renders_next_to_the_names(self):
        text = render_markdown(
            build_digest(self._bench_runs(), ["AAA"], ["WDAY"], CFG),
            1, date(2026, 8, 15),
        )
        assert "WDAY." in text                      # the configured-names line
        assert "| WDAY | 2 of 2 | 57.0 | -4.0 | n/a | n/a | n/a | n/a | dilution |" in text
        assert "compare directly with the attention list" in text

    def test_configured_bench_name_without_snapshots_is_named(self):
        digest = build_digest(self._bench_runs(), ["AAA"], ["WDAY", "ZM"], CFG)
        assert [b.ticker for b in digest.bench_weeks] == ["WDAY"]
        text = render_markdown(digest, 1, date(2026, 8, 15))
        assert "No snapshots this window for: ZM" in text

    def test_bench_absent_from_history_is_a_warm_up_note(self):
        # every run predating the feature has no bench block at all
        runs = [_run(11, {"AAA": _healthy()}), _run(12, {"AAA": _healthy()})]
        digest = build_digest(runs, ["AAA"], ["WDAY", "SHOP"], CFG)
        assert digest.bench_weeks == []
        text = render_markdown(digest, 1, date(2026, 8, 15))
        assert "warming up rather than failing" in text
        assert "WDAY, SHOP." in text                # names still listed

    def test_bench_never_reaches_the_attention_list_or_coverage(self):
        runs = [
            RunSnapshot("2026-08-11", "scheduled", {"AAA": _healthy()},
                        {"WDAY": _decaying(composite=40.0)}),
            RunSnapshot("2026-08-12", "scheduled", {"AAA": _healthy()},
                        {"WDAY": _decaying(composite=20.0)}),
        ]
        digest = build_digest(runs, ["AAA"], ["WDAY"], CFG)
        assert digest.attention == []          # decaying hard, still not ranked
        assert digest.coverage_gaps == []      # not a scored name, not a gap
        assert digest.change_counts == {}      # never diffed

    def test_bench_order_follows_config_then_leftovers(self):
        runs = [
            RunSnapshot("2026-08-12", "scheduled", {"AAA": _healthy()}, {
                "ZM": TickerSnapshot(composite=30.0),
                "WDAY": TickerSnapshot(composite=61.0),
                "OLDBENCH": TickerSnapshot(composite=50.0),
            }),
        ]
        digest = build_digest(runs, ["AAA"], ["WDAY", "ZM"], CFG)
        assert [b.ticker for b in digest.bench_weeks] == ["WDAY", "ZM", "OLDBENCH"]


def _scored(
    score: float | None, r40_trend: float | None = 0.05, flags: list[str] | None = None,
    r40_fcf: float | None = None, rank: int | None = None,
) -> TickerSnapshot:
    return TickerSnapshot(
        composite=score, score=score, rank=rank, r40_fcf=r40_fcf,
        r40_trend=r40_trend, trend_state="uptrend", flags=list(flags or []),
    )


def _level_runs(n_runs: int, per_run) -> list[RunSnapshot]:
    """n_runs runs, dated 08-01 onward; per_run(i) -> tickers for run i."""
    return [_run(1 + i, per_run(i)) for i in range(n_runs)]


class TestLevelRule:
    # CFG: week_window_runs 5, weak_bottom_n 3, weak_windows 3, so 15 runs hold
    # exactly three full-window ends at indexes 14, 9 and 4
    BASE = {"AAA": 90.0, "BBB": 80.0, "CCC": 10.0, "DDD": 20.0, "EEE": 30.0}
    ENDS = (4, 9, 14)

    def _runs(self, n_runs: int = 15, override=None) -> list[RunSnapshot]:
        def per_run(i):
            scores = dict(self.BASE)
            if override:
                scores.update(override(i))
            return {t: _scored(v) for t, v in scores.items()}
        return _level_runs(n_runs, per_run)

    def test_bottom_three_qualify_after_three_window_ends(self):
        digest = build_digest(self._runs(), list(self.BASE), [], CFG)
        rows = {w.ticker: w for w in digest.level_watch}
        assert set(rows) == {"CCC", "DDD", "EEE"}
        assert all(w.qualifies and w.windows == 3 for w in rows.values())
        assert all(w.windows_available == 3 and w.windows_needed == 3 for w in rows.values())
        assert [w.ticker for w in digest.level_watch] == ["CCC", "DDD", "EEE"]  # score order

    def test_only_window_ends_count(self):
        # DDD is high everywhere except the three window ends: still 3 windows.
        # EEE is bottom-3 everywhere except the middle window end: streak of 1.
        def override(i):
            out = {}
            if i not in self.ENDS:
                out["DDD"] = 95.0
            if i == 9:
                out["EEE"] = 95.0
            return out
        digest = build_digest(self._runs(override=override), list(self.BASE), [], CFG)
        rows = {w.ticker: w for w in digest.level_watch}
        assert rows["DDD"].qualifies and rows["DDD"].windows == 3
        assert not rows["EEE"].qualifies and rows["EEE"].windows == 1
        assert [w.ticker for w in digest.level_watch][:2] == ["CCC", "DDD"]  # qualifiers first

    def test_warm_up_holds_a_slot_but_never_qualifies(self):
        def per_run(i):
            t = {k: _scored(v) for k, v in self.BASE.items()}
            t["DDD"] = _scored(20.0, r40_trend=None, flags=["insufficient_history"])
            t["EEE"] = _scored(30.0, r40_trend=0.02, flags=["growth_from_annual", "high_sbc"])
            return t
        digest = build_digest(_level_runs(15, per_run), list(self.BASE), [], CFG)
        rows = {w.ticker: w for w in digest.level_watch}
        assert set(rows) == {"CCC", "DDD", "EEE"}      # slots are held regardless
        assert rows["CCC"].qualifies
        assert not rows["DDD"].eligible and rows["DDD"].windows == 0
        assert not rows["EEE"].eligible                # a data-quality flag disqualifies
        assert rows["EEE"].flags_business == ["high_sbc"]
        text = render_markdown(digest, 7, date(2026, 9, 26))
        assert "| DDD | 20.0 | n/a | n/a | not eligible (insufficient history) |" in text
        assert "not eligible (growth from annual)" in text

    def test_decay_and_level_rows_each_get_their_own_subheading(self):
        # the parent heading names neither row kind: the level table is not decay
        digest = build_digest(
            _level_runs(15, lambda i: {k: _scored(v) for k, v in self.BASE.items()}),
            list(self.BASE), [], CFG,
        )
        text = render_markdown(digest, 7, date(2026, 9, 26))
        assert "## Attention list\n" in text
        assert "(persistent decay)" not in text
        assert text.index("### Persistent decay") < text.index("### Persistently weak")

    def test_liveness_is_checked_at_every_window_end(self):
        # went live only at the latest window end: one window, not three
        def per_run(i):
            t = {k: _scored(v) for k, v in self.BASE.items()}
            if i < 14:
                t["CCC"] = _scored(10.0, r40_trend=None, flags=["insufficient_history"])
            return t
        digest = build_digest(_level_runs(15, per_run), list(self.BASE), [], CFG)
        ccc = next(w for w in digest.level_watch if w.ticker == "CCC")
        assert ccc.eligible and ccc.windows == 1 and not ccc.qualifies

    def test_missing_r40_trend_without_a_flag_reads_as_such(self):
        def per_run(i):
            t = {k: _scored(v) for k, v in self.BASE.items()}
            t["CCC"] = _scored(10.0, r40_trend=None)
            return t
        digest = build_digest(_level_runs(15, per_run), list(self.BASE), [], CFG)
        text = render_markdown(digest, 7, date(2026, 9, 26))
        assert "| CCC | 10.0 | n/a | n/a | not eligible (r40 trend n/a) |" in text

    def test_tie_at_the_boundary_is_broken_by_ticker_every_time(self):
        # two names at the 0.0 clamp plus a third tie at the boundary: the same
        # name is excluded at every window end, so streaks cannot flip-flop
        scores = {"AAA": 90.0, "CCC": 0.0, "DDD": 0.0, "EEE": 20.0, "FFF": 20.0}
        digest = build_digest(
            _level_runs(15, lambda i: {t: _scored(v) for t, v in scores.items()}),
            list(scores), [], CFG,
        )
        assert [(w.ticker, w.windows) for w in digest.level_watch] == [
            ("CCC", 3), ("DDD", 3), ("EEE", 3)
        ]

    def test_unknown_score_never_ranks(self):
        def per_run(i):
            t = {k: _scored(v) for k, v in self.BASE.items()}
            t["CCC"] = _scored(None)
            return t
        digest = build_digest(_level_runs(15, per_run), list(self.BASE), [], CFG)
        assert [w.ticker for w in digest.level_watch] == ["DDD", "EEE", "BBB"]
        assert digest.level_watch[0].scored_latest == 4
        text = render_markdown(digest, 7, date(2026, 9, 26))
        assert "Bottom 3 of the 4 names scored on the latest run" in text

    def test_only_full_windows_count_as_ends(self):
        # 11 runs: indexes 10 and 5 end full windows; index 0 does not (the
        # first run ever recorded was no digest's window end)
        digest = build_digest(self._runs(n_runs=11), list(self.BASE), [], CFG)
        rows = {w.ticker: w for w in digest.level_watch}
        assert rows["CCC"].windows == 2 and rows["CCC"].windows_available == 2
        assert not rows["CCC"].qualifies
        text = render_markdown(digest, 2, date(2026, 8, 8))
        assert "| CCC | 10.0 | n/a | n/a | 2 of 3 windows |" in text
        assert "History holds 2 window end(s) and the rule needs 3" in text

    def test_short_history_cannot_qualify(self):
        digest = build_digest(self._runs(n_runs=6), list(self.BASE), [], CFG)
        rows = {w.ticker: w for w in digest.level_watch}
        assert rows["CCC"].windows == 1 and rows["CCC"].windows_available == 1
        assert build_digest(self._runs(n_runs=4), list(self.BASE), [], CFG).level_watch == []

    def test_thresholds_come_from_config(self):
        cfg = ChangesCfg(weak_windows=2, weak_bottom_n=1)
        digest = build_digest(self._runs(n_runs=10), list(self.BASE), [], cfg)
        assert [(w.ticker, w.qualifies) for w in digest.level_watch] == [("CCC", True)]

    def test_r40_fcf_pass_fail_beside_the_score(self):
        def per_run(i):
            t = {k: _scored(v) for k, v in self.BASE.items()}
            t["CCC"] = _scored(10.0, r40_fcf=0.239, rank=20, flags=["dilution", "high_sbc"])
            t["DDD"] = _scored(20.0, r40_fcf=0.4866, rank=19)
            return t
        digest = build_digest(_level_runs(15, per_run), list(self.BASE), [], CFG)
        rows = {w.ticker: w for w in digest.level_watch}
        assert rows["CCC"].r40_fcf_passes is False and rows["DDD"].r40_fcf_passes is True
        assert rows["EEE"].r40_fcf_passes is None
        text = render_markdown(digest, 7, date(2026, 9, 26))
        assert "| CCC | 10.0 | 23.9 fail | 20 | qualifies (3 of 3 windows) | dilution, high sbc |" in text
        assert "| DDD | 20.0 | 48.7 pass | 19 | qualifies (3 of 3 windows) | n/a |" in text
        assert "qualifies and is a swap candidate" in text
        assert "At most one swap per round, longest-standing qualifier first" in text
        assert "growing 15% or more that would rank above the bottom 3 on entry" in text
        assert "(a slower grower only when none does), otherwise refresh the bench" in text
        assert "r40 fcf is context, not a hold" in text
        assert "Surfacing only" not in text

    def test_r40_cell_never_reads_as_40_point_0_with_a_verdict(self):
        from sentinel.digest import _r40_cell
        assert _r40_cell(0.3999) == "39.99 fail"
        assert _r40_cell(0.4001) == "40.01 pass"
        assert _r40_cell(0.40) == "40.00 pass"
        assert _r40_cell(0.239) == "23.9 fail"

    def test_bench_table_carries_the_fundamental_leg(self):
        runs = [
            RunSnapshot("2026-08-12", "scheduled", {"AAA": _healthy()}, {
                "SHOP": TickerSnapshot(composite=45.9, score=62.7, r40_fcf=0.502,
                                       flags=["passes_all_r40"]),
            }),
            # degraded last run: every bench field null; the row must keep the
            # last observed fundamental beside the last observed composite
            RunSnapshot("2026-08-13", "scheduled", {"AAA": _healthy()}, {
                "SHOP": TickerSnapshot(),
            }),
        ]
        digest = build_digest(runs, ["AAA"], ["SHOP"], CFG)
        assert digest.bench_weeks[0].score_last == 62.7
        text = render_markdown(digest, 7, date(2026, 9, 26))
        assert "| SHOP | 2 of 2 | 45.9 | n/a | 62.7 | 50.2 pass | n/a |" in text

    def test_json_carries_the_rows(self):
        digest = build_digest(self._runs(), list(self.BASE), [], CFG)
        payload = json.loads(render_json(digest, 7, date(2026, 9, 26)))
        rows = {r["ticker"]: r for r in payload["level_watch"]}
        assert rows["CCC"]["qualifies"] is True and rows["CCC"]["windows"] == 3
        assert payload["bench_weeks"] == []

    def test_empty_history_has_no_rows(self):
        assert build_digest([], ["AAA"], [], CFG).level_watch == []


class TestSwapCheck:
    """SPEC 7.0.1 condition 2 with the round-15 growth preference: the swap
    takes the best growth-tier bench name that clears the tail on entry, a
    slower grower only when none can, and no swap when nothing clears."""

    # same shape as TestLevelRule: CCC, DDD, EEE qualify after 15 runs and
    # CCC (lowest score) is the first qualifier
    BASE = {"AAA": 90.0, "BBB": 80.0, "CCC": 10.0, "DDD": 20.0, "EEE": 30.0}

    def _runs(self, bench: dict[str, tuple[float | None, float | None]],
              n_runs: int = 15) -> list[RunSnapshot]:
        def snap(score, growth):
            return TickerSnapshot(composite=score, score=score, growth=growth,
                                  r40_trend=0.05)
        return [
            RunSnapshot(
                f"2026-08-{1 + i:02d}", "scheduled",
                {t: _scored(v) for t, v in self.BASE.items()},
                {t: snap(s, g) for t, (s, g) in bench.items()},
            )
            for i in range(n_runs)
        ]

    def _digest(self, bench, n_runs: int = 15):
        return build_digest(self._runs(bench, n_runs), list(self.BASE), list(bench), CFG)

    def test_growth_name_beats_a_higher_scoring_slow_grower(self):
        # after CCC leaves, GRO (85) sits above DDD, EEE and BBB: clears
        d = self._digest({"SLOW": (95.0, 0.10), "GRO": (85.0, 0.20)})
        assert d.swap_outgoing == "CCC" and d.swap_outgoing_qualifies
        assert d.swap_pick == "GRO"
        rows = {b.ticker: b for b in d.bench_weeks}
        assert rows["GRO"].growth_tier == "growth" and rows["GRO"].clears_tail
        assert rows["SLOW"].growth_tier == "fallback" and rows["SLOW"].clears_tail
        assert (d.bench_clearing, d.bench_clearing_growth) == (2, 1)
        text = render_markdown(d, 15, date(2026, 8, 15))
        assert "Swap check: CCC qualifies" in text
        assert "The rule promotes GRO." in text
        assert "| 20.0 growth | yes |" in text

    def test_promoted_or_unbenched_name_in_the_window_is_not_an_entrant(self):
        # a mid-week swap leaves bench snapshots of the promoted name (BBB,
        # now in the universe) and of a name dropped from the bench (OLD) in
        # the window: neither can be swapped in
        runs = self._runs({"BBB": (99.0, 0.30), "OLD": (98.0, 0.30), "GRO": (85.0, 0.20)})
        d = build_digest(runs, list(self.BASE), ["GRO"], CFG)
        rows = {b.ticker: b for b in d.bench_weeks}
        assert set(rows) == {"BBB", "OLD", "GRO"}
        assert rows["BBB"].clears_tail is None and rows["OLD"].clears_tail is None
        assert d.swap_pick == "GRO" and d.bench_clearing == 1

    def test_unknown_growth_pick_is_said_as_unknown_not_slow(self):
        # history recorded before the growth field: the tier is n/a
        d = self._digest({"OLDREC": (85.0, None)})
        assert d.swap_pick == "OLDREC"
        text = render_markdown(d, 15, date(2026, 8, 15))
        assert "its growth is n/a this window" in text
        assert "a slower grower: no" not in text

    def test_slow_grower_is_the_fallback_when_no_growth_name_clears(self):
        # GRO at 25 has only DDD below it once CCC leaves: does not clear
        d = self._digest({"SLOW": (95.0, 0.10), "GRO": (25.0, 0.20)})
        assert d.swap_pick == "SLOW"
        text = render_markdown(d, 15, date(2026, 8, 15))
        assert "The rule promotes SLOW, a slower grower" in text

    def test_nothing_clears_means_refresh_the_bench(self):
        d = self._digest({"GRO": (25.0, 0.20), "SLOW": (15.0, 0.05)})
        assert d.swap_pick is None and d.bench_clearing == 0
        text = render_markdown(d, 15, date(2026, 8, 15))
        assert "no swap this round: refresh the bench" in text
        assert "python -m sentinel.screen" in text

    def test_a_tie_is_not_above(self):
        # 80.0 ties BBB: only DDD and EEE are strictly below, so it fails
        d = self._digest({"TIE": (80.0, 0.20)})
        assert d.bench_weeks[0].clears_tail is False

    def test_unknown_growth_is_never_growth_tier(self):
        d = self._digest({"UNK": (95.0, None), "GRO": (85.0, 0.20)})
        rows = {b.ticker: b for b in d.bench_weeks}
        assert rows["UNK"].growth_tier is None
        assert d.swap_pick == "GRO"
        assert "| UNK |" in render_markdown(d, 15, date(2026, 8, 15))

    def test_threshold_comes_from_config(self):
        cfg = ChangesCfg(swap_min_growth=0.25)
        runs = self._runs({"SLOW": (95.0, 0.10), "GRO": (85.0, 0.20)})
        d = build_digest(runs, list(self.BASE), ["SLOW", "GRO"], cfg)
        assert d.swap_pick == "SLOW"           # 20% is below a 25% floor
        assert "25% or more" in render_markdown(d, 15, date(2026, 8, 15))

    def test_no_qualifier_reports_bench_depth_as_a_what_if(self):
        # 4 runs: no full window end yet, so nothing qualifies
        d = self._digest({"SLOW": (95.0, 0.10)}, n_runs=4)
        assert not d.swap_outgoing_qualifies and d.swap_outgoing == "CCC"
        assert d.swap_pick == "SLOW"           # what the rule would do
        text = render_markdown(d, 4, date(2026, 8, 15))
        assert "no name qualifies, so no swap this round" in text
        assert "1 eligible bench name would clear the tail on entry (0 growth-tier)" in text
        assert "Only slower growers among the eligible names would clear" in text

    def test_empty_bench_depth_says_refresh(self):
        d = self._digest({"LOW": (15.0, 0.30)}, n_runs=4)
        text = render_markdown(d, 4, date(2026, 8, 15))
        assert "None would clear: refresh the bench before a name qualifies" in text

    def test_name_benched_mid_window_waits_a_full_window(self):
        # SPEC 7.0.1 round 7: one window of shadow scoring before promotion
        runs = self._runs({"OLD": (25.0, 0.20)})
        live = TickerSnapshot(composite=85.0, score=85.0, growth=0.30, r40_trend=0.05)
        runs[-1].bench["NEW"] = live
        d = build_digest(runs, list(self.BASE), ["OLD", "NEW"], CFG)
        rows = {b.ticker: b for b in d.bench_weeks}
        assert rows["NEW"].runs_seen == 1 and not rows["NEW"].full_window
        # the on-entry test still runs, so the digest can say "wait for NEW"
        # instead of asking for a refresh of a bench that was just refreshed
        assert rows["NEW"].clears_tail is True and d.swap_pick is None
        assert d.bench_waiting == ["NEW"] and d.bench_clearing == 0
        text = render_markdown(d, 15, date(2026, 8, 15))
        assert "NEW (joined the bench mid-window" in text
        assert "| yes, not yet eligible |" in text
        assert "The rule promotes NEW" not in text
        assert "NEW (growth tier) would clear but is not eligible yet" in text
        assert "recheck at the next digest before refreshing the bench" in text
        assert "refresh the bench (`python -m sentinel.screen`)" not in text

    def test_failed_bench_scoring_on_the_window_open_does_not_block(self):
        # the whole bench block failed on the window's first run
        runs = self._runs({"GRO": (85.0, 0.20)})
        first = len(runs) - CFG.week_window_runs
        runs[first] = RunSnapshot(runs[first].date, runs[first].run_type, runs[first].tickers, {})
        d = build_digest(runs, list(self.BASE), ["GRO"], CFG)
        assert d.bench_weeks[0].full_window and d.swap_pick == "GRO"

    def test_one_failed_fetch_on_the_window_open_does_not_block_that_name(self):
        # only GRO's fetch failed on the window's first run; OTHER was scored
        runs = self._runs({"GRO": (85.0, 0.20), "OTHER": (5.0, 0.20)})
        del runs[len(runs) - CFG.week_window_runs].bench["GRO"]
        d = build_digest(runs, list(self.BASE), ["GRO", "OTHER"], CFG)
        rows = {b.ticker: b for b in d.bench_weeks}
        assert rows["GRO"].full_window and rows["GRO"].entry_block is None
        assert d.swap_pick == "GRO"

    def test_new_name_after_failed_bench_runs_still_waits_a_window(self):
        # earlier bench blocks empty (failed or no bench yet): NEWB first
        # appears on the window's last 2 runs, so it has 2 runs, not a window
        runs = self._runs({})
        for run in runs[-2:]:
            run.bench["NEWB"] = TickerSnapshot(composite=85.0, score=85.0,
                                               growth=0.30, r40_trend=0.05)
        d = build_digest(runs, list(self.BASE), ["NEWB"], CFG)
        assert not d.bench_weeks[0].full_window
        assert d.bench_weeks[0].entry_block.startswith("joined the bench mid-window")
        assert d.swap_pick is None

    def test_demoted_name_counts_its_window_from_its_first_bench_run(self):
        # DEM was scored in the universe until the window opened, on the
        # bench from then on: a full window. Moving one run later is not.
        def demoted(last_universe_run: int):
            runs = self._runs({})
            for i, run in enumerate(runs):
                snap = TickerSnapshot(composite=85.0, score=85.0, growth=0.30, r40_trend=0.05)
                (run.tickers if i <= last_universe_run else run.bench)["DEM"] = snap
            return build_digest(runs, list(self.BASE), ["DEM"], CFG)

        first = 15 - CFG.week_window_runs
        assert demoted(first - 1).bench_weeks[0].full_window
        assert not demoted(first).bench_weeks[0].full_window

    def test_warm_up_bench_name_is_not_an_entrant(self):
        runs = self._runs({"GRO": (85.0, 0.20)})
        for run in runs:
            run.bench["WARM"] = TickerSnapshot(composite=95.0, score=95.0,
                                               growth=0.40, r40_trend=None)
        d = build_digest(runs, list(self.BASE), ["GRO", "WARM"], CFG)
        rows = {b.ticker: b for b in d.bench_weeks}
        assert rows["WARM"].entry_block.startswith("warm-up")
        assert rows["WARM"].clears_tail is True and d.swap_pick == "GRO"
        assert d.bench_waiting == ["WARM"] and d.bench_clearing == 1
        text = render_markdown(d, 15, date(2026, 8, 15))
        assert ("Not part of this round's pick: WARM (growth tier) would clear "
                "but is not eligible yet") in text
        # GRO is a growth-tier pick: no fallback, so no recheck advice
        assert "before taking the fallback" not in text

    def test_swap_merged_after_the_latest_run_is_not_recommended_again(self):
        # the latest run scored CCC in the universe and GRO on the bench, but
        # the config already swapped them (a failed Saturday run, or a manual
        # dispatch after the merge): the check reads the configured roles
        runs = self._runs({"GRO": (85.0, 0.20), "SLOW": (95.0, 0.10)})
        universe = ["AAA", "BBB", "DDD", "EEE", "GRO"]
        d = build_digest(runs, universe, ["CCC", "SLOW"], CFG)
        assert (d.swap_moved_out, d.swap_moved_in) == (["CCC"], ["GRO"])
        assert d.swap_outgoing == "DDD" and d.swap_outgoing_qualifies
        assert d.swap_pick == "SLOW"           # GRO is in the universe now
        rows = {b.ticker: b for b in d.bench_weeks}
        assert rows["GRO"].entry_block == "in the universe now"
        assert rows["GRO"].clears_tail is None
        text = render_markdown(d, 15, date(2026, 8, 15))
        assert ("The latest run (2026-08-15) predates the current watchlist "
                "(left the universe: CCC; joined it: GRO)") in text
        assert "Swap check: DDD qualifies" in text
        assert "CCC qualifies" not in text
        assert "CCC: moved to the bench (scored in the universe in 5 of 5 runs" in text
        assert "GRO: joined the universe from the bench" in text
        assert "check listing status" not in text and "no longer configured" not in text

    def test_current_roles_print_no_predates_line(self):
        d = self._digest({"GRO": (85.0, 0.20)})
        assert (d.swap_moved_out, d.swap_moved_in) == ([], [])
        assert "predates the current watchlist" not in render_markdown(
            d, 15, date(2026, 8, 15))

    def test_outgoing_is_the_first_qualifier_not_the_lowest_score(self):
        # a warm-up name below every qualifier holds a bottom slot but never
        # qualifies, so it is not the name the rule swaps out
        runs = self._runs({"GRO": (85.0, 0.20)})
        for run in runs:
            run.tickers["WRM"] = _scored(5.0, r40_trend=None)
        d = build_digest(runs, [*self.BASE, "WRM"], ["GRO"], CFG)
        assert d.swap_outgoing == "CCC" and d.swap_outgoing_qualifies

    def test_bench_row_reads_the_last_observed_score_and_growth(self):
        runs = self._runs({"GRO": (85.0, 0.10)})
        runs[-1].bench["GRO"] = TickerSnapshot(composite=88.0, score=88.0,
                                               growth=0.30, r40_trend=0.05)
        row = build_digest(runs, list(self.BASE), ["GRO"], CFG).bench_weeks[0]
        assert (row.score_last, row.growth_last, row.growth_tier) == (88.0, 0.30, "growth")

    def test_live_earlier_but_warm_up_on_the_last_run_is_blocked(self):
        runs = self._runs({"GRO": (85.0, 0.20)})
        runs[-1].bench["GRO"] = TickerSnapshot(composite=85.0, score=85.0,
                                               growth=0.20, r40_trend=None)
        d = build_digest(runs, list(self.BASE), ["GRO"], CFG)
        assert d.bench_weeks[0].entry_block.startswith("warm-up")
        assert d.swap_pick is None

    def test_round_trip_back_to_the_bench_starts_a_new_spell(self):
        # bench for runs 0-5, universe for 6-11, bench again from run 12: the
        # earlier bench spell does not count toward the new one
        runs = self._runs({"GRO": (85.0, 0.20)})
        for i in range(6, 12):
            snap = runs[i].bench.pop("GRO")
            runs[i].tickers["GRO"] = snap
        d = build_digest(runs, list(self.BASE), ["GRO"], CFG)
        assert not d.bench_weeks[0].full_window
        assert d.bench_weeks[0].entry_block.startswith("joined the bench mid-window")

    def test_fallback_pick_names_a_waiting_growth_name_and_advises_a_recheck(self):
        # WARM is growth-tier and would clear but is in warm-up; SLOW is the
        # only eligible clearing name: the fallback line must not claim that
        # no growth-tier name clears
        runs = self._runs({"SLOW": (85.0, 0.05)})
        for run in runs:
            run.bench["WARM"] = TickerSnapshot(composite=95.0, score=95.0,
                                               growth=0.40, r40_trend=None)
        d = build_digest(runs, list(self.BASE), ["SLOW", "WARM"], CFG)
        assert d.swap_pick == "SLOW"
        text = render_markdown(d, 15, date(2026, 8, 15))
        assert ("The rule promotes SLOW, a slower grower: no eligible growth-tier "
                "bench name clears the tail") in text
        assert ("Not part of this round's pick: WARM (growth tier) would clear but "
                "is not eligible yet (reason in the bench notes above). Growth comes "
                "first, so consider rechecking at the next digest before taking the "
                "fallback.") in text

    def test_fallback_pick_names_a_clearing_name_with_unknown_growth(self):
        d = self._digest({"SLOW": (95.0, 0.10), "UNK": (85.0, None)})
        assert d.swap_pick == "SLOW"
        text = render_markdown(d, 15, date(2026, 8, 15))
        assert ("Growth is n/a this window for UNK, which would also clear: "
                "check it before taking the fallback.") in text

    def test_growth_computed_at_exactly_the_floor_is_growth_tier(self):
        # 1150 / 1000 - 1 is 0.1499999999999999 in floating point
        d = self._digest({"EDGE": (85.0, 1150 / 1000 - 1), "SLOW": (95.0, 0.10)})
        assert d.swap_pick == "EDGE"
        text = render_markdown(d, 15, date(2026, 8, 15))
        assert "| 15.00 growth |" in text
        assert "The rule promotes EDGE." in text

    def test_growth_just_under_the_floor_never_prints_as_the_floor(self):
        d = self._digest({"NEAR": (85.0, 0.1496)})
        text = render_markdown(d, 15, date(2026, 8, 15))
        assert "| 14.96 fallback |" in text and "15.0 fallback" not in text

    def test_name_dropped_from_the_bench_config_is_not_an_entrant(self):
        # scored on the bench all window, then removed from the config
        d = build_digest(self._runs({"GRO": (85.0, 0.20)}), list(self.BASE), [], CFG)
        assert d.bench_weeks[0].entry_block == "no longer configured on the bench"
        assert d.swap_pick is None

    def test_name_both_benched_and_in_the_universe_is_not_an_entrant(self):
        d = build_digest(self._runs({"GRO": (85.0, 0.20)}),
                         [*self.BASE, "GRO"], ["GRO"], CFG)
        assert d.bench_weeks[0].entry_block == "in the universe now"
        assert d.swap_pick is None

    def test_growth_tier_is_filled_when_the_latest_run_has_no_scores(self):
        runs = self._runs({"GRO": (85.0, 0.20)})
        runs[-1] = RunSnapshot(
            runs[-1].date, runs[-1].run_type,
            {t: TickerSnapshot(composite=None, score=None) for t in self.BASE},
            runs[-1].bench,
        )
        d = build_digest(runs, list(self.BASE), ["GRO"], CFG)
        assert d.swap_outgoing is None
        assert d.bench_weeks[0].growth_tier == "growth"
        text = render_markdown(d, 15, date(2026, 8, 15))
        assert "| 20.0 growth |" in text
        assert "Swap check: n/a (no scored names on the latest run)." in text

    def test_empty_history_reads_its_growth_floor_from_config(self):
        d = build_digest([], list(self.BASE), ["GRO"], ChangesCfg(swap_min_growth=0.25))
        assert d.swap_min_growth == 0.25
        text = render_markdown(d, 1, date(2026, 8, 15))
        assert "yearly revenue growth of 25% or more" in text
        assert "Swap check: n/a (no runs in the window)." in text

    def test_one_unknown_growth_name_is_singular(self):
        d = self._digest({"UNK": (85.0, None)}, n_runs=4)
        text = render_markdown(d, 4, date(2026, 8, 15))
        assert "Growth is n/a this window for UNK, so check it before counting on it" in text

    def test_qualifier_with_no_bench_snapshots_still_gets_a_swap_line(self):
        d = build_digest(self._runs({}), list(self.BASE), ["WDAY"], CFG)
        assert d.swap_outgoing == "CCC" and d.swap_outgoing_qualifies
        text = render_markdown(d, 15, date(2026, 8, 15))
        assert "Swap check: CCC qualifies, but no bench name was scored" in text
        assert "python -m sentinel.screen" in text

    def test_unknown_growth_on_the_what_if_is_not_called_slow(self):
        d = self._digest({"UNK": (95.0, None)}, n_runs=4)
        text = render_markdown(d, 4, date(2026, 8, 15))
        assert "Growth is n/a this window for UNK" in text
        assert "Only slower growers would clear" not in text

    def test_swap_fields_reach_the_json(self):
        d = self._digest({"GRO": (85.0, 0.20)})
        payload = json.loads(render_json(d, 15, date(2026, 8, 15)))
        assert payload["swap_pick"] == "GRO"
        assert payload["bench_weeks"][0]["clears_tail"] is True
        assert payload["bench_weeks"][0]["growth_tier"] == "growth"


class TestJsonOutput:
    def _digest(self):
        runs = [
            RunSnapshot("2026-08-11", "scheduled",
                        {"AAA": _healthy(), "BBB": _decaying(composite=42.0)},
                        {"WDAY": TickerSnapshot(composite=61.0)}),
            RunSnapshot("2026-08-12", "scheduled",
                        {"AAA": _healthy(), "BBB": _decaying(composite=40.0)},
                        {"WDAY": TickerSnapshot(composite=57.0)}),
        ]
        return build_digest(runs, ["AAA", "BBB", "MISSING"], ["WDAY"], CFG)

    def test_payload_shape_is_stable_and_sorted(self):
        text = render_json(self._digest(), 4, date(2026, 8, 15))
        assert text == json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n"
        payload = json.loads(text)
        assert payload["refresh_number"] == 4
        assert payload["generated"] == "2026-08-15"
        assert payload["window_start"] == "2026-08-11"
        assert payload["runs_in_window"] == 2

    def test_streaks_and_flag_split_survive_serialization(self):
        payload = json.loads(render_json(self._digest(), 1, date(2026, 8, 15)))
        week = next(w for w in payload["attention"] if w["ticker"] == "BBB")
        assert week["decay_hits"] == 2 and week["decay_streak"] == 2
        assert week["flags_business"] == ["high_sbc"]
        assert week["flags_data_quality"] == []
        gap = next(g for g in payload["coverage_gaps"] if g["ticker"] == "MISSING")
        assert gap["kind"] == "absent_all" and gap["streak"] == 2
        assert gap["text"].startswith("MISSING: configured but absent")

    def test_bench_and_busiest_are_named_objects(self):
        payload = json.loads(render_json(self._digest(), 1, date(2026, 8, 15)))
        assert payload["bench"] == ["WDAY"]
        assert payload["bench_weeks"][0]["ticker"] == "WDAY"
        assert payload["bench_weeks"][0]["composite_delta"] == pytest.approx(-4.0)
        assert all({"ticker", "changes"} == set(b) for b in payload["busiest"])

    def test_cli_writes_markdown_and_json_together(self, tmp_path: Path):
        cfg_path = tmp_path / "watchlist.yaml"
        cfg_path.write_text(
            "universe:\n  - ticker: CRWD\n    tags: [r40]\nbench: [WDAY]\n"
        )
        hist = tmp_path / "run_history.json"
        hist.write_text(json.dumps({"version": 1, "runs": [
            RunSnapshot("2026-08-11", "scheduled", {"CRWD": _decaying()},
                        {"WDAY": TickerSnapshot(composite=61.0)}).to_dict(),
            RunSnapshot("2026-08-12", "scheduled", {"CRWD": _decaying()},
                        {"WDAY": TickerSnapshot(composite=57.0)}).to_dict(),
        ]}))
        md, js = tmp_path / "digest.md", tmp_path / "digest.json"
        rc = main([
            "--config", str(cfg_path), "--history", str(hist),
            "--refresh-number", "2", "--date", "2026-08-15",
            "--out", str(md), "--json", str(js),
        ])
        assert rc == 0
        assert "Watchlist candidate refresh #2" in md.read_text()
        payload = json.loads(js.read_text())
        assert payload["refresh_number"] == 2
        assert payload["bench_weeks"][0]["ticker"] == "WDAY"
        # read-only over history: the CLI wrote only what it was asked to
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "digest.json", "digest.md", "run_history.json", "watchlist.yaml",
        ]


class TestRendering:
    def _digest(self, **kw):
        runs = [
            _run(11, {"AAA": _healthy(), "BBB": _decaying(composite=42.0)}),
            _run(12, {"AAA": _healthy(), "BBB": _decaying(composite=40.0)}),
        ]
        return build_digest(runs, ["AAA", "BBB"], ["WDAY", "SHOP"], CFG, **kw)

    def test_no_em_or_en_dashes_anywhere(self):
        text = render_markdown(self._digest(), 1, date(2026, 8, 15))
        assert not re.search(r"[–—]", text)

    def test_body_carries_evidence_bench_and_checklist(self):
        text = render_markdown(self._digest(), 2, date(2026, 8, 15))
        assert "refresh #2" in text
        assert "| BBB | 2 of 2 |" in text
        assert "WDAY, SHOP." in text
        assert "SPEC.md" in text and "- [ ]" in text
        assert "sentinel.backfill --dry-run" in text  # promotion step (spec 7.0)
        assert "high sbc" in text  # flags humanized, no underscores

    def test_automation_decision_appears_after_calibration_rounds(self):
        early = render_markdown(self._digest(), CALIBRATION_REFRESHES - 1, date(2026, 8, 15))
        due = render_markdown(self._digest(), CALIBRATION_REFRESHES, date(2026, 8, 15))
        assert "decide whether to automate" not in early
        assert "decide whether to automate" in due

    def test_calibration_label_drops_after_the_calibration_rounds(self):
        during = render_markdown(self._digest(), CALIBRATION_REFRESHES, date(2026, 8, 15))
        after = render_markdown(self._digest(), CALIBRATION_REFRESHES + 2, date(2026, 8, 15))
        assert f"calibration round {CALIBRATION_REFRESHES} of" in during
        assert "calibration round" not in after

    def test_quiet_window_states_no_changes_is_valid(self):
        runs = [_run(11, {"AAA": _healthy()}), _run(12, {"AAA": _healthy()})]
        digest = build_digest(runs, ["AAA"], [], CFG)
        text = render_markdown(digest, 1, date(2026, 8, 15))
        assert "valid refresh outcome" in text
        assert "No threshold crossings" in text

    def test_data_note_surfaces(self):
        digest = self._digest(notes=["run history unreadable, change detection reset"])
        text = render_markdown(digest, 1, date(2026, 8, 15))
        assert "data note: run history unreadable" in text


class TestCli:
    def test_end_to_end_from_files(self, tmp_path: Path):
        # the CLI reads a config the test owns: the live watchlist is
        # owner-tunable, so its universe and bench must not drive assertions
        cfg_path = tmp_path / "watchlist.yaml"
        cfg_path.write_text(
            "universe:\n  - ticker: CRWD\n    tags: [r40]\n"
            "bench: [WDAY, SHOP]\n"
        )
        history = {
            "version": 1,
            "runs": [
                _run(11, {"CRWD": _decaying()}).to_dict(),
                _run(12, {"CRWD": _decaying()}).to_dict(),
            ],
        }
        hist = tmp_path / "run_history.json"
        hist.write_text(json.dumps(history))
        out = tmp_path / "digest.md"
        rc = main([
            "--config", str(cfg_path), "--history", str(hist),
            "--refresh-number", "1", "--date", "2026-08-15", "--out", str(out),
        ])
        assert rc == 0
        text = out.read_text()
        assert "Watchlist candidate refresh #1" in text
        assert "| CRWD | 2 of 2 |" in text
        assert "WDAY, SHOP." in text          # bench key wired through to the body
        assert not re.search(r"[–—]", text)


def test_basis_rows_never_pollute_noisiest_tickers():
    # review fix: a universe-wide basis flip inside the window produces a
    # collapsed pseudo-ticker "watchlist" row; it may appear in change counts
    # but must not compete in the per-ticker noise ranking
    from sentinel.config import ChangesCfg
    from sentinel.digest import build_digest
    from sentinel.report.changes import RunSnapshot, TickerSnapshot

    def run(date, tech):
        return RunSnapshot(date, "scheduled", {
            t: TickerSnapshot(composite=50.0 if tech else 30.0, score=30.0,
                              technical_score=80.0 if tech else None, rank=i + 1)
            for i, t in enumerate(["AAA", "BBB", "CCC", "DDD"])
        })

    runs = [run("2026-08-11", True), run("2026-08-12", False), run("2026-08-13", True)]
    digest = build_digest(runs, ["AAA", "BBB", "CCC", "DDD"], [], ChangesCfg())
    assert "score_basis" in digest.change_counts
    assert all(t != "watchlist" for t, _ in digest.busiest)
