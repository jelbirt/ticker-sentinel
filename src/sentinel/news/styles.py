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
import re
from datetime import datetime
from urllib.parse import urlparse

from sentinel.news.matching import SHORT_TICKER_MAX_LEN, ticker_pattern
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


def _headlines(
    digest: NewsDigest,
    model: str | None = None,
    tone: str | None = None,
    known_tickers: set[str] | None = None,
) -> str:
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


def _brief(
    digest: NewsDigest,
    model: str | None = None,
    tone: str | None = None,
    known_tickers: set[str] | None = None,
) -> str:
    """One line per ticker: the top story only — trims the digest (allowed)."""
    lines = []
    for tn in digest.tickers:
        top = tn.items[0]
        more = f" · +{len(tn.items) - 1} more" if len(tn.items) > 1 else ""
        lines.append(
            f"<b>{_esc(tn.ticker)}</b>: "
            f'<a href="{_esc(top.link)}" style="color:#1f5fa8;text-decoration:none;">'
            f"{_esc(top.title)}</a> "
            f'<span style="font-size:11px;color:#8a94a0;">({_item_meta(top, digest.as_of)}){_esc(more)}</span>'
        )
    return (
        f'<div style="{_FONT}font-size:13px;line-height:1.9;color:#33404d;">'
        + "<br>".join(lines)
        + "</div>"
    )


# The prompt is two layers: a swappable VOICE (tone presets below) and the
# structural RULES, which are invariant and always override the voice — they
# guarantee coverage, plain-text output, and the no-advice contract no matter
# which tone is configured.
_LLM_PROMPT = """You write the "What mattered today" section of a private daily stock-report email.
Below are today's headlines already matched to the owner's watchlist tickers.

VOICE:
{voice}

RULES (these always override the voice):
Plain text only: no markdown, no HTML. One short paragraph per ticker that
has meaningful news, each starting with the ticker symbol; if several tickers
only have minor items, group them into a single final one-liner. EVERY ticker
listed below must be mentioned exactly once — either in its own paragraph or
in the grouped one-liner; never omit one, and never discuss a ticker that is
not listed below. Only state what the headlines support: no outside
knowledge, numbers, or events. Never give investment advice or tell the
reader what to do. Under 180 words total. Never use em dashes or en dashes
anywhere in the text; use commas, colons, parentheses, or separate
sentences instead.

OUTPUT FORMAT (this text goes directly into an email a reader sees):
wrap the finished section between <REPORT> and </REPORT> markers. Everything
outside the markers is discarded, so put NOTHING else inside them: no
planning notes, no drafts, no word counts, no commentary about the task, no
sign-off. If you revise, only the final <REPORT> block counts.

HEADLINES:
{headlines}"""

_REPORT_RE = re.compile(r"<REPORT>(.*?)</REPORT>", re.DOTALL | re.IGNORECASE)
_OPEN_RE = re.compile(r"<REPORT>", re.IGNORECASE)
_CLOSE_RE = re.compile(r"</REPORT>", re.IGNORECASE)


def _extract_report(text: str) -> str | None:
    """Take the LAST marker-wrapped block — immune to narration and draft passes.

    No markers, or UNBALANCED markers (a stray close mid-narrative would make
    the non-greedy match silently truncate the block), mean the output contract
    wasn't honored: return None so the tone skips rather than shipping a
    fragment or meta-commentary.
    """
    if len(_OPEN_RE.findall(text)) != len(_CLOSE_RE.findall(text)):
        return None
    blocks = _REPORT_RE.findall(text)
    if not blocks:
        return None
    return blocks[-1].strip() or None


def _sentence_start_symbol(ticker: str) -> re.Pattern:
    """Bare symbol opening a sentence or paragraph: start of text, start of a
    line, or right after sentence-ending punctuation plus whitespace.

    This is the shape a fabricated claim about a short ticker actually takes,
    because the prompt tells the model to OPEN each paragraph with the symbol
    ("S fell 30% after a breach."). The trailing lookahead keeps the abbrevi-
    ations that share the letter out of it: "S&P 500" and "S. Korea" opening a
    sentence are prose, not a claim about the symbol.
    """
    return re.compile(rf"(?:^|(?<=[.!?:]\s)){re.escape(ticker)}\b(?![&.])", re.MULTILINE)


def _narrative_valid(
    text: str, digest: NewsDigest, known_tickers: set[str] | None = None
) -> bool:
    """Guard against hallucination shipping under authoritative source links.

    Rejects a narrative that (a) drops a digest ticker entirely (neither its
    symbol nor company name appears — the sources footer would then point at
    stories the prose never covered), or (b) claims something about a known
    watchlist ticker that is NOT in today's digest — the model had no headlines
    for it, so any claim about it is fabricated. Peer mentions by company name
    (Oracle, Fortinet, ...) remain legitimate editorial comparison.

    The two checks use DELIBERATELY different symbol tests, because a false
    positive costs the opposite thing in each direction:
    - COVERAGE stays permissive (bare word boundary or company name): the model
      was handed this ticker's headlines and told to open its paragraph with
      the symbol, so any plausible mention counts. A stricter test here would
      veto good narratives (with S on the watchlist, every "U.S." sentence used
      to look like proof, and every valid narrative without one looked like a
      drop).
    - FABRICATION stays strict-plus-sentence-start: the strict
      matching.ticker_pattern (cashtag, parenthesized, exchange-prefixed) plus,
      for 1-2 char symbols, the bare symbol at a sentence or paragraph start.
      Strict alone would be near-vacuous for short symbols, since a fabricated
      claim will read "S fell 30%..." at a paragraph opening, never "$S".
      Bare-word-boundary alone would veto any narrative saying "U.S." or
      "S&P 500" whenever S had no news that day.
    """
    for tn in digest.tickers:
        symbol_present = re.search(rf"\b{re.escape(tn.ticker)}\b", text)
        name_present = tn.company_name and tn.company_name.lower() in text.lower()
        if not symbol_present and not name_present:
            return False
    if known_tickers:
        in_digest = {tn.ticker for tn in digest.tickers}
        for ticker in known_tickers - in_digest:
            if ticker_pattern(ticker).search(text):
                return False
            if len(ticker) <= SHORT_TICKER_MAX_LEN and _sentence_start_symbol(
                ticker
            ).search(text):
                return False
    return True

