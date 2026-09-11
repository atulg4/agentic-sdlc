"""Append-only, versioned lifecycle event ledger for the Forge factory.

Every project Forge operates on emits the same telemetry stream: one immutable
event per meaningful lifecycle moment, carrying who acted, on which project and
work unit, at which stage, with which model and worker, and where the evidence
lives. Current state is never stored twice — it is *projected* from the event
history, deterministically, so a dashboard, a metric, or an audit can be
rebuilt from durable artifacts rather than from hidden agent memory.

Two invariants shape the schema:

- **Durable state is separate from ephemeral activity.** ``state`` is the
  factory state machine's durable answer (``implementing``); ``activity`` is a
  disposable human-readable note about right now ("Claude editing X on runner
  Y"). Activity never drives a transition and never survives one.
- **Telemetry carries no credentials.** No event field can hold a deployment,
  merge, broker, or model credential, and the loader rejects any document key
  whose name suggests one.

The ledger extends, and never contradicts, ``orchestration.py``: every
``WorkUnitState`` is a ``FactoryState`` with the same value, and the projected
transition history is byte-identical to the orchestrator's own
``TransitionRecord`` documents, so existing state artifacts migrate losslessly.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from enum import StrEnum
from typing import Any

from .orchestration import TRANSITIONS, WorkUnitState
from .project_registry import EMPTY_PROJECT_REGISTRY, ProjectRegistry

__all__ = [
    "EVENT_SCHEMA_TYPES",
    "EVENT_SCHEMA_VERSION",
    "FACTORY_TRANSITIONS",
    "LEDGER_SCHEMA_VERSION",
    "STAGE_FOR_STATE",
    "SUPPORTED_EVENT_SCHEMA_VERSIONS",
    "TERMINAL_FACTORY_STATES",
    "EventActor",
    "EventLedger",
    "EventProvenance",
    "FactoryState",
    "LedgerError",
    "LifecycleEvent",
    "LifecycleStage",
    "ProjectionTransition",
    "WorkUnitProjection",
    "WorkUnitRef",
    "events_from_orchestrator_document",
    "factory_state",
    "load_lifecycle_event",
    "migrate_event_document",
    "secret_bearing_event_fields",
]

LEDGER_SCHEMA_VERSION = 1
EVENT_SCHEMA_VERSION = 1

#: Version 0 is the pre-ledger orchestration vocabulary (``unitId``/``eventKey``
#: without an explicit ``schemaVersion``). It is accepted and upgraded on read.
SUPPORTED_EVENT_SCHEMA_VERSIONS = (0, 1)

_ACTOR_KINDS = frozenset({"human", "agent", "system"})
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")
_SECRET_KEY = re.compile(
    r"credential|secret|token|password|passphrase|apikey|api_key|authorization|bearer|private_key",
    re.I,
)


class LedgerError(ValueError):
    """Raised when a lifecycle event or ledger document cannot be trusted."""


class LifecycleStage(StrEnum):
    """The canonical factory stages, from backlog intake to rollback."""

    INTAKE = "intake"
    SPECIFICATION = "specification"
    PLANNING = "planning"
    DEPENDENCY_WAIT = "dependency-wait"
    DISPATCH = "dispatch"
    IMPLEMENTATION = "implementation"
    DETERMINISTIC_VERIFICATION = "deterministic-verification"
    INDEPENDENT_REVIEW = "independent-review"
    REPAIR = "repair"
    RE_REVIEW = "re-review"
    MERGE = "merge"
    DEPLOY = "deploy"
    DEPLOYMENT_VERIFICATION = "deployment-verification"
    LIVE = "live"
    ROLLBACK = "rollback"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class FactoryState(StrEnum):
    """Durable factory state: every ``WorkUnitState`` plus the release states.

    ``orchestration.py`` deliberately owns no deployment authority and stops at
    ``merged``. The ledger only *observes* what happens after a merge, so the
    release states live here and never widen the orchestrator's transition
    table.
    """

    INTAKE = "intake"
    TRIAGED = "triaged"
    SPECIFIED = "specified"
    PLANNED = "planned"
    APPROVAL_PENDING = "approval-pending"
    APPROVED = "approved"
    DISPATCHED = "dispatched"
    IMPLEMENTING = "implementing"
    VERIFYING = "verifying"
    REVIEWING = "reviewing"
    REPAIR_NEEDED = "repair-needed"
    REPAIRING = "repairing"
    RE_REVIEWING = "re-reviewing"
    READY_FOR_HUMAN_MERGE = "ready-for-human-merge"
    MERGED = "merged"
    DEPLOYING = "deploying"
    DEPLOYMENT_VERIFYING = "deployment-verifying"
    LIVE = "live"
    ROLLING_BACK = "rolling-back"
    ROLLED_BACK = "rolled-back"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


_F = FactoryState
TERMINAL_FACTORY_STATES = frozenset({_F.FAILED, _F.CANCELLED, _F.SUPERSEDED, _F.ROLLED_BACK})

_RELEASE_ABORT = frozenset({_F.ROLLING_BACK, _F.BLOCKED, _F.FAILED})


def _release_transitions() -> dict[FactoryState, frozenset[FactoryState]]:
    """Post-merge transitions the orchestrator does not own."""
    return {
        _F.MERGED: frozenset({_F.DEPLOYING, _F.LIVE, _F.BLOCKED}),
        _F.DEPLOYING: frozenset({_F.DEPLOYMENT_VERIFYING, _F.LIVE}) | _RELEASE_ABORT,
        _F.DEPLOYMENT_VERIFYING: frozenset({_F.LIVE}) | _RELEASE_ABORT,
        _F.LIVE: frozenset({_F.DEPLOYING, _F.ROLLING_BACK, _F.BLOCKED}),
        _F.ROLLING_BACK: frozenset({_F.ROLLED_BACK, _F.BLOCKED, _F.FAILED}),
        _F.ROLLED_BACK: frozenset(),
    }


def _factory_transitions() -> dict[FactoryState, frozenset[FactoryState]]:
    """Derive the factory table from the orchestrator's so the two cannot drift."""
    table = {
        FactoryState(state.value): frozenset(FactoryState(item.value) for item in targets)
        for state, targets in TRANSITIONS.items()
    }
    for state, targets in _release_transitions().items():
        table[state] = table.get(state, frozenset()) | targets
    # Blocked release work resumes into the release states it came from.
    table[_F.BLOCKED] = table[_F.BLOCKED] | frozenset({_F.DEPLOYING, _F.LIVE, _F.ROLLING_BACK})
    return table


