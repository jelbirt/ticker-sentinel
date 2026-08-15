# Spec: history backfill (one-time SEC EDGAR deepening of the committed cache)

Status: APPROVED 2026-08-15 (D1 b, D2 1 percent relative with 100k absolute
floor, D3 yes with riders)

This spec is scoped to the `history-backfill` workstream. It does not replace
`SPEC.md`, which remains the live Phase 4 spec (and holds the rotation rubric
the weekly digest references).

## Objective

The committed cache holds roughly 6 to 7 quarters per ticker. Growth needs 8
quarters and `r40_trend` needs 12, so four capabilities are inert until roughly
mid-2027 if history only deepens one quarter per quarter:

- the F-score trend term (`15 x clamp(r40_trend / 15, -1, 1)`),
- R40-inflection change detection,
- the Deterioration watch R40 signal,
- the weekly digest decay gate.

Build a one-time backfill tool that deepens `data/cache/*.parquet` to at least
12 complete quarters per ticker from SEC EDGAR, verified against the existing
cache before anything is written. Steady state stays yfinance: EDGAR is never
wired into the scheduled run.

## Source and client

SEC EDGAR companyfacts XBRL API
(`https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json`), free, no key,
with a declared User-Agent and a request throttle.

The client is adapted from the owner's own split-signal project
(`src/split_signal/data/edgar.py`: CIK lookup, throttled session, tag-alias
normalization, quarterly/ytd/annual period typing) into a self-contained
`src/sentinel/data/edgar.py`. Keep an attribution comment pointing at
split-signal.

## Field mapping

Canonical field <- us-gaap tags, first match wins:

| canonical | us-gaap tags |
|---|---|
| `revenue` | `Revenues`, `RevenueFromContractWithCustomerExcludingAssessedTax`, `RevenueFromContractWithCustomerIncludingAssessedTax`, `SalesRevenueNet` |
| `operating_income` | `OperatingIncomeLoss` |
| `d_and_a` | `DepreciationDepletionAndAmortization`, `DepreciationAndAmortization`, `DepreciationAmortizationAndAccretionNet` |
| `ocf` | `NetCashProvidedByUsedInOperatingActivities`, `NetCashProvidedByUsedInOperatingActivitiesContinuingOperations` |
| `capex` | `PaymentsToAcquirePropertyPlantAndEquipment`, `PaymentsToAcquireProductiveAssets` |
| `sbc` | `ShareBasedCompensation` |
| `diluted_shares` | `WeightedAverageNumberOfDilutedSharesOutstanding` |

NOT backfilled: `ebitda` (the OpInc + D&A fallback in `build_ttm` computes it),
`total_debt`, `cash` (EV uses only the newest reading, so backfilled quarters
keep NaN).

Signs are normalized to the cache convention: capex is a positive magnitude
(see `NEGATIVE_MAGNITUDE_FIELDS` in `src/sentinel/data/fundamentals.py`).

## Quarterly derivation

- Income-statement and share facts are filed per quarter in 10-Qs. Q4 is
  derived as FY minus (Q1 + Q2 + Q3) when it is not filed directly.
- Cash-flow facts are year-to-date in 10-Qs: quarter n = YTD(n) - YTD(n-1),
  Q1 = YTD Q1, Q4 = FY - YTD Q3.
- A missing intermediate YTD leaves that quarter NaN. `build_ttm`'s all-4 rule
  and the contiguity guard handle it downstream.
- Restatements: the latest filed date wins per (metric, period_end). This is
  deliberately the opposite of split-signal's point-in-time rule (earliest
  filing wins), because the cache mirrors yfinance's restated view.
- Columns are labeled by fiscal period_end, matching the cache convention.

### Implementation note: weighted averages are not derived

Added during implementation, not part of the approved text. The approved spec
groups "income/share facts" together under "Q4 = FY minus (Q1 + Q2 + Q3)", but
`diluted_shares` is a weighted average, not a flow: differencing a year-to-date
mean, or subtracting three quarters from a fiscal-year mean, produces nonsense
(an FY mean of 241M minus three quarterly means near 241M would land at roughly
-482M). `diluted_shares` therefore accepts DIRECT 3-month facts only and stays
NaN otherwise. In practice that leaves most backfilled Q4 columns without a
share count, since a 10-K's income statement carries the annual column rather
than a Q4 one. `_paired_shares` already degrades to None on a missing reading,
and the affected columns sit deeper than the yfinance cache, so no live metric
changes.

## Verification gate (D2)

Per ticker, before any write:

1. Build the canonical frame from EDGAR.
2. Compare EVERY overlap quarter against the cached yfinance parquet, on the
   mapped fields, where both sides have a value.
3. Match = relative difference <= 1 percent, OR absolute difference <= 100000
   (the absolute floor keeps near-zero values from failing on rounding).
4. All overlaps match -> ACCEPT: merge the EDGAR quarters older than the cache
   using `cache.merge_statements` semantics (yfinance wins on overlap), capped
   at `MAX_QUARTERS` (16).
