from __future__ import annotations

import json
from io import BytesIO

import pytest

from agentic_sdlc.github_merge import GitHubMergeGateway


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body


def test_gateway_sends_only_exact_sha_and_normal_squash_merge() -> None:
    seen = []

    def opener(request):
        seen.append(request)
        return FakeResponse({"merged": True, "sha": "b" * 40, "message": "merged"})

    gateway = GitHubMergeGateway("secret-token", opener=opener)
    result = gateway.merge(
        repository="atulg4/example",
        pull_request_number=67,
        expected_head_sha="a" * 40,
    )

    assert result.merged is True
    assert result.merge_sha == "b" * 40
    assert len(seen) == 1
    request = seen[0]
    assert request.full_url == "https://api.github.com/repos/atulg4/example/pulls/67/merge"
    assert request.method == "PUT"
    assert json.loads(request.data) == {"sha": "a" * 40, "merge_method": "squash"}
    assert b"secret-token" not in request.data
    assert "bypass" not in request.full_url
    assert "force" not in request.full_url


def test_token_is_required_and_custom_origins_are_rejected() -> None:
    with pytest.raises(ValueError, match="token"):
        GitHubMergeGateway("")
    with pytest.raises(ValueError, match="origin"):
        GitHubMergeGateway("token", api_base="https://example.invalid")


def test_rejected_merge_does_not_claim_success() -> None:
    gateway = GitHubMergeGateway(
        "token",
        opener=lambda request: FakeResponse({"merged": False, "message": "Required checks pending"}),
    )

    result = gateway.merge(
        repository="atulg4/example",
        pull_request_number=67,
        expected_head_sha="a" * 40,
    )

    assert result.merged is False
    assert result.message == "Required checks pending"


def test_source_has_no_bypass_or_api_key_fallback_contracts() -> None:
    from pathlib import Path

    source = (Path(__file__).parents[1] / "src/agentic_sdlc/github_merge.py").read_text()
    for forbidden in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "browser cookie",
        "session scraping",
        "force=true",
        "bypass=true",
    ):
        assert forbidden not in source
