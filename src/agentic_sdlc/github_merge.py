"""Minimal GitHub transport for the protected merge executor."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .merge_executor import MergeGatewayResult

__all__ = ["GitHubMergeGateway"]


@dataclass(frozen=True)
class _Response:
    status: int
    body: bytes


class GitHubMergeGateway:
    """Call GitHub's normal PR merge endpoint with an exact expected head SHA.

    The token is injected only into the request header. No force, bypass, ruleset,
    deployment, workflow, or broker operation is exposed by this adapter.
    """

    def __init__(
        self,
        token: str,
        *,
        api_base: str = "https://api.github.com",
        opener: Callable[[Request], object] | None = None,
    ) -> None:
        if not token.strip():
            raise ValueError("merge token is required")
        if api_base != "https://api.github.com":
            raise ValueError("only the GitHub API origin is allowed")
        self._token = token
        self._api_base = api_base
        self._opener = opener or urlopen

    def merge(
        self,
        *,
        repository: str,
        pull_request_number: int,
        expected_head_sha: str,
    ) -> MergeGatewayResult:
        url = f"{self._api_base}/repos/{repository}/pulls/{pull_request_number}/merge"
        payload = json.dumps({"sha": expected_head_sha, "merge_method": "squash"}).encode()
        request = Request(
            url,
            data=payload,
            method="PUT",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
        )
        try:
            response = self._opener(request)
            body = response.read()
        except HTTPError as exc:
            body = exc.read()
            message = self._message(body) or f"GitHub merge rejected with HTTP {exc.code}"
            return MergeGatewayResult(False, message=message)

        data = json.loads(body.decode("utf-8"))
        if data.get("merged") is not True:
            return MergeGatewayResult(False, message=str(data.get("message") or "merge rejected"))
        return MergeGatewayResult(True, str(data.get("sha") or ""), str(data.get("message") or ""))

    @staticmethod
    def _message(body: bytes) -> str:
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return ""
        return str(data.get("message") or "")
