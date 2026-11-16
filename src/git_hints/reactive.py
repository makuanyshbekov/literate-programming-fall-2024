# `reactive.py` -- Explain a failed Git command
# =============================================
#
# `git-hints explain -- git <command>` runs the command once, with standard
# input and (normally) standard output connected to the terminal, so prompts,
# editors, and Git's own output behave as usual. Standard error is captured,
# shown after Git exits, and matched against the English diagnostics that
# reactive hints recognize; an unrecognized diagnostic produces no hint rather
# than a guess.
#
# Most reactive hints also inspect the repository after the command runs. When
# global options select a repository, inspections reuse that ordered prefix.
# Unknown global options disable state-dependent checks rather than falling
# back to the caller's repository.
#
# Imports
# -------
#
# ### Standard library
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
from pathlib import Path

# ### Local
from .git import GitLaunchError, InspectionError, Repo, decode
from .hint import (
    Command,
    Hint,
    Priority,
    Safety,
    code,
    git_with_paths,
    render_block,
    shell_quote,
    sort_hints,
    summarize,
)
from .proactive import Context, divergence_advice, merge_conflicts

# Parsing the Git command
# -----------------------
#
# Git's global options which take a value as the following argument (rather
# than after `=`).
GLOBAL_OPTIONS_WITH_VALUE = {
    "-C",
    "-c",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--super-prefix",
    "--config-env",
    "--attr-source",
}
GLOBAL_FLAGS = {
    "--bare",
    "--no-pager",
    "--paginate",
    "-p",
    "-P",
    "--no-replace-objects",
    "--no-optional-locks",
    "--literal-pathspecs",
    "--glob-pathspecs",
    "--noglob-pathspecs",
    "--icase-pathspecs",
    "--no-lazy-fetch",
}


@dataclass(frozen=True)
class GitCommand:
    # The complete argument list, starting with `git`.
    argv: Sequence[str]
    # The subcommand, such as `push`, or `None` when there is none.
    subcommand: str | None
    # The arguments following the subcommand.
    arguments: Sequence[str]
    # Ordered global options, or None when their inspection effect is unknown.
    prefix: tuple[str, ...] | None

    # The command as the user could type it again.
    @property
    def text(self) -> str:
        return " ".join(shell_quote(word) for word in self.argv)


def parse_command(argv: Sequence[str]) -> GitCommand:
    index = 1
    while index < len(argv) and argv[index].startswith("-"):
        option = argv[index]
        if option in GLOBAL_OPTIONS_WITH_VALUE:
            if index + 1 >= len(argv):
                return GitCommand(argv, None, [], None)
            index += 2
            continue
        if not (
            option in GLOBAL_FLAGS
            or (
                "=" in option
                and option.split("=", 1)[0] in GLOBAL_OPTIONS_WITH_VALUE
            )
            or (option.startswith(("-C", "-c")) and len(option) > 2)
        ):
            return GitCommand(argv, None, [], None)
        index += 1
    prefix = tuple(argv[1:index])
    if index >= len(argv):
        return GitCommand(argv, None, [], prefix)
    return GitCommand(argv, argv[index], argv[index + 1 :], prefix)


# Return a subcommand's positional arguments.
def positional_arguments(
    arguments: Sequence[str],
    # Options which take a value as the following argument.
    options_with_value: set[str],
) -> list[str]:
    positionals: list[str] = []
    words = iter(arguments)
    for word in words:
        if word == "--":
            positionals += words
            break
        if word.startswith("-") and word != "-":
            if word in options_with_value:
                next(words, None)
            continue
        positionals.append(word)
    return positionals


# True when a `git pull` replays local commits instead of merging; the last of
# conflicting options wins, as in Git.
def pull_rebases(arguments: Sequence[str]) -> bool:
    rebase = False
    for word in arguments:
        if word == "--":
            break
        if word in ("-r", "--rebase"):
            rebase = True
        elif word.startswith("--rebase="):
            rebase = word.split("=", 1)[1] not in ("false", "no", "off", "0")
        elif word == "--no-rebase":
            rebase = False
    return rebase


