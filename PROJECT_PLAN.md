# Ticker Sentinel — Personal Stock Analysis & Daily Report Service
**Version:** 1.0 (planning spec) · **Date:** 2026-07-05
**Purpose:** A small personal service that pulls end-of-day stock/fundamentals data from free sources, computes a Rule-of-40-centered fundamental scorecard plus a technical/momentum overlay, ranks strongest/weakest candidates, and emails a daily HTML report to a couple of personal addresses. Runs on GitHub Actions (scheduled + ad hoc). Designed to later grow a personalized finance-news digest module.
> ⚠️ Personal research tool only. Not financial advice; outputs are heuristics, not recommendations.
---
## 1. Goals & Non-Goals
**Goals (v1):**
- Daily automated run after US market close, plus one-tap ad hoc runs (`workflow_dispatch`, works from GitHub mobile app).
- Analyze a configurable watchlist (~10–75 tickers; stocks primarily, ETFs tolerated for the technical overlay only).
- Fundamentals: Rule of 40 in three variants (FCF, EBITDA, SBC-adjusted), Rule of X, dilution and valuation guards, quarter-over-quarter R40 trend.
- Technicals: trend/momentum overlay to time entries and flag breakdowns.
- Composite ranking → "Strongest" and "Weakest" lists with per-ticker flags and explanations.
- HTML email report to N personal recipients; artifacts (CSV/JSON) attached to the workflow run.
**Non-goals (v1):** real-time data, intraday alerts, order execution, options, backtesting engine, web UI, databases/servers to maintain.
---
## 2. Architecture Overview
```
GitHub Actions (cron weekdays + workflow_dispatch)
  └── Python 3.12 job
        1. load config (watchlist.yaml, .env/Actions secrets)
        2. data layer
           ├── prices: yfinance batch download (primary)
           │            └── fallback: Twelve Data REST (free key)
           └── fundamentals: yfinance statements (quarterly + TTM)
                              cached in repo (data/cache/*.parquet), refreshed weekly or on staleness
        3. indicator engine (pandas + pandas-ta) — all computed locally
        4. scoring engine → composite score + flags per ticker
        5. report builder (Jinja2 → HTML, inline CSS, matplotlib sparkline PNGs embedded)
        6. delivery (SMTP via Gmail app password, or Resend/SendGrid free tier)
        7. persist run outputs: reports/YYYY-MM-DD/{report.html, scores.csv, raw.json}
```
**Key resilience decisions:**
- Compute all indicators locally — never depend on API indicator endpoints or their call budgets.
- One batched price pull per run (yfinance `download(tickers, period="1y")`), so a 50-ticker list ≈ 1 request, not 50.
- Fundamentals change quarterly → cache and refresh at most weekly (or when `earnings_date` passed). Keeps API load trivial and runs fast.
- GitHub-hosted runner IPs occasionally get rate-limited by Yahoo → automatic fallback to Twelve Data (~800 calls/day free) for prices; fundamentals fall back to last cached values with a staleness flag in the report.
- Every external call wrapped with retry (exponential backoff, max 3) and a per-source circuit breaker; a run must still produce a (possibly degraded, clearly-labeled) report.
---
## 3. Configuration
`config/watchlist.yaml`
```yaml
recipients:            # actual addresses live in secrets; these are labels
  - primary
  - secondary
universe:
  - ticker: CRWD
    tags: [software, r40]     # r40 tag => fundamentals scorecard applies
  - ticker: DDOG
    tags: [software, r40]
  - ticker: XOM
    tags: [energy]            # no r40 tag => technicals-only row
benchmark: SPY
report:
  top_n: 10
  bottom_n: 5
  timezone: America/New_York
  ranking: breadth       # breadth | score (see section 6)
scoring:               # weights, overridable without code changes
  fundamentals_weight: 0.6
  technicals_weight: 0.4
```
**Secrets (GitHub Actions secrets):** `SMTP_HOST/PORT/USER/PASS` (or `RESEND_API_KEY`), `RECIPIENT_EMAILS` (comma-separated), `TWELVEDATA_API_KEY`.
---
## 4. Data Layer
| Need | Primary | Fallback | Cadence |
|---|---|---|---|
| Daily OHLCV (1y history) | yfinance batch | Twelve Data `/time_series` | every run |
| Income stmt / cash flow / balance sheet (quarterly) | yfinance `Ticker.quarterly_income_stmt`, `.quarterly_cashflow`, `.quarterly_balance_sheet` | cached values + staleness flag | weekly / post-earnings |
| Shares outstanding (diluted), EV, market cap | yfinance `Ticker.info` / statements | cached | weekly |
| Sector / industry tags | yfinance `Ticker.info` | manual tag in watchlist.yaml | on add |
**Fields required per ticker (fundamentals):** Total Revenue, EBITDA (or compute: Operating Income + D&A), Operating Income, Operating Cash Flow, Capital Expenditure, Stock-Based Compensation (from cash flow stmt), Diluted Shares Outstanding, Total Debt, Cash & Equivalents.
**TTM construction:** sum of the four most recent quarters; require ≥4 quarters or mark `insufficient_data`. Also build TTM as of −2Q and −4Q for trend metrics.
**Quarter anchoring and contiguity (2026-08-14):** Yahoo publishes a fresh quarter's statements piecemeal for a few days after earnings; a newest column missing any core field (revenue, OCF, capex) is skipped (bounded at 2 columns, so a drifted field alias surfaces as degraded data instead of re-anchoring on old quarters) and the TTM windows anchor on the newest complete quarter with a "scored as of" note. Windows whose columns sit more than 120 days apart (a missing quarter) are treated as insufficient data rather than silently summing 15+ months; 4-4-5 retail calendars pass untouched.
**History depth and R40 trend warm-up (2026-08-15):** yfinance returns roughly 5 quarters at a time and the cache accumulates one more each quarter, so the committed cache holds 6 to 7 today. Growth needs 8 quarters and `r40_trend` needs 12 (TTM now against TTM at -4Q, each with a year of denominator behind it), which leaves the F-score trend term, R40-inflection change detection, the Deterioration watch R40 signal and the weekly digest decay gate inert until roughly mid-2027 on organic accumulation alone. Two responses ship together. (1) Disclosure: the report carries one aggregate data note while the metric is warming up ("R40 trend warming up: n/a for N of M scored names"), which self-erases once every scored name has a trend. (2) Mechanism: `python -m sentinel.backfill` is a one-time tool that deepens the cache from the SEC EDGAR companyfacts XBRL API (free, no key, declared User-Agent, throttled) up to the 16-quarter cap. EDGAR is never wired into the scheduled run; steady state stays yfinance. Income-statement facts come per quarter from 10-Qs, cash-flow facts are differenced out of the year-to-date figures, and Q4 is derived from the fiscal year minus the first three quarters. Nothing is written until a ticker passes the verification gate: every quarter present in both the EDGAR-derived frame and the cached yfinance frame must agree on every mapped field within 1 percent, or within 100,000 absolute for near-zero values. One mismatch rejects that ticker whole (no partial trust), and a ticker with no overlap to check is rejected as unverified rather than trusted. Accepted tickers keep every cached yfinance value and gain only quarters older than the cache. MNDY is skipped a priori: as a foreign private issuer filing 20-F it has no 10-Q XBRL quarterly facts. Bench names have no cache to verify against, so the tool fetches their current yfinance statements to create the overlap, and cache pruning now keeps the bench (`cfg.cache_tickers`) so the seeded parquets survive the next scheduled run. `--dry-run` is the default and writes nothing; `--apply` is a one-time owner-gated run, because `data/cache/` is the scheduled bot's pen. Spec and decisions: `tasks/spec-history-backfill.md`.
---
## 5. Metric Definitions (exact formulas)
Let `Rev_TTM` = trailing-12-month revenue; `Rev_TTM_prior` = TTM revenue one year earlier.
**Growth**
- `growth = (Rev_TTM / Rev_TTM_prior) − 1` (as %)
**Margins (all TTM, % of Rev_TTM)**
- `fcf_margin = (OCF − CapEx) / Rev_TTM`
- `ebitda_margin = EBITDA / Rev_TTM`
- `op_margin = OperatingIncome / Rev_TTM`
- `fcf_margin_exSBC = (OCF − CapEx − SBC) / Rev_TTM`  ← treats SBC as a cash cost
**Rule-of-40 family**
- `r40_fcf = growth + fcf_margin` (headline; best market-alignment)
- `r40_ebitda = growth + ebitda_margin` (stricter; shown side-by-side)
- `r40_sbc_adj = growth + fcf_margin_exSBC` (the "honest" number; big gaps vs r40_fcf are a flag)
- `rule_of_x = 2 × growth + fcf_margin` (growth-weighted variant; target ≥ 50)
- `r40_trend = r40_fcf(now) − r40_fcf(−4Q)`; also store −2Q point for the sparkline
**Dilution & valuation guards**
- `dilution = (DilutedShares_now / DilutedShares_1y_ago) − 1` — flag if > 3%/yr
- `sbc_intensity = SBC_TTM / Rev_TTM` — flag if > 15%
- `ev_revenue = EnterpriseValue / Rev_TTM` (EV = MarketCap + TotalDebt − Cash)
- `fcf_yield = (OCF − CapEx)_TTM / MarketCap`
**Technical overlay (computed from daily closes, pandas-ta)**
- `sma50`, `sma200`; states: price>both (uptrend), price<both (downtrend), cross events in last 10 sessions (golden/death cross flag)
- `rsi14` — overbought >70, oversold <30 (informational, not scored heavily)
- `rel_strength_3m = (P/P_63d) / (SPY/SPY_63d) − 1`
- `dist_52w_high = P / max(P, 252d) − 1`
- `vol_ratio = mean(volume, 20d) / mean(volume, 100d)` — unusual activity flag
---
## 6. Scoring Model
Applies fully only to tickers tagged `r40`; others get technicals-only rows in a separate table.
**Fundamental score F (0–100):**
- Base: `min(r40_fcf, 80) / 80 × 60` (a 40 scores 30; a 65 scores ~49; capped so one metric can't dominate)
- + `min(max(rule_of_x − 50, 0), 30)` × 0.5 (bonus up to 15 for growth-weighted excellence)
- + `10` if `r40_ebitda ≥ 40` (passes the strict version too)
- + `15 × clamp(r40_trend / 15, −1, 1)` (improving/deteriorating trajectory, ±15)
- Penalties: −10 if `dilution > 3%`; −10 if `sbc_intensity > 15%`; −10 if `r40_fcf − r40_sbc_adj > 20` (SBC is doing the heavy lifting)
- Clamp to [0, 100].
**Technical score T (0–100):** +40 uptrend / +0 mixed / −20 downtrend (rescaled), +30 × clamp(rel_strength_3m / 0.15, −1, 1), +15 if golden cross recent / −15 death cross, +15 proximity to 52-week high (`1 + dist_52w_high` scaled). Clamp [0, 100].
**Composite:** `C = 0.6 F + 0.4 T` (weights from config).
**Valuation is a label, not a score input:** each strong name gets a tag — `cheap` (fcf_yield > 4%), `fair`, or `priced-for-perfection` (ev_revenue > 12 and fcf_yield < 1%). Rationale: R40 measures quality, not price; mixing them hides both signals.
**Report ranking (`report.ranking`, default `breadth`):** the order names appear in, separate from the scores themselves.
- `breadth` (default): sort by how many of the three R40 variants (`r40_fcf`, `r40_ebitda`, `r40_sbc_adj`) clear 40, most first, with the composite as the tiebreak. A `None` variant never counts toward breadth. Rationale: passing R40 three different ways is stronger evidence of durable quality than one high reading, which a single generous variant can manufacture.
- `score`: sort by composite alone (the pre-breadth behavior), for when a raw ranking is wanted.
Either way the composite is what gets displayed and what change detection diffs; ranking only decides row order and therefore which names fall into the strongest/weakest tables.
**Weakest-buy list:** lowest composites among r40-tagged names, prioritizing the combination `r40_trend < −10` AND downtrend/death-cross — "deteriorating fundamentals with technical confirmation."
**Flags rendered in report:** `⚠ SBC-inflated`, `⚠ Dilution`, `⚠ Stale fundamentals`, `★ Passes all 3 R40 variants`, `📉 Death cross`, `📈 Golden cross`, `🏷 priced-for-perfection`.
---
## 7. Report Design (HTML email)
1. **Header:** date, run type (scheduled/ad hoc), data freshness notes.
2. **Market context strip:** SPY 1d/1m change, watchlist median R40.
3. **What changed today (Phase 4, 2026-08-06):** every move since the prior run that cleared a config threshold (`changes:` block): composite/rank moves, flag transitions, R40 trend inflections, trend-state changes, new crosses, estimate-revision swings, short-interest readings, universe changes. Quality-signal arrows (up improving, down worsening). A quiet day renders exactly one line; the first run ever renders a "no prior state" line. Baselines come from the committed `data/cache/run_history.json` (12-run retention, written by real full-universe runs only, committed by the scheduled bot alongside the parquet cache; ticker-subset runs skip detection with a note; dry runs read fixture state and never write).
4. **Deterioration watch:** names with >= `min_signals` concurrent negative signals (1-run and week-window score drops, R40 trend below the threshold, technical breakdown, estimate cuts, worsening short interest), or the section-6 deteriorating() combo alone. One-line reason per name; the section is omitted entirely when empty.
5. **Strong performers (top N):** table — ticker, composite, r40_fcf / r40_ebitda / r40_sbc_adj, growth, fcf margin, valuation tag, trend sparkline (inline PNG), flags.
6. **Weak performers (bottom N):** same columns + one-line reason string (template-generated, e.g., "R40 fell 18pts YoY; broke below 200-day"). The two tables never overlap: `bottom_n` names are reserved for the weak table before the strong table takes its share (changed 2026-08-06 — previously strongest took `top_n` first, so a watchlist smaller than `top_n` rendered an empty weakest table every day).
7. **Movers & alerts:** any golden/death cross, RSI extremes, new 52-week highs/lows, unusual volume.
8. **Technicals-only table** for non-r40 tickers.
9. **Appendix/footer:** methodology one-liner, disclaimer, link to repo run.
Email must render in Gmail/Apple Mail: inline CSS only, tables not flexbox, images embedded as CID attachments. Also write `report.html` + `scores.csv` as workflow artifacts.
**Style rules (owner preference, 2026-08-06):** no em or en dashes anywhere the reader can see — email body, subject line, data notes, and LLM prose (enforced by prompt rule + post-scrub in `news/styles.py`; use commas, colons, parentheses, or hyphens). Missing values render as `n/a`, never a dash. Multi-tone news sections render in `news.tones` config order; `barrons` leads.
---
## 8. Repository Layout
```
ticker-sentinel/
├── CLAUDE.md
├── PROJECT_PLAN.md            # this file
├── pyproject.toml             # deps: yfinance, pandas, pandas-ta, jinja2, matplotlib, pyyaml, requests, tenacity, pytest
├── config/
│   └── watchlist.yaml
├── src/sentinel/
│   ├── config.py
│   ├── data/
│   │   ├── prices.py          # yfinance batch + twelvedata fallback
│   │   ├── fundamentals.py    # statements, TTM builder, cache
│   │   ├── edgar.py           # SEC EDGAR companyfacts client (backfill tool only)
│   │   └── cache.py           # parquet cache w/ staleness rules
│   ├── indicators/
│   │   ├── fundamentals.py    # section 5 formulas
│   │   └── technicals.py
│   ├── scoring.py             # section 6
│   ├── report/
│   │   ├── builder.py
│   │   ├── charts.py          # sparklines
│   │   └── templates/report.html.j2
│   ├── deliver.py             # SMTP / Resend
│   ├── backfill.py            # one-time EDGAR history backfill: python -m sentinel.backfill [--dry-run | --apply]
│   └── run.py                 # CLI entrypoint: python -m sentinel.run [--tickers ...] [--no-email] [--deep]
├── tests/                     # unit tests w/ fixture statement data (no network)
├── data/cache/                # committed parquet cache (small) — or Actions cache, decide in build
└── .github/workflows/daily-report.yml
```
---
## 9. GitHub Actions Workflow
```yaml
name: daily-report
on:
  schedule:
    - cron: "0 10 * * 2-6"   # 10:00 UTC = 6:00am EDT / 5:00am EST: pre-market briefing on the prior session; Tue–Sat so Friday's close arrives Saturday morning (changed 2026-07-07 from post-close 21:45 UTC Mon–Fri)
  workflow_dispatch:
    inputs:
      tickers: {description: "Optional comma-separated override", required: false}
      send_email: {description: "Send email?", type: boolean, default: true}
      deep: {description: "Deep-dive mode (more history/detail)", type: boolean, default: false}
jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: "3.12", cache: pip}
      - run: pip install -e .
      - run: python -m sentinel.run ${TICKERS:+--tickers "$TICKERS"} ${{ inputs.send_email == false && '--no-email' || '' }} ${{ inputs.deep && '--deep' || '' }}
        env:
          TICKERS: ${{ inputs.tickers }}   # via env, never interpolated into the command line
          SMTP_USER: ${{ secrets.SMTP_USER }}
          SMTP_PASS: ${{ secrets.SMTP_PASS }}
          RECIPIENT_EMAILS: ${{ secrets.RECIPIENT_EMAILS }}
          TWELVEDATA_API_KEY: ${{ secrets.TWELVEDATA_API_KEY }}
      - uses: actions/upload-artifact@v4
        with: {name: report, path: reports/}
      - name: refresh fundamentals cache commit (state-changing runs)
        if: ${{ !cancelled() && github.event_name == 'schedule' }}
        run: |
          git config user.name sentinel-bot
          git config user.email bot@users.noreply.github.com
          git add data/cache
          git diff --cached --quiet || { git commit -m "cache: refresh fundamentals" &&
            git pull --rebase origin main && git push; }
```
Sketch only: `.github/workflows/daily-report.yml` is authoritative and carries the
operational hardening (concurrency group, `!cancelled()` gates so a failed send still
uploads the report and commits the baseline, one push retry after a rebase, step-scoped
secrets, and the `failure()` step that opens or comments on a `run-failure` issue).
Notes: US market holidays → job runs but detects "no new bar" and sends nothing (or a one-line "market closed" email — config option). Repo is public (2026-08-06): the watchlist and bot-committed cache are deliberately published; recipient addresses and all credentials live only in Actions secrets.
---
## 10. Testing & Quality
- Unit tests for every formula in section 5 against hand-computed fixtures (incl. edge cases: negative margins, <4 quarters, missing SBC line, ticker with no EBITDA reported).
- Golden-file test for the HTML report (fixture data → stable rendered output).
- A `--dry-run` mode using committed fixture data so CI tests never hit the network.
- Data-sanity guards: refuse to score a ticker whose statements are >200 days stale; label rather than crash on any missing field.
## 11. Phased Roadmap
- **Phase 1 (MVP):** config + data layer + fundamentals scorecard + basic HTML email + scheduled workflow. Ship when one real email arrives with correct R40 numbers spot-checked against a public source.
- **Phase 2:** technical overlay + composite scoring + flags + sparklines + ad hoc dispatch inputs + weakest-buy logic + tests hardened. Quick wins folded in: daily market-cap repricing (valuation label moves daily) and earnings-date-aware cache refresh (new quarters land within ~1 day).
- **Phase 2.5 (shipped 2026-07-06):** between-quarter fundamental signals — analyst estimate revisions (current-quarter EPS, 7d/30d analyst counts), recommendation trends (month-over-month), short interest (FINRA cycle + MoM delta), insider activity (6-month net shares). Implemented with yfinance as primary for all four (verified richer than expected); Finnhub free tier (`FINNHUB_API_KEY`, optional) as insider-data fallback hedging yfinance scraping breakage. Rendered as a "Between-quarter signals" table + Movers alerts; informational only, never score inputs.
- **Phase 3.1 (shipped 2026-07-07):** named LLM *tones* for the news synthesis — `neutral-analyst` (default), `skeptic`, `brief-wire`, `morning-brew`, `barrons` (researched Barron's editorial voice: fundamentally driven, valuation-anchored, verdict-shaped, dry wit) — selected via `news.tone` config. Implemented purely as prompt presets in the presentation layer (`news/styles.py`): the prompt splits into a swappable VOICE block and invariant RULES (full ticker coverage, plain text, no advice, word cap) that always override the voice, so no tone can break rendering safety or completeness. Tone may influence emphasis but never pipeline selection; unknown tones fall back to the default with a data note. All five verified live: 8/8 ticker coverage, distinct voices, no truncation. The daily email renders every configured tone (`news.tones` list) as its own clearly-labeled section from the single shared digest — one pipeline pass, N voices; a failed tone is skipped with a note, and only if all fail does one headlines fallback render.
- **Phase 3 (news module — headline delivery shipped 2026-07-06):** RSS ingestion (`feedparser`) from config-curated feeds (general feeds text-matched to tickers + a per-ticker feed URL template) producing a personalized "What mattered today" section. Architecture is deliberately two-layer: `news/pipeline.py` (data side — fetch, recency window, attribution, dedupe, rank, cap; emits a neutral `NewsDigest` and owns primary selection) and `news/styles.py` (presentation side — swappable renderers chosen via `news.style` config; a style may trim the digest further but never expands or fetches). The LLM summarization pass shipped as the `llm-brief` style (2026-07-06): one headless `claude -p` call per configured tone per run using subscription auth (`claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN` secret; no API billing), model pinned from config (`news.model`, default claude-sonnet-5). The model writes prose only — escaped before injection, so it can never emit live HTML; links/attribution render deterministically from the digest. Guardrails: single call, prompt truncation, output-token cap, timeout, and fail-open to the `headlines` style on any failure.
- **Phase 4 (shipped 2026-08-06):** day-over-day change emphasis + deterioration detection. Cross-run state lives in `data/cache/run_history.json` (single versioned JSON, 12-run retention, same-date replace, corrupt-file degradation to a data note); it rides the scheduled workflow's existing `git add data/cache` commit step, so the bot keeps the pen and no workflow edits were needed. The report leads with a "What changed today" section (nine change types, all thresholds in the `changes:` config block) and a "Deterioration watch" subsection (multi-signal decay gate, section-6 deteriorating() combo sufficient alone). Watchlist expanded 8 -> 20 (12 owner-approved adds; CYBR and CFLT swapped for bench names FTNT and ESTC same day after live verification showed both delisted post-acquisition; WDAY, SHOP, TWLO, ZM remain benched); Twelve Data fallback paced to the free tier's 8 requests/min when more than 8 symbols need it. Rotation process: roughly weekly, the owner requests a candidate refresh using the deterioration evidence; every `config/watchlist.yaml` edit stays owner-gated.
- **Phase 4.1 (weekly refresh digest):** the rotation reminder is automated, the decision is not. A `weekly-refresh` workflow (Saturdays 12:00 UTC, after the daily run's history commit) runs `python -m sentinel.digest` over the committed `run_history.json` and opens an owner-assigned GitHub issue (label `watchlist-refresh`): attention list (persistent decay-gate hits or week-scale composite drops, thresholds from the `changes:` config), change-activity counts, coverage/liveness gaps (a delisted name stops appearing in runs), bench, and an owner checklist that builds the rotation rubric in SPEC.md. The first 3 refreshes are calibration rounds; from refresh #3 the checklist adds the decision point on automating proposal drafting against the written rubric (a scheduled agent drafting the swap PR) vs staying manual. The bench moved from watchlist.yaml comments to a structured `bench:` key. Digest is read-only over history: no network, no scoring, no cache writes.
## 12. Risks & Mitigations
| Risk | Mitigation |
|---|---|
| yfinance breakage / Yahoo rate-limits runner IPs | Twelve Data fallback; cached fundamentals; degraded-but-delivered report |
| yfinance statement field names shift between versions | pin version; field-mapping layer with aliases; tests catch renames |
| Gmail SMTP blocks | app password + low volume; Resend free tier as alternate path |
| R40 misapplied to non-software names | `r40` tag gating; sector shown in report |
| Metric garbage-in (restated quarters, missing SBC) | sanity guards + flags instead of silent numbers |
| Dead RSS URLs / feeds redirecting to HTML error pages (parse as empty, not as errors) | per-feed degradation notes; feed list lives in config for zero-code swaps; recency window + per-ticker cap bound the blast radius of a noisy feed |
| LLM news style outage (OAuth token expiry ~1yr, Pro usage-window limits, CLI failure) | fail-open to deterministic `headlines` style + data note; one call per configured tone per run (5/day at the current tone lineup, Tue–Sat pre-market — a small, deliberate draw that also opens the owner's usage window early), each with prompt truncation and output cap; a failed tone skips with a note; kill switches: trim `news.tones` or set `news.style: headlines` |
| Committed cache grows unboundedly | 16-quarter cap on parquet width; watchlist-driven pruning; per-run signals snapshots untracked |
| Shallow quarterly history keeps the trend metrics inert (r40_trend needs 12 quarters; the cache accumulates 4 a year) | decided 2026-08-15, rather than waiting it out: a one-time `python -m sentinel.backfill` deepens the cache from SEC EDGAR behind a per-ticker verification gate (every overlap quarter must agree with the cached yfinance values within 1 percent or 100,000 absolute; one mismatch rejects the ticker whole), with MNDY excluded a priori as a 20-F filer and `--apply` an owner-gated one-time run against the bot's pen. Until it lands, the report discloses the gap with a self-erasing warm-up note. EDGAR stays out of the scheduled run |
| Run-history state corrupted or growing | versioned JSON with wrong-shape detection degrading to "change detection reset" note (never a crash); 12-run retention pruned on every write; sorted-key pretty printing keeps bot-commit diffs reviewable |
| Larger watchlist trips API limits | one batched yfinance call regardless of size; Twelve Data fallback paced to 8 requests/min when >8 symbols need it; per-ticker fundamentals/signals calls degrade to cached values + staleness notes as before |
| Watchlist goes stale as businesses drift | weekly digest issue (Saturday workflow) summarizing deterioration evidence + liveness gaps prompts the owner-gated candidate refresh; structured `bench:` list in watchlist.yaml; cache pruning cleans up dropped names automatically |
| Partially-published newest quarter or a gap in quarterly history poisons TTM windows | anchor on the newest complete core quarter (bounded skip, scored-as-of note); windows spanning a >120-day column gap degrade to insufficient data with a note (2026-08-14) |
| Degraded runs corrupting change detection (missing technicals change composite construction; wholesale fetch outage empties the scored set) | diffs compare like for like (F vs F across a basis change, info-labeled otherwise; universe-wide flips collapse to one line); runs scoring below `changes.baseline_min_fraction` of the prior baseline's tickers are reported but neither diffed nor saved as baseline; universe_removed rows carry the unscored reason (2026-08-14) |
| Scope creep | phases; news module explicitly Phase 3 |
