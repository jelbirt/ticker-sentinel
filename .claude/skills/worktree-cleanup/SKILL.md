---
name: worktree-cleanup
description: Tear down finished ticker-sentinel worktrees safely. Use when the user wants to clean up, tear down, or remove a worktree or workstream, sweep merged worktrees, or delete a finished branch's directory.
---

# worktree-cleanup (ticker-sentinel)

Thin wrapper over `scripts/rm-worktree.sh`, which owns the gating: branch
merged into main, tree clean, never the main checkout, never the directory
you are standing in. Worktree removal happens before branch delete, then
prune.

## With a branch argument

Run `scripts/rm-worktree.sh <branch>`. If it refuses because the branch was
squash-merged (the merged-check cannot see those), confirm with the user,
then rerun with `--force`. Offer `--delete-remote` when a remote branch
exists.

## With no argument: sweep

1. `git worktree list --porcelain` to enumerate worktrees (skip the main
   checkout).
2. For each: merged into main — `git merge-base --is-ancestor
   refs/heads/<branch> refs/heads/main`, or the same against
   `refs/remotes/origin/main` (fetch first) — and clean
   (`git -C <dir> status --porcelain` empty)? Use fully-qualified refs: a
   bare name lets a same-named tag win the lookup and make an unmerged
   branch look merged. Check origin too — branches here merge by PR, so
   local `main` is routinely behind and a local-only test would drop merged
   branches off this list.
3. List the qualifying worktrees, confirm ONCE with the user, then run
   `scripts/rm-worktree.sh <branch>` for each.

## Repo notes

- Each worktree carries its own `.venv` (recreated by `new-worktree.sh`);
  removal needs no extra cleanup, the venv dies with the directory.
- `data/cache/` is tracked; the pen is held by the scheduled Actions bot
  (see tasks/todo.md). A worktree dirty only in `data/cache/` means someone
  edited state the bot owns; surface that instead of forcing removal.
