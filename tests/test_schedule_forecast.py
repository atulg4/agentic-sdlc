from __future__ import annotations

import json
from datetime import datetime

import pytest

from agentic_sdlc.event_ledger import EventActor, LifecycleStage, WorkUnitRef
from agentic_sdlc.schedule_forecast import (
    DEFAULT_MIN_SAMPLE_SIZE,
    BlockingReason,
    ScheduleForecastError,
    build_schedule_forecast,
    load_work_graph_document,
    quantile,
)
from agentic_sdlc.usage_ledger import UsageActual, UsageRecord, UsageResult
from agentic_sdlc.work_graph import WorkGraph, WorkUnitSpec

NOW = "2026-10-29T00:00:00Z"
ARTIFACT_A = "sha256:" + "a" * 64
ARTIFACT_B = "sha256:" + "b" * 64
ARTIFACT_C = "sha256:" + "c" * 64
ARTIFACT_D = "sha256:" + "d" * 64


def unit(unit_id: str, **overrides: object) -> WorkUnitSpec:
    defaults: dict[str, object] = {
        "mission_id": "m",
        "mission_version": "1",
        "output_artifact": f"sha256:{unit_id * 64}"[:71],
        "max_retries": 2,
    }
    defaults.update(overrides)
    return WorkUnitSpec(unit_id=unit_id, **defaults)  # type: ignore[arg-type]


def graph(*specs: WorkUnitSpec) -> WorkGraph:
    units = {spec.unit_id: spec for spec in specs}
    order = []
    remaining = {uid: set(spec.depends_on) for uid, spec in units.items()}
    while remaining:
        ready = sorted(uid for uid, deps in remaining.items() if not deps)
        for uid in ready:
            order.append(uid)
            del remaining[uid]
        for deps in remaining.values():
            deps.difference_update(ready)
    ancestors: dict[str, frozenset[str]] = {}
    for uid in order:
        merged: set[str] = set()
        for dep in units[uid].depends_on:
            merged.add(dep)
            merged |= ancestors[dep]
        ancestors[uid] = frozenset(merged)
    return WorkGraph(units=units, order=tuple(order), ancestors=ancestors)


def record(
    work_unit_id: str,
    *,
    project_id: str = "proj",
    stage: LifecycleStage = LifecycleStage.IMPLEMENTATION,
    runtime_seconds: int | None = 100,
    result: UsageResult = UsageResult.COMPLETED,
    attempt: int = 1,
    recorded_at: str = NOW,
    finished: bool = True,
    usage_id: str | None = None,
) -> UsageRecord:
    actual = None
    if runtime_seconds is not None or result is not UsageResult.UNKNOWN:
        actual = UsageActual(
            runtime_seconds=runtime_seconds,
            result=result,
            started_at=recorded_at if finished else "",
            finished_at=recorded_at if finished else "",
        )
    return UsageRecord(
        usage_id=usage_id or f"{work_unit_id}-{stage.value}-{attempt}-{recorded_at}",
        work_unit=WorkUnitRef(project_id=project_id, work_unit_id=work_unit_id),
        stage=stage,
        run_id=f"run-{work_unit_id}-{attempt}",
        attempt=attempt,
        recorded_at=recorded_at,
        actor=EventActor(name="claude", kind="agent"),
        actual=actual,
    )


def unit_out(doc: dict, project_id: str, unit_id: str) -> dict:
    return doc["byProject"][project_id]["units"][unit_id]


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


# -- hand-computed quantiles ----------------------------------------------------


def test_quantile_matches_hand_computed_nearest_rank_values() -> None:
    samples = [10, 20, 30, 40, 50]
    assert quantile(samples, 0.50) == 30
    assert quantile(samples, 0.80) == 40
    assert quantile(samples, 0.95) == 50
    assert quantile(samples, 1.0) == 50


def test_quantile_requires_samples_and_a_valid_share() -> None:
    with pytest.raises(ScheduleForecastError):
        quantile([], 0.5)
    with pytest.raises(ScheduleForecastError):
        quantile([1.0], 0.0)
    with pytest.raises(ScheduleForecastError):
        quantile([1.0], 1.5)


