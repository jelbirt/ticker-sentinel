"""Phase 3 news matching — pure logic, no network."""
from __future__ import annotations

from sentinel.news.feeds import NewsEntry
from sentinel.news.matching import match_entries, matches_ticker


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

    def test_company_name_path_is_unchanged(self):
        # unchanged also means unchanged in its limits: the configured name has
        # to appear verbatim, and yfinance supplies it with the legal suffix
        # attached, so it is a partial recovery at best
        assert matches_ticker(_entry("sentinelone wins federal deal"), "S", "SentinelOne")
        assert matches_ticker(_entry("Dynatrace rallies on cloud demand"), "DT", "Dynatrace")
        assert not matches_ticker(
            _entry("SentinelOne wins federal deal"), "S", "SentinelOne, Inc."
        )

    def test_longer_tickers_keep_word_boundary_behavior(self):
        assert matches_ticker(_entry("NET jumped 9% on AI enthusiasm"), "NET")
        assert not matches_ticker(_entry("Company reports higher net income"), "NET")


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
