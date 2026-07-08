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
from sentinel.indicators.fundamentals import FundamentalInputs, compute_scorecard
from sentinel.indicators.technicals import TechnicalSnapshot, compute_technicals
from sentinel.report.builder import build_context, render_report, write_outputs
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

    # cache hygiene: drop files for tickers no longer on the watchlist —
    # skipped for --tickers overrides so an ad hoc subset never deletes siblings
    if not args.dry_run and not args.tickers:
        from sentinel.data import cache

        removed = cache.prune(set(cfg.all_tickers))
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
                notes.append("dry run: LLM news style skipped — rendering headlines instead")
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
                    notes.append(f"news tone '{tone}' failed — section skipped")
            if not sections:  # every tone failed: one deterministic fallback section
                fragment, fb_notes = render_news(digest, "headlines")
                notes.extend(fb_notes)
                notes.append("all news tones failed — fell back to headlines")
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

    outdir = args.out_dir or repo_root() / "reports" / date.today().isoformat()
    paths = write_outputs(outdir, html_file, scorecards)
    log.info("report written: %s", paths["html"])

    # stdout spot-check table (points)
    print(
        f"\n{'ticker':<8}{'comp':>8}{'F':>7}{'T':>7}{'r40_fcf':>10}"
        f"{'r40_ebitda':>12}{'r40_sbc_adj':>13}  trend/flags"
    )
    for sc in sorted(scorecards, key=lambda s: (s.composite is None, -(s.composite or 0))):
        fmt = lambda v, m=100: "—" if v is None else f"{v * m:.1f}"
        trend = sc.tech.trend_state if sc.tech else "—"
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
            subject=f"Ticker Sentinel — {date.today().isoformat()}",
            images={f"spark_{t}": png for t, png in sparks.items()},
        )
        log.info(status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
