"""Cache bounding: quarter cap on merge, pruning of departed tickers."""
from __future__ import annotations

import pandas as pd
from pytest import approx

from sentinel.data import cache
from sentinel.data.cache import MAX_QUARTERS, merge_statements, prune


def _frame(n_cols: int, start="2026-04-30", value=100.0) -> pd.DataFrame:
    latest = pd.Timestamp(start)
    cols = pd.DatetimeIndex([latest - pd.DateOffset(months=3 * i) for i in range(n_cols)])
    return pd.DataFrame({c: {"revenue": value} for c in cols}).astype("float64")


class TestMergeCap:
    def test_merge_caps_at_max_quarters(self):
        cached = _frame(MAX_QUARTERS, start="2026-01-31", value=90.0)  # already full
        fresh = _frame(2, start="2026-07-31", value=100.0)             # 2 newer quarters
        merged = merge_statements(cached, fresh)
        assert merged.shape[1] == MAX_QUARTERS
        assert merged.columns[0] == pd.Timestamp("2026-07-31")          # newest kept
        assert merged.loc["revenue"].iloc[0] == approx(100.0)
        # the 2 oldest cached quarters fell off the end
        assert cached.columns[-1] not in merged.columns

    def test_merge_below_cap_keeps_everything(self):
        merged = merge_statements(_frame(4, start="2025-07-31"), _frame(4))
        assert merged.shape[1] == 8


class TestPrune:
    def _seed(self, tmp_path, monkeypatch, tickers):
        monkeypatch.setenv("SENTINEL_ROOT", str(tmp_path))
        d = tmp_path / "data" / "cache"
        d.mkdir(parents=True)
        (d / ".gitkeep").touch()
        for t in tickers:
            for suffix in (".parquet", ".meta.json", ".signals.json"):
                (d / f"{t}{suffix}").write_text("x")
        return d

    def test_prune_removes_departed_keeps_current(self, tmp_path, monkeypatch):
        d = self._seed(tmp_path, monkeypatch, ["CRWD", "MDB"])
        removed = prune({"CRWD"})
        assert removed == ["MDB.meta.json", "MDB.parquet", "MDB.signals.json"]
        assert (d / "CRWD.parquet").exists()
        assert not (d / "MDB.parquet").exists()
        assert (d / ".gitkeep").exists()  # non-cache files untouched

    def test_prune_handles_dotted_tickers(self, tmp_path, monkeypatch):
        d = self._seed(tmp_path, monkeypatch, ["BRK.B"])
        assert prune({"BRK.B"}) == []
        assert (d / "BRK.B.parquet").exists()

    def test_prune_missing_dir_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SENTINEL_ROOT", str(tmp_path))
        assert prune({"CRWD"}) == []
