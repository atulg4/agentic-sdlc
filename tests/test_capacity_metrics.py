from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from agentic_sdlc.capacity_metrics import (
    BottleneckKind,
    CapacityError,
    CapacityInputs,
    ObservationWindow,
    PlanCapacityObservation,
    RunnerPool,
    WaitCause,
    build_capacity_report,
    load_capacity_inputs,
)
from agentic_sdlc.event_ledger import EventActor, LifecycleStage, WorkUnitRef
from agentic_sdlc.usage_ledger import (
    MonetaryEquivalent,
    MonetaryStatus,
    TokenCounts,
    UsageActual,
    UsageRecord,
    UsageResult,
    load_pricing_snapshot,
    monetary_equivalent,
)

WINDOW = ObservationWindow("2026-09-22T00:00:00Z", "2026-09-22T01:00:00Z")

CODEX_PLAN = load_pricing_snapshot(
    {
        "pricingId": "codex-plan",
        "provider": "codex",
        "model": "gpt-5-codex",
        "billingMode": "subscription",
        "version": "2026-09",
        "plan": {
            "name": "ChatGPT Pro",
            "capacityUnit": "requests",
            "capacityUnitsPerMillionTokens": 10,
        },
    }
)

CLAUDE_MAX = load_pricing_snapshot(
    {
        "pricingId": "claude-max",
        "provider": "anthropic",
        "model": "claude-opus-5",
        "billingMode": "subscription",
        "version": "2026-09",
        "plan": {"name": "Claude Max", "capacityUnit": "five-hour-window-share"},
    }
)


def at(minute: int) -> str:
    """Minutes from the top of the window, carrying into the next hour."""
    hour, remainder = divmod(minute, 60)
    return f"2026-09-22T{hour:02d}:{remainder:02d}:00Z"


def run(
    usage_id: str,
    worker: str,
    started: int | None,
    finished: int | None,
    *,
    queued: int | None = None,
    attempt: int = 1,
    result: UsageResult = UsageResult.COMPLETED,
    project_id: str = "alpha",
    work_unit_id: str = "unit-1",
    model: str = "claude-opus-5",
    provider: str = "anthropic",
    stage: LifecycleStage = LifecycleStage.IMPLEMENTATION,
    repair_cycle: int | None = None,
    review_round: int | None = None,
    monetary: MonetaryEquivalent | None = None,
) -> UsageRecord:
    actual = UsageActual(
        queued_at=at(queued) if queued is not None else "",
        started_at=at(started) if started is not None else "",
        finished_at=at(finished) if finished is not None else "",
        result=result,
        monetary=monetary or MonetaryEquivalent(status=MonetaryStatus.UNKNOWN),
    )
    return UsageRecord(
        usage_id=usage_id,
        work_unit=WorkUnitRef(project_id=project_id, work_unit_id=work_unit_id),
        stage=stage,
        run_id=usage_id,
        attempt=attempt,
        repair_cycle=repair_cycle,
        review_round=review_round,
        recorded_at=at(0),
        actor=EventActor(
            name="claude", kind="agent", provider=provider, model=model, worker=worker
        ),
        actual=actual,
    )


def find(report: dict[str, Any], kind: BottleneckKind) -> dict[str, Any] | None:
    for item in report["bottlenecks"]:
        if item["kind"] == kind.value:
            return item
    return None


# -- hand-computed concurrency timelines ---------------------------------------


def test_concurrency_timeline_matches_a_hand_computed_schedule() -> None:
    # A 00:00-00:30, B 00:10-00:40, C 00:30-00:50 over a one-hour window.
    report = build_capacity_report(
        [run("A", "w1", 0, 30), run("B", "w2", 10, 40), run("C", "w1", 30, 50)],
        window=WINDOW,
    )
    concurrency = report["concurrency"]

    assert concurrency["peak"] == 2
    assert concurrency["busySeconds"] == 1800 + 1800 + 1200
    assert concurrency["occupiedSeconds"] == 3000
    assert concurrency["idleSeconds"] == 600
    assert concurrency["mean"] == round(4800 / 3600, 4)
    assert [(item["active"], item["seconds"]) for item in concurrency["segments"]] == [
        (1, 600),
        (2, 1200),
        (2, 600),
        (1, 600),
    ]
    # The segments plus the idle tail account for the whole window.
    assert sum(item["seconds"] for item in concurrency["segments"]) + 600 == WINDOW.seconds


def test_a_handover_at_one_instant_is_not_counted_as_two_concurrent_runs() -> None:
    report = build_capacity_report([run("A", "w1", 0, 30), run("B", "w1", 30, 60)], window=WINDOW)
    assert report["concurrency"]["peak"] == 1
    assert report["concurrency"]["busySeconds"] == 3600
    assert report["concurrency"]["idleSeconds"] == 0


