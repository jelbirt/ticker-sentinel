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
- [x] T11 real-run verification, two-run state round-trip (verified 2026-08-06
      in an isolated SENTINEL_ROOT: no-prior line on run 1, quiet-day diff vs
      backdated baseline on run 2, 2-entry state file, sections render, no
      dashes. Finding: CFLT and CYBR return "possibly delisted" from Yahoo and
      degrade to insufficient_data rows; owner should approve bench swaps.)
- [x] T12 PR #1 open; review pass done, 4 findings fixed in follow-up commit
      (state-file resilience, week-window off-by-one, double-counted signal,
      future-proofed dry-run test). CI checks missing on PR #1: root cause was
      the 2026-08-06 GitHub Actions major outage (confirmed on githubstatus);
      no ci.yml change needed, re-trigger after the incident resolves. CFLT and
      CYBR (delisted post-acquisition) swapped for bench names FTNT and ESTC,
      owner-approved 2026-08-06.

## Next gates (owner)
- Variety + deterioration feature: spec APPROVED 2026-08-06; plan approval
  pending; then PR review.
