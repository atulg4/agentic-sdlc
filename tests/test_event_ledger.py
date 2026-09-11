from __future__ import annotations

import json
from typing import Any

import pytest

from agentic_sdlc.event_ledger import (
    EVENT_SCHEMA_TYPES,
    EVENT_SCHEMA_VERSION,
    FACTORY_TRANSITIONS,
    LEDGER_SCHEMA_VERSION,
    STAGE_FOR_STATE,
    EventActor,
    EventLedger,
    EventProvenance,
    FactoryState,
    LedgerError,
    LifecycleEvent,
    LifecycleStage,
    WorkUnitRef,
    events_from_orchestrator_document,
    factory_state,
    load_lifecycle_event,
    secret_bearing_event_fields,
)
from agentic_sdlc.orchestration import Orchestrator, TransitionRecord, WorkUnitState
from agentic_sdlc.project_registry import load_project_registry

T = "2026-09-04T09:00:00Z"


def _registry_document(*project_ids: str) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "projects": [
            {
                "projectId": project_id,
                "displayName": project_id.title(),
                "repositories": [{"provider": "github", "identifier": f"example/{project_id}"}],
            }
            for project_id in project_ids
        ],
    }


def _event(
    sequence: int,
    state: FactoryState,
    *,
    project_id: str = "alpha",
    work_unit_id: str = "github:example/alpha:issue:1",
    stage: LifecycleStage | None = None,
    actor: str = "forge-lifecycle",
    actor_kind: str = "system",
    activity: str = "",
    reason: str = "",
    timestamp: str = T,
    **work_unit: Any,
) -> LifecycleEvent:
    return LifecycleEvent(
        event_id=f"{project_id}:{work_unit_id}:{sequence:04d}",
        idempotency_key=f"{work_unit_id}:{sequence:04d}",
        work_unit=WorkUnitRef(project_id=project_id, work_unit_id=work_unit_id, **work_unit),
        stage=stage or STAGE_FOR_STATE[state],
        state=state,
        occurred_at=timestamp,
        actor=EventActor(name=actor, kind=actor_kind),
        provenance=EventProvenance(source="forge-ci"),
        activity=activity,
        reason=reason,
    )


_NORMAL_PATH = (
    FactoryState.INTAKE,
    FactoryState.TRIAGED,
    FactoryState.SPECIFIED,
    FactoryState.PLANNED,
    FactoryState.APPROVED,
    FactoryState.DISPATCHED,
    FactoryState.IMPLEMENTING,
    FactoryState.VERIFYING,
    FactoryState.REVIEWING,
    FactoryState.READY_FOR_HUMAN_MERGE,
)


def _walk(
    ledger: EventLedger,
    states: tuple[FactoryState, ...],
    *,
    project_id: str = "alpha",
    work_unit_id: str = "github:example/alpha:issue:1",
    start: int = 0,
) -> None:
    for offset, state in enumerate(states):
        human = state in {FactoryState.MERGED, FactoryState.APPROVED}
        ledger.append(
            _event(
                start + offset,
                state,
                project_id=project_id,
                work_unit_id=work_unit_id,
                actor="owner" if human else "forge-lifecycle",
                actor_kind="human" if human else "system",
            )
        )


# -- schema, versioning, and migration ------------------------------------


def test_every_orchestration_state_is_a_factory_state() -> None:
    for state in WorkUnitState:
        assert factory_state(state).value == state.value
    assert set(STAGE_FOR_STATE) == set(FactoryState)
    assert set(FACTORY_TRANSITIONS) == set(FactoryState)


def test_event_round_trips_through_its_document() -> None:
    event = _event(
        1,
        FactoryState.IMPLEMENTING,
        activity="Claude editing src/agentic_sdlc/cli.py on runner gha-7",
        repository="example/alpha",
        issue_ref="example/alpha#1",
        change_request_ref="example/alpha!12",
        workflow_ref=".github/workflows/reusable-implement.yml",
        run_ref="31823903027",
    )

    document = event.as_dict()
    reloaded = load_lifecycle_event(document)

    assert reloaded == event
    assert reloaded.as_dict() == document
    assert document["schemaVersion"] == EVENT_SCHEMA_VERSION