FACTORY_TRANSITIONS: dict[FactoryState, frozenset[FactoryState]] = _factory_transitions()

STAGE_FOR_STATE: Mapping[FactoryState, LifecycleStage] = {
    _F.INTAKE: LifecycleStage.INTAKE,
    _F.TRIAGED: LifecycleStage.INTAKE,
    _F.SPECIFIED: LifecycleStage.SPECIFICATION,
    _F.PLANNED: LifecycleStage.PLANNING,
    _F.APPROVAL_PENDING: LifecycleStage.PLANNING,
    _F.APPROVED: LifecycleStage.PLANNING,
    _F.DISPATCHED: LifecycleStage.DISPATCH,
    _F.IMPLEMENTING: LifecycleStage.IMPLEMENTATION,
    _F.VERIFYING: LifecycleStage.DETERMINISTIC_VERIFICATION,
    _F.REVIEWING: LifecycleStage.INDEPENDENT_REVIEW,
    _F.REPAIR_NEEDED: LifecycleStage.REPAIR,
    _F.REPAIRING: LifecycleStage.REPAIR,
    _F.RE_REVIEWING: LifecycleStage.RE_REVIEW,
    _F.READY_FOR_HUMAN_MERGE: LifecycleStage.MERGE,
    _F.MERGED: LifecycleStage.MERGE,
    _F.DEPLOYING: LifecycleStage.DEPLOY,
    _F.DEPLOYMENT_VERIFYING: LifecycleStage.DEPLOYMENT_VERIFICATION,
    _F.LIVE: LifecycleStage.LIVE,
    _F.ROLLING_BACK: LifecycleStage.ROLLBACK,
    _F.ROLLED_BACK: LifecycleStage.ROLLBACK,
    _F.BLOCKED: LifecycleStage.BLOCKED,
    _F.FAILED: LifecycleStage.FAILED,
    _F.CANCELLED: LifecycleStage.CANCELLED,
    _F.SUPERSEDED: LifecycleStage.SUPERSEDED,
}


def factory_state(state: WorkUnitState | str) -> FactoryState:
    """Widen an orchestration ``WorkUnitState`` into the factory vocabulary."""
    return FactoryState(WorkUnitState(state).value)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise LedgerError(message)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    _require(isinstance(value, Mapping), f"{label} must be an object")
    assert isinstance(value, Mapping)
    for key in value:
        _require(isinstance(key, str), f"{label} keys must be strings")
        _require(
            _SECRET_KEY.search(key) is None,
            f"{label} must not carry credentials or secrets: {key}",
        )
    return value


def _known_keys(entry: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(entry) - allowed)
    if unknown:
        raise LedgerError(f"{label} declares unknown keys: " + ", ".join(unknown))


def _text(entry: Mapping[str, Any], key: str, label: str, *, required: bool = False) -> str:
    value = entry.get(key, "")
    if value is None:
        value = ""
    _require(isinstance(value, str), f"{label}: {key} must be a string")
    assert isinstance(value, str)
    value = value.strip()
    _require(not required or bool(value), f"{label}: {key} is required")
    return value