def test_runs_outside_the_window_are_excluded_and_overlapping_runs_are_clipped() -> None:
    before = run("before", "w1", None, None)
    report = build_capacity_report(
        [
            run("inside", "w1", 10, 20),
            run("clipped", "w2", 50, 90),
            run("outside", "w3", 70, 90),
            before,
        ],
        window=WINDOW,
    )
    coverage = report["coverage"]

    assert coverage["records"] == 4
    assert coverage["runsTimed"] == 2
    assert coverage["runsClipped"] == 1
    assert coverage["runsOutsideWindow"] == 1
    assert coverage["runsUntimed"] == 1
    # The clipped run contributes only its ten minutes inside the window.
    assert report["concurrency"]["busySeconds"] == 600 + 600


def test_an_empty_window_reports_idle_capacity_rather_than_nothing() -> None:
    report = build_capacity_report([], window=WINDOW)
    assert report["concurrency"] == {
        "peak": 0,
        "mean": 0.0,
        "busySeconds": 0,
        "occupiedSeconds": 0,
        "idleSeconds": 3600,
        "windowSeconds": 3600,
        "runs": 0,
        "segments": [],
    }
    assert report["usefulWork"]["usefulWorkRatio"] is None, "no work is not zero useful work"


def test_the_window_must_be_a_real_interval() -> None:
    with pytest.raises(CapacityError, match="must end after it starts"):
        ObservationWindow("2026-09-22T01:00:00Z", "2026-09-22T00:00:00Z")
    with pytest.raises(CapacityError, match="RFC 3339"):
        ObservationWindow("yesterday", "2026-09-22T01:00:00Z")


# -- queue, wait and runtime decomposition -------------------------------------


def test_queue_and_wait_decompose_against_runtime() -> None:
    records = [
        run("A", "w1", 0, 30, queued=0),
        run("B", "w1", 30, 50, queued=10),
        run("C", "w2", 20, 40, queued=5),
    ]
    report = build_capacity_report(records, window=WINDOW)

    # B waited 00:10-00:30 and C waited 00:05-00:20, overlapping 00:10-00:20.
    assert report["queue"]["peak"] == 2
    assert report["queue"]["busySeconds"] == 1200 + 900
    assert report["wait"]["totalSeconds"] == 2100
    assert report["wait"]["byStageSeconds"] == {"implementation": 2100}
    assert report["wait"]["waitsTimed"] == 3
    assert report["concurrency"]["busySeconds"] == 1800 + 1200 + 1200


def test_handoff_gaps_between_stages_are_measured_per_work_unit() -> None:
    records = [
        run("plan", "w1", 0, 10, queued=0, stage=LifecycleStage.PLANNING),
        # queued fifteen minutes after planning finished
        run("build", "w1", 30, 40, queued=25, stage=LifecycleStage.IMPLEMENTATION),
        run("other", "w2", 0, 10, queued=0, work_unit_id="unit-2"),
    ]
    report = build_capacity_report(records, window=WINDOW)
    handoff = report["wait"]["handoff"]

    assert handoff["byWorkUnitSeconds"] == {"alpha:unit-1": 900}
    assert handoff["totalSeconds"] == 900
    assert find(report, BottleneckKind.HANDOFF_DELAY)["evidence"]["worstSeconds"] == 900


def test_records_without_timing_are_counted_not_assumed() -> None:
    report = build_capacity_report(
        [run("timed", "w1", 0, 30, queued=0), run("untimed", "w1", None, None)],
        window=WINDOW,
    )
    assert report["coverage"]["runsUntimed"] == 1
    assert report["coverage"]["waitsUntimed"] == 1
    assert report["concurrency"]["runs"] == 1


# -- dependency-blocked versus resource-blocked --------------------------------


def _saturated_pool_records() -> list[UsageRecord]:
    # One slot: A holds it 00:00-00:30 while B waits from 00:05.
    return [run("A", "w1", 0, 30, queued=0), run("B", "w1", 30, 40, queued=5)]


def test_a_wait_behind_a_full_pool_is_classified_as_resource_blocked() -> None:
    inputs = CapacityInputs(pools=(RunnerPool("hosted", slots=1, workers=("w1",)),))
    report = build_capacity_report(_saturated_pool_records(), window=WINDOW, inputs=inputs)
    causes = report["wait"]["byCause"]

    assert causes[WaitCause.RESOURCE.value]["seconds"] == 1500
    assert causes[WaitCause.RESOURCE.value]["waits"] == 1
    assert causes[WaitCause.RESOURCE.value]["blockers"] == {"hosted": 1}
    assert causes[WaitCause.DEPENDENCY.value]["seconds"] == 0
    assert causes[WaitCause.UNCLASSIFIED.value]["seconds"] == 0


