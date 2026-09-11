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


def test_validate_missions_cli_writes_registry(tmp_path: Path, policy_file: Path) -> None:
    output = tmp_path / "registry.json"
    result = main(["validate-missions", "--config", str(policy_file), "--output", str(output)])
    assert result == 0
    document = json.loads(output.read_text(encoding="utf-8"))
    assert "implementation-worker" in document["missions"]


def test_dispatch_mission_cli_produces_envelope(tmp_path: Path, policy_file: Path) -> None:
    agents = tmp_path / "agents.json"
    history = tmp_path / "history.json"
    prompt = tmp_path / "prompt.md"
    output = tmp_path / "envelope.json"
    agents.write_text(
        json.dumps(
            [
                {
                    "agentId": "codex-1",
                    "adapter": "codex",
                    "adapterVersion": "1.0.0",
                    "provider": "openai",
                    "model": "gpt-5",
                    "capabilities": ["edit-code", "author-tests", "run-commands"],
                },
                {
                    "agentId": "claude-1",
                    "adapter": "claude",
                    "adapterVersion": "2.0.0",
                    "provider": "anthropic",
                    "model": "claude-opus-5",
                    "capabilities": ["review-code", "review-security"],
                },
            ]
        ),
        encoding="utf-8",
    )
    history.write_text(json.dumps({"implementation-worker": "codex-1"}), encoding="utf-8")
    prompt.write_text("Review the patch.", encoding="utf-8")
    result = main(
        [
            "dispatch-mission",
            "--config",
            str(policy_file),
            "--mission-id",
            "security-reviewer",
            "--agents",
            str(agents),
            "--history",
            str(history),
            "--work-ref",
            "example/project#7",
            "--input-ref",
            "patch",
            "--prompt",
            str(prompt),
            "--output",
            str(output),
        ]
    )
    assert result == 0
    envelope = json.loads(output.read_text(encoding="utf-8"))
    assert envelope["agentId"] == "claude-1"
    assert envelope["missionId"] == "security-reviewer"
    assert len(envelope["envelopeSha256"]) == 64


def test_dispatch_mission_cli_fails_closed_on_independence(
    tmp_path: Path, policy_file: Path
) -> None:
    agents = tmp_path / "agents.json"
    history = tmp_path / "history.json"
    prompt = tmp_path / "prompt.md"
    agents.write_text(
        json.dumps(
            [
                {
                    "agentId": "codex-1",
                    "adapter": "codex",
                    "adapterVersion": "1.0.0",
                    "provider": "openai",
                    "model": "gpt-5",
                    "capabilities": ["edit-code", "review-code", "review-security"],
                }
            ]
        ),
        encoding="utf-8",
    )
    history.write_text(json.dumps({"implementation-worker": "codex-1"}), encoding="utf-8")
    prompt.write_text("Review the patch.", encoding="utf-8")
    result = main(
        [
            "dispatch-mission",
            "--config",
            str(policy_file),
            "--mission-id",
            "security-reviewer",
            "--agents",
            str(agents),
            "--history",
            str(history),
            "--work-ref",
            "example/project#7",
            "--prompt",
            str(prompt),
        ]
    )
    assert result == 2


