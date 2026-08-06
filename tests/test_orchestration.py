from __future__ import annotations

from pathlib import Path

import pytest

from agentic_sdlc.missions import AgentProfile, load_registry
from agentic_sdlc.models import RiskLevel
from agentic_sdlc.orchestration import (
    STATUS_MARKER,
    OrchestrationError,
    Orchestrator,
    WorkUnitState,
)
from agentic_sdlc.policy import load_policy

T = "2026-08-06T12:00:00Z"


def _envelope(agent_id: str = "codex-1", mission_id: str = "implementation-worker") -> dict:
    return {
        "missionId": mission_id,
        "missionVersion": "1.0.0",
        "agentId": agent_id,
        "adapter": "codex",
        "adapterVersion": "1.0.0",
        "provider": "openai",
        "model": "gpt-5",
        "promptSha256": "a" * 64,
        "envelopeSha256": "b" * 64,
        "workRef": "example/project#1",
        "inputRefs": ["task-spec"],
    }


def _review_envelope(agent_id: str = "claude-1") -> dict:
    return {**_envelope(agent_id, "code-reviewer"), "envelopeSha256": "c" * 64}


def _advance_to_dispatched(engine: Orchestrator, unit_id: str = "u1", **create_kwargs) -> None:
    engine.create_unit(unit_id, event_key=f"{unit_id}-created", timestamp=T, **create_kwargs)
    steps = (
        WorkUnitState.TRIAGED,
        WorkUnitState.SPECIFIED,
        WorkUnitState.PLANNED,
        WorkUnitState.APPROVAL_PENDING,
        WorkUnitState.APPROVED,
        WorkUnitState.DISPATCHED,
    )
    for index, state in enumerate(steps):
        engine.transition(
            unit_id,
            state,
            actor="owner" if state is WorkUnitState.APPROVED else "system",
            actor_kind="human" if state is WorkUnitState.APPROVED else "system",
            event_key=f"{unit_id}-step-{index}",
            timestamp=T,
        )


def _advance_to_reviewing(engine: Orchestrator, unit_id: str = "u1") -> str:
    """Returns the review run id."""
    _advance_to_dispatched(engine, unit_id)
    run = engine.start_run(
        unit_id, "implementation", _envelope(), event_key=f"{unit_id}-impl", timestamp=T
    )
    engine.finish_run(unit_id, run.run_id, result="succeeded", timestamp=T, commit_sha="d" * 40)
    engine.transition(
        unit_id,
        WorkUnitState.VERIFYING,
        actor="system",
        actor_kind="system",
        event_key=f"{unit_id}-verify",
        timestamp=T,
    )
    engine.record_verification(unit_id, passed=True, event_key=f"{unit_id}-verified", timestamp=T)
    review = engine.start_run(
        unit_id, "review", _review_envelope(), event_key=f"{unit_id}-review", timestamp=T
    )
    return review.run_id


def test_duplicate_events_do_not_duplicate_units_or_runs() -> None:
    engine = Orchestrator()
    first = engine.create_unit("u1", event_key="evt-1", timestamp=T)
    second = engine.create_unit("u1", event_key="evt-1-retry", timestamp=T)
    assert first is second

    _advance_to_dispatched(engine := Orchestrator(), "u1")
    run_a = engine.start_run("u1", "implementation", _envelope(), event_key="impl-1", timestamp=T)
    run_b = engine.start_run("u1", "implementation", _envelope(), event_key="impl-1", timestamp=T)
    assert run_a.run_id == run_b.run_id
    assert len(engine.get("u1").runs) == 1

    # Replayed transitions are idempotent no-ops.
    state_before = engine.get("u1").state
    engine.transition(
        "u1",
        WorkUnitState.VERIFYING,
        actor="s",
        actor_kind="system",
        event_key="impl-1",
        timestamp=T,
    )
    assert engine.get("u1").state is state_before


def test_invalid_transitions_fail_closed() -> None:
    engine = Orchestrator()
    engine.create_unit("u1", event_key="evt-1", timestamp=T)
    with pytest.raises(OrchestrationError, match="invalid transition"):
        engine.transition(
            "u1",
            WorkUnitState.MERGED,
            actor="owner",
            actor_kind="human",
            event_key="evt-2",
            timestamp=T,
        )
    with pytest.raises(OrchestrationError, match="unknown work unit"):
        engine.transition(
            "ghost",
            WorkUnitState.TRIAGED,
            actor="s",
            actor_kind="system",
            event_key="evt-3",
            timestamp=T,
        )


