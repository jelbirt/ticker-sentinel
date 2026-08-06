# ticker-sentinel: workstream state

## Pen registry (serialized shared state)
- `data/cache/` (committed parquet fundamentals cache): pen held by the
  **scheduled GitHub Actions job**, which commits `cache: refresh fundamentals`
  straight to main after scheduled runs. Workstream branches must not modify
  `data/cache/`; expect to rebase over bot commits before merging.

## Active workstreams
- `variety-deterioration` (worktree `../ticker-sentinel.variety-deterioration`,
  started 2026-08-06): day-over-day change emphasis, deterioration detection,
  committed run-history state file, watchlist expansion proposal. Spec: SPEC.md
  on the branch. Adds code and fixtures only; never writes the live
  `data/cache/` contents (bot pen).

One branch per workstream via `scripts/new-worktree.sh <branch>`; main is the
review inbox; merge back via PR.

## variety-deterioration task list (tasks/plan.md has acceptance criteria)
- [ ] T1 land planning docs (SPEC.md, plan.md, todo.md)
- [ ] T2 config `changes` block
- [ ] T3 snapshot dataclasses + history I/O (data/cache/run_history.json)
- [ ] T4 diff engine (what-changed ChangeSet)
- [ ] T5 deterioration rows (min-signals + deteriorating() sufficiency)
- [ ] T6 report sections: What changed today + Deterioration watch
- [ ] T7 run.py wiring + fixture state + dry-run end-to-end
- [ ] T8 Twelve Data fallback pacing (8/min free-tier cap)
- [ ] T9 watchlist +12 (owner-approved 2026-08-06)
- [ ] T10 docs: PROJECT_PLAN.md sections 7/11/12, pen wording
- [ ] T11 real-run verification, two-run state round-trip (no commit)
- [ ] T12 PR + post-open review pass (owner merges)

## Next gates (owner)
- Variety + deterioration feature: spec APPROVED 2026-08-06; plan approval
  pending; then PR review.
