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


def test_packet_commands_run_under_strict_executable_trust(tmp_path: Path, monkeypatch, capsys):
    # Packet derivation and attested orchestration derive trust artifacts:
    # they resolve git strictly, and an executable override is ignored with a
    # warning instead of trusting a candidate-planted program.
    from agentic_sdlc.cli import GATE_COMMANDS

    assert {
        "context-packet",
        "dispatch-packet",
        "orchestrate",
        "attest-completion",
        "attest-outcome",
    } <= GATE_COMMANDS
    monkeypatch.setenv("AGENTIC_SDLC_GIT", str(tmp_path / "planted-git"))
    state = tmp_path / "state.json"
    assert (
        main(
            [
                "orchestrate",
                "--state",
                str(state),
                "--unit",
                "9",
                "--timestamp",
                "2026-08-06T12:00:00Z",
                "--action",
                "create",
                "--event-key",
                "evt-1",
            ]
        )
        == 0
    )
    assert "is ignored by" in capsys.readouterr().err


def test_orchestrate_attests_only_runs_a_trusted_dispatch_produced(tmp_path: Path, capsys) -> None:
    # An attested state records only dispatch-signed envelopes: a
    # hand-authored envelope — or a signed one whose agent was re-labeled —
    # must not seed run attribution, and a finished run's commit must exist
    # in the trusted checkout before the state attests it.
    import hashlib
    import subprocess

    from agentic_sdlc.cli import _attest_document

    secret = b"an-attestation-secret-of-at-least-32-bytes"
    key_file = tmp_path / "attestation.key"
    key_file.write_bytes(secret)
    state = tmp_path / "state.json"
    base = [
        "orchestrate",
        "--state",
        str(state),
        "--unit",
        "42",
        "--attestation-key",
        str(key_file),
    ]
    stamp = "2026-08-06T12:00:00Z"
    assert main([*base, "--action", "create", "--event-key", "evt-0", "--timestamp", stamp]) == 0
    for index, to in enumerate(("triaged", "specified", "planned", "approved", "dispatched"), 1):
        assert (
            main(
                [
                    *base,
                    "--action",
                    "transition",
                    "--event-key",
                    f"evt-{index}",
                    "--to",
                    to,
                    "--timestamp",
                    stamp,
                    "--actor",
                    "operator",
                    "--actor-kind",
                    "human",
                ]
            )
            == 0
        )
    # A trusted dispatch digests the attested state's lineage id AND the
    # work unit's exact snapshot, so the crafted envelopes bind both exactly
    # as dispatch-packet would.
    state_id = json.loads(state.read_text(encoding="utf-8"))["stateId"]

    def _unit_snapshot_sha256() -> str:
        document = json.loads(state.read_text(encoding="utf-8"))
        canonical = json.dumps(
            document["units"]["42"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    body = {
        "schemaVersion": 1,
        "missionId": "implementation-worker",
        "missionVersion": "1.0.0",
        "agentId": "builder-agent",
        "adapter": "claude-code",
        "adapterVersion": "1.0.0",
        "provider": "anthropic",
        "model": "claude-sonnet-5-5",
        "workRef": "42",
        "inputRefs": [],
        "promptSha256": "0" * 64,
        "packetRole": "builder",
        "runKind": "implementation",
        "stateId": state_id,
        "unitStateSha256": _unit_snapshot_sha256(),
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    envelope = {**body, "envelopeSha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest()}
    signed = {**envelope, "attestation": _attest_document(envelope, secret)}

    def _envelope_file(name: str, document: dict) -> str:
        path = tmp_path / name
        path.write_text(json.dumps(document), encoding="utf-8")
        return str(path)

    def _signed_for(**overrides) -> dict:
        changed = {**body, **overrides}
        changed_canonical = json.dumps(changed, sort_keys=True, separators=(",", ":"))
        changed = {
            **changed,
            "envelopeSha256": hashlib.sha256(changed_canonical.encode("utf-8")).hexdigest(),
        }
        return {**changed, "attestation": _attest_document(changed, secret)}

    start = [
        *base,
        "--action",
        "start-run",
        "--kind",
        "implementation",
        "--event-key",
        "evt-run",
        "--timestamp",
        stamp,
        "--envelope",
    ]
    # Unsigned, re-labeled after signing, replayed from another work item,
    # or declared for a non-producing role: each refuses.
    assert main([*start, _envelope_file("unsigned.json", envelope)]) == 2
    assert main([*start, _envelope_file("relabeled.json", {**signed, "agentId": "x"})]) == 2
    assert main([*start, _envelope_file("replayed.json", _signed_for(workRef="43"))]) == 2
    assert main([*start, _envelope_file("reviewer.json", _signed_for(packetRole="reviewer"))]) == 2
    # The EXACT kind is digested: an implementation envelope cannot seed a
    # repair run, which would bypass the fresh repair dispatch.
    repair_start = [entry if entry != "implementation" else "repair" for entry in start]
    assert main([*repair_start, _envelope_file("replay-kind.json", signed)]) == 2
    # A genuinely signed envelope from ANOTHER state lineage (a recreated
    # state) cannot replay here.
    assert main([*start, _envelope_file("other-state.json", _signed_for(stateId="f" * 32))]) == 2
    # A genuinely signed envelope bound to a STALE snapshot of this same
    # lineage (the unit's digest before it advanced) cannot start a run
    # against the current state: the lineage id alone never changes, so
    # only the unit-snapshot binding stops a replay from a retained copy.
    capsys.readouterr()
    assert (
        main([*start, _envelope_file("stale-unit.json", _signed_for(unitStateSha256="e" * 64))])
        == 2
    )
    assert "current state snapshot" in capsys.readouterr().err
    assert main([*start, _envelope_file("signed.json", signed)]) == 0
    # A retry of the SAME event with the SAME envelope is the orchestrator's
    # documented event-key idempotency (a caller that lost the response): it
    # returns the recorded run instead of refusing, and records no second run.
    assert main([*start, _envelope_file("retried.json", signed)]) == 0
    assert len(json.loads(state.read_text(encoding="utf-8"))["units"]["42"]["runs"]) == 1
    # Under a DIFFERENT event, one digest still identifies one dispatch: the
    # recorded envelope cannot seed a second run (a reused review envelope
    # would otherwise report on the pre-repair packet while advancing the
    # repaired unit).
    capsys.readouterr()
    replay_start = [entry if entry != "evt-run" else "evt-run-2" for entry in start]
    assert main([*replay_start, _envelope_file("replayed-digest.json", signed)]) == 2
    assert "already recorded" in capsys.readouterr().err

    # The finished run's commit needs COMPLETION PROVENANCE: the execution
    # harness's attest-completion record binding this unit, run and commit —
    # and attest-completion itself requires the commit to exist in the
    # trusted checkout.
    repo = tmp_path / "trusted-checkout"
    repo.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
    }
    subprocess.run(
        ["git", "-C", str(repo), "init", "-q", "-b", "main"],
        check=True,
        env=env,
        capture_output=True,
    )
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "a.txt"], check=True, env=env, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "work"],
        check=True,
        env=env,
        capture_output=True,
    )
    commit = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    ).stdout.strip()
    run_id = "42:run-001"
    completion = tmp_path / "completion.json"
    # attest-completion refuses a commit that exists nowhere, and binds the
    # record to this exact unit, run and commit.
    attest = [
        "attest-completion",
        "--unit",
        "42",
        "--run-id",
        run_id,
        "--result",
        "succeeded",
        "--envelope-sha256",
        signed["envelopeSha256"],
        "--commit-repository",
        str(repo),
        "--attestation-key",
        str(key_file),
        "--output",
        str(completion),
    ]
    assert main([*attest, "--commit-sha", "f" * 40]) == 2
    assert main([*attest, "--commit-sha", commit]) == 0
    finish = [
        *base,
        "--action",
        "finish-run",
        "--run-id",
        run_id,
        "--result",
        "succeeded",
        "--timestamp",
        stamp,
        "--commit-sha",
        commit,
    ]
    # Without the completion record — or with one bound to another run — the
    # attested finish refuses; the genuine record finishes the run.
    assert main(finish) == 2
    other = json.loads(completion.read_text(encoding="utf-8"))
    other["runId"] = "42:run-999"
    from agentic_sdlc.cli import _attest_document as _sign

    other["attestation"] = _sign(other, secret)
    mismatched = tmp_path / "mismatched-completion.json"
    mismatched.write_text(json.dumps(other), encoding="utf-8")
    assert main([*finish, "--completion", str(mismatched)]) == 2
    tampered_completion = json.loads(completion.read_text(encoding="utf-8"))
    tampered_completion["commitSha"] = commit
    tampered_completion["unit"] = "43"
    unsigned_completion = tmp_path / "unsigned-completion.json"
    unsigned_completion.write_text(json.dumps(tampered_completion), encoding="utf-8")
    assert main([*finish, "--completion", str(unsigned_completion)]) == 2
    # Run IDs restart when state is recreated: a genuine completion bound to
    # ANOTHER dispatch's envelope digest must not finish this run.
    stale = json.loads(completion.read_text(encoding="utf-8"))
    stale["envelopeSha256"] = "e" * 64
    stale["attestation"] = _sign(stale, secret)
    stale_file = tmp_path / "stale-completion.json"
    stale_file.write_text(json.dumps(stale), encoding="utf-8")
    assert main([*finish, "--completion", str(stale_file)]) == 2
    # The RESULT is bound too: a genuinely signed completion the harness
    # recorded as FAILED must not finish the run as succeeded, which would
    # promote a failed producer into the reviewer dispatch's latest
    # successful producer.
    failed_completion = tmp_path / "failed-completion.json"
    failed_attest = [
        entry if entry != str(completion) else str(failed_completion) for entry in attest
    ]
    failed_attest[failed_attest.index("succeeded")] = "failed"
    assert main([*failed_attest, "--commit-sha", commit]) == 0
    capsys.readouterr()
    assert main([*finish, "--completion", str(failed_completion)]) == 2
    assert "result and commit" in capsys.readouterr().err
    assert main([*finish, "--completion", str(completion)]) == 0

    # VERIFICATION and REVIEW verdicts are caller input too: under a key each
    # transition requires the attest-outcome record the judging harness
    # signed, bound to the judged run's dispatch envelope.
    assert (
        main(
            [
                *base,
                "--action",
                "transition",
                "--event-key",
                "evt-verifying",
                "--to",
                "verifying",
                "--timestamp",
                stamp,
                "--actor",
                "operator",
                "--actor-kind",
                "human",
            ]
        )
        == 0
    )
    # The verdict EDGES belong to those actions too: a generic transition
    # taking verifying -> reviewing under the key would bypass the outcome
    # gating and still receive a genuine state attestation.
    capsys.readouterr()
    assert (
        main(
            [
                *base,
                "--action",
                "transition",
                "--event-key",
                "evt-skip",
                "--to",
                "reviewing",
                "--timestamp",
                stamp,
                "--actor",
                "operator",
                "--actor-kind",
                "human",
            ]
        )
        == 2
    )
    assert "verification or review verdict" in capsys.readouterr().err
    verify = [
        *base,
        "--action",
        "verification",
        "--result",
        "succeeded",
        "--event-key",
        "evt-verdict",
        "--timestamp",
        stamp,
    ]
    capsys.readouterr()
    assert main(verify) == 2
    assert "requires --outcome" in capsys.readouterr().err
    outcome = tmp_path / "outcome.json"
    assert (
        main(
            [
                "attest-outcome",
                "--unit",
                "42",
                "--kind",
                "verification",
                "--result",
                "succeeded",
                "--envelope-sha256",
                signed["envelopeSha256"],
                "--attestation-key",
                str(key_file),
                "--output",
                str(outcome),
            ]
        )
        == 0
    )

    def _reissued(path_name: str, **changed) -> str:
        document = {**json.loads(outcome.read_text(encoding="utf-8")), **changed}
        del document["attestation"]
        document["attestation"] = _attest_document(document, secret)
        path = tmp_path / path_name
        path.write_text(json.dumps(document), encoding="utf-8")
        return str(path)

    # A verdict signed over another envelope — or over the opposite result —
    # must not drive this transition.
    assert main([*verify, "--outcome", _reissued("other-env.json", envelopeSha256="e" * 64)]) == 2
    assert main([*verify, "--outcome", _reissued("other-result.json", result="failed")]) == 2
    assert main([*verify, "--outcome", str(outcome)]) == 0

    # The unit is now reviewing: start the review run from a reviewer-signed
    # dispatch, then the review transition needs its own signed outcome bound
    # to that run's envelope, run id and findings count.
    # A review dispatch happens against the ADVANCED state, so its envelope
    # binds the unit's current snapshot, not the implementation-time one.
    review_envelope = _signed_for(
        missionId="code-reviewer",
        agentId="reviewer-agent",
        packetRole="reviewer",
        runKind="review",
        unitStateSha256=_unit_snapshot_sha256(),
    )
    review_start = [
        *base,
        "--action",
        "start-run",
        "--kind",
        "review",
        "--event-key",
        "evt-review-run",
        "--timestamp",
        stamp,
        "--envelope",
        _envelope_file("review-envelope.json", review_envelope),
    ]
    assert main(review_start) == 0
    review = [
        *base,
        "--action",
        "review",
        "--run-id",
        "42:run-002",
        "--findings",
        "0",
        "--event-key",
        "evt-review",
        "--timestamp",
        stamp,
    ]
    capsys.readouterr()
    assert main(review) == 2
    assert "requires --outcome" in capsys.readouterr().err
    review_outcome = tmp_path / "review-outcome.json"
    assert (
        main(
            [
                "attest-outcome",
                "--unit",
                "42",
                "--kind",
                "review",
                "--result",
                "succeeded",
                "--findings",
                "0",
                "--run-id",
                "42:run-002",
                "--envelope-sha256",
                review_envelope["envelopeSha256"],
                "--attestation-key",
                str(key_file),
                "--output",
                str(review_outcome),
            ]
        )
        == 0
    )

    def _review_reissued(path_name: str, **changed) -> str:
        document = {**json.loads(review_outcome.read_text(encoding="utf-8")), **changed}
        del document["attestation"]
        document["attestation"] = _attest_document(document, secret)
        path = tmp_path / path_name
        path.write_text(json.dumps(document), encoding="utf-8")
        return str(path)

    # A review WITH findings carries the report inside the attestation: the
    # count alone is not the review, so attest-outcome demands the canonical
    # findings array and binds its digest.
    capsys.readouterr()
    found_base = [
        "attest-outcome",
        "--unit",
        "42",
        "--kind",
        "review",
        "--result",
        "succeeded",
        "--findings",
        "2",
        "--run-id",
        "42:run-002",
        "--envelope-sha256",
        review_envelope["envelopeSha256"],
        "--attestation-key",
        str(key_file),
    ]
    assert main(found_base) == 2
    assert "requires --findings-report" in capsys.readouterr().err
    report = tmp_path / "findings.json"
    report.write_text(json.dumps(["finding one", "finding two"]), encoding="utf-8")
    short_report = tmp_path / "short-findings.json"
    short_report.write_text(json.dumps(["finding one"]), encoding="utf-8")
    capsys.readouterr()
    assert main([*found_base, "--findings-report", str(short_report)]) == 2
    assert "declares 2" in capsys.readouterr().err
    found_outcome = tmp_path / "found-outcome.json"
    assert (
        main([*found_base, "--findings-report", str(report), "--output", str(found_outcome)]) == 0
    )
    # The recorded transition verifies the report against the signed digest:
    # a tampered report — or none at all — never rides the genuine count.
    review_two = [
        *base,
        "--action",
        "review",
        "--run-id",
        "42:run-002",
        "--findings",
        "2",
        "--event-key",
        "evt-review-two",
        "--timestamp",
        stamp,
    ]
    tampered_report = tmp_path / "tampered-findings.json"
    tampered_report.write_text(json.dumps(["finding one", "tampered"]), encoding="utf-8")
    capsys.readouterr()
    assert (
        main(
            [
                *review_two,
                "--outcome",
                str(found_outcome),
                "--findings-report",
                str(tampered_report),
            ]
        )
        == 2
    )
    assert "different findings report" in capsys.readouterr().err
    capsys.readouterr()
    assert main([*review_two, "--outcome", str(found_outcome)]) == 2
    assert "different findings report" in capsys.readouterr().err

    # The reviewing -> ready-for-human-merge edge is verdict-owned too.
    capsys.readouterr()
    assert (
        main(
            [
                *base,
                "--action",
                "transition",
                "--event-key",
                "evt-skip-2",
                "--to",
                "ready-for-human-merge",
                "--timestamp",
                stamp,
                "--actor",
                "operator",
                "--actor-kind",
                "human",
            ]
        )
        == 2
    )
    assert "verification or review verdict" in capsys.readouterr().err
    # A review signed for another run, another findings count, or another
    # envelope must not advance this unit; a verification outcome cannot be
    # replayed as a review, and a genuinely signed FAILED review (a crashed
    # reviewer) proves nothing and must not pass as a zero-finding verdict.
    for bogus in (
        _review_reissued("review-run.json", runId="42:run-001"),
        _review_reissued("review-count.json", findings=3),
        _review_reissued("review-env.json", envelopeSha256=signed["envelopeSha256"]),
        _review_reissued("review-failed.json", result="failed"),
        str(outcome),
    ):
        assert main([*review, "--outcome", bogus]) == 2
    assert main([*review, "--outcome", str(review_outcome)]) == 0
    document = json.loads(state.read_text(encoding="utf-8"))
    assert document["units"]["42"]["state"] == "ready-for-human-merge"