# Diagnostics
# -----------
#
# Match fragments rather than complete lines, which vary between Git versions.
#
# Tracked changes blocking a merge or checkout. Git lists the affected paths on
# the following lines, each indented by a tab. An untracked-file obstruction
# says "untracked working tree files" instead, so it doesn't match.
OVERWRITTEN = re.compile(
    r"local changes.*would be overwritten by", re.IGNORECASE
)

# One rejected ref per line, such as ` ! [rejected]  main -> main (fetch
# first)`. A hook rejection reads `[remote rejected]`, so it doesn't match.
REJECTED = re.compile(
    r"^\s*!\s+\[rejected\]\s+(?P<source>\S+)\s+->\s+(?P<destination>\S+)\s+"
    r"\((?P<reason>fetch first|non-fast-forward)\)",
    re.MULTILINE,
)

# `git pull` refusing to integrate diverged branches.
PULL_DIVERGED = (
    "Need to specify how to reconcile divergent branches",
    "Not possible to fast-forward",
)


# Return the paths whose tracked changes blocked the command, an empty list
# when the diagnostic names none reliably, or `None` when the diagnostic
# doesn't report tracked changes being overwritten.
def overwritten_paths(stderr: str) -> list[str] | None:
    lines = stderr.splitlines()
    for index, line in enumerate(lines):
        if OVERWRITTEN.search(line):
            paths = []
            for following in lines[index + 1 :]:
                if not following.startswith("\t"):
                    break
                path = following[1:]
                # Diagnostic filenames may be C-quoted or decoded with
                # replacement. Neither is a reliable concrete shell operand.
                if path.startswith('"') or "\ufffd" in path:
                    return []
                paths.append(path)
            return paths
    return None


# Reactive hints
# --------------
@dataclass(frozen=True)
class Failure:
    command: GitCommand
    returncode: int
    stderr: str
    # The repository the command ran in, or `None` when inspecting it doesn't
    # apply: the global options are unsupported or it ran outside a work tree.
    repo: Repo | None

    def display_path(self, path: str) -> str:
        return self.repo.display_path(path) if self.repo else path


def reactive_merge_conflicts(failure: Failure) -> Hint | None:
    if failure.repo is None:
        return None
    return merge_conflicts(Context(failure.repo, fetched=False))


def pull_blocked_local_changes(failure: Failure) -> Hint | None:
    if failure.command.subcommand != "pull" or failure.repo is None:
        return None
    paths = overwritten_paths(failure.stderr)
    if paths is None:
        return None
    # Git refuses to pull at all while conflicts remain; `merge-conflicts`
    # covers that.
    if failure.repo.unmerged_paths:
        return None
    paths = [failure.display_path(path) for path in paths]
    text = (
        "The attempted [git pull]"
        "(https://git-scm.com/docs/git-pull#_description "
        '"Fetch and integrate changes into the current branch") was blocked '
        "because incoming changes would overwrite uncommitted tracked "
        "changes. Preserve your local work before retrying."
    )
    if paths:
        text += f" Affected files: {summarize(paths)}."
    text += (
        " Default stashing excludes untracked files. See "
        "[git stash](https://git-scm.com/docs/git-stash#_description "
        '"Temporarily save working-directory and index changes").'
    )
    add = (
        git_with_paths(["add"], paths[:5], separator=True)
        if paths
        else "git add -- <files>"
    )
    return Hint(
        id="pull-blocked-local-changes",
        priority=Priority.ERROR_BLOCKED,
        text=text,
        stage=(
            "Your working directory or staging area contains uncommitted "
            "tracked changes that incoming commits would overwrite."
        ),
        commands=(
            Command(
                "`git status`, `git diff`, and `git diff --cached`",
                "Git shows the uncommitted changes and the files they affect.",
            ),
            Command(
                f"To commit: {code(add)}, then "
                '`git commit -m "Save local changes"`',
                "The selected changes are saved in a local commit.",
            ),
            Command(
                'To stash: `git stash push -m "Save work before pull"`, then '
                "`git stash list`",
                "Tracked changes are saved and removed from the working "
                "directory and index; the list shows the saved entry.",
            ),
            Command(
                "After preserving the changes, retry: "
                + code(failure.command.text),
                "The pull proceeds. If you stashed, run `git stash apply` "
                "afterward, resolve any conflicts, and keep the stash until "
                "the restoration is verified.",
            ),
        ),
    )


