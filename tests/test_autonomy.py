from __future__ import annotations

from dataclasses import replace

import pytest

from agentic_sdlc.autonomy import (
    AutonomyGateEvidence,
    AutonomyPhase,
    evaluate_autonomy,
)


def _implementation_ready() -> AutonomyGateEvidence:
    return AutonomyGateEvidence(
        forge_managed=True,
        scope_bounded=True,
        security_clear=True,
        claude_oauth_available=True,
        high_risk=False,
        unresolved_conversations=0,
    )


def _merge_ready() -> AutonomyGateEvidence:
    return replace(
        _implementation_ready(),
        deterministic_verification_passed=True,
        quality_passed=True,
        independent_review_approved=True,
        secret_scan_passed=True,
        branch_protection_allows=True,
    )


def test_implementation_allows_only_explicit_low_risk_managed_work_with_oauth() -> None:
    decision = evaluate_autonomy(AutonomyPhase.IMPLEMENTATION, _implementation_ready())

    assert decision.allowed is True
    assert decision.reasons == ()


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("forge_managed", "not explicitly forge-managed"),
        ("scope_bounded", "scope is not bounded"),
        ("security_clear", "security gate is not clear"),
        ("claude_oauth_available", "Claude Max OAuth is unavailable"),
    ],
)
def test_implementation_fails_closed_when_required_evidence_is_missing(
    field: str, reason: str
) -> None:
    decision = evaluate_autonomy(
        AutonomyPhase.IMPLEMENTATION,
        replace(_implementation_ready(), **{field: False}),
    )

    assert decision.allowed is False
    assert any(reason in item for item in decision.reasons)


def test_high_risk_work_never_auto_advances() -> None:
    decision = evaluate_autonomy(
        AutonomyPhase.IMPLEMENTATION,
        replace(_implementation_ready(), high_risk=True),
    )

    assert decision.allowed is False
    assert "high-risk work requires human policy review" in decision.reasons


@pytest.mark.parametrize("field", ["deployment_requested", "broker_access_requested"])
def test_runtime_authority_boundaries_deny_sensitive_side_effects(field: str) -> None:
    decision = evaluate_autonomy(
        AutonomyPhase.IMPLEMENTATION,
        replace(_implementation_ready(), **{field: True}),
    )

    assert decision.allowed is False


def test_merge_requires_every_automated_and_repository_gate() -> None:
    decision = evaluate_autonomy(AutonomyPhase.MERGE, _merge_ready())

    assert decision.allowed is True
    assert decision.as_dict() == {"phase": "merge", "allowed": True, "reasons": []}


@pytest.mark.parametrize(
    "field",
    [
        "deterministic_verification_passed",
        "quality_passed",
        "independent_review_approved",
        "secret_scan_passed",
        "branch_protection_allows",
    ],
)
def test_merge_fails_closed_if_any_required_gate_is_not_positive(field: str) -> None:
    decision = evaluate_autonomy(
        AutonomyPhase.MERGE,
        replace(_merge_ready(), **{field: False}),
    )

    assert decision.allowed is False
    assert decision.reasons


def test_merge_requires_all_review_conversations_resolved() -> None:
    decision = evaluate_autonomy(
        AutonomyPhase.MERGE,
        replace(_merge_ready(), unresolved_conversations=2),
    )

    assert decision.allowed is False
    assert "review conversations remain unresolved" in decision.reasons


def test_default_evidence_denies_both_autonomous_boundaries() -> None:
    evidence = AutonomyGateEvidence()

    assert evaluate_autonomy(AutonomyPhase.IMPLEMENTATION, evidence).allowed is False
    assert evaluate_autonomy(AutonomyPhase.MERGE, evidence).allowed is False


def test_unknown_phase_is_rejected_instead_of_falling_back() -> None:
    with pytest.raises(ValueError):
        evaluate_autonomy("deploy", _merge_ready())


def test_policy_contract_contains_no_paid_api_key_fallback_names() -> None:
    source = (__import__("pathlib").Path(__file__).parents[1] / "src/agentic_sdlc/autonomy.py").read_text()

    forbidden = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "browser cookie", "session scraping")
    for token in forbidden:
        assert token not in source
