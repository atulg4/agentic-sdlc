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
    "RETRY_COMMENT_MARKER",
    "FailureClass",
    "InfraRecoveryError",
    "RetryAction",
    "RetryDecision",
    "RetryState",
    "backoff_delay_seconds",
    "build_blocker",
    "classify_failure",
    "decide_retry",
    "load_retry_state",
    "render_retry_comment",
    "retry_state_from_comments",
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
    ROUTE_TO_BOUNDED_REPAIR = "route_to_bounded_repair"
    BLOCK = "block"
    NOOP = "noop"


#: Durable retry evidence lives in trusted bot comments on the pull request so
#: it survives workflow interruption without a parallel state service.
RETRY_COMMENT_MARKER = "forge-transient-retry"

#: Failure classes that a bounded exact-head repair cycle owns. They are never
#: retried as infrastructure: rerunning them would only reproduce the failure.
_REPAIR_ROUTED = frozenset(
    {
        FailureClass.DETERMINISTIC_CODE_OR_TEST,
        FailureClass.REVIEW_CHANGES_REQUESTED,
    }
)


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
    head_unchanged: bool = True
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
            "headUnchanged": self.head_unchanged,
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

    def has_event(
        self,
        repository: str,
        pull_request_number: int,
        run_id: int,
        head_sha: str,
        event_key: str,
    ) -> bool:
        """Report whether this exact completion event was already acted on."""

        record = self._records.get(self.key(repository, pull_request_number, run_id, head_sha), {})
        events = record.get("events", [])
        return isinstance(events, list) and event_key in events

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
    event_key: str = "",
    last_error_summary: str = "",
) -> RetryDecision:
    """Return the next idempotent exact-head retry action.

    ``event_key`` identifies one trusted completion event (for GitHub Actions,
    the run id plus its run attempt). When supplied, an event that was already
    acted on is a no-op, so duplicate deliveries and the hourly watchdog can
    replay the same evidence without spending retry budget. Without it the
    decision falls back to the coarser in-flight check.
    """

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
    head_unchanged = head_sha == current_head_sha

    def _decision(
        action: RetryAction,
        reason: str,
        *,
        recorded_attempts: int | None = None,
        retry_job_ids: tuple[int, ...] = (),
        next_delay_seconds: int = 0,
        blocker: dict[str, Any] | None = None,
    ) -> RetryDecision:
        return RetryDecision(
            action,
            failure_class,
            reason,
            attempts if recorded_attempts is None else recorded_attempts,
            max_attempts,
            repository,
            pull_request_number,
            run_id,
            head_sha,
            retry_job_ids=retry_job_ids,
            next_delay_seconds=next_delay_seconds,
            head_unchanged=head_unchanged,
            blocker=blocker,
        )

    if not head_unchanged:
        # A new head owns its own budget; old-head evidence is never reused.
        return _decision(RetryAction.NOOP, "failed run is stale for the current PR head")
    if failure_class in _REPAIR_ROUTED:
        return _decision(
            RetryAction.ROUTE_TO_BOUNDED_REPAIR,
            "substantive failure belongs to bounded exact-head repair, not infrastructure retry",
        )
    if failure_class is not FailureClass.TRANSIENT_INFRASTRUCTURE:
        return _decision(RetryAction.NOOP, "failure class is not retryable as infrastructure")
    if event_key:
        if state.has_event(repository, pull_request_number, run_id, head_sha, event_key):
            return _decision(
                RetryAction.NOOP,
                "this completion event was already acted on for the exact head",
            )
    elif state.in_flight(repository, pull_request_number, run_id, head_sha):
        return _decision(
            RetryAction.NOOP, "transient retry is already in flight for this exact head"
        )
    if attempts >= max_attempts:
        return _decision(
            RetryAction.BLOCK,
            "transient infrastructure retry budget exhausted",
            blocker=build_blocker(
                repository=repository,
                pull_request_number=pull_request_number,
                run_id=run_id,
                head_sha=head_sha,
                attempts=attempts,
                max_attempts=max_attempts,
                last_error_summary=last_error_summary,
            ),
        )

    next_attempt = attempts + 1
    return _decision(
        RetryAction.RETRY_FAILED_JOBS,
        "retry only failed or cancelled jobs for the unchanged exact head",
        recorded_attempts=next_attempt,
        retry_job_ids=tuple(sorted(set(failed_job_ids))),
        next_delay_seconds=backoff_delay_seconds(
            repository=repository,
            pull_request_number=pull_request_number,
            run_id=run_id,
            head_sha=head_sha,
            attempt=next_attempt,
            base_delay_seconds=base_delay_seconds,
        ),
    )


