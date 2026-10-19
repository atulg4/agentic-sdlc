"""GitHub adapter: turn tracker payloads into Control Center inputs.

``control_center`` is provider-neutral on purpose: it joins a registry, a
lifecycle ledger, a backlog document and an incident document, and knows
nothing about any tracker. This module is one adapter that produces the last
two from GitHub REST payloads, so the console's data is reproducible from a
command instead of maintained by hand.

Every inference here is table-driven and documented, because a console that
guesses is worse than one that says *unknown*:

- :func:`label_states` maps a label set to a factory state, highest precedence
  first, for a project's configured label names (:class:`LifecycleLabels`;
  :data:`LABEL_STATES` is the default-named table). An issue matching no row
  carries no declared state at all, and the Control Center reports it as
  unknown rather than inventing ``intake``.
- :data:`CLOSED_STATES` maps GitHub's close reason to a terminal state. "Closed
  as completed" means completed; "closed as not planned" means cancelled.
- :func:`dispatch_rules` encodes the label preconditions Forge's own dispatch
  triggers require, again per configured names (:data:`DISPATCH_RULES` is the
  default table). A row that fires becomes an :class:`Incident`, which is how
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
    RELEASED_SPEC_RULE,
    SPEC_WITHOUT_APPROVAL_RULE,
    ControlCenterError,
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ControlCenterError(message)


#: API references are ANCHORED to GitHub's own origin: a trailing-path match
#: alone would let https://evil.example/repos/acme/forge/issues/1 pass the
#: ownership checks and supply hierarchy edges or repository identity.
_PARENT_URL = re.compile(r"/issues/(\d+)/?$")
_PARENT_REPOSITORY = re.compile(r"^https://api\.github\.com/repos/([^/\s]+/[^/\s]+)/issues/\d+/?$")
_REPOSITORY_URL = re.compile(r"^https://api\.github\.com/repos/([^/\s]+/[^/\s]+)/?$")
#: A GitHub issue's own web address, exactly: the exported link must identify
#: the validated repository and number, never an arbitrary https destination.
#: Pull records never reach the backlog, so /pull/ is NOT accepted: an issue
#: payload must not link its title to the same-numbered change request.
_ISSUE_URL = re.compile(
    r"https://github\.com/(?P<name>[A-Za-z0-9._-]+/[A-Za-z0-9._-]+)/issues/(?P<number>\d+)"
)

#: The OWNER/REPO shape every GitHub repository identifier must carry; a bare
#: or slashless value would generate references no registry entry or ledger
#: issue reference can ever match.
_REPOSITORY = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?/[A-Za-z0-9._-]+")


def _require_repository(repository: Any) -> None:
    _require(
        isinstance(repository, str) and bool(_REPOSITORY.fullmatch(repository)),
        f"repository must be an OWNER/REPO pair: {repository!r}",
    )


#: The only pull states GitHub emits; anything else is malformed provider data.
PULL_STATES = frozenset({"open", "closed"})

#: Issues carry the same two states as pulls.
ISSUE_STATES = PULL_STATES


def _record_number(record: Mapping[str, Any], label: str) -> int:
    """The record's number as a positive non-boolean integer, validated here
    in the adapter itself: a float, boolean or numeric string silently coerced
    by ``int()`` could attach items and incidents to a DIFFERENT real issue."""

    number = record.get("number")
    _require(
        isinstance(number, int) and not isinstance(number, bool) and number > 0,
        f"{label} number must be a positive integer: {number!r}",
    )
    return number


def _issue_repository(issue: Mapping[str, Any], repository: str) -> str:
    """The record's own repository spelling, REQUIRED to match ``repository``.

    Every GitHub issue payload carries ``repository_url``; without it the
    adapter has no evidence the record belongs to this repository at all, and
    a record naming another repository must not attach a same-numbered local
    issue, its hierarchy, or its incidents to this project.
    """

    url = issue.get("repository_url")
    number = issue.get("number")
    _require(
        isinstance(url, str) and bool(url.strip()),
        f"issue {number!r}: repository_url is required",
    )
    assert isinstance(url, str)
    match = _REPOSITORY_URL.search(url)
    _require(
        match is not None,
        f"issue {number!r}: repository_url is not a recognized repository URL: {url}",
    )
    assert match is not None
    name = match.group(1)
    _require(
        name.lower() == repository.lower(),
        f"issue {number!r} belongs to {name}, not repository {repository}",
    )
    return name


def canonical_repository(issues: Iterable[Mapping[str, Any]], declared: str) -> str:
    """The provider's own spelling of ``declared``, taken from the payloads.

    The read model joins references by exact string equality, so the export
    must emit the spelling the ledger records carry. Every record must carry
    a recognized ``repository_url`` matching ``declared`` (the builders
    enforce the same rule); the first record's spelling is canonical.
    """

    _require_repository(declared)
    canonical = declared
    found = False
    for issue in issues:
        name = _issue_repository(issue, declared)
        if not found:
            canonical = name
            found = True
    return canonical


def _issue_state(issue: Mapping[str, Any]) -> str:
    """The issue's explicit state, or a controlled error: a record without
    "open" or "closed" must never silently classify as open."""

    state = issue.get("state")
    _require(
        isinstance(state, str) and state in ISSUE_STATES,
        f'issue {issue.get("number")}: state must be "open" or "closed"',
    )
    return state


#: An epic is declared by title convention or by owning sub-issues.
#: Case-sensitive on the marker: an ordinary sentence mentioning "epic"
#: ("Fix epic timeout handling") is not a hierarchy declaration. The
#: documented conventions are an all-caps EPIC marker ("EPIC: ...",
#: "EPIC P1: ...") or a capitalized "Epic:" with its colon, each optionally
#: after one project-prefix word ("FORGE Epic: ...").
EPIC_TITLE = re.compile(r"^\s*(?:[A-Z][A-Za-z0-9]*\s+)?(?:EPIC\b|Epic:)")

#: Label sets that declare a factory state, most specific first. The first row
#: whose ``required`` labels are all present and whose ``absent`` labels are all
#: missing decides the state.
#: The platform-fixed spec stage labels; the policy does not rename these.
SPEC_DRAFTED_LABEL = "spec-drafted"
SPEC_APPROVED_LABEL = "spec-approved"


@dataclass(frozen=True)
class LifecycleLabels:
    """The automation label names a project's policy configures.

    ``agentic-sdlc.toml`` can rename the ready, human-review and
    implementation labels (``ready_label``, ``human_review_label``,
    ``implementation_label``); the generated workflows then fire on those
    names, so the adapter must evaluate the same names rather than the
    defaults. Names are compared casefolded, matching GitHub's own
    ``contains()`` comparison, and must stay distinct from each other and
    from the platform-fixed spec labels.
    """

    ready: str = "agent-ready"
    human_review: str = "human-review-required"
    implementation: str = "implementation-approved"

    def __post_init__(self) -> None:
        names = []
        for field in ("ready", "human_review", "implementation"):
            value = getattr(self, field)
            _require(
                isinstance(value, str) and bool(value.strip()),
                f"lifecycle label {field} must be a non-empty string",
            )
            object.__setattr__(self, field, value.strip().lower())
            names.append(getattr(self, field))
        names.extend((SPEC_DRAFTED_LABEL, SPEC_APPROVED_LABEL))
        _require(
            len(set(names)) == len(names),
            "lifecycle labels must be distinct from each other and from the spec labels",
        )


DEFAULT_LABELS = LifecycleLabels()


def label_states(
    labels: LifecycleLabels = DEFAULT_LABELS,
) -> tuple[tuple[frozenset[str], frozenset[str], str], ...]:
    """The declared-state table for a project's configured label names."""

    return (
        # An unreleased spec wins over a stale approval: the spec stage
        # re-drafts without clearing the implementation approval, and the
        # safety gate blocks planning and implementation until the draft is
        # released.
        (frozenset({SPEC_DRAFTED_LABEL}), frozenset({SPEC_APPROVED_LABEL}), "specified"),
        (frozenset({labels.implementation, labels.ready}), frozenset(), "approved"),
        (frozenset({labels.implementation}), frozenset({labels.ready}), "approved"),
        (frozenset({SPEC_DRAFTED_LABEL}), frozenset({labels.implementation}), "specified"),
        (frozenset({labels.human_review}), frozenset({SPEC_DRAFTED_LABEL}), "triaged"),
    )


