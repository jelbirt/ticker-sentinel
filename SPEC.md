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

One price event counts once (2026-10-10, refresh #9 review). A technical
breakdown moves T by itself (the trend term alone is -20 T from mixed or -60 T
from uptrend, a new death cross -15 T, so 6 or more composite points at the
default 0.4 `technicals_weight`), and a drop that happened entirely today also
appears in the week window. So:
- A technical breakdown and the 1-run drop in the same run merge into one
  signal, reported as one clause: "broke into downtrend (composite fell 9.8
  since prior run)".
- When the 1-run drop counts, the week drop counts too only if the decline was
  already underway before today (week-ago to prior run fell >= `week_drop_pts`).
  With no 1-run drop counted, the week drop stands alone as before.

Replayed over the 24 committed runs to that date, this cut listings from 22 to
12; every dropped listing was a lone price event (one-day SMA whipsaws, or a
single large drop counted as both 1-run and week). Those still surface in What
changed and Movers.

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

**Working rubric (as of round 14):**
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
  2. A qualifier is a swap candidate (amended round 14). Swap it for the
     bench name with the highest fundamental score, provided that name would
     rank above the bottom `weak_bottom_n` on entry (the round-7 tail rule
     below). At most one swap per round: when several names qualify, the one
     with the most consecutive windows goes first and the rest wait for the
     next round. If no bench name clears the tail, the action is refreshing
     the bench. Rounds 5 to 13 read this row as surfacing only, which,
     together with the round-7 `r40_fcf` hold below, left no path to a swap
     for any name passing `r40_fcf`.
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
- **The `r40_fcf` verdict no longer holds a qualifier (round 7, retired
  round 14).** Round 7 held any qualifier that passed `r40_fcf`, on the
  reading that a bottom slot earned by penalties says nothing against the
  business. That was wrong on two counts. The dilution and SBC penalties
  are the scoring model's deliberate judgment that those costs fall on
  shareholders (PROJECT_PLAN section 6), and a rotation rule should not
  overrule the score it reads. And `r40_fcf` is the variant SBC inflates,
  because free cash flow adds SBC back: on 11-14 ZS read 48.7 on `r40_fcf`
  and 24.1 on `r40_sbc_adj` (the plan's "honest number"), OKTA 42.4 and
  26.1, RBRK 61.0 and 38.8. The same SBC that put each name at the bottom
  kept it passing the test that held it there. On 11-14, 19 of 20 names
  pass `r40_fcf` and the one that fails (HUBS, 38.9) is not in the bottom
  3, so no qualifier could be swapped; rounds 9 to 13 all closed with no
  changes. The R40 columns stay in the level table as context for the
  comparison, not as a gate. Testing on `r40_sbc_adj` instead was
  considered and rejected: 15 of 20 names fail it on 11-14, so it does not
  tell the bottom from the middle.
- **A replacement must enter above the tail (round 7).** A bench name that
  would itself land in the bottom `weak_bottom_n` on entry is not a better
  candidate, however much it beats the qualifier: the swap only relabels
  the weakest slot. When no bench name clears that, the action is refreshing
  the bench, not holding the qualifier indefinitely.
- **Bench before promotion (round 7).** A name found by an off-pipeline
  screen goes onto the bench first and is shadow-scored for at least one
  digest window on the committed basis before it can be promoted, so the
  swap decision reads the same numbers the watchlist is scored on.
  Fundamentals are constant inside a window by construction, so one window
  is enough.
- **A warm-up flag can be a source defect (round 7).** Five names (MDB,
  HUBS, NOW, WDAY, TWLO) read `growth_from_annual` on 16-quarter caches
  because the backfill's revenue tag choice picked a pre-2018 `Revenues`
  series over the live contract-revenue tag (fixed in PR #32). Extends the
  round-3 GTLB lesson: before reading warm-up as missing quarters, read the
  parquet row by row.
- **A backfill apply steps fundamentals at the boundary (round 8).** An
  owner-gated `--apply` lands between windows and rewrites history, so the
  names it touched step on the next run with nothing new in the business:
  MDB 24.5 to 44.1, HUBS 32.4 to 35.8, NOW 60.3 to 63.2 and WDAY 22.9 to
  24.1 on 09-29, all from `3001feb`. Check `git log data/cache` for an apply
  commit before reading a boundary step as an event.
- **The tape does not veto a promotion (round 8).** The swap reads the
  fundamental leg, so a replacement in a `downtrend` enters low on
  composite: ADSK, second on the fundamental leg, sits about 16th of 20 by
  composite on a technical score clamped at 0 (the breadth-ranked report
  still puts it 4th, since it passes all three R40 variants), while the
  name it replaces carries its composite on the technical leg. That is the leg the level rule stays out
  of (the SHOP precedent, round 6); a promotion is not reconsidered because
  the replacement's tape is weak.
- **Read breadth before names (round 10).** When most of the attention
  list moves on the same runs, the cause is the market or the sector, not
  any one business: check the composite moves against QQQ and peers, and
  read the next run before reading a single name. In window 10-13 to 10-17
  all 8 attention names fell on the technical leg through two software
  selloff sessions (10-14 and 10-16 closes, QQQ -2.0 and -2.5 percent,
  the names -6.3 to -8.4 percent on 10-16), with every fundamental score
  flat. The technical leg's trend-state cliffs turn a multi-session
  decline into one step, and the step can land on a quiet session: PANW
  and NET lost 48 and 51 points of T on the 10-15 close (QQQ -0.1 percent)
  after the 10-14 leg, while FTNT, IOT, SNOW, CRWD and MDB stepped on the
  10-16 close; DDOG, which never left uptrend, lost 17. Part of it
  reversed one run later (IOT T 12.2 to 71.0 on a +1.8 percent session).
  So date a step by the closes around it, not by the run it lands on. A
  shared drop is a market reading, and the level rule already stays out
  of it.
- **A scoring-method switch steps fundamentals on the switch run (round
  11).** Like a backfill apply (round 8), a formula change moves the
  fundamental score with nothing new in the business, and it lands on the
  first scheduled run after the merge, which can be mid-window rather than
  at the boundary. Runs carry a scoring `method` in `run_history.json`
  (absent on older runs, which read as 1; 2 since PR #45) and the first run on a new method prints one "scoring
  method changed" note: CRWD 28.0 to 38.0, DDOG 33.2 to 43.2 and IOT 25.0
  to 35.0 on 10-21, each losing a dilution flag the diluted share count had
  misread. Check the `method` field before reading an in-window step as an
  event.
- **A quarter lands when the cache next fetches a complete posting, not
  when the company reports (round 13).** Across the early-November cluster
  the lag from report to scored quarter ran 1 to 8 days, for two different
  reasons. Where the earnings-aware refetch ran on every run, the lag is
  Yahoo's posting delay (to run resolution): MNDY 1, SHOP 2, DT, DDOG and
  HUBS 3, APPF and NET 5. NOW, PLTR and TEAM took 8 because of the refetch
  policy, not Yahoo: daily refetch keys on the cached `next_earnings` date
  (`earnings_refetch_due`, the 14 days after it), Yahoo rolled each of
  their dates into 2027 on the first fetch after the report, before the
  quarter was posted, and that switched daily refetch off until the weekly
  refresh (`REFRESH_DAYS`, 7) came round. Their quarters may have been on
  Yahoo days earlier. A posting can also arrive hollow: FTNT's 09-30
  quarter appeared on 10-30 with 2 of its 11 fields, the scorer skipped it
  under an "incomplete at the source" data note and kept scoring the prior
  quarter, and the step landed on 11-04 when the column filled. So the
  earnings calendar says roughly which window a step can land in, not
  which run: round 11 placed PLTR's step on the 11-03 boundary run and it
  landed on 11-10, and TEAM, expected in round 13, stepped in round 14.
  Date a step by the run on which the parquet's newest column fills (`git
  log data/cache`), not by the report date, and when a name's cached
  `next_earnings` jumps to next year with the old quarter still newest,
  expect the step about a week after that fetch.

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

**Round 7 (issue #31, 2026-09-26, window 2026-09-22 to 2026-09-26, 5 runs):**
- Outcome: no watchlist change; bench refreshed (ADSK, APPF, GWRE added,
  TWLO dropped). ESTC's swap is decided at round 8 against shadow-scored
  bench evidence. ZS held under the new pass/fail rubric line. All four
  attention names held.
- First round read from the computed level table (PR #30). It matched the
  round-6 expectation: ESTC (15.4, `r40_fcf` 35.5 fail) and ZS (16.7, 48.7
  pass) qualify at 3 of 3 windows (ends 09-12, 09-19, 09-26); OKTA (23.6)
  holds the third slot but is not eligible (`insufficient_history`).
- Not one fundamental score moved for any of the 20 names across the 09-19
  to 09-22 boundary or inside the window, so every attention row was
  technical. NOW -6.0 and PLTR -5.8 were golden crosses expiring on 09-24
  (the TEAM mechanism from round 5; T 95.9 to 81.0 and 98.7 to 83.9). MNDY
  -5.6 and HUBS -5.1 slid to T 2.1 and 3.5 in a `downtrend` on flat 48.8
  and 32.4 fundamentals, both warm-up names. Gate 0 hits in 25 runs;
  `uptrend` breadth 17 or 18 of 20 every run; coverage clean.
- ZS: held. Pre-penalty score about 46.8; the 16.7 is three 10-point
  penalties (dilution, high sbc, sbc inflated) that CRWD, SNOW, DDOG and
  RBRK also carry. Its dilution reads 3.4 percent against the 3.0 limit;
  under the limit it would score 26.7 and leave the bottom 3. `r40_fcf`
  48.7 and rule of X 74.1 pass comfortably. Hence the pass/fail rubric line.
- ESTC: the only `r40_trend`-live name failing the Rule of 40 (35.5), the
  lowest `r40_sbc_adj` in the universe (18.7), `r40_ebitda` 17.2, and the
  09-12 quarter stepped it 19.4 to 15.4 with `r40_trend` +0.011 to -0.021.
  Mitigating: 16.2 percent growth, share count shrinking (-1.2 percent),
  FCF yield 3.8 percent. Next report 2026-11-19.
- Bench comparison, and why it failed to produce a swap: the only live
  bench name besides S was ZM (33.8, `r40_fcf` 43.6 pass, growth 5.0
  percent, margin only). WDAY and TWLO read warm-up, which the round traced
  to a backfill defect: the EDGAR revenue tag choice took a pre-ASC 606
  `Revenues` series (WDAY: 2011-10 to 2018-04) over the live
  contract-revenue tag, so revenue was never backfilled for WDAY, TWLO,
  MDB, HUBS and NOW. Fixed in PR #32 (post-merge apply fills 52 revenue
  cells, every overlap check at 0.00 percent). Recomputed in memory on
  corrected revenue: WDAY 24.1 (`r40_fcf` 41.4 pass, `r40_trend` +0.007,
  live), TWLO 28.4 (35.6 fail), MDB 44.1 (`r40_trend` +0.164, live), NOW
  63.2 (live), HUBS 35.8 (still `insufficient_history`: capex holes).
  Even corrected, WDAY would enter at 24.1 and sit in the bottom 3 beside
  ZS and OKTA: a relabelled weakest slot, not an upgrade. Hence the
  enter-above-the-tail rubric line, and a bench refresh instead of a swap.
- Bench refresh: 28 candidates screened in memory with the repo's own
  scorecard and scoring code (yfinance statements, EDGAR history behind the
  verification gate, corrected revenue tag rule; nothing written). Kept to
  names that are `r40_trend`-live, pass `r40_fcf`, score F above 30:
  ADSK 73.6 (growth 17.9, `r40_fcf` 53.9, `r40_sbc_adj` 44.9, `r40_trend`
  +12.2, `passes_all_r40`); APPF 58.8 (20.7, 46.2, 39.0, +5.8, no flags);
  GWRE 46.2 (22.7, 47.0, 34.7, +1.0, no flags); ZETA 39.5 and KVYO 36.2
  (higher growth, both `dilution`); QLYS 66.7, INTU 65.7, ADBE 57.8, CRM
  53.5 (growth 10 to 14 percent, margin led, not added: a growth watchlist
  question for the owner). Not usable: DUOL (EDGAR 3 of 18 overlap checks
  failed, warm-up), DOCS (short history), APP and VEEV (recent capex
  missing at the source, unscorable), CWAN (no price data, likely
  delisted); AXON, PATH, PCOR, NTNX, TOST, TYL, PCTY, BILL, TENB, TTD,
  SAIL, PAYC fail `r40_fcf` or score under 30. ADSK, APPF and GWRE added;
  TWLO dropped (fails `r40_fcf` on corrected data). WDAY, ZM and S stay.
  Backfill dry run for the three adds: ACCEPT, 7 to 16 quarters each, all
  overlap checks matched; GWRE gains 5 revenue checks under the PR #32 rule,
  so the owner-gated `--apply --tickers ADSK,APPF,GWRE` runs after PR #32
  merges (one apply can cover the PR #32 five as well).
- Round-6 watch items: SHOP's expected technical drag did not happen; it
  turned `uptrend` by 09-22 and ranked 3, then 2, composite about 70 on a
  flat 62.7 fundamental. RBRK unchanged (27.3, `r40_trend` -0.035). S on
  the bench at 0.0.
- Automate-vs-manual: stay manual. `r40_trend` coverage 13 of 20 today, 15
  after the PR #32 apply (MDB, NOW); gate 0 hits.
- Round 8 decides ESTC: compare it against the shadow-scored bench on the
  fundamental leg; the strongest current replacements are ADSK (highest F)
  and APPF (cleanest SBC profile). If no bench name clears the tail by
  then, ESTC's 2026-11-19 report is the next checkpoint.
- Operational note, not a rubric line: since about 2026-08-27 the 10:00
  UTC schedule has started 3 to 4.5 hours late, after the US open, so
  Tuesday to Friday runs score technicals on a partial session (the 09-25
  report was built 10:52 EDT on a same-day bar). Irrelevant to the
  fundamental-leg level rule; it adds noise to the technical leg the
  attention list reads.
- What mattered: the `r40_fcf` pass/fail column (it settled ZS), the
  enter-above-the-tail check on the bench comparison, and reading the
  parquet rows behind a warm-up flag. What was noise: all four attention
  rows (two cross expiries, two technical slides), IOT's 6 crossings (T
  toggling 83 and 42), and the short-interest crossings.

**Round 8 (issue #34, 2026-10-03, window 2026-09-29 to 2026-10-03, 5 runs):**
- Outcome: ESTC demoted to the bench, ADSK promoted. The first swap of a
  Rule-of-40 failure under the round-7 pass/fail line. ZS held again.
  Attention list empty (no decay-gate hits, no week-scale composite drop).
- Level table: ESTC (15.4, `r40_fcf` 35.5 fail) and ZS (16.7, 48.7 pass)
  qualify at 3 of 3 windows; OKTA (23.6) holds the third slot, not eligible
  (`insufficient_history`). ZS is held under the round-7 line: a passing
  qualifier is penalties, not thesis.
- ESTC against the bench on the fundamental leg: every round-7 add is
  `r40_trend`-live in the digest and passes `r40_fcf`, so the round-7 watch
  item is met. ADSK 73.6 (53.9 pass, `r40_trend` +0.122, `passes_all_r40`,
  valuation cheap), APPF 58.8 (46.2 pass), GWRE 46.2 (47.0 pass). Each
  shadow-scored all 5 runs of one full window, so bench-before-promotion is
  met, and each enters far above the tail (the post-swap bottom 3 tops out
  at 24.9). ADSK chosen: highest fundamental score on the bench, second in
  the universe behind PLTR. ESTC: `r40_trend` -0.021, `r40_sbc_adj` the
  lowest in the universe, `high_sbc`; no fundamental change since the 09-12
  quarter. Next report 2026-11-19, now read from the bench.
- The composite points the other way, and was not used: ESTC 42.9 (16th by
  composite, 20th in the breadth-ranked report)
  rides a technical 84 (up 58 percent in three months, near its 52-week
  high), while ADSK reads 44.1 on a technical score of 0.0 in all 5 runs.
  Checked against live prices on 10-04 that the 0.0 is real, not a price
  fetch failure: 212 against a 233 SMA50 and a 239 SMA200, 34 percent below
  its 52-week high, flat over three months while SPY rose, which clamps the
  technical score at 0. ADSK sits about 16th by composite, but the report
  ranks by R40 breadth first and ADSK passes all three variants, so it
  enters 4th (behind PLTR, SHOP, FTNT). Hence the tape-does-not-veto
  rubric line.
- Backfill: ADSK's history was already seeded to 16 quarters by `3001feb`
  (round 7), and the 2026-10-04 dry run accepts it with 0 cells to fill, so
  the swap needs no post-merge apply. ESTC keeps its parquet on the bench.
- No fundamental score moved inside the window. The only steps were at the
  09-26 to 09-29 boundary, all from the `3001feb` apply (MDB, HUBS, NOW,
  WDAY on the bench): hence the apply-step rubric line. MDB's composite
  fell 47.9 to 26.5 across the same boundary, entirely technical (T 83 to
  0, `downtrend`), and the window delta could not see it (round 5 line).
- Round-7 watch items: MDB is `r40_trend`-live at +0.164 and out of the
  bottom 3 (44.1). RBRK unchanged (27.3, `r40_trend` -0.035). S on the
  bench at 0.0. SHOP rank 2 or 3 all window, flat 62.7.
- Found this round: HUBS and OKTA were in warm-up on single capex holes in
  the year-ago window, because an addend filed only cumulatively (HUBS's
  10-K-only intangibles, OKTA's nine-month 0) dropped the whole capex
  quarter. Fixed on branch `fix/edgar-capex-addend`; after its apply HUBS
  (39.0) and OKTA (23.1) go live, coverage 15 to 17 of 20. OKTA then
  becomes eligible in the bottom 3 with `r40_fcf` 42.4, which passes: a
  held case under the round-7 line, not a swap. FTNT's holes are a
  different cause (base tag switch in its 10-K) and stay open.
- Automate-vs-manual: stay manual. Gate 0 hits in 25 runs.
- Watch items for round 9: ADSK's first ranked runs (technical drag while
  the downtrend lasts), OKTA under the level rule once the capex apply
  lands, ZS, ESTC on the bench through its 11-19 report.
- What mattered: the bench table's fundamental and `r40_fcf` columns (the
  whole decision), and the run count proving one full shadow window. What
  was noise: IOT's 5 crossings (T 39 to 23 to 67), MNDY's 2 on a flat 0.0
  technical, and every composite on the swap pair.

**Round 9 (issue #38, 2026-10-10, window 2026-10-06 to 2026-10-10, 5 runs):**
- Outcome: no changes. First digest delivered by the `workflow_run` chain
  from PR #37 (weekday runs gate-skipped in seconds, Saturday opened #38).
  Attention list: no persistent decay; three persistently weak rows.
- Level table: ZS (16.7, `r40_fcf` 48.7 pass) qualifies at 3 of 3 windows,
  OKTA (23.1, 42.4 pass) and TEAM (24.9, 46.1 pass) at 1 of 3. ZS is held
  under the round-7 line (a passing qualifier is penalties, not thesis);
  OKTA and TEAM do not qualify yet, and both pass the rule too. OKTA
  entered as predicted in round 8, eligible now that the capex apply made
  it `r40_trend`-live. The third slot is a coin toss: IOT sits at 25.0
  against TEAM's 24.9, so only the three-window persistence keeps it from
  being noise.
- Boundary steps 10-03 to 10-06, all from the `5c71174` apply (round-8
  apply-step line): FTNT 69.6 to 83.3, HUBS 35.8 to 39.0, OKTA 23.6 to 23.1.
  An apply can step a name down as well as up. No fundamental score moved
  inside the window.
- First live `r40_trend` readings: FTNT +0.136, HUBS +0.032, OKTA -0.006.
  RBRK unchanged at -0.035. Nothing near `deteriorating_r40_trend` (-0.10),
  so the decay gate stays at 0 hits in 25 runs (the retained history,
  09-08 to 10-10; none in any committed history since 08-07). Coverage 18 of 20
  `r40_trend`-live; MNDY (foreign filer, no quarterly XBRL) and SHOP remain
  in warm-up.
- ADSK's first ranked window: 4th all week on R40 breadth, F 73.6. Its
  tape turned from `downtrend` to `mixed` on 10-08 (T 0 to 29 to 24),
  composite 44.1 to 53.8. The round-8 tape-does-not-veto call needed no
  revisiting.
- Bench (fundamental, `r40_fcf`): APPF 58.8 (46.2 pass) and GWRE 46.2
  (47.0 pass) remain the ready replacements, both above the tail; GWRE's
  composite rose 16.0 in the window to 59.2, all technical. ZM 33.8 (43.6
  pass), WDAY 24.1 (41.4 pass, `high_sbc`). S 0.0 (23.9 fail) and ESTC 15.4
  (35.5 fail) fail the rule; ESTC reports 2026-11-19.
- Found this round, fixed in their own PRs: the Deterioration watch counted
  one price event as two signals (HUBS 10-10, a one-day SMA whipsaw at +1.1
  over the week, and ZS 10-06 at -5.9 one-run and -5.9 week; PR #40,
  replay 22 listings to 12), and LLM news tones were rejected with no log
  line naming why (2 of 25 this week, about 1 per 2 runs since 08-19;
  PR #39).
- Automate-vs-manual: stay manual. The gate has never fired, and the one
  swap the rubric has produced (round 8) still needed a judgment call on
  which bench name. Revisit when the decay gate first fires.
- Watch items for round 10: ZS (4th window), OKTA and TEAM (2 of 3), IOT
  against TEAM for the third slot, ADSK's tape, ESTC through its 11-19
  report, and the first runs after PR #40 merges (fewer Deterioration
  listings, each multi-signal).
- What mattered: the `r40_fcf` column on the level table (the whole
  decision) and the boundary comparison attributing FTNT, HUBS and OKTA to
  the apply. What was noise: change activity (MNDY 5, HUBS 4, both
  SMA crossings; HUBS a whipsaw, MNDY one
  cross into `uptrend` that held) and the HUBS Deterioration listing.

**Round 10 (issue #42, 2026-10-17, window 2026-10-13 to 2026-10-17, 5 runs):**
- Outcome: no changes. Second digest delivered by the `workflow_run` chain
  (weekday runs gate-skipped in seconds, Saturday opened #42). Attention
  list: 8 persistent-decay rows, all at 0 of 5 gate hits; three
  persistently weak rows.
- Every fundamental score is identical across the 7 runs from 10-10 to
  10-20 for all 20 names, and `data/cache` has only bot commits since
  10-09 (no apply), so the boundary carries no step and 100 percent of
  every attention delta is technical: PANW -29.0, NET -24.5, FTNT -24.1,
  IOT -23.6, SNOW -22.1, CRWD -19.9, MDB -10.1, DDOG -9.8.
- The move is a real sector selloff, checked against live closes on 10-20:
  on 10-16 CRWD -7.0, DDOG -7.2, FTNT -7.0, IOT -7.1, MDB -7.9, NET -6.3,
  PANW -7.6 and SNOW -8.4 percent against QQQ -2.5 and SPY -1.1, after a
  10-14 leg (NET -6.7, SNOW -6.3, PANW -5.5, QQQ -2.0). Not a fetch
  defect. Part of it reversed by the 10-20 run: IOT composite 19.9 to 43.4,
  FTNT 57.0 to 75.7, MDB 26.5 to 39.2; PANW (32.8), NET (33.2), SNOW
  (35.5) and CRWD (33.7) stayed low. Hence the breadth rubric line.
- Level table: ZS (16.7, `r40_fcf` 48.7 pass) qualifies for a 4th
  consecutive window and is held under the round-7 line. OKTA (23.1, 42.4
  pass) and TEAM (24.9, 46.1 pass) at 2 of 3; IOT (25.0) still just
  outside the third slot. With no failing qualifier there is no swap
  candidate.
- Bench (fundamental, `r40_fcf`): APPF 58.8 (46.2 pass) and GWRE 46.2
  (47.0 pass) remain the ready replacements; their composite gains (APPF
  +20.2 to 68.1, GWRE +13.6 to 55.5) are technical and were not used. ZM
  33.8 (43.6 pass), WDAY 24.1 (41.4 pass). S 0.0 (23.9 fail) and ESTC 15.4
  (35.5 fail); ESTC reports 2026-11-19.
- ADSK's tape: `mixed` 10-13, `downtrend` 10-14 and 10-15 (T 4.4, 0.0),
  `uptrend` from 10-16 (T 67.5, 73.5 on 10-20); rank 4 then 3. The
  round-8 promotion needed no revisiting.
- First full window under PR #40: 10 Deterioration listings in 5 runs,
  each with two or more signals (ADSK 10-13 to 10-15 on estimates cut 20
  down vs 1 up plus a price or short-float signal, PANW 10-15 and 10-16,
  and five on 10-17: HUBS, PANW, NET, DDOG, FTNT); none on 10-20. Checked
  against the rule: each week-drop signal beside a 1-run drop had a
  decline already underway before that run (NET fell about 21 on 10-16
  before -3.5 on 10-17), and HUBS paired a whipsaw drop with short
  interest up 69 percent MoM. Working as designed; the volume is the
  selloff, not double counting.
- LLM tone (PR #39): no rejection logged in 30 calls (6 runs, 10-13 to
  10-20).
- Decay gate: 0 hits in 25 runs (the retained history, 09-16 to 10-20).
  Lowest `r40_trend` RBRK -0.035, then ZS -0.018, TEAM -0.007, OKTA
  -0.006, against -0.10. Coverage 18 of 20 live (MNDY, SHOP in warm-up).
- Automate-vs-manual: stay manual (owner decision). Revisit when the gate
  first fires.
- Watch items for round 11: OKTA and TEAM reach 3 of 3 if they hold the
  slots; both pass the rule, so the level table would be three held
  qualifiers, and the absolute floor named under the level rule becomes
  the question if that persists. ZS in its 5th window. The earnings
  season can step fundamentals on a non-boundary run (round-5 line).
  PANW, NET, SNOW and CRWD tapes after the selloff. ESTC through 11-19.
  If PR #45 (scoring method 2) merges before or inside the window, CRWD,
  IOT and DDOG step +10 F (about +6 composite) on the switch run by
  construction: dilution moved to basic shares and their flag cleared.
  Read that step as the method change, not the business.
- What mattered: the flat fundamental column and checking the drop against
  QQQ and peers, which made the whole attention list one market event.
  What was noise: every attention-list delta, change activity (MNDY 9 on
  a T of 58 to 0.8 to 91.9 whipsaw, HUBS 6) and the bench composite gains.

**Round 11 (issue #46, 2026-10-24, window 2026-10-20 to 2026-10-24, 5 runs):**
- Outcome: no changes. Third digest delivered by the `workflow_run` chain;
  every daily-report and weekly-refresh run in the window succeeded.
  Attention list: no persistent decay; three persistently weak rows.
- Level table: ZS (16.7, `r40_fcf` 48.7 pass, 5th consecutive window), OKTA
  (23.1, 42.4 pass) and TEAM (24.9, 46.1 pass) all qualify at 3 of 3. This
  is the case round 10 anticipated: three passing qualifiers, all held under
  the round-7 line, so no swap candidate. IOT leaving 25.0 on the switch run
  settled the IOT against TEAM coin toss (rounds 9 and 10) for TEAM.
- Absolute floor deferred (owner decision). All three qualifiers sit low on
  penalties, not on thesis: ZS carries dilution (3.44 percent on basic
  shares against the 3.0 limit) and both SBC tiers (SBC 24.5 percent of
  revenue), TEAM both SBC tiers (24.4 percent, while its basic average
  shares fell 3.8 percent), OKTA the first tier (16.4 percent). A floor on the raw
  fundamental score would count the same penalties a second time, so it is
  not adopted on this evidence. Revisit after TEAM (11-05) and ZS (11-24)
  report, or when a qualifier fails the rule.
- The PR #45 method switch landed on the 10-21 run as predicted: CRWD 28.0
  to 38.0, DDOG 33.2 to 43.2, IOT 25.0 to 35.0, each with its dilution flag
  cleared (the digest's 3 flag-cleared rows), and that run's report printed
  the one-run method note. Every other fundamental score, watchlist and
  bench, is identical across the 7 runs from 10-16 to 10-24, and
  `data/cache` has only bot commits since 10-17 (no apply), so the 10-17 to
  10-20 boundary carries no step. Every cache refetched on 10-21 (PR #45
  Amendment 1; APPF again on 10-24). Hence the method-switch rubric line.
- Earnings season: no new quarter landed in the window. APPF reported 10-22
  and was refetched on 10-24, but Yahoo had not posted the quarter yet
  (newest cached quarter 2026-06-30, F flat at 58.8).
- Breadth first, then names: the 10-24 run (10-23 close) took NET T 36.1 to
  83.5 and PANW 24.2 to 83.6, both `mixed` to `uptrend`, NET composite 37.6
  to 56.6 (rank 19 to 9) and PANW 38.5 to 62.3, on a security-name rally
  (CRWD +8.2, NET +7.3, PANW +7.2, S +6.7 percent) with QQQ flat and SPY
  -0.5. CRWD returned to `uptrend` on the 10-22 run (T 42.2 to 82.9), HUBS
  on the 10-21 run (T 16.2 to 77.6). Of the four tapes on the round-10
  watch, NET, PANW and CRWD recovered; SNOW stays `mixed` (T about 43, rank
  20), as does MDB (T about 42).
- Bench (fundamental, `r40_fcf`): APPF 58.8 (46.2 pass) and GWRE 46.2
  (47.0 pass) remain the ready replacements above the tail. APPF's
  composite fell 68.3 to 52.1 on the 10-24 run (a -3.1 percent session
  after its report) with F flat, so technical; ZM 33.8 (43.6 pass) fell
  51.7 to 26.9 over the last two runs, also technical. WDAY 24.1 (41.4
  pass). S 0.0 (23.9 fail) and ESTC 15.4 (35.5 fail); ESTC reports 11-19.
- Deterioration watch: one listing in 5 runs, ADSK on 10-24 (composite
  -5.4 on one run, estimates cut 20 down vs 1 up over 30 days), the same
  estimate signal behind its 10-13 to 10-15 listings. ADSK stays 3rd on F
  73.6; the drop is T 82.2 to 68.7.
- LLM tone (PR #39): 2 rejections in 25 calls, both on 10-21 (barrons and
  neutral-analyst, each a narrative that dropped a digest ticker, DT and
  MNDY), each logged with its reason as PR #39 intended.
- Decay gate: 0 hits in 25 runs (the retained history, 09-22 to 10-24).
  Lowest `r40_trend` unchanged: RBRK -0.035, ZS -0.018, TEAM -0.007, OKTA
  -0.006, against -0.10. Coverage 18 of 20 live (MNDY, SHOP in warm-up).
- Automate-vs-manual: stay manual. Revisit when the gate first fires.
- Watch items for round 12: FTNT and NOW report 10-28, inside the window,
  so their fundamentals can step on a non-boundary run (round-5 line).
  APPF's quarter when Yahoo posts it. ZS in its 6th window. SNOW's tape.
  The early-November cluster lands in round 13's window: PLTR 11-02 (a
  Monday, so its step would land on the boundary run, invisible to the
  window delta), DT and SHOP 11-03, DDOG and HUBS 11-04, NET and TEAM
  11-05. MNDY 11-09 is also a Monday and lands in round 14.
- What mattered: the run `method` field and the method note (the only
  fundamental moves in the window), the `r40_fcf` column, and the penalty
  breakdown of the three qualifiers (the floor decision). What was noise:
  change activity (DDOG 6, CRWD 5, NET 4), the bench composite moves and
  the 10-24 rank jumps.

**Round 12 (issue #48, 2026-10-31, window 2026-10-27 to 2026-10-31, 5 runs;
recorded 2026-11-14 with round 13):**
- Outcome: no changes. The digest opened #48 on schedule but the round was
  not worked that week; this catch-up record reads the same sources a
  weekly round does (retained run history, the window's reports and run
  logs, live closes). Every daily-report and weekly-refresh run in the
  window succeeded. Attention list: 10 persistent-decay rows, all at 0 of
  5 gate hits; three persistently weak rows.
- Level table: ZS (16.7, `r40_fcf` 48.7 pass, 6th consecutive window),
  OKTA (23.1, 42.4 pass) and TEAM (24.9, 46.1 pass, both 4th) qualify. All
  three pass, so all are held under the round-7 line and there is no swap
  candidate. Floor still deferred (round 11).
- Every watchlist fundamental score is flat across the window, so each
  attention delta is 0.4 times its technical move: two earnings reactions
  inside a two-session software selloff, plus two earlier cliffs. FTNT
  (-33.6) and NOW (-33.0) reported 10-28 and fell 20.1 and 26.9 percent on
  the 10-29 close (QQQ -1.4, IGV -3.5), T to 0 on the 10-30 run; the same
  session took SNOW (-2.8 percent) from `uptrend` to `mixed`, T 72.3 to
  31.6. The 10-30 close (QQQ -3.5, SPY -1.8, IGV -3.4) took DDOG -11.5, MDB
  -8.3, SNOW -7.3, PLTR -6.3, TEAM -6.1 and PANW -3.9 percent, read on the
  10-31 run (DDOG T 45.8 to 0, SNOW 31.6 to 11.5, MDB 73.2 to 8.4, PANW
  75.8 to 23.9, PLTR 84.1 to 43.2, TEAM 83.5 to 42.6). NET (T 82.9 to 28.7
  on the 10-29 run, a -2.8 percent session) stepped before the selloff, the
  trend-state cliff of the round-10 line, and MNDY's -6.2 (warm-up) is
  mostly the 10-28 run (T 77.6 to 56.0 after a -4.0 percent session).
- FTNT's new quarter first posted as a stub on 10-30 (2 of 11 fields); the
  scorer skipped it with a data note and scored the prior quarter, and the
  step landed on 11-04 (round 13). APPF's quarter (reported 10-22) landed
  on the 10-27 boundary run, F 58.8 to 55.5 and `r40_fcf` 46.2 to 45.5, so
  its -3.0 window delta does not show it (round-5 line).
- Bench (fundamental, `r40_fcf`): APPF 55.5 (45.5 pass) and GWRE 46.2
  (47.0 pass) remain the ready replacements above the tail; GWRE's
  composite -22.7 and ZM's -21.2 are technical. ZM 33.8 (43.6 pass), WDAY
  24.1 (41.4 pass), S 0.0 (23.9 fail), ESTC 15.4 (35.5 fail).
- Deterioration watch: 11 listings in 5 runs. ADSK on 10-27 to 10-30 and
  HUBS on four runs pair a drop with a standing signal (ADSK's estimates
  cut, 20 down vs 1 up; HUBS's short interest, up 69 then 21 percent MoM);
  on 10-31 DDOG, NET and PLTR (-16.4 on one run, estimates cut 2 down vs 0
  up). FTNT and NOW were not listed although they lost 27.8 and 32.8
  composite points on the 10-30 run: one price event counts as one signal (PR #40), and the
  week signal needs a decline already underway before the run. Each
  surfaced once a second signal arrived (FTNT on 11-03 with a downtrend
  break, NOW on 11-04 with an estimates cut). Working as designed.
- LLM tone (PR #39): 2 rejections in 25 calls (10-28 and 10-29), each a
  narrative that dropped a digest ticker (ZS, NET).
- Decay gate: 0 hits. Lowest `r40_trend` unchanged: RBRK -0.035, ZS
  -0.018, TEAM -0.007, OKTA -0.006, against -0.10. Coverage 18 of 20 live
  (MNDY, SHOP in warm-up).
- Automate-vs-manual: stay manual.
- What mattered: the flat fundamental column and the closes, which made
  the list two earnings reactions and a two-session software selloff, with
  NET's and MNDY's earlier cliffs (and SNOW's tape, a round 11 watch item,
  part of the selloff). What was noise:
  every attention delta, change activity (rank 28, score 26; HUBS 9 on its
  10-28 golden cross, the window's one flag set) and the bench composite
  drops.

**Round 13 (issue #49, 2026-11-07, window 2026-11-03 to 2026-11-07, 5 runs):**
- Outcome: no changes (owner decision 2026-11-14, worked a week late with
  round 12's catch-up). Every daily-report and weekly-refresh run in the
  window succeeded. Attention list: 11 persistent-decay rows, all at 0 of
  5 gate hits; three persistently weak rows.
- Level table: ZS (16.7, `r40_fcf` 48.7 pass, 7th consecutive window),
  OKTA (23.1, 42.4 pass) and TEAM (24.9, 46.1 pass, both 5th) qualify. All
  three pass and are held; no swap candidate. The floor stays deferred:
  TEAM reported 11-05 but its quarter landed in the cache only on 11-13
  (round 14; see the posting-lag rubric line), and ZS moved its report from
  11-24 to 12-02.
- Boundary: no fundamental step from 10-31 to 11-03 (`data/cache` has only
  bot commits, no apply; FTNT still on its stub). The 11-03 boundary run
  did carry technical cliffs the window deltas cannot see (round-5 line):
  DT 83.3 to 42.7, OKTA 83.5 to 41.9, HUBS 57.9 to 14.0.
- Breadth first: earnings reactions and one market-wide session. DT -14.7
  and SHOP -8.7 percent on the 11-03 close (both reported that day), DDOG
  -14.4 on 11-04, TEAM -16.2 on 11-06, and MNDY -6.3 on 11-06, three days
  before its report (T 59.5 to 15.1 on the 11-07 run). The 11-05 close
  (QQQ -6.5, SPY -4.4, IGV -4.4) took RBRK -11.1, OKTA -10.4, CRWD -9.9,
  PLTR -9.7, PANW -9.6 and ZS -9.2 percent, read on the 11-06 run: GTLB T
  83.6 to 42.7, RBRK 83.1 to 41.7, ZS 80.2 to 39.2, PLTR 32.5 to 0.8.
  CRWD's step (T 83.2 to 43.0) came
  on the 11-05 run, a -1.5 percent session at the end of a slide of about
  10 percent from 10-28. ADSK and IOT held `uptrend` throughout; ADSK is
  rank 1 from 11-05.
- Attribution: every attention delta reconciles as 0.6 times the F move
  plus 0.4 times the T move. CRWD -27.7, SHOP -27.6, RBRK -17.8, GTLB and
  ZS -16.4, MNDY -15.9 (warm-up), PLTR -13.5 and TEAM -8.0 are technical;
  DT -17.8 and HUBS -7.6 are mostly technical (their quarters: F 40.7 to
  39.5 on 11-06, 39.0 to 35.7 on 11-07). NOW's -6.5 is entirely
  fundamental: T was already 0 from 10-30, and its quarter (landed 11-05)
  set `high_sbc` with TTM SBC at 15.05 percent of revenue against the 15.0
  limit (14.83 before), a -10 penalty for 0.22 points of SBC: F 63.2 to
  52.4, `r40_fcf` 52.6 still passing, rank 6. The digest's one flag-set
  row, a cliff rather than a thesis change (round 7).
- The same cliff runs the other way without a flag: DDOG's quarter (11-07)
  took `r40_ebitda` from 38.6 to 42.0 across the +10 bonus at 40, so F
  43.2 to 54.9 (+11.7, the rest from the gradual terms) and rank 16 to 5
  with every flag unchanged. DDOG's T had been 0 since 10-31, so its 11-04
  session could not move its composite and the step only lifted it (about
  +7); DDOG never reached the attention list. An F step near 10 with no
  flag change may be this bonus, so check `r40_ebitda` against 40 first:
  TEAM's +11.3 on 11-13 (round 14) came from the gradual terms alone
  (`r40_fcf`, rule of x, `r40_trend` turning positive) with `r40_ebitda`
  at 33.2. FTNT's filled quarter (11-04, the stub of the posting-lag
  rubric line): F 83.3 to 87.8, `r40_fcf` 63.8, revisions 36 to 1
  (round-4 rollover).
- Bench: APPF 55.5 (45.5 pass, `uptrend`) and GWRE 46.2 (47.0 pass) remain
  the ready replacements above the tail. WDAY's composite -20.2 and S's
  -24.5 are technical. Fundamentals as in round 12.
- Deterioration watch: 35 listings in 5 runs (6 to 8 a run). Most pair a
  week drop with a standing signal re-read on every run: short interest up
  MoM (DT 38, SHOP 26, HUBS 21 percent), estimates cuts (ADSK 20 down vs 1
  up, standing since 10-13; PLTR 2, then 4, then 6 down vs 0 up), and
  post-earnings cuts (NOW 4 down vs 1 up, TEAM 24 down vs 1 up from 11-07) that the
  round-4 line reads as a possible rollover. DT, HUBS and PLTR were listed
  on all 5 runs. As designed; the volume is the selloff and the earnings
  cluster.
- LLM tone (PR #39): 4 rejections in 25 calls (11-04, two on 11-06,
  11-07), all a narrative that dropped a digest ticker (NOW, SNOW, MDB,
  DDOG). Against 0 of 30 in round 10 and 2 of 25 in rounds 11 and 12,
  worth watching; every rejection so far is the same failure.
- Decay gate: 0 hits in all 25 runs retained on 11-14 (10-13 to 11-14).
  Lowest `r40_trend` in the window RBRK -0.035, ZS -0.018, TEAM -0.007,
  OKTA -0.006, against -0.10. Coverage 18 of 20 live (MNDY, SHOP in warm-up).
- Automate-vs-manual: stay manual. Revisit when the gate first fires.
- Carried to round 14: TEAM's quarter and the floor question it reopens;
  MNDY (reported 11-09), NET and PLTR, whose steps landed on the 11-10
  boundary run where the window delta cannot see them; post-earnings
  revisions for NOW and TEAM; the tone rejection rate. PANW 11-18 and ESTC
  11-19 report in round 15's window.
- What mattered: the per-run F and T columns read against the closes,
  which split the eleven rows into one market session (GTLB, RBRK, ZS,
  PLTR, and CRWD at the end of its slide), earnings reactions (DT, SHOP,
  TEAM, and MNDY ahead of its report) and fundamental steps (NOW's
  `high_sbc` cliff, HUBS's quarter after its boundary cliff); DDOG's
  opposite cliff, which explains its absence; and the parquet's newest
  column (the FTNT stub). What was noise: the eleven attention deltas, the
  Deterioration volume and change activity (rank 17, score 26, trend state
  15; HUBS 9, CRWD 7).

**Round 14 (issue #50, 2026-11-14, window 2026-11-10 to 2026-11-14, 5 runs):**
- Outcome: one swap, ZS out (demoted to the bench, still shadow-scored and
  keeps its cache) and APPF in, the first under the amended level rule
  (condition 2 and the retired round-7 bullet above). Owner decision
  2026-11-14, after the review found that rounds 9 to 13 could not have
  swapped any name. Every daily-report and weekly-refresh run in the window
  succeeded.
- Level table: ZS (16.7, `r40_fcf` 48.7, `r40_sbc_adj` 24.1, 8th
  consecutive window) and OKTA (23.1, 42.4 and 26.1, 6th) qualify. One swap
  per round, longest-standing first, so ZS goes now and OKTA is next in line
  for round 15 if it still qualifies. RBRK (27.3, 61.0 and 38.8) enters at
  1 of 3, with dilution, both SBC tiers and the lowest `r40_trend` in the
  universe (-0.035). TEAM left the bottom 3 on its own quarter (11-13): F
  24.9 to 36.2 from `r40_fcf` 46.1 to 50.7 and `r40_trend` turning positive
  (-0.007 to +0.045, the digest's one inflection row), both SBC flags still
  set.
- Replacement: APPF is the highest-scoring bench name (fundamental 55.5,
  `r40_fcf` 45.5, no flags, `r40_trend` 0.036 live, `uptrend`, composite
  66.7) and enters 5th of 20 on the fundamental leg, well above the tail
  (after the swap the bottom 3 are OKTA 23.1, RBRK 27.3 and GTLB 30.7). Its
  cache already
  holds 16 quarters from the round-7 seed, so no backfill apply is needed.
  Its `r40_sbc_adj` was not checked (bench names are not in the daily raw
  file); no SBC flag means SBC is under 15 percent of revenue.
- Absolute floor: closed. Round 11 deferred it until TEAM and ZS reported.
  The bottom slots did not need a floor; they needed the round-7 hold gone.
- Boundary: MNDY (reported 11-09), NET and PLTR stepped on the 11-10
  boundary run, invisible to the window delta (round-5 line): MNDY F 48.8
  to 41.1 and `r40_fcf` 48.3 to 42.2 (composite 35.3 to 24.7, rank 10 to
  16, still in warm-up), NET 38.6 to 39.7, PLTR clamped at 100 (`r40_fcf`
  133.5 to 142.7). TEAM's 11-13 step is the only fundamental step in the
  window.
- Attribution: ZS's decay row (24.7 to 19.2, -5.5, rank 15 to 19) is
  technical, with F flat at 16.7. Most of it came on the 11-12 run, when
  the 3-month relative-strength input fell for nearly every name at once:
  its 63-session window dropped 2026-08-13, a strong software session
  (ZS +6.1, RBRK +8.6, WDAY +17.8 percent against SPY +0.7), so each name's
  3-month outperformance against SPY measured lower (ZS 0.130 to 0.052).
  That is the measure working as designed. ZS's T went 34.7 to 19.5 on that
  run, and the same run listed ZS and RBRK in the Deterioration watch.
  ADSK's +9.9 (golden cross on 11-14, T 91.4, rank 1 since 11-05) is the
  window's only large move up. HUBS's golden cross cleared on 11-11 by
  lookback expiry (set 10-28).
- Bench: composite moves are technical (ZM +18.6, WDAY +14.4, S -18.7,
  ESTC -5.8); fundamentals unchanged. After this swap the bench is WDAY
  24.1, ZM 33.8, S 0.0, ZS 16.7, GWRE 46.2 and ESTC 15.4 on the
  fundamental leg. GWRE is the replacement for OKTA in round 15; after it,
  only ZM clears the tail, by a hair, so the bench needs new candidates
  soon (round-7 screen pattern).
- Deterioration watch: 20 listings in 5 runs (7, 3, 7, 3, 0); 11-14 printed
  "nothing cleared a threshold". NOW's post-earnings cuts widened to 32
  down vs 1 up from 11-10.
- LLM tone (PR #39): 3 rejections in 25 calls (11-10, 11-11, 11-14), each a
  narrative that dropped a digest ticker (MNDY, MDB, HUBS).
- Decay gate: 0 hits. Lowest `r40_trend` RBRK -0.035, ZS -0.018, OKTA
  -0.006 against -0.10. Coverage 18 of 20 live (MNDY, SHOP still in
  warm-up).
- Automate-vs-manual: stay manual.
- Watch items for round 15: OKTA against GWRE under the amended rule. APPF's
  first window on the watchlist. PANW (11-18) and ESTC (11-19) report. The
  digest wording now states the amended rule.
- What mattered: the level table's window counts read against the bench's
  fundamental scores, once the hold no longer overruled them. What was
  noise: change activity (score 11, revisions 9, short interest 8).

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
