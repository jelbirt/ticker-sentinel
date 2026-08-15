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


class TestSplitRebasing:
    """A split restates only the ~5 quarters the source still serves; the
    cache-only tail keeps the old basis unless the merge carries the factor
    back over it (CRWD 4:1 on 2026-07-02, NOW 5:1 on 2025-12-18)."""

    # newest first; the cached frame reaches further back than the source does
    FRESH_Q = ["2026-04-30", "2026-01-31", "2025-10-31"]
    CACHED_Q = ["2026-01-31", "2025-10-31", "2025-07-31", "2025-04-30"]

    def _shares(self, values, quarters):
        cols = pd.DatetimeIndex([pd.Timestamp(q) for q in quarters])
        return pd.DataFrame(
            {c: {"diluted_shares": v} for c, v in zip(cols, values)}
        ).astype("float64")

    def test_consistent_factor_rebases_the_cached_tail(self):
        cached = self._shares([258.0, 256.0, 254.0, 252.0], self.CACHED_Q)
        fresh = self._shares([1040.0, 1032.0, 1024.0], self.FRESH_Q)  # 4x, restated
        merged = merge_statements(cached, fresh)
        row = merged.loc["diluted_shares"]
        assert row[pd.Timestamp("2026-04-30")] == approx(1040.0)  # fresh untouched
        assert row[pd.Timestamp("2026-01-31")] == approx(1032.0)  # fresh wins the overlap
        assert row[pd.Timestamp("2025-07-31")] == approx(1016.0)  # 254 x 4, cache only
        assert row[pd.Timestamp("2025-04-30")] == approx(1008.0)  # 252 x 4, cache only

    def test_inconsistent_ratios_are_left_alone(self):
        # a revision to one quarter's figures is not a rebasing
        cached = self._shares([100.0, 100.0, 100.0, 100.0], self.CACHED_Q)
        fresh = self._shares([210.0, 130.0, 100.0], self.FRESH_Q)
        merged = merge_statements(cached, fresh)
        assert merged.loc["diluted_shares", pd.Timestamp("2025-07-31")] == approx(100.0)

    def test_same_basis_is_untouched(self):
        cached = self._shares([102.0, 101.0, 100.0, 99.0], self.CACHED_Q)
        fresh = self._shares([104.0, 102.0, 101.0], self.FRESH_Q)
        merged = merge_statements(cached, fresh)
        assert merged.loc["diluted_shares", pd.Timestamp("2025-07-31")] == approx(100.0)

    def test_single_overlapping_quarter_does_not_rebase(self):
        cached = self._shares([250.0, 248.0], ["2026-01-31", "2025-10-31"])
        fresh = self._shares([1040.0, 1000.0], ["2026-04-30", "2026-01-31"])
        merged = merge_statements(cached, fresh)
        assert merged.loc["diluted_shares", pd.Timestamp("2025-10-31")] == approx(248.0)

    def test_zero_fresh_values_never_rebase(self):
        # a zeroed fresh row (alias drift, source glitch) must not infer a
        # factor of 0 and zero out the cache-only history
        cached = self._shares([100.0, 100.0, 100.0, 100.0], self.CACHED_Q)
        fresh = self._shares([0.0, 0.0, 0.0], self.FRESH_Q)
        merged = merge_statements(cached, fresh)
        assert merged.loc["diluted_shares", pd.Timestamp("2025-07-31")] == approx(100.0)
        assert merged.loc["diluted_shares", pd.Timestamp("2025-04-30")] == approx(100.0)

    def test_other_rows_are_never_rebased(self):
        cached = self._shares([100.0, 100.0, 100.0, 100.0], self.CACHED_Q)
        cached.loc["revenue"] = 50.0
        fresh = self._shares([400.0, 400.0, 400.0], self.FRESH_Q)
        fresh.loc["revenue"] = 50.0
        merged = merge_statements(cached, fresh)
        assert merged.loc["diluted_shares", pd.Timestamp("2025-07-31")] == approx(400.0)
        assert merged.loc["revenue", pd.Timestamp("2025-07-31")] == approx(50.0)


class TestScrubIsNeverPersisted:
    """The saved cache keeps the unsanitized merge: cells inside Yahoo's
    served window self-correct on the next fetch anyway, so persisting the
    scrub could only destroy cache-only history, and a false positive (stale
    shares-outstanding reference) would destroy it permanently."""

    def test_saved_cache_keeps_history_the_read_time_guard_drops(
        self, tmp_path, monkeypatch
    ):
        from sentinel.data import fundamentals as fnd
        from tests.conftest import make_canonical

        monkeypatch.setenv("SENTINEL_ROOT", str(tmp_path))
        shape = [400.0, 398.0, 100.0, 99.0]  # basis step, no reference
        frame = make_canonical(
            {"revenue": [100.0] * 4, "diluted_shares": shape}
        )
        meta = {"fetched_at": "2026-08-15T00:00:00+00:00", "company_name": "X Corp"}
        monkeypatch.setattr(fnd, "fetch_statements", lambda ticker: (frame, meta))

        inputs, notes = fnd.get_fundamentals("X")

        assert inputs.diluted_shares_now is None  # scoring sees the scrub
        assert any("no shares outstanding reference" in n for n in notes)
        saved, _ = cache.load("X")
        assert list(saved.loc["diluted_shares"]) == approx(shape)  # cache does not


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

class TestPruneKeepsBench:
    """The backfill seeds bench parquets; pruning on the scored universe alone
    would delete them on the next scheduled run (spec D3 rider 1)."""

    def test_bench_files_survive_a_prune_on_cache_tickers(self, tmp_path, monkeypatch):
        from sentinel.config import Config, TickerCfg

        monkeypatch.setenv("SENTINEL_ROOT", str(tmp_path))
        d = tmp_path / "data" / "cache"
        d.mkdir(parents=True)
        for t in ("CRWD", "WDAY", "CFLT"):
            for suffix in (".parquet", ".meta.json"):
                (d / f"{t}{suffix}").write_text("x")

        cfg = Config(universe=(TickerCfg("CRWD", ("r40",)),), bench=("WDAY",))
        removed = prune(set(cfg.cache_tickers))

        assert removed == ["CFLT.meta.json", "CFLT.parquet"]  # departed name only
        assert (d / "WDAY.parquet").exists()
        assert (d / "CRWD.parquet").exists()

    def test_pruning_on_scored_universe_alone_would_delete_the_bench(
        self, tmp_path, monkeypatch
    ):
        """Guards the regression the rider fixes, so run.py cannot drift back."""
        from sentinel.config import Config, TickerCfg

        monkeypatch.setenv("SENTINEL_ROOT", str(tmp_path))
        d = tmp_path / "data" / "cache"
        d.mkdir(parents=True)
        (d / "WDAY.parquet").write_text("x")

        cfg = Config(universe=(TickerCfg("CRWD", ("r40",)),), bench=("WDAY",))
        assert prune(set(cfg.all_tickers)) == ["WDAY.parquet"]
