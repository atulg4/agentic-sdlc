from __future__ import annotations

from pathlib import Path

import pytest

from agentic_sdlc.models import RiskLevel
from agentic_sdlc.policy import evaluate_diff, evaluate_task, load_policy
from agentic_sdlc.task_spec import parse_task


def test_task_requires_eligibility_labels(policy_file: Path, valid_body: str) -> None:
    policy = load_policy(policy_file)
    decision = evaluate_task(parse_task("Task", valid_body), policy)
    assert not decision.allowed
    assert "agent-ready" in decision.reasons[0]


def test_eligible_task_still_requires_human_merge(policy_file: Path, valid_body: str) -> None:
    policy = load_policy(policy_file)
    task = parse_task("Task", valid_body, ("agent-ready", "human-review-required"))
    decision = evaluate_task(task, policy)
    assert decision.allowed
    assert "human-merge-approval" in decision.required_gates
    assert not decision.automatic_merge_allowed


def test_implementation_requires_separate_approval_label(
    policy_file: Path, valid_body: str
) -> None:
    policy = load_policy(policy_file)
    task = parse_task("Task", valid_body, ("agent-ready", "human-review-required"))
    decision = evaluate_task(task, policy, "implement")
    assert not decision.allowed
    assert "implementation-approved" in decision.reasons[0]


def test_forbidden_task_is_rejected(policy_file: Path, valid_body: str) -> None:
    policy = load_policy(policy_file)
    task = parse_task(
        "Place a live trade now",
        valid_body,
        ("agent-ready", "human-review-required"),
    )
    decision = evaluate_task(task, policy)
    assert not decision.allowed
    assert decision.risk == RiskLevel.CRITICAL


def test_forbidden_workflow_change_is_blocked(policy_file: Path) -> None:
    decision = evaluate_diff(
        ("src/app.py", ".github/workflows/release.yml"),
        10,
        2,
        load_policy(policy_file),
    )
    assert not decision.allowed
    assert decision.risk == RiskLevel.CRITICAL


def test_protected_change_requires_architect_review(policy_file: Path) -> None:
    decision = evaluate_diff(
        ("src/auth/session.py",),
        10,
        2,
        load_policy(policy_file),
    )
    assert decision.allowed
    assert decision.risk == RiskLevel.HIGH
    assert "architect-security-review" in decision.required_gates
    assert not decision.automatic_merge_allowed


def test_documentation_only_change_is_low_risk(policy_file: Path) -> None:
    decision = evaluate_diff(("docs/usage.md",), 12, 1, load_policy(policy_file))
    assert decision.allowed
    assert decision.risk == RiskLevel.LOW
    assert not decision.automatic_merge_allowed


def test_diff_caps_are_enforced(policy_file: Path) -> None:
    paths = ("src/a.py", "src/b.py", "src/c.py", "src/d.py")
    decision = evaluate_diff(paths, 101, 0, load_policy(policy_file))
    assert not decision.allowed
    assert len(decision.reasons) == 2


def test_patch_byte_cap_blocks_oversized_binary_change(policy_file: Path) -> None:
    decision = evaluate_diff(
        ("assets/model.bin",),
        0,
        0,
        load_policy(policy_file),
        patch_bytes=1001,
    )

    assert not decision.allowed
    assert decision.risk == RiskLevel.CRITICAL
    assert decision.reasons == ("patch-byte cap exceeded: 1001 > 1000",)


def test_diff_policy_rejects_negative_measurements(policy_file: Path) -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        evaluate_diff(("src/app.py",), -1, 0, load_policy(policy_file))


def test_baseline_policy_always_protects_its_own_configuration(policy_file: Path) -> None:
    decision = evaluate_diff(("agentic-sdlc.toml",), 1, 1, load_policy(policy_file))

    assert not decision.allowed
    assert decision.reasons == ("forbidden paths changed: agentic-sdlc.toml",)


@pytest.mark.parametrize(
    "path",
    (
        ".github/CODEOWNERS",
        "services/api/AGENTS.md",
        "services/api/.env.production",
        "root-private.key",
    ),
)
def test_baseline_policy_protects_nested_control_and_secret_paths(
    policy_file: Path,
    path: str,
) -> None:
    decision = evaluate_diff((path,), 1, 1, load_policy(policy_file))

    assert not decision.allowed


def _disable_human_merge(policy_file: Path) -> None:
    document = policy_file.read_text(encoding="utf-8").replace(
        "human_merge_required = true",
        "human_merge_required = false",
    )
    policy_file.write_text(document, encoding="utf-8")


def test_policy_version_one_accepts_explicit_autonomous_merge_mode(policy_file: Path) -> None:
    _disable_human_merge(policy_file)

    policy = load_policy(policy_file)
    assert policy.human_merge_required is False


def test_autonomous_mode_removes_only_the_human_merge_gate(
    policy_file: Path, valid_body: str
) -> None:
    _disable_human_merge(policy_file)
    policy = load_policy(policy_file)
    task = parse_task("Task", valid_body, ("agent-ready", "human-review-required"))

    decision = evaluate_task(task, policy)

    assert decision.allowed
    assert "deterministic-ci" in decision.required_gates
    assert "independent-agent-review" in decision.required_gates
    assert "human-merge-approval" not in decision.required_gates
    assert not decision.automatic_merge_allowed


def test_autonomous_mode_allows_only_unprotected_low_risk_diff_eligibility(
    policy_file: Path,
) -> None:
    _disable_human_merge(policy_file)
    policy = load_policy(policy_file)

    low_risk = evaluate_diff(("docs/usage.md",), 5, 1, policy)
    ordinary_code = evaluate_diff(("src/app.py",), 5, 1, policy)
    protected = evaluate_diff(("src/auth/session.py",), 5, 1, policy)
    forbidden = evaluate_diff(("agentic-sdlc.toml",), 1, 1, policy)

    assert low_risk.allowed
    assert low_risk.risk is RiskLevel.LOW
    assert low_risk.automatic_merge_allowed

    assert ordinary_code.allowed
    assert ordinary_code.risk is RiskLevel.MEDIUM
    assert not ordinary_code.automatic_merge_allowed

    assert protected.allowed
    assert protected.risk is RiskLevel.HIGH
    assert "architect-security-review" in protected.required_gates
    assert not protected.automatic_merge_allowed

    assert not forbidden.allowed
    assert forbidden.risk is RiskLevel.CRITICAL
    assert not forbidden.automatic_merge_allowed


def test_human_merge_policy_must_be_boolean(policy_file: Path) -> None:
    document = policy_file.read_text(encoding="utf-8").replace(
        "human_merge_required = true",
        'human_merge_required = "false"',
    )
    policy_file.write_text(document, encoding="utf-8")

    with pytest.raises(ValueError, match="human_merge_required must be a boolean"):
        load_policy(policy_file)