def test_declared_dependency_evidence_outranks_the_resource_derivation() -> None:
    inputs = CapacityInputs(
        pools=(RunnerPool("hosted", slots=1, workers=("w1",)),),
        dependency_blocked={"B": "alpha:unit-0"},
    )
    report = build_capacity_report(_saturated_pool_records(), window=WINDOW, inputs=inputs)
    causes = report["wait"]["byCause"]

    assert causes[WaitCause.DEPENDENCY.value]["seconds"] == 1500
    assert causes[WaitCause.DEPENDENCY.value]["blockers"] == {"alpha:unit-0": 1}
    assert causes[WaitCause.RESOURCE.value]["seconds"] == 0


def test_a_wait_with_no_evidence_stays_unclassified() -> None:
    # No pool declared, so nothing can say the wait was for a slot.
    report = build_capacity_report(_saturated_pool_records(), window=WINDOW)
    causes = report["wait"]["byCause"]

    assert causes[WaitCause.UNCLASSIFIED.value]["seconds"] == 1500
    assert causes[WaitCause.RESOURCE.value]["seconds"] == 0
    assert causes[WaitCause.DEPENDENCY.value]["seconds"] == 0


def test_a_wait_on_a_pool_with_a_free_slot_is_not_called_resource_blocked() -> None:
    inputs = CapacityInputs(pools=(RunnerPool("hosted", slots=4, workers=("w1", "w2")),))
    report = build_capacity_report(_saturated_pool_records(), window=WINDOW, inputs=inputs)

    assert report["wait"]["byCause"][WaitCause.RESOURCE.value]["seconds"] == 0
    assert report["wait"]["byCause"][WaitCause.UNCLASSIFIED.value]["seconds"] == 1500


# -- multi-runner and multi-project utilization --------------------------------


def _fleet_records() -> list[UsageRecord]:
    return [
        run("a1", "w1", 0, 30, queued=0),
        run("a2", "w2", 0, 15, queued=0),
        run("b1", "mm-runner-1", 0, 60, queued=0, project_id="beta", work_unit_id="unit-9"),
    ]


def test_per_runner_and_per_pool_utilization_across_projects() -> None:
    inputs = CapacityInputs(
        pools=(
            RunnerPool("hosted", slots=2, workers=("w1", "w2"), kind="github-hosted"),
            RunnerPool("self-hosted", slots=1, workers=("mm-runner-1",), kind="self-hosted"),
        )
    )
    report = build_capacity_report(_fleet_records(), window=WINDOW, inputs=inputs)

    workers = report["runners"]["byWorker"]
    assert workers["w1"]["utilization"] == round(1800 / 3600, 4)
    assert workers["w2"]["utilization"] == round(900 / 3600, 4)
    assert workers["mm-runner-1"]["utilization"] == 1.0
    assert workers["w1"]["pool"] == "hosted"

    pools = report["runners"]["byPool"]
    assert pools["hosted"]["availableSlotSeconds"] == 2 * 3600
    assert pools["hosted"]["utilization"] == round(2700 / 7200, 4)
    assert pools["hosted"]["saturatedSeconds"] == 900, "both slots busy only for the first 15m"
    assert pools["self-hosted"]["utilization"] == 1.0
    assert pools["self-hosted"]["kind"] == "self-hosted"
    assert report["runners"]["undeclaredWorkers"] == []

    by_project = report["byProject"]
    assert set(by_project) == {"alpha", "beta"}
    assert by_project["alpha"]["concurrency"]["busySeconds"] == 2700
    assert by_project["beta"]["concurrency"]["busySeconds"] == 3600
    assert by_project["beta"]["concurrency"]["peak"] == 1


def test_project_scoping_excludes_other_projects_entirely() -> None:
    report = build_capacity_report(_fleet_records(), window=WINDOW, project_ids=["beta"])

    assert set(report["byProject"]) == {"beta"}
    assert report["concurrency"]["busySeconds"] == 3600
    assert report["coverage"]["records"] == 1


def test_an_undeclared_runner_has_no_pool_utilization_to_report() -> None:
    inputs = CapacityInputs(pools=(RunnerPool("hosted", slots=2, workers=("w1", "w2")),))
    report = build_capacity_report(_fleet_records(), window=WINDOW, inputs=inputs)

    assert report["runners"]["undeclaredWorkers"] == ["mm-runner-1"]
    assert report["runners"]["undeclaredWorkerUtilization"] == "unknown"
    assert set(report["runners"]["byPool"]) == {"hosted"}


