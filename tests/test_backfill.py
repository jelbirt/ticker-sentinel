"""Backfill verification gate, merge semantics, and the write-free dry run.

Offline only: EDGAR and yfinance are both injected as fakes.
"""
from __future__ import annotations

import hashlib
from typing import Any

import pandas as pd
import pytest
from pytest import approx

from sentinel import backfill
from sentinel.backfill import (
    ACCEPT,
    ERROR,
    MATCH_ABS_FLOOR,
    REJECT,
    SKIP,
    align_columns,
    backfill_ticker,
    compare,
    format_report,
    merge_backfill,
    values_match,
)
from sentinel.data import cache
from sentinel.data.fundamentals import CANONICAL_FIELDS

_TAGS = {
    "revenue": "Revenues",
    "operating_income": "OperatingIncomeLoss",
    "d_and_a": "DepreciationDepletionAndAmortization",
    "ocf": "NetCashProvidedByUsedInOperatingActivities",
    "capex": "PaymentsToAcquirePropertyPlantAndEquipment",
    "sbc": "ShareBasedCompensation",
}


def quarter_ends(n: int, latest: str = "2026-04-30") -> list[pd.Timestamp]:
    end = pd.Timestamp(latest)
    return [end - pd.DateOffset(months=3 * i) for i in range(n)]


def canonical(values: dict[pd.Timestamp, dict[str, float]]) -> pd.DataFrame:
    """A cached-shaped canonical frame from {quarter_end: {field: value}}."""
    cols = sorted(values, reverse=True)
    frame = pd.DataFrame(index=CANONICAL_FIELDS, columns=cols, dtype="float64")
    for col, fields in values.items():
        for name, value in fields.items():
            frame.loc[name, col] = value
    return frame


def payload(values: dict[pd.Timestamp, dict[str, float]]) -> dict[str, Any]:
    """A companyfacts payload of direct 3-month facts matching `values`."""
    facts: dict[str, Any] = {}
    for end, fields in values.items():
        start = (end - pd.DateOffset(months=3) + pd.DateOffset(days=1)).date().isoformat()
        for name, value in fields.items():
            tag = _TAGS[name]
            facts.setdefault(tag, {"units": {"USD": []}})["units"]["USD"].append(
                {
                    "start": start,
                    "end": end.date().isoformat(),
                    "val": value,
                    "form": "10-Q",
                    "filed": end.date().isoformat(),
                }
            )
    return {"cik": 1, "entityName": "TEST", "facts": {"us-gaap": facts}}


def series(n: int, base: float, step: float = 1_000_000.0) -> dict[pd.Timestamp, dict[str, float]]:
    """`n` quarters of plausible revenue/ocf/capex, newest first."""
    return {
        end: {
            "revenue": base - i * step,
            "ocf": (base - i * step) / 4,
            "capex": 5_000_000.0 + i,
        }
        for i, end in enumerate(quarter_ends(n))
    }


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_ROOT", str(tmp_path))
    d = tmp_path / "data" / "cache"
    d.mkdir(parents=True)
    return d


def seed_cache(ticker: str, frame: pd.DataFrame) -> None:
    cache.save(ticker, frame, {"fetched_at": "2026-08-15T00:00:00+00:00", "source": "yfinance"})


def digest(directory) -> dict[str, str]:
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.iterdir())
        if p.is_file()
    }


class TestValuesMatch:
    def test_identical_values_match(self):
        assert values_match(1_000_000_000.0, 1_000_000_000.0)

    def test_exactly_one_percent_apart_matches(self):
        assert values_match(1_000_000_000.0, 1_010_000_000.0)

    def test_just_over_one_percent_apart_does_not(self):
        assert not values_match(1_000_000_000.0, 1_010_100_000.0)

    def test_absolute_floor_rescues_a_near_zero_value(self):
        """A 10 percent gap on a small number still passes under the floor."""
        assert values_match(1_000_000.0, 1_100_000.0)

    def test_exactly_at_the_absolute_floor_matches(self):
        assert values_match(0.0, MATCH_ABS_FLOOR)

    def test_just_past_the_floor_and_past_the_percentage_fails(self):
        assert not values_match(1_000_000.0, 1_200_000.0)

    def test_zero_cached_value_only_passes_under_the_floor(self):
        assert values_match(0.0, 50_000.0)
        assert not values_match(0.0, 1_000_000_000.0)

    def test_sign_flip_is_a_mismatch(self):
        assert not values_match(50_000_000.0, -50_000_000.0)


