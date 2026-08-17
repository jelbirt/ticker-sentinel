# Ticker Sentinel

[![ci](https://github.com/jelbirt/ticker-sentinel/actions/workflows/ci.yml/badge.svg)](https://github.com/jelbirt/ticker-sentinel/actions/workflows/ci.yml)

A personal stock research service that runs in production on GitHub Actions: every trading morning it pulls end-of-day market and fundamentals data for a 20-name growth watchlist, computes a Rule-of-40-centered scorecard with a technical overlay, detects what changed since the prior run, has an LLM write the news brief under strict guardrails, and emails a pre-market HTML report before the open (10:00 UTC, Tue-Sat).

This is the live repo, not a demo: scheduled runs commit the bounded data cache (and, as of the Phase 4 change-detection release, the run history) back to this repository whenever state changes. See the commit log.

## What the report contains

- **Composite scorecard.** Fundamental score built around Rule of 40 (three variants: FCF, EBITDA, SBC-adjusted), combined with a technical/momentum overlay into a composite (C = 0.6F + 0.4T), with per-ticker sparklines and strongest/weakest ranking.
- **Between-quarter signals.** Estimate revisions, analyst recommendation trends, short interest, and insider net activity, so quarters do not go dark between earnings.
- **What changed today (day-over-day deltas).** Change detection across rank and score moves (config-thresholded), flag transitions, trend inflections, estimate swings, and universe changes, diffed against committed run history with 12-run retention. A quiet day renders as exactly one line.
- **Deterioration watch.** A ticker appears only when at least two negative signals fire concurrently; the section is omitted entirely when empty.
- **News digest in five voices.** A two-layer news module separates data from presentation: the pipeline fetches, attributes, dedupes, ranks, and caps headlines into a neutral digest; swappable style renderers then present it. Five LLM-written tones (barrons, neutral-analyst, skeptic, brief-wire, morning-brew) render from one shared digest.

## The LLM integration (and its guardrails)

The news brief is written by Claude via a single headless CLI call per run, treated as an untrusted text generator inside a deterministic pipeline:

- **Output contract:** the model must wrap its section in `<REPORT>` markers; only that block is parsed.
- **No live HTML from the model:** all model prose is HTML-escaped before the template injects it, so generated text can never become markup.
- **Bounded:** prompt truncation, output caps, and a hard timeout on the call.
- **Fail-open:** any failure (bad token, timeout, malformed output) degrades to the deterministic headline renderer, proven in production when an auth failure fell back cleanly rather than blocking delivery.
- **Style rules override voice:** invariant formatting rules are enforced by prompt and then re-enforced by a post-scrub, so tone presets cannot break the report's conventions.

## Engineering notes

- **One green bar.** `scripts/checks.sh` runs the full suite (256 tests); CI, the local commit guard, and developers all run the same script.
- **Tests never touch the network.** The suite runs against committed fixtures; `--dry-run` renders a full report offline from fixture state and never writes.
- **State is bounded and versioned.** The bot-committed cache has a 16-quarter cap and watchlist pruning; run history keeps 12 runs with same-date replace; corrupt state degrades to a data note, never a crash.
- **Failure philosophy.** Partial data renders with explicit per-ticker data notes; market holidays are detected rather than special-cased; ticker-subset runs skip delta detection (partial-universe ranks are not comparable) and say so.
- **Config-driven.** Watchlist, weights, thresholds, tones, model choice, and delivery all live in `config/watchlist.yaml`.

## Running it yourself

Fork it, set the secrets, and the scheduled workflow does the rest.

```bash
# local setup
python -m venv .venv && source .venv/bin/activate
# -c constraints.txt is the committed pin set the workflows install with
pip install -e ".[dev]" -c constraints.txt

# full offline report from fixtures (no network, no email)
python -m sentinel.run --dry-run

# run the test suite the same way CI does
scripts/checks.sh
```

Actions secrets used by the scheduled run:

| Secret | Purpose |
|---|---|
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS` | Email delivery |
| `RECIPIENT_EMAILS` | Delivery targets (never committed; config uses labels only) |
| `TWELVEDATA_API_KEY` | Price-data fallback (paced to the free tier) |
| `FINNHUB_API_KEY` | Optional insider-data fallback (runs gracefully without it) |
| `CLAUDE_CODE_OAUTH_TOKEN` | LLM news styles (from `claude setup-token`; omit to fall back to plain headlines) |

Data sources: yfinance (version-pinned; row-label drift between releases is real), Twelve Data as price fallback, Finnhub as optional insider fallback, CNBC/MarketWatch/Yahoo Finance feeds for news.

## Documentation

`PROJECT_PLAN.md` is the authoritative spec: scoring formulas, resilience decisions, the LLM risk table and kill switches, and phase history.

## Disclaimer

Personal research tooling. Nothing here is investment advice; the scores are one hobbyist's configurable heuristics over free data sources, with all the accuracy caveats that implies.

## License

MIT, see [LICENSE](LICENSE).