def test_version_zero_documents_migrate_from_the_orchestration_vocabulary() -> None:
    legacy = _event(1, FactoryState.TRIAGED).as_dict()
    legacy.pop("schemaVersion")
    legacy["eventKey"] = legacy.pop("idempotencyKey")
    legacy["workUnit"]["unitId"] = legacy["workUnit"].pop("workUnitId")

    migrated = load_lifecycle_event(legacy)

    assert migrated.schema_version == EVENT_SCHEMA_VERSION
    assert migrated.work_unit_id == "github:example/alpha:issue:1"
    assert migrated.idempotency_key == "github:example/alpha:issue:1:0001"


def test_future_schema_versions_fail_closed() -> None:
    document = _event(1, FactoryState.TRIAGED).as_dict()
    document["schemaVersion"] = EVENT_SCHEMA_VERSION + 1

    with pytest.raises(LedgerError, match="unsupported lifecycle event schemaVersion"):
        load_lifecycle_event(document)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda doc: doc.update(surprise=1), "unknown keys"),
        (lambda doc: doc.update(apiToken="x"), "must not carry credentials or secrets"),
        (lambda doc: doc.update(stage="teleport"), "unknown stage"),
        (lambda doc: doc.update(state="orbiting"), "unknown state"),
        (lambda doc: doc.update(occurredAt="yesterday"), "must be an RFC 3339 timestamp"),
        (lambda doc: doc.update(eventId=""), "eventId is required"),
        (lambda doc: doc.update(idempotencyKey=""), "idempotencyKey is required"),
        (lambda doc: doc["actor"].update(kind="robot"), "unknown actor kind"),
        (lambda doc: doc["actor"].update(bearerToken="x"), "credentials or secrets"),
        (lambda doc: doc["provenance"].update(source=""), "source is required"),
        (lambda doc: doc["workUnit"].update(projectId=""), "projectId is required"),
    ],
)
def test_event_loader_fails_closed(mutate: Any, message: str) -> None:
    document = _event(1, FactoryState.TRIAGED).as_dict()
    mutate(document)

    with pytest.raises(LedgerError, match=message):
        load_lifecycle_event(document)


def test_ledger_document_round_trips_and_rejects_unknown_versions() -> None:
    ledger = EventLedger()
    _walk(ledger, _NORMAL_PATH)

    document = ledger.as_dict()
    reloaded = EventLedger.from_dict(document)

    assert reloaded.as_dict() == document
    assert document["schemaVersion"] == LEDGER_SCHEMA_VERSION
    assert len(reloaded) == len(_NORMAL_PATH)

    document["schemaVersion"] = 99
    with pytest.raises(LedgerError, match="unsupported event ledger schemaVersion"):
        EventLedger.from_dict(document)


# -- idempotency -----------------------------------------------------------


def test_replaying_a_duplicate_event_is_a_no_op() -> None:
    ledger = EventLedger()
    _walk(ledger, _NORMAL_PATH)
    before = ledger.as_dict()

    for event in list(ledger.events):
        assert ledger.append(event) is event

    assert ledger.as_dict() == before
    assert ledger.projection("alpha", "github:example/alpha:issue:1").state is (
        FactoryState.READY_FOR_HUMAN_MERGE
    )


def test_reusing_an_idempotency_key_with_a_different_payload_fails_closed() -> None:
    ledger = EventLedger()
    _walk(ledger, (FactoryState.INTAKE,))
    conflicting = _event(0, FactoryState.INTAKE, reason="rewritten history")

    with pytest.raises(LedgerError, match="already recorded .* with a different payload"):
        ledger.append(conflicting)


def test_reusing_an_event_id_under_another_key_fails_closed() -> None:
    ledger = EventLedger()
    _walk(ledger, (FactoryState.INTAKE,))
    original = ledger.events[0]

    with pytest.raises(LedgerError, match="already recorded under a different"):
        ledger.append(
            LifecycleEvent(
                event_id=original.event_id,
                idempotency_key="a-different-key",
                work_unit=original.work_unit,
                stage=original.stage,
                state=original.state,
                occurred_at=original.occurred_at,
                actor=original.actor,
                provenance=original.provenance,
            )
        )


def test_the_same_idempotency_key_isolates_per_work_unit() -> None:
    ledger = EventLedger()
    _walk(ledger, (FactoryState.INTAKE,), work_unit_id="unit-a")
    _walk(ledger, (FactoryState.INTAKE,), work_unit_id="unit-b")

    assert len(ledger) == 2
    assert ledger.work_unit_ids("alpha") == ("unit-a", "unit-b")


# -- cross-project isolation and aggregates --------------------------------