# Named tone presets — presentation-layer only (Phase 3.1). A tone may shift
# emphasis within the digest but never what the pipeline selected.
TONES = {
    "neutral-analyst": (
        "Factual and analytical. State what happened and why it matters; "
        "no hype, no editorializing beyond what the headlines support."
    ),
    "skeptic": (
        "Lead with what could be wrong or overstated. Treat bullish headlines "
        "as claims to be tested: flag promotional or vague language, note what "
        "is unconfirmed, and keep company facts clearly separated from market "
        "chatter and analyst opinion."
    ),
    "brief-wire": (
        "Terse newswire clips: the shortest accurate sentence per item, minimal "
        "adjectives, no color, no transitions. Facts and numbers only."
    ),
    "morning-brew": (
        "Conversational and light, like a sharp finance newsletter a friend "
        "writes over coffee: approachable phrasing and a touch of wit, but "
        "every fact intact and nothing dumbed down."
    ),
    "barrons": (
        "The voice of a veteran Barron's columnist: fundamentally driven and "
        "valuation-anchored, written for a sophisticated individual investor "
        "who reads actively for ideas. Weigh each development against the "
        "company's investment case — growth, margins, the multiple — and say "
        "plainly whether the market's reaction looks proportionate, overdone, "
        "or underappreciated. Crisp declarative sentences, plain language over "
        "jargon, an occasional dry turn of phrase; skeptical of hype, "
        "respectful of numbers."
    ),
}

DEFAULT_TONE = "neutral-analyst"
LLM_STYLES = {"llm-brief"}  # styles that consume the tone/model options


def _digest_text(digest: NewsDigest) -> str:
    lines = []
    for tn in digest.tickers:
        name = f" ({tn.company_name})" if tn.company_name else ""
        lines.append(f"{tn.ticker}{name}:")
        for item in tn.items:
            age = _age(item.published, digest.as_of)
            lines.append(f"- {item.title}" + (f" [{age}]" if age else ""))
    return "\n".join(lines)


def _llm_brief(
    digest: NewsDigest,
    model: str | None = None,
    tone: str | None = None,
    known_tickers: set[str] | None = None,
) -> str | None:
    """LLM-written narrative over the digest; sources rendered deterministically.

    The model only ever writes prose (escaped before injection — it can never
    emit live HTML); links/attribution come from the digest itself. Returns
    None on any LLM failure so render_news fails open to the default style.
    """
    from sentinel.news.llm import call_claude

    if not model:
        return None
    tone = tone if tone in TONES else DEFAULT_TONE
    prompt = _LLM_PROMPT.format(voice=TONES[tone], headlines=_digest_text(digest))
    raw = call_claude(prompt, model=model)
    if not raw:
        return None
    text = _extract_report(raw)
    if not text:  # model ignored the output contract — skip rather than leak meta-talk
        return None
    # Belt-and-braces on the prompt's no-dash rule: the reader must never see
    # an em/en dash, whatever the model does.
    text = re.sub(r"[ \t]*[—–][ \t]*", " - ", text)
    if not _narrative_valid(text, digest, known_tickers):
        return None  # dropped or fabricated ticker coverage — skip rather than mislead

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
        f"Synthesized by {_esc(model)} · {_esc(tone)} tone · from {digest.matched} "
        f"matched headlines; links above are the underlying stories.</div>"
    )


STYLES = {
    "headlines": _headlines,
    "brief": _brief,
    "llm-brief": _llm_brief,
}

DEFAULT_STYLE = "headlines"


def render_news(
    digest: NewsDigest,
    style_name: str,
    model: str | None = None,
    tone: str | None = None,
    fallback: bool = True,
    known_tickers: set[str] | None = None,
) -> tuple[str | None, list[str]]:
    """Render with the configured style and tone. Unknown names and styles that
    return None (e.g. the LLM path failing) degrade to safe defaults; pass
    fallback=False to get (None, notes) instead — used by multi-tone rendering,
    where a failed tone should be skipped rather than duplicated as headlines."""
    if digest.empty:
        return None, []
    notes: list[str] = []
    renderer = STYLES.get(style_name)
    if renderer is None:
        notes.append(
            f"unknown news style '{style_name}'; using '{DEFAULT_STYLE}' "
            f"(available: {', '.join(sorted(STYLES))})"
        )
        renderer = STYLES[DEFAULT_STYLE]
    if tone is not None and tone not in TONES:
        if style_name in LLM_STYLES:  # tone is inert elsewhere; don't note config noise
            notes.append(
                f"unknown news tone '{tone}'; using '{DEFAULT_TONE}' "
                f"(available: {', '.join(sorted(TONES))})"
            )
        tone = DEFAULT_TONE
    html_fragment = renderer(digest, model, tone, known_tickers)
    if html_fragment is None and fallback and renderer is not STYLES[DEFAULT_STYLE]:
        notes.append(f"news style '{style_name}' unavailable; fell back to '{DEFAULT_STYLE}'")
        html_fragment = STYLES[DEFAULT_STYLE](digest, model, tone, known_tickers)
    return html_fragment, notes
