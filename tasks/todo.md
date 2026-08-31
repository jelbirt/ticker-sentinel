# ticker-sentinel: workstream state

This file has a pen: the COORDINATING SESSION opens and closes workstream
entries here as small direct-to-main commits (adopted 2026-08-16). Workstream
branches must not edit this file; parallel branches editing the same bullets
made every second merge conflict here.

## Pen registry (serialized shared state)
- `data/cache/` (committed parquet fundamentals cache AND `run_history.json`,
  the cross-run change-detection state added in Phase 4): pen held by the
  **scheduled GitHub Actions job**, which commits `cache: refresh fundamentals`
  straight to main after scheduled runs (the existing `git add data/cache` step
  picks up both). Workstream branches must not modify `data/cache/` contents;
  expect to rebase over bot commits before merging. Local real runs write
  `run_history.json`; discard that change rather than committing it.

## Active workstreams
- none open.

  One branch per workstream via `scripts/new-worktree.sh <branch>`; main is the
  review inbox; merge back via PR.

## Accepted residuals (2026-08-14 audit; considered and deliberately not fixed)
- Neighbour rank shifts on partial basis-flip days: a same-basis name's rank
  row can move mechanically when a neighbour's basis flips; suppressing it
  would hide real moves. Accepted as informational noise.
- `universe_removed` renders a bare "dropped from scored universe" when a
  ticker vanishes with no scorecard to carry an unscored reason. Accepted:
  the reason is only ever known when a scorecard exists.
- One config test pins `week_window_runs == 5` as a literal. Accepted as a
  deliberate config-value pin; the retention test asserts the derived
  relation instead.

## Done
- `rotation-round-2` (2026-08-23, merged as PR #23, worktree torn down): the
  refresh #2 calibration round recorded in SPEC 7.0.1 (issue #22, closed with
  no watchlist changes). All five attention names held: fundamentals,
  revisions and short interest are identical across a window by construction
  (they only step at the weekly refresh boundary), so the whole composite move
  was technical and the bench did not fall with it. Three rubric lines added:
  a zero decay-gate hit count is silence rather than reassurance for a name
  still in `r40_trend` warm-up; an in-window composite delta is always
  technical; the attention list detects change, not level. The decay gate was
  investigated and deliberately left unchanged: it was structurally unfirable
  universe-wide until the EDGAR backfill fed the 2026-08-18 run (0 of 20 names
  had an `r40_trend`, now 11 of 20), so its zero hit count is a warm-up plus a
  quiet month, not a defect. Round-1 open item closed: TEAM was the cache gap,
  not alias drift. Carried to refresh #3: every attention name qualified via
  the composite-drop leg that round 1 discounts while the gate leg contributed
  nothing, so the rubric as written leaves nothing actionable, which the
  automate-vs-manual call has to resolve. Watch item logged, not acted on: S
  sits at the `fundamental_score` 0.0 clamp, so its decay is censored.
- `ops-hygiene-3` (2026-08-16, merged as PR #19, worktree torn down): the
  audit backlog closer, ending the 2026-08-14 audit list. `constraints.txt`
  pins the transitive dependency set for every install path (the three
  workflows, CI, local setup, worktree venvs; regenerate per its header
  comment when upgrading deps). `report.timezone` is wired display-only into
  the report header ("built HH:MM ZONE"); persisted run dates, directory
  naming and change detection are provably unaffected, and an unloadable
  zone degrades to UTC with a data note. MNDY's skip reason now names the
  real blocker in all four sites: 20-F/6-K filer, XBRL facts annual and
  half-year only, no 3-month periods (per the 2026-08-16 EDGAR
  investigation; owner accepted the warm-up path, no 6-K parser).
- `guard-escape-anchor` (2026-08-16, merged as PRs #17 and #18, worktree torn
  down; #18 re-landed the content a stacked merge base made #17 miss): the
  last escape-detection hole in `.claude/hooks/pre-commit-guard.sh`. An
  override (`ALLOW_MAIN_COMMIT=1`, `SKIP_CHECKS=1`) now counts only in
  command-prefix position, quoted spans are masked before the scan, heredoc
  bodies are dropped unconditionally, and every ambiguous case is decided
  toward NOT escaping. Merely naming an escape in a commit message no longer
  switches the guard off.
- `commit-guard-attribution` (2026-08-16, merged as PR #16, worktree torn
  down): the guard blocked worktree commits as main commits whenever shlex
  could not tokenize the message quoting; heredoc bodies are now stripped
  before parsing and an unparseable command has its `cd`s replayed so the
  commit is judged against the directory it actually runs in. Adds
  `tests/test_pre_commit_guard.py`.
- `rotation-evidence` (2026-08-16, merged as PR #15, worktree torn down):
  bench shadow-scoring, structurally quarantined (never in ranking, diffs,
  alerts, the baseline gate or the watchlist median) and persisted under a
  new sibling `bench` key in `run_history.json` at schema version 1
  (additive; the key first appears in the wild with the next scheduled bot
  run). Digest rotation groundwork: coverage-gap and decay-streak counters,
  data-quality vs business flag split, `--json` twin uploaded as a
  weekly-refresh artifact, `retention_runs` 12 -> 25 (the one authorized
  watchlist.yaml line).
- `ops-hygiene-2` (2026-08-16, merged as PR #14, worktree torn down):
  weekly-refresh failure alerting on the daily-report pattern, the LLM news
  prompt now fits the char cap by construction (per-ticker round-robin trim,
  fail-open when even one headline each cannot fit), and the rotation
  promotion step (seed plus backfill) written into SPEC 7.0 and the digest
  owner checklist.
- `twelvedata-check` (2026-08-16, merged as PR #13, worktree torn down): the
  Twelve Data fallback now requests `adjust=all` (its default is split-only
  while yfinance serves split and dividend adjusted; verified empirically on
  AAPL) and honours the run's period depth (`--deep` 2y maps to 520 bars,
  was hardcoded 260). The degradation note states the basis as requested,
  not guaranteed, since Twelve Data silently ignores unsupported adjust
  values.
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
  watchlist.yaml). Calibration complete: rounds 1-3 (issues #6, #22, #24) all
  closed with no changes; the option-3 decision (2026-08-31, SPEC 7.0.1 round
  3) is STAY MANUAL, revisit when the remaining warm-up names go
  r40_trend-live or the decay gate first fires. Standing watch items for
  refresh #4: GTLB (warm-up ends after 2026-09-01 earnings; does the rank
  slide survive the boundary), S (level-not-change gap), and whether to add a
  level rule to the rubric.
- (done) First scheduled run after merge created `data/cache/run_history.json`
  on 2026-08-07; change detection live since 2026-08-08.
