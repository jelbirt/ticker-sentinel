"""Bench screen: in-memory scoring, the on-entry test, and the write-free CLI.

Offline only: EDGAR and yfinance are injected as fakes (helpers shared with
the backfill tests, which own the frame and payload builders).
"""
from __future__ import annotations

import re

import pytest

from sentinel import backfill, screen
from sentinel.config import ChangesCfg, Config
from sentinel.digest import MID_WINDOW
from sentinel.report.changes import RunSnapshot, TickerSnapshot
from sentinel.rotation import Entrant
from sentinel.screen import Baseline, format_report, run_screen, screen_ticker
from tests.test_backfill import _fetchers, canonical, digest, seed_cache, series

CFG = Config(universe=(), changes=ChangesCfg())
# a fast grower: revenue falls 20M a quarter going back, so TTM growth is ~40%
FAST = dict(n=14, base=300_000_000.0, step=20_000_000.0)
# a slow grower: 1M a quarter, about 1.4% growth
SLOW = dict(n=14, base=300_000_000.0, step=1_000_000.0)


def _series(spec: dict, n: int | None = None):
    return series(n or spec["n"], spec["base"], spec["step"])


def _yf(spec: dict):
    return canonical(_series(spec, n=5))


@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_ROOT", str(tmp_path))
    d = tmp_path / "data" / "cache"
    d.mkdir(parents=True)
    return d


def _baseline(scores: dict[str, float], outgoing: str | None = "A",
              bench: list[Entrant] | None = None) -> Baseline:
    return Baseline("2026-11-14", scores, outgoing, True, bench or [])


LOW_TAIL = {"A": 1.0, "B": 2.0, "C": 3.0, "D": 4.0, "E": 90.0}


class TestScreenTicker:
    def test_verified_history_makes_the_name_live_and_writes_nothing(self, cache_root):
        facts, yf = _fetchers(_series(FAST), yf_frame=_yf(FAST))
        row = screen_ticker("NEWCO", facts, yf, _baseline(LOW_TAIL), CFG)
        assert row.error is None
        assert row.history == "EDGAR verified, 14 quarters" and row.quarters == 14
        assert row.live and row.r40_trend is not None
        assert row.growth is not None and row.growth >= 0.15 and row.tier == "growth"
        assert not list(cache_root.iterdir())

    def test_rejected_history_scores_on_yfinance_alone(self, cache_root):
        facts, yf = _fetchers(series(14, 999_000_000.0), yf_frame=_yf(FAST))
        row = screen_ticker("BADCO", facts, yf, _baseline(LOW_TAIL), CFG)
        assert row.history.startswith("yfinance only, 5 quarters (reject:")
        assert not row.live

    def test_a_failed_fetch_is_an_error_row_not_a_crash(self, cache_root):
        def boom(ticker):
            raise RuntimeError("yahoo said no")

        row = screen_ticker("GONE", lambda t: {}, boom, _baseline(LOW_TAIL), CFG)
        assert row.error is not None and "yahoo said no" in row.error

    def test_on_entry_test_removes_the_outgoing_name(self, cache_root):
        facts, yf = _fetchers(_series(SLOW), yf_frame=_yf(SLOW))
        row = screen_ticker("SLOWCO", facts, yf, _baseline(LOW_TAIL), CFG)
        assert row.score is not None and row.score > 4.0
        assert row.clears is True          # B, C, D below once A leaves
        # the outgoing name A must not count: with it, A, B and C (three) sit
        # below and the name would wrongly clear; without it, only B and C
        tight = {"A": 1.0, "B": 2.0, "C": 3.0, "D": 80.0, "E": 90.0}
        assert row.score < 80.0
        facts, yf = _fetchers(_series(SLOW), yf_frame=_yf(SLOW))
        assert screen_ticker("SLOWCO", facts, yf, _baseline(tight), CFG).clears is False

    def test_no_history_means_no_on_entry_verdict(self, cache_root):
        facts, yf = _fetchers(_series(FAST), yf_frame=_yf(FAST))
        assert screen_ticker("NEWCO", facts, yf, None, CFG).clears is None


