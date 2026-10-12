"""GitHub adapter: turn tracker payloads into Control Center inputs.

``control_center`` is provider-neutral on purpose: it joins a registry, a
lifecycle ledger, a backlog document and an incident document, and knows
nothing about any tracker. This module is one adapter that produces the last
two from GitHub REST payloads, so the console's data is reproducible from a
command instead of maintained by hand.

Every inference here is table-driven and documented, because a console that
guesses is worse than one that says *unknown*:

- :data:`LABEL_STATES` maps a label set to a factory state, highest precedence
  first. An issue matching no row carries no declared state at all, and the
  Control Center reports it as unknown rather than inventing ``intake``.
- :data:`CLOSED_STATES` maps GitHub's close reason to a terminal state. "Closed
  as completed" means completed; "closed as not planned" means cancelled.
- :data:`DISPATCH_RULES` encodes the label preconditions Forge's own dispatch
  triggers require. A row that fires becomes an :class:`Incident`, which is how
  "approved but nothing is running it" becomes visible and remediable.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .control_center import (
    BACKLOG_SCHEMA_VERSION,
    INCIDENT_SCHEMA_VERSION,
    MAX_BACKLOG_ITEMS,
    MAX_INCIDENTS,
    PLAN_EVIDENCE_RULE,
    ControlCenterError,
)

_PARENT_URL = re.compile(r"/issues/(\d+)/?$")

#: An epic is declared by title convention or by owning sub-issues.
EPIC_TITLE = re.compile(r"^\s*(?:[A-Z][A-Za-z]*\s+)?EPIC\b", re.I)

#: Label sets that declare a factory state, most specific first. The first row
#: whose ``required`` labels are all present and whose ``absent`` labels are all
#: missing decides the state.
LABEL_STATES: tuple[tuple[frozenset[str], frozenset[str], str], ...] = (
    # An unreleased spec wins over a stale approval: the spec stage re-drafts
    # without clearing implementation-approved, and the safety gate blocks
    # planning and implementation until the draft is released.
    (frozenset({"spec-drafted"}), frozenset({"spec-approved"}), "specified"),
    (frozenset({"implementation-approved", "agent-ready"}), frozenset(), "approved"),
    (frozenset({"implementation-approved"}), frozenset({"agent-ready"}), "approved"),
    (frozenset({"spec-drafted"}), frozenset({"implementation-approved"}), "specified"),
    (frozenset({"human-review-required"}), frozenset({"spec-drafted"}), "triaged"),
)

#: GitHub's close reason, mapped to the terminal state it actually means. A
#: missing or unrecognised reason maps to nothing: the API permits a null
#: ``state_reason``, and a snapshot never fabricates terminal evidence.
CLOSED_STATES: Mapping[str, str] = {
    "completed": "merged",
    "not_planned": "cancelled",
    "duplicate": "superseded",
}

#: Mergeability verdicts in which a change request is actually waiting on the
#: human merge authority.
READY_MERGE_STATES = frozenset({"clean", "has_hooks"})

#: Known non-ready verdicts, each a real impediment GitHub has already named.
#: They surface as incidents the operator must classify; only an absent or
#: ``unknown`` verdict is suppressed, because unknown is never fabricated.
IMPEDED_MERGE_STATES: Mapping[str, str] = {
    "blocked": "a required review or required check is outstanding",
    "behind": "the head branch is out of date with its base",
    "unstable": "a status check on the head commit is not passing",
}


@dataclass(frozen=True)
class DispatchRule:
    """One precondition Forge's dispatch triggers require of a tracker item."""

    rule_id: str
    required: frozenset[str]
    missing: frozenset[str]
    kind: str
    severity: str
    summary: str

    def matches(self, labels: frozenset[str]) -> bool:
        return self.required <= labels and not (self.missing & labels)


