# Ticker Sentinel — Personal Stock Analysis & Daily Report Service
**Version:** 1.0 (planning spec) · **Date:** 2026-07-05
**Purpose:** A small private service that pulls end-of-day stock/fundamentals data from free sources, computes a Rule-of-40-centered fundamental scorecard plus a technical/momentum overlay, ranks strongest/weakest candidates, and emails a daily HTML report to a couple of personal addresses. Runs on GitHub Actions (scheduled + ad hoc). Designed to later grow a personalized finance-news digest module.
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
**Weakest-buy list:** lowest composites among r40-tagged names, prioritizing the combination `r40_trend < −10` AND downtrend/death-cross — "deteriorating fundamentals with technical confirmation."
**Flags rendered in report:** `⚠ SBC-inflated`, `⚠ Dilution`, `⚠ Stale fundamentals`, `★ Passes all 3 R40 variants`, `📉 Death cross`, `📈 Golden cross`, `🏷 priced-for-perfection`.
---
## 7. Report Design (HTML email)
1. **Header:** date, run type (scheduled/ad hoc), data freshness notes.
2. **Market context strip:** SPY 1d/1m change, watchlist median R40.
3. **Strongest (top N):** table — ticker, composite, r40_fcf / r40_ebitda / r40_sbc_adj, growth, fcf margin, valuation tag, trend sparkline (inline PNG), flags.
4. **Weakest (bottom N):** same columns + one-line reason string (template-generated, e.g., "R40 fell 18pts YoY; broke below 200-day").
5. **Movers & alerts:** any golden/death cross, RSI extremes, new 52-week highs/lows, unusual volume.
6. **Technicals-only table** for non-r40 tickers.
7. **Appendix/footer:** methodology one-liner, disclaimer, link to repo run.
Email must render in Gmail/Apple Mail: inline CSS only, tables not flexbox, images embedded as CID attachments. Also write `report.html` + `scores.csv` as workflow artifacts.
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
    - cron: "45 21 * * 1-5"   # 21:45 UTC ≈ 4:45/5:45pm ET depending on DST; acceptable drift, or run at 22:30 UTC to be safe year-round
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
      - run: python -m sentinel.run ${{ inputs.tickers && format('--tickers {0}', inputs.tickers) || '' }} ${{ inputs.send_email == false && '--no-email' || '' }} ${{ inputs.deep && '--deep' || '' }}
        env:
          SMTP_USER: ${{ secrets.SMTP_USER }}
          SMTP_PASS: ${{ secrets.SMTP_PASS }}
          RECIPIENT_EMAILS: ${{ secrets.RECIPIENT_EMAILS }}
          TWELVEDATA_API_KEY: ${{ secrets.TWELVEDATA_API_KEY }}
      - uses: actions/upload-artifact@v4
        with: {name: report, path: reports/}
      - name: refresh fundamentals cache commit (weekly)
        if: github.event_name == 'schedule'
        run: |
          git config user.name sentinel-bot && git config user.email bot@users.noreply.github.com
          git add data/cache && git diff --cached --quiet || git commit -m "cache: refresh fundamentals" && git push
```
Notes: US market holidays → job runs but detects "no new bar" and sends nothing (or a one-line "market closed" email — config option). Keep repo private (watchlist + cache are personal data).
---
## 10. Testing & Quality
- Unit tests for every formula in section 5 against hand-computed fixtures (incl. edge cases: negative margins, <4 quarters, missing SBC line, ticker with no EBITDA reported).
- Golden-file test for the HTML report (fixture data → stable rendered output).
- A `--dry-run` mode using committed fixture data so CI tests never hit the network.
- Data-sanity guards: refuse to score a ticker whose statements are >200 days stale; label rather than crash on any missing field.
## 11. Phased Roadmap
- **Phase 1 (MVP):** config + data layer + fundamentals scorecard + basic HTML email + scheduled workflow. Ship when one real email arrives with correct R40 numbers spot-checked against a public source.
- **Phase 2:** technical overlay + composite scoring + flags + sparklines + ad hoc dispatch inputs + weakest-buy logic + tests hardened. Quick wins folded in: daily market-cap repricing (valuation label moves daily) and earnings-date-aware cache refresh (new quarters land within ~1 day).
- **Phase 2.5 (committed, next after Phase 2):** between-quarter fundamental signals — analyst estimate revisions, short interest, insider transactions — via Financial Modeling Prep and/or Finnhub free tiers. Owner is 100% certain more-than-quarterly signal granularity is wanted; do not drop this.
- **Phase 3 (news module):** RSS ingestion (`feedparser`) from curated feeds + per-ticker news matching; optional LLM summarization pass to produce a personalized "what mattered today for your names" section appended to the same report. Designed as `src/sentinel/news/` feeding the existing report builder.
## 12. Risks & Mitigations
| Risk | Mitigation |
|---|---|
| yfinance breakage / Yahoo rate-limits runner IPs | Twelve Data fallback; cached fundamentals; degraded-but-delivered report |
| yfinance statement field names shift between versions | pin version; field-mapping layer with aliases; tests catch renames |
| Gmail SMTP blocks | app password + low volume; Resend free tier as alternate path |
| R40 misapplied to non-software names | `r40` tag gating; sector shown in report |
| Metric garbage-in (restated quarters, missing SBC) | sanity guards + flags instead of silent numbers |
| Scope creep | phases; news module explicitly Phase 3 |
