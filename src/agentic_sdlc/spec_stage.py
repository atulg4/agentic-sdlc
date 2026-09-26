"""Spec stage: draft the sections a work request is missing, then hold it for review.

An issue that fails the intake contract (``check_task_spec``) is sent to the
planner executor in ``spec`` mode. Its output is untrusted text; this module
parses it deterministically, appends only the sections intake rejected beneath
a visible owner-review marker, and never edits the existing request text.

A drafted specification is not an approved one. ``spec_review_block`` is the
deterministic gate that keeps a ``spec-drafted`` request out of implementation
and autonomous dispatch until a human removes that label or adds
``spec-approved``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .task_spec import (
    REQUIRED_SECTION_HEADINGS,
    REQUIRED_SECTIONS,
    TaskSpecError,
    _dependencies,
    _has_meaningful_text,
    check_task_spec,
    parse_task,
    split_sections,
)

__all__ = [
    "BLOCK_BEGIN",
    "BLOCK_END",
    "REVIEW_MARKER",
    "SPEC_APPROVED_LABEL",
    "SPEC_DRAFTED_LABEL",
    "SpecDraft",
    "SpecMerge",
    "SpecStageError",
    "merge_spec",
    "parse_draft",
    "spec_review_block",
    "strip_spec_block",
]

SPEC_DRAFTED_LABEL = "spec-drafted"
SPEC_APPROVED_LABEL = "spec-approved"
REVIEW_MARKER = "Drafted by Forge spec stage — owner review required"
BLOCK_BEGIN = "<!-- forge-spec-stage:begin -->"
BLOCK_END = "<!-- forge-spec-stage:end -->"
OPEN_QUESTIONS_HEADING = "Open Questions (Forge spec stage)"
_MARKER_FRAGMENT = "forge-spec-stage:"
_UNCONFIRMED_DEPENDENCIES = (
    "Dependencies were drafted as None because none could be grounded in the request "
    "or repository; confirm there are no prerequisite issues."
)


class SpecStageError(TaskSpecError):
    """Raised when a spec draft is malformed or cannot be merged safely."""


def spec_review_block(labels: Iterable[str]) -> str | None:
    """Return why a request is held for spec review, or ``None`` when it is not.

    A ``spec-drafted`` request stays ineligible for implementation and
    autonomous dispatch until a human removes the label or adds
    ``spec-approved``.
    """
    present = set(labels)
    if SPEC_DRAFTED_LABEL in present and SPEC_APPROVED_LABEL not in present:
        return (
            f"specification was drafted by the Forge spec stage and awaits owner review: "
            f"remove '{SPEC_DRAFTED_LABEL}' or add '{SPEC_APPROVED_LABEL}'"
        )
    return None


@dataclass(frozen=True)
class SpecDraft:
    """A parsed spec-mode executor output."""

    verdict: str
    sections: Mapping[str, str]
    open_questions: str = ""
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "sections": [REQUIRED_SECTION_HEADINGS[key] for key in self.sections],
            "openQuestions": self.open_questions,
            "reason": self.reason,
        }


def parse_draft(text: str) -> SpecDraft:
    """Parse executor output into required sections, open questions, or a refusal.

    Headings end sections, so a drafted section can never smuggle in another
    heading. Unknown headings are dropped; Forge markers are rejected.
    """
    if "\x00" in text:
        raise SpecStageError("spec draft contains a NUL byte")
    if _MARKER_FRAGMENT in text:
        raise SpecStageError("spec draft contains a Forge spec-stage marker")
    parsed = split_sections(text)
    reason = parsed.get("not drafted", "")
    if _has_meaningful_text(reason):
        return SpecDraft("not-drafted", {}, reason=reason.strip())
    sections = {
        key: parsed[key].strip()
        for key in REQUIRED_SECTIONS
        if key in parsed and _has_meaningful_text(parsed[key])
    }
    if not sections:
        raise SpecStageError("spec draft contains no required section and no Not Drafted reason")
    questions = parsed.get("open questions", "")
    return SpecDraft(
        "drafted",
        sections,
        open_questions=questions.strip() if _has_meaningful_text(questions) else "",
    )


def strip_spec_block(body: str) -> str:
    """Return ``body`` without the Forge-owned block, i.e. the author's own text."""
    begins = body.count(BLOCK_BEGIN)
    ends = body.count(BLOCK_END)
    if begins == 0 and ends == 0:
        return body
    if begins != 1 or ends != 1 or body.index(BLOCK_BEGIN) > body.index(BLOCK_END):
        raise SpecStageError(
            "issue body has a malformed Forge spec-stage block; the owner must repair "
            "or remove it before the spec stage can run again"
        )
    before, rest = body.split(BLOCK_BEGIN, 1)
    after = rest.split(BLOCK_END, 1)[1]
    parts = [part for part in (before.rstrip(), after.strip()) if part]
    return "\n\n".join(parts)


