"""Control Center read model: one snapshot of every project Forge manages.

The Control Center UI is a *view*. It never re-derives lifecycle state from a
provider API, and it holds no credential. This module builds the single
document the UI renders, by joining three canonical sources that already exist:

- ``project_registry`` — which projects exist, and what they are allowed to do.
- ``event_ledger`` — what happened to each work unit, projected deterministically.
- a **backlog document** — the declared epic/story/task structure of a project,
  produced by whichever intake adapter owns that project's tracker.

Two rules shape everything below:

- **Unknown is never fabricated.** A backlog item with no ledger events and no
  declared state reports ``unknown`` with an explicit basis string, never a
  guessed stage.
- **Nothing is dropped.** Data that arrives for a project the registry does not
  declare still appears, flagged ``registered: false``, because an operator
  cannot remediate what the console hides.

Execution problems are carried as :class:`Incident` records. Each incident kind
maps to declared :class:`RemediationAction` descriptors in
:data:`REMEDIATION_CATALOGUE`. A descriptor *describes* a remediation and names
the authority that may run it; this module executes nothing and mutates
nothing, so a console built on it stays read-only by construction.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .event_ledger import (
    FactoryState,
    LifecycleStage,
    WorkUnitProjection,
)
from .project_registry import (
    EMPTY_PROJECT_REGISTRY,
    ProjectRegistry,
    read_optional_project_registry,
)

CONTROL_CENTER_SCHEMA_VERSION = 1
BACKLOG_SCHEMA_VERSION = 1
INCIDENT_SCHEMA_VERSION = 1

STATE_BASIS_LEDGER = "state projected from the lifecycle ledger"
STATE_BASIS_DECLARED = "state declared by the backlog source"
STATE_BASIS_UNKNOWN = "state unknown: no ledger events and no declared state"

MAX_TEXT_LENGTH = 512
MAX_BACKLOG_ITEMS = 5000
MAX_INCIDENTS = 2000

#: A tracker reference charset: ``owner/repo#123`` and ``owner/repo!123`` both
#: fit, so a GitHub issue and a GitLab merge request need no re-encoding.
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/#!:-]{0,127}")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")
_SECRET_KEY = re.compile(
    r"credential|secret|token|password|passphrase|api_?key|authorization|bearer|private_?key",
    re.I,
)

_FACTORY_STATES = frozenset(item.value for item in FactoryState)
_LIFECYCLE_STAGES = frozenset(item.value for item in LifecycleStage)

#: States where the factory has stopped and is waiting on a person.
AWAITING_HUMAN_STATES = frozenset(
    {FactoryState.APPROVAL_PENDING, FactoryState.READY_FOR_HUMAN_MERGE}
)

#: States in which something is executing *right now*. Backlog-shaped states
#: (intake through approved) and settled ones (merged, live) are deliberately
#: excluded: ``live`` is not terminal — a rollback stays possible — but a live
#: unit is not running work, and counting it would pin a project at "running"
#: forever.
EXECUTING_STATES = frozenset(
    {
        FactoryState.DISPATCHED,
        FactoryState.IMPLEMENTING,
        FactoryState.VERIFYING,
        FactoryState.REVIEWING,
        FactoryState.REPAIR_NEEDED,
        FactoryState.REPAIRING,
        FactoryState.RE_REVIEWING,
        FactoryState.DEPLOYING,
        FactoryState.DEPLOYMENT_VERIFYING,
        FactoryState.ROLLING_BACK,
    }
)


class ControlCenterError(ValueError):
    """Raised when a Control Center input is malformed or inconsistent."""


class BacklogKind(StrEnum):
    """The declared shape of a backlog item, from coarsest to finest."""

    EPIC = "epic"
    STORY = "story"
    TASK = "task"


class IncidentSeverity(StrEnum):
    """How much an execution problem costs the project right now."""

    BLOCKER = "blocker"
    DEGRADED = "degraded"
    ADVISORY = "advisory"


class RemediationAuthority(StrEnum):
    """Who is allowed to carry out a remediation."""

    OPERATOR = "operator"
    AGENT = "agent"
    HUMAN_ONLY = "human-only"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ControlCenterError(message)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    _require(isinstance(value, Mapping), f"{label} must be an object")
    assert isinstance(value, Mapping)
    for key in value:
        _require(isinstance(key, str), f"{label} keys must be strings")
        _require(
            not _SECRET_KEY.search(key),
            f"{label} must not carry a credential-bearing field: {key}",
        )
    return value


def _known_keys(entry: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(entry) - allowed)
    _require(not unknown, f"{label} has unknown keys: {', '.join(unknown)}")


def _text(entry: Mapping[str, Any], key: str, label: str, *, required: bool = False) -> str:
    raw = entry.get(key, "")
    if raw is None:
        raw = ""
    _require(isinstance(raw, str), f"{label}.{key} must be a string")
    assert isinstance(raw, str)
    value = raw.strip()
    _require(not required or value, f"{label}.{key} is required")
    _require(
        len(value) <= MAX_TEXT_LENGTH,
        f"{label}.{key} must be at most {MAX_TEXT_LENGTH} characters",
    )
    return value


def _identifier(entry: Mapping[str, Any], key: str, label: str, *, required: bool = True) -> str:
    value = _text(entry, key, label, required=required)
    if not value:
        return ""
    _require(bool(_ID.fullmatch(value)), f"{label}.{key} is not a valid identifier: {value}")
    return value


def _timestamp(entry: Mapping[str, Any], key: str, label: str, *, required: bool = False) -> str:
    value = _text(entry, key, label, required=required)
    if not value:
        return ""
    _require(
        bool(_TIMESTAMP.fullmatch(value)),
        f"{label}.{key} must be an RFC 3339 timestamp: {value}",
    )
    return value


def _string_tuple(value: Any, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    _require(
        isinstance(value, Sequence) and not isinstance(value, (str, bytes)),
        f"{label} must be an array of strings",
    )
    assert isinstance(value, Sequence)
    values: list[str] = []
    for index, item in enumerate(value):
        _require(isinstance(item, str), f"{label}[{index}] must be a string")
        assert isinstance(item, str)
        entry = item.strip()
        _require(bool(entry), f"{label}[{index}] must not be empty")
        _require(
            len(entry) <= MAX_TEXT_LENGTH,
            f"{label}[{index}] must be at most {MAX_TEXT_LENGTH} characters",
        )
        if entry not in values:
            values.append(entry)
    return tuple(values)


def _digest(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class RemediationAction:
    """A declared way to clear one class of execution problem.

    The descriptor is documentation the console can render and an operator can
    run elsewhere. ``authority`` says who may run it, so a console never offers
    a human-only step as if it were automatable.
    """

    action_id: str
    title: str
    description: str
    authority: RemediationAuthority
    command: str = ""
    handoff: str = ""

    def __post_init__(self) -> None:
        _require(bool(_ID.fullmatch(self.action_id)), f"invalid action id: {self.action_id}")
        _require(bool(self.title), f"{self.action_id} requires a title")
        _require(bool(self.description), f"{self.action_id} requires a description")
        if self.authority is RemediationAuthority.HUMAN_ONLY:
            _require(bool(self.handoff), f"{self.action_id} is human-only and requires a handoff")
        _require(
            not _SECRET_KEY.search(self.command),
            f"{self.action_id} command must not carry a credential",
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "actionId": self.action_id,
            "title": self.title,
            "description": self.description,
            "authority": self.authority.value,
            "command": self.command,
            "handoff": self.handoff,
        }


def _action(
    action_id: str,
    title: str,
    description: str,
    authority: RemediationAuthority,
    *,
    command: str = "",
    handoff: str = "",
) -> RemediationAction:
    return RemediationAction(
        action_id=action_id,
        title=title,
        description=description,
        authority=authority,
        command=command,
        handoff=handoff,
    )


_OPERATOR = RemediationAuthority.OPERATOR
_AGENT = RemediationAuthority.AGENT
_HUMAN = RemediationAuthority.HUMAN_ONLY

#: Declared remediations per incident kind. Adding a kind here is the only way
#: a console learns to offer an action for it, which keeps the action surface
#: reviewable instead of improvised per incident.
REMEDIATION_CATALOGUE: Mapping[str, tuple[RemediationAction, ...]] = {
    "runner-capacity": (
        _action(
            "inspect-runner-pool",
            "Inspect the runner pool",
            "Read current runner capacity and the queue depth that is waiting on it.",
            _OPERATOR,
            command="sdlcctl capacity-report --ledger usage-ledger.json --output capacity.json",
        ),
        _action(
            "scale-runner-pool",
            "Scale the runner pool",
            "Raise the pool's minimum replicas so queued work units get a worker.",
            _HUMAN,
            handoff=(
                "Deployment capacity is owner-controlled: change the runner scale set and apply it."
            ),
        ),
    ),
    "quota-exhausted": (
        _action(
            "route-to-fallback-executor",
            "Route to a fallback executor",
            "Re-run routing so the work unit lands on an executor that still has quota.",
            _OPERATOR,
            command="sdlcctl route-executor --registry executors.json --policy routing.json",
        ),
        _action(
            "wait-for-quota-reset",
            "Wait for the quota window to reset",
            "Quota exhaustion is recoverable; the unit resumes when the window rolls over.",
            _OPERATOR,
        ),
    ),
    "dispatch-blocked": (
        _action(
            "complete-dispatch-preconditions",
            "Complete the dispatch preconditions",
            "Dispatch is gated on declared labels and an approval; supply the missing one.",
            _HUMAN,
            handoff="Apply the approval label the mission requires, or record the approval event.",
        ),
        _action(
            "re-run-dispatch",
            "Re-run mission dispatch",
            "Once preconditions hold, re-dispatch the mission for this work unit.",
            _OPERATOR,
            command="sdlcctl dispatch-mission --registry missions.json --task task.json",
        ),
    ),
    "deterministic-ci-failed": (
        _action(
            "reproduce-ci-failure",
            "Reproduce the failure locally",
            "Run the same deterministic gates the pipeline ran, before changing anything.",
            _OPERATOR,
            command="sdlcctl run-gates --task task.json --diff diff.json",
        ),
        _action(
            "route-to-bounded-repair",
            "Route to bounded repair",
            "Hand the failure to a repair cycle with its declared cycle budget.",
            _AGENT,
        ),
    ),
    "review-gate-failed": (
        _action(
            "read-review-verdict",
            "Read the independent verdict",
            "The review gate records why it withheld approval; start from that reason.",
            _OPERATOR,
        ),
        _action(
            "route-to-bounded-repair",
            "Route to bounded repair",
            "Address the verdict's findings within the declared repair budget.",
            _AGENT,
        ),
    ),
    "merge-conflict": (
        _action(
            "refresh-change-request",
            "Refresh the change request",
            "Bring the base branch into the change request and resolve the conflict.",
            _AGENT,
        ),
    ),
    "dependency-wait": (
        _action(
            "inspect-work-graph",
            "Inspect the dependency graph",
            "Confirm which upstream unit the blocked unit is actually waiting on.",
            _OPERATOR,
        ),
    ),
    "awaiting-human-approval": (
        _action(
            "record-approval-decision",
            "Record the approval decision",
            "The factory stops here by design until a person approves or rejects the plan.",
            _HUMAN,
            handoff="Review the plan and record an approval or a rejection.",
        ),
    ),
    "awaiting-human-merge": (
        _action(
            "merge-as-human-authority",
            "Merge as the human authority",
            "Merge authority never leaves the human; the agent cannot complete this step.",
            _HUMAN,
            handoff="Review the change request and merge it, or ask for another repair cycle.",
        ),
    ),
    "infrastructure-failure": (
        _action(
            "classify-infrastructure-failure",
            "Classify the failure",
            "Decide whether the failure is infrastructure or the change itself before retrying.",
            _OPERATOR,
            command="sdlcctl classify-failure --log run.log",
        ),
        _action(
            "decide-infrastructure-retry",
            "Decide the retry",
            "Apply the declared retry budget instead of retrying by hand.",
            _OPERATOR,
            command="sdlcctl decide-infra-retry --classification classification.json",
        ),
    ),
    "unclassified": (
        _action(
            "classify-incident",
            "Classify the incident",
            "An unclassified incident has no declared remediation; classify it first.",
            _OPERATOR,
        ),
    ),
}

INCIDENT_KINDS = frozenset(REMEDIATION_CATALOGUE)


def remediations_for(kind: str) -> tuple[RemediationAction, ...]:
    """Return the declared remediations for ``kind``, never guessing one."""

    return REMEDIATION_CATALOGUE.get(kind, REMEDIATION_CATALOGUE["unclassified"])


_BACKLOG_ITEM_KEYS = frozenset(
    {
        "itemId",
        "kind",
        "title",
        "projectId",
        "parentId",
        "workUnitId",
        "issueRef",
        "declaredState",
        "labels",
        "url",
        "updatedAt",
    }
)
_BACKLOG_DOCUMENT_KEYS = frozenset({"schemaVersion", "items", "source"})


@dataclass(frozen=True)
class BacklogItem:
    """One declared unit of intent: an epic, a story, or a task."""

    item_id: str
    kind: BacklogKind
    title: str
    project_id: str
    parent_id: str = ""
    work_unit_id: str = ""
    issue_ref: str = ""
    declared_state: str = ""
    labels: tuple[str, ...] = ()
    url: str = ""
    updated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "itemId": self.item_id,
            "kind": self.kind.value,
            "title": self.title,
            "projectId": self.project_id,
            "parentId": self.parent_id,
            "workUnitId": self.work_unit_id,
            "issueRef": self.issue_ref,
            "declaredState": self.declared_state,
            "labels": list(self.labels),
            "url": self.url,
            "updatedAt": self.updated_at,
        }


def _load_backlog_item(raw: Any, index: int) -> BacklogItem:
    label = f"items[{index}]"
    entry = _mapping(raw, label)
    _known_keys(entry, _BACKLOG_ITEM_KEYS, label)
    kind = _text(entry, "kind", label, required=True)
    _require(
        kind in frozenset(item.value for item in BacklogKind),
        f"{label}.kind must be one of epic, story, task: {kind}",
    )
    declared = _text(entry, "declaredState", label)
    if declared:
        _require(
            declared in _FACTORY_STATES,
            f"{label}.declaredState is not a factory state: {declared}",
        )
    url = _text(entry, "url", label)
    if url:
        _require(
            url.startswith("https://"),
            f"{label}.url must be an https URL: {url}",
        )
    return BacklogItem(
        item_id=_identifier(entry, "itemId", label),
        kind=BacklogKind(kind),
        title=_text(entry, "title", label, required=True),
        project_id=_identifier(entry, "projectId", label),
        parent_id=_identifier(entry, "parentId", label, required=False),
        work_unit_id=_identifier(entry, "workUnitId", label, required=False),
        issue_ref=_identifier(entry, "issueRef", label, required=False),
        declared_state=declared,
        labels=_string_tuple(entry.get("labels"), f"{label}.labels"),
        url=url,
        updated_at=_timestamp(entry, "updatedAt", label),
    )


def load_backlog_document(document: Any) -> tuple[BacklogItem, ...]:
    """Load a backlog document, failing closed on anything inconsistent."""

    entry = _mapping(document, "backlog document")
    _known_keys(entry, _BACKLOG_DOCUMENT_KEYS, "backlog document")
    version = entry.get("schemaVersion")
    _require(
        version == BACKLOG_SCHEMA_VERSION,
        f"unsupported backlog schemaVersion: {version!r}",
    )
    raw_items = entry.get("items", [])
    _require(
        isinstance(raw_items, Sequence) and not isinstance(raw_items, (str, bytes)),
        "backlog document items must be an array",
    )
    assert isinstance(raw_items, Sequence)
    _require(
        len(raw_items) <= MAX_BACKLOG_ITEMS,
        f"backlog document holds more than {MAX_BACKLOG_ITEMS} items",
    )
    items = tuple(_load_backlog_item(raw, index) for index, raw in enumerate(raw_items))
    by_id: dict[str, BacklogItem] = {}
    for item in items:
        _require(item.item_id not in by_id, f"duplicate backlog itemId: {item.item_id}")
        by_id[item.item_id] = item
    for item in items:
        if not item.parent_id or item.parent_id not in by_id:
            continue
        parent = by_id[item.parent_id]
        _require(
            parent.project_id == item.project_id,
            f"backlog item {item.item_id} has a parent in another project: {item.parent_id}",
        )
        _require(
            item.parent_id != item.item_id,
            f"backlog item {item.item_id} is its own parent",
        )
    _detect_backlog_cycles(by_id)
    return items


def _detect_backlog_cycles(by_id: Mapping[str, BacklogItem]) -> None:
    """Reject a parent chain that loops, which no renderer could terminate on."""

    for item_id in by_id:
        seen = {item_id}
        cursor = by_id[item_id].parent_id
        while cursor and cursor in by_id:
            _require(cursor not in seen, f"backlog parent cycle through {item_id}")
            seen.add(cursor)
            cursor = by_id[cursor].parent_id


def read_backlog_document(path: str | Path) -> tuple[BacklogItem, ...]:
    """Read a backlog document from ``path``."""

    target = Path(path)
    _require(target.is_file(), f"backlog document not found: {target}")
    return load_backlog_document(json.loads(target.read_text(encoding="utf-8")))


def read_optional_backlog_document(path: str | Path | None) -> tuple[BacklogItem, ...]:
    """Read a backlog document when one is declared, else return nothing."""

    if path is None:
        return ()
    target = Path(path)
    if not target.exists():
        return ()
    return read_backlog_document(target)


_INCIDENT_KEYS = frozenset(
    {
        "incidentId",
        "projectId",
        "kind",
        "severity",
        "summary",
        "detail",
        "workUnitId",
        "backlogItemId",
        "observedAt",
        "evidenceRef",
    }
)
_INCIDENT_DOCUMENT_KEYS = frozenset({"schemaVersion", "incidents", "source"})


@dataclass(frozen=True)
class Incident:
    """One observed execution problem, with a kind that maps to remediations."""

    incident_id: str
    project_id: str
    kind: str
    severity: IncidentSeverity
    summary: str
    detail: str = ""
    work_unit_id: str = ""
    backlog_item_id: str = ""
    observed_at: str = ""
    evidence_ref: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "incidentId": self.incident_id,
            "projectId": self.project_id,
            "kind": self.kind,
            "severity": self.severity.value,
            "summary": self.summary,
            "detail": self.detail,
            "workUnitId": self.work_unit_id,
            "backlogItemId": self.backlog_item_id,
            "observedAt": self.observed_at,
            "evidenceRef": self.evidence_ref,
            "remediations": [action.as_dict() for action in remediations_for(self.kind)],
        }


def _load_incident(raw: Any, index: int) -> Incident:
    label = f"incidents[{index}]"
    entry = _mapping(raw, label)
    _known_keys(entry, _INCIDENT_KEYS, label)
    kind = _text(entry, "kind", label, required=True)
    _require(
        kind in INCIDENT_KINDS,
        f"{label}.kind is not a declared incident kind: {kind}",
    )
    severity = _text(entry, "severity", label, required=True)
    _require(
        severity in frozenset(item.value for item in IncidentSeverity),
        f"{label}.severity must be blocker, degraded or advisory: {severity}",
    )
    return Incident(
        incident_id=_identifier(entry, "incidentId", label),
        project_id=_identifier(entry, "projectId", label),
        kind=kind,
        severity=IncidentSeverity(severity),
        summary=_text(entry, "summary", label, required=True),
        detail=_text(entry, "detail", label),
        work_unit_id=_identifier(entry, "workUnitId", label, required=False),
        backlog_item_id=_identifier(entry, "backlogItemId", label, required=False),
        observed_at=_timestamp(entry, "observedAt", label),
        evidence_ref=_text(entry, "evidenceRef", label),
    )


def load_incident_document(document: Any) -> tuple[Incident, ...]:
    """Load an incident document, failing closed on an undeclared kind."""

    entry = _mapping(document, "incident document")
    _known_keys(entry, _INCIDENT_DOCUMENT_KEYS, "incident document")
    version = entry.get("schemaVersion")
    _require(
        version == INCIDENT_SCHEMA_VERSION,
        f"unsupported incident schemaVersion: {version!r}",
    )
    raw_items = entry.get("incidents", [])
    _require(
        isinstance(raw_items, Sequence) and not isinstance(raw_items, (str, bytes)),
        "incident document incidents must be an array",
    )
    assert isinstance(raw_items, Sequence)
    _require(
        len(raw_items) <= MAX_INCIDENTS,
        f"incident document holds more than {MAX_INCIDENTS} incidents",
    )
    incidents = tuple(_load_incident(raw, index) for index, raw in enumerate(raw_items))
    seen: set[str] = set()
    for incident in incidents:
        _require(
            incident.incident_id not in seen,
            f"duplicate incidentId: {incident.incident_id}",
        )
        seen.add(incident.incident_id)
    return incidents


def read_incident_document(path: str | Path) -> tuple[Incident, ...]:
    """Read an incident document from ``path``."""

    target = Path(path)
    _require(target.is_file(), f"incident document not found: {target}")
    return load_incident_document(json.loads(target.read_text(encoding="utf-8")))


def read_optional_incident_document(path: str | Path | None) -> tuple[Incident, ...]:
    """Read an incident document when one is declared, else return nothing."""

    if path is None:
        return ()
    target = Path(path)
    if not target.exists():
        return ()
    return read_incident_document(target)


@dataclass(frozen=True)
class WorkUnitStatus:
    """What one work unit is doing right now, durable facts first."""

    work_unit_id: str
    state: FactoryState
    stage: LifecycleStage
    terminal: bool
    repair_cycles: int = 0
    blocked_reason: str = ""
    depends_on: tuple[str, ...] = ()
    repository: str = ""
    issue_ref: str = ""
    change_request_ref: str = ""
    run_ref: str = ""
    last_event_at: str = ""
    activity: str = ""
    activity_at: str = ""
    actor: str = ""
    model: str = ""
    worker: str = ""

    @property
    def awaits_human(self) -> bool:
        """True when the factory has deliberately stopped for a person."""

        return self.state in AWAITING_HUMAN_STATES

    @property
    def needs_attention(self) -> bool:
        """True when nothing will move this unit without an intervention."""

        return self.awaits_human or self.state in {FactoryState.BLOCKED, FactoryState.FAILED}

    def as_dict(self) -> dict[str, Any]:
        return {
            "workUnitId": self.work_unit_id,
            "state": self.state.value,
            "stage": self.stage.value,
            "terminal": self.terminal,
            "awaitsHuman": self.awaits_human,
            "needsAttention": self.needs_attention,
            "repairCycles": self.repair_cycles,
            "blockedReason": self.blocked_reason,
            "dependsOn": list(self.depends_on),
            "repository": self.repository,
            "issueRef": self.issue_ref,
            "changeRequestRef": self.change_request_ref,
            "runRef": self.run_ref,
            "lastEventAt": self.last_event_at,
            "activity": self.activity,
            "activityAt": self.activity_at,
            "actor": self.actor,
            "model": self.model,
            "worker": self.worker,
        }


def work_unit_status(projection: WorkUnitProjection) -> WorkUnitStatus:
    """Narrow a ledger projection to what a console needs to render."""

    references = projection.references
    return WorkUnitStatus(
        work_unit_id=projection.work_unit_id,
        state=projection.state,
        stage=projection.stage,
        terminal=projection.is_terminal,
        repair_cycles=projection.repair_cycles,
        blocked_reason=projection.blocked_reason,
        depends_on=tuple(projection.depends_on),
        repository=references.repository,
        issue_ref=references.issue_ref,
        change_request_ref=references.change_request_ref,
        run_ref=references.run_ref,
        last_event_at=projection.last_event_at,
        activity=projection.activity,
        activity_at=projection.activity_at,
        actor=projection.activity_actor,
        model=projection.activity_model,
        worker=projection.activity_worker,
    )


@dataclass(frozen=True)
class BacklogNode:
    """One backlog item with its resolved state and its children."""

    item: BacklogItem
    state: str
    state_basis: str
    status: WorkUnitStatus | None = None
    parent_missing: bool = False
    children: tuple[BacklogNode, ...] = ()

    @property
    def descendant_count(self) -> int:
        return len(self.children) + sum(child.descendant_count for child in self.children)

    def as_dict(self) -> dict[str, Any]:
        payload = self.item.as_dict()
        payload.update(
            {
                "state": self.state,
                "stateBasis": self.state_basis,
                "parentMissing": self.parent_missing,
                "status": self.status.as_dict() if self.status is not None else None,
                "children": [child.as_dict() for child in self.children],
            }
        )
        return payload


@dataclass(frozen=True)
class ProjectView:
    """Everything the console shows for one project, on one screen."""

    project_id: str
    display_name: str
    registered: bool
    group: str = ""
    default_branch: str = ""
    repositories: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    environments: tuple[str, ...] = ()
    state_counts: Mapping[str, int] = field(default_factory=dict)
    stage_counts: Mapping[str, int] = field(default_factory=dict)
    active: tuple[WorkUnitStatus, ...] = ()
    attention: tuple[WorkUnitStatus, ...] = ()
    backlog: tuple[BacklogNode, ...] = ()
    incidents: tuple[Incident, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def work_unit_count(self) -> int:
        return sum(self.state_counts.values())

    @property
    def backlog_count(self) -> int:
        return len(self.backlog) + sum(node.descendant_count for node in self.backlog)

    @property
    def blocker_count(self) -> int:
        return sum(1 for item in self.incidents if item.severity is IncidentSeverity.BLOCKER)

    @property
    def health(self) -> str:
        """A single word for the project, derived only from observed facts."""

        if not self.registered:
            return "unregistered"
        if self.blocker_count:
            return "blocked"
        if self.attention:
            return "needs-attention"
        if self.incidents:
            return "degraded"
        if self.active:
            return "running"
        if self.work_unit_count or self.backlog_count:
            return "idle"
        return "no-data"

    def as_dict(self) -> dict[str, Any]:
        return {
            "projectId": self.project_id,
            "displayName": self.display_name,
            "registered": self.registered,
            "health": self.health,
            "group": self.group,
            "defaultBranch": self.default_branch,
            "repositories": list(self.repositories),
            "capabilities": list(self.capabilities),
            "environments": list(self.environments),
            "counts": {
                "workUnits": self.work_unit_count,
                "backlogItems": self.backlog_count,
                "active": len(self.active),
                "attention": len(self.attention),
                "incidents": len(self.incidents),
                "blockers": self.blocker_count,
                "byState": dict(sorted(self.state_counts.items())),
                "byStage": dict(sorted(self.stage_counts.items())),
            },
            "active": [item.as_dict() for item in self.active],
            "attention": [item.as_dict() for item in self.attention],
            "backlog": [node.as_dict() for node in self.backlog],
            "incidents": [item.as_dict() for item in self.incidents],
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class ControlCenterSnapshot:
    """The whole portfolio, as one read-only document the UI renders."""

    generated_at: str
    projects: tuple[ProjectView, ...] = ()
    sources: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    schema_version: int = CONTROL_CENTER_SCHEMA_VERSION

    @property
    def totals(self) -> dict[str, int]:
        return {
            "projects": len(self.projects),
            "workUnits": sum(view.work_unit_count for view in self.projects),
            "backlogItems": sum(view.backlog_count for view in self.projects),
            "active": sum(len(view.active) for view in self.projects),
            "attention": sum(len(view.attention) for view in self.projects),
            "incidents": sum(len(view.incidents) for view in self.projects),
            "blockers": sum(view.blocker_count for view in self.projects),
            "unregistered": sum(1 for view in self.projects if not view.registered),
        }

    def get(self, project_id: str) -> ProjectView:
        """Return one project view, or raise when the snapshot has no such project."""

        for view in self.projects:
            if view.project_id == project_id:
                return view
        raise ControlCenterError(f"snapshot has no project: {project_id}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "generatedAt": self.generated_at,
            "sources": list(self.sources),
            "totals": self.totals,
            "projects": [view.as_dict() for view in self.projects],
            "warnings": list(self.warnings),
        }

    @property
    def snapshot_sha256(self) -> str:
        """Digest over the content, excluding when it was generated or observed.

        ``generatedAt`` and each incident's ``observedAt`` are run metadata: two
        snapshots of an unchanged factory must compare equal even when the
        exporter stamped its incidents with the time it happened to run.
        """

        payload = self.as_dict()
        payload.pop("generatedAt", None)
        for project in payload.get("projects", ()):
            for incident in project.get("incidents", ()):
                incident.pop("observedAt", None)
        return _digest(payload)

    def as_document(self) -> dict[str, Any]:
        """The serialized snapshot plus its content digest, as published."""

        payload = self.as_dict()
        payload["snapshotSha256"] = self.snapshot_sha256
        return payload


def _resolve_state(
    item: BacklogItem,
    statuses: Mapping[str, WorkUnitStatus],
    by_issue_ref: Mapping[str, WorkUnitStatus],
) -> tuple[str, str, WorkUnitStatus | None]:
    """Resolve a backlog item's state, naming the basis and never inventing one.

    A declared work-unit key wins; an unambiguous issue reference joins next,
    because tracker-exported backlogs carry ``issueRef`` while the ledger's
    projections carry the same reference. Only then does a source-declared
    state apply, and with no evidence at all the state is *unknown*.
    """

    if item.work_unit_id and item.work_unit_id in statuses:
        status = statuses[item.work_unit_id]
        return status.state.value, STATE_BASIS_LEDGER, status
    if item.issue_ref and item.issue_ref in by_issue_ref:
        status = by_issue_ref[item.issue_ref]
        return status.state.value, STATE_BASIS_LEDGER, status
    if item.declared_state:
        return item.declared_state, STATE_BASIS_DECLARED, None
    return "unknown", STATE_BASIS_UNKNOWN, None


def _index_by_issue_ref(
    statuses: Mapping[str, WorkUnitStatus],
) -> tuple[dict[str, WorkUnitStatus], tuple[str, ...]]:
    """Index projections by issue reference, dropping ambiguous references.

    Two work units legitimately share an issue when one supersedes another;
    joining either would guess, so the reference is excluded with a warning.
    """

    index: dict[str, WorkUnitStatus] = {}
    ambiguous: set[str] = set()
    for key in sorted(statuses):
        status = statuses[key]
        if not status.issue_ref:
            continue
        if status.issue_ref in index:
            ambiguous.add(status.issue_ref)
            continue
        index[status.issue_ref] = status
    warnings = tuple(
        f"issue reference {reference} names more than one work unit; "
        "its backlog items keep their declared state"
        for reference in sorted(ambiguous)
    )
    for reference in ambiguous:
        index.pop(reference, None)
    return index, warnings


def _build_backlog_tree(
    items: Sequence[BacklogItem],
    statuses: Mapping[str, WorkUnitStatus],
    by_issue_ref: Mapping[str, WorkUnitStatus],
) -> tuple[tuple[BacklogNode, ...], tuple[str, ...]]:
    """Build the epic/story/task forest, rooting any item whose parent is absent."""

    present = {item.item_id for item in items}
    children: dict[str, list[BacklogItem]] = {}
    roots: list[BacklogItem] = []
    orphans: set[str] = set()
    warnings: list[str] = []
    for item in items:
        if item.parent_id and item.parent_id in present:
            children.setdefault(item.parent_id, []).append(item)
            continue
        if item.parent_id:
            orphans.add(item.item_id)
            warnings.append(
                f"backlog item {item.item_id} names a parent outside this snapshot: "
                f"{item.parent_id}"
            )
        roots.append(item)

    def node_for(item: BacklogItem) -> BacklogNode:
        state, basis, status = _resolve_state(item, statuses, by_issue_ref)
        return BacklogNode(
            item=item,
            state=state,
            state_basis=basis,
            status=status,
            parent_missing=item.item_id in orphans,
            children=tuple(node_for(child) for child in children.get(item.item_id, ())),
        )

    return tuple(node_for(item) for item in roots), tuple(warnings)


def _registered_view_fields(registry: ProjectRegistry, project_id: str) -> dict[str, Any]:
    profile = registry.get(project_id)
    return {
        "display_name": profile.display_name,
        "group": profile.group,
        "default_branch": profile.default_branch,
        "repositories": tuple(
            f"{repository.provider}:{repository.identifier}" for repository in profile.repositories
        ),
        "capabilities": tuple(profile.capabilities),
        "environments": tuple(
            f"{environment.name}:{environment.kind}" for environment in profile.environments
        ),
    }


def build_control_center_snapshot(
    *,
    generated_at: str,
    registry: ProjectRegistry = EMPTY_PROJECT_REGISTRY,
    projections: Iterable[WorkUnitProjection] = (),
    backlog: Iterable[BacklogItem] = (),
    incidents: Iterable[Incident] = (),
    sources: Iterable[str] = (),
    project_ids: Iterable[str] | None = None,
) -> ControlCenterSnapshot:
    """Join registry, ledger projections, backlog and incidents into one view.

    Data for a project the registry does not declare is kept and flagged rather
    than dropped: an operator cannot remediate what the console hides.
    """

    _timestamp({"generatedAt": generated_at}, "generatedAt", "snapshot", required=True)

    by_project_units: dict[str, dict[str, WorkUnitStatus]] = {}
    for projection in projections:
        units = by_project_units.setdefault(projection.project_id, {})
        _require(
            projection.work_unit_id not in units,
            f"duplicate projection for {projection.project_id}/{projection.work_unit_id}",
        )
        units[projection.work_unit_id] = work_unit_status(projection)

    by_project_backlog: dict[str, list[BacklogItem]] = {}
    seen_items: set[str] = set()
    for item in backlog:
        _require(item.item_id not in seen_items, f"duplicate backlog itemId: {item.item_id}")
        seen_items.add(item.item_id)
        by_project_backlog.setdefault(item.project_id, []).append(item)

    by_project_incidents: dict[str, list[Incident]] = {}
    seen_incidents: set[str] = set()
    for incident in incidents:
        _require(
            incident.incident_id not in seen_incidents,
            f"duplicate incidentId: {incident.incident_id}",
        )
        seen_incidents.add(incident.incident_id)
        by_project_incidents.setdefault(incident.project_id, []).append(incident)

    universe = set(registry.project_ids())
    observed = set(by_project_units) | set(by_project_backlog) | set(by_project_incidents)
    universe |= observed
    if project_ids is not None:
        requested = tuple(project_ids)
        missing = sorted(set(requested) - universe)
        _require(not missing, f"snapshot has no such project: {', '.join(missing)}")
        universe &= set(requested)

    warnings: list[str] = []
    views: list[ProjectView] = []
    for project_id in sorted(universe):
        registered = project_id in registry.project_ids()
        statuses = by_project_units.get(project_id, {})
        items = by_project_backlog.get(project_id, [])
        project_incidents = tuple(by_project_incidents.get(project_id, ()))
        by_issue_ref, reference_warnings = _index_by_issue_ref(statuses)
        tree, tree_warnings = _build_backlog_tree(items, statuses, by_issue_ref)
        view_warnings = list(reference_warnings) + list(tree_warnings)

        if not registered:
            view_warnings.append(
                f"project {project_id} has factory data but is not declared in the registry"
            )
            warnings.append(
                f"project {project_id} has factory data but is not declared in the registry"
            )

        known_units = frozenset(statuses)
        known_items = frozenset(item.item_id for item in items)
        for incident in project_incidents:
            if incident.work_unit_id and incident.work_unit_id not in known_units:
                view_warnings.append(
                    f"incident {incident.incident_id} names an unknown work unit: "
                    f"{incident.work_unit_id}"
                )
            if incident.backlog_item_id and incident.backlog_item_id not in known_items:
                view_warnings.append(
                    f"incident {incident.incident_id} names an unknown backlog item: "
                    f"{incident.backlog_item_id}"
                )

        state_counts: dict[str, int] = {}
        stage_counts: dict[str, int] = {}
        for status in statuses.values():
            state_counts[status.state.value] = state_counts.get(status.state.value, 0) + 1
            stage_counts[status.stage.value] = stage_counts.get(status.stage.value, 0) + 1

        ordered = tuple(statuses[key] for key in sorted(statuses))
        active = tuple(
            status
            for status in ordered
            if status.state in EXECUTING_STATES and not status.needs_attention
        )
        attention = tuple(status for status in ordered if status.needs_attention)

        fields: dict[str, Any] = {"display_name": project_id}
        if registered:
            fields = _registered_view_fields(registry, project_id)
        views.append(
            ProjectView(
                project_id=project_id,
                registered=registered,
                state_counts=state_counts,
                stage_counts=stage_counts,
                active=active,
                attention=attention,
                backlog=tree,
                incidents=project_incidents,
                warnings=tuple(view_warnings),
                **fields,
            )
        )

    return ControlCenterSnapshot(
        generated_at=generated_at,
        projects=tuple(views),
        sources=_string_tuple(list(sources), "sources"),
        warnings=tuple(warnings),
    )


def read_control_center_inputs(
    *,
    registry_path: str | None = None,
    backlog_path: str | None = None,
    incidents_path: str | None = None,
) -> tuple[ProjectRegistry, tuple[BacklogItem, ...], tuple[Incident, ...]]:
    """Read every declared Control Center input, each one optional."""

    registry = read_optional_project_registry(registry_path)
    return (
        registry,
        read_optional_backlog_document(backlog_path),
        read_optional_incident_document(incidents_path),
    )


def secret_bearing_control_center_fields() -> tuple[str, ...]:
    """Field names a Control Center document must never carry."""

    return (
        "apiKey",
        "authorization",
        "bearerToken",
        "credential",
        "password",
        "passphrase",
        "privateKey",
        "secret",
        "token",
    )


CONTROL_CENTER_TYPES = (
    BacklogItem,
    BacklogNode,
    ControlCenterSnapshot,
    Incident,
    ProjectView,
    RemediationAction,
    WorkUnitStatus,
)
