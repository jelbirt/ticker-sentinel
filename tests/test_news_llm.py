"""LLM news style — subprocess mocked throughout; the suite never calls Claude."""
from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from sentinel.news import llm
from sentinel.news.pipeline import NewsDigest, NewsItem, TickerNews
from sentinel.news.styles import DEFAULT_TONE, TONES, render_news

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
                        title="CRWD beats estimates",
                        link="https://news.example.com/crwd",
                        source="https://feeds.example.com/top",
                        published=NOW - timedelta(hours=2),
                    )
                ],
            )
        ],
        scanned=10,
        matched=1,
    )


class TestCallClaude:
    def test_missing_cli_returns_none(self, monkeypatch):
        def boom(*a, **k):
            raise FileNotFoundError("claude")

        monkeypatch.setattr(subprocess, "run", boom)
        assert llm.call_claude("hi", model="claude-sonnet-5") is None

    def test_timeout_returns_none(self, monkeypatch):
        def slow(*a, **k):
            raise subprocess.TimeoutExpired(cmd="claude", timeout=1)

        monkeypatch.setattr(subprocess, "run", slow)
        assert llm.call_claude("hi", model="claude-sonnet-5") is None

    def test_nonzero_exit_returns_none(self, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="rate limited"),
        )
        assert llm.call_claude("hi", model="claude-sonnet-5") is None

    @staticmethod
    def _stream(*events) -> str:
        import json as jsonlib

        return "\n".join(jsonlib.dumps(e) for e in events)

    def _assistant(self, *texts) -> dict:
        return {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": t} for t in texts]},
        }

    def test_success_returns_text_and_pins_model(self, monkeypatch):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env", {})
            stdout = self._stream(
                self._assistant("a narrative"), {"type": "result", "is_error": False}
            )
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        out = llm.call_claude("summarize this", model="claude-sonnet-5")
        assert out == "a narrative"
        assert captured["cmd"][:7] == [
            "claude", "-p", "--model", "claude-sonnet-5",
            "--output-format", "stream-json", "--verbose",
        ]
        assert captured["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == llm.MAX_OUTPUT_TOKENS
        assert captured["env"]["MAX_THINKING_TOKENS"] == "0"
        assert captured["cmd"][-1] == "summarize this"
        assert len(captured["cmd"]) == 11  # exactly one prompt arg — one call, no extras

    def test_call_runs_with_no_tools(self, monkeypatch):
        # feed text is attacker-controlled: the call must carry an empty
        # built-in tool set and ignore ambient MCP config
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return SimpleNamespace(returncode=0, stdout="prose", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        llm.call_claude("summarize this", model="claude-sonnet-5")
        cmd = captured["cmd"]
        assert cmd[cmd.index("--tools") + 1] == ""   # "" disables every built-in tool
        assert "--strict-mcp-config" in cmd
        # the empty value must not be swallowed by the prompt slot
        assert cmd.index("--tools") < cmd.index("--strict-mcp-config") < len(cmd) - 1

    def test_multi_message_answers_are_concatenated(self, monkeypatch):
        # the front-truncation bug: continuation turns split the answer across
        # assistant messages; every chunk must survive, joined exactly
        stdout = self._stream(
            self._assistant("CRWD had a strong quarter. NET rallied on comparisons to Orac"),
            self._assistant("le, CoreWeave, and Snowflake."),
            {"type": "result", "is_error": False},
        )
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout=stdout, stderr=""),
        )
        out = llm.call_claude("hi", model="claude-sonnet-5")
        assert out == (
            "CRWD had a strong quarter. NET rallied on comparisons to "
            "Oracle, CoreWeave, and Snowflake."
        )

    def test_error_result_returns_none(self, monkeypatch):
        stdout = self._stream(
            {"type": "result", "is_error": True, "result": "usage limit reached"}
        )
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout=stdout, stderr=""),
        )
        assert llm.call_claude("hi", model="claude-sonnet-5") is None

    def test_plain_text_stdout_tolerated(self, monkeypatch):
        # if a CLI update changes the output format, degrade to using raw text
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: SimpleNamespace(returncode=0, stdout="just text", stderr=""),
        )
        assert llm.call_claude("hi", model="claude-sonnet-5") == "just text"

    def test_prompt_hard_truncated(self, monkeypatch):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["prompt"] = cmd[-1]
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        llm.call_claude("x" * 50_000, model="claude-sonnet-5")
        assert len(captured["prompt"]) == llm.MAX_PROMPT_CHARS