def test_duration_forecast_uses_real_history_once_enough_samples_exist() -> None:
    g = graph(unit("a", timeout_seconds=1000))
    records = [
        record("a", runtime_seconds=seconds, recorded_at=f"2026-10-2{i}T00:00:00Z")
        for i, seconds in enumerate([10, 20, 30, 40, 50])
    ]
    doc = build_schedule_forecast({"proj": g}, records, now=NOW)
    duration = unit_out(doc, "proj", "a")["expectedDuration"]
    assert duration == {"p50": 30, "p80": 40, "p95": 50, "sampleSize": 5, "basis": "history"}


# -- low-sample widening across every fallback basis ----------------------------


def test_below_minimum_samples_falls_back_to_pooled_project_history() -> None:
    g = graph(unit("a", timeout_seconds=1000), unit("b", timeout_seconds=1000))
    # "a" has only 2 samples of its own; "b" has enough to pool from.
    records = [record("a", runtime_seconds=100)] * 2 + [
        record("b", runtime_seconds=s, recorded_at=f"2026-10-2{i}T00:00:00Z")
        for i, s in enumerate([10, 20, 30, 40, 50])
    ]
    doc = build_schedule_forecast({"proj": g}, records, now=NOW, min_sample_size=5)
    duration = unit_out(doc, "proj", "a")["expectedDuration"]
    assert duration["basis"] == "pooled-history"
    assert duration["sampleSize"] == 7
    # Real quantiles over the pooled 7 samples, not a narrow range from 2.
    assert duration["p50"] != duration["p95"]


def test_an_insufficient_stage_pool_still_falls_through_to_the_project_pool() -> None:
    # "a" has 1 sample of its own stage (implementation); the project's
    # implementation-stage pool is also just that 1 sample, since "b" runs a
    # different stage. Only the full, cross-stage project pool clears the
    # minimum — the fallback must not stop at the insufficient stage pool.
    g = graph(unit("a", timeout_seconds=1000), unit("b", timeout_seconds=1000))
    records = [record("a", runtime_seconds=100)] + [
        record(
            "b",
            stage=LifecycleStage.INDEPENDENT_REVIEW,
            runtime_seconds=s,
            recorded_at=f"2026-10-2{i}T00:00:00Z",
        )
        for i, s in enumerate([10, 20, 30, 40, 50])
    ]
    doc = build_schedule_forecast({"proj": g}, records, now=NOW, min_sample_size=5)
    duration = unit_out(doc, "proj", "a")["expectedDuration"]
    assert duration["basis"] == "pooled-history"
    assert duration["sampleSize"] == 6


def test_no_history_at_all_falls_back_to_a_widening_declared_limit_range() -> None:
    g = graph(unit("a", timeout_seconds=1000))
    doc = build_schedule_forecast({"proj": g}, [], now=NOW)
    duration = unit_out(doc, "proj", "a")["expectedDuration"]
    assert duration == {
        "p50": 500,
        "p80": 800,
        "p95": 950,
        "sampleSize": 0,
        "basis": "declared-limit",
    }
    assert duration["p50"] < duration["p80"] < duration["p95"]


def test_a_handful_of_samples_never_produces_a_narrow_range() -> None:
    # Three identical-duration samples: real quantiles would be degenerate
    # (p50 == p95), which this module refuses to report as "history".
    g = graph(unit("a", timeout_seconds=1000))
    records = [
        record("a", runtime_seconds=42, recorded_at=f"2026-10-2{i}T00:00:00Z") for i in range(3)
    ]
    doc = build_schedule_forecast({"proj": g}, records, now=NOW, min_sample_size=5)
    duration = unit_out(doc, "proj", "a")["expectedDuration"]
    assert duration["basis"] == "declared-limit"
    assert duration["sampleSize"] == 0


