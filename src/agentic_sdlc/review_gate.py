"""Fail-closed enforcement of role-aware acceptance reviews (issue #119).

The independent reviewer writes one structured document per selected role;
this module is the TRUSTED judge of those documents. Nothing here calls a
model or a provider: the workflow validates and enforces with these
functions, so an ``approve`` that still lists a blocking finding, skips an
acceptance criterion, or comes from an older schema can never pass the gate.

The canonical review schema lives beside this module in
``schemas/review-v2.json`` and is versioned: a review without
``schemaVersion: 2`` — including every legacy document written before the
acceptance-criteria verdict existed — fails closed.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from importlib import resources
from typing import Any

from .review_roles import RoleSelection

__all__ = [
    "REVIEW_SCHEMA_VERSION",
    "ReviewGateDecision",
    "enforce_reviews",
    "evaluate_review_document",
    "load_review_schema",
    "select_repair_criteria",
    "select_repair_findings",
]

REVIEW_SCHEMA_VERSION = 2

#: Severities that block regardless of the finding's own ``blocking`` flag.
_BLOCKING_SEVERITIES = frozenset({"critical", "high"})
_SEVERITIES = ("critical", "high", "medium", "low")
_VERDICTS = ("approve", "changes_requested")
_CRITERION_VERDICTS = ("met", "not_met", "not_verifiable")
_GATE_OUTCOMES = ("block", "advise")
_HEAD_SHA = re.compile(r"^[0-9a-f]{40}$")
_ROLE = re.compile(r"^[a-z][a-z-]{1,63}$")


def load_review_schema() -> dict[str, Any]:
    """The packaged canonical review schema (version 2).

    The review workflow embeds a byte-for-byte copy; a contract test keeps
    the two in sync so the schema the reviewer is held to is always the one
    this module enforces.
    """
    text = resources.files("agentic_sdlc").joinpath("schemas/review-v2.json").read_text("utf-8")
    return json.loads(text)


@dataclass(frozen=True)
class ReviewGateDecision:
    allowed: bool
    reasons: tuple[str, ...]


def _bounded_text(value: Any, limit: int) -> bool:
    return isinstance(value, str) and len(value) <= limit


def _structural_errors(document: Mapping[str, Any]) -> list[str]:
    """Why the document is not a valid version-2 review — empty when valid.

    Validation is fail-closed and total: unknown fields, missing fields,
    wrong types and out-of-vocabulary values each produce an error, so a
    legacy (pre-versioned) review is rejected rather than read permissively.
    """
    errors: list[str] = []
    if not isinstance(document, Mapping):
        return ["review document must be a JSON object"]
    expected = {
        "schemaVersion",
        "role",
        "headSha",
        "verdict",
        "summary",
        "findings",
        "residualRisks",
        "acceptanceCriteriaVerdict",
    }
    unknown = sorted(set(document) - expected)
    if unknown:
        errors.append("review document carries unknown fields: " + ", ".join(unknown))
    if document.get("schemaVersion") != REVIEW_SCHEMA_VERSION:
        errors.append(
            "review document does not declare schemaVersion "
            f"{REVIEW_SCHEMA_VERSION}; legacy reviews fail closed"
        )
    role = document.get("role")
    if not isinstance(role, str) or not _ROLE.fullmatch(role):
        errors.append("review document must name its reviewer role")
    head = document.get("headSha")
    if not isinstance(head, str) or not _HEAD_SHA.fullmatch(head):
        errors.append("review document must bind the exact reviewed head SHA")
    if document.get("verdict") not in _VERDICTS:
        errors.append("review verdict must be approve or changes_requested")
    if not _bounded_text(document.get("summary"), 4000):
        errors.append("review summary must be a string of at most 4000 characters")
    findings = document.get("findings")
    if not isinstance(findings, list) or len(findings) > 50:
        errors.append("findings must be an array of at most 50 items")
    else:
        for index, finding in enumerate(findings):
            errors.extend(_finding_errors(index, finding))
    risks = document.get("residualRisks")
    if (
        not isinstance(risks, list)
        or len(risks) > 20
        or not all(_bounded_text(risk, 1000) for risk in risks)
    ):
        errors.append("residualRisks must be an array of at most 20 bounded strings")
    criteria = document.get("acceptanceCriteriaVerdict")
    if not isinstance(criteria, list) or not criteria or len(criteria) > 100:
        errors.append(
            "acceptanceCriteriaVerdict must be a non-empty array: a review "
            "that verified no acceptance criteria cannot pass the gate"
        )
    else:
        for index, entry in enumerate(criteria):
            if (
                not isinstance(entry, Mapping)
                or set(entry) != {"criterion", "verdict"}
                or not isinstance(entry.get("criterion"), str)
                or not 1 <= len(entry["criterion"]) <= 1000
                or entry.get("verdict") not in _CRITERION_VERDICTS
            ):
                errors.append(
                    f"acceptanceCriteriaVerdict[{index}] must pair a criterion "
                    "with met, not_met, or not_verifiable"
                )
    return errors


def _finding_errors(index: int, finding: Any) -> list[str]:
    expected = {
        "severity",
        "title",
        "body",
        "file",
        "line",
        "blocking",
        "gateOutcome",
        "repairRecommendation",
    }
    if not isinstance(finding, Mapping) or set(finding) != expected:
        return [f"findings[{index}] must carry exactly the version-2 finding fields"]
    errors: list[str] = []
    if finding["severity"] not in _SEVERITIES:
        errors.append(f"findings[{index}] severity is out of vocabulary")
    if not _bounded_text(finding["title"], 300) or not _bounded_text(finding["body"], 4000):
        errors.append(f"findings[{index}] title or body is missing or over-long")
    if not _bounded_text(finding["file"], 1000):
        errors.append(f"findings[{index}] file must be a bounded string")
    line = finding["line"]
    if line is not None and (not isinstance(line, int) or isinstance(line, bool) or line < 1):
        errors.append(f"findings[{index}] line must be a positive integer or null")
    if not isinstance(finding["blocking"], bool):
        errors.append(f"findings[{index}] blocking must be a boolean")
    if finding["gateOutcome"] not in _GATE_OUTCOMES:
        errors.append(f"findings[{index}] gateOutcome must be block or advise")
    if not _bounded_text(finding["repairRecommendation"], 2000):
        errors.append(f"findings[{index}] repairRecommendation must be a bounded string")
    return errors


def _finding_blocks(finding: Mapping[str, Any]) -> bool:
    return (
        finding.get("blocking") is True
        or finding.get("severity") in _BLOCKING_SEVERITIES
        or finding.get("gateOutcome") == "block"
    )


def _normalized_criterion(text: str) -> str:
    """Whitespace-insensitive comparison key for one acceptance criterion.

    The reviewer copies criteria out of a rendered prompt, so line wrapping
    must not read as a different criterion — but any WORD difference still
    does, so an omitted criterion can never hide behind a reworded one.
    """
    return " ".join(text.split())


def _coverage_errors(document: Mapping[str, Any], expected_criteria: Iterable[str]) -> list[str]:
    """Why the verdict does not cover EXACTLY the packet's criteria.

    The reviewer writes ``acceptanceCriteriaVerdict`` itself, so without this
    check a review could omit the criterion it could not verify, or fabricate
    an extra one marked met: coverage must be total — every expected
    criterion exactly once, and nothing the work item never asked for.
    """
    errors: list[str] = []
    expected = [_normalized_criterion(criterion) for criterion in expected_criteria]
    expected_set = set(expected)
    seen: set[str] = set()
    for entry in document["acceptanceCriteriaVerdict"]:
        key = _normalized_criterion(entry["criterion"])
        if key in seen:
            errors.append(f"duplicate acceptance criterion verdict: {entry['criterion']}")
        seen.add(key)
        if key not in expected_set:
            errors.append(f"verdict names a criterion the work item does not: {entry['criterion']}")
    for criterion, key in zip(expected_criteria, expected, strict=True):
        if key not in seen:
            errors.append(f"no verdict covers acceptance criterion: {criterion}")
    return errors


def evaluate_review_document(
    document: Mapping[str, Any],
    *,
    expected_criteria: Iterable[str] | None = None,
) -> ReviewGateDecision:
    """Does ONE review document pass the gate? Reasons name what does not.

    Passing requires all three: an ``approve`` verdict, zero blocking
    findings (``blocking: true``, ``gateOutcome: block``, or severity
    critical/high), and every acceptance criterion marked ``met`` — a
    ``not_verifiable`` criterion blocks, because unverified is not verified.
    With ``expected_criteria`` (the packet's own acceptance criteria), the
    verdict must also cover exactly those criteria: an omitted, duplicated,
    or fabricated criterion entry blocks with a named reason.
    """
    errors = _structural_errors(document)
    if errors:
        return ReviewGateDecision(False, tuple(errors))
    reasons: list[str] = []
    if document["verdict"] != "approve":
        reasons.append(f"review verdict is {document['verdict']}, not approve")
    for finding in document["findings"]:
        if _finding_blocks(finding):
            reasons.append(f"blocking finding ({finding['severity']}): {finding['title']}")
    if expected_criteria is not None:
        reasons.extend(_coverage_errors(document, tuple(expected_criteria)))
    for entry in document["acceptanceCriteriaVerdict"]:
        if entry["verdict"] != "met":
            reasons.append(f"acceptance criterion {entry['verdict']}: {entry['criterion']}")
    return ReviewGateDecision(not reasons, tuple(reasons))


def enforce_reviews(
    selection: RoleSelection,
    documents: Iterable[Mapping[str, Any]],
    head_sha: str,
    *,
    expected_criteria: Iterable[str] | None = None,
) -> ReviewGateDecision:
    """Does the WHOLE review gate pass for ``head_sha``?

    Every selected role must have a passing review document bound to exactly
    this head: when the security reviewer was selected, a code review alone
    never passes, and a verdict for any other head is not a verdict at all.
    ``expected_criteria`` — the packet's acceptance criteria — makes coverage
    total per role: a review that omits, duplicates, or fabricates a
    criterion entry blocks with a named reason.
    """
    criteria = tuple(expected_criteria) if expected_criteria is not None else None
    if not _HEAD_SHA.fullmatch(head_sha or ""):
        return ReviewGateDecision(False, ("enforcement requires the exact 40-hex head SHA",))
    by_role: dict[str, list[Mapping[str, Any]]] = {}
    reasons: list[str] = []
    for document in documents:
        role = document.get("role") if isinstance(document, Mapping) else None
        by_role.setdefault(role if isinstance(role, str) else "", []).append(document)
    for role in selection.roles:
        candidates = [
            document for document in by_role.get(role, []) if document.get("headSha") == head_sha
        ]
        if not candidates:
            reasons.append(f"no {role} verdict exists for head {head_sha}")
            continue
        for document in candidates:
            decision = evaluate_review_document(document, expected_criteria=criteria)
            if not decision.allowed:
                reasons.extend(f"{role}: {reason}" for reason in decision.reasons)
    return ReviewGateDecision(not reasons, tuple(reasons))


def select_repair_findings(document: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """Only the BLOCKING findings, for the repair prompt.

    Low-severity and non-blocking advisory findings stay out of repair: the
    bounded repair budget is spent on what actually gates the merge. The
    document must be a valid version-2 review, so a legacy document cannot
    smuggle unvalidated findings into a write-capable repair run.
    """
    errors = _structural_errors(document)
    if errors:
        raise ValueError("; ".join(errors))
    return tuple(dict(finding) for finding in document["findings"] if _finding_blocks(finding))


def select_repair_criteria(document: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """The not-met and not-verifiable criterion verdicts, for the repair prompt.

    A criterion verdict blocks the gate independently of any finding, so a
    review that reports ``not_met`` without adding a blocking finding must
    still reach repair — otherwise the gate blocks on a reason the bounded
    repair run is never told about. The document must be a valid version-2
    review, exactly like ``select_repair_findings``.
    """
    errors = _structural_errors(document)
    if errors:
        raise ValueError("; ".join(errors))
    return tuple(
        dict(entry) for entry in document["acceptanceCriteriaVerdict"] if entry["verdict"] != "met"
    )