def test_events_are_isolated_per_project_even_for_identical_work_unit_ids() -> None:
    registry = load_project_registry(_registry_document("alpha", "beta"))
    ledger = EventLedger(registry=registry)
    shared_unit = "issue:1"
    _walk(ledger, _NORMAL_PATH, project_id="alpha", work_unit_id=shared_unit)
    _walk(
        ledger,
        (FactoryState.INTAKE, FactoryState.TRIAGED),
        project_id="beta",
        work_unit_id=shared_unit,
    )

    alpha = ledger.projection("alpha", shared_unit)
    beta = ledger.projection("beta", shared_unit)

    assert alpha.state is FactoryState.READY_FOR_HUMAN_MERGE
    assert beta.state is FactoryState.TRIAGED
    assert {event.project_id for event in ledger.query(project_id="beta")} == {"beta"}
    assert len(ledger.query(project_id="beta")) == 2
    assert ledger.project_ids() == ("alpha", "beta")


def test_unregistered_projects_are_refused_when_a_registry_is_attached() -> None:
    ledger = EventLedger(registry=load_project_registry(_registry_document("alpha")))

    with pytest.raises(LedgerError, match="unregistered project: gamma"):
        ledger.append(_event(0, FactoryState.INTAKE, project_id="gamma"))


def test_aggregate_rolls_up_across_projects_and_can_be_scoped_to_one() -> None:
    ledger = EventLedger()
    _walk(ledger, _NORMAL_PATH, project_id="alpha", work_unit_id="issue:1")
    _walk(
        ledger,
        (FactoryState.INTAKE, FactoryState.TRIAGED),
        project_id="alpha",
        work_unit_id="issue:2",
    )
    _walk(
        ledger,
        (FactoryState.INTAKE, FactoryState.TRIAGED, FactoryState.BLOCKED),
        project_id="beta",
        work_unit_id="issue:9",
    )

    everything = ledger.aggregate()
    only_beta = ledger.aggregate(project_id="beta")

    assert everything["totals"]["projects"] == 2
    assert everything["totals"]["workUnits"] == 3
    assert everything["totals"]["byState"]["ready-for-human-merge"] == 1
    assert everything["projects"]["alpha"]["workUnits"] == 2
    assert everything["projects"]["beta"]["blocked"] == 1
    assert set(only_beta["projects"]) == {"beta"}
    assert only_beta["totals"]["workUnits"] == 1


def test_queries_filter_by_stage_state_and_time_window() -> None:
    ledger = EventLedger()
    _walk(ledger, _NORMAL_PATH)

    assert len(ledger.query(stage=LifecycleStage.INDEPENDENT_REVIEW)) == 1
    assert len(ledger.query(state=FactoryState.IMPLEMENTING)) == 1
    assert len(ledger.query(since="2026-09-05T00:00:00Z")) == 0
    assert len(ledger.query(until="2026-09-05T00:00:00Z")) == len(_NORMAL_PATH)
    with pytest.raises(LedgerError, match="not both"):
        ledger.query(project_id="alpha", project_ids=["alpha"])


# -- projection ------------------------------------------------------------


def test_normal_lifecycle_projects_to_ready_for_human_merge() -> None:
    ledger = EventLedger()
    _walk(ledger, _NORMAL_PATH)

    projection = ledger.projection("alpha", "github:example/alpha:issue:1")
    document = projection.as_dict()

    assert projection.state is FactoryState.READY_FOR_HUMAN_MERGE
    assert projection.stage is LifecycleStage.MERGE
    assert projection.repair_cycles == 0
    assert projection.is_terminal is False
    assert len(projection.transitions) == len(_NORMAL_PATH) - 1
    assert [item["to"] for item in document["durable"]["transitions"]] == [
        state.value for state in _NORMAL_PATH[1:]
    ]
    assert document["durable"]["eventCount"] == len(_NORMAL_PATH)


def test_repair_loop_lifecycle_counts_every_repair_cycle() -> None:
    ledger = EventLedger()
    _walk(ledger, _NORMAL_PATH[:-1])
    _walk(
        ledger,
        (
            FactoryState.REPAIR_NEEDED,
            FactoryState.REPAIRING,
            FactoryState.RE_REVIEWING,
            FactoryState.REPAIR_NEEDED,
            FactoryState.REPAIRING,
            FactoryState.RE_REVIEWING,
            FactoryState.READY_FOR_HUMAN_MERGE,
        ),
        start=len(_NORMAL_PATH),
    )

    projection = ledger.projection("alpha", "github:example/alpha:issue:1")

    assert projection.state is FactoryState.READY_FOR_HUMAN_MERGE
    assert projection.repair_cycles == 2


