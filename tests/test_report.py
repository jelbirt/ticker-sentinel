"""Report rendering + artifact writing + offline CLI run. No network anywhere."""
from __future__ import annotations

import csv

import pytest

from sentinel.config import ChangesCfg, Config
from sentinel.indicators.fundamentals import compute_scorecard
from sentinel.report.builder import build_context, render_report, write_outputs
from sentinel.scoring import apply_scores
from tests.conftest import FIXED_TODAY


def _cfg(**overrides) -> Config:
    """Rendering config owned by the tests, not read from config/watchlist.yaml.

    top_n/bottom_n are owner-tunable knobs that get retuned as the watchlist
    grows; assertions about table sizes must pin their own values so a retune
    is a config change, not a red suite.
    """
    base = dict(
        universe=(), benchmark="SPY", top_n=10, bottom_n=4, ranking="breadth",
        fundamentals_weight=0.6, technicals_weight=0.4, changes=ChangesCfg(),
    )
    return Config(**{**base, **overrides})


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
    ctx = build_context(scored, _cfg(), run_type="dry", notes=["fixture note"], today=FIXED_TODAY)
    return render_report(ctx)


def test_report_contains_expected_content(html):
    for ticker in ("ALFA", "BRVO", "CHRL"):
        assert ticker in html
    assert "Strong performers" in html
    assert "Weak performers" in html
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
    ctx = build_context(scored, _cfg(), run_type="dry", notes=[], today=FIXED_TODAY)
    ctx["deep"] = True
    deep_html = render_report(ctx)
    assert "Deep dive: full metric grid" in deep_html
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
    assert "-400k" in html                    # BRVO insider net selling
    assert "−400k" not in html                # ASCII hyphen, not U+2212
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
    cfg = _cfg(top_n=1, bottom_n=2)  # force BRVO/CHRL into weakest
    ctx = build_context(scored, cfg, run_type="dry", notes=[], today=FIXED_TODAY)
    assert [sc.ticker for sc in ctx["strongest"]] == ["ALFA"]
    assert len(ctx["weakest"]) == 2
    html = render_report(ctx)
    assert "Why weakest" in html
    assert "⚠ Dilution" in html and "⚠ High SBC" in html  # flags survive in weakest cells


def test_movers_grouped_by_ticker(scored):
    from sentinel.report.builder import build_movers

    movers = build_movers(scored)
    tickers = [m.split(":")[0].lstrip("📈📉 ") for m in movers]
    # each ticker's alerts must be contiguous (no interleaving)
    seen, last = set(), None
    for t in tickers:
        if t != last:
            assert t not in seen, f"{t} alerts split across the list: {tickers}"
            seen.add(t)
        last = t


def test_deep_grid_covers_every_scored_ticker(scored):
    # top_n=1/bottom_n=1 forces a middle ticker to be excluded from strongest+weakest
    cfg = _cfg(top_n=1, bottom_n=1)
    ctx = build_context(scored, cfg, run_type="dry", notes=[], today=FIXED_TODAY)
    ctx["deep"] = True
    middle = set(sc.ticker for sc in ctx["all_scored"]) - {
        sc.ticker for sc in ctx["strongest"]
    } - {sc.ticker for sc in ctx["weakest"]}
    assert middle, "test setup should leave a middle ticker out of strongest/weakest"
    deep_html = render_report(ctx)
    for ticker in middle:
        assert ticker in deep_html.split("Deep dive")[1]


def test_news_section_renders_single_unlabeled(scored):
    ctx = build_context(
        scored, _cfg(), run_type="dry", notes=[], today=FIXED_TODAY,
        news_sections=[{"label": None, "html": "<b>ALFA</b> fixture headline"}],
    )
    html = render_report(ctx)
    assert "What mattered today" in html
    assert "fixture headline" in html
    assert "text-transform:uppercase" not in html  # no tone chip for a single section


def test_news_sections_multi_tone_labeled_and_separated(scored):
    ctx = build_context(
        scored, _cfg(), run_type="dry", notes=[], today=FIXED_TODAY,
        news_sections=[
            {"label": "skeptic", "html": "<i>skeptic voice</i>"},
            {"label": "barrons", "html": "<i>barrons voice</i>"},
        ],
    )
    html = render_report(ctx)
    assert "skeptic voice" in html and "barrons voice" in html
    assert html.count("text-transform:uppercase") == 2      # one chip per tone
    assert html.index("skeptic") < html.index("barrons")     # config order preserved


def test_news_section_absent_without_sections(html):
    assert "What mattered today" not in html


