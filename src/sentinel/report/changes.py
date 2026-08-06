"""Day-over-day change detection: snapshots, diffs, deterioration (pure logic, no I/O).

The report leads with what changed since the prior run. Prior-run state comes from
data/cache/run_history.json (see sentinel.data.history for the I/O side); this
module only builds snapshots from scorecards and compares them.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import date

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
