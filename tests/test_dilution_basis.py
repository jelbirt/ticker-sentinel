"""Dilution on basic shares (PLAN section 5, spec-p2-methodology D1/D2).

Diluted average shares change basis when a company turns profitable (ASC 260:
options are antidilutive in a loss quarter) and swell with the share price
(treasury stock method), so dilution reads basic average shares instead.
"""
from __future__ import annotations

from datetime import datetime, timezone

from pytest import approx

from sentinel.data import cache
from sentinel.data.fundamentals import inputs_from_canonical, sanitize_share_counts
from sentinel.indicators.fundamentals import (
    FLAG_DILUTION,
    FLAG_HIGH_SBC,
    FLAG_SBC_INFLATED,
    FundamentalInputs,
    TTMWindow,
    compute_scorecard,
    display_flags,
)
from tests.conftest import make_canonical

# CRWD's shape on 2026-10-20 (millions): a loss in the two oldest quarters,
# profit from 2026-01-31, so diluted steps +2.7% in one quarter while basic
# moves 0.5%. Diluted reads 4.5% a year; basic reads 2.0%.
CRWD_DILUTED = [1044.5, 1031.5, 1032.5, 1005.3, 999.6]
CRWD_BASIC = [1019.4, 1014.9, 1010.3, 1005.3, 999.6]


def _scorecard(**shares):
    return compute_scorecard(FundamentalInputs(
        ticker="X", ttm_now=TTMWindow(revenue=100.0), **shares
    ))


class TestDilutionBasis:
    def test_profit_turn_does_not_read_as_issuance(self):
        sc = _scorecard(
            diluted_shares_now=CRWD_DILUTED[0], diluted_shares_1y_ago=CRWD_DILUTED[4],
            basic_shares_now=CRWD_BASIC[0], basic_shares_1y_ago=CRWD_BASIC[4],
        )
        assert sc.dilution == approx(1019.4 / 999.6 - 1)
        assert FLAG_DILUTION not in sc.flags

    def test_real_issuance_still_flags_on_basic(self):
        # PANW's acquisition issuance: 22% on basic
        sc = _scorecard(
            diluted_shares_now=817.0, diluted_shares_1y_ago=709.0,
            basic_shares_now=800.0, basic_shares_1y_ago=655.7,
        )
        assert sc.dilution == approx(800.0 / 655.7 - 1)
        assert FLAG_DILUTION in sc.flags

    def test_falls_back_to_diluted_without_a_basic_pair(self):
        # a cache written before the basic row existed (D2)
        sc = _scorecard(
            diluted_shares_now=CRWD_DILUTED[0], diluted_shares_1y_ago=CRWD_DILUTED[4],
            basic_shares_now=CRWD_BASIC[0], basic_shares_1y_ago=None,
        )
        assert sc.dilution == approx(1044.5 / 999.6 - 1)

    def test_inputs_pair_basic_from_the_canonical_frame(self):
        df = make_canonical({
            "revenue": [100.0] * 5,
            "diluted_shares": CRWD_DILUTED,
            "basic_shares": CRWD_BASIC,
        })
        inp = inputs_from_canonical("X", df, None)
        assert (inp.basic_shares_now, inp.basic_shares_1y_ago) == (1019.4, 999.6)
        assert (inp.diluted_shares_now, inp.diluted_shares_1y_ago) == (1044.5, 999.6)


class TestShareGuardCoversBasic:
    def test_off_scale_basic_cell_dropped_with_its_own_label(self):
        df = make_canonical({
            "diluted_shares": [1044.5e6] * 5,
            "basic_shares": [1019.4e6, 1014.9e6, 1010.3, 1005.3e6, 999.6e6],  # units slip
        })
        notes: list[str] = []
        out = sanitize_share_counts("CRWD", df, 1023.9e6, notes)
        assert out.loc["basic_shares"].isna().sum() == 1
        assert out.loc["diluted_shares"].notna().all()
        assert any("basic share count implausible" in n for n in notes)

    def test_split_rebases_cached_basic_quarters(self):
        # fresh (post-split) quarters overlap the cache 4x; cached-only tail rebases
        cached = make_canonical({"basic_shares": [250.0, 249.0, 248.0, 247.0, 246.0]})
        fresh = cached.iloc[:, :2] * 4
        merged = cache.merge_statements(cached, fresh)
        assert list(merged.loc["basic_shares"]) == approx([1000.0, 996.0, 992.0, 988.0, 984.0])


class TestDisplayFlags:
    def test_second_tier_hides_first_tier_label(self):
        assert display_flags([FLAG_HIGH_SBC, FLAG_SBC_INFLATED, FLAG_DILUTION]) == [
            FLAG_SBC_INFLATED, FLAG_DILUTION,
        ]

    def test_first_tier_alone_is_shown(self):
        assert display_flags([FLAG_HIGH_SBC]) == [FLAG_HIGH_SBC]

    def test_both_tiers_flag_from_sbc_intensity(self):
        sc = compute_scorecard(FundamentalInputs(
            ticker="X", ttm_now=TTMWindow(revenue=100.0, sbc=25.0),
        ))
        assert sc.sbc_intensity == approx(0.25)
        assert FLAG_HIGH_SBC in sc.flags and FLAG_SBC_INFLATED in sc.flags


