#!/usr/bin/env bash
# Agent commit guard: PreToolUse hook for the Bash tool (wired in
# .claude/settings.json). Enforces at the tool call what CLAUDE.md states:
#   1. agent commits into THIS repo must pass scripts/checks.sh, and
#   2. agent commits must not land on main (branch-per-workstream + PRs).
#
# Contract: exit 0 allows, exit 2 blocks (stderr becomes the reason shown to
# the agent). This is a guardrail for cooperating sessions, NOT a security
# boundary: it fails open on anything it cannot parse at the payload level so
# it never wedges unrelated work. It scopes to this repo by git common dir, so
# every worktree of this repo is gated and sibling repos are ignored.
#
# Escapes, typed into the commit command itself so overrides are deliberate
# and visible in the transcript (documented in CLAUDE.md):
#   SKIP_CHECKS=1        git commit ...   skip the checks.sh gate
#   ALLOW_MAIN_COMMIT=1  git commit ...   permit a commit on main
set -uo pipefail

HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export GUARD_REPO_ROOT="$(cd "$HOOK_DIR/../.." && pwd)"

CODE="$(cat <<'PYEOF'
import json, os, re, shlex, subprocess, sys

def allow():
    sys.exit(0)

def block(msg):
    sys.stderr.write(msg + "\n")
    sys.exit(2)

try:
    payload = json.load(sys.stdin)
except Exception:
    allow()  # unreadable payload: fail open

if payload.get("tool_name") != "Bash":
    allow()
cmd = (payload.get("tool_input") or {}).get("command") or ""
if "git" not in cmd:
    allow()  # cheap pre-filter: nothing git-shaped here
cwd = payload.get("cwd") or os.getcwd()
repo_root = os.environ.get("GUARD_REPO_ROOT", "")


def run_git(args, timeout=20):
    try:
        r = subprocess.run(["git"] + args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip()
    except Exception:
        return 1, ""


def common_dir(path):
    rc, out = run_git(["-C", path, "rev-parse", "--path-format=absolute", "--git-common-dir"])
    return os.path.realpath(out) if rc == 0 and out else None


GUARD_COMMON = common_dir(repo_root)
if GUARD_COMMON is None:
    allow()  # cannot identify our own repo: fail open


def tokenize(text):
    """Shell-aware token list, or None when the text will not tokenize
    (unbalanced quotes: common with multi-line/heredoc commit messages)."""
    lex = shlex.shlex(text, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        return list(lex)
    except ValueError:
        return None


def split_segments(tokens):
    segs, cur = [], []
    for t in tokens:
        if t and set(t) <= set(";&|()"):
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        segs.append(cur)
    return segs


ENV_RE = re.compile(r"^([A-Za-z_][A-Za-z_0-9]*)=(.*)$")


def resolve(base, path):
    path = os.path.expanduser(path)
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(base, path))


def analyze(text, cwd, depth=0):
    """Return list of (target_dir, env_prefix_dict) for every git commit found.
    A segment that will not tokenize is treated conservatively as a commit in
    the current directory when it smells like one, never skipped."""
    if depth > 4:
        return []
    # shlex treats a bare newline as whitespace, which would merge multi-line
    # commands (the canonical `git add` / `git commit` two-liner) into one
    # undetectable segment. The text is only analyzed, never executed, so
    # rewriting newlines as separators is safe: inside quotes it merely
    # alters a message token's content, which detection never inspects.
    tokens = tokenize(text.replace("\n", " ; "))
    if tokens is None:
        if "git" in text and "commit" in text:
            return [(cwd, {})]
        return []
    commits = []
    cur = cwd
    for seg in split_segments(tokens):
        i, envs = 0, {}
        while i < len(seg):
            t = seg[i]
            if t == "env":
                i += 1
                continue
            m = ENV_RE.match(t)
            if m:
                envs[m.group(1)] = m.group(2)
                i += 1
                continue
            break
        if i >= len(seg):
            continue
        prog = os.path.basename(seg[i])
        if prog == "cd":
            cur = resolve(cur, seg[i + 1]) if i + 1 < len(seg) else os.path.expanduser("~")
            continue
        if prog in ("sh", "bash", "zsh", "dash"):
            for j in range(i + 1, len(seg) - 1):
                if seg[j] == "-c":
                    commits.extend(analyze(seg[j + 1], cur, depth + 1))
                    break
            continue
        if prog == "eval":
            commits.extend(analyze(" ".join(seg[i + 1:]), cur, depth + 1))
            continue
        if prog != "git":
            continue
        j, gdir = i + 1, cur
        while j < len(seg):
            t = seg[j]
            if t == "-C" and j + 1 < len(seg):
                gdir = resolve(gdir, seg[j + 1])
                j += 2
                continue
            if t == "-c" and j + 1 < len(seg):
                j += 2
                continue
            if t.startswith("-"):
                j += 1
                continue
            break
        if j < len(seg) and seg[j] == "commit":
            commits.append((gdir, envs))
    return commits


commits = analyze(cmd, cwd)
if not commits:
    allow()

# Escapes typed anywhere in the command count: overrides stay visible in the
# transcript, and the tokenizer-failure path still honors them.
skip_checks = "SKIP_CHECKS=1" in cmd
allow_main = "ALLOW_MAIN_COMMIT=1" in cmd

for gdir, envs in commits:
    if common_dir(gdir) != GUARD_COMMON:
        continue  # commit aimed at a different repo (or not a repo): not ours to gate

    if not (allow_main or envs.get("ALLOW_MAIN_COMMIT") == "1"):
        rc, branch = run_git(["-C", gdir, "symbolic-ref", "--short", "HEAD"])
        if rc == 0 and branch == "main":
            block(
                "Commit on main blocked: this repo uses branch-per-workstream + PRs "
                "(scripts/new-worktree.sh <branch>). For a deliberate direct-to-main "
                "commit, prefix the command with ALLOW_MAIN_COMMIT=1."
            )

    if not (skip_checks or envs.get("SKIP_CHECKS") == "1"):
        rc, top = run_git(["-C", gdir, "rev-parse", "--show-toplevel"])
        tree = top if rc == 0 and top else repo_root
        # the tree being committed is judged by ITS OWN checks.sh, so a branch
        # that changes the bar is measured against its own version; a tree
        # without one (branched before the scaffold landed) is not measured
        # against a DIFFERENT tree's state — fail open instead
        checks = os.path.join(tree, "scripts", "checks.sh")
        if os.path.isfile(checks):
            try:
                r = subprocess.run(
                    ["bash", checks], capture_output=True, text=True, timeout=600, cwd=tree
                )
            except Exception:
                allow()  # cannot run the bar: fail open, per header
            if r.returncode != 0:
                tail = ((r.stdout or "") + "\n" + (r.stderr or "")).strip().splitlines()[-15:]
                block(
                    "Commit blocked: scripts/checks.sh failed for " + tree + "\n"
                    + "\n".join(tail)
                    + "\nFix the failures (or, deliberately: SKIP_CHECKS=1 git commit ...)."
                )

allow()
PYEOF
)"

exec python3 -c "$CODE"
