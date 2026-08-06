# ticker-sentinel

Personal, private stock-analysis report service. Single user. Not financial advice.
PROJECT_PLAN.md is the authoritative spec — read it before making changes.

## Commands
- Install: `pip install -e ".[dev]"`
- Run (no email): `python -m sentinel.run --no-email`
- Offline run: `python -m sentinel.run --dry-run`
- Tests: `pytest -q` (tests must never hit the network)

## Conventions
- Never emit em dashes (or en dashes) in any user-visible output: email HTML, subject lines, data notes, CLI summaries, LLM prompts/prose. Use commas, colons, parentheses, or hyphens. Missing values render as `n/a`.
- Python 3.12, type hints everywhere, small pure functions for all metric formulas (src/sentinel/indicators/) so they're unit-testable in isolation.
- All external I/O (yfinance, Twelve Data, SMTP) lives in src/sentinel/data/ and deliver.py only. Indicator and scoring code takes plain DataFrames/dataclasses in, values out.
- Every metric formula must match PROJECT_PLAN.md section 5 exactly; scoring weights come from config, never hardcoded.
- Failures degrade, they don't crash: a run should always produce a report, with staleness/missing-data flags instead of exceptions.
- Secrets only via env vars / GitHub Actions secrets. Never write them to files. .env and reports/ are gitignored.
- Pin yfinance and pandas-ta versions in pyproject.toml; field-name mappings from yfinance statements go through the alias layer in data/fundamentals.py.

## Gotchas
- yfinance statement row labels change between versions — update the alias map, don't scatter string literals.
- HTML email: inline CSS + tables only (Gmail strips <style> in some contexts); images as CID attachments.
- GitHub runner IPs are sometimes rate-limited by Yahoo — that's what the Twelve Data fallback and cache are for; don't "fix" it by adding retries alone.
- TTM = sum of last 4 quarters; require all 4 or flag insufficient_data. Never annualize a single quarter silently.
