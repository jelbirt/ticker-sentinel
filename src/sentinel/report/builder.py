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
}

CSV_COLUMNS = [
    "ticker", "company_name", "score", "r40_fcf", "r40_ebitda", "r40_sbc_adj", "rule_of_x", "r40_trend",
    "growth", "growth_source", "fcf_margin", "ebitda_margin", "op_margin",
    "fcf_margin_ex_sbc", "dilution", "sbc_intensity", "ev_revenue", "fcf_yield",
    "valuation", "statement_date", "stale", "flags",
]


def _pts(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.1f}"


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def _mult(v: float | None) -> str:
    return "—" if v is None else f"{v:.1f}×"


def _score(v: float | None) -> str:
    return "—" if v is None else f"{v:.1f}"


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html", "j2"]),
    )
    env.filters.update(pts=_pts, pct=_pct, mult=_mult, score=_score)
    return env


def flag_labels(sc: Scorecard) -> list[str]:
    return [FLAG_LABELS.get(f, f) for f in sc.flags]


def r40_breadth(sc: Scorecard) -> int:
    """How many of the three R40 variants clear 40 (None never counts)."""
    return sum(
        1 for v in (sc.r40_fcf, sc.r40_ebitda, sc.r40_sbc_adj) if v is not None and v >= 0.40
    )


def rank_key(sc: Scorecard, ranking: str):
    """Sort key (ascending sort, so negated): breadth-first or plain score."""
    if ranking == "breadth":
        return (-r40_breadth(sc), -(sc.score or 0.0))
    return (-(sc.score or 0.0),)


def build_context(
    scorecards: list[Scorecard],
    cfg: Config,
    run_type: str,
    notes: list[str],
    benchmark_line: str | None = None,
    today: date | None = None,
) -> dict:
    scored = sorted(
        (sc for sc in scorecards if sc.score is not None),
        key=lambda s: rank_key(s, cfg.ranking),
    )
    unscored = [sc for sc in scorecards if sc.score is None]
    strongest = scored[: cfg.top_n]
    weakest = list(reversed(scored[cfg.top_n :][-cfg.bottom_n :]))  # worst first
    r40_values = [sc.r40_fcf for sc in scorecards if sc.r40_fcf is not None]
    return {
        "report_date": (today or date.today()).isoformat(),
        "run_type": run_type,
        "notes": notes,
        "benchmark_line": benchmark_line,
        "strongest": strongest,
        "weakest": weakest,
        "unscored": unscored,
        "median_r40": _pts(median(r40_values)) if r40_values else "—",
        "n_total": len(scorecards),
        "flag_labels": flag_labels,
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
        rows.append({k: row.get(k) for k in CSV_COLUMNS})
    pd.DataFrame(rows, columns=CSV_COLUMNS).to_csv(paths["csv"], index=False)

    paths["json"].write_text(
        json.dumps([asdict(sc) for sc in scorecards], indent=2, default=str)
    )
    return paths
