"""Transparent throughput and waiting-time metrics for the Forge factory.

These metrics are observational only. They consume immutable RunOutcome records
and explicit scheduler capacity observations; they cannot alter routing, merge,
deployment, or security policy.
"""

from __future__ import annotations

from collections.abc import Sequence
from statistics import median
from typing import Any

from .efficacy import RunOutcome, compute_metrics

__all__ = ["FactoryMetricsError", "build_factory_metrics"]


class FactoryMetricsError(ValueError):
    """Raised when a factory observation window is internally inconsistent."""


def _non_negative_seconds(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise FactoryMetricsError(f"{name} must be a non-negative integer")
    return value


def _wait_seconds(outcome: RunOutcome) -> int:
    cycle = _non_negative_seconds(outcome.cycle_time_seconds, "cycle_time_seconds")
    runtime = _non_negative_seconds(outcome.runtime_seconds, "runtime_seconds")
    return max(cycle - runtime, 0)


def build_factory_metrics(
    outcomes: Sequence[RunOutcome],
    *,
    window_seconds: int,
    parallel_slots: int | None = None,
    busy_slot_seconds: int | None = None,
) -> dict[str, Any]:
    """Build honest speed/quality/utilization metrics for one observation window.

    Throughput uses an explicit window supplied by the collector rather than
    inferring a convenient interval from successful runs. Parallel utilization
    stays unknown unless both capacity and observed busy-slot time are supplied.
    """

    window = _non_negative_seconds(window_seconds, "window_seconds")
    if window == 0:
        raise FactoryMetricsError("window_seconds must be greater than zero")

    sample = tuple(outcomes)
    for outcome in sample:
        _non_negative_seconds(outcome.cycle_time_seconds, "cycle_time_seconds")
        _non_negative_seconds(outcome.runtime_seconds, "runtime_seconds")
        if outcome.repair_rounds < 0:
            raise FactoryMetricsError("repair_rounds must be non-negative")

    efficacy = compute_metrics(sample)
    completed = sum(1 for item in sample if item.result == "completed" and item.merged)
    cycle_values = [item.cycle_time_seconds for item in sample]
    total_cycle = sum(cycle_values)
    total_runtime = sum(item.runtime_seconds for item in sample)
    total_wait = sum(_wait_seconds(item) for item in sample)
    window_hours = window / 3600

    utilization: dict[str, Any]
    if parallel_slots is None and busy_slot_seconds is None:
        utilization = {
            "status": "unknown",
            "parallelSlots": None,
            "busySlotSeconds": None,
            "availableSlotSeconds": None,
            "value": None,
        }
    elif parallel_slots is None or busy_slot_seconds is None:
        raise FactoryMetricsError(
            "parallel_slots and busy_slot_seconds must be supplied together or both omitted"
        )
    else:
        slots = _non_negative_seconds(parallel_slots, "parallel_slots")
        busy = _non_negative_seconds(busy_slot_seconds, "busy_slot_seconds")
        if slots == 0:
            raise FactoryMetricsError("parallel_slots must be greater than zero")
        available = window * slots
        if busy > available:
            raise FactoryMetricsError("busy_slot_seconds cannot exceed available slot capacity")
        utilization = {
            "status": "observed",
            "parallelSlots": slots,
            "busySlotSeconds": busy,
            "availableSlotSeconds": available,
            "value": round(busy / available, 4),
        }

    return {
        "schemaVersion": 1,
        "sampleCount": len(sample),
        "completedMergedCount": completed,
        "observationWindowSeconds": window,
        "completedPerHour": round(completed / window_hours, 4),
        "medianCycleSeconds": round(float(median(cycle_values)), 2) if cycle_values else 0.0,
        "firstPassVerificationRate": efficacy["first_pass_verification_rate"].value,
        "firstPassReviewAcceptanceRate": efficacy["first_pass_review_acceptance_rate"].value,
        "meanRepairRounds": (
            round(sum(item.repair_rounds for item in sample) / len(sample), 4) if sample else 0.0
        ),
        "totalRuntimeSeconds": total_runtime,
        "totalWaitSeconds": total_wait,
        "waitingFractionOfCycle": round(total_wait / total_cycle, 4) if total_cycle else 0.0,
        "parallelUtilization": utilization,
        "limitations": [
            "failed, cancelled, abandoned, and inconclusive outcomes remain in the sample",
            "wait time is max(cycle_time_seconds - runtime_seconds, 0)",
            "parallel utilization is unknown unless scheduler capacity observations are supplied",
        ],
    }
