"""Config loading: watchlist.yaml + repo-root resolution. Secrets stay in env vars."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


def repo_root() -> Path:
    """Repo root: SENTINEL_ROOT env override, else two levels above this package (src layout)."""
    env = os.environ.get("SENTINEL_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class TickerCfg:
    ticker: str
    tags: tuple[str, ...] = ()

    @property
    def is_r40(self) -> bool:
        return "r40" in self.tags


@dataclass(frozen=True)
class Config:
    universe: tuple[TickerCfg, ...]
    benchmark: str = "SPY"
    top_n: int = 10
    bottom_n: int = 5
    timezone: str = "America/New_York"
    ranking: str = "breadth"  # "breadth": most R40 variants ≥ 40 first; "score": plain F-score
    fundamentals_weight: float = 0.6
    technicals_weight: float = 0.4

    @property
    def r40_tickers(self) -> list[str]:
        return [t.ticker for t in self.universe if t.is_r40]

    @property
    def all_tickers(self) -> list[str]:
        return [t.ticker for t in self.universe]


def load_config(path: Path | None = None) -> Config:
    path = path or repo_root() / "config" / "watchlist.yaml"
    raw = yaml.safe_load(path.read_text())
    universe = tuple(
        TickerCfg(ticker=str(item["ticker"]).upper(), tags=tuple(item.get("tags", [])))
        for item in raw.get("universe", [])
    )
    report = raw.get("report", {}) or {}
    scoring = raw.get("scoring", {}) or {}
    return Config(
        universe=universe,
        benchmark=str(raw.get("benchmark", "SPY")).upper(),
        top_n=int(report.get("top_n", 10)),
        bottom_n=int(report.get("bottom_n", 5)),
        timezone=str(report.get("timezone", "America/New_York")),
        ranking=str(report.get("ranking", "breadth")),
        fundamentals_weight=float(scoring.get("fundamentals_weight", 0.6)),
        technicals_weight=float(scoring.get("technicals_weight", 0.4)),
    )
