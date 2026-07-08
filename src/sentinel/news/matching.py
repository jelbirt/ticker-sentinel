"""Per-ticker news matching — pure logic, no I/O (Phase 3).

Matches by ticker symbol (word-boundary, case-SENSITIVE — prose words like
"net"/"team"/"snow" must never match NET/TEAM/SNOW) or company name
(case-insensitive; names are natural language) appearing in an entry's
title/summary.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sentinel.news.feeds import NewsEntry


@dataclass(frozen=True)
class TickerMatch:
    ticker: str
    entry: NewsEntry


def _ticker_pattern(ticker: str) -> re.Pattern:
    """Word-boundary AND case-sensitive: symbols are written uppercase, so this
    matches "NET jumped 9%" but not the English words in "net income", "the
    team", or "snow fell" — which otherwise misattribute constantly for
    watchlist symbols like NET/TEAM/SNOW on general business feeds."""
    return re.compile(rf"\b{re.escape(ticker)}\b")


def matches_ticker(entry: NewsEntry, ticker: str, company_name: str | None = None) -> bool:
    """True if `entry` mentions the ticker symbol or (optionally) the company name."""
    text = f"{entry.title} {entry.summary}"
    if _ticker_pattern(ticker).search(text):
        return True
    if company_name:
        # company names are natural-language phrases, not symbols: plain
        # case-insensitive substring is enough, no word-boundary needed
        return company_name.lower() in text.lower()
    return False


def match_entries(
    entries: list[NewsEntry], tickers: dict[str, str | None]
) -> list[TickerMatch]:
    """`tickers` maps ticker -> company name (or None). Returns every match found."""
    matches: list[TickerMatch] = []
    for entry in entries:
        for ticker, company_name in tickers.items():
            if matches_ticker(entry, ticker, company_name):
                matches.append(TickerMatch(ticker=ticker, entry=entry))
    return matches