# Push options which take a value as the following argument.
PUSH_OPTIONS_WITH_VALUE = {
    "-o",
    "--push-option",
    "--receive-pack",
    "--exec",
    "--repo",
}


# Return the remote a push went to: its explicit repository argument, or the
# configured push destination.
def push_remote(repo: Repo, command: GitCommand, branch: str | None) -> str:
    positionals = positional_arguments(
        command.arguments, PUSH_OPTIONS_WITH_VALUE
    )
    if positionals:
        return positionals[0]
    for index, argument in enumerate(command.arguments):
        if argument == "--repo" and index + 1 < len(command.arguments):
            return command.arguments[index + 1]
        if argument.startswith("--repo="):
            return argument.removeprefix("--repo=")
    keys = ["remote.pushDefault"]
    if branch is not None:
        keys = [f"branch.{branch}.pushRemote", *keys, f"branch.{branch}.remote"]
    for key in keys:
        value = repo.config(key)
        if value:
            return value
    return "origin"


# Map a branch on a remote to its remote-tracking branch using the remote's
# fetch refspecs, such as `+refs/heads/*:refs/remotes/origin/*`. Return `None`
# when no refspec maps it.
def tracking_branch(repo: Repo, remote: str, destination: str) -> str | None:
    if remote not in repo.remotes:
        return None
    ref = (
        destination
        if destination.startswith("refs/")
        else f"refs/heads/{destination}"
    )
    refspecs = repo.config_all(f"remote.{remote}.fetch")
    if any(
        spec.startswith("^") and fnmatchcase(ref, spec[1:]) for spec in refspecs
    ):
        return None
    for refspec in refspecs:
        refspec = refspec.removeprefix("+")
        if refspec.startswith("^") or ":" not in refspec:
            continue
        source, target = refspec.split(":", 1)
        if "*" in source:
            prefix, suffix = source.split("*", 1)
            if (
                ref.startswith(prefix)
                and ref.endswith(suffix)
                and len(ref) >= len(prefix) + len(suffix)
            ):
                match = ref[len(prefix) : len(ref) - len(suffix)]
                return target.replace("*", match, 1).removeprefix(
                    "refs/remotes/"
                )
        elif source == ref:
            return target.removeprefix("refs/remotes/")
    return None


def current_rejection(repo: Repo, stderr: str) -> re.Match[str] | None:
    sources = {"HEAD"}
    branch = repo.current_branch
    if branch is not None:
        sources |= {branch, f"refs/heads/{branch}"}
    return next(
        (r for r in REJECTED.finditer(stderr) if r["source"] in sources), None
    )


def push_rejected_non_fast_forward(failure: Failure) -> Hint | None:
    if failure.command.subcommand != "push":
        return None
    rejections = list(REJECTED.finditer(failure.stderr))
    if not rejections:
        return None
    repo = failure.repo
    branch = repo.current_branch if repo else None
    refs = [f"{r['source']} -> {r['destination']}" for r in rejections]
    text = (
        "Git rejected the "
        "[push](https://git-scm.com/docs/git-push#_note_about_fast_forwards "
        '"A non-fast-forward push would replace remote history") because the '
        "remote branch contains history missing from the branch being pushed. "
        f"Integrate that history before retrying. Rejected: {summarize(refs)}."
    )
    status = Command(
        "`git status`",
        "Git shows the current branch and the state of the working directory "
        "and staging area.",
    )
    # Find the rejection of the current branch, which the commands below can
    # integrate.
    current = current_rejection(repo, failure.stderr) if repo else None
    if current is None or repo is None:
        if repo is None:
            text += " Run the commands below in the repository you pushed from."
        else:
            rejected = rejections[0]["source"]
            text += (
                f" The rejected branch {code(rejected)} is not your current "
                f"branch{f' ({code(branch)})' if branch else ''}; switch to it "
                "before integrating its remote history."
            )
        commands = [status]
    elif repo.unmerged_paths or repo.operations.combining:
        text += (
            " Finish or cancel the active operation before starting another "
            "integration; follow git status for operation-specific guidance."
        )
        commands = [status]
    else:
        remote = push_remote(repo, failure.command, branch)
        tracking = tracking_branch(repo, remote, current["destination"])
        if tracking is None:
            text += (
                " Replace the placeholders below with the remote and branch "
                "you pushed to."
            )
        fetch = (
            f"git fetch {shell_quote(remote)}"
            if tracking
            else "git fetch <remote>"
        )
        merge = (
            f"git merge {shell_quote(tracking)}"
            if tracking
            else "git merge <remote>/<branch>"
        )
        commands = [
            status,
            Command(
                code(fetch),
                "Git downloads the remote's new commits and updates the "
                "remote-tracking branch.",
            ),
            Command(
                code(merge)
                + ", resolving any conflicts, then retry "
                + code(failure.command.text),
                "The local branch incorporates the rejected destination's "
                "history, and the push can succeed if no other errors remain.",
            ),
        ]
    return Hint(
        id="push-rejected-non-fast-forward",
        priority=Priority.ERROR_BLOCKED,
        text=text,
        stage=(
            "The local and remote repositories have different branch "
            "histories that must be integrated before a normal push can "
            "succeed."
        ),
        commands=commands,
    )