#: The label preconditions the bundled ``agent-auto-plan`` and
#: ``agent-auto-implement`` triggers enforce. Each unmet row is why an approved
#: item is sitting still, which is precisely what an operator needs told.
DISPATCH_RULES: tuple[DispatchRule, ...] = (
    DispatchRule(
        rule_id="spec-awaiting-human-review",
        required=frozenset({"spec-drafted"}),
        missing=frozenset({"spec-approved"}),
        kind="awaiting-spec-review",
        severity="degraded",
        summary=(
            "Specification draft held for human spec review: planning and "
            "implementation stay blocked until spec-drafted is removed or "
            "spec-approved is added"
        ),
    ),
    DispatchRule(
        rule_id="approved-without-dispatch-labels",
        required=frozenset({"implementation-approved"}),
        missing=frozenset({"agent-ready", "human-review-required"}),
        kind="dispatch-blocked",
        severity="blocker",
        summary=(
            "Approved for implementation but carrying neither agent-ready nor "
            "human-review-required, so no trigger fires"
        ),
    ),
    DispatchRule(
        rule_id="approved-without-agent-ready",
        required=frozenset({"implementation-approved", "human-review-required"}),
        missing=frozenset({"agent-ready"}),
        kind="dispatch-blocked",
        severity="blocker",
        summary="Approved for implementation but not labelled agent-ready, so no trigger fires",
    ),
    DispatchRule(
        rule_id="approved-without-human-review",
        required=frozenset({"implementation-approved", "agent-ready"}),
        missing=frozenset({"human-review-required"}),
        kind="dispatch-blocked",
        severity="blocker",
        summary=(
            "Approved and agent-ready but missing human-review-required, which both "
            "bundled triggers require, so nothing fires"
        ),
    ),
    DispatchRule(
        rule_id="agent-ready-without-human-review",
        required=frozenset({"agent-ready"}),
        missing=frozenset({"human-review-required"}),
        kind="dispatch-blocked",
        severity="degraded",
        summary="Labelled agent-ready without human-review-required; both labels are required",
    ),
    # With agent-ready also present, the plan-ordering advisory below is the
    # actionable message, so this row yields to it.
    DispatchRule(
        rule_id="spec-without-approval",
        required=frozenset({"spec-drafted", "human-review-required"}),
        missing=frozenset({"implementation-approved", "agent-ready"}),
        kind="awaiting-human-approval",
        severity="degraded",
        summary="Specification drafted and waiting on a human implementation approval",
    ),
    DispatchRule(
        rule_id="eligible-awaiting-dispatch-evidence",
        required=frozenset({"implementation-approved", "human-review-required", "agent-ready"}),
        missing=frozenset(),
        kind="dispatch-blocked",
        severity="advisory",
        summary=(
            "Every dispatch label is present, but the bundled implementation trigger "
            "fires only on the implementation-approved label event: confirm a run "
            "started, and re-apply implementation-approved to re-kick if none did"
        ),
    ),
    # An unreleased draft is caught by the spec-review rule above, so only
    # implementation-approved blocks this row: a released spec (spec-drafted
    # plus spec-approved) still has the plan-trigger ordering hazard.
    DispatchRule(
        rule_id=PLAN_EVIDENCE_RULE,
        required=frozenset({"agent-ready", "human-review-required"}),
        missing=frozenset({"implementation-approved"}),
        kind="dispatch-blocked",
        severity="advisory",
        summary=(
            "Both planning labels are present, but the bundled plan trigger fires "
            "only on the agent-ready label event: confirm a plan run started, and "
            "re-apply agent-ready to re-kick if none did"
        ),
    ),
    DispatchRule(
        rule_id="released-spec-without-dispatch-labels",
        required=frozenset({"spec-drafted", "spec-approved"}),
        missing=frozenset({"implementation-approved", "agent-ready", "human-review-required"}),
        kind="awaiting-human-approval",
        severity="degraded",
        summary=(
            "Specification released, but no dispatch label follows it: the work "
            "waits on an implementation approval and the dispatch labels"
        ),
    ),
    DispatchRule(
        rule_id="untriaged",
        required=frozenset(),
        missing=frozenset(
            {"human-review-required", "implementation-approved", "agent-ready", "spec-drafted"}
        ),
        kind="triage-needed",
        severity="advisory",
        summary="Not triaged: no lifecycle label, so no stage owns this item",
    ),
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ControlCenterError(message)


def _issue_labels(issue: Mapping[str, Any]) -> frozenset[str]:
    """Collect label names case-folded: GitHub's workflow ``contains()``
    comparison is case-insensitive, so ``Agent-Ready`` dispatches exactly like
    ``agent-ready`` and must evaluate the same here."""

    raw = issue.get("labels") or []
    _require(
        isinstance(raw, list),
        f"issue {issue.get('number')}: labels must be an array",
    )
    names: set[str] = set()
    for entry in raw:
        name = entry.get("name") if isinstance(entry, Mapping) else entry
        if isinstance(name, str) and name.strip():
            names.add(name.strip().lower())
    return frozenset(names)


def _declared_state(issue: Mapping[str, Any], labels: frozenset[str]) -> str:
    """Resolve a declared state, or return "" so the console reports unknown."""

    if issue.get("state") == "closed":
        reason = issue.get("state_reason")
        if not isinstance(reason, str):
            return ""
        return CLOSED_STATES.get(reason, "")
    for required, absent, state in LABEL_STATES:
        if required <= labels and not (absent & labels):
            return state
    return ""


def _kind(issue: Mapping[str, Any], labels: frozenset[str], has_children: bool) -> str:
    title = str(issue.get("title") or "")
    summary = issue.get("sub_issues_summary") or {}
    owns_children = has_children or bool(isinstance(summary, Mapping) and summary.get("total"))
    if EPIC_TITLE.match(title) or "epic" in labels:
        return "epic"
    if owns_children:
        return "story"
    return "task"


def backlog_document_from_issues(
    issues: Iterable[Mapping[str, Any]],
    *,
    project_id: str,
    repository: str,
    parents: Mapping[int, int] | None = None,
    source: str = "github-issues",
) -> dict[str, Any]:
    """Build a backlog document from GitHub issue payloads.

    ``parents`` maps a child issue number to its parent's, as returned by the
    sub-issue endpoints. Pull requests are skipped: GitHub returns them from the
    issues endpoint, but a change request is not a backlog item.
    """

    _require(bool(project_id), "project_id is required")
    _require(bool(repository), "repository is required")
    links = dict(parents or {})
    rows = [issue for issue in issues if "pull_request" not in issue]
    numbers = {int(issue["number"]) for issue in rows}
    parent_of_children = {parent for parent in links.values() if parent in numbers}

    items: list[dict[str, Any]] = []
    for issue in rows:
        number = int(issue["number"])
        labels = _issue_labels(issue)
        parent = links.get(number)
        item: dict[str, Any] = {
            "itemId": f"{repository}#{number}",
            "kind": _kind(issue, labels, number in parent_of_children),
            "title": str(issue.get("title") or f"issue {number}")[:512],
            "projectId": project_id,
            "issueRef": f"{repository}#{number}",
            "labels": sorted(labels),
        }
        if parent is not None:
            # Kept even when the parent is outside this export: the read model
            # roots the child and warns "parent outside this snapshot".
            item["parentId"] = f"{repository}#{parent}"
        state = _declared_state(issue, labels)
        if state:
            item["declaredState"] = state
        url = issue.get("html_url")
        if isinstance(url, str) and url.startswith("https://"):
            item["url"] = url
        updated = issue.get("updated_at")
        if isinstance(updated, str) and updated:
            item["updatedAt"] = updated
        items.append(item)

    _require(len(items) == len(rows), "every issue must produce exactly one backlog item")
    _require(
        len(items) <= MAX_BACKLOG_ITEMS,
        f"{len(items)} issues exceed the read model's {MAX_BACKLOG_ITEMS}-item budget; "
        "narrow the export (open issues only, or one project area at a time)",
    )
    return {"schemaVersion": BACKLOG_SCHEMA_VERSION, "source": source, "items": items}


def incidents_from_issues(
    issues: Iterable[Mapping[str, Any]],
    *,
    project_id: str,
    repository: str,
    observed_at: str,
) -> list[dict[str, Any]]:
    """Report, per open issue, which declared dispatch precondition is unmet."""

    incidents: list[dict[str, Any]] = []
    for issue in issues:
        if "pull_request" in issue or issue.get("state") == "closed":
            continue
        number = int(issue["number"])
        labels = _issue_labels(issue)
        for rule in DISPATCH_RULES:
            if not rule.matches(labels):
                continue
            incidents.append(
                {
                    "incidentId": f"{repository}#{number}:{rule.rule_id}",
                    "projectId": project_id,
                    "kind": rule.kind,
                    "severity": rule.severity,
                    "summary": rule.summary,
                    "detail": str(issue.get("title") or "")[:512],
                    "backlogItemId": f"{repository}#{number}",
                    "observedAt": observed_at,
                    "evidenceRef": f"{repository}#{number}",
                }
            )
            break
    return incidents


def incidents_from_change_requests(
    pulls: Iterable[Mapping[str, Any]],
    *,
    project_id: str,
    repository: str,
    observed_at: str,
    failing: Mapping[int, str] | None = None,
) -> list[dict[str, Any]]:
    """Report what is holding each open change request.

    ``failing`` maps a change-request number to a failing check's name, which
    the caller reads from whichever checks API it already has. Mergeability
    comes from the change request's own detail payload (GitHub's *list*
    endpoint omits ``mergeable_state``, so the caller fetches each one — the
    documented recipe does). Drafts are skipped before any classification: a
    work in progress is not a blocker. Only a verdict in
    :data:`READY_MERGE_STATES` reports "waiting on the merge authority":
    ``blocked``, ``behind``, ``unstable``, ``unknown`` and an absent field all
    mean something else is owed first, and unknown is never fabricated.
    """

    checks = dict(failing or {})
    incidents: list[dict[str, Any]] = []
    for pull in pulls:
        state_field = pull.get("state")
        if state_field is not None and state_field != "open":
            # A merged or closed change request is not an open problem,
            # whatever its final mergeability read.
            continue
        if pull.get("draft"):
            continue
        number = int(pull["number"])
        ref = f"{repository}!{number}"
        title = str(pull.get("title") or "")[:512]
        state = pull.get("mergeable_state")
        if state == "dirty":
            incidents.append(
                {
                    "incidentId": f"{ref}:merge-conflict",
                    "projectId": project_id,
                    "kind": "merge-conflict",
                    "severity": "blocker",
                    "summary": "Change request conflicts with its base branch",
                    "detail": title,
                    "observedAt": observed_at,
                    "evidenceRef": ref,
                }
            )
            continue
        if number in checks:
            incidents.append(
                {
                    "incidentId": f"{ref}:ci-failed",
                    "projectId": project_id,
                    "kind": "deterministic-ci-failed",
                    "severity": "blocker",
                    "summary": f"Deterministic verification failed: {checks[number]}"[:512],
                    "detail": title,
                    "observedAt": observed_at,
                    "evidenceRef": ref,
                }
            )
            continue
        if state in READY_MERGE_STATES:
            incidents.append(
                {
                    "incidentId": f"{ref}:awaiting-merge",
                    "projectId": project_id,
                    "kind": "awaiting-human-merge",
                    "severity": "advisory",
                    "summary": "Change request is ready and waiting on the human merge authority",
                    "detail": title,
                    "observedAt": observed_at,
                    "evidenceRef": ref,
                }
            )
            continue
        if isinstance(state, str) and state in IMPEDED_MERGE_STATES:
            incidents.append(
                {
                    "incidentId": f"{ref}:merge-state-{state}",
                    "projectId": project_id,
                    "kind": "unclassified",
                    "severity": "degraded",
                    "summary": (
                        f"Change request mergeability is {state}: {IMPEDED_MERGE_STATES[state]}"
                    ),
                    "detail": title,
                    "observedAt": observed_at,
                    "evidenceRef": ref,
                }
            )
    return incidents


def incident_document(
    *groups: Sequence[Mapping[str, Any]], source: str = "github"
) -> dict[str, Any]:
    """Combine incident groups into one document, rejecting duplicate ids."""

    incidents: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for group in groups:
        for incident in group:
            incident_id = str(incident.get("incidentId") or "")
            _require(bool(incident_id), "every incident requires an incidentId")
            _require(incident_id not in seen, f"duplicate incidentId: {incident_id}")
            seen.add(incident_id)
            incidents.append(incident)
    _require(
        len(incidents) <= MAX_INCIDENTS,
        f"{len(incidents)} incidents exceed the read model's {MAX_INCIDENTS}-incident "
        "budget; narrow the export",
    )
    return {
        "schemaVersion": INCIDENT_SCHEMA_VERSION,
        "source": source,
        "incidents": list(incidents),
    }


def parent_links(
    *groups: Iterable[Mapping[str, Any]] | tuple[Iterable[Mapping[str, Any]], int | None],
) -> dict[int, int]:
    """Map child issue number to parent issue number, from sub-issue payloads.

    Each group is either a payload (children carrying their own
    ``parent_issue_url``, which GitHub's sub-issue listing returns today) or a
    ``(payload, parent_number)`` pair naming the parent the listing was fetched
    for. Pass the explicit pair where you can: it does not depend on a response
    field the API documentation does not promise, and a child whose own
    reference disagrees with the declared parent is rejected rather than
    silently relinked.
    """

    links: dict[int, int] = {}

    def link(child: int, parent: int) -> None:
        _require(child != parent, f"issue {child} is listed as its own parent")
        existing = links.get(child)
        _require(
            existing is None or existing == parent,
            f"issue {child} is claimed by two parents: {existing} and {parent}",
        )
        links[child] = parent

    for group in groups:
        declared: int | None = None
        payload = group
        if (
            isinstance(group, tuple)
            and len(group) == 2
            and (group[1] is None or isinstance(group[1], int))
            and not isinstance(group[1], Mapping)
        ):
            # The explicit (payload, parent_number) pair; any other tuple is an
            # ordinary payload of child records.
            payload, declared = group
        for entry in payload:
            number = entry.get("number")
            if not isinstance(number, int):
                continue
            referenced: int | None = None
            url = entry.get("parent_issue_url")
            if isinstance(url, str):
                match = _PARENT_URL.search(url)
                if match is not None:
                    referenced = int(match.group(1))
            if declared is not None and referenced is not None:
                _require(
                    referenced == declared,
                    f"issue {number} references parent {referenced}, "
                    f"but the listing was declared for {declared}",
                )
            parent = declared if declared is not None else referenced
            if parent is not None:
                link(int(number), parent)
    return links