LABEL_STATES: tuple[tuple[frozenset[str], frozenset[str], str], ...] = label_states()

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
#: ``behind`` is precise enough to carry its own kind (branch-behind) with a
#: branch-refresh remediation; the rest surface as incidents the operator must
#: classify. Only an absent or ``unknown`` verdict is suppressed, because
#: unknown is never fabricated.
IMPEDED_MERGE_STATES: Mapping[str, str] = {
    "blocked": "a required review or required check is outstanding",
    "behind": "the head branch is out of date with its base",
    "unstable": "a status check on the head commit is not passing",
}

#: Every mergeability verdict this adapter understands. ``unknown`` and an
#: absent field are deliberately unknown; anything else is malformed or newly
#: introduced provider data that must fail closed rather than silently drop
#: the change request from the snapshot.
KNOWN_MERGE_STATES = (
    frozenset({"dirty", "unknown"}) | READY_MERGE_STATES | frozenset(IMPEDED_MERGE_STATES)
)


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


def dispatch_rules(labels: LifecycleLabels = DEFAULT_LABELS) -> tuple[DispatchRule, ...]:
    """The label preconditions the bundled ``agent-auto-plan`` and
    ``agent-auto-implement`` triggers enforce, evaluated against a project's
    configured label names. Each unmet row is why an approved item is sitting
    still, which is precisely what an operator needs told."""

    ready = labels.ready
    review = labels.human_review
    approve = labels.implementation
    return (
        DispatchRule(
            rule_id="spec-awaiting-human-review",
            required=frozenset({SPEC_DRAFTED_LABEL}),
            missing=frozenset({SPEC_APPROVED_LABEL}),
            kind="awaiting-spec-review",
            severity="degraded",
            summary=(
                "Specification draft held for human spec review: planning and "
                f"implementation stay blocked until {SPEC_DRAFTED_LABEL} is removed or "
                f"{SPEC_APPROVED_LABEL} is added"
            ),
        ),
        DispatchRule(
            rule_id="approved-without-dispatch-labels",
            required=frozenset({approve}),
            missing=frozenset({ready, review}),
            kind="dispatch-blocked",
            severity="blocker",
            summary=(
                f"Approved for implementation but carrying neither {ready} nor "
                f"{review}, so no trigger fires"
            ),
        ),
        DispatchRule(
            rule_id="approved-without-agent-ready",
            required=frozenset({approve, review}),
            missing=frozenset({ready}),
            kind="dispatch-blocked",
            severity="blocker",
            summary=f"Approved for implementation but not labelled {ready}, so no trigger fires",
        ),
        DispatchRule(
            rule_id="approved-without-human-review",
            required=frozenset({approve, ready}),
            missing=frozenset({review}),
            kind="dispatch-blocked",
            severity="blocker",
            summary=(
                f"Approved and {ready} but missing {review}, which both "
                "bundled triggers require, so nothing fires"
            ),
        ),
        DispatchRule(
            rule_id="agent-ready-without-human-review",
            required=frozenset({ready}),
            missing=frozenset({review}),
            kind="dispatch-blocked",
            severity="degraded",
            summary=f"Labelled {ready} without {review}; both labels are required",
        ),
        # The symmetric loss: review-labelled work that lost (or never got)
        # the ready label is just as stalled, because both bundled triggers
        # require the pair. Spec-drafted items are the spec rules' concern.
        DispatchRule(
            rule_id="review-required-without-agent-ready",
            required=frozenset({review}),
            missing=frozenset({ready, approve, SPEC_DRAFTED_LABEL}),
            kind="dispatch-blocked",
            severity="degraded",
            summary=f"Labelled {review} without {ready}; both bundled triggers require the pair",
        ),
        # With the ready label also present, the plan-ordering advisory below
        # is the actionable message, so this row yields to it.
        DispatchRule(
            rule_id=SPEC_WITHOUT_APPROVAL_RULE,
            required=frozenset({SPEC_DRAFTED_LABEL, review}),
            missing=frozenset({approve, ready}),
            kind="awaiting-human-approval",
            severity="degraded",
            summary=(
                "Specification drafted and waiting on a human implementation "
                f"approval: dispatch needs {approve} and {ready}"
            ),
        ),
        DispatchRule(
            rule_id="eligible-awaiting-dispatch-evidence",
            required=frozenset({approve, review, ready}),
            missing=frozenset(),
            kind="awaiting-dispatch-evidence",
            severity="advisory",
            summary=(
                "Every dispatch label is present, but the bundled implementation trigger "
                f"fires only on the {approve} label event: confirm a run "
                f"started, and re-apply {approve} to re-kick if none did"
            ),
        ),
        # An unreleased draft is caught by the spec-review rule above, so only
        # the implementation approval blocks this row: a released spec (drafted
        # plus approved) still has the plan-trigger ordering hazard.
        DispatchRule(
            rule_id=PLAN_EVIDENCE_RULE,
            required=frozenset({ready, review}),
            missing=frozenset({approve}),
            kind="awaiting-dispatch-evidence",
            severity="advisory",
            summary=(
                "Both planning labels are present, but the bundled plan trigger fires "
                f"only on the {ready} label event: confirm a plan run started, and "
                f"re-apply {ready} to re-kick if none did; implementation dispatch "
                f"then fires on the {approve} label event"
            ),
        ),
        DispatchRule(
            rule_id=RELEASED_SPEC_RULE,
            required=frozenset({SPEC_DRAFTED_LABEL, SPEC_APPROVED_LABEL}),
            missing=frozenset({approve, ready, review}),
            kind="awaiting-human-approval",
            severity="degraded",
            summary=(
                "Specification released, but no dispatch label follows it: the work "
                f"waits on {approve} with {ready} and {review}"
            ),
        ),
        DispatchRule(
            rule_id="untriaged",
            required=frozenset(),
            missing=frozenset({review, approve, ready, SPEC_DRAFTED_LABEL}),
            kind="triage-needed",
            severity="advisory",
            summary="Not triaged: no lifecycle label, so no stage owns this item",
        ),
    )