class TestCompare:
    def test_matching_overlap_produces_all_ok(self):
        frame = canonical(series(6, 400_000_000.0))
        checks = compare(frame, frame)
        assert checks and all(c.ok for c in checks)

    def test_a_single_bad_field_is_flagged(self):
        cached = canonical(series(6, 400_000_000.0))
        edgar = cached.copy()
        edgar.loc["ocf", edgar.columns[1]] = 1.0
        bad = [c for c in compare(cached, edgar) if not c.ok]
        assert [(c.field, c.quarter) for c in bad] == [("ocf", cached.columns[1])]

    def test_nan_on_one_side_is_not_a_mismatch(self):
        cached = canonical(series(6, 400_000_000.0))
        edgar = cached.copy()
        edgar.loc["ocf"] = float("nan")
        checks = compare(cached, edgar)
        assert all(c.ok for c in checks)
        assert not any(c.field == "ocf" for c in checks)

    def test_only_overlapping_quarters_are_checked(self):
        cached = canonical(series(4, 400_000_000.0))
        edgar = canonical(series(10, 400_000_000.0))
        assert {c.quarter for c in compare(cached, edgar)} == set(cached.columns)

    def test_unmapped_fields_are_never_compared(self):
        cached = canonical(series(4, 400_000_000.0))
        cached.loc["ebitda"] = 7.0
        edgar = cached.copy()
        assert not any(c.field in ("ebitda", "total_debt", "cash") for c in compare(cached, edgar))

    def test_operating_income_and_d_and_a_are_outside_the_gate(self):
        """Amendment 1 change 1: as-filed vs normalized gaps stop rejecting."""
        cached = canonical(series(4, 400_000_000.0))
        cached.loc["operating_income"] = 50_000_000.0
        cached.loc["d_and_a"] = 20_000_000.0
        edgar = cached.copy()
        edgar.loc["operating_income"] = -900_000_000.0  # wildly disagreeing
        edgar.loc["d_and_a"] = 1.0
        checks = compare(cached, edgar)
        assert checks and all(c.ok for c in checks)
        assert not any(c.field in ("operating_income", "d_and_a") for c in checks)


class TestAlignColumns:
    def test_near_identical_quarter_ends_are_snapped_to_the_cache(self):
        cached = canonical(series(4, 400_000_000.0))
        shifted = {c + pd.DateOffset(days=2): v for c, v in series(4, 400_000_000.0).items()}
        aligned = align_columns(canonical(shifted), cached.columns)
        assert list(aligned.columns) == list(cached.columns)

    def test_distant_quarter_ends_are_left_alone(self):
        cached = canonical(series(4, 400_000_000.0))
        shifted = {c + pd.DateOffset(days=40): v for c, v in series(4, 400_000_000.0).items()}
        aligned = align_columns(canonical(shifted), cached.columns)
        assert not set(aligned.columns) & set(cached.columns)


class TestMergeBackfill:
    def test_older_edgar_quarters_are_appended(self):
        cached = canonical(series(6, 400_000_000.0))
        edgar = canonical(series(14, 400_000_000.0))
        merged = merge_backfill(cached, edgar)
        assert merged.shape[1] == 14
        assert list(merged.columns) == sorted(edgar.columns, reverse=True)

    def test_yfinance_wins_on_every_cached_quarter(self):
        cached = canonical(series(6, 400_000_000.0))
        edgar = canonical(series(14, 400_000_000.0))
        edgar.loc["revenue", edgar.columns[0]] = 1.0  # EDGAR disagreeing on an overlap
        merged = merge_backfill(cached, edgar)
        assert merged.loc["revenue", cached.columns[0]] == approx(400_000_000.0)

    def test_merge_is_capped_at_max_quarters(self):
        cached = canonical(series(6, 400_000_000.0))
        edgar = canonical(series(30, 400_000_000.0))
        merged = merge_backfill(cached, edgar)
        assert merged.shape[1] == cache.MAX_QUARTERS

    def test_edgar_no_deeper_than_the_cache_is_a_no_op(self):
        cached = canonical(series(6, 400_000_000.0))
        merged = merge_backfill(cached, canonical(series(3, 400_000_000.0)))
        assert list(merged.columns) == list(cached.columns)