def test_blocked_lifecycle_keeps_the_reason_until_it_is_resolved() -> None:
    ledger = EventLedger()
    _walk(ledger, (FactoryState.INTAKE, FactoryState.TRIAGED))
    ledger.append(_event(2, FactoryState.BLOCKED, reason="escalated: repair budget exhausted"))

    blocked = ledger.projection("alpha", "github:example/alpha:issue:1")
    assert blocked.state is FactoryState.BLOCKED
    assert blocked.stage is LifecycleStage.BLOCKED
    assert blocked.blocked_reason == "escalated: repair budget exhausted"

    ledger.append(_event(3, FactoryState.TRIAGED, actor="owner", actor_kind="human"))
    resumed = ledger.projection("alpha", "github:example/alpha:issue:1")

    assert resumed.state is FactoryState.TRIAGED
    assert resumed.blocked_reason == ""


def test_rollback_lifecycle_is_observable_after_a_merge() -> None:
    ledger = EventLedger()
    _walk(ledger, _NORMAL_PATH)
    _walk(
        ledger,
        (
            FactoryState.MERGED,
            FactoryState.DEPLOYING,
            FactoryState.DEPLOYMENT_VERIFYING,
            FactoryState.LIVE,
            FactoryState.ROLLING_BACK,
            FactoryState.ROLLED_BACK,
        ),
        start=len(_NORMAL_PATH),
    )

    projection = ledger.projection("alpha", "github:example/alpha:issue:1")

    assert projection.state is FactoryState.ROLLED_BACK
    assert projection.stage is LifecycleStage.ROLLBACK
    assert projection.is_terminal is True
    stages = [event.stage for event in ledger.query(work_unit_id="github:example/alpha:issue:1")]
    assert LifecycleStage.DEPLOY in stages
    assert LifecycleStage.DEPLOYMENT_VERIFICATION in stages
    assert LifecycleStage.LIVE in stages


def test_superseded_lifecycle_records_its_replacement_and_is_terminal() -> None:
    ledger = EventLedger()
    _walk(ledger, (FactoryState.INTAKE, FactoryState.TRIAGED))
    ledger.append(
        _event(
            2,
            FactoryState.SUPERSEDED,
            reason="superseded by github:example/alpha:issue:7",
            superseded_by="github:example/alpha:issue:7",
        )
    )

    projection = ledger.projection("alpha", "github:example/alpha:issue:1")

    assert projection.state is FactoryState.SUPERSEDED
    assert projection.references.superseded_by == "github:example/alpha:issue:7"
    assert projection.is_terminal is True

    ledger.append(_event(3, FactoryState.TRIAGED, actor="owner", actor_kind="human"))
    with pytest.raises(LedgerError, match="is terminal"):
        ledger.projection("alpha", "github:example/alpha:issue:1")


def test_dependency_wait_is_a_stage_not_a_state() -> None:
    ledger = EventLedger()
    _walk(ledger, _NORMAL_PATH[:5])
    ledger.append(
        _event(
            5,
            FactoryState.APPROVED,
            stage=LifecycleStage.DEPENDENCY_WAIT,
            reason="waiting on github:example/alpha:issue:4",
            activity="queued behind 1 unmerged dependency",
        )
    )

    projection = ledger.projection("alpha", "github:example/alpha:issue:1")

    assert projection.state is FactoryState.APPROVED
    assert projection.stage is LifecycleStage.DEPENDENCY_WAIT
    assert projection.transitions[-1].to_state is FactoryState.APPROVED
    assert projection.activity == "queued behind 1 unmerged dependency"


def test_invalid_transitions_and_non_intake_genesis_fail_closed() -> None:
    ledger = EventLedger()
    _walk(ledger, (FactoryState.INTAKE, FactoryState.TRIAGED))
    ledger.append(_event(2, FactoryState.MERGED, actor="owner", actor_kind="human"))

    with pytest.raises(LedgerError, match="invalid transition"):
        ledger.projection("alpha", "github:example/alpha:issue:1")

    late = EventLedger()
    late.append(_event(0, FactoryState.IMPLEMENTING, work_unit_id="unit-late"))
    with pytest.raises(LedgerError, match="must open at intake"):
        late.projection("alpha", "unit-late")

    with pytest.raises(LedgerError, match="no events recorded"):
        EventLedger().projection("alpha", "unit-missing")