DISPATCH_RULES: tuple[DispatchRule, ...] = dispatch_rules()


def _issue_labels(issue: Mapping[str, Any]) -> frozenset[str]:
    """Collect label names case-folded: GitHub's workflow ``contains()``
    comparison is case-insensitive, so ``Agent-Ready`` dispatches exactly like
    ``agent-ready`` and must evaluate the same here."""

    raw = issue.get("labels")
    # The GitHub export contract always supplies the array - [] for a
    # genuinely unlabeled issue - so absence or null is a partial payload,
    # and reading it as "unlabeled" would fabricate a triage diagnosis.
    _require(
        isinstance(raw, list),
        f"issue {issue.get('number')}: labels must be an array ([] when unlabeled)",
    )
    names: set[str] = set()
    for entry in raw:
        name = entry.get("name") if isinstance(entry, Mapping) else entry
        _require(
            isinstance(name, str) and bool(name.strip()),
            f"issue {issue.get('number')}: every label must be a string or carry a string name",
        )
        names.add(name.strip().lower())
    return frozenset(names)


def _declared_state(
    issue: Mapping[str, Any],
    labels: frozenset[str],
    states: tuple[tuple[frozenset[str], frozenset[str], str], ...],
) -> str:
    """Resolve a declared state, or return "" so the console reports unknown."""

    if _issue_state(issue) == "closed":
        reason = issue.get("state_reason")
        if not isinstance(reason, str):
            return ""
        return CLOSED_STATES.get(reason, "")
    for required, absent, state in states:
        if required <= labels and not (absent & labels):
            return state
    return ""


