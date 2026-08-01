"""Provider-neutral domain models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class WorkKind(StrEnum):
    ISSUE = "issue"
    CHANGE_REQUEST = "change_request"
    PIPELINE = "pipeline"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TaskSpec:
    title: str
    summary: str
    acceptance_criteria: tuple[str, ...]
    required_tests: tuple[str, ...]
    non_goals: tuple[str, ...]
    dependencies: tuple[int, ...]
    labels: tuple[str, ...] = ()
    raw_body: str = ""


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    risk: RiskLevel
    reasons: tuple[str, ...]
    required_gates: tuple[str, ...]
    automatic_merge_allowed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "risk": self.risk.value,
            "reasons": list(self.reasons),
            "requiredGates": list(self.required_gates),
            "automaticMergeAllowed": self.automatic_merge_allowed,
        }


@dataclass(frozen=True)
class WorkEvent:
    provider: str
    kind: WorkKind
    action: str
    repository: str
    number: int | None
    title: str
    body: str
    labels: tuple[str, ...]
    actor: str
    raw: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "kind": self.kind.value,
            "action": self.action,
            "repository": self.repository,
            "number": self.number,
            "title": self.title,
            "body": self.body,
            "labels": list(self.labels),
            "actor": self.actor,
        }
