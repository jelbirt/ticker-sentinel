#!/usr/bin/env bash
# The single definition of this repo's green bar.
# CI (.github/workflows/ci.yml) and the agent commit guard
# (.claude/hooks/pre-commit-guard.sh) both run THIS script, so the local bar
# and the CI bar cannot drift: there is one list of checks, and this is it.
# If you change the commands here, update the Commands section of CLAUDE.md
# in the same commit.
#
# Bar today: pytest -q (the repo documents no lint/format step; do not invent one here
# without also documenting it in CLAUDE.md).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

PY=python3
if [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
fi

if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
  echo "checks.sh: Python >= 3.12 required (repo pins requires-python >= 3.12)." >&2
  echo "checks.sh: bootstrap with: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'" >&2
  exit 1
fi

if ! "$PY" -c 'import pytest' 2>/dev/null; then
  echo "checks.sh: pytest not importable with $PY." >&2
  echo "checks.sh: bootstrap with: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'" >&2
  exit 1
fi

exec "$PY" -m pytest -q
