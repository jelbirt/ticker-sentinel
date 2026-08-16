# SPEC: Day-over-day change emphasis, deterioration detection, watchlist expansion

Branch: `variety-deterioration` (worktree `../ticker-sentinel.variety-deterioration`).
Status: APPROVED 2026-08-06 (all design decisions resolved by owner; see section 11).
Next gate: task-plan approval (gate 2), then PR review.
Authoritative spec context: PROJECT_PLAN.md (sections 5, 6, 7 unchanged unless noted here).

## 1. Objective

The daily email currently re-ranks mostly-stable levels, so it reads the same every
morning. This phase makes the report lead with what changed since the prior run and
gives negative changes first-class, symmetric treatment. Three deliverables:

1. **Run-history state file** (foundation): a small, bounded, committed JSON file
   holding the last N runs of per-ticker outputs, because `reports/` is gitignored
   and hosted runners start clean; without committed state a scheduled run cannot
   see yesterday.
2. **"What changed today" section** near the top of the email: score and rank moves,
   flag transitions, R40 trend inflections, trend-state changes and new crosses,
   estimate-revision swings, short-interest deltas. A quiet day renders one line.
3. **"Deterioration watch" subsection**: per-ticker negative-change roll-up with
   one-line reasons, seeded from `deteriorating()` / `weakness_reason()` in
   `src/sentinel/report/builder.py`.

Plus a **watchlist expansion proposal** (owner picks before any config change) and
capacity verification at the larger size.

Existing strong/weak/movers/signals tables stay, but become secondary to the
change-focused sections.

## 2. Run-history state file (foundation)

### 2.1 Location and pen rule
- Path: `data/cache/run_history.json` (**D1: decided**).
  Rationale: the scheduled workflow's existing commit step does `git add data/cache`,
  so the state file is persisted with **zero workflow-file changes**; there is one
  pen (the bot) for one directory. `cache.prune()` ignores the file already (it only
  matches `.parquet` / `.meta.json` / `.signals.json` suffixes); a unit test will pin
  that.
- Pen-rule coexistence: the pen registry entry in `tasks/todo.md` is broadened to
  "data/cache/ including run_history.json: bot holds the pen". Workstream branches
  never commit changes to the live state file; this branch ships only code, fixtures,
  and (optionally) the file's first empty scaffold is NOT committed: the bot creates
  the file on its first scheduled run after merge (absence = "no prior state" note).
- The bot's commit message stays `cache: refresh fundamentals` (message text is
  cosmetic; changing the workflow is out of scope).

### 2.2 Format and schema (D2: decided, single versioned JSON)
Single JSON document, versioned, runs newest-last:

```json
{
  "version": 1,
  "runs": [
    {
      "date": "2026-08-06",
      "run_type": "scheduled",
      "tickers": {
        "CRWD": {
          "composite": 71.2, "score": 68.0, "technical_score": 76.1, "rank": 1,
          "r40_fcf": 0.52, "r40_trend": 0.03,
          "trend_state": "uptrend", "golden_cross": false, "death_cross": false,
          "flags": ["passes_all_r40"], "valuation": "fair",
          "net_revisions_30d": 4,
          "short_pct_float": 0.021, "shares_short": 2000000
        }
      }
    }
  ]
}
```

- Scores stored on their rendered 0-100 scale; ratios stored as fractions
  (consistent with `Scorecard`). Missing values stored as `null`, never omitted
  keys, so diffs distinguish "unknown" from "absent ticker".
- `rank` is the position in the ranked `scored` list (1-based) under the configured
  ranking mode, captured at snapshot time.
- Written with `indent=2` and sorted keys: the file is committed, so diffs must be
  stable and reviewable.
- JSON over parquet/CSV because it is human-inspectable in the repo, diff-friendly,
  schema-flexible (nullable fields), and small (see 2.4).

### 2.3 Write rules
- A real full-universe run (scheduled or ad hoc) **appends or replaces** the entry
  for its date: one entry per calendar date, last run of the day wins, so ad hoc
  re-runs are idempotent and never double-count a day.
- `--dry-run` **never touches** the live file: it reads fixture state
  (`src/sentinel/fixtures/state/run_history.json`) and writes nothing.
- `--tickers` subset runs **skip both read-compare and append**: ranks and deltas
  from a partial universe are not comparable; the report notes
  "change detection skipped (ticker subset)".
