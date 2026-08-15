from __future__ import annotations

from dataclasses import replace

import pytest

from agentic_sdlc.autonomy import AutonomyGateEvidence
from agentic_sdlc.lifecycle import AutonomousLifecycleDriver, StageResult
from agentic_sdlc.orchestration import Orchestrator, WorkUnitState


def _autonomy_ready() -> AutonomyGateEvidence:
    return AutonomyGateEvidence(
        forge_managed=True,
        scope_bounded=True,
        security_clear=True,
        claude_oauth_available=True,
        deterministic_verification_passed=True,
        quality_passed=True,
        independent_review_approved=True,
        secret_scan_passed=True,
        branch_protection_allows=True,
        unresolved_conversations=0,
    )


def _driver_at(state: WorkUnitState) -> tuple[AutonomousLifecycleDriver, Orchestrator, str]:
    orchestrator = Orchestrator()
    unit_id = "github:atulg4/example:issue:51"
    orchestrator.create_unit(unit_id, event_key="create", timestamp="2026-08-15T06:00:00Z")
    path = [
        WorkUnitState.TRIAGED,
        WorkUnitState.SPECIFIED,
        WorkUnitState.PLANNED,
        WorkUnitState.APPROVED,
        WorkUnitState.DISPATCHED,
        WorkUnitState.IMPLEMENTING,
        WorkUnitState.VERIFYING,
        WorkUnitState.REVIEWING,
        WorkUnitState.READY_FOR_HUMAN_MERGE,
    ]
    for index, target in enumerate(path, start=1):
        if orchestrator.get(unit_id).state is state:
            break
        orchestrator.transition(
            unit_id,
            target,
            actor="test-system",
            actor_kind="system",
            event_key=f"seed-{index}",
            timestamp="2026-08-15T06:00:00Z",
        )
    assert orchestrator.get(unit_id).state is state
    return AutonomousLifecycleDriver(orchestrator), orchestrator, unit_id


def test_triaged_work_advances_to_specified_without_manual_label() -> None:
    driver, orchestrator, unit_id = _driver_at(WorkUnitState.TRIAGED)

    result = driver.advance(
        unit_id,
        timestamp="2026-08-15T06:01:00Z",
        event_key="advance-1",
        autonomy=_autonomy_ready(),
    )

    assert result.advanced is True
    assert result.to_state is WorkUnitState.SPECIFIED
    assert orchestrator.get(unit_id).state is WorkUnitState.SPECIFIED


def test_planning_waits_until_a_result_exists() -> None:
    driver, orchestrator, unit_id = _driver_at(WorkUnitState.SPECIFIED)

    result = driver.advance(
        unit_id,
        timestamp="2026-08-15T06:01:00Z",
        event_key="advance-plan",
        autonomy=_autonomy_ready(),
    )

    assert result.advanced is False
    assert result.reason == "waiting for planning result"
    assert orchestrator.get(unit_id).state is WorkUnitState.SPECIFIED


def test_completed_plan_advances_to_planned() -> None:
    driver, orchestrator, unit_id = _driver_at(WorkUnitState.SPECIFIED)

    result = driver.advance(
        unit_id,
        timestamp="2026-08-15T06:01:00Z",
        event_key="advance-plan-pass",
        autonomy=_autonomy_ready(),
        plan=StageResult.PASSED,
    )

    assert result.to_state is WorkUnitState.PLANNED
    assert orchestrator.get(unit_id).state is WorkUnitState.PLANNED


def test_planned_work_is_system_approved_only_when_autonomy_policy_passes() -> None:
    driver, orchestrator, unit_id = _driver_at(WorkUnitState.PLANNED)

    result = driver.advance(
        unit_id,
        timestamp="2026-08-15T06:01:00Z",
        event_key="advance-policy",
        autonomy=_autonomy_ready(),
    )

    assert result.to_state is WorkUnitState.APPROVED
    transition = orchestrator.get(unit_id).transitions[-1]
    assert transition.actor == "forge-lifecycle"
    assert transition.actor_kind == "system"


@pytest.mark.parametrize(
    "unsafe",
    [
        replace(_autonomy_ready(), forge_managed=False),
        replace(_autonomy_ready(), security_clear=False),
        replace(_autonomy_ready(), claude_oauth_available=False),
        replace(_autonomy_ready(), high_risk=True),
        replace(_autonomy_ready(), deployment_requested=True),
        replace(_autonomy_ready(), broker_access_requested=True),
    ],
)
def test_planned_work_fails_closed_when_autonomy_policy_rejects(
    unsafe: AutonomyGateEvidence,
) -> None:
    driver, orchestrator, unit_id = _driver_at(WorkUnitState.PLANNED)

    result = driver.advance(
        unit_id,
        timestamp="2026-08-15T06:01:00Z",
        event_key="advance-reject",
        autonomy=unsafe,
    )

    assert result.to_state is WorkUnitState.BLOCKED
    assert result.reason
    assert orchestrator.get(unit_id).state is WorkUnitState.BLOCKED


