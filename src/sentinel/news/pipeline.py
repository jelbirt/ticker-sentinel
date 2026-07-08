"""News digest pipeline — the DATA side of Phase 3.

Fetches, windows, attributes, dedupes, ranks and caps news into a neutral,
style-agnostic NewsDigest. Presentation lives entirely in styles.py; the same
digest can be rendered any number of ways. The pipeline owns PRIMARY selection
(recency window, dedupe, per-ticker cap from config) — a style may trim
further, but never expands or fetches.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sentinel.news.feeds import NewsEntry, fetch_entries
from sentinel.news.matching import matches_ticker

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NewsItem:
    title: str
    link: str
    source: str                     # feed URL the entry came from
    published: datetime | None = None
    summary: str = ""


@dataclass
class TickerNews:
    ticker: str
    company_name: str | None
    items: list[NewsItem]


@dataclass
class NewsDigest:
    """The stable contract between pipeline and styles."""
    as_of: datetime
    tickers: list[TickerNews]       # only tickers that matched something
    scanned: int                    # entries seen across all feeds
    matched: int                    # UNIQUE entries attributed to ≥1 ticker (pre-cap)
    max_age_hours: int = 36

    @property
    def empty(self) -> bool:
        return not self.tickers


def _within_window(entry: NewsEntry, now: datetime, max_age_hours: int) -> bool:
    # undated entries are kept (can't age them; dropping silently loses news)
    if entry.published is None:
        return True
    return now - entry.published <= timedelta(hours=max_age_hours)


def _dedupe(entries: list[NewsEntry]) -> list[NewsEntry]:
    seen: set[str] = set()
    out: list[NewsEntry] = []
    for e in entries:
        key = e.link or e.title
        if key and key not in seen:
            seen.add(key)
            out.append(e)
    return out


def _to_item(e: NewsEntry) -> NewsItem:
    return NewsItem(
        title=e.title, link=e.link, source=e.source, published=e.published, summary=e.summary
    )


def build_digest(
    generic_entries: list[NewsEntry],
    per_ticker_entries: dict[str, list[NewsEntry]],
    tickers: dict[str, str | None],
    now: datetime | None = None,
    max_age_hours: int = 36,
    max_per_ticker: int = 3,
) -> NewsDigest:
    """Pure assembly: window -> attribute -> dedupe -> rank -> cap.

    `generic_entries` get text-matched against every ticker; `per_ticker_entries`
    are pre-attributed (e.g. from a per-ticker feed URL) and skip matching.
    """
    now = now or datetime.now(timezone.utc)
    scanned = len(generic_entries) + sum(len(v) for v in per_ticker_entries.values())

    generic = [e for e in generic_entries if _within_window(e, now, max_age_hours)]
    ticker_news: list[TickerNews] = []
    matched_keys: set[str] = set()  # unique entries — one story hitting 2 tickers counts once
    for ticker, company_name in tickers.items():
        candidates = [
            e
            for e in per_ticker_entries.get(ticker, [])
            if _within_window(e, now, max_age_hours)
        ]
        candidates += [e for e in generic if matches_ticker(e, ticker, company_name)]
        candidates = _dedupe(candidates)
        matched_keys.update(e.link or e.title for e in candidates)
        if not candidates:
            continue
        # newest first; undated entries rank last
        candidates.sort(
            key=lambda e: e.published or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        ticker_news.append(
            TickerNews(
                ticker=ticker,
                company_name=company_name,
                items=[_to_item(e) for e in candidates[:max_per_ticker]],
            )
        )
    return NewsDigest(
        as_of=now,
        tickers=ticker_news,
        scanned=scanned,
        matched=len(matched_keys),
        max_age_hours=max_age_hours,
    )


def collect_news(
    feed_urls: list[str],
    per_ticker_template: str | None,
    tickers: list[str],
) -> tuple[list[NewsEntry], dict[str, list[NewsEntry]], list[str]]:
    """I/O side: fetch generic feeds + expand the per-ticker feed template.

    Returns (generic entries, entries pre-attributed by ticker, degradation notes).
    """
    generic, notes = fetch_entries(feed_urls)
    per_ticker: dict[str, list[NewsEntry]] = {}
    if per_ticker_template:
        for ticker in tickers:
            url = per_ticker_template.format(ticker=ticker)
            entries, t_notes = fetch_entries([url])
            notes.extend(t_notes)
            if entries:
                per_ticker[ticker] = entries
    return generic, per_ticker, notes
