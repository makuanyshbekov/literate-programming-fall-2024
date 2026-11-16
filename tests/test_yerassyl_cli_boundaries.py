# Regression tests contributed for the final Project 2 submission.
# Focus: CLI argument boundaries and preventing unintended Git execution.
from pathlib import Path

import pytest
from git_test_tools import run_hints


@pytest.mark.parametrize("separator", [[], ["--"]])
def test_git_options_after_git_are_forwarded(
    tmp_path: Path, separator: list[str]
) -> None:
    """Git's --version belongs to Git, with or without the separator."""
    result = run_hints(tmp_path, "explain", *separator, "git", "--version")
    assert result.returncode == 0
    assert result.stdout.startswith("git version ")
    assert "Git exited with code 0." in result.stdout
    assert "No error detected." in result.stdout


@pytest.mark.parametrize("separator", [[], ["--"]])
def test_wrapper_fetch_cannot_create_repository(
    tmp_path: Path, separator: list[str]
) -> None:
    """Invalid wrapper --fetch must fail before running git init."""
    result = run_hints(
        tmp_path, "explain", "--fetch", *separator, "git", "init", "created"
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "--fetch cannot be used with explain" in result.stderr
    assert not (tmp_path / "created").exists()


def test_git_fetch_flag_after_command_is_not_wrapper_flag(tmp_path: Path) -> None:
    """A --fetch token after 'git' must reach Git, not wrapper validation."""
    result = run_hints(tmp_path, "explain", "--", "git", "--fetch")
    assert result.returncode == 129
    assert "--fetch cannot be used with explain" not in result.stderr
    assert "unknown option: --fetch" in result.stderr
    assert "Git exited with code 129." in result.stdout
