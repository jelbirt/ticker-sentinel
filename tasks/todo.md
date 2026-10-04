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
- `fix/edgar-capex-addend` (opened 2026-10-04, PR #35): an addend filed only
  cumulatively (HUBS 10-K-only intangibles, OKTA nine-month 0) no longer
  drops the capex quarter; HUBS and OKTA go r40_trend-live. Needs the
  owner-gated post-merge `python -m sentinel.backfill --apply` (15 cells).
  FTNT's holes (10-K base tag switch) stay open, out of scope.
- `rotation-round-8` (opened 2026-10-04, PR #36): refresh #8 (issue #34),
  ESTC to the bench, ADSK promoted (already seeded, no apply needed).

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
- `rotation-round-7` (2026-09-29, merged as PR #33, worktree torn down):
  refresh #7 (issue #31, closed). No watchlist change; bench refreshed from
  an in-memory screen of 28 candidates: ADSK, APPF, GWRE in (all
  r40_trend-live, pass r40_fcf, 18 to 23 percent growth), TWLO out (fails
  r40_fcf on corrected data). ZS held (passes r40, low on penalties); ESTC
  (fails r40) not swapped because no bench name entered above the bottom 3,
  decided at round 8 against the new bench. Four rubric lines added in SPEC
  7.0.1. Post-merge apply `3001feb` seeded the three adds (7 to 16 quarters
  each).
- `fix/edgar-revenue-tag` (2026-09-29, merged as PR #32, worktree torn
  down): the backfill's EDGAR revenue tag choice took a pre-2018 `Revenues`
  series over the live contract-revenue tag, so WDAY, TWLO, MDB, HUBS and
  NOW never got revenue history; the most recent deriving tag now wins.
  Apply commit `3001feb` (with the round-7 bench seeds, 7 accepted, 0
  rejected) filled 41 revenue cells for WDAY, MDB, HUBS, NOW with no
  existing value changed (TWLO left out, dropped from the bench). Rescored:
  MDB 44.1 and NOW 63.2 go r40_trend-live (coverage 13 to 15 of 20), WDAY
  24.1 live, HUBS 35.8 still insufficient_history on capex holes (open
  follow-up: missing capex quarters, mostly fiscal Q4, also block OKTA,
  FTNT, TWLO).
- `digest-persistently-weak` (2026-09-20, merged as PR #30, worktree torn
  down): the digest computes the level-rule "persistently weak" table (bottom
  N fundamental scores on the last run of N consecutive full windows,
  r40_trend-live with no data-quality flag at each, r40_fcf pass/fail against
  the shared `R40_BAR`), and the bench table carries the fundamental leg.
  Config keys `changes.weak_bottom_n` and `changes.weak_windows` (both 3).
  Rebuilt on the 2026-09-19 history it matched the round-6 hand read line
  for line. Review fix landed: bench rows pair last observed fundamentals
  with the last observed composite. Left cosmetic: the parent heading still
  reads "(persistent decay)" above a row that is not decay.
- `rotation-round-6` (2026-09-20, merged as PR #29, worktree torn down): the
  refresh #6 round (issue #28, closed) and the first swap under the level
  rule: S demoted to the bench (reversible, its cache history survives the
  prune), SHOP promoted; MNDY and HUBS held as technical noise on flat
  fundamentals; ESTC surfaced and held. Window membership fixed as bottom 3
  on the window's LAST run, and the row must print `r40_fcf` pass/fail (ZS
  is bottom 3 on penalties with a passing r40). SHOP backfill dry run: 10 of
  10 quarters already cached, EDGAR holds nothing older, no post-merge
  `--apply`; `r40_trend` goes live around 2027-02. Coverage 13 of 20 live
  until then. Stay manual.
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
  from the issue. Bench: WDAY, ZM, S, ADSK, APPF, GWRE (a structured `bench:`
  key in watchlist.yaml). Calibration complete: rounds 1-5 (issues #6, #22, #24, #25, #26)
  all closed with no changes; the option-3 decision (2026-08-31, SPEC 7.0.1
  round 3) is STAY MANUAL, reaffirmed 2026-09-17 (round 5: 14 of 20
  r40_trend-live, gate 0 hits in 25 runs), revisit when the remaining
  warm-up names go r40_trend-live or the decay gate first fires. Round 6
  (issue #28, 2026-09-19) made the first swap: S out to the bench, SHOP in
  (PR #29). Round 7 (issue #31, 2026-09-26) read the computed level table
  (it matched the expectation: ESTC and ZS qualify), held ZS as a
  penalty case and refreshed the bench instead of swapping ESTC (PR #33).
  Standing watch items for refresh #8 (Sat 2026-10-03): decide ESTC against
  the shadow-scored bench on the fundamental leg (ADSK 73.6 highest, APPF
  58.8 cleanest; the replacement must enter above the bottom 3), check the
  new bench names read r40_trend-live in the digest (seeded by `3001feb`),
  MDB's first live windows (44.1, leaves the bottom), RBRK at r40_trend
  -0.035, S on the bench, ESTC's 2026-11-19 report as the fallback
  checkpoint.
- (done) First scheduled run after merge created `data/cache/run_history.json`
  on 2026-08-07; change detection live since 2026-08-08.