def _timestamp(value: str, label: str) -> str:
    _require(
        bool(_TIMESTAMP.fullmatch(value)),
        f"{label} must be an RFC 3339 timestamp: {value!r}",
    )
    return value


def _string_tuple(value: Any, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    _require(isinstance(value, list), f"{label} must be an array of strings")
    assert isinstance(value, list)
    items = []
    for item in value:
        _require(isinstance(item, str) and item.strip(), f"{label} must be non-empty strings")
        assert isinstance(item, str)
        items.append(item.strip())
    return tuple(items)


@dataclass(frozen=True)
class WorkUnitRef:
    """Where one unit of work lives, without assuming any single consumer repo."""

    project_id: str
    work_unit_id: str
    repository: str = ""
    issue_ref: str = ""
    change_request_ref: str = ""
    workflow_ref: str = ""
    run_ref: str = ""
    superseded_by: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "projectId": self.project_id,
            "workUnitId": self.work_unit_id,
            "repository": self.repository,
            "issueRef": self.issue_ref,
            "changeRequestRef": self.change_request_ref,
            "workflowRef": self.workflow_ref,
            "runRef": self.run_ref,
            "supersededBy": self.superseded_by,
        }


@dataclass(frozen=True)
class EventActor:
    """Who or what produced the event, and on which worker and model."""

    name: str
    kind: str
    agent_id: str = ""
    provider: str = ""
    model: str = ""
    model_alias: str = ""
    worker: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "agentId": self.agent_id,
            "provider": self.provider,
            "model": self.model,
            "modelAlias": self.model_alias,
            "worker": self.worker,
        }


@dataclass(frozen=True)
class EventProvenance:
    """Immutable references that make one event reproducible and auditable."""

    source: str
    mission_id: str = ""
    mission_version: str = ""
    adapter: str = ""
    adapter_version: str = ""
    prompt_sha256: str = ""
    envelope_sha256: str = ""
    context_pack_digest: str = ""
    commit_sha: str = ""
    policy_version: str = ""
    evidence_refs: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "missionId": self.mission_id,
            "missionVersion": self.mission_version,
            "adapter": self.adapter,
            "adapterVersion": self.adapter_version,
            "promptSha256": self.prompt_sha256,
            "envelopeSha256": self.envelope_sha256,
            "contextPackDigest": self.context_pack_digest,
            "commitSha": self.commit_sha,
            "policyVersion": self.policy_version,
            "evidenceRefs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class LifecycleEvent:
    """One immutable observation of the factory lifecycle."""

    event_id: str
    idempotency_key: str
    work_unit: WorkUnitRef
    stage: LifecycleStage
    state: FactoryState
    occurred_at: str
    actor: EventActor
    provenance: EventProvenance
    recorded_at: str = ""
    activity: str = ""
    environment: str = ""
    reason: str = ""
    parent_event_id: str = ""
    depends_on: tuple[str, ...] = ()
    schema_version: int = EVENT_SCHEMA_VERSION

    @property
    def project_id(self) -> str:
        return self.work_unit.project_id

    @property
    def work_unit_id(self) -> str:
        return self.work_unit.work_unit_id

    @property
    def scope(self) -> tuple[str, str]:
        """The isolation scope one event belongs to."""
        return self.work_unit.project_id, self.work_unit.work_unit_id

    def as_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "eventId": self.event_id,
            "idempotencyKey": self.idempotency_key,
            "workUnit": self.work_unit.as_dict(),
            "stage": self.stage.value,
            "state": self.state.value,
            "activity": self.activity,
            "environment": self.environment,
            "occurredAt": self.occurred_at,
            "recordedAt": self.recorded_at,
            "reason": self.reason,
            "actor": self.actor.as_dict(),
            "provenance": self.provenance.as_dict(),
            "parentEventId": self.parent_event_id,
            "dependsOn": list(self.depends_on),
        }


_EVENT_KEYS = frozenset(
    {
        "schemaVersion",
        "eventId",
        "idempotencyKey",
        "workUnit",
        "stage",
        "state",
        "activity",
        "environment",
        "occurredAt",
        "recordedAt",
        "reason",
        "actor",
        "provenance",
        "parentEventId",
        "dependsOn",
    }
)
_WORK_UNIT_KEYS = frozenset(
    {
        "projectId",
        "workUnitId",
        "repository",
        "issueRef",
        "changeRequestRef",
        "workflowRef",
        "runRef",
        "supersededBy",
    }
)
_ACTOR_KEYS = frozenset({"name", "kind", "agentId", "provider", "model", "modelAlias", "worker"})
_PROVENANCE_KEYS = frozenset(
    {
        "source",
        "missionId",
        "missionVersion",
        "adapter",
        "adapterVersion",
        "promptSha256",
        "envelopeSha256",
        "contextPackDigest",
        "commitSha",
        "policyVersion",
        "evidenceRefs",
    }
)


