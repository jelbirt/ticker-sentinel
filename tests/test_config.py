"""Config parsing: news tones list/singular handling, unknown-key warnings."""
from __future__ import annotations

import logging

from sentinel.config import ChangesCfg, load_config


def _write_cfg(tmp_path, news_block: str):
    (tmp_path / "config").mkdir(parents=True)
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
    assert ch.retention_runs == 25   # 5 weeks at the Tue-Sat cadence
    assert ch.week_window_runs == 5
    assert ch.score_delta_pts == 3.0
    assert ch.rank_delta == 2
    assert ch.revision_swing == 3
    assert ch.short_delta == 0.05
    assert ch.week_drop_pts == 5.0
    assert ch.revision_cut == 2
    assert ch.min_signals == 2
    assert ch.deteriorating_r40_trend == -0.10


def test_default_retention_spans_several_digest_windows():
    """Retention and the digest window are independent knobs: history must
    outlive one week_window_runs window by enough runs that a rotation
    decision can look back over several weeks, not just the current one."""
    ch = ChangesCfg()
    assert ch.retention_runs >= 5 * ch.week_window_runs


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


# --- unknown-key detection: a typo must not silently take the default ----------


def _warnings(caplog) -> str:
    return " | ".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)


def test_unknown_key_in_changes_block_warns_and_names_the_key(tmp_path, caplog):
    path = _write_cfg(tmp_path, "changes:\n  scores_delta_pts: 5.5\n")
    with caplog.at_level(logging.WARNING, logger="sentinel.config"):
        cfg = load_config(path)
    warned = _warnings(caplog)
    assert "scores_delta_pts" in warned
    assert "changes: block" in warned
    assert cfg.changes.score_delta_pts == 3.0  # still the default, now loudly


def test_unknown_keys_in_report_scoring_and_news_blocks_warn(tmp_path, caplog):
    path = _write_cfg(
        tmp_path,
        "report:\n  top_nn: 3\n"
        "scoring:\n  fundamental_weight: 0.7\n"
        "news:\n  feeds: [x]\n  max_age_hrs: 12\n",
    )
    with caplog.at_level(logging.WARNING, logger="sentinel.config"):
        cfg = load_config(path)
    warned = _warnings(caplog)
    assert "top_nn" in warned and "report: block" in warned
    assert "fundamental_weight" in warned and "scoring: block" in warned
    assert "max_age_hrs" in warned and "news: block" in warned
    assert cfg.top_n == 10 and cfg.fundamentals_weight == 0.6
    assert cfg.news.max_age_hours == 36


def test_unknown_top_level_key_warns(tmp_path, caplog):
    path = _write_cfg(tmp_path, "univrese:\n  - ticker: NOPE\n")
    with caplog.at_level(logging.WARNING, logger="sentinel.config"):
        cfg = load_config(path)
    warned = _warnings(caplog)
    assert "univrese" in warned and "top level" in warned
    assert cfg.all_tickers == ["CRWD"]  # the real universe still loaded


def test_known_keys_are_silent_and_still_load(tmp_path, caplog):
    path = _write_cfg(
        tmp_path,
        "bench: [WDAY]\nbenchmark: QQQ\n"
        "report:\n  top_n: 3\n  bottom_n: 2\n  timezone: UTC\n  ranking: score\n"
        "scoring:\n  fundamentals_weight: 0.7\n  technicals_weight: 0.3\n"
        "changes:\n  score_delta_pts: 5.5\n  baseline_min_fraction: 0.75\n"
        "news:\n  feeds: [x]\n  tone: skeptic\n  style: brief\n  model: m\n"
        "  per_ticker_feed: 'u/{ticker}'\n  max_age_hours: 12\n  max_per_ticker: 1\n",
    )
    with caplog.at_level(logging.WARNING, logger="sentinel.config"):
        cfg = load_config(path)
    assert _warnings(caplog) == ""
    assert cfg.benchmark == "QQQ" and cfg.bench == ("WDAY",)
    assert (cfg.top_n, cfg.bottom_n, cfg.ranking) == (3, 2, "score")
    assert cfg.timezone == "UTC"
    assert cfg.changes.score_delta_pts == 5.5
    assert cfg.changes.baseline_min_fraction == 0.75
    assert cfg.news.style == "brief" and cfg.news.max_per_ticker == 1


def test_repo_watchlist_has_no_unknown_keys(caplog):
    """The owner-gated config must stay warning-clean, or the signal is noise."""
    with caplog.at_level(logging.WARNING, logger="sentinel.config"):
        load_config()
    assert _warnings(caplog) == ""


def test_recipients_is_accepted_and_reserved(tmp_path, caplog):
    # labels only (real addresses come from secrets); it must not warn
    path = _write_cfg(tmp_path, "recipients:\n  - primary\n")
    with caplog.at_level(logging.WARNING, logger="sentinel.config"):
        load_config(path)
    assert _warnings(caplog) == ""


def test_screen_candidates_load_uppercased_and_typos_warn(tmp_path, caplog):
    path = _write_cfg(tmp_path, "screen:\n  candidates: [qlys, Intu]\n  candidate: [X]\n")
    with caplog.at_level(logging.WARNING, logger="sentinel.config"):
        cfg = load_config(path)
    assert cfg.screen_candidates == ("QLYS", "INTU")
    assert "candidate" in _warnings(caplog) and "screen: block" in _warnings(caplog)


def test_repo_screen_pool_never_overlaps_the_scored_names():
    # a name already scored daily needs no screen; overlap means a stale pool
    cfg = load_config()
    assert cfg.screen_candidates
    assert not set(cfg.screen_candidates) & (set(cfg.all_tickers) | set(cfg.bench))
    assert cfg.changes.swap_min_growth == 0.15


def test_malformed_screen_block_warns_and_never_breaks_the_load(tmp_path, caplog):
    # the daily run loads this file; only the bench screen reads screen:
    path = _write_cfg(tmp_path, "screen: [QLYS]\n")
    with caplog.at_level(logging.WARNING, logger="sentinel.config"):
        cfg = load_config(path)
    assert cfg.screen_candidates == ()
    assert "screen: must be a block" in _warnings(caplog)


def test_screen_candidates_as_one_string_split_on_commas(tmp_path, caplog):
    path = _write_cfg(tmp_path / "a", "screen:\n  candidates: qlys, intu\n")
    assert load_config(path).screen_candidates == ("QLYS", "INTU")
    path = _write_cfg(tmp_path / "b", "screen:\n  candidates: {a: 1}\n")
    with caplog.at_level(logging.WARNING, logger="sentinel.config"):
        assert load_config(path).screen_candidates == ()
    assert "screen.candidates must be a list" in _warnings(caplog)


def test_swap_min_growth_out_of_range_or_not_a_number_falls_back(tmp_path, caplog):
    for bad in ("15", "fast", "true"):
        path = _write_cfg(tmp_path / bad, f"changes:\n  swap_min_growth: {bad}\n")
        with caplog.at_level(logging.WARNING, logger="sentinel.config"):
            assert load_config(path).changes.swap_min_growth == 0.15
        assert "0.15 means 15 percent" in _warnings(caplog)
        caplog.clear()
    path = _write_cfg(tmp_path / "ok", "changes:\n  swap_min_growth: 0.2\n")
    assert load_config(path).changes.swap_min_growth == 0.2
