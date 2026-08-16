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
from sentinel.indicators.signals import (
    SHORT_MOM_ALERT,
    net_revisions_30d,
    short_change_mom,
)


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


def _snapshots(raw: object) -> dict[str, TickerSnapshot]:
    """Parse one persisted ticker->snapshot mapping, tolerating garbage.

    Anything that is not a mapping of objects is dropped rather than raised:
    the bench block in particular must never be able to take change detection
    for the scored universe down with it.
    """
    known = {f.name for f in fields(TickerSnapshot)}
    out: dict[str, TickerSnapshot] = {}
    if not isinstance(raw, dict):
        return out
    for ticker, snap in raw.items():
        if not isinstance(snap, dict):
            continue
        kw = {k: v for k, v in snap.items() if k in known}
        if not isinstance(kw.get("flags"), list):  # null/garbage never crashes a diff
            kw["flags"] = []
        out[str(ticker)] = TickerSnapshot(**kw)
    return out


@dataclass(frozen=True)
class RunSnapshot:
    date: str                 # ISO date; one entry per calendar date
    run_type: str
    tickers: dict[str, TickerSnapshot] = field(default_factory=dict)
    # shadow-scored bench names: a SIBLING of `tickers`, never merged into it.
    # Nothing that ranks, diffs, gates or alerts reads this key; it exists so
    # the weekly digest can compare rotation candidates on the same basis as
    # the attention list (spec 7.0.1).
    bench: dict[str, TickerSnapshot] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "run_type": self.run_type,
            "tickers": {t: asdict(snap) for t, snap in sorted(self.tickers.items())},
            "bench": {t: asdict(snap) for t, snap in sorted(self.bench.items())},
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "RunSnapshot":
        return cls(
            date=str(raw.get("date", "")),
            run_type=str(raw.get("run_type", "")),
            tickers=_snapshots(raw.get("tickers")),
            bench=_snapshots(raw.get("bench")),
        )


