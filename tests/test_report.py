"""Report rendering + artifact writing + offline CLI run. No network anywhere."""
from __future__ import annotations

import csv

import pytest

from sentinel.config import load_config
from sentinel.indicators.fundamentals import compute_scorecard
from sentinel.report.builder import build_context, render_report, write_outputs
from sentinel.scoring import apply_scores
from tests.conftest import FIXED_TODAY


@pytest.fixture()
def scored(fixture_inputs):
    return apply_scores(
        [compute_scorecard(inp, today=FIXED_TODAY) for inp in fixture_inputs.values()]
    )


@pytest.fixture()
def html(scored):
    cfg = load_config()
    ctx = build_context(scored, cfg, run_type="dry", notes=["fixture note"], today=FIXED_TODAY)
    return render_report(ctx)


def test_report_contains_expected_content(html):
    for ticker in ("ALFA", "BRVO", "CHRL"):
        assert ticker in html
    assert "Strongest" in html
    assert "Weakest" in html
    assert "62.0" in html   # ALFA r40_fcf in points
    assert "not financial advice" in html
    assert "fixture note" in html
    assert "2026-07-05" in html


def test_report_is_inline_css_only(html):
    assert "<style" not in html   # Gmail strips <style>
    assert "flex" not in html     # tables, not flexbox
    assert 'style="' in html


def test_write_outputs(tmp_path, html, scored):
    paths = write_outputs(tmp_path, html, scored)
    assert paths["html"].read_text() == html
    with paths["csv"].open() as fh:
        rows = list(csv.DictReader(fh))
    assert {r["ticker"] for r in rows} == {"ALFA", "BRVO", "CHRL"}
    alfa = next(r for r in rows if r["ticker"] == "ALFA")
    assert float(alfa["score"]) == pytest.approx(77.5779, abs=1e-3)
    assert paths["json"].exists()


def test_cli_dry_run_end_to_end(tmp_path, capsys):
    from sentinel.run import main

    rc = main(["--dry-run", "--no-email", "--out-dir", str(tmp_path)])
    assert rc == 0
    assert (tmp_path / "report.html").exists()
    assert (tmp_path / "scores.csv").exists()
    out = capsys.readouterr().out
    assert "ALFA" in out and "r40_fcf" in out


def test_small_watchlist_weakest_empty(scored):
    cfg = load_config()  # top_n=10 > 3 fixtures
    ctx = build_context(scored, cfg, run_type="dry", notes=[], today=FIXED_TODAY)
    assert len(ctx["strongest"]) == 3
    assert ctx["weakest"] == []