def test_the_harness_trust_anchor_pins_the_attestation_key(tmp_path: Path, monkeypatch, capsys):
    # The artifacts and the key that verifies them must not both be caller
    # inputs: under the anchor, attestation cannot be switched off by
    # omitting the flag, and a caller-minted substitute key is refused.
    from agentic_sdlc.cli import _attest_document

    secret = b"an-attestation-secret-of-at-least-32-bytes"
    anchor_file = tmp_path / "anchor.key"
    anchor_file.write_bytes(secret)
    minted = tmp_path / "minted.key"
    minted.write_bytes(b"a-caller-minted-key-of-32-bytes-or-more!")
    monkeypatch.setenv("AGENTIC_SDLC_ATTESTATION_KEY_FILE", str(anchor_file))
    state = tmp_path / "state.json"
    stamp = "2026-08-06T12:00:00Z"
    base = ["orchestrate", "--state", str(state), "--unit", "42", "--timestamp", stamp]
    # No flag at all: the anchor still signs the written state.
    assert main([*base, "--action", "create", "--event-key", "evt-1"]) == 0
    document = json.loads(state.read_text(encoding="utf-8"))
    assert document["attestation"] == _attest_document(document, secret)
    # A caller-minted key is refused outright, not trusted for verification.
    capsys.readouterr()
    transition = [*base, "--action", "transition", "--to", "triaged", "--event-key", "evt-2"]
    assert main([*transition, "--attestation-key", str(minted)]) == 2
    assert "trust anchor" in capsys.readouterr().err
    # The same key material passed explicitly still works.
    assert main([*transition, "--attestation-key", str(anchor_file)]) == 0


