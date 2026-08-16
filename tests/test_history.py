"""Run-history persistence: round-trip, retention, same-date replace, corruption."""
from __future__ import annotations

import json

import pytest

from sentinel.data import cache, history
from sentinel.report.changes import RunSnapshot, TickerSnapshot


@pytest.fixture()
def isolated_root(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_ROOT", str(tmp_path))
    (tmp_path / "data" / "cache").mkdir(parents=True)
    return tmp_path


def _run(date: str, composite: float = 50.0) -> dict:
    return {
        "date": date,
        "run_type": "scheduled",
        "tickers": {"AAA": {"composite": composite, "rank": 1, "flags": []}},
    }


class TestRoundTrip:
    def test_missing_file_is_empty_history(self, isolated_root):
        runs, notes = history.load_history()
        assert runs == [] and notes == []

    def test_save_then_load(self, isolated_root):
        history.save_run(_run("2026-08-05"), retention=12)
        history.save_run(_run("2026-08-06", composite=53.0), retention=12)
        runs, notes = history.load_history()
        assert notes == []
        assert [r["date"] for r in runs] == ["2026-08-05", "2026-08-06"]
        assert runs[-1]["tickers"]["AAA"]["composite"] == 53.0

    def test_output_is_stable_and_versioned(self, isolated_root):
        history.save_run(_run("2026-08-06"), retention=12)
        raw = history.history_path().read_text()
        assert json.loads(raw)["version"] == 1
        # committed file: pretty-printed with sorted keys for reviewable diffs
        assert raw == json.dumps(json.loads(raw), indent=2, sort_keys=True)


class TestWriteRules:
    def test_same_date_replaces(self, isolated_root):
        history.save_run(_run("2026-08-06", composite=50.0), retention=12)
        history.save_run(_run("2026-08-06", composite=61.0), retention=12)
        runs, _ = history.load_history()
        assert len(runs) == 1
        assert runs[0]["tickers"]["AAA"]["composite"] == 61.0

    def test_retention_prunes_oldest(self, isolated_root):
        for day in range(1, 16):  # 15 runs, retention 12
            history.save_run(_run(f"2026-07-{day:02d}"), retention=12)
        runs, _ = history.load_history()
        assert len(runs) == 12
        assert runs[0]["date"] == "2026-07-04"
        assert runs[-1]["date"] == "2026-07-15"

    def test_out_of_order_dates_sort(self, isolated_root):
        history.save_run(_run("2026-08-06"), retention=12)
        history.save_run(_run("2026-08-04"), retention=12)
        runs, _ = history.load_history()
        assert [r["date"] for r in runs] == ["2026-08-04", "2026-08-06"]


class TestDegradation:
    def test_corrupt_file_degrades_with_note(self, isolated_root):
        history.history_path().write_text("{not json")
        runs, notes = history.load_history()
        assert runs == []
        assert any("run history" in n for n in notes)

    def test_wrong_shape_degrades_with_note(self, isolated_root):
        history.history_path().write_text(json.dumps({"version": 1, "runs": "nope"}))
        runs, notes = history.load_history()
        assert runs == [] and len(notes) == 1

    def test_ticker_payload_wrong_shape_degrades(self, isolated_root):
        history.history_path().write_text(
            json.dumps({"version": 1, "runs": [{"date": "d", "tickers": {"AAA": "oops"}}]})
        )
        runs, notes = history.load_history()
        assert runs == [] and len(notes) == 1

    def test_newer_schema_version_degrades_with_note(self, isolated_root):
        history.history_path().write_text(
            json.dumps({"version": history.SCHEMA_VERSION + 1, "runs": [_run("2026-08-06")]})
        )
        runs, notes = history.load_history()
        assert runs == []
        assert len(notes) == 1
        assert f"schema v{history.SCHEMA_VERSION + 1} is newer" in notes[0]
        assert "change detection reset" in notes[0]

    def test_current_schema_version_is_accepted(self, isolated_root):
        history.history_path().write_text(
            json.dumps({"version": history.SCHEMA_VERSION, "runs": [_run("2026-08-06")]})
        )
        runs, notes = history.load_history()
        assert [r["date"] for r in runs] == ["2026-08-06"] and notes == []

    def test_missing_version_is_accepted(self, isolated_root):
        # pre-versioning files (and hand-edited ones) still load
        history.history_path().write_text(json.dumps({"runs": [_run("2026-08-06")]}))
        runs, notes = history.load_history()
        assert [r["date"] for r in runs] == ["2026-08-06"] and notes == []

    def test_non_numeric_version_degrades_like_corruption(self, isolated_root):
        history.history_path().write_text(
            json.dumps({"version": "two", "runs": [_run("2026-08-06")]})
        )
        runs, notes = history.load_history()
        assert runs == [] and len(notes) == 1

    def test_retention_floor_of_one(self, isolated_root):
        history.save_run(_run("2026-08-05"), retention=0)
        history.save_run(_run("2026-08-06"), retention=0)
        runs, _ = history.load_history()
        assert [r["date"] for r in runs] == ["2026-08-06"]

    def test_save_over_corrupt_starts_fresh(self, isolated_root):
        history.history_path().write_text("{not json")
        history.save_run(_run("2026-08-06"), retention=12)
        runs, notes = history.load_history()
        assert [r["date"] for r in runs] == ["2026-08-06"] and notes == []


class TestBenchBlock:
    """The `bench` sibling key is additive: new code reads old files, old code
    reads new ones (it ignores unknown keys), so SCHEMA_VERSION does not move."""

    def test_round_trip_with_bench(self, isolated_root):
        run = _run("2026-08-06")
        run["bench"] = {"WDAY": {"composite": 61.0, "rank": None, "flags": []}}
        history.save_run(run, retention=12)
        runs, notes = history.load_history()
        assert notes == []
        assert runs[0]["bench"]["WDAY"]["composite"] == 61.0
        assert runs[0]["tickers"]["AAA"]["composite"] == 50.0

    def test_old_entry_without_bench_still_loads(self, isolated_root):
        history.history_path().write_text(
            json.dumps({"version": 1, "runs": [_run("2026-08-06")]})
        )
        runs, notes = history.load_history()
        assert notes == []
        assert "bench" not in runs[0]

        snap = RunSnapshot.from_dict(runs[0])
        assert snap.bench == {} and set(snap.tickers) == {"AAA"}

    def test_new_entry_parses_bench_into_its_own_block(self, isolated_root):
        run = _run("2026-08-06")
        run["bench"] = {"WDAY": {"composite": 61.0, "rank": None, "flags": ["dilution"]}}
        snap = RunSnapshot.from_dict(run)
        assert set(snap.tickers) == {"AAA"}          # bench never merged in
        assert snap.bench["WDAY"].composite == 61.0
        assert snap.bench["WDAY"].rank is None

    def test_garbled_bench_does_not_reset_change_detection(self, isolated_root):
        # a bench block is digest evidence only: it must never be able to take
        # the scored universe's diffs down with it
        history.history_path().write_text(json.dumps({
            "version": 1,
            "runs": [{**_run("2026-08-06"), "bench": "nope"}],
        }))
        runs, notes = history.load_history()
        assert notes == [] and len(runs) == 1
        snap = RunSnapshot.from_dict(runs[0])
        assert snap.bench == {} and set(snap.tickers) == {"AAA"}

    def test_snapshot_round_trip_preserves_bench(self):
        original = RunSnapshot(
            date="2026-08-06", run_type="scheduled",
            tickers={"AAA": TickerSnapshot(composite=50.0, rank=1)},
            bench={"WDAY": TickerSnapshot(composite=61.0)},
        )
        assert RunSnapshot.from_dict(original.to_dict()) == original


class TestPruneSafety:
    def test_cache_prune_leaves_run_history_alone(self, isolated_root):
        history.save_run(_run("2026-08-06"), retention=12)
        removed = cache.prune(keep_tickers=set())  # empty watchlist: everything else goes
        assert removed == []
        assert history.history_path().exists()
