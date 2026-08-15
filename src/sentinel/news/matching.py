"""Per-ticker news matching — pure logic, no I/O (Phase 3).

Matches by ticker symbol (case-SENSITIVE — prose words like "net"/"team"/"snow"
must never match NET/TEAM/SNOW; symbols of 1-2 chars additionally require
explicit symbol context, see ticker_pattern) or company name (case-insensitive;
names are natural language) appearing in an entry's title/summary.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sentinel.news.feeds import NewsEntry


@dataclass(frozen=True)
class TickerMatch:
    ticker: str
    entry: NewsEntry


SHORT_TICKER_MAX_LEN = 2

# Exchange-prefixed symbol forms seen on the wires: "NYSE: S", "NASDAQ:S",
# "NYSE American: S" (AMEX's name since 2017; both spellings still circulate).
_EXCHANGES = r"NYSE(?: American)?|NASDAQ|Nasdaq|AMEX"


def ticker_pattern(ticker: str) -> re.Pattern:
    """Symbol-only matching, case-SENSITIVE (never matches lowercase prose).

    Two regimes, because a bare word boundary is only safe once a symbol is
    long enough to be improbable as an abbreviation:

    - 3+ chars: word-boundary + case-sensitive. Matches "NET jumped 9%" but not
      the English words in "net income", "the team", or "snow fell", which
      otherwise misattribute constantly for symbols like NET/TEAM/SNOW.
    - 1-2 chars: the word boundary collapses. `\\bS\\b` matches the S inside
      "U.S.", "S&P 500", and "S. Korea", so on general business feeds EVERY
      macro headline was attributed to SentinelOne (S), filling its item cap
      and poisoning the LLM prompt. Short symbols therefore require explicit
      symbol context: a cashtag ($S), a parenthesized symbol "(S)", or an
      exchange prefix (NYSE: S).

    Deliberate tradeoff: a bare "DT rallies" headline on a general feed no
    longer matches DT. What still carries these tickers is the PER-TICKER feed,
    whose entries are pre-attributed and skip matching entirely (see
    news.pipeline). The company-name path helps only when the configured name
    appears verbatim: names come from yfinance shortName, so "SentinelOne, Inc."
    does not substring-match a headline that says "SentinelOne". Even so, a
    missed general-feed headline is far cheaper than a digest of misattributed
    macro noise, which is what the bare word boundary produced every single day.
    """
    symbol = re.escape(ticker)
    if len(ticker) <= SHORT_TICKER_MAX_LEN:
        return re.compile(
            rf"(?:\${symbol}\b|\({symbol}\)|\b(?:{_EXCHANGES}):\s*{symbol}\b)"
        )
    return re.compile(rf"\b{symbol}\b")


def matches_ticker(entry: NewsEntry, ticker: str, company_name: str | None = None) -> bool:
    """True if `entry` mentions the ticker symbol or (optionally) the company name."""
    text = f"{entry.title} {entry.summary}"
    if ticker_pattern(ticker).search(text):
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
