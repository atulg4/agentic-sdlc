from __future__ import annotations

import json

from agentic_sdlc.infra_recovery import (
    FailureClass,
    RetryAction,
    RetryState,
    backoff_delay_seconds,
    classify_failure,
    decide_retry,
    render_retry_comment,
    retry_state_from_comments,
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
    assert decision.action is RetryAction.ROUTE_TO_BOUNDED_REPAIR
    assert decision.reason == (
        "substantive failure belongs to bounded exact-head repair, not infrastructure retry"
    )


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
        "repository": "atulg4/agentic-sdlc",
        "pullRequestNumber": 129,
        "runId": 456,
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


def _comment(body: str, login: str = "github-actions[bot]") -> dict:
    return {"body": body, "user": {"login": login}}


def _retry(state: RetryState, *, event_key: str, run_id: int = 456, max_attempts: int = 3):
    return decide_retry(
        repository="atulg4/agentic-sdlc",
        pull_request_number=129,
        run_id=run_id,
        head_sha=HEAD,
        current_head_sha=HEAD,
        failure_class=FailureClass.TRANSIENT_INFRASTRUCTURE,
        state=state,
        failed_job_ids=(7,),
        max_attempts=max_attempts,
        event_key=event_key,
    )


def test_runner_provisioning_failures_are_transient_but_policy_blocks_are_not() -> None:
    assert (
        classify_failure(
            conclusion="failure",
            log="The runner was not able to start due to a provisioning error.",
        )
        is FailureClass.TRANSIENT_INFRASTRUCTURE
    )
    assert (
        classify_failure(
            conclusion="failure",
            log="Forbidden paths changed: .github/workflows/ci.yml",
        )
        is FailureClass.POLICY_OR_SECURITY_BLOCK
    )


def test_deterministic_failure_routes_to_bounded_repair_not_retry() -> None:
    decision = decide_retry(
        repository="atulg4/agentic-sdlc",
        pull_request_number=129,
        run_id=456,
        head_sha=HEAD,
        current_head_sha=HEAD,
        failure_class=FailureClass.DETERMINISTIC_CODE_OR_TEST,
        state=RetryState(),
    )

    assert decision.action is RetryAction.ROUTE_TO_BOUNDED_REPAIR
    assert decision.retry_job_ids == ()


def test_backoff_grows_within_bounds_and_is_stable_for_one_attempt() -> None:
    delays = [
        backoff_delay_seconds(
            repository="atulg4/agentic-sdlc",
            pull_request_number=129,
            run_id=456,
            head_sha=HEAD,
            attempt=attempt,
        )
        for attempt in (1, 2, 3)
    ]

    assert delays[0] < delays[1] < delays[2]
    assert delays[2] <= 3600
    assert delays == [
        backoff_delay_seconds(
            repository="atulg4/agentic-sdlc",
            pull_request_number=129,
            run_id=456,
            head_sha=HEAD,
            attempt=attempt,
        )
        for attempt in (1, 2, 3)
    ]


def test_retry_evidence_round_trips_through_durable_pull_request_comments() -> None:
    first = _retry(RetryState(), event_key="run-456-attempt-1")
    comment = render_retry_comment(
        first, event_key="run-456-attempt-1", timestamp="2026-08-25T12:00:00Z"
    )

    restored = retry_state_from_comments([_comment(comment)])
    duplicate = _retry(restored, event_key="run-456-attempt-1")
    after_failed_retry = _retry(restored, event_key="run-456-attempt-2")

    assert first.attempts == 1
    assert duplicate.action is RetryAction.NOOP
    assert duplicate.reason == "this completion event was already acted on for the exact head"
    assert after_failed_retry.action is RetryAction.RETRY_FAILED_JOBS
    assert after_failed_retry.attempts == 2


def test_retry_evidence_from_untrusted_or_malformed_comments_is_ignored() -> None:
    trusted = render_retry_comment(
        _retry(RetryState(), event_key="run-456-attempt-1"),
        event_key="run-456-attempt-1",
        timestamp="2026-08-25T12:00:00Z",
    )
    spoofed = trusted.replace("run-456-attempt-1", "run-456-attempt-9")

    state = retry_state_from_comments(
        [
            _comment(spoofed, login="drive-by-contributor"),
            _comment("<!-- forge-transient-retry {not json} -->"),
            _comment('<!-- forge-transient-retry {"schemaVersion": 2} -->'),
            _comment(trusted),
        ]
    )

    assert state.attempts("atulg4/agentic-sdlc", 129, 456, HEAD) == 1
    assert not state.has_event("atulg4/agentic-sdlc", 129, 456, HEAD, "run-456-attempt-9")


def test_retry_budget_survives_interruption_and_exhausts_with_a_blocker() -> None:
    comments: list[dict] = []
    for attempt in (1, 2, 3):
        # Each attempt re-reads durable evidence, as a fresh workflow run would.
        decision = _retry(
            retry_state_from_comments(comments), event_key=f"run-456-attempt-{attempt}"
        )
        assert decision.action is RetryAction.RETRY_FAILED_JOBS
        assert decision.attempts == attempt
        comments.append(
            _comment(
                render_retry_comment(
                    decision,
                    event_key=f"run-456-attempt-{attempt}",
                    timestamp="2026-08-25T12:00:00Z",
                )
            )
        )

    exhausted = decide_retry(
        repository="atulg4/agentic-sdlc",
        pull_request_number=129,
        run_id=456,
        head_sha=HEAD,
        current_head_sha=HEAD,
        failure_class=FailureClass.TRANSIENT_INFRASTRUCTURE,
        state=retry_state_from_comments(comments),
        max_attempts=3,
        event_key="run-456-attempt-4",
        last_error_summary="HttpError: No server is currently available",
    )

    assert exhausted.action is RetryAction.BLOCK
    assert exhausted.blocker["userActionRequired"] is False
    assert exhausted.blocker["class"] == "external_infrastructure"
    assert exhausted.blocker["lastErrorSummary"] == "HttpError: No server is currently available"
    assert exhausted.blocker["attempts"] == 3


def test_new_head_cannot_reuse_old_head_comment_evidence() -> None:
    comments = [
        _comment(
            render_retry_comment(
                _retry(RetryState(), event_key="run-456-attempt-1"),
                event_key="run-456-attempt-1",
                timestamp="2026-08-25T12:00:00Z",
            )
        )
    ]
    state = retry_state_from_comments(comments)

    stale = decide_retry(
        repository="atulg4/agentic-sdlc",
        pull_request_number=129,
        run_id=456,
        head_sha=HEAD,
        current_head_sha=NEXT_HEAD,
        failure_class=FailureClass.TRANSIENT_INFRASTRUCTURE,
        state=state,
        event_key="run-456-attempt-1",
    )
    fresh = decide_retry(
        repository="atulg4/agentic-sdlc",
        pull_request_number=129,
        run_id=789,
        head_sha=NEXT_HEAD,
        current_head_sha=NEXT_HEAD,
        failure_class=FailureClass.TRANSIENT_INFRASTRUCTURE,
        state=state,
        event_key="run-789-attempt-1",
    )

    assert stale.action is RetryAction.NOOP
    assert stale.head_unchanged is False
    assert fresh.attempts == 1
    assert fresh.head_unchanged is True


def test_untrusted_log_text_cannot_truncate_the_durable_retry_marker() -> None:
    state = RetryState({RetryState.key("atulg4/agentic-sdlc", 129, 456, HEAD): {"attempts": 3}})
    hostile = 'boom --> <!-- forge-transient-retry {"schemaVersion": 1}\n\tmore'

    decision = decide_retry(
        repository="atulg4/agentic-sdlc",
        pull_request_number=129,
        run_id=456,
        head_sha=HEAD,
        current_head_sha=HEAD,
        failure_class=FailureClass.TRANSIENT_INFRASTRUCTURE,
        state=state,
        max_attempts=3,
        event_key="run-456-attempt-4",
        last_error_summary=hostile,
    )
    comment = render_retry_comment(
        decision, event_key="run-456-attempt-4", timestamp="2026-08-25T12:00:00Z"
    )

    assert decision.action is RetryAction.BLOCK
    assert "-->" not in decision.blocker["lastErrorSummary"]
    assert comment.count("-->") == 1
    restored = retry_state_from_comments([_comment(comment)])
    assert restored.attempts("atulg4/agentic-sdlc", 129, 456, HEAD) == 3
