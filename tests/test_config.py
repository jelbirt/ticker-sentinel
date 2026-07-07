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
    assert cfg.news.tones[0] == "neutral-analyst"