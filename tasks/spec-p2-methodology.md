# Spec: P2 methodology fixes (dilution basis, SBC gap penalty)

Status: APPROVED 2026-10-20 (D1 a basic shares, D2 a fall back to diluted,
D3 a note without suppression, D4 a keep the SBC tier and say so)

This spec is scoped to the two P2 findings of the 2026-10-20 methodology audit.
It does not replace `SPEC.md` (the live Phase 4 spec and rotation rubric) or
`PROJECT_PLAN.md`; it names the amendments it makes to the latter.

## Objective

The fundamental score F should charge each name for what actually happened to
it. Two penalties in PROJECT_PLAN section 6 do not:

1. **Dilution misfires across a loss-to-profit turn.** `dilution` compares
   yfinance "Diluted Average Shares" a year apart. Under ASC 260 a loss quarter
   reports diluted equal to basic (options and RSUs are antidilutive and
   excluded), while a profitable quarter adds them, so a name that turns
   profitable inside the year shows its option overhang as new issuance.
   Separately, the treasury stock method makes the overhang grow with the
   share price, so the diluted series also tracks the stock.
2. **The SBC gap penalty duplicates the SBC intensity penalty.**
   `r40_fcf - r40_sbc_adj` reduces algebraically to `SBC_TTM / Rev_TTM`, which
   is `sbc_intensity`. So "gap > 20 pts" is "SBC > 20%", a strict subset of
   "SBC > 15%". The plan presents them as two independent signals; they are
   one signal applied twice.

Success: each penalty measures a distinct, real property, the plan text says
what the code does, and the change lands without a fabricated move in change
detection or the weekly digest.

## Evidence (2026-10-20 cache, live yfinance for net income and basic shares)

### Dilution

| name | net income turn | diluted YoY (now) | basic YoY | flag now | flag on basic |
|---|---|---|---|---|---|
| CRWD | loss to profit at 2026-01-31 (-34.0M to +59.4M) | 4.5% | 2.0% | yes | no |
| IOT | loss to profit at 2025-10-31 (-16.8M to +7.8M) | 3.3% | 2.2% | yes | no |
| DDOG | none: profitable throughout (2.6M at 2025-06-30) | 3.4% | 2.9% | yes | no |

DDOG shows the second mechanism: the diluted overhang (diluted minus basic)
grew from 12.5M to 14.7M shares with no profit turn. Under the treasury stock
method the overhang rises with the share price, so the diluted series moves
with the stock as well as with issuance. Basic shares are free of both.

Every other name keeps its flag state on basic shares. PANW stays flagged
(22.0% basic, the CyberArk share issuance, real). MNDY stays unflagged
(-15.0%, buyback, real). CRWD's diluted count stepped 2.7% in the single
profit-turn quarter (1005.3M to 1032.5M) while basic moved 0.5%.

### SBC gap

`r40_fcf - r40_sbc_adj` equals `sbc_intensity` to the decimal for all 26
cached names. 9 universe names sit above 20% SBC and take -20 for it today:
ZS, TEAM, RBRK, CRWD, GTLB, DDOG, SNOW, NET, MDB (plus S on the bench).

### F under each option (universe; bench marked *)

`D1` = dilution on basic shares. `a` keeps the SBC tier as is, `b` drops the
third penalty, `c` redefines it as "passes R40 on FCF but not SBC-adjusted".

