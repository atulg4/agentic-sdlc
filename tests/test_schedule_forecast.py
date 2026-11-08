from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from agentic_sdlc.policy import load_policy
from agentic_sdlc.schedule_forecast import (
    BlockingReason,
    ForecastError,
    build_schedule_forecast,
    classify_signal,
    load_forecast_document,
    nearest_rank,
)
from agentic_sdlc.usage_ledger import load_usage_record

NOW = "2026-11-01T12:00:00Z"
_START = datetime.fromisoformat("2026-10-01T00:00:00+00:00")


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _record(project: str, mission: str, seconds: int, index: int, *, finished: bool = True):
    started = _START + timedelta(hours=index)
    actual = {"startedAt": _iso(started), "result": "completed"}
    if finished:
        actual["finishedAt"] = _iso(started + timedelta(seconds=seconds))
    return load_usage_record(
        {
            "schemaVersion": 1,
            "usageId": f"usage-{project}-{mission}-{index}",
            "workUnit": {"projectId": project, "workUnitId": f"github:example/{project}:issue:1"},
            "stage": "implementation",
            "runId": f"run-{mission}-{index}",
            "missionId": mission,
            "actor": {"name": "claude", "kind": "agent"},
            "recordedAt": _iso(started),
            "actual": actual,
        }
    )


def _history(project: str, mission: str, values: list[int], offset: int = 0) -> list:
    return [_record(project, mission, value, offset + i) for i, value in enumerate(values)]


def _unit(unit_id: str, *, mission: str = "worker", depends_on=(), **observed) -> dict:
    return {
        "unitId": unit_id,
        "missionId": mission,
        "missionVersion": "1.0.0",
        "outputArtifact": "patch",
        "dependsOn": list(depends_on),
        "writePaths": [f"src/{unit_id}/**"],
        "ownedPaths": [f"src/{unit_id}"],
        "timeoutSeconds": 3600,
        **observed,
    }


def _document(*units: dict, project: str = "alpha") -> dict:
    return {"schemaVersion": 1, "graphs": [{"projectId": project, "units": list(units)}]}


@pytest.fixture
def policy(policy_file: Path):
    return load_policy(policy_file)


def _forecast(policy, document: dict, records: list, **kwargs) -> dict:
    return build_schedule_forecast(
        load_forecast_document(document), records, policy=policy, now=NOW, **kwargs
    )


def _project(result: dict, project: str = "alpha") -> dict:
    return next(item for item in result["projects"] if item["projectId"] == project)


def _units(result: dict, project: str = "alpha") -> dict:
    return {unit["unitId"]: unit for unit in _project(result, project)["units"]}


# -- quantiles and fallback ------------------------------------------------


def test_nearest_rank_matches_hand_computed_quantiles() -> None:
    samples = [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000]
    # ceil(0.5*10)=5 -> 500, ceil(0.8*10)=8 -> 800, ceil(0.95*10)=10 -> 1000
    assert [nearest_rank(samples, q) for q in (0.5, 0.8, 0.95)] == [500, 800, 1000]
    # n=5: ceil(2.5)=3 -> 30, ceil(4.0)=4 -> 40, ceil(4.75)=5 -> 50
    assert [nearest_rank([50, 10, 40, 20, 30], q) for q in (0.5, 0.8, 0.95)] == [30, 40, 50]


def test_history_basis_uses_the_missions_own_runtimes(policy) -> None:
    records = _history("alpha", "worker", [100, 200, 300, 400, 500])

    duration = _units(_forecast(policy, _document(_unit("a")), records))["a"]["expectedDuration"]

    assert duration == {"p50": 300, "p80": 400, "p95": 500, "sampleSize": 5, "basis": "history"}


def test_thin_mission_history_falls_back_to_pooled_project_history(policy) -> None:
    records = _history("alpha", "worker", [60, 60]) + _history(
        "alpha", "reviewer", [100, 200, 300, 400], offset=10
    )

    duration = _units(_forecast(policy, _document(_unit("a")), records))["a"]["expectedDuration"]

    # pooled sorted: 60, 60, 100, 200, 300, 400 -> ranks 3, 5, 6
    assert duration == {
        "p50": 100,
        "p80": 300,
        "p95": 400,
        "sampleSize": 6,
        "basis": "pooled-history",
    }


def test_thin_project_history_falls_back_to_the_declared_limit(policy) -> None:
    records = _history("alpha", "worker", [60, 60])

    duration = _units(_forecast(policy, _document(_unit("a")), records))["a"]["expectedDuration"]

    assert duration == {
        "p50": 3600,
        "p80": 3600,
        "p95": 3600,
        "sampleSize": 0,
        "basis": "declared-limit",
    }