def test_min_sample_size_is_configurable() -> None:
    g = graph(unit("a", timeout_seconds=1000))
    records = [
        record("a", runtime_seconds=s, recorded_at=f"2026-10-2{i}T00:00:00Z")
        for i, s in enumerate([10, 20])
    ]
    doc = build_schedule_forecast({"proj": g}, records, now=NOW, min_sample_size=2)
    duration = unit_out(doc, "proj", "a")["expectedDuration"]
    assert duration["basis"] == "history"
    assert duration["sampleSize"] == 2


# -- critical path ---------------------------------------------------------------


def _fixed_duration_records(unit_id: str, seconds: int) -> list[UsageRecord]:
    # Enough identical-ish samples to clear the default minimum, with a
    # slight spread so the range is not degenerate.
    return [
        record(unit_id, runtime_seconds=seconds + offset, recorded_at=f"2026-10-2{i}T00:00:00Z")
        for i, offset in enumerate(range(DEFAULT_MIN_SAMPLE_SIZE))
    ]


def test_critical_path_on_a_linear_chain() -> None:
    g = graph(
        unit("a", timeout_seconds=100),
        unit("b", depends_on=("a",), timeout_seconds=100),
        unit("c", depends_on=("b",), timeout_seconds=100),
    )
    records = (
        _fixed_duration_records("a", 10)
        + _fixed_duration_records("b", 10)
        + _fixed_duration_records("c", 10)
    )
    doc = build_schedule_forecast({"proj": g}, records, now=NOW)
    assert doc["byProject"]["proj"]["criticalPath"]["p50"] == ["a", "b", "c"]
    for unit_id in ("a", "b", "c"):
        assert unit_out(doc, "proj", unit_id)["onCriticalPath"] is True


def test_critical_path_on_a_diamond_prefers_the_longer_branch() -> None:
    #   a -> b (short) -> d
    #   a -> c (long)  -> d
    g = graph(
        unit("a", timeout_seconds=100),
        unit("b", depends_on=("a",), timeout_seconds=100),
        unit("c", depends_on=("a",), timeout_seconds=100),
        unit("d", depends_on=("b", "c"), timeout_seconds=100),
    )
    records = (
        _fixed_duration_records("a", 10)
        + _fixed_duration_records("b", 5)
        + _fixed_duration_records("c", 50)
        + _fixed_duration_records("d", 10)
    )
    doc = build_schedule_forecast({"proj": g}, records, now=NOW)
    assert doc["byProject"]["proj"]["criticalPath"]["p50"] == ["a", "c", "d"]
    assert unit_out(doc, "proj", "b")["onCriticalPath"] is False
    assert unit_out(doc, "proj", "c")["onCriticalPath"] is True


def test_fully_independent_units_each_anchor_their_own_path() -> None:
    g = graph(unit("a", timeout_seconds=100), unit("b", timeout_seconds=100))
    records = _fixed_duration_records("a", 10) + _fixed_duration_records("b", 50)
    doc = build_schedule_forecast({"proj": g}, records, now=NOW)
    # The longer of the two independent units is the critical path.
    assert doc["byProject"]["proj"]["criticalPath"]["p50"] == ["b"]


def test_critical_path_ties_break_on_unit_id() -> None:
    g = graph(unit("x", timeout_seconds=100), unit("y", timeout_seconds=100))
    records = _fixed_duration_records("x", 10) + _fixed_duration_records("y", 10)
    doc = build_schedule_forecast({"proj": g}, records, now=NOW)
    # Both finish at the same offset; "x" sorts before "y".
    assert doc["byProject"]["proj"]["criticalPath"]["p50"] == ["x"]