def test_concurrent_dispatch_for_same_group_is_serialized() -> None:
    engine = Orchestrator()
    _advance_to_dispatched(engine, "u1", concurrency_group="payments")
    _advance_to_dispatched(engine, "u2", concurrency_group="payments")
    engine.start_run("u1", "implementation", _envelope(), event_key="impl-1", timestamp=T)
    with pytest.raises(OrchestrationError, match="serialized"):
        engine.start_run("u2", "implementation", _envelope(), event_key="impl-2", timestamp=T)
    # A different concurrency group is unaffected.
    _advance_to_dispatched(engine, "u3", concurrency_group="docs")
    engine.start_run("u3", "implementation", _envelope(), event_key="impl-3", timestamp=T)


def test_review_findings_trigger_one_bounded_repair_cycle() -> None:
    engine = Orchestrator(max_repair_cycles=2)
    review_id = _advance_to_reviewing(engine, "u1")
    engine.record_review("u1", review_id, findings=2, event_key="review-1", timestamp=T)
    assert engine.get("u1").state is WorkUnitState.REPAIR_NEEDED

    repair = engine.start_run(
        "u1", "repair", _envelope("codex-1", "repair-agent"), event_key="repair-1", timestamp=T
    )
    assert engine.get("u1").state is WorkUnitState.REPAIRING
    assert engine.get("u1").repair_count == 1
    engine.finish_run("u1", repair.run_id, result="succeeded", timestamp=T, commit_sha="e" * 40)
    engine.record_repair_complete("u1", event_key="repair-1-done", timestamp=T, actor="codex-1")
    assert engine.get("u1").state is WorkUnitState.RE_REVIEWING


def test_passing_review_does_not_trigger_repair() -> None:
    engine = Orchestrator()
    review_id = _advance_to_reviewing(engine, "u1")
    engine.record_review("u1", review_id, findings=0, event_key="review-1", timestamp=T)
    unit = engine.get("u1")
    assert unit.state is WorkUnitState.READY_FOR_HUMAN_MERGE
    assert unit.repair_count == 0


def test_repair_exhaustion_escalates_instead_of_looping() -> None:
    engine = Orchestrator(max_repair_cycles=1)
    review_id = _advance_to_reviewing(engine, "u1")
    engine.record_review("u1", review_id, findings=1, event_key="review-1", timestamp=T)
    repair = engine.start_run(
        "u1", "repair", _envelope("codex-1", "repair-agent"), event_key="repair-1", timestamp=T
    )
    engine.finish_run("u1", repair.run_id, result="succeeded", timestamp=T)
    engine.record_repair_complete("u1", event_key="repair-done", timestamp=T, actor="codex-1")
    fresh = engine.start_run("u1", "review", _review_envelope(), event_key="review-2", timestamp=T)
    engine.record_review("u1", fresh.run_id, findings=1, event_key="review-2-result", timestamp=T)
    unit = engine.get("u1")
    assert unit.state is WorkUnitState.BLOCKED
    assert "escalated" in unit.transitions[-1].reason
    # And no further repair can start.
    with pytest.raises(OrchestrationError, match="invalid transition|repair requires"):
        engine.start_run(
            "u1", "repair", _envelope("codex-1", "repair-agent"), event_key="repair-2", timestamp=T
        )


def test_every_repair_requires_a_fresh_independent_review() -> None:
    engine = Orchestrator(max_repair_cycles=2)
    stale_review_id = _advance_to_reviewing(engine, "u1")
    engine.record_review("u1", stale_review_id, findings=1, event_key="review-1", timestamp=T)
    repair = engine.start_run(
        "u1", "repair", _envelope("codex-1", "repair-agent"), event_key="repair-1", timestamp=T
    )
    engine.finish_run("u1", repair.run_id, result="succeeded", timestamp=T)
    engine.record_repair_complete("u1", event_key="repair-done", timestamp=T, actor="codex-1")
    # Re-using the pre-repair review to approve is rejected.
    with pytest.raises(OrchestrationError, match="fresh independent review"):
        engine.record_review(
            "u1", stale_review_id, findings=0, event_key="review-stale", timestamp=T
        )
    fresh = engine.start_run("u1", "review", _review_envelope(), event_key="review-2", timestamp=T)
    engine.record_review("u1", fresh.run_id, findings=0, event_key="review-2-result", timestamp=T)
    assert engine.get("u1").state is WorkUnitState.READY_FOR_HUMAN_MERGE


