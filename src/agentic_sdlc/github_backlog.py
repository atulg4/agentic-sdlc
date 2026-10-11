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
    ControlCenterError,
)

_PARENT_URL = re.compile(r"/issues/(\d+)/?$")

#: An epic is declared by title convention or by owning sub-issues.
EPIC_TITLE = re.compile(r"^\s*(?:[A-Z][A-Za-z]*\s+)?EPIC\b", re.I)

#: Label sets that declare a factory state, most specific first. The first row
#: whose ``required`` labels are all present and whose ``absent`` labels are all
#: missing decides the state.
LABEL_STATES: tuple[tuple[frozenset[str], frozenset[str], str], ...] = (
    (frozenset({"implementation-approved", "agent-ready"}), frozenset(), "approved"),
    (frozenset({"implementation-approved"}), frozenset({"agent-ready"}), "approved"),
    (frozenset({"spec-drafted"}), frozenset({"implementation-approved"}), "specified"),
    (frozenset({"human-review-required"}), frozenset({"spec-drafted"}), "triaged"),
)

#: GitHub's close reason, mapped to the terminal state it actually means.
CLOSED_STATES: Mapping[str, str] = {
    "completed": "merged",
    "not_planned": "cancelled",
    "duplicate": "superseded",
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
        rule_id="approved-without-agent-ready",
        required=frozenset({"implementation-approved", "human-review-required"}),
        missing=frozenset({"agent-ready"}),
        kind="dispatch-blocked",
        severity="blocker",
        summary="Approved for implementation but not labelled agent-ready, so no trigger fires",
    ),
    DispatchRule(
        rule_id="agent-ready-without-human-review",
        required=frozenset({"agent-ready"}),
        missing=frozenset({"human-review-required"}),
        kind="dispatch-blocked",
        severity="degraded",
        summary="Labelled agent-ready without human-review-required; both labels are required",
    ),
    DispatchRule(
        rule_id="spec-without-approval",
        required=frozenset({"spec-drafted", "human-review-required"}),
        missing=frozenset({"implementation-approved"}),
        kind="awaiting-human-approval",
        severity="degraded",
        summary="Specification drafted and waiting on a human implementation approval",
    ),
    DispatchRule(
        rule_id="untriaged",
        required=frozenset(),
        missing=frozenset(
            {"human-review-required", "implementation-approved", "agent-ready", "spec-drafted"}
        ),
        kind="awaiting-human-approval",
        severity="advisory",
        summary="Not triaged: no lifecycle label, so no stage owns this item",
    ),
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ControlCenterError(message)


def _issue_labels(issue: Mapping[str, Any]) -> frozenset[str]:
    raw = issue.get("labels") or []
    names: set[str] = set()
    for entry in raw:
        name = entry.get("name") if isinstance(entry, Mapping) else entry
        if isinstance(name, str) and name.strip():
            names.add(name.strip())
    return frozenset(names)


def _declared_state(issue: Mapping[str, Any], labels: frozenset[str]) -> str:
    """Resolve a declared state, or return "" so the console reports unknown."""

    if issue.get("state") == "closed":
        reason = issue.get("state_reason") or "completed"
        return CLOSED_STATES.get(str(reason), "")
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
        if parent is not None and parent in numbers:
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
    the caller reads from whichever checks API it already has. A conflicted
    change request is reported from GitHub's own ``mergeable_state``.
    """

    checks = dict(failing or {})
    incidents: list[dict[str, Any]] = []
    for pull in pulls:
        number = int(pull["number"])
        ref = f"{repository}!{number}"
        title = str(pull.get("title") or "")[:512]
        if pull.get("mergeable_state") == "dirty":
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
        if pull.get("draft"):
            continue
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
    return {
        "schemaVersion": INCIDENT_SCHEMA_VERSION,
        "source": source,
        "incidents": list(incidents),
    }


def parent_links(*payloads: Iterable[Mapping[str, Any]]) -> dict[int, int]:
    """Map child issue number to parent issue number, from sub-issue payloads.

    GitHub's sub-issue listing carries ``parent_issue_url`` on each child, so a
    parent link never has to be inferred from a title or a checklist.
    """

    links: dict[int, int] = {}
    for payload in payloads:
        for entry in payload:
            url = entry.get("parent_issue_url")
            number = entry.get("number")
            if not isinstance(url, str) or not isinstance(number, int):
                continue
            match = _PARENT_URL.search(url)
            if match is None:
                continue
            parent = int(match.group(1))
            child = int(number)
            if child == parent:
                continue
            existing = links.get(child)
            _require(
                existing is None or existing == parent,
                f"issue {child} is claimed by two parents: {existing} and {parent}",
            )
            links[child] = parent
    return links
