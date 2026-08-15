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


DETERMINISTIC_STYLES = sorted(set(STYLES) - {"llm-brief"})  # llm-brief: see test_news_llm.py


@pytest.mark.parametrize("style", DETERMINISTIC_STYLES)
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


@pytest.fixture()
def hostile_link_digest():
    """A feed can put anything in <link>: escaping alone still yields a live
    javascript: href."""
    return NewsDigest(
        as_of=NOW,
        tickers=[
            TickerNews(
                ticker="CRWD",
                company_name="CrowdStrike",
                items=[
                    NewsItem(
                        title="Hostile link story",
                        link="javascript:alert(1)",
                        source="https://feeds.example.com/top",
                        published=NOW - timedelta(hours=1),
                    ),
                    NewsItem(
                        title="Honest story",
                        link="https://news.example.com/ok",
                        source="https://feeds.example.com/top",
                        published=NOW - timedelta(hours=2),
                    ),
                ],
            )
        ],
        scanned=5,
        matched=2,
    )


@pytest.mark.parametrize("style", DETERMINISTIC_STYLES)
def test_non_http_links_render_as_plain_text(style, hostile_link_digest):
    html, notes = render_news(hostile_link_digest, style)
    assert notes == []
    assert "javascript:" not in html            # never reaches an href
    assert "Hostile link story" in html         # visible text is unchanged
    assert "Hostile link story</a>" not in html  # but nothing clickable behind it


def test_https_links_still_render_as_links(hostile_link_digest):
    html, _ = render_news(hostile_link_digest, "headlines")
    assert '<a href="https://news.example.com/ok"' in html


def _one_item_digest(link: str) -> NewsDigest:
    return NewsDigest(
        as_of=NOW,
        tickers=[
            TickerNews(
                ticker="CRWD",
                company_name="CrowdStrike",
                items=[
                    NewsItem(
                        title="Hostile link story",
                        link=link,
                        source="https://feeds.example.com/top",
                        published=NOW - timedelta(hours=1),
                    )
                ],
            )
        ],
        scanned=1,
        matched=1,
    )


# An allowlist is only as good as the shapes it was tested against: a naive
# rewrite (`url.lower().startswith("http")`) would pass a javascript-only test
# while admitting "httpx://" and "http-evil:".
@pytest.mark.parametrize(
    "link",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",          # scheme comparison is case-insensitive
        " javascript:alert(1)",         # leading whitespace is stripped by urlparse
        "java\tscript:alert(1)",        # embedded control character
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox(1)",
        "httpx://evil.example.com/x",   # prefix of an allowed scheme, not the scheme
        "http-evil:payload",
        "//evil.example.com/x",         # protocol-relative, no scheme at all
        "http://[::1",                  # malformed: urlparse raises, must degrade
    ],
)
@pytest.mark.parametrize("style", DETERMINISTIC_STYLES)
def test_only_http_schemes_reach_an_href(style, link):
    html, notes = render_news(_one_item_digest(link), style)
    assert notes == []
    assert "<a " not in html                # the only item here is the hostile one
    assert "Hostile link story" in html     # text still shown, just not clickable


@pytest.mark.parametrize("style", DETERMINISTIC_STYLES)
def test_uppercase_http_scheme_still_links(style):
    html, _ = render_news(_one_item_digest("HTTPS://news.example.com/ok"), style)
    assert '<a href="HTTPS://news.example.com/ok"' in html


def test_llm_brief_sources_footer_drops_non_http_links(hostile_link_digest, monkeypatch):
    from sentinel.news import llm

    monkeypatch.setattr(
        llm, "call_claude", lambda *a, **k: "<REPORT>CRWD had a busy day.</REPORT>"
    )
    html, notes = render_news(hostile_link_digest, "llm-brief", model="claude-sonnet-5")
    assert notes == []
    assert "javascript:" not in html
    assert "[1]" in html                                    # the marker still shows
    assert '<a href="https://news.example.com/ok"' in html  # the safe one still links
