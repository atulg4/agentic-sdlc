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
from dataclasses import dataclass, field, replace
from datetime import datetime
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
    SCM_PROVIDERS,
    ProjectRegistry,
    read_optional_project_registry,
    read_project_registry,
)

CONTROL_CENTER_SCHEMA_VERSION = 1
BACKLOG_SCHEMA_VERSION = 1
INCIDENT_SCHEMA_VERSION = 1

STATE_BASIS_LEDGER = "state projected from the lifecycle ledger"
STATE_BASIS_DECLARED = "state declared by the backlog source"
STATE_BASIS_UNKNOWN = "state unknown: no ledger events and no declared state"

MAX_TEXT_LENGTH = 512
MAX_BACKLOG_ITEMS = 5000
MAX_BACKLOG_DEPTH = 32
MAX_INCIDENTS = 2000
#: The snapshot's portfolio budget, shared with the console's validator: a
#: snapshot either fits every declared and observed project in it or fails
#: with a reason, so the producer and the viewer agree on what is renderable.
MAX_PROJECTS = 500
#: The sources list budget, shared with the console: two documents per
#: project at the full portfolio budget, plus a registry and a ledger entry,
#: still fit with headroom.
MAX_SOURCES = 1200
#: Warnings budget per project and for the snapshot itself, shared with the
#: console: a snapshot that would exceed it is truncated WITH a marker, so
#: every producer-valid snapshot stays renderable and the truncation shows.
MAX_WARNINGS = 5000
#: Work units per project, shared with the console's active/attention
#: budgets: merged ledger exports beyond it fail with a reason.
MAX_WORK_UNITS = 5000
#: Portfolio-wide budgets, applied AFTER --project-id filtering and shared
#: with the console: per-project limits alone would still let a full
#: 500-project portfolio materialize millions of records.
MAX_TOTAL_BACKLOG_ITEMS = 50_000
MAX_TOTAL_INCIDENTS = 10_000
MAX_TOTAL_WORK_UNITS = 50_000


def _bounded_warnings(entries: Sequence[str]) -> tuple[str, ...]:
    if len(entries) <= MAX_WARNINGS:
        return tuple(entries)
    kept = list(entries[: MAX_WARNINGS - 1])
    kept.append(
        f"{len(entries) - (MAX_WARNINGS - 1)} more warnings were truncated; "
        "resolve the ones above to surface them"
    )
    return tuple(kept)


