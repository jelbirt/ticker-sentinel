"""LLM news style — subprocess mocked throughout; the suite never calls Claude."""
from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from sentinel.news import llm
from sentinel.news.pipeline import NewsDigest, NewsItem, TickerNews
from sentinel.news.styles import render_news

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

    def test_success_returns_text_and_pins_model(self, monkeypatch):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env", {})
            return SimpleNamespace(returncode=0, stdout="  a narrative  ", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        out = llm.call_claude("summarize this", model="claude-sonnet-5")
        assert out == "a narrative"
        assert captured["cmd"][:4] == ["claude", "-p", "--model", "claude-sonnet-5"]
        assert captured["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == llm.MAX_OUTPUT_TOKENS
        assert len(captured["cmd"]) == 5  # exactly one prompt arg — one call, no extras

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
            llm, "call_claude", lambda prompt, model, **k: "CRWD had a strong beat.\n\nQuiet otherwise."
        )
        html, notes = render_news(digest, "llm-brief", model="claude-sonnet-5")
        assert notes == []
        assert "CRWD had a strong beat.<br><br>Quiet otherwise." in html
        assert 'href="https://news.example.com/crwd"' in html          # sources from digest
        assert "Synthesized by claude-sonnet-5" in html                # attribution
        assert "<style" not in html

    def test_model_output_is_escaped_never_live_html(self, digest, monkeypatch):
        monkeypatch.setattr(
            llm, "call_claude", lambda *a, **k: '<script>alert(1)</script> & "quotes"'
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
            return "text"

        monkeypatch.setattr(llm, "call_claude", fake)
        render_news(digest, "llm-brief", model="claude-sonnet-5")
        assert "CRWD (CrowdStrike):" in captured["prompt"]
        assert "CRWD beats estimates" in captured["prompt"]
        assert "investment advice" in captured["prompt"]
