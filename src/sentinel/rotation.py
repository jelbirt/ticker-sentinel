"""Watchlist rotation arithmetic shared by the weekly digest and the bench
screen (SPEC 7.0.1 level rule: amended round 14, growth preference from
round 15).

Pure functions over plain values: no I/O, so both callers and the tests feed
them numbers directly.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

GROWTH = "growth"
FALLBACK = "fallback"

# growth is a float division (1150 / 1000 - 1 is 0.1499999999999999), and
# "15% or more" has to include exactly 15%
FLOOR_TOLERANCE = 1e-9


def _known(value: float | None) -> bool:
    return value is not None and not math.isnan(value)


def growth_tier(growth: float | None, min_growth: float) -> str | None:
    """`growth` when yearly revenue growth meets the floor, `fallback` below
    it, None when growth is unknown. Unknown is not slow: it is reported as
    n/a and ranked with the fallback names, never with the growth names.
    NaN counts as unknown."""
    if not _known(growth):
        return None
    return GROWTH if growth >= min_growth - FLOOR_TOLERANCE else FALLBACK


def enters_above_tail(
    entrant_score: float,
    universe_scores: Mapping[str, float],
    outgoing: str | None,
    bottom_n: int,
) -> bool:
    """True when the entrant would rank above the bottom `bottom_n` by
    fundamental score in the universe AFTER `outgoing` is removed (SPEC
    condition 2, "on entry"): at least `bottom_n` remaining names score
    strictly below it. A tie is not "above"; an unknown (NaN) score never
    clears."""
    if not _known(entrant_score):
        return False
    below = sum(
        1 for ticker, score in universe_scores.items()
        if ticker != outgoing and score < entrant_score
    )
    return below >= max(bottom_n, 0)


@dataclass(frozen=True)
class Entrant:
    ticker: str
    score: float | None
    tier: str | None        # growth_tier()
    clears: bool | None     # enters_above_tail(); None when score is unknown


def pick_entrant(entrants: Sequence[Entrant]) -> Entrant | None:
    """The name the rule promotes: the highest-scoring growth-tier entrant
    that clears the tail, else the highest-scoring other entrant that clears
    it, else None (refresh the bench). Ties break on ticker so the pick is
    deterministic."""
    clearing = [e for e in entrants if e.clears and _known(e.score)]
    for pool in (
        [e for e in clearing if e.tier == GROWTH],
        [e for e in clearing if e.tier != GROWTH],
    ):
        if pool:
            return sorted(pool, key=lambda e: (-(e.score or 0.0), e.ticker))[0]
    return None