# -- durable state versus ephemeral activity -------------------------------


def test_activity_is_ephemeral_and_never_survives_a_state_change() -> None:
    ledger = EventLedger()
    _walk(ledger, _NORMAL_PATH[:7])
    ledger.append(
        LifecycleEvent(
            event_id="alpha:activity-1",
            idempotency_key="alpha:activity-1",
            work_unit=WorkUnitRef(project_id="alpha", work_unit_id="github:example/alpha:issue:1"),
            stage=LifecycleStage.IMPLEMENTATION,
            state=FactoryState.IMPLEMENTING,
            occurred_at=T,
            actor=EventActor(
                name="claude-1",
                kind="agent",
                agent_id="claude-1",
                provider="anthropic",
                model="claude-opus-5",
                model_alias="claude-opus",
                worker="gha-runner-7",
            ),
            provenance=EventProvenance(source="forge-ci"),
            activity="Claude editing src/agentic_sdlc/cli.py on runner gha-7",
        )
    )

    working = ledger.projection("alpha", "github:example/alpha:issue:1")
    document = working.as_dict()

    assert document["durable"]["state"] == "implementing"
    assert document["ephemeral"]["activity"] == (
        "Claude editing src/agentic_sdlc/cli.py on runner gha-7"
    )
    assert document["ephemeral"]["worker"] == "gha-runner-7"
    assert document["ephemeral"]["model"] == "claude-opus"
    # The activity event asserts the state it already had, so no transition.
    assert len(working.transitions) == len(_NORMAL_PATH[:7]) - 1

    _walk(ledger, (FactoryState.VERIFYING,), start=len(_NORMAL_PATH))
    verified = ledger.projection("alpha", "github:example/alpha:issue:1")

    assert verified.state is FactoryState.VERIFYING
    assert verified.activity == ""
    assert verified.activity_worker == ""


# -- credential-free telemetry --------------------------------------------


def test_event_dataclasses_have_no_field_that_can_hold_a_secret() -> None:
    assert secret_bearing_event_fields() == ()
    assert len(EVENT_SCHEMA_TYPES) == 4

    serialized = json.dumps(
        _event(
            1,
            FactoryState.IMPLEMENTING,
            repository="example/alpha",
            issue_ref="example/alpha#1",
        ).as_dict()
    ).lower()
    for forbidden in ("credential", "secret", "token", "password", "authorization"):
        assert forbidden not in serialized


# -- backward compatibility with existing WorkUnitState documents ----------


def _envelope(agent_id: str, mission_id: str, digest: str) -> dict[str, Any]:
    return {
        "missionId": mission_id,
        "missionVersion": "1.0.0",
        "agentId": agent_id,
        "adapter": "codex",
        "adapterVersion": "1.0.0",
        "provider": "openai",
        "model": "gpt-5",
        "modelAlias": "gpt-5",
        "promptSha256": "a" * 64,
        "envelopeSha256": digest * 64,
        "workRef": "example/alpha#1",
    }