- Only scheduled runs are committed by the bot (existing workflow behavior); an ad
  hoc run on a hosted runner writes locally and evaporates. That is acceptable:
  the baseline advances once per scheduled day.
- Corrupt/unreadable state degrades to "no prior state" with a data note, never a
  crash; the next successful run rewrites the file.

### 2.4 Retention (D3: decided, 12 runs; raised to 25 on 2026-08-16)
- Keep the most recent `changes.retention_runs` entries (config; default 25,
  5 weeks at Tue-Sat cadence). Pruned on every write.
- Raised from 12 (about 2.5 weeks) because rotation decisions read back over
  several weekly digest windows, not one: at 12 the third-oldest refresh round
  had already aged out of the file it was supposed to be evidence for.
  Retention and `week_window_runs` stay independent knobs, and the digest
  window is unchanged at 5.
- Size envelope: ~26 tickers x ~14 fields x 25 runs, plus a 4-name bench block
  per run, pretty-printed, is roughly 250-350 KB; still bounded and small next
  to the existing parquet cache.

### 2.5 Module layout
- `src/sentinel/data/history.py`: I/O only. `load_history()`,
  `save_run(snapshot, retention)`, path resolution, corruption handling.
- `src/sentinel/report/changes.py`: pure logic, no I/O (unit-testable in
  isolation, matching the indicators convention). Dataclasses `TickerSnapshot`,
  `RunSnapshot`, `Change`, `ChangeSet`; functions `snapshot_from_scorecards()`,
  `diff_runs(current, prior, week_ago, cfg)`, `deterioration_rows(...)`.
- `src/sentinel/run.py` wires: load -> snapshot -> diff -> context -> save.

## 3. "What changed today" section

### 3.1 Comparison baselines
- **Prior run** = newest history entry with `date < today` (a same-day re-run
  compares against yesterday, not itself).
- **Week-ago run** = entry 4 positions before the prior run, so today vs the
  week-ago run spans exactly `week_window_runs` (5) run-steps, one Tue-Sat week;
  if history is shorter, the oldest entry is used and the section labels the
  actual span ("vs 3 runs ago"). Used only for deterioration windows (section 4).
- First run ever (no prior): section renders "No prior run state yet; change
  detection starts tomorrow." and everything else proceeds.

### 3.2 Change types detected (all thresholds from config, `changes:` block)
| Change | Trigger (defaults) |
|---|---|
| Composite move | abs(composite - prior) >= `score_delta_pts` (3.0) |
| Rank move | abs(rank - prior_rank) >= `rank_delta` (2) |
| Flag set / cleared | any difference in the `flags` set (each named) |
| R40 trend inflection | `r40_trend` sign change, or crossing the `deteriorating_r40_trend` threshold (-0.10) in either direction |
| Trend-state change | `trend_state` transition (e.g. uptrend to mixed) |
| New cross | golden/death cross false -> true vs prior run |
| Estimate-revision swing | abs(net_revisions_30d - prior) >= `revision_swing` (3) |
| Short-interest delta | `shares_short` changed (new FINRA reading) and abs(pct change) >= `short_delta` (0.05) |
| Universe change | ticker appears in / disappears from the scored set |

- Rendered as a compact table (ticker, change, direction arrow, detail), positive
  and negative changes intermixed, sorted by abs(composite move) then ticker.
  Direction arrows use existing ASCII/emoji conventions; **no em/en dashes**.
- Quiet day (zero rows): exactly one line, e.g.
  "Quiet day: no material changes vs the prior run (2026-08-05)." No padding.

## 4. "Deterioration watch" subsection

Symmetric, first-class negative coverage. A ticker is listed when it accumulates
**at least `min_signals` (default 2)** of these negative signals, OR it satisfies the
existing plan-section-6 `deteriorating()` combination (which remains sufficient
alone):

| Signal | Trigger (defaults) |
|---|---|
| 1-run score drop | composite fell >= `score_delta_pts` (3.0) vs prior run |
| Week score drop | composite fell >= `week_drop_pts` (5.0) vs week-ago run |
| R40 trend deeply negative | `r40_trend` < `deteriorating_r40_trend` (-0.10), level, from Scorecard |
| Technical breakdown | new death cross, or trend-state transitioned to downtrend |
| Estimate cuts | net_revisions_30d <= -`revision_cut` (2), or down > up (existing alert rule) |
| Worsening short interest | MoM shares-short rise > `SHORT_MOM_ALERT` (0.20, reused), or short_pct_float rose >= `short_delta` |

