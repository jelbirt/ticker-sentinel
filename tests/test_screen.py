"""Bench screen: in-memory scoring, the on-entry test, and the write-free CLI.

Offline only: EDGAR and yfinance are injected as fakes (helpers shared with
the backfill tests, which own the frame and payload builders).
"""
from __future__ import annotations

import re

import pytest

from sentinel import backfill, screen
from sentinel.config import ChangesCfg, Config
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
        # same name against a tail it cannot clear
        tight = {"A": 1.0, "B": 2.0, "C": 80.0, "D": 85.0, "E": 90.0}
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
        assert "none clears the tail on entry" in text

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

    def test_joined_name_scores_from_its_bench_snapshot_and_left_name_is_bench(
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
        assert [e.ticker for e in base.bench] == ["OLD"]
        assert base.moved_out == ("OLD",) and base.moved_in == ("NEW",)
        text = format_report([], base, cfg)
        assert "left: OLD; joined: NEW" in text

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
        assert base.bench[0].clears is None
        assert base.bench_blocked[0][0] == "NEW"
        assert "cannot be swapped in yet: NEW (warm-up" in format_report([], base, cfg)

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