def migrate_event_document(document: Any) -> dict[str, Any]:
    """Upgrade one stored event document to the current schema version.

    A document without ``schemaVersion`` is treated as version 0: the
    orchestration-era shape that named a work unit ``unitId`` and its
    idempotency key ``eventKey``. Future versions fail closed rather than being
    read optimistically with unknown semantics.
    """
    entry = dict(_mapping(document, "lifecycle event"))
    version = entry.get("schemaVersion", 0)
    _require(
        isinstance(version, int) and not isinstance(version, bool),
        "lifecycle event schemaVersion must be an integer",
    )
    _require(
        version in SUPPORTED_EVENT_SCHEMA_VERSIONS,
        f"unsupported lifecycle event schemaVersion: {version}",
    )
    if version == EVENT_SCHEMA_VERSION:
        return entry

    # Version 0 spoke the orchestration vocabulary: a work unit was a ``unitId``
    # and its idempotency key an ``eventKey``, at either nesting level.
    work_unit = dict(_mapping(entry.get("workUnit", {}), "lifecycle event workUnit"))
    for holder in (entry, work_unit):
        if "unitId" in holder:
            work_unit.setdefault("workUnitId", holder.pop("unitId"))
    if "eventKey" in entry:
        entry.setdefault("idempotencyKey", entry.pop("eventKey"))
    if work_unit:
        entry["workUnit"] = work_unit
    entry["schemaVersion"] = EVENT_SCHEMA_VERSION
    return entry


def _load_work_unit(raw: Any) -> WorkUnitRef:
    label = "lifecycle event workUnit"
    entry = _mapping(raw, label)
    _known_keys(entry, _WORK_UNIT_KEYS, label)
    return WorkUnitRef(
        project_id=_text(entry, "projectId", label, required=True),
        work_unit_id=_text(entry, "workUnitId", label, required=True),
        repository=_text(entry, "repository", label),
        issue_ref=_text(entry, "issueRef", label),
        change_request_ref=_text(entry, "changeRequestRef", label),
        workflow_ref=_text(entry, "workflowRef", label),
        run_ref=_text(entry, "runRef", label),
        superseded_by=_text(entry, "supersededBy", label),
    )


def _load_actor(raw: Any) -> EventActor:
    label = "lifecycle event actor"
    entry = _mapping(raw, label)
    _known_keys(entry, _ACTOR_KEYS, label)
    kind = _text(entry, "kind", label, required=True)
    _require(kind in _ACTOR_KINDS, f"{label}: unknown actor kind: {kind}")
    return EventActor(
        name=_text(entry, "name", label, required=True),
        kind=kind,
        agent_id=_text(entry, "agentId", label),
        provider=_text(entry, "provider", label),
        model=_text(entry, "model", label),
        model_alias=_text(entry, "modelAlias", label),
        worker=_text(entry, "worker", label),
    )


def _load_provenance(raw: Any) -> EventProvenance:
    label = "lifecycle event provenance"
    entry = _mapping(raw, label)
    _known_keys(entry, _PROVENANCE_KEYS, label)
    return EventProvenance(
        source=_text(entry, "source", label, required=True),
        mission_id=_text(entry, "missionId", label),
        mission_version=_text(entry, "missionVersion", label),
        adapter=_text(entry, "adapter", label),
        adapter_version=_text(entry, "adapterVersion", label),
        prompt_sha256=_text(entry, "promptSha256", label),
        envelope_sha256=_text(entry, "envelopeSha256", label),
        context_pack_digest=_text(entry, "contextPackDigest", label),
        commit_sha=_text(entry, "commitSha", label),
        policy_version=_text(entry, "policyVersion", label),
        evidence_refs=_string_tuple(entry.get("evidenceRefs"), f"{label}: evidenceRefs"),
    )


