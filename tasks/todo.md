# ticker-sentinel: workstream state

## Pen registry (serialized shared state)
- `data/cache/` (committed parquet fundamentals cache AND `run_history.json`,
  the cross-run change-detection state added in Phase 4): pen held by the
  **scheduled GitHub Actions job**, which commits `cache: refresh fundamentals`
  straight to main after scheduled runs (the existing `git add data/cache` step
  picks up both). Workstream branches must not modify `data/cache/` contents;
  expect to rebase over bot commits before merging. Local real runs write
  `run_history.json`; discard that change rather than committing it.

## Active workstreams
- `ops-alerting` (worktree `../ticker-sentinel.ops-alerting`): make silent
  failures loud: a requested email that does not send exits non-zero, and
  daily-report.yml gains a failure-alert issue, a concurrency group,
  artifact/cache steps that survive that non-zero exit, a rebase-and-retry
  cache push, and step-scoped secrets with no raw input interpolation.
- `news-matching` (worktree `../ticker-sentinel.news-matching`): fix the short
  ticker collision in news matching (`\bS\b` matched "U.S." and "S&P 500", so
  every macro headline was attributed to SentinelOne), split the narrative
  fabrication guard from the coverage check, restrict feed link hrefs to
  http(s), and run the headless claude call with no tools.
- `run-integrity` (opened 2026-08-14, worktree `../ticker-sentinel.run-integrity`):
  audit fixes 1, 3, 7: partial-quarter TTM anchoring (bounded), quarter-gap
  contiguity guard, degraded-run diff/baseline guards with basis-aware
  comparisons, unscored-reason disclosure. Code/tests/docs only; no
  data/cache changes (bot keeps the pen). Siblings ops-alerting and
  news-matching run in parallel; registry lines will conflict trivially.

  One branch per workstream via `scripts/new-worktree.sh <branch>`; main is the
  review inbox; merge back via PR.

## Done
- `weekly-refresh-digest` (2026-08-14, merged as PR #2, worktree torn down):
  Phase 4.1 shipped: sentinel.digest module + Saturday weekly-refresh workflow
  opening the owner-assigned watchlist-refresh issue; bench now a structured
  `bench:` key; review findings fixed (degraded-run deltas, calibration label,
  test coupling, workflow concurrency). First digest issue expected Sat
  2026-08-15 12:00 UTC.
- `variety-deterioration` (2026-08-06, merged as PR #1, worktree torn down):
  Phase 4 shipped: run-history state file, What-changed and Deterioration-watch
  sections, watchlist 8 -> 20 (CYBR/CFLT swapped for FTNT/ESTC after
  delisting), Twelve Data pacing. Spec/plan: SPEC.md + tasks/plan.md. Note: PR
  checks were absent due to the 2026-08-06 GitHub Actions outage; local bar was
  green with the same script.

## Next gates (owner)
- Weekly watchlist candidate refresh: once `weekly-refresh-digest` merges, a
  digest issue opens Saturdays (label `watchlist-refresh`, owner-assigned);
  do the refresh from the issue. Bench: WDAY, SHOP, TWLO, ZM (now a structured
  `bench:` key in watchlist.yaml). First 3 refreshes are calibration rounds;
  refresh #3's checklist adds the automate-vs-manual decision (option 3).
- (done) First scheduled run after merge created `data/cache/run_history.json`
  on 2026-08-07; change detection live since 2026-08-08.
