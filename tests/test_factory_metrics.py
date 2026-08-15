from __future__ import annotations

import pytest

from agentic_sdlc.efficacy import RunOutcome
from agentic_sdlc.factory_metrics import FactoryMetricsError, build_factory_metrics


def _outcome(
    ref: str,
    *,
    result: str = "completed",
    merged: bool = True,
    verification: bool | None = True,
    findings: int | None = 0,
    repairs: int = 0,
    runtime: int = 60,
    cycle: int = 120,
) -> RunOutcome:
    return RunOutcome(
        work_ref=ref,
        mission_id="implementation",
        mission_version="1",
        agent_id="claude",
        model="claude-opus-5",
        task_class="code",
        cohort="factory",
        result=result,
        verification_passed=verification,
        review_findings=findings,
        repair_rounds=repairs,
        merged=merged,
        runtime_seconds=runtime,
        cycle_time_seconds=cycle,
        finished_at=f"2026-08-15T12:{ref[-2:]}:00Z",
    )


def test_factory_metrics_expose_throughput_waiting_and_first_pass_quality() -> None:
    outcomes = [
        _outcome("work-01", runtime=60, cycle=120),
        _outcome("work-02", runtime=90, cycle=300, findings=2, repairs=1),
        _outcome(
            "work-03",
            result="failed",
            merged=False,
            verification=False,
            findings=None,
            runtime=30,
            cycle=180,
        ),
    ]

    report = build_factory_metrics(outcomes, window_seconds=3600)

    assert report["sampleCount"] == 3
    assert report["completedMergedCount"] == 2
    assert report["completedPerHour"] == 2.0
    assert report["medianCycleSeconds"] == 180.0
    assert report["firstPassVerificationRate"] == pytest.approx(1 / 3, abs=0.0001)
    assert report["firstPassReviewAcceptanceRate"] == 0.5
    assert report["meanRepairRounds"] == pytest.approx(1 / 3, abs=0.0001)
    assert report["totalRuntimeSeconds"] == 180
    assert report["totalWaitSeconds"] == 420
    assert report["waitingFractionOfCycle"] == 0.7


def test_failures_and_cancelled_work_remain_in_denominators() -> None:
    outcomes = [
        _outcome("work-01"),
        _outcome("work-02", result="failed", merged=False, verification=False, findings=None),
        _outcome("work-03", result="cancelled", merged=False, verification=None, findings=None),
    ]

    report = build_factory_metrics(outcomes, window_seconds=7200)

    assert report["sampleCount"] == 3
    assert report["completedMergedCount"] == 1
    assert report["completedPerHour"] == 0.5
    assert report["firstPassVerificationRate"] == pytest.approx(1 / 3, abs=0.0001)


def test_wait_time_never_becomes_negative_when_runtime_exceeds_cycle() -> None:
    report = build_factory_metrics(
        [_outcome("work-01", runtime=200, cycle=100)],
        window_seconds=3600,
    )

    assert report["totalWaitSeconds"] == 0
    assert report["waitingFractionOfCycle"] == 0.0


def test_parallel_utilization_is_unknown_without_capacity_observation() -> None:
    report = build_factory_metrics([_outcome("work-01")], window_seconds=3600)

    assert report["parallelUtilization"] == {
        "status": "unknown",
        "parallelSlots": None,
        "busySlotSeconds": None,
        "availableSlotSeconds": None,
        "value": None,
    }


def test_parallel_utilization_uses_explicit_slot_capacity() -> None:
    report = build_factory_metrics(
        [_outcome("work-01")],
        window_seconds=3600,
        parallel_slots=4,
        busy_slot_seconds=7200,
    )

    assert report["parallelUtilization"] == {
        "status": "observed",
        "parallelSlots": 4,
        "busySlotSeconds": 7200,
        "availableSlotSeconds": 14400,
        "value": 0.5,
    }


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"window_seconds": 0}, "greater than zero"),
        (
            {"window_seconds": 3600, "parallel_slots": 2},
            "must be supplied together",
        ),
        (
            {
                "window_seconds": 3600,
                "parallel_slots": 1,
                "busy_slot_seconds": 3601,
            },
            "cannot exceed available",
        ),
    ],
)
def test_invalid_factory_observations_fail_closed(
    kwargs: dict[str, int],
    message: str,
) -> None:
    with pytest.raises(FactoryMetricsError, match=message):
        build_factory_metrics([_outcome("work-01")], **kwargs)
