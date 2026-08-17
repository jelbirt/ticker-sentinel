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
        assert "| WDAY | 2 of 2 | 57.0 | -4.0 | dilution |" in text
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