class TestReport:
    def _rows(self, cache_root):
        fast_f, fast_y = _fetchers(_series(FAST), yf_frame=_yf(FAST))
        slow_f, slow_y = _fetchers(_series(SLOW), yf_frame=_yf(SLOW))
        base = _baseline(LOW_TAIL)
        return base, [
            screen_ticker("FAST", fast_f, fast_y, base, CFG),
            screen_ticker("SLOW", slow_f, slow_y, base, CFG),
        ]

    def test_growth_candidate_is_the_pick_and_listed_first(self, cache_root):
        base, rows = self._rows(cache_root)
        assert rows[0].clears and rows[1].clears     # both clear: tier decides
        text = format_report(list(reversed(rows)), base, CFG)
        body = text.split("| --- " * 10 + "|")[1]
        assert body.index("| FAST |") < body.index("| SLOW |")
        assert "Rule pick over the current bench and the live candidates: FAST (a candidate, growth tier" in text
        assert "Nothing was written." in text

    def test_growth_tier_sorts_ahead_of_a_higher_scoring_slow_grower(self):
        from sentinel.screen import ScreenRow
        rows = [
            ScreenRow("SLOWHI", score=90.0, growth=0.10, tier="fallback", clears=True, live=True),
            ScreenRow("GROLO", score=30.0, growth=0.30, tier="growth", clears=True, live=True),
        ]
        text = format_report(rows, _baseline(LOW_TAIL), CFG)
        body = text.split("| --- " * 10 + "|")[1]
        assert body.index("| GROLO |") < body.index("| SLOWHI |")
        assert "live candidates: GROLO (a candidate, growth tier" in text

    def test_bench_name_can_be_the_pick(self, cache_root):
        base, rows = self._rows(cache_root)
        base = _baseline(LOW_TAIL, bench=[Entrant("BENCH", 95.0, "growth", True)])
        text = format_report([r for r in rows if r.ticker == "SLOW"], base, CFG)
        assert "BENCH (already on the bench, growth tier" in text

    def test_warm_up_candidates_never_become_the_pick(self, cache_root):
        facts, yf = _fetchers(series(14, 999_000_000.0), yf_frame=_yf(FAST))
        row = screen_ticker("BADCO", facts, yf, _baseline(LOW_TAIL), CFG)
        text = format_report([row], _baseline(LOW_TAIL), CFG)
        assert ("Rule pick over the current bench and the live candidates: none "
                "of the eligible names clears the tail on entry.") in text

    def test_values_just_under_a_floor_never_print_as_the_floor(self):
        from sentinel.screen import ScreenRow
        row = ScreenRow("NEAR", score=50.0, growth=0.1496, tier="fallback",
                        r40_fcf=0.3996, r40_trend=0.01, live=True, clears=False,
                        history="yfinance only")
        text = format_report([row], _baseline(LOW_TAIL), CFG)
        assert "| 14.96 | fallback |" in text and "| 39.96 fail |" in text
        assert "15.0 |" not in text and "40.0 fail" not in text

    def test_pick_line_names_a_warm_up_row_the_table_shows_clearing(self):
        from sentinel.screen import ScreenRow
        rows = [ScreenRow("WARM", score=95.0, growth=0.40, tier="growth",
                          clears=True, live=False)]
        text = format_report(rows, _baseline(LOW_TAIL), CFG)
        assert "none of the eligible names clears the tail on entry" in text
        assert ("Would clear the tail but not eligible yet: WARM (warm-up, "
                "growth tier).") in text
        # warm-up has no set end, so it never puts off widening the pool
        assert "Widen the pool" in text

    def test_a_live_clearing_candidate_is_never_listed_as_waiting(self):
        from sentinel.screen import ScreenRow
        rows = [ScreenRow("LIVE", score=95.0, growth=0.40, tier="growth",
                          clears=True, live=True)]
        text = format_report(rows, _baseline(LOW_TAIL), CFG)
        assert "live candidates: LIVE (a candidate, growth tier" in text
        assert "not eligible yet" not in text

    def test_a_mid_window_bench_name_puts_widening_off_one_window(self):
        base = Baseline(
            "2026-11-14", LOW_TAIL, "A", True, [],
            bench_blocked=(("NEW", MID_WINDOW),), bench_waiting=("NEW",),
            bench_waiting_tiers=(("NEW", "growth"),),
        )
        text = format_report([], base, CFG)
        assert ("none of the eligible names clears the tail on entry. Recheck "
                "after NEW has a full window on the bench before widening the "
                "pool.") in text
        assert ("Would clear the tail but not eligible yet: NEW (on the bench, "
                "mid-window, growth tier).") in text
        assert "Widen the pool" not in text
        assert "\n\n\n" not in text          # one blank line before the pick

    def test_after_a_merged_swap_the_pick_is_a_preview(self):
        base = Baseline(
            "2026-11-14", LOW_TAIL, "A", True,
            [Entrant("BENCH", 95.0, "growth", True)],
            moved_out=("ZS",), moved_in=("APPF",),
        )
        text = format_report([], base, CFG)
        assert "(at most one swap per round), so the outgoing name and the rule pick preview the next round" in text
        assert ("Rule pick over the current bench and the live candidates "
                "(preview of the next round): BENCH") in text

    def test_within_a_tier_a_clearing_name_sorts_ahead_of_a_higher_score(self):
        from sentinel.screen import ScreenRow
        rows = [
            ScreenRow("HIGHNO", score=80.0, growth=0.30, tier="growth", clears=False, live=True),
            ScreenRow("LOWYES", score=30.0, growth=0.30, tier="growth", clears=True, live=True),
        ]
        body = format_report(rows, _baseline(LOW_TAIL), CFG).split("| --- " * 10 + "|")[1]
        assert body.index("| LOWYES |") < body.index("| HIGHNO |")

    def test_a_data_quality_flag_makes_a_candidate_not_live(self, cache_root, monkeypatch):
        from dataclasses import replace

        real = screen.apply_scores

        def flagged(cards, **kw):
            return [replace(c, flags=[*c.flags, "growth_from_annual"]) for c in real(cards, **kw)]

        monkeypatch.setattr(screen, "apply_scores", flagged)
        facts, yf = _fetchers(_series(FAST), yf_frame=_yf(FAST))
        row = screen_ticker("FAST", facts, yf, _baseline(LOW_TAIL), CFG)
        assert row.r40_trend is not None and row.live is False

    def test_report_is_plain_ascii_without_dashes(self, cache_root):
        base, rows = self._rows(cache_root)
        text = format_report(rows, base, CFG) + format_report([], None, CFG)
        assert not re.search("[\u2013\u2014]", text)
        assert text.isascii()