def test_superseded_and_cancelled_work_cannot_publish() -> None:
    engine = Orchestrator()
    _advance_to_dispatched(engine, "u1")
    engine.supersede(
        "u1", by_unit_id="u2", actor="owner", actor_kind="human", event_key="sup-1", timestamp=T
    )
    unit = engine.get("u1")
    assert unit.state is WorkUnitState.SUPERSEDED
    assert unit.superseded_by == "u2"
    with pytest.raises(OrchestrationError, match="terminal"):
        engine.transition(
            "u1",
            WorkUnitState.READY_FOR_HUMAN_MERGE,
            actor="agent",
            actor_kind="agent",
            event_key="pub-1",
            timestamp=T,
        )
    with pytest.raises(OrchestrationError, match="implementation requires state dispatched"):
        engine.start_run("u1", "implementation", _envelope(), event_key="impl-9", timestamp=T)

    engine.create_unit("u3", event_key="evt-u3", timestamp=T)
    engine.cancel("u3", actor="owner", actor_kind="human", event_key="cancel-1", timestamp=T)
    with pytest.raises(OrchestrationError, match="terminal"):
        engine.transition(
            "u3",
            WorkUnitState.TRIAGED,
            actor="s",
            actor_kind="system",
            event_key="evt-4",
            timestamp=T,
        )


def test_human_merge_remains_mandatory() -> None:
    engine = Orchestrator()
    review_id = _advance_to_reviewing(engine, "u1")
    engine.record_review("u1", review_id, findings=0, event_key="review-1", timestamp=T)
    with pytest.raises(OrchestrationError, match="human actor"):
        engine.transition(
            "u1",
            WorkUnitState.MERGED,
            actor="codex-1",
            actor_kind="agent",
            event_key="merge-1",
            timestamp=T,
        )
    engine.transition(
        "u1",
        WorkUnitState.MERGED,
        actor="atulg4",
        actor_kind="human",
        event_key="merge-2",
        timestamp=T,
    )
    assert engine.get("u1").state is WorkUnitState.MERGED
    with pytest.raises(OrchestrationError, match="cannot be disabled"):
        Orchestrator(human_merge_required=False)


def test_manual_override_cannot_mint_merge_authority() -> None:
    engine = Orchestrator()
    _advance_to_dispatched(engine, "u1")
    engine.manual_override(
        "u1",
        WorkUnitState.TRIAGED,
        actor="atulg4",
        event_key="override-1",
        timestamp=T,
        reason="re-triage after scope change",
    )
    unit = engine.get("u1")
    assert unit.state is WorkUnitState.TRIAGED
    assert unit.transitions[-1].reason.startswith("manual-override")
    with pytest.raises(OrchestrationError, match="cannot mint merge authority"):
        engine.manual_override(
            "u1",
            WorkUnitState.MERGED,
            actor="atulg4",
            event_key="override-2",
            timestamp=T,
            reason="ship it",
        )
    with pytest.raises(OrchestrationError, match="must state a reason"):
        engine.manual_override(
            "u1", WorkUnitState.PLANNED, actor="atulg4", event_key="o3", timestamp=T, reason=" "
        )


def test_dependency_blocking_prevents_dispatch() -> None:
    engine = Orchestrator()
    engine.create_unit("dep", event_key="evt-dep", timestamp=T)
    _advance_to_dispatched_partial = (
        WorkUnitState.TRIAGED,
        WorkUnitState.SPECIFIED,
        WorkUnitState.PLANNED,
        WorkUnitState.APPROVED,
    )
    engine.create_unit("u1", event_key="evt-u1", timestamp=T, depends_on=("dep",))
    for index, state in enumerate(_advance_to_dispatched_partial):
        engine.transition(
            "u1",
            state,
            actor="owner",
            actor_kind="human",
            event_key=f"u1-{index}",
            timestamp=T,
        )
    with pytest.raises(OrchestrationError, match="dependency-blocked by: dep"):
        engine.transition(
            "u1",
            WorkUnitState.DISPATCHED,
            actor="system",
            actor_kind="system",
            event_key="u1-dispatch",
            timestamp=T,
        )


