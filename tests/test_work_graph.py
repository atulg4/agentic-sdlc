from __future__ import annotations

from pathlib import Path

import pytest

from agentic_sdlc.policy import load_policy
from agentic_sdlc.work_graph import (
    GraphExecutor,
    LeaseManager,
    WorkGraphError,
    WorkUnitSpec,
    validate_graph,
)

BASE = "c" * 40
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "d" * 64


def _unit(unit_id: str, **overrides) -> WorkUnitSpec:
    values = {
        "unit_id": unit_id,
        "mission_id": "implementation-worker",
        "mission_version": "1.0.0",
        "output_artifact": "patch",
        "write_paths": (f"src/{unit_id}/**",),
        "owned_paths": (f"src/{unit_id}",),
    }
    values.update(overrides)
    return WorkUnitSpec(**values)


@pytest.fixture
def policy(policy_file: Path):
    return load_policy(policy_file)


def _parallel_graph(policy):
    return validate_graph(
        (
            _unit("backend"),
            _unit(
                "docs",
                mission_id="documentation-agent",
                write_paths=("docs/**",),
                owned_paths=("docs",),
            ),
            _unit(
                "review",
                mission_id="code-reviewer",
                output_artifact="review-report",
                write_paths=(),
                owned_paths=(),
                depends_on=("backend",),
            ),
        ),
        policy,
    )


def test_independent_units_run_in_parallel_and_compose(policy) -> None:
    graph = _parallel_graph(policy)
    executor = GraphExecutor(graph, base_sha=BASE, max_parallel=4)
    assert set(executor.ready()) == {"backend", "docs"}

    ws_backend = executor.start("backend", agent_id="codex-1", now=0)
    ws_docs = executor.start("docs", agent_id="claude-1", now=0)
    assert ws_backend != ws_docs  # isolated workspaces, never shared

    executor.complete(
        "backend", artifact_digest=DIGEST_A, verification_passed=True, tokens_used=1000, now=10
    )
    executor.complete(
        "docs", artifact_digest=DIGEST_B, verification_passed=True, tokens_used=500, now=10
    )
    # The read-only review unit becomes ready only after its dependency.
    assert executor.ready() == ("review",)
    executor.start("review", agent_id="claude-1", now=11)
    executor.complete(
        "review", artifact_digest=DIGEST_C, verification_passed=True, tokens_used=200, now=20
    )
    executor.record_final_verification(passed=True)
    report = executor.compose()
    assert report["composedTreeVerified"] is True
    assert report["baseSha"] == BASE
    statuses = {item["unitId"]: item["status"] for item in report["units"]}
    assert statuses == {"backend": "completed", "docs": "completed", "review": "completed"}
    digests = {item["unitId"]: item["artifactDigest"] for item in report["units"]}
    assert digests["backend"] == DIGEST_A


def test_overlapping_write_sets_fail_closed_unless_ordered_or_strategized(policy) -> None:
    overlapping = (
        _unit("alpha", write_paths=("src/shared/**",), owned_paths=()),
        _unit("beta", write_paths=("src/shared/**",), owned_paths=()),
    )
    with pytest.raises(WorkGraphError, match="overlapping write"):
        validate_graph(overlapping, policy)

    # Serializing with a dependency makes the same write sets legal.
    ordered = (
        _unit("alpha", write_paths=("src/shared/**",), owned_paths=()),
        _unit("beta", write_paths=("src/shared/**",), owned_paths=(), depends_on=("alpha",)),
    )
    graph = validate_graph(ordered, policy)
    assert graph.order == ("alpha", "beta")

    # An explicit shared composition strategy is also accepted.
    strategized = (
        _unit(
            "alpha", write_paths=("src/shared/**",), owned_paths=(), composition="three-way-append"
        ),
        _unit(
            "beta", write_paths=("src/shared/**",), owned_paths=(), composition="three-way-append"
        ),
    )
    validate_graph(strategized, policy)