class TestCli:
    def test_cli_screens_the_given_tickers_and_leaves_the_cache_untouched(
        self, cache_root, monkeypatch, capsys
    ):
        seed_cache("AAA", canonical(series(6, 400_000_000.0)))
        history = cache_root / "run_history.json"
        run = RunSnapshot(
            "2026-11-14", "scheduled",
            {t: TickerSnapshot(composite=s, score=s, r40_trend=0.01)
             for t, s in LOW_TAIL.items()},
        )
        history.write_text('{"version": 1, "runs": [' + __import__("json").dumps(run.to_dict()) + "]}")
        before = digest(cache_root)
        monkeypatch.setattr(
            backfill, "_default_fetchers",
            lambda: _fetchers(_series(FAST), yf_frame=_yf(FAST)),
        )
        cfg_path = cache_root.parent.parent / "watchlist.yaml"
        cfg_path.write_text("universe:\n  - ticker: A\n    tags: [r40]\n")
        assert screen.main(["--tickers", "newco", "--config", str(cfg_path),
                            "--history", str(history)]) == 0
        out = capsys.readouterr().out
        assert "| NEWCO |" in out and "Measured against the 2026-11-14 run" in out
        assert digest(cache_root) == before

    def test_empty_pool_says_how_to_fill_it(self, cache_root, capsys):
        cfg_path = cache_root.parent.parent / "watchlist.yaml"
        cfg_path.write_text("universe:\n  - ticker: A\n")
        assert screen.main(["--config", str(cfg_path)]) == 0
        assert "No candidates" in capsys.readouterr().out


    def test_universe_and_bench_names_are_not_screened(self, cache_root, monkeypatch, capsys):
        monkeypatch.setattr(
            backfill, "_default_fetchers",
            lambda: _fetchers(_series(FAST), yf_frame=_yf(FAST)),
        )
        cfg_path = cache_root.parent.parent / "watchlist.yaml"
        cfg_path.write_text("universe:\n  - ticker: A\n    tags: [r40]\nbench: [BEN]\n")
        assert screen.main(["--tickers", "a,ben", "--config", str(cfg_path)]) == 0
        assert "Nothing to screen: A, BEN already scored" in capsys.readouterr().out
        assert screen.main(["--tickers", "a,newco", "--config", str(cfg_path)]) == 0
        out = capsys.readouterr().out
        assert "| NEWCO |" in out and "| A |" not in out
        assert "Not screened, already scored by the pipeline: A." in out

    def test_repeated_tickers_are_screened_and_named_once(self, cache_root, monkeypatch, capsys):
        seen: list[list[str]] = []
        monkeypatch.setattr(
            screen, "run_screen",
            lambda tickers, cfg, baseline: seen.append(list(tickers)) or [],
        )
        cfg_path = cache_root.parent.parent / "watchlist.yaml"
        cfg_path.write_text("universe:\n  - ticker: A\n    tags: [r40]\nbench: [BEN]\n")
        assert screen.main(["--tickers", "newco,NEWCO,ben,ben", "--config", str(cfg_path)]) == 0
        assert seen == [["NEWCO"]]
        assert "already scored by the pipeline: BEN." in capsys.readouterr().out


