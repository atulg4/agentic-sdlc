"""Deterministic review-role selection for factory runs (issue #119).

Which independent reviewers a candidate change needs is a GATE decision, so
it is computed here — deterministically, from the task text, the work item's
labels, and the changed paths — never left to a model's judgement or a
caller's assertion. Every selection records WHY each role was selected, and
the selection travels with the review run so the enforcement step can demand
a same-head verdict from every selected role.

The security triggers are documented data, not heuristics hidden in code:

- ``SECURITY_TEXT_PATTERNS`` are the casefolded keywords that mark a task or
  label security-sensitive (authentication, authorization, permissions,
  tokens, secrets, credentials, privacy, and policy changes).
- ``SECURITY_PATH_PATTERNS`` are the path globs that mark a changed file
  security-sensitive whatever the task says.
- Any changed path matching the project policy's ``protected_paths`` selects
  the security reviewer too: those paths are protected precisely because a
  change there needs stronger review.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .policy import _matches

__all__ = [
    "ROLE_CHECKLISTS",
    "SECURITY_PATH_PATTERNS",
    "SECURITY_TEXT_PATTERNS",
    "RoleSelection",
    "render_role_checklists",
    "select_review_roles",
    "selection_from_dict",
]

CODE_REVIEWER = "code-reviewer"
SECURITY_REVIEWER = "security-reviewer"

#: Casefolded keywords that mark a task, or a label, security-sensitive.
#: Matched on word boundaries, with hyphen/underscore compounds contributing
#: their parts: "token" matches "rotate the API token" and "token-based
#: sessions", and "tokenizer" does not.
SECURITY_TEXT_PATTERNS: tuple[str, ...] = (
    "auth",
    "authentication",
    "authorization",
    "credential",
    "credentials",
    "oauth",
    "password",
    "permission",
    "permissions",
    "policy",
    "privacy",
    "secret",
    "secrets",
    "security",
    "token",
    "tokens",
)

#: Path globs (fnmatch, ``*`` crosses ``/``, casefolded) that mark a changed
#: file security-sensitive regardless of what the task text says.
SECURITY_PATH_PATTERNS: tuple[str, ...] = (
    ".github/workflows/*",
    "*auth*",
    "*credential*",
    "*oauth*",
    "*permission*",
    "*policy*",
    "*privacy*",
    "*secret*",
    "*token*",
)

#: Role-specific review checklists, rendered into the reviewer prompt as
#: TRUSTED pipeline text (never inside an untrusted boundary): the checklist
#: instructs the reviewer, it does not quote candidate content.
ROLE_CHECKLISTS: Mapping[str, tuple[str, ...]] = {
    CODE_REVIEWER: (
        "Verify the change does what the acceptance criteria require — judge "
        "each criterion met, not met, or not verifiable from this diff.",
        "Hunt for correctness bugs and behavioral regressions the diff "
        "introduces, including in code it calls or that calls it.",
        "Check the required tests exist, exercise the change, and would fail "
        "without it; name missing or weakened coverage.",
        "Confirm the change stays inside the task's scope and non-goals.",
    ),
    SECURITY_REVIEWER: (
        "Trace how untrusted input reaches the changed code and whether it "
        "can reach an interpreter, a shell, a template, or a query.",
        "Check authentication and authorization decisions: who can now do "
        "what they could not before, and what stopped being checked.",
        "Look for secrets, tokens, or credentials that are logged, "
        "serialized, echoed in errors, or handed to a less-trusted context.",
        "Confirm no gate, policy, sandbox, or verification step is weakened, "
        "bypassed, or made caller-controllable.",
    ),
}

_WORD = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)*")


@dataclass(frozen=True)
class RoleSelection:
    """The selected reviewer roles and, per role, why each was selected."""

    roles: tuple[str, ...]
    reasons: Mapping[str, tuple[str, ...]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "roles": list(self.roles),
            "reasons": {role: list(self.reasons[role]) for role in self.roles},
        }


def _words(text: str) -> set[str]:
    """The comparison vocabulary of ``text``: tokens AND their compound parts.

    ``token-based`` and ``security-sensitive`` must match the documented
    keywords ``token`` and ``security``, so every hyphen/underscore compound
    also contributes its constituent words. Lexical containment alone still
    never matches: ``tokenizer`` is one undivided word, not ``token``.
    """
    words: set[str] = set()
    for token in _WORD.findall(text.casefold()):
        words.add(token)
        words.update(re.split(r"[-_]", token))
    return words


def select_review_roles(
    task_text: str,
    labels: Iterable[str],
    changed_paths: Iterable[str],
    *,
    protected_paths: tuple[str, ...] = (),
) -> RoleSelection:
    """Select reviewer roles for one candidate change, deterministically.

    ``code-reviewer`` is always selected: every change gets an independent
    correctness review. ``security-reviewer`` is added when the task text or
    a label carries a documented security keyword, a changed path matches a
    documented security path pattern, or a changed path matches the policy's
    protected paths. The reasons name exactly what triggered each role.
    """
    reasons: dict[str, list[str]] = {CODE_REVIEWER: ["every change gets an independent review"]}
    security: list[str] = []
    task_words = _words(task_text)
    for keyword in SECURITY_TEXT_PATTERNS:
        if keyword in task_words:
            security.append(f"task text matches security keyword: {keyword}")
    for label in sorted({str(label) for label in labels}):
        matched = sorted(_words(label) & set(SECURITY_TEXT_PATTERNS))
        if matched:
            security.append(f"label {label} matches security keyword: {', '.join(matched)}")
    for path in sorted({str(path) for path in changed_paths}):
        if _matches(path, SECURITY_PATH_PATTERNS, casefold=True):
            security.append(f"changed path matches security path pattern: {path}")
        if protected_paths and _matches(path, protected_paths, casefold=True):
            security.append(f"changed path is policy-protected: {path}")
    roles = [CODE_REVIEWER]
    if security:
        roles.append(SECURITY_REVIEWER)
        reasons[SECURITY_REVIEWER] = security
    return RoleSelection(
        roles=tuple(roles),
        reasons={role: tuple(values) for role, values in reasons.items()},
    )


def selection_from_dict(document: Mapping[str, Any]) -> RoleSelection:
    """The validated inverse of ``RoleSelection.as_dict`` — fail closed.

    The enforcement step consumes a stored selection, so a malformed or
    role-less document must refuse rather than quietly gate nothing.
    """
    if not isinstance(document, Mapping) or document.get("schemaVersion") != 1:
        raise ValueError("role selection document must declare schemaVersion 1")
    raw_roles = document.get("roles")
    raw_reasons = document.get("reasons")
    if (
        not isinstance(raw_roles, list)
        or not raw_roles
        or not all(isinstance(role, str) and role for role in raw_roles)
        or not isinstance(raw_reasons, Mapping)
    ):
        raise ValueError("role selection document must list selected roles and reasons")
    if CODE_REVIEWER not in raw_roles:
        raise ValueError("role selection must always include code-reviewer")
    reasons: dict[str, tuple[str, ...]] = {}
    for role in raw_roles:
        values = raw_reasons.get(role)
        if (
            not isinstance(values, list)
            or not values
            or not all(isinstance(value, str) and value for value in values)
        ):
            raise ValueError(f"role selection records no reasons for {role}")
        reasons[role] = tuple(values)
    return RoleSelection(roles=tuple(raw_roles), reasons=reasons)


def render_role_checklists(roles: Iterable[str]) -> str:
    """The selected roles' checklists as trusted prompt text."""
    sections: list[str] = []
    for role in roles:
        checklist = ROLE_CHECKLISTS.get(role)
        if checklist is None:
            raise ValueError(f"no checklist is defined for reviewer role: {role}")
        sections.append("\n".join([f"Checklist for {role}:", *(f"- {item}" for item in checklist)]))
    return "\n\n".join(sections)
