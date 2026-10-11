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


#: A file that never exists: pointing GIT_GRAFT_FILE at it disables `.git/info/grafts`.
_NO_GRAFTS = os.devnull + ".no-grafts"


def _object_env() -> dict[str, str]:
    """The environment every diff read runs in: history as the object store holds it. Git
    otherwise substitutes `refs/replace/*` objects and grafts when it reads a commit or tree,
    while a push publishes the ORIGINAL objects -- so a replace ref could make a guard inspect a
    benign tree and pass a commit it never saw (Codex 4241532610)."""
    env = dict(os.environ)
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["GIT_GRAFT_FILE"] = _NO_GRAFTS
    env.pop("GIT_REPLACE_REF_BASE", None)
    return env


def _run(arguments: list[str], repository: Path) -> subprocess.CompletedProcess[bytes]:
    if arguments[:1] != ["git"]:
        raise GitDiffError(f"not a git command: {arguments[:1]!r}")
    # Never a bare `git`: a PATH lookup could find one this session planted to fabricate the
    # output a policy check reads (Codex 4241614592). No trusted git: fail closed.
    try:
        program = trusted_git()
    except UntrustedGitError as exc:
        raise GitDiffError(str(exc)) from exc
    # `--no-replace-objects` on the command line as well as in the environment: either alone
    # disables replacement; both together survive a caller that rewrites one of them.
    arguments = [program, "--no-replace-objects", *arguments[1:]]
    result = subprocess.run(
        arguments,
        cwd=repository,
        env=_object_env(),
        capture_output=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace")[-1000:]
        raise GitDiffError(f"git {arguments[2]} failed: {message}")
    return result


def _decode_path(value: bytes) -> str:
    return value.decode("utf-8", errors="surrogateescape")


def _paths_from_name_status(raw: bytes) -> tuple[str, ...]:
    fields = raw.split(b"\0")
    paths: set[str] = set()
    index = 0
    while index < len(fields) and fields[index]:
        status = fields[index]
        index += 1
        path_count = 2 if status[:1] in {b"R", b"C"} else 1
        if index + path_count > len(fields) or any(
            not value for value in fields[index : index + path_count]
        ):
            raise GitDiffError("git name-status output is malformed")
        paths.update(_decode_path(value) for value in fields[index : index + path_count])
        index += path_count
    return tuple(sorted(paths))


def collect_git_diff(repository: str | Path, base: str = "HEAD") -> GitDiff:
    root = Path(repository)
    _run(["git", "add", "-N", "--", "."], root)

    names = _run(
        [
            "git",
            "diff",
            "--name-status",
            "-z",
            "--find-renames",
            "--diff-filter=ACMRD",
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


def collect_commit_diff(repository: str | Path, base: str, head: str = "HEAD") -> GitDiff:
    """The COMMITTED change `head` brings onto `base`: the diff from their merge base to `head`,
    tree to tree -- what a pull request from `head` into `base` shows, and exactly what a push
    of `head` publishes (Codex 4241434509). The working tree and index are never read, so an
    uncommitted edit can neither hide a committed change nor add one that is not pushed. No
    external diff or textconv program runs."""
    for rev in (base, head):
        if not rev or rev.startswith("-"):
            raise GitDiffError(f"not a revision: {rev!r}")
    root = Path(repository)
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
            "--diff-filter=ACMRD",
            *span,
            "--",
        ],
        root,
    ).stdout
    paths = _paths_from_name_status(names)
    raw_stats = _run(["git", "diff", *plain, "--numstat", "-z", *span, "--"], root).stdout
    added = 0
    deleted = 0
    for match in re.finditer(rb"(?:^|\x00)(\d+|-)\t(\d+|-)\t", raw_stats):
        added += int(match.group(1)) if match.group(1).isdigit() else 0
        deleted += int(match.group(2)) if match.group(2).isdigit() else 0
    patch = _run(["git", "diff", *plain, "--binary", "--full-index", *span, "--"], root).stdout
    return GitDiff(paths=paths, added_lines=added, deleted_lines=deleted, patch=patch)


def committed_path(repository: str | Path, path: str | Path) -> str:
    """`path` as a path inside the repository at `repository` (POSIX, relative to its top
    level), for reading it from a commit. A relative `path` is taken relative to the top level;
    an absolute one must lie inside it."""
    root = Path(repository)
    top = _run(["git", "rev-parse", "--show-toplevel"], root).stdout.decode().strip()
    if not top:
        raise GitDiffError(f"{repository} is not inside a git work tree")
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


def read_committed_file(repository: str | Path, rev: str, path: str) -> bytes:
    """The bytes of `path` (relative to the top level) as commit `rev` holds them -- never the
    working tree or the index, which the agent being checked can change without committing
    (Codex 4241614590). Replace refs and grafts are ignored as for every read here, and no
    filter, textconv or external program runs (`cat-file blob`). A missing file is an error."""
    if not rev or rev.startswith("-"):
        raise GitDiffError(f"not a revision: {rev!r}")
    root = Path(repository)
    commit = _run(["git", "rev-parse", "--verify", f"{rev}^{{commit}}"], root).stdout.decode()
    commit = commit.strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise GitDiffError(f"{rev} does not name a commit")
    try:
        return _run(["git", "cat-file", "blob", f"{commit}:{path}"], root).stdout
    except GitDiffError as exc:
        raise GitDiffError(f"{path} could not be read from {rev} ({commit[:12]}): {exc}") from exc
