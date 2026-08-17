#!/usr/bin/env bash
# Create a worktree for one workstream: scripts/new-worktree.sh <branch> [dir]
# Reuses the branch if it already exists; otherwise branches from main.
#
# What gets wired per worktree, and why:
#   .env / .env.*  copied when present (gitignored secrets; none exist today,
#                  since runtime secrets live in GitHub Actions secrets)
#   .venv          recreated fresh: the editable install (pip install -e) is
#                  path-bound, so a shared or copied venv silently runs the
#                  wrong checkout's code
#   reports/ and data/cache/*.signals.json  gitignored per-run outputs;
#                  recreated by runs, nothing to wire
#   data/cache/*.parquet  tracked in git, so every worktree gets it for free
#
# Shared-state pen rule: data/cache/ is written by the scheduled GitHub
# Actions job, which commits "cache: refresh fundamentals" straight to main
# after each scheduled run. THE BOT HOLDS THE PEN. Do not hand-edit
# data/cache on a workstream branch, and expect to rebase over bot commits
# before merging. Registry: tasks/todo.md.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

BRANCH="${1:?usage: scripts/new-worktree.sh <branch> [dir]}"
DIR="${2:-../ticker-sentinel.$BRANCH}"
DEFAULT=main

trap 'echo "new-worktree: FAILED mid-wiring. Recover with: scripts/rm-worktree.sh $BRANCH --force" >&2' ERR

if git show-ref --verify --quiet "refs/heads/$BRANCH"; then
  git worktree add "$DIR" "$BRANCH"
else
  git worktree add -b "$BRANCH" "$DIR" "$DEFAULT"
fi

for f in .env .env.*; do
  if [ -f "$f" ]; then
    cp "$f" "$DIR/$f"
    echo "new-worktree: copied $f"
  fi
done

echo "new-worktree: creating venv + editable install (takes a minute)..."
python3 -m venv "$DIR/.venv"
# -c constraints.txt: the same committed pin set CI and the scheduled workflows
# install with, so a worktree venv cannot resolve a different transitive set
(cd "$DIR" && .venv/bin/pip install --quiet -e ".[dev]" -c constraints.txt)

echo ""
echo "Worktree ready: $DIR (branch $BRANCH)"
echo "Next step: (cd $DIR && scripts/checks.sh)"