def _sanitize_summary(last_error_summary: str) -> str:
    """Reduce an untrusted CI log excerpt to inert single-line marker text.

    The summary is quoted back inside an HTML comment marker, so a log that
    contains ``-->`` would otherwise truncate the durable retry record and
    silently reset the budget.
    """

    collapsed = " ".join(last_error_summary.split())
    neutralized = collapsed.replace("-->", "--&gt;").replace("<!--", "&lt;!--")
    return neutralized[:500] or "transient infrastructure retry budget exhausted"


def backoff_delay_seconds(
    *,
    repository: str,
    pull_request_number: int,
    run_id: int,
    head_sha: str,
    attempt: int,
    base_delay_seconds: int = 60,
    max_delay_seconds: int = 3600,
) -> int:
    """Bounded exponential backoff with deterministic per-target jitter.

    Jitter is derived from the retry target rather than a random source so the
    same attempt always yields the same delay. Concurrent PRs hitting the same
    platform incident still spread out, but a replayed decision never changes.
    """

    if attempt <= 0:
        raise InfraRecoveryError("attempt must be positive")
    if base_delay_seconds <= 0:
        raise InfraRecoveryError("base_delay_seconds must be positive")
    if max_delay_seconds < base_delay_seconds:
        raise InfraRecoveryError("max_delay_seconds must not be below base_delay_seconds")
    digest = hashlib.sha256(
        f"{repository}:{pull_request_number}:{run_id}:{head_sha}:{attempt}".encode()
    ).hexdigest()
    jitter = int(digest[:4], 16) % base_delay_seconds
    return min(base_delay_seconds * (2 ** (attempt - 1)) + jitter, max_delay_seconds)


def build_blocker(
    *,
    repository: str,
    pull_request_number: int,
    run_id: int,
    head_sha: str,
    attempts: int,
    max_attempts: int,
    last_error_summary: str = "",
) -> dict[str, Any]:
    """Build the durable external-infrastructure blocker record.

    ``userActionRequired`` is false: budget exhaustion is evidence about the
    platform, not about the change under review. Nothing here bypasses a
    required check or converts a red result to green.
    """

    summary = _sanitize_summary(last_error_summary)
    return {
        "class": "external_infrastructure",
        "userActionRequired": False,
        "repository": repository,
        "pullRequestNumber": pull_request_number,
        "runId": run_id,
        "headSha": head_sha,
        "attempts": attempts,
        "maxAttempts": max_attempts,
        "lastErrorSummary": summary,
        "nextAction": "blocked_exhausted",
    }


def load_retry_state(path: str | Path) -> RetryState:
    state_path = Path(path)
    if not state_path.exists():
        return RetryState()
    return RetryState.from_dict(json.loads(state_path.read_text(encoding="utf-8")))