class TestHoleFilling:
    """Amendment 1 change 3: fill EMPTY cells inside the cached range.

    The promise is no longer "no cached cell is touched", it is "no cached
    VALUE is overwritten; verified EDGAR values may fill empty cells".
    """

    def test_a_hollow_mid_frame_column_is_filled(self):
        cached = canonical(series(8, 400_000_000.0))
        hollow = cached.columns[3]
        cached.loc[:, hollow] = float("nan")  # yfinance shell column
        edgar = canonical(series(14, 400_000_000.0))
        merged = merge_backfill(cached, edgar)
        for name in ("revenue", "ocf", "capex"):
            assert merged.loc[name, hollow] == approx(edgar.loc[name, hollow])

    def test_a_single_missing_cell_is_filled(self):
        cached = canonical(series(6, 400_000_000.0))
        cached.loc["sbc"] = float("nan")
        edgar = canonical(series(14, 400_000_000.0))
        edgar.loc["sbc"] = 9_000_000.0
        merged = merge_backfill(cached, edgar)
        assert merged.loc["sbc", cached.columns[0]] == approx(9_000_000.0)

    def test_a_cached_value_is_never_overwritten(self):
        """EDGAR disagreeing inside the cached range still loses."""
        cached = canonical(series(6, 400_000_000.0))
        edgar = canonical(series(14, 400_000_000.0))
        for col in cached.columns:
            edgar.loc["revenue", col] = 1.0
        merged = merge_backfill(cached, edgar)
        for col in cached.columns:
            assert merged.loc["revenue", col] == approx(cached.loc["revenue", col])

    def test_the_partial_newest_column_stays_partial_without_edgar_facts(self):
        """EDGAR has nothing that recent, so the hole survives the merge."""
        cached = canonical(series(6, 400_000_000.0))
        newest = cached.columns[0]
        cached.loc["ocf", newest] = float("nan")
        cached.loc["capex", newest] = float("nan")
        edgar = canonical(series(14, 400_000_000.0)).drop(columns=[newest])
        merged = merge_backfill(cached, edgar)
        assert pd.isna(merged.loc["ocf", newest])
        assert pd.isna(merged.loc["capex", newest])
        assert merged.loc["revenue", newest] == approx(cached.loc["revenue", newest])

    def test_an_all_nan_edgar_row_never_erases_a_cached_value(self):
        """The narrowed field set leaves operating_income NaN on the EDGAR side."""
        cached = canonical(series(6, 400_000_000.0))
        cached.loc["operating_income"] = 12_000_000.0
        edgar = canonical(series(14, 400_000_000.0))
        merged = merge_backfill(cached, edgar)
        assert (merged.loc["operating_income", cached.columns] == 12_000_000.0).all()

    def test_row_and_column_order_survive_the_fill(self):
        cached = canonical(series(6, 400_000_000.0))
        cached.loc[:, cached.columns[2]] = float("nan")
        merged = merge_backfill(cached, canonical(series(14, 400_000_000.0)))
        assert list(merged.index) == CANONICAL_FIELDS
        assert list(merged.columns) == sorted(merged.columns, reverse=True)

    def test_edgar_columns_the_cache_lacks_are_not_inserted_mid_range(self):
        """Hole filling fills cells; it does not invent quarters."""
        cached = canonical(series(6, 400_000_000.0))
        gap = cached.columns[2]
        cached = cached.drop(columns=[gap])
        merged = merge_backfill(cached, canonical(series(14, 400_000_000.0)))
        assert gap not in merged.columns


class TestTrendInputsGoLive:
    """End to end: the fill is what turns 16 quarters into usable TTM windows."""

    def test_a_hollow_column_blocks_a_ttm_window_until_it_is_filled(self):
        from sentinel.data.fundamentals import build_ttm

        cached = canonical(series(16, 400_000_000.0))
        cached.loc[:, cached.columns[5]] = float("nan")
        # the -4Q window r40_trend needs spans the hollow column, so every one
        # of its sums is None and r40_trend stays n/a at full cache depth
        before = build_ttm(cached, offset=4)
        assert (before.revenue, before.ocf, before.capex) == (None, None, None)

        merged = merge_backfill(cached, canonical(series(16, 400_000_000.0)))
        windows = [build_ttm(merged, offset=o) for o in (0, 4, 8)]
        assert all(w is not None for w in windows)
        assert all(w.revenue is not None and w.ocf is not None for w in windows)
        assert all(w.capex is not None for w in windows)


