from __future__ import annotations

from dataclasses import dataclass

from agentic_sdlc.github_evidence import GitHubPullRequestEvidence
from agentic_sdlc.merge_evidence import MergeEvidencePolicy
from agentic_sdlc.merge_executor import MergeGatewayResult, MergeRequest
from agentic_sdlc.protected_merge import ProtectedMergeContext, TrustedProtectedMergeService

HEAD = "a" * 40
MERGE_SHA = "b" * 40
POLICY = MergeEvidencePolicy(
    deterministic_checks=("test",),
    quality_checks=("quality",),
    independent_review_checks=("Independent agent review",),
    secret_scan_checks=("GitGuardian Security Checks",),
)
CONTEXT = ProtectedMergeContext(risk_level="low", scope_bounded=True, security_clear=True)
REQUEST = MergeRequest("atulg4/example", 7, HEAD)


def _checks(*, head: str = HEAD, test_conclusion: str = "success") -> tuple[dict[str, object], ...]:
    return (
        {"id": 1, "name": "test", "head_sha": head, "conclusion": test_conclusion},
        {"id": 2, "name": "quality", "head_sha": head, "conclusion": "success"},
        {"id": 3, "name": "Independent agent review", "head_sha": head, "conclusion": "success"},
        {"id": 4, "name": "GitGuardian Security Checks", "head_sha": head, "conclusion": "success"},
    )


@dataclass
class FakeCollector:
    evidence: GitHubPullRequestEvidence
    calls: int = 0

    def collect(self, *, repository: str, pull_request_number: int) -> GitHubPullRequestEvidence:
        assert repository == "atulg4/example"
        assert pull_request_number == 7
        self.calls += 1
        return self.evidence


@dataclass
class FailingCollector:
    def collect(self, *, repository: str, pull_request_number: int) -> GitHubPullRequestEvidence:
        raise RuntimeError("upstream unavailable with sensitive implementation detail")


@dataclass
class FakeGateway:
    calls: int = 0
    seen_head: str = ""

    def merge(
        self, *, repository: str, pull_request_number: int, expected_head_sha: str
    ) -> MergeGatewayResult:
        self.calls += 1
        self.seen_head = expected_head_sha
        return MergeGatewayResult(True, MERGE_SHA, "merged")


def _collector(
    *,
    head: str = HEAD,
    labels: tuple[str, ...] = ("forge-managed",),
    test: str = "success",
    branch_ready: bool = True,
    unresolved: int = 0,
) -> FakeCollector:
    evidence = GitHubPullRequestEvidence(
        head,
        labels,
        _checks(head=head, test_conclusion=test),
        branch_ready,
        unresolved,
    )
    return FakeCollector(evidence)


def _execute(collector: object, gateway: FakeGateway, *, context: ProtectedMergeContext = CONTEXT):
    return TrustedProtectedMergeService().execute(
        REQUEST,
        context=context,
        policy=POLICY,
        collector=collector,  # type: ignore[arg-type]
        gateway=gateway,
    )


def test_fresh_exact_head_green_evidence_reaches_merge_gateway_once() -> None:
    collector = _collector()
    gateway = FakeGateway()
    outcome = _execute(collector, gateway)
    assert outcome.allowed is True
    assert outcome.merged is True
    assert outcome.merge_sha == MERGE_SHA
    assert collector.calls == 1
    assert gateway.calls == 1
    assert gateway.seen_head == HEAD


def test_head_drift_fails_closed_before_merge_gateway() -> None:
    gateway = FakeGateway()
    outcome = _execute(_collector(head="c" * 40), gateway)
    assert outcome.allowed is False
    assert "head changed" in outcome.reason
    assert gateway.calls == 0


def test_failed_required_check_cannot_be_overridden_by_caller_context() -> None:
    gateway = FakeGateway()
    outcome = _execute(_collector(test="failure"), gateway)
    assert outcome.allowed is False
    assert "deterministic verification has not passed" in outcome.reason
    assert gateway.calls == 0


def test_missing_forge_managed_label_blocks_merge() -> None:
    gateway = FakeGateway()
    outcome = _execute(_collector(labels=()), gateway)
    assert outcome.allowed is False
    assert "not explicitly forge-managed" in outcome.reason
    assert gateway.calls == 0


def test_branch_readiness_and_conversation_state_come_from_collector() -> None:
    for collector, message in (
        (_collector(branch_ready=False), "branch protection does not allow merge"),
        (_collector(unresolved=2), "review conversations remain unresolved"),
    ):
        gateway = FakeGateway()
        outcome = _execute(collector, gateway)
        assert outcome.allowed is False
        assert message in outcome.reason
        assert gateway.calls == 0


def test_context_has_no_branch_or_conversation_override_fields() -> None:
    fields = ProtectedMergeContext.__dataclass_fields__
    assert "branch_protection_allows" not in fields
    assert "unresolved_conversations" not in fields


def test_collection_failure_is_bounded_and_never_reaches_gateway() -> None:
    gateway = FakeGateway()
    outcome = _execute(FailingCollector(), gateway)
    assert outcome.allowed is False
    assert outcome.reason == "trusted GitHub evidence collection failed: RuntimeError"
    assert "sensitive implementation detail" not in outcome.reason
    assert gateway.calls == 0


def test_sensitive_context_remains_denied() -> None:
    gateway = FakeGateway()
    context = ProtectedMergeContext(
        risk_level="low",
        scope_bounded=True,
        security_clear=True,
        broker_access_requested=True,
    )
    outcome = _execute(_collector(), gateway, context=context)
    assert outcome.allowed is False
    assert "broker access" in outcome.reason
    assert gateway.calls == 0
