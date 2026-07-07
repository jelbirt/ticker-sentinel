"""Phase 3 feed ingestion — no network; feedparser fed raw XML strings directly
(feedparser parses string content without touching the network when it isn't a URL)."""
from __future__ import annotations

from sentinel.news.feeds import fetch_entries

RSS_OK = """<?xml version="1.0"?>
<rss version="2.0"><channel>
<title>Test Feed</title>
<item>
  <title>CRWD beats estimates</title>
  <link>https://example.com/1</link>
  <description>Strong quarter</description>
  <pubDate>Mon, 06 Jul 2026 12:00:00 GMT</pubDate>
</item>
<item>
  <title>Market roundup</title>
  <link>https://example.com/2</link>
  <description>DDOG and SNOW rallied</description>
</item>
</channel></rss>"""

RSS_BROKEN = "not xml at all, just garbage text"


def test_fetch_entries_parses_feed():
    entries, notes = fetch_entries([RSS_OK])
    assert notes == []
    assert len(entries) == 2
    assert entries[0].title == "CRWD beats estimates"
    assert entries[0].summary == "Strong quarter"
    assert entries[0].published is not None
    assert entries[1].published is None  # no pubDate on this entry


def test_fetch_entries_degrades_on_bad_feed():
    entries, notes = fetch_entries([RSS_BROKEN])
    assert entries == []
    assert len(notes) == 1
    assert "news feed unavailable" in notes[0]


def test_fetch_entries_one_bad_one_good():
    entries, notes = fetch_entries([RSS_BROKEN, RSS_OK])
    assert len(entries) == 2
    assert len(notes) == 1


def test_fetch_entries_empty_list():
    entries, notes = fetch_entries([])
    assert entries == []
    assert notes == []
