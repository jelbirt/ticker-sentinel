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
- `history-backfill` (branch `history-backfill`, worktree
  `../ticker-sentinel.history-backfill`): one-time SEC EDGAR backfill tool
  deepening the committed cache to >= 12 quarters per ticker behind a
  verification gate, plus the R40-trend warm-up disclosure. Spec:
  `tasks/spec-history-backfill.md`. Does NOT write `data/cache/` on this
  branch; the `--apply` run is a separate owner-approved post-merge commit.
- `news-quality` (branch `news-quality`, worktree
  `../ticker-sentinel.news-quality`): revive the dead company-name matching
  path by normalizing yfinance legal names to their trading name.

  One branch per workstream via `scripts/new-worktree.sh <branch>`; main is the
  review inbox; merge back via PR.

## Done
- `run-integrity` (2026-08-15, merged as PR #7, worktree torn down): audit
  fixes 1, 3, 7 shipped: TTM windows anchor on the newest complete quarter
  (bounded 2-column skip, "scored as of" note), windows spanning a >120-day
  column gap degrade to insufficient data, degraded runs are reported but
  neither diffed nor saved as baseline (`changes.baseline_min_fraction`),
  diffs compare like for like across a basis change, and universe_removed
  rows carry the unscored reason.
- `news-matching` (2026-08-15, merged as PR #5, worktree torn down): 1-2 char
  tickers now need explicit symbol context, so macro headlines ("U.S.",
  "S&P 500") stop being attributed to SentinelOne; the narrative fabrication
  guard is split from the coverage check; rendered hrefs are restricted to an
  http(s) allowlist; the headless claude call runs with no tools.
- `ops-alerting` (2026-08-15, merged as PR #4, worktree torn down): silent run
  failures made loud: a requested email that does not send exits non-zero, and
  daily-report.yml gained a failure-alert issue, a concurrency group,
  artifact/cache steps that survive that non-zero exit, a rebase-and-retry
  cache push, and step-scoped secrets with no raw input interpolation.
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
- Weekly watchlist candidate refresh: live since 2026-08-15. A digest issue
  opens Saturdays (label `watchlist-refresh`, owner-assigned); do the refresh
  from the issue. Bench: WDAY, SHOP, TWLO, ZM (a structured `bench:` key in
  watchlist.yaml). First 3 refreshes are calibration rounds; refresh #3's
  checklist adds the automate-vs-manual decision (option 3). Refresh #1
  opened as issue #6 on 2026-08-15 and is awaiting the owner.
- (done) First scheduled run after merge created `data/cache/run_history.json`
  on 2026-08-07; change detection live since 2026-08-08.