class TestDegrades:
    def test_edgar_down_scores_every_name_on_yfinance(self, cache_root, monkeypatch):
        def down():
            raise ConnectionError("sec.gov unreachable")

        monkeypatch.setattr(backfill, "_default_fetchers", down)
        _, yf = _fetchers(_series(FAST), yf_frame=_yf(FAST))
        rows = run_screen(["X"], CFG, None, fetch_yfinance=yf)
        # the sweep finished and the row says why it is yfinance-only
        assert rows[0].error is None
        assert "yfinance only" in rows[0].history and "EDGAR unavailable" in rows[0].history

    def test_backfill_exception_is_a_yfinance_row_not_a_crash(self, cache_root, monkeypatch):
        def boom(*_a, **_k):
            raise RuntimeError("merge failed")

        monkeypatch.setattr(screen, "backfill_ticker", boom)
        facts, yf = _fetchers(_series(FAST), yf_frame=_yf(FAST))
        row = screen_ticker("X", facts, yf, None, CFG)
        assert row.error is None and "backfill failed (merge failed)" in row.history

    def test_no_history_says_why_there_is_no_pick(self, cache_root):
        facts, yf = _fetchers(_series(FAST), yf_frame=_yf(FAST))
        text = format_report([screen_ticker("X", facts, yf, None, CFG)], None, CFG)
        assert "Rule pick: n/a (no scored run to test against)." in text
        assert "Widen the pool" not in text


def test_run_screen_uses_injected_fetchers(cache_root):
    facts, yf = _fetchers(_series(FAST), yf_frame=_yf(FAST))
    rows = run_screen(["X", "Y"], CFG, None, facts, yf)
    assert [r.ticker for r in rows] == ["X", "Y"]