def stash_local_changes(failure: Failure) -> Hint | None:
    repo = failure.repo
    if failure.command.subcommand not in ("merge", "switch") or repo is None:
        return None
    paths = overwritten_paths(failure.stderr)
    if paths is None or repo.unmerged_paths:
        return None
    paths = [repo.display_path(path) for path in paths]
    affected = f" Affected files: {summarize(paths)}." if paths else ""
    stash_link = (
        "[stash](https://git-scm.com/docs/git-stash#_description "
        '"Save working-directory and index changes temporarily")'
    )
    status = Command("`git status`", "Git shows the changes.")
    # Git can't stash on a branch with no commits.
    if repo.head_commit is None:
        text = (
            "Git blocked the operation because it would overwrite tracked "
            f"local changes.{affected} This branch has no commits yet, so a "
            f"{stash_link} cannot save them; back up the files first."
        )
        commands = [
            status,
            Command(
                "Copy the affected files to a folder outside this repository, "
                "then verify the copies",
                "The work has a separate backup.",
            ),
        ]
    else:
        text = (
            "Git blocked the operation because it would overwrite tracked "
            f"local changes.{affected} A {stash_link} can preserve those "
            "changes while you retry the operation. Default stashing excludes "
            "untracked files."
        )
        commands = [
            status,
            Command(
                '`git stash push -m "Save work before retrying"`, then '
                "`git stash list`",
                "Git saves the tracked changes and removes them from the "
                "working directory and index; the list shows the saved entry.",
            ),
            Command(
                f"Retry: {code(failure.command.text)}",
                "The blocked operation proceeds.",
            ),
            Command(
                "`git stash apply <stash>`, using the entry from "
                "`git stash list`",
                "Git restores the saved changes. Reapplying may cause "
                "conflicts; resolve them and keep the stash until the "
                "restoration is verified.",
            ),
        ]
    return Hint(
        id="stash-local-changes",
        priority=Priority.ERROR_BLOCKED,
        text=text,
        stage=(
            "The working tree or index contains tracked changes, and a "
            "`git merge` or `git switch` was blocked before it changed the "
            "repository state."
        ),
        commands=commands,
    )


PULL_OPTIONS_WITH_VALUE = {
    "-s",
    "--strategy",
    "-X",
    "--strategy-option",
    "--depth",
    "--shallow-since",
    "--shallow-exclude",
    "--upload-pack",
    "--jobs",
    "-j",
    "--server-option",
    "--negotiation-tip",
    "--filter",
}


def integrates_upstream(repo: Repo, command: GitCommand, stderr: str) -> bool:
    branch = repo.current_branch
    if branch is None:
        return False
    remote = repo.config(f"branch.{branch}.remote")
    merges = repo.config_all(f"branch.{branch}.merge")
    if not remote or len(merges) != 1:
        return False
    if command.subcommand == "push":
        rejection = current_rejection(repo, stderr)
        if rejection is None or push_remote(repo, command, branch) != remote:
            return False
        destination = rejection["destination"]
        if not destination.startswith("refs/"):
            destination = f"refs/heads/{destination}"
        return destination == merges[0]
    if command.subcommand == "pull":
        positionals = positional_arguments(
            command.arguments, PULL_OPTIONS_WITH_VALUE
        )
        if not positionals:
            return True
        if positionals[0] != remote or len(positionals) > 2:
            return False
        if len(positionals) == 1:
            return True
        ref = positionals[1]
        if not ref.startswith("refs/"):
            ref = f"refs/heads/{ref}"
        return ref == merges[0]
    return False


