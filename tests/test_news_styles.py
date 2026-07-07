"""News styles — swappable rendering of one digest; Gmail-safe, fully escaped."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sentinel.news.pipeline import NewsDigest, NewsItem, TickerNews
from sentinel.news.styles import STYLES, render_news

NOW = datetime(2026, 7, 6, 22, 0, tzinfo=timezone.utc)


@pytest.fixture()
def digest():
    return NewsDigest(
        as_of=NOW,
        tickers=[
            TickerNews(
                ticker="CRWD",
                company_name="CrowdStrike",
                items=[
                    NewsItem(
                        title="CRWD beats <estimates> & raises",
                        link="https://news.example.com/crwd?a=1&b=2",
                        source="https://feeds.example.com/top",
                        published=NOW - timedelta(hours=2),
                    ),
                    NewsItem(
                        title="Second story",
                        link="https://news.example.com/crwd2",
                        source="https://feeds.example.com/top",
                        published=NOW - timedelta(hours=30),
                    ),
                ],
            )
        ],
        scanned=10,
        matched=2,
    )


@pytest.mark.parametrize("style", sorted(STYLES))
def test_every_style_renders_gmail_safe_escaped_html(style, digest):
    html, notes = render_news(digest, style)
    assert notes == []
    assert "CRWD" in html
    assert "<style" not in html                          # inline CSS only
    assert "CRWD beats &lt;estimates&gt; &amp; raises" in html  # text escaped
    assert 'href="https://news.example.com/crwd?a=1&amp;b=2"' in html
    assert "2h ago" in html
    assert "feeds.example.com" in html                   # source domain shown


def test_headlines_shows_all_items_brief_trims_to_top(digest):
    headlines, _ = render_news(digest, "headlines")
    brief, _ = render_news(digest, "brief")
    assert "Second story" in headlines
    assert "Second story" not in brief                   # style may trim (never expand)
    assert "+1 more" in brief


def test_unknown_style_falls_back_with_note(digest):
    html, notes = render_news(digest, "nonexistent")
    assert html is not None
    assert len(notes) == 1
    assert "unknown news style" in notes[0]


def test_empty_digest_renders_nothing(digest):
    empty = NewsDigest(as_of=NOW, tickers=[], scanned=5, matched=0)
    html, notes = render_news(empty, "headlines")
    assert html is None
    assert notes == []
