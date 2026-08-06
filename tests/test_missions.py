from __future__ import annotations

from pathlib import Path

import pytest

from agentic_sdlc.missions import (
    KNOWN_CAPABILITIES,
    PLATFORM_MISSIONS,
    AgentProfile,
    MissionError,
    MissionLedger,
    create_dispatch_envelope,
    load_agents,
    load_registry,
    validate_output,
)
from agentic_sdlc.models import RiskLevel
from agentic_sdlc.policy import evaluate_diff, load_policy


def _agent(agent_id: str, capabilities: tuple[str, ...], **overrides) -> AgentProfile:
    values = {
        "agent_id": agent_id,
        "adapter": "codex",
        "adapter_version": "1.2.3",
        "provider": "openai",
        "model": "gpt-5",
        "capabilities": capabilities,
        "available": True,
        "max_risk": RiskLevel.HIGH,
    }
    values.update(overrides)
    return AgentProfile(**values)


IMPLEMENTER_CAPS = ("edit-code", "author-tests", "run-commands")
REVIEWER_CAPS = ("review-code", "review-security", "review-architecture")


def _write_missions(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "missions.toml"
    path.write_text(body.strip() + "\n", encoding="utf-8")
    return path


def test_platform_registry_loads_without_consumer_file(policy_file: Path) -> None:
    registry = load_registry(None, load_policy(policy_file))
    assert set(registry.missions) == {mission.mission_id for mission in PLATFORM_MISSIONS}
    document = registry.as_dict()
    assert document["schemaVersion"] == 1
    assert "implementation-worker" in document["missions"]


def test_unknown_mission_fails_closed(policy_file: Path) -> None:
    registry = load_registry(None, load_policy(policy_file))
    with pytest.raises(MissionError, match="unknown mission"):
        registry.get("does-not-exist")


def test_malformed_mission_fails_closed(policy_file: Path, tmp_path: Path) -> None:
    missions = _write_missions(
        tmp_path,
        """
version = 1

[[mission]]
id = "custom-checker"
version = "1.0.0"
purpose = "Check things"
success_criteria = ["Checks pass"]
capabilities = ["run-commands"]
output_artifacts = ["gate-report"]
surprise_key = true
""",
    )
    with pytest.raises(MissionError, match="unknown keys: surprise_key"):
        load_registry(missions, load_policy(policy_file))


def test_unknown_capability_fails_closed(policy_file: Path, tmp_path: Path) -> None:
    missions = _write_missions(
        tmp_path,
        """
version = 1

[[mission]]
id = "shell-runner"
version = "1.0.0"
purpose = "Run anything"
success_criteria = ["Ran"]
capabilities = ["arbitrary-shell"]
output_artifacts = ["gate-report"]
""",
    )
    with pytest.raises(MissionError, match="unknown capabilities: arbitrary-shell"):
        load_registry(missions, load_policy(policy_file))


def test_mission_requesting_forbidden_paths_is_blocked(policy_file: Path, tmp_path: Path) -> None:
    missions = _write_missions(
        tmp_path,
        """
version = 1

[[mission]]
id = "workflow-editor"
version = "1.0.0"
purpose = "Edit workflows"
success_criteria = ["Edited"]
capabilities = ["edit-code"]
output_artifacts = ["patch"]
write_paths = [".github/workflows/deploy.yml"]
""",
    )
    with pytest.raises(MissionError, match="forbidden path"):
        load_registry(missions, load_policy(policy_file))


def test_mission_requesting_broad_write_scope_is_blocked(policy_file: Path, tmp_path: Path) -> None:
    missions = _write_missions(
        tmp_path,
        """
version = 1

[[mission]]
id = "everything-editor"
version = "1.0.0"
purpose = "Edit everything"
success_criteria = ["Edited"]
capabilities = ["edit-code"]
output_artifacts = ["patch"]
write_paths = ["**"]
""",
    )
    with pytest.raises(MissionError, match="write scope is too broad"):
        load_registry(missions, load_policy(policy_file))


def test_mission_requesting_denied_capability_is_blocked(policy_file: Path, tmp_path: Path) -> None:
    missions = _write_missions(
        tmp_path,
        """
version = 1

[registry]
allowed_capabilities = ["specify", "plan", "review-code"]

[[mission]]
id = "custom-implementer"
version = "1.0.0"
purpose = "Implement features"
success_criteria = ["Implemented"]
capabilities = ["edit-code"]
output_artifacts = ["patch"]
write_paths = ["src/**"]
""",
    )
    with pytest.raises(MissionError, match="unavailable under repository policy"):
        load_registry(missions, load_policy(policy_file))


def test_capability_narrowing_prunes_platform_missions(policy_file: Path, tmp_path: Path) -> None:
    missions = _write_missions(
        tmp_path,
        """
version = 1

[registry]
allowed_capabilities = ["specify", "plan", "review-code"]
""",
    )
    registry = load_registry(missions, load_policy(policy_file))
    assert "specification-planner" in registry.missions
    assert "implementation-worker" not in registry.missions


def test_consumer_missions_extend_platform_rather_than_replace(
    policy_file: Path, tmp_path: Path
) -> None:
    missions = _write_missions(
        tmp_path,
        """
version = 1

[[mission]]
id = "implementation-worker"
version = "9.9.9"
purpose = "Replace the platform worker with a permissive one"
success_criteria = ["Replaced"]
capabilities = ["edit-code"]
output_artifacts = ["patch"]
write_paths = ["src/**"]
""",
    )
    with pytest.raises(MissionError, match="cannot replace platform missions"):
        load_registry(missions, load_policy(policy_file))


def test_consumer_mission_extends_with_platform_safety_intact(
    policy_file: Path, tmp_path: Path
) -> None:
    missions = _write_missions(
        tmp_path,
        """
version = 1

[[mission]]
id = "strategy-quant-reviewer"
version = "1.0.0"
purpose = "Review trading-strategy math"
success_criteria = ["Math reviewed"]
capabilities = ["review-quantitative"]
output_artifacts = ["review-report"]
independent_of = ["implementation-worker", "repair-agent"]
""",
    )
    policy = load_policy(policy_file)
    registry = load_registry(missions, policy)
    mission = registry.get("strategy-quant-reviewer")
    assert mission.origin == "consumer"
    # Platform baseline still applies: the mission config file itself stays
    # off-limits to diffs regardless of what missions exist.
    decision = evaluate_diff(("missions.toml",), 1, 0, policy)
    assert not decision.allowed


def test_reviewer_cannot_be_the_implementer_when_independence_required(
    policy_file: Path,
) -> None:
    registry = load_registry(None, load_policy(policy_file))
    shared = _agent("codex-1", IMPLEMENTER_CAPS + REVIEWER_CAPS)
    with pytest.raises(MissionError, match="independence conflict"):
        registry.select_agent(
            "code-reviewer",
            (shared,),
            history={"implementation-worker": "codex-1"},
        )


def test_review_missions_prohibit_self_approval_even_without_declared_independence(
    policy_file: Path, tmp_path: Path
) -> None:
    missions = _write_missions(
        tmp_path,
        """
version = 1

[[mission]]
id = "casual-reviewer"
version = "1.0.0"
purpose = "Review changes"
success_criteria = ["Reviewed"]
capabilities = ["review-code"]
output_artifacts = ["review-report"]
""",
    )
    registry = load_registry(missions, load_policy(policy_file))
    shared = _agent("claude-1", IMPLEMENTER_CAPS + REVIEWER_CAPS)
    other = _agent("codex-2", REVIEWER_CAPS)
    with pytest.raises(MissionError, match="independence conflict"):
        registry.select_agent(
            "casual-reviewer",
            (shared,),
            history={"implementation-worker": "claude-1"},
        )
    selected = registry.select_agent(
        "casual-reviewer",
        (shared, other),
        history={"implementation-worker": "claude-1"},
    )
    assert selected.agent_id == "codex-2"


def test_fallback_selection_preserves_capability_and_independence(
    policy_file: Path,
) -> None:
    registry = load_registry(None, load_policy(policy_file))
    unavailable = _agent("primary", REVIEWER_CAPS, available=False)
    tainted = _agent("implementer", REVIEWER_CAPS + IMPLEMENTER_CAPS)
    weak = _agent("planner-only", ("plan", "specify"))
    low_clearance = _agent("junior", REVIEWER_CAPS, max_risk=RiskLevel.LOW)
    eligible = _agent("fallback-reviewer", REVIEWER_CAPS)
    selected = registry.select_agent(
        "security-reviewer",
        (unavailable, tainted, weak, low_clearance, eligible),
        history={"implementation-worker": "implementer"},
    )
    assert selected.agent_id == "fallback-reviewer"


def test_dispatch_fails_closed_when_no_agent_satisfies_contract(
    policy_file: Path,
) -> None:
    registry = load_registry(None, load_policy(policy_file))
    with pytest.raises(MissionError, match="no eligible agent"):
        registry.select_agent(
            "security-reviewer",
            (_agent("planner", ("plan",)),),
        )


def test_identical_inputs_produce_reproducible_dispatch_envelope(
    policy_file: Path,
) -> None:
    registry = load_registry(None, load_policy(policy_file))
    mission = registry.get("implementation-worker")
    agent = _agent("codex-1", IMPLEMENTER_CAPS)
    first = create_dispatch_envelope(
        mission,
        agent,
        work_ref="example/project#42",
        prompt="Implement the approved plan.",
        input_refs=("task-spec", "plan-comment"),
    )
    second = create_dispatch_envelope(
        mission,
        agent,
        work_ref="example/project#42",
        prompt="Implement the approved plan.",
        input_refs=("plan-comment", "task-spec"),
    )
    assert first == second
    assert first["missionVersion"] == "1.0.0"
    assert first["adapterVersion"] == "1.2.3"
    assert first["model"] == "gpt-5"
    assert len(first["promptSha256"]) == 64
    assert len(first["envelopeSha256"]) == 64

    changed = create_dispatch_envelope(
        mission,
        agent,
        work_ref="example/project#42",
        prompt="Implement the approved plan!",
        input_refs=("task-spec", "plan-comment"),
    )
    assert changed["envelopeSha256"] != first["envelopeSha256"]


def test_concurrency_limit_is_enforced(policy_file: Path) -> None:
    registry = load_registry(None, load_policy(policy_file))
    ledger = MissionLedger(registry)
    ledger.begin_run("implementation-worker", "issue-1")
    with pytest.raises(MissionError, match="concurrency limit"):
        ledger.begin_run("implementation-worker", "issue-2")
    ledger.finish_run("implementation-worker", tokens_used=1000)
    ledger.begin_run("implementation-worker", "issue-2")


def test_token_budget_is_enforced(policy_file: Path) -> None:
    registry = load_registry(None, load_policy(policy_file))
    mission = registry.get("implementation-worker")
    ledger = MissionLedger(registry)
    ledger.begin_run("implementation-worker", "issue-1")
    ledger.finish_run("implementation-worker", tokens_used=mission.max_tokens)
    with pytest.raises(MissionError, match="token budget exhausted"):
        ledger.begin_run("implementation-worker", "issue-2")


def test_retry_limit_is_enforced_per_work_reference(policy_file: Path) -> None:
    registry = load_registry(None, load_policy(policy_file))
    ledger = MissionLedger(registry)
    for _ in range(2):  # initial attempt + one retry (max_retries = 1)
        ledger.begin_run("implementation-worker", "issue-1")
        ledger.finish_run("implementation-worker")
    with pytest.raises(MissionError, match="retry limit exceeded"):
        ledger.begin_run("implementation-worker", "issue-1")
    # Unrelated work is unaffected.
    ledger.begin_run("implementation-worker", "issue-2")


def test_ledger_blocks_selection_when_saturated(policy_file: Path) -> None:
    registry = load_registry(None, load_policy(policy_file))
    ledger = MissionLedger(registry)
    ledger.begin_run("implementation-worker", "issue-1")
    with pytest.raises(MissionError, match="concurrency limit"):
        registry.select_agent(
            "implementation-worker",
            (_agent("codex-1", IMPLEMENTER_CAPS),),
            ledger=ledger,
        )


def test_output_validation_before_downstream_consumption(policy_file: Path) -> None:
    registry = load_registry(None, load_policy(policy_file))
    mission = registry.get("implementation-worker")
    validate_output(mission, {"patch": "sha256:abc", "patch-manifest": "sha256:def"})
    with pytest.raises(MissionError, match="missing declared outputs: patch-manifest"):
        validate_output(mission, {"patch": "sha256:abc"})
    with pytest.raises(MissionError, match="undeclared outputs: deploy-log"):
        validate_output(
            mission,
            {"patch": "a", "patch-manifest": "b", "deploy-log": "c"},
        )
    with pytest.raises(MissionError, match="empty outputs: patch"):
        validate_output(mission, {"patch": "  ", "patch-manifest": "b"})


def test_review_and_write_capabilities_cannot_mix(policy_file: Path, tmp_path: Path) -> None:
    missions = _write_missions(
        tmp_path,
        """
version = 1

[[mission]]
id = "judge-and-fix"
version = "1.0.0"
purpose = "Review and fix in one pass"
success_criteria = ["Done"]
capabilities = ["review-code", "edit-code"]
output_artifacts = ["patch"]
write_paths = ["src/**"]
""",
    )
    with pytest.raises(MissionError, match="cannot both review and write"):
        load_registry(missions, load_policy(policy_file))


def test_agent_roster_parsing_fails_closed(policy_file: Path) -> None:
    with pytest.raises(MissionError, match="non-empty JSON array"):
        load_agents([])
    with pytest.raises(MissionError, match="unknown keys"):
        load_agents(
            [
                {
                    "agentId": "codex-1",
                    "adapter": "codex",
                    "adapterVersion": "1.0.0",
                    "provider": "openai",
                    "model": "gpt-5",
                    "capabilities": ["plan"],
                    "sudo": True,
                }
            ]
        )
    with pytest.raises(MissionError, match="unknown capabilities"):
        load_agents(
            [
                {
                    "agentId": "codex-1",
                    "adapter": "codex",
                    "adapterVersion": "1.0.0",
                    "provider": "openai",
                    "model": "gpt-5",
                    "capabilities": ["root-shell"],
                }
            ]
        )


def test_platform_capability_universe_is_closed(policy_file: Path) -> None:
    for mission in PLATFORM_MISSIONS:
        assert set(mission.capabilities) <= KNOWN_CAPABILITIES
