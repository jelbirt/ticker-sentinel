"""News presentation styles — the RENDERING side of Phase 3.

A style turns one NewsDigest into a Gmail-safe HTML fragment. Styles are
swappable via config (`news.style`) so the same pipeline output can be
represented in different tones/formats — and future styles (e.g. an
LLM-narrative pass) register here without touching the pipeline.

Style contract:
- input: NewsDigest (may carry more items than the style chooses to show —
  trimming is allowed, expanding/fetching is not) + the configured model name
  (deterministic styles ignore it)
- output: HTML fragment string — tables + inline CSS only, no <style> blocks
  (the template injects it with |safe, so styles MUST html-escape all text) —
  or None, meaning "I can't render right now": render_news then fails open
  to the default deterministic style
"""
from __future__ import annotations

import html
from datetime import datetime
from urllib.parse import urlparse

from sentinel.news.pipeline import NewsDigest, NewsItem

_FONT = "font-family:Arial,Helvetica,sans-serif;"


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _domain(url: str) -> str:
    netloc = urlparse(url).netloc
    return netloc.removeprefix("www.") or "feed"


def _age(published: datetime | None, as_of: datetime) -> str:
    if published is None:
        return ""
    hours = (as_of - published).total_seconds() / 3600
    if hours < 1:
        return f"{max(int(hours * 60), 1)}m ago"
    if hours < 24:
        return f"{int(hours)}h ago"
    return f"{int(hours // 24)}d ago"


def _item_meta(item: NewsItem, as_of: datetime) -> str:
    parts = [p for p in (_domain(item.source), _age(item.published, as_of)) if p]
    return _esc(" · ".join(parts))


def _headlines(digest: NewsDigest, model: str | None = None) -> str:
    """Per-ticker linked headlines — the full digest, nothing editorialized."""
    rows = []
    for tn in digest.tickers:
        lines = []
        for item in tn.items:
            lines.append(
                f'<a href="{_esc(item.link)}" style="color:#1f5fa8;text-decoration:none;">'
                f"{_esc(item.title)}</a> "
                f'<span style="font-size:11px;color:#8a94a0;">({_item_meta(item, digest.as_of)})</span>'
            )
        company = (
            f'<br><span style="font-size:11px;color:#66727f;">{_esc(tn.company_name)}</span>'
            if tn.company_name
            else ""
        )
        rows.append(
            f'<tr style="border-bottom:1px solid #dde3e9;">'
            f'<td valign="top" style="padding:7px 10px;{_FONT}font-size:13px;white-space:nowrap;">'
            f"<b>{_esc(tn.ticker)}</b>{company}</td>"
            f'<td style="padding:7px 10px;{_FONT}font-size:13px;line-height:1.7;">'
            + "<br>".join(lines)
            + "</td></tr>"
        )
    return (
        '<table cellpadding="0" cellspacing="0" border="0" width="100%" '
        'style="border-collapse:collapse;">' + "".join(rows) + "</table>"
    )


def _brief(digest: NewsDigest, model: str | None = None) -> str:
    """One line per ticker: the top story only — trims the digest (allowed)."""
    lines = []
    for tn in digest.tickers:
        top = tn.items[0]
        more = f" · +{len(tn.items) - 1} more" if len(tn.items) > 1 else ""
        lines.append(
            f"<b>{_esc(tn.ticker)}</b> — "
            f'<a href="{_esc(top.link)}" style="color:#1f5fa8;text-decoration:none;">'
            f"{_esc(top.title)}</a> "
            f'<span style="font-size:11px;color:#8a94a0;">({_item_meta(top, digest.as_of)}){_esc(more)}</span>'
        )
    return (
        f'<div style="{_FONT}font-size:13px;line-height:1.9;color:#33404d;">'
        + "<br>".join(lines)
        + "</div>"
    )


_LLM_PROMPT = """You write the "What mattered today" section of a private daily stock-report email.
Below are today's headlines already matched to the owner's watchlist tickers.

Write a tight synthesis in plain text (no markdown, no HTML, no preamble):
one short paragraph per ticker that has meaningful news, each starting with
the ticker symbol; if several tickers only have minor items, group them into
a single final one-liner. Factual and analytical in tone — no hype, no
investment advice. Under 160 words total.

HEADLINES:
{headlines}"""


def _digest_text(digest: NewsDigest) -> str:
    lines = []
    for tn in digest.tickers:
        name = f" ({tn.company_name})" if tn.company_name else ""
        lines.append(f"{tn.ticker}{name}:")
        for item in tn.items:
            age = _age(item.published, digest.as_of)
            lines.append(f"- {item.title}" + (f" [{age}]" if age else ""))
    return "\n".join(lines)


def _llm_brief(digest: NewsDigest, model: str | None = None) -> str | None:
    """LLM-written narrative over the digest; sources rendered deterministically.

    The model only ever writes prose (escaped before injection — it can never
    emit live HTML); links/attribution come from the digest itself. Returns
    None on any LLM failure so render_news fails open to the default style.
    """
    from sentinel.news.llm import call_claude

    if not model:
        return None
    prompt = _LLM_PROMPT.format(headlines=_digest_text(digest))
    text = call_claude(prompt, model=model)
    if not text:
        return None

    narrative = _esc(text).replace("\n\n", "<br><br>").replace("\n", "<br>")
    source_bits = []
    for tn in digest.tickers:
        links = " ".join(
            f'<a href="{_esc(item.link)}" style="color:#1f5fa8;text-decoration:none;">[{i}]</a>'
            for i, item in enumerate(tn.items, start=1)
        )
        source_bits.append(f"{_esc(tn.ticker)} {links}")
    return (
        f'<div style="{_FONT}font-size:13px;line-height:1.7;color:#33404d;">{narrative}</div>'
        f'<div style="{_FONT}font-size:11px;color:#8a94a0;padding-top:8px;">'
        f"Sources: {' · '.join(source_bits)}<br>"
        f"Synthesized by {_esc(model)} from {digest.matched} matched headlines; "
        f"links above are the underlying stories.</div>"
    )


STYLES = {
    "headlines": _headlines,
    "brief": _brief,
    "llm-brief": _llm_brief,
}

DEFAULT_STYLE = "headlines"


def render_news(
    digest: NewsDigest, style_name: str, model: str | None = None
) -> tuple[str | None, list[str]]:
    """Render with the configured style. Unknown names and styles that return
    None (e.g. the LLM path failing) degrade to the default deterministic style."""
    if digest.empty:
        return None, []
    notes: list[str] = []
    renderer = STYLES.get(style_name)
    if renderer is None:
        notes.append(
            f"unknown news style '{style_name}' — using '{DEFAULT_STYLE}' "
            f"(available: {', '.join(sorted(STYLES))})"
        )
        renderer = STYLES[DEFAULT_STYLE]
    html_fragment = renderer(digest, model)
    if html_fragment is None and renderer is not STYLES[DEFAULT_STYLE]:
        notes.append(f"news style '{style_name}' unavailable — fell back to '{DEFAULT_STYLE}'")
        html_fragment = STYLES[DEFAULT_STYLE](digest, model)
    return html_fragment, notes
