"""Collect a proposed Git patch without unsafe filename splitting."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .trusted_git import UntrustedGitError, trusted_git


class GitDiffError(RuntimeError):
    """Raised when a diff cannot be collected."""


@dataclass(frozen=True)
class GitDiff:
    paths: tuple[str, ...]
    added_lines: int
    deleted_lines: int
    patch: bytes
    #: The endpoint diff's own paths, when `paths` also holds those of intermediate commits
    #: (`collect_commit_diff`); None when `paths` is the endpoint diff alone.
    net_paths: tuple[str, ...] | None = None


#: A file that never exists: pointing GIT_GRAFT_FILE at it disables `.git/info/grafts`.
_NO_GRAFTS = os.devnull + ".no-grafts"


#: The fixed search path every trusted program runs with: system directories only, so nothing a
#: caller put on PATH runs as a helper of the trusted git (or gh).
SAFE_PATH = "/usr/bin:/bin"


def _object_env() -> dict[str, str]:
    """The environment every git run here gets: BUILT from an allowlist, never inherited
    (Codex 4241839908). Inheriting all but `GIT_*` (Codex 4241745395) still handed git the
    dynamic loader's variables -- `LD_PRELOAD`, `LD_LIBRARY_PATH`, `LD_AUDIT`, `DYLD_*` -- which
    load code of the caller's choosing into the trusted git, and a caller PATH for the helpers
    git spawns. No variable of the caller's survives: `GIT_DIR`, `GIT_WORK_TREE`,
    `GIT_COMMON_DIR`, `GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY`,
    `GIT_ALTERNATE_OBJECT_DIRECTORIES`, `GIT_NAMESPACE`, `GIT_CONFIG*`, `GIT_EXEC_PATH`, ... each
    re-points what git reads, so a guard could inspect a benign repository while a later plain
    push publishes the real one. The set below is the whole of git's environment:

    - a fixed system PATH, the C locale, and no HOME (no per-user configuration or attributes);
    - history as the object store holds it: git otherwise substitutes `refs/replace/*` objects
      and grafts when it reads a commit or tree, while a push publishes the ORIGINAL objects
      (Codex 4241532610);
    - no system or global configuration: only the repository's own (and the `-c` overrides
      `_run` passes) applies;
    - never a prompt."""
    return {
        "PATH": SAFE_PATH,
        "LC_ALL": "C",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_GRAFT_FILE": _NO_GRAFTS,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }


@dataclass(frozen=True)
class _Repository:
    """A repository resolved once, from its real path, with a clean environment; every later
    git run is bound to it explicitly (`--git-dir`, `--work-tree`)."""

    git: str
    git_dir: str
    top: str


def _git_program() -> str:
    # Never a bare `git`: a PATH lookup could find one this session planted to fabricate the
    # output a policy check reads (Codex 4241614592). No trusted git: fail closed.
    try:
        return trusted_git()
    except UntrustedGitError as exc:
        raise GitDiffError(str(exc)) from exc


def _exec(arguments: list[str], cwd: str, label: str) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        arguments,
        cwd=cwd,
        env=_object_env(),
        capture_output=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace")[-1000:]
        raise GitDiffError(f"git {label} failed: {message}")
    return result


def _bind(repository: str | Path) -> _Repository:
    """The repository at `repository` -- its top level, which `repository` must BE -- found by
    git's own discovery from that directory with every `GIT_*` variable removed, so nothing in
    the caller's environment can substitute another repository (Codex 4241745395)."""
    program = _git_program()
    where = os.path.realpath(repository)
    if not os.path.isdir(where):
        raise GitDiffError(f"{repository} is not a directory")
    out = _exec(
        [program, "-C", where, "rev-parse", "--absolute-git-dir", "--show-toplevel"],
        where,
        "rev-parse",
    ).stdout.decode("utf-8", errors="surrogateescape")
    lines = out.split("\n")
    if len(lines) != 3 or lines[2] or not lines[0] or not lines[1]:
        raise GitDiffError(f"{repository} is not the top level of a git work tree")
    git_dir, top = lines[0], lines[1]
    if os.path.realpath(top) != where:
        raise GitDiffError(
            f"{repository} is not the top level of its git work tree ({top}); pass the top "
            "level as the repository"
        )
    return _Repository(git=program, git_dir=os.path.realpath(git_dir), top=where)