def _is_pull_marker(issue: Mapping[str, Any]) -> bool:
    """Whether the record carries GitHub's real pull-request marker.

    The provider emits the key only on pull requests, as an object. A null or
    string value is a transformed payload, and treating it as the marker
    would silently drop the record from the backlog - a no-drop violation -
    so a malformed marker is a controlled error.
    """
    if "pull_request" not in issue:
        return False
    marker = issue.get("pull_request")
    _require(
        isinstance(marker, Mapping),
        f"issue {issue.get('number')}: pull_request must be an object when present",
    )
    return True


def _title(record: Mapping[str, Any], label: str) -> str:
    """The record's title: a string or absent, never coerced from another type.

    ``str()`` on an object would serialize nested provider fields -
    potentially credential-bearing - into the snapshot and the shared store,
    bypassing the document loaders' key checks.
    """
    value = record.get("title")
    _require(value is None or isinstance(value, str), f"{label}: title must be a string")
    return value or ""


def _kind(issue: Mapping[str, Any], labels: frozenset[str], has_children: bool) -> str:
    title = _title(issue, f"issue {issue.get('number')}")
    summary = issue.get("sub_issues_summary")
    total = 0
    if summary is not None:
        # Fail closed like every other malformed field: a truthy non-count
        # ("0", true, -1) must not silently classify a childless issue as a
        # story and corrupt the exported hierarchy.
        number = issue.get("number")
        _require(
            isinstance(summary, Mapping),
            f"issue {number!r}: sub_issues_summary must be an object",
        )
        assert isinstance(summary, Mapping)
        raw_total = summary.get("total", 0)
        _require(
            type(raw_total) is int and raw_total >= 0,
            f"issue {number!r}: sub_issues_summary.total must be a non-negative "
            f"integer: {raw_total!r}",
        )
        total = raw_total
    owns_children = has_children or total > 0
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
    labels: LifecycleLabels | None = None,
) -> dict[str, Any]:
    """Build a backlog document from GitHub issue payloads.

    ``parents`` maps a child issue number to its parent's, as returned by the
    sub-issue endpoints. Pull requests are skipped: GitHub returns them from the
    issues endpoint, but a change request is not a backlog item. ``labels``
    carries the project's configured lifecycle label names; the defaults match
    the platform's.
    """

    _require(bool(project_id), "project_id is required")
    _require_repository(repository)
    states = label_states(labels or DEFAULT_LABELS)
    links = dict(parents or {})
    for child, parent in links.items():
        # Key equality would let True or 1.0 silently attach the real issue
        # #1, so direct callers get the same validation parent_links applies.
        for value, role in ((child, "child"), (parent, "parent")):
            _require(
                isinstance(value, int) and not isinstance(value, bool) and value > 0,
                f"parents map {role} must be a positive integer: {value!r}",
            )
    issues = list(issues)
    for issue in issues:
        _issue_repository(issue, repository)
    rows = [issue for issue in issues if not _is_pull_marker(issue)]
    numbers = {_record_number(issue, "issue") for issue in rows}
    # No-drop: a sub-issue listing naming a child the issues payload does not
    # carry would silently vanish from the export, concealing a partial
    # issues file, so it is a controlled error rather than a dropped edge.
    missing_children = sorted(child for child in links if child not in numbers)
    _require(
        not missing_children,
        "sub-issue children missing from the issues export: "
        + ", ".join(f"#{child}" for child in missing_children)
        + "; export the children too, or narrow --sub-issues",
    )
    parent_of_children = {parent for parent in links.values() if parent in numbers}

    items: list[dict[str, Any]] = []
    for issue in rows:
        number = _record_number(issue, "issue")
        labels = _issue_labels(issue)
        parent = links.get(number)
        item: dict[str, Any] = {
            "itemId": f"{repository}#{number}",
            "kind": _kind(issue, labels, number in parent_of_children),
            "title": (_title(issue, f"issue {number}") or f"issue {number}")[:512],
            "projectId": project_id,
            "issueRef": f"{repository}#{number}",
            "labels": sorted(labels),
        }
        if parent is not None:
            # Kept even when the parent is outside this export: the read model
            # roots the child and warns "parent outside this snapshot".
            item["parentId"] = f"{repository}#{parent}"
        state = _declared_state(issue, labels, states)
        if state:
            item["declaredState"] = state
        url = issue.get("html_url")
        if url is not None and url != "":
            # The console turns the issue title into this link, so the URL
            # must IDENTIFY the validated issue: a payload carrying a correct
            # repository_url and number but a foreign html_url could
            # otherwise present any site as the local issue.
            _require(isinstance(url, str), f"issue {number!r}: html_url must be a string")
            assert isinstance(url, str)
            match = _ISSUE_URL.fullmatch(url.strip())
            _require(
                match is not None,
                f"issue {number!r}: html_url is not a recognized GitHub issue URL: {url}",
            )
            assert match is not None
            _require(
                match.group("name").lower() == repository.lower()
                and int(match.group("number")) == number,
                f"issue {number!r}: html_url does not identify {repository}#{number}: {url}",
            )
            item["url"] = url.strip()
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
    labels: LifecycleLabels | None = None,
) -> list[dict[str, Any]]:
    """Report, per open issue, which declared dispatch precondition is unmet.

    ``labels`` carries the project's configured lifecycle label names; the
    defaults match the platform's."""

    _require_repository(repository)
    rules = dispatch_rules(labels or DEFAULT_LABELS)
    incidents: list[dict[str, Any]] = []
    for issue in issues:
        _issue_repository(issue, repository)
        if _is_pull_marker(issue) or _issue_state(issue) == "closed":
            continue
        number = _record_number(issue, "issue")
        issue_labels = _issue_labels(issue)
        for rule in rules:
            if not rule.matches(issue_labels):
                continue
            incidents.append(
                {
                    "incidentId": f"{repository}#{number}:{rule.rule_id}",
                    "projectId": project_id,
                    "kind": rule.kind,
                    "severity": rule.severity,
                    "summary": rule.summary,
                    "detail": _title(issue, f"issue {number}")[:512],
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

    _require_repository(repository)
    checks = dict(failing or {})
    for key, name in checks.items():
        # Dictionary equality would let True or 1.0 match change request 1,
        # fabricating a failing-check blocker _record_number already rejects.
        _require(
            isinstance(key, int) and not isinstance(key, bool) and key > 0,
            f"failing-check key must be a positive integer: {key!r}",
        )
        # The name is interpolated into an incident summary: a non-string
        # value would be coerced whole (nested payloads, credentials and
        # all), and a blank one would fabricate a CI blocker with no named
        # check, so both fail closed before any formatting.
        _require(
            isinstance(name, str) and bool(name.strip()),
            f"failing-check name for change request {key} must be a non-empty string",
        )
        _require(
            len(name) <= 512,
            f"change request {key}: too many failing check names to record; narrow --failing",
        )
    incidents: list[dict[str, Any]] = []
    # One record per change request: a concatenated export carrying the same
    # number twice (say once dirty, once clean) would otherwise emit
    # contradictory incidents under distinct ids, which the later
    # duplicate-id check cannot catch.
    seen_numbers: set[int] = set()
    for pull in pulls:
        state_field = pull.get("state")
        # The state must be explicit: without it the adapter has no evidence
        # the change request is still open, so it never classifies one.
        _require(
            isinstance(state_field, str) and state_field in PULL_STATES,
            f'change request {pull.get("number")}: state must be "open" or "closed"',
        )
        # Numbers are validated and recorded BEFORE the closed/draft skips, so
        # a contradictory duplicate cannot hide behind a skipped record.
        number = _record_number(pull, "change request")
        _require(
            number not in seen_numbers,
            f"change request {number} appears more than once in the pulls export",
        )
        seen_numbers.add(number)
        if state_field != "open":
            # A merged or closed change request is not an open problem,
            # whatever its final mergeability read.
            continue
        draft = pull.get("draft")
        # GitHub's pull-detail schema defines draft as a boolean; a partial
        # payload without it carries no evidence the pull is ready for
        # review, so it must not classify as non-draft.
        _require(
            isinstance(draft, bool),
            f"change request {pull.get('number')}: draft must be a boolean",
        )
        if draft:
            continue
        # A detail payload names its own target repository, and the adapter
        # REQUIRES it: without base.repo.full_name there is no evidence the
        # record belongs to this repository at all, and one naming another
        # repository must not attribute its blockers to a same-numbered
        # local change request.
        base = pull.get("base")
        base_repo = base.get("repo") if isinstance(base, Mapping) else None
        full_name = base_repo.get("full_name") if isinstance(base_repo, Mapping) else None
        _require(
            isinstance(full_name, str) and bool(full_name.strip()),
            f"change request {number}: base.repo.full_name is required",
        )
        assert isinstance(full_name, str)
        _require(
            full_name.lower() == repository.lower(),
            f"change request {number} belongs to {full_name}, not repository {repository}",
        )
        # The caller's canonical spelling, which every other emitted id uses
        # (issue item ids, label incident ids): the read model joins
        # change-request references by EXACT string equality, so one export
        # must never mix the detail payload's casing into refs the backlog
        # and ledger spell canonically. The payload's own full_name was
        # validated against it case-insensitively above.
        ref = f"{repository}!{number}"
        title = _title(pull, f"change request {number}")[:512]
        state = pull.get("mergeable_state")
        _require(
            state is None or isinstance(state, str),
            f"change request {number}: mergeable_state must be a string",
        )
        _require(
            state is None or state in KNOWN_MERGE_STATES,
            f"change request {number}: unrecognized mergeable_state: {state!r}",
        )
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
            # No continue: a conflicted pull can also carry a declared CI
            # failure, and both blockers are real.
        if number in checks:
            summary = f"Deterministic verification failed: {checks[number]}"
            _require(
                len(summary) <= 512,
                f"change request {number}: too many failing check names to record; "
                "narrow --failing",
            )
            incidents.append(
                {
                    "incidentId": f"{ref}:ci-failed",
                    "projectId": project_id,
                    "kind": "deterministic-ci-failed",
                    "severity": "blocker",
                    "summary": summary,
                    "detail": title,
                    "observedAt": observed_at,
                    "evidenceRef": ref,
                }
            )
            # No continue: the branch can also be behind or otherwise impeded,
            # and both remediation requirements are real.
        if state == "dirty":
            continue
        if number in checks and state in READY_MERGE_STATES:
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
        if state == "unstable" and number in checks:
            # "unstable" means a status check is failing, which the explicit
            # failing-check incident already names; reporting both would
            # double-count one condition.
            continue
        if isinstance(state, str) and state in IMPEDED_MERGE_STATES:
            incidents.append(
                {
                    "incidentId": f"{ref}:merge-state-{state}",
                    "projectId": project_id,
                    # "behind" is already a precise diagnosis with one known
                    # fix, so it carries its own kind and the branch-refresh
                    # remediation instead of asking the operator to classify.
                    "kind": "branch-behind" if state == "behind" else "unclassified",
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
    repository: str | None = None,
) -> dict[int, int]:
    """Map child issue number to parent issue number, from sub-issue payloads.

    Each group is either a payload (children carrying their own
    ``parent_issue_url``, which GitHub's sub-issue listing returns today) or a
    ``(payload, parent_number)`` pair naming the parent the listing was fetched
    for. Pass the explicit pair where you can: it does not depend on a response
    field the API documentation does not promise, and a child whose own
    reference disagrees with the declared parent is rejected rather than
    silently relinked.

    ``repository`` pins the export's repository: GitHub supports sub-issues
    from OTHER repositories, and such a child's bare number could collide with
    an unrelated local issue, so with the repository given every record must
    PROVE it is local with a recognized matching ``repository_url`` - a record
    without one is rejected, not presumed local, because a partial foreign
    payload could otherwise reparent the same-numbered local issue - and a
    ``parent_issue_url`` naming a different repository is rejected too.
    """

    links: dict[int, int] = {}

    def require_local(entry: Mapping[str, Any], number: int) -> None:
        if repository is None:
            return
        _issue_repository(entry, repository)
        url = entry.get("parent_issue_url")
        if isinstance(url, str):
            match = _PARENT_REPOSITORY.search(url)
            if match is not None and match.group(1).lower() != repository.lower():
                _require(
                    False,
                    f"sub-issue record {number} belongs to another repository "
                    f"({match.group(1)}); cross-repository sub-issues are not "
                    "supported by this export",
                )

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
        if isinstance(group, tuple) and len(group) == 2 and not isinstance(group[1], Mapping):
            # The explicit (payload, parent_number) pair; any other tuple is an
            # ordinary payload of child records. The declared parent passes
            # the same validation as every child number: True would alias
            # issue #1 and corrupt the tree rather than reject the link.
            payload, declared = group
            _require(
                declared is None
                or (isinstance(declared, int) and not isinstance(declared, bool) and declared > 0),
                f"declared parent number must be a positive integer: {declared!r}",
            )
        for entry in payload:
            _require(isinstance(entry, Mapping), "every sub-issue record must be an object")
            # The shared validator, not a type check: bool is an int, so a
            # record with number true would alias issue #1.
            number = _record_number(entry, "sub-issue record")
            require_local(entry, number)
            referenced: int | None = None
            url = entry.get("parent_issue_url")
            if isinstance(url, str):
                # Only the recognized API shape may contribute a parent
                # number: a web URL like https://github.com/other/repo/
                # issues/1 would otherwise bypass the repository check while
                # its trailing number reparents the child to local issue #1.
                _require(
                    _PARENT_REPOSITORY.search(url) is not None,
                    f"sub-issue record {number}: parent_issue_url is not a "
                    f"recognized GitHub issue URL: {url}",
                )
                match = _PARENT_URL.search(url)
                if match is not None and int(match.group(1)) > 0:
                    referenced = int(match.group(1))
            if declared is not None and referenced is not None:
                _require(
                    referenced == declared,
                    f"issue {number} references parent {referenced}, "
                    f"but the listing was declared for {declared}",
                )
            if declared is None:
                # A bare listing was supplied *as* sub-issue data: a record
                # that yields no usable parent would silently drop the edge
                # and render the child as a root, so it is a controlled error.
                _require(
                    referenced is not None,
                    f"sub-issue record {number} carries no usable parent_issue_url; "
                    "pass the listing as a PARENT=path pair or fix the export",
                )
            parent = declared if declared is not None else referenced
            if parent is not None:
                link(int(number), parent)
    return links
