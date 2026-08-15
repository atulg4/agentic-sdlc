"""Fail-closed normalization of merge-gate evidence.

This module converts a trusted GitHub evidence snapshot into the policy-neutral
:class:`AutonomyGateEvidence` consumed by the protected merge executor. It never
performs a merge and never accepts a caller-provided "approved" shortcut.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .autonomy import AutonomyGateEvidence

__all__ = ["MergeEvidencePolicy", "normalize_merge_evidence"]

_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class MergeEvidencePolicy:
    """Named required checks for one consumer repository."""

    deterministic_checks: tuple[str, ...]
    quality_checks: tuple[str, ...]
    independent_review_checks: tuple[str, ...]
    secret_scan_checks: tuple[str, ...]

    def __post_init__(self) -> None:
        groups = (
            self.deterministic_checks,
            self.quality_checks,
            self.independent_review_checks,
            self.secret_scan_checks,
        )
        if any(not group for group in groups):
            raise ValueError("every merge evidence check group must be non-empty")
        names = [name for group in groups for name in group]
        if any(not name.strip() for name in names):
            raise ValueError("required check names must be non-empty")
        if len(names) != len(set(names)):
            raise ValueError("required check names must be unique across groups")


def _latest_check_conclusions(
    checks: Sequence[Mapping[str, object]], expected_head_sha: str
) -> dict[str, str]:
    latest: dict[str, tuple[int, str]] = {}
    for check in checks:
        name = check.get("name")
        head_sha = check.get("head_sha")
        conclusion = check.get("conclusion")
        check_id = check.get("id")
        if not isinstance(name, str) or not name.strip():
            continue
        if head_sha != expected_head_sha:
            continue
        if not isinstance(check_id, int) or check_id <= 0:
            continue
        if not isinstance(conclusion, str):
            conclusion = ""
        current = latest.get(name)
        if current is None or check_id > current[0]:
            latest[name] = (check_id, conclusion)
    return {name: conclusion for name, (_, conclusion) in latest.items()}


def _all_success(names: Sequence[str], conclusions: Mapping[str, str]) -> bool:
    return all(conclusions.get(name) == "success" for name in names)


def normalize_merge_evidence(
    *,
    expected_head_sha: str,
    labels: Sequence[str],
    risk_level: str,
    scope_bounded: bool,
    security_clear: bool,
    branch_protection_allows: bool,
    unresolved_conversations: int,
    deployment_requested: bool,
    broker_access_requested: bool,
    checks: Sequence[Mapping[str, object]],
    policy: MergeEvidencePolicy,
) -> AutonomyGateEvidence:
    """Return merge evidence bound to one exact head SHA.

    Missing, stale, malformed, pending, cancelled, neutral, or failing required
    checks all remain denying values. Re-run check suites are handled by taking
    the greatest check-run id for each exact name on the expected head SHA.
    """

    if not _SHA.fullmatch(expected_head_sha):
        raise ValueError("expected head SHA must be an exact lowercase 40-character commit SHA")
    if unresolved_conversations < 0:
        raise ValueError("unresolved conversation count cannot be negative")

    conclusions = _latest_check_conclusions(checks, expected_head_sha)
    normalized_labels = {label.strip() for label in labels if label.strip()}

    return AutonomyGateEvidence(
        forge_managed="forge-managed" in normalized_labels,
        scope_bounded=scope_bounded,
        security_clear=security_clear,
        high_risk=risk_level.strip().lower() != "low",
        deterministic_verification_passed=_all_success(
            policy.deterministic_checks, conclusions
        ),
        quality_passed=_all_success(policy.quality_checks, conclusions),
        independent_review_approved=_all_success(
            policy.independent_review_checks, conclusions
        ),
        secret_scan_passed=_all_success(policy.secret_scan_checks, conclusions),
        branch_protection_allows=branch_protection_allows,
        unresolved_conversations=unresolved_conversations,
        deployment_requested=deployment_requested,
        broker_access_requested=broker_access_requested,
    )
