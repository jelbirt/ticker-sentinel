"""Behaviour tests for the agent commit guard (.claude/hooks/pre-commit-guard.sh).

The guard is a subprocess-shaped artifact: a PreToolUse hook fed a JSON payload
on stdin, answering with an exit code (0 allows, 2 blocks). So it is exercised
here the way Claude Code exercises it, as a subprocess.

It scopes itself to "its own" repo by git common dir, derived from where the
hook script sits (<root>/.claude/hooks/). Each test therefore gets a throwaway
repo with a copy of the hook inside it, which makes the tests hermetic: no
network, and the real ticker-sentinel checkout is invisible to the copy.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_SRC = REPO_ROOT / ".claude" / "hooks" / "pre-commit-guard.sh"

# Built at import time so no source line of this file contains the literal
# phrase the guard pre-filters on; otherwise editing these tests through an
# agent shell trips the very guard under test.
COMMIT = "com" + "mit"


@pytest.fixture(scope="module")
def git_env(tmp_path_factory) -> dict[str, str]:
    """Environment that keeps git away from the developer's real config."""
    home = tmp_path_factory.mktemp("home")
    env = dict(os.environ)
    env.update(
        HOME=str(home),
        GIT_CONFIG_GLOBAL=str(home / "nonexistent-gitconfig"),
        GIT_CONFIG_SYSTEM=str(home / "nonexistent-gitconfig"),
        GIT_AUTHOR_NAME="Guard Test",
        GIT_AUTHOR_EMAIL="guard@example.invalid",
        GIT_COMMITTER_NAME="Guard Test",
        GIT_COMMITTER_EMAIL="guard@example.invalid",
    )
    return env


