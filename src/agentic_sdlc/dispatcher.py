"""Event-driven intake for explicitly Forge-managed work.

This module is deliberately transport-agnostic. Webhook/workflow adapters normalize
provider payloads into :class:`WorkEvent`; the dispatcher then makes one fail-closed,
idempotent decision about whether the event may enter autonomous orchestration.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .models import WorkEvent
from .orchestration import OrchestrationError, Orchestrator, WorkUnitState

__all__ = [
    "AutonomousIntakeDispatcher",
    "DispatchDecision",
    "DispatchError",
    "event_fingerprint",
]


class DispatchError(ValueError):
    """Raised when an autonomous intake event is malformed or unsafe to route."""


@dataclass(frozen=True)
class DispatchDecision:
    accepted: bool
    unit_id: str
    event_key: str
    state: str
    reason: str

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "accepted": self.accepted,
            "unitId": self.unit_id,
            "eventKey": self.event_key,
            "state": self.state,
            "reason": self.reason,
        }


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DispatchError(message)


def event_fingerprint(event: WorkEvent) -> str:
    """Return a deterministic idempotency key without serializing provider secrets."""

    payload = {
        "provider": event.provider,
        "kind": event.kind.value,
        "action": event.action,
        "repository": event.repository,
        "number": event.number,
        "title": event.title,
        "labels": sorted(event.labels),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "forge-event:" + hashlib.sha256(encoded).hexdigest()


class AutonomousIntakeDispatcher:
    """Admit explicitly marked work into the durable orchestration lifecycle."""

    def __init__(
        self,
        orchestrator: Orchestrator,
        *,
        managed_label: str = "forge-managed",
        blocked_labels: tuple[str, ...] = ("forge-paused", "security-review-required"),
    ) -> None:
        _require(bool(managed_label.strip()), "managed_label is required")
        self._orchestrator = orchestrator
        self.managed_label = managed_label
        self.blocked_labels = frozenset(blocked_labels)

    def dispatch(self, event: WorkEvent, *, timestamp: str) -> DispatchDecision:
        """Normalize one event into an idempotent Forge work unit when policy permits."""

        labels = frozenset(event.labels)
        key = event_fingerprint(event)
        if self.managed_label not in labels:
            return DispatchDecision(False, "", key, "ignored", "missing forge-managed marker")
        blocked = sorted(labels & self.blocked_labels)
        if blocked:
            return DispatchDecision(
                False,
                "",
                key,
                "blocked",
                "autonomous intake blocked by label: " + ", ".join(blocked),
            )

        _require(bool(event.provider.strip()), "provider is required")
        _require(bool(event.repository.strip()), "repository is required")
        _require(event.number is not None and event.number > 0, "positive work item number is required")
        _require(bool(timestamp.strip()), "timestamp is required")

        unit_id = f"{event.provider}:{event.repository}:{event.kind.value}:{event.number}"
        unit = self._orchestrator.create_unit(
            unit_id,
            event_key=key,
            timestamp=timestamp,
            concurrency_group=f"repo:{event.repository}",
        )
        if unit.state is WorkUnitState.INTAKE:
            try:
                unit = self._orchestrator.transition(
                    unit_id,
                    WorkUnitState.TRIAGED,
                    actor="forge-dispatcher",
                    actor_kind="system",
                    event_key=key + ":triaged",
                    timestamp=timestamp,
                    reason="explicit forge-managed autonomous intake",
                )
            except OrchestrationError as exc:
                raise DispatchError(str(exc)) from exc

        return DispatchDecision(True, unit_id, key, unit.state.value, "accepted")