def test_orchestrate_attests_state_under_a_key(tmp_path: Path) -> None:
    # With --attestation-key the written state carries an HMAC mark over
    # everything else in it (what dispatch-packet --history verifies), the
    # pipeline round-trips its own marks, and state edited outside
    # orchestrate is refused on the next read.
    from agentic_sdlc.cli import _attest_document

    secret = b"an-attestation-secret-of-at-least-32-bytes"
    key_file = tmp_path / "attestation.key"
    key_file.write_bytes(secret)
    state = tmp_path / "state.json"
    base = [
        "orchestrate",
        "--state",
        str(state),
        "--unit",
        "42",
        "--timestamp",
        "2026-08-06T12:00:00Z",
        "--attestation-key",
        str(key_file),
    ]
    assert main([*base, "--action", "create", "--event-key", "evt-1"]) == 0
    document = json.loads(state.read_text(encoding="utf-8"))
    assert document["attestation"] == _attest_document(document, secret)
    assert main([*base, "--action", "transition", "--event-key", "evt-2", "--to", "triaged"]) == 0
    document = json.loads(state.read_text(encoding="utf-8"))
    document["units"]["42"]["state"] = "merged"
    state.write_text(json.dumps(document), encoding="utf-8")
    assert main([*base, "--action", "transition", "--event-key", "evt-3", "--to", "specified"]) == 2


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


