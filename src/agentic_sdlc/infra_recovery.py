"""Classify and budget exact-head retries for transient platform failures."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

__all__ = [
    "FailureClass",
    "InfraRecoveryError",
    "RetryAction",
    "RetryDecision",
    "RetryState",
    "classify_failure",
    "decide_retry",
    "load_retry_state",
    "write_retry_state",
]


class InfraRecoveryError(ValueError):
    """Raised when recovery evidence or retry state is structurally invalid."""


class FailureClass(StrEnum):
    TRANSIENT_INFRASTRUCTURE = "transient_infrastructure"
    DETERMINISTIC_CODE_OR_TEST = "deterministic_code_or_test"
    REVIEW_CHANGES_REQUESTED = "review_changes_requested"
    POLICY_OR_SECURITY_BLOCK = "policy_or_security_block"
    UNKNOWN = "unknown"


class RetryAction(StrEnum):
    RETRY_FAILED_JOBS = "retry_failed_jobs"
    BLOCK = "block"
    NOOP = "noop"


_TRANSIENT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bHTTP(?:Error)?\s*5\d\d\b",
        r"\b50[0234]\b.*\b(github|api|server|service|gateway)\b",
        r"\bserver is currently unavailable\b",
        r"\bno server is currently available\b",
        r"\bservice unavailable\b",
        r"\bbad gateway\b",
        r"\bgateway timeout\b",
        r"\bapi rate limit.*secondary\b",
        r"\btemporary api availability\b",
        r"\bfailed to check permissions\b.*\btry resubmitting\b",
        r"\bthe hosted runner.*encountered an error\b",
        r"\bwe were unable to provision.*runner\b",
        r"\bthe runner was not able to start\b",
    )
)

_DETERMINISTIC_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bassertionerror\b",
        r"\bpytest\b.*\bfailed\b",
        r"\b\d+\s+failed\b",
        r"\bruff\b.*\bfailed\b",
        r"\blint\b.*\berror\b",
        r"\btypeerror\b",
        r"\bsyntaxerror\b",
        r"\bmodule not found\b",
        r"\btest failures?\b",
    )
)

_POLICY_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bforbidden paths? changed\b",
        r"\bpolicy(?: check)? rejected\b",
        r"\bsecurity review required\b",
        r"\bbranch protection\b",
        r"\bprotected paths? require\b",
        r"\bpermission denied\b",
        r"\bnot authorized\b",
    )
)

_HEAD_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class RetryDecision:
    action: RetryAction
    failure_class: FailureClass
    reason: str
    attempts: int
    max_attempts: int
    repository: str
    pull_request_number: int
    run_id: int
    head_sha: str
    retry_job_ids: tuple[int, ...] = ()
    next_delay_seconds: int = 0
    blocker: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "action": self.action.value,
            "failureClass": self.failure_class.value,
            "reason": self.reason,
            "attempts": self.attempts,
            "maxAttempts": self.max_attempts,
            "repository": self.repository,
            "pullRequestNumber": self.pull_request_number,
            "runId": self.run_id,
            "headSha": self.head_sha,
            "retryJobIds": list(self.retry_job_ids),
            "nextDelaySeconds": self.next_delay_seconds,
        }
        if self.blocker is not None:
            document["blocker"] = self.blocker
        return document


class RetryState:
    """Durable idempotency state for exact-head transient retries."""

    def __init__(self, records: dict[str, dict[str, Any]] | None = None) -> None:
        self._records = records or {}

    @staticmethod
    def key(repository: str, pull_request_number: int, run_id: int, head_sha: str) -> str:
        return f"{repository}#pr-{pull_request_number}:run-{run_id}:head-{head_sha}"

    @classmethod
    def from_dict(cls, document: dict[str, Any]) -> RetryState:
        if document.get("schemaVersion") != 1:
            raise InfraRecoveryError("retry state schemaVersion must be 1")
        records = document.get("records", {})
        if not isinstance(records, dict):
            raise InfraRecoveryError("retry state records must be an object")
        normalized: dict[str, dict[str, Any]] = {}
        for key, value in records.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                raise InfraRecoveryError("retry state records must map strings to objects")
            normalized[key] = dict(value)
        return cls(normalized)

    def as_dict(self) -> dict[str, Any]:
        return {"schemaVersion": 1, "records": self._records}

    def attempts(
        self, repository: str, pull_request_number: int, run_id: int, head_sha: str
    ) -> int:
        record = self._records.get(self.key(repository, pull_request_number, run_id, head_sha), {})
        value = record.get("attempts", 0)
        return int(value) if type(value) is int else 0

    def in_flight(
        self,
        repository: str,
        pull_request_number: int,
        run_id: int,
        head_sha: str,
    ) -> bool:
        record = self._records.get(self.key(repository, pull_request_number, run_id, head_sha), {})
        return record.get("status") == "retrying"

    def record_retry(self, decision: RetryDecision, *, event_key: str, timestamp: str) -> None:
        if decision.action is not RetryAction.RETRY_FAILED_JOBS:
            raise InfraRecoveryError("only retry decisions can be recorded as retry attempts")
        _require_non_empty(event_key, "event_key")
        _require_non_empty(timestamp, "timestamp")
        key = self.key(
            decision.repository,
            decision.pull_request_number,
            decision.run_id,
            decision.head_sha,
        )
        record = self._records.setdefault(key, {"attempts": 0, "events": []})
        events = record.setdefault("events", [])
        if not isinstance(events, list):
            raise InfraRecoveryError("retry state events must be a list")
        if event_key in events:
            return
        record["attempts"] = decision.attempts
        record["status"] = "retrying"
        record["headSha"] = decision.head_sha
        record["runId"] = decision.run_id
        record["pullRequestNumber"] = decision.pull_request_number
        record["repository"] = decision.repository
        record["lastRetryJobIds"] = list(decision.retry_job_ids)
        record["lastRetryAt"] = timestamp
        events.append(event_key)

    def record_exhaustion(self, decision: RetryDecision, *, event_key: str, timestamp: str) -> None:
        if decision.action is not RetryAction.BLOCK:
            raise InfraRecoveryError("only block decisions can record exhaustion")
        _require_non_empty(event_key, "event_key")
        _require_non_empty(timestamp, "timestamp")
        key = self.key(
            decision.repository,
            decision.pull_request_number,
            decision.run_id,
            decision.head_sha,
        )
        record = self._records.setdefault(key, {"attempts": decision.attempts, "events": []})
        events = record.setdefault("events", [])
        if event_key in events:
            return
        record["attempts"] = decision.attempts
        record["status"] = "exhausted"
        record["blocker"] = decision.blocker or {}
        record["lastBlockedAt"] = timestamp
        events.append(event_key)


def _require_non_empty(value: str, name: str) -> None:
    if not value.strip():
        raise InfraRecoveryError(f"{name} is required")


def _contains(patterns: tuple[re.Pattern[str], ...], text: str) -> bool:
    return any(pattern.search(text) for pattern in patterns)


def classify_failure(
    *,
    conclusion: str = "",
    log: str = "",
    review_verdict: str = "",
    policy_decision: str = "",
) -> FailureClass:
    """Classify terminal evidence without treating all Actions failures as transient."""

    verdict = review_verdict.strip().lower().replace("-", "_")
    if verdict in {"changes_requested", "request_changes"}:
        return FailureClass.REVIEW_CHANGES_REQUESTED
    policy = policy_decision.strip().lower()
    text = "\n".join((conclusion, log, policy_decision))
    if policy in {"blocked", "denied", "rejected"} or _contains(_POLICY_PATTERNS, text):
        return FailureClass.POLICY_OR_SECURITY_BLOCK
    if _contains(_DETERMINISTIC_PATTERNS, text):
        return FailureClass.DETERMINISTIC_CODE_OR_TEST
    if _contains(_TRANSIENT_PATTERNS, text):
        return FailureClass.TRANSIENT_INFRASTRUCTURE
    if conclusion.strip().lower() in {"success", "succeeded", "passed"}:
        return FailureClass.UNKNOWN
    return FailureClass.UNKNOWN


def decide_retry(
    *,
    repository: str,
    pull_request_number: int,
    run_id: int,
    head_sha: str,
    current_head_sha: str,
    failure_class: FailureClass,
    state: RetryState,
    failed_job_ids: tuple[int, ...] = (),
    max_attempts: int = 3,
    base_delay_seconds: int = 60,
) -> RetryDecision:
    """Return the next idempotent exact-head retry action."""

    _require_non_empty(repository, "repository")
    if pull_request_number <= 0:
        raise InfraRecoveryError("pull_request_number must be positive")
    if run_id <= 0:
        raise InfraRecoveryError("run_id must be positive")
    if not _HEAD_SHA.fullmatch(head_sha) or not _HEAD_SHA.fullmatch(current_head_sha):
        raise InfraRecoveryError("head SHA values must be 40 lowercase hex characters")
    if not 1 <= max_attempts <= 20:
        raise InfraRecoveryError("max_attempts must be between 1 and 20")
    if base_delay_seconds <= 0:
        raise InfraRecoveryError("base_delay_seconds must be positive")
    if any(job_id <= 0 for job_id in failed_job_ids):
        raise InfraRecoveryError("failed job IDs must be positive")

    attempts = state.attempts(repository, pull_request_number, run_id, head_sha)
    if head_sha != current_head_sha:
        return RetryDecision(
            RetryAction.NOOP,
            failure_class,
            "failed run is stale for the current PR head",
            attempts,
            max_attempts,
            repository,
            pull_request_number,
            run_id,
            head_sha,
        )
    if failure_class is not FailureClass.TRANSIENT_INFRASTRUCTURE:
        return RetryDecision(
            RetryAction.NOOP,
            failure_class,
            "failure class is not retryable as infrastructure",
            attempts,
            max_attempts,
            repository,
            pull_request_number,
            run_id,
            head_sha,
        )
    if state.in_flight(repository, pull_request_number, run_id, head_sha):
        return RetryDecision(
            RetryAction.NOOP,
            failure_class,
            "transient retry is already in flight for this exact head",
            attempts,
            max_attempts,
            repository,
            pull_request_number,
            run_id,
            head_sha,
        )
    if attempts >= max_attempts:
        blocker = {
            "class": "external_infrastructure",
            "userActionRequired": False,
            "headSha": head_sha,
            "attempts": attempts,
            "maxAttempts": max_attempts,
            "lastErrorSummary": "transient infrastructure retry budget exhausted",
            "nextAction": "blocked_exhausted",
        }
        return RetryDecision(
            RetryAction.BLOCK,
            failure_class,
            "transient infrastructure retry budget exhausted",
            attempts,
            max_attempts,
            repository,
            pull_request_number,
            run_id,
            head_sha,
            blocker=blocker,
        )

    next_attempt = attempts + 1
    digest = hashlib.sha256(
        f"{repository}:{pull_request_number}:{run_id}:{head_sha}:{next_attempt}".encode()
    ).hexdigest()
    jitter = int(digest[:4], 16) % base_delay_seconds
    delay = min(base_delay_seconds * (2 ** (next_attempt - 1)) + jitter, 3600)
    return RetryDecision(
        RetryAction.RETRY_FAILED_JOBS,
        failure_class,
        "retry only failed or cancelled jobs for the unchanged exact head",
        next_attempt,
        max_attempts,
        repository,
        pull_request_number,
        run_id,
        head_sha,
        retry_job_ids=tuple(sorted(set(failed_job_ids))),
        next_delay_seconds=delay,
    )


def load_retry_state(path: str | Path) -> RetryState:
    state_path = Path(path)
    if not state_path.exists():
        return RetryState()
    return RetryState.from_dict(json.loads(state_path.read_text(encoding="utf-8")))


def write_retry_state(state: RetryState, path: str | Path) -> None:
    Path(path).write_text(
        json.dumps(state.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
