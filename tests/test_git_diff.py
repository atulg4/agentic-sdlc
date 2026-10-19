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


def test_derivation_worktree_materializes_the_committed_tree(tmp_path: Path) -> None:
    from agentic_sdlc.git_diff import derivation_worktree

    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "a.txt").write_text("committed\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    _git(tmp_path, "add", "--", ".")
    _git(tmp_path, "commit", "-qm", "base")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True
    ).stdout.strip()

    # Dirty the caller's checkout in every way a gate could observe: an
    # uncommitted edit, an ignored file, and status output suppressed by
    # repository-local configuration.
    (tmp_path / "a.txt").write_text("uncommitted\n", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("SECRET=1\n", encoding="utf-8")
    _git(tmp_path, "config", "status.showUntrackedFiles", "no")

    with derivation_worktree(tmp_path, head) as tree:
        kept = tree
        # The disposable tree holds exactly the committed content: no
        # uncommitted edit, no ignored file.
        assert (tree / "a.txt").read_text(encoding="utf-8") == "committed\n"
        assert not (tree / "ignored.txt").exists()
    # The worktree is removed afterwards, administrative entry included,
    # and the caller's own state is untouched.
    assert not kept.exists()
    listed = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert listed.count("worktree ") == 1
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "uncommitted\n"
