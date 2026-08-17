#!/usr/bin/env bash
# Tear down a finished worktree:
#   scripts/rm-worktree.sh <branch> [--force] [--delete-remote]
#
# Gates before removing anything: a worktree exists for that branch and its
# directory is still present, the target is not the main checkout, it is not
# the directory you are standing in, the tree is clean, and the branch is
# merged into main (locally or on origin). --force skips only the merged gate,
# which is what a squash merge needs.
#
# Order matters, and so does who decides. The worktree must be removed before
# the branch, because git refuses to delete a branch that is still checked out
# somewhere. That makes the removal irreversible by the time the branch delete
# runs, so nothing after it may veto: we use `git branch -D` and let the merged
# gate above be the sole authority. `git branch -d` asks a different question
# (reachable from HEAD or upstream, not "merged into main"), and when the two
# disagree it refuses after the worktree is already gone: worktree removed,
# branch orphaned, prune never run, and the error steers the next attempt at
# --force, the one flag that skips the merged gate.
set -euo pipefail

# Snapshot the caller's cwd BEFORE the cd below, or the "you are inside it"
# gate compares the repo root against itself and never fires.
INVOKED_FROM="$(pwd -P 2>/dev/null || printf '%s' "${PWD:-}")"

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

# A registered worktree whose directory is gone is 'prunable'. Every gate below
# would fail open on it, so stop here rather than deleting a branch whose real
# checkout may only have been moved.
if [ ! -d "$DIR" ]; then
  echo "rm-worktree: worktree for $BRANCH is registered at $DIR, but that" >&2
  echo "rm-worktree: directory does not exist. If it moved, run 'git worktree" >&2
  echo "rm-worktree: repair'; if it is gone, run 'git worktree prune'." >&2
  exit 1
fi

MAIN_DIR="$(git rev-parse --show-toplevel)"
if [ "$DIR" = "$MAIN_DIR" ]; then
  echo "rm-worktree: refusing to remove the main checkout" >&2
  exit 1
fi
# Both the caller's cwd and the repo root: removing the directory a session is
# standing in leaves every later command failing on a deleted cwd.
for d in "$INVOKED_FROM" "$MAIN_DIR"; do
  case "$d" in
    "$DIR"|"$DIR"/*) echo "rm-worktree: refusing: you are inside $DIR" >&2; exit 1 ;;
  esac
done

# Capture the status output and its exit code separately: a command
# substitution inside `[ ]` throws the exit code away, so a FAILING git status
# would read as an empty (clean) tree and skip this gate entirely.
if ! STATUS="$(git -C "$DIR" status --porcelain 2>&1)"; then
  echo "rm-worktree: cannot read the status of $DIR, refusing" >&2
  printf '%s\n' "$STATUS" >&2
  exit 1
fi
if [ -n "$STATUS" ]; then
  echo "rm-worktree: tree not clean: $DIR" >&2
  printf '%s\n' "$STATUS" >&2
  exit 1
fi

# Fully-qualified refs: a bare name lets a same-named TAG win the lookup and
# make an unmerged branch look merged. Check origin as well as the local ref,
# because branches here are merged by a PR on GitHub and local main is usually
# behind, so a local-only test reports "unmerged" and trains the --force habit.
git fetch --quiet origin "$DEFAULT" 2>/dev/null || true
MERGED=0
for base in "refs/heads/$DEFAULT" "refs/remotes/origin/$DEFAULT"; do
  git show-ref --verify --quiet "$base" || continue
  if git merge-base --is-ancestor "refs/heads/$BRANCH" "$base"; then
    MERGED=1
    break
  fi
done
if [ "$MERGED" -eq 0 ]; then
  if [ "$FORCE" -eq 0 ]; then
    echo "rm-worktree: branch $BRANCH is not merged into $DEFAULT (local or origin)" >&2
    echo "rm-worktree: use --force if it was squash-merged (the merged-check cannot see those)" >&2
    exit 1
  fi
  echo "rm-worktree: $BRANCH is not detectably merged; proceeding because --force was given"
fi

git worktree remove "$DIR"
# -D, not -d: see the header. The merged gate above is the authority, and
# nothing may veto once the worktree is already gone.
git branch -D "$BRANCH"
git worktree prune
if [ "$DELREMOTE" -eq 1 ]; then
  git push origin --delete "$BRANCH"
fi
echo "rm-worktree: removed $DIR and branch $BRANCH"
