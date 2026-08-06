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
- `variety-deterioration` (worktree `../ticker-sentinel.variety-deterioration`,
  started 2026-08-06): day-over-day change emphasis, deterioration detection,
  committed run-history state file, watchlist expansion proposal. Spec: SPEC.md
  on the branch. Adds code and fixtures only; never writes the live
  `data/cache/` contents (bot pen).

One branch per workstream via `scripts/new-worktree.sh <branch>`; main is the
review inbox; merge back via PR.

## variety-deterioration task list (tasks/plan.md has acceptance criteria)
- [x] T1 land planning docs (SPEC.md, plan.md, todo.md)
- [x] T2 config `changes` block
- [x] T3 snapshot dataclasses + history I/O (data/cache/run_history.json)
- [x] T4 diff engine (what-changed ChangeSet)
- [x] T5 deterioration rows (min-signals + deteriorating() sufficiency)
- [x] T6 report sections: What changed today + Deterioration watch
- [x] T7 run.py wiring + fixture state + dry-run end-to-end
- [x] T8 Twelve Data fallback pacing (8/min free-tier cap)
- [x] T9 watchlist +12 (owner-approved 2026-08-06)
- [x] T10 docs: PROJECT_PLAN.md sections 7/11/12, pen wording
- [ ] T11 real-run verification, two-run state round-trip (no commit)
- [ ] T12 PR + post-open review pass (owner merges)

## Next gates (owner)
- Variety + deterioration feature: spec APPROVED 2026-08-06; plan approval
  pending; then PR review.