- Rendered as its own clearly-visible subsection directly after "What changed
  today": red-accented header, table of ticker, composite (with 1-run and week
  deltas), and a one-line reason string listing every triggered signal.
- Reason strings extend `weakness_reason()` style: short clauses joined by "; ".
- Single-signal events are not lost: they already surface in What-changed or
  Movers; this subsection is reserved for confirmed multi-signal decay so it stays
  scary when it appears.
- Empty state: subsection is omitted entirely (the quiet-day line already covers
  "nothing happened"); the existing weak-performers table still shows relative
  laggards every day.

## 5. Report layout (D4: decided, context first)

Order:
1. Header (date, run type, freshness notes)
2. Market context strip (SPY, median R40)
3. **What changed today** (new)
4. **Deterioration watch** (new, omitted when empty)
5. Strong performers (existing)
6. Weak performers (existing)
7. Movers & alerts, Between-quarter signals, tech-only, news sections (existing order)

PROJECT_PLAN.md section 7 gets the updated ordering; section 11 gains a phase entry
("Phase 4: change emphasis + deterioration detection"); section 12 gains risk rows
(state-file corruption/growth; Twelve Data per-minute cap at larger watchlist).

## 6. Config additions (`config/watchlist.yaml`)

```yaml
changes:
  retention_runs: 25       # history entries kept in data/cache/run_history.json
  week_window_runs: 5      # "week" lookback, in runs (Tue-Sat cadence)
  score_delta_pts: 3.0     # composite move worth reporting (0-100 scale)
  rank_delta: 2            # rank move worth reporting
  revision_swing: 3        # net 30d analyst-revision swing worth reporting
  short_delta: 0.05        # short-interest fractional change worth reporting
  week_drop_pts: 5.0       # week-window composite drop counting as deterioration
  revision_cut: 2          # net downward revisions counting as deterioration
  min_signals: 2           # negative signals needed for Deterioration watch
  deteriorating_r40_trend: -0.10   # existing section-6 threshold, now config-driven
```

All thresholds flow through `Config` (new `ChangesConfig` dataclass with these
defaults; absent block = defaults, consistent with existing config style). Nothing
hardcoded in logic modules. `deteriorating()` keeps its current behavior but reads
its threshold from config.

## 7. Watchlist expansion proposal (owner gate; NO config edits until approved)

Candidates fitting the Rule-of-40 software/growth profile, deliberately spanning
clear passers, borderline names, and contrast cases so ranks actually move:

| # | Ticker | Company | One-line rationale |
|---|---|---|---|
| 1 | S | SentinelOne | Endpoint security, direct CRWD comp, ~30% growth with newly positive FCF |
| 2 | RBRK | Rubrik | Data security, hypergrowth, R40 comfortably above 40 |
| 3 | CYBR | CyberArk | Identity security leader, 25%+ growth, consistent FCF |
| 4 | OKTA | Okta | Identity SaaS, moderated growth but sharp FCF-margin swing (trend test) |
| 5 | FTNT | Fortinet | Network security, slower growth, elite FCF margins keep R40 > 40 |
| 6 | DT | Dynatrace | Observability, direct DDOG comp, balanced growth + margin |
| 7 | ESTC | Elastic | Search/observability, mid-teens growth, improving margins |
| 8 | CFLT | Confluent | Data streaming, ~25% growth, FCF recently inflected positive |
| 9 | GTLB | GitLab | DevSecOps, ~30% growth, margin inflection underway |
| 10 | IOT | Samsara | Connected ops, 30%+ growth, an R40 standout among recent IPOs |
| 11 | MNDY | monday.com | Work management, ~30% growth plus double-digit FCF margin |
| 12 | HUBS | HubSpot | SMB CRM suite, ~20% growth with steady FCF expansion |
| 13 | NOW | ServiceNow | Large-cap workflow SaaS, the most consistent R40 name at scale |
| 14 | WDAY | Workday | HR/finance SaaS, high-teens growth, mid-20s FCF margin |
| 15 | SHOP | Shopify | Commerce platform, re-accelerated 25%+ growth, FCF positive |
| 16 | PLTR | Palantir | Elite growth + margin; will exercise the priced-for-perfection tag |
| 17 | TWLO | Twilio | Single-digit growth but a big FCF swing; exercises the SBC-gap flag |
| 18 | ZM | Zoom | Low growth, fat FCF margin: margin-only R40 contrast name |