def _ticker_snapshot(sc: Scorecard, rank: int | None) -> TickerSnapshot:
    tech = sc.tech
    signals = sc.signals
    return TickerSnapshot(
        composite=sc.composite,
        score=sc.score,
        technical_score=sc.technical_score,
        rank=rank,
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


def snapshot_from_scorecards(
    ranked: list[Scorecard], today: date, run_type: str,
    bench: list[Scorecard] | None = None,
) -> RunSnapshot:
    """Snapshot the scored universe. `ranked` must already be in rank order
    (the caller sorts with the configured ranking mode); rank = position.

    `bench` names are shadow-scored comparison references: they land in the
    sibling `bench` block with rank None, because they were never ranked
    against anything. Between-quarter signals are not fetched for them, so
    their signal-derived fields persist as null.
    """
    tickers = {
        sc.ticker: _ticker_snapshot(sc, position)
        for position, sc in enumerate(ranked, start=1)
    }
    bench_snaps = {
        sc.ticker: _ticker_snapshot(sc, None) for sc in (bench or [])
    }
    return RunSnapshot(
        date=today.isoformat(), run_type=run_type,
        tickers=tickers, bench=bench_snaps,
    )


@dataclass(frozen=True)
class Change:
    """One reportable difference vs the prior run. `detail` is user-visible text
    (plain ASCII arrows, never em/en dashes); `direction` drives the arrow glyph:
    up (improving), down (worsening), info (neutral)."""

    ticker: str
    kind: str      # score | score_basis | rank | flag_set | flag_cleared |
                   # r40_inflection | trend_state | new_cross | revisions |
                   # short_interest | universe_added | universe_removed
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

# collapse per-ticker basis-flip rows into one line when at least this many
# tickers AND more than half the compared universe flipped together (a
# wholesale price outage must not bury real changes under 20 identical rows)
BASIS_COLLAPSE_MIN = 3


def same_score_basis(cur: TickerSnapshot, prior: TickerSnapshot) -> bool:
    """Composites are only comparable when both were built the same way.

    C = w_f F + w_t T when technicals ran, but C falls back to F alone when
    they did not (scoring.composite_score), so a day of missing price data
    shifts every composite by construction, not by anything real. Diffing
    across that boundary fabricates large score and rank moves.
    """
    return (cur.technical_score is None) == (prior.technical_score is None)


def _scorecard_basis_matches(sc: Scorecard, snap: TickerSnapshot) -> bool:
    """same_score_basis for the live-Scorecard vs persisted-snapshot pairing."""
    return (sc.technical_score is None) == (snap.technical_score is None)


def baseline_reference(
    prior_tickers: set[str] | None, intended: set[str]
) -> set[str]:
    """Reference set for the degraded-run gate.

    The prior baseline's names restricted to what this run INTENDED to score
    (the configured universe, or the fixture set on a dry run). Restricting
    matters: without it, a deliberate watchlist shrink below the gate fraction
    would deadlock change detection forever, because a rejected run is never
    saved and so the stale reference could never shrink. Names removed on
    purpose drop out of the reference immediately; a wholesale outage leaves
    the intended set unchanged and still fails the gate.
    """
    if not prior_tickers:
        return intended
    return prior_tickers & intended


def baseline_ok(
    scored_tickers: set[str], reference: set[str], min_fraction: float
) -> bool:
    """Should this run's snapshot be diffed and become tomorrow's baseline?

    Gates on the overlap with `reference`, which the caller sets to the PRIOR
    baseline's tickers (falling back to the configured universe on a first
    run): a wholesale fetch outage empties the overlap and is rejected, while
    a watchlist expansion merely adds unscored NEW names and passes, so
    change detection keeps running for the names that were diffable
    yesterday. Rejected runs are reported with a note but neither diffed
    (wall of universe_removed rows) nor saved (mirror-image wall tomorrow).
    """
    if not reference:
        return True
    return len(scored_tickers & reference) >= min_fraction * len(reference)


def _human(flag: str) -> str:
    return flag.replace("_", " ")


def _sign(v: float) -> int:
    return (v > 0) - (v < 0)


def _ticker_changes(
    ticker: str, cur: TickerSnapshot, prior: TickerSnapshot, cfg: ChangesCfg
) -> list[Change]:
    out: list[Change] = []

    basis_same = same_score_basis(cur, prior)
    if cur.composite is not None and prior.composite is not None:
        if basis_same:
            delta = cur.composite - prior.composite
            if abs(delta) >= cfg.score_delta_pts:
                out.append(Change(
                    ticker, "score",
                    f"composite {cur.composite:.1f} ({delta:+.1f})",
                    "up" if delta > 0 else "down",
                ))
        else:
            # construction changed (technicals dropped out or came back):
            # composite deltas are fabrication, so compare F to F instead
            tech_word = "unavailable" if cur.technical_score is None else "restored"
            if (
                cur.score is not None and prior.score is not None
                and abs(cur.score - prior.score) >= cfg.score_delta_pts
            ):
                delta = cur.score - prior.score
                out.append(Change(
                    ticker, "score",
                    f"fundamental score {cur.score:.1f} ({delta:+.1f}); "
                    f"technicals {tech_word}, composite not comparable",
                    "up" if delta > 0 else "down",
                ))
            else:
                out.append(Change(
                    ticker, "score_basis",
                    f"technicals {tech_word}; composite basis changed, "
                    "day-over-day move not comparable",
                ))

    if cur.rank is not None and prior.rank is not None and basis_same:
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
                "down" if frac > 0 else "up",   # rising shorts = worsening
            ))

    return out


