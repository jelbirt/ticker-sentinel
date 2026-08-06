"""Run-history persistence: round-trip, retention, same-date replace, corruption."""
from __future__ import annotations

import json

import pytest

from sentinel.data import cache, history


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

    def test_save_over_corrupt_starts_fresh(self, isolated_root):
        history.history_path().write_text("{not json")
        history.save_run(_run("2026-08-06"), retention=12)
        runs, notes = history.load_history()
        assert [r["date"] for r in runs] == ["2026-08-06"] and notes == []


class TestPruneSafety:
    def test_cache_prune_leaves_run_history_alone(self, isolated_root):
        history.save_run(_run("2026-08-06"), retention=12)
        removed = cache.prune(keep_tickers=set())  # empty watchlist: everything else goes
        assert removed == []
        assert history.history_path().exists()
