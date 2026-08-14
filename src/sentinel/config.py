"""Config loading: watchlist.yaml + repo-root resolution. Secrets stay in env vars."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
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
class NewsCfg:
    feeds: tuple[str, ...] = ()          # general feeds, text-matched to tickers
    per_ticker_feed: str | None = None   # URL template with {ticker}, pre-attributed
    max_age_hours: int = 36
    max_per_ticker: int = 3
    style: str = "headlines"             # presentation style — see news/styles.py
    model: str = "claude-sonnet-5"       # only used by LLM styles (llm-brief)
    # LLM voice presets (see TONES in news/styles.py); several -> one labeled
    # section per tone in the same email
    tones: tuple[str, ...] = ("neutral-analyst",)

    @property
    def enabled(self) -> bool:
        return bool(self.feeds or self.per_ticker_feed)


@dataclass(frozen=True)
class ChangesCfg:
    """Day-over-day change detection thresholds (SPEC.md sections 3, 4)."""

    retention_runs: int = 12        # history entries kept in data/cache/run_history.json
    week_window_runs: int = 5       # "week" lookback in runs (Tue-Sat cadence)
    score_delta_pts: float = 3.0    # composite move worth reporting (0-100 scale)
    rank_delta: int = 2             # rank move worth reporting
    revision_swing: int = 3         # net 30d analyst-revision swing worth reporting
    short_delta: float = 0.05       # short-interest fractional change worth reporting
    week_drop_pts: float = 5.0      # week-window composite drop counting as deterioration
    revision_cut: int = 2           # net downward revisions counting as deterioration
    min_signals: int = 2            # negative signals needed for Deterioration watch
    deteriorating_r40_trend: float = -0.10  # plan section 6 weakest-buy threshold
    digest_decay_runs: int = 2      # decay-gate hits in the weekly digest window
                                    # for a ticker to make the attention list
    baseline_min_fraction: float = 0.5  # scored fraction of the universe below
                                        # which a run is too degraded to diff
                                        # or to become tomorrow's baseline


@dataclass(frozen=True)
class Config:
    universe: tuple[TickerCfg, ...]
    bench: tuple[str, ...] = ()   # reserve swap candidates, not scored (spec 7.0)
    benchmark: str = "SPY"
    top_n: int = 10
    bottom_n: int = 5
    timezone: str = "America/New_York"
    ranking: str = "breadth"  # "breadth": most R40 variants ≥ 40 first; "score": plain F-score
    fundamentals_weight: float = 0.6
    technicals_weight: float = 0.4
    news: NewsCfg = NewsCfg()
    changes: ChangesCfg = ChangesCfg()

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
    news_raw = raw.get("news", {}) or {}
    changes_raw = raw.get("changes", {}) or {}
    known = {f.name for f in fields(ChangesCfg)}
    changes = ChangesCfg(**{k: v for k, v in changes_raw.items() if k in known})
    news = NewsCfg(
        feeds=tuple(news_raw.get("feeds", []) or []),
        per_ticker_feed=news_raw.get("per_ticker_feed"),
        max_age_hours=int(news_raw.get("max_age_hours", 36)),
        max_per_ticker=int(news_raw.get("max_per_ticker", 3)),
        style=str(news_raw.get("style", "headlines")),
        model=str(news_raw.get("model", "claude-sonnet-5")),
        # `tones` (list) preferred; singular `tone` accepted for convenience
        tones=tuple(news_raw.get("tones") or [news_raw.get("tone", "neutral-analyst")]),
    )
    return Config(
        universe=universe,
        bench=tuple(str(t).upper() for t in raw.get("bench", []) or []),
        benchmark=str(raw.get("benchmark", "SPY")).upper(),
        top_n=int(report.get("top_n", 10)),
        bottom_n=int(report.get("bottom_n", 5)),
        timezone=str(report.get("timezone", "America/New_York")),
        ranking=str(report.get("ranking", "breadth")),
        fundamentals_weight=float(scoring.get("fundamentals_weight", 0.6)),
        technicals_weight=float(scoring.get("technicals_weight", 0.4)),
        news=news,
        changes=changes,
    )