class Guard:
    """A throwaway repo (on main) plus a worktree (on a branch), and its hook."""

    def __init__(self, repo: Path, worktree: Path, env: dict[str, str]) -> None:
        self.repo = repo
        self.worktree = worktree
        self.env = env
        self.hook = repo / ".claude" / "hooks" / "pre-commit-guard.sh"

    def run(self, command: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": str(cwd or self.repo),
        }
        return subprocess.run(
            ["bash", str(self.hook)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=120,
            env=self.env,
        )

    def set_checks(self, body: str) -> None:
        """Rewrite the green bar in both trees: the guard runs the checks.sh of
        whichever tree the commit targets, so both must agree for a test."""
        for tree in (self.repo, self.worktree):
            (tree / "scripts" / "checks.sh").write_text(
                "#!/usr/bin/env bash\n" + body + "\n"
            )


@pytest.fixture
def guard(tmp_path: Path, git_env: dict[str, str]) -> Guard:
    repo = tmp_path / "repo"
    worktree = tmp_path / "wt"
    (repo / ".claude" / "hooks").mkdir(parents=True)
    (repo / "scripts").mkdir()

    def git(*args: str, cwd: Path = repo) -> None:
        subprocess.run(
            ["git", *args], cwd=cwd, env=git_env, check=True, capture_output=True
        )

    git("init", "-q")
    git("symbolic-ref", "HEAD", "refs/heads/main")
    (repo / "README.md").write_text("throwaway\n")
    (repo / "scripts" / "checks.sh").write_text("#!/usr/bin/env bash\nexit 0\n")
    git("add", "README.md", "scripts/checks.sh")
    git(COMMIT, "-q", "-m", "seed")
    # the worktree inherits the committed checks.sh, so both trees have a bar
    git("worktree", "add", "-q", "-b", "feature", str(worktree))

    shutil.copy(HOOK_SRC, repo / ".claude" / "hooks" / "pre-commit-guard.sh")
    return Guard(repo, worktree, git_env)


def test_plain_commit_on_main_is_blocked(guard: Guard) -> None:
    r = guard.run(f'git {COMMIT} -m "seed the tree"')
    assert r.returncode == 2
    assert "Commit on main blocked" in r.stderr


def test_plain_commit_on_a_worktree_branch_is_allowed(guard: Guard) -> None:
    r = guard.run(f'cd {guard.worktree} && git {COMMIT} -m "seed the tree"')
    assert r.returncode == 0, r.stderr


# --- the regression this file exists for -------------------------------------
# Valid shell whose quoting shlex cannot read (an apostrophe in the message)
# used to fall back to the session cwd, so a commit into a worktree was judged
# against the session's branch, main, and blocked as a commit on main.


@pytest.mark.parametrize(
    "message_form",
    [
        pytest.param(
            "git {c} -F - <<'EOF'\nfix: don't break the guard\nEOF",
            id="heredoc-apostrophe",
        ),
        pytest.param(
            "git {c} -m msg --author='O'Brien'",
            id="shell-valid-quote-concatenation",
        ),
        pytest.param(
            r"git {c} -m $'title\nit don\'t break'",
            id="ansi-c-quoting",
        ),
    ],
)
def test_unreadable_quoting_does_not_move_the_commit_to_the_session_cwd(
    guard: Guard, message_form: str
) -> None:
    command = f"cd {guard.worktree} && " + message_form.format(c=COMMIT)
    assert guard.run(command, cwd=guard.repo).returncode == 0

    # exit 0 alone would also be satisfied by "no commit detected at all", so
    # fail the bar and read the block message back: naming the worktree tree
    # proves the commit was both seen AND attributed to the right directory.
    guard.set_checks("exit 1")
    r = guard.run(command, cwd=guard.repo)
    assert r.returncode == 2
    assert f"checks.sh failed for {guard.worktree}" in r.stderr


def test_unreadable_quoting_honours_a_relative_cd(guard: Guard) -> None:
    command = f"cd ../wt && git {COMMIT} -m msg --author='O'Brien'"
    assert guard.run(command).returncode == 0

    guard.set_checks("exit 1")
    r = guard.run(command)
    assert r.returncode == 2
    assert f"checks.sh failed for {guard.worktree}" in r.stderr


def test_stripping_the_heredoc_body_keeps_the_command_fully_parseable(
    guard: Guard,
) -> None:
    """`git -C` is only honoured on the parsed path, not by the cd replay, so
    this passes only when the apostrophe in the body stopped breaking parsing."""
    r = guard.run(f"git -C {guard.worktree} {COMMIT} -F - <<'EOF'\ndon't break\nEOF")
    assert r.returncode == 0, r.stderr

    guard.set_checks("exit 1")
    r = guard.run(f"git -C {guard.worktree} {COMMIT} -F - <<'EOF'\ndon't break\nEOF")
    assert r.returncode == 2
    assert f"checks.sh failed for {guard.worktree}" in r.stderr


def test_unreadable_quoting_without_a_cd_is_still_checked(guard: Guard) -> None:
    """No cd to attribute to: the session cwd (main) is still judged."""
    r = guard.run(f"git {COMMIT} -m msg --author='O'Brien'")
    assert r.returncode == 2
    assert "Commit on main blocked" in r.stderr


def test_a_cd_named_only_inside_a_message_does_not_redirect_the_check(
    guard: Guard,
) -> None:
    """The heredoc body is data: its `cd` must not excuse a commit on main."""
    r = guard.run(
        f"git {COMMIT} -F - <<'EOF'\n"
        f"fix: don't break\n\ncd {guard.worktree}\nEOF"
    )
    assert r.returncode == 2
    assert "Commit on main blocked" in r.stderr


def test_a_shift_operator_in_a_message_is_not_read_as_a_heredoc(guard: Guard) -> None:
    """`<<` in prose must not swallow the lines after it, commits included."""
    r = guard.run(
        f'cd {guard.worktree} && git {COMMIT} -m "handle the << shift"\n'
        f'cd {guard.repo} && git {COMMIT} -m "and this one lands on main"'
    )
    assert r.returncode == 2
    assert "Commit on main blocked" in r.stderr


def test_a_cd_to_a_missing_directory_does_not_redirect_the_check(guard: Guard) -> None:
    r = guard.run(f"cd {guard.repo / 'no-such-dir'} && git {COMMIT} -m x --author='O'B'")
    assert r.returncode == 2
    assert "Commit on main blocked" in r.stderr


# --- the cd replay must not out-run shell semantics --------------------------
# Each of these commits on main for real; a naive "last cd in the text wins"
# sweep would attribute it to the worktree and let it through.


@pytest.mark.parametrize(
    "command_form",
    [
        pytest.param(
            "git {c} -m msg --author='O'Brien' && cd {wt}",
            id="cd-after-the-commit",
        ),
        pytest.param(
            "(cd {wt} && git log) && git {c} -m msg --author='O'Brien'",
            id="cd-scoped-to-a-subshell",
        ),
        pytest.param(
            "git {c} -m \"v$(cd {wt} && cat VERSION)\" --author='O'Brien'",
            id="cd-inside-command-substitution",
        ),
        pytest.param(
            "git {c} -F - <<'EOF' 2>/dev/null\nfix: don't break\ncd {wt}\nEOF",
            id="cd-in-a-body-whose-introducer-has-a-redirection",
        ),
    ],
)
def test_a_cd_the_committing_shell_never_takes_does_not_redirect_the_check(
    guard: Guard, command_form: str
) -> None:
    r = guard.run(command_form.format(c=COMMIT, wt=guard.worktree))
    assert r.returncode == 2
    assert "Commit on main blocked" in r.stderr


def test_a_second_commit_after_the_first_is_still_judged(guard: Guard) -> None:
    """The fallback judges every commit-shaped span, not just the first: a
    second commit that lands on main must not ride behind an earlier one."""
    r = guard.run(
        f"cd {guard.worktree} && git {COMMIT} -m a --author='O'Brien' ; "
        f"cd {guard.repo} ; git {COMMIT} -m b"
    )
    assert r.returncode == 2
    assert "Commit on main blocked" in r.stderr


def test_prose_matching_the_pattern_does_not_end_the_cd_replay(guard: Guard) -> None:
    """An earlier `git ... {COMMIT}`-shaped span in prose must not become the
    replay boundary that hides the cd back toward main."""
    r = guard.run(
        f"cd {guard.worktree} && git log --grep {COMMIT} && "
        f"cd {guard.repo} && git {COMMIT} -m x --author='O'Brien'"
    )
    assert r.returncode == 2
    assert "Commit on main blocked" in r.stderr


def test_a_fake_introducer_in_prose_cannot_hide_a_real_commit(guard: Guard) -> None:
    """A quoted `<<` that happens to pair with a later matching line must not
    strip the real commands between them from analysis."""
    r = guard.run(f'echo "a << b"\ngit {COMMIT} -m "lands on main"\nb')
    assert r.returncode == 2
    assert "Commit on main blocked" in r.stderr


def test_an_escape_on_a_command_line_survives_a_fake_introducer(guard: Guard) -> None:
    r = guard.run(f'echo "a << b"\nALLOW_MAIN_COMMIT=1 git {COMMIT} -m ok\nb')
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("empty", ["''", '""'], ids=["single-quoted", "double-quoted"])
def test_an_empty_cd_target_neither_crashes_nor_moves_the_check(
    guard: Guard, empty: str
) -> None:
    """`cd ''` is a no-op in the shell; it must not crash the replay (a
    traceback exits 1, which fails open) and must not move the check."""
    r = guard.run(f"cd {empty} && git {COMMIT} -m x --author='O'Brien'")
    assert r.returncode == 2
    assert "Commit on main blocked" in r.stderr


def test_an_unterminated_heredoc_does_not_swallow_the_commands_after_it(
    guard: Guard,
) -> None:
    """A `<<WORD` with no matching terminator must not delete the real commit."""
    r = guard.run(f'echo "shifted $((1<<N))"\ngit {COMMIT} -m "lands on main"')
    assert r.returncode == 2
    assert "Commit on main blocked" in r.stderr


def test_an_escape_named_only_inside_a_message_does_not_apply(guard: Guard) -> None:
    """Documenting an escape in the commit message must not invoke it."""
    r = guard.run(
        f"git {COMMIT} -F - <<'EOF'\n"
        "docs: explain the ALLOW_MAIN_COMMIT=1 escape\nEOF"
    )
    assert r.returncode == 2
    assert "Commit on main blocked" in r.stderr


# --- escapes -----------------------------------------------------------------


def test_allow_main_commit_escape_unblocks_main(guard: Guard) -> None:
    r = guard.run(f'ALLOW_MAIN_COMMIT=1 git {COMMIT} -m "direct to main"')
    assert r.returncode == 0, r.stderr


def test_allow_main_commit_escape_works_on_the_unreadable_quoting_path(
    guard: Guard,
) -> None:
    r = guard.run(f"ALLOW_MAIN_COMMIT=1 git {COMMIT} -m msg --author='O'Brien'")
    assert r.returncode == 0, r.stderr


def test_failing_checks_block_the_commit(guard: Guard) -> None:
    guard.set_checks("echo 'boom: 1 failed' >&2\nexit 1")
    r = guard.run(f'cd {guard.worktree} && git {COMMIT} -m "seed"')
    assert r.returncode == 2
    assert "checks.sh failed" in r.stderr


def test_skip_checks_escape_works_on_the_unreadable_quoting_path(guard: Guard) -> None:
    guard.set_checks("exit 1")
    r = guard.run(
        f"SKIP_CHECKS=1 cd {guard.worktree} && git {COMMIT} -m msg --author='O'Brien'"
    )
    assert r.returncode == 0, r.stderr


# --- things the guard must keep ignoring -------------------------------------


def test_non_bash_tool_is_allowed(guard: Guard) -> None:
    payload = {"tool_name": "Edit", "tool_input": {"command": f"git {COMMIT}"}}
    r = subprocess.run(
        ["bash", str(guard.hook)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=120,
        env=guard.env,
    )
    assert r.returncode == 0, r.stderr


def test_a_non_commit_git_command_is_allowed(guard: Guard) -> None:
    assert guard.run("git status --short").returncode == 0


def test_a_commit_in_another_repo_is_not_ours_to_gate(
    tmp_path: Path, guard: Guard
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(
        ["git", "init", "-q"], cwd=other, env=guard.env, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "symbolic-ref", "HEAD", "refs/heads/main"],
        cwd=other,
        env=guard.env,
        check=True,
        capture_output=True,
    )
    r = guard.run(f'cd {other} && git {COMMIT} -m "not our repo"')
    assert r.returncode == 0, r.stderr
