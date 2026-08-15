from __future__ import annotations

import pytest

from agentic_sdlc.merge_evidence import MergeEvidencePolicy, normalize_merge_evidence

HEAD = "a" * 40
POLICY = MergeEvidencePolicy(
    deterministic_checks=("test",),
    quality_checks=("quality",),
    independent_review_checks=("Independent agent review",),
    secret_scan_checks=("GitGuardian Security Checks",),
)


def _checks(*, head: str = HEAD) -> list[dict[str, object]]:
    return [
        {"id": 10, "name": "test", "head_sha": head, "conclusion": "success"},
        {"id": 11, "name": "quality", "head_sha": head, "conclusion": "success"},
        {
            "id": 12,
            "name": "Independent agent review",
            "head_sha": head,
            "conclusion": "success",
        },
        {
            "id": 13,
            "name": "GitGuardian Security Checks",
            "head_sha": head,
            "conclusion": "success",
        },
    ]


def _normalize(**overrides: object):
    values: dict[str, object] = {
        "expected_head_sha": HEAD,
        "labels": ["forge-managed"],
        "risk_level": "low",
        "scope_bounded": True,
        "security_clear": True,
        "branch_protection_allows": True,
        "unresolved_conversations": 0,
        "deployment_requested": False,
        "broker_access_requested": False,
        "checks": _checks(),
        "policy": POLICY,
    }
    values.update(overrides)
    return normalize_merge_evidence(**values)  # type: ignore[arg-type]


def test_all_required_exact_head_checks_produce_green_merge_evidence() -> None:
    evidence = _normalize()

    assert evidence.forge_managed is True
    assert evidence.scope_bounded is True
    assert evidence.security_clear is True
    assert evidence.high_risk is False
    assert evidence.deterministic_verification_passed is True
    assert evidence.quality_passed is True
    assert evidence.independent_review_approved is True
    assert evidence.secret_scan_passed is True
    assert evidence.branch_protection_allows is True
    assert evidence.unresolved_conversations == 0


@pytest.mark.parametrize(
    ("missing_name", "attribute"),
    [
        ("test", "deterministic_verification_passed"),
        ("quality", "quality_passed"),
        ("Independent agent review", "independent_review_approved"),
        ("GitGuardian Security Checks", "secret_scan_passed"),
    ],
)
def test_missing_required_check_fails_closed(missing_name: str, attribute: str) -> None:
    checks = [check for check in _checks() if check["name"] != missing_name]

    evidence = _normalize(checks=checks)

    assert getattr(evidence, attribute) is False


def test_stale_head_checks_do_not_satisfy_current_head() -> None:
    evidence = _normalize(checks=_checks(head="b" * 40))

    assert evidence.deterministic_verification_passed is False
    assert evidence.quality_passed is False
    assert evidence.independent_review_approved is False
    assert evidence.secret_scan_passed is False


def test_latest_rerun_result_wins_for_same_check_name() -> None:
    checks = _checks()
    checks.extend(
        [
            {"id": 1, "name": "test", "head_sha": HEAD, "conclusion": "failure"},
            {"id": 99, "name": "quality", "head_sha": HEAD, "conclusion": "failure"},
        ]
    )

    evidence = _normalize(checks=checks)

    assert evidence.deterministic_verification_passed is True
    assert evidence.quality_passed is False


def test_pending_cancelled_or_malformed_checks_fail_closed() -> None:
    checks = _checks()
    checks[0]["conclusion"] = ""
    checks[1]["conclusion"] = "cancelled"
    checks[2]["id"] = "not-an-id"
    checks[3]["head_sha"] = "b" * 40

    evidence = _normalize(checks=checks)

    assert evidence.deterministic_verification_passed is False
    assert evidence.quality_passed is False
    assert evidence.independent_review_approved is False
    assert evidence.secret_scan_passed is False


def test_non_low_risk_defaults_to_human_policy_review() -> None:
    assert _normalize(risk_level="medium").high_risk is True
    assert _normalize(risk_level="unknown").high_risk is True
    assert _normalize(risk_level="").high_risk is True


def test_unmanaged_or_sensitive_work_stays_explicitly_denied() -> None:
    evidence = _normalize(
        labels=[], deployment_requested=True, broker_access_requested=True
    )

    assert evidence.forge_managed is False
    assert evidence.deployment_requested is True
    assert evidence.broker_access_requested is True


def test_invalid_head_sha_and_negative_thread_count_are_rejected() -> None:
    with pytest.raises(ValueError, match="exact lowercase 40-character"):
        _normalize(expected_head_sha="main")
    with pytest.raises(ValueError, match="cannot be negative"):
        _normalize(unresolved_conversations=-1)


def test_policy_requires_nonempty_unique_check_groups() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        MergeEvidencePolicy((), ("quality",), ("review",), ("secret",))
    with pytest.raises(ValueError, match="unique"):
        MergeEvidencePolicy(("test",), ("test",), ("review",), ("secret",))
