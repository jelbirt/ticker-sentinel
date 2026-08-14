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
- `audit-fixes` (opened 2026-08-14, worktree `../ticker-sentinel.audit-fixes`):
  top-5 findings from the 2026-08-14 project audit: partial-quarter TTM
  anchoring, r40_trend warm-up disclosure, degraded-run diff/baseline guards,
  short-ticker news matching, email-failure exit code + run-failure alerting.
  Code/tests/docs only; no data/cache changes (bot keeps the pen). Registry
  entry rides this branch (direct-to-main registry commit was not available
  this session).

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
