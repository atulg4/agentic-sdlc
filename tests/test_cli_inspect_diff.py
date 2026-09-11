from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agentic_sdlc.cli import main


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )


def _repo(tmp_path: Path) -> Path:
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "test@example.com")
    _git(repository, "config", "user.name", "Test")
    (repository / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repository, "add", "--", ".")
    _git(repository, "commit", "-qm", "base")
    return repository


def _inspect(repository: Path, policy_file: Path, tmp_path: Path) -> int:
    return main(
        [
            "inspect-diff",
            "--config",
            str(policy_file),
            "--repository",
            str(repository),
            "--base",
            "HEAD",
            "--patch-output",
            str(tmp_path / "change.patch"),
            "--paths-output",
            str(tmp_path / "paths.txt"),
            "--decision-output",
            str(tmp_path / "decision.json"),
        ]
    )


def test_inspect_diff_explains_an_empty_patch(
    tmp_path: Path, policy_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repo(tmp_path)

    assert _inspect(repository, policy_file, tmp_path) == 2

    captured = capsys.readouterr()
    assert "ERROR: inspect-diff rejected the generated patch" in captured.err
    assert "no files changed" in captured.err


def test_inspect_diff_explains_policy_rejection(
    tmp_path: Path, policy_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repo(tmp_path)
    workflows = repository / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("name: ci\n", encoding="utf-8")

    assert _inspect(repository, policy_file, tmp_path) == 2

    captured = capsys.readouterr()
    assert "ERROR: inspect-diff rejected the generated patch" in captured.err
    assert "forbidden paths changed" in captured.err
    document = json.loads((tmp_path / "decision.json").read_text(encoding="utf-8"))
    assert document["allowed"] is False


def test_inspect_diff_accepts_an_allowed_patch(
    tmp_path: Path, policy_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repository = _repo(tmp_path)
    (repository / "module.py").write_text("VALUE = 2\n", encoding="utf-8")

    assert _inspect(repository, policy_file, tmp_path) == 0
    assert "ERROR" not in capsys.readouterr().err
