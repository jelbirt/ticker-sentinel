"""Weekly refresh digest: window aggregation, attention gating, rendering."""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from sentinel.config import ChangesCfg
from sentinel.digest import (
    CALIBRATION_REFRESHES,
    build_digest,
    main,
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
        gaps = " ".join(digest.coverage_gaps)
        assert "NEVER: configured but absent from every run" in gaps
        assert "GONE: missing from the latest run (2026-08-12)" in gaps

    def test_stale_history_ticker_reported(self):
        runs = [_run(12, {"AAA": _healthy(), "OLD": _healthy(rank=2)})]
        digest = build_digest(runs, ["AAA"], [], CFG)
        assert any(g.startswith("OLD: in run history") for g in digest.coverage_gaps)

    def test_empty_history_degrades_to_note_not_crash(self):
        digest = build_digest([], ["AAA", "BBB"], ["WDAY"], CFG)
        assert digest.runs_in_window == 0
        assert "no run history yet" in digest.coverage_gaps[0]


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
