"""Between-quarter signal I/O — Phase 2.5.

Primary source is yfinance for all four signals (estimate revisions, recommendation
trends, short interest, insider activity). Finnhub's free insider-transactions
endpoint is an optional fallback (FINNHUB_API_KEY) hedging yfinance scraping
breakage. Every part degrades independently; last-good snapshots are cached as
JSON so a bad fetch day never blanks the section.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pandas as pd
import requests

from sentinel.data.cache import cache_dir
from sentinel.indicators.signals import SignalSnapshot

log = logging.getLogger(__name__)

FINNHUB_INSIDER_URL = "https://finnhub.io/api/v1/stock/insider-transactions"


def _int_or_none(val: Any) -> int | None:
    try:
        return None if val is None or pd.isna(val) else int(val)
    except (TypeError, ValueError):
        return None


def _float_or_none(val: Any) -> float | None:
    try:
        return None if val is None or pd.isna(val) else float(val)
    except (TypeError, ValueError):
        return None


def parse_eps_revisions(df: pd.DataFrame | None) -> dict[str, int | None]:
    """yfinance eps_revisions frame -> current-quarter (0q) analyst revision counts."""
    out: dict[str, int | None] = {}
    if df is None or df.empty or "0q" not in df.index:
        return out
    row = df.loc["0q"]
    # note yfinance's inconsistent casing on the last one
    for field, cols in {
        "eps_rev_up_7d": ["upLast7days"],
        "eps_rev_up_30d": ["upLast30days"],
        "eps_rev_down_30d": ["downLast30days"],
        "eps_rev_down_7d": ["downLast7Days", "downLast7days"],
    }.items():
        for col in cols:
            if col in row.index:
                out[field] = _int_or_none(row[col])
                break
    return out


def parse_recommendations(df: pd.DataFrame | None) -> dict[str, int | None]:
    """yfinance recommendations frame -> bullish/neutral/bearish counts, now and −1m."""
    out: dict[str, int | None] = {}
    if df is None or df.empty or "period" not in df.columns:
        return out
    frame = df.set_index("period")

    def counts(period: str) -> tuple[int, int, int] | None:
        if period not in frame.index:
            return None
        row = frame.loc[period]
        try:
            return (
                int(row["strongBuy"]) + int(row["buy"]),
                int(row["hold"]),
                int(row["sell"]) + int(row["strongSell"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

    now, prior = counts("0m"), counts("-1m")
    if now:
        out["rec_bullish"], out["rec_neutral"], out["rec_bearish"] = now
    if prior:
        out["rec_bullish_prior"], _, out["rec_bearish_prior"] = prior
    return out


def parse_insider_purchases(df: pd.DataFrame | None) -> dict[str, Any]:
    """yfinance insider_purchases (6-month aggregate) -> net shares + txn counts."""
    out: dict[str, Any] = {}
    if df is None or df.empty:
        return out
    label_col = df.columns[0]  # e.g. "Insider Purchases Last 6m"
    frame = df.set_index(label_col)
    rows = {
        "Purchases": ("insider_buy_txns_6m", "Trans"),
        "Sales": ("insider_sell_txns_6m", "Trans"),
        "Net Shares Purchased (Sold)": ("insider_net_shares_6m", "Shares"),
    }
    for row_label, (field, col) in rows.items():
        if row_label in frame.index and col in frame.columns:
            val = frame.loc[row_label, col]
            out[field] = (
                _float_or_none(val) if field == "insider_net_shares_6m" else _int_or_none(val)
            )
    return out


def parse_short_info(info: dict | None) -> dict[str, Any]:
    """yfinance Ticker.info -> short-interest fields (FINRA cycle via Yahoo)."""
    out: dict[str, Any] = {}
    if not info:
        return out
    out["shares_short"] = _float_or_none(info.get("sharesShort"))
    out["shares_short_prior"] = _float_or_none(info.get("sharesShortPriorMonth"))
    out["short_pct_float"] = _float_or_none(info.get("shortPercentOfFloat"))
    epoch = info.get("dateShortInterest")
    if epoch:
        try:
            out["short_interest_date"] = datetime.fromtimestamp(
                int(epoch), tz=timezone.utc
            ).date()
        except (TypeError, ValueError, OSError):
            pass
    return out


def finnhub_insider(ticker: str, api_key: str, months: int = 6) -> dict[str, Any]:
    """Fallback: Finnhub free insider-transactions endpoint -> same aggregate shape."""
    since = (date.today() - timedelta(days=months * 30)).isoformat()
    try:
        resp = requests.get(
            FINNHUB_INSIDER_URL,
            params={"symbol": ticker, "from": since, "to": date.today().isoformat(),
                    "token": api_key},
            timeout=30,
        )
        data = (resp.json() or {}).get("data") or []
    except Exception as exc:
        log.warning("finnhub insider fetch failed for %s: %s", ticker, exc)
        return {}
    if not data:
        return {}
    buys = [t for t in data if (t.get("change") or 0) > 0]
    sells = [t for t in data if (t.get("change") or 0) < 0]
    return {
        "insider_net_shares_6m": float(sum(t.get("change") or 0 for t in data)),
        "insider_buy_txns_6m": len(buys),
        "insider_sell_txns_6m": len(sells),
    }


def _cache_path(ticker: str):
    return cache_dir() / f"{ticker}.signals.json"


def _save_snapshot(snap: SignalSnapshot) -> None:
    try:
        path = _cache_path(snap.ticker)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(snap), default=str))
    except Exception:
        pass


def _load_snapshot(ticker: str) -> SignalSnapshot | None:
    try:
        raw = json.loads(_cache_path(ticker).read_text())
        if raw.get("short_interest_date"):
            raw["short_interest_date"] = date.fromisoformat(raw["short_interest_date"])
        return SignalSnapshot(**raw)
    except Exception:
        return None


def fetch_signals(ticker: str) -> tuple[SignalSnapshot | None, list[str]]:
    """Fresh between-quarter signals; parts degrade independently, whole falls back to cache."""
    import yfinance as yf

    notes: list[str] = []
    fields: dict[str, Any] = {}
    sources: list[str] = []
    t = yf.Ticker(ticker)

    parts = {
        "estimate revisions": lambda: parse_eps_revisions(t.eps_revisions),
        "recommendations": lambda: parse_recommendations(t.recommendations),
        "insider activity": lambda: parse_insider_purchases(t.insider_purchases),
        "short interest": lambda: parse_short_info(t.info),
    }
    failed: list[str] = []
    for name, getter in parts.items():
        try:
            parsed = getter()
        except Exception as exc:
            log.warning("%s: %s fetch failed: %s", ticker, name, exc)
            parsed = {}
        if parsed:
            fields.update(parsed)
            sources.append(f"yfinance:{name}")
        else:
            failed.append(name)

    # Finnhub fallback covers the insider slice only
    if "insider activity" in failed:
        api_key = os.environ.get("FINNHUB_API_KEY")
        if api_key:
            parsed = finnhub_insider(ticker, api_key)
            if parsed:
                fields.update(parsed)
                sources.append("finnhub:insider activity")
                failed.remove("insider activity")

    if not fields:
        cached = _load_snapshot(ticker)
        if cached is not None:
            notes.append(f"{ticker}: signals fetch failed, using last cached snapshot")
            return cached, notes
        notes.append(f"{ticker}: between-quarter signals unavailable")
        return None, notes

    if failed:
        notes.append(f"{ticker}: signals partial ({', '.join(failed)} unavailable)")
    snap = SignalSnapshot(ticker=ticker, sources=sources, **fields)
    _save_snapshot(snap)
    return snap, notes