def test_critical_path_recomputes_after_a_unit_completes() -> None:
    g = graph(
        unit("a", timeout_seconds=100),
        unit("b", depends_on=("a",), timeout_seconds=100),
    )
    records = _fixed_duration_records("a", 10) + _fixed_duration_records("b", 10)
    before = build_schedule_forecast({"proj": g}, records, now=NOW)
    assert unit_out(before, "proj", "a")["blockingReason"] is None
    assert unit_out(before, "proj", "b")["blockingReason"] == "dependency_blocked"

    merged = record("a", stage=LifecycleStage.MERGE, runtime_seconds=None)
    after = build_schedule_forecast({"proj": g}, [*records, merged], now=NOW)
    assert unit_out(after, "proj", "a")["done"] is True
    assert unit_out(after, "proj", "b")["blockingReason"] is None
    # "a" is still the structural predecessor on the path to "b", but as a
    # completed unit it contributes zero remaining duration: "b" can start
    # immediately (at "now") rather than waiting out "a"'s own duration again.
    assert after["byProject"]["proj"]["criticalPath"]["p50"] == ["a", "b"]
    assert unit_out(after, "proj", "b")["earliestStart"] == "2026-10-29T00:00:00+00:00"


def test_critical_path_recomputes_after_a_unit_fails_and_retries() -> None:
    # max_retries=2 allows 3 total attempts (one initial plus two retries),
    # matching MissionLedger's own "attempts >= max_retries + 1" rule.
    g = graph(unit("a", timeout_seconds=100, max_retries=2))
    failing = _fixed_duration_records("a", 10) + [
        record(
            "a",
            stage=LifecycleStage.DETERMINISTIC_VERIFICATION,
            result=UsageResult.FAILED,
            attempt=1,
            recorded_at="2026-10-28T00:00:00Z",
        )
    ]
    doc = build_schedule_forecast({"proj": g}, failing, now=NOW)
    # A retry is still available (attempt 1 of 3 allowed): this reads as active.
    assert unit_out(doc, "proj", "a")["blockingReason"] is None

    exhausted = failing + [
        record(
            "a",
            stage=LifecycleStage.DETERMINISTIC_VERIFICATION,
            result=UsageResult.FAILED,
            attempt=2,
            recorded_at="2026-10-28T01:00:00Z",
        ),
        record(
            "a",
            stage=LifecycleStage.DETERMINISTIC_VERIFICATION,
            result=UsageResult.FAILED,
            attempt=3,
            recorded_at="2026-10-28T02:00:00Z",
        ),
    ]
    doc2 = build_schedule_forecast({"proj": g}, exhausted, now=NOW)
    assert unit_out(doc2, "proj", "a")["blockingReason"] == "test_or_verification_failed"


# -- every BlockingReason mapping -------------------------------------------------


def test_dependency_blocked_from_an_unfinished_ancestor() -> None:
    g = graph(unit("a", timeout_seconds=100), unit("b", depends_on=("a",), timeout_seconds=100))
    doc = build_schedule_forecast({"proj": g}, [], now=NOW)
    assert unit_out(doc, "proj", "b")["blockingReason"] == "dependency_blocked"


def test_test_or_verification_failed_from_exhausted_review_retries() -> None:
    # max_retries=1 allows 2 total attempts; both must fail to be exhausted.
    g = graph(unit("a", timeout_seconds=100, max_retries=1))
    records = [
        record(
            "a",
            stage=LifecycleStage.INDEPENDENT_REVIEW,
            result=UsageResult.FAILED,
            attempt=attempt,
            recorded_at=f"2026-10-28T0{attempt}:00:00Z",
        )
        for attempt in (1, 2)
    ]
    doc = build_schedule_forecast({"proj": g}, records, now=NOW)
    assert unit_out(doc, "proj", "a")["blockingReason"] == "test_or_verification_failed"


def test_unknown_from_exhausted_non_verification_retries() -> None:
    g = graph(unit("a", timeout_seconds=100, max_retries=1))
    records = [
        record(
            "a",
            stage=LifecycleStage.IMPLEMENTATION,
            result=UsageResult.FAILED,
            attempt=attempt,
            recorded_at=f"2026-10-28T0{attempt}:00:00Z",
        )
        for attempt in (1, 2)
    ]
    doc = build_schedule_forecast({"proj": g}, records, now=NOW)
    assert unit_out(doc, "proj", "a")["blockingReason"] == "unknown"


