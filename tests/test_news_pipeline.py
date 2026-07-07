"""News digest pipeline — pure assembly logic, no network."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sentinel.news.feeds import NewsEntry
from sentinel.news.pipeline import build_digest

NOW = datetime(2026, 7, 6, 22, 0, tzinfo=timezone.utc)


def _entry(title, link="", hours_ago: float | None = 1.0, summary=""):
    return NewsEntry(
        title=title,
        link=link or f"https://example.com/{abs(hash(title))}",
        source="https://example.com/feed",
        summary=summary,
        published=None if hours_ago is None else NOW - timedelta(hours=hours_ago),
    )


TICKERS = {"CRWD": "CrowdStrike", "DDOG": "Datadog"}


class TestWindowing:
    def test_old_entries_dropped(self):
        digest = build_digest(
            [_entry("CRWD news", hours_ago=48)], {}, TICKERS, now=NOW, max_age_hours=36
        )
        assert digest.empty
        assert digest.scanned == 1

    def test_fresh_entries_kept(self):
        digest = build_digest([_entry("CRWD news", hours_ago=2)], {}, TICKERS, now=NOW)
        assert [tn.ticker for tn in digest.tickers] == ["CRWD"]

    def test_undated_entries_kept_but_ranked_last(self):
        digest = build_digest(
            [_entry("CRWD undated", hours_ago=None), _entry("CRWD dated", hours_ago=5)],
            {},
            TICKERS,
            now=NOW,
        )
        titles = [i.title for i in digest.tickers[0].items]
        assert titles == ["CRWD dated", "CRWD undated"]


class TestAttribution:
    def test_per_ticker_entries_skip_matching(self):
        # entry text never mentions the ticker — pre-attribution carries it anyway
        digest = build_digest(
            [], {"CRWD": [_entry("Analyst day recap")]}, TICKERS, now=NOW
        )
        assert digest.tickers[0].ticker == "CRWD"
        assert digest.matched == 1

    def test_generic_entry_can_match_multiple_tickers(self):
        digest = build_digest(
            [_entry("CRWD and DDOG both rallied")], {}, TICKERS, now=NOW
        )
        assert sorted(tn.ticker for tn in digest.tickers) == ["CRWD", "DDOG"]
        assert digest.matched == 2

    def test_dedupe_by_link_across_sources(self):
        dupe_link = "https://example.com/same-story"
        digest = build_digest(
            [_entry("CRWD beats", link=dupe_link)],
            {"CRWD": [_entry("CRWD beats", link=dupe_link)]},
            TICKERS,
            now=NOW,
        )
        assert len(digest.tickers[0].items) == 1


class TestCapAndOrder:
    def test_per_ticker_cap_newest_win(self):
        entries = [_entry(f"CRWD story {i}", hours_ago=i) for i in range(1, 6)]
        digest = build_digest(entries, {}, TICKERS, now=NOW, max_per_ticker=3)
        titles = [i.title for i in digest.tickers[0].items]
        assert titles == ["CRWD story 1", "CRWD story 2", "CRWD story 3"]
        assert digest.matched == 5  # matched counts pre-cap

    def test_empty_inputs(self):
        digest = build_digest([], {}, TICKERS, now=NOW)
        assert digest.empty
        assert digest.scanned == 0
