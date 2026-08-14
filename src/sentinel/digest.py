"""Weekly watchlist-refresh digest: evidence summary for the owner's candidate
refresh session (spec 7.0). Pure aggregation over committed run history; no
network, no scoring. The weekly-refresh workflow renders this into a GitHub
issue; the swap decision itself stays owner-gated.

Run: python -m sentinel.digest [--refresh-number N] [--out PATH]
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from sentinel.config import ChangesCfg, Config, load_config
from sentinel.data.history import load_history
from sentinel.report.changes import RunSnapshot, TickerSnapshot, diff_runs

# manual refreshes before the digest starts prompting the automation decision
CALIBRATION_REFRESHES = 3


def snapshot_decaying(snap: TickerSnapshot, threshold: float) -> bool:
    """Plan section 6 decay gate evaluated from a persisted snapshot: R40 trend
    below `threshold` AND technical confirmation (downtrend or death cross).
    Mirrors changes.deteriorating(), which needs a live Scorecard."""
    fell = snap.r40_trend is not None and snap.r40_trend < threshold
    return fell and (snap.trend_state == "downtrend" or snap.death_cross)


@dataclass(frozen=True)
class TickerWeek:
    """One ticker's aggregated evidence across the digest window."""

    ticker: str
    runs_seen: int
    decay_hits: int                    # runs where snapshot_decaying() held
    composite_latest: float | None
    composite_delta: float | None      # latest minus earliest appearance
    rank_earliest: int | None
    rank_latest: int | None
    down_changes: int                  # worsening threshold crossings in window
    flags_latest: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class WeeklyDigest:
    window_start: str
    window_end: str
    runs_in_window: int
    attention: list[TickerWeek]        # persistent decliners, worst first
    change_counts: dict[str, int]      # change kind -> occurrences in window
    busiest: list[tuple[str, int]]     # (ticker, change count), noisiest first
    coverage_gaps: list[str]           # human-readable clauses
    bench: list[str]
    notes: list[str]                   # degradation notes (unreadable history etc.)


def _window(runs: list[RunSnapshot], cfg: ChangesCfg) -> list[RunSnapshot]:
    return runs[-max(cfg.week_window_runs, 1):]


def build_digest(
    runs: list[RunSnapshot],
    universe: list[str],
    bench: list[str],
    cfg: ChangesCfg,
    notes: list[str] | None = None,
) -> WeeklyDigest:
    """Aggregate the last week_window_runs runs. Every threshold comes from cfg;
    absent values never fabricate evidence (same contract as diff_runs)."""
    window = _window(runs, cfg)
    if not window:
        return WeeklyDigest(
            window_start="n/a", window_end="n/a", runs_in_window=0,
            attention=[], change_counts={}, busiest=[],
            coverage_gaps=[f"no run history yet for {len(universe)} configured tickers"],
            bench=list(bench), notes=list(notes or []),
        )

    change_counts: dict[str, int] = {}
    per_ticker_changes: dict[str, int] = {}
    per_ticker_down: dict[str, int] = {}
    for prior, current in zip(window, window[1:]):
        for change in diff_runs(current, prior, cfg).changes:
            change_counts[change.kind] = change_counts.get(change.kind, 0) + 1
            per_ticker_changes[change.ticker] = per_ticker_changes.get(change.ticker, 0) + 1
            if change.direction == "down":
                per_ticker_down[change.ticker] = per_ticker_down.get(change.ticker, 0) + 1

    seen: set[str] = set()
    for run in window:
        seen.update(run.tickers)

    weeks: list[TickerWeek] = []
    for ticker in sorted(seen):
        appearances = [
            run.tickers[ticker] for run in window if ticker in run.tickers
        ]
        earliest, latest = appearances[0], appearances[-1]
        delta = (
            latest.composite - earliest.composite
            if latest.composite is not None and earliest.composite is not None
            and len(appearances) > 1 else None
        )
        weeks.append(TickerWeek(
            ticker=ticker,
            runs_seen=len(appearances),
            decay_hits=sum(
                snapshot_decaying(s, cfg.deteriorating_r40_trend) for s in appearances
            ),
            composite_latest=latest.composite,
            composite_delta=delta,
            rank_earliest=earliest.rank,
            rank_latest=latest.rank,
            down_changes=per_ticker_down.get(ticker, 0),
            flags_latest=list(latest.flags),
        ))

    attention = [
        w for w in weeks
        if w.decay_hits >= cfg.digest_decay_runs
        or (w.composite_delta is not None and -w.composite_delta >= cfg.week_drop_pts)
    ]
    attention.sort(key=lambda w: (
        -w.decay_hits,
        w.composite_delta if w.composite_delta is not None else 0.0,
        w.ticker,
    ))

    latest_run = window[-1]
    gaps: list[str] = []
    for ticker in universe:
        if ticker not in seen:
            gaps.append(
                f"{ticker}: configured but absent from every run in the window, "
                "check listing status"
            )
        elif ticker not in latest_run.tickers:
            gaps.append(f"{ticker}: missing from the latest run ({latest_run.date})")
    for ticker in sorted(seen - set(universe)):
        gaps.append(f"{ticker}: in run history but no longer configured")

    busiest = sorted(
        per_ticker_changes.items(), key=lambda item: (-item[1], item[0])
    )[:5]

    return WeeklyDigest(
        window_start=window[0].date,
        window_end=latest_run.date,
        runs_in_window=len(window),
        attention=attention,
        change_counts=dict(sorted(change_counts.items())),
        busiest=busiest,
        coverage_gaps=gaps,
        bench=list(bench),
        notes=list(notes or []),
    )