def _pricing_document() -> dict:
    return {
        "schemaVersion": 1,
        "pricing": [
            {
                "pricingId": "anthropic-payg",
                "provider": "anthropic",
                "model": "claude-opus-5",
                "billingMode": "payg",
                "version": "2026-09-01",
                "usdPerMillion": {"input": 15, "output": 75, "cacheRead": 1.5, "cacheWrite": 18.75},
            },
            {
                "pricingId": "claude-max",
                "provider": "anthropic",
                "model": "claude-opus-5",
                "billingMode": "subscription",
                "version": "2026-09",
                "plan": {"name": "Claude Max", "capacityUnit": "five-hour-window-share"},
            },
        ],
    }


def _usage_document(
    run_id: str,
    *,
    project_id: str = "alpha",
    estimate: dict | None = None,
    actual: dict | None = None,
) -> dict:
    return {
        "schemaVersion": 1,
        "usageId": f"usage-{project_id}-{run_id}",
        "workUnit": {
            "projectId": project_id,
            "workUnitId": f"github:example/{project_id}:issue:1",
            "changeRequestRef": f"example/{project_id}!12",
        },
        "stage": "implementation",
        "taskClass": "implementation",
        "runId": run_id,
        "attempt": 1,
        "actor": {"name": "claude", "kind": "agent", "model": "claude-opus-5", "worker": "gha-7"},
        "complexityClass": "medium",
        "recordedAt": "2026-09-21T10:00:00Z",
        "estimate": estimate,
        "actual": actual,
    }