def test_repair_dispatch_routes_to_eligible_independent_agents(policy_file: Path) -> None:
    registry = load_registry(None, load_policy(policy_file))
    engine = Orchestrator(registry=registry)
    review_id = _advance_to_reviewing(engine, "u1")
    engine.record_review("u1", review_id, findings=1, event_key="review-1", timestamp=T)

    implementer = AgentProfile(
        agent_id="codex-1",
        adapter="codex",
        adapter_version="1.0.0",
        provider="openai",
        model="gpt-5",
        capabilities=("edit-code", "author-tests", "run-commands", "repair"),
    )
    reviewer = AgentProfile(
        agent_id="claude-1",
        adapter="claude",
        adapter_version="1.0.0",
        provider="anthropic",
        model="claude-opus-5",
        capabilities=("review-code", "review-security"),
        max_risk=RiskLevel.HIGH,
    )
    # The original implementer may repair its own work.
    assert engine.select_repair_agent("u1", (implementer, reviewer)).agent_id == "codex-1"
    # But the implementer can never review it, even as a fallback.
    both = AgentProfile(
        agent_id="codex-1",
        adapter="codex",
        adapter_version="1.0.0",
        provider="openai",
        model="gpt-5",
        capabilities=("edit-code", "author-tests", "run-commands", "review-code"),
    )
    selected = engine.select_review_agent("u1", (both, reviewer))
    assert selected.agent_id == "claude-1"


def test_status_document_is_single_and_updateable() -> None:
    engine = Orchestrator()
    review_id = _advance_to_reviewing(engine, "u1")
    first = engine.status_document("u1")
    assert first.startswith(STATUS_MARKER)
    assert first.count(STATUS_MARKER) == 1
    engine.record_review("u1", review_id, findings=0, event_key="review-1", timestamp=T)
    second = engine.status_document("u1")
    assert second.count(STATUS_MARKER) == 1
    assert "ready-for-human-merge" in second
    assert "code-reviewer@1.0.0" in second


def test_state_round_trips_through_durable_documents() -> None:
    engine = Orchestrator(max_repair_cycles=1)
    review_id = _advance_to_reviewing(engine, "u1")
    engine.record_review("u1", review_id, findings=1, event_key="review-1", timestamp=T)
    document = engine.as_dict()

    restored = Orchestrator.from_dict(document)
    assert restored.as_dict() == document
    unit = restored.get("u1")
    assert unit.state is WorkUnitState.REPAIR_NEEDED
    assert len(unit.runs) == 2
    assert unit.runs[0].envelope_sha256 == "b" * 64
    # The restored engine keeps enforcing the same rules.
    repair = restored.start_run(
        "u1", "repair", _envelope("codex-1", "repair-agent"), event_key="repair-1", timestamp=T
    )
    assert repair.repair_cycle == 1

    with pytest.raises(OrchestrationError, match="unsupported orchestration state"):
        Orchestrator.from_dict({"schemaVersion": 99})


def test_run_records_pin_identity_and_artifacts() -> None:
    engine = Orchestrator()
    _advance_to_dispatched(engine, "u1")
    run = engine.start_run("u1", "implementation", _envelope(), event_key="impl-1", timestamp=T)
    finished = engine.finish_run(
        "u1",
        run.run_id,
        result="succeeded",
        timestamp=T,
        commit_sha="f" * 40,
        output_artifacts={"patch": "sha256:aaa", "patch-manifest": "sha256:bbb"},
    )
    document = finished.as_dict()
    assert document["missionId"] == "implementation-worker"
    assert document["missionVersion"] == "1.0.0"
    assert document["promptSha256"] == "a" * 64
    assert document["commitSha"] == "f" * 40
    assert document["outputArtifacts"] == {
        "patch": "sha256:aaa",
        "patch-manifest": "sha256:bbb",
    }
    # Envelopes missing pinned identity fields are rejected.
    incomplete = {key: value for key, value in _envelope().items() if key != "promptSha256"}
    with pytest.raises(OrchestrationError, match="missing: promptSha256"):
        engine.start_run("u1", "verification", incomplete, event_key="v-1", timestamp=T)
