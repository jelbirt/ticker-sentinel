"""One-time history backfill: python -m sentinel.backfill [--dry-run | --apply] [--tickers ...]

The committed cache holds roughly 6 to 7 quarters per ticker, but growth needs
8 and r40_trend needs 12. This tool deepens data/cache/*.parquet from SEC
EDGAR, and refuses to write anything it cannot first reconcile against the
yfinance data already cached.

It is a tool, not a scheduled job: both modes fetch EDGAR live, and nothing
here is imported by sentinel.run. Steady-state fundamentals stay on yfinance.
Spec and decisions: tasks/spec-history-backfill.md.

Verification gate: for every quarter present on both sides, every backfilled
field where both sides have a value must agree within 1 percent relative OR
100,000 absolute (the floor keeps near-zero values from failing on rounding).
One mismatch rejects the whole ticker. No partial trust.

Amendment 1 (approved 2026-08-15) narrows the backfilled set to the fields the
historical quarters actually feed, derives capex as a composite of its base and
software-capitalization tags, and lets a verified EDGAR frame fill EMPTY cells
inside the cached range (never overwrite a cached value).
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from sentinel.config import load_config
from sentinel.data import cache
from sentinel.data.edgar import BACKFILL_FIELDS, canonical_from_companyfacts

log = logging.getLogger("sentinel.backfill")

# D2: a match is within 1 percent of the cached value, OR within this many
# dollars/shares absolutely. Near-zero quarters (an operating income crossing
# through zero) would otherwise fail on rounding alone.
MATCH_REL_TOLERANCE = 0.01
MATCH_ABS_FLOOR = 100_000.0

TARGET_QUARTERS = 12  # what r40_trend needs; MAX_QUARTERS (16) is the cap

# Fiscal period ends can differ by a few days between yfinance and the filing
# (52/53-week calendars). Quarter ends this close are the same quarter.
COLUMN_ALIGN_DAYS = 7

# Documented a-priori skips: no 10-Q XBRL quarterly facts exist to derive from.
SKIPPED: dict[str, str] = {
    "MNDY": "foreign private issuer, files 20-F; no 10-Q XBRL quarterly facts",
}

ACCEPT, REJECT, SKIP, ERROR = "ACCEPT", "REJECT", "SKIP", "ERROR"


@dataclass(frozen=True)
class Comparison:
    """One overlap check: cached yfinance value vs derived EDGAR value."""

    field: str
    quarter: pd.Timestamp
    cached: float
    edgar: float
    ok: bool

    @property
    def rel_diff(self) -> float | None:
        if self.cached == 0:
            return None
        return abs(self.cached - self.edgar) / abs(self.cached)


@dataclass
class BackfillResult:
    ticker: str
    status: str
    reason: str | None = None
    comparisons: list[Comparison] = field(default_factory=list)
    quarters_before: int = 0
    quarters_after: int = 0
    cells_filled: int = 0  # empty cells inside the cached range filled from EDGAR
    seeded: bool = False  # bench name whose yfinance overlap had to be fetched

    @property
    def gained(self) -> int:
        return self.quarters_after - self.quarters_before

    @property
    def mismatches(self) -> list[Comparison]:
        return [c for c in self.comparisons if not c.ok]


# --- pure verification / merge logic ---------------------------------------


def values_match(
    cached: float,
    edgar: float,
    rel_tolerance: float = MATCH_REL_TOLERANCE,
    abs_floor: float = MATCH_ABS_FLOOR,
) -> bool:
    """D2 tolerance: within `rel_tolerance` of the cached value, or within `abs_floor`.

    The cached (yfinance) value is the denominator: it is the reference the
    backfill has to reconcile against, so the test is deterministic regardless
    of which side is larger.
    """
    diff = abs(cached - edgar)
    if diff <= abs_floor:
        return True
    if cached == 0:
        return False
    return diff / abs(cached) <= rel_tolerance


def align_columns(
    edgar: pd.DataFrame, cache_columns: pd.Index, tol_days: int = COLUMN_ALIGN_DAYS
) -> pd.DataFrame:
    """Relabel EDGAR quarter ends onto near-identical cached quarter ends.

    A 52/53-week filer can report 2026-05-02 where yfinance carries
    2026-04-30. Without this the two frames would never overlap (nothing to
    verify) and the merge would emit two columns for one quarter.
    """
    if edgar.shape[1] == 0 or len(cache_columns) == 0:
        return edgar
    mapping: dict[pd.Timestamp, pd.Timestamp] = {}
    for col in edgar.columns:
        near = [c for c in cache_columns if abs((c - col).days) <= tol_days]
        if near:
            mapping[col] = min(near, key=lambda c: abs((c - col).days))
    if not mapping:
        return edgar
    out = edgar.rename(columns=mapping)
    out = out.loc[:, ~out.columns.duplicated()]  # newest wins on a collision
    return out.sort_index(axis=1, ascending=False)


def compare(cached: pd.DataFrame, edgar: pd.DataFrame) -> list[Comparison]:
    """Every overlap quarter x backfilled field where BOTH sides carry a value.

    A NaN on either side is not a mismatch: it is simply nothing to check.
    The gate covers BACKFILL_FIELDS only, so the fields EDGAR never derives
    (ebitda, operating_income, d_and_a, total_debt, cash) are not compared and
    cannot reject a ticker.
    """
    overlap = sorted(set(cached.columns) & set(edgar.columns), reverse=True)
    out: list[Comparison] = []
    for col in overlap:
        for name in BACKFILL_FIELDS:
            if name not in cached.index or name not in edgar.index:
                continue
            left, right = cached.loc[name, col], edgar.loc[name, col]
            if pd.isna(left) or pd.isna(right):
                continue
            left, right = float(left), float(right)
            out.append(Comparison(name, col, left, right, values_match(left, right)))
    return out


def fill_holes(cached: pd.DataFrame, edgar: pd.DataFrame) -> pd.DataFrame:
    """Cached frame with its EMPTY cells filled from the aligned EDGAR frame.

    Amendment 1 (change 3). yfinance leaves hollow columns inside the cached
    range: a quarter end with no core field at all, published piecemeal and
    never completed. Every ticker accepted by the live apply run carried one or
    two, and they cost more than they look: `build_ttm` returns None for any
    window containing them, so growth and r40_trend stayed n/a even at 16
    quarters of depth.

    Only genuinely empty cells are filled. A cached non-NaN value always wins
    (`combine_first` semantics), so the acceptance decision, which rests solely
    on the overlap checks of non-NaN cached cells, is never undermined by what
    gets written. Columns the cache does not already have are left to
    `merge_backfill`.
    """
    in_range = [c for c in edgar.columns if c in set(cached.columns)]
    if not in_range:
        return cached
    filled = cached.combine_first(edgar.loc[:, in_range])
    # combine_first sorts the union; restore the cache's own row/column order
    return filled.reindex(index=cached.index, columns=cached.columns)


def merge_backfill(cached: pd.DataFrame, edgar: pd.DataFrame) -> pd.DataFrame:
    """Cached frame, holes filled, plus the EDGAR quarters that predate it.

    No cached VALUE is overwritten; verified EDGAR values may fill empty
    cells (see `fill_holes`). Quarters strictly older than the cache are
    appended through cache.merge_statements, which does the union with the
    cached frame as the winning side and applies the MAX_QUARTERS cap.
    """
    if cached.shape[1] == 0:
        return cached
    filled = fill_holes(cached, edgar)
    oldest = min(cached.columns)
    older_cols = [c for c in edgar.columns if c < oldest]
    if not older_cols:
        return cache.merge_statements(None, filled)
    return cache.merge_statements(edgar.loc[:, older_cols], filled)


def count_filled_cells(cached: pd.DataFrame, merged: pd.DataFrame) -> int:
    """How many empty cells inside the cached range the merge filled.

    Quarters gained is the wrong measure of what change 3 buys: a hollow column
    already counted toward depth while blocking every TTM window that spanned
    it. This counts the cells that were NaN in the cache and carry a value
    after the merge, so the report can show the hole filling separately.
    """
    if cached.shape[1] == 0:
        return 0
    after = merged.reindex(index=cached.index, columns=cached.columns)
    return int((cached.isna() & after.notna()).to_numpy().sum())


# --- per-ticker orchestration ----------------------------------------------


def backfill_ticker(
    ticker: str,
    fetch_facts: Callable[[str], dict[str, Any]],
    fetch_yfinance: Callable[[str], tuple[pd.DataFrame, dict[str, Any]]],
    apply: bool = False,
) -> BackfillResult:
    """Verify one ticker against its cache and, with `apply`, rewrite its parquet."""
    if ticker in SKIPPED:
        return BackfillResult(ticker, SKIP, reason=SKIPPED[ticker])

    cached_df, meta = cache.load(ticker)
    seeded = cached_df is None
    if seeded:
        # D3 rider 2: a bench name has no cache to verify against, so fetch its
        # current statements to create the overlap. In a dry run this stays in
        # memory; only --apply seeds the cache from it.
        try:
            cached_df, meta = fetch_yfinance(ticker)
        except Exception as exc:  # noqa: BLE001 - report and continue the sweep
            return BackfillResult(ticker, ERROR, reason=f"yfinance fetch failed ({exc})")
    if cached_df is None or cached_df.shape[1] == 0:
        return BackfillResult(ticker, ERROR, reason="no yfinance statements to verify against")

    before = int(cached_df.shape[1])
    try:
        edgar = canonical_from_companyfacts(fetch_facts(ticker))
    except Exception as exc:  # noqa: BLE001 - report and continue the sweep
        return BackfillResult(
            ticker, ERROR, reason=f"EDGAR fetch failed ({exc})",
            quarters_before=before, quarters_after=before, seeded=seeded,
        )
    if edgar.shape[1] == 0:
        return BackfillResult(
            ticker, REJECT, reason="no quarterly facts derivable from EDGAR",
            quarters_before=before, quarters_after=before, seeded=seeded,
        )

    edgar = align_columns(edgar, cached_df.columns)
    comparisons = compare(cached_df, edgar)
    result = BackfillResult(
        ticker, REJECT, comparisons=comparisons,
        quarters_before=before, quarters_after=before, seeded=seeded,
    )
    if not comparisons:
        result.reason = "no overlapping quarters to verify against"
        return result
    if result.mismatches:
        result.reason = (
            f"{len(result.mismatches)} of {len(comparisons)} overlap checks failed"
        )
        return result

    merged = merge_backfill(cached_df, edgar)
    result.status = ACCEPT
    result.quarters_after = int(merged.shape[1])
    result.cells_filled = count_filled_cells(cached_df, merged)
    if apply:
        cache.save(ticker, merged, meta or {})
    return result


def _default_fetchers() -> tuple[
    Callable[[str], dict[str, Any]], Callable[[str], tuple[pd.DataFrame, dict[str, Any]]]
]:
    """Live SEC + yfinance fetchers, sharing one throttled session and CIK map."""
    from sentinel.data import edgar as edgar_api
    from sentinel.data.fundamentals import fetch_statements

    session = edgar_api.make_session()
    cik_map = edgar_api.fetch_cik_map(session)

    def fetch_facts(ticker: str) -> dict[str, Any]:
        cik = cik_map.get(ticker.upper())
        if cik is None:
            raise LookupError(f"no CIK on file for {ticker}")
        return edgar_api.fetch_companyfacts(cik, session)

    return fetch_facts, fetch_statements


def run_backfill(
    tickers: list[str],
    apply: bool = False,
    fetch_facts: Callable[[str], dict[str, Any]] | None = None,
    fetch_yfinance: Callable[[str], tuple[pd.DataFrame, dict[str, Any]]] | None = None,
) -> list[BackfillResult]:
    if fetch_facts is None or fetch_yfinance is None:
        live_facts, live_yf = _default_fetchers()
        fetch_facts = fetch_facts or live_facts
        fetch_yfinance = fetch_yfinance or live_yf
    results = []
    for ticker in tickers:
        log.info("backfill: %s", ticker)
        results.append(backfill_ticker(ticker, fetch_facts, fetch_yfinance, apply=apply))
    return results


# --- report -----------------------------------------------------------------


def _num(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "n/a"
    return f"{value:,.0f}"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.2f}%"


def format_report(results: list[BackfillResult], apply: bool = False) -> str:
    mode = "apply: accepted tickers rewritten" if apply else "dry run: nothing written"
    lines = [
        f"Ticker Sentinel history backfill ({mode})",
        f"Match tolerance: within {100 * MATCH_REL_TOLERANCE:.0f} percent of the cached "
        f"value, or within {MATCH_ABS_FLOOR:,.0f} absolute",
        f"Target depth: {TARGET_QUARTERS} quarters (cache cap {cache.MAX_QUARTERS})",
        "",
    ]
    for r in results:
        if r.status == SKIP:
            lines.append(f"{r.ticker:<6} SKIP    {r.reason}")
            lines.append("")
            continue
        if r.status == ERROR:
            lines.append(f"{r.ticker:<6} ERROR   {r.reason}")
            lines.append("")
            continue
        seeded = " (bench, yfinance overlap fetched live)" if r.seeded else ""
        if r.status == ACCEPT:
            head = (
                f"{r.ticker:<6} ACCEPT  {r.quarters_before} -> {r.quarters_after} "
                f"quarters (+{r.gained}), {r.cells_filled} empty cells filled{seeded}"
            )
        else:
            head = (
                f"{r.ticker:<6} REJECT  {r.quarters_before} quarters kept, no change"
                f"{seeded}"
            )
        lines.append(head)
        if r.reason:
            lines.append(f"       reason: {r.reason}")
        matched = len(r.comparisons) - len(r.mismatches)
        lines.append(
            f"       overlap checks: {len(r.comparisons)} compared, {matched} matched, "
            f"{len(r.mismatches)} failed"
        )
        for c in r.comparisons:
            tag = "ok  " if c.ok else "FAIL"
            lines.append(
                f"       {tag} {c.quarter.date().isoformat()} {c.field:<17}"
                f"{_num(c.cached):>18}{_num(c.edgar):>18}  {_pct(c.rel_diff):>8}"
            )
        lines.append("")

    counts = {status: sum(1 for r in results if r.status == status) for status in
              (ACCEPT, REJECT, SKIP, ERROR)}
    gained = sum(r.gained for r in results if r.status == ACCEPT)
    filled = sum(r.cells_filled for r in results if r.status == ACCEPT)
    short = [
        r.ticker for r in results
        if r.status == ACCEPT and r.quarters_after < TARGET_QUARTERS
    ]
    lines.append(
        f"Summary: {counts[ACCEPT]} accepted, {counts[REJECT]} rejected, "
        f"{counts[SKIP]} skipped, {counts[ERROR]} errored; "
        f"{gained} quarters gained across the universe, "
        f"{filled} empty cells filled inside the cached range"
    )
    lines.append(
        "Below target depth after accept: "
        + (", ".join(short) if short else "none")
    )
    if not apply:
        lines.append("Dry run: no cache file was written or modified.")
    return "\n".join(lines)


# --- CLI ---------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m sentinel.backfill",
        description=(
            "One-time SEC EDGAR backfill of the committed quarterly cache. "
            "Both modes fetch EDGAR live; only --apply writes."
        ),
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true",
        help="verify and report only, writing nothing (the default)",
    )
    mode.add_argument(
        "--apply", action="store_true",
        help="rewrite the parquet of every ticker that passes verification",
    )
    p.add_argument(
        "--tickers",
        help="comma-separated override (default: watchlist r40 tickers plus bench)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = parse_args(argv)
    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        cfg = load_config()
        tickers = cfg.r40_tickers + [t for t in cfg.bench if t not in cfg.r40_tickers]
    results = run_backfill(tickers, apply=args.apply)
    print(format_report(results, apply=args.apply))
    return 1 if any(r.status == ERROR for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