def _estimate_cli(tmp_path: Path, output: Path, *extra: str) -> int:
    return main(
        [
            "estimate-usage",
            "--model",
            "claude-opus-5",
            "--stage",
            "implementation",
            "--task-class",
            "implementation",
            "--complexity",
            "medium",
            "--baseline-input-tokens",
            "1000",
            "--baseline-output-tokens",
            "500",
            "--baseline-cache-read-tokens",
            "0",
            "--baseline-cache-write-tokens",
            "0",
            "--baseline-runtime-seconds",
            "600",
            "--output",
            str(output),
            *extra,
        ]
    )


def test_usage_cli_records_estimates_then_actuals_idempotently(tmp_path: Path) -> None:
    pricing = tmp_path / "pricing.json"
    pricing.write_text(json.dumps(_pricing_document()), encoding="utf-8")
    registry = tmp_path / "projects.json"
    registry.write_text(json.dumps(_registry_document()), encoding="utf-8")
    estimate = tmp_path / "estimate.json"
    ledger = tmp_path / "usage.json"
    record = tmp_path / "record.json"
    error = tmp_path / "error.json"

    assert (
        _estimate_cli(
            tmp_path, estimate, "--pricing", str(pricing), "--pricing-id", "anthropic-payg"
        )
        == 0
    )
    estimated = json.loads(estimate.read_text(encoding="utf-8"))
    assert estimated["basis"].startswith("cold-start")
    assert estimated["monetary"]["status"] == "payg"
    assert estimated["monetary"]["billedUsd"] == 0.0525

    record.write_text(json.dumps(_usage_document("run-1", estimate=estimated)), encoding="utf-8")
    command = ["record-usage", "--ledger", str(ledger), "--registry", str(registry), "--record"]
    assert main([*command, str(record), "--error-output", str(error)]) == 0
    assert json.loads(error.read_text(encoding="utf-8"))["status"] == "unavailable"
    before = ledger.read_text(encoding="utf-8")
    assert main([*command, str(record)]) == 0
    assert ledger.read_text(encoding="utf-8") == before, "replay changes nothing"

    actual = {
        "tokens": {"input": 1500, "output": 500, "cacheRead": 0, "cacheWrite": 0},
        "monetary": {
            "status": "payg",
            "pricing": _pricing_document()["pricing"][0],
            "paygEquivalentUsd": 0.06,
            "billedUsd": 0.06,
        },
        "result": "completed",
        "startedAt": "2026-09-21T10:01:00Z",
        "finishedAt": "2026-09-21T10:11:00Z",
    }
    record.write_text(json.dumps(_usage_document("run-1", actual=actual)), encoding="utf-8")
    assert main([*command, str(record), "--error-output", str(error)]) == 0
    stored = json.loads(ledger.read_text(encoding="utf-8"))["records"]
    assert len(stored) == 1
    assert stored[0]["estimate"]["tokens"]["input"] == 1000
    assert stored[0]["actual"]["runtimeSeconds"] == 600
    computed = json.loads(error.read_text(encoding="utf-8"))
    assert computed["status"] == "available"
    assert computed["dimensions"]["input"]["percentage"] == 50.0

    # A rewritten actual and an unregistered project both fail closed, leaving the ledger intact.
    before = ledger.read_text(encoding="utf-8")
    actual["tokens"]["input"] = 1
    record.write_text(json.dumps(_usage_document("run-1", actual=actual)), encoding="utf-8")
    assert main([*command, str(record)]) == 2
    record.write_text(json.dumps(_usage_document("run-9", project_id="gamma")), encoding="utf-8")
    assert main([*command, str(record)]) == 2
    assert ledger.read_text(encoding="utf-8") == before