def load_lifecycle_event(document: Any) -> LifecycleEvent:
    """Parse one event document fail-closed, migrating older schema versions."""
    entry = migrate_event_document(document)
    label = "lifecycle event"
    _known_keys(entry, _EVENT_KEYS, label)
    event_id = _text(entry, "eventId", label, required=True)
    stage_value = _text(entry, "stage", label, required=True)
    state_value = _text(entry, "state", label, required=True)
    try:
        stage = LifecycleStage(stage_value)
    except ValueError as error:
        raise LedgerError(f"{label}: unknown stage: {stage_value}") from error
    try:
        state = FactoryState(state_value)
    except ValueError as error:
        raise LedgerError(f"{label}: unknown state: {state_value}") from error
    occurred_at = _timestamp(
        _text(entry, "occurredAt", label, required=True), f"{label}: occurredAt"
    )
    recorded_at = _text(entry, "recordedAt", label)
    if recorded_at:
        _timestamp(recorded_at, f"{label}: recordedAt")
    return LifecycleEvent(
        event_id=event_id,
        idempotency_key=_text(entry, "idempotencyKey", label, required=True),
        work_unit=_load_work_unit(entry.get("workUnit", {})),
        stage=stage,
        state=state,
        occurred_at=occurred_at,
        actor=_load_actor(entry.get("actor", {})),
        provenance=_load_provenance(entry.get("provenance", {})),
        recorded_at=recorded_at,
        activity=_text(entry, "activity", label),
        environment=_text(entry, "environment", label),
        reason=_text(entry, "reason", label),
        parent_event_id=_text(entry, "parentEventId", label),
        depends_on=_string_tuple(entry.get("dependsOn"), f"{label}: dependsOn"),
    )


@dataclass(frozen=True)
class ProjectionTransition:
    """One durable state change, in the orchestrator's own record shape."""

    sequence: int
    from_state: FactoryState
    to_state: FactoryState
    actor: str
    actor_kind: str
    event_key: str
    timestamp: str
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "from": self.from_state.value,
            "to": self.to_state.value,
            "actor": self.actor,
            "actorKind": self.actor_kind,
            "eventKey": self.event_key,
            "timestamp": self.timestamp,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class WorkUnitProjection:
    """Deterministic current state of one work unit, folded from its events."""

    project_id: str
    work_unit_id: str
    state: FactoryState
    stage: LifecycleStage
    references: WorkUnitRef
    transitions: tuple[ProjectionTransition, ...] = ()
    repair_cycles: int = 0
    blocked_reason: str = ""
    depends_on: tuple[str, ...] = ()
    event_count: int = 0
    last_event_id: str = ""
    last_event_at: str = ""
    activity: str = ""
    activity_at: str = ""
    activity_actor: str = ""
    activity_agent_id: str = ""
    activity_model: str = ""
    activity_worker: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_FACTORY_STATES

    def as_dict(self) -> dict[str, Any]:
        """Serialize with durable and ephemeral facts structurally separated."""
        return {
            "schemaVersion": LEDGER_SCHEMA_VERSION,
            "projectId": self.project_id,
            "workUnitId": self.work_unit_id,
            "durable": {
                "state": self.state.value,
                "stage": self.stage.value,
                "terminal": self.is_terminal,
                "repairCycles": self.repair_cycles,
                "blockedReason": self.blocked_reason,
                "dependsOn": list(self.depends_on),
                "references": self.references.as_dict(),
                "transitions": [item.as_dict() for item in self.transitions],
                "eventCount": self.event_count,
                "lastEventId": self.last_event_id,
                "lastEventAt": self.last_event_at,
            },
            "ephemeral": {
                "activity": self.activity,
                "activityAt": self.activity_at,
                "actor": self.activity_actor,
                "agentId": self.activity_agent_id,
                "model": self.activity_model,
                "worker": self.activity_worker,
            },
        }


@dataclass
class _Fold:
    """Mutable accumulator used while folding one work unit's events."""

    project_id: str
    work_unit_id: str
    state: FactoryState = FactoryState.INTAKE
    stage: LifecycleStage = LifecycleStage.INTAKE
    references: WorkUnitRef = field(init=False)
    transitions: list[ProjectionTransition] = field(default_factory=list)
    repair_cycles: int = 0
    blocked_reason: str = ""
    depends_on: tuple[str, ...] = ()
    event_count: int = 0
    last_event_id: str = ""
    last_event_at: str = ""
    activity: str = ""
    activity_at: str = ""
    activity_actor: str = ""
    activity_agent_id: str = ""
    activity_model: str = ""
    activity_worker: str = ""

    def __post_init__(self) -> None:
        self.references = WorkUnitRef(project_id=self.project_id, work_unit_id=self.work_unit_id)


def _merge_references(current: WorkUnitRef, incoming: WorkUnitRef) -> WorkUnitRef:
    """Later non-empty references win; an event never blanks a known reference."""
    return WorkUnitRef(
        project_id=current.project_id,
        work_unit_id=current.work_unit_id,
        repository=incoming.repository or current.repository,
        issue_ref=incoming.issue_ref or current.issue_ref,
        change_request_ref=incoming.change_request_ref or current.change_request_ref,
        workflow_ref=incoming.workflow_ref or current.workflow_ref,
        run_ref=incoming.run_ref or current.run_ref,
        superseded_by=incoming.superseded_by or current.superseded_by,
    )


