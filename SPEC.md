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
- Foreign private issuers (20-F/6-K filers) publish XBRL facts on annual and
  half-year periods only, with no 3-month periods to derive quarters from, and
  are skipped a priori (MNDY today, in `backfill.SKIPPED`). Their data is not
  suspect, just too coarse. Promoting one means accepting the warm-up gap: say
  so in the swap PR.
- Standing requirement from the share-count-guard workstream: `diluted_shares`
  stays out of the backfilled field set (Amendment 2), because the overlap gate
  cannot see split-driven basis breaks outside its window.

#### 7.0.1 Rotation rubric (built round by round from the weekly digests)
The first 3 refreshes are calibration rounds: each one records which digest
evidence actually drove a decision and which was noise, so the rubric below is
written from observed rounds rather than guessed up front. From refresh #3 the
checklist adds the decision on automating proposal drafting against this rubric
vs staying manual.

**Working rubric (as of round 6):**
- Act on **decay-gate hit counts**, not on composite deltas. A name with 0 gate
  hits has not shown persistent decay however far its composite moved. But read
  a zero for what it is: the gate needs `r40_trend` below
  `deteriorating_r40_trend` AND technical confirmation (downtrend or a recent
  death cross), and `r40_trend` needs 12 quarters, so for a name still in
  warm-up the gate cannot fire at all. There, "0 of N hits" is silence, not
  reassurance. Check the data-quality column for `insufficient_history` before
  reading a zero as health (round 2).