class TestLlmBriefStyle:
    def test_renders_narrative_sources_and_attribution(self, digest, monkeypatch):
        monkeypatch.setattr(
            llm, "call_claude", lambda prompt, model, **k: "<REPORT>CRWD had a strong beat.\n\nQuiet otherwise.</REPORT>"
        )
        html, notes = render_news(digest, "llm-brief", model="claude-sonnet-5")
        assert notes == []
        assert "CRWD had a strong beat.<br><br>Quiet otherwise." in html
        assert 'href="https://news.example.com/crwd"' in html          # sources from digest
        assert "Synthesized by claude-sonnet-5" in html                # attribution
        assert "<style" not in html

    def test_model_output_is_escaped_never_live_html(self, digest, monkeypatch):
        monkeypatch.setattr(
            llm, "call_claude", lambda *a, **k: '<REPORT>CRWD: <script>alert(1)</script> & "quotes"</REPORT>'
        )
        html, _ = render_news(digest, "llm-brief", model="claude-sonnet-5")
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_llm_failure_fails_open_to_headlines(self, digest, monkeypatch):
        monkeypatch.setattr(llm, "call_claude", lambda *a, **k: None)
        html, notes = render_news(digest, "llm-brief", model="claude-sonnet-5")
        assert html is not None
        assert "CRWD beats estimates" in html   # headlines fallback content
        assert any("fell back to 'headlines'" in n for n in notes)

    def test_missing_model_fails_open(self, digest):
        html, notes = render_news(digest, "llm-brief", model=None)
        assert html is not None
        assert any("fell back" in n for n in notes)

    def test_prompt_contains_digest_not_instructions_only(self, digest, monkeypatch):
        captured = {}

        def fake(prompt, model, **k):
            captured["prompt"] = prompt
            return "<REPORT>CRWD text</REPORT>"

        monkeypatch.setattr(llm, "call_claude", fake)
        render_news(digest, "llm-brief", model="claude-sonnet-5")
        assert "CRWD (CrowdStrike):" in captured["prompt"]
        assert "CRWD beats estimates" in captured["prompt"]
        assert "investment advice" in captured["prompt"]


class TestReportExtraction:
    """The output contract: only marker-wrapped content ever reaches the email."""

    def test_narration_around_markers_is_discarded(self, digest, monkeypatch):
        monkeypatch.setattr(
            llm, "call_claude",
            lambda *a, **k: (
                "Let me check the ticker list first.\n"
                "<REPORT>CRWD had a clean beat.</REPORT>\n"
                "The section above is the final output — no further action needed."
            ),
        )
        html, notes = render_news(digest, "llm-brief", model="claude-sonnet-5")
        assert notes == []
        assert "CRWD had a clean beat." in html
        assert "Let me check" not in html
        assert "no further action needed" not in html
        assert "REPORT" not in html  # markers themselves stripped

    def test_draft_then_revise_takes_last_block(self, digest, monkeypatch):
        monkeypatch.setattr(
            llm, "call_claude",
            lambda *a, **k: (
                "<REPORT>first draft, too wordy</REPORT>\n"
                "Word count is high — let me tighten.\n"
                "<REPORT>CRWD final tight version</REPORT>"
            ),
        )
        html, _ = render_news(digest, "llm-brief", model="claude-sonnet-5")
        assert "CRWD final tight version" in html
        assert "first draft" not in html
        assert "let me tighten" not in html

    def test_missing_markers_skips_rather_than_leaks(self, digest, monkeypatch):
        monkeypatch.setattr(
            llm, "call_claude", lambda *a, **k: "unmarked meta-commentary output"
        )
        html, notes = render_news(digest, "llm-brief", model="claude-sonnet-5")
        # fell back to headlines — the unmarked text never reached the email
        assert "unmarked meta-commentary" not in html
        assert any("fell back to 'headlines'" in n for n in notes)

    def test_prompt_states_the_output_contract(self, digest, monkeypatch):
        captured = {}

        def fake(prompt, model, **k):
            captured["prompt"] = prompt
            return "<REPORT>CRWD text</REPORT>"

        monkeypatch.setattr(llm, "call_claude", fake)
        render_news(digest, "llm-brief", model="claude-sonnet-5")
        assert "<REPORT>" in captured["prompt"]
        assert "planning notes" in captured["prompt"]  # (line-wraps preclude longer match)