def test_small_watchlist_still_splits_strong_and_weak(scored):
    """The weak table is never starved: bottom_n names are carved out of the
    ranked list before strongest takes its share, so the two tables never
    overlap and the weak table is empty only with a single scored name."""
    cfg = _cfg(top_n=10, bottom_n=4)  # top_n exceeds the 3 fixtures; bottom_n=4
    ctx = build_context(scored, cfg, run_type="dry", notes=[], today=FIXED_TODAY)
    assert len(ctx["strongest"]) == 1
    assert len(ctx["weakest"]) == 2
    assert len(ctx["strongest"]) + len(ctx["weakest"]) == 3  # every fixture placed
    strong = {sc.ticker for sc in ctx["strongest"]}
    weak = {sc.ticker for sc in ctx["weakest"]}
    assert not strong & weak


# --- Bench (unranked) shadow-scored reserve ------------------------------------


@pytest.fixture()
def bench_cards():
    from sentinel.data.fixtures import load_fixture_bench_inputs

    return apply_scores(
        [compute_scorecard(inp, today=FIXED_TODAY) for inp in load_fixture_bench_inputs()]
    )


def _bench_ctx_html(scored, bench_cards):
    ctx = build_context(
        scored, _cfg(), run_type="dry", notes=[], today=FIXED_TODAY, bench=bench_cards
    )
    return ctx, render_report(ctx)


def test_bench_table_renders_with_its_disclaimer(scored, bench_cards):
    _, html = _bench_ctx_html(scored, bench_cards)
    assert "Bench (unranked)" in html
    assert "comparison references, not recommendations" in html
    bench_block = html.split("Bench (unranked)")[1]
    assert "DLTA" in bench_block
    assert "Delta Networks Inc." in bench_block
    assert "36.7" in bench_block       # DLTA r40_fcf in points: a strong-table column


def test_bench_absent_when_no_bench_rows(html):
    assert "Bench (unranked)" not in html


def test_bench_never_enters_a_ranked_or_alerting_section(scored, bench_cards):
    ctx, html = _bench_ctx_html(scored, bench_cards)
    for key in ("strongest", "weakest", "all_scored", "unscored", "signal_rows"):
        assert all(sc.ticker != "DLTA" for sc in ctx[key]), key
    assert all(row["ticker"] != "DLTA" for row in ctx["tech_only"])
    assert all("DLTA" not in m for m in ctx["movers"])
    # the bench table is the only place the name appears in the email
    assert html.count("DLTA") == 1
    assert "DLTA" not in html.split("Bench (unranked)")[0]


def test_bench_stays_out_of_the_watchlist_breadth_numbers(scored, bench_cards):
    plain = build_context(scored, _cfg(), run_type="dry", notes=[], today=FIXED_TODAY)
    with_bench, _ = _bench_ctx_html(scored, bench_cards)
    assert with_bench["median_r40"] == plain["median_r40"]
    assert with_bench["n_total"] == plain["n_total"]


def test_bench_rows_are_alphabetical_not_ranked(scored):
    from sentinel.indicators.fundamentals import Scorecard

    rows = [Scorecard(ticker=t, composite=c) for t, c in (("ZZB", 90.0), ("AAB", 10.0))]
    ctx = build_context(
        scored, _cfg(), run_type="dry", notes=[], today=FIXED_TODAY, bench=rows
    )
    assert [sc.ticker for sc in ctx["bench_rows"]] == ["AAB", "ZZB"]


def test_no_em_or_en_dashes_in_report(html):
    assert "—" not in html and "–" not in html
    assert "&mdash;" not in html and "&#8212;" not in html
    assert "&ndash;" not in html and "&#8211;" not in html


# --- What changed today + Deterioration watch (phase 4) -------------------------


def _change_ctx(scored, **kw):
    return build_context(scored, _cfg(), run_type="dry", notes=[], today=FIXED_TODAY, **kw)


def _changeset(changes=(), prior_date="2026-07-04"):
    from sentinel.report.changes import ChangeSet

    return ChangeSet(prior_date=prior_date, changes=list(changes))


def test_what_changed_table_renders(scored):
    from sentinel.report.changes import Change

    ctx = _change_ctx(
        scored,
        change_set=_changeset([
            Change("CHRL", "score", "composite 31.0 (-6.2)", "down"),
            Change("ALFA", "rank", "rank 3 -> 1", "up"),
            Change("BRVO", "short_interest", "short interest +33% (new reading)", "down"),
        ]),
    )
    html = render_report(ctx)
    assert "What changed today" in html
    assert "composite 31.0 (-6.2)" in html
    assert "rank 3 -&gt; 1" in html or "rank 3 -> 1" in html
    assert "▲" in html and "▼" in html
    assert "2026-07-04" in html  # baseline date shown