class TestSchemaRefetch:
    """A cache written before the basic_shares row refetches once, so every
    name moves to the new basis on the same run (D3 note stays truthful)."""

    def _seed(self, tmp_path, monkeypatch, with_basic):
        from sentinel.data import fundamentals as fnd

        monkeypatch.setenv("SENTINEL_ROOT", str(tmp_path))
        fields = {"revenue": [100.0] * 4}
        frame = make_canonical(fields)
        if not with_basic:
            frame = frame.drop(index="basic_shares")
        cache.save("X", frame, {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "company_name": "X Corp",
        })
        calls: list[str] = []

        def recording_fetch(ticker):
            calls.append(ticker)
            raise RuntimeError("refetch attempted")

        monkeypatch.setattr(fnd, "fetch_statements", recording_fetch)
        return fnd, calls

    def test_cache_without_basic_row_refetches(self, tmp_path, monkeypatch):
        fnd, calls = self._seed(tmp_path, monkeypatch, with_basic=False)
        fnd.get_fundamentals("X")
        assert calls == ["X"]

    def test_cache_with_basic_row_is_served(self, tmp_path, monkeypatch):
        fnd, calls = self._seed(tmp_path, monkeypatch, with_basic=True)
        fnd.get_fundamentals("X")
        assert calls == []


class TestMethodChangeNote:
    """D3: the first run on a new scoring method names the cause once."""

    def _run(self, method):
        from sentinel.report.changes import RunSnapshot
        return RunSnapshot(date="2026-10-21", run_type="scheduled", method=method)

    def test_history_without_the_field_reads_as_method_1(self):
        from sentinel.report.changes import RunSnapshot
        assert RunSnapshot.from_dict({"date": "2026-10-20", "run_type": "scheduled"}).method == 1

    def test_round_trip_keeps_method(self):
        from sentinel.report.changes import RunSnapshot
        assert RunSnapshot.from_dict(self._run(2).to_dict()).method == 2

    def test_note_on_the_switch_run_only(self):
        from sentinel.report.changes import SCORING_METHOD, method_change_note
        current = self._run(SCORING_METHOD)
        note = method_change_note(self._run(1), current)
        assert note and "basic shares" in note
        assert "—" not in note and "–" not in note
        assert method_change_note(self._run(SCORING_METHOD), current) is None
        assert method_change_note(None, current) is None


class TestMethodWiring:
    """The note only works end to end if snapshots are stamped and run.py
    surfaces it; both are pinned here, not just the helper."""

    def test_snapshots_are_stamped_with_the_current_method(self):
        from datetime import date

        from sentinel.report.changes import SCORING_METHOD, snapshot_from_scorecards
        assert snapshot_from_scorecards([], date(2026, 10, 21), "scheduled").method == SCORING_METHOD

    def _dry_run_html(self, tmp_path, monkeypatch, strip_method):
        import json

        from sentinel.data import fixtures
        from sentinel.run import main

        if strip_method:
            raw = json.loads(fixtures.fixture_history_path().read_text())
            for run in raw["runs"]:
                run.pop("method", None)
            old = tmp_path / "history.json"
            old.write_text(json.dumps(raw))
            monkeypatch.setattr(fixtures, "fixture_history_path", lambda: old)
        out = tmp_path / "out"
        assert main(["--dry-run", "--no-email", "--out-dir", str(out)]) == 0
        return (out / "report.html").read_text()

    def test_first_run_on_new_method_carries_the_note(self, tmp_path, monkeypatch):
        html = self._dry_run_html(tmp_path, monkeypatch, strip_method=True)
        assert "scoring method changed since the last run" in html

    def test_no_note_once_history_is_on_the_method(self, tmp_path, monkeypatch):
        html = self._dry_run_html(tmp_path, monkeypatch, strip_method=False)
        assert "scoring method changed" not in html


class TestDigestAndFallbackDisclosure:
    def test_digest_flag_cell_dedupes_sbc_tiers(self):
        from sentinel.digest import _flag_cell
        assert _flag_cell([FLAG_HIGH_SBC, FLAG_SBC_INFLATED]) == "sbc inflated"

    def test_diluted_fallback_is_disclosed(self):
        df = make_canonical({"revenue": [100.0] * 5, "diluted_shares": CRWD_DILUTED})
        notes: list[str] = []
        inputs_from_canonical("X", df, None, notes=notes)
        assert any("dilution on diluted shares" in n for n in notes)

    def test_no_disclosure_once_basic_is_paired(self):
        df = make_canonical({
            "revenue": [100.0] * 5, "diluted_shares": CRWD_DILUTED, "basic_shares": CRWD_BASIC,
        })
        notes: list[str] = []
        inputs_from_canonical("X", df, None, notes=notes)
        assert not any("dilution on diluted shares" in n for n in notes)
