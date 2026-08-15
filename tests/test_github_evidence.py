from __future__ import annotations

import json

import pytest

from agentic_sdlc.github_evidence import GitHubMergeEvidenceCollector

HEAD = "a" * 40


class _Response:
    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body


def test_collector_binds_checks_to_the_prs_current_exact_head() -> None:
    requests = []
    responses = iter(
        [
            _Response(
                {
                    "head": {"sha": HEAD},
                    "labels": [{"name": "forge-managed"}, {"name": "low-risk"}],
                }
            ),
            _Response(
                {
                    "check_runs": [
                        {
                            "id": 21,
                            "name": "test",
                            "head_sha": HEAD,
                            "conclusion": "success",
                            "untrusted_extra": "discard-me",
                        }
                    ]
                }
            ),
        ]
    )

    def opener(request):
        requests.append(request)
        return next(responses)

    evidence = GitHubMergeEvidenceCollector("secret-token", opener=opener).collect(
        repository="atulg4/marketmaestro", pull_request_number=121
    )

    assert evidence.head_sha == HEAD
    assert evidence.labels == ("forge-managed", "low-risk")
    assert evidence.checks == (
        {"id": 21, "name": "test", "head_sha": HEAD, "conclusion": "success"},
    )
    assert requests[0].full_url.endswith("/repos/atulg4/marketmaestro/pulls/121")
    assert requests[1].full_url.endswith(f"/commits/{HEAD}/check-runs?per_page=100")
    assert all(request.method == "GET" for request in requests)


def test_token_is_header_only_and_never_serialized_into_urls() -> None:
    requests = []
    responses = iter(
        [
            _Response({"head": {"sha": HEAD}, "labels": []}),
            _Response({"check_runs": []}),
        ]
    )

    def opener(request):
        requests.append(request)
        return next(responses)

    GitHubMergeEvidenceCollector("top-secret", opener=opener).collect(
        repository="owner/repo", pull_request_number=7
    )

    assert all("top-secret" not in request.full_url for request in requests)
    assert all(request.get_header("Authorization") == "Bearer top-secret" for request in requests)
    assert all(request.data is None for request in requests)


@pytest.mark.parametrize(
    ("repository", "number"),
    [("bad repo", 1), ("owner/repo/extra", 1), ("owner/repo", 0), ("owner/repo", -1)],
)
def test_invalid_targets_fail_before_any_network_call(repository: str, number: int) -> None:
    called = False

    def opener(_request):
        nonlocal called
        called = True
        raise AssertionError("network must not be called")

    collector = GitHubMergeEvidenceCollector("token", opener=opener)
    with pytest.raises(ValueError):
        collector.collect(repository=repository, pull_request_number=number)
    assert called is False


def test_malformed_pr_head_fails_closed_before_check_collection() -> None:
    calls = 0

    def opener(_request):
        nonlocal calls
        calls += 1
        return _Response({"head": {"sha": "main"}, "labels": []})

    with pytest.raises(ValueError, match="invalid head SHA"):
        GitHubMergeEvidenceCollector("token", opener=opener).collect(
            repository="owner/repo", pull_request_number=3
        )
    assert calls == 1


def test_malformed_labels_or_check_runs_fail_closed() -> None:
    responses = iter(
        [
            _Response({"head": {"sha": HEAD}, "labels": "not-a-list"}),
        ]
    )
    with pytest.raises(ValueError, match="missing labels"):
        GitHubMergeEvidenceCollector("token", opener=lambda _request: next(responses)).collect(
            repository="owner/repo", pull_request_number=3
        )

    responses = iter(
        [
            _Response({"head": {"sha": HEAD}, "labels": []}),
            _Response({"check_runs": "not-a-list"}),
        ]
    )
    with pytest.raises(ValueError, match="check-runs response is malformed"):
        GitHubMergeEvidenceCollector("token", opener=lambda _request: next(responses)).collect(
            repository="owner/repo", pull_request_number=3
        )


def test_constructor_rejects_missing_token_and_non_github_origin() -> None:
    with pytest.raises(ValueError, match="read token is required"):
        GitHubMergeEvidenceCollector(" ")
    with pytest.raises(ValueError, match="only the GitHub API origin"):
        GitHubMergeEvidenceCollector("token", api_base="https://example.com")