def write_retry_state(state: RetryState, path: str | Path) -> None:
    Path(path).write_text(
        json.dumps(state.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


_MARKER = re.compile(
    rf"<!--\s*{RETRY_COMMENT_MARKER}\s+(\{{.*?\}})\s*-->",
    re.DOTALL,
)

#: Only comments authored by the repository's own Actions identity are trusted
#: as retry evidence. Anyone can write a comment; nobody but the workflow can
#: write one as this author.
TRUSTED_RETRY_COMMENT_LOGINS: tuple[str, ...] = ("github-actions[bot]",)


def render_retry_comment(decision: RetryDecision, *, event_key: str, timestamp: str) -> str:
    """Render the durable pull-request comment that records one retry decision."""

    if decision.action not in {RetryAction.RETRY_FAILED_JOBS, RetryAction.BLOCK}:
        raise InfraRecoveryError("only retry and block decisions produce durable evidence")
    _require_non_empty(event_key, "event_key")
    _require_non_empty(timestamp, "timestamp")
    if "-->" in event_key:
        raise InfraRecoveryError("event_key must not close an HTML comment")
    retrying = decision.action is RetryAction.RETRY_FAILED_JOBS
    payload = {
        "schemaVersion": 1,
        "repository": decision.repository,
        "pullRequestNumber": decision.pull_request_number,
        "runId": decision.run_id,
        "headSha": decision.head_sha,
        "attempts": decision.attempts,
        "maxAttempts": decision.max_attempts,
        "status": "retrying" if retrying else "exhausted",
        "eventKey": event_key,
        "timestamp": timestamp,
        "retryJobIds": list(decision.retry_job_ids),
    }
    if decision.blocker is not None:
        payload["blocker"] = decision.blocker
    marker = f"<!-- {RETRY_COMMENT_MARKER} {json.dumps(payload, sort_keys=True)} -->"
    if retrying:
        body = (
            f"Forge classified run {decision.run_id} at exact head "
            f"`{decision.head_sha}` as a transient GitHub infrastructure failure and "
            f"is re-running only the failed jobs (auto-retry "
            f"{decision.attempts}/{decision.max_attempts}, next attempt in "
            f"{decision.next_delay_seconds}s). No required check was bypassed, "
            "skipped, or converted to green."
        )
    else:
        blocker = decision.blocker or {}
        body = (
            "Blocked: GitHub infrastructure. Forge stopped automatic retries after "
            f"{decision.attempts}/{decision.max_attempts} attempts on unchanged head "
            f"`{decision.head_sha}`. User action required: no. Last error: "
            f"{blocker.get('lastErrorSummary', 'unknown')}. The pull request stays red "
            "until the platform recovers or a new commit arrives."
        )
    return f"{marker}\n{body}"


def retry_state_from_comments(
    comments: Any,
    *,
    trusted_logins: tuple[str, ...] = TRUSTED_RETRY_COMMENT_LOGINS,
) -> RetryState:
    """Rebuild durable retry state from trusted pull-request comments.

    Comment bodies are untrusted text: markers from any other author are
    ignored, and a malformed payload is skipped rather than allowed to grant
    extra budget.
    """

    if not isinstance(comments, list):
        raise InfraRecoveryError("comments must be a list")
    trusted = {login.lower() for login in trusted_logins}
    records: dict[str, dict[str, Any]] = {}
    for comment in comments:
        if not isinstance(comment, dict):
            raise InfraRecoveryError("each comment must be an object")
        user = comment.get("user")
        login = user.get("login", "") if isinstance(user, dict) else comment.get("login", "")
        if not isinstance(login, str) or login.lower() not in trusted:
            continue
        body = comment.get("body")
        if not isinstance(body, str):
            continue
        for match in _MARKER.finditer(body):
            payload = _parse_marker(match.group(1))
            if payload is None:
                continue
            _merge_marker(records, payload)
    return RetryState(records)


def _parse_marker(raw: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("schemaVersion") != 1:
        return None
    repository = payload.get("repository")
    head_sha = payload.get("headSha")
    event_key = payload.get("eventKey")
    if not isinstance(repository, str) or not repository:
        return None
    if not isinstance(head_sha, str) or not _HEAD_SHA.fullmatch(head_sha):
        return None
    if not isinstance(event_key, str) or not event_key:
        return None
    for field in ("pullRequestNumber", "runId", "attempts"):
        value = payload.get(field)
        if type(value) is not int or value <= 0:
            return None
    if payload.get("status") not in {"retrying", "exhausted"}:
        return None
    return payload


def _merge_marker(records: dict[str, dict[str, Any]], payload: dict[str, Any]) -> None:
    key = RetryState.key(
        payload["repository"],
        payload["pullRequestNumber"],
        payload["runId"],
        payload["headSha"],
    )
    record = records.setdefault(
        key,
        {
            "attempts": 0,
            "events": [],
            "repository": payload["repository"],
            "pullRequestNumber": payload["pullRequestNumber"],
            "runId": payload["runId"],
            "headSha": payload["headSha"],
        },
    )
    events = record["events"]
    if payload["eventKey"] not in events:
        events.append(payload["eventKey"])
    record["attempts"] = max(int(record["attempts"]), payload["attempts"])
    if payload["status"] == "exhausted":
        record["status"] = "exhausted"
        blocker = payload.get("blocker")
        if isinstance(blocker, dict):
            record["blocker"] = blocker
    elif record.get("status") != "exhausted":
        record["status"] = "retrying"