def test_implementation_must_pass_before_verification_starts() -> None:
    driver, orchestrator, unit_id = _driver_at(WorkUnitState.IMPLEMENTING)

    waiting = driver.advance(
        unit_id,
        timestamp="2026-08-15T06:01:00Z",
        event_key="impl-wait",
        autonomy=_autonomy_ready(),
    )
    assert waiting.advanced is False

    passed = driver.advance(
        unit_id,
        timestamp="2026-08-15T06:02:00Z",
        event_key="impl-pass",
        autonomy=_autonomy_ready(),
        implementation=StageResult.PASSED,
    )
    assert passed.to_state is WorkUnitState.VERIFYING
    assert orchestrator.get(unit_id).state is WorkUnitState.VERIFYING


def test_failed_deterministic_verification_is_terminal() -> None:
    driver, orchestrator, unit_id = _driver_at(WorkUnitState.VERIFYING)

    result = driver.advance(
        unit_id,
        timestamp="2026-08-15T06:01:00Z",
        event_key="verify-fail",
        autonomy=_autonomy_ready(),
        verification=StageResult.FAILED,
    )

    assert result.to_state is WorkUnitState.FAILED
    assert orchestrator.get(unit_id).state is WorkUnitState.FAILED


def test_review_changes_enter_bounded_repair_path() -> None:
    driver, orchestrator, unit_id = _driver_at(WorkUnitState.REVIEWING)

    requested = driver.advance(
        unit_id,
        timestamp="2026-08-15T06:01:00Z",
        event_key="review-changes",
        autonomy=_autonomy_ready(),
        review=StageResult.CHANGES_REQUESTED,
    )
    assert requested.to_state is WorkUnitState.REPAIR_NEEDED

    repairing = driver.advance(
        unit_id,
        timestamp="2026-08-15T06:02:00Z",
        event_key="repair-start",
        autonomy=_autonomy_ready(),
    )
    assert repairing.to_state is WorkUnitState.REPAIRING

    repaired = driver.advance(
        unit_id,
        timestamp="2026-08-15T06:03:00Z",
        event_key="repair-pass",
        autonomy=_autonomy_ready(),
        repair=StageResult.PASSED,
    )
    assert repaired.to_state is WorkUnitState.RE_REVIEWING
    assert orchestrator.get(unit_id).state is WorkUnitState.RE_REVIEWING


def test_review_approval_stops_at_protected_merge_executor_boundary() -> None:
    driver, orchestrator, unit_id = _driver_at(WorkUnitState.REVIEWING)

    approved = driver.advance(
        unit_id,
        timestamp="2026-08-15T06:01:00Z",
        event_key="review-pass",
        autonomy=_autonomy_ready(),
        review=StageResult.PASSED,
    )
    assert approved.to_state is WorkUnitState.READY_FOR_HUMAN_MERGE

    merge_ready = driver.advance(
        unit_id,
        timestamp="2026-08-15T06:02:00Z",
        event_key="merge-ready",
        autonomy=_autonomy_ready(),
    )
    assert merge_ready.advanced is False
    assert "protected repository merge executor required" in merge_ready.reason
    assert orchestrator.get(unit_id).state is WorkUnitState.READY_FOR_HUMAN_MERGE


def test_merge_ready_work_blocks_if_any_required_merge_gate_is_missing() -> None:
    driver, orchestrator, unit_id = _driver_at(WorkUnitState.READY_FOR_HUMAN_MERGE)
    unsafe = replace(_autonomy_ready(), secret_scan_passed=False)

    result = driver.advance(
        unit_id,
        timestamp="2026-08-15T06:01:00Z",
        event_key="merge-block",
        autonomy=unsafe,
    )

    assert result.to_state is WorkUnitState.BLOCKED
    assert orchestrator.get(unit_id).state is WorkUnitState.BLOCKED


def test_replayed_transition_event_is_idempotent() -> None:
    driver, orchestrator, unit_id = _driver_at(WorkUnitState.TRIAGED)

    first = driver.advance(
        unit_id,
        timestamp="2026-08-15T06:01:00Z",
        event_key="same-event",
        autonomy=_autonomy_ready(),
    )
    second = driver.advance(
        unit_id,
        timestamp="2026-08-15T06:01:00Z",
        event_key="same-event",
        autonomy=_autonomy_ready(),
        plan=StageResult.PASSED,
    )

    assert first.to_state is WorkUnitState.SPECIFIED
    assert second.advanced is False
    assert orchestrator.get(unit_id).state is WorkUnitState.SPECIFIED