def test_a_pool_whose_declared_slots_cannot_hold_the_observed_work_fails_closed() -> None:
    inputs = CapacityInputs(pools=(RunnerPool("hosted", slots=1, workers=("w1", "w2")),))
    with pytest.raises(CapacityError, match="declared slot count cannot be right"):
        build_capacity_report(_fleet_records(), window=WINDOW, inputs=inputs)


def test_pool_declarations_are_validated() -> None:
    with pytest.raises(CapacityError, match="slots must be an integer greater than zero"):
        RunnerPool("hosted", slots=0)
    with pytest.raises(CapacityError, match="poolId is required"):
        RunnerPool("  ", slots=1)
    with pytest.raises(CapacityError, match="duplicate worker"):
        RunnerPool("hosted", slots=2, workers=("w1", "w1"))
    with pytest.raises(CapacityError, match="declared by both"):
        CapacityInputs(
            pools=(
                RunnerPool("a", slots=1, workers=("w1",)),
                RunnerPool("b", slots=1, workers=("w1",)),
            )
        )
    with pytest.raises(CapacityError, match="duplicate runner pool"):
        CapacityInputs(pools=(RunnerPool("a", slots=1), RunnerPool("a", slots=1)))


# -- model and provider concurrency --------------------------------------------


def test_per_model_concurrency_and_stage_mix() -> None:
    records = [
        run("a", "w1", 0, 30, model="claude-opus-5"),
        run("b", "w2", 10, 40, model="claude-opus-5", stage=LifecycleStage.INDEPENDENT_REVIEW),
        run("c", "w3", 0, 20, model="gpt-5-codex", provider="codex"),
    ]
    report = build_capacity_report(records, window=WINDOW)

    claude = report["models"]["byModel"]["claude-opus-5"]
    assert claude["peak"] == 2
    assert claude["busySeconds"] == 3600
    assert claude["stageMix"] == {"implementation": 1, "independent-review": 1}
    assert report["models"]["byModel"]["gpt-5-codex"]["peak"] == 1
    assert set(report["models"]["byProvider"]) == {"anthropic", "codex"}


# -- unknown capacity is never a made-up percentage ----------------------------


def test_plan_capacity_without_an_observation_is_unknown_with_proxies() -> None:
    subscription = monetary_equivalent(
        TokenCounts(1000, 500, 0, 0), CLAUDE_MAX, plan_capacity_units=0.25
    )
    report = build_capacity_report(
        [run("a", "w1", 0, 30, monetary=subscription), run("b", "w2", 10, 40)],
        window=WINDOW,
    )
    anthropic = report["planCapacity"]["anthropic"]

    assert anthropic["status"] == "unknown"
    assert anthropic["unitsTotal"] is None
    assert anthropic["unitsUsed"] is None
    assert anthropic["usedFraction"] is None, "an unobserved plan is never a percentage"
    proxies = anthropic["usageProxies"]
    assert proxies["peakConcurrentRuns"] == 2
    assert proxies["observedCapacityUnits"] == {"five-hour-window-share": 0.25}
    assert proxies["runs"] == 2


def test_an_observed_plan_reports_its_real_fraction_and_remaining_units() -> None:
    observation = PlanCapacityObservation(
        provider="anthropic",
        plan="Claude Max",
        capacity_unit="five-hour-window-share",
        observed_at=at(30),
        units_total=100,
        units_used=25,
    )
    report = build_capacity_report(
        [run("a", "w1", 0, 30)],
        window=WINDOW,
        inputs=CapacityInputs(plan_capacity=(observation,)),
    )
    anthropic = report["planCapacity"]["anthropic"]

    assert anthropic["status"] == "observed"
    assert anthropic["usedFraction"] == 0.25
    assert anthropic["unitsRemaining"] == 75


def test_a_partially_observed_plan_does_not_invent_the_missing_half() -> None:
    observation = PlanCapacityObservation(
        provider="anthropic",
        plan="Claude Max",
        capacity_unit="five-hour-window-share",
        observed_at=at(30),
        units_used=25,
    )
    report = build_capacity_report(
        [run("a", "w1", 0, 30)],
        window=WINDOW,
        inputs=CapacityInputs(plan_capacity=(observation,)),
    )
    anthropic = report["planCapacity"]["anthropic"]

    assert anthropic["status"] == "partial"
    assert anthropic["usedFraction"] is None
    assert anthropic["unitsRemaining"] is None
    assert anthropic["unitsUsed"] == 25