class TestNarrativeValidation:
    """P1 guard: hallucinated or dropped coverage never ships under source links."""

    def test_unbalanced_stray_close_marker_fails_open(self, digest, monkeypatch):
        # P2: a stray </REPORT> would make the non-greedy regex ship a fragment
        monkeypatch.setattr(
            llm, "call_claude",
            lambda *a, **k: "<REPORT>CRWD beat.</REPORT> stray tail </REPORT>",
        )
        html, notes = render_news(digest, "llm-brief", model="claude-sonnet-5")
        assert any("fell back to 'headlines'" in n for n in notes)

    def test_dropped_digest_ticker_fails_open(self, digest, monkeypatch):
        monkeypatch.setattr(
            llm, "call_claude", lambda *a, **k: "<REPORT>Markets were quiet today.</REPORT>"
        )
        html, notes = render_news(digest, "llm-brief", model="claude-sonnet-5")
        assert "Markets were quiet" not in html
        assert any("fell back to 'headlines'" in n for n in notes)

    def test_company_name_coverage_accepted(self, digest, monkeypatch):
        monkeypatch.setattr(
            llm, "call_claude",
            lambda *a, **k: "<REPORT>CrowdStrike delivered a clean beat.</REPORT>",
        )
        html, notes = render_news(digest, "llm-brief", model="claude-sonnet-5")
        assert notes == []
        assert "CrowdStrike delivered a clean beat." in html

    def test_fabricated_watchlist_ticker_fails_open(self, digest, monkeypatch):
        # DDOG is on the watchlist but NOT in today's digest — the model had no
        # headlines for it, so any claim about it is fabricated
        monkeypatch.setattr(
            llm, "call_claude",
            lambda *a, **k: "<REPORT>CRWD beat estimates. DDOG collapsed 20%.</REPORT>",
        )
        html, notes = render_news(
            digest, "llm-brief", model="claude-sonnet-5",
            known_tickers={"CRWD", "DDOG"},
        )
        assert "DDOG collapsed" not in html
        assert any("fell back to 'headlines'" in n for n in notes)

    def _render(self, digest, monkeypatch, narrative, known_tickers):
        monkeypatch.setattr(llm, "call_claude", lambda *a, **k: f"<REPORT>{narrative}</REPORT>")
        return render_news(
            digest, "llm-brief", model="claude-sonnet-5", known_tickers=known_tickers
        )

    def test_macro_prose_does_not_look_like_a_short_ticker(self, digest, monkeypatch):
        # S (SentinelOne) is on the watchlist but has no news today. A bare
        # word boundary saw "U.S." and "S&P 500" as claims about S and vetoed
        # every valid narrative that mentioned the macro backdrop.
        html, notes = self._render(
            digest, monkeypatch,
            "CRWD beat estimates. U.S. markets rallied and the S&P 500 closed at a record.",
            {"CRWD", "S"},
        )
        assert notes == []
        assert "S&amp;P 500 closed at a record" in html

    def test_cashtag_claim_about_absent_short_ticker_fails_open(self, digest, monkeypatch):
        html, notes = self._render(
            digest, monkeypatch, "CRWD beat estimates. $S collapsed 20%.", {"CRWD", "S"}
        )
        assert "collapsed 20%" not in html
        assert any("fell back to 'headlines'" in n for n in notes)

    def test_bare_short_ticker_opening_a_paragraph_fails_open(self, digest, monkeypatch):
        # the prompt tells the model to OPEN each paragraph with the symbol, so
        # this is the shape a fabricated claim about S actually takes
        html, notes = self._render(
            digest, monkeypatch,
            "CRWD beat estimates.\n\nS fell 30% after a breach.",
            {"CRWD", "S"},
        )
        assert "fell 30%" not in html
        assert any("fell back to 'headlines'" in n for n in notes)

    @pytest.mark.parametrize(
        "narrative",
        [
            "CRWD beat estimates. S fell 30% after a breach.",
            "CRWD beat estimates.  S fell 30% after a breach.",     # two spaces
            "CRWD beat estimates.\n\n  S fell 30% after a breach.",  # indented
            "CRWD beat estimates; S fell 30% after a breach.",       # semicolon
            'CRWD beat estimates. "S fell 30%," analysts said.',     # quoted
            "S fell 30% after a breach. CRWD beat estimates.",       # first thing said
        ],
    )
    def test_sentence_start_claims_fail_open_in_every_spelling(
        self, digest, monkeypatch, narrative
    ):
        # one sentence start has many spellings; all of them are the same claim
        html, notes = self._render(digest, monkeypatch, narrative, {"CRWD", "S"})
        assert "fell 30%" not in html
        assert any("fell back to 'headlines'" in n for n in notes)

    def test_short_ticker_in_the_digest_is_covered_by_the_bare_symbol(self, monkeypatch):
        # coverage stays permissive: when S DOES have news, opening its
        # paragraph with the bare symbol is exactly what the prompt asked for
        s_digest = NewsDigest(
            as_of=NOW,
            tickers=[
                TickerNews(
                    ticker="S",
                    company_name="SentinelOne",
                    items=[
                        NewsItem(
                            title="SentinelOne lands a federal deal",
                            link="https://news.example.com/s",
                            source="https://feeds.example.com/top",
                            published=NOW - timedelta(hours=3),
                        )
                    ],
                )
            ],
            scanned=10,
            matched=1,
        )
        html, notes = self._render(
            s_digest, monkeypatch, "S won a federal contract, its largest to date.", {"S", "CRWD"}
        )
        assert notes == []
        assert "S won a federal contract" in html

    def test_peer_mention_by_company_name_is_legitimate(self, digest, monkeypatch):
        monkeypatch.setattr(
            llm, "call_claude",
            lambda *a, **k: "<REPORT>CRWD rallied, echoing Fortinet and Oracle moves.</REPORT>",
        )
        html, notes = render_news(
            digest, "llm-brief", model="claude-sonnet-5", known_tickers={"CRWD", "DDOG"}
        )
        assert notes == []
        assert "echoing Fortinet and Oracle" in html


