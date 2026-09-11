from __future__ import annotations

import json
from pathlib import Path

import pytest

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


def test_route_executor_cli_rejects_non_finite_budgets(tmp_path: Path, policy_file: Path) -> None:
    executors = tmp_path / "executors.json"
    executors.write_text(
        json.dumps(
            [
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
    base = [
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
    ]

    for budget in ("nan", "inf", "-inf", "-1"):
        with pytest.raises(SystemExit) as error:
            main([*base, "--budget-usd", budget])
        assert error.value.code == 2


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


def _registry_document() -> dict:
    return {
        "schemaVersion": 1,
        "projects": [
            {
                "projectId": "alpha",
                "displayName": "Alpha",
                "group": "maestro",
                "repositories": [{"provider": "github", "identifier": "example/alpha"}],
                "environments": [{"name": "production", "kind": "production"}],
                "capabilities": ["planning", "implementation"],
            },
            {
                "projectId": "beta",
                "displayName": "Beta",
                "repositories": [{"provider": "github", "identifier": "example/beta"}],
            },
        ],
    }


def _event_document(
    sequence: int,
    state: str,
    stage: str,
    *,
    project_id: str = "alpha",
    unit: str = "github:example/alpha:issue:1",
    activity: str = "",
) -> dict:
    return {
        "schemaVersion": 1,
        "eventId": f"{project_id}:{sequence:04d}",
        "idempotencyKey": f"{unit}:{sequence:04d}",
        "workUnit": {
            "projectId": project_id,
            "workUnitId": unit,
            "repository": f"example/{project_id}",
            "issueRef": f"example/{project_id}#1",
        },
        "stage": stage,
        "state": state,
        "activity": activity,
        "occurredAt": "2026-09-04T09:00:00Z",
        "actor": {"name": "forge-lifecycle", "kind": "system"},
        "provenance": {"source": "forge-ci"},
    }


def test_validate_registry_cli_writes_the_registry(tmp_path: Path) -> None:
    registry = tmp_path / "projects.json"
    output = tmp_path / "registry.json"
    registry.write_text(json.dumps(_registry_document()), encoding="utf-8")

    assert main(["validate-registry", "--registry", str(registry), "--output", str(output)]) == 0

    document = json.loads(output.read_text(encoding="utf-8"))
    assert [item["projectId"] for item in document["projects"]] == ["alpha", "beta"]


def test_validate_registry_cli_fails_closed(tmp_path: Path) -> None:
    registry = tmp_path / "projects.json"
    registry.write_text(
        json.dumps({"schemaVersion": 1, "projects": [{"projectId": "alpha"}]}), encoding="utf-8"
    )

    assert main(["validate-registry", "--registry", str(registry)]) == 2
    assert main(["validate-registry", "--registry", str(tmp_path / "absent.json")]) == 2


def test_record_event_cli_appends_idempotently_and_projects_state(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.json"
    registry = tmp_path / "projects.json"
    event = tmp_path / "event.json"
    projection = tmp_path / "projection.json"
    registry.write_text(json.dumps(_registry_document()), encoding="utf-8")

    for sequence, (state, stage) in enumerate(
        (("intake", "intake"), ("triaged", "intake"), ("specified", "specification"))
    ):
        event.write_text(json.dumps(_event_document(sequence, state, stage)), encoding="utf-8")
        assert (
            main(
                [
                    "record-event",
                    "--ledger",
                    str(ledger),
                    "--registry",
                    str(registry),
                    "--event",
                    str(event),
                    "--projection-output",
                    str(projection),
                ]
            )
            == 0
        )

    # Replaying the last delivery changes nothing.
    before = ledger.read_text(encoding="utf-8")
    assert main(["record-event", "--ledger", str(ledger), "--event", str(event)]) == 0
    assert ledger.read_text(encoding="utf-8") == before

    document = json.loads(projection.read_text(encoding="utf-8"))
    assert document["durable"]["state"] == "specified"
    assert document["durable"]["references"]["issueRef"] == "example/alpha#1"
    assert document["ephemeral"]["activity"] == ""
    assert len(json.loads(before)["events"]) == 3


def test_record_event_cli_fails_closed_on_an_unregistered_project(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.json"
    registry = tmp_path / "projects.json"
    event = tmp_path / "event.json"
    registry.write_text(json.dumps(_registry_document()), encoding="utf-8")
    event.write_text(
        json.dumps(_event_document(0, "intake", "intake", project_id="gamma")), encoding="utf-8"
    )

    assert (
        main(
            [
                "record-event",
                "--ledger",
                str(ledger),
                "--registry",
                str(registry),
                "--event",
                str(event),
            ]
        )
        == 2
    )
    assert not ledger.exists()


def test_record_event_cli_works_without_a_registry(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.json"
    event = tmp_path / "event.json"
    event.write_text(json.dumps(_event_document(0, "intake", "intake")), encoding="utf-8")

    assert main(["record-event", "--ledger", str(ledger), "--event", str(event)]) == 0
    assert len(json.loads(ledger.read_text(encoding="utf-8"))["events"]) == 1


def test_project_state_cli_answers_cross_project_and_single_unit_queries(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.json"
    event = tmp_path / "event.json"
    output = tmp_path / "state.json"
    plan = (
        ("alpha", "github:example/alpha:issue:1", "intake", "intake", ""),
        ("alpha", "github:example/alpha:issue:1", "triaged", "intake", ""),
        (
            "alpha",
            "github:example/alpha:issue:1",
            "triaged",
            "intake",
            "Claude reading the backlog on runner gha-7",
        ),
        ("beta", "github:example/beta:issue:9", "intake", "intake", ""),
    )
    for sequence, (project_id, unit, state, stage, activity) in enumerate(plan):
        event.write_text(
            json.dumps(
                _event_document(
                    sequence, state, stage, project_id=project_id, unit=unit, activity=activity
                )
            ),
            encoding="utf-8",
        )
        assert main(["record-event", "--ledger", str(ledger), "--event", str(event)]) == 0

    assert main(["project-state", "--ledger", str(ledger), "--output", str(output)]) == 0
    everything = json.loads(output.read_text(encoding="utf-8"))
    assert everything["aggregate"]["totals"]["projects"] == 2
    assert len(everything["projections"]) == 2

    assert (
        main(
            [
                "project-state",
                "--ledger",
                str(ledger),
                "--project-id",
                "beta",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    only_beta = json.loads(output.read_text(encoding="utf-8"))
    assert set(only_beta["aggregate"]["projects"]) == {"beta"}

    assert (
        main(
            [
                "project-state",
                "--ledger",
                str(ledger),
                "--project-id",
                "alpha",
                "--unit",
                "github:example/alpha:issue:1",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    unit_document = json.loads(output.read_text(encoding="utf-8"))
    assert unit_document["durable"]["state"] == "triaged"
    assert unit_document["ephemeral"]["activity"] == ("Claude reading the backlog on runner gha-7")

    # A single-unit query needs exactly one project scope.
    assert main(["project-state", "--ledger", str(ledger), "--unit", "whatever"]) == 2