| name | F now | D1 (+a) | D1+b | D1+c | SBC % |
|---|---|---|---|---|---|
| ZS | 16.7 | 16.7 | 26.7 | 16.7 | 24.5 |
| OKTA | 23.1 | 23.1 | 23.1 | 13.1 | 16.4 |
| TEAM | 24.9 | 24.9 | 34.9 | 24.9 | 24.4 |
| IOT | 25.0 | 35.0 | 35.0 | 25.0 | 17.9 |
| RBRK | 27.3 | 27.3 | 37.3 | 27.3 | 22.2 |
| CRWD | 28.0 | 38.0 | 48.0 | 38.0 | 23.1 |
| GTLB | 30.7 | 30.7 | 40.7 | 30.7 | 21.8 |
| DDOG | 33.2 | 43.2 | 53.2 | 43.2 | 20.7 |
| SNOW | 33.3 | 33.3 | 43.3 | 33.3 | 30.2 |
| NET | 38.6 | 38.6 | 48.6 | 38.6 | 20.2 |
| HUBS | 39.0 | 39.0 | 39.0 | 29.0 | 14.9 |
| DT | 40.7 | 40.7 | 40.7 | 30.7 | 14.4 |
| MDB | 44.1 | 44.1 | 54.1 | 44.1 | 20.3 |
| PANW | 48.1 | 48.1 | 48.1 | 48.1 | 15.5 |
| MNDY | 48.8 | 48.8 | 48.8 | 38.8 | 11.7 |
| SHOP | 62.7 | 62.7 | 62.7 | 62.7 | 3.6 |
| NOW | 63.2 | 63.2 | 63.2 | 53.2 | 14.8 |
| ADSK | 73.6 | 73.6 | 73.6 | 73.6 | 9.1 |
| FTNT | 83.3 | 83.3 | 83.3 | 83.3 | 3.9 |
| PLTR | 100.0 | 100.0 | 100.0 | 100.0 | 13.6 |
| WDAY* | 24.1 | 24.1 | 24.1 | 14.1 | 16.2 |
| ZM* | 33.8 | 33.8 | 33.8 | 23.8 | 14.6 |
| GWRE* | 46.2 | 46.2 | 46.2 | 36.2 | 12.3 |
| APPF* | 58.8 | 58.8 | 58.8 | 48.8 | 7.2 |

The level rule's bottom 3 is ZS, OKTA, TEAM under every combination, so no
option disturbs the round-11 rotation table's membership. Composite moves are
0.6 x the F move.

## Design

### Item 1: dilution basis

- New canonical row `basic_shares`, alias `("income", ["Basic Average Shares"])`
  in `ALIASES` (`src/sentinel/data/fundamentals.py`). yfinance serves it for
  all 26 names with at least 5 quarters, which is all the YoY needs.
