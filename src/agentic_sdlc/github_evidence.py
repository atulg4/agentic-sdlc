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


class GitHubMergeEvidenceCollector:
    """Collect exact-head PR labels and check runs through read-only GitHub APIs."""

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
        labels: list[str] = []
        for item in raw_labels:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                labels.append(item["name"])

        check_document = self._get_json(
            f"/repos/{repository}/commits/{head_sha}/check-runs?per_page=100"
        )
        raw_checks = check_document.get("check_runs")
        if not isinstance(raw_checks, list):
            raise ValueError("GitHub check-runs response is malformed")

        checks: list[dict[str, object]] = []
        for raw in raw_checks:
            if not isinstance(raw, dict):
                continue
            checks.append(
                {
                    "id": raw.get("id"),
                    "name": raw.get("name"),
                    "head_sha": raw.get("head_sha"),
                    "conclusion": raw.get("conclusion"),
                }
            )

        return GitHubPullRequestEvidence(head_sha, tuple(labels), tuple(checks))

    def _get_json(self, path: str) -> dict[str, object]:
        request = Request(
            f"{self._api_base}{path}",
            method="GET",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        response = self._opener(request)
        document = json.loads(response.read().decode("utf-8"))
        if not isinstance(document, dict):
            raise ValueError("GitHub response must be an object")
        return document