def test_exclusive_resource_locked_from_a_held_resource() -> None:
    g = graph(unit("a", timeout_seconds=100, shared_resources=("schema:billing",)))
    doc = build_schedule_forecast(
        {"proj": g},
        [],
        now=NOW,
        held_resources={"proj": {"schema:billing": "someone-else"}},
    )
    assert unit_out(doc, "proj", "a")["blockingReason"] == "exclusive_resource_locked"


def test_a_shared_composition_strategy_alone_does_not_block_either_unit() -> None:
    # GraphExecutor.start leases only ownedPaths/sharedResources, never
    # writePaths, so two units sharing an explicit composition strategy over
    # overlapping write scope are free to run concurrently in the real
    # pipeline — this module must not invent a serialization that isn't
    # actually enforced.
    g = graph(
        unit("a", timeout_seconds=100, write_paths=("src/x.py",), composition="last-writer"),
        unit("b", timeout_seconds=100, write_paths=("src/x.py",), composition="last-writer"),
    )
    doc = build_schedule_forecast({"proj": g}, [], now=NOW)
    assert unit_out(doc, "proj", "a")["blockingReason"] is None
    assert unit_out(doc, "proj", "b")["blockingReason"] is None


@pytest.mark.parametrize(
    "reason",
    [
        BlockingReason.POLICY_BLOCKED,
        BlockingReason.HUMAN_APPROVAL_REQUIRED,
        BlockingReason.PROVIDER_CAPACITY_EXHAUSTED,
        BlockingReason.SUBSCRIPTION_QUOTA_EXHAUSTED,
        BlockingReason.RUNNER_CAPACITY_EXHAUSTED,
        BlockingReason.BUDGET_BLOCKED,
        BlockingReason.QUALITY_FLOOR_UNMET,
        BlockingReason.CONFLICTING_CHANGE_SCOPE,
        BlockingReason.ENVIRONMENT_UNAVAILABLE,
        BlockingReason.SECURITY_OR_COMPLIANCE_BLOCKED,
    ],
)
def test_every_declared_blocking_reason_is_reported_verbatim(reason: BlockingReason) -> None:
    g = graph(unit("a", timeout_seconds=100))
    doc = build_schedule_forecast({"proj": g}, [], now=NOW, declared_blocks={"proj": {"a": reason}})
    assert unit_out(doc, "proj", "a")["blockingReason"] == reason.value


def test_unknown_is_never_counted_as_active_or_queued() -> None:
    g = graph(unit("a", timeout_seconds=100, max_retries=1))
    records = [
        record(
            "a",
            stage=LifecycleStage.IMPLEMENTATION,
            result=UsageResult.FAILED,
            attempt=attempt,
            recorded_at=f"2026-10-28T0{attempt}:00:00Z",
        )
        for attempt in (1, 2)
    ]
    doc = build_schedule_forecast({"proj": g}, records, now=NOW)
    project = doc["byProject"]["proj"]
    assert project["activeCount"] == 0
    assert project["queuedCount"] == 0
    assert project["blockersByReason"]["unknown"] == ["a"]


def test_a_runnable_unit_in_flight_counts_as_active_not_queued() -> None:
    g = graph(unit("a", timeout_seconds=100))
    records = [record("a", runtime_seconds=None, result=UsageResult.UNKNOWN, finished=False)]
    doc = build_schedule_forecast({"proj": g}, records, now=NOW)
    project = doc["byProject"]["proj"]
    assert project["activeCount"] == 1
    assert project["queuedCount"] == 0


def test_declared_blocks_must_reference_a_real_unit() -> None:
    g = graph(unit("a", timeout_seconds=100))
    with pytest.raises(ScheduleForecastError):
        build_schedule_forecast(
            {"proj": g},
            [],
            now=NOW,
            declared_blocks={"proj": {"does-not-exist": BlockingReason.POLICY_BLOCKED}},
        )


