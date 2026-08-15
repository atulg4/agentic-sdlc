from __future__ import annotations

from pathlib import Path

import pytest

from agentic_sdlc.adaptive_scheduler import fill_safe_slots
from agentic_sdlc.policy import load_policy
from agentic_sdlc.work_graph import GraphExecutor, WorkUnitSpec, validate_graph

BASE = "c" * 40


def _unit(unit_id: str, **overrides) -> WorkUnitSpec:
    values = {
        "unit_id": unit_id,
        "mission_id": "implementation-worker",
        "mission_version": "1.0.0",
        "output_artifact": "patch",
        "write_paths": (f"src/{unit_id}/**",),
        "owned_paths": (f"src/{unit_id}",),
        "provider": "claude",
        "max_tokens": 100,
    }
    values.update(overrides)
    return WorkUnitSpec(**values)


@pytest.fixture
def policy(policy_file: Path):
    return load_policy(policy_file)


def test_independent_ready_work_fills_all_safe_slots(policy) -> None:
    graph = validate_graph((_unit("alpha"), _unit("beta"), _unit("gamma")), policy)
    executor = GraphExecutor(graph, base_sha=BASE, max_parallel=3)

    decisions = fill_safe_slots(executor, now=1, agent_for=lambda unit_id: f"agent-{unit_id}")

    assert [item.status for item in decisions] == ["started", "started", "started"]
    assert {unit_id for unit_id in graph.order if executor.status(unit_id) == "running"} == {
        "alpha",
        "beta",
        "gamma",
    }


def test_provider_saturation_does_not_idle_other_provider_capacity(policy) -> None:
    graph = validate_graph(
        (
            _unit("claude-a", provider="claude"),
            _unit("claude-b", provider="claude"),
            _unit("local-c", provider="local"),
        ),
        policy,
    )
    executor = GraphExecutor(
        graph,
        base_sha=BASE,
        max_parallel=3,
        provider_limits={"claude": 1, "local": 2},
    )

    decisions = fill_safe_slots(executor, now=1, agent_for={name: "worker" for name in graph.order})

    assert executor.status("claude-a") == "running"
    assert executor.status("claude-b") == "pending"
    assert executor.status("local-c") == "running"
    assert [(item.unit_id, item.status) for item in decisions] == [
        ("claude-a", "started"),
        ("claude-b", "waiting"),
        ("local-c", "started"),
    ]


def test_global_parallel_limit_stops_scan_once_factory_is_full(policy) -> None:
    graph = validate_graph((_unit("alpha"), _unit("beta"), _unit("gamma")), policy)
    executor = GraphExecutor(graph, base_sha=BASE, max_parallel=2)

    decisions = fill_safe_slots(executor, now=1, agent_for=lambda _: "worker")

    assert [(item.unit_id, item.status) for item in decisions] == [
        ("alpha", "started"),
        ("beta", "started"),
        ("gamma", "waiting"),
    ]
    assert executor.status("gamma") == "pending"


def test_token_backpressure_skips_large_unit_and_starts_smaller_ready_work(policy) -> None:
    graph = validate_graph(
        (
            _unit("large", max_tokens=900),
            _unit("small", max_tokens=100),
        ),
        policy,
    )
    executor = GraphExecutor(graph, base_sha=BASE, max_parallel=2, total_token_budget=500)

    decisions = fill_safe_slots(executor, now=1, agent_for=lambda _: "worker")

    assert executor.status("large") == "pending"
    assert executor.status("small") == "running"
    assert [(item.unit_id, item.status) for item in decisions] == [
        ("large", "waiting"),
        ("small", "started"),
    ]


def test_missing_agent_identity_fails_closed(policy) -> None:
    graph = validate_graph((_unit("alpha"),), policy)
    executor = GraphExecutor(graph, base_sha=BASE)

    with pytest.raises(ValueError, match="missing agent id"):
        fill_safe_slots(executor, now=1, agent_for={})