def test_plan_capacity_observations_are_validated() -> None:
    with pytest.raises(CapacityError, match="used exceeds the declared total"):
        PlanCapacityObservation("anthropic", "Max", "share", at(0), units_total=1, units_used=2)
    with pytest.raises(CapacityError, match="provider is required"):
        PlanCapacityObservation(" ", "Max", "share", at(0))
    with pytest.raises(CapacityError, match="capacityUnit is required"):
        PlanCapacityObservation("anthropic", "Max", " ", at(0))
    with pytest.raises(CapacityError, match="RFC 3339"):
        PlanCapacityObservation("anthropic", "Max", "share", "soon")


# -- retry and redundant-work accounting ---------------------------------------


def test_useful_work_separates_first_pass_success_from_rework() -> None:
    records = [
        run("ok", "w1", 0, 10, result=UsageResult.COMPLETED),
        run("failed", "w2", 0, 20, result=UsageResult.FAILED),
        run("retry", "w3", 20, 30, attempt=2, result=UsageResult.COMPLETED),
        run("cancelled", "w4", 0, 5, result=UsageResult.CANCELLED),
    ]
    report = build_capacity_report(records, window=WINDOW)
    useful = report["usefulWork"]

    assert useful["rawBusySeconds"] == 600 + 1200 + 600 + 300
    assert useful["usefulSeconds"] == 600
    assert useful["reworkSeconds"] == 2100
    assert useful["usefulWorkRatio"] == round(600 / 2700, 4)
    assert useful["byResult"]["failed"] == {"seconds": 1200, "runs": 1}
    assert useful["byResult"]["cancelled"] == {"seconds": 300, "runs": 1}
    assert useful["retries"] == {"seconds": 600, "runs": 1}
    assert useful["firstAttempt"]["runs"] == 3


def test_a_successful_retry_still_counts_as_rework() -> None:
    report = build_capacity_report(
        [run("second-try", "w1", 0, 30, attempt=2, result=UsageResult.COMPLETED)],
        window=WINDOW,
    )
    assert report["usefulWork"]["usefulSeconds"] == 0
    assert report["usefulWork"]["usefulWorkRatio"] == 0.0
    assert report["usefulWork"]["retries"]["seconds"] == 1800


# -- bottlenecks and recommendations -------------------------------------------


def test_runner_shortage_is_reported_with_the_seconds_that_show_it() -> None:
    inputs = CapacityInputs(pools=(RunnerPool("hosted", slots=1, workers=("w1",)),))
    report = build_capacity_report(_saturated_pool_records(), window=WINDOW, inputs=inputs)
    finding = find(report, BottleneckKind.RUNNER_SHORTAGE)

    assert finding["scope"] == "pool:hosted"
    assert finding["impactSeconds"] == 1500
    assert finding["evidence"]["saturatedSeconds"] == 2400
    assert finding["evidence"]["queuedWhileSaturatedSeconds"] == 1500
    assert finding["evidence"]["slots"] == 1


def test_a_saturated_pool_with_nothing_queued_is_not_a_shortage() -> None:
    inputs = CapacityInputs(pools=(RunnerPool("hosted", slots=1, workers=("w1",)),))
    report = build_capacity_report([run("A", "w1", 0, 60, queued=0)], window=WINDOW, inputs=inputs)
    assert report["runners"]["byPool"]["hosted"]["utilization"] == 1.0
    assert find(report, BottleneckKind.RUNNER_SHORTAGE) is None


def test_a_serialized_concurrency_group_is_identified() -> None:
    records = [run("A", "w1", 0, 30, queued=0), run("B", "w2", 30, 50, queued=5)]
    inputs = CapacityInputs(concurrency_groups={"A": "migrations", "B": "migrations"})
    report = build_capacity_report(records, window=WINDOW, inputs=inputs)
    finding = find(report, BottleneckKind.SERIALIZED_CONCURRENCY_GROUP)

    assert finding["scope"] == "group:migrations"
    assert finding["evidence"]["peakConcurrency"] == 1
    assert finding["evidence"]["queuedWhileGroupBusySeconds"] == 1500


def test_model_capacity_throttling_uses_a_declared_ceiling() -> None:
    records = [
        run("A", "w1", 0, 30, queued=0),
        run("B", "w2", 0, 30, queued=0),
        run("C", "w3", 30, 40, queued=10),
    ]
    inputs = CapacityInputs(provider_limits={"anthropic": 2})
    report = build_capacity_report(records, window=WINDOW, inputs=inputs)
    finding = find(report, BottleneckKind.MODEL_CAPACITY_THROTTLING)

    assert finding["scope"] == "provider:anthropic"
    assert finding["evidence"]["concurrentRunLimit"] == 2
    assert finding["evidence"]["atLimitSeconds"] == 1800
    assert finding["evidence"]["queuedWhileAtLimitSeconds"] == 1200


