"""Provider-neutral autonomous multi-agent orchestration state machine.

The orchestrator owns the bounded lifecycle of a work unit — triage, planning,
approval, dispatch, verification, independent review, bounded repair, and
human merge. Every transition is explicit, idempotent, policy-gated, and
recorded so the whole history is reproducible from durable artifacts rather
than hidden agent memory.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from .missions import AgentProfile, MissionRegistry

__all__ = [
    "AgentRunRecord",
    "LIFECYCLE_VERSION",
    "OrchestrationError",
    "Orchestrator",
    "STATUS_MARKER",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "TransitionRecord",
    "WorkUnitState",
]

LIFECYCLE_VERSION = 1
STATUS_MARKER = "<!-- agentic-sdlc:status -->"


class OrchestrationError(ValueError):
    """Raised when a lifecycle transition or run action violates policy."""


class WorkUnitState(StrEnum):
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
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


_S = WorkUnitState
TERMINAL_STATES = frozenset({_S.MERGED, _S.FAILED, _S.CANCELLED, _S.SUPERSEDED})
_ABORT = frozenset({_S.CANCELLED, _S.SUPERSEDED})
TRANSITIONS: dict[WorkUnitState, frozenset[WorkUnitState]] = {
    _S.INTAKE: frozenset({_S.TRIAGED}) | _ABORT,
    _S.TRIAGED: frozenset({_S.SPECIFIED, _S.BLOCKED}) | _ABORT,
    _S.SPECIFIED: frozenset({_S.PLANNED, _S.BLOCKED}) | _ABORT,
    _S.PLANNED: frozenset({_S.APPROVAL_PENDING, _S.APPROVED, _S.BLOCKED}) | _ABORT,
    _S.APPROVAL_PENDING: frozenset({_S.APPROVED, _S.BLOCKED}) | _ABORT,
    _S.APPROVED: frozenset({_S.DISPATCHED, _S.BLOCKED}) | _ABORT,
    _S.DISPATCHED: frozenset({_S.IMPLEMENTING, _S.BLOCKED}) | _ABORT,
    _S.IMPLEMENTING: frozenset({_S.VERIFYING, _S.FAILED}) | _ABORT,
    _S.VERIFYING: frozenset({_S.REVIEWING, _S.FAILED}) | _ABORT,
    _S.REVIEWING: frozenset({_S.READY_FOR_HUMAN_MERGE, _S.REPAIR_NEEDED, _S.BLOCKED, _S.FAILED})
    | _ABORT,
    _S.REPAIR_NEEDED: frozenset({_S.REPAIRING, _S.BLOCKED}) | _ABORT,
    _S.REPAIRING: frozenset({_S.RE_REVIEWING, _S.FAILED, _S.BLOCKED}) | _ABORT,
    _S.RE_REVIEWING: frozenset({_S.READY_FOR_HUMAN_MERGE, _S.REPAIR_NEEDED, _S.BLOCKED, _S.FAILED})
    | _ABORT,
    _S.READY_FOR_HUMAN_MERGE: frozenset({_S.MERGED, _S.BLOCKED}) | _ABORT,
    _S.BLOCKED: frozenset({_S.TRIAGED, _S.DISPATCHED}) | _ABORT,
    _S.MERGED: frozenset(),
    _S.FAILED: frozenset(),
    _S.CANCELLED: frozenset(),
    _S.SUPERSEDED: frozenset(),
}

RUN_KINDS = frozenset({"planning", "context", "implementation", "verification", "review", "repair"})
_EXCLUSIVE_RUN_KINDS = frozenset({"implementation", "repair"})
_ACTOR_KINDS = frozenset({"human", "agent", "system"})
_ENVELOPE_KEYS = (
    "missionId",
    "missionVersion",
    "agentId",
    "adapter",
    "adapterVersion",
    "provider",
    "model",
    "promptSha256",
    "envelopeSha256",
)


@dataclass(frozen=True)
class TransitionRecord:
    sequence: int
    from_state: WorkUnitState
    to_state: WorkUnitState
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
class AgentRunRecord:
    run_id: str
    kind: str
    mission_id: str
    mission_version: str
    agent_id: str
    adapter: str
    adapter_version: str
    provider: str
    model: str
    prompt_sha256: str
    envelope_sha256: str
    work_ref: str
    input_refs: tuple[str, ...]
    context_pack_digest: str
    repair_cycle: int
    started_at: str
    event_key: str
    finished_at: str = ""
    result: str = "running"
    commit_sha: str = ""
    output_artifacts: tuple[tuple[str, str], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "runId": self.run_id,
            "kind": self.kind,
            "missionId": self.mission_id,
            "missionVersion": self.mission_version,
            "agentId": self.agent_id,
            "adapter": self.adapter,
            "adapterVersion": self.adapter_version,
            "provider": self.provider,
            "model": self.model,
            "promptSha256": self.prompt_sha256,
            "envelopeSha256": self.envelope_sha256,
            "workRef": self.work_ref,
            "inputRefs": list(self.input_refs),
            "contextPackDigest": self.context_pack_digest,
            "repairCycle": self.repair_cycle,
            "startedAt": self.started_at,
            "eventKey": self.event_key,
            "finishedAt": self.finished_at,
            "result": self.result,
            "commitSha": self.commit_sha,
            "outputArtifacts": dict(self.output_artifacts),
        }


class _WorkUnit:
    def __init__(
        self,
        unit_id: str,
        *,
        concurrency_group: str,
        depends_on: tuple[str, ...],
        created_at: str,
    ) -> None:
        self.unit_id = unit_id
        self.state = WorkUnitState.INTAKE
        self.concurrency_group = concurrency_group
        self.depends_on = depends_on
        self.repair_count = 0
        self.superseded_by = ""
        self.created_at = created_at
        self.transitions: list[TransitionRecord] = []
        self.runs: list[AgentRunRecord] = []
        self.event_keys: set[str] = set()

    def as_dict(self) -> dict[str, Any]:
        return {
            "unitId": self.unit_id,
            "state": self.state.value,
            "concurrencyGroup": self.concurrency_group,
            "dependsOn": list(self.depends_on),
            "repairCount": self.repair_count,
            "supersededBy": self.superseded_by,
            "createdAt": self.created_at,
            "transitions": [item.as_dict() for item in self.transitions],
            "runs": [item.as_dict() for item in self.runs],
            "eventKeys": sorted(self.event_keys),
        }


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise OrchestrationError(message)


class Orchestrator:
    """Bounded, auditable lifecycle engine for autonomous work units."""

    def __init__(
        self,
        *,
        max_repair_cycles: int = 2,
        human_merge_required: bool = True,
        registry: MissionRegistry | None = None,
    ) -> None:
        _require(human_merge_required, "human merge authority cannot be disabled")
        _require(0 <= max_repair_cycles <= 10, "max_repair_cycles must be between 0 and 10")
        self.max_repair_cycles = max_repair_cycles
        self.human_merge_required = True
        self._registry = registry
        self._units: dict[str, _WorkUnit] = {}

    # -- unit management ---------------------------------------------------

    def get(self, unit_id: str) -> _WorkUnit:
        unit = self._units.get(unit_id)
        if unit is None:
            raise OrchestrationError(f"unknown work unit: {unit_id}")
        return unit

    def create_unit(
        self,
        unit_id: str,
        *,
        event_key: str,
        timestamp: str,
        concurrency_group: str = "",
        depends_on: Sequence[str] = (),
    ) -> _WorkUnit:
        """Create a work unit; replaying the same event or ID is a no-op."""
        _require(bool(unit_id.strip()), "unit_id is required")
        _require(bool(event_key.strip()), "event_key is required")
        existing = self._units.get(unit_id)
        if existing is not None:
            return existing
        for unit in self._units.values():
            if event_key in unit.event_keys:
                return unit
        unit = _WorkUnit(
            unit_id,
            concurrency_group=concurrency_group or unit_id,
            depends_on=tuple(depends_on),
            created_at=timestamp,
        )
        unit.event_keys.add(event_key)
        self._units[unit_id] = unit
        return unit

    # -- transitions -------------------------------------------------------

    def transition(
        self,
        unit_id: str,
        to_state: WorkUnitState | str,
        *,
        actor: str,
        actor_kind: str,
        event_key: str,
        timestamp: str,
        reason: str = "",
    ) -> _WorkUnit:
        """Apply one validated lifecycle transition; duplicates are no-ops."""
        unit = self.get(unit_id)
        target = WorkUnitState(to_state)
        _require(actor_kind in _ACTOR_KINDS, f"unknown actor kind: {actor_kind}")
        _require(bool(event_key.strip()), "event_key is required")
        if event_key in unit.event_keys:
            return unit
        _require(
            unit.state not in TERMINAL_STATES,
            f"work unit {unit_id} is terminal ({unit.state.value}) and cannot change",
        )
        _require(
            target in TRANSITIONS[unit.state],
            f"invalid transition for {unit_id}: {unit.state.value} -> {target.value}",
        )
        if target is WorkUnitState.MERGED:
            _require(
                actor_kind == "human",
                "merge requires a human actor; agents never hold merge authority",
            )
        if target is WorkUnitState.APPROVED:
            _require(
                actor_kind != "agent",
                "approval cannot be granted by an agent actor",
            )
            if unit.state is WorkUnitState.APPROVAL_PENDING:
                _require(
                    actor_kind == "human",
                    "explicit approval requires a human actor",
                )
        if unit.state is WorkUnitState.BLOCKED and target not in _ABORT:
            _require(
                actor_kind == "human",
                "resuming blocked work requires a human decision",
            )
        if target is WorkUnitState.DISPATCHED:
            blocking = [
                dependency
                for dependency in unit.depends_on
                if self.get(dependency).state is not WorkUnitState.MERGED
            ]
            _require(
                not blocking,
                f"work unit {unit_id} is dependency-blocked by: " + ", ".join(blocking),
            )
        self._record_transition(
            unit,
            target,
            actor=actor,
            actor_kind=actor_kind,
            event_key=event_key,
            timestamp=timestamp,
            reason=reason,
        )
        return unit

    def _record_transition(
        self,
        unit: _WorkUnit,
        target: WorkUnitState,
        *,
        actor: str,
        actor_kind: str,
        event_key: str,
        timestamp: str,
        reason: str,
    ) -> None:
        unit.transitions.append(
            TransitionRecord(
                sequence=len(unit.transitions) + 1,
                from_state=unit.state,
                to_state=target,
                actor=actor,
                actor_kind=actor_kind,
                event_key=event_key,
                timestamp=timestamp,
                reason=reason,
            )
        )
        unit.event_keys.add(event_key)
        unit.state = target

    def cancel(
        self, unit_id: str, *, actor: str, actor_kind: str, event_key: str, timestamp: str
    ) -> _WorkUnit:
        return self.transition(
            unit_id,
            WorkUnitState.CANCELLED,
            actor=actor,
            actor_kind=actor_kind,
            event_key=event_key,
            timestamp=timestamp,
            reason="cancelled",
        )

    def supersede(
        self,
        unit_id: str,
        *,
        by_unit_id: str,
        actor: str,
        actor_kind: str,
        event_key: str,
        timestamp: str,
    ) -> _WorkUnit:
        unit = self.transition(
            unit_id,
            WorkUnitState.SUPERSEDED,
            actor=actor,
            actor_kind=actor_kind,
            event_key=event_key,
            timestamp=timestamp,
            reason=f"superseded by {by_unit_id}",
        )
        unit.superseded_by = by_unit_id
        return unit

    def manual_override(
        self,
        unit_id: str,
        to_state: WorkUnitState | str,
        *,
        actor: str,
        event_key: str,
        timestamp: str,
        reason: str,
    ) -> _WorkUnit:
        """Let a human force a non-merge state outside the normal graph."""
        unit = self.get(unit_id)
        target = WorkUnitState(to_state)
        _require(bool(reason.strip()), "manual overrides must state a reason")
        _require(
            target is not WorkUnitState.MERGED,
            "manual override cannot mint merge authority; merge from ready-for-human-merge instead",
        )
        _require(
            unit.state not in TERMINAL_STATES,
            f"work unit {unit_id} is terminal and cannot be overridden",
        )
        if event_key in unit.event_keys:
            return unit
        self._record_transition(
            unit,
            target,
            actor=actor,
            actor_kind="human",
            event_key=event_key,
            timestamp=timestamp,
            reason=f"manual-override: {reason}",
        )
        return unit

    # -- agent runs --------------------------------------------------------

    def _running(self, kind_filter: frozenset[str]) -> list[tuple[_WorkUnit, AgentRunRecord]]:
        return [
            (unit, run)
            for unit in self._units.values()
            for run in unit.runs
            if run.result == "running" and run.kind in kind_filter
        ]

    def start_run(
        self,
        unit_id: str,
        kind: str,
        envelope: Mapping[str, Any],
        *,
        event_key: str,
        timestamp: str,
    ) -> AgentRunRecord:
        """Record an agent run pinned to its dispatch envelope.

        Duplicate events return the already-started run instead of a new one.
        Implementation and repair runs are serialized per concurrency group.
        """
        unit = self.get(unit_id)
        _require(kind in RUN_KINDS, f"unknown run kind: {kind}")
        _require(bool(event_key.strip()), "event_key is required")
        for run in unit.runs:
            if run.event_key == event_key:
                return run
        missing = sorted(name for name in _ENVELOPE_KEYS if not envelope.get(name))
        _require(not missing, "dispatch envelope is missing: " + ", ".join(missing))

        if kind == "implementation":
            _require(
                unit.state is WorkUnitState.DISPATCHED,
                f"implementation requires state dispatched, not {unit.state.value}",
            )
        elif kind == "repair":
            _require(
                unit.state is WorkUnitState.REPAIR_NEEDED,
                f"repair requires state repair-needed, not {unit.state.value}",
            )
            _require(
                unit.repair_count < self.max_repair_cycles,
                f"repair budget exhausted for {unit_id}",
            )
        elif kind == "review":
            _require(
                unit.state in (WorkUnitState.REVIEWING, WorkUnitState.RE_REVIEWING),
                f"review requires a reviewing state, not {unit.state.value}",
            )

        if kind in _EXCLUSIVE_RUN_KINDS:
            for other_unit, run in self._running(_EXCLUSIVE_RUN_KINDS):
                _require(
                    other_unit.concurrency_group != unit.concurrency_group,
                    f"concurrency group {unit.concurrency_group} already has an active "
                    f"{run.kind} run ({run.run_id}); dispatch is serialized",
                )

        record = AgentRunRecord(
            run_id=f"{unit.unit_id}:run-{len(unit.runs) + 1:03d}",
            kind=kind,
            mission_id=str(envelope["missionId"]),
            mission_version=str(envelope["missionVersion"]),
            agent_id=str(envelope["agentId"]),
            adapter=str(envelope["adapter"]),
            adapter_version=str(envelope["adapterVersion"]),
            provider=str(envelope["provider"]),
            model=str(envelope["model"]),
            prompt_sha256=str(envelope["promptSha256"]),
            envelope_sha256=str(envelope["envelopeSha256"]),
            work_ref=str(envelope.get("workRef", unit.unit_id)),
            input_refs=tuple(envelope.get("inputRefs", ())),
            context_pack_digest=str(envelope.get("contextPackDigest") or ""),
            repair_cycle=unit.repair_count + (1 if kind == "repair" else 0),
            started_at=timestamp,
            event_key=event_key,
        )
        unit.runs.append(record)
        unit.event_keys.add(event_key)
        if kind == "implementation":
            self._record_transition(
                unit,
                WorkUnitState.IMPLEMENTING,
                actor=record.agent_id,
                actor_kind="agent",
                event_key=f"{event_key}:implementing",
                timestamp=timestamp,
                reason=f"run {record.run_id} started",
            )
        elif kind == "repair":
            unit.repair_count += 1
            self._record_transition(
                unit,
                WorkUnitState.REPAIRING,
                actor=record.agent_id,
                actor_kind="agent",
                event_key=f"{event_key}:repairing",
                timestamp=timestamp,
                reason=f"repair cycle {unit.repair_count} started",
            )
        return record

    def finish_run(
        self,
        unit_id: str,
        run_id: str,
        *,
        result: str,
        timestamp: str,
        commit_sha: str = "",
        output_artifacts: Mapping[str, str] | None = None,
    ) -> AgentRunRecord:
        unit = self.get(unit_id)
        _require(result in {"succeeded", "failed"}, "result must be succeeded or failed")
        for index, run in enumerate(unit.runs):
            if run.run_id == run_id:
                if run.result != "running":
                    return run
                updated = replace(
                    run,
                    finished_at=timestamp,
                    result=result,
                    commit_sha=commit_sha,
                    output_artifacts=tuple(sorted((output_artifacts or {}).items())),
                )
                unit.runs[index] = updated
                return updated
        raise OrchestrationError(f"unknown run: {run_id}")

    def agent_history(self, unit_id: str) -> dict[str, str]:
        """Mission -> agent assignments, for independence-aware dispatch."""
        return {run.mission_id: run.agent_id for run in self.get(unit_id).runs}

    def select_repair_agent(
        self,
        unit_id: str,
        agents: Sequence[AgentProfile],
        mission_id: str = "repair-agent",
    ) -> AgentProfile:
        """Route reviewer findings to an eligible repair agent automatically."""
        _require(self._registry is not None, "a mission registry is required for dispatch")
        assert self._registry is not None
        return self._registry.select_agent(mission_id, agents, history=self.agent_history(unit_id))

    def select_review_agent(
        self,
        unit_id: str,
        agents: Sequence[AgentProfile],
        mission_id: str = "code-reviewer",
    ) -> AgentProfile:
        """Select an independent reviewer; implementers are never eligible."""
        _require(self._registry is not None, "a mission registry is required for dispatch")
        assert self._registry is not None
        return self._registry.select_agent(mission_id, agents, history=self.agent_history(unit_id))

    # -- verification and review ------------------------------------------

    def record_verification(
        self,
        unit_id: str,
        *,
        passed: bool,
        event_key: str,
        timestamp: str,
        actor: str = "deterministic-verifier",
    ) -> _WorkUnit:
        unit = self.get(unit_id)
        if event_key in unit.event_keys:
            return unit
        _require(
            unit.state is WorkUnitState.VERIFYING,
            f"verification result requires state verifying, not {unit.state.value}",
        )
        return self.transition(
            unit_id,
            WorkUnitState.REVIEWING if passed else WorkUnitState.FAILED,
            actor=actor,
            actor_kind="system",
            event_key=event_key,
            timestamp=timestamp,
            reason="verification passed" if passed else "verification failed",
        )

    def record_review(
        self,
        unit_id: str,
        run_id: str,
        *,
        findings: int,
        event_key: str,
        timestamp: str,
    ) -> _WorkUnit:
        """Apply an independent review outcome with bounded repair routing."""
        unit = self.get(unit_id)
        if event_key in unit.event_keys:
            return unit
        _require(findings >= 0, "findings cannot be negative")
        _require(
            unit.state in (WorkUnitState.REVIEWING, WorkUnitState.RE_REVIEWING),
            f"review result requires a reviewing state, not {unit.state.value}",
        )
        review = next((run for run in unit.runs if run.run_id == run_id), None)
        _require(review is not None, f"unknown review run: {run_id}")
        assert review is not None
        _require(review.kind == "review", f"run {run_id} is not a review run")
        _require(
            review.repair_cycle == unit.repair_count,
            f"review {run_id} predates repair cycle {unit.repair_count}; every repair "
            "requires a fresh independent review",
        )
        if findings == 0:
            return self.transition(
                unit_id,
                WorkUnitState.READY_FOR_HUMAN_MERGE,
                actor=review.agent_id,
                actor_kind="agent",
                event_key=event_key,
                timestamp=timestamp,
                reason="review passed with no findings",
            )
        if unit.repair_count >= self.max_repair_cycles:
            return self.transition(
                unit_id,
                WorkUnitState.BLOCKED,
                actor=review.agent_id,
                actor_kind="agent",
                event_key=event_key,
                timestamp=timestamp,
                reason=(
                    f"escalated: {findings} finding(s) after "
                    f"{unit.repair_count} repair cycle(s); human decision required"
                ),
            )
        return self.transition(
            unit_id,
            WorkUnitState.REPAIR_NEEDED,
            actor=review.agent_id,
            actor_kind="agent",
            event_key=event_key,
            timestamp=timestamp,
            reason=f"review found {findings} finding(s)",
        )

    def record_repair_complete(
        self, unit_id: str, *, event_key: str, timestamp: str, actor: str
    ) -> _WorkUnit:
        """A repair commit always routes to a fresh independent re-review."""
        return self.transition(
            unit_id,
            WorkUnitState.RE_REVIEWING,
            actor=actor,
            actor_kind="agent",
            event_key=event_key,
            timestamp=timestamp,
            reason="repair complete; fresh independent review required",
        )

    # -- reporting and persistence ----------------------------------------

    def status_document(self, unit_id: str) -> str:
        """One updateable status body; callers upsert the marked comment."""
        unit = self.get(unit_id)
        lines = [
            STATUS_MARKER,
            f"## Agentic SDLC status: `{unit.unit_id}`",
            "",
            f"- **State:** `{unit.state.value}`",
            f"- **Repair cycles:** {unit.repair_count}/{self.max_repair_cycles}",
            f"- **Runs:** {len(unit.runs)}",
        ]
        if unit.depends_on:
            lines.append("- **Depends on:** " + ", ".join(unit.depends_on))
        if unit.superseded_by:
            lines.append(f"- **Superseded by:** {unit.superseded_by}")
        if unit.transitions:
            latest = unit.transitions[-1]
            lines.append(
                f"- **Last transition:** {latest.from_state.value} → "
                f"{latest.to_state.value} by {latest.actor} ({latest.actor_kind}) "
                f"at {latest.timestamp}"
            )
            if latest.reason:
                lines.append(f"- **Reason:** {latest.reason}")
        if unit.runs:
            lines.extend(["", "| run | kind | mission | agent | result |", "|---|---|---|---|---|"])
            lines.extend(
                f"| `{run.run_id}` | {run.kind} | {run.mission_id}@{run.mission_version} "
                f"| {run.agent_id} ({run.model}) | {run.result} |"
                for run in unit.runs
            )
        return "\n".join(lines) + "\n"

    def as_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "lifecycleVersion": LIFECYCLE_VERSION,
            "maxRepairCycles": self.max_repair_cycles,
            "humanMergeRequired": True,
            "units": {unit_id: unit.as_dict() for unit_id, unit in sorted(self._units.items())},
        }

    @classmethod
    def from_dict(
        cls, document: Mapping[str, Any], *, registry: MissionRegistry | None = None
    ) -> Orchestrator:
        if document.get("schemaVersion") != 1 or document.get("lifecycleVersion") != 1:
            raise OrchestrationError("unsupported orchestration state document")
        engine = cls(
            max_repair_cycles=int(document.get("maxRepairCycles", 2)),
            registry=registry,
        )
        units = document.get("units", {})
        if not isinstance(units, Mapping):
            raise OrchestrationError("units must be a mapping")
        for unit_id, raw in units.items():
            unit = _WorkUnit(
                unit_id,
                concurrency_group=str(raw.get("concurrencyGroup", unit_id)),
                depends_on=tuple(raw.get("dependsOn", ())),
                created_at=str(raw.get("createdAt", "")),
            )
            unit.state = WorkUnitState(raw.get("state", "intake"))
            unit.repair_count = int(raw.get("repairCount", 0))
            unit.superseded_by = str(raw.get("supersededBy", ""))
            unit.event_keys = set(raw.get("eventKeys", ()))
            unit.transitions = [
                TransitionRecord(
                    sequence=int(item["sequence"]),
                    from_state=WorkUnitState(item["from"]),
                    to_state=WorkUnitState(item["to"]),
                    actor=str(item["actor"]),
                    actor_kind=str(item["actorKind"]),
                    event_key=str(item["eventKey"]),
                    timestamp=str(item["timestamp"]),
                    reason=str(item.get("reason", "")),
                )
                for item in raw.get("transitions", ())
            ]
            unit.runs = [
                AgentRunRecord(
                    run_id=str(item["runId"]),
                    kind=str(item["kind"]),
                    mission_id=str(item["missionId"]),
                    mission_version=str(item["missionVersion"]),
                    agent_id=str(item["agentId"]),
                    adapter=str(item["adapter"]),
                    adapter_version=str(item["adapterVersion"]),
                    provider=str(item["provider"]),
                    model=str(item["model"]),
                    prompt_sha256=str(item["promptSha256"]),
                    envelope_sha256=str(item["envelopeSha256"]),
                    work_ref=str(item.get("workRef", "")),
                    input_refs=tuple(item.get("inputRefs", ())),
                    context_pack_digest=str(item.get("contextPackDigest", "")),
                    repair_cycle=int(item.get("repairCycle", 0)),
                    started_at=str(item["startedAt"]),
                    event_key=str(item.get("eventKey", "")),
                    finished_at=str(item.get("finishedAt", "")),
                    result=str(item.get("result", "running")),
                    commit_sha=str(item.get("commitSha", "")),
                    output_artifacts=tuple(sorted(dict(item.get("outputArtifacts", {})).items())),
                )
                for item in raw.get("runs", ())
            ]
            engine._units[unit_id] = unit
        return engine