def test_an_indeterminate_block_propagates_through_dependents_and_the_project_eta() -> None:
    # "a" is blocked on something with no knowable resolution time; "b"
    # depends on "a" and is itself only dependency_blocked. Even so, "b"'s
    # ETA must not assume "a"'s approval arrives immediately, and neither
    # should the project's own rollup ETA.
    g = graph(unit("a", timeout_seconds=100), unit("b", depends_on=("a",), timeout_seconds=100))
    doc = build_schedule_forecast(
        {"proj": g},
        [],
        now=NOW,
        declared_blocks={"proj": {"a": BlockingReason.HUMAN_APPROVAL_REQUIRED}},
    )
    assert unit_out(doc, "proj", "a")["etaRange"] == {"p50": None, "p80": None, "p95": None}
    assert unit_out(doc, "proj", "b")["blockingReason"] == "dependency_blocked"
    assert unit_out(doc, "proj", "b")["earliestStart"] is None
    assert unit_out(doc, "proj", "b")["etaRange"] == {"p50": None, "p80": None, "p95": None}
    assert doc["byProject"]["proj"]["etaRange"] == {"p50": None, "p80": None, "p95": None}
    # The critical path is still reported structurally; only its timing is unknown.
    assert doc["byProject"]["proj"]["criticalPath"]["p50"] == ["a", "b"]


def test_recorded_at_offsets_are_compared_as_instants_not_text() -> None:
    # A later UTC timestamp ("...09:00:00Z") can sort after an earlier one
    # with a positive offset ("...10:00:00+02:00" == 08:00 UTC) as plain
    # text; the actual instant order is the reverse.
    g = graph(unit("a", timeout_seconds=100, max_retries=1))
    earlier_but_textually_later = record(
        "a",
        stage=LifecycleStage.INDEPENDENT_REVIEW,
        result=UsageResult.FAILED,
        attempt=2,
        recorded_at="2026-10-28T10:00:00+02:00",  # 08:00 UTC
    )
    later_but_textually_earlier = record(
        "a",
        stage=LifecycleStage.IMPLEMENTATION,
        result=UsageResult.COMPLETED,
        attempt=1,
        recorded_at="2026-10-28T09:00:00Z",  # 09:00 UTC, genuinely later
    )
    doc = build_schedule_forecast(
        {"proj": g}, [earlier_but_textually_later, later_but_textually_earlier], now=NOW
    )
    # The genuinely latest record completed cleanly; it is not misread as
    # the exhausted-retry failure that happened earlier.
    assert unit_out(doc, "proj", "a")["blockingReason"] is None


def test_an_active_units_eta_credits_elapsed_time_instead_of_restarting() -> None:
    g = graph(unit("a", timeout_seconds=1000))
    history = [
        record("a", runtime_seconds=100, recorded_at=f"2026-10-2{i}T00:00:00Z") for i in range(5)
    ]
    # Dispatched 40 seconds before "now", with a p50 history of 100s: about
    # 60s of its own duration forecast should remain, not a fresh 100s.
    in_flight = UsageRecord(
        usage_id="a-impl-running",
        work_unit=WorkUnitRef(project_id="proj", work_unit_id="a"),
        stage=LifecycleStage.IMPLEMENTATION,
        run_id="run-a-running",
        attempt=1,
        recorded_at=NOW,
        actor=EventActor(name="claude", kind="agent"),
        actual=UsageActual(
            result=UsageResult.UNKNOWN,
            started_at="2026-10-28T23:59:20Z",
        ),
    )
    doc = build_schedule_forecast({"proj": g}, [*history, in_flight], now=NOW)
    unit_doc = unit_out(doc, "proj", "a")
    assert unit_doc["expectedDuration"]["p50"] == 100
    remaining = (
        _parse_iso(unit_doc["etaRange"]["p50"]) - _parse_iso(NOW.replace("Z", "+00:00"))
    ).total_seconds()
    assert 55 <= remaining <= 65


# -- cross-project isolation ------------------------------------------------------