def _fold(events: Sequence[LifecycleEvent]) -> WorkUnitProjection:
    first = events[0]
    _require(
        first.state is FactoryState.INTAKE,
        f"work unit {first.work_unit_id}: the first event must open at intake, "
        f"not {first.state.value}",
    )
    accumulator = _Fold(project_id=first.project_id, work_unit_id=first.work_unit_id)

    for event in events:
        accumulator.event_count += 1
        accumulator.last_event_id = event.event_id
        accumulator.last_event_at = event.occurred_at
        accumulator.references = _merge_references(accumulator.references, event.work_unit)
        accumulator.stage = event.stage
        if event.depends_on:
            accumulator.depends_on = tuple(
                sorted(set(accumulator.depends_on) | set(event.depends_on))
            )

        if event.state is not accumulator.state:
            _require(
                accumulator.state not in TERMINAL_FACTORY_STATES,
                f"work unit {event.work_unit_id} is terminal ({accumulator.state.value}) "
                f"and cannot change",
            )
            _require(
                event.state in FACTORY_TRANSITIONS[accumulator.state],
                f"invalid transition for {event.work_unit_id}: "
                f"{accumulator.state.value} -> {event.state.value}",
            )
            accumulator.transitions.append(
                ProjectionTransition(
                    sequence=len(accumulator.transitions) + 1,
                    from_state=accumulator.state,
                    to_state=event.state,
                    actor=event.actor.name,
                    actor_kind=event.actor.kind,
                    event_key=event.idempotency_key,
                    timestamp=event.occurred_at,
                    reason=event.reason,
                )
            )
            if event.state is FactoryState.REPAIRING:
                accumulator.repair_cycles += 1
            accumulator.blocked_reason = event.reason if event.state is FactoryState.BLOCKED else ""
            accumulator.state = event.state
            # A durable state change retires the previous ephemeral activity.
            accumulator.activity = ""
            accumulator.activity_at = ""
            accumulator.activity_actor = ""
            accumulator.activity_agent_id = ""
            accumulator.activity_model = ""
            accumulator.activity_worker = ""

        if event.activity:
            accumulator.activity = event.activity
            accumulator.activity_at = event.occurred_at
            accumulator.activity_actor = event.actor.name
            accumulator.activity_agent_id = event.actor.agent_id
            accumulator.activity_model = event.actor.model_alias or event.actor.model
            accumulator.activity_worker = event.actor.worker

    return WorkUnitProjection(
        project_id=accumulator.project_id,
        work_unit_id=accumulator.work_unit_id,
        state=accumulator.state,
        stage=accumulator.stage,
        references=accumulator.references,
        transitions=tuple(accumulator.transitions),
        repair_cycles=accumulator.repair_cycles,
        blocked_reason=accumulator.blocked_reason,
        depends_on=accumulator.depends_on,
        event_count=accumulator.event_count,
        last_event_id=accumulator.last_event_id,
        last_event_at=accumulator.last_event_at,
        activity=accumulator.activity,
        activity_at=accumulator.activity_at,
        activity_actor=accumulator.activity_actor,
        activity_agent_id=accumulator.activity_agent_id,
        activity_model=accumulator.activity_model,
        activity_worker=accumulator.activity_worker,
    )