5. Any mismatch -> REJECT the whole ticker, with a report line naming the
   field, the quarter, and both values. No partial trust.

MNDY is excluded a priori (foreign private issuer, files 20-F, no 10-Q XBRL
quarterly facts) with a documented skip.

## CLI

`python -m sentinel.backfill [--dry-run | --apply] [--tickers ...]`

- Both modes fetch EDGAR live. This is a tool, not a scheduled job.
- `--dry-run` (the default) writes NOTHING. Verifiable as: `git status` clean
  and `data/cache` byte-identical afterwards. It prints the full per-ticker
  report: accept or reject, quarters gained, and every overlap comparison.
- `--apply` rewrites the accepted tickers' parquets.
- Default universe: the watchlist r40 tickers plus the bench (D3).

## D3 riders (both in scope)

1. `cache.prune`'s keep-set must include the bench. `run.py` currently prunes
   with `cfg.all_tickers`, so bench parquets would be deleted on the next
   scheduled run. Change plus test.
2. The bench names (WDAY, SHOP, TWLO, ZM) have no yfinance cache to verify
   against, so the tool fetches their current statements through the existing
   yfinance path to create the overlap. In `--dry-run` that fetch is in-memory
   only (nothing written); in `--apply` it seeds their yfinance cache via the
   normal `cache.save` and then merges the verified EDGAR history.

## Warm-up disclosure (ships regardless)

`run.py` gains a pure helper `_trend_warmup_note(scorecards)` returning one
aggregate data note when any SCORED name lacks `r40_trend`, for example:

`R40 trend warming up: n/a for 19 of 20 scored names (needs 12 cached quarters;
the committed cache deepens by 4 per year)`

It is appended to `notes` after scoring and self-erases as history deepens.
Unit tests cover scored-only counting, silence when every scored name has a
trend, and silence on an empty scorecard list.

## Docs

- `PROJECT_PLAN.md` section 4 gains the warm-up timeline and the backfill
  mechanism (one-time EDGAR tool, verification gate, MNDY exclusion).
- The section 12 risk row about shallow history moves from "open owner
  decision" to the decided mechanism.
- No em dashes or en dashes in anything user-visible, the report output and
  the data notes included.

## Pen rule (D1 = option b)

This branch must NOT modify `data/cache/` contents. The `--apply` run happens
after the merge, as a separate owner-approved commit. The PR body repeats this.

## Amendment 1 (approved 2026-08-15)

Status: APPROVED 2026-08-15. Three changes, driven by the live dry run on PR
#10 (6 accepts, 17 rejects) and by the apply commit `2621e02`. Everything above
this heading is the original approved text and stays as filed; where the two
disagree, this amendment governs.

### Change 1: narrow the backfilled field set

Historical quarters exist for exactly two consumers: `r40_fcf` trend and
growth. Those read `revenue`, `ocf` and `capex`, plus `sbc` for the
SBC-adjusted level and `diluted_shares` for dilution. `operating_income` and
`d_and_a` are read only from the NEWEST quarter (the `ebitda` fallback in
`build_ttm` and `op_margin`), which always comes from yfinance.

`BACKFILL_FIELDS` therefore becomes `revenue`, `ocf`, `capex`, `sbc`,
`diluted_shares`. Backfilled quarters keep NaN for `operating_income` and
`d_and_a`, and those fields leave the verification gate entirely: nothing reads
them historically, so their as-filed-vs-normalized mismatches must not reject a
ticker whose trend inputs are clean.

This dissolves the open "as-filed vs normalized operating income" question:
there is no longer a comparison to reconcile.

Judgment call, documented as required: the `TAG_MAP` entries for
`operating_income` and `d_and_a` are KEPT, not removed. They are the
documentation of which us-gaap tags those fields would come from, they keep
`facts_for_field` usable for ad-hoc inspection, and re-widening the set later
is then a one-line change. Nothing derives them: `canonical_from_companyfacts`
iterates `BACKFILL_FIELDS`, not `TAG_MAP`.

### Change 2: capex is a composite, not a single tag

yfinance's "Capital Expenditure" for SaaS filers bundles capitalized software
development with plain PP&E purchases. Read alone,
`PaymentsToAcquirePropertyPlantAndEquipment` runs 7 to 97 percent low against
the cached value, which is what most of the capex overlap failures were.

`capex` becomes a COMPOSITE field. Each component tag gets its own independent
quarterly derivation (the same YTD differencing as any other field) and the
components are summed per quarter. Deriving first and summing second is the
only correct order: a filer can report PP&E purchases per quarter while filing
capitalized software year-to-date, so summing raw facts would mix period types.

| role | us-gaap tags |
|---|---|
| base (alternatives, first present wins) | `PaymentsToAcquirePropertyPlantAndEquipment`, `PaymentsToAcquireProductiveAssets` |
| addends (summed when present) | `PaymentsToDevelopSoftware`, `PaymentsToCapitalizeInternalUseSoftware`, `PaymentsToAcquireIntangibleAssets` |

