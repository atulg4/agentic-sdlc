"""Credential-isolated protected merge execution boundary.

The executor accepts only trusted normalized gate evidence and an exact pull-request
head SHA. It has no bypass mode and never performs deployment or broker operations.
A transport adapter may implement :class:`MergeGateway` with repository-scoped
credentials that remain separate from planning, implementation, and review agents.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from .autonomy import AutonomyGateEvidence, AutonomyPhase, evaluate_autonomy

__all__ = [
    "MergeGateway",
    "MergeGatewayResult",
    "MergeOutcome",
    "MergeRequest",
    "ProtectedMergeExecutor",
]

_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class MergeRequest:
    repository: str
    pull_request_number: int
    expected_head_sha: str


@dataclass(frozen=True)
class MergeGatewayResult:
    merged: bool
    merge_sha: str = ""
    message: str = ""


@dataclass(frozen=True)
class MergeOutcome:
    allowed: bool
    merged: bool
    merge_sha: str
    reason: str


class MergeGateway(Protocol):
    def merge(
        self,
        *,
        repository: str,
        pull_request_number: int,
        expected_head_sha: str,
    ) -> MergeGatewayResult: ...


class ProtectedMergeExecutor:
    """Perform a merge only after every trusted autonomous merge gate passes."""

    @staticmethod
    def _validate_request(request: MergeRequest) -> str | None:
        if not _REPOSITORY.fullmatch(request.repository):
            return "invalid repository identifier"
        if request.pull_request_number <= 0:
            return "pull request number must be positive"
        if not _SHA256_HEX.fullmatch(request.expected_head_sha):
            return "expected head SHA must be an exact lowercase 40-character commit SHA"
        return None

    def execute(
        self,
        request: MergeRequest,
        evidence: AutonomyGateEvidence,
        gateway: MergeGateway,
    ) -> MergeOutcome:
        invalid = self._validate_request(request)
        if invalid is not None:
            return MergeOutcome(False, False, "", invalid)

        decision = evaluate_autonomy(AutonomyPhase.MERGE, evidence)
        if not decision.allowed:
            return MergeOutcome(False, False, "", "; ".join(decision.reasons))

        result = gateway.merge(
            repository=request.repository,
            pull_request_number=request.pull_request_number,
            expected_head_sha=request.expected_head_sha,
        )
        if not result.merged:
            return MergeOutcome(True, False, "", result.message or "repository merge was rejected")
        if not _SHA256_HEX.fullmatch(result.merge_sha):
            return MergeOutcome(True, False, "", "merge gateway returned an invalid merge SHA")
        return MergeOutcome(True, True, result.merge_sha, result.message or "merged")