- `diluted_shares` stays: it is the guarded cross-check for the market-cap
  repricer (`run._repricing_shares`, PR #44) and its fallback.
- Share integrity applies to both rows: `sanitize_share_counts` checks
  `basic_shares` against shares outstanding the same way it checks diluted
  (basic sits at or under outstanding, so the existing band fits), and
  `cache.REBASED_ROWS` gains `basic_shares` so a split rebases cached-only
  quarters of both rows.
- `_paired_shares(stmts, row)` takes the row name; `FundamentalInputs` gains
  `basic_shares_now` and `basic_shares_1y_ago`; `compute_scorecard` computes
  dilution from basic, falling back per D2.
- Threshold unchanged: flag and -10 when dilution > 3% a year.
- EDGAR backfill does not need the row (dilution reads only the newest five
  quarters, the same reason Amendment 2 dropped `diluted_shares`). No
  `edgar.py` change.
- PROJECT_PLAN section 5 amendment: `dilution = (BasicShares_now /
  BasicShares_1y_ago) - 1`, with one sentence on why (ASC 260 antidilution
  makes the diluted series change basis when profitability turns).

### Item 2: SBC gap penalty (shape depends on D4)

- **a (keep the tier, say so):** score unchanged. The code expresses the third
  penalty as `sbc_intensity > 0.20` directly instead of via the gap, so it
  cannot be read as a separate signal again; PROJECT_PLAN section 6 line 111
  is rewritten as "-10 if SBC > 15% of revenue, a further -10 if SBC > 20%".
  `sbc_inflated` renders only when `high_sbc` would not already say the same
  thing, or merges into it (D4 rider).
- **b (drop it):** the third penalty and the `sbc_inflated` flag go; SBC costs
  at most -10. The `r40_sbc_adj` variant still feeds breadth ranking and the
  `passes_all_r40` star, so heavy SBC is still visible.
- **c (redefine it):** -10 when `r40_fcf >= 40` and `r40_sbc_adj < 40` (passes
  only because SBC is added back). Distinct from intensity, but it hits 19 of
  26 names including low-SBC ones near the bar (APPF at 7.2% SBC), a new
  cliff at the bar.

### Transition (D3)

Changing F changes every affected name's composite on the switch run, and
change detection would read that as a real move (score_delta, rank rows,
flag_cleared, possibly a digest change-activity spike). Options in D3.

## Decisions for the owner

- **D1, dilution basis.** (a) basic average shares always (recommended:
  issuance is what dilution means, and the option overhang is already charged
  through the SBC terms); (b) diluted when both endpoints are in the same
  profit regime, basic otherwise (needs a net income row too, and still
  flags DDOG, whose overhang grew with the share price inside one regime); (c) leave as is and disclose.
- **D2, fallback when the basic row is missing.** (a) fall back to diluted
  with no note (recommended: transitional only, every cache refetches within
  7 days and gains the row); (b) n/a until basic arrives (drops the dilution
  term for up to a week).
- **D3, switch-day handling.** (a) one-run data note on the switch run naming
  the method change, and a round-11 line in SPEC 7.0.1 so the digest reader
  knows CRWD, IOT, DDOG (and any D4 names) stepped by construction
  (recommended); (b) also suppress score_delta and rank rows for that one run
  (code to remove again later); (c) nothing.
- **D4, SBC gap penalty.** (a) keep the -20 tier for SBC > 20%, rewrite the
  plan and code to say so, and stop rendering `sbc_inflated` beside
  `high_sbc` (recommended: F's base is `r40_fcf`, which ignores SBC, so the
  SBC penalties are F's only SBC charge; a 30% SBC name paying the same as a
  15.1% one would understate it); (b) drop it; (c) redefine as passes only
  with SBC added back.

## Commands

- Install: `pip install -e ".[dev]" -c constraints.txt`
- Green bar: `scripts/checks.sh` before every commit
- Offline check: `python -m sentinel.run --dry-run`

## Files touched (expected, D1 a, D2 a, D3 a, D4 a)

- `src/sentinel/data/fundamentals.py`: alias, `_paired_shares(row)`, guard over
  both rows, inputs.
- `src/sentinel/data/cache.py`: `REBASED_ROWS`.
- `src/sentinel/indicators/fundamentals.py`: inputs fields, dilution source,
  SBC flag constants.
- `src/sentinel/scoring.py`: third penalty expressed on `sbc_intensity`.
- `src/sentinel/run.py`: one-run method-change note.
- `src/sentinel/data/fixtures.py` and fixture JSON: add `basic_shares` so the
  dry run exercises the new path.
- `PROJECT_PLAN.md` sections 5 and 6; `SPEC.md` 7.0.1 note at round 11.
- Tests alongside each.

## Testing strategy

- Unit: dilution on basic with a loss-to-profit diluted step (the CRWD shape)
  reads the basic YoY and does not flag; fallback to diluted when basic is
  absent; basic row passes through the split rebase and the share guard.
- Unit: the SBC tier (D4 a) charges -10 at 15.1% and -20 at 20.1%, and the
  rendered flags do not duplicate.
- Offline recompute against the committed cache reproduces the D1 column of
  the table above (CRWD 38.0, IOT 35.0, DDOG 43.2, every other name
  unchanged).
- `--dry-run` end to end.

## Boundaries

- Always: green bar before each commit; one workstream branch; PR for owner
  review.
- Ask first: any threshold change (3%, 15%, 20%), anything touching the level
  rule or the rotation rubric beyond the round-11 note.
- Never: write `data/cache/` from the branch (the scheduled job holds the pen
  and adds `basic_shares` on its next refetch of each name); force-refresh the
  cache to make the switch land faster; change the decay gate.

### Amendment 1 (proposed in PR #45, approved by the owner 2026-10-20)

- A cache with no `basic_shares` row counts as stale once, so every name
  refetches on the first run after merge and switches basis on that run. This
  replaces the "never force-refresh" boundary above for this one migration.
  Reason: the D3 note fires once, on the first run on method 2; trickling over
  the weekly window would make that note wrong for most names and move them
  later with no explanation. Cost: one extra Yahoo fetch per name, once,
  through the existing degrade-to-cache path.
- D2 gains a disclosure: a name still scoring dilution on diluted shares (its
  migration refetch failed, or Yahoo serves no basic row) carries a data note
  saying so, every run, until it switches.
- Row presence is the schema marker, so a backfill `--apply` (which builds
  every canonical row, `basic_shares` as NaN) must not run before the
  migration refetch; after it, the row is already there.
