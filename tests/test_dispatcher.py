from __future__ import annotations

import pytest

from agentic_sdlc.dispatcher import AutonomousIntakeDispatcher, DispatchError, event_fingerprint
from agentic_sdlc.models import WorkEvent, WorkKind
from agentic_sdlc.orchestration import OrchestrationError, Orchestrator, WorkUnitState

T = "2026-08-15T03:20:00Z"


def _event(*, labels: tuple[str, ...] = ("forge-managed",), action: str = "opened") -> WorkEvent:
    return WorkEvent(
        provider="github",
        kind=WorkKind.ISSUE,
        action=action,
        repository="atulg4/example",
        number=42,
        title="Build the thing",
        body="untrusted issue body must not influence control-plane routing",
        labels=labels,
        actor="atulg4",
        raw={"issue": {"body": "contains arbitrary untrusted text"}},
    )


def test_forge_managed_event_enters_triaged_state_without_manual_label_chain() -> None:
    engine = Orchestrator()
    decision = AutonomousIntakeDispatcher(engine).dispatch(_event(), timestamp=T)

    assert decision.accepted is True
    assert decision.unit_id == "github:atulg4/example:issue:42"
    assert decision.state == WorkUnitState.TRIAGED.value
    unit = engine.get(decision.unit_id)
    assert unit.state is WorkUnitState.TRIAGED
    assert unit.concurrency_group == "repo:atulg4/example"
    assert len(unit.transitions) == 1
    assert unit.transitions[0].actor_kind == "system"


def test_duplicate_delivery_is_idempotent() -> None:
    engine = Orchestrator()
    dispatcher = AutonomousIntakeDispatcher(engine)
    first = dispatcher.dispatch(_event(), timestamp=T)
    second = dispatcher.dispatch(_event(), timestamp=T)

    assert first == second
    unit = engine.get(first.unit_id)
    assert len(unit.transitions) == 1
    assert len(unit.event_keys) == 2  # creation and triage keys only


def test_later_event_for_same_work_item_never_resets_lifecycle() -> None:
    engine = Orchestrator()
    dispatcher = AutonomousIntakeDispatcher(engine)
    opened = dispatcher.dispatch(_event(action="opened"), timestamp=T)
    engine.transition(
        opened.unit_id,
        WorkUnitState.SPECIFIED,
        actor="system",
        actor_kind="system",
        event_key="specified",
        timestamp=T,
    )

    updated = dispatcher.dispatch(_event(action="edited"), timestamp=T)
    assert updated.accepted is True
    assert updated.state == WorkUnitState.SPECIFIED.value
    assert engine.get(opened.unit_id).state is WorkUnitState.SPECIFIED


def test_unmanaged_work_is_ignored_without_creating_state() -> None:
    engine = Orchestrator()
    decision = AutonomousIntakeDispatcher(engine).dispatch(_event(labels=("bug",)), timestamp=T)

    assert decision.accepted is False
    assert decision.state == "ignored"
    with pytest.raises(OrchestrationError, match="unknown work unit"):
        engine.get("github:atulg4/example:issue:42")


def test_pause_and_security_labels_fail_closed_before_state_creation() -> None:
    for label in ("forge-paused", "security-review-required"):
        engine = Orchestrator()
        decision = AutonomousIntakeDispatcher(engine).dispatch(
            _event(labels=("forge-managed", label)), timestamp=T
        )
        assert decision.accepted is False
        assert decision.state == "blocked"
        assert label in decision.reason
        with pytest.raises(OrchestrationError, match="unknown work unit"):
            engine.get("github:atulg4/example:issue:42")


def test_malformed_managed_event_fails_closed() -> None:
    engine = Orchestrator()
    malformed = WorkEvent(
        provider="github",
        kind=WorkKind.ISSUE,
        action="opened",
        repository="",
        number=None,
        title="bad",
        body="",
        labels=("forge-managed",),
        actor="",
        raw={},
    )
    with pytest.raises(DispatchError, match="repository is required"):
        AutonomousIntakeDispatcher(engine).dispatch(malformed, timestamp=T)


def test_event_fingerprint_excludes_raw_payload_and_is_stable() -> None:
    first = _event()
    second = WorkEvent(**{**first.__dict__, "raw": {"secret": "must-not-affect-idempotency"}})
    assert event_fingerprint(first) == event_fingerprint(second)
    assert event_fingerprint(first).startswith("forge-event:")
    assert "secret" not in event_fingerprint(second)


def test_material_event_change_gets_a_distinct_idempotency_key() -> None:
    assert event_fingerprint(_event(action="opened")) != event_fingerprint(_event(action="edited"))
