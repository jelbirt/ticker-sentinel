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
