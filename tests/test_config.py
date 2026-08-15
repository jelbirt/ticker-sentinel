"""Config parsing — news tones list/singular handling."""
from __future__ import annotations

from sentinel.config import load_config


def _write_cfg(tmp_path, news_block: str):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "watchlist.yaml").write_text(
        "universe:\n  - ticker: CRWD\n    tags: [r40]\n" + news_block
    )
    return tmp_path / "config" / "watchlist.yaml"


def test_tones_list(tmp_path):
    path = _write_cfg(tmp_path, "news:\n  feeds: [x]\n  tones: [skeptic, barrons]\n")
    assert load_config(path).news.tones == ("skeptic", "barrons")


def test_singular_tone_accepted(tmp_path):
    path = _write_cfg(tmp_path, "news:\n  feeds: [x]\n  tone: barrons\n")
    assert load_config(path).news.tones == ("barrons",)


def test_tone_default(tmp_path):
    path = _write_cfg(tmp_path, "news:\n  feeds: [x]\n")
    assert load_config(path).news.tones == ("neutral-analyst",)


def test_repo_config_ships_all_five_tones():
    cfg = load_config()
    assert len(cfg.news.tones) == 5
    assert cfg.news.tones[0] == "barrons"  # owner preference: barrons section leads the email


def test_changes_defaults_when_block_absent(tmp_path):
    path = _write_cfg(tmp_path, "")
    ch = load_config(path).changes
    assert ch.retention_runs == 12
    assert ch.week_window_runs == 5
    assert ch.score_delta_pts == 3.0
    assert ch.rank_delta == 2
    assert ch.revision_swing == 3
    assert ch.short_delta == 0.05
    assert ch.week_drop_pts == 5.0
    assert ch.revision_cut == 2
    assert ch.min_signals == 2
    assert ch.deteriorating_r40_trend == -0.10


def test_changes_partial_block_overrides_only_given_keys(tmp_path):
    path = _write_cfg(tmp_path, "changes:\n  score_delta_pts: 5.5\n  retention_runs: 20\n")
    ch = load_config(path).changes
    assert ch.score_delta_pts == 5.5
    assert ch.retention_runs == 20
    assert ch.rank_delta == 2  # untouched default
def test_cache_tickers_includes_the_bench(tmp_path):
    """Bench names are unscored but their cache files must survive pruning."""
    path = _write_cfg(tmp_path, "bench: [WDAY, SHOP]\n")
    cfg = load_config(path)
    assert cfg.all_tickers == ["CRWD"]
    assert cfg.cache_tickers == ["CRWD", "WDAY", "SHOP"]


def test_cache_tickers_does_not_duplicate_a_benched_universe_name(tmp_path):
    path = _write_cfg(tmp_path, "bench: [CRWD, SHOP]\n")
    assert load_config(path).cache_tickers == ["CRWD", "SHOP"]


def test_repo_config_bench_is_cache_protected():
    cfg = load_config()
    assert set(cfg.bench) <= set(cfg.cache_tickers)
