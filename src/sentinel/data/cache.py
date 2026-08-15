"""Parquet cache for normalized quarterly statements, with staleness rules.

Layout: data/cache/{TICKER}.parquet (canonical statements frame)
        data/cache/{TICKER}.meta.json (fetched_at, market_cap, shares_outstanding,
        annual_revenue, source)
Fresh fetches are merged into the cached frame so quarter history accumulates
beyond the ~5 quarters yfinance returns at any one time.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from sentinel.config import repo_root

log = logging.getLogger(__name__)

REFRESH_DAYS = 7  # fundamentals change quarterly; refresh at most weekly
MAX_QUARTERS = 16  # formulas need 12 (TTM windows at offsets 0..8); cap keeps parquet bounded

# Share counts are the one row a corporate action rebases retroactively: after
# a split, Yahoo restates every quarter it still serves (~5), while the older
# quarters that exist only in this cache keep the pre-split basis. Left alone,
# combine_first stitches the two together and dilution reads as a 300% share
# issuance. Carrying the observed factor back over the cached-only quarters is
# exactly the restatement the source already applied to the ones it serves.
REBASED_ROWS = ("diluted_shares",)
MIN_REBASE_OVERLAP = 2  # a single matching pair could be any restatement
REBASE_TOLERANCE = 0.02  # ratios this tight across the overlap mean one factor

# every file the cache writes per ticker; prune() recognizes tickers by these
_SUFFIXES = (".signals.json", ".meta.json", ".parquet")


def cache_dir() -> Path:
    return repo_root() / "data" / "cache"


def load(ticker: str) -> tuple[pd.DataFrame | None, dict[str, Any] | None]:
    pq = cache_dir() / f"{ticker}.parquet"
    meta_path = cache_dir() / f"{ticker}.meta.json"
    if not pq.exists() or not meta_path.exists():
        return None, None
    try:
        df = pd.read_parquet(pq)
        df.columns = pd.to_datetime(df.columns)
        meta = json.loads(meta_path.read_text())
        return df, meta
    except Exception:
        return None, None


def save(ticker: str, df: pd.DataFrame, meta: dict[str, Any]) -> None:
    d = cache_dir()
    d.mkdir(parents=True, exist_ok=True)
    out = df.copy()
    out.columns = [c.isoformat() for c in out.columns]  # parquet wants string columns
    out.to_parquet(d / f"{ticker}.parquet")
    (d / f"{ticker}.meta.json").write_text(json.dumps(meta, indent=2, default=str))


def is_fresh(meta: dict[str, Any] | None, max_age_days: int = REFRESH_DAYS) -> bool:
    if not meta or "fetched_at" not in meta:
        return False
    try:
        fetched = datetime.fromisoformat(meta["fetched_at"])
    except (TypeError, ValueError):
        return False
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - fetched < timedelta(days=max_age_days)


def _rebase_factor(cached: pd.Series, fresh: pd.Series) -> float | None:
    """The single factor the source restated the overlapping quarters by.

    None unless several overlapping quarters agree on one factor other than 1:
    a genuine split moves every restated quarter by the same ratio, while a
    revision to one quarter's figures moves them by different ones.
    """
    # a duplicated quarter label would make the lookups below return Series;
    # keep the first reading for it, as the alias layer does for repeated rows
    cached = cached[~cached.index.duplicated()]
    fresh = fresh[~fresh.index.duplicated()]
    ratios = []
    for col in cached.index.intersection(fresh.index):
        old, new = cached.get(col), fresh.get(col)
        if pd.notna(old) and pd.notna(new) and float(old) > 0:
            ratios.append(float(new) / float(old))
    if len(ratios) < MIN_REBASE_OVERLAP:
        return None
    ratios.sort()
    if ratios[-1] - ratios[0] > REBASE_TOLERANCE * ratios[0]:
        return None  # inconsistent: a restatement, not a rebasing
    factor = ratios[len(ratios) // 2]
    return factor if abs(factor - 1.0) > REBASE_TOLERANCE else None


def _rebase_share_history(cached: pd.DataFrame, fresh: pd.DataFrame) -> pd.DataFrame:
    """Cached share rows carried onto whatever basis the fresh fetch uses."""
    out = cached.copy()
    for field in REBASED_ROWS:
        if field not in out.index or field not in fresh.index:
            continue
        factor = _rebase_factor(out.loc[field], fresh.loc[field])
        if factor is not None:
            out.loc[field] = out.loc[field] * factor
            log.info(
                "cache: rebased cached %s history by %.4gx to match the "
                "refreshed share basis",
                field,
                factor,
            )
    return out


def merge_statements(cached: pd.DataFrame | None, fresh: pd.DataFrame) -> pd.DataFrame:
    """Union of quarter columns, fresh values winning, sorted newest-first.

    Cached share counts are rebased onto the fresh basis first, so a split does
    not leave the row half restated. Capped at MAX_QUARTERS so the committed
    parquet never grows unboundedly: quarters older than any formula can use
    are dropped.
    """
    if cached is None or cached.empty:
        merged = fresh
    else:
        merged = fresh.combine_first(_rebase_share_history(cached, fresh))
    return merged.sort_index(axis=1, ascending=False).iloc[:, :MAX_QUARTERS]


def prune(keep_tickers: set[str]) -> list[str]:
    """Delete cache files for tickers no longer in the watchlist.

    Keeps the committed cache self-cleaning: removing a ticker from
    watchlist.yaml removes its data on the next run instead of leaving
    orphaned files forever. Returns the removed filenames.
    """
    removed: list[str] = []
    d = cache_dir()
    if not d.exists():
        return removed
    for path in d.iterdir():
        if path.is_dir():
            continue
        for suffix in _SUFFIXES:
            if path.name.endswith(suffix):
                ticker = path.name.removesuffix(suffix)
                if ticker not in keep_tickers:
                    path.unlink()
                    removed.append(path.name)
                break  # non-cache files (.gitkeep etc.) never match a suffix
    return sorted(removed)