def test_an_exhausted_plan_is_reported_even_without_a_queue() -> None:
    observation = PlanCapacityObservation(
        provider="anthropic",
        plan="Claude Max",
        capacity_unit="share",
        observed_at=at(30),
        units_total=10,
        units_used=10,
    )
    report = build_capacity_report(
        [run("a", "w1", 0, 30)],
        window=WINDOW,
        inputs=CapacityInputs(plan_capacity=(observation,)),
    )
    finding = find(report, BottleneckKind.MODEL_CAPACITY_THROTTLING)
    assert finding["evidence"]["unitsUsed"] == 10
    assert "fully consumed" in finding["explanation"]
    assert finding["scope"] == "provider:anthropic:plan"


def test_every_finding_is_separately_addressable() -> None:
    # A provider can hit its concurrency ceiling and exhaust its plan at once;
    # the two findings must not collapse into one indistinguishable action.
    observation = PlanCapacityObservation(
        provider="anthropic",
        plan="Claude Max",
        capacity_unit="share",
        observed_at=at(30),
        units_total=8,
        units_used=8,
    )
    records = [
        run("A", "w1", 0, 30, queued=0),
        run("B", "w2", 0, 30, queued=0),
        run("C", "w3", 30, 40, queued=10),
    ]
    report = build_capacity_report(
        records,
        window=WINDOW,
        inputs=CapacityInputs(provider_limits={"anthropic": 2}, plan_capacity=(observation,)),
    )
    scopes = [(item["kind"], item["scope"]) for item in report["bottlenecks"]]
    assert len(scopes) == len(set(scopes))
    assert ("model-capacity-throttling", "provider:anthropic") in scopes
    assert ("model-capacity-throttling", "provider:anthropic:plan") in scopes


def test_dependency_fan_in_needs_enough_waits_to_be_worth_reporting() -> None:
    def waiting(name: str, start: int) -> UsageRecord:
        return run(name, f"w{name}", start, start + 5, queued=0, work_unit_id=f"unit-{name}")

    blocked = {"a": "alpha:unit-0", "b": "alpha:unit-0"}
    two = build_capacity_report(
        [waiting("a", 10), waiting("b", 10)],
        window=WINDOW,
        inputs=CapacityInputs(dependency_blocked=blocked),
    )
    assert find(two, BottleneckKind.DEPENDENCY_FAN_IN) is None

    blocked["c"] = "alpha:unit-0"
    three = build_capacity_report(
        [waiting("a", 10), waiting("b", 10), waiting("c", 10)],
        window=WINDOW,
        inputs=CapacityInputs(dependency_blocked=blocked),
    )
    finding = find(three, BottleneckKind.DEPENDENCY_FAN_IN)
    assert finding["evidence"]["waitingRuns"] == 3
    assert finding["evidence"]["blockedSeconds"] == 1800
    assert finding["scope"] == "dependency:alpha:unit-0"


def test_repeated_review_cycles_are_surfaced() -> None:
    records = [
        run("repair", "w1", 0, 20, repair_cycle=1),
        run("re-review", "w2", 0, 10, review_round=2),
        run("clean", "w3", 0, 10),
    ]
    report = build_capacity_report(records, window=WINDOW)
    finding = find(report, BottleneckKind.REPEATED_REVIEW_CYCLES)

    assert finding["evidence"]["runs"] == 2
    assert finding["evidence"]["seconds"] == 1200 + 600


def test_recommendations_are_advisory_and_trace_back_to_their_evidence() -> None:
    inputs = CapacityInputs(pools=(RunnerPool("hosted", slots=1, workers=("w1",)),))
    report = build_capacity_report(_saturated_pool_records(), window=WINDOW, inputs=inputs)

    assert report["bottlenecks"], "the scenario has a real bottleneck"
    assert len(report["recommendations"]) == len(report["bottlenecks"])
    for recommendation, finding in zip(
        report["recommendations"], report["bottlenecks"], strict=True
    ):
        assert recommendation["advisory"] is True
        assert recommendation["kind"] == finding["kind"]
        assert recommendation["scope"] == finding["scope"]
        assert recommendation["evidence"] == finding["evidence"]
        assert recommendation["rationale"] == finding["explanation"]
        assert recommendation["action"]
    assert any("change no routing" in item for item in report["limitations"])


def test_bottlenecks_are_ordered_by_the_time_they_cost() -> None:
    records = _saturated_pool_records() + [run("repair", "w2", 0, 5, repair_cycle=1)]
    inputs = CapacityInputs(pools=(RunnerPool("hosted", slots=1, workers=("w1",)),))
    report = build_capacity_report(records, window=WINDOW, inputs=inputs)

    impacts = [item["impactSeconds"] for item in report["bottlenecks"]]
    assert impacts == sorted(impacts, reverse=True)
    assert report["recommendations"][0]["recommendationId"] == "cap-001"