def test_validate_executors_cli_writes_normalized_registry(tmp_path: Path) -> None:
    executors = tmp_path / "executors.json"
    output = tmp_path / "normalized.json"
    executors.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "executors": [
                    {
                        "executorId": "deepseek-1",
                        "provider": "deepseek",
                        "adapter": "deepseek-direct",
                        "adapterVersion": "1.0.0",
                        "executionType": "direct-api",
                        "authMode": "api-key",
                        "model": "deepseek-reasoner",
                        "modelFamily": "deepseek",
                        "taskClasses": ["implementation"],
                        "capabilities": ["edit-code", "author-tests", "run-commands"],
                        "qualityLowerBound": 0.8,
                        "directCostUsd": 0.5,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = main(["validate-executors", "--executors", str(executors), "--output", str(output)])

    assert result == 0
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["executors"][0]["provider"] == "deepseek"
    assert "authMode" in document["executors"][0]


def test_route_executor_cli_selects_lowest_ecps_eligible_model(
    tmp_path: Path, policy_file: Path
) -> None:
    executors = tmp_path / "executors.json"
    output = tmp_path / "route.json"
    base = {
        "adapterVersion": "1.0.0",
        "executionType": "direct-api",
        "authMode": "api-key",
        "taskClasses": ["implementation"],
        "capabilities": ["edit-code", "author-tests", "run-commands"],
        "toolCapabilities": ["structured-output"],
        "contextWindow": 128000,
        "maxRisk": "medium",
        "qualityLowerBound": 0.78,
    }
    executors.write_text(
        json.dumps(
            [
                {
                    **base,
                    "executorId": "openai-1",
                    "provider": "openai",
                    "adapter": "openai-direct",
                    "model": "gpt-5",
                    "modelFamily": "gpt-5",
                    "directCostUsd": 3.0,
                },
                {
                    **base,
                    "executorId": "deepseek-1",
                    "provider": "deepseek",
                    "adapter": "deepseek-direct",
                    "model": "deepseek-reasoner",
                    "modelFamily": "deepseek",
                    "directCostUsd": 0.7,
                },
            ]
        ),
        encoding="utf-8",
    )

    result = main(
        [
            "route-executor",
            "--config",
            str(policy_file),
            "--mission-id",
            "implementation-worker",
            "--executors",
            str(executors),
            "--repository",
            "example/project",
            "--task-class",
            "implementation",
            "--required-tool-capability",
            "structured-output",
            "--budget-usd",
            "5",
            "--output",
            str(output),
        ]
    )

    assert result == 0
    decision = json.loads(output.read_text(encoding="utf-8"))
    assert decision["selectedExecutorId"] == "deepseek-1"
    assert decision["policyVersion"] == "1.0.0"


def test_route_executor_cli_fails_closed_when_quality_floor_rejects_cheap_models(
    tmp_path: Path, policy_file: Path
) -> None:
    executors = tmp_path / "executors.json"
    routing_policy = tmp_path / "routing-policy.json"
    output = tmp_path / "route.json"
    executors.write_text(
        json.dumps(
            [
                {
                    "executorId": "deepseek-cheap",
                    "provider": "deepseek",
                    "adapter": "deepseek-direct",
                    "adapterVersion": "1.0.0",
                    "executionType": "direct-api",
                    "authMode": "api-key",
                    "model": "deepseek-reasoner",
                    "modelFamily": "deepseek",
                    "taskClasses": ["implementation"],
                    "capabilities": ["edit-code", "author-tests", "run-commands"],
                    "toolCapabilities": ["structured-output"],
                    "contextWindow": 128000,
                    "maxRisk": "medium",
                    "qualityLowerBound": 0.8,
                    "directCostUsd": 0.5,
                }
            ]
        ),
        encoding="utf-8",
    )
    routing_policy.write_text(
        json.dumps({"policyVersion": "strict", "qualityFloors": {"medium": 0.9}}),
        encoding="utf-8",
    )

    result = main(
        [
            "route-executor",
            "--config",
            str(policy_file),
            "--mission-id",
            "implementation-worker",
            "--executors",
            str(executors),
            "--routing-policy",
            str(routing_policy),
            "--repository",
            "example/project",
            "--task-class",
            "implementation",
            "--required-tool-capability",
            "structured-output",
            "--budget-usd",
            "5",
            "--output",
            str(output),
        ]
    )

    assert result == 2
    decision = json.loads(output.read_text(encoding="utf-8"))
    assert decision["status"] == "insufficient-budget-or-assurance"
    assert decision["selectedExecutorId"] == ""
    assert decision["candidates"][0]["rejectionReasons"] == ["quality floor not met: 0.800 < 0.900"]


def test_classify_failure_cli_writes_transient_class(tmp_path: Path) -> None:
    log = tmp_path / "failed.log"
    output = tmp_path / "class.json"
    log.write_text(
        "HttpError: No server is currently available to service your request.",
        encoding="utf-8",
    )

    result = main(
        [
            "classify-failure",
            "--conclusion",
            "failure",
            "--log",
            str(log),
            "--output",
            str(output),
        ]
    )

    assert result == 0
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "failureClass": "transient_infrastructure"
    }


def test_decide_infra_retry_cli_updates_exact_head_state(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    output = tmp_path / "decision.json"
    head = "a" * 40

    result = main(
        [
            "decide-infra-retry",
            "--state",
            str(state),
            "--repository",
            "atulg4/agentic-sdlc",
            "--pull-request-number",
            "129",
            "--run-id",
            "456",
            "--head-sha",
            head,
            "--current-head-sha",
            head,
            "--failure-class",
            "transient_infrastructure",
            "--failed-job-id",
            "42",
            "--max-attempts",
            "3",
            "--event-key",
            "workflow-run-456",
            "--timestamp",
            "2026-08-25T12:00:00Z",
            "--output",
            str(output),
        ]
    )

    assert result == 0
    decision = json.loads(output.read_text(encoding="utf-8"))
    assert decision["action"] == "retry_failed_jobs"
    assert decision["retryJobIds"] == [42]
    persisted = json.loads(state.read_text(encoding="utf-8"))
    assert persisted["records"][f"atulg4/agentic-sdlc#pr-129:run-456:head-{head}"]["attempts"] == 1


def test_decide_infra_retry_cli_reads_durable_comment_state_and_renders_evidence(
    tmp_path: Path,
) -> None:
    comments = tmp_path / "comments.json"
    output = tmp_path / "decision.json"
    comment_output = tmp_path / "comment.md"
    panel_output = tmp_path / "panel.json"
    head = "a" * 40
    comments.write_text(json.dumps([]), encoding="utf-8")

    result = main(
        [
            "decide-infra-retry",
            "--comments",
            str(comments),
            "--repository",
            "atulg4/agentic-sdlc",
            "--pull-request-number",
            "129",
            "--run-id",
            "456",
            "--head-sha",
            head,
            "--current-head-sha",
            head,
            "--failure-class",
            "transient_infrastructure",
            "--failed-job-id",
            "42",
            "--event-key",
            "run-456-attempt-1",
            "--timestamp",
            "2026-08-25T12:00:00Z",
            "--comment-output",
            str(comment_output),
            "--panel-output",
            str(panel_output),
            "--output",
            str(output),
        ]
    )

    assert result == 0
    assert json.loads(output.read_text(encoding="utf-8"))["action"] == "retry_failed_jobs"
    assert "forge-transient-retry" in comment_output.read_text(encoding="utf-8")
    assert json.loads(panel_output.read_text(encoding="utf-8"))["autoRetry"] == "1/3"


def test_decide_infra_retry_cli_blocks_when_comment_evidence_exhausts_the_budget(
    tmp_path: Path,
) -> None:
    comments = tmp_path / "comments.json"
    output = tmp_path / "decision.json"
    panel_output = tmp_path / "panel.json"
    head = "a" * 40
    comments.write_text(
        json.dumps(
            [
                {
                    "user": {"login": "github-actions[bot]"},
                    "body": (
                        "<!-- forge-transient-retry "
                        + json.dumps(
                            {
                                "schemaVersion": 1,
                                "repository": "atulg4/agentic-sdlc",
                                "pullRequestNumber": 129,
                                "runId": 456,
                                "headSha": head,
                                "attempts": attempt,
                                "maxAttempts": 3,
                                "status": "retrying",
                                "eventKey": f"run-456-attempt-{attempt}",
                                "timestamp": "2026-08-25T12:00:00Z",
                            }
                        )
                        + " -->"
                    ),
                }
                for attempt in (1, 2, 3)
            ]
        ),
        encoding="utf-8",
    )

    result = main(
        [
            "decide-infra-retry",
            "--comments",
            str(comments),
            "--repository",
            "atulg4/agentic-sdlc",
            "--pull-request-number",
            "129",
            "--run-id",
            "456",
            "--head-sha",
            head,
            "--current-head-sha",
            head,
            "--failure-class",
            "transient_infrastructure",
            "--event-key",
            "run-456-attempt-4",
            "--timestamp",
            "2026-08-25T13:00:00Z",
            "--last-error-summary",
            "HttpError: no server is currently available",
            "--panel-output",
            str(panel_output),
            "--output",
            str(output),
        ]
    )

    assert result == 2
    decision = json.loads(output.read_text(encoding="utf-8"))
    assert decision["action"] == "block"
    assert decision["blocker"]["userActionRequired"] is False
    panel = json.loads(panel_output.read_text(encoding="utf-8"))
    assert panel["headline"] == "Blocked: GitHub infrastructure"
    assert panel["autoRetry"] == "3/3"


def test_decide_infra_retry_cli_requires_exactly_one_state_source(tmp_path: Path) -> None:
    head = "a" * 40

    result = main(
        [
            "decide-infra-retry",
            "--repository",
            "atulg4/agentic-sdlc",
            "--pull-request-number",
            "129",
            "--run-id",
            "456",
            "--head-sha",
            head,
            "--current-head-sha",
            head,
            "--failure-class",
            "transient_infrastructure",
            "--output",
            str(tmp_path / "decision.json"),
        ]
    )

    assert result == 2
    assert not (tmp_path / "decision.json").exists()


def test_orchestrate_cli_round_trips_state(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    status = tmp_path / "status.md"
    base = [
        "orchestrate",
        "--state",
        str(state),
        "--unit",
        "42",
        "--timestamp",
        "2026-08-06T12:00:00Z",
        "--status-output",
        str(status),
    ]
    assert main([*base, "--action", "create", "--event-key", "evt-1"]) == 0
    assert (
        main(
            [
                *base,
                "--action",
                "transition",
                "--event-key",
                "evt-2",
                "--to",
                "triaged",
                "--actor",
                "atulg4",
                "--actor-kind",
                "human",
            ]
        )
        == 0
    )
    document = json.loads(state.read_text(encoding="utf-8"))
    assert document["units"]["42"]["state"] == "triaged"
    assert status.read_text(encoding="utf-8").startswith("<!-- agentic-sdlc:status -->")
    # Replaying the same event is a no-op, and invalid transitions fail closed.
    assert main([*base, "--action", "transition", "--event-key", "evt-2", "--to", "triaged"]) == 0
    assert main([*base, "--action", "transition", "--event-key", "evt-3", "--to", "merged"]) == 2
