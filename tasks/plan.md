# Plan: variety-deterioration (from SPEC.md, approved 2026-08-06)

Branch `variety-deterioration`. One commit per task unless noted; `scripts/checks.sh`
green before every commit; no em/en dashes in user-visible output; review boundary is
the PR (no per-commit review passes). Owner gates: this plan, then the PR.

Dependency graph (arrows = "needs"):

```
T2 config block
  -> T4 diff engine -> T5 deterioration rows -> T6 report sections -> T7 run wiring
T3 snapshot + history I/O -> T4, T7
T8 twelvedata pacing (independent)
T9 watchlist +12 (independent; after T7 so first real run exercises everything)
T10 docs (last code-adjacent commit)
T11 real-run verification (no commit) -> T12 PR + review pass
```

## T1. Land planning docs
Commit SPEC.md, tasks/plan.md, tasks/todo.md updates (registration + task list).
- **Accept**: docs on branch; checks green (no code touched).

## T2. Config: `changes` block
`ChangesConfig` dataclass in `src/sentinel/config.py` with spec section 6 defaults;
parse optional `changes:` from watchlist.yaml; expose via `Config`.
- **Accept**: absent block yields defaults; partial block overrides only given keys;
  `test_config.py` covers both; no threshold literals anywhere downstream.

## T3. Snapshot dataclasses + history I/O
`report/changes.py`: `TickerSnapshot`, `RunSnapshot`, `snapshot_from_scorecards()`
(captures composite, score F/T, rank under configured ranking, r40_fcf, r40_trend,
trend_state, crosses, flags, valuation, net_revisions_30d, short_pct_float,
shares_short; None -> null policy). `data/history.py`: `load_history()`,
`save_run()` with retention prune + same-date replace, corruption -> (None, note).
- **Accept**: round-trip test; retention at 12; same-date replace idempotent;
  corrupt file degrades with note; `cache.prune()` test proves `run_history.json`
  survives; `.gitignore` verified to not exclude it; stable sorted-key output.

## T4. Diff engine
`diff_runs(current, prior, week_ago, changes_cfg) -> ChangeSet`: all nine change
types from spec section 3.2, thresholds from config, quiet-day flag, no-prior case.
- **Accept**: one test per change type at threshold and just below; None fields
  never fabricate deltas; universe add/drop detected; quiet day == zero changes;
  reasons/details strings dash-free (scan test).

## T5. Deterioration rows
`deterioration_rows(...)`: six negative signals (spec section 4), `min_signals`
gate, `deteriorating()` sufficiency rule; `deteriorating()` threshold moves to
config (same default -0.10); reason strings extend `weakness_reason()` style.
- **Accept**: each signal unit-tested; 1-signal ticker excluded unless
  `deteriorating()`; combo ticker included with full reason line; builder's
  existing weak-table behavior unchanged (`test_report.py` still green).

## T6. Report sections (template + builder)
`report.html.j2`: "What changed today" table + "Deterioration watch" subsection in
D4 order (after market strip, before strong performers); `build_context()` accepts
`change_set` / `deterioration` and renders quiet-day single line; section omitted
when no deterioration; red-accent inline CSS, Gmail-safe tables.
- **Accept**: golden/report tests for populated, quiet, and no-prior variants;
  em-dash scan covers new sections; existing sections unchanged below the new ones.

## T7. Run wiring + fixture state + dry-run end-to-end
`run.py`: load history -> snapshot -> diff -> context -> save per spec 2.3 write
rules (dry-run reads `src/sentinel/fixtures/state/run_history.json`, writes
nothing; `--tickers` subset skips with note; real runs append/replace by date).
New committed fixture history (6+ runs: CHRL decays, ALFA improves, BRVO quiet).
- **Accept**: `python -m sentinel.run --dry-run` renders both sections with every
  change type firing at least once across the fixture set; integration test
  asserts section presence + subset-skip note + dry-run leaves `data/cache/`
  untouched (tmp-path guard).

## T8. Twelve Data fallback pacing
`data/prices.py`: when >8 symbols need fallback, pace requests to at most 8/min;
unpaced below that (today's behavior preserved for the common case).
- **Accept**: unit test with mocked HTTP + mocked sleep asserts pacing kicks in
  only >8 missing and recovered symbols still merge; notes unchanged.

## T9. Watchlist expansion (+12, owner-approved)
Add S, RBRK, CYBR, OKTA, DT, CFLT, GTLB, IOT, MNDY, HUBS, NOW, PLTR to
`config/watchlist.yaml` with `tags: [software, r40]` (approved 2026-08-06).
- **Accept**: config loads 20 r40 names; checks green (offline: no cache files for
  new names is fine; first real run fetches and caches them with staleness notes).

## T10. Docs: PROJECT_PLAN.md + CLAUDE.md pen wording
Section 7 report order; section 11 Phase 4 entry (change emphasis + deterioration
+ state file + weekly rotation process); section 12 risk rows (state-file
corruption/growth; Twelve Data per-minute cap; larger-universe Yahoo rate
pressure); tasks/todo.md + CLAUDE.md pen registry wording covers
`run_history.json`.
- **Accept**: docs match shipped behavior; no em/en dashes introduced.

## T11. Real-run verification (no commit; ⛳ checkpoint)
From the worktree: `python -m sentinel.run --no-email` twice on consecutive
invocations; verify report renders both sections, second run diffs against the
first (state round-trip), new watchlist names fetch or degrade gracefully. Then
restore tracked `data/cache/` files and delete the locally created
`run_history.json` + `*.signals.json` (pen rule: nothing under `data/cache/`
lands from this branch).
- **Accept**: two-run round-trip observed; `git status` clean of `data/cache/`
  changes afterward; findings recorded in the PR body.

## T12. PR + post-open review pass
Push, open PR (decisions-first template), run the code-review pass over the full
PR diff, address findings with follow-up commits, CI green. Hand to owner.
- **Accept**: PR open, review findings fixed or explicitly waived with reasons,
  CI green, owner notified. Owner merges (never the agent).