# -- findings from the independent Codex review on #126 ------------------------


def _at_second(value: str) -> str:
    return f"2026-09-22T00:00:{value}Z"


def _sub_second_run(usage_id: str, started: str, finished: str) -> UsageRecord:
    return UsageRecord(
        usage_id=usage_id,
        work_unit=WorkUnitRef(project_id="alpha", work_unit_id="unit-1"),
        stage=LifecycleStage.IMPLEMENTATION,
        run_id=usage_id,
        recorded_at=at(0),
        actor=EventActor(name="claude", kind="agent", worker="w1"),
        actual=UsageActual(started_at=started, finished_at=finished),
    )


def test_fractional_durations_survive_aggregation() -> None:
    # Three back-to-back runs of six tenths of a second fill two seconds; each
    # truncated on its own would measure zero and the window would read idle.
    window = ObservationWindow(_at_second("00"), _at_second("02"))
    report = build_capacity_report(
        [
            _sub_second_run("a", _at_second("00"), _at_second("00.6")),
            _sub_second_run("b", _at_second("00.6"), _at_second("01.2")),
            _sub_second_run("c", _at_second("01.2"), _at_second("02")),
        ],
        window=window,
    )
    concurrency = report["concurrency"]

    assert concurrency["busySeconds"] == 2
    assert concurrency["occupiedSeconds"] == 2
    assert concurrency["idleSeconds"] == 0
    assert concurrency["mean"] == 1.0
    assert [item["seconds"] for item in concurrency["segments"]] == [0.6, 0.6, 0.8]
    # Whole numbers stay integers so ordinary reports are unchanged.
    assert isinstance(concurrency["busySeconds"], int)


def test_a_retry_of_the_same_stage_is_not_a_handoff() -> None:
    same_stage = build_capacity_report(
        [
            run("try-1", "w1", 0, 10, queued=0),
            run("try-2", "w1", 30, 40, queued=25, attempt=2),
        ],
        window=WINDOW,
    )
    assert same_stage["wait"]["handoff"]["byWorkUnitSeconds"] == {}
    assert find(same_stage, BottleneckKind.HANDOFF_DELAY) is None

    # A genuine change of stage is still measured.
    crossed = build_capacity_report(
        [
            run("plan", "w1", 0, 10, queued=0, stage=LifecycleStage.PLANNING),
            run("build", "w1", 30, 40, queued=25, stage=LifecycleStage.IMPLEMENTATION),
        ],
        window=WINDOW,
    )
    assert crossed["wait"]["handoff"]["byWorkUnitSeconds"] == {"alpha:unit-1": 900}


def test_a_project_that_only_queued_work_still_gets_a_report() -> None:
    # Queued at 00:10, still not started when the window closes at 01:00.
    report = build_capacity_report(
        [run("backlogged", "w1", 70, 80, queued=10, project_id="beta", work_unit_id="unit-9")],
        window=WINDOW,
    )
    assert report["queue"]["busySeconds"] == 3000
    assert "beta" in report["byProject"], "the fully backlogged project must not vanish"
    assert report["byProject"]["beta"]["queue"]["busySeconds"] == 3000
    assert report["byProject"]["beta"]["concurrency"]["busySeconds"] == 0


def test_a_nearly_full_plan_is_not_called_exhausted() -> None:
    nearly = PlanCapacityObservation(
        provider="anthropic",
        plan="Claude Max",
        capacity_unit="share",
        observed_at=at(30),
        units_total=10_000,
        units_used=9_999.6,
    )
    report = build_capacity_report(
        [run("a", "w1", 0, 10)], window=WINDOW, inputs=CapacityInputs(plan_capacity=(nearly,))
    )
    anthropic = report["planCapacity"]["anthropic"]

    assert anthropic["usedFraction"] == 1.0, "rounded for display"
    assert anthropic["unitsRemaining"] == 0.4
    assert anthropic["exhausted"] is False, "judged on the raw values, not the rounded share"
    assert find(report, BottleneckKind.MODEL_CAPACITY_THROTTLING) is None

    full = replace(nearly, units_used=10_000)
    exhausted = build_capacity_report(
        [run("a", "w1", 0, 10)], window=WINDOW, inputs=CapacityInputs(plan_capacity=(full,))
    )
    assert exhausted["planCapacity"]["anthropic"]["exhausted"] is True
    assert find(exhausted, BottleneckKind.MODEL_CAPACITY_THROTTLING) is not None