def test_usage_report_cli_groups_and_scopes(tmp_path: Path) -> None:
    ledger = tmp_path / "usage.json"
    record = tmp_path / "record.json"
    output = tmp_path / "report.json"
    subscription = {
        "tokens": {"input": 2000, "output": 800, "cacheRead": 0, "cacheWrite": 0},
        "monetary": {
            "status": "subscription",
            "pricing": _pricing_document()["pricing"][1],
            "planCapacityUnits": 0.2,
            "planCapacityUnit": "five-hour-window-share",
        },
        "result": "completed",
        "runtimeSeconds": 700,
    }
    for run_id, project_id, actual in (
        ("run-1", "alpha", subscription),
        ("run-2", "alpha", None),
        ("run-3", "beta", subscription),
    ):
        record.write_text(
            json.dumps(_usage_document(run_id, project_id=project_id, actual=actual)),
            encoding="utf-8",
        )
        assert main(["record-usage", "--ledger", str(ledger), "--record", str(record)]) == 0

    assert main(["usage-report", "--ledger", str(ledger), "--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["groupBy"] == ["projectId"]
    assert [bucket["key"]["projectId"] for bucket in report["buckets"]] == ["alpha", "beta"]
    alpha = report["buckets"][0]
    assert alpha["actual"]["billedUsd"] == {"value": None, "knownRecords": 0, "unknownRecords": 2}
    assert alpha["actual"]["planCapacityUnits"]["five-hour-window-share"]["value"] == 0.2
    assert alpha["actual"]["tokens"]["input"] == {
        "value": 2000,
        "knownRecords": 1,
        "unknownRecords": 1,
    }

    assert (
        main(
            [
                "usage-report",
                "--ledger",
                str(ledger),
                "--project-id",
                "beta",
                "--group-by",
                "changeRequest",
                "--group-by",
                "model",
                "--output",
                str(output),
            ]
        )
        == 0
    )
    scoped = json.loads(output.read_text(encoding="utf-8"))
    assert scoped["buckets"] == [scoped["buckets"][0]]
    assert scoped["buckets"][0]["key"] == {
        "changeRequestRef": "example/beta!12",
        "model": "claude-opus-5",
    }
    assert scoped["totals"]["records"] == 1

    assert main(["usage-report", "--ledger", str(ledger), "--group-by", "colour"]) == 2
    assert main(["usage-report", "--ledger", str(tmp_path / "absent.json")]) == 2


def test_usage_cli_calibrates_and_applies_learned_coefficients(tmp_path: Path) -> None:
    ledger = tmp_path / "usage.json"
    record = tmp_path / "record.json"
    calibration = tmp_path / "calibration.json"
    summary = tmp_path / "summary.json"
    estimate = tmp_path / "estimate.json"
    pricing = tmp_path / "pricing.json"
    pricing.write_text(json.dumps(_pricing_document()), encoding="utf-8")

    baseline = {
        "tokens": {"input": 1000, "output": 500, "cacheRead": 0, "cacheWrite": 0},
        "runtimeSeconds": 600,
    }
    for run_id, factor in (("run-1", 2), ("run-2", 2), ("run-3", 2)):
        actual = {
            "tokens": {
                "input": 1000 * factor,
                "output": 500 * factor,
                "cacheRead": 0,
                "cacheWrite": 0,
            },
            "runtimeSeconds": 600 * factor,
            "result": "completed",
        }
        record.write_text(
            json.dumps(_usage_document(run_id, estimate=baseline, actual=actual)),
            encoding="utf-8",
        )
        assert main(["record-usage", "--ledger", str(ledger), "--record", str(record)]) == 0

    command = ["calibrate-usage", "--ledger", str(ledger), "--calibration", str(calibration)]
    assert main([*command, "--min-samples", "3", "--output", str(summary)]) == 0
    first = json.loads(summary.read_text(encoding="utf-8"))
    assert first == {
        "schemaVersion": 1,
        "learnedRecords": 3,
        "observedRecords": 3,
        "ledgerRecords": 3,
    }
    assert json.loads(calibration.read_text(encoding="utf-8"))["minSamples"] == 3

    assert main([*command, "--output", str(summary)]) == 0
    assert json.loads(summary.read_text(encoding="utf-8"))["learnedRecords"] == 0

    assert (
        _estimate_cli(
            tmp_path,
            estimate,
            "--calibration",
            str(calibration),
            "--pricing",
            str(pricing),
            "--pricing-id",
            "claude-max",
            "--plan-capacity-units",
            "0.1",
        )
        == 0
    )
    learned = json.loads(estimate.read_text(encoding="utf-8"))
    assert learned["tokens"] == {
        "input": 2000,
        "output": 1000,
        "cacheRead": 0,
        "cacheWrite": 0,
        "total": 3000,
        "status": "known",
    }
    assert learned["runtimeSeconds"] == 1200
    assert learned["basis"].startswith("calibrated:")
    assert learned["monetary"]["status"] == "subscription"
    assert learned["monetary"]["billedUsd"] is None
    assert learned["monetary"]["planCapacityUnits"] == 0.1

    # An out-of-range --min-samples must never overwrite a stored calibration with a
    # document the loader would then refuse to read back.
    stored = calibration.read_text(encoding="utf-8")
    assert main([*command, "--min-samples", "0"]) == 2
    assert calibration.read_text(encoding="utf-8") == stored
    assert main([*command, "--output", str(summary)]) == 0

    assert _estimate_cli(tmp_path, estimate, "--pricing-id", "claude-max") == 2
    assert _estimate_cli(tmp_path, estimate, "--pricing", str(pricing), "--pricing-id", "nope") == 2
    assert main([*command[:-2], "--calibration", str(tmp_path / "missing" / "c.json")]) == 2


def _timed_usage_document(
    run_id: str,
    worker: str,
    queued: str,
    started: str,
    finished: str,
    *,
    project_id: str = "alpha",
    result: str = "completed",
) -> dict:
    document = _usage_document(run_id, project_id=project_id)
    document["usageId"] = f"usage-{project_id}-{run_id}"
    document["actor"]["worker"] = worker
    document["actor"]["provider"] = "anthropic"
    document["actual"] = {
        "queuedAt": queued,
        "startedAt": started,
        "finishedAt": finished,
        "result": result,
    }
    return document


def test_capacity_report_cli_measures_a_window_and_names_its_bottleneck(tmp_path: Path) -> None:
    ledger = tmp_path / "usage.json"
    record = tmp_path / "record.json"
    inputs = tmp_path / "capacity-inputs.json"
    output = tmp_path / "capacity.json"

    plan = (
        # One slot: run A holds it while run B waits behind it.
        ("run-a", "w1", "2026-09-21T10:00:00Z", "2026-09-21T10:00:00Z", "2026-09-21T10:30:00Z"),
        ("run-b", "w1", "2026-09-21T10:05:00Z", "2026-09-21T10:30:00Z", "2026-09-21T10:40:00Z"),
    )
    for run_id, worker, queued, started, finished in plan:
        record.write_text(
            json.dumps(_timed_usage_document(run_id, worker, queued, started, finished)),
            encoding="utf-8",
        )
        assert main(["record-usage", "--ledger", str(ledger), "--record", str(record)]) == 0

    inputs.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "pools": [
                    {"poolId": "hosted", "slots": 1, "workers": ["w1"], "kind": "github-hosted"}
                ],
            }
        ),
        encoding="utf-8",
    )
    command = [
        "capacity-report",
        "--usage",
        str(ledger),
        "--window-start",
        "2026-09-21T10:00:00Z",
        "--window-end",
        "2026-09-21T11:00:00Z",
    ]
    assert main([*command, "--inputs", str(inputs), "--output", str(output)]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))

    assert report["window"]["seconds"] == 3600
    assert report["concurrency"]["peak"] == 1
    assert report["concurrency"]["busySeconds"] == 1800 + 600
    assert report["queue"]["busySeconds"] == 1500
    assert report["wait"]["byCause"]["resource"]["seconds"] == 1500
    assert report["runners"]["byPool"]["hosted"]["utilization"] == round(2400 / 3600, 4)
    assert report["bottlenecks"][0]["kind"] == "runner-shortage"
    assert report["recommendations"][0]["advisory"] is True
    # Without the pool declaration nothing can say the wait was for a slot.
    assert main([*command, "--output", str(output)]) == 0
    bare = json.loads(output.read_text(encoding="utf-8"))
    assert bare["wait"]["byCause"]["unclassified"]["seconds"] == 1500
    assert bare["runners"]["byPool"] == {}
    assert bare["planCapacity"]["anthropic"]["usedFraction"] is None