def pull_diverged(failure: Failure) -> Hint | None:
    if failure.command.subcommand != "pull" or not any(
        fragment in failure.stderr for fragment in PULL_DIVERGED
    ):
        return None
    if failure.repo and (
        failure.repo.unmerged_paths or failure.repo.operations.combining
    ):
        return None
    hint = divergence_advice()
    positionals = positional_arguments(
        failure.command.arguments, PULL_OPTIONS_WITH_VALUE
    )
    uses_upstream = failure.repo is not None and integrates_upstream(
        failure.repo, failure.command, failure.stderr
    )
    if not positionals and uses_upstream:
        return replace(hint, priority=Priority.ERROR_BLOCKED)
    # Preserve simple explicit destinations. Complex fetch refspecs still
    # need confirmation rather than being copied into integration advice.
    known_destination = (
        len(positionals) == 2
        and not positionals[1].startswith(("+", "^"))
        and not any(char in positionals[1] for char in ":*?[]")
    ) or (len(positionals) == 1 and uses_upstream)
    destination = (
        " ".join(shell_quote(word) for word in positionals)
        if known_destination
        else "<remote> <branch>"
    )
    return replace(
        hint,
        priority=Priority.ERROR_BLOCKED,
        text=(
            "The attempted [git pull]"
            "(https://git-scm.com/docs/git-pull#_description "
            '"Fetch and integrate changes into the current branch") found '
            "divergent histories. Choose whether to merge or replay local "
            "commits on the selected history."
            + (
                " Confirm the pull remote and branch before retrying."
                if not known_destination
                else ""
            )
        ),
        commands=(
            Command(
                code(f"git pull --no-rebase {destination}"),
                "Git integrates the selected history; resolve any conflicts.",
            ),
            Command(
                code(f"git pull --rebase {destination}"),
                "Git replays local commits on the selected history.",
                Safety.CAUTION,
            ),
        ),
    )


REACTIVE_CHECKS: list[Callable[[Failure], Hint | None]] = [
    reactive_merge_conflicts,
    pull_diverged,
    pull_blocked_local_changes,
    push_rejected_non_fast_forward,
    stash_local_changes,
]


# Repeated integration failures
# -----------------------------
#
# After five qualifying failures to integrate with the upstream branch, suggest
# `git pull --rebase`. Counts are kept per local branch, keyed by its full ref,
# in a JSON file under the Git directory (so that linked worktrees each have
# their own), along with HEAD and the configured upstream. Changing either
# starts a new count; old records without upstream metadata start over too.
FAILURE_COUNTS_PATH = "git-hints/integration-failures.json"
FAILURE_THRESHOLD = 5


def rebase_after_repeated_integration_failures() -> Hint:
    return Hint(
        id="rebase-after-repeated-integration-failures",
        priority=Priority.ERROR_BLOCKED,
        text=(
            "There have been five qualifying failures to integrate with the "
            "upstream branch. If your branch contains local-only commits, "
            "replaying them using "
            "[git pull --rebase]"
            "(https://git-scm.com/docs/git-pull#_description "
            '"Fetch upstream changes and replay local commits on top of '
            'them") may resolve the problem. Rebasing changes local commit '
            "IDs and may cause conflicts. Make sure your local work is "
            "committed or otherwise backed up before attempting it. See the "
            "[git rebase documentation]"
            "(https://git-scm.com/docs/git-rebase#_description "
            '"Replay commits on another base and resolve rebase conflicts").'
        ),
        stage=(
            "Your current branch in the local repository could not integrate "
            "with its upstream; compare their histories before choosing an "
            "integration method."
        ),
        commands=(
            Command(
                "`git pull --rebase`",
                "The local branch contains the upstream commits followed by "
                "any replayed local commits and can be pushed if no other "
                "errors remain. If Git stops on conflicts, run `git status` "
                "and follow the operation-specific continuation or "
                "cancellation guidance.",
                Safety.CAUTION,
            ),
        ),
    )


