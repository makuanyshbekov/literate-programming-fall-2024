# Regression coverage from the final review of `git-hints explain`.
import json
import shlex
from pathlib import Path

import pytest
from git_test_tools import (
    blocks,
    commit_file,
    config_user,
    git,
    make_repo,
    make_temps,
    output_ids,
    run_hints,
    write,
)
from test_explain import REBASE_HINT, diverged_clones, explain, rejected_pushes

from git_hints.git import Repo
from git_hints.hint import git_with_paths, render_block
from git_hints.proactive import Context, merge_conflicts
from git_hints.reactive import overwritten_paths


@pytest.mark.parametrize("separator", [[], ["--"]])
def test_explain_forwards_git_help(
    tmp_path: Path, separator: list[str]
) -> None:
    result = run_hints(tmp_path, "explain", *separator, "git", "--help")
    expected = git(tmp_path, "--help")
    assert result.returncode == expected.returncode
    assert result.stdout.startswith(expected.stdout)
    assert "Git exited with code 0." in result.stdout


@pytest.mark.parametrize("separator", [[], ["--"]])
def test_explain_rejects_fetch_after_subcommand(
    tmp_path: Path, separator: list[str]
) -> None:
    result = run_hints(
        tmp_path, "explain", "--fetch", *separator, "git", "init", "created"
    )
    assert result.returncode == 2
    assert "--fetch cannot be used with explain" in result.stderr
    assert result.stdout == ""
    assert not (tmp_path / "created").exists()