def _fmt(value: float | None, spec: str = ".1f") -> str:
    return format(value, spec) if value is not None else "n/a"


def _rank_cell(week: TickerWeek) -> str:
    if week.rank_earliest is None or week.rank_latest is None:
        return "n/a"
    if week.rank_earliest == week.rank_latest:
        return str(week.rank_latest)
    return f"{week.rank_earliest} -> {week.rank_latest}"


def render_markdown(
    digest: WeeklyDigest, refresh_number: int, today: date
) -> str:
    """GitHub-issue body. Plain ASCII, no em or en dashes (repo rule)."""
    lines = [
        f"# Watchlist candidate refresh #{refresh_number}",
        "",
        f"Manual refresh (calibration round {min(refresh_number, CALIBRATION_REFRESHES)}"
        f" of {CALIBRATION_REFRESHES}) generated {today.isoformat()}. Evidence window: "
        f"{digest.runs_in_window} run(s), {digest.window_start} to {digest.window_end}.",
        "",
    ]
    for note in digest.notes:
        lines.append(f"> data note: {note}")
    if digest.notes:
        lines.append("")

    lines.append("## Attention list (persistent decay)")
    if digest.attention:
        lines += [
            "",
            "| ticker | decay-gate hits | composite | window delta | rank | latest flags |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for w in digest.attention:
            flags = ", ".join(f.replace("_", " ") for f in w.flags_latest) or "n/a"
            lines.append(
                f"| {w.ticker} | {w.decay_hits} of {w.runs_seen} | "
                f"{_fmt(w.composite_latest)} | {_fmt(w.composite_delta, '+.1f')} | "
                f"{_rank_cell(w)} | {flags} |"
            )
    else:
        lines.append("")
        lines.append(
            "No ticker cleared the persistence bar this window "
            "(decay-gate hits or a week-scale composite drop). "
            "\"No changes\" is a valid refresh outcome."
        )
    lines.append("")

    lines.append("## Change activity this window")
    lines.append("")
    if digest.change_counts:
        counts = ", ".join(
            f"{kind.replace('_', ' ')}: {n}" for kind, n in digest.change_counts.items()
        )
        lines.append(f"Threshold crossings by type: {counts}.")
        if digest.busiest:
            busiest = ", ".join(f"{t} ({n})" for t, n in digest.busiest)
            lines.append(f"Noisiest tickers: {busiest}.")
    else:
        lines.append("No threshold crossings between runs in this window.")
    lines.append("")

    lines.append("## Coverage and liveness")
    lines.append("")
    if digest.coverage_gaps:
        lines += [f"- {gap}" for gap in digest.coverage_gaps]
    else:
        lines.append("All configured tickers present in every run of the window.")
    lines.append("")

    bench = ", ".join(digest.bench) if digest.bench else "n/a"
    lines += [
        "## Bench (first-call swap candidates)",
        "",
        bench + ".",
        "",
        "## Owner checklist",
        "",
        "- [ ] Review the evidence above alongside this week's daily emails",
        "- [ ] For each attention-list name, decide: hold, or propose a swap "
        "(promote bench or a new candidate)",
        "- [ ] If no swaps, note \"no changes\" here and close the issue",
        "- [ ] Any watchlist.yaml edit lands via an owner-reviewed PR "
        "(the list stays owner-gated)",
        "- [ ] Record what evidence mattered (and what was noise) in SPEC.md "
        "section 7.0, building the rotation rubric",
    ]
    if refresh_number >= CALIBRATION_REFRESHES:
        lines.append(
            "- [ ] Calibration rounds complete: decide whether to automate "
            "proposal drafting against the written rubric (option 3, a scheduled "
            "agent that drafts the swap PR), or keep the refresh manual"
        )
    lines.append("")
    return "\n".join(lines)


def build_from_files(
    config_path: Path | None = None, history_path: Path | None = None
) -> tuple[WeeklyDigest, Config]:
    cfg = load_config(config_path)
    raw_runs, notes = load_history(history_path)
    runs = [RunSnapshot.from_dict(r) for r in raw_runs]
    return build_digest(runs, cfg.all_tickers, list(cfg.bench), cfg.changes, notes), cfg


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="weekly watchlist-refresh digest")
    parser.add_argument("--refresh-number", type=int, default=1)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--history", type=Path, default=None)
    parser.add_argument("--date", type=date.fromisoformat, default=None,
                        help="digest date (ISO), defaults to today")
    parser.add_argument("--out", type=Path, default=None,
                        help="write markdown here instead of stdout")
    args = parser.parse_args(argv)

    digest, _ = build_from_files(args.config, args.history)
    text = render_markdown(digest, args.refresh_number, args.date or date.today())
    if args.out:
        args.out.write_text(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
