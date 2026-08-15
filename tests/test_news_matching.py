"""Phase 3 news matching — pure logic, no network."""
from __future__ import annotations

import pytest

from sentinel.news.feeds import NewsEntry
from sentinel.news.matching import (
    company_name_matches,
    match_entries,
    matches_ticker,
    normalize_company_name,
)


def _entry(title: str, summary: str = "") -> NewsEntry:
    return NewsEntry(title=title, link="https://example.com/a", source="feed", summary=summary)


class TestMatchesTicker:
    def test_ticker_in_title(self):
        assert matches_ticker(_entry("CRWD beats on earnings"), "CRWD")

    def test_lowercase_prose_never_matches_symbol(self):
        # P0 regression: symbols are case-sensitive — English words must not
        # misattribute headlines to NET/TEAM/SNOW-style tickers
        assert not matches_ticker(_entry("Company reports higher net income"), "NET")
        assert not matches_ticker(_entry("The team behind the new fund"), "TEAM")
        assert not matches_ticker(_entry("Heavy snow disrupts retail traffic"), "SNOW")
        assert not matches_ticker(_entry("crwd rallies after hours"), "CRWD")

    def test_uppercase_symbol_still_matches(self):
        assert matches_ticker(_entry("NET jumped 9% on AI enthusiasm"), "NET")
        assert matches_ticker(_entry("Cloudflare (NET) beats estimates"), "NET")
        assert matches_ticker(_entry("Analysts weigh SNOW ahead of earnings"), "SNOW")

    def test_company_name_still_case_insensitive(self):
        assert matches_ticker(_entry("snowflake expands AI platform"), "SNOW", "Snowflake")

    def test_ticker_word_boundary_avoids_false_positive(self):
        # "NET" must not match inside "INTERNET" either
        assert not matches_ticker(_entry("INTERNET INFRASTRUCTURE REPORT"), "NET")

    def test_ticker_in_summary(self):
        assert matches_ticker(_entry("Cloud stocks rally", summary="DDOG up 5% today"), "DDOG")

    def test_company_name_match(self):
        assert matches_ticker(
            _entry("Snowflake announces new product"), "SNOW", company_name="Snowflake"
        )

    def test_no_match(self):
        assert not matches_ticker(_entry("Oil prices climb"), "CRWD", company_name="CrowdStrike")


class TestShortTickers:
    """1-2 char symbols: a bare word boundary is not evidence of a mention.

    `\\bS\\b` matches the S inside "U.S." and "S&P 500", so with S
    (SentinelOne) on the watchlist every macro headline on the general feeds
    was attributed to it.
    """

    def test_abbreviations_are_not_the_symbol_s(self):
        assert not matches_ticker(_entry("U.S. stocks rally"), "S")
        assert not matches_ticker(_entry("S&P 500 hits record high"), "S")
        assert not matches_ticker(_entry("S. Korea trade data beats"), "S")
        assert not matches_ticker(_entry("Markets climb", summary="The U.S. dollar slipped"), "S")

    def test_explicit_symbol_context_matches(self):
        assert matches_ticker(_entry("$S jumped after the breach report"), "S")
        assert matches_ticker(_entry("SentinelOne (S) beats estimates"), "S")
        assert matches_ticker(_entry("NYSE: S added to the index"), "S")
        assert matches_ticker(_entry("NASDAQ:S halted briefly"), "S")
        assert matches_ticker(_entry("NYSE:  S added (reformatted wire)"), "S")
        assert matches_ticker(_entry("NYSE American: S begins trading"), "S")

    def test_bare_two_char_symbol_no_longer_matches(self):
        # deliberate tradeoff: a bare "DT rallies" headline on a general feed is
        # given up, because accepting it also means accepting every
        # "U.S."-style abbreviation. Per-ticker feeds skip matching entirely,
        # so DT still gets its own coverage.
        assert not matches_ticker(_entry("DT rallies on cloud demand"), "DT")
        assert matches_ticker(_entry("Dynatrace (DT) raises outlook"), "DT")

    def test_company_name_path_carries_the_short_tickers(self):
        # with bare symbols given up, the name path is what still attributes a
        # general-feed headline to S/DT — and it works on the LEGAL name
        # yfinance supplies, not just a hand-trimmed one
        assert matches_ticker(_entry("sentinelone wins federal deal"), "S", "SentinelOne")
        assert matches_ticker(_entry("Dynatrace rallies on cloud demand"), "DT", "Dynatrace")
        assert matches_ticker(
            _entry("SentinelOne wins federal deal"), "S", "SentinelOne, Inc."
        )

    def test_longer_tickers_keep_word_boundary_behavior(self):
        assert matches_ticker(_entry("NET jumped 9% on AI enthusiasm"), "NET")
        assert not matches_ticker(_entry("Company reports higher net income"), "NET")