def _fetchers(edgar_values, yf_frame=None):
    def fetch_facts(ticker: str) -> dict[str, Any]:
        return payload(edgar_values)

    def fetch_yf(ticker: str):
        if yf_frame is None:
            raise AssertionError("yfinance must not be called for a cached ticker")
        return yf_frame, {"fetched_at": "2026-08-15T00:00:00+00:00", "source": "yfinance"}

    return fetch_facts, fetch_yf


class TestBackfillTicker:
    def test_skipped_ticker_never_fetches(self, cache_root):
        def boom(ticker):
            raise AssertionError("no fetch for a skipped ticker")

        result = backfill_ticker("MNDY", boom, boom)
        assert result.status == SKIP
        # the reason names the real blocker (period granularity), not missing
        # or untrustworthy data: MNDY does file US-GAAP XBRL, just never on a
        # 3-month period (read-only EDGAR check, CIK 1845338, 2026-08-16)
        assert "20-F" in result.reason
        assert "no 3-month periods" in result.reason
        assert not list(cache_root.iterdir())

    def test_accept_reports_the_gain_without_writing(self, cache_root):
        seed_cache("AAA", canonical(series(6, 400_000_000.0)))
        before = digest(cache_root)
        facts, yf = _fetchers(series(14, 400_000_000.0))
        result = backfill_ticker("AAA", facts, yf, apply=False)
        assert (result.status, result.quarters_before, result.quarters_after) == (ACCEPT, 6, 14)
        assert result.gained == 8
        assert digest(cache_root) == before

    def test_apply_rewrites_the_parquet(self, cache_root):
        seed_cache("AAA", canonical(series(6, 400_000_000.0)))
        facts, yf = _fetchers(series(14, 400_000_000.0))
        assert backfill_ticker("AAA", facts, yf, apply=True).status == ACCEPT
        reloaded, _ = cache.load("AAA")
        assert reloaded.shape[1] == 14

    def test_one_mismatch_rejects_the_whole_ticker(self, cache_root):
        seed_cache("AAA", canonical(series(6, 400_000_000.0)))
        edgar_values = series(14, 400_000_000.0)
        worst = sorted(edgar_values)[-1]
        edgar_values[worst]["revenue"] *= 1.5
        facts, yf = _fetchers(edgar_values)
        result = backfill_ticker("AAA", facts, yf, apply=True)
        assert result.status == REJECT
        assert "1 of" in result.reason
        assert cache.load("AAA")[0].shape[1] == 6  # untouched

    def test_no_overlap_is_rejected_not_trusted(self, cache_root):
        seed_cache("AAA", canonical(series(4, 400_000_000.0)))
        old = {
            end: {"revenue": 1.0} for end in quarter_ends(4, latest="2020-04-30")
        }
        facts, yf = _fetchers(old)
        result = backfill_ticker("AAA", facts, yf, apply=True)
        assert result.status == REJECT
        assert result.reason == "no overlapping quarters to verify against"
        assert cache.load("AAA")[0].shape[1] == 4

    def test_empty_edgar_payload_is_rejected(self, cache_root):
        seed_cache("AAA", canonical(series(6, 400_000_000.0)))
        result = backfill_ticker("AAA", lambda t: {"facts": {"us-gaap": {}}}, lambda t: None)
        assert result.status == REJECT
        assert "no quarterly facts" in result.reason

    def test_edgar_fetch_failure_is_reported_not_raised(self, cache_root):
        seed_cache("AAA", canonical(series(6, 400_000_000.0)))

        def boom(ticker):
            raise LookupError("no CIK on file for AAA")

        result = backfill_ticker("AAA", boom, lambda t: None)
        assert result.status == ERROR
        assert "no CIK" in result.reason


class TestBenchSeeding:
    def test_dry_run_verifies_a_bench_name_without_writing(self, cache_root):
        yf_frame = canonical(series(5, 300_000_000.0))
        facts, yf = _fetchers(series(14, 300_000_000.0), yf_frame=yf_frame)
        result = backfill_ticker("WDAY", facts, yf, apply=False)
        assert (result.status, result.seeded) == (ACCEPT, True)
        assert result.quarters_before == 5 and result.quarters_after == 14
        assert not list(cache_root.iterdir())

    def test_apply_seeds_the_cache_and_merges_history(self, cache_root):
        yf_frame = canonical(series(5, 300_000_000.0))
        facts, yf = _fetchers(series(14, 300_000_000.0), yf_frame=yf_frame)
        assert backfill_ticker("WDAY", facts, yf, apply=True).status == ACCEPT
        reloaded, meta = cache.load("WDAY")
        assert reloaded.shape[1] == 14
        assert meta["source"] == "yfinance"

    def test_a_failed_bench_fetch_degrades_to_an_error_row(self, cache_root):
        def boom(ticker):
            raise RuntimeError("yahoo said no")

        result = backfill_ticker("WDAY", lambda t: {}, boom)
        assert result.status == ERROR
        assert "yahoo said no" in result.reason
        assert not list(cache_root.iterdir())