**Owner picked (2026-08-06) the recommended 12**: S, RBRK, CYBR, OKTA, DT, CFLT,
GTLB, IOT, MNDY, HUBS, NOW, PLTR; universe goes from 8 to 20 names, all with
`tags: [software, r40]`. The remaining 6 (FTNT, ESTC, WDAY, SHOP, TWLO, ZM) stay
on the bench as first-call swap candidates.

### 7.0 Watchlist rotation process (owner decision, 2026-08-06)
The watchlist is a living list. **Roughly weekly**, the owner asks for a candidate
refresh: using the deterioration/change evidence now in the report, propose swaps
(drop persistent decliners, promote bench or new candidates). Expected churn is
small week to week. This is a process cadence recorded in PROJECT_PLAN.md
(roadmap), not code; every actual `config/watchlist.yaml` edit remains
owner-gated. Cache pruning already cleans up dropped tickers automatically.

**Promotion step (seed and backfill the incoming name).** A promoted bench name
or a new candidate starts with no cached history, so growth (needs 8 quarters)
and `r40_trend` (needs 12) would read `n/a` for roughly a year while the
committed cache deepens 4 quarters per year. The one-time backfill tool closes
that gap, and it is idempotent: re-running it on a name already deep is a no-op
accept. The exact sequence, from `tasks/spec-history-backfill.md` and
`sentinel/backfill.py`:

1. Land the `config/watchlist.yaml` swap through the owner-reviewed PR as usual.
   That branch must not touch `data/cache/`.
2. Dry run, safe on any branch (fetches SEC EDGAR live, writes nothing):
   `python -m sentinel.backfill --dry-run --tickers NEW` (comma separated for
   several names). Read the per-ticker report: ACCEPT with quarters gained, or
   REJECT with the overlap checks that failed.
3. Apply, owner-gated because `data/cache/` is the scheduled bot's pen: run it
   on main after the swap PR merges and land it as its own commit (the pattern
   of apply commits `2621e02` and `4cd9d67`):
   `python -m sentinel.backfill --apply --tickers NEW`. Keep `--tickers` scoped
   to the incoming names: with no `--tickers` the tool sweeps the whole r40
   universe plus the bench and rewrites every parquet that passes.

What decides the outcome:
- A name with no parquet is SEEDED automatically: the tool fetches its current
  yfinance statements to create the overlap the verification gate needs. In
  `--dry-run` that stays in memory; only `--apply` writes it, and the per-ticker
  line says `seeded`.
- The gate is all or nothing per ticker: one field disagreeing beyond 1 percent
  or 100,000 absolute on any overlapping quarter rejects that ticker and writes
  nothing for it. A reject is information, not something to work around.
- Foreign private issuers file no 10-Q XBRL quarterly facts and are skipped a
  priori (MNDY today, in `backfill.SKIPPED`). Promoting one means accepting the
  warm-up gap: say so in the swap PR.
- Standing requirement from the share-count-guard workstream: `diluted_shares`
  stays out of the backfilled field set (Amendment 2), because the overlap gate
  cannot see split-driven basis breaks outside its window.

#### 7.0.1 Rotation rubric (built round by round from the weekly digests)
The first 3 refreshes are calibration rounds: each one records which digest
evidence actually drove a decision and which was noise, so the rubric below is
written from observed rounds rather than guessed up front. From refresh #3 the
checklist adds the decision on automating proposal drafting against this rubric
vs staying manual.

**Working rubric (as of round 1):**
- Act on **decay-gate hit counts**, not on composite deltas. A name with 0 gate
  hits has not shown persistent decay however far its composite moved.
- Before treating an attention-list name as deterioration, check whether its
  flags are **data-quality** (insufficient history, growth from annual, sbc
  inflated) or **business** flags. Data-quality flags are a fetch/coverage
  problem, not a rotation signal.
- A **coverage gap outranks the attention list**: a name that stops being scored
  is either a data failure to chase or a delisting to swap, and both matter more
  than a few points of composite drift.
