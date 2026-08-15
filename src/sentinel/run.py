"""CLI entrypoint: python -m sentinel.run [--tickers ...] [--no-email] [--dry-run] [--deep]

A run always produces a report — failures degrade into data notes, never crashes.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date
from pathlib import Path

import pandas as pd

from sentinel.config import Config, load_config, repo_root
from sentinel.data import history
from sentinel.indicators.fundamentals import FundamentalInputs, compute_scorecard
from sentinel.indicators.technicals import TechnicalSnapshot, compute_technicals
from sentinel.report.builder import build_context, rank_key, render_report, write_outputs
from sentinel.report.changes import (
    RunSnapshot,
    baseline_ok,
    baseline_reference,
    deterioration_rows,
    diff_runs,
    select_baselines,
    snapshot_from_scorecards,
)
from sentinel.report.charts import data_uri, sparkline_png
from sentinel.scoring import apply_scores, technical_score

log = logging.getLogger("sentinel")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m sentinel.run", description=__doc__)
    p.add_argument("--tickers", help="comma-separated override of the watchlist")
    p.add_argument("--no-email", action="store_true", help="build the report but do not send it")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="fixture data, no network, no email (for CI and offline checks)",
    )
    p.add_argument(
        "--deep", action="store_true", help="deep-dive mode: 2y price history + full metric grid"
    )
    p.add_argument("--out-dir", type=Path, help="override output directory (default reports/YYYY-MM-DD)")
    return p.parse_args(argv)


def _run_type(args: argparse.Namespace) -> str:
    if args.dry_run:
        return "dry"
    return "scheduled" if os.environ.get("GITHUB_EVENT_NAME") == "schedule" else "ad hoc"


def _benchmark_line(close: pd.DataFrame | None, benchmark: str) -> str | None:
    if close is None or benchmark not in close.columns:
        return None
    series = close[benchmark].dropna()
    if len(series) < 22:
        return None
    last, prev, month_ago = series.iloc[-1], series.iloc[-2], series.iloc[-22]
    return (
        f"{benchmark}: {last:.2f} · 1d {100 * (last / prev - 1):+.2f}% · "
        f"1m {100 * (last / month_ago - 1):+.2f}%"
    )


# A benchmark bar older than this many calendar days means no session closed
# recently. 3 clears every normal weekend (a Saturday run sees Friday's bar at
# 1 day; a Tuesday run sees Monday's at 1; a Monday run sees Friday's at 3) and
# fires on the Tuesday after a Monday holiday, where Friday's bar is 4 days old.
STALE_SESSION_DAYS = 3


def _stale_session_note(bench_close: pd.Series | None, today: date) -> str | None:
    """Disclose that a run is re-reporting an old bar (market holiday, or a data
    feed that stopped updating). Silent on missing prices: no data is a
    different failure and already has its own notes."""
    if bench_close is None:
        return None
    series = bench_close.dropna()
    if series.empty:
        return None
    try:
        last = pd.Timestamp(series.index[-1]).date()
    except (TypeError, ValueError):
        return None
    if (today - last).days <= STALE_SESSION_DAYS:
        return None
    return f"no new market session since {last.isoformat()} (market holiday?)"


def _column(frame: pd.DataFrame | None, ticker: str) -> pd.Series | None:
    if frame is None or ticker not in frame.columns:
        return None
    return frame[ticker]


def _reprice_market_cap(
    inputs_list: list[FundamentalInputs], close: pd.DataFrame | None
) -> list[str]:
    """Daily market-cap repricing: latest close × TTM-average diluted shares.

    Keeps the valuation label moving with the market instead of the weekly cache.
    Approximation (TTM-average shares, not today's count); cached value is the
    fallback. Returns the tickers whose valuation is still riding a cached (up
    to week-old) market cap so the report can disclose it.
    """
    stale: list[str] = []
    for inp in inputs_list:
        series = _column(close, inp.ticker)
        prices = series.dropna() if series is not None else None
        if prices is None or prices.empty or inp.diluted_shares_now is None:
            if inp.market_cap is not None:
                stale.append(inp.ticker)
            continue
        inp.market_cap = float(prices.iloc[-1]) * inp.diluted_shares_now
    return stale


def _trend_warmup_note(scorecards: list) -> str | None:
    """One aggregate note while r40_trend is still warming up, else None.

    r40_trend compares r40_fcf now against r40_fcf four quarters back, and each
    of those needs a year of revenue behind it, so the metric wants 12 cached
    quarters. The committed cache deepens by 4 quarters a year, so most names
    currently score with the trend term inert. One line saying so beats a
    column of unexplained n/a cells. Counts SCORED names only: a name that
    could not be scored at all is already covered by its own note. Returns None
    once every scored name has a trend, so the disclosure self-erases as the
    history deepens (the backfill tool exists to make that happen sooner).
    """
    scored = [sc for sc in scorecards if sc.score is not None]
    missing = sum(1 for sc in scored if sc.r40_trend is None)
    if not missing:
        return None
    return (
        f"R40 trend warming up: n/a for {missing} of {len(scored)} scored names "
        "(needs 12 cached quarters; the committed cache deepens by 4 per year)"
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = parse_args(argv)
    cfg = load_config()
    notes: list[str] = []

    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        tech_only_tickers: list[str] = []
    else:
        tickers = cfg.r40_tickers
        tech_only_tickers = [t.ticker for t in cfg.universe if not t.is_r40]

    # --- data: fundamentals + prices -------------------------------------------------
    if args.dry_run:
        from sentinel.data.fixtures import fixture_signals, load_fixture_inputs, synthetic_prices

        inputs_list = load_fixture_inputs()
        close, volume = synthetic_prices()
        signals = fixture_signals()
        notes.append("dry run: committed fixture data, no network access")
    else:
        from sentinel.data.fundamentals import get_fundamentals
        from sentinel.data.prices import fetch_prices
        from sentinel.data.signals import fetch_signals

        inputs_list = []
        signals = {}
        for ticker in tickers:
            inputs, t_notes = get_fundamentals(ticker)
            notes.extend(t_notes)
            if inputs is not None:
                inputs_list.append(inputs)
            snap, s_notes = fetch_signals(ticker)
            notes.extend(s_notes)
            if snap is not None:
                signals[ticker] = snap
        price_universe = sorted({*tickers, *tech_only_tickers, cfg.benchmark})
        close, volume, price_notes = fetch_prices(
            price_universe, period="2y" if args.deep else "1y"
        )
        notes.extend(price_notes)

    bench_close = _column(close, cfg.benchmark)
    if not args.dry_run:
        # dry-run fixtures are frozen at a fixed date and would always look
        # stale; the run already discloses that it is running on fixtures
        stale_session = _stale_session_note(bench_close, date.today())
        if stale_session:
            notes.append(stale_session)

    # --- technicals + quick win: reprice market cap from today's close ---------------
    technicals: dict[str, TechnicalSnapshot | None] = {
        inp.ticker: compute_technicals(
            _column(close, inp.ticker), _column(volume, inp.ticker), bench_close
        )
        for inp in inputs_list
    }
    stale_mcap = _reprice_market_cap(inputs_list, close)
    if stale_mcap:
        notes.append(
            f"valuation from cached market cap (no fresh price): {', '.join(stale_mcap)}"
        )

    # --- score ------------------------------------------------------------------------
    scorecards = apply_scores(
        [compute_scorecard(inp) for inp in inputs_list],
        technicals,
        cfg.fundamentals_weight,
        cfg.technicals_weight,
    )
    for sc in scorecards:  # between-quarter signals: informational, never scored
        sc.signals = signals.get(sc.ticker)

    warmup = _trend_warmup_note(scorecards)
    if warmup:
        notes.append(warmup)

    # --- day-over-day change detection vs committed run history ----------------------
    today = date.today()
    change_set = None
    det_rows = []
    week_span = 0
    current_run = None
    if args.tickers:
        # partial-universe ranks and deltas are not comparable; skip and say so
        notes.append("change detection skipped (ticker subset)")
    else:
        if args.dry_run:
            from sentinel.data.fixtures import fixture_history_path

            runs_raw, h_notes = history.load_history(fixture_history_path())
        else:
            runs_raw, h_notes = history.load_history()
        notes.extend(h_notes)
        try:
            prior_runs = [RunSnapshot.from_dict(r) for r in runs_raw]
            ranked = sorted(
                (sc for sc in scorecards if sc.score is not None),
                key=lambda s: rank_key(s, cfg.ranking),
            )
            # degraded-run gate: reference = prior baseline's tickers restricted
            # to what this run intended to score (expansion adds unscored new
            # names without tripping it; a deliberate shrink drops removed names
            # from the reference instead of deadlocking; an outage still fails)
            intended = (
                {inp.ticker for inp in inputs_list} if args.dry_run else set(tickers)
            )
            eligible = [r for r in prior_runs if r.date < today.isoformat()]
            reference = baseline_reference(
                set(eligible[-1].tickers) if eligible else None, intended
            )
            if not baseline_ok(
                {sc.ticker for sc in ranked}, reference,
                cfg.changes.baseline_min_fraction,
            ):
                notes.append(
                    f"run degraded ({len({sc.ticker for sc in ranked} & reference)} "
                    f"of {len(reference)} baseline names scored); change detection "
                    "skipped and baseline not advanced"
                )
            else:
                unscored_reasons = {
                    sc.ticker: "; ".join(f.replace("_", " ") for f in sc.flags)
                    or "not scorable this run"
                    for sc in scorecards if sc.score is None
                }
                current_run = snapshot_from_scorecards(
                    ranked, today=today, run_type=_run_type(args)
                )
                prior, week_ago, week_span = select_baselines(
                    prior_runs, current_run.date, cfg.changes.week_window_runs
                )
                change_set = diff_runs(
                    current_run, prior, cfg.changes, unscored_reasons
                )
                det_rows = deterioration_rows(ranked, prior, week_ago, cfg.changes)
        except Exception as exc:  # degrade, never lose the report to a bad state file
            notes.append(f"change detection failed, sections skipped ({exc})")
            change_set, det_rows, week_span, current_run = None, [], 0, None

    # cache hygiene: drop files for tickers no longer on the watchlist —
    # skipped for --tickers overrides so an ad hoc subset never deletes siblings.
    # The keep-set is cfg.cache_tickers, not cfg.all_tickers: the bench is
    # unscored but its parquets are seeded and deepened by the history backfill,
    # and pruning on the scored universe alone would delete them every run.
    if not args.dry_run and not args.tickers:
        from sentinel.data import cache

        removed = cache.prune(set(cfg.cache_tickers))
        if removed:
            notes.append(f"pruned cache for departed tickers: {', '.join(removed)}")

    # --- news: pipeline builds ONE neutral digest; each configured tone renders
    # its own labeled section from it (styles may trim, never refetch) ---
    news_sections = None
    if args.dry_run or cfg.news.enabled:
        from sentinel.news.pipeline import build_digest, collect_news
        from sentinel.news.styles import LLM_STYLES, render_news

        name_map = {sc.ticker: sc.company_name for sc in scorecards}
        style = cfg.news.style
        if args.dry_run:
            from sentinel.data.fixtures import fixture_news

            generic, per_ticker = fixture_news()
            if style in LLM_STYLES:  # dry run means offline — no claude subprocess
                notes.append("dry run: LLM news style skipped; rendering headlines instead")
                style = "headlines"
        else:
            generic, per_ticker, news_notes = collect_news(
                list(cfg.news.feeds), cfg.news.per_ticker_feed, list(name_map)
            )
            notes.extend(news_notes)
        digest = build_digest(
            generic,
            per_ticker,
            name_map,
            max_age_hours=cfg.news.max_age_hours,
            max_per_ticker=cfg.news.max_per_ticker,
        )
        if style in LLM_STYLES and not digest.empty:
            sections = []
            label_tones = len(cfg.news.tones) > 1
            for tone in cfg.news.tones:
                fragment, tone_notes = render_news(
                    digest, style, model=cfg.news.model, tone=tone, fallback=False,
                    known_tickers=set(name_map),
                )
                notes.extend(tone_notes)
                if fragment:
                    sections.append({"label": tone if label_tones else None, "html": fragment})
                else:
                    notes.append(f"news tone '{tone}' failed; section skipped")
            if not sections:  # every tone failed: one deterministic fallback section
                fragment, fb_notes = render_news(digest, "headlines")
                notes.extend(fb_notes)
                notes.append("all news tones failed; fell back to headlines")
                sections = [{"label": None, "html": fragment}] if fragment else []
            news_sections = sections or None
        else:
            fragment, style_notes = render_news(
                digest, style, model=cfg.news.model, tone=cfg.news.tones[0]
            )
            notes.extend(style_notes)
            news_sections = [{"label": None, "html": fragment}] if fragment else None

    tech_only = []
    for ticker in tech_only_tickers:
        snap = compute_technicals(_column(close, ticker), _column(volume, ticker), bench_close)
        if snap is not None:
            tech_only.append({"ticker": ticker, "tech": snap, "t_score": technical_score(snap)})

    # --- report -----------------------------------------------------------------------
    context = build_context(
        scorecards,
        cfg,
        run_type=_run_type(args),
        notes=notes,
        benchmark_line=_benchmark_line(close, cfg.benchmark),
        tech_only=tech_only,
        news_sections=news_sections,
        change_set=change_set,
        deterioration=det_rows,
        week_span=week_span,
    )
    context["deep"] = args.deep

    sparks: dict[str, bytes] = {}
    for ticker in [sc.ticker for sc in scorecards] + [row["ticker"] for row in tech_only]:
        png = sparkline_png(_column(close, ticker))
        if png is not None:
            sparks[ticker] = png

    context["spark_src"] = {t: f"cid:spark_{t}" for t in sparks}
    html_email = render_report(context)
    context["spark_src"] = {t: data_uri(png) for t, png in sparks.items()}
    html_file = render_report(context)

    outdir = args.out_dir or repo_root() / "reports" / today.isoformat()
    paths = write_outputs(outdir, html_file, scorecards)
    log.info("report written: %s", paths["html"])

    # persist state for tomorrow's diff only after a report actually landed;
    # real full-universe runs only (dry runs read fixture state and never write,
    # subset runs were skipped above). The scheduled workflow commits the file.
    if current_run is not None and not args.dry_run:
        history.save_run(current_run.to_dict(), cfg.changes.retention_runs)
        log.info("run history updated: %s", history.history_path())

    # stdout spot-check table (points)
    print(
        f"\n{'ticker':<8}{'comp':>8}{'F':>7}{'T':>7}{'r40_fcf':>10}"
        f"{'r40_ebitda':>12}{'r40_sbc_adj':>13}  trend/flags"
    )
    for sc in sorted(scorecards, key=lambda s: (s.composite is None, -(s.composite or 0))):
        fmt = lambda v, m=100: "n/a" if v is None else f"{v * m:.1f}"
        trend = sc.tech.trend_state if sc.tech else "n/a"
        print(
            f"{sc.ticker:<8}{fmt(sc.composite, 1):>8}{fmt(sc.score, 1):>7}"
            f"{fmt(sc.technical_score, 1):>7}{fmt(sc.r40_fcf):>10}"
            f"{fmt(sc.r40_ebitda):>12}{fmt(sc.r40_sbc_adj):>13}  {trend};{';'.join(sc.flags)}"
        )
    print()

    if args.no_email or args.dry_run:
        log.info("email not sent (%s)", "--dry-run" if args.dry_run else "--no-email")
    else:
        from sentinel.deliver import send_report

        sent, status = send_report(
            html_email,
            subject=f"Ticker Sentinel · {date.today().isoformat()}",
            images={f"spark_{t}": png for t, png in sparks.items()},
        )
        if not sent:
            # a requested email that never left is a failed run, and a silent
            # green job is worse than a red one: exit non-zero so the workflow
            # alerts. The report files and the run history are already written
            # above, so failing here loses nothing (the workflow still uploads
            # the report artifact and commits the day's cache baseline).
            log.error("EMAIL NOT SENT: %s", status)
            return 1
        log.info(status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
