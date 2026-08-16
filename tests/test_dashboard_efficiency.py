from __future__ import annotations

import pytest

from agentic_sdlc.dashboard_efficiency import (
    DashboardEfficiencyError,
    build_dashboard_efficiency,
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
