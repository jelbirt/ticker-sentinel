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
    from sentinel.data.fixtures import fixture_signals

    cards = apply_scores(
        [compute_scorecard(inp, today=FIXED_TODAY) for inp in fixture_inputs.values()]
    )
    sigs = fixture_signals()
    for sc in cards:
        sc.signals = sigs.get(sc.ticker)
    return cards


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


def test_company_names_rendered(html):
    assert "Alfa Systems, Inc." in html
    assert "Bravo Cloud Corp." in html


def test_legend_rendered(html):
    assert "How to read this report" in html
    assert "Rule of 40" in html
    assert "stock-based compensation" in html
    assert "free cash flow" in html
    # alert terms are always defined (movers can mention them any day)...
    assert "relative strength index" in html
    assert "RS 3m" in html
    assert "earnings per share" in html          # EPS expanded
    assert "Golden / death cross" in html
    assert "FINRA" in html
    # ...but the deep-grid glossary only ships with --deep
    assert "Deep-dive grid" not in html


def test_deep_legend_and_grid(scored):
    cfg = load_config()
    ctx = build_context(scored, cfg, run_type="dry", notes=[], today=FIXED_TODAY)
    ctx["deep"] = True
    deep_html = render_report(ctx)
    assert "Deep dive — full metric grid" in deep_html
    assert "Deep-dive grid" in deep_html
    assert "Rule of X" in deep_html
    assert "enterprise value" in deep_html
    assert "FCF yield" in deep_html


def test_breadth_ranking_beats_raw_score():
    """A name passing all 3 R40 variants outranks a higher-scoring 1-variant name."""
    from dataclasses import replace

    from sentinel.indicators.fundamentals import Scorecard
    from sentinel.report.builder import rank_key

    all_three = Scorecard(
        ticker="AAA", r40_fcf=0.45, r40_ebitda=0.42, r40_sbc_adj=0.41, score=50.0
    )
    one_only = Scorecard(
        ticker="BBB", r40_fcf=0.55, r40_ebitda=0.30, r40_sbc_adj=0.25, score=70.0
    )
    ranked = sorted([one_only, all_three], key=lambda s: rank_key(s, "breadth"))
    assert [s.ticker for s in ranked] == ["AAA", "BBB"]
    # plain score mode preserves old behavior
    ranked = sorted([one_only, all_three], key=lambda s: rank_key(s, "score"))
    assert [s.ticker for s in ranked] == ["BBB", "AAA"]
    # None variants never count toward breadth
    missing = replace(all_three, r40_ebitda=None, r40_sbc_adj=None)
    ranked = sorted([one_only, missing], key=lambda s: rank_key(s, "breadth"))
    assert [s.ticker for s in ranked] == ["BBB", "AAA"]


def test_signals_table_rendered(html):
    assert "Between-quarter signals" in html
    assert "▲6" in html                       # ALFA estimate revisions up
    assert "+150k" in html                    # ALFA insider net buying
    assert "−400k" in html                    # BRVO insider net selling
    assert "Insider net 6m" in html           # legend/table header


def test_signal_alerts_reach_movers(html):
    assert "ALFA: estimates revised up by 6 analysts" in html
    assert "BRVO: short interest up 33% month-over-month" in html
    assert "CHRL: estimates revised down" in html
    assert "CHRL: analyst bullishness slipping (4 → 2" in html


def test_ranking_explanation_and_conditional_bold(html):
    assert "then by Comp" in html            # legend matches the actual sort key
    assert 'font-weight:bold;">62.0' in html     # ALFA r40_fcf ≥ 40 → bold
    assert 'font-weight:bold;">53.9' in html     # ALFA r40_sbc_adj ≥ 40 → bold
    assert 'font-weight:bold;">-19.6' not in html  # CHRL failing variant not emphasized
    assert 'font-weight:bold;">35.0' not in html   # BRVO r40_fcf below 40 not bold


def test_weakest_rows_keep_reason_and_flags(scored):
    from sentinel.config import Config

    cfg = Config(universe=(), top_n=1, bottom_n=2)  # force BRVO/CHRL into weakest
    ctx = build_context(scored, cfg, run_type="dry", notes=[], today=FIXED_TODAY)
    assert [sc.ticker for sc in ctx["strongest"]] == ["ALFA"]
    assert len(ctx["weakest"]) == 2
    html = render_report(ctx)
    assert "Why weakest" in html
    assert "⚠ Dilution" in html and "⚠ High SBC" in html  # flags survive in weakest cells


def test_small_watchlist_weakest_empty(scored):
    cfg = load_config()  # top_n=10 > 3 fixtures
    ctx = build_context(scored, cfg, run_type="dry", notes=[], today=FIXED_TODAY)
    assert len(ctx["strongest"]) == 3
    assert ctx["weakest"] == []