def test_cross_project_fixtures_keep_projects_isolated() -> None:
    graph_a = graph(unit("a", timeout_seconds=1000))
    graph_b = graph(unit("a", timeout_seconds=1000))  # same unit id, different project
    # Project alpha has rich history; beta has none. Mixed together in one
    # usage_records list to prove alpha's samples never leak into beta.
    records = [
        record("a", project_id="alpha", runtime_seconds=s, recorded_at=f"2026-10-2{i}T00:00:00Z")
        for i, s in enumerate([10, 20, 30, 40, 50])
    ]
    doc = build_schedule_forecast({"alpha": graph_a, "beta": graph_b}, records, now=NOW)
    alpha_duration = unit_out(doc, "alpha", "a")["expectedDuration"]
    beta_duration = unit_out(doc, "beta", "a")["expectedDuration"]
    assert alpha_duration["basis"] == "history"
    assert beta_duration["basis"] == "declared-limit"
    assert beta_duration["sampleSize"] == 0


def test_project_ids_filters_which_projects_are_forecast() -> None:
    doc = build_schedule_forecast(
        {"alpha": graph(unit("a")), "beta": graph(unit("a"))},
        [],
        now=NOW,
        project_ids=["alpha"],
    )
    assert set(doc["byProject"]) == {"alpha"}


def test_requesting_an_unknown_project_id_fails_closed() -> None:
    with pytest.raises(ScheduleForecastError):
        build_schedule_forecast({"alpha": graph(unit("a"))}, [], now=NOW, project_ids=["ghost"])


# -- determinism -------------------------------------------------------------------


def test_identical_inputs_and_now_produce_byte_identical_json() -> None:
    g = graph(
        unit("a", timeout_seconds=100),
        unit("b", depends_on=("a",), timeout_seconds=100),
    )
    records = _fixed_duration_records("a", 10) + _fixed_duration_records("b", 20)
    first = json.dumps(build_schedule_forecast({"proj": g}, records, now=NOW), sort_keys=True)
    second = json.dumps(build_schedule_forecast({"proj": g}, records, now=NOW), sort_keys=True)
    assert first == second


# -- work graph document loading --------------------------------------------------


def test_load_work_graph_document_builds_one_graph_per_project() -> None:
    document = {
        "schemaVersion": 1,
        "projects": {
            "proj": {
                "units": {
                    "a": {
                        "missionId": "m",
                        "missionVersion": "1",
                        "outputArtifact": ARTIFACT_A,
                    },
                    "b": {
                        "missionId": "m",
                        "missionVersion": "1",
                        "outputArtifact": ARTIFACT_B,
                        "dependsOn": ["a"],
                    },
                }
            }
        },
    }
    graphs = load_work_graph_document(document)
    assert set(graphs) == {"proj"}
    assert graphs["proj"].order == ("a", "b")
    assert graphs["proj"].ancestors["b"] == frozenset({"a"})


def test_load_work_graph_document_rejects_a_cycle() -> None:
    document = {
        "schemaVersion": 1,
        "projects": {
            "proj": {
                "units": {
                    "a": {
                        "missionId": "m",
                        "missionVersion": "1",
                        "outputArtifact": ARTIFACT_A,
                        "dependsOn": ["b"],
                    },
                    "b": {
                        "missionId": "m",
                        "missionVersion": "1",
                        "outputArtifact": ARTIFACT_B,
                        "dependsOn": ["a"],
                    },
                }
            }
        },
    }
    with pytest.raises(ScheduleForecastError):
        load_work_graph_document(document)


def test_load_work_graph_document_rejects_a_missing_dependency() -> None:
    document = {
        "schemaVersion": 1,
        "projects": {
            "proj": {
                "units": {
                    "a": {
                        "missionId": "m",
                        "missionVersion": "1",
                        "outputArtifact": ARTIFACT_A,
                        "dependsOn": ["ghost"],
                    }
                }
            }
        },
    }
    with pytest.raises(ScheduleForecastError):
        load_work_graph_document(document)


def test_load_work_graph_document_rejects_an_unsupported_schema_version() -> None:
    with pytest.raises(ScheduleForecastError):
        load_work_graph_document({"schemaVersion": 2, "projects": {}})


def test_load_work_graph_document_requires_at_least_one_project() -> None:
    with pytest.raises(ScheduleForecastError):
        load_work_graph_document({"schemaVersion": 1, "projects": {}})
