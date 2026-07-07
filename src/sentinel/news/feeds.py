"""RSS ingestion — Phase 3 (PROJECT_PLAN.md section 11 news module).

Feed URLs are a config decision (§3), not hardcoded here — this module just
fetches and normalizes whatever list it's given. All I/O; degrades per-feed
so one dead RSS URL never blanks the section.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from tenacity import retry, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NewsEntry:
    title: str
    link: str
    source: str          # feed URL it came from
    summary: str = ""
    published: datetime | None = None


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, max=20), reraise=True)
def _parse_feed(url: str):
    import feedparser

    parsed = feedparser.parse(url)
    if parsed.bozo and not parsed.entries:
        raise RuntimeError(f"unparseable feed: {parsed.get('bozo_exception', 'unknown error')}")
    return parsed


def _entry_published(entry) -> datetime | None:
    parsed_time = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed_time:
        return None
    return datetime(*parsed_time[:6], tzinfo=timezone.utc)


def fetch_entries(feed_urls: list[str]) -> tuple[list[NewsEntry], list[str]]:
    """Fetch + normalize every feed. Returns (entries, degradation notes).

    A feed that fails to parse is skipped with a note; it never blocks the rest.
    """
    entries: list[NewsEntry] = []
    notes: list[str] = []
    for url in feed_urls:
        try:
            parsed = _parse_feed(url)
        except Exception as exc:
            log.warning("feed fetch failed for %s: %s", url, exc)
            notes.append(f"news feed unavailable: {url} ({exc})")
            continue
        for entry in parsed.entries:
            entries.append(
                NewsEntry(
                    title=entry.get("title", ""),
                    link=entry.get("link", ""),
                    source=url,
                    summary=entry.get("summary", ""),
                    published=_entry_published(entry),
                )
            )
    return entries, notes