def _run(
    arguments: list[str], repository: str | Path | _Repository
) -> subprocess.CompletedProcess[bytes]:
    if arguments[:1] != ["git"]:
        raise GitDiffError(f"not a git command: {arguments[:1]!r}")
    repo = repository if isinstance(repository, _Repository) else _bind(repository)
    # `--no-replace-objects` on the command line as well as in the environment: either alone
    # disables replacement; both together survive a caller that rewrites one of them. The
    # repository is named explicitly on every run, never discovered from the environment, and
    # no fsmonitor program from the repository's config runs.
    subcommand = arguments[1:]
    if subcommand[:1] == ["diff"]:
        # The repository's own config is the agent's to write: `diff.ignoreSubmodules=all` (or a
        # `submodule.<name>.ignore`) would hide a gitlink change from the listing while the
        # push publishes it. The command line outranks every config file.
        subcommand = ["diff", "--ignore-submodules=none", *subcommand[1:]]
    command = [
        repo.git,
        "--no-replace-objects",
        f"--git-dir={repo.git_dir}",
        f"--work-tree={repo.top}",
        "-c",
        "core.fsmonitor=false",
        *subcommand,
    ]
    return _exec(command, repo.top, arguments[1] if len(arguments) > 1 else "")


def _decode_path(value: bytes) -> str:
    return value.decode("utf-8", errors="surrogateescape")


#: Every `--name-status` letter a tree, index or working-tree diff can report for a changed path
#: (Codex 4241745397): Added, Copied, Deleted, Modified, Renamed, Type changed (file <->
#: symlink <-> submodule). Unmerged (U), unknown (X) or anything else is an error, never a path
#: that is silently left out of the policy check.
_KNOWN_STATUS = re.compile(rb"[ACDMRT][0-9]*")


def _paths_from_name_status(raw: bytes) -> tuple[str, ...]:
    """Every path the diff touches, BOTH sides of a rename or copy. No `--diff-filter` limits
    the listing: a filter that leaves out one status (it once dropped `T`) lets that change ride
    the patch past the forbidden-path check."""
    fields = raw.split(b"\0")
    paths: set[str] = set()
    index = 0
    while index < len(fields) and fields[index]:
        status = fields[index]
        index += 1
        if not _KNOWN_STATUS.fullmatch(status):
            raise GitDiffError(
                f"git reported a change of unsupported status {_decode_path(status)!r}; "
                "not concluding"
            )
        path_count = 2 if status[:1] in {b"R", b"C"} else 1
        if index + path_count > len(fields) or any(
            not value for value in fields[index : index + path_count]
        ):
            raise GitDiffError("git name-status output is malformed")
        paths.update(_decode_path(value) for value in fields[index : index + path_count])
        index += path_count
    if index != len(fields) - 1 or fields[index]:
        raise GitDiffError("git name-status output is malformed")
    return tuple(sorted(paths))


def collect_git_diff(repository: str | Path, base: str = "HEAD") -> GitDiff:
    root = _bind(repository)
    _run(["git", "add", "-N", "--", "."], root)

    names = _run(
        [
            "git",
            "diff",
            "--name-status",
            "-z",
            "--find-renames",
            base,
            "--",
        ],
        root,
    ).stdout
    paths = _paths_from_name_status(names)

    raw_stats = _run(["git", "diff", "--numstat", "-z", base, "--"], root).stdout
    added = 0
    deleted = 0
    for match in re.finditer(rb"(?:^|\x00)(\d+|-)\t(\d+|-)\t", raw_stats):
        added += int(match.group(1)) if match.group(1).isdigit() else 0
        deleted += int(match.group(2)) if match.group(2).isdigit() else 0

    patch = _run(["git", "diff", "--binary", "--full-index", base, "--"], root).stdout
    return GitDiff(paths=paths, added_lines=added, deleted_lines=deleted, patch=patch)


#: A full object name, as `rev-list` prints it.
_OBJECT_NAME = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _paths_of_every_commit(root: _Repository, merge_base: str, tip: str) -> set[str]:
    """Every path ANY commit in `merge_base..tip` touches (Codex 4241839922). A push publishes
    every one of those commits, not only the endpoint tree: a forbidden or secret file added in
    one commit and deleted in the next is absent from the endpoint diff yet published in the
    history. Each commit is diffed against its single parent, both sides of every rename
    (`--no-renames`: a rename is its deletion plus its addition). A merge commit in the range is
    refused (exit 2) rather than judged: which parent its change is relative to is ambiguous,
    and the routine never needs one -- its branch is cut from the base and committed onto."""
    listing = _run(["git", "rev-list", "--parents", f"{merge_base}..{tip}", "--"], root)
    paths: set[str] = set()
    for line in listing.stdout.decode("ascii", errors="replace").splitlines():
        names = line.split(" ")
        if not all(_OBJECT_NAME.fullmatch(name) for name in names):
            raise GitDiffError("git rev-list output is malformed")
        if len(names) != 2:
            kind = "a merge commit" if len(names) > 2 else "a commit without a parent"
            raise GitDiffError(
                f"{names[0][:12]} is {kind}; check-diff judges a linear series of commits on "
                "the base only -- not concluding"
            )
        commit, parent = names
        listed = _run(
            [
                "git",
                "diff-tree",
                "-r",
                "-z",
                "--no-commit-id",
                "--name-status",
                "--no-renames",
                "--no-ext-diff",
                "--no-textconv",
                "--ignore-submodules=none",
                parent,
                commit,
                "--",
            ],
            root,
        ).stdout
        paths.update(_paths_from_name_status(listed))
    return paths