# True for a push rejected as non-fast-forward, or a merging pull refused
# because the branches diverged.
def is_qualifying_failure(command: GitCommand, stderr: str) -> bool:
    if command.subcommand == "push":
        return "(fetch first)" in stderr or "(non-fast-forward)" in stderr
    if command.subcommand == "pull":
        return not pull_rebases(command.arguments) and any(
            fragment in stderr for fragment in PULL_DIVERGED
        )
    return False


# Read the stored counts; a missing or unreadable file means no failures.
def load_counts(path: Path) -> dict[str, dict[str, object]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        ref: record
        for ref, record in data.items()
        if isinstance(record, dict)
        and isinstance(record.get("count"), int)
        and isinstance(record.get("head"), str)
    }


# Write the counts atomically, so an interrupted write can't leave a partial
# file.
def save_counts(path: Path, counts: dict[str, dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
    ) as temporary:
        json.dump(counts, temporary, indent=2)
    os.replace(temporary.name, path)


# Record the outcome of a push or pull for the current branch. Return true
# when this failure reaches the threshold, which displays the rebase hint and
# resets the count.
def record_integration_outcome(
    repo: Repo, command: GitCommand, returncode: int, stderr: str
) -> bool:
    if command.subcommand not in ("push", "pull"):
        return False
    # Detached HEAD has no branch to count for.
    symbolic = repo.check("symbolic-ref", "--quiet", "HEAD", ok=(0, 1))
    head = repo.head_commit
    if symbolic.returncode != 0 or head is None:
        return False
    branch_ref = symbolic.stdout.strip()
    path = repo.git_paths(FAILURE_COUNTS_PATH)[0]
    counts = load_counts(path)
    record = counts.get(branch_ref)
    branch = branch_ref.removeprefix("refs/heads/")
    upstream_identity = {
        "remote": repo.config(f"branch.{branch}.remote"),
        "merge": repo.config_all(f"branch.{branch}.merge"),
    }
    count = (
        record["count"]
        if record
        and record["head"] == head
        and record.get("upstream") == upstream_identity
        else 0
    )
    assert isinstance(count, int)
    show = False
    if returncode == 0:
        if record is None:
            return False
        count = 0
    else:
        if not is_qualifying_failure(command, stderr):
            return False
        upstream = repo.check(
            "rev-parse",
            "--abbrev-ref",
            "--symbolic-full-name",
            "@{upstream}",
            ok=(0, 128),
        )
        # Rebasing isn't the answer while another operation is in progress,
        # and isn't possible without an upstream.
        if (
            upstream.returncode != 0
            or repo.unmerged_paths
            or repo.operations.combining
            or not integrates_upstream(repo, command, stderr)
        ):
            return False
        count += 1
        show = count >= FAILURE_THRESHOLD
        if show:
            count = 0
    counts[branch_ref] = {
        "count": count,
        "head": head,
        "upstream": upstream_identity,
    }
    save_counts(path, counts)
    return show


# Running the command
# -------------------
#
# Explain where commands without a global prefix should be run. Keep the
# original launch directory for relative -C, --git-dir, and --work-tree values.
def suggestion_context(repo: Repo, command: GitCommand) -> str:
    target = repo.check("rev-parse", "--show-toplevel").stdout.strip()
    directory = str(repo.cwd or Path.cwd())
    note = (
        f" These suggestions target {code(target)}. "
        f"Run the suggested commands from {code(directory)}, retaining the "
        "original repository-selection environment."
    )
    matching_flags = {
        "--literal-pathspecs",
        "--glob-pathspecs",
        "--noglob-pathspecs",
        "--icase-pathspecs",
    }
    suggested_options = []
    options = iter(command.prefix or ())
    for option in options:
        if option not in matching_flags:
            suggested_options.append(option)
        if option in GLOBAL_OPTIONS_WITH_VALUE:
            # Preserve values even when their text resembles a matching flag.
            suggested_options.append(next(options))
    if suggested_options:
        prefix = " ".join(shell_quote(w) for w in ("git", *suggested_options))
        note += (
            " When a suggested command omits repository/configuration options, "
            f"insert those from {code(prefix)} immediately after `git`, "
            "before any displayed options."
        )
    if tuple(suggested_options) != command.prefix and command.prefix:
        note += (
            " Original filename-matching options are omitted from suggestions."
        )
    return note


# Copy Git's captured standard error to ours unchanged.
def emit_stderr(data: bytes) -> None:
    sys.stderr.flush()
    buffer = getattr(sys.stderr, "buffer", None)
    if buffer is None:
        sys.stderr.write(decode(data))
    else:
        buffer.write(data)
        buffer.flush()


# Run a Git command, then explain its failure. Return Git's exit code, or 1
# when Git can't be launched or inspection fails unexpectedly.
def explain(
    # The command, starting with `git`.
    argv: Sequence[str],
    # Print only reactive hint IDs, and discard Git's standard output.
    ids_only: bool,
) -> int:
    command = parse_command(argv)
    try:
        completed = subprocess.run(
            list(argv),
            stdout=subprocess.DEVNULL if ids_only else None,
            stderr=subprocess.PIPE,
            shell=False,
            check=False,
        )
    except OSError as error:
        print(f"git-hints: Could not launch Git: {error}", file=sys.stderr)
        return 1
    emit_stderr(completed.stderr)
    stderr = decode(completed.stderr)
    returncode = completed.returncode
    # subprocess reports POSIX signal N as -N; use Bash's shell status 128+N.
    if returncode < 0:
        returncode = 128 - returncode

    errors: list[Exception] = []
    repo: Repo | None = None
    show_rebase = False
    try:
        candidate = Repo(prefix=command.prefix or ())
        if command.prefix is not None:
            try:
                location = candidate.work_tree
            except InspectionError:
                # A failed command may name a missing or unusable target.
                # Keep its diagnostic and exit code; do not inspect elsewhere.
                if returncode == 0 or not (
                    command.prefix
                    or os.environ.get("GIT_DIR")
                    or os.environ.get("GIT_WORK_TREE")
                ):
                    raise
                location = "not-a-repo"
            if location == "inside":
                repo = candidate
            elif location == "outside":
                # --git-dir/--work-tree can operate on a work tree while
                # the launch directory is outside it. Resolve with Git first,
                # then make inspections relative to that work tree.
                root = candidate.run("rev-parse", "--show-toplevel")
                if root.returncode == 0 and root.stdout.strip():
                    git_dir = candidate.check(
                        "rev-parse", "--absolute-git-dir"
                    ).stdout.strip()
                    repo = Repo(
                        prefix=(
                            *candidate.command_prefix,
                            "--git-dir",
                            git_dir,
                            "--work-tree",
                            root.stdout.strip(),
                            "-C",
                            root.stdout.strip(),
                        )
                    )
        if repo is not None:
            show_rebase = record_integration_outcome(
                repo, command, returncode, stderr
            )
    except (GitLaunchError, InspectionError, OSError) as error:
        errors.append(error)

    lines = [] if ids_only else [f"Git exited with code {returncode}."]
    if returncode == 0:
        if not ids_only:
            lines.append("No error detected.")
    else:
        failure = Failure(command, returncode, stderr, repo)
        hints = []
        for check in REACTIVE_CHECKS:
            try:
                hint = check(failure)
            except (GitLaunchError, InspectionError) as error:
                errors.append(error)
                continue
            if hint is not None:
                hints.append(hint)
        if show_rebase:
            hints.append(rebase_after_repeated_integration_failures())
        hints = sort_hints(hints)
        if repo and hints and not ids_only:
            try:
                note = suggestion_context(repo, command)
                hints = [replace(hint, text=hint.text + note) for hint in hints]
            except (GitLaunchError, InspectionError) as error:
                errors.append(error)
                hints = []  # Do not print advice with an unknown location.
        if ids_only:
            lines += [hint.id for hint in hints]
        elif hints:
            lines += [line for hint in hints for line in render_block(hint)]
        else:
            lines.append("No hints.")
    if lines:
        print("\n".join(lines))
    for error in errors:
        print(f"git-hints: {error}", file=sys.stderr)
    return 1 if errors else returncode