def test_quiet_day_is_one_line(scored):
    ctx = _change_ctx(scored, change_set=_changeset())
    html = render_report(ctx)
    assert "Quiet day: no material changes vs the prior run (2026-07-04)." in html


def test_no_prior_state_line(scored):
    ctx = _change_ctx(scored, change_set=_changeset(prior_date=None))
    html = render_report(ctx)
    assert "What changed today" in html
    assert "No prior run state yet" in html


def test_what_changed_absent_when_detection_skipped(html):
    assert "What changed today" not in html
    assert "Deterioration watch" not in html


def test_deterioration_watch_renders(scored):
    from sentinel.report.changes import DeteriorationRow

    ctx = _change_ctx(
        scored,
        change_set=_changeset(),
        deterioration=[
            DeteriorationRow(
                ticker="CHRL", composite=31.0, delta_1run=-6.2, delta_week=-9.8,
                reasons=["composite fell 6.2 since prior run", "estimates cut (4 down vs 0 up, 30d)"],
            )
        ],
        week_span=5,
    )
    html = render_report(ctx)
    assert "Deterioration watch" in html
    assert "estimates cut (4 down vs 0 up, 30d)" in html
    assert "-6.2" in html and "-9.8" in html
    assert "week window = 5 runs" in html


def test_deterioration_absent_when_empty(scored):
    ctx = _change_ctx(scored, change_set=_changeset(), deterioration=[])
    html = render_report(ctx)
    assert "Deterioration watch" not in html


def test_dry_run_renders_change_sections_from_fixture_state(tmp_path):
    from sentinel.config import repo_root
    from sentinel.run import main

    # dry runs must not touch the live state file, whether or not it exists yet
    # (the bot commits one to main after the first scheduled run post-merge)
    state = repo_root() / "data" / "cache" / "run_history.json"
    before = state.read_bytes() if state.exists() else None

    assert main(["--dry-run", "--no-email", "--out-dir", str(tmp_path)]) == 0
    html = (tmp_path / "report.html").read_text()

    assert "What changed today" in html
    assert "(vs 2026-08-04)" in html                       # fixture prior run date
    assert "rank 3 -&gt; 1" in html                        # ALFA rank improvement
    assert "flag cleared: high sbc" in html                # ALFA flag transition
    assert "trend mixed -&gt; downtrend" in html           # CHRL trend break
    assert "net 30d revisions +1 -&gt; -4" in html         # CHRL estimate swing
    assert "short interest +8% (new reading)" in html      # CHRL new short reading
    assert "dropped from scored universe" in html          # ZZZZ departed

    assert "Deterioration watch" in html
    assert "week window = 5 runs" in html
    det = html.split("Deterioration watch")[1].split("Strong performers")[0]
    assert "CHRL" in det and "BRVO" not in det             # multi-signal gate holds
    assert "broke into downtrend" in det
    assert "estimates cut (4 down vs 0 up, 30d)" in det
    assert "composite fell 8.0 since prior run" in det
    assert "composite fell 13.0 over the week window" in det
    assert "short float up to 8.0%" in det

    after = state.read_bytes() if state.exists() else None
    assert after == before


def test_dry_run_shadow_scores_the_bench_without_ranking_it(tmp_path, capsys):
    from sentinel.run import main

    assert main(["--dry-run", "--no-email", "--out-dir", str(tmp_path)]) == 0
    html = (tmp_path / "report.html").read_text()
    assert "Bench (unranked)" in html and "DLTA" in html

    # quarantine end to end: not in the CSV/JSON artifacts, not in the stdout
    # ranking table, not in any section above the bench table
    with (tmp_path / "scores.csv").open() as fh:
        assert {r["ticker"] for r in csv.DictReader(fh)} == {"ALFA", "BRVO", "CHRL"}
    assert "DLTA" not in (tmp_path / "raw.json").read_text()
    assert "DLTA" not in capsys.readouterr().out
    assert "DLTA" not in html.split("Bench (unranked)")[0]


def test_dry_run_subset_skips_the_bench_too(tmp_path):
    from sentinel.run import main

    assert main(["--dry-run", "--tickers", "ALFA", "--no-email", "--out-dir", str(tmp_path)]) == 0
    assert "Bench (unranked)" not in (tmp_path / "report.html").read_text()


def test_dry_run_subset_skips_change_detection(tmp_path):
    from sentinel.run import main

    assert main(["--dry-run", "--tickers", "ALFA", "--no-email", "--out-dir", str(tmp_path)]) == 0
    html = (tmp_path / "report.html").read_text()
    assert "What changed today" not in html
    assert "Deterioration watch" not in html
    assert "change detection skipped (ticker subset)" in html