def test_capacity_report_cli_fails_closed(tmp_path: Path) -> None:
    ledger = tmp_path / "usage.json"
    record = tmp_path / "record.json"
    inputs = tmp_path / "capacity-inputs.json"
    record.write_text(
        json.dumps(
            _timed_usage_document(
                "run-a",
                "w1",
                "2026-09-21T10:00:00Z",
                "2026-09-21T10:00:00Z",
                "2026-09-21T10:30:00Z",
            )
        ),
        encoding="utf-8",
    )
    assert main(["record-usage", "--ledger", str(ledger), "--record", str(record)]) == 0
    command = [
        "capacity-report",
        "--usage",
        str(ledger),
        "--window-start",
        "2026-09-21T10:00:00Z",
        "--window-end",
        "2026-09-21T11:00:00Z",
    ]

    inputs.write_text(json.dumps({"pools": [{"poolId": "hosted", "slot": 1}]}), encoding="utf-8")
    assert main([*command, "--inputs", str(inputs)]) == 2

    assert main([*command[:2], str(tmp_path / "absent.json"), *command[3:]]) == 2
    assert (
        main(
            [
                "capacity-report",
                "--usage",
                str(ledger),
                "--window-start",
                "2026-09-21T11:00:00Z",
                "--window-end",
                "2026-09-21T10:00:00Z",
            ]
        )
        == 2
    )
