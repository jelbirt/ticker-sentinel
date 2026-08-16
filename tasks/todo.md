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
- `twelvedata-check` (opened 2026-08-16): audit of the Twelve Data price
  fallback against the yfinance primary. Two divergences found and fixed in
  `src/sentinel/data/prices.py`: the request never asked for an adjustment
  basis, so it took Twelve Data's `adjust=splits` default while yfinance runs
  `auto_adjust=True` (split and dividend adjusted); and the bar count was
  hardcoded at 260, so a `--deep` run (2y) recovered half the history it asked
  for. Scope is `prices.py` plus `tests/test_prices.py`; no cache, config or
  `run.py` changes.

  One branch per workstream via `scripts/new-worktree.sh <branch>`; main is the
  review inbox; merge back via PR.

## Done
- `backfill-amendment` (2026-08-15, merged as PR #11, worktree torn down;
  final apply commit `4cd9d67`): Amendments 1+2 to the backfill spec, plus
  three main-session verification fixes (tag precedence on derived quarters,
  gate-arbitrated composite capex base). Amendment 2's diluted_shares
  exclusion satisfies the share-count-guard standing requirement below. The
  apply accepted 23 of 24 (MNDY structurally excluded), +159 quarters, 135
  holes filled, TEAM restored to pre-2621e02 first; 11 names r40_trend-live
  and 18 of 24 on true TTM growth as of the apply, remainder self-heals as
  quarters accumulate.
- `share-count-guard` (2026-08-15, merged as PR #12, worktree torn down):
  `diluted_shares` integrity at two layers, after investigating the PR #10
  dry-run CRWD/NOW mismatch and finding the cache CORRECT (CRWD split 4:1 on
  2026-07-02, NOW 5:1 on 2025-12-18; yfinance restates every served quarter
  and auto-adjusted prices agree, so those cached values must never be
  rescaled). Merge time: cached share history is rebased by the factor the
  overlapping quarters agree on, so a split cannot leave the row half
  restated. Read time: cells outside 0.33x to 3x of shares outstanding (new
  meta key) are dropped, and a >1.5x neighbour step keeps the side closer to
  shares outstanding, dropping every reading when no reference can arbitrate.
  The scrub never touches the cache, so a false positive cannot destroy
  accumulated history. Related data fix landed direct to main as `b33044c`:
  PANW's 4 pre-split cells from the `2621e02` backfill (2023-10 back to
  2022-10) rescaled x2, verified per cell against SEC filings. Standing
  requirement for any future backfill apply: split-adjust `diluted_shares` or
  exclude the field per ticker, since the overlap gate cannot see basis
  breaks beyond its window.
- `hygiene` (2026-08-15, merged as PR #9, worktree torn down): audit items 3,
  4, 5, 7, 8, 9: test/config decoupling, unknown-config-key warnings, digest
  coverage false positive, run-history schema-version guard, market-holiday
  note, and the docs/packaging nits.
- `news-quality` (2026-08-15, merged as PR #8, worktree torn down): the dead
  company-name matching path revived by normalizing yfinance legal names to
  their trading name.
- `history-backfill` (2026-08-15, merged as PR #10, apply commit `2621e02`
  landed, worktree torn down): one-time SEC EDGAR backfill tool deepening the
  committed cache behind a per-ticker verification gate, plus the R40-trend
  warm-up disclosure and the bench keep-set fix. Spec:
  `tasks/spec-history-backfill.md`. The live apply run accepted 6 tickers and
  rejected 17; the follow-up findings are the `backfill-amendment` workstream
  above.
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
