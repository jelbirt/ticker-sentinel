"""Run-history persistence: data/cache/run_history.json.

Cross-run state for day-over-day change detection. reports/ is gitignored and
hosted runners start clean, so this file is committed; it rides the scheduled
workflow's existing `git add data/cache` step (the bot holds the pen, same as
the parquet cache). Bounded by the configured retention; pretty-printed with
sorted keys so committed diffs stay reviewable.

This module deals in plain dicts (the JSON shape); sentinel.report.changes owns
the typed snapshot layer.
"""
from __future__ import annotations

import json
from pathlib import Path

from sentinel.data.cache import cache_dir

SCHEMA_VERSION = 1


def history_path() -> Path:
    return cache_dir() / "run_history.json"


def load_history(path: Path | None = None) -> tuple[list[dict], list[str]]:
    """Return (runs oldest-first, degradation notes). Missing file is an empty
    history; a corrupt, wrong-shaped, or newer-schema file degrades to empty
    with a note."""
    p = path or history_path()
    if not p.exists():
        return [], []
    try:
        raw = json.loads(p.read_text())
        # a file written by a newer schema may reuse "runs" with different
        # semantics: degrade like corruption rather than half-parsing it.
        # Missing (pre-versioning) or equal versions are accepted.
        version = raw.get("version")
        if version is not None and int(version) > SCHEMA_VERSION:
            return [], [
                f"run history schema v{int(version)} is newer than this code "
                f"(v{SCHEMA_VERSION}), change detection reset"
            ]
        runs = raw["runs"]
        if not isinstance(runs, list) or not all(isinstance(r, dict) for r in runs):
            raise ValueError("runs is not a list of objects")
        for r in runs:  # a committed file humans and bots both touch: validate
            tickers = r.get("tickers", {})
            if not isinstance(tickers, dict) or not all(
                isinstance(snap, dict) for snap in tickers.values()
            ):
                raise ValueError("tickers is not a mapping of objects")
    except Exception as exc:
        return [], [f"run history unreadable, change detection reset ({exc})"]
    return sorted(runs, key=lambda r: str(r.get("date", ""))), []


def save_run(run: dict, retention: int, path: Path | None = None) -> None:
    """Append or replace the entry for `run['date']`, prune to `retention`
    entries, and rewrite the file. A corrupt existing file is overwritten fresh
    (its content already degraded to notes on the load side)."""
    p = path or history_path()
    runs, _ = load_history(p)
    runs = [r for r in runs if r.get("date") != run.get("date")] + [run]
    runs = sorted(runs, key=lambda r: str(r.get("date", "")))[-max(retention, 1):]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"version": SCHEMA_VERSION, "runs": runs},
                            indent=2, sort_keys=True))
