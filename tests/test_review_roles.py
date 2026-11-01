"""Deterministic reviewer-role selection (issue #119)."""

from __future__ import annotations

import pytest

from agentic_sdlc.review_roles import (
    ROLE_CHECKLISTS,
    RoleSelection,
    render_role_checklists,
    select_review_roles,
    selection_from_dict,
)


def test_a_benign_task_selects_only_the_code_reviewer() -> None:
    selection = select_review_roles(
        "Refactor the CSV exporter for streaming writes",
        ("enhancement",),
        ("src/exporter.py", "tests/test_exporter.py"),
    )
    assert selection.roles == ("code-reviewer",)
    assert selection.reasons["code-reviewer"]


def test_security_keywords_in_task_text_select_the_security_reviewer() -> None:
    selection = select_review_roles(
        "Rotate the API token when a session expires",
        (),
        ("src/exporter.py",),
    )
    assert selection.roles == ("code-reviewer", "security-reviewer")
    assert any("token" in reason for reason in selection.reasons["security-reviewer"])


def test_keyword_matching_is_word_bounded() -> None:
    # "tokenizer" is not "token": lexical containment alone must not
    # escalate every parser change to a security review.
    selection = select_review_roles(
        "Speed up the tokenizer and the authorship report",
        (),
        ("src/exporter.py",),
    )
    assert selection.roles == ("code-reviewer",)


def test_security_labels_select_the_security_reviewer() -> None:
    selection = select_review_roles(
        "Refactor the CSV exporter",
        ("security", "enhancement"),
        ("src/exporter.py",),
    )
    assert "security-reviewer" in selection.roles
    assert any("label security" in reason for reason in selection.reasons["security-reviewer"])


def test_security_sensitive_paths_select_the_security_reviewer() -> None:
    for path in (".github/workflows/ci.yml", "src/app/auth/session.py", "config/tokens.toml"):
        selection = select_review_roles("Refactor the CSV exporter", (), (path,))
        assert selection.roles == ("code-reviewer", "security-reviewer"), path
        assert any(path in reason for reason in selection.reasons["security-reviewer"])


def test_policy_protected_paths_always_select_the_security_reviewer() -> None:
    selection = select_review_roles(
        "Refactor the CSV exporter",
        (),
        ("src/billing/ledger.py",),
        protected_paths=("src/billing/**",),
    )
    assert selection.roles == ("code-reviewer", "security-reviewer")
    assert any("policy-protected" in reason for reason in selection.reasons["security-reviewer"])


def test_selection_is_deterministic_whatever_the_input_order() -> None:
    first = select_review_roles(
        "Harden credential storage",
        ("security", "backend"),
        ("src/a_auth.py", "src/z_token.py"),
    )
    second = select_review_roles(
        "Harden credential storage",
        ("backend", "security"),
        ("src/z_token.py", "src/a_auth.py"),
    )
    assert first == second
    assert first.as_dict() == second.as_dict()


def test_the_selection_round_trips_and_fails_closed() -> None:
    selection = select_review_roles("Rotate the token", (), ())
    assert selection_from_dict(selection.as_dict()) == selection
    with pytest.raises(ValueError, match="schemaVersion"):
        selection_from_dict({"roles": ["code-reviewer"]})
    with pytest.raises(ValueError, match="code-reviewer"):
        selection_from_dict({"schemaVersion": 1, "roles": ["security-reviewer"], "reasons": {}})
    with pytest.raises(ValueError, match="no reasons"):
        selection_from_dict({"schemaVersion": 1, "roles": ["code-reviewer"], "reasons": {}})
    with pytest.raises(ValueError, match="list selected roles"):
        selection_from_dict({"schemaVersion": 1, "roles": [], "reasons": {}})


def test_checklists_are_role_specific_and_reviewers_get_no_write_instructions() -> None:
    code = render_role_checklists(("code-reviewer",))
    security = render_role_checklists(("security-reviewer",))
    assert "Checklist for code-reviewer:" in code
    assert "acceptance criteria" in code
    assert "Checklist for security-reviewer:" in security
    assert "untrusted input" in security.lower() or "secrets" in security.lower()
    # Role separation: each prompt carries only its own checklist, and no
    # checklist instructs the reviewer to change anything.
    assert "security-reviewer" not in code
    assert "Checklist for code-reviewer" not in security
    for checklist in ROLE_CHECKLISTS.values():
        for item in checklist:
            lowered = item.lower()
            assert "edit" not in lowered and "push" not in lowered and "write" not in lowered
    with pytest.raises(ValueError, match="no checklist"):
        render_role_checklists(("quant-reviewer",))


def test_reasons_serialize_with_the_selection() -> None:
    selection = select_review_roles("Fix the OAuth callback", (), ("src/app.py",))
    document = selection.as_dict()
    assert document["schemaVersion"] == 1
    assert document["roles"] == ["code-reviewer", "security-reviewer"]
    assert document["reasons"]["security-reviewer"]
    assert isinstance(selection, RoleSelection)
