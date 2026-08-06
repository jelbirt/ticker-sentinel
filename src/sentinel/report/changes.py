"""Day-over-day change detection: snapshots, diffs, deterioration (pure logic, no I/O).

The report leads with what changed since the prior run. Prior-run state comes from
data/cache/run_history.json (see sentinel.data.history for the I/O side); this
module only builds snapshots from scorecards and compares them.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import date

from sentinel.config import ChangesCfg
from sentinel.indicators.fundamentals import Scorecard
from sentinel.indicators.signals import net_revisions_30d


@dataclass(frozen=True)
class TickerSnapshot:
    """One ticker's persisted outputs for one run (spec 2.2).

    Scores are on their rendered 0-100 scale; ratios are fractions, matching
    Scorecard. None means unknown and is stored as an explicit null.
    """

    composite: float | None = None
    score: float | None = None
    technical_score: float | None = None
    rank: int | None = None
    r40_fcf: float | None = None
    r40_trend: float | None = None
    trend_state: str | None = None
    golden_cross: bool = False
    death_cross: bool = False
    flags: list[str] = field(default_factory=list)
    valuation: str | None = None
    net_revisions_30d: int | None = None
    short_pct_float: float | None = None
    shares_short: float | None = None


@dataclass(frozen=True)
class RunSnapshot:
    date: str                 # ISO date; one entry per calendar date
    run_type: str
    tickers: dict[str, TickerSnapshot] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "run_type": self.run_type,
            "tickers": {t: asdict(snap) for t, snap in sorted(self.tickers.items())},
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "RunSnapshot":
        known = {f.name for f in fields(TickerSnapshot)}
        tickers = {
            t: TickerSnapshot(**{k: v for k, v in snap.items() if k in known})
            for t, snap in (raw.get("tickers") or {}).items()
        }
        return cls(
            date=str(raw.get("date", "")),
            run_type=str(raw.get("run_type", "")),
            tickers=tickers,
        )


def snapshot_from_scorecards(
    ranked: list[Scorecard], today: date, run_type: str
) -> RunSnapshot:
    """Snapshot the scored universe. `ranked` must already be in rank order
    (the caller sorts with the configured ranking mode); rank = position."""
    tickers: dict[str, TickerSnapshot] = {}
    for position, sc in enumerate(ranked, start=1):
        tech = sc.tech
        signals = sc.signals
        tickers[sc.ticker] = TickerSnapshot(
            composite=sc.composite,
            score=sc.score,
            technical_score=sc.technical_score,
            rank=position,
            r40_fcf=sc.r40_fcf,
            r40_trend=sc.r40_trend,
            trend_state=tech.trend_state if tech is not None else None,
            golden_cross=bool(tech.golden_cross_recent) if tech is not None else False,
            death_cross=bool(tech.death_cross_recent) if tech is not None else False,
            flags=sorted(sc.flags),
            valuation=sc.valuation,
            net_revisions_30d=net_revisions_30d(signals) if signals is not None else None,
            short_pct_float=signals.short_pct_float if signals is not None else None,
            shares_short=signals.shares_short if signals is not None else None,
        )
    return RunSnapshot(date=today.isoformat(), run_type=run_type, tickers=tickers)


@dataclass(frozen=True)
class Change:
    """One reportable difference vs the prior run. `detail` is user-visible text
    (plain ASCII arrows, never em/en dashes); `direction` drives the arrow glyph:
    up (improving), down (worsening), info (neutral)."""

    ticker: str
    kind: str      # score | rank | flag_set | flag_cleared | r40_inflection |
                   # trend_state | new_cross | revisions | short_interest |
                   # universe_added | universe_removed
    detail: str
    direction: str = "info"


@dataclass(frozen=True)
class ChangeSet:
    prior_date: str | None = None       # None: first run, no baseline
    changes: list[Change] = field(default_factory=list)

    @property
    def quiet(self) -> bool:
        """True when a baseline existed and nothing cleared a threshold."""
        return self.prior_date is not None and not self.changes


_TREND_LEVEL = {"downtrend": 0, "mixed": 1, "uptrend": 2}


def _human(flag: str) -> str:
    return flag.replace("_", " ")


def _sign(v: float) -> int:
    return (v > 0) - (v < 0)


def _ticker_changes(
    ticker: str, cur: TickerSnapshot, prior: TickerSnapshot, cfg: ChangesCfg
) -> list[Change]:
    out: list[Change] = []

    if cur.composite is not None and prior.composite is not None:
        delta = cur.composite - prior.composite
        if abs(delta) >= cfg.score_delta_pts:
            out.append(Change(
                ticker, "score",
                f"composite {cur.composite:.1f} ({delta:+.1f})",
                "up" if delta > 0 else "down",
            ))

    if cur.rank is not None and prior.rank is not None:
        if abs(cur.rank - prior.rank) >= cfg.rank_delta:
            out.append(Change(
                ticker, "rank",
                f"rank {prior.rank} -> {cur.rank}",
                "up" if cur.rank < prior.rank else "down",
            ))

    for flag in sorted(set(cur.flags) - set(prior.flags)):
        out.append(Change(ticker, "flag_set", f"flag set: {_human(flag)}"))
    for flag in sorted(set(prior.flags) - set(cur.flags)):
        out.append(Change(ticker, "flag_cleared", f"flag cleared: {_human(flag)}"))

    if cur.r40_trend is not None and prior.r40_trend is not None:
        threshold = cfg.deteriorating_r40_trend
        sign_flip = _sign(cur.r40_trend) != _sign(prior.r40_trend)
        threshold_cross = (cur.r40_trend < threshold) != (prior.r40_trend < threshold)
        if sign_flip or threshold_cross:
            out.append(Change(
                ticker, "r40_inflection",
                f"R40 trend {prior.r40_trend * 100:+.1f} -> {cur.r40_trend * 100:+.1f} pts",
                "up" if cur.r40_trend > prior.r40_trend else "down",
            ))

    if (
        cur.trend_state is not None
        and prior.trend_state is not None
        and cur.trend_state != prior.trend_state
    ):
        was = _TREND_LEVEL.get(prior.trend_state, 1)
        now = _TREND_LEVEL.get(cur.trend_state, 1)
        out.append(Change(
            ticker, "trend_state",
            f"trend {prior.trend_state} -> {cur.trend_state}",
            "up" if now > was else "down" if now < was else "info",
        ))

    if cur.golden_cross and not prior.golden_cross:
        out.append(Change(ticker, "new_cross", "new golden cross", "up"))
    if cur.death_cross and not prior.death_cross:
        out.append(Change(ticker, "new_cross", "new death cross", "down"))

    if cur.net_revisions_30d is not None and prior.net_revisions_30d is not None:
        swing = cur.net_revisions_30d - prior.net_revisions_30d
        if abs(swing) >= cfg.revision_swing:
            out.append(Change(
                ticker, "revisions",
                f"net 30d revisions {prior.net_revisions_30d:+d} -> {cur.net_revisions_30d:+d}",
                "up" if swing > 0 else "down",
            ))

    if (
        cur.shares_short is not None
        and prior.shares_short is not None
        and prior.shares_short > 0
        and cur.shares_short != prior.shares_short
    ):
        frac = cur.shares_short / prior.shares_short - 1
        if abs(frac) >= cfg.short_delta:
            out.append(Change(
                ticker, "short_interest",
                f"short interest {frac * 100:+.0f}% (new reading)",
                "up" if frac > 0 else "down",   # up = shorts rising
            ))

    return out


def diff_runs(
    current: RunSnapshot, prior: RunSnapshot | None, cfg: ChangesCfg
) -> ChangeSet:
    """Compare two runs; every threshold comes from config. None on either side of
    a comparison means unknown and never fabricates a change (spec 3.2)."""
    if prior is None:
        return ChangeSet(prior_date=None, changes=[])

    changes: list[Change] = []
    for ticker in sorted(set(current.tickers) | set(prior.tickers)):
        cur_snap, prior_snap = current.tickers.get(ticker), prior.tickers.get(ticker)
        if cur_snap is None:
            changes.append(Change(ticker, "universe_removed", "dropped from scored universe"))
        elif prior_snap is None:
            changes.append(Change(ticker, "universe_added", "added to scored universe"))
        else:
            changes.extend(_ticker_changes(ticker, cur_snap, prior_snap, cfg))

    def _move(ticker: str) -> float:
        cur_snap, prior_snap = current.tickers.get(ticker), prior.tickers.get(ticker)
        if (
            cur_snap is None or prior_snap is None
            or cur_snap.composite is None or prior_snap.composite is None
        ):
            return 0.0
        return abs(cur_snap.composite - prior_snap.composite)

    changes.sort(key=lambda c: (-_move(c.ticker), c.ticker))
    return ChangeSet(prior_date=prior.date, changes=changes)