@dataclass(frozen=True)
class SpecMerge:
    body: str
    drafted_sections: tuple[str, ...]
    ignored_sections: tuple[str, ...]
    dependencies: tuple[int, ...]
    changed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "verdict": "drafted",
            "changed": self.changed,
            "draftedSections": [REQUIRED_SECTION_HEADINGS[key] for key in self.drafted_sections],
            "ignoredSections": [REQUIRED_SECTION_HEADINGS[key] for key in self.ignored_sections],
            "dependencies": list(self.dependencies),
        }


def _render_block(
    sections: Mapping[str, str], missing: tuple[str, ...], open_questions: str
) -> str:
    lines = [
        BLOCK_BEGIN,
        f"## {REVIEW_MARKER}",
        "",
        (
            "> Forge drafted the sections below because this issue did not satisfy the "
            f"intake contract (incomplete: {', '.join(missing)}). The text above is "
            f"unchanged. Review and edit them, then remove the `{SPEC_DRAFTED_LABEL}` label "
            f"or add `{SPEC_APPROVED_LABEL}`; until then the issue cannot be dispatched for "
            "implementation."
        ),
    ]
    for key in REQUIRED_SECTIONS:
        if key in sections:
            lines += ["", f"## {REQUIRED_SECTION_HEADINGS[key]}", "", sections[key]]
    if open_questions:
        lines += ["", f"## {OPEN_QUESTIONS_HEADING}", "", open_questions]
    lines.append(BLOCK_END)
    return "\n".join(lines) + "\n"


def merge_spec(
    title: str,
    body: str,
    sections: Mapping[str, str],
    *,
    open_questions: str = "",
    issue_number: int | None = None,
) -> SpecMerge:
    """Append drafted sections for exactly the deficient ones; never touch author text.

    The merge is deterministic and idempotent: an existing Forge block is
    removed first and the block is rebuilt from the author's text, so merging
    the same draft twice yields the same body. Drafts for sections the author
    already satisfied are ignored, because a later heading would override them.
    The result must pass ``parse_task`` or nothing is returned.
    """
    unknown = sorted(set(sections) - set(REQUIRED_SECTIONS))
    if unknown:
        raise SpecStageError("spec draft names unknown sections: " + ", ".join(unknown))
    for key, content in sections.items():
        if _MARKER_FRAGMENT in content or "\x00" in content:
            raise SpecStageError(f"drafted section {key!r} contains forbidden content")
        if split_sections("\n" + content):
            raise SpecStageError(f"drafted section {key!r} contains a heading")
    original = strip_spec_block(body)
    check = check_task_spec(title, original)
    if check.ready:
        return SpecMerge(body, (), tuple(sections), (), False)
    if not check.draftable:
        raise SpecStageError("request cannot be drafted: " + "; ".join(check.blockers))

    deficient = tuple(finding.key for finding in check.findings)
    uncovered = [REQUIRED_SECTION_HEADINGS[key] for key in deficient if key not in sections]
    if uncovered:
        raise SpecStageError("spec draft does not cover: " + ", ".join(uncovered))
    accepted = {key: sections[key].strip() for key in deficient}
    ignored = tuple(key for key in REQUIRED_SECTIONS if key in sections and key not in accepted)

    questions = open_questions.strip()
    dependencies: tuple[int, ...] = ()
    if "dependencies" in accepted:
        dependencies, explicitly_none = _dependencies(accepted["dependencies"])
        if issue_number is not None and issue_number in dependencies:
            raise SpecStageError(
                f"drafted dependencies reference the issue itself (#{issue_number})"
            )
        if not dependencies and explicitly_none and _UNCONFIRMED_DEPENDENCIES not in questions:
            questions = "\n".join(filter(None, (questions, f"- {_UNCONFIRMED_DEPENDENCIES}")))

    block = _render_block(accepted, check.missing_sections, questions)
    merged = f"{original.rstrip()}\n\n{block}" if original.strip() else block
    try:
        parse_task(title, merged)
    except TaskSpecError as error:
        raise SpecStageError(f"merged body still fails the intake contract: {error}") from error
    changed = merged != body
    return SpecMerge(merged, deficient, ignored, dependencies, changed)
