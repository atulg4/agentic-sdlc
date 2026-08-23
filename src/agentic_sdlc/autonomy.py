"""Fail-closed policy decisions for Forge-managed autonomous lifecycle steps.

This module does not perform GitHub mutations. It converts trusted, normalized gate
evidence into an auditable decision that a separate credential-isolated publisher may
consume. Missing or negative evidence always denies autonomous advancement.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "AutonomyDecision",
    "AutonomyGateEvidence",
    "AutonomyPhase",
    "evaluate_autonomy",
]


class AutonomyPhase(StrEnum):
    """Lifecycle boundaries at which Forge may advance work without a human click."""

    IMPLEMENTATION = "implementation"
    MERGE = "merge"


@dataclass(frozen=True)
class AutonomyGateEvidence:
    """Trusted gate evidence; every field defaults to the safest denying value."""

    forge_managed: bool = False
    scope_bounded: bool = False
    security_clear: bool = False
    claude_oauth_available: bool = False
    high_risk: bool = True
    deterministic_verification_passed: bool = False
    quality_passed: bool = False
    independent_review_approved: bool = False
    secret_scan_passed: bool = False
    branch_protection_allows: bool = False
    unresolved_conversations: int = 1
    deployment_requested: bool = False
    broker_access_requested: bool = False


@dataclass(frozen=True)
class AutonomyDecision:
    """Auditable allow/deny result for one lifecycle boundary."""

    phase: AutonomyPhase
    allowed: bool
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase.value,
            "allowed": self.allowed,
            "reasons": list(self.reasons),
        }


def _common_denials(evidence: AutonomyGateEvidence) -> list[str]:
    reasons: list[str] = []
    if not evidence.forge_managed:
        reasons.append("work is not explicitly forge-managed")
    if not evidence.scope_bounded:
        reasons.append("scope is not bounded")
    if not evidence.security_clear:
        reasons.append("security gate is not clear")
    if evidence.high_risk:
        reasons.append("high-risk work requires human policy review")
    if evidence.deployment_requested:
        reasons.append("deployment is outside autonomous implementation authority")
    if evidence.broker_access_requested:
        reasons.append("broker access is outside autonomous implementation authority")
    return reasons


def evaluate_autonomy(
    phase: AutonomyPhase | str,
    evidence: AutonomyGateEvidence,
) -> AutonomyDecision:
    """Return a fail-closed autonomous advancement decision from trusted evidence."""

    target = AutonomyPhase(phase)
    reasons = _common_denials(evidence)

    if target is AutonomyPhase.IMPLEMENTATION:
        if not evidence.claude_oauth_available:
            reasons.append("Claude Max OAuth is unavailable")
    else:
        if not evidence.deterministic_verification_passed:
            reasons.append("deterministic verification has not passed")
        if not evidence.quality_passed:
            reasons.append("quality gates have not passed")
        if not evidence.independent_review_approved:
            reasons.append("independent review has not approved")
        if not evidence.secret_scan_passed:
            reasons.append("secret scanning has not passed")
        if not evidence.branch_protection_allows:
            reasons.append("branch protection does not allow merge")
        if evidence.unresolved_conversations != 0:
            reasons.append("review conversations remain unresolved")

    return AutonomyDecision(target, not reasons, tuple(reasons))