class TestDryRunIsWriteFree:
    def test_cli_dry_run_leaves_the_cache_dir_byte_identical(
        self, cache_root, monkeypatch, capsys
    ):
        seed_cache("AAA", canonical(series(6, 400_000_000.0)))
        seed_cache("BBB", canonical(series(7, 500_000_000.0)))
        before = digest(cache_root)
        monkeypatch.setattr(
            backfill, "_default_fetchers", lambda: _fetchers(series(16, 400_000_000.0))
        )
        assert backfill.main(["--dry-run", "--tickers", "AAA,BBB,MNDY"]) == 0
        assert digest(cache_root) == before
        out = capsys.readouterr().out
        assert "dry run: nothing written" in out
        assert "no cache file was written" in out

    def test_dry_run_is_the_default_mode(self, cache_root, monkeypatch):
        seed_cache("AAA", canonical(series(6, 400_000_000.0)))
        before = digest(cache_root)
        monkeypatch.setattr(
            backfill, "_default_fetchers", lambda: _fetchers(series(16, 400_000_000.0))
        )
        assert backfill.main(["--tickers", "AAA"]) == 0
        assert digest(cache_root) == before


class TestReport:
    def test_report_names_field_quarter_and_both_values_on_a_mismatch(self, cache_root):
        seed_cache("AAA", canonical(series(6, 400_000_000.0)))
        edgar_values = series(14, 400_000_000.0)
        broken = sorted(edgar_values)[-1]
        edgar_values[broken]["ocf"] = 1_000_000_000.0
        facts, yf = _fetchers(edgar_values)
        text = format_report([backfill_ticker("AAA", facts, yf)])
        assert "REJECT" in text
        assert "FAIL" in text
        assert broken.date().isoformat() in text
        assert "1,000,000,000" in text

    def test_report_has_no_em_or_en_dashes(self, cache_root):
        seed_cache("AAA", canonical(series(6, 400_000_000.0)))
        facts, yf = _fetchers(series(14, 400_000_000.0))
        results = [backfill_ticker("AAA", facts, yf), backfill_ticker("MNDY", facts, yf)]
        text = format_report(results)
        assert "—" not in text and "–" not in text

    def test_summary_counts_every_status(self, cache_root):
        seed_cache("AAA", canonical(series(6, 400_000_000.0)))
        facts, yf = _fetchers(series(14, 400_000_000.0))
        results = [
            backfill_ticker("AAA", facts, yf),
            backfill_ticker("MNDY", facts, yf),
            backfill_ticker("ZZZ", facts, lambda t: (_ for _ in ()).throw(RuntimeError("x"))),
        ]
        text = format_report(results)
        assert "1 accepted, 0 rejected, 1 skipped, 1 errored" in text

    def test_filled_cells_are_reported_per_ticker_and_in_the_summary(self, cache_root):
        cached = canonical(series(6, 400_000_000.0))
        cached.loc[:, cached.columns[2]] = float("nan")  # a hollow shell column
        seed_cache("AAA", cached)
        facts, yf = _fetchers(series(14, 400_000_000.0))
        result = backfill_ticker("AAA", facts, yf)
        assert result.cells_filled == 3  # revenue, ocf, capex of the hollow column
        text = format_report([result])
        assert "3 empty cells filled" in text
        assert "3 empty cells filled inside the cached range" in text

    def test_a_cache_with_no_holes_reports_zero_filled(self, cache_root):
        seed_cache("AAA", canonical(series(6, 400_000_000.0)))
        facts, yf = _fetchers(series(14, 400_000_000.0))
        assert backfill_ticker("AAA", facts, yf).cells_filled == 0

    def test_below_target_depth_is_called_out(self, cache_root):
        seed_cache("AAA", canonical(series(6, 400_000_000.0)))
        facts, yf = _fetchers(series(8, 400_000_000.0))
        text = format_report([backfill_ticker("AAA", facts, yf)])
        assert "Below target depth after accept: AAA" in text


