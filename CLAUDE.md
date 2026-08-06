# ticker-sentinel

Personal, private stock-analysis report service. Single user. Not financial advice.
PROJECT_PLAN.md is the authoritative spec — read it before making changes.

## Commands
- Install: `pip install -e ".[dev]"`
- Run (no email): `python -m sentinel.run --no-email`
- Offline run: `python -m sentinel.run --dry-run`
- Tests: `pytest -q` (tests must never hit the network)
- Green bar: `scripts/checks.sh` (the single definition; CI and the commit guard run this exact script)

## Conventions
- Never emit em dashes (or en dashes) in any user-visible output: email HTML, subject lines, data notes, CLI summaries, LLM prompts/prose. Use commas, colons, parentheses, or hyphens. Missing values render as `n/a`.
- Python 3.12, type hints everywhere, small pure functions for all metric formulas (src/sentinel/indicators/) so they're unit-testable in isolation.
- All external I/O (yfinance, Twelve Data, SMTP) lives in src/sentinel/data/ and deliver.py only. Indicator and scoring code takes plain DataFrames/dataclasses in, values out.
- Every metric formula must match PROJECT_PLAN.md section 5 exactly; scoring weights come from config, never hardcoded.
- Failures degrade, they don't crash: a run should always produce a report, with staleness/missing-data flags instead of exceptions.
- Secrets only via env vars / GitHub Actions secrets. Never write them to files. .env and reports/ are gitignored.
- Pin yfinance and pandas-ta versions in pyproject.toml; field-name mappings from yfinance statements go through the alias layer in data/fundamentals.py.

## Workflow (adopted 2026-08-06)
- One branch per workstream, created with `scripts/new-worktree.sh <branch>`; main is the review inbox. Merge back via PR (opened for owner review, never merged by the agent). Tear down with `scripts/rm-worktree.sh` or the repo `worktree-cleanup` skill.
- `scripts/checks.sh` must be green before every commit. The commit guard (`.claude/hooks/pre-commit-guard.sh`) enforces this and blocks agent commits on main. Deliberate overrides, typed into the commit command itself: `ALLOW_MAIN_COMMIT=1` (e.g. a tiny config fix landing direct), `SKIP_CHECKS=1`. If you change `checks.sh`, update the Commands list above in the same commit.
- Serialized shared state: `data/cache/` (parquet fundamentals cache AND `run_history.json`, the cross-run change-detection state) is written by the scheduled Actions job, which commits to main daily; the bot holds the pen (registry: `tasks/todo.md`). Never modify `data/cache/` contents on a workstream branch; local real runs write `data/cache/run_history.json`, so discard that change before committing.

## Gotchas
- yfinance statement row labels change between versions — update the alias map, don't scatter string literals.
- HTML email: inline CSS + tables only (Gmail strips <style> in some contexts); images as CID attachments.
- GitHub runner IPs are sometimes rate-limited by Yahoo — that's what the Twelve Data fallback and cache are for; don't "fix" it by adding retries alone.
- TTM = sum of last 4 quarters; require all 4 or flag insufficient_data. Never annualize a single quarter silently.
