from __future__ import annotations

import json

from agentic_sdlc.infra_recovery import (
    FailureClass,
    RetryAction,
    RetryState,
    classify_failure,
    decide_retry,
)

HEAD = "a" * 40
NEXT_HEAD = "b" * 40


def test_github_service_unavailable_permission_failure_is_transient() -> None:
    log = (
        "Failed to check permissions: HttpError: No server is currently available "
        "to service your request. Sorry about that. Please try resubmitting your request."
    )

    assert classify_failure(conclusion="failure", log=log) is FailureClass.TRANSIENT_INFRASTRUCTURE


def test_deterministic_pytest_and_ruff_failures_are_not_transient() -> None:
    pytest_log = "tests/test_policy.py::test_policy FAILED\nAssertionError: expected approval"
    ruff_log = "ruff failed\nsrc/agentic_sdlc/policy.py:1:1: F401 unused import"

    assert classify_failure(conclusion="failure", log=pytest_log) is (
        FailureClass.DETERMINISTIC_CODE_OR_TEST
    )
    assert classify_failure(conclusion="failure", log=ruff_log) is (
        FailureClass.DETERMINISTIC_CODE_OR_TEST
    )


def test_review_changes_requested_routes_to_repair_not_infrastructure_retry() -> None:
    failure_class = classify_failure(
        conclusion="failure",
        review_verdict="changes_requested",
        log="The retry loop never backs off.",
    )

    decision = decide_retry(
        repository="atulg4/agentic-sdlc",
        pull_request_number=129,
        run_id=456,
        head_sha=HEAD,
        current_head_sha=HEAD,
        failure_class=failure_class,
        state=RetryState(),
    )

    assert failure_class is FailureClass.REVIEW_CHANGES_REQUESTED
    assert decision.action is RetryAction.NOOP
    assert decision.reason == "failure class is not retryable as infrastructure"


def test_retry_targets_failed_jobs_on_same_run_and_exact_head() -> None:
    decision = decide_retry(
        repository="atulg4/agentic-sdlc",
        pull_request_number=129,
        run_id=456,
        head_sha=HEAD,
        current_head_sha=HEAD,
        failure_class=FailureClass.TRANSIENT_INFRASTRUCTURE,
        state=RetryState(),
        failed_job_ids=(42, 7, 42),
    )

    assert decision.action is RetryAction.RETRY_FAILED_JOBS
    assert decision.run_id == 456
    assert decision.head_sha == HEAD
    assert decision.retry_job_ids == (7, 42)
    assert decision.next_delay_seconds >= 60


def test_duplicate_completion_or_watchdog_events_do_not_create_duplicate_retries() -> None:
    state = RetryState()
    first = decide_retry(
        repository="atulg4/agentic-sdlc",
        pull_request_number=129,
        run_id=456,
        head_sha=HEAD,
        current_head_sha=HEAD,
        failure_class=FailureClass.TRANSIENT_INFRASTRUCTURE,
        state=state,
        failed_job_ids=(7,),
    )
    state.record_retry(first, event_key="workflow-run-456", timestamp="2026-08-25T12:00:00Z")
    state.record_retry(first, event_key="workflow-run-456", timestamp="2026-08-25T12:00:00Z")

    duplicate = decide_retry(
        repository="atulg4/agentic-sdlc",
        pull_request_number=129,
        run_id=456,
        head_sha=HEAD,
        current_head_sha=HEAD,
        failure_class=FailureClass.TRANSIENT_INFRASTRUCTURE,
        state=state,
        failed_job_ids=(7,),
    )

    assert (
        state.as_dict()["records"][f"atulg4/agentic-sdlc#pr-129:run-456:head-{HEAD}"]["attempts"]
        == 1
    )
    assert duplicate.action is RetryAction.NOOP
    assert duplicate.reason == "transient retry is already in flight for this exact head"


def test_retry_budget_exhaustion_emits_external_infrastructure_blocker() -> None:
    state = RetryState()
    for attempt in range(1, 4):
        decision = decide_retry(
            repository="atulg4/agentic-sdlc",
            pull_request_number=129,
            run_id=456,
            head_sha=HEAD,
            current_head_sha=HEAD,
            failure_class=FailureClass.TRANSIENT_INFRASTRUCTURE,
            state=state,
            max_attempts=3,
        )
        assert decision.attempts == attempt
        state.record_retry(
            decision,
            event_key=f"workflow-run-456-attempt-{attempt}",
            timestamp="2026-08-25T12:00:00Z",
        )
        state.as_dict()["records"][f"atulg4/agentic-sdlc#pr-129:run-456:head-{HEAD}"]["status"] = (
            "failed"
        )

    exhausted = decide_retry(
        repository="atulg4/agentic-sdlc",
        pull_request_number=129,
        run_id=456,
        head_sha=HEAD,
        current_head_sha=HEAD,
        failure_class=FailureClass.TRANSIENT_INFRASTRUCTURE,
        state=state,
        max_attempts=3,
    )

    assert exhausted.action is RetryAction.BLOCK
    assert exhausted.blocker == {
        "class": "external_infrastructure",
        "userActionRequired": False,
        "headSha": HEAD,
        "attempts": 3,
        "maxAttempts": 3,
        "lastErrorSummary": "transient infrastructure retry budget exhausted",
        "nextAction": "blocked_exhausted",
    }


def test_new_head_sha_cannot_reuse_old_retry_evidence() -> None:
    state = RetryState()
    old = decide_retry(
        repository="atulg4/agentic-sdlc",
        pull_request_number=129,
        run_id=456,
        head_sha=HEAD,
        current_head_sha=HEAD,
        failure_class=FailureClass.TRANSIENT_INFRASTRUCTURE,
        state=state,
    )
    state.record_retry(old, event_key="old-head", timestamp="2026-08-25T12:00:00Z")

    stale = decide_retry(
        repository="atulg4/agentic-sdlc",
        pull_request_number=129,
        run_id=456,
        head_sha=HEAD,
        current_head_sha=NEXT_HEAD,
        failure_class=FailureClass.TRANSIENT_INFRASTRUCTURE,
        state=state,
    )
    fresh = decide_retry(
        repository="atulg4/agentic-sdlc",
        pull_request_number=129,
        run_id=789,
        head_sha=NEXT_HEAD,
        current_head_sha=NEXT_HEAD,
        failure_class=FailureClass.TRANSIENT_INFRASTRUCTURE,
        state=state,
    )

    assert stale.action is RetryAction.NOOP
    assert stale.reason == "failed run is stale for the current PR head"
    assert fresh.attempts == 1


def test_retry_state_round_trips_schema_document() -> None:
    state = RetryState.from_dict({"schemaVersion": 1, "records": {}})
    assert json.loads(json.dumps(state.as_dict())) == {"schemaVersion": 1, "records": {}}