def test_cycles_missing_dependencies_and_ambiguous_ownership_are_rejected(policy) -> None:
    with pytest.raises(WorkGraphError, match="cycle"):
        validate_graph(
            (
                _unit("a", depends_on=("b",)),
                _unit("b", depends_on=("a",)),
            ),
            policy,
        )
    with pytest.raises(WorkGraphError, match="missing dependencies: ghost"):
        validate_graph((_unit("a", depends_on=("ghost",)),), policy)
    with pytest.raises(WorkGraphError, match="ambiguous ownership"):
        validate_graph(
            (
                _unit("a", owned_paths=("src/core",), write_paths=("src/a/**",)),
                _unit("b", owned_paths=("src/core",), write_paths=("src/b/**",)),
            ),
            policy,
        )
    with pytest.raises(WorkGraphError, match="cannot depend on itself"):
        validate_graph((_unit("a", depends_on=("a",)),), policy)


def test_forbidden_paths_are_globally_enforced(policy) -> None:
    with pytest.raises(WorkGraphError, match="forbidden path"):
        validate_graph(
            (_unit("sneaky", write_paths=(".github/workflows/deploy.yml",)),),
            policy,
        )


def test_shared_semantic_resources_detect_conflicts_paths_cannot_see(policy) -> None:
    # Two units touching disjoint files but the same API contract conflict.
    conflicting = (
        _unit(
            "api-server",
            write_paths=("src/server/**",),
            owned_paths=(),
            shared_resources=("api-contract",),
        ),
        _unit(
            "api-client",
            write_paths=("src/client/**",),
            owned_paths=(),
            shared_resources=("api-contract",),
        ),
    )
    with pytest.raises(WorkGraphError, match="share semantic resource.*api-contract"):
        validate_graph(conflicting, policy)
    # Ordering them resolves the conflict.
    ordered = (
        conflicting[0],
        WorkUnitSpec(
            unit_id="api-client",
            mission_id="implementation-worker",
            mission_version="1.0.0",
            output_artifact="patch",
            write_paths=("src/client/**",),
            shared_resources=("api-contract",),
            depends_on=("api-server",),
        ),
    )
    validate_graph(ordered, policy)


def test_upstream_artifact_change_invalidates_dependents(policy) -> None:
    graph = _parallel_graph(policy)
    executor = GraphExecutor(graph, base_sha=BASE)
    executor.start("backend", agent_id="codex-1", now=0)
    executor.complete(
        "backend", artifact_digest=DIGEST_A, verification_passed=True, tokens_used=100, now=1
    )
    executor.start("review", agent_id="claude-1", now=2)
    executor.complete(
        "review", artifact_digest=DIGEST_C, verification_passed=True, tokens_used=50, now=3
    )
    invalidated = executor.invalidate_upstream_change("backend", new_digest=DIGEST_B)
    assert invalidated == ("review",)
    assert executor.status("review") == "pending"
    # The stale review artifact is gone and composition is refused.
    with pytest.raises(WorkGraphError, match="composition"):
        executor.compose()
    # Unchanged digest is a no-op.
    assert executor.invalidate_upstream_change("backend", new_digest=DIGEST_B) == ()


def test_concurrent_agents_cannot_share_an_exclusive_lease() -> None:
    leases = LeaseManager()
    leases.acquire("migrations", "unit-a", now=0, ttl_seconds=100)
    with pytest.raises(WorkGraphError, match="exclusively leased to unit-a"):
        leases.acquire("migrations", "unit-b", now=50)
    # Expired leases are reclaimable, with the takeover audited.
    leases.acquire("migrations", "unit-b", now=150)
    assert leases.holder("migrations", now=151) == "unit-b"
    assert any("expired" in line for line in leases.audit())
    with pytest.raises(WorkGraphError, match="does not hold a lease"):
        leases.release("migrations", "unit-a", now=160)