def test_unfinished_runs_are_not_history(policy) -> None:
    records = [_record("alpha", "worker", 10, i, finished=False) for i in range(6)]

    duration = _units(_forecast(policy, _document(_unit("a")), records))["a"]["expectedDuration"]

    assert duration["basis"] == "declared-limit"


# -- critical path ---------------------------------------------------------


def _fixed(policy, *units: dict, values: dict[str, list[int]]) -> dict:
    records = []
    for offset, (mission, samples) in enumerate(sorted(values.items())):
        records += _history("alpha", mission, samples, offset=offset * 100)
    return _forecast(policy, _document(*units), records)


def test_linear_chain_path_and_eta(policy) -> None:
    result = _fixed(
        policy,
        _unit("a", mission="m1"),
        _unit("b", mission="m2", depends_on=["a"]),
        _unit("c", mission="m3", depends_on=["b"]),
        values={"m1": [600] * 5, "m2": [1200] * 5, "m3": [300] * 5},
    )
    project = _project(result)
    units = _units(result)

    assert project["criticalPath"] == ["a", "b", "c"]
    assert units["b"]["earliestStart"] == "2026-11-01T12:10:00Z"
    assert units["c"]["etaRange"]["p50"] == "2026-11-01T12:35:00Z"
    assert project["etaRange"]["p50"] == "2026-11-01T12:35:00Z"
    assert all(unit["onCriticalPath"] for unit in units.values())


def test_diamond_follows_the_longer_branch(policy) -> None:
    result = _fixed(
        policy,
        _unit("start", mission="m1"),
        _unit("fast", mission="m2", depends_on=["start"]),
        _unit("slow", mission="m3", depends_on=["start"]),
        _unit("join", mission="m1", depends_on=["fast", "slow"]),
        values={"m1": [60] * 5, "m2": [60] * 5, "m3": [600] * 5},
    )

    assert _project(result)["criticalPath"] == ["start", "slow", "join"]
    assert _units(result)["fast"]["onCriticalPath"] is False


def test_tied_branches_break_by_unit_id(policy) -> None:
    result = _fixed(
        policy,
        _unit("root", mission="m1"),
        _unit("beta", mission="m1", depends_on=["root"]),
        _unit("alpha", mission="m1", depends_on=["root"]),
        _unit("tail", mission="m1", depends_on=["beta", "alpha"]),
        values={"m1": [100] * 5},
    )

    assert _project(result)["criticalPath"] == ["root", "alpha", "tail"]


def test_independent_units_path_is_the_single_longest_unit(policy) -> None:
    result = _fixed(
        policy,
        _unit("x", mission="m1"),
        _unit("y", mission="m2"),
        _unit("z", mission="m1"),
        values={"m1": [100] * 5, "m2": [900] * 5},
    )
    units = _units(result)

    assert _project(result)["criticalPath"] == ["y"]
    assert all(unit["earliestStart"] == "2026-11-01T12:00:00Z" for unit in units.values())
    assert [unit["unitId"] for unit in units.values() if unit["runnable"]] == ["x", "y", "z"]


def test_p80_path_is_reported_separately(policy) -> None:
    result = _fixed(
        policy,
        _unit("steady", mission="m1"),
        _unit("spiky", mission="m2"),
        # m1: always 500. m2: median 100, but p80 rank 4 of 5 -> 1000.
        values={"m1": [500] * 5, "m2": [100, 100, 100, 1000, 1000]},
    )

    assert _project(result)["criticalPath"] == ["steady"]
    assert _project(result)["criticalPathP80"] == ["spiky"]


def test_path_recomputes_as_units_complete_fail_and_retry(policy) -> None:
    values = {"m1": [600] * 5, "m2": [60] * 5}

    def path(**status: dict) -> list[str]:
        units = [
            _unit("a", mission="m1", **status.get("a", {})),
            _unit("b", mission="m1", depends_on=["a"], **status.get("b", {})),
            _unit("c", mission="m2", **status.get("c", {})),
        ]
        return _project(_fixed(policy, *units, values=values))["criticalPath"]

    assert path() == ["a", "b"]
    # a completed: it contributes zero remaining duration and leaves the path.
    assert path(a={"status": "completed"}) == ["b"]
    # a failed: it must run again, so it stays on the path, and b is blocked behind it.
    assert path(a={"status": "failed"}) == ["a", "b"]
    # a retried and running for 9 of its 10 minutes: b now finishes sooner.
    running = _fixed(
        policy,
        _unit("a", mission="m1", status="running", startedAt="2026-11-01T11:51:00Z"),
        _unit("b", mission="m1", depends_on=["a"]),
        _unit("c", mission="m2"),
        values=values,
    )
    assert _units(running)["b"]["earliestStart"] == "2026-11-01T12:01:00Z"
    # everything done: no path, and the ETA is now.
    done = _fixed(
        policy,
        _unit("a", mission="m1", status="completed"),
        _unit("b", mission="m1", depends_on=["a"], status="completed"),
        _unit("c", mission="m2", status="completed"),
        values=values,
    )
    assert _project(done)["criticalPath"] == []
    assert _project(done)["etaRange"]["p95"] == NOW


