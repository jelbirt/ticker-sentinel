#!/usr/bin/env bash
# The single definition of this repo's green bar.
# CI (.github/workflows/ci.yml) and the agent commit guard
# (.claude/hooks/pre-commit-guard.sh) both run THIS script, so the local bar
# and the CI bar cannot drift: there is one list of checks, and this is it.
# If you change the commands here, update the Commands section of CLAUDE.md
# in the same commit.
#
# Bar today: shell syntax over scripts/ and .claude/hooks/, then pytest -q (the
# repo documents no lint/format step; do not invent one here without also
# documenting it in CLAUDE.md). CI runs only this script, so a check added to
# the workflow instead of here would not gate local commits.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

PY=python3
if [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
fi

if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
  echo "checks.sh: Python >= 3.12 required (repo pins requires-python >= 3.12)." >&2
  echo "checks.sh: bootstrap with: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]' -c constraints.txt" >&2
  exit 1
fi

if ! "$PY" -c 'import pytest' 2>/dev/null; then
  echo "checks.sh: pytest not importable with $PY." >&2
  echo "checks.sh: bootstrap with: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]' -c constraints.txt" >&2
  exit 1
fi

# Shell syntax over the scaffolding scripts. One file per invocation: `bash -n
# a.sh b.sh` parses only a.sh and hands the rest to it as positional params, so
# a multi-file call silently checks nothing but the first name. Globbed rather
# than listed so a script added later is covered without editing this loop.
# An unmatched glob stays literal and would be skipped, so this check would
# report green having parsed nothing. Require each pattern to match something.
for pattern in 'scripts/*.sh' '.claude/hooks/*.sh'; do
  matched=0
  for f in $pattern; do
    [ -f "$f" ] || continue
    bash -n "$f"
    matched=1
  done
  if [ "$matched" -eq 0 ]; then
    echo "checks.sh: no shell scripts matched $pattern, refusing to report green." >&2
    exit 1
  fi
done

exec "$PY" -m pytest -q
