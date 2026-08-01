from __future__ import annotations

import subprocess
from pathlib import Path

from agentic_sdlc.git_diff import collect_git_diff


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )


def test_collect_diff_handles_untracked_and_spaced_paths(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "existing.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "--", ".")
    _git(tmp_path, "commit", "-qm", "base")

    (tmp_path / "existing.py").write_text("VALUE = 2\n", encoding="utf-8")
    (tmp_path / "file with spaces.py").write_text("NEW = True\n", encoding="utf-8")
    snapshot = collect_git_diff(tmp_path)

    assert snapshot.paths == ("existing.py", "file with spaces.py")
    assert snapshot.added_lines == 2
    assert snapshot.deleted_lines == 1
    assert b"file with spaces.py" in snapshot.patch


def test_collect_diff_includes_deleted_paths(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    target = tmp_path / "protected.py"
    target.write_text("SECRET = False\n", encoding="utf-8")
    _git(tmp_path, "add", "--", ".")
    _git(tmp_path, "commit", "-qm", "base")

    target.unlink()
    snapshot = collect_git_diff(tmp_path)

    assert snapshot.paths == ("protected.py",)
    assert snapshot.added_lines == 0
    assert snapshot.deleted_lines == 1


def test_collect_diff_includes_both_sides_of_rename(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    workflow = tmp_path / ".github/workflows/ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: CI\n", encoding="utf-8")
    _git(tmp_path, "add", "--", ".")
    _git(tmp_path, "commit", "-qm", "base")

    workflow.rename(tmp_path / "renamed.yml")
    snapshot = collect_git_diff(tmp_path)

    assert snapshot.paths == (".github/workflows/ci.yml", "renamed.yml")
    assert b"rename from .github/workflows/ci.yml" in snapshot.patch
    assert b"rename to renamed.yml" in snapshot.patch
