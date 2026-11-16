# Focused regressions for the three medium-severity round-3 findings.
import shlex
from pathlib import Path

import pytest
from git_test_tools import (
    all_blocks,
    commit_file,
    config_user,
    git,
    hint_ids,
    make_repo,
    write,
)

from git_hints.git import Repo
from git_hints.hint import git_with_paths
from git_hints.reactive import parse_command, suggestion_context


def test_suggestions_preserve_repository_without_glob_conflict(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "target"
    repo.mkdir()
    make_repo(repo, [["git", "init", "-b", "main"]])
    for name in ("[ab].txt", "a.txt", "b.txt"):
        write(repo / name, "example")()
    command = parse_command(
        [
            "git",
            "-C",
            str(repo),
            "--glob-pathspecs",
            "status",
        ]
    )
    note = suggestion_context(Repo(prefix=command.prefix or ()), command)
    assert "--glob-pathspecs" not in note
    assert str(repo) in note
    generated = git_with_paths(["add"], ["[ab].txt"], separator=True)
    git(tmp_path, "-C", str(repo), *shlex.split(generated)[1:])
    assert git(repo, "ls-files", "-z").stdout.split("\0") == ["[ab].txt", ""]


def test_conflicting_environment_gets_manual_path_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_GLOB_PATHSPECS", "1")
    command = git_with_paths(["add"], ["[ab].txt"], separator=True)
    assert "Temporarily clear GIT_GLOB_PATHSPECS" in command
    assert "restore" in command
    assert "git --literal-pathspecs add" in command


def test_environment_identity_is_not_reported_as_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_repo(tmp_path, [["git", "init", "-b", "main"]])
    git(tmp_path, "config", "user.useConfigOnly", "true")
    git(tmp_path, "config", "user.name", "")
    git(tmp_path, "config", "user.email", "")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Review Example")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "review@example.invalid")
    git(tmp_path, "commit", "--allow-empty", "-m", "Environment identity")
    assert "commit-identity" not in hint_ids(tmp_path)


@pytest.mark.parametrize("role", ["AUTHOR", "COMMITTER"])
def test_invalid_environment_identity_is_a_blocker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
) -> None:
    make_repo(tmp_path, [["git", "init", "-b", "main"]])
    config_user("Review Example", "review@example.invalid")
    monkeypatch.setenv(f"GIT_{role}_NAME", " \t ")
    _, found = all_blocks(tmp_path)
    block = found["commit-identity"]
    assert "Priority: error/blocked" in block
    assert "environment" in block.lower()
    assert "Command: git config" not in block


def test_unresolved_upstream_preserves_configured_destination(
    tmp_path: Path,
) -> None:
    make_repo(tmp_path, [["git", "init", "-b", "main"]])
    config_user("Review Example", "review@example.invalid")
    commit_file(tmp_path, "foo.txt", "example", "Base")
    git(tmp_path, "remote", "add", "origin", "../unused-origin")
    git(tmp_path, "remote", "add", "team", "../unused-team")
    git(tmp_path, "config", "branch.main.remote", "team")
    git(tmp_path, "config", "branch.main.merge", "refs/heads/review")
    before = (tmp_path / ".git/config").read_bytes()
    _, found = all_blocks(tmp_path)
    block = found["no-upstream-branch"]
    assert "team" in block and "refs/heads/review" in block
    assert "unavailable" in block
    assert "git push -u" not in block
    assert "Command: git status" in block
    assert (tmp_path / ".git/config").read_bytes() == before
