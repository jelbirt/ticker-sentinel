"""Parquet cache for normalized quarterly statements, with staleness rules.

Layout: data/cache/{TICKER}.parquet (canonical statements frame)
        data/cache/{TICKER}.meta.json (fetched_at, market_cap, annual_revenue, source)
Fresh fetches are merged into the cached frame so quarter history accumulates
beyond the ~5 quarters yfinance returns at any one time.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from sentinel.config import repo_root

REFRESH_DAYS = 7  # fundamentals change quarterly; refresh at most weekly


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


def merge_statements(cached: pd.DataFrame | None, fresh: pd.DataFrame) -> pd.DataFrame:
    """Union of quarter columns, fresh values winning, sorted newest-first."""
    if cached is None or cached.empty:
        merged = fresh
    else:
        merged = fresh.combine_first(cached)
    return merged.sort_index(axis=1, ascending=False)
