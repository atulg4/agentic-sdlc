from __future__ import annotations

import json
from dataclasses import replace

from agentic_sdlc.autonomy import AutonomyGateEvidence
from agentic_sdlc.github_merge import GitHubMergeGateway
from agentic_sdlc.merge_executor import MergeRequest, ProtectedMergeExecutor


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body


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


def test_failed_gate_never_reaches_github_transport() -> None:
    requests = []

    def opener(request):
        requests.append(request)
        return FakeResponse({"merged": True, "sha": "b" * 40, "message": "merged"})

    gateway = GitHubMergeGateway("secret-token", opener=opener)
    request = MergeRequest("atulg4/example", 68, "a" * 40)
    evidence = replace(_ready(), independent_review_approved=False)

    outcome = ProtectedMergeExecutor().execute(request, evidence, gateway)

    assert outcome.allowed is False
    assert outcome.merged is False
    assert requests == []


def test_green_gate_path_sends_only_exact_protected_merge_request() -> None:
    requests = []

    def opener(request):
        requests.append(request)
        return FakeResponse({"merged": True, "sha": "b" * 40, "message": "merged"})

    gateway = GitHubMergeGateway("secret-token", opener=opener)
    request = MergeRequest("atulg4/example", 68, "a" * 40)

    outcome = ProtectedMergeExecutor().execute(request, _ready(), gateway)

    assert outcome.allowed is True
    assert outcome.merged is True
    assert outcome.merge_sha == "b" * 40
    assert len(requests) == 1
    github_request = requests[0]
    assert github_request.full_url == "https://api.github.com/repos/atulg4/example/pulls/68/merge"
    assert github_request.method == "PUT"
    assert json.loads(github_request.data) == {
        "sha": "a" * 40,
        "merge_method": "squash",
    }
    assert b"secret-token" not in github_request.data
    assert github_request.get_header("Authorization") == "Bearer secret-token"