- Before treating an attention-list name as deterioration, check whether its
  flags are **data-quality** (insufficient data/history, growth from annual,
  stale fundamentals) or **business** flags (dilution, high sbc, sbc inflated,
  crosses). Data-quality flags are a fetch/coverage problem, not a rotation
  signal. (Corrected round 3: sbc inflated is a business flag, matching the
  digest's flag split; the bullet previously misfiled it.)
- A **coverage gap outranks the attention list**: a name that stops being scored
  is either a data failure to chase or a delisting to swap, and both matter more
  than a few points of composite drift.
- **A flat fundamental score against a moving technical score means cached
  fundamentals**, and is worth checking before the name disappears entirely.
- At a 5-run window, raw **change-activity counts are volume, not signal**.
- **Inside one window a composite move is technical unless the fundamental
  column says otherwise.** Fundamentals, analyst revisions and short interest
  refresh weekly, so across most Tue-Sat windows they are constant and a
  composite delta attributes entirely to the technical leg. That is the
  default, not a guarantee: the earnings-aware refetch in
  `data/fundamentals.py` lands a new quarter on whatever weekday it appears
  (nine names stepped on a non-boundary run between 09-09 and 09-16), short interest
  publishes mid-week, and the revisions counter rolls over at earnings. Read
  the per-run fundamental score to attribute a move rather than assuming it
  (round 2, amended round 5).
- **The attention list detects change, not level.** It surfaces movers, so a
  name that is persistently weak but stable never drops far enough to appear.
  Read the latest rank and fundamental score beside the deltas (round 2).
- **Standing business flags are not events.** A name can carry dilution or
  sbc flags for months; only a flag appearing or clearing across the refresh
  boundary is evidence. Compare flag sets, do not re-read the standing set as
  news each week (round 3).
- **The window delta cannot see a boundary step.** The attention list
  measures first-vs-last inside the window, so a fundamental step that lands
  on the window's first run is invisible to it, and a name that stepped down
  at the boundary only surfaces if its technical leg happens to dip later in
  the week. Compare the last run of the previous window with the first run
  of this one before reading the attention table (RBRK, round 5).
- **A cross flag clearing is the lookback expiring, not a reversal.**
  `golden_cross` and `death_cross` are recency flags (`CROSS_LOOKBACK`, 10
  sessions, in `indicators/technicals.py`). A golden cross ageing out takes
  roughly 15 technical points off with no price event behind it, and the
  change detector reports it as `flag_cleared`. Only `death_cross` being SET
  is a negative event (TEAM, round 5).
- **Post-earnings revision swings are suspect.** `net_revisions_30d` is built from `parse_eps_revisions` (`data/signals.py`), which reads yfinance's current-quarter (`0q`) row, which advances to the next fiscal
  quarter once a company reports, so the counts restart: PANW read +39 on
  09-02 and 0 on 09-03, MDB +34 then -23, ZS +41 then +10, each within days
  of its report, and all three were back at or above +24 two weeks later. Not
  verified against the raw frame, but a revision swing within two weeks of
  earnings should be read as a rollover until it is (round 4).
- **Level rule (adopted 2026-09-17, round 5).** A name whose fundamental
  score sits in the bottom 3 of the universe for 3 consecutive digest
  windows joins the attention list as "persistently weak", a separate row
  kind from decay, subject to two conditions:
  1. The name must be `r40_trend`-live. Warm-up names score partly from
     annual data (`growth_from_annual`, `insufficient_history`), so their
     level is not yet trustworthy; a data-quality flag disqualifies.
  2. The row is surfacing only, not action. It means "compare this name
     against the bench on the fundamental leg in this round's evidence
     comment". It does not by itself propose a swap; standing conditions are
     still not events (round 3), the row just guarantees the comparison is
     made and recorded each round.
  The rule reads the fundamental leg only, never the composite. Composite is
  40 percent technical and the technical leg whipsaws universe-wide (18 of
  20 names in `uptrend` on 09-01, 11 on 09-12, 18 on 09-16); a
  composite-level rule would have swapped GTLB for SHOP in round 3 and been
  wrong within two weeks. Bottom 3 is relative, so the row never goes empty;
  the three-window persistence and the bench comparison are what keep it
  from being noise, and an absolute floor is the natural next tune if they
  prove insufficient. On 09-19 the bottom 3 are S (0.0), ESTC (15.4) and ZS
  (16.7), all live; ZS only entered the bottom 3 on 09-09 (CRWD held the
  slot before). Window membership (decided round 6): a name is "in the bottom 3
  for a window" when it is bottom 3 on the window's LAST run, matching the
  digest's first-vs-last convention and the level table it prints; under
  that reading ZS holds windows 5 and 6 and comes up at round 7 if it is
  still there. The row must print `r40_fcf` pass/fail next to the score:
  ZS sits in the bottom 3 with an `r40_fcf` of 48.7 that passes the Rule
  of 40, its score is three penalties (dilution, SBC, SBC-inflated), while
  S fails the rule outright at 23.9. A low score for penalties and a low
  score for a failed thesis are different conversations, and the row has
  to say which one it is opening. DDOG, the weakest composite outside the S
  clamp, would not qualify:
  its 33.2 fundamental is mid-pack and its weakness is a technical score of
  0 to 17 across the 15 runs from 09-01 to 09-19, exactly the leg the rule
  stays out of. The digest computes this row from refresh #7 on (the
  "Persistently weak" table under the attention list, `level_watch` in the
  JSON twin; see "What the digest carries as of 2026-09-20" below), so no
  round reads it off the level table by hand again.

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

