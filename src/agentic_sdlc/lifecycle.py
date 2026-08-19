"""Deterministic autonomous lifecycle advancement for Forge-managed work.

The driver never performs repository, deployment, or broker side effects. It only
advances the trusted orchestration state machine when explicit evidence permits
that transition. External workflow adapters remain responsible for executing
Claude Max OAuth stages and for repository-protected merge operations.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .autonomy import AutonomyGateEvidence, AutonomyPhase, evaluate_autonomy
from .orchestration import Orchestrator, WorkUnitState

__all__ = [
    "AutonomousLifecycleDriver",
    "LifecycleAdvance",
    "StageResult",
]


class StageResult(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    CHANGES_REQUESTED = "changes-requested"


@dataclass(frozen=True)
class LifecycleAdvance:
    unit_id: str
    from_state: WorkUnitState
    to_state: WorkUnitState
    advanced: bool
    reason: str


class AutonomousLifecycleDriver:
    """Advance one work unit by at most one auditable lifecycle transition."""

    def __init__(self, orchestrator: Orchestrator) -> None:
        self._orchestrator = orchestrator

    def _transition(
        self,
        unit_id: str,
        target: WorkUnitState,
        *,
        timestamp: str,
        event_key: str,
        reason: str,
    ) -> LifecycleAdvance:
        unit = self._orchestrator.get(unit_id)
        before = unit.state
        self._orchestrator.transition(
            unit_id,
            target,
            actor="forge-lifecycle",
            actor_kind="system",
            event_key=event_key,
            timestamp=timestamp,
            reason=reason,
        )
        return LifecycleAdvance(unit_id, before, unit.state, before != unit.state, reason)

    def _waiting(self, unit_id: str, reason: str) -> LifecycleAdvance:
        unit = self._orchestrator.get(unit_id)
        return LifecycleAdvance(unit_id, unit.state, unit.state, False, reason)

    def advance(
        self,
        unit_id: str,
        *,
        timestamp: str,
        event_key: str,
        autonomy: AutonomyGateEvidence,
        plan: StageResult = StageResult.PENDING,
        implementation: StageResult = StageResult.PENDING,
        verification: StageResult = StageResult.PENDING,
        review: StageResult = StageResult.PENDING,
        repair: StageResult = StageResult.PENDING,
    ) -> LifecycleAdvance:
        """Advance exactly one state, or return an explicit waiting decision."""

        unit = self._orchestrator.get(unit_id)
        state = unit.state

        if state is WorkUnitState.TRIAGED:
            return self._transition(
                unit_id,
                WorkUnitState.SPECIFIED,
                timestamp=timestamp,
                event_key=event_key,
                reason="forge-managed work normalized for autonomous planning",
            )
        if state is WorkUnitState.SPECIFIED:
            if plan is StageResult.PASSED:
                return self._transition(
                    unit_id,
                    WorkUnitState.PLANNED,
                    timestamp=timestamp,
                    event_key=event_key,
                    reason="deterministic planning stage completed",
                )
            if plan is StageResult.FAILED:
                return self._transition(
                    unit_id,
                    WorkUnitState.BLOCKED,
                    timestamp=timestamp,
                    event_key=event_key,
                    reason="planning failed; autonomous progress stopped",
                )
            return self._waiting(unit_id, "waiting for planning result")
        if state is WorkUnitState.PLANNED:
            decision = evaluate_autonomy(AutonomyPhase.IMPLEMENTATION, autonomy)
            if not decision.allowed:
                return self._transition(
                    unit_id,
                    WorkUnitState.BLOCKED,
                    timestamp=timestamp,
                    event_key=event_key,
                    reason="; ".join(decision.reasons),
                )
            return self._transition(
                unit_id,
                WorkUnitState.APPROVED,
                timestamp=timestamp,
                event_key=event_key,
                reason="autonomous implementation policy gates passed",
            )
        if state is WorkUnitState.APPROVED:
            return self._transition(
                unit_id,
                WorkUnitState.DISPATCHED,
                timestamp=timestamp,
                event_key=event_key,
                reason="implementation dispatch authorized by policy",
            )
        if state is WorkUnitState.DISPATCHED:
            return self._transition(
                unit_id,
                WorkUnitState.IMPLEMENTING,
                timestamp=timestamp,
                event_key=event_key,
                reason="implementation stage started",
            )
        if state is WorkUnitState.IMPLEMENTING:
            if implementation is StageResult.PASSED:
                return self._transition(
                    unit_id,
                    WorkUnitState.VERIFYING,
                    timestamp=timestamp,
                    event_key=event_key,
                    reason="implementation completed; deterministic verification required",
                )
            if implementation is StageResult.FAILED:
                return self._transition(
                    unit_id,
                    WorkUnitState.FAILED,
                    timestamp=timestamp,
                    event_key=event_key,
                    reason="implementation failed",
                )
            return self._waiting(unit_id, "waiting for implementation result")
        if state is WorkUnitState.VERIFYING:
            if verification is StageResult.PASSED:
                return self._transition(
                    unit_id,
                    WorkUnitState.REVIEWING,
                    timestamp=timestamp,
                    event_key=event_key,
                    reason="deterministic verification passed",
                )
            if verification is StageResult.FAILED:
                if unit.repair_count >= self._orchestrator.max_repair_cycles:
                    return self._transition(
                        unit_id,
                        WorkUnitState.BLOCKED,
                        timestamp=timestamp,
                        event_key=event_key,
                        reason="deterministic verification failed; repair budget exhausted",
                    )
                return self._transition(
                    unit_id,
                    WorkUnitState.REPAIR_NEEDED,
                    timestamp=timestamp,
                    event_key=event_key,
                    reason="deterministic verification failed; bounded repair required",
                )
            return self._waiting(unit_id, "waiting for deterministic verification")
        if state in {WorkUnitState.REVIEWING, WorkUnitState.RE_REVIEWING}:
            if review is StageResult.PASSED:
                return self._transition(
                    unit_id,
                    WorkUnitState.READY_FOR_HUMAN_MERGE,
                    timestamp=timestamp,
                    event_key=event_key,
                    reason="independent review approved exact verified change",
                )
            if review is StageResult.CHANGES_REQUESTED:
                return self._transition(
                    unit_id,
                    WorkUnitState.REPAIR_NEEDED,
                    timestamp=timestamp,
                    event_key=event_key,
                    reason="independent review requested bounded repair",
                )
            if review is StageResult.FAILED:
                return self._transition(
                    unit_id,
                    WorkUnitState.FAILED,
                    timestamp=timestamp,
                    event_key=event_key,
                    reason="independent review execution failed",
                )
            return self._waiting(unit_id, "waiting for independent review")
        if state is WorkUnitState.REPAIR_NEEDED:
            return self._transition(
                unit_id,
                WorkUnitState.REPAIRING,
                timestamp=timestamp,
                event_key=event_key,
                reason="bounded repair cycle started",
            )
        if state is WorkUnitState.REPAIRING:
            if repair is StageResult.PASSED:
                return self._transition(
                    unit_id,
                    WorkUnitState.RE_REVIEWING,
                    timestamp=timestamp,
                    event_key=event_key,
                    reason="repair completed; independent re-review required",
                )
            if repair is StageResult.FAILED:
                return self._transition(
                    unit_id,
                    WorkUnitState.FAILED,
                    timestamp=timestamp,
                    event_key=event_key,
                    reason="bounded repair failed",
                )
            return self._waiting(unit_id, "waiting for bounded repair")
        if state is WorkUnitState.READY_FOR_HUMAN_MERGE:
            merge = evaluate_autonomy(AutonomyPhase.MERGE, autonomy)
            if merge.allowed:
                return self._waiting(
                    unit_id,
                    "merge policy gates passed; protected repository merge executor required",
                )
            return self._transition(
                unit_id,
                WorkUnitState.BLOCKED,
                timestamp=timestamp,
                event_key=event_key,
                reason="; ".join(merge.reasons),
            )

        return self._waiting(unit_id, f"no autonomous transition from {state.value}")