@pytest.mark.parametrize("selection", ["relative", "paths", "environment"])
def test_explain_inspects_target_and_preserves_work(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    selection: str,
) -> None:
    caller, repo = make_temps(tmp_path_factory, 2)
    config_user("review", "review@example.com")
    make_repo(repo, [["git", "init", "-b", "main"]])
    commit_file(repo, "foo.txt", "base", "Base")
    git(repo, "switch", "-c", "topic")
    commit_file(repo, "foo.txt", "topic", "Topic")
    git(repo, "switch", "main")
    write(repo / "foo.txt", "unsaved")()
    if selection == "relative":
        prefix = ["-C", str(repo.parent), "-C", repo.name]
    elif selection == "paths":
        prefix = ["--git-dir", str(repo / ".git"), "--work-tree", str(repo)]
    else:
        monkeypatch.setenv("GIT_DIR", str(repo / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(repo))
        prefix = []
    result = explain(caller, *prefix, "switch", "topic")
    assert result.returncode != 0
    block = blocks(result.stdout)["stash-local-changes"]
    assert "Run the suggested commands from" in block
    assert str(repo).replace("\\", "/") in block.replace("\\", "/")
    assert (repo / "foo.txt").read_text(encoding="utf-8") == "unsaved"
    assert not (caller / ".git").exists()


def test_redirected_counter_uses_target(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    _, local, caller = diverged_clones(tmp_path_factory)
    for index in range(5):
        result = explain(caller, "-C", str(local), "push")
        assert result.returncode != 0
        assert (REBASE_HINT in output_ids(result.stdout)) == (index == 4)
    assert (local / ".git/git-hints/integration-failures.json").exists()
    assert not (caller / ".git/git-hints/integration-failures.json").exists()


@pytest.mark.parametrize(
    "failure", ["other-source", "other-destination", "pull"]
)
def test_counter_excludes_unrelated_integration(
    tmp_path_factory: pytest.TempPathFactory, failure: str
) -> None:
    _, local, other = diverged_clones(tmp_path_factory)
    git(other, "push", "origin", "HEAD:other")
    rejected_pushes(local, 4)
    if failure == "other-source":
        git(local, "branch", "side")
        command = ["push", "origin", "side:main"]
    elif failure == "other-destination":
        command = ["push", "origin", "HEAD:other"]
    else:
        command = ["pull", "--ff-only", "origin", "other"]
    result = explain(local, *command)
    assert result.returncode != 0
    assert REBASE_HINT not in output_ids(result.stdout)
    counts = json.loads(
        (local / ".git/git-hints/integration-failures.json").read_text(
            encoding="utf-8"
        )
    )
    assert counts["refs/heads/main"]["count"] == 4
    assert REBASE_HINT in output_ids(explain(local, "push").stdout)


def test_first_divergent_pull_has_immediate_hint(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    _, local, _ = diverged_clones(tmp_path_factory)
    result = explain(local, "pull", "--ff-only")
    assert result.returncode != 0
    block = blocks(result.stdout)["diverged"]
    assert "Priority: error/blocked" in block
    assert "git pull --no-rebase" in block
    assert "Caution: git pull --rebase" in block
    assert REBASE_HINT not in output_ids(result.stdout)


@pytest.mark.parametrize("option", [["--repo", "mirror"], ["--repo=mirror"]])
def test_named_push_repository_is_resolved(
    tmp_path_factory: pytest.TempPathFactory, option: list[str]
) -> None:
    remote, local, _ = diverged_clones(tmp_path_factory)
    git(local, "remote", "add", "mirror", str(remote))
    result = explain(local, "push", *option)
    assert result.returncode != 0
    assert "git fetch mirror" in result.stdout
    assert not (local / ".git/git-hints/integration-failures.json").exists()


def test_command_configuration_is_used_for_inspection(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    remote, local, _ = diverged_clones(tmp_path_factory)
    git(local, "remote", "add", "mirror", str(remote))
    git(local, "fetch", "mirror")
    rejected_pushes(local, 4)
    result = explain(local, "-c", "branch.main.remote=mirror", "push")
    # A command-scoped upstream change starts its own count too.
    assert REBASE_HINT not in output_ids(result.stdout)
    assert "git fetch mirror" in result.stdout
    assert "branch.main.remote=mirror" in result.stdout
    # Inspection and suggestions must not persist command-scoped settings.
    assert git(local, "config", "branch.main.remote").stdout.strip() == "origin"


def test_push_advice_respects_negative_fetch_refspec(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    _, local, _ = diverged_clones(tmp_path_factory)
    git(local, "config", "--add", "remote.origin.fetch", "^refs/heads/main")
    result = explain(local, "push")
    block = blocks(result.stdout)["push-rejected-non-fast-forward"]
    assert "git merge origin/main" not in block
    assert "Replace the placeholders" in block


def test_unresolvable_target_preserves_git_failure(tmp_path: Path) -> None:
    args = ["-C", str(tmp_path / "missing"), "status"]
    expected = git(tmp_path, *args, check=False)
    result = explain(tmp_path, *args)
    assert result.returncode == expected.returncode
    assert result.stderr == expected.stderr
    assert output_ids(result.stdout) == []


def test_counter_write_failure_is_reported_without_losing_hint(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    _, local, _ = diverged_clones(tmp_path_factory)
    # A file where the state directory should be prevents a counter write.
    (local / ".git/git-hints").write_text("blocked", encoding="utf-8")
    result = explain(local, "push")
    assert result.returncode == 1
    assert "git-hints:" in result.stderr
    assert "Traceback" not in result.stderr
    assert "push-rejected-non-fast-forward" in output_ids(result.stdout)


# Round 2: suggested file operands must not expand as Git pathspec patterns.
@pytest.mark.parametrize("operation", ["add", "remove"])
def test_suggested_paths_are_literal(tmp_path: Path, operation: str) -> None:
    make_repo(tmp_path, [["git", "init", "-b", "main"]])
    names = {"[ab].txt", "a.txt", "b.txt"}
    for name in names:
        write(tmp_path / name, "example")()
    args = ["add"]
    if operation == "remove":
        git(tmp_path, "add", ".")
        args = ["rm", "--cached"]
    command = git_with_paths(args, ["[ab].txt"], separator=True)
    git(tmp_path, *shlex.split(command)[1:])
    staged = set(
        filter(None, git(tmp_path, "ls-files", "-z").stdout.split("\0"))
    )
    expected = {"[ab].txt"} if operation == "add" else {"a.txt", "b.txt"}
    assert staged == expected
    assert all((tmp_path / name).exists() for name in names)


def test_rebase_hint_does_not_assume_local_only_commits(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    _, local, _ = diverged_clones(tmp_path_factory)
    git(local, "switch", "-c", "behind", "origin/main")
    git(local, "fetch", "origin")
    assert git(
        local, "rev-list", "--left-right", "--count", "HEAD...@{upstream}"
    ).stdout.split() == ["0", "1"]
    for index in range(5):
        result = explain(local, "push", "origin", "HEAD:main")
        assert result.returncode != 0
        assert (REBASE_HINT in output_ids(result.stdout)) == (index == 4)
    block = blocks(result.stdout)[REBASE_HINT]
    assert "If your branch contains local-only commits" in block
    assert (
        "Your local repository contains commits that have not been" not in block
    )
    assert "git rebase --abort" not in block
    assert "git status" in block


# Round 4: retain explicit pull destinations and reset changed upstreams.
@pytest.mark.parametrize("configured_upstream", [True, False])
def test_divergent_pull_preserves_explicit_destination(
    tmp_path_factory: pytest.TempPathFactory, configured_upstream: bool
) -> None:
    remote, local, _ = diverged_clones(tmp_path_factory)
    git(local, "remote", "add", "other", str(remote))
    if not configured_upstream:
        git(local, "branch", "--unset-upstream")
    result = explain(local, "pull", "--ff-only", "other", "main")
    assert result.returncode != 0
    block = blocks(result.stdout)["diverged"]
    assert "Command: git pull --no-rebase other main" in block
    assert "Command: Caution: git pull --rebase other main" in block
    assert "<remote>" not in block
    assert "git push" not in block
    assert REBASE_HINT not in output_ids(result.stdout)


@pytest.mark.parametrize("change", ["remote", "branch"])
def test_counter_resets_when_upstream_changes(
    tmp_path_factory: pytest.TempPathFactory, change: str
) -> None:
    remote, local, other = diverged_clones(tmp_path_factory)
    rejected_pushes(local, 4)
    head = git(local, "rev-parse", "HEAD").stdout
    if change == "remote":
        git(local, "remote", "add", "mirror", str(remote))
        git(local, "fetch", "mirror")
        git(local, "branch", "--set-upstream-to=mirror/main")
        push = ["push", "mirror", "HEAD:main"]
    else:
        git(other, "push", "origin", "HEAD:other")
        git(local, "fetch", "origin")
        git(local, "branch", "--set-upstream-to=origin/other")
        push = ["push", "origin", "HEAD:other"]
    for index in range(5):
        result = explain(local, *push)
        assert result.returncode != 0
        assert (REBASE_HINT in output_ids(result.stdout)) == (index == 4)
    assert git(local, "rev-parse", "HEAD").stdout == head


def test_undecodable_conflict_path_gets_manual_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Windows cannot create this POSIX filename, so inject captured Git bytes
    # at the subprocess boundary and exercise the real parser and hint builder.
    import subprocess

    monkeypatch.setattr(
        "git_hints.git.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, b"bad\xff.txt\0", b""
        ),
    )
    repo = Repo(tmp_path)
    monkeypatch.setattr(repo, "prefix", "")
    hint = merge_conflicts(Context(repo, fetched=False))
    assert hint is not None
    block = "\n".join(render_block(hint))
    command_lines = [
        line for line in block.splitlines() if line.startswith("Command:")
    ]
    assert all("\ufffd" not in line for line in command_lines)
    assert any(
        "manually" in line and "git status" in line for line in command_lines
    )


@pytest.mark.parametrize("path", ['"tab\\tname.txt"', "bad\ufffd.txt"])
def test_unreliable_diagnostic_paths_are_not_used_as_operands(
    path: str,
) -> None:
    diagnostic = (
        "error: Your local changes to the following files would be overwritten "
        f"by merge:\n\t{path}\nPlease commit your changes or stash them."
    )
    assert overwritten_paths(diagnostic) == []