def test_runs_without_plan_capacity_evidence_count_as_unknown() -> None:
    subscription = monetary_equivalent(
        TokenCounts(1000, 500, 0, 0), CLAUDE_MAX, plan_capacity_units=0.25
    )
    report = build_capacity_report(
        [run("known", "w1", 0, 10, monetary=subscription), run("silent", "w2", 0, 10)],
        window=WINDOW,
    )
    proxies = report["planCapacity"]["anthropic"]["usageProxies"]

    assert proxies["observedCapacityUnits"] == {"five-hour-window-share": 0.25}
    assert proxies["runsWithUnknownCapacityUnits"] == 1, "silence is not complete coverage"


def test_units_derived_from_tokens_are_not_reported_as_observed() -> None:
    estimated = monetary_equivalent(TokenCounts(1000, 500, 0, 0), CODEX_PLAN)
    assert estimated.plan_capacity_units == 0.015

    report = build_capacity_report(
        [run("a", "w1", 0, 10, provider="codex", monetary=estimated)], window=WINDOW
    )
    proxies = report["planCapacity"]["codex"]["usageProxies"]

    assert proxies["observedCapacityUnits"] == {}
    assert proxies["estimatedCapacityUnits"] == {"requests": 0.015}


def test_plan_capacity_rejects_booleans_and_non_finite_numbers() -> None:
    for value in (True, float("inf"), float("nan"), -1):
        with pytest.raises(CapacityError, match="must be a finite number >= 0 or null"):
            PlanCapacityObservation("anthropic", "Max", "share", at(0), units_total=value)


def test_a_dependency_wait_is_not_also_charged_to_a_runner_shortage() -> None:
    inputs = CapacityInputs(
        pools=(RunnerPool("hosted", slots=1, workers=("w1",)),),
        dependency_blocked={"B": "alpha:unit-0"},
    )
    report = build_capacity_report(_saturated_pool_records(), window=WINDOW, inputs=inputs)

    assert report["wait"]["byCause"][WaitCause.DEPENDENCY.value]["seconds"] == 1500
    assert find(report, BottleneckKind.RUNNER_SHORTAGE) is None, (
        "the wait already has an explicit cause; charging it twice contradicts it"
    )
    assert report["runners"]["byPool"]["hosted"]["saturatedSeconds"] == 2400


def test_a_worker_outside_every_declared_pool_has_no_utilization() -> None:
    bare = build_capacity_report([run("a", "w1", 30, 60)], window=WINDOW)
    worker = bare["runners"]["byWorker"]["w1"]

    assert worker["utilization"] is None, "nothing grounds an undeclared worker's capacity"
    assert worker["utilizationStatus"] == "unknown"
    assert worker["pool"] is None
    assert worker["busySeconds"] == 1800

    declared = build_capacity_report(
        [run("a", "w1", 30, 60)],
        window=WINDOW,
        inputs=CapacityInputs(pools=(RunnerPool("hosted", slots=1, workers=("w1",)),)),
    )
    grounded = declared["runners"]["byWorker"]["w1"]
    assert grounded["utilization"] == 0.5
    assert grounded["utilizationStatus"] == "observed"


def test_two_observations_for_one_provider_are_refused() -> None:
    early = PlanCapacityObservation(
        "anthropic", "Max", "share", at(10), units_total=100, units_used=10
    )
    late = replace(early, observed_at=at(50), units_used=90)
    with pytest.raises(CapacityError, match="more than one plan capacity observation"):
        CapacityInputs(plan_capacity=(early, late))


def test_declared_identifiers_must_be_strings() -> None:
    base = {
        "planCapacity": [
            {"provider": "anthropic", "plan": "Max", "capacityUnit": "share", "observedAt": at(0)}
        ]
    }
    assert load_capacity_inputs(base).plan_capacity[0].provider == "anthropic"

    for field_name, message in (
        ("provider", "provider is required"),
        ("capacityUnit", "capacityUnit is required"),
    ):
        document = {"planCapacity": [dict(base["planCapacity"][0]) | {field_name: None}]}
        with pytest.raises(CapacityError, match=message):
            load_capacity_inputs(document)

    document = {"planCapacity": [dict(base["planCapacity"][0]) | {"provider": 7}]}
    with pytest.raises(CapacityError, match="provider must be a string"):
        load_capacity_inputs(document)

    with pytest.raises(CapacityError, match="poolId must be a string"):
        load_capacity_inputs({"pools": [{"poolId": 1, "slots": 1}]})
    with pytest.raises(CapacityError, match="non-empty strings"):
        load_capacity_inputs({"pools": [{"poolId": "p", "slots": 1, "workers": [""]}]})
    with pytest.raises(CapacityError, match="must be a finite number >= 0 or null"):
        load_capacity_inputs(
            {"planCapacity": [dict(base["planCapacity"][0]) | {"unitsTotal": True}]}
        )
