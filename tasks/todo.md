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
- `weekly-refresh-digest` (started 2026-08-14): Phase 4.1, Saturday workflow
  opens a watchlist-refresh digest issue from committed run history (owner
  decision stays manual; first 3 refreshes calibrate the rotation rubric).

## Done
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
