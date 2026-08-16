"""Weekly watchlist-refresh digest: evidence summary for the owner's candidate
refresh session (spec 7.0). Pure aggregation over committed run history; no
network, no scoring. The weekly-refresh workflow renders this into a GitHub
issue; the swap decision itself stays owner-gated.

Run: python -m sentinel.digest [--refresh-number N] [--out PATH] [--json PATH]
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from sentinel.config import ChangesCfg, Config, load_config
from sentinel.data.history import load_history
from sentinel.report.changes import RunSnapshot, TickerSnapshot, diff_runs

# manual refreshes before the digest starts prompting the automation decision
CALIBRATION_REFRESHES = 3

# Flag vocabulary split (SPEC 7.0.1 round-1 lesson): a fetch or coverage
# problem is not a rotation signal, and the attention table has to make that
# visible per name instead of pooling both kinds in one cell. Membership here
# is the data-quality side; everything else is business. test_digest pins that
# every flag constant in the codebase is classified, so a new flag cannot slip
# in unclassified.
DATA_QUALITY_FLAGS = frozenset({
    "insufficient_data",       # fewer than 4 quarters: no TTM at all
    "insufficient_history",    # trend/growth window unavailable
    "growth_from_annual",      # growth fell back to FY over FY
    "stale_fundamentals",      # statements more than 200 days old
})
# The business side, listed explicitly so the classification is an inventory a
# reader can check rather than a silent default. classify_flags() still treats
# anything unlisted as business, so a new flag renders sensibly before anyone
# gets round to classifying it; the test enforces that "round to" is the same
# commit that adds the flag.
BUSINESS_FLAGS = frozenset({
    "sbc_inflated",            # r40_fcf minus r40_sbc_adj > 20 pts
    "high_sbc",                # sbc_intensity > 15%
    "dilution",                # dilution > 3%/yr
    "passes_all_r40",          # all three R40 variants clear 40
    "golden_cross",
    "death_cross",
})


def classify_flags(flags: list[str]) -> tuple[list[str], list[str]]:
    """Split persisted flags into (data quality, business), order preserved.

    Unknown flags count as business: a flag nobody classified is more likely a
    new business signal than a new fetch problem, and putting it where the
    owner reads rotation evidence is the safer error.
    """
    quality = [f for f in flags if f in DATA_QUALITY_FLAGS]
    business = [f for f in flags if f not in DATA_QUALITY_FLAGS]
    return quality, business


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
    decay_streak: int                  # longest CONSECUTIVE run of those hits
    composite_latest: float | None
    composite_delta: float | None      # latest minus earliest appearance
    rank_earliest: int | None
    rank_latest: int | None
    down_changes: int                  # worsening threshold crossings in window
    flags_latest: list[str] = field(default_factory=list)
    flags_data_quality: list[str] = field(default_factory=list)
    flags_business: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BenchWeek:
    """One bench name's window-scale composite move, on the same first-vs-last
    basis as the attention list so the two tables can be read side by side."""

    ticker: str
    runs_seen: int
    composite_first: float | None
    composite_last: float | None
    composite_delta: float | None
    flags_latest: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CoverageGap:
    """A scored name that is missing, or a history name no longer configured.

    Structured rather than a rendered string because the streak is the point
    (SPEC 7.0.1 round-1 gap 1): a 2-run streak and a 1-run blip are different
    evidence, and the JSON digest needs the number, not the prose.
    """

    ticker: str
    kind: str                     # absent_all | missing_streak | unconfigured
    streak: int = 0               # consecutive runs missing at the END of the window
    runs_seen: int = 0            # runs in the window where the name appeared
    runs_in_window: int = 0
    since: str | None = None      # first run date of the missing streak
    until: str | None = None      # latest run date in the window

    @property
    def text(self) -> str:
        if self.kind == "no_history":
            return f"no run history yet for {self.runs_seen} scored tickers"
        if self.kind == "unconfigured":
            return f"{self.ticker}: in run history but no longer configured"
        if self.kind == "absent_all":
            return (
                f"{self.ticker}: configured but absent from every run in the "
                f"window ({self.streak} runs), check listing status"
            )
        if self.streak <= 1:
            return f"{self.ticker}: missing from the latest run ({self.until})"
        return (
            f"{self.ticker}: missing from the latest {self.streak} runs "
            f"({self.since} to {self.until}), seen in {self.runs_seen} of "
            f"{self.runs_in_window} runs this window"
        )


@dataclass(frozen=True)
class WeeklyDigest:
    window_start: str
    window_end: str
    runs_in_window: int
    attention: list[TickerWeek]        # persistent decliners, worst first
    change_counts: dict[str, int]      # change kind -> occurrences in window
    busiest: list[tuple[str, int]]     # (ticker, change count), noisiest first
    coverage_gaps: list[CoverageGap]
    bench: list[str]
    notes: list[str]                   # degradation notes (unreadable history etc.)
    bench_weeks: list[BenchWeek] = field(default_factory=list)


def _window(runs: list[RunSnapshot], cfg: ChangesCfg) -> list[RunSnapshot]:
    return runs[-max(cfg.week_window_runs, 1):]


def _longest_decay_streak(
    window: list[RunSnapshot], ticker: str, threshold: float
) -> int:
    """Longest run of CONSECUTIVE decay-gate hits in the window.

    A run the name is missing from breaks the streak: an absence is not
    evidence that the gate held. Reported alongside the raw hit count so a
    persistent decline reads differently from the same number of scattered
    hits (the gate itself still counts hits, unchanged)."""
    best = current = 0
    for run in window:
        snap = run.tickers.get(ticker)
        if snap is not None and snapshot_decaying(snap, threshold):
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def _missing_streak(window: list[RunSnapshot], ticker: str) -> int:
    """Consecutive runs, counting back from the latest, without this ticker."""
    streak = 0
    for run in reversed(window):
        if ticker in run.tickers:
            break
        streak += 1
    return streak


def _bench_weeks(
    window: list[RunSnapshot], bench: list[str]
) -> list[BenchWeek]:
    """Window-scale composite move per bench name, first vs last appearance.

    Names configured on the bench but absent from history (every run predating
    bench shadow-scoring) are simply not returned; the renderer turns an empty
    list into a warm-up note rather than an error.
    """
    seen: list[str] = []
    for run in window:
        for ticker in run.bench:
            if ticker not in seen:
                seen.append(ticker)
    order = [t for t in bench if t in seen] + [t for t in sorted(seen) if t not in bench]

    weeks: list[BenchWeek] = []
    for ticker in order:
        appearances = [run.bench[ticker] for run in window if ticker in run.bench]
        valued = [a.composite for a in appearances if a.composite is not None]
        weeks.append(BenchWeek(
            ticker=ticker,
            runs_seen=len(appearances),
            composite_first=valued[0] if valued else None,
            composite_last=valued[-1] if valued else None,
            composite_delta=valued[-1] - valued[0] if len(valued) > 1 else None,
            flags_latest=list(appearances[-1].flags) if appearances else [],
        ))
    return weeks


def build_digest(
    runs: list[RunSnapshot],
    universe: list[str],
    bench: list[str],
    cfg: ChangesCfg,
    notes: list[str] | None = None,
) -> WeeklyDigest:
    """Aggregate the last week_window_runs runs. Every threshold comes from cfg;
    absent values never fabricate evidence (same contract as diff_runs).

    `universe` is the set of tickers run history is expected to contain, i.e.
    the scored (r40-tagged) names. Coverage gaps are measured against it, so
    passing the full configured universe would report tech-only names as
    permanently missing.
    """
    window = _window(runs, cfg)
    if not window:
        return WeeklyDigest(
            window_start="n/a", window_end="n/a", runs_in_window=0,
            attention=[], change_counts={}, busiest=[],
            coverage_gaps=[
                CoverageGap(ticker="", kind="no_history", runs_seen=len(universe))
            ],
            bench=list(bench), notes=list(notes or []), bench_weeks=[],
        )

    change_counts: dict[str, int] = {}
    per_ticker_changes: dict[str, int] = {}
    per_ticker_down: dict[str, int] = {}
    for prior, current in zip(window, window[1:]):
        for change in diff_runs(current, prior, cfg).changes:
            change_counts[change.kind] = change_counts.get(change.kind, 0) + 1
            if change.kind == "score_basis":
                # basis rows describe data availability, not ticker behavior,
                # and a collapsed row's pseudo-ticker "watchlist" must never
                # compete in the noisiest-tickers ranking
                continue
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
        # delta anchors on observed composites only: a rate-limited run that
        # degraded to None must not null the week-scale evidence around it
        valued = [a.composite for a in appearances if a.composite is not None]
        delta = valued[-1] - valued[0] if len(valued) > 1 else None
        quality_flags, business_flags = classify_flags(list(latest.flags))
        weeks.append(TickerWeek(
            ticker=ticker,
            runs_seen=len(appearances),
            decay_hits=sum(
                snapshot_decaying(s, cfg.deteriorating_r40_trend) for s in appearances
            ),
            decay_streak=_longest_decay_streak(
                window, ticker, cfg.deteriorating_r40_trend
            ),
            composite_latest=latest.composite,
            composite_delta=delta,
            rank_earliest=earliest.rank,
            rank_latest=latest.rank,
            down_changes=per_ticker_down.get(ticker, 0),
            flags_latest=list(latest.flags),
            flags_data_quality=quality_flags,
            flags_business=business_flags,
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
    gaps: list[CoverageGap] = []
    for ticker in universe:
        if ticker not in seen:
            gaps.append(CoverageGap(
                ticker=ticker, kind="absent_all", streak=len(window),
                runs_seen=0, runs_in_window=len(window),
                since=window[0].date, until=latest_run.date,
            ))
        elif ticker not in latest_run.tickers:
            # a streak, not a binary: SPEC 7.0.1 round-1 gap 1, where a 2-run
            # absence and a 1-run blip read identically and TEAM's real gap
            # was indistinguishable from noise
            streak = _missing_streak(window, ticker)
            gaps.append(CoverageGap(
                ticker=ticker, kind="missing_streak", streak=streak,
                runs_seen=sum(1 for r in window if ticker in r.tickers),
                runs_in_window=len(window),
                since=window[len(window) - streak].date, until=latest_run.date,
            ))
    for ticker in sorted(seen - set(universe)):
        gaps.append(CoverageGap(
            ticker=ticker, kind="unconfigured",
            runs_seen=sum(1 for r in window if ticker in r.tickers),
            runs_in_window=len(window), until=latest_run.date,
        ))

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
        bench_weeks=_bench_weeks(window, list(bench)),
    )


def _fmt(value: float | None, spec: str = ".1f") -> str:
    return format(value, spec) if value is not None else "n/a"


def _flag_cell(flags: list[str]) -> str:
    return ", ".join(f.replace("_", " ") for f in flags) or "n/a"


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
    calibration = (
        f" (calibration round {refresh_number} of {CALIBRATION_REFRESHES})"
        if refresh_number <= CALIBRATION_REFRESHES else ""
    )
    lines = [
        f"# Watchlist candidate refresh #{refresh_number}",
        "",
        f"Manual refresh{calibration} generated {today.isoformat()}. Evidence window: "
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
            "Flags are split because a fetch or coverage problem is not a "
            "rotation signal (rubric, round 1): read the business column for "
            "decay evidence and the data-quality column as a to-fix list.",
            "",
            "| ticker | decay-gate hits | decay streak | composite | window delta "
            "| rank | business flags | data-quality flags |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for w in digest.attention:
            business = _flag_cell(w.flags_business)
            quality = _flag_cell(w.flags_data_quality)
            lines.append(
                f"| {w.ticker} | {w.decay_hits} of {w.runs_seen} | "
                f"{w.decay_streak} consecutive | "
                f"{_fmt(w.composite_latest)} | {_fmt(w.composite_delta, '+.1f')} | "
                f"{_rank_cell(w)} | {business} | {quality} |"
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
        lines += [f"- {gap.text}" for gap in digest.coverage_gaps]
    else:
        lines.append("All configured tickers present in every run of the window.")
    lines.append("")

    bench = ", ".join(digest.bench) if digest.bench else "n/a"
    lines += [
        "## Bench (first-call swap candidates)",
        "",
        bench + ".",
        "",
    ]
    if digest.bench_weeks:
        lines += [
            "Shadow-scored every run on the same basis as the watchlist, so "
            "these composites compare directly with the attention list above. "
            "Unranked, and never part of any watchlist number.",
            "",
            "| ticker | runs seen | composite | window delta | latest flags |",
            "| --- | --- | --- | --- | --- |",
        ]
        for b in digest.bench_weeks:
            lines.append(
                f"| {b.ticker} | {b.runs_seen} of {digest.runs_in_window} | "
                f"{_fmt(b.composite_last)} | {_fmt(b.composite_delta, '+.1f')} | "
                f"{_flag_cell(b.flags_latest)} |"
            )
        scored_bench = {b.ticker for b in digest.bench_weeks}
        unseen = [t for t in digest.bench if t not in scored_bench]
        if unseen:
            lines += [
                "",
                f"No snapshots this window for: {', '.join(unseen)} "
                "(newly benched, or its fundamentals did not fetch).",
            ]
    else:
        lines.append(
            "No bench snapshots in this window yet: bench shadow-scoring "
            "starts with the first scheduled run after it ships, so early "
            "windows are warming up rather than failing."
        )
    lines += [
        "",
        "## Owner checklist",
        "",
        "- [ ] Review the evidence above alongside this week's daily emails",
        "- [ ] For each attention-list name, decide: hold, or propose a swap "
        "(promote bench or a new candidate)",
        "- [ ] If no swaps, note \"no changes\" here and close the issue",
        "- [ ] Any watchlist.yaml edit lands via an owner-reviewed PR "
        "(the list stays owner-gated)",
        "- [ ] For any promoted name, seed and backfill its history so it does "
        "not restart the r40_trend warm-up: dry run "
        "`python -m sentinel.backfill --dry-run --tickers NEW` with the swap PR, "
        "then the owner-gated `--apply` on main after it merges (SPEC.md 7.0)",
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


def render_json(digest: WeeklyDigest, refresh_number: int, today: date) -> str:
    """Machine-readable twin of render_markdown.

    Substrate for the refresh #3 automate-vs-manual decision: an agent drafting
    swap proposals against the written rubric needs the streaks and the flag
    split as numbers and lists, not parsed out of a table. Stable sorted keys
    and ISO dates so successive weeks diff cleanly.
    """
    payload = asdict(digest)
    payload["coverage_gaps"] = [
        {**asdict(gap), "text": gap.text} for gap in digest.coverage_gaps
    ]
    payload["busiest"] = [
        {"ticker": ticker, "changes": n} for ticker, n in digest.busiest
    ]
    payload["refresh_number"] = refresh_number
    payload["generated"] = today.isoformat()
    payload["calibration_refreshes"] = CALIBRATION_REFRESHES
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def build_from_files(
    config_path: Path | None = None, history_path: Path | None = None
) -> tuple[WeeklyDigest, Config]:
    cfg = load_config(config_path)
    raw_runs, notes = load_history(history_path)
    runs = [RunSnapshot.from_dict(r) for r in raw_runs]
    # r40 names only: run history records the scored set, and a tech-only
    # ticker never enters it. Passing the whole universe would report every
    # non-r40 name as "absent from every run, check listing status" forever.
    return build_digest(runs, cfg.r40_tickers, list(cfg.bench), cfg.changes, notes), cfg


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="weekly watchlist-refresh digest")
    parser.add_argument("--refresh-number", type=int, default=1)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--history", type=Path, default=None)
    parser.add_argument("--date", type=date.fromisoformat, default=None,
                        help="digest date (ISO), defaults to today")
    parser.add_argument("--out", type=Path, default=None,
                        help="write markdown here instead of stdout")
    parser.add_argument("--json", type=Path, default=None, dest="json_out",
                        help="also write the digest as JSON here (machine-readable)")
    args = parser.parse_args(argv)

    today = args.date or date.today()
    digest, _ = build_from_files(args.config, args.history)
    text = render_markdown(digest, args.refresh_number, today)
    if args.out:
        args.out.write_text(text)
    else:
        sys.stdout.write(text)
    if args.json_out:
        args.json_out.write_text(render_json(digest, args.refresh_number, today))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