class TestTones:
    """Phase 3.1: swappable voice presets; structural rules survive every tone."""

    def _capture_prompt(self, digest, monkeypatch, tone):
        captured = {}

        def fake(prompt, model, **k):
            captured["prompt"] = prompt
            return "<REPORT>CRWD text</REPORT>"

        monkeypatch.setattr(llm, "call_claude", fake)
        _, notes = render_news(digest, "llm-brief", model="claude-sonnet-5", tone=tone)
        return captured["prompt"], notes

    @pytest.mark.parametrize("tone", sorted(TONES))
    def test_every_tone_keeps_structural_invariants(self, digest, monkeypatch, tone):
        prompt, notes = self._capture_prompt(digest, monkeypatch, tone)
        assert notes == []
        assert TONES[tone] in prompt                                  # voice slot filled
        assert "must be mentioned exactly once" in prompt             # coverage rule
        assert "investment advice" in prompt                          # no-advice rule
        assert "no markdown, no HTML" in prompt                       # render-safety rule
        assert "RULES (these always override the voice)" in prompt

    def test_tone_lands_in_attribution_footer(self, digest, monkeypatch):
        monkeypatch.setattr(llm, "call_claude", lambda *a, **k: "<REPORT>CRWD narrative text</REPORT>")
        html, _ = render_news(digest, "llm-brief", model="claude-sonnet-5", tone="barrons")
        assert "barrons tone" in html

    def test_unknown_tone_falls_back_with_note(self, digest, monkeypatch):
        prompt, notes = self._capture_prompt(digest, monkeypatch, "sports-desk")
        assert TONES[DEFAULT_TONE] in prompt
        assert any("unknown news tone 'sports-desk'" in n for n in notes)

    def test_unknown_tone_silent_for_non_llm_styles(self, digest):
        html, notes = render_news(digest, "headlines", tone="sports-desk")
        assert html is not None
        assert notes == []  # tone is inert here; no config-noise note

    def test_fallback_false_returns_none_for_multi_tone_skipping(self, digest, monkeypatch):
        monkeypatch.setattr(llm, "call_claude", lambda *a, **k: None)
        html, notes = render_news(
            digest, "llm-brief", model="claude-sonnet-5", tone="skeptic", fallback=False
        )
        assert html is None
        assert not any("fell back" in n for n in notes)  # caller decides what to do