# -- blocking reasons ------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "value", "reason"),
    [
        (
            "orchestration-error",
            "work unit u is dependency-blocked by: a, b",
            BlockingReason.DEPENDENCY_BLOCKED,
        ),
        ("orchestration-state", "approval-pending", BlockingReason.HUMAN_APPROVAL_REQUIRED),
        (
            "lease-error",
            "resource db is exclusively leased to u2 until 99",
            BlockingReason.EXCLUSIVE_RESOURCE_LOCKED,
        ),
        (
            "work-graph-error",
            "units a and b have overlapping write scopes and no explicit shared",
            BlockingReason.CONFLICTING_CHANGE_SCOPE,
        ),
        ("executor-runtime", "quota-exhausted", BlockingReason.SUBSCRIPTION_QUOTA_EXHAUSTED),
        ("executor-runtime", "capacity-exhausted", BlockingReason.PROVIDER_CAPACITY_EXHAUSTED),
        ("capacity-wait", "resource", BlockingReason.RUNNER_CAPACITY_EXHAUSTED),
        ("capacity-wait", "dependency", BlockingReason.DEPENDENCY_BLOCKED),
        ("verification", "failed", BlockingReason.TEST_OR_VERIFICATION_FAILED),
        ("executor-runtime", "auth-exhausted", BlockingReason.UNKNOWN),
        ("capacity-wait", "unclassified", BlockingReason.UNKNOWN),
        ("weather", "stormy", BlockingReason.UNKNOWN),
    ],
)
def test_signals_map_deterministically(source: str, value: str, reason: BlockingReason) -> None:
    assert classify_signal(source, value) is reason


def test_messages_match_what_the_producing_modules_actually_raise(policy) -> None:
    from agentic_sdlc.orchestration import OrchestrationError, Orchestrator, WorkUnitState
    from agentic_sdlc.work_graph import LeaseManager, WorkGraphError

    leases = LeaseManager()
    leases.acquire("db", "u1", now=0)
    with pytest.raises(WorkGraphError) as lease_error:
        leases.acquire("db", "u2", now=1)
    assert classify_signal("lease-error", str(lease_error.value)) is (
        BlockingReason.EXCLUSIVE_RESOURCE_LOCKED
    )

    orchestrator = Orchestrator()
    orchestrator.create_unit("dep", event_key="k0", timestamp=NOW)
    orchestrator.create_unit("u", event_key="k1", timestamp=NOW, depends_on=("dep",))
    for index, state in enumerate(
        (
            WorkUnitState.TRIAGED,
            WorkUnitState.SPECIFIED,
            WorkUnitState.PLANNED,
            WorkUnitState.APPROVED,
        )
    ):
        orchestrator.transition(
            "u", state, actor="ops", actor_kind="human", event_key=f"k{index + 2}", timestamp=NOW
        )
    with pytest.raises(OrchestrationError) as dispatch_error:
        orchestrator.transition(
            "u",
            WorkUnitState.DISPATCHED,
            actor="ops",
            actor_kind="system",
            event_key="k9",
            timestamp=NOW,
        )
    assert classify_signal("orchestration-error", str(dispatch_error.value)) is (
        BlockingReason.DEPENDENCY_BLOCKED
    )

    with pytest.raises(ForecastError) as overlap_error:
        _forecast(
            policy,
            _document(
                {**_unit("a"), "writePaths": ["src/shared/**"]},
                {**_unit("b"), "writePaths": ["src/shared/**"]},
            ),
            [],
        )
    assert classify_signal("work-graph-error", str(overlap_error.value)) is (
        BlockingReason.CONFLICTING_CHANGE_SCOPE
    )