#: A tracker reference charset: ``owner/repo#123`` and ``owner/repo!123`` both
#: fit, so a GitHub issue and a GitLab merge request need no re-encoding. The
#: leading class matches the registry's repository grammar (GitLab allows
#: ``_`` and ``.`` anywhere, so ``_group/repo#7`` is a real reference). The
#: length budget is the read model's own text budget (``MAX_TEXT_LENGTH``).
#: The registry caps repository identifiers at 255 characters
#: (``project_registry.MAX_REPOSITORY_LENGTH``), so under the SHARED contract
#: every registry-valid repository leaves room for its tracker suffix
#: (``#N``/``!N`` and a rule id).
_ID = re.compile(r"[A-Za-z0-9._-][A-Za-z0-9._/#!:-]{0,511}")
#: A tracker reference that names a repository: an identifier of one or more
#: slash-separated segments in the registry's own segment contract (GitLab
#: nests groups and allows . and _ anywhere), followed by a MANDATORY #/! and
#: number (and an optional :suffix, as incident ids carry). Arbitrary
#: slash-bearing evidence text ("build/123", a CI URL) is NOT one.
_REPOSITORY_REFERENCE = re.compile(
    r"(?P<name>[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+?)[#!]\d+(?::.+)?"
)
#: A bare repository identifier, for fields whose CONTRACT is a repository
#: (a ledger status's repository field), never for free-form references.
_REPOSITORY_IDENTIFIER = re.compile(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+")
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

#: The adapter rule id for the plan-ordering advisory. The read model retires
#: that advisory on earlier evidence than the implementation one, so the rule
#: name is shared here rather than guessed from text.
PLAN_EVIDENCE_RULE = "plan-ready-awaiting-dispatch-evidence"

#: The adapter rule id for a released spec with no dispatch label after it.
#: Approval proof alone must NOT retire it: the rule reports missing dispatch
#: labels, which an approved unit still needs before anything can fire, so it
#: holds until dispatch itself is proven.
RELEASED_SPEC_RULE = "released-spec-without-dispatch-labels"

#: The adapter rule id for a drafted spec held on its implementation approval
#: AND the dispatch labels. Approval proof alone must not retire it either:
#: the ready label is still missing, so neither bundled trigger can fire.
SPEC_WITHOUT_APPROVAL_RULE = "spec-without-approval"

#: States that PROVE dispatch happened: each is reachable only through a
#: dispatched run. ``blocked``, ``cancelled`` and ``superseded`` are reachable
#: from pre-dispatch states too, so they prove nothing and retire nothing.
DISPATCH_PROVEN_STATES = frozenset(
    {
        FactoryState.DISPATCHED,
        FactoryState.IMPLEMENTING,
        FactoryState.VERIFYING,
        FactoryState.REVIEWING,
        FactoryState.REPAIR_NEEDED,
        FactoryState.REPAIRING,
        FactoryState.RE_REVIEWING,
        FactoryState.READY_FOR_HUMAN_MERGE,
        FactoryState.MERGED,
        FactoryState.DEPLOYING,
        FactoryState.DEPLOYMENT_VERIFYING,
        FactoryState.LIVE,
        FactoryState.ROLLING_BACK,
        FactoryState.ROLLED_BACK,
        FactoryState.FAILED,
    }
)

#: States that retire the plan-ordering advisory: a plan provably ran and the
#: next gate is visible elsewhere. ``approved`` is deliberately NOT here: an
#: approved unit whose implementation-approved label went missing can only be
#: re-triggered by that label event, so its label-derived incident must stay
#: visible until dispatch itself is proven.
PLAN_PROVEN_STATES = DISPATCH_PROVEN_STATES | frozenset(
    {FactoryState.PLANNED, FactoryState.APPROVAL_PENDING}
)

#: States that prove the implementation approval was granted.
APPROVAL_PROVEN_STATES = DISPATCH_PROVEN_STATES | frozenset({FactoryState.APPROVED})

#: Per incident kind, the ledger states that retire a label-derived incident:
#: durable evidence that the gate the labels describe was already passed.
_LEDGER_RETIREMENT: Mapping[str, frozenset[FactoryState]] = {
    "dispatch-blocked": DISPATCH_PROVEN_STATES,
    "awaiting-dispatch-evidence": DISPATCH_PROVEN_STATES,
    # Labels are the dispatch mechanism, so losing every label matters right up
    # to dispatch: a unit the ledger shows approved still cannot fire without a
    # fresh label event.
    "triage-needed": DISPATCH_PROVEN_STATES,
    # A fresh re-draft can hold work that already planned, so only dispatch
    # evidence retires the spec-review hold.
    "awaiting-spec-review": DISPATCH_PROVEN_STATES,
    "awaiting-human-approval": APPROVAL_PROVEN_STATES,
}

#: States that prove a change request was merged: ``merged`` itself and every
#: state only reachable after it. A rollback does not un-merge the change
#: request, so the rollback states still carry the proof.
MERGE_PROVEN_STATES = frozenset(
    {
        FactoryState.MERGED,
        FactoryState.DEPLOYING,
        FactoryState.DEPLOYMENT_VERIFYING,
        FactoryState.LIVE,
        FactoryState.ROLLING_BACK,
        FactoryState.ROLLED_BACK,
    }
)


def _merge_proven(status: WorkUnitStatus) -> bool:
    """The unit's own durable history proves its change request merged."""
    return status.state in MERGE_PROVEN_STATES or bool(status.states_seen & MERGE_PROVEN_STATES)


#: Change-request-scoped incident kinds: each describes an obstacle to merging
#: ONE change request (named by ``evidenceRef``), so durable ledger proof that
#: the same change request merged moots the incident - a file exported before
#: the merge, or racing it, must not tell the operator to merge again.
_CHANGE_REQUEST_RETIREMENT = frozenset(
    {
        "awaiting-human-merge",
        "merge-conflict",
        "branch-behind",
        "deterministic-ci-failed",
    }
)


def _adapter_rule_incident(incident: Incident, rule: str) -> bool:
    """The incident IS the adapter's label-derived record for ``rule``.

    The adapter's complete identity relationship must hold - the document
    declared a GitHub source, the id is exactly the named backlog item plus
    the rule suffix, and the evidenceRef repeats that same item, the shape
    the GitHub adapter emits - so a provider-neutral incident that merely
    matches the adapter's id spelling is never silently rewritten or retired.
    """
    return (
        incident.source_provider == "github"
        and bool(incident.backlog_item_id)
        and incident.evidence_ref == incident.backlog_item_id
        and incident.incident_id == f"{incident.backlog_item_id}:{rule}"
    )


def _change_request_scoped(incident: Incident) -> bool:
    """The incident describes an obstacle to MERGING its evidenceRef.

    ``unclassified`` is provider-neutral and its evidenceRef is free text, so
    only the adapter's own merge-state records count: the id spelling
    (``<ref>:merge-state-<x>``) AND the document's declared GitHub source,
    since only the GitHub adapter defines that spelling. Any other
    unclassified incident citing a merged change request may describe
    something else entirely (a post-merge regression) and must stay.
    """
    if incident.kind in _CHANGE_REQUEST_RETIREMENT:
        return True
    return (
        incident.source_provider == "github"
        and incident.kind == "unclassified"
        and incident.incident_id.startswith(incident.evidence_ref + ":merge-state-")
    )


#: States in which something is executing *right now*. Backlog-shaped states
#: (intake through approved), queued ones (dispatched — a worker has not
#: started) and settled ones (merged, live) are deliberately
#: excluded: ``live`` is not terminal — a rollback stays possible — but a live
#: unit is not running work, and counting it would pin a project at "running"
#: forever.
EXECUTING_STATES = frozenset(
    {
        FactoryState.IMPLEMENTING,
        FactoryState.VERIFYING,
        FactoryState.REVIEWING,
        FactoryState.REPAIRING,
        FactoryState.RE_REVIEWING,
        FactoryState.DEPLOYING,
        FactoryState.DEPLOYMENT_VERIFYING,
        FactoryState.ROLLING_BACK,
    }
)

#: Review states are entered when verification or repair completes, before any
#: review run starts, so they count as running only with activity evidence.
REVIEW_GATE_STATES = frozenset({FactoryState.REVIEWING, FactoryState.RE_REVIEWING})


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


def _unit_text(entry: Mapping[str, Any], key: str, label: str) -> str:
    """A work-unit id under the LEDGER's identifier contract: any string, with
    no character-set restriction and NO length cap, because existing
    version-1 ledgers already hold unbounded ids and every unit the ledger
    accepted must stay addressable from backlog items and incidents."""

    raw = entry.get(key, "")
    if raw is None:
        raw = ""
    _require(isinstance(raw, str), f"{label}.{key} must be a string")
    return raw.strip()


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
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _require(False, f"{label}.{key} is not a real instant: {value}")
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
            command=(
                "sdlcctl capacity-report --usage usage-ledger.json "
                "--window-start <start> --window-end <end> --output capacity.json"
            ),
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
            command=(
                "sdlcctl route-executor --config agentic-sdlc.toml --mission-id <mission> "
                "--executors executors.json --repository <owner/repo> "
                "--task-class implementation --budget-usd 5"
            ),
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
            handoff=(
                "Apply the label(s) the incident names as missing - the ready label, the "
                "human-review label, or the implementation approval - or record the approval "
                "event; approval alone does not restore a missing trigger label."
            ),
        ),
        _action(
            "re-run-dispatch",
            "Re-run mission dispatch",
            "Once preconditions hold, re-dispatch the mission for this work unit.",
            _OPERATOR,
            command=(
                "sdlcctl dispatch-mission --config agentic-sdlc.toml --mission-id <mission> "
                "--agents agents.json --work-ref <work-ref> --prompt prompt.md"
            ),
        ),
    ),
    "deterministic-ci-failed": (
        _action(
            "reproduce-ci-failure",
            "Reproduce the failure locally",
            "Run the same deterministic gates the pipeline ran, before changing anything.",
            _OPERATOR,
            command=(
                "sdlcctl run-gates --config agentic-sdlc.toml --repository . "
                "--output gates-report.json"
            ),
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
    "branch-behind": (
        _action(
            "update-change-request-branch",
            "Update the change request branch",
            "Bring the base branch into the change request so checks run against the current base.",
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
            command="sdlcctl classify-failure --log run.log --output classification.json",
        ),
        _action(
            "decide-infrastructure-retry",
            "Decide the retry",
            "Apply the declared retry budget instead of retrying by hand.",
            _OPERATOR,
            command=(
                "sdlcctl decide-infra-retry --repository <owner/repo> "
                "--pull-request-number <pull-number> --run-id <run-id> --head-sha <sha> "
                "--current-head-sha <sha> --failure-class transient_infrastructure "
                "--state <retry-state-file> --output decision.json"
            ),
        ),
    ),
    "awaiting-dispatch-evidence": (
        _action(
            "confirm-the-run",
            "Confirm the run started",
            "Every precondition label is present; check whether the trigger actually fired.",
            _OPERATOR,
        ),
        _action(
            "re-apply-the-trigger-label",
            "Re-apply the trigger label",
            "The bundled triggers fire only on their label event; re-apply it to re-kick.",
            _HUMAN,
            handoff=(
                "Remove and re-add the trigger label the incident's summary names "
                "(implementation-approved for the implement trigger and agent-ready for the "
                "plan trigger by default; a project policy may rename them) to fire the "
                "event again."
            ),
        ),
    ),
    "awaiting-spec-review": (
        _action(
            "release-or-reject-the-spec",
            "Release or reject the drafted specification",
            "The spec stage holds the work until a human releases the draft.",
            _HUMAN,
            handoff=(
                "Review the drafted specification: add spec-approved to release it, or "
                "remove spec-drafted to reject it."
            ),
        ),
    ),
    "triage-needed": (
        _action(
            "triage-the-work",
            "Triage the work",
            "No lifecycle label owns this item; decide its stage and label it.",
            _HUMAN,
            handoff=(
                "Review the item and give it the lifecycle labels that admit it to a "
                "stage (spec review, implementation approval, dispatch)."
            ),
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
    #: The SCM provider the item's DOCUMENT declared as its source; carried
    #: for provider-qualified ownership checks, never serialized back out.
    source_provider: str = ""

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
        work_unit_id=_unit_text(entry, "workUnitId", label),
        issue_ref=_identifier(entry, "issueRef", label, required=False),
        declared_state=declared,
        labels=_string_tuple(entry.get("labels"), f"{label}.labels"),
        url=url,
        updated_at=_timestamp(entry, "updatedAt", label),
    )


def _source_provider(entry: Mapping[str, Any]) -> str:
    """The SCM provider a document's declared source names, or "".

    Adapter sources lead with the provider ("github-issues", "github"); an
    absent or unrecognized source yields "" so ownership checks stay
    name-only for that document's records.
    """
    source = entry.get("source")
    if not isinstance(source, str):
        return ""
    provider = source.split("-", 1)[0].strip().lower()
    return provider if provider in SCM_PROVIDERS else ""


def load_backlog_document(document: Any) -> tuple[BacklogItem, ...]:
    """Load a backlog document, failing closed on anything inconsistent."""

    entry = _mapping(document, "backlog document")
    _known_keys(entry, _BACKLOG_DOCUMENT_KEYS, "backlog document")
    version = entry.get("schemaVersion")
    _require(
        isinstance(version, int)
        and not isinstance(version, bool)
        and version == BACKLOG_SCHEMA_VERSION,
        f"unsupported backlog schemaVersion: {version!r}",
    )
    if "source" in entry:
        _require(
            isinstance(entry.get("source"), str),
            "backlog document.source must be a string",
        )
        _text(entry, "source", "backlog document")
    _require(
        "items" in entry,
        "backlog document requires an items array; an intentional empty export "
        "carries an explicit []",
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
    provider = _source_provider(entry)
    if provider:
        items = tuple(replace(item, source_provider=provider) for item in items)
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
    """Reject a looping or excessively deep parent chain.

    Tree construction and serialization recurse along the parent chain, so a
    chain deeper than :data:`MAX_BACKLOG_DEPTH` is rejected here, before any
    traversal can hit the interpreter's recursion limit.
    """

    for item_id in by_id:
        seen = {item_id}
        cursor = by_id[item_id].parent_id
        depth = 1
        while cursor and cursor in by_id:
            _require(cursor not in seen, f"backlog parent cycle through {item_id}")
            depth += 1
            _require(
                depth <= MAX_BACKLOG_DEPTH,
                f"backlog parent chain through {item_id} exceeds {MAX_BACKLOG_DEPTH} levels",
            )
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
    #: The SCM provider the incident's DOCUMENT declared as its source;
    #: carried for provider-qualified ownership checks, never serialized.
    source_provider: str = ""

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
        work_unit_id=_unit_text(entry, "workUnitId", label),
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
        isinstance(version, int)
        and not isinstance(version, bool)
        and version == INCIDENT_SCHEMA_VERSION,
        f"unsupported incident schemaVersion: {version!r}",
    )
    if "source" in entry:
        _require(
            isinstance(entry.get("source"), str),
            "incident document.source must be a string",
        )
        _text(entry, "source", "incident document")
    _require(
        "incidents" in entry,
        "incident document requires an incidents array; an intentional empty "
        "export carries an explicit []",
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
    provider = _source_provider(entry)
    if provider:
        incidents = tuple(replace(item, source_provider=provider) for item in incidents)
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
    #: Every state the unit durably reached, from the projection's transition
    #: history. Retirement evidence, not render data: a unit that crossed a
    #: gate and later re-entered a pre-gate state (dispatched, then blocked)
    #: still proved the gate passed.
    states_seen: frozenset[FactoryState] = frozenset()

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
        states_seen=_states_seen(projection),
    )


#: States that restart a unit's lifecycle: work sent back to triage must pass
#: every gate again, so crossed-gate history from before the restart is void.
_LIFECYCLE_RESTART_STATES = frozenset({FactoryState.INTAKE, FactoryState.TRIAGED})


def _states_seen(projection: WorkUnitProjection) -> frozenset[FactoryState]:
    """The states durably reached since the most recent lifecycle restart."""

    seen: set[FactoryState] = set()
    for transition in projection.transitions:
        if transition.to_state in _LIFECYCLE_RESTART_STATES:
            seen.clear()
        seen.add(transition.to_state)
    seen.add(projection.state)
    return frozenset(seen)


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

    if item.work_unit_id:
        status = statuses.get(item.work_unit_id)
        if status is not None:
            return status.state.value, STATE_BASIS_LEDGER, status
        # An explicit work-unit reference is authoritative: when it resolves
        # to nothing, the item must NOT fall through to the issue-reference
        # join, which could silently adopt a DIFFERENT unit's progress.
        if item.declared_state:
            return item.declared_state, STATE_BASIS_DECLARED, None
        return "unknown", STATE_BASIS_UNKNOWN, None
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
        if item.work_unit_id and item.work_unit_id not in statuses:
            warnings.append(
                f"backlog item {item.item_id} names an unknown work unit: {item.work_unit_id}"
            )
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

    # The validator returns the normalized (stripped) instant; serializing the
    # raw input instead would let surrounding whitespace survive into a
    # document whose consumers anchor their own RFC 3339 checks.
    generated_at = _timestamp(
        {"generatedAt": generated_at}, "generatedAt", "snapshot", required=True
    )

    by_project_units: dict[str, dict[str, WorkUnitStatus]] = {}
    for projection in projections:
        units = by_project_units.setdefault(projection.project_id, {})
        _require(
            projection.work_unit_id not in units,
            f"duplicate projection for {projection.project_id}/{projection.work_unit_id}",
        )
        units[projection.work_unit_id] = work_unit_status(projection)

    by_project_backlog: dict[str, list[BacklogItem]] = {}
    # Identity is scoped PER PROJECT: a portfolio may legitimately register
    # github:acme/forge and gitlab:acme/forge to different projects, and both
    # naturally use acme/forge#1 item ids. Within one project the ids stay
    # unique, because its tree joins by them.
    seen_items: set[tuple[str, str]] = set()
    for item in backlog:
        key = (item.project_id, item.item_id)
        _require(
            key not in seen_items,
            f"duplicate backlog itemId in project {item.project_id}: {item.item_id}",
        )
        seen_items.add(key)
        by_project_backlog.setdefault(item.project_id, []).append(item)

    by_project_incidents: dict[str, list[Incident]] = {}
    seen_incidents: set[tuple[str, str]] = set()
    for incident in incidents:
        key = (incident.project_id, incident.incident_id)
        _require(
            key not in seen_incidents,
            f"duplicate incidentId in project {incident.project_id}: {incident.incident_id}",
        )
        seen_incidents.add(key)
        by_project_incidents.setdefault(incident.project_id, []).append(incident)

    universe = set(registry.project_ids())
    observed = set(by_project_units) | set(by_project_backlog) | set(by_project_incidents)
    universe |= observed
    if project_ids is not None:
        requested = tuple(project_ids)
        missing = sorted(set(requested) - universe)
        _require(not missing, f"snapshot has no such project: {', '.join(missing)}")
        universe &= set(requested)
    _require(
        len(universe) <= MAX_PROJECTS,
        f"snapshot would hold {len(universe)} projects, more than the read model's "
        f"{MAX_PROJECTS}-project budget; narrow --project-id",
    )

    warnings: list[str] = []
    views: list[ProjectView] = []
    total_items = 0
    total_incidents = 0
    total_units = 0
    for project_id in sorted(universe):
        registered = project_id in registry.project_ids()
        statuses = by_project_units.get(project_id, {})
        items = by_project_backlog.get(project_id, [])
        project_incidents = tuple(by_project_incidents.get(project_id, ()))
        # The combined per-project budgets, cycle detection and cross-project
        # parent checks run AFTER --project-id filtering, on the projects the
        # snapshot actually renders: an oversized unselected project must not
        # stop the documented narrowing option from producing this one.
        _require(
            len(items) <= MAX_BACKLOG_ITEMS,
            f"project {project_id} combines more than {MAX_BACKLOG_ITEMS} backlog items",
        )
        _require(
            len(project_incidents) <= MAX_INCIDENTS,
            f"project {project_id} combines more than {MAX_INCIDENTS} incidents",
        )
        _require(
            len(statuses) <= MAX_WORK_UNITS,
            f"project {project_id} holds {len(statuses)} work units, more than the "
            f"read model's {MAX_WORK_UNITS}-unit budget; narrow the ledger export",
        )
        total_items += len(items)
        total_incidents += len(project_incidents)
        total_units += len(statuses)
        _require(
            total_items <= MAX_TOTAL_BACKLOG_ITEMS,
            f"snapshot would hold more than {MAX_TOTAL_BACKLOG_ITEMS} backlog items "
            "in all; narrow --project-id or the exports",
        )
        _require(
            total_incidents <= MAX_TOTAL_INCIDENTS,
            f"snapshot would hold more than {MAX_TOTAL_INCIDENTS} incidents in all; "
            "narrow --project-id or the exports",
        )
        _require(
            total_units <= MAX_TOTAL_WORK_UNITS,
            f"snapshot would hold more than {MAX_TOTAL_WORK_UNITS} work units in all; "
            "narrow --project-id or the ledger export",
        )
        _detect_backlog_cycles({item.item_id: item for item in items})
        # A parent id absent from this project is treated as a missing parent
        # (the item roots itself and the view warns), never matched against
        # other projects: identity is project-scoped, so a same-spelled item
        # in another project is an unrelated record, not proof of a
        # cross-project edge. Explicit cross-project parents are still
        # rejected where a document declares both items.
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
        else:
            # Registered data must belong to the registered repositories: a
            # mistyped --project-id would otherwise file another project's
            # issue tree and incidents under this profile without a trace.
            # Ownership is PROVIDER-QUALIFIED: gitlab:acme/forge does not own
            # GitHub-adapter data about acme/forge, so a reference matches
            # only a declaration whose provider agrees (or any declaration,
            # for data whose provider is unknown).
            declared_providers: dict[str, set[str]] = {}
            for repository in registry.get(project_id).repositories:
                declared_providers.setdefault(repository.identifier.lower(), set()).add(
                    repository.provider
                )

            def owned(
                name: str, provider: str, declared: Mapping[str, set[str]] = declared_providers
            ) -> bool:
                providers = declared.get(name.lower())
                if providers is None:
                    return False
                return not provider or provider in providers

            # Each field participates under ITS OWN contract: free-form
            # references ("build/123", a CI URL) count only when they carry
            # the full repo#number shape, while a status's repository field
            # is a bare identifier by contract. Ledger statuses carry no
            # adapter provider, so they stay name-only.
            references: list[tuple[str, str]] = []
            for item in items:
                references.extend(
                    (reference, item.source_provider)
                    for reference in (item.item_id, item.issue_ref, item.parent_id)
                )
            for status in statuses.values():
                references.extend(
                    (reference, "") for reference in (status.issue_ref, status.change_request_ref)
                )
            for incident in project_incidents:
                references.extend(
                    (reference, incident.source_provider)
                    for reference in (
                        incident.incident_id,
                        incident.evidence_ref,
                        incident.backlog_item_id,
                    )
                )
            foreign: set[tuple[str, str]] = set()
            for reference, provider in references:
                match = _REPOSITORY_REFERENCE.fullmatch(reference) if reference else None
                if match is not None and not owned(match.group("name"), provider):
                    foreign.add((match.group("name"), provider))
            malformed: set[str] = set()
            for status in statuses.values():
                name = status.repository
                if not name:
                    continue
                if _REPOSITORY_IDENTIFIER.fullmatch(name):
                    if not owned(name, ""):
                        foreign.add((name, ""))
                else:
                    # The ledger accepts free-form repository text, so a typo
                    # or URL would otherwise dodge the ownership check
                    # entirely; it is flagged rather than silently skipped.
                    malformed.add(name)
            if malformed:
                # The values are withheld: a malformed identifier is often a
                # clone URL, and URLs can embed credentials the snapshot (and
                # any store it lands in) must never republish.
                view_warnings.append(
                    f"project {project_id} ledger data names {len(malformed)} repository "
                    "identifier(s) that are not owner/name shaped; the values are "
                    "withheld because malformed identifiers can embed credentials - "
                    "fix the ledger entries so ownership checks can cover them"
                )
            for name, provider in sorted(foreign):
                declared_for = declared_providers.get(name.lower())
                if provider and declared_for:
                    view_warnings.append(
                        f"project {project_id} {provider} data references repository "
                        f"{name}, which its registry profile declares only for "
                        + ", ".join(sorted(declared_for))
                    )
                else:
                    view_warnings.append(
                        f"project {project_id} data references repository {name}, "
                        "which its registry profile does not declare"
                    )

        known_units = frozenset(statuses)
        known_items = frozenset(item.item_id for item in items)
        item_status: dict[str, WorkUnitStatus] = {}
        for item in items:
            _, basis, status = _resolve_state(item, statuses, by_issue_ref)
            if basis == STATE_BASIS_LEDGER and status is not None:
                item_status[item.item_id] = status
        # Ledger statuses by the change-request reference they carry, for
        # merge retirement. The ledger permits the same reference on more
        # than one unit, so merge proof is resolved per unit below.
        by_change_request: dict[str, list[WorkUnitStatus]] = {}
        for status in statuses.values():
            if status.change_request_ref:
                by_change_request.setdefault(status.change_request_ref, []).append(status)
        retained: list[Incident] = []
        for incident in project_incidents:
            # A change-request-scoped incident is moot once the ledger proves
            # that same change request merged: an incident file exported
            # before the merge (or racing it) must not tell the operator to
            # merge, refresh, or fix what is already in the base branch.
            # An incident NAMING a work unit is judged on that unit's own
            # history alone; otherwise reference-wide proof applies only when
            # every unit carrying the reference proves the merge, so one
            # unit's merge never hides another unit's real blocker.
            if incident.evidence_ref and _change_request_scoped(incident):
                if incident.work_unit_id:
                    named = statuses.get(incident.work_unit_id)
                    merged = (
                        named is not None
                        and named.change_request_ref == incident.evidence_ref
                        and _merge_proven(named)
                    )
                else:
                    carriers = by_change_request.get(incident.evidence_ref, [])
                    merged = bool(carriers) and all(_merge_proven(s) for s in carriers)
                if merged:
                    continue
            # A named work unit is direct ledger evidence; the backlog-item
            # join applies ONLY when no unit is named, so an incident naming
            # an unknown unit is never retired on some other unit's proof.
            if incident.work_unit_id:
                proven = statuses.get(incident.work_unit_id)
            else:
                proven = item_status.get(incident.backlog_item_id)
            if incident.kind in _LEDGER_RETIREMENT and proven is not None:
                # Label-derived incidents are outranked by durable ledger
                # proof that their gate was already passed: a label removed
                # after dispatch must not report a running project as blocked,
                # and a unit the ledger shows implementing needs no triage.
                # A state that does not imply the threshold (blocked,
                # cancelled) retires nothing.
                if _adapter_rule_incident(incident, PLAN_EVIDENCE_RULE):
                    # Dispatch proof (current or historical) moots the plan
                    # advisory entirely; proven approval replaces its guidance
                    # with the implementation hold; otherwise planned or
                    # approval-pending evidence, current or historical,
                    # retires it (a plan provably ran even if the unit later
                    # blocked).
                    seen = proven.states_seen
                    if proven.state in DISPATCH_PROVEN_STATES or seen & DISPATCH_PROVEN_STATES:
                        continue
                    if FactoryState.APPROVED in seen:
                        # Not an advisory any more: approval is proven and
                        # dispatch cannot occur without the label event, so
                        # the incident becomes a dispatch blocker. The hold's
                        # own summary names the CONFIGURED labels (a policy
                        # may rename them), so it is quoted rather than
                        # replaced with default names.
                        retained.append(
                            replace(
                                incident,
                                kind="dispatch-blocked",
                                severity=IncidentSeverity.BLOCKER,
                                summary=(
                                    "Plan and approval are proven by the ledger, but the "
                                    "bundled implementation trigger fires only on its "
                                    f"label event. The hold said: {incident.summary} - "
                                    "re-apply the implementation approval label it names "
                                    "to dispatch"
                                ),
                            )
                        )
                        continue
                    if proven.state in PLAN_PROVEN_STATES or seen & PLAN_PROVEN_STATES:
                        continue
                    retained.append(incident)
                    continue
                if _adapter_rule_incident(incident, RELEASED_SPEC_RULE) or _adapter_rule_incident(
                    incident, SPEC_WITHOUT_APPROVAL_RULE
                ):
                    # These rules report missing dispatch labels, not only the
                    # approval wait their kind shares: an approved unit still
                    # cannot fire without the label events, so only dispatch
                    # proof retires them. Once approval IS proven, the hold is
                    # re-stated as the dispatch-label gap it has become, so
                    # the operator is not told to repeat a finished approval.
                    seen = proven.states_seen
                    if proven.state in DISPATCH_PROVEN_STATES or seen & DISPATCH_PROVEN_STATES:
                        continue
                    if FactoryState.APPROVED in seen:
                        # The hold's own summary already names the project's
                        # CONFIGURED labels (a policy may rename them), so the
                        # replacement quotes it rather than substituting
                        # default names an operator may not have.
                        retained.append(
                            replace(
                                incident,
                                kind="dispatch-blocked",
                                severity=IncidentSeverity.BLOCKER,
                                summary=(
                                    "Approval is proven by the ledger, but no dispatch "
                                    f"label follows it. The hold said: {incident.summary} "
                                    "- apply the label(s) it names so a trigger can fire"
                                ),
                            )
                        )
                        continue
                    retained.append(incident)
                    continue
                else:
                    proven_states = _LEDGER_RETIREMENT[incident.kind]
                    history_states = proven_states
                # Current state or durable history: a unit that crossed the
                # gate and later re-entered a pre-gate state (dispatched,
                # then blocked) still proved the gate passed.
                if proven.state in proven_states or proven.states_seen & history_states:
                    continue
            retained.append(incident)
        project_incidents = tuple(retained)
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
            if status.state in EXECUTING_STATES
            and not status.needs_attention
            and (status.state not in REVIEW_GATE_STATES or status.activity)
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
                warnings=_bounded_warnings(view_warnings),
                **fields,
            )
        )

    sources_tuple = _string_tuple(list(sources), "sources")
    _require(
        len(sources_tuple) <= MAX_SOURCES,
        f"snapshot would record {len(sources_tuple)} sources, more than the read "
        f"model's {MAX_SOURCES}-source budget",
    )
    return ControlCenterSnapshot(
        generated_at=generated_at,
        projects=tuple(views),
        sources=sources_tuple,
        warnings=_bounded_warnings(warnings),
    )


def read_control_center_inputs(
    *,
    registry_path: str | None = None,
    backlog_paths: Sequence[str] = (),
    incidents_paths: Sequence[str] = (),
) -> tuple[ProjectRegistry, tuple[BacklogItem, ...], tuple[Incident, ...]]:
    """Read every declared Control Center input.

    ``backlog_paths`` and ``incidents_paths`` take one document per exported
    project, so a portfolio snapshot is built from each adapter's output
    directly, with no hand-merged JSON. Duplicate item or incident identifiers
    across documents are rejected by the snapshot builder.
    """

    if registry_path is None or not str(registry_path).strip():
        registry = read_optional_project_registry(None)
    else:
        # An explicitly named registry that cannot be read is a deployment
        # error: a typo must fail the snapshot, never publish an empty or
        # wholly unregistered portfolio.
        registry = read_project_registry(registry_path)
    backlog: list[BacklogItem] = []
    for path in backlog_paths:
        backlog.extend(read_backlog_document(path))
    incidents: list[Incident] = []
    for path in incidents_paths:
        incidents.extend(read_incident_document(path))
    return registry, tuple(backlog), tuple(incidents)


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
