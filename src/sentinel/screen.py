"""Bench screen: python -m sentinel.screen [--tickers A,B,...]

Dry run by construction: it scores candidate tickers in memory with the run's
own scorecard and scoring code and writes nothing (no cache file, no run
history). Run it when the weekly digest's swap check says the bench needs
refreshing (SPEC 7.0.1). Per candidate it answers the two questions the swap
rule asks: is the name in the growth tier, and would it clear the tail on
entry against the latest scheduled run.

History: each candidate's yfinance statements are deepened from SEC EDGAR
behind the backfill verification gate (sentinel.backfill), in memory, so
`r40_trend` is live wherever the gate accepts. A rejected or skipped name is
scored on yfinance alone and usually reads as warm-up.

Promoting a name is still the owner-gated path: a watchlist.yaml bench edit in
a PR, then the backfill `--dry-run` with it and `--apply` after it merges.
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from sentinel.backfill import ACCEPT, backfill_ticker
from sentinel.config import Config, load_config
from sentinel.data import cache
from sentinel.data.fundamentals import inputs_from_frame
from sentinel.data.history import load_history
from sentinel.digest import (
    DATA_QUALITY_FLAGS,
    build_digest,
    pct_beside_floor,
    swap_universe,
)
from sentinel.indicators.fundamentals import R40_BAR, compute_scorecard, display_flags
from sentinel.report.changes import RunSnapshot
from sentinel.rotation import Entrant, enters_above_tail, growth_tier, pick_entrant
from sentinel.scoring import apply_scores

log = logging.getLogger("sentinel.screen")

FetchFacts = Callable[[str], dict[str, Any]]
FetchYf = Callable[[str], tuple[pd.DataFrame, dict[str, Any]]]


@dataclass(frozen=True)
class ScreenRow:
    ticker: str
    score: float | None = None
    growth: float | None = None
    tier: str | None = None
    r40_fcf: float | None = None
    r40_trend: float | None = None
    live: bool = False                 # r40_trend present, no data-quality flag
    clears: bool | None = None
    quarters: int = 0
    history: str = ""
    flags: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class Baseline:
    """What a candidate is measured against: the latest scheduled run."""

    run_date: str
    universe_scores: dict[str, float]
    outgoing: str | None
    outgoing_qualifies: bool
    bench: list[Entrant]               # eligible bench names only
    # set when the latest run predates the current config (a swap merged
    # since): names that left or joined, said in the report header
    moved_out: tuple[str, ...] = ()
    moved_in: tuple[str, ...] = ()
    # bench names that cannot be swapped in yet, with the reason
    bench_blocked: tuple[tuple[str, str], ...] = ()
    # of those, the ones that would clear the tail once eligible
    bench_waiting: tuple[str, ...] = ()


def _sort_key(row: ScreenRow) -> tuple:
    return (
        row.error is not None,
        row.tier != "growth",
        not row.live,           # a warm-up name is not a level to act on
        not row.clears,
        -(row.score if row.score is not None else -1.0),
        row.ticker,
    )


def baseline_from_history(cfg: Config, history_path: Path | None = None) -> Baseline | None:
    """The digest's swap check over the recorded runs, under the CURRENT
    config: the outgoing name, the universe scores and the bench rows all
    come from `build_digest` and `swap_universe`, so the screen and the
    weekly digest cannot disagree. None when no run scored any universe name.
    """
    raw_runs, _ = load_history(history_path)
    runs = [RunSnapshot.from_dict(r) for r in raw_runs]
    if not runs:
        return None
    latest = runs[-1]
    universe_scores, moved_out, moved_in = swap_universe(latest, cfg.r40_tickers)
    digest = build_digest(runs, cfg.r40_tickers, list(cfg.bench), cfg.changes)
    if not universe_scores or digest.swap_outgoing is None:
        return None
    # a configured bench name with no row (a name just moved to the bench, or
    # one no run has scored there yet) is named, never dropped
    rows = {b.ticker: b for b in digest.bench_weeks}
    bench: list[Entrant] = []
    blocked: list[tuple[str, str]] = []
    for t in cfg.bench:
        week = rows.get(t)
        if week is None:
            blocked.append((t, "not shadow-scored on the bench this window"))
        elif week.entry_block is not None:
            blocked.append((t, week.entry_block))
        else:
            bench.append(Entrant(t, week.score_last, week.growth_tier, week.clears_tail))
    return Baseline(
        run_date=latest.date,
        universe_scores=universe_scores,
        outgoing=digest.swap_outgoing,
        outgoing_qualifies=digest.swap_outgoing_qualifies,
        bench=bench,
        moved_out=tuple(moved_out),
        moved_in=tuple(moved_in),
        bench_blocked=tuple(blocked),
        bench_waiting=tuple(digest.bench_waiting),
    )


def screen_ticker(
    ticker: str,
    fetch_facts: FetchFacts,
    fetch_yfinance: FetchYf,
    baseline: Baseline | None,
    cfg: Config,
) -> ScreenRow:
    """Score one candidate in memory. Never raises: a failure is an error row."""
    cached_df, meta = cache.load(ticker)
    if cached_df is None:
        try:
            base_df, meta = fetch_yfinance(ticker)
        except Exception as exc:  # noqa: BLE001 - report and continue the sweep
            return ScreenRow(ticker, error=f"yfinance fetch failed ({exc})")
    else:
        base_df = cached_df
    if base_df is None or base_df.shape[1] == 0:
        return ScreenRow(ticker, error="no yfinance statements")

    # hand backfill the frame already in hand, so an uncached name is fetched once
    try:
        result = backfill_ticker(ticker, fetch_facts, lambda _t: (base_df, meta), apply=False)
    except Exception as exc:  # noqa: BLE001 - fall back to yfinance, keep sweeping
        result = None
        reason = f"backfill failed ({exc})"
    if result is not None and result.status == ACCEPT and result.merged is not None:
        frame = result.merged
        history = f"EDGAR verified, {frame.shape[1]} quarters"
    else:
        frame = base_df
        if result is not None:
            reason = f"{result.status.lower()}: {result.reason or 'n/a'}"
        history = f"yfinance only, {frame.shape[1]} quarters ({reason})"


    notes: list[str] = []
    try:
        inputs = inputs_from_frame(ticker, frame, meta, notes)
        sc = apply_scores(
            [compute_scorecard(inputs)],
            fundamentals_weight=cfg.fundamentals_weight,
            technicals_weight=cfg.technicals_weight,
        )[0]
    except Exception as exc:  # noqa: BLE001 - one bad name never ends the screen
        return ScreenRow(ticker, history=history, error=f"scoring failed ({exc})")

    clears = None
    if sc.score is not None and baseline is not None:
        clears = enters_above_tail(
            sc.score, baseline.universe_scores, baseline.outgoing,
            cfg.changes.weak_bottom_n,
        )
    return ScreenRow(
        ticker=ticker,
        score=sc.score,
        growth=sc.growth,
        tier=growth_tier(sc.growth, cfg.changes.swap_min_growth),
        r40_fcf=sc.r40_fcf,
        r40_trend=sc.r40_trend,
        live=sc.r40_trend is not None
        and not any(f in DATA_QUALITY_FLAGS for f in sc.flags),
        clears=clears,
        quarters=int(frame.shape[1]),
        history=history,
        flags=display_flags(sc.flags),
    )


def run_screen(
    tickers: list[str],
    cfg: Config,
    baseline: Baseline | None,
    fetch_facts: FetchFacts | None = None,
    fetch_yfinance: FetchYf | None = None,
) -> list[ScreenRow]:
    if fetch_facts is None or fetch_yfinance is None:
        from sentinel.backfill import _default_fetchers
        from sentinel.data.fundamentals import fetch_statements

        try:
            live_facts, live_yf = _default_fetchers()
        except Exception as exc:  # noqa: BLE001 - degrade to yfinance-only scoring
            log.warning("screen: EDGAR unavailable (%s); scoring on yfinance only", exc)
            message = f"EDGAR unavailable ({exc})"

            def live_facts(_ticker: str) -> dict[str, Any]:
                raise LookupError(message)

            live_yf = fetch_statements
        fetch_facts = fetch_facts or live_facts
        fetch_yfinance = fetch_yfinance or live_yf
    rows = []
    for ticker in tickers:
        log.info("screen: %s", ticker)
        rows.append(screen_ticker(ticker, fetch_facts, fetch_yfinance, baseline, cfg))
    return rows


def _num(value: float | None, spec: str = ".1f") -> str:
    return "n/a" if value is None else format(value, spec)


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}"


def _growth(value: float | None, floor: float) -> str:
    return "n/a" if value is None else pct_beside_floor(value, floor)


def _r40(value: float | None) -> str:
    if value is None:
        return "n/a"
    verdict = "pass" if value >= R40_BAR else "fail"
    return f"{pct_beside_floor(value, R40_BAR)} {verdict}"


def format_report(
    rows: list[ScreenRow],
    baseline: Baseline | None,
    cfg: Config,
    skipped: list[str] | None = None,
) -> str:
    """Plain ASCII markdown; no em or en dashes (repo rule)."""
    floor = f"{cfg.changes.swap_min_growth * 100:.0f}"
    lines = [
        "# Bench screen (dry run, nothing written)",
        "",
        f"Growth tier: yearly revenue growth of {floor}% or more. Clears tail: "
        f"would rank above the bottom {cfg.changes.weak_bottom_n} by fundamental "
        "score once the outgoing name leaves (SPEC 7.0.1 condition 2).",
        "",
    ]
    if baseline is None:
        lines += [
            "No scored run in the history: the on-entry test needs one, so it reads n/a.",
            "",
        ]
    else:
        why = "qualifies" if baseline.outgoing_qualifies else "lowest score, a what-if"
        lines += [
            f"Measured against the {baseline.run_date} run "
            f"({len(baseline.universe_scores)} names scored). Outgoing name: "
            f"{baseline.outgoing or 'n/a'} ({why}).",
            "",
        ]
        if baseline.moved_out or baseline.moved_in:
            lines += [
                "That run predates the current watchlist (left: "
                f"{', '.join(baseline.moved_out) or 'none'}; joined: "
                f"{', '.join(baseline.moved_in) or 'none'}), so its scores are "
                "used under the current roles: a joined name's bench snapshot "
                "counts as its universe score.",
                "",
            ]
        if baseline.bench_blocked:
            lines += [
                "Bench names that cannot be swapped in yet: "
                + "; ".join(f"{t} ({why})" for t, why in baseline.bench_blocked)
                + ".",
                "",
            ]
    if skipped:
        lines += [
            f"Not screened, already scored by the pipeline: {', '.join(skipped)}.",
            "",
        ]
    lines += [
        "| ticker | fundamental | growth | tier | clears tail | r40 fcf "
        "| r40 trend | trend live | history | flags |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in sorted(rows, key=_sort_key):
        if r.error:
            lines.append(
                f"| {r.ticker} | n/a | n/a | n/a | n/a | n/a | n/a | n/a | "
                f"{r.history or 'n/a'} | error: {r.error} |"
            )
            continue
        clears = "n/a" if r.clears is None else ("yes" if r.clears else "no")
        lines.append(
            f"| {r.ticker} | {_num(r.score)} "
            f"| {_growth(r.growth, cfg.changes.swap_min_growth)} | {r.tier or 'n/a'} "
            f"| {clears} | {_r40(r.r40_fcf)} | {_pct(r.r40_trend)} "
            f"| {'yes' if r.live else 'no'} | {r.history} "
            f"| {', '.join(r.flags) if r.flags else 'none'} |"
        )

    # the rule's pick over the current bench plus every live candidate: a
    # warm-up candidate is not a level anyone should act on
    pool = list(baseline.bench) if baseline is not None else []
    pool += [
        Entrant(r.ticker, r.score, r.tier, r.clears)
        for r in rows if r.error is None and r.live
    ]
    pick = pick_entrant(pool)
    bench_names = {e.ticker for e in (baseline.bench if baseline else [])}
    # names the table shows clearing that the pick cannot take yet, so the
    # pick line never contradicts the table above it
    waiting = [
        f"{r.ticker} (warm-up)" for r in sorted(rows, key=_sort_key)
        if r.error is None and not r.live and r.clears
    ]
    if baseline is not None:
        waiting += [f"{t} (on the bench, not eligible yet)" for t in baseline.bench_waiting]
    lines.append("")
    if baseline is None:
        lines.append("Rule pick: n/a (no scored run to test against).")
    elif pick is None:
        lines.append(
            "Rule pick over the current bench and the live candidates: none "
            "clears the tail on entry. Widen the pool (screen: candidates in "
            "config/watchlist.yaml, or --tickers)."
            if not waiting else
            "Rule pick over the current bench and the live candidates: none "
            "of the eligible names clears the tail on entry."
        )
    else:
        where = "already on the bench" if pick.ticker in bench_names else "a candidate"
        if pick.tier == "growth":
            tier = "growth tier"
        elif pick.tier is None:
            tier = "growth n/a, so ranked with the slower growers: check its growth first"
        else:
            tier = "slower grower, fallback"
        lines.append(
            f"Rule pick over the current bench and the live candidates: "
            f"{pick.ticker} ({where}, {tier}, fundamental {_num(pick.score)})."
        )
    if baseline is not None and waiting:
        lines.append(
            f"Would clear the tail but not eligible yet: {', '.join(waiting)}."
        )
    lines += [
        "",
        "Nothing was written. To bench a name: edit `bench:` in "
        "config/watchlist.yaml in a PR with `python -m sentinel.backfill "
        "--dry-run --tickers NEW`, then the owner-gated `--apply` after it merges.",
        "",
    ]
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="score bench candidates in memory (dry run, writes nothing)"
    )
    parser.add_argument(
        "--tickers", default=None,
        help="comma-separated tickers (default: screen.candidates in watchlist.yaml)",
    )
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--history", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = parse_args(argv)
    cfg = load_config(args.config)
    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        tickers = list(cfg.screen_candidates)
    # a universe or bench name is already scored every run; screening it again
    # would let it compete against its own score (or twice in the pick pool)
    scored = set(cfg.all_tickers) | set(cfg.bench)
    tickers = list(dict.fromkeys(tickers))
    skipped = [t for t in tickers if t in scored]
    tickers = [t for t in tickers if t not in scored]
    if not tickers:
        if skipped:
            sys.stdout.write(
                f"Nothing to screen: {', '.join(skipped)} already scored by the pipeline.\n"
            )
            return 0
        sys.stdout.write("No candidates: set screen.candidates in watchlist.yaml or pass --tickers.\n")
        return 0
    baseline = baseline_from_history(cfg, args.history)
    rows = run_screen(tickers, cfg, baseline)
    sys.stdout.write(format_report(rows, baseline, cfg, skipped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