def test_units_carry_typed_reasons_and_unknown_is_never_runnable(policy) -> None:
    result = _forecast(
        policy,
        _document(
            _unit("done", status="completed"),
            _unit("ready"),
            _unit("waits", depends_on=["ready"]),
            _unit(
                "approval", signals=[{"source": "orchestration-state", "value": "approval-pending"}]
            ),
            _unit("mystery", signals=[{"source": "weather", "value": "stormy"}]),
            _unit("broken", status="failed"),
            _unit(
                "quota",
                status="running",
                startedAt=NOW,
                signals=[{"source": "executor-runtime", "value": "quota-exhausted"}],
            ),
        ),
        [],
    )
    units = _units(result)

    assert units["done"]["blockingReason"] is None
    assert units["ready"]["runnable"] is True
    assert units["waits"]["blockingReason"] == "dependency_blocked"
    assert units["approval"]["blockingReason"] == "human_approval_required"
    assert units["mystery"]["blockingReason"] == "unknown"
    assert units["mystery"]["runnable"] is False
    assert units["broken"]["blockingReason"] == "unknown"
    assert units["quota"]["blockingReason"] == "subscription_quota_exhausted"
    assert units["quota"]["runnable"] is False
    assert _project(result)["blockers"] == {
        "dependency_blocked": ["waits"],
        "human_approval_required": ["approval"],
        "subscription_quota_exhausted": ["quota"],
        "unknown": ["broken", "mystery"],
    }
    assert (_project(result)["active"], _project(result)["queued"]) == (1, 5)


def test_multiple_reasons_report_the_highest_precedence_first(policy) -> None:
    result = _forecast(
        policy,
        _document(
            _unit("first", status="failed"),
            _unit(
                "u",
                depends_on=["first"],
                signals=[
                    {"source": "verification", "value": "failed"},
                    {"source": "orchestration-state", "value": "approval-pending"},
                ],
            ),
        ),
        [],
    )
    unit = _units(result)["u"]

    assert unit["blockingReason"] == "dependency_blocked"
    assert unit["blockingReasons"] == [
        "dependency_blocked",
        "human_approval_required",
        "test_or_verification_failed",
    ]


def test_parallelism_group_is_the_concurrency_group_or_null(policy) -> None:
    result = _forecast(policy, _document(_unit("a", concurrencyGroup="repo:alpha"), _unit("b")), [])

    assert _units(result)["a"]["parallelismGroup"] == "repo:alpha"
    assert _units(result)["b"]["parallelismGroup"] is None


# -- isolation, determinism, validation ------------------------------------


def test_projects_never_share_history(policy) -> None:
    document = {
        "schemaVersion": 1,
        "graphs": [
            {"projectId": "alpha", "units": [_unit("a")]},
            {"projectId": "beta", "units": [_unit("a")]},
        ],
    }
    records = _history("alpha", "worker", [100] * 5) + _history(
        "beta", "worker", [900] * 5, offset=50
    )

    result = _forecast(policy, document, records)

    assert _units(result, "alpha")["a"]["expectedDuration"]["p50"] == 100
    assert _units(result, "beta")["a"]["expectedDuration"]["p50"] == 900
    only_alpha = _forecast(policy, document, records, project_ids=["alpha"])
    assert [project["projectId"] for project in only_alpha["projects"]] == ["alpha"]


def test_forecast_is_deterministic(policy) -> None:
    document = _document(_unit("a"), _unit("b", depends_on=["a"]), _unit("c"))
    records = _history("alpha", "worker", [100, 300, 200, 500, 400])

    first = json.dumps(_forecast(policy, document, records), sort_keys=True)
    second = json.dumps(_forecast(policy, document, list(reversed(records))), sort_keys=True)

    assert first == second


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"schemaVersion": 2, "graphs": []}, "schemaVersion"),
        ({"schemaVersion": 1, "graphs": []}, "graphs array"),
        (_document({**_unit("a"), "colour": "red"}), "unknown keys"),
        (_document(_unit("a", status="sleeping")), "status must be one of"),
        (_document(_unit("a", startedAt="noon")), "RFC 3339"),
        (_document(_unit("a", signals=[{"source": "x"}])), "exactly source and value"),
        (_document({**_unit("a"), "dependsOn": "b"}), "array of strings"),
        (_document({**_unit("a"), "timeoutSeconds": "1h"}), "must be an integer"),
    ],
)
def test_malformed_documents_fail_closed(document: dict, message: str) -> None:
    with pytest.raises(ForecastError, match=message):
        load_forecast_document(document)


def test_invalid_graphs_fail_closed(policy) -> None:
    with pytest.raises(ForecastError, match="cycle"):
        _forecast(policy, _document(_unit("a", depends_on=["b"]), _unit("b", depends_on=["a"])), [])
    with pytest.raises(ForecastError, match="forbidden path"):
        _forecast(policy, _document({**_unit("a"), "writePaths": [".github/workflows/x.yml"]}), [])
    with pytest.raises(ForecastError, match="no graph for project"):
        _forecast(policy, _document(_unit("a")), [], project_ids=["beta"])
    with pytest.raises(ForecastError, match="UTC offset"):
        build_schedule_forecast(
            load_forecast_document(_document(_unit("a"))),
            [],
            policy=policy,
            now="2026-11-01T12:00:00",
        )