class TestNormalizeCompanyName:
    """yfinance supplies legal names; headlines print the trading name.

    Without normalization the whole company-name path is dead on this
    watchlist: "sentinelone, inc." is not a substring of any real headline.
    """

    @pytest.mark.parametrize(
        "legal_name, expected",
        [
            # the real watchlist names, straight out of the fundamentals cache
            ("SentinelOne, Inc.", "sentinelone"),
            ("CrowdStrike Holdings, Inc.", "crowdstrike"),  # iterative: inc, then holdings
            ("Elastic N.V.", "elastic"),
            ("monday.com Ltd.", "monday.com"),  # interior punctuation survives
            ("Atlassian Corporation", "atlassian"),
            ("Palo Alto Networks, Inc.", "palo alto networks"),
            ("Palantir Technologies Inc.", "palantir technologies"),
            ("Okta, Inc.", "okta"),  # lands exactly on the 4-char floor
            ("GitLab Inc.", "gitlab"),
            ("Snowflake Inc.", "snowflake"),
            # spelling variants of one suffix collapse to the same key
            ("Elastic N V", "elastic"),
            ("Elastic NV", "elastic"),
            ("Acme S.A.", "acme"),
        ],
    )
    def test_legal_suffixes_are_stripped(self, legal_name, expected):
        assert normalize_company_name(legal_name) == expected

    def test_only_trailing_suffixes_are_stripped(self):
        # "holding" and "company" here are part of the name, not designators
        assert normalize_company_name("Holding Company X") == "holding company x"
        assert normalize_company_name("Corporation Services Group") == "corporation services group"

    def test_never_strips_below_the_length_floor(self):
        # "sea" alone would hit inside "research", "seasonal", "increase"
        assert normalize_company_name("Sea Limited") == "sea limited"
        # a name made only of suffix words keeps its last token
        assert normalize_company_name("Co Inc") == "co inc"
        assert normalize_company_name("Inc.") == "inc"


class TestCompanyNameMatches:
    def test_legal_names_match_ordinary_headlines(self):
        assert company_name_matches("SentinelOne, Inc.", "SentinelOne posts a strong quarter")
        assert company_name_matches("CrowdStrike Holdings, Inc.", "CrowdStrike delivered a beat")
        assert company_name_matches("Elastic N.V.", "Elastic shares jumped")
        assert company_name_matches("monday.com Ltd.", "monday.com raises guidance")

    def test_unrelated_text_does_not_match(self):
        assert not company_name_matches("SentinelOne, Inc.", "Oil prices climb on supply cuts")

    def test_short_normalized_names_never_probe(self):
        # "3m" is 2 chars: as a substring it would match "3mm", "$3m", "H3M"
        assert not company_name_matches("3M Company", "Shares rose to $3m in volume")
        assert not company_name_matches("Box", "The boxes shipped late")

    def test_missing_name_is_not_a_match(self):
        assert not company_name_matches(None, "anything at all")
        assert not company_name_matches("", "anything at all")


class TestMatchesTickerCompanyPath:
    @pytest.mark.parametrize(
        "ticker, legal_name, headline",
        [
            ("S", "SentinelOne, Inc.", "SentinelOne posts a strong quarter"),
            ("CRWD", "CrowdStrike Holdings, Inc.", "CrowdStrike delivered a clean beat"),
            ("ESTC", "Elastic N.V.", "Elastic shares jumped"),
            ("MNDY", "monday.com Ltd.", "monday.com raises guidance"),
        ],
    )
    def test_watchlist_legal_names_match(self, ticker, legal_name, headline):
        assert matches_ticker(_entry(headline), ticker, legal_name)

    def test_negative_still_negative(self):
        assert not matches_ticker(
            _entry("Oil prices climb on supply cuts"), "S", "SentinelOne, Inc."
        )

    def test_symbol_path_is_untouched(self):
        # the name path must not smuggle back the macro noise the short-ticker
        # rules exist to keep out
        assert not matches_ticker(_entry("U.S. stocks rally"), "S", "SentinelOne, Inc.")
        assert not matches_ticker(_entry("S&P 500 hits record"), "S", "SentinelOne, Inc.")
        assert not matches_ticker(_entry("Company reports higher net income"), "NET", "Cloudflare, Inc.")


class TestMatchEntries:
    def test_multiple_tickers_and_entries(self):
        entries = [
            _entry("CRWD announces earnings beat"),
            _entry("Market roundup", summary="DDOG and SNOW both rallied"),
            _entry("Unrelated oil news"),
        ]
        tickers = {"CRWD": "CrowdStrike", "DDOG": "Datadog", "SNOW": "Snowflake"}
        matches = match_entries(entries, tickers)
        matched_tickers = sorted((m.ticker, m.entry.title) for m in matches)
        assert matched_tickers == [
            ("CRWD", "CRWD announces earnings beat"),
            ("DDOG", "Market roundup"),
            ("SNOW", "Market roundup"),
        ]

    def test_no_entries_no_matches(self):
        assert match_entries([], {"CRWD": None}) == []

    def test_no_tickers_no_matches(self):
        assert match_entries([_entry("CRWD news")], {}) == []
