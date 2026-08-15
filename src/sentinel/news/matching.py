"""Per-ticker news matching — pure logic, no I/O (Phase 3).

Matches by ticker symbol (case-SENSITIVE — prose words like "net"/"team"/"snow"
must never match NET/TEAM/SNOW; symbols of 1-2 chars additionally require
explicit symbol context, see ticker_pattern) or company name (case-insensitive
and legal-suffix-normalized; names are natural language, see
normalize_company_name) appearing in an entry's title/summary.
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
    news.pipeline), plus the company-name path, which normalizes the yfinance
    legal name ("SentinelOne, Inc." -> "sentinelone") so an ordinary headline
    about the company still matches (see normalize_company_name). Even so, a
    missed general-feed headline is far cheaper than a digest of misattributed
    macro noise, which is what the bare word boundary produced every single day.
    """
    symbol = re.escape(ticker)
    if len(ticker) <= SHORT_TICKER_MAX_LEN:
        return re.compile(
            rf"(?:\${symbol}\b|\({symbol}\)|\b(?:{_EXCHANGES}):\s*{symbol}\b)"
        )
    return re.compile(rf"\b{symbol}\b")


# Trailing legal-entity suffixes, written without punctuation or spaces so that
# every spelling of one suffix collapses to the same key: "N.V.", "N V" and
# "NV" all reduce to "nv", "S.A."/"S A"/"SA" to "sa". Deliberately conservative:
# only forms that are unambiguously entity designators, never descriptive words
# ("technologies", "systems", "networks", "labs") that are part of how a company
# is actually referred to in a headline.
_LEGAL_SUFFIXES = frozenset(
    {
        "inc", "incorporated", "corp", "corporation", "holdings", "holding",
        "ltd", "limited", "plc", "co", "company", "nv", "sa", "ag", "se",
    }
)

# A normalized name shorter than this is not usable as a substring probe: a
# 2-3 char fragment ("sea", "box") hits inside ordinary words and would
# misattribute headlines wholesale — exactly the failure the short-ticker rules
# above exist to prevent.
MIN_COMPANY_NAME_LEN = 4

# Punctuation stripped from the EDGES of each token only: "Inc." -> "inc",
# "SentinelOne," -> "sentinelone". Interior punctuation stays, because it is
# part of the name a headline actually prints ("monday.com" must not become
# "mondaycom").
_EDGE_PUNCT = ",."


def normalize_company_name(name: str) -> str:
    """Reduce a legal company name to the form headlines actually use.

    yfinance supplies legal names ("CrowdStrike Holdings, Inc.", "Elastic
    N.V."), but a headline says "CrowdStrike delivered a clean beat". A plain
    substring test of the full legal name therefore never fires, which left the
    company-name path effectively dead for the entire watchlist.

    Lowercases, drops edge punctuation, then iteratively removes TRAILING legal
    suffixes: "CrowdStrike Holdings, Inc." -> "crowdstrike" (drops "inc", then
    "holdings"). Two guards keep the reduction honest:

    - Trailing only, and never the last remaining token. A suffix word that is
      not at the end is part of the name ("Holding Company X" keeps "holding"),
      and a name made entirely of suffix words keeps its final word.
    - Never reduces below MIN_COMPANY_NAME_LEN characters: the longest safe
      form is returned instead, falling back to the pre-strip form. A name whose
      normalized form is still under that length is simply not matched on
      (see company_name_matches), because a 2-3 char probe matches everything.
    """
    tokens = [t for t in (raw.strip(_EDGE_PUNCT) for raw in name.lower().split()) if t]
    form = " ".join(tokens)
    while tokens:
        for size in (2, 1):  # longest first: "n v" before "v"
            # tokens[-size:] joined without separators, so "n"+"v" -> "nv"
            if len(tokens) > size and "".join(
                t.replace(".", "") for t in tokens[-size:]
            ) in _LEGAL_SUFFIXES:
                candidate = " ".join(tokens[:-size])
                break
        else:
            return form
        if len(candidate) < MIN_COMPANY_NAME_LEN:
            return form  # longest safe form: stripping further loses the name
        tokens = tokens[:-size]
        form = candidate
    return form


def company_name_matches(company_name: str | None, text: str) -> bool:
    """True if `text` mentions the company, by its normalized name.

    Company names are natural-language phrases, not symbols: a plain
    case-insensitive substring of the normalized name is enough, no word
    boundary needed. The single implementation shared by feed matching and the
    narrative coverage check in news.styles.
    """
    if not company_name:
        return False
    normalized = normalize_company_name(company_name)
    if len(normalized) < MIN_COMPANY_NAME_LEN:
        return False
    return normalized in text.lower()


def matches_ticker(entry: NewsEntry, ticker: str, company_name: str | None = None) -> bool:
    """True if `entry` mentions the ticker symbol or (optionally) the company name."""
    text = f"{entry.title} {entry.summary}"
    if ticker_pattern(ticker).search(text):
        return True
    return company_name_matches(company_name, text)


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