class TestBaselineAfterASwap:
    """Between a merged swap and the next scheduled run, the latest run still
    holds the old roles. The baseline must follow the current config."""

    def _history(self, cache_root):
        import json

        run = RunSnapshot(
            "2026-11-14", "scheduled",
            {t: TickerSnapshot(composite=s, score=s, r40_trend=0.01)
             for t, s in {"OLD": 1.0, "B": 2.0, "C": 3.0, "D": 4.0, "E": 90.0}.items()},
            {"NEW": TickerSnapshot(composite=60.0, score=60.0, growth=0.2, r40_trend=0.01)},
        )
        path = cache_root / "run_history.json"
        path.write_text(json.dumps({"version": 1, "runs": [run.to_dict()]}))
        return path

    def test_joined_name_scores_from_its_bench_snapshot_and_left_name_waits(
        self, cache_root
    ):
        from sentinel.config import TickerCfg
        from sentinel.screen import baseline_from_history

        cfg = Config(
            universe=tuple(TickerCfg(t, ("r40",)) for t in ("B", "C", "D", "E", "NEW")),
            bench=("OLD",),
        )
        base = baseline_from_history(cfg, self._history(cache_root))
        assert base is not None
        assert set(base.universe_scores) == {"B", "C", "D", "E", "NEW"}
        assert base.universe_scores["NEW"] == 60.0
        assert base.outgoing == "B" and base.outgoing_qualifies is False
        # OLD was scored in the universe, never on the bench: it has had no
        # shadow window yet, so it is named and never an entrant (the digest
        # would say the same about it on the next run)
        assert base.bench == []
        assert base.bench_blocked == (("OLD", "not shadow-scored on the bench this window"),)
        assert base.moved_out == ("OLD",) and base.moved_in == ("NEW",)
        text = format_report([], base, cfg)
        assert "left: OLD; joined: NEW" in text
        assert "cannot be swapped in yet: OLD (not shadow-scored" in text

    def test_universe_name_missing_from_the_run_has_not_joined(self, cache_root):
        # a fetch miss is an absence, not a role change
        from sentinel.config import TickerCfg
        from sentinel.screen import baseline_from_history

        cfg = Config(
            universe=tuple(TickerCfg(t, ("r40",)) for t in ("OLD", "B", "C", "D", "E", "GONE")),
            bench=("NEW",),
        )
        base = baseline_from_history(cfg, self._history(cache_root))
        assert base.moved_in == () and base.moved_out == ()

    def test_warm_up_bench_name_is_blocked_from_the_pick(self, cache_root):
        import json

        from sentinel.config import TickerCfg
        from sentinel.screen import baseline_from_history

        path = self._history(cache_root)
        data = json.loads(path.read_text())
        data["runs"][0]["bench"]["NEW"]["r40_trend"] = None
        path.write_text(json.dumps(data))
        cfg = Config(
            universe=tuple(TickerCfg(t, ("r40",)) for t in ("OLD", "B", "C", "D", "E")),
            bench=("NEW",),
        )
        base = baseline_from_history(cfg, path)
        assert base.bench == []                  # never in the pick pool
        assert base.bench_blocked[0][0] == "NEW"
        # NEW (60.0) would clear once live, but warm-up has no set end, so
        # it is named as waiting and the pool is still to be widened
        assert base.bench_waiting == ("NEW",)
        text = format_report([], base, cfg)
        assert "cannot be swapped in yet: NEW (warm-up" in text
        assert "none of the eligible names clears the tail on entry" in text
        assert "Would clear the tail but not eligible yet: NEW (on the bench, warm-up" in text
        assert "Widen the pool" in text

    def test_bench_name_missing_from_the_latest_run_is_read_like_the_digest(
        self, cache_root
    ):
        # GRO's fetch failed on the latest run only: the digest still reads
        # its last observed score and picks it, so the screen must too
        import json

        from sentinel.config import TickerCfg
        from sentinel.digest import build_digest
        from sentinel.screen import baseline_from_history

        universe = {"A": 10.0, "B": 20.0, "C": 30.0, "D": 80.0, "E": 90.0}
        runs = [
            RunSnapshot(
                f"2026-11-{10 + i:02d}", "scheduled",
                {t: TickerSnapshot(composite=s, score=s, r40_trend=0.01)
                 for t, s in universe.items()},
                {"GRO": TickerSnapshot(composite=85.0, score=85.0, growth=0.3, r40_trend=0.01),
                 "LOW": TickerSnapshot(composite=5.0, score=5.0, growth=0.3, r40_trend=0.01)},
            )
            for i in range(5)
        ]
        del runs[-1].bench["GRO"]
        path = cache_root / "run_history.json"
        path.write_text(json.dumps({"version": 1, "runs": [r.to_dict() for r in runs]}))
        cfg = Config(
            universe=tuple(TickerCfg(t, ("r40",)) for t in universe),
            bench=("GRO", "LOW"),
        )
        assert build_digest(runs, list(universe), ["GRO", "LOW"], cfg.changes).swap_pick == "GRO"
        base = baseline_from_history(cfg, path)
        entrants = {e.ticker: e for e in base.bench}
        assert entrants["GRO"] == Entrant("GRO", 85.0, "growth", True)
        assert entrants["LOW"].clears is False and base.bench_blocked == ()
        assert "Rule pick over the current bench and the live candidates: GRO" in (
            format_report([], base, cfg)
        )

    def test_unknown_growth_pick_is_said_as_unknown_not_slow(self, cache_root):
        # every bench name reads growth n/a until runs record it (history
        # written before this feature has no growth key)
        from dataclasses import replace

        base = replace(_baseline({"A": 1.0, "B": 2.0, "C": 3.0, "D": 4.0}),
                       bench=[Entrant("BEN", 95.0, None, True)])
        text = format_report([], base, CFG)
        assert "BEN (already on the bench, growth n/a, so ranked with the slower growers" in text
        assert "slower grower, fallback" not in text

    def test_qualifier_already_moved_to_the_bench_is_not_the_outgoing_name(
        self, cache_root
    ):
        # 15 runs: Q, B, C hold the bottom 3 for three windows and all
        # qualify; Q has since been moved to the bench by a merged swap
        import json

        from sentinel.config import TickerCfg
        from sentinel.screen import baseline_from_history

        scores = {"Q": 1.0, "B": 2.0, "C": 3.0, "D": 50.0, "E": 90.0}
        runs = [
            RunSnapshot(
                f"2026-10-{1 + i:02d}", "scheduled",
                {t: TickerSnapshot(composite=s, score=s, r40_trend=0.01)
                 for t, s in scores.items()},
                {"NEW": TickerSnapshot(composite=60.0, score=60.0, growth=0.2,
                                       r40_trend=0.01)},
            )
            for i in range(15)
        ]
        path = cache_root / "run_history.json"
        path.write_text(json.dumps({"version": 1, "runs": [r.to_dict() for r in runs]}))
        cfg = Config(
            universe=tuple(TickerCfg(t, ("r40",)) for t in ("B", "C", "D", "E", "NEW")),
            bench=("Q",),
        )
        base = baseline_from_history(cfg, path)
        assert base.outgoing == "B" and base.outgoing_qualifies
        assert base.moved_out == ("Q",) and base.moved_in == ("NEW",)

    def test_matching_run_says_nothing_about_roles(self, cache_root):
        from sentinel.config import TickerCfg
        from sentinel.screen import baseline_from_history

        cfg = Config(
            universe=tuple(TickerCfg(t, ("r40",)) for t in ("OLD", "B", "C", "D", "E")),
            bench=("NEW",),
        )
        base = baseline_from_history(cfg, self._history(cache_root))
        assert base.moved_out == () and base.moved_in == ()
        new = base.bench[0]
        assert new.ticker == "NEW" and new.tier == "growth" and new.clears is True
        assert "predates the current watchlist" not in format_report([], base, cfg)


def test_warm_up_growth_name_sorts_below_live_growth_names():
    from sentinel.screen import ScreenRow

    rows = [
        ScreenRow("WARM", score=95.0, growth=0.40, tier="growth", clears=True, live=False),
        ScreenRow("LIVE", score=40.0, growth=0.20, tier="growth", clears=True, live=True),
    ]
    text = format_report(rows, _baseline(LOW_TAIL), CFG)
    body = text.split("| --- " * 10 + "|")[1]
    assert body.index("| LIVE |") < body.index("| WARM |")
    assert "live candidates: LIVE (a candidate" in text
