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

from sentinel.config import load_config, repo_root
from sentinel.indicators.fundamentals import compute_scorecard
from sentinel.report.builder import build_context, render_report, write_outputs
from sentinel.scoring import apply_scores

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
        "--deep", action="store_true", help="deep-dive mode (Phase 2 — accepted, currently a no-op)"
    )
    p.add_argument("--out-dir", type=Path, help="override output directory (default reports/YYYY-MM-DD)")
    return p.parse_args(argv)


def _run_type(args: argparse.Namespace) -> str:
    if args.dry_run:
        return "dry"
    return "scheduled" if os.environ.get("GITHUB_EVENT_NAME") == "schedule" else "ad hoc"


def _benchmark_line(benchmark: str, notes: list[str]) -> str | None:
    """Best-effort benchmark context; any failure becomes a note, not an error."""
    from sentinel.data.prices import fetch_close_prices

    close, price_notes = fetch_close_prices([benchmark])
    notes.extend(price_notes)
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


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = parse_args(argv)
    cfg = load_config()
    notes: list[str] = []

    if args.deep:
        notes.append("--deep requested: deep-dive mode lands in Phase 2, ignored for now")

    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        tickers = cfg.r40_tickers

    benchmark_line = None
    if args.dry_run:
        from sentinel.data.fixtures import load_fixture_inputs

        inputs_list = load_fixture_inputs()
        notes.append("dry run: committed fixture data, no network access")
    else:
        from sentinel.data.fundamentals import get_fundamentals

        inputs_list = []
        for ticker in tickers:
            inputs, t_notes = get_fundamentals(ticker)
            notes.extend(t_notes)
            if inputs is not None:
                inputs_list.append(inputs)
        benchmark_line = _benchmark_line(cfg.benchmark, notes)

    scorecards = apply_scores([compute_scorecard(inp) for inp in inputs_list])

    context = build_context(
        scorecards, cfg, run_type=_run_type(args), notes=notes, benchmark_line=benchmark_line
    )
    html = render_report(context)
    outdir = args.out_dir or repo_root() / "reports" / date.today().isoformat()
    paths = write_outputs(outdir, html, scorecards)
    log.info("report written: %s", paths["html"])

    # stdout spot-check table (points)
    print(f"\n{'ticker':<8}{'score':>8}{'r40_fcf':>10}{'r40_ebitda':>12}{'r40_sbc_adj':>13}  flags")
    for sc in sorted(scorecards, key=lambda s: (s.score is None, -(s.score or 0))):
        fmt = lambda v, m=100: "—" if v is None else f"{v * m:.1f}"
        print(
            f"{sc.ticker:<8}{fmt(sc.score, 1):>8}{fmt(sc.r40_fcf):>10}"
            f"{fmt(sc.r40_ebitda):>12}{fmt(sc.r40_sbc_adj):>13}  {';'.join(sc.flags)}"
        )
    print()

    if args.no_email or args.dry_run:
        log.info("email not sent (%s)", "--dry-run" if args.dry_run else "--no-email")
    else:
        from sentinel.deliver import send_report

        sent, status = send_report(html, subject=f"Ticker Sentinel — {date.today().isoformat()}")
        log.info(status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
