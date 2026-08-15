"""End-to-end trusted evidence path for autonomous protected merges.

This service is the narrow seam between read-only GitHub evidence collection and the
credential-isolated merge gateway. Callers may supply policy context that GitHub
cannot infer (bounded scope, risk, security classification), but they cannot assert
that required checks passed, conversations are resolved, branch protection allows
merge, or substitute a different PR head.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .github_evidence import GitHubPullRequestEvidence
from .merge_evidence import MergeEvidencePolicy, normalize_merge_evidence
from .merge_executor import MergeGateway, MergeOutcome, MergeRequest, ProtectedMergeExecutor

__all__ = ["ProtectedMergeContext", "TrustedProtectedMergeService"]


class MergeEvidenceCollector(Protocol):
    def collect(
        self, *, repository: str, pull_request_number: int
    ) -> GitHubPullRequestEvidence: ...


@dataclass(frozen=True)
class ProtectedMergeContext:
    """Trusted non-GitHub policy facts required to evaluate autonomous merge."""

    risk_level: str
    scope_bounded: bool
    security_clear: bool
    deployment_requested: bool = False
    broker_access_requested: bool = False


class TrustedProtectedMergeService:
    """Collect exact-head GitHub facts, normalize gates, then execute protected merge."""

    def __init__(self, *, executor: ProtectedMergeExecutor | None = None) -> None:
        self._executor = executor or ProtectedMergeExecutor()

    def execute(
        self,
        request: MergeRequest,
        *,
        context: ProtectedMergeContext,
        policy: MergeEvidencePolicy,
        collector: MergeEvidenceCollector,
        gateway: MergeGateway,
    ) -> MergeOutcome:
        """Attempt a merge using only freshly collected exact-head gate evidence."""

        try:
            collected = collector.collect(
                repository=request.repository,
                pull_request_number=request.pull_request_number,
            )
        except Exception as exc:  # collection failure is a policy denial, never a merge attempt
            return MergeOutcome(
                False,
                False,
                "",
                f"trusted GitHub evidence collection failed: {type(exc).__name__}",
            )

        if collected.head_sha != request.expected_head_sha:
            return MergeOutcome(
                False,
                False,
                "",
                "pull request head changed after merge request was prepared",
            )

        try:
            evidence = normalize_merge_evidence(
                expected_head_sha=collected.head_sha,
                labels=collected.labels,
                risk_level=context.risk_level,
                scope_bounded=context.scope_bounded,
                security_clear=context.security_clear,
                branch_protection_allows=collected.branch_protection_allows,
                unresolved_conversations=collected.unresolved_conversations,
                deployment_requested=context.deployment_requested,
                broker_access_requested=context.broker_access_requested,
                checks=collected.checks,
                policy=policy,
            )
        except (TypeError, ValueError) as exc:
            return MergeOutcome(False, False, "", f"trusted merge evidence is invalid: {exc}")

        return self._executor.execute(request, evidence, gateway)
