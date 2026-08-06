#!/usr/bin/env bash
# Tear down a finished worktree:
#   scripts/rm-worktree.sh <branch> [--force] [--delete-remote]
#
# Gates before removing anything: the branch is merged into main (--force
# skips this, needed for squash-merged branches, and upgrades the branch
# delete to -D), the tree is clean, the target is not the main checkout,
# and it is not the directory you are standing in.
# Order matters: worktree remove, THEN branch delete (git refuses to delete
# a branch still checked out somewhere), then prune.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

BRANCH="${1:?usage: scripts/rm-worktree.sh <branch> [--force] [--delete-remote]}"
shift
FORCE=0
DELREMOTE=0
for a in "$@"; do
  case "$a" in
    --force) FORCE=1 ;;
    --delete-remote) DELREMOTE=1 ;;
    *) echo "rm-worktree: unknown argument $a" >&2; exit 1 ;;
  esac
done
DEFAULT=main

DIR="$(git worktree list --porcelain | awk -v b="refs/heads/$BRANCH" '$1=="worktree"{d=substr($0,10)} $1=="branch"&&$2==b{print d}')"
if [ -z "$DIR" ]; then
  echo "rm-worktree: no worktree found for branch $BRANCH" >&2
  exit 1
fi

MAIN_DIR="$(git rev-parse --show-toplevel)"
if [ "$DIR" = "$MAIN_DIR" ]; then
  echo "rm-worktree: refusing to remove the main checkout" >&2
  exit 1
fi
case "$PWD/" in
  "$DIR"/*) echo "rm-worktree: refusing: you are inside $DIR" >&2; exit 1 ;;
esac

if [ -n "$(git -C "$DIR" status --porcelain)" ]; then
  echo "rm-worktree: tree not clean: $DIR" >&2
  exit 1
fi

if [ "$FORCE" -eq 0 ]; then
  if ! git merge-base --is-ancestor "$BRANCH" "$DEFAULT"; then
    echo "rm-worktree: branch $BRANCH is not merged into $DEFAULT" >&2
    echo "rm-worktree: use --force if it was squash-merged (the merged-check cannot see those)" >&2
    exit 1
  fi
fi

git worktree remove "$DIR"
if [ "$FORCE" -eq 1 ]; then
  git branch -D "$BRANCH"
else
  git branch -d "$BRANCH"
fi
git worktree prune
if [ "$DELREMOTE" -eq 1 ]; then
  git push origin --delete "$BRANCH"
fi
echo "rm-worktree: removed $DIR and branch $BRANCH"
