"""Read-only GitHub evidence collection for protected merge decisions."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.request import Request, urlopen

__all__ = ["GitHubMergeEvidenceCollector", "GitHubPullRequestEvidence"]

_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class GitHubPullRequestEvidence:
    """Trusted read-only facts collected directly from GitHub."""

    head_sha: str
    labels: tuple[str, ...]
    checks: tuple[dict[str, object], ...]
    branch_protection_allows: bool = False
    unresolved_conversations: int = 1


class GitHubMergeEvidenceCollector:
    """Collect exact-head PR gate evidence through read-only GitHub APIs."""

    def __init__(
        self,
        token: str,
        *,
        api_base: str = "https://api.github.com",
        opener: Callable[[Request], object] | None = None,
    ) -> None:
        if not token.strip():
            raise ValueError("read token is required")
        if api_base != "https://api.github.com":
            raise ValueError("only the GitHub API origin is allowed")
        self._token = token
        self._api_base = api_base
        self._opener = opener or urlopen

    def collect(self, *, repository: str, pull_request_number: int) -> GitHubPullRequestEvidence:
        if not _REPOSITORY.fullmatch(repository):
            raise ValueError("invalid repository identifier")
        if pull_request_number <= 0:
            raise ValueError("pull request number must be positive")

        pr = self._get_json(f"/repos/{repository}/pulls/{pull_request_number}")
        head = pr.get("head")
        if not isinstance(head, dict):
            raise ValueError("GitHub pull request response is missing head")
        head_sha = head.get("sha")
        if not isinstance(head_sha, str) or not _SHA.fullmatch(head_sha):
            raise ValueError("GitHub pull request response has invalid head SHA")

        raw_labels = pr.get("labels")
        if not isinstance(raw_labels, list):
            raise ValueError("GitHub pull request response is missing labels")
        labels = tuple(
            item["name"]
            for item in raw_labels
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        )

        check_document = self._get_json(
            f"/repos/{repository}/commits/{head_sha}/check-runs?per_page=100"
        )
        raw_checks = check_document.get("check_runs")
        if not isinstance(raw_checks, list):
            raise ValueError("GitHub check-runs response is malformed")
        checks = tuple(
            {
                "id": raw.get("id"),
                "name": raw.get("name"),
                "head_sha": raw.get("head_sha"),
                "conclusion": raw.get("conclusion"),
            }
            for raw in raw_checks
            if isinstance(raw, dict)
        )

        owner, name = repository.split("/", 1)
        readiness = self._graphql(
            """
            query($owner:String!, $name:String!, $number:Int!) {
              repository(owner:$owner, name:$name) {
                pullRequest(number:$number) {
                  headRefOid
                  mergeStateStatus
                  reviewThreads(first:100) {
                    nodes { isResolved }
                    pageInfo { hasNextPage }
                  }
                }
              }
            }
            """,
            {"owner": owner, "name": name, "number": pull_request_number},
        )
        repository_data = readiness.get("repository")
        if not isinstance(repository_data, dict):
            raise ValueError("GitHub GraphQL response is missing repository")
        pull_request = repository_data.get("pullRequest")
        if not isinstance(pull_request, dict):
            raise ValueError("GitHub GraphQL response is missing pull request")
        if pull_request.get("headRefOid") != head_sha:
            raise ValueError("GitHub evidence sources disagree on pull request head")

        threads = pull_request.get("reviewThreads")
        if not isinstance(threads, dict):
            raise ValueError("GitHub GraphQL response is missing review threads")
        page_info = threads.get("pageInfo")
        if not isinstance(page_info, dict) or page_info.get("hasNextPage") is not False:
            raise ValueError("review thread evidence is incomplete")
        nodes = threads.get("nodes")
        if not isinstance(nodes, list):
            raise ValueError("GitHub review thread response is malformed")
        unresolved = sum(
            1 for node in nodes if not isinstance(node, dict) or node.get("isResolved") is not True
        )
        branch_protection_allows = pull_request.get("mergeStateStatus") == "CLEAN"

        return GitHubPullRequestEvidence(
            head_sha,
            labels,
            checks,
            branch_protection_allows,
            unresolved,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _get_json(self, path: str) -> dict[str, object]:
        request = Request(f"{self._api_base}{path}", method="GET", headers=self._headers())
        return self._read_object(self._opener(request))

    def _graphql(self, query: str, variables: dict[str, object]) -> dict[str, object]:
        payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        request = Request(
            f"{self._api_base}/graphql",
            data=payload,
            method="POST",
            headers={**self._headers(), "Content-Type": "application/json"},
        )
        document = self._read_object(self._opener(request))
        if document.get("errors"):
            raise ValueError("GitHub GraphQL returned errors")
        data = document.get("data")
        if not isinstance(data, dict):
            raise ValueError("GitHub GraphQL response is missing data")
        return data

    @staticmethod
    def _read_object(response: object) -> dict[str, object]:
        document = json.loads(response.read().decode("utf-8"))  # type: ignore[attr-defined]
        if not isinstance(document, dict):
            raise ValueError("GitHub response must be an object")
        return document
