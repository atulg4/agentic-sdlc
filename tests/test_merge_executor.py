from __future__ import annotations

from dataclasses import replace

import pytest

from agentic_sdlc.autonomy import AutonomyGateEvidence
from agentic_sdlc.merge_executor import (
    MergeGatewayResult,
    MergeRequest,
    ProtectedMergeExecutor,
)


class RecordingGateway:
    def __init__(self, result: MergeGatewayResult | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.result = result or MergeGatewayResult(
            True,
            "b" * 40,
            "merged through protected repository API",
        )

    def merge(
        self,
        *,
        repository: str,
        pull_request_number: int,
        expected_head_sha: str,
    ) -> MergeGatewayResult:
        self.calls.append(
            {
                "repository": repository,
                "pull_request_number": pull_request_number,
                "expected_head_sha": expected_head_sha,
            }
        )
        return self.result


def _ready() -> AutonomyGateEvidence:
    return AutonomyGateEvidence(
        forge_managed=True,
        scope_bounded=True,
        security_clear=True,
        claude_oauth_available=True,
        high_risk=False,
        deterministic_verification_passed=True,
        quality_passed=True,
        independent_review_approved=True,
        secret_scan_passed=True,
        branch_protection_allows=True,
        unresolved_conversations=0,
    )


def _request() -> MergeRequest:
    return MergeRequest("atulg4/example", 66, "a" * 40)


def test_merge_forwards_exact_head_only_after_all_policy_gates_pass() -> None:
    gateway = RecordingGateway()

    outcome = ProtectedMergeExecutor().execute(_request(), _ready(), gateway)

    assert outcome.allowed is True
    assert outcome.merged is True
    assert outcome.merge_sha == "b" * 40
    assert gateway.calls == [
        {
            "repository": "atulg4/example",
            "pull_request_number": 66,
            "expected_head_sha": "a" * 40,
        }
    ]


@pytest.mark.parametrize(
    "evidence",
    [
        replace(_ready(), forge_managed=False),
        replace(_ready(), scope_bounded=False),
        replace(_ready(), security_clear=False),
        replace(_ready(), high_risk=True),
        replace(_ready(), deterministic_verification_passed=False),
        replace(_ready(), quality_passed=False),
        replace(_ready(), independent_review_approved=False),
        replace(_ready(), secret_scan_passed=False),
        replace(_ready(), branch_protection_allows=False),
        replace(_ready(), unresolved_conversations=1),
        replace(_ready(), deployment_requested=True),
        replace(_ready(), broker_access_requested=True),
    ],
)
def test_merge_gateway_is_never_called_when_any_gate_denies(
    evidence: AutonomyGateEvidence,
) -> None:
    gateway = RecordingGateway()

    outcome = ProtectedMergeExecutor().execute(_request(), evidence, gateway)

    assert outcome.allowed is False
    assert outcome.merged is False
    assert outcome.reason
    assert gateway.calls == []


@pytest.mark.parametrize(
    "request",
    [
        MergeRequest("invalid", 66, "a" * 40),
        MergeRequest("atulg4/example", 0, "a" * 40),
        MergeRequest("atulg4/example", -1, "a" * 40),
        MergeRequest("atulg4/example", 66, "A" * 40),
        MergeRequest("atulg4/example", 66, "a" * 39),
        MergeRequest("atulg4/example", 66, "../" + "a" * 40),
    ],
)
def test_malformed_merge_requests_fail_before_gateway(
    request: MergeRequest,
) -> None:
    gateway = RecordingGateway()

    outcome = ProtectedMergeExecutor().execute(request, _ready(), gateway)

    assert outcome.allowed is False
    assert outcome.merged is False
    assert gateway.calls == []


def test_repository_rejection_is_not_reported_as_merge_success() -> None:
    gateway = RecordingGateway(MergeGatewayResult(False, message="protected branch rejected merge"))

    outcome = ProtectedMergeExecutor().execute(_request(), _ready(), gateway)

    assert outcome.allowed is True
    assert outcome.merged is False
    assert outcome.reason == "protected branch rejected merge"


def test_invalid_merge_sha_from_gateway_fails_closed() -> None:
    gateway = RecordingGateway(MergeGatewayResult(True, "not-a-sha", "unexpected response"))

    outcome = ProtectedMergeExecutor().execute(_request(), _ready(), gateway)

    assert outcome.allowed is True
    assert outcome.merged is False
    assert outcome.reason == "merge gateway returned an invalid merge SHA"


def test_gateway_contract_has_no_force_or_bypass_arguments() -> None:
    gateway = RecordingGateway()
    ProtectedMergeExecutor().execute(_request(), _ready(), gateway)

    assert gateway.calls
    assert set(gateway.calls[0]) == {
        "repository",
        "pull_request_number",
        "expected_head_sha",
    }
