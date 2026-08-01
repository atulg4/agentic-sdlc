"""Collect a proposed Git patch without unsafe filename splitting."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitDiffError(RuntimeError):
    """Raised when a diff cannot be collected."""


@dataclass(frozen=True)
class GitDiff:
    paths: tuple[str, ...]
    added_lines: int
    deleted_lines: int
    patch: bytes


def _run(arguments: list[str], repository: Path) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        arguments,
        cwd=repository,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace")[-1000:]
        raise GitDiffError(f"{' '.join(arguments[:2])} failed: {message}")
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
