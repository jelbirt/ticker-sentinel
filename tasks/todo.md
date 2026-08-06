# ticker-sentinel: workstream state

## Pen registry (serialized shared state)
- `data/cache/` (committed parquet fundamentals cache): pen held by the
  **scheduled GitHub Actions job**, which commits `cache: refresh fundamentals`
  straight to main after scheduled runs. Workstream branches must not modify
  `data/cache/`; expect to rebase over bot commits before merging.

## Active workstreams
- (none yet). One branch per workstream via `scripts/new-worktree.sh <branch>`;
  main is the review inbox; merge back via PR.

## Next gates (owner)
- Variety + deterioration feature (planned): spec approval, then plan approval,
  then PR review.
