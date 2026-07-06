"""Jinja2 HTML report + CSV/JSON artifacts. Inline CSS only (Gmail-safe)."""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date
from pathlib import Path
from statistics import median

import pandas as pd
from jinja2 import Environment, FileSystemLoader, select_autoescape

from sentinel.config import Config
from sentinel.indicators.fundamentals import Scorecard
from sentinel.indicators.signals import short_change_mom, signal_alerts

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

FLAG_LABELS = {
    "sbc_inflated": "⚠ SBC-inflated",
    "dilution": "⚠ Dilution",
    "high_sbc": "⚠ High SBC",
    "stale_fundamentals": "⚠ Stale fundamentals",
    "passes_all_r40": "★ Passes all 3 R40",
    "growth_from_annual": "ℹ Growth from annual",
    "insufficient_history": "ℹ Short history",
    "insufficient_data": "⚠ Insufficient data",
    "golden_cross": "📈 Golden cross",
    "death_cross": "📉 Death cross",
}

TREND_ARROWS = {"uptrend": "↑ up", "downtrend": "↓ down", "mixed": "→ mixed"}

CSV_COLUMNS = [
    "ticker", "company_name", "composite", "score", "technical_score",
    "r40_fcf", "r40_ebitda", "r40_sbc_adj", "rule_of_x", "r40_trend",
    "growth", "growth_source", "fcf_margin", "ebitda_margin", "op_margin",
    "fcf_margin_ex_sbc", "dilution", "sbc_intensity", "ev_revenue", "fcf_yield",
    "valuation", "trend_state", "rsi14", "rel_strength_3m", "dist_52w_high",
    "vol_ratio", "eps_rev_up_30d", "eps_rev_down_30d", "rec_bullish", "rec_bearish",
    "short_pct_float", "shares_short", "insider_net_shares_6m",
    "statement_date", "stale", "flags",
]

TECH_CSV_FIELDS = ["trend_state", "rsi14", "rel_strength_3m", "dist_52w_high", "vol_ratio"]
SIGNAL_CSV_FIELDS = [
    "eps_rev_up_30d", "eps_rev_down_30d", "rec_bullish", "rec_bearish",
    "short_pct_float", "shares_short", "insider_net_shares_6m",
]


def _pts(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.1f}"


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def _mult(v: float | None) -> str:
    return "—" if v is None else f"{v:.1f}×"


def _score(v: float | None) -> str:
    return "—" if v is None else f"{v:.1f}"


def _shares(v: float | None) -> str:
    """Signed share counts humanized: +733k, −1.2M."""
    if v is None:
        return "—"
    sign = "+" if v > 0 else "−" if v < 0 else ""
    a = abs(v)
    if a >= 1e6:
        return f"{sign}{a / 1e6:.1f}M"
    if a >= 1e3:
        return f"{sign}{a / 1e3:.0f}k"
    return f"{sign}{a:.0f}"


def _spct(v: float | None) -> str:
    """Signed percent for deltas: +33%, −6%."""
    return "—" if v is None else f"{v * 100:+.0f}%"


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html", "j2"]),
    )
    env.filters.update(pts=_pts, pct=_pct, mult=_mult, score=_score, shares=_shares, spct=_spct)
    return env


def flag_labels(sc: Scorecard) -> list[str]:
    return [FLAG_LABELS.get(f, f) for f in sc.flags]


def r40_breadth(sc: Scorecard) -> int:
    """How many of the three R40 variants clear 40 (None never counts)."""
    return sum(
        1 for v in (sc.r40_fcf, sc.r40_ebitda, sc.r40_sbc_adj) if v is not None and v >= 0.40
    )


def _strength(sc: Scorecard) -> float:
    """Composite when technicals ran, else fundamental score."""
    if sc.composite is not None:
        return sc.composite
    return sc.score or 0.0


def rank_key(sc: Scorecard, ranking: str):
    """Sort key (ascending sort, so negated): breadth-first or plain strength."""
    if ranking == "breadth":
        return (-r40_breadth(sc), -_strength(sc))
    return (-_strength(sc),)


def deteriorating(sc: Scorecard) -> bool:
    """Section 6 weakest-buy priority: R40 fell >10pts AND technical confirmation."""
    fell = sc.r40_trend is not None and sc.r40_trend < -0.10
    tech_confirms = sc.tech is not None and (
        sc.tech.trend_state == "downtrend" or sc.tech.death_cross_recent
    )
    return fell and tech_confirms