class EventLedger:
    """An append-only ledger of lifecycle events across every Forge project.

    Events are never mutated or removed. Appending an event whose idempotency
    key was already recorded for that work unit is a no-op, so a replayed
    webhook delivery, a re-run workflow, or a duplicated CLI invocation cannot
    fork history.
    """

    def __init__(self, *, registry: ProjectRegistry = EMPTY_PROJECT_REGISTRY) -> None:
        self._registry = registry
        self._events: list[LifecycleEvent] = []
        self._event_ids: dict[str, LifecycleEvent] = {}
        self._idempotency: dict[tuple[str, str, str], LifecycleEvent] = {}

    @property
    def registry(self) -> ProjectRegistry:
        return self._registry

    @property
    def events(self) -> tuple[LifecycleEvent, ...]:
        return tuple(self._events)

    def __len__(self) -> int:
        return len(self._events)

    # -- append ------------------------------------------------------------

    def _idempotency_id(self, event: LifecycleEvent) -> tuple[str, str, str]:
        return event.project_id, event.work_unit_id, event.idempotency_key

    def append(self, event: LifecycleEvent) -> LifecycleEvent:
        """Record one event. Replaying the same idempotency key is a no-op."""
        if not self._registry.is_empty:
            _require(
                event.project_id in self._registry,
                f"unregistered project: {event.project_id}",
            )
        key = self._idempotency_id(event)
        existing = self._idempotency.get(key)
        if existing is not None:
            _require(
                existing.as_dict() == event.as_dict(),
                f"idempotency key {event.idempotency_key} was already recorded for "
                f"{event.work_unit_id} with a different payload",
            )
            return existing
        clash = self._event_ids.get(event.event_id)
        _require(
            clash is None,
            f"event ID {event.event_id} is already recorded under a different idempotency key",
        )
        self._events.append(event)
        self._event_ids[event.event_id] = event
        self._idempotency[key] = event
        return event

    def extend(self, events: Iterable[LifecycleEvent]) -> tuple[LifecycleEvent, ...]:
        return tuple(self.append(event) for event in events)

    # -- queries -----------------------------------------------------------

    def query(
        self,
        *,
        project_id: str | None = None,
        project_ids: Sequence[str] | None = None,
        work_unit_id: str | None = None,
        stage: LifecycleStage | str | None = None,
        state: FactoryState | str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> tuple[LifecycleEvent, ...]:
        """Filter events. Project scoping is exact, so projects never leak."""
        scope = self._scope(project_id, project_ids)
        wanted_stage = LifecycleStage(stage) if stage is not None else None
        wanted_state = FactoryState(state) if state is not None else None
        return tuple(
            event
            for event in self._ordered()
            if (scope is None or event.project_id in scope)
            and (work_unit_id is None or event.work_unit_id == work_unit_id)
            and (wanted_stage is None or event.stage is wanted_stage)
            and (wanted_state is None or event.state is wanted_state)
            and (since is None or event.occurred_at >= since)
            and (until is None or event.occurred_at <= until)
        )

    def _scope(
        self, project_id: str | None, project_ids: Sequence[str] | None
    ) -> frozenset[str] | None:
        _require(
            project_id is None or project_ids is None,
            "pass project_id or project_ids, not both",
        )
        if project_id is not None:
            return frozenset({project_id})
        if project_ids is not None:
            return frozenset(project_ids)
        return None

    def _ordered(self) -> tuple[LifecycleEvent, ...]:
        """Events in deterministic fold order: occurrence time, then arrival."""
        return tuple(
            event
            for _, event in sorted(
                enumerate(self._events), key=lambda item: (item[1].occurred_at, item[0])
            )
        )

    def project_ids(self) -> tuple[str, ...]:
        return tuple(sorted({event.project_id for event in self._events}))

    def work_unit_ids(self, project_id: str | None = None) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    event.work_unit_id
                    for event in self._events
                    if project_id is None or event.project_id == project_id
                }
            )
        )

    # -- projection --------------------------------------------------------

    def projection(self, project_id: str, work_unit_id: str) -> WorkUnitProjection:
        """Fold one work unit's events into its current state, deterministically."""
        events = self.query(project_id=project_id, work_unit_id=work_unit_id)
        _require(
            bool(events),
            f"no events recorded for {work_unit_id} in project {project_id}",
        )
        return _fold(events)

    def projections(
        self,
        *,
        project_id: str | None = None,
        project_ids: Sequence[str] | None = None,
    ) -> tuple[WorkUnitProjection, ...]:
        scope = self._scope(project_id, project_ids)
        grouped: dict[tuple[str, str], list[LifecycleEvent]] = {}
        for event in self._ordered():
            if scope is not None and event.project_id not in scope:
                continue
            grouped.setdefault(event.scope, []).append(event)
        return tuple(_fold(events) for _, events in sorted(grouped.items()))

    def aggregate(
        self,
        *,
        project_id: str | None = None,
        project_ids: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Cross-project rollup of durable state, drawn only from the ledger."""
        projections = self.projections(project_id=project_id, project_ids=project_ids)
        projects: dict[str, dict[str, Any]] = {}
        totals_state: dict[str, int] = {}
        totals_stage: dict[str, int] = {}
        for item in projections:
            bucket = projects.setdefault(
                item.project_id,
                {
                    "workUnits": 0,
                    "events": 0,
                    "blocked": 0,
                    "terminal": 0,
                    "repairCycles": 0,
                    "byState": {},
                    "byStage": {},
                },
            )
            bucket["workUnits"] += 1
            bucket["events"] += item.event_count
            bucket["repairCycles"] += item.repair_cycles
            bucket["blocked"] += 1 if item.state is FactoryState.BLOCKED else 0
            bucket["terminal"] += 1 if item.is_terminal else 0
            bucket["byState"][item.state.value] = bucket["byState"].get(item.state.value, 0) + 1
            bucket["byStage"][item.stage.value] = bucket["byStage"].get(item.stage.value, 0) + 1
            totals_state[item.state.value] = totals_state.get(item.state.value, 0) + 1
            totals_stage[item.stage.value] = totals_stage.get(item.stage.value, 0) + 1
        return {
            "schemaVersion": LEDGER_SCHEMA_VERSION,
            "projects": {name: projects[name] for name in sorted(projects)},
            "totals": {
                "projects": len(projects),
                "workUnits": len(projections),
                "events": sum(item.event_count for item in projections),
                "byState": dict(sorted(totals_state.items())),
                "byStage": dict(sorted(totals_stage.items())),
            },
        }

    # -- persistence -------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": LEDGER_SCHEMA_VERSION,
            "eventSchemaVersion": EVENT_SCHEMA_VERSION,
            "events": [event.as_dict() for event in self._events],
        }

    @classmethod
    def from_dict(
        cls,
        document: Any,
        *,
        registry: ProjectRegistry = EMPTY_PROJECT_REGISTRY,
    ) -> EventLedger:
        entry = _mapping(document, "event ledger")
        _known_keys(
            entry, frozenset({"schemaVersion", "eventSchemaVersion", "events"}), "event ledger"
        )
        version = entry.get("schemaVersion")
        _require(
            version == LEDGER_SCHEMA_VERSION,
            f"unsupported event ledger schemaVersion: {version!r}",
        )
        raw_events = entry.get("events", [])
        _require(isinstance(raw_events, list), "event ledger events must be an array")
        assert isinstance(raw_events, list)
        ledger = cls(registry=registry)
        for item in raw_events:
            ledger.append(load_lifecycle_event(item))
        return ledger


#: Every dataclass that makes up one serialized lifecycle event.
EVENT_SCHEMA_TYPES = (LifecycleEvent, WorkUnitRef, EventActor, EventProvenance)


def secret_bearing_event_fields() -> tuple[str, ...]:
    """Event-schema field names that could hold a secret.

    Telemetry records carry no deployment, merge, broker, or model credential,
    so this is structurally empty. It is exposed rather than asserted inline so
    the guarantee is a test other schemas can reuse, not a comment.
    """
    return tuple(
        f"{schema.__name__}.{item.name}"
        for schema in EVENT_SCHEMA_TYPES
        for item in fields(schema)
        if _SECRET_KEY.search(item.name) is not None
    )


def events_from_orchestrator_document(
    document: Mapping[str, Any],
    *,
    project_id: str,
    repository: str = "",
    source: str = "orchestration-migration",
) -> tuple[LifecycleEvent, ...]:
    """Migrate an ``Orchestrator.as_dict()`` document into ledger events.

    The produced events replay to exactly the state and transition history the
    orchestrator recorded, so an existing durable state artifact becomes ledger
    history without losing a single transition. Each transition keeps its
    original ``eventKey`` as its idempotency key, so replaying a migration is a
    no-op just like replaying the original event.
    """
    _require(
        document.get("schemaVersion") == 1 and document.get("lifecycleVersion") == 1,
        "unsupported orchestration state document",
    )
    units = document.get("units", {})
    _require(isinstance(units, Mapping), "units must be a mapping")
    assert isinstance(units, Mapping)

    events: list[LifecycleEvent] = []
    for unit_id, raw in sorted(units.items()):
        _require(isinstance(raw, Mapping), f"work unit {unit_id} must be an object")
        assert isinstance(raw, Mapping)
        transitions = list(raw.get("transitions", ()))
        created_at = str(raw.get("createdAt", "")) or str(
            transitions[0]["timestamp"] if transitions else ""
        )
        _require(bool(created_at), f"work unit {unit_id}: createdAt is required to migrate")
        references = WorkUnitRef(
            project_id=project_id,
            work_unit_id=str(unit_id),
            repository=repository,
            superseded_by=str(raw.get("supersededBy", "")),
        )
        provenance = EventProvenance(source=source)
        events.append(
            LifecycleEvent(
                event_id=f"{project_id}:{unit_id}:0000",
                idempotency_key=f"{unit_id}:ledger-genesis",
                work_unit=references,
                stage=LifecycleStage.INTAKE,
                state=FactoryState.INTAKE,
                occurred_at=_timestamp(created_at, f"work unit {unit_id}: createdAt"),
                actor=EventActor(name=source, kind="system"),
                provenance=provenance,
                reason="migrated from orchestration state",
                depends_on=tuple(str(item) for item in raw.get("dependsOn", ())),
            )
        )
        for item in transitions:
            _require(
                isinstance(item, Mapping),
                f"work unit {unit_id}: every transition must be an object",
            )
            assert isinstance(item, Mapping)
            sequence = int(item["sequence"])
            state = FactoryState(str(item["to"]))
            events.append(
                LifecycleEvent(
                    event_id=f"{project_id}:{unit_id}:{sequence:04d}",
                    idempotency_key=str(item["eventKey"]),
                    work_unit=references,
                    stage=STAGE_FOR_STATE[state],
                    state=state,
                    occurred_at=_timestamp(
                        str(item["timestamp"]), f"work unit {unit_id}: transition timestamp"
                    ),
                    actor=EventActor(name=str(item["actor"]), kind=str(item["actorKind"])),
                    provenance=provenance,
                    reason=str(item.get("reason", "")),
                )
            )
    return tuple(events)
