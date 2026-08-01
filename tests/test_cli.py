from __future__ import annotations

import json
from pathlib import Path

from agentic_sdlc.cli import main


def test_validate_task_cli_writes_decision(
    tmp_path: Path, policy_file: Path, valid_body: str
) -> None:
    task_file = tmp_path / "task.md"
    output = tmp_path / "decision.json"
    task_file.write_text(valid_body, encoding="utf-8")
    result = main(
        [
            "validate-task",
            "--config",
            str(policy_file),
            "--task",
            str(task_file),
            "--title",
            "Valid task",
            "--label",
            "agent-ready",
            "--label",
            "human-review-required",
            "--output",
            str(output),
        ]
    )
    assert result == 0
    assert json.loads(output.read_text(encoding="utf-8"))["allowed"] is True


def test_validate_task_cli_fails_closed(tmp_path: Path, policy_file: Path) -> None:
    task_file = tmp_path / "task.md"
    task_file.write_text("## Summary\nIncomplete", encoding="utf-8")
    result = main(
        [
            "validate-task",
            "--config",
            str(policy_file),
            "--task",
            str(task_file),
            "--title",
            "Invalid task",
        ]
    )
    assert result == 2


def test_prepare_request_uses_file_boundaries(
    tmp_path: Path, policy_file: Path, valid_body: str
) -> None:
    request = tmp_path / "request.json"
    task = tmp_path / "task.md"
    prompt = tmp_path / "prompt.md"
    decision = tmp_path / "decision.json"
    metadata = tmp_path / "metadata.json"
    request.write_text(
        json.dumps(
            {
                "title": "Plan a safe change",
                "body": valid_body,
                "labels": [
                    {"name": "agent-ready"},
                    {"name": "human-review-required"},
                ],
            }
        ),
        encoding="utf-8",
    )
    result = main(
        [
            "prepare-request",
            "--provider",
            "github",
            "--config",
            str(policy_file),
            "--expected-project-id",
            "example/project",
            "--expected-default-branch",
            "main",
            "--request",
            str(request),
            "--mode",
            "plan",
            "--task-output",
            str(task),
            "--prompt-output",
            str(prompt),
            "--decision-output",
            str(decision),
            "--metadata-output",
            str(metadata),
        ]
    )
    assert result == 0
    assert task.read_text(encoding="utf-8").startswith("## Summary")
    assert "<untrusted-work-request>" in prompt.read_text(encoding="utf-8")
    assert json.loads(decision.read_text(encoding="utf-8"))["allowed"] is True
    assert json.loads(metadata.read_text(encoding="utf-8"))["dependencies"] == []


def test_prepare_request_rejects_wrong_repository_context(
    tmp_path: Path,
    policy_file: Path,
    valid_body: str,
) -> None:
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "title": "Plan a safe change",
                "body": valid_body,
                "labels": ["agent-ready", "human-review-required"],
            }
        ),
        encoding="utf-8",
    )

    result = main(
        [
            "prepare-request",
            "--provider",
            "github",
            "--config",
            str(policy_file),
            "--expected-project-id",
            "other/project",
            "--request",
            str(request),
            "--mode",
            "plan",
            "--task-output",
            str(tmp_path / "task.md"),
            "--prompt-output",
            str(tmp_path / "prompt.md"),
            "--decision-output",
            str(tmp_path / "decision.json"),
        ]
    )

    assert result == 2
