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
# They count only as an env-var PREFIX on a command (line start or just after a
# shell separator, optionally behind other assignments), so naming one inside a
# commit message does not invoke it. See escape_prefixes() for the details.
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
    (unbalanced quotes: shlex's quoting rules are simpler than the shell's, so
    e.g. --author='O'Brien' or $'it don\\'t' is valid shell it cannot read)."""
    lex = shlex.shlex(text, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        return list(lex)
    except ValueError:
        return None


# `<<WORD` introducing a heredoc, in redirection position: preceded by a space
# or a separator, which excludes `<<<` herestrings and shifts like $((1<<N)).
HEREDOC_RE = re.compile(r"(?:^|(?<=[\s|&;(]))<<-?\s*(['\"]?)([A-Za-z_][A-Za-z_0-9]*)\1")

# The `git ... commit` invocation, with no other `git` in between so the match
# lands on the committing invocation rather than an earlier one.
GIT_COMMIT_RE = re.compile(r"\bgit\b(?:(?!\bgit\b).)*?\bcommit\b", re.S)


def mask_quoted(text):
    """Blank every quoted span to a placeholder letter, position for position.

    Lets the escape scan below tell shell syntax from message text: without it
    a quoted `-m` message is just more characters, and an escape merely NAMED
    in one (`-m "wip; SKIP_CHECKS=1 was not used"`) would invoke it. Both
    failure modes land conservatively: an unbalanced quote masks everything to
    the end of the text, hiding escapes rather than inventing them, and a
    quoted assignment value collapses to one word so `X='a b' SKIP_CHECKS=1
    git commit ...` still reads as a prefix run. The placeholder is a letter,
    not a space, so masking can never splice a separator up against an escape
    that was not in prefix position (`; "X=foo" SKIP_CHECKS=1` stays inert,
    matching the shell, where a quoted word is a command name, not a prefix).
    Lengths are preserved so offsets into the mask index the original text.

    Only quoting is modeled, not the shell's full grammar: a backslash outside
    quotes masks the character it escapes (which is how `\\;` stops being a
    separator, and how a `\\`-newline line continuation stops being one), while
    inside single quotes a backslash stays literal, per POSIX.
    """
    out, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            out.append("QQ")
            i += 2
        elif ch in "'\"":
            j = text.find(ch, i + 1)
            if j < 0:
                out.append("Q" * (n - i))  # unbalanced: the rest is quoted
                break
            out.append("Q" * (j - i + 1))
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def strip_heredocs(text, strict=False):
    """Drop heredoc BODIES (and their terminator lines) before analysis.

    A heredoc body is data, never command syntax, but the newline rewrite in
    analyze() flattens it onto the command line, where an apostrophe in a
    commit message ("don't") reads as an unbalanced quote and kills tokenize().
    Removing the body first keeps the canonical `git commit -F - <<'EOF'`
    message form fully parseable, so its `cd` prefix is honored normally.

    Stripping only happens when the terminator line is actually there, and
    never when the body itself smells like a commit. Both are the conservative
    direction: a `<<` in prose (`-m "the << shift"`) almost never has a
    terminator, so its lines survive to be analyzed, and when quoted prose DOES
    pair with a later matching line (HEREDOC_RE cannot see shell quoting), a
    real `git commit` between them is kept rather than deleted as "body"; a
    genuine message body that merely mentions git commit then degrades to the
    conservative fallback, which blocks toward the session cwd. Only the first
    heredoc on a line is handled; a second one's body stays as text, where at
    worst it fails tokenize() and takes the conservative fallback in analyze().

    strict=True is the escape scan's variant, because for escapes the keep runs
    the WRONG way: keeping the body means a commit message whose line starts
    with `SKIP_CHECKS=1 git commit ...` (an entirely normal message in a repo
    whose agents write about this guard) switches the guard off. Quoting is
    what tells the two cases apart, so strict mode trusts it: an introducer
    inside a quoted span is prose (`echo "a << b"`), not an introducer, and its
    lines survive; an unquoted one is real, so its terminated body is dropped
    unconditionally. The mask is taken per line, so an apostrophe elsewhere in
    the command cannot move the answer. Both directions stay conservative here:
    a body wrongly kept can only fail to hide an escape, and a body wrongly
    dropped can only hide one.
    """
    lines, out, i = text.split("\n"), [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        m = HEREDOC_RE.search(line)
        if not m:
            continue
        if strict and "Q" in mask_quoted(line)[m.start():m.start() + 2]:
            continue  # the `<<` is inside quotes on this line: prose
        word, j = m.group(2), i
        while j < len(lines) and lines[j].strip() != word:
            j += 1
        if j >= len(lines):
            continue  # unterminated: not a heredoc we can trust, keep the lines
        if not strict and GIT_COMMIT_RE.search("\n".join(lines[i:j])):
            continue  # the "body" smells like a commit: keep it, per above
        i = j + 1  # drop the body and its terminator
    return "\n".join(out)


# A `cd` that starts a command (line start or after a separator, optionally
# behind env-var prefixes), plus the parens that scope one. The separator is
# matched look-behind so an opening `(` is left for the paren tracking in
# scan_cd. Only used on the tokenizer-failure path below.
CD_RE = re.compile(
    r"(?:^|(?<=[\n;&|(]))\s*(?:[A-Za-z_][A-Za-z_0-9]*=\S*\s+)*"
    r"cd\s+(?:'([^']*)'|\"([^\"]*)\"|([^\s;&|<>()]+))"
)
CD_OR_PAREN_RE = re.compile(r"[()]|" + CD_RE.pattern)


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


def scan_cd(text, cwd, end):
    """Directory the command at text[end:] would run in, by replaying the
    `cd`s in text[:end] with a regex.

    The fallback for text tokenize() rejected: without this the commit is
    attributed to the session cwd, so `cd <worktree> && git commit ...` with a
    quote shlex cannot read gets judged against the session's branch (main) and
    blocked as a commit on main.

    A regex sweep is not shell semantics, so the sweep is bounded to keep every
    ambiguity resolving toward the session cwd (what the old fallback used, and
    the stricter answer whenever the session sits on main):
      - only `cd`s BEFORE the commit being attributed count (the caller passes
        that commit's match start as `end`), so a trailing `cd` cannot
        retroactively move a commit that already ran;
      - only `cd`s at paren depth 0 count, since a subshell or a `$( )` never
        moves the shell that commits;
      - only targets that exist as directories count, so a `cd` quoted inside
        prose cannot aim the check at a phantom path (and a real `cd` to a
        missing directory would fail in the shell anyway); an empty target
        (`cd ''`) is skipped, matching the shell, where it does not move.
    Unhandled, both resolving to the session cwd: `cd "$VAR"` (no variable
    expansion here, so the target is not a directory and is skipped), and
    `git -C <dir>`, which the session cwd fallback never honored either.
    """
    cur, depth = cwd, 0
    for m in CD_OR_PAREN_RE.finditer(text, 0, end):
        tok = m.group(0)
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            target = next(g for g in m.group(1, 2, 3) if g is not None)
            if target:
                cand = resolve(cur, target)
                if os.path.isdir(cand):
                    cur = cand
    return cur


def analyze(text, cwd, depth=0):
    """Return list of (target_dir, env_prefix_dict) for every git commit found.
    Text that will not tokenize is treated conservatively as commits when it
    smells like one, never skipped: every commit-shaped span is attributed to
    the directory its own preceding `cd`s lead to (the session cwd when there
    are none), so quoting inside a commit message cannot move the check to the
    wrong directory."""
    if depth > 4:
        return []
    # A heredoc body is data; strip it before the newline rewrite below can
    # flatten its punctuation onto the command line.
    text = strip_heredocs(text)
    # shlex treats a bare newline as whitespace, which would merge multi-line
    # commands (the canonical `git add` / `git commit` two-liner) into one
    # undetectable segment. The text is only analyzed, never executed, so
    # rewriting newlines as separators is safe: inside quotes it merely
    # alters a message token's content, which detection never inspects.
    tokens = tokenize(text.replace("\n", " ; "))
    if tokens is None:
        # judge EVERY commit-shaped span, each against the directory its own
        # preceding cds lead to: a single record for the first span would let
        # a second commit later in the same command escape unjudged
        found = [
            (scan_cd(text, cwd, m.start()), {}) for m in GIT_COMMIT_RE.finditer(text)
        ]
        if found:
            return found
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


# An escape counts only in command-prefix position: at the start of the text or
# just after a shell separator, optionally behind other env-var assignments
# (`SKIP_CHECKS=1 ALLOW_MAIN_COMMIT=1 git commit ...`). Same separator class as
# CD_RE, and deliberately no more: `{` (group command) and a backtick are left
# out, as is a leading `env` word, because the parsed path already reads those
# through envs, so all the textual scan would add there is another way for text
# to turn an escape on. Every such call is decided toward NOT escaping, since
# an escape only ever makes the guard more permissive.
ESCAPE_RE = r"(?:^|[\n;&|(])\s*(?:[A-Za-z_][A-Za-z_0-9]*=\S*\s+)*%s=1\b"


def escape_prefixes(name, text):
    return re.search(ESCAPE_RE % name, text) is not None


commits = analyze(cmd, cwd)
if not commits:
    allow()

# Escapes typed as a prefix ANYWHERE in the command count, not only on the
# committing segment: overrides stay visible in the transcript, and the
# tokenizer-failure path (where envs is empty) still honors them. Heredoc
# bodies are dropped and the quoted spans of what is left are masked, so a
# commit message that merely documents an escape does not trip it. Masking
# comes AFTER the strip, since a body's apostrophe would otherwise read as an
# unbalanced quote and hide a real escape typed after the terminator.
escapable = mask_quoted(strip_heredocs(cmd, strict=True))
skip_checks = escape_prefixes("SKIP_CHECKS", escapable)
allow_main = escape_prefixes("ALLOW_MAIN_COMMIT", escapable)

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
        # against a DIFFERENT tree's state: fail open instead
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
