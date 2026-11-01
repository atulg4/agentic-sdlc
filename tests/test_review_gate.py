"""Fail-closed enforcement of role-aware acceptance reviews (issue #119)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentic_sdlc.review_gate import (
    enforce_reviews,
    evaluate_review_document,
    load_review_schema,
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


def test_the_packaged_schema_is_versioned_and_loads(tmp_path: Path) -> None:
    schema = load_review_schema()
    assert schema["properties"]["schemaVersion"] == {"const": 2}
    required = set(schema["required"])
    assert {"schemaVersion", "role", "headSha", "acceptanceCriteriaVerdict"} <= required
    finding_schema = schema["properties"]["findings"]["items"]
    assert {"blocking", "gateOutcome", "repairRecommendation"} <= set(finding_schema["required"])
    # The schema is real JSON that a workflow can materialize verbatim.
    (tmp_path / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