def test_executor_leases_owned_paths_during_runs(policy) -> None:
    graph = validate_graph(
        (
            _unit("alpha", owned_paths=("src/core",), write_paths=("src/alpha/**",)),
            _unit("beta", owned_paths=(), write_paths=("src/beta/**",)),
        ),
        policy,
    )
    executor = GraphExecutor(graph, base_sha=BASE)
    executor.start("alpha", agent_id="codex-1", now=0)
    assert executor.leases.holder("src/core", now=1) == "alpha"
    # The lease is keyed by unit; another unit cannot claim it while running.
    with pytest.raises(WorkGraphError, match="exclusively leased"):
        executor.leases.acquire("src/core", "beta", now=1)
    executor.complete(
        "alpha", artifact_digest=DIGEST_A, verification_passed=True, tokens_used=10, now=2
    )
    executor.leases.acquire("src/core", "beta", now=3)


def test_failed_unit_prevents_unsafe_fan_in_but_preserves_evidence(policy) -> None:
    graph = _parallel_graph(policy)
    executor = GraphExecutor(graph, base_sha=BASE)
    executor.start("backend", agent_id="codex-1", now=0)
    executor.start("docs", agent_id="claude-1", now=0)
    executor.complete(
        "docs", artifact_digest=DIGEST_B, verification_passed=True, tokens_used=10, now=1
    )
    executor.fail("backend", now=2, reason="tests exploded")
    assert executor.status("review") == "blocked"  # downstream blocked
    assert executor.status("docs") == "completed"  # unrelated evidence intact
    with pytest.raises(WorkGraphError, match="attested evidence preserved for: docs"):
        executor.compose()


def test_unattested_artifacts_cannot_enter_composition(policy) -> None:
    graph = validate_graph((_unit("solo"),), policy)
    executor = GraphExecutor(graph, base_sha=BASE)
    executor.start("solo", agent_id="codex-1", now=0)
    # Completing with failed verification is not attestation.
    executor.complete(
        "solo", artifact_digest=DIGEST_A, verification_passed=False, tokens_used=10, now=1
    )
    with pytest.raises(WorkGraphError, match="failed units solo"):
        executor.compose()
    with pytest.raises(WorkGraphError, match="attested"):
        executor.record_final_verification(passed=True)


def test_final_verification_runs_against_the_composed_tree(policy) -> None:
    graph = validate_graph((_unit("solo"),), policy)
    executor = GraphExecutor(graph, base_sha=BASE)
    executor.start("solo", agent_id="codex-1", now=0)
    executor.complete(
        "solo", artifact_digest=DIGEST_A, verification_passed=True, tokens_used=10, now=1
    )
    # Per-unit verification alone is not enough.
    with pytest.raises(WorkGraphError, match="composed tree"):
        executor.compose()
    executor.record_final_verification(passed=False)
    with pytest.raises(WorkGraphError, match="composed tree"):
        executor.compose()
    executor.record_final_verification(passed=True)
    assert executor.compose()["composedTreeVerified"] is True


def test_max_parallelism_provider_limits_and_budget_are_enforced(policy) -> None:
    graph = validate_graph(
        (
            _unit("a", provider="openai"),
            _unit("b", provider="openai"),
            _unit("c", provider="anthropic", max_tokens=400_000),
        ),
        policy,
    )
    executor = GraphExecutor(
        graph,
        base_sha=BASE,
        max_parallel=2,
        total_token_budget=1_200_000,
        provider_limits={"openai": 1},
    )
    executor.start("a", agent_id="codex-1", now=0)
    with pytest.raises(WorkGraphError, match="provider openai concurrency limit 1"):
        executor.start("b", agent_id="codex-2", now=0)
    executor.start("c", agent_id="claude-1", now=0)
    with pytest.raises(WorkGraphError, match="maximum parallelism 2"):
        executor.start("b", agent_id="codex-2", now=0)
    executor.complete(
        "a", artifact_digest=DIGEST_A, verification_passed=True, tokens_used=800_000, now=1
    )
    # Budget: 800k spent + b's 500k cap would exceed 1.2M.
    with pytest.raises(WorkGraphError, match="exceed the total token budget"):
        executor.start("b", agent_id="codex-2", now=2)


def test_graph_serializes_to_machine_readable_document(policy) -> None:
    graph = _parallel_graph(policy)
    document = graph.as_dict()
    assert document["schemaVersion"] == 1
    assert document["order"][-1] == "review"
    assert document["units"]["review"]["readOnly"] is True
    assert document["units"]["backend"]["readOnly"] is False