Dedupe: `PaymentsToAcquireProductiveAssets` is a broader BASE that some filers
use INSTEAD of PP&E, never an addend on top of it. When both are filed, PP&E
wins and ProductiveAssets is discarded, so the two can never double count.

Missing-quarter rules, deliberately asymmetric:

- A quarter missing from the BASE component leaves that quarter absent (NaN
  downstream). The base is the bulk of the number; without it there is nothing
  to report.
- A quarter missing from an optional ADDEND treats that addend as 0 for that
  quarter ONLY when the addend tag files nothing at all ending on that period
  end. A filer that capitalized no software in a quarter simply omits the tag,
  and reading that omission as 0 is what the cash flow statement means. When
  the tag DOES file for that period end and the quarter still could not be
  derived (a missing intermediate YTD point), the addend is unknown rather than
  zero, and the whole quarter is dropped instead of being silently understated.

The verification gate stays the arbiter. The D2 tolerance is UNCHANGED (1
percent relative, 100000 absolute). A composite that still does not reconcile
inside it rejects the ticker exactly as before.

### Change 3: verified hole filling inside the cached range

Live finding from the owner-approved apply run (main commit `2621e02`): all six
accepted tickers carry 1 or 2 pre-existing HOLLOW cached columns, yfinance
shell columns with no core fields (TEAM 2024-12-31 and 2026-06-30, PANW
2025-01-31 and 2024-10-31, FTNT 2025-03-31 and 2024-12-31, ESTC and IOT
2025-01-31, PLTR 2024-12-31). `merge_backfill`'s only-strictly-older rule
preserved those holes, and `build_ttm` returns nothing usable for any window
spanning one, so growth and `r40_trend` were STILL n/a for all six despite 16
quarters of depth. The backfill bought depth and delivered no capability.

For ACCEPTED tickers the merge now also fills NaN CELLS within the cached range
from the aligned EDGAR frame:

- Cached non-NaN values always win (`cached.combine_first(edgar)` semantics).
  Only genuinely empty cells are filled.
- Only quarter columns the cache already has are touched. Hole filling fills
  cells; it does not invent quarters inside the cached range.
- The `MAX_QUARTERS` cap and the column alignment are unchanged.
- The acceptance decision still rests SOLELY on the overlap checks of non-NaN
  cached cells. Nothing about which cells get filled feeds back into it.

This supersedes the promise in the verification-gate section above and in the
`merge_backfill` docstring that "no cached cell is touched". The new promise:
no cached VALUE is overwritten; verified EDGAR values may fill empty cells.

### Unchanged by this amendment

The D2 tolerance, the all-or-nothing per-ticker rejection, the MNDY a-priori
skip, the D3 riders, the CLI contract (`--dry-run` writes nothing), and the pen
rule: this branch does not modify `data/cache/`. The `--apply` run remains a
separate owner-approved post-merge commit.

### Implementation note: the cache is no longer a pure yfinance reference

Added during implementation from the live dry run, not part of the approved
text. The verification gate's premise is that the cached frame is the yfinance
view to reconcile against. After apply commit `2621e02` that is only true for
the six applied tickers' NEWEST quarters: their deeper quarters are EDGAR
values this tool wrote. The gate now compares new EDGAR against old EDGAR
there, which is a weaker check than intended, and change 2 makes it visible:
TEAM rejects on a single 2023-06-30 capex check (2,585,000 cached vs 2,745,000
composite, 6.19 percent), a quarter that only exists in the cache because the
apply run wrote it with the pre-amendment single-tag capex. PANW, FTNT, ESTC,
IOT and PLTR happen to agree inside the tolerance and still accept.

No behavior change is made for it here: widening the tolerance is out of
bounds, and teaching the gate to skip EDGAR-written quarters is a fourth change
nobody approved. It is an owner decision for the post-merge apply run, which is
gated anyway. The options are to leave TEAM as it stands (16 quarters, holes
unfilled), or to restore TEAM's parquet to its pre-`2621e02` yfinance-only
state and re-run, which would verify the composite against yfinance as the
spec intends.

### Implementation note: tag precedence and base selection (verification pass, 2026-08-15)

Two live counterexamples forced the tag-selection rules to be stricter than
Amendment 1's prose:

1. A tag can carry facts yet derive zero quarters. PANW files 18 annual-only
   "Revenues" shells; first-tag-with-any-facts returned them and shadowed the
   contract-revenue tag holding 35 derivable quarters, leaving every
   backfilled quarter without revenue (and the gate silent, since NaN is
   never compared). Precedence now sits on derived quarters, not raw facts,
   for plain fields and composite bases alike.
2. Among base alternatives, coverage decides. PANW also files a single stray
   PP&E quarter next to a 59-quarter PaymentsToAcquireProductiveAssets
   series; "prefer PP&E" as written would have blanked capex across its
   whole history. The base with the most derivable quarters wins (ties break
   toward the earlier tag); addends still sum on top and nothing is double
   counted, which is what the preference rule existed to guarantee.