def collect_commit_diff(repository: str | Path, base: str, head: str = "HEAD") -> GitDiff:
    """The COMMITTED change `head` brings onto `base`: the diff from their merge base to `head`,
    tree to tree -- what a pull request from `head` into `base` shows, and what a push of `head`
    publishes (Codex 4241434509). `paths` is that diff's paths PLUS every path any single commit
    of the series touches (Codex 4241839922: a push publishes the history, not only the end
    state); the line and byte counts and the patch are the endpoint diff's. The working tree and
    index are never read, so an uncommitted edit can neither hide a committed change nor add one
    that is not pushed. No external diff or textconv program runs."""
    for rev in (base, head):
        if not rev or rev.startswith("-"):
            raise GitDiffError(f"not a revision: {rev!r}")
    root = _bind(repository)
    merge_base = _run(["git", "merge-base", base, head], root).stdout.decode().strip()
    tip = _run(["git", "rev-parse", "--verify", f"{head}^{{commit}}"], root).stdout.decode()
    if not merge_base or not tip.strip():
        raise GitDiffError(f"no merge base between {base} and {head}")
    span = [merge_base, tip.strip()]
    plain = ["--no-ext-diff", "--no-textconv"]
    names = _run(
        [
            "git",
            "diff",
            *plain,
            "--name-status",
            "-z",
            "--find-renames",
            *span,
            "--",
        ],
        root,
    ).stdout
    net_paths = _paths_from_name_status(names)
    paths = tuple(sorted(set(net_paths) | _paths_of_every_commit(root, *span)))
    raw_stats = _run(["git", "diff", *plain, "--numstat", "-z", *span, "--"], root).stdout
    added = 0
    deleted = 0
    for match in re.finditer(rb"(?:^|\x00)(\d+|-)\t(\d+|-)\t", raw_stats):
        added += int(match.group(1)) if match.group(1).isdigit() else 0
        deleted += int(match.group(2)) if match.group(2).isdigit() else 0
    patch = _run(["git", "diff", *plain, "--binary", "--full-index", *span, "--"], root).stdout
    return GitDiff(
        paths=paths, added_lines=added, deleted_lines=deleted, patch=patch, net_paths=net_paths
    )


def committed_path(repository: str | Path, path: str | Path) -> str:
    """`path` as a path inside the repository at `repository` (POSIX, relative to its top
    level), for reading it from a commit. A relative `path` is taken relative to the top level;
    an absolute one must lie inside it."""
    top = _bind(repository).top
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            candidate = Path(os.path.realpath(candidate)).relative_to(os.path.realpath(top))
        except ValueError as exc:
            raise GitDiffError(f"{path} is not inside the repository {top}") from exc
    relative = PurePosixPath(*candidate.parts)
    if not relative.parts or any(part in ("", ".", "..") for part in relative.parts):
        raise GitDiffError(f"not a path inside the repository: {str(path)!r}")
    return str(relative)


def resolve_commit(repository: str | Path, rev: str) -> str:
    """The full object name of the commit `rev` names in the repository at `repository`."""
    if not rev or rev.startswith("-"):
        raise GitDiffError(f"not a revision: {rev!r}")
    root = _bind(repository)
    commit = _run(["git", "rev-parse", "--verify", f"{rev}^{{commit}}"], root).stdout.decode()
    commit = commit.strip()
    if not _OBJECT_NAME.fullmatch(commit):
        raise GitDiffError(f"{rev} does not name a commit")
    return commit


def read_committed_file(
    repository: str | Path, rev: str, path: str, label: str | None = None
) -> bytes:
    """The bytes of `path` (relative to the top level) as commit `rev` holds them -- never the
    working tree or the index, which the agent being checked can change without committing
    (Codex 4241614590). Replace refs and grafts are ignored as for every read here, and no
    filter, textconv or external program runs (`cat-file blob`). A missing file is an error."""
    if not rev or rev.startswith("-"):
        raise GitDiffError(f"not a revision: {rev!r}")
    root = _bind(repository)
    commit = _run(["git", "rev-parse", "--verify", f"{rev}^{{commit}}"], root).stdout.decode()
    commit = commit.strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise GitDiffError(f"{rev} does not name a commit")
    try:
        return _run(["git", "cat-file", "blob", f"{commit}:{path}"], root).stdout
    except GitDiffError as exc:
        shown = label or rev
        raise GitDiffError(f"{path} could not be read from {shown} ({commit[:12]}): {exc}") from exc