**Round 2 (issue #22, 2026-08-22, window 2026-08-18 to 2026-08-22, 5 runs):**
- Outcome: no changes, all five attention names held. DDOG, CRWD, OKTA, FTNT
  and ZS each moved on the technical leg alone. Fundamental scores were
  identical across all five runs for all 20 names, so 100 percent of every
  composite delta in this window is technical. The moves ran over three
  sessions (08-19 to 08-21): CRWD 84.4 to 42.0, DDOG 83.3 to 21.4, FTNT 84.3
  to 43.1, OKTA 84.3 to 43.1, ZS 77.8 to 53.7. The bench did not collapse with
  them (WDAY and SHOP held near 82 technical throughout), so a swap would have
  been momentum chasing out of a three-day drawdown rather than a rotation on
  evidence.
- The only business-flag change all window was ESTC gaining a golden cross, a
  positive. Every other flag change was the 08-18 backfill clearing
  `growth_from_annual` and `insufficient_history`, which is data quality
  improving, not business news.
- Round-1 open item closed: TEAM. Scored in all five runs, no coverage gaps
  anywhere in the window, and its fundamental score came unpinned from the
  stuck 13.7 to 24.9 with rank 11 to 10. The cause was the cache gap and the
  EDGAR backfill closed it, so there is no alias drift in
  `data/fundamentals.py` to chase.
- The decay gate has never fired, and for most of its life it could not. Before
  the backfill fed the 2026-08-18 run, 0 of 20 names had an `r40_trend` at all,
  so the gate was structurally unfirable universe-wide across the first 7 runs;
  it went to 11 of 20 on 08-18. Zero hits in the 55 live snapshots since is
  unremarkable: the worst `r40_trend` in the universe is S at -0.054, roughly
  half the -0.10 threshold, and 191 of 238 snapshots sat in `uptrend`. The gate
  is wired correctly (`digest.snapshot_decaying` mirrors
  `changes.deteriorating`, including the detail that the persisted
  `death_cross` field is written from `tech.death_cross_recent`), so this is a
  quiet month plus a warm-up, not a defect. Owner decision this round: record
  it, change nothing, revisit once the remaining 9 names finish warming up.
- Consequence for the rubric, and the thing refresh #3 has to resolve: all five
  attention names qualified through the composite-drop leg, which round 1 says
  to discount, while the gate leg contributed nothing. Read literally, the two
  rules together leave no name actionable. That is a fine outcome for a
  calibration round that holds steady, but it is not a rule an automated
  proposal drafter could run on.
- Watch item, not a rotation candidate: S. Rank 20 of 20 for the whole window,
  fundamental score pinned at exactly 0.0 for all 12 runs (`fundamental_score`
  clamps at 0, so any further fundamental decay is censored and cannot show up
  as a change), and the only meaningfully negative `r40_trend` in the universe.
  It never reaches the attention list because it never drops, it just stays
  weak: the level-versus-change gap above, with a name attached.
- What mattered: the technical-versus-fundamental attribution of the composite
  move, the flag-change list, and the coverage line reading clean.
- What was noise: composite deltas on their own (again), change-activity counts
  (score 14, trend state 8, rank 7), and the bench first-versus-last delta,
  which hid TWLO dipping to 40.8 mid-window and recovering.

**Round 3 (issue #24, 2026-08-29, window 2026-08-25 to 2026-08-29, 5 runs):**
- Outcome: no changes; GTLB, the only attention name, held. Its entire -5.7
  window delta was technical by construction, the rank slide 8 -> 15 was the
  same dip through the change detector, and the 0 of 5 gate count is warm-up
  silence (insufficient_history in the data-quality column). All three
  business flags (dilution, high sbc, sbc inflated) were standing, carried
  since before the window: nothing new happened to the business this week.
- Warm-up finding worth keeping: GTLB's cache already holds 16 quarters,
  more than the 12 r40_trend needs. The blocker is capex holes in the
  2024-07 and 2023-07 columns breaking the year-ago TTM window (the r40_m4
  point in indicators/fundamentals.py). GTLB reports 2026-09-01; the July
  quarter rolls the window past the 2024-07 hole and the trend goes live on
  its own. A warm-up zero can therefore be a single-field cache hole rather
  than a year of missing quarters; check the parquet before assuming either.
- The only pro-swap argument was level, not change: SHOP shadow-scored 70.8
  vs GTLB 46.1 on the same basis, with passes_all_r40. Not acted on: the
  rubric has no level rule, and adding one is a rubric decision, deferred to
  refresh #4 alongside the S watch item (still rank 20, fundamental score
  still clamped at 0.0, decay still censored).
- Automate-vs-manual (option 3, the decision that closes calibration): STAY
  MANUAL, revisit later. Three rounds produced three no-changes; every
  attention name so far qualified through the composite-drop leg the rubric
  discounts while the gate leg was structurally silent in warm-up, so the
  rubric has never seen an actionable week to calibrate against. Automating
  now would automate "hold" and risk mishandling the first real signal.
  Revisit trigger: the remaining warm-up names go r40_trend-live, or the
  decay gate fires anywhere, whichever comes first.
- Rubric wording fix found preparing that decision: the flag-split bullet
  above misfiled sbc inflated as data-quality while the shipped flag split
  classifies sbc_inflated as business. Corrected in place; an automated
  drafter running on the written rubric would have tripped on the mismatch.
- What mattered: technical-vs-fundamental attribution (again), the
  standing-versus-new distinction on business flags, and the cache-depth
  check behind the warm-up zero.
- What was noise: the composite delta and rank slide on their own,
  change-activity counts (short interest 16, score 12), and the bench
  first-vs-last deltas (all within 0.4 of flat).

**Round 4 (issue #25, 2026-09-05, window 2026-09-01 to 2026-09-05, 5 runs):**
- Outcome: no changes; all six attention names held (MDB, FTNT, MNDY, NET,
  PANW, ZS). Not one fundamental score changed for any of the 20 names in
  any run pair of the window, nor across the 08-29 to 09-01 boundary, so
  every composite delta was technical, and the technical leg moved
  universe-wide: names in `uptrend` went 18 (09-01) to 15 (09-02) to 12
  (09-05) to 11 (09-08). Same shape as round 2, a broad drawdown read
  through the change detector.
- Reversion, checked at 09-17: FTNT 71.0 to 49.7 and back to 75.8, PANW
  58.4 to 41.9 to 62.6, NET 50.1 to 33.4 to 57.1, MDB 43.2 to 19.0 to 47.8,
  ZS 45.1 to 29.0 to 41.6. Five of six ended the fortnight at or near their
  window-start composite; MNDY (55.0 to 34.3 to 43.8) is the exception and
  its residual is still technical (fundamental 51.0 to 48.8).
- The bench diverged the same way as in round 2. WDAY and SHOP gained golden
  crosses on 09-01 and sat near 97 technical all window; TWLO and ZM dipped
  with the market. Promoting either leader would have been momentum chasing,
  and the fortnight proved it: SHOP went 76.5 (09-05) to 45.9 (09-17) in a
  `downtrend`, while the name it would have replaced in round 3, GTLB, went
  r40_trend-live at +0.101 (round 5 below).
- Revisions noise, explained: 11 revision crossings in the window, all
  within days of the late-August and early-September reports. PANW read +39
  on 09-02, 0 on 09-03, +27 on 09-15; MDB +34, -23, -1, +24; ZS +41, +10,
  +1, +29. `net_revisions_30d` comes from `parse_eps_revisions`, which reads yfinance's current-quarter row, and that row
  rolls to the next fiscal quarter at earnings, so the counter restarts.
  Rubric line added above.
- Level rule, S watch item and automate-vs-manual: deferred again to round 5
  (below), where the boundary steps gave them real data to be judged on.
  Nothing in this window changed the round-3 stay-manual reading.
- What mattered: the fundamental column being flat everywhere (one check
  settled six names), the breadth count on `trend_state`, and the bench
  reading the same technical shock differently.
- What was noise: the six composite deltas, the rank (25) and score (23)
  crossings, and the revision crossings (a rollover, not sentiment).

**Round 5 (issue #26, 2026-09-12, window 2026-09-08 to 2026-09-12, 5 runs):**
- Outcome: no changes; RBRK held, TEAM held. This was the first window with
  fundamental events behind the numbers, so the reading differs from rounds
  1 to 4.
- RBRK is the first genuine fundamental step down the rubric has seen, and
  the attention list caught it for the wrong reason. The July quarter
  landed on 09-08 through the earnings-aware refetch (cache column
  2026-07-31), and the fundamental score stepped 45.9 to 27.3 with
  `r40_trend` +0.119 to -0.035, rank 4 to 6 on 09-08 and 10 by 09-17. The
  digest's -16.2 window delta contains none of that: it is the
  single-session 09-12 technical dip (82.7 to 42.1, `uptrend` to `mixed`),
  reverted on 09-15 (83.9). The step sat at the window boundary, exactly
  where first-vs-last cannot see it, and RBRK surfaced only because the
  technical leg happened to blink four sessions later. Held because the
  decay gate is nowhere near: -0.035 is a third of the -0.10 threshold,
  `trend_state` is `uptrend`, no death cross, and all three business flags
  are standing. Now a named watch item: RBRK carries the worst `r40_trend`
  in the universe (S did before), and a second step down at the next
  quarter with a trend break is what a real gate hit looks like.
- TEAM: -6.0, entirely the golden cross ageing out on 09-09 (set 08-26,
  expired after the 10-session lookback). Technical 98.9 to 83.9 on flat
  fundamentals (24.9), rank 5 to 9 mechanical. Nothing happened to the
  business.
- The rest of the fortnight's fundamental steps, none of which reached the attention list because they landed mid-window, lifted a score, or were too small for the 5-point drop leg (ESTC's 09-12 step cost 2.4 composite points): GTLB
  21.3 to 30.7 (09-09, `r40_trend` live at +0.101, `dilution` cleared), CRWD
  20.7 to 28.0 (09-09, live at +0.052), SNOW 25.7 to 33.3 (09-11, live at
  +0.078), PANW 41.4 to 48.1 (09-16), ZS 23.2 to 16.7 (09-11, `r40_trend`
  +0.001 to -0.018), ESTC 19.4 to 15.4 (09-12, +0.011 to -0.021), MNDY 51.0
  to 48.8, MDB 23.3 to 24.5, OKTA 22.9 to 23.6. The digest counted these as
  20 `score` crossings and 2 `r40_inflection` rows: real events, filed
  under volume.
- Round-3 watch items closed. GTLB: exactly as predicted, the July quarter
  rolled the year-ago window past the 2024-07 capex hole and `r40_trend`
  went live on 09-09; the round-3 rank slide (8 to 15) did not survive the
  boundary (rank 8 on 09-17). S: still rank 19 or 20 in all 25 runs,
  fundamental score still clamped at 0.0 in all 25, `r40_trend` -0.054 to
  -0.033 on the new quarter, and a `dilution` flag SET on 09-08, the first
  non-cross business flag to appear on a watchlist name since the digest
  started. Still never on the attention list, still the level-not-change
  case. The level rule is adopted in the rubric above (fundamental leg
  only, live names only, surfacing not action); S and ESTC meet it at
  round 6, so round 6 carries the first bench comparison on the
  fundamental leg.
- Coverage: clean in both the round-4 and round-5 windows, every configured name in every run.
- Automate-vs-manual: the round-3 revisit trigger has not fired. `r40_trend`
  coverage went 11 to 14 of 20 (CRWD, GTLB, SNOW now live; FTNT, HUBS, MDB,
  MNDY, NOW, OKTA still warming up, MNDY structurally), and the decay gate
  has 0 hits in 25 runs; the worst current reading is RBRK at -0.035, and the worst in the history is S at -0.054 (08-18 through 09-05). Stay manual. What
  changed is that the rubric now has boundary-step events to calibrate
  against, so the next rounds can test the boundary-comparison bullet on
  real data. Warm-up note for the remaining names: MDB and OKTA hold 16
  quarters yet still read n/a; the GTLB pattern (a field hole in the
  year-ago window) is the first thing to check before assuming missing
  quarters.
- What mattered: the boundary comparison (09-05 vs 09-08) that the digest
  does not make, the cross-lookback mechanics behind TEAM, and the
  cache-column check confirming which quarter landed.
- What was noise: both window deltas as printed (RBRK's was the wrong move,
  TEAM's was a flag expiring), `flag_cleared` rows for crosses, and the 9
  short-interest crossings (exchange publication lands mid-week).

**Round 6 (issue #28, 2026-09-19, window 2026-09-15 to 2026-09-19, 5 runs):**
- Outcome: first swap. S out (demoted to the bench, still shadow-scored so
  the move is reversible with its history intact), SHOP in. MNDY and HUBS,
  the two attention-list names, held. ESTC surfaced under the level rule
  and held.
- The attention list was noise both times. MNDY -24.3 (rank 4 to 16) on a
  flat 48.8 fundamental: its technical score read 0.0 on 09-12, 76.5 on
  09-15 and 76.4 on 09-16 (the window's first two runs), 36.3 on 09-17
  and 15.8 by 09-19, so the window delta measured a two-run spike at the
  window start, and the 35.6 composite it ended on is 6 points above the
  29.3 it held through round 5, a technical-leg recovery on a flat
  fundamental. HUBS -8.3 (rank 19
  to 20) on a flat 32.4, toggling between `mixed` and `downtrend` as it has
  since late August. Both are warm-up names with data-quality flags only.
- The window itself was the quietest yet: one fundamental step (PANW 41.4
  to 48.1 on 09-16, upward), zero flag events on the watchlist, `uptrend`
  breadth 17 or 18 of 20 in every run (the round-4 drawdown fully
  reverted), gate 0 hits in 25 runs, coverage clean. On the bench, WDAY's
  -5.9 is its golden cross expiring on 09-16 (the TEAM mechanism from
  round 5), SHOP sat flat at 45.9 in a `downtrend`, ZM's technical score
  reached 0.0 on 09-18, TWLO +0.8.
- Level rule, first application, read by hand from the level table. S and
  ESTC were bottom 3 by fundamental score on every run of windows 4, 5 and
  6 and both are live, so both qualify; ZS entered on 09-09 and holds
  windows 5 and 6 under the last-run reading. Bench comparison on the
  fundamental leg, recomputed from the committed cache as of 09-19
  (`r40_fcf` is the Rule of 40 on the FCF leg, the universe's admission
  criterion):
  S 0.0 (`r40_fcf` 23.9, growth 21.1 percent TTM, FCF margin 2.8, SBC 29.3
  percent of revenue, dilution 3.2, `r40_trend` -0.033; the unclamped
  score is about -15, so the 0.0 the table shows is censored);
  ESTC 15.4 (`r40_fcf` 35.5, growth 16.2, FCF margin 19.4, SBC 16.8);
  ZS 16.7 (`r40_fcf` 48.7, passes);
  SHOP 62.7 (`r40_fcf` 50.2, growth 32.5 percent TTM, FCF margin 17.7, SBC
  3.6, dilution -0.8, `passes_all_r40`; warm-up, only the `r40_trend`
  term is missing);
  ZM 33.8 (live, `r40_fcf` 43.6, growth 5.0);
  WDAY 22.9 and TWLO 23.6 (both `growth_from_annual`, not trustworthy yet
  by the rule's own condition 1, read symmetrically).
- Why S goes: it is not marginal. Rank 19 or 20 in all 25 runs of the
  history, fails the Rule of 40 on the FCF leg, goes negative once SBC is
  counted (`r40_sbc_adj` -5.4), `dilution` set on 09-08, `r40_trend`
  negative for the whole history, and the score is pinned at the clamp so
  no further decay can ever register. SHOP beats it by 62.7 fundamental
  points with a clean SBC profile. Round 4 refused SHOP at composite 76.5
  on a fresh golden cross because that was momentum chasing; at 45.9 in a
  `downtrend` the objection has inverted, and the rule reads the
  fundamental leg by design. Holding S again would have been the
  stay-manual-by-default failure round 5 warned about.
- Why ESTC stays: a narrow Rule of 40 miss on a profitable business, one
  penalty. The only live bench name that beats it is ZM at 33.8, growing
  5 percent a year; not a better candidate. Reviewed again at round 7 with
  ZS.
- Promotion mechanics: backfill dry run for SHOP came back ACCEPT with 30
  of 30 overlap checks matched and 0 quarters gained. Its cache already
  holds 10 quarters (2024-03 to 2026-06) and EDGAR has nothing older, so
  the owner-gated `--apply` step is a no-op and is skipped. SHOP's
  fundamental score is built on TTM data (10 quarters clears the 8 that
  growth needs); `r40_trend` needs 12 and goes live when the 2026-12
  quarter lands, around 2027-02. Until then SHOP is a warm-up name and
  cannot itself qualify under the level rule.
- Automate-vs-manual: stay manual, unchanged trigger (14 of 20 live at 09-19, 13 of 20
  after the swap until SHOP's `r40_trend` goes live; gate 0 hits).
  The first swap was made by the rubric's own rule with a hand-read row, so
  the next automation step is the digest computing the persistently-weak
  row with the `r40_fcf` pass/fail column, not drafting proposals.
- Watch items for round 7: SHOP's first ranked runs (expect a technical
  drag while the `downtrend` lasts; the fundamental leg is the thesis),
  ZS and ESTC under the level rule, RBRK at -0.035 (unchanged this
  window), S on the bench.
- What mattered: the level rule's bench comparison and the scorecard
  breakdown behind the scores (the clamp, the `r40_fcf` pass/fail, the SBC
  share). What was noise: both attention rows, WDAY's bench delta, the
  short-interest and rank crossings.

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

**What the digest carries as of 2026-09-20** (branch
`digest-persistently-weak`, implementing the round-5 level rule so refresh #7
does not depend on the hand read that round 6 made):

- **Persistently weak table.** Under the attention list, one row per name in
  the bottom `changes.weak_bottom_n` (3) by fundamental score on the latest
  run, eligible or not, so the tail of the level table is visible whole: a
  warm-up name holding a slot is why the next name up is not in it. Each row
  carries the fundamental score, `r40_fcf` in points with a pass or fail
  verdict against the Rule of 40 bar (the one constant, `R40_BAR` in
  `indicators/fundamentals.py`, now shared with the `passes_all_r40` flag),
  rank, business flags, and a status: "qualifies (3 of 3 windows)", "2 of 3
  windows", or "not eligible (insufficient history)". Reads the fundamental
  leg only; composite never enters it.
- **Window ends, as decided in round 6.** A name holds a window when it is
  bottom N on that window's LAST run; the digest steps back
  `week_window_runs` runs at a time from the latest run to find the previous
  windows' last runs, which reproduces the earlier digests' windows whenever
  every week had a full set of runs (a missed run shifts the earlier
  boundaries by one run, at most one window at the margin; an end counts
  only when its whole window is in history, so the first run ever recorded
  is never a phantom window end). The name must be
  `r40_trend`-live with no data-quality flag at every counted window end,
  not just the latest, since a warm-up score in an earlier window is no more
  a level than one today. `changes.weak_windows` (3) sets the bar; a history
  shorter than that says so in the table rather than qualifying nobody in
  silence.
- **Bench table gains the fundamental leg.** The bench rows now carry the
  fundamental score and the same `r40_fcf` pass or fail cell, so the
  comparison the level rule asks for (this name against the bench on the
  fundamental leg) is two columns of one issue rather than an offline
  scorecard run. Both tables are in the JSON twin (`level_watch`,
  `bench_weeks.score_last`, `bench_weeks.r40_fcf_last`).
- **Checked against round 6.** Rebuilt on the committed history as of
  2026-09-19 the table reads S 0.0, 23.9 fail, qualifies (3 of 3); ESTC
  15.4, 35.5 fail, qualifies (3 of 3); ZS 16.7, 48.7 pass, 2 of 3 windows,
  which is the round-6 hand read line for line, including ZS coming up at
  round 7.

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
