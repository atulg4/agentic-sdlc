from __future__ import annotations

import pytest

from agentic_sdlc.dashboard_efficiency import (
    DashboardEfficiencyError,
    build_dashboard_efficiency,
    build_infrastructure_blocker_panel,
)
from agentic_sdlc.infra_recovery import (
    FailureClass,
    RetryState,
    decide_retry,
)


def test_unknown_efficiency_never_fabricates_metrics_or_freshness() -> None:
    contract = build_dashboard_efficiency(
        None,
        observed_at=None,
        age_seconds=None,
    )

    assert contract == {
        "schemaVersion": 1,
        "status": "unknown",
        "observedAt": None,
        "ageSeconds": None,
        "staleAfterSeconds": 900,
        "metrics": None,
    }


def test_observed_efficiency_preserves_factory_metrics() -> None:
    metrics = {
        "completedPerHour": 2.0,
        "completedPerDay": 48.0,
        "parallelUtilization": {"status": "observed", "value": 0.5},
    }

    contract = build_dashboard_efficiency(
        metrics,
        observed_at="2026-08-16T09:00:00Z",
        age_seconds=120,
    )

    assert contract["status"] == "observed"
    assert contract["observedAt"] == "2026-08-16T09:00:00Z"
    assert contract["ageSeconds"] == 120
    assert contract["metrics"] == metrics


def test_stale_efficiency_remains_visible_but_explicitly_stale() -> None:
    contract = build_dashboard_efficiency(
        {"completedPerHour": 1.25},
        observed_at="2026-08-16T08:00:00Z",
        age_seconds=901,
        stale_after_seconds=900,
    )

    assert contract["status"] == "stale"
    assert contract["metrics"]["completedPerHour"] == 1.25


@pytest.mark.parametrize(
    ("metrics", "observed_at", "age_seconds", "message"),
    [
        (None, "2026-08-16T09:00:00Z", None, "unknown metrics"),
        ({"completedPerHour": 1.0}, None, 1, "require observed_at"),
        ({"completedPerHour": 1.0}, "2026-08-16T09:00:00Z", None, "require observed_at"),
        ({"completedPerHour": 1.0}, "2026-08-16T09:00:00Z", -1, "non-negative"),
    ],
)
def test_inconsistent_freshness_evidence_fails_closed(
    metrics: dict[str, float] | None,
    observed_at: str | None,
    age_seconds: int | None,
    message: str,
) -> None:
    with pytest.raises(DashboardEfficiencyError, match=message):
        build_dashboard_efficiency(
            metrics,
            observed_at=observed_at,
            age_seconds=age_seconds,
        )


HEAD = "c" * 40


def _decision(state: RetryState, *, max_attempts: int = 3, last_error_summary: str = ""):
    return decide_retry(
        repository="atulg4/agentic-sdlc",
        pull_request_number=129,
        run_id=456,
        head_sha=HEAD,
        current_head_sha=HEAD,
        failure_class=FailureClass.TRANSIENT_INFRASTRUCTURE,
        state=state,
        max_attempts=max_attempts,
        last_error_summary=last_error_summary,
        event_key="run-456-attempt-1",
    )


def test_control_center_renders_an_in_flight_transient_retry_with_next_attempt() -> None:
    decision = _decision(RetryState()).as_dict()

    panel = build_infrastructure_blocker_panel(decision, observed_at="2026-08-25T12:00:00Z")

    assert panel["status"] == "retrying"
    assert panel["headline"] == "Retrying: GitHub infrastructure"
    assert panel["autoRetry"] == "1/3"
    assert panel["userActionRequired"] is False
    assert panel["headUnchanged"] is True
    assert panel["headSha"] == HEAD
    assert panel["nextRetryAt"] > "2026-08-25T12:00:00Z"


def test_control_center_renders_exhaustion_as_a_no_user_action_infrastructure_blocker() -> None:
    state = RetryState({RetryState.key("atulg4/agentic-sdlc", 129, 456, HEAD): {"attempts": 3}})

    panel = build_infrastructure_blocker_panel(
        _decision(state, last_error_summary="HttpError: no server is currently available").as_dict()
    )

    assert panel["status"] == "blocked"
    assert panel["headline"] == "Blocked: GitHub infrastructure"
    assert panel["blockerClass"] == "external_infrastructure"
    assert panel["userActionRequired"] is False
    assert panel["autoRetry"] == "3/3"
    assert panel["nextAction"] == "blocked_exhausted"
    assert panel["nextRetryAt"] is None
    assert panel["lastErrorSummary"] == "HttpError: no server is currently available"


def test_panel_reports_a_superseded_head_as_clear_without_inventing_a_retry() -> None:
    decision = decide_retry(
        repository="atulg4/agentic-sdlc",
        pull_request_number=129,
        run_id=456,
        head_sha=HEAD,
        current_head_sha="d" * 40,
        failure_class=FailureClass.TRANSIENT_INFRASTRUCTURE,
        state=RetryState(),
    )

    panel = build_infrastructure_blocker_panel(decision.as_dict())

    assert panel["status"] == "clear"
    assert panel["headUnchanged"] is False
    assert panel["blockerClass"] is None
    assert panel["nextRetryAt"] is None


def test_panel_refuses_evidence_it_cannot_trust() -> None:
    with pytest.raises(DashboardEfficiencyError):
        build_infrastructure_blocker_panel({"action": "merge", "headSha": HEAD})
    with pytest.raises(DashboardEfficiencyError):
        build_infrastructure_blocker_panel(
            {"action": "block", "headSha": HEAD, "attempts": 3, "maxAttempts": 3}
        )