def weakness_reason(sc: Scorecard) -> str:
    parts: list[str] = []
    if sc.r40_trend is not None and sc.r40_trend < -0.10:
        parts.append(f"R40 fell {abs(sc.r40_trend) * 100:.0f}pts YoY")
    if sc.tech is not None:
        if sc.tech.death_cross_recent:
            parts.append("death cross")
        elif sc.tech.trend_state == "downtrend":
            parts.append("below 50/200-day")
    if not parts:
        parts.append("lowest composite in watchlist")
    return "; ".join(parts)


def build_movers(scorecards: list[Scorecard]) -> list[str]:
    """Section 7 'Movers & alerts': crosses, RSI extremes, 52w highs/lows, volume,
    plus between-quarter signal alerts. One pass per ticker so its alerts sit together."""
    movers: list[str] = []
    for sc in scorecards:
        t = sc.tech
        if t is not None:
            if t.golden_cross_recent:
                movers.append(f"📈 {sc.ticker}: golden cross")
            if t.death_cross_recent:
                movers.append(f"📉 {sc.ticker}: death cross")
            if t.rsi14 is not None and t.rsi14 > 70:
                movers.append(f"{sc.ticker}: RSI {t.rsi14:.0f} — overbought")
            if t.rsi14 is not None and t.rsi14 < 30:
                movers.append(f"{sc.ticker}: RSI {t.rsi14:.0f} — oversold")
            if t.dist_52w_high is not None and t.dist_52w_high >= -0.001:
                movers.append(f"{sc.ticker}: new 52-week high")
            elif t.dist_52w_low is not None and t.dist_52w_low <= 0.001:
                movers.append(f"{sc.ticker}: new 52-week low")
            if t.vol_ratio is not None and t.vol_ratio > 1.5:
                movers.append(
                    f"{sc.ticker}: unusual volume ({t.vol_ratio:.1f}× the 100-day average)"
                )
        if sc.signals is not None:
            movers.extend(f"{sc.ticker}: {alert}" for alert in signal_alerts(sc.signals))
    return movers


def build_context(
    scorecards: list[Scorecard],
    cfg: Config,
    run_type: str,
    notes: list[str],
    benchmark_line: str | None = None,
    today: date | None = None,
    tech_only: list[dict] | None = None,
) -> dict:
    scored = sorted(
        (sc for sc in scorecards if sc.score is not None),
        key=lambda s: rank_key(s, cfg.ranking),
    )
    unscored = [sc for sc in scorecards if sc.score is None]
    strongest = scored[: cfg.top_n]
    # weakest: deteriorating names first (plan section 6), then lowest strength
    remaining = sorted(scored[cfg.top_n :], key=lambda s: (not deteriorating(s), _strength(s)))
    weakest = remaining[: cfg.bottom_n]
    for sc in weakest:
        sc.reason = weakness_reason(sc)
    r40_values = [sc.r40_fcf for sc in scorecards if sc.r40_fcf is not None]
    return {
        "report_date": (today or date.today()).isoformat(),
        "run_type": run_type,
        "notes": notes,
        "benchmark_line": benchmark_line,
        "strongest": strongest,
        "weakest": weakest,
        "all_scored": scored,   # deep grid covers every ranked name, not just top/bottom
        "unscored": unscored,
        "movers": build_movers(scorecards),
        "tech_only": tech_only or [],
        "signal_rows": sorted(
            (sc for sc in scorecards if sc.signals is not None), key=lambda s: s.ticker
        ),
        "short_change_mom": short_change_mom,
        "median_r40": _pts(median(r40_values)) if r40_values else "—",
        "n_total": len(scorecards),
        "flag_labels": flag_labels,
        "trend_arrows": TREND_ARROWS,
        "spark_src": {},   # ticker -> img src; run.py injects cid:/data: URIs
        "deep": False,
    }


def render_report(context: dict) -> str:
    return _env().get_template("report.html.j2").render(**context)


def write_outputs(outdir: Path, html: str, scorecards: list[Scorecard]) -> dict[str, Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    paths = {
        "html": outdir / "report.html",
        "csv": outdir / "scores.csv",
        "json": outdir / "raw.json",
    }
    paths["html"].write_text(html)

    rows = []
    for sc in scorecards:
        row = asdict(sc)
        row["flags"] = ";".join(sc.flags)
        for f in TECH_CSV_FIELDS:
            row[f] = getattr(sc.tech, f) if sc.tech is not None else None
        for f in SIGNAL_CSV_FIELDS:
            row[f] = getattr(sc.signals, f) if sc.signals is not None else None
        rows.append({k: row.get(k) for k in CSV_COLUMNS})
    pd.DataFrame(rows, columns=CSV_COLUMNS).to_csv(paths["csv"], index=False)

    paths["json"].write_text(
        json.dumps([asdict(sc) for sc in scorecards], indent=2, default=str)
    )
    return paths