- **A flat fundamental score against a moving technical score means cached
  fundamentals**, and is worth checking before the name disappears entirely.
- At a 5-run window, raw **change-activity counts are volume, not signal**.

**Round 1 (issue #6, 2026-08-15, window 2026-08-11 to 2026-08-15, 5 runs):**
- Outcome: no changes. DDOG held (0 of 5 gate hits, drop was composite-only,
  flags all data-quality). TEAM held but logged as a data issue: scored stably
  for 5 runs, then unscored on 2026-08-14 and 2026-08-15 while still configured.
- What mattered: the decay-gate hit count, and the coverage line.
- What was noise: change-activity counts (short interest 16, score 10, rank 9),
  and the composite delta on its own.
- Digest gaps found, worth fixing before the automate-vs-manual call:
  1. Coverage is binary. `digest.py` reports "missing from the latest run"
     unless a name is absent from every run in the window, so a 2-run streak
     and a 1-run blip read identically. It should carry a streak count, in
     line with `digest_decay_runs: 2`.
  2. The attention table does not distinguish data-quality flags from business
     flags, which is most of the noise in round 1.
- Open item: the unscored-reason disclosure from PR #7 merged after the
  2026-08-15 run, so the first run to name TEAM's cause is 2026-08-18. Revisit
  at refresh #2.

**What the digest carries as of 2026-08-16** (branch `rotation-evidence`,
answering round-1 gaps 1 and 2 and preparing the refresh #3 decision):

- **Coverage streaks.** Coverage gaps are structured (`CoverageGap`), not
  prose: each carries a consecutive-runs-missing streak counted back from the
  latest run, the dates it spans, and how many runs the name was actually seen
  in. A 1-run blip keeps the old wording; 2 or more names the streak
  explicitly, so TEAM's real 2-run absence can no longer read as noise
  (round-1 gap 1).
- **Decay streaks.** Attention entries carry the longest CONSECUTIVE run of
  decay-gate hits beside the raw hit count. The gate itself is unchanged and
  still counts hits; the streak is what separates persistent decay from the
  same number of scattered hits. A run the name is missing from breaks the
  streak, because an absence is not evidence the gate held.
- **Flag split.** The attention table has separate business-flag and
  data-quality-flag columns (round-1 gap 2). Data quality means
  `insufficient_data`, `insufficient_history`, `growth_from_annual`,
  `stale_fundamentals`: fetch and coverage problems, a to-fix list, never a
  rotation signal. Business means `sbc_inflated`, `high_sbc`, `dilution`,
  `passes_all_r40`, `golden_cross`, `death_cross`. Unclassified flags render
  as business, and a test pins that every `FLAG_` constant in the codebase is
  classified.
- **Bench evidence.** The bench section now carries a table of window-scale
  composite moves (first vs last appearance in the window, the same basis as
  the attention list), read from the `bench` block that shadow-scored runs
  write into `run_history.json`. So "is this candidate better than the name I
  would drop" is now a comparison of two numbers built the same way, rather
  than a guess. Windows predating the feature render a warm-up note, not an
  error, and configured bench names with no snapshots are named.
- **JSON twin.** `python -m sentinel.digest --json PATH` writes the whole
  digest as sorted-key JSON (dates ISO, dataclasses serialized, coverage gaps
  carrying both their fields and their rendered text), and the weekly-refresh
  workflow uploads it as an artifact next to the issue. This is the
  machine-readable substrate the refresh #3 automate-vs-manual decision needs:
  an agent drafting swap proposals against this rubric reads the streaks and
  the flag split as data instead of parsing a markdown table.
- **Retention.** `changes.retention_runs` 12 -> 25 (section 2.4), so the file
  holds five digest windows rather than two and a half.

The digest remains read-only over history: no network, no scoring, no cache
writes.

### 7.1 Capacity verification at ~22-28 tickers
- **Batched price pull**: unchanged, still one yfinance `download()` call for the
  whole universe plus SPY. OK.
- **16-quarter cache cap**: per-ticker parquets are a few KB each; ~26 tickers
  roughly triples `data/cache/` size, still trivially small. OK.
- **Watchlist pruning**: `cache.prune()` is watchlist-driven and unaffected. OK.
- **Twelve Data free tier: NOT OK as-is in the worst case.** ~800 calls/day is fine,
  but the free tier also caps at 8 requests/min, and the fallback loop in
  `data/prices.py` is sequential and unthrottled. Today (8 names) a full-Yahoo
  outage stays under the cap; at ~26 names most fallback calls would be rejected.
  In scope: pace fallback requests (sleep so at most 8/min) only when more than 8
  symbols are missing; worst case adds ~3 minutes to a degraded run. Notes still
  report anything unrecovered.
- **Per-ticker Yahoo calls** (fundamentals weekly refresh, signals every run) scale
  linearly; existing degradation paths (cache + staleness flags, skipped-signal
  notes) already handle rate-limiting. Accepted risk, noted in PROJECT_PLAN
  section 12.
- **News**: per-ticker RSS fetches scale linearly (bounded by `max_age_hours` /
  `max_per_ticker`); the LLM prompt grows with ticker count but the existing
  truncation and output caps hold. The full-ticker-coverage rule at 26 names will
  produce terser per-name coverage; acceptable, no change.
- **Email size**: signals and deep-grid tables grow to ~26 rows; still HTML tables,
  no layout change. `top_n`/`bottom_n` unchanged (owner may retune later).

## 8. Dry-run and fixtures

- New committed fixture `src/sentinel/fixtures/state/run_history.json` with 6
  entries, engineered so the dry run fires every change type the existing
  fixtures can express: CHRL decays across runs (score/rank drops, trend break,
  estimate cuts, rising shorts; five deterioration signals), ALFA improves (rank
  3 to 1, flag set and cleared, R40 sign flip), BRVO stays quiet (proves
  selective reporting), ZZZZ departs (universe change). Three cases are
  unit-tested instead of fixture-rendered, because current price/statement
  fixtures cannot produce them without invalidating pinned metric tests:
  new_cross, universe_added, and the R40-level deterioration signal (CHRL's
  r40_trend is -0.06, above the -0.10 threshold). (Amended at build time,
  2026-08-06.)
- `--dry-run` loads fixture state, renders both new sections, writes nothing to
  `data/cache/`.
- Fixture dates are fixed (relative to the fixture price end date), fully
  deterministic.

## 9. Testing strategy (all offline, `pytest -q` via `scripts/checks.sh`)

- **Unit, `report/changes.py`**: every change type triggers at exactly its
  threshold; below-threshold silence; quiet-day detection; first-run/no-prior;
  missing fields (None) never crash or fabricate deltas; rank moves respect the
  ranking mode; deterioration min-signals logic including the `deteriorating()`
  sufficiency rule; reason strings contain no em/en dashes.
- **Unit, `data/history.py`**: round-trip; retention pruning; same-date replace;
  corrupt-file degradation; `cache.prune()` leaves `run_history.json` alone.
- **Report/golden**: template renders both sections from fixture context; quiet-day
  single line; deterioration section omitted when empty; existing em-dash scan
  covers the new sections automatically.
- **Integration**: `--dry-run` end-to-end renders both new sections from fixture
  state (extends existing `test_run.py` pattern); `--tickers` subset skips change
  detection with the note.

## 10. Boundaries

**Always**: thresholds from config; failures degrade to data notes; no em/en dashes
in anything user-visible; state file written only by real full-universe runs;
`scripts/checks.sh` green before every commit; PROJECT_PLAN.md updated in the same
workstream.

**Ask first (owner gates)**: this spec; the task plan; the watchlist picks (before
any `config/watchlist.yaml` edit); anything touching delivery schedule, recipients,
secrets, or workflow steps beyond what is specced here (which is: nothing; D1
deliberately avoids workflow edits); merging the PR.

**Never**: commit to main; modify live `data/cache/` contents on this branch;
change scoring formulas or weights (change detection reads scores, never alters
them); let tests touch the network.

## 11. Resolved decisions (owner, 2026-08-06)

- **D1 state location**: `data/cache/run_history.json`, riding the existing bot
  commit step and pen; zero workflow changes.
- **D2 format**: single versioned JSON (diffable, inspectable, nullable-friendly).
- **D3 retention**: 12 runs (~2.5 weeks), config-overridable.
- **D4 section order**: market strip, then What changed today, then Deterioration
  watch, then existing tables.
- **Watchlist**: add the recommended 12 (universe 8 -> 20); 6 bench names held in
  reserve; rotation via roughly weekly owner-initiated candidate refresh
  (section 7.0), watchlist edits always owner-gated.