def diff_runs(
    current: RunSnapshot,
    prior: RunSnapshot | None,
    cfg: ChangesCfg,
    unscored_reasons: dict[str, str] | None = None,
) -> ChangeSet:
    """Compare two runs; every threshold comes from config. None on either side of
    a comparison means unknown and never fabricates a change (spec 3.2).

    `unscored_reasons` (ticker -> human-readable reason) lets universe_removed
    rows say WHY a name left the scored set: "dropped: newest quarter partial"
    reads very differently from an unexplained disappearance that looks like a
    delisting.
    """
    if prior is None:
        return ChangeSet(prior_date=None, changes=[])

    changes: list[Change] = []
    compared = 0
    for ticker in sorted(set(current.tickers) | set(prior.tickers)):
        cur_snap, prior_snap = current.tickers.get(ticker), prior.tickers.get(ticker)
        if cur_snap is None:
            reason = (unscored_reasons or {}).get(ticker)
            detail = "dropped from scored universe" + (f" ({reason})" if reason else "")
            changes.append(Change(ticker, "universe_removed", detail))
        elif prior_snap is None:
            changes.append(Change(ticker, "universe_added", "added to scored universe"))
        else:
            compared += 1
            changes.extend(_ticker_changes(ticker, cur_snap, prior_snap, cfg))

    # a wholesale technicals outage (or recovery) flips the basis for the whole
    # universe at once; 20 identical per-ticker rows would bury the real
    # changes. Only rows in the majority direction collapse, so a mixed day
    # (some names losing technicals, some regaining) never miscounts: the
    # minority stays as per-ticker rows.
    basis_rows = [c for c in changes if c.kind == "score_basis"]
    if basis_rows:
        n_gone = sum("unavailable" in c.detail for c in basis_rows)
        word = "unavailable" if n_gone * 2 >= len(basis_rows) else "restored"
        collapsed = [c for c in basis_rows if word in c.detail]
        if len(collapsed) >= BASIS_COLLAPSE_MIN and len(collapsed) * 2 > compared:
            changes = [c for c in changes if c not in collapsed]
            changes.append(Change(
                "watchlist", "score_basis",
                f"technicals {word} for {len(collapsed)} names; composite "
                "moves not comparable for them today",
            ))

    def _move(ticker: str) -> float:
        cur_snap, prior_snap = current.tickers.get(ticker), prior.tickers.get(ticker)
        if (
            cur_snap is None or prior_snap is None
            or cur_snap.composite is None or prior_snap.composite is None
        ):
            return 0.0
        if not same_score_basis(cur_snap, prior_snap):
            # the composite shift is a construction artifact (see above);
            # rank flipped tickers by their real F move instead
            if cur_snap.score is not None and prior_snap.score is not None:
                return abs(cur_snap.score - prior_snap.score)
            return 0.0
        return abs(cur_snap.composite - prior_snap.composite)

    changes.sort(key=lambda c: (-_move(c.ticker), c.ticker))
    return ChangeSet(prior_date=prior.date, changes=changes)


def select_baselines(
    runs: list[RunSnapshot], today: str, week_window: int
) -> tuple[RunSnapshot | None, RunSnapshot | None, int]:
    """Pick (prior run, week-ago run, actual span in runs) from oldest-first
    history. Entries dated today are skipped so a same-day re-run compares
    against yesterday, not itself. Today vs week-ago spans exactly `week_window`
    run-steps (one Tue-Sat week at the default 5). Short history falls back to
    the oldest entry; the span tells the report what window it actually got."""
    eligible = [r for r in runs if r.date < today]
    if not eligible:
        return None, None, 0
    prior_idx = len(eligible) - 1
    week_idx = max(prior_idx - (week_window - 1), 0)
    return eligible[prior_idx], eligible[week_idx], prior_idx - week_idx + 1


def deteriorating(sc: Scorecard, threshold: float) -> bool:
    """Plan section 6 weakest-buy priority: R40 fell past `threshold` AND
    technical confirmation (downtrend or recent death cross)."""
    fell = sc.r40_trend is not None and sc.r40_trend < threshold
    tech_confirms = sc.tech is not None and (
        sc.tech.trend_state == "downtrend" or sc.tech.death_cross_recent
    )
    return fell and tech_confirms


@dataclass(frozen=True)
class DeteriorationRow:
    ticker: str
    composite: float | None
    delta_1run: float | None       # composite change vs the prior run
    delta_week: float | None       # composite change vs the week-ago run
    reasons: list[str]             # one clause per triggered signal, dash-free