class TestGateArbitratedBase:
    """Verification-pass regression (live cases: PANW and FTNT are mirror
    images). The capex base must be chosen by the verification gate, not by a
    static preference: every viable base becomes a candidate frame, and the
    one that reconciles with the cached yfinance overlap wins."""

    def _cached(self, capex_values):
        cols = pd.to_datetime(["2026-04-30", "2026-01-31", "2025-10-31", "2025-07-31"])
        df = pd.DataFrame(100.0, index=CANONICAL_FIELDS, columns=cols)
        df.loc["capex"] = capex_values
        return df

    def _payload(self, ppe_entries, productive_entries):
        starts = {"2026-04-30": "2026-02-01", "2026-01-31": "2025-11-01",
                  "2025-10-31": "2025-08-01", "2025-07-31": "2025-05-01",
                  "2022-04-30": "2022-02-01"}
        def entries(pairs):
            return [
                {"start": starts[end], "end": end, "val": val,
                 "form": "10-Q", "filed": end}
                for end, val in pairs
            ]
        tags = {
            "RevenueFromContractWithCustomerExcludingAssessedTax": entries(
                [("2026-04-30", 100.0), ("2026-01-31", 100.0),
                 ("2025-10-31", 100.0), ("2025-07-31", 100.0)]),
            "NetCashProvidedByUsedInOperatingActivities": entries(
                [("2026-04-30", 100.0), ("2026-01-31", 100.0),
                 ("2025-10-31", 100.0), ("2025-07-31", 100.0)]),
            "ShareBasedCompensation": entries(
                [("2026-04-30", 100.0), ("2026-01-31", 100.0),
                 ("2025-10-31", 100.0), ("2025-07-31", 100.0)]),
        }
        if ppe_entries:
            tags["PaymentsToAcquirePropertyPlantAndEquipment"] = entries(ppe_entries)
        if productive_entries:
            tags["PaymentsToAcquireProductiveAssets"] = entries(productive_entries)
        return {"facts": {"us-gaap": {t: {"units": {"USD": e}} for t, e in tags.items()}}}

    def _run(self, payload, cached):
        return backfill_ticker(
            "XXXX", lambda t: payload, lambda t: (cached, {}), apply=False
        )

    def test_ftnt_shape_ppe_reconciles_deeper_base_does_not(self):
        # magnitudes matter: values must dwarf MATCH_ABS_FLOOR or every
        # candidate trivially verifies through the near-zero floor
        cached = self._cached([100e6, 100e6, 100e6, 100e6])
        payload = self._payload(
            ppe_entries=[("2026-04-30", 100e6), ("2026-01-31", 100e6),
                         ("2025-10-31", 100e6), ("2025-07-31", 100e6)],
            productive_entries=[("2026-04-30", 15e6), ("2026-01-31", 15e6),
                                ("2025-10-31", 15e6), ("2025-07-31", 15e6),
                                ("2022-04-30", 15e6)],  # deeper but wrong
        )
        result = self._run(payload, cached)
        assert result.status == "ACCEPT"
        assert "PropertyPlantAndEquipment" in (result.base_note or "")

    def test_panw_shape_stray_ppe_loses_to_verified_deep_base(self):
        cached = self._cached([90e6, 90e6, 90e6, 90e6])
        payload = self._payload(
            ppe_entries=[("2022-04-30", 999e6)],  # one stray, no overlap at all
            productive_entries=[("2026-04-30", 90e6), ("2026-01-31", 90e6),
                                ("2025-10-31", 90e6), ("2025-07-31", 90e6)],
        )
        result = self._run(payload, cached)
        assert result.status == "ACCEPT"
        assert "ProductiveAssets" in (result.base_note or "")

    def test_no_candidate_reconciles_rejects_with_closest_miss(self):
        cached = self._cached([100e6, 100e6, 100e6, 100e6])
        payload = self._payload(
            ppe_entries=[("2026-04-30", 55e6), ("2026-01-31", 55e6),
                         ("2025-10-31", 55e6), ("2025-07-31", 55e6)],
            productive_entries=[("2026-04-30", 15e6), ("2026-01-31", 15e6),
                                ("2025-10-31", 15e6), ("2025-07-31", 15e6)],
        )
        result = self._run(payload, cached)
        assert result.status == "REJECT"
        assert len(result.mismatches) == 4  # the closest miss is reported
