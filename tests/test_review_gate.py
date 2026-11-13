"""Fail-closed enforcement of role-aware acceptance reviews (issue #119)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_sdlc.review_gate import (
    enforce_reviews,
    evaluate_review_document,
    load_review_schema,
    select_repair_criteria,
    select_repair_findings,
)
from agentic_sdlc.review_roles import select_review_roles

HEAD = "a" * 40


def _finding(**overrides):
    finding = {
        "severity": "low",
        "title": "A nit",
        "body": "Rename the helper.",
        "file": "src/app.py",
        "line": 10,
        "blocking": False,
        "gateOutcome": "advise",
        "repairRecommendation": "",
    }
    finding.update(overrides)
    return finding


def _review(**overrides):
    document = {
        "schemaVersion": 2,
        "role": "code-reviewer",
        "headSha": HEAD,
        "verdict": "approve",
        "summary": "Looks correct.",
        "findings": [],
        "residualRisks": [],
        "acceptanceCriteriaVerdict": [
            {"criterion": "Exporter streams rows", "verdict": "met"},
            {"criterion": "Tests cover the empty file", "verdict": "met"},
        ],
    }
    document.update(overrides)
    return document


def test_a_clean_approve_passes() -> None:
    decision = evaluate_review_document(_review())
    assert decision.allowed and decision.reasons == ()


def test_approve_with_a_high_or_blocking_finding_is_rejected() -> None:
    high = evaluate_review_document(_review(findings=[_finding(severity="high")]))
    assert not high.allowed
    assert any("blocking finding (high): A nit" in reason for reason in high.reasons)
    flagged = evaluate_review_document(_review(findings=[_finding(blocking=True)]))
    assert not flagged.allowed
    gated = evaluate_review_document(_review(findings=[_finding(gateOutcome="block")]))
    assert not gated.allowed
    # A medium advisory finding alone does not block.
    advisory = evaluate_review_document(_review(findings=[_finding(severity="medium")]))
    assert advisory.allowed


def test_an_unmet_or_unverifiable_criterion_is_rejected_by_name() -> None:
    unmet = evaluate_review_document(
        _review(
            acceptanceCriteriaVerdict=[
                {"criterion": "Exporter streams rows", "verdict": "met"},
                {"criterion": "Tests cover the empty file", "verdict": "not_met"},
            ]
        )
    )
    assert not unmet.allowed
    assert "acceptance criterion not_met: Tests cover the empty file" in unmet.reasons
    # Unverified is not verified: not_verifiable fails closed too.
    unverifiable = evaluate_review_document(
        _review(
            acceptanceCriteriaVerdict=[
                {"criterion": "Exporter streams rows", "verdict": "not_verifiable"}
            ]
        )
    )
    assert not unverifiable.allowed


def test_a_legacy_review_without_the_versioned_fields_fails_closed() -> None:
    legacy = {
        "verdict": "approve",
        "summary": "Looks fine.",
        "findings": [
            {
                "severity": "low",
                "title": "nit",
                "body": "rename",
                "file": "src/app.py",
                "line": 1,
            }
        ],
        "residualRisks": [],
    }
    decision = evaluate_review_document(legacy)
    assert not decision.allowed
    assert any("schemaVersion" in reason for reason in decision.reasons)
    # An empty criteria verdict is a review that verified nothing.
    empty = evaluate_review_document(_review(acceptanceCriteriaVerdict=[]))
    assert not empty.allowed
    assert any("non-empty" in reason for reason in empty.reasons)
    # Unknown fields fail closed instead of being read permissively.
    extra = evaluate_review_document(_review(extra="field"))
    assert not extra.allowed


def test_the_criteria_verdict_must_cover_exactly_the_packets_criteria() -> None:
    expected = ("Exporter streams rows", "Tests cover the empty file")
    # Exact coverage passes, and line wrapping is not a different criterion.
    assert evaluate_review_document(_review(), expected_criteria=expected).allowed
    wrapped = _review(
        acceptanceCriteriaVerdict=[
            {"criterion": "Exporter\n  streams   rows", "verdict": "met"},
            {"criterion": "Tests cover the empty file", "verdict": "met"},
        ]
    )
    assert evaluate_review_document(wrapped, expected_criteria=expected).allowed
    # An omitted criterion blocks by name: the reviewer cannot skip the one
    # criterion it could not verify.
    omitted = _review(
        acceptanceCriteriaVerdict=[{"criterion": "Exporter streams rows", "verdict": "met"}]
    )
    decision = evaluate_review_document(omitted, expected_criteria=expected)
    assert not decision.allowed
    assert "no verdict covers acceptance criterion: Tests cover the empty file" in decision.reasons
    # A fabricated criterion blocks: met-marking an invented requirement is
    # not verification of the work item.
    fabricated = _review(
        acceptanceCriteriaVerdict=[
            {"criterion": "Exporter streams rows", "verdict": "met"},
            {"criterion": "Tests cover the empty file", "verdict": "met"},
            {"criterion": "Code compiles", "verdict": "met"},
        ]
    )
    invented = evaluate_review_document(fabricated, expected_criteria=expected)
    assert not invented.allowed
    assert any("Code compiles" in reason for reason in invented.reasons)
    # A duplicated entry blocks: two verdicts for one criterion could carry
    # conflicting answers.
    duplicated = _review(
        acceptanceCriteriaVerdict=[
            {"criterion": "Exporter streams rows", "verdict": "met"},
            {"criterion": "Exporter streams rows", "verdict": "met"},
            {"criterion": "Tests cover the empty file", "verdict": "met"},
        ]
    )
    doubled = evaluate_review_document(duplicated, expected_criteria=expected)
    assert not doubled.allowed
    assert any("duplicate acceptance criterion" in reason for reason in doubled.reasons)


def test_enforcement_applies_criteria_coverage_to_every_role() -> None:
    selection = select_review_roles("Rotate the token", (), ())
    expected = ("Exporter streams rows", "Tests cover the empty file")
    code = _review()
    security = _review(
        role="security-reviewer",
        acceptanceCriteriaVerdict=[{"criterion": "Exporter streams rows", "verdict": "met"}],
    )
    decision = enforce_reviews(selection, [code, security], HEAD, expected_criteria=expected)
    assert not decision.allowed
    assert any(
        reason.startswith("security-reviewer: no verdict covers acceptance criterion")
        for reason in decision.reasons
    )
    full_security = _review(role="security-reviewer")
    assert enforce_reviews(
        selection, [code, full_security], HEAD, expected_criteria=expected
    ).allowed


def test_changes_requested_never_passes_even_without_findings() -> None:
    decision = evaluate_review_document(_review(verdict="changes_requested"))
    assert not decision.allowed
    assert any("not approve" in reason for reason in decision.reasons)


def test_the_gate_requires_every_selected_role_at_the_exact_head() -> None:
    selection = select_review_roles("Rotate the token", (), ())
    assert selection.roles == ("code-reviewer", "security-reviewer")
    code = _review()
    # A code review alone stays blocked while the security reviewer is
    # selected.
    partial = enforce_reviews(selection, [code], HEAD)
    assert not partial.allowed
    assert f"no security-reviewer verdict exists for head {HEAD}" in partial.reasons
    # A security verdict for ANOTHER head is not a verdict at all.
    stale = _review(role="security-reviewer", headSha="b" * 40)
    staled = enforce_reviews(selection, [code, stale], HEAD)
    assert not staled.allowed
    # Both roles passing at the same head pass the gate.
    security = _review(role="security-reviewer")
    passing = enforce_reviews(selection, [code, security], HEAD)
    assert passing.allowed
    # A failing security review blocks even when the code review approves.
    failing = _review(
        role="security-reviewer",
        verdict="changes_requested",
        findings=[_finding(severity="high", title="Token logged")],
    )
    blocked = enforce_reviews(selection, [code, failing], HEAD)
    assert not blocked.allowed
    assert any("Token logged" in reason for reason in blocked.reasons)
    # The head itself is validated.
    assert not enforce_reviews(selection, [code, security], "main").allowed


def test_repair_receives_only_blocking_findings() -> None:
    document = _review(
        verdict="changes_requested",
        findings=[
            _finding(title="Low nit"),
            _finding(severity="medium", title="Medium advisory"),
            _finding(severity="high", title="High bug"),
            _finding(title="Flagged blocker", blocking=True),
            _finding(title="Gated blocker", gateOutcome="block"),
        ],
    )
    titles = [finding["title"] for finding in select_repair_findings(document)]
    assert titles == ["High bug", "Flagged blocker", "Gated blocker"]
    with pytest.raises(ValueError, match="schemaVersion"):
        select_repair_findings({"verdict": "changes_requested", "findings": []})


def test_repair_also_receives_the_unverified_acceptance_criteria() -> None:
    # A criterion verdict blocks the gate independently of any finding, so
    # an ordinary acceptance failure must still reach repair.
    document = _review(
        verdict="changes_requested",
        acceptanceCriteriaVerdict=[
            {"criterion": "Exporter streams rows", "verdict": "met"},
            {"criterion": "Tests cover the empty file", "verdict": "not_met"},
            {"criterion": "Exporter handles BOMs", "verdict": "not_verifiable"},
        ],
    )
    entries = select_repair_criteria(document)
    assert [entry["criterion"] for entry in entries] == [
        "Tests cover the empty file",
        "Exporter handles BOMs",
    ]
    assert [entry["verdict"] for entry in entries] == ["not_met", "not_verifiable"]
    assert select_repair_criteria(_review()) == ()
    # Like the findings filter, a legacy document fails closed.
    with pytest.raises(ValueError, match="schemaVersion"):
        select_repair_criteria({"verdict": "changes_requested", "findings": []})


def test_the_packaged_schema_is_versioned_and_loads(tmp_path: Path) -> None:
    schema = load_review_schema()
    assert schema["properties"]["schemaVersion"] == {"const": 2}
    required = set(schema["required"])
    assert {"schemaVersion", "role", "headSha", "acceptanceCriteriaVerdict"} <= required
    finding_schema = schema["properties"]["findings"]["items"]
    assert {"blocking", "gateOutcome", "repairRecommendation"} <= set(finding_schema["required"])
    # The schema is real JSON that a workflow can materialize verbatim.
    (tmp_path / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