def _negative_signals(
    sc: Scorecard,
    prior: TickerSnapshot | None,
    week_ago: TickerSnapshot | None,
    cfg: ChangesCfg,
) -> list[str]:
    reasons: list[str] = []

    if (
        sc.composite is not None and prior is not None and prior.composite is not None
        and _scorecard_basis_matches(sc, prior)
        and prior.composite - sc.composite >= cfg.score_delta_pts
    ):
        reasons.append(f"composite fell {prior.composite - sc.composite:.1f} since prior run")

    if (
        sc.composite is not None and week_ago is not None
        and week_ago.composite is not None
        and _scorecard_basis_matches(sc, week_ago)
        and week_ago.composite - sc.composite >= cfg.week_drop_pts
    ):
        reasons.append(
            f"composite fell {week_ago.composite - sc.composite:.1f} over the week window"
        )

    if sc.r40_trend is not None and sc.r40_trend < cfg.deteriorating_r40_trend:
        reasons.append(f"R40 fell {abs(sc.r40_trend) * 100:.0f}pts YoY")

    if sc.tech is not None and prior is not None:
        if sc.tech.death_cross_recent and not prior.death_cross:
            reasons.append("new death cross")
        elif (
            sc.tech.trend_state == "downtrend"
            and prior.trend_state is not None
            and prior.trend_state != "downtrend"
        ):
            reasons.append("broke into downtrend")

    sig = sc.signals
    if sig is not None:
        net = net_revisions_30d(sig)
        cut = net is not None and net <= -cfg.revision_cut
        down_beats_up = (
            sig.eps_rev_down_30d is not None and sig.eps_rev_up_30d is not None
            and sig.eps_rev_down_30d > sig.eps_rev_up_30d
        )
        if cut or down_beats_up:
            reasons.append(
                f"estimates cut ({sig.eps_rev_down_30d} down vs {sig.eps_rev_up_30d} up, 30d)"
            )

        mom = short_change_mom(sig.shares_short, sig.shares_short_prior)
        if mom is not None and mom > SHORT_MOM_ALERT:
            reasons.append(f"short interest up {mom * 100:.0f}% MoM")
        elif (
            sig.short_pct_float is not None and prior is not None
            and prior.short_pct_float is not None and prior.short_pct_float > 0
            and sig.short_pct_float / prior.short_pct_float - 1 >= cfg.short_delta
        ):
            reasons.append(f"short float up to {sig.short_pct_float * 100:.1f}%")

    return reasons


def deterioration_rows(
    scorecards: list[Scorecard],
    prior: RunSnapshot | None,
    week_ago: RunSnapshot | None,
    cfg: ChangesCfg,
) -> list[DeteriorationRow]:
    """Confirmed multi-signal decay: a ticker is listed with >= cfg.min_signals
    negative signals, or with the plan-section-6 deteriorating() combination
    alone. Sorted most signals first, then weakest composite."""
    if week_ago is prior:
        # short history: one composite drop must not double-count as both the
        # 1-run and week-window signals and clear the gate by itself
        week_ago = None
    rows: list[DeteriorationRow] = []
    for sc in scorecards:
        prior_snap = prior.tickers.get(sc.ticker) if prior is not None else None
        week_snap = week_ago.tickers.get(sc.ticker) if week_ago is not None else None
        reasons = _negative_signals(sc, prior_snap, week_snap, cfg)
        plan6 = deteriorating(sc, cfg.deteriorating_r40_trend)
        if len(reasons) < cfg.min_signals and not plan6:
            continue
        if plan6:  # name the technical confirmation even without a baseline
            confirm = (
                "death cross" if sc.tech is not None and sc.tech.death_cross_recent
                else "below 50/200-day"
            )
            if not any(confirm in r or "downtrend" in r for r in reasons):
                reasons.append(confirm)
        delta_1run = (
            sc.composite - prior_snap.composite
            if sc.composite is not None and prior_snap is not None
            and prior_snap.composite is not None
            and _scorecard_basis_matches(sc, prior_snap) else None
        )
        delta_week = (
            sc.composite - week_snap.composite
            if sc.composite is not None and week_snap is not None
            and week_snap.composite is not None
            and _scorecard_basis_matches(sc, week_snap) else None
        )
        rows.append(DeteriorationRow(
            ticker=sc.ticker,
            composite=sc.composite,
            delta_1run=delta_1run,
            delta_week=delta_week,
            reasons=reasons,
        ))
    rows.sort(key=lambda r: (-len(r.reasons), r.composite if r.composite is not None else 0.0))
    return rows