def _orchestrated_unit(unit_id: str = "github:example/alpha:issue:1") -> Orchestrator:
    """Drive one work unit through a real repair loop to a human merge."""
    engine = Orchestrator()
    engine.create_unit(unit_id, event_key=f"{unit_id}:created", timestamp=T)
    for index, (state, actor, actor_kind) in enumerate(
        (
            (WorkUnitState.TRIAGED, "system", "system"),
            (WorkUnitState.SPECIFIED, "system", "system"),
            (WorkUnitState.PLANNED, "system", "system"),
            (WorkUnitState.APPROVAL_PENDING, "system", "system"),
            (WorkUnitState.APPROVED, "owner", "human"),
            (WorkUnitState.DISPATCHED, "system", "system"),
        )
    ):
        engine.transition(
            unit_id,
            state,
            actor=actor,
            actor_kind=actor_kind,
            event_key=f"{unit_id}:step-{index}",
            timestamp=T,
            reason=f"step {index}",
        )
    implementation = engine.start_run(
        unit_id,
        "implementation",
        _envelope("claude-1", "implementation-worker", "b"),
        event_key=f"{unit_id}:implement",
        timestamp=T,
    )
    engine.finish_run(unit_id, implementation.run_id, result="succeeded", timestamp=T)
    engine.transition(
        unit_id,
        WorkUnitState.VERIFYING,
        actor="system",
        actor_kind="system",
        event_key=f"{unit_id}:verify",
        timestamp=T,
    )
    engine.record_verification(unit_id, passed=True, event_key=f"{unit_id}:verified", timestamp=T)
    review = engine.start_run(
        unit_id,
        "review",
        _envelope("codex-1", "code-reviewer", "c"),
        event_key=f"{unit_id}:review",
        timestamp=T,
    )
    engine.record_review(
        unit_id, review.run_id, findings=1, event_key=f"{unit_id}:reviewed", timestamp=T
    )
    repair = engine.start_run(
        unit_id,
        "repair",
        _envelope("claude-1", "repair-agent", "d"),
        event_key=f"{unit_id}:repair",
        timestamp=T,
    )
    engine.finish_run(unit_id, repair.run_id, result="succeeded", timestamp=T)
    engine.record_repair_complete(
        unit_id, event_key=f"{unit_id}:repaired", timestamp=T, actor="claude-1"
    )
    re_review = engine.start_run(
        unit_id,
        "review",
        _envelope("codex-1", "code-reviewer", "e"),
        event_key=f"{unit_id}:re-review",
        timestamp=T,
    )
    engine.record_review(
        unit_id, re_review.run_id, findings=0, event_key=f"{unit_id}:re-reviewed", timestamp=T
    )
    engine.transition(
        unit_id,
        WorkUnitState.MERGED,
        actor="owner",
        actor_kind="human",
        event_key=f"{unit_id}:merged",
        timestamp=T,
        reason="human merge",
    )
    return engine


def test_existing_work_unit_state_documents_migrate_without_losing_history() -> None:
    unit_id = "github:example/alpha:issue:1"
    document = _orchestrated_unit(unit_id).as_dict()

    ledger = EventLedger()
    ledger.extend(
        events_from_orchestrator_document(document, project_id="alpha", repository="example/alpha")
    )
    projection = ledger.projection("alpha", unit_id)

    assert projection.state.value == document["units"][unit_id]["state"]
    assert [item.as_dict() for item in projection.transitions] == (
        document["units"][unit_id]["transitions"]
    )
    assert projection.repair_cycles == document["units"][unit_id]["repairCount"]
    assert projection.references.project_id == "alpha"
    assert projection.references.repository == "example/alpha"


def test_migrated_transitions_keep_the_orchestrator_record_shape() -> None:
    unit_id = "github:example/alpha:issue:1"
    ledger = EventLedger()
    ledger.extend(
        events_from_orchestrator_document(_orchestrated_unit(unit_id).as_dict(), project_id="alpha")
    )

    first = ledger.projection("alpha", unit_id).transitions[0]
    reference = TransitionRecord(
        sequence=first.sequence,
        from_state=WorkUnitState(first.from_state.value),
        to_state=WorkUnitState(first.to_state.value),
        actor=first.actor,
        actor_kind=first.actor_kind,
        event_key=first.event_key,
        timestamp=first.timestamp,
        reason=first.reason,
    )

    assert first.as_dict() == reference.as_dict()


def test_replaying_a_migration_is_idempotent() -> None:
    document = _orchestrated_unit().as_dict()
    events = events_from_orchestrator_document(document, project_id="alpha")

    ledger = EventLedger()
    ledger.extend(events)
    before = ledger.as_dict()
    ledger.extend(events_from_orchestrator_document(document, project_id="alpha"))

    assert ledger.as_dict() == before


def test_migration_rejects_unsupported_orchestration_documents() -> None:
    with pytest.raises(LedgerError, match="unsupported orchestration state document"):
        events_from_orchestrator_document({"schemaVersion": 2}, project_id="alpha")


def test_the_same_work_unit_can_be_migrated_into_two_isolated_projects() -> None:
    unit_id = "github:example/alpha:issue:1"
    document = _orchestrated_unit(unit_id).as_dict()
    ledger = EventLedger(registry=load_project_registry(_registry_document("alpha", "beta")))
    ledger.extend(events_from_orchestrator_document(document, project_id="alpha"))
    ledger.extend(events_from_orchestrator_document(document, project_id="beta"))

    assert ledger.projection("alpha", unit_id).state is FactoryState.MERGED
    assert ledger.projection("beta", unit_id).state is FactoryState.MERGED
    assert ledger.aggregate()["totals"]["workUnits"] == 2
