"""Generic autonomous work intake: discover, normalize, dedupe, prioritize.

Events from SCM, trackers, CI, scanners, and alerts become versioned
``WorkRequest`` records with deterministic fingerprints, explainable
priorities, and policy-driven routing. External systems can propose work but
can never bypass repository policy, grant themselves write authority, or set
their own priority opaquely.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

__all__ = [
    "INTAKE_CONTRACTS",
    "GitHubCIIntake",
    "GitHubIssueIntake",
    "GitHubPullRequestIntake",
    "IntakeAdapter",
    "IntakeError",
    "IntakeSource",
    "WorkFingerprint",
    "WorkPriority",
    "WorkQueue",
    "WorkRequest",
    "check_intake_conformance",
    "load_intake_sources",
    "prioritize",
    "route",
]

PRIORITY_FORMULA_VERSION = "1.0.0"


class IntakeError(ValueError):
    """Raised when an intake event, source, or rule is invalid."""


INTAKE_SOURCE_TYPES = frozenset(
    {
        "github-issues",
        "github-pulls",
        "github-ci",
        "gitlab-issues",
        "jira",
        "webhook",
        "scanner",
        "observability",
        "code-quality",
        "scheduled-maintenance",
        "platform-efficacy",
        "rest-api",
    }
)
WORK_TYPES = frozenset(
    {
        "bug",
        "feature",
        "maintenance",
        "security",
        "data-quality",
        "documentation",
        "test",
        "investigation",
    }
)
SEVERITIES = ("low", "medium", "high", "critical")
LIFECYCLE_STATES = frozenset(
    {"new", "deduplicated", "planned", "approved", "active", "blocked", "ignored"}
)
# The only actions an intake source can ever hold. Closing or updating an
# external ticket is a separate mission with its own policy, never an intake
# permission.
ALLOWED_SOURCE_ACTIONS = frozenset({"create-work", "update-work"})
PREEMPTIVE_TYPES = frozenset({"security", "data-quality"})
_SOURCE_ID = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
_SOURCE_KEYS = {"id", "type", "provider", "permitted_actions", "credential_ref"}


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical(document: Any) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class IntakeSource:
    """A declared, authenticated origin of work events."""

    source_id: str
    source_type: str
    provider: str
    permitted_actions: tuple[str, ...] = ("create-work", "update-work")
    credential_ref: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "sourceId": self.source_id,
            "type": self.source_type,
            "provider": self.provider,
            "permittedActions": list(self.permitted_actions),
            "credentialRef": self.credential_ref,
        }


def load_intake_sources(path: str | Path) -> dict[str, IntakeSource]:
    """Load declared intake sources from protected configuration."""
    with Path(path).open("rb") as handle:
        document = tomllib.load(handle)
    if document.get("version") != 1:
        raise IntakeError("intake configuration version must be 1")
    entries = document.get("source", [])
    if not isinstance(entries, list) or not entries:
        raise IntakeError("intake configuration must declare at least one [[source]]")
    sources: dict[str, IntakeSource] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise IntakeError("each intake source must be a TOML table")
        unknown = sorted(set(entry) - _SOURCE_KEYS)
        if unknown:
            raise IntakeError("intake source declares unknown keys: " + ", ".join(unknown))
        source_id = entry.get("id")
        if not isinstance(source_id, str) or not _SOURCE_ID.fullmatch(source_id):
            raise IntakeError("intake source id must match " + _SOURCE_ID.pattern)
        if source_id in sources:
            raise IntakeError(f"duplicate intake source id: {source_id}")
        source_type = entry.get("type")
        if source_type not in INTAKE_SOURCE_TYPES:
            raise IntakeError(f"intake source {source_id}: unknown type: {source_type}")
        provider = entry.get("provider")
        if not isinstance(provider, str) or not provider.strip():
            raise IntakeError(f"intake source {source_id}: provider is required")
        actions = entry.get("permitted_actions", ["create-work", "update-work"])
        if not isinstance(actions, list) or not all(isinstance(item, str) for item in actions):
            raise IntakeError(f"intake source {source_id}: permitted_actions must be strings")
        excess = sorted(set(actions) - ALLOWED_SOURCE_ACTIONS)
        if excess:
            raise IntakeError(
                f"intake source {source_id}: sources cannot hold write authority: "
                + ", ".join(excess)
            )
        credential_ref = entry.get("credential_ref", "")
        if not isinstance(credential_ref, str):
            raise IntakeError(f"intake source {source_id}: credential_ref must be a string")
        sources[source_id] = IntakeSource(
            source_id=source_id,
            source_type=source_type,
            provider=provider.strip(),
            permitted_actions=tuple(actions),
            credential_ref=credential_ref.strip(),
        )
    return sources


@dataclass(frozen=True)
class WorkFingerprint:
    """Deterministic identity used to converge duplicate events."""

    work_type: str
    project: str
    component: str
    signature: str

    @property
    def digest(self) -> str:
        return _sha256_text(
            _canonical([self.work_type, self.project, self.component, self.signature])
        )[:24]

    def as_dict(self) -> dict[str, str]:
        return {
            "workType": self.work_type,
            "project": self.project,
            "component": self.component,
            "signature": self.signature,
            "digest": self.digest,
        }


@dataclass(frozen=True)
class WorkRequest:
    """A versioned, normalized unit of proposed work."""

    source_id: str
    source_identifier: str
    project: str
    work_type: str
    severity: str
    title: str
    problem_statement: str
    fingerprint: WorkFingerprint
    evidence: tuple[str, ...] = ()
    affected_components: tuple[str, ...] = ()
    candidate_paths: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    linked_work: tuple[str, ...] = ()
    requested_outcome: str = ""
    acceptance_criteria: tuple[str, ...] = ()
    risk_inputs: tuple[str, ...] = ()
    confidence: int = 100
    event_timestamp: str = ""
    received_at: str = ""
    lifecycle_state: str = "new"
    human_approval_required: bool = True
    version: int = 1

    @property
    def request_id(self) -> str:
        return "wr-" + self.fingerprint.digest

    def as_dict(self) -> dict[str, Any]:
        return {
            "requestId": self.request_id,
            "sourceId": self.source_id,
            "sourceIdentifier": self.source_identifier,
            "project": self.project,
            "workType": self.work_type,
            "severity": self.severity,
            "title": self.title,
            "problemStatement": self.problem_statement,
            "fingerprint": self.fingerprint.as_dict(),
            "evidence": list(self.evidence),
            "affectedComponents": list(self.affected_components),
            "candidatePaths": list(self.candidate_paths),
            "dependencies": list(self.dependencies),
            "linkedWork": list(self.linked_work),
            "requestedOutcome": self.requested_outcome,
            "acceptanceCriteria": list(self.acceptance_criteria),
            "riskInputs": list(self.risk_inputs),
            "confidence": self.confidence,
            "eventTimestamp": self.event_timestamp,
            "receivedAt": self.received_at,
            "lifecycleState": self.lifecycle_state,
            "humanApprovalRequired": self.human_approval_required,
            "version": self.version,
        }


def _validate_request(request: WorkRequest) -> None:
    if request.work_type not in WORK_TYPES:
        raise IntakeError(f"unknown work type: {request.work_type}")
    if request.severity not in SEVERITIES:
        raise IntakeError(f"unknown severity: {request.severity}")
    if not request.title.strip():
        raise IntakeError("work request title is required")
    if not request.project.strip():
        raise IntakeError("work request project is required")
    if not request.source_identifier.strip():
        raise IntakeError("work request source identifier is required")
    if not 0 <= request.confidence <= 100:
        raise IntakeError("confidence must be between 0 and 100")
    if request.lifecycle_state not in LIFECYCLE_STATES:
        raise IntakeError(f"unknown lifecycle state: {request.lifecycle_state}")


class IntakeAdapter:
    """Base intake adapter: authenticate the source, then normalize."""

    source_type = ""

    def normalize(
        self,
        source: IntakeSource,
        payload: Mapping[str, Any],
        *,
        received_at: str = "",
    ) -> WorkRequest:
        raise NotImplementedError

    def _check_source(self, source: IntakeSource) -> None:
        if source.source_type != self.source_type:
            raise IntakeError(
                f"source {source.source_id} ({source.source_type}) cannot feed a "
                f"{self.source_type} adapter"
            )
        if "create-work" not in source.permitted_actions:
            raise IntakeError(f"source {source.source_id} is not permitted to create work")


class GitHubIssueIntake(IntakeAdapter):
    source_type = "github-issues"

    _TYPE_LABELS = {
        "bug": "bug",
        "security": "security",
        "enhancement": "feature",
        "feature": "feature",
        "documentation": "documentation",
        "maintenance": "maintenance",
        "test": "test",
    }

    def normalize(
        self,
        source: IntakeSource,
        payload: Mapping[str, Any],
        *,
        received_at: str = "",
    ) -> WorkRequest:
        self._check_source(source)
        issue = payload.get("issue")
        repository = payload.get("repository", {})
        if not isinstance(issue, Mapping) or not isinstance(repository, Mapping):
            raise IntakeError("GitHub issue payload requires issue and repository")
        project = str(repository.get("full_name", ""))
        number = issue.get("number")
        if number is None or not project:
            raise IntakeError("GitHub issue payload is missing number or repository")
        labels = {
            str(item.get("name", "")) if isinstance(item, Mapping) else str(item)
            for item in issue.get("labels", [])
        }
        work_type = next(
            (self._TYPE_LABELS[label] for label in sorted(labels) if label in self._TYPE_LABELS),
            "investigation",
        )
        severity = "high" if work_type == "security" else "medium"
        request = WorkRequest(
            source_id=source.source_id,
            source_identifier=f"{project}#{number}",
            project=project,
            work_type=work_type,
            severity=severity,
            title=str(issue.get("title", "")),
            problem_statement=str(issue.get("body") or ""),
            fingerprint=WorkFingerprint(work_type, project, "issues", f"issue-{number}"),
            evidence=(str(issue.get("html_url", "")),),
            event_timestamp=str(issue.get("updated_at") or issue.get("created_at") or ""),
            received_at=received_at,
        )
        _validate_request(request)
        return request


class GitHubPullRequestIntake(IntakeAdapter):
    """Review findings and PR follow-ups become linked work requests."""

    source_type = "github-pulls"

    def normalize(
        self,
        source: IntakeSource,
        payload: Mapping[str, Any],
        *,
        received_at: str = "",
    ) -> WorkRequest:
        self._check_source(source)
        pull = payload.get("pull_request")
        repository = payload.get("repository", {})
        if not isinstance(pull, Mapping) or not isinstance(repository, Mapping):
            raise IntakeError("GitHub PR payload requires pull_request and repository")
        project = str(repository.get("full_name", ""))
        number = pull.get("number")
        finding = str(payload.get("finding") or "")
        if number is None or not project or not finding.strip():
            raise IntakeError("GitHub PR finding payload is missing number, repository, or finding")
        request = WorkRequest(
            source_id=source.source_id,
            source_identifier=f"{project}#{number}",
            project=project,
            work_type="bug",
            severity=str(payload.get("severity", "medium")),
            title=f"Review finding on PR #{number}: {finding[:80]}",
            problem_statement=finding,
            fingerprint=WorkFingerprint(
                "bug", project, "pull-request", f"pr-{number}-{_sha256_text(finding)[:12]}"
            ),
            evidence=(str(pull.get("html_url", "")),),
            linked_work=(f"{project}#{number}",),
            event_timestamp=str(pull.get("updated_at") or ""),
            received_at=received_at,
        )
        _validate_request(request)
        return request


class GitHubCIIntake(IntakeAdapter):
    """CI failures correlate with their PR/commit instead of opening
    unrelated issues: the fingerprint is workflow + head branch, and the
    triggering PR/commit lands in linked work."""

    source_type = "github-ci"

    def normalize(
        self,
        source: IntakeSource,
        payload: Mapping[str, Any],
        *,
        received_at: str = "",
    ) -> WorkRequest:
        self._check_source(source)
        run = payload.get("workflow_run")
        repository = payload.get("repository", {})
        if not isinstance(run, Mapping) or not isinstance(repository, Mapping):
            raise IntakeError("GitHub CI payload requires workflow_run and repository")
        project = str(repository.get("full_name", ""))
        conclusion = str(run.get("conclusion", ""))
        if not project or not run.get("name"):
            raise IntakeError("GitHub CI payload is missing repository or workflow name")
        if conclusion != "failure":
            raise IntakeError(f"only failed runs become work: conclusion={conclusion or 'none'}")
        workflow = str(run.get("name"))
        branch = str(run.get("head_branch", ""))
        head_sha = str(run.get("head_sha", ""))
        pulls = tuple(
            f"{project}#{item.get('number')}"
            for item in run.get("pull_requests", [])
            if isinstance(item, Mapping) and item.get("number") is not None
        )
        request = WorkRequest(
            source_id=source.source_id,
            source_identifier=f"{project}:run-{run.get('id', 'unknown')}",
            project=project,
            work_type="bug",
            severity="medium",
            title=f"CI failure: {workflow} on {branch or head_sha[:12]}",
            problem_statement=(
                f"Workflow {workflow} concluded failure for {head_sha or 'unknown sha'}."
            ),
            fingerprint=WorkFingerprint("bug", project, "ci", f"{workflow}@{branch}"),
            evidence=(str(run.get("html_url", "")),),
            linked_work=pulls or ((f"{project}@{head_sha}",) if head_sha else ()),
            confidence=70,
            event_timestamp=str(run.get("updated_at") or ""),
            received_at=received_at,
        )
        _validate_request(request)
        return request


INTAKE_CONTRACTS: dict[str, dict[str, Any]] = {
    "jira": {
        "sourceType": "jira",
        "requiredFields": ["key", "issuetype", "summary", "priority", "updated"],
        "authentication": "least-privilege API token via credential_ref",
        "writeSupport": False,
    },
    "gitlab-issues": {
        "sourceType": "gitlab-issues",
        "requiredFields": ["object_kind", "object_attributes", "project"],
        "authentication": "webhook secret token",
        "writeSupport": False,
    },
    "webhook": {
        "sourceType": "webhook",
        "requiredFields": ["source", "signature", "event", "body"],
        "authentication": "HMAC signature over the raw body",
        "writeSupport": False,
    },
    "scanner": {
        "sourceType": "scanner",
        "requiredFields": ["tool", "ruleId", "severity", "location", "message"],
        "authentication": "authenticated upload or trusted CI artifact",
        "writeSupport": False,
    },
    "rest-api": {
        "sourceType": "rest-api",
        "requiredFields": ["submitter", "title", "problem", "authorization"],
        "authentication": "bearer token bound to a declared source",
        "writeSupport": False,
    },
}


def check_intake_conformance(
    adapter: IntakeAdapter,
    source: IntakeSource,
    fixtures: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Prove an intake adapter is deterministic, validated, and read-only."""
    if not fixtures:
        raise IntakeError("conformance requires at least one fixture payload")
    if any(callable(getattr(adapter, name, None)) for name in ("write", "close", "resolve")):
        raise IntakeError("intake adapters must not expose ticket write operations")
    ids = []
    for fixture in fixtures:
        first = adapter.normalize(source, fixture)
        second = adapter.normalize(source, fixture)
        if first.request_id != second.request_id:
            raise IntakeError("intake normalization is not deterministic")
        _validate_request(first)
        ids.append(first.request_id)
    return {
        "adapter": type(adapter).__name__,
        "sourceType": adapter.source_type,
        "fixtures": len(fixtures),
        "requestIds": ids,
        "passed": True,
    }


@dataclass(frozen=True)
class WorkPriority:
    """Explainable priority: every component score carries its reasons."""

    urgency: int
    impact: int
    confidence: int
    effort: int
    risk: int
    reasons: tuple[str, ...]
    preemptive: bool

    @property
    def total(self) -> int:
        # Versioned, explicit formula — never an opaque model ordering.
        weighted = (
            0.3 * self.urgency
            + 0.3 * self.impact
            + 0.2 * self.risk
            + 0.1 * self.confidence
            + 0.1 * (100 - self.effort)
        )
        return min(100, round(weighted) + (20 if self.preemptive else 0))

    def as_dict(self) -> dict[str, Any]:
        return {
            "formulaVersion": PRIORITY_FORMULA_VERSION,
            "components": {
                "urgency": self.urgency,
                "impact": self.impact,
                "confidence": self.confidence,
                "effort": self.effort,
                "risk": self.risk,
            },
            "weights": {
                "urgency": 0.3,
                "impact": 0.3,
                "risk": 0.2,
                "confidence": 0.1,
                "inverseEffort": 0.1,
            },
            "preemptive": self.preemptive,
            "total": self.total,
            "reasons": list(self.reasons),
            "advisory": "effort and urgency estimates are advisory and calibrated over time",
        }


_SEVERITY_URGENCY = {"low": 25, "medium": 50, "high": 80, "critical": 100}


def prioritize(
    request: WorkRequest,
    *,
    impact_override: int | None = None,
) -> WorkPriority:
    """Deterministic, explainable priority for a normalized work request.

    ``impact_override`` lets a consumer express business impact, but the
    platform safety floor stands: high-severity security/data work stays
    preemptive no matter what the override says.
    """
    reasons = []
    urgency = _SEVERITY_URGENCY[request.severity]
    reasons.append(f"urgency {urgency} from severity {request.severity}")

    impact = 60 if request.work_type in PREEMPTIVE_TYPES else 40
    if impact_override is not None:
        if not 0 <= impact_override <= 100:
            raise IntakeError("impact override must be between 0 and 100")
        impact = impact_override
        reasons.append(f"impact {impact} set by consumer override")
    else:
        reasons.append(f"impact {impact} from work type {request.work_type}")

    effort = min(90, 30 + 10 * len(request.candidate_paths))
    reasons.append(f"effort {effort} from {len(request.candidate_paths)} candidate path(s)")
    risk = 80 if request.work_type in PREEMPTIVE_TYPES else 40
    reasons.append(f"risk {risk} from work type {request.work_type}")

    preemptive = request.work_type in PREEMPTIVE_TYPES and request.severity in (
        "high",
        "critical",
    )
    if preemptive:
        reasons.append(
            "preemptive: high-severity security/data signal preempts ordinary work "
            "(platform rule; consumer overrides cannot remove it)"
        )
    return WorkPriority(
        urgency=urgency,
        impact=impact,
        confidence=request.confidence,
        effort=effort,
        risk=risk,
        reasons=tuple(reasons),
        preemptive=preemptive,
    )


@dataclass(frozen=True)
class RoutingRules:
    """Consumer policy for what happens after normalization."""

    auto_plan_types: tuple[str, ...] = ("documentation", "test", "maintenance")
    approval_required_types: tuple[str, ...] = ("security", "data-quality")
    ignore_signatures: tuple[str, ...] = ()
    min_confidence: int = 60


def route(request: WorkRequest, rules: RoutingRules) -> dict[str, str]:
    """Route a work request to a mission entry point or block it with a reason.

    Low-confidence inferred work is always an investigation first; protected
    work types always require human approval but keep their prepared evidence.
    """
    if request.fingerprint.signature in rules.ignore_signatures:
        return {"action": "ignore", "reason": "signature is on the configured ignore list"}
    if request.confidence < rules.min_confidence:
        return {
            "action": "investigate",
            "reason": (
                f"confidence {request.confidence} below {rules.min_confidence}; "
                "low-confidence inferred issues stay investigations"
            ),
        }
    if request.work_type in rules.approval_required_types:
        return {
            "action": "approval-required",
            "reason": f"work type {request.work_type} requires human approval; "
            "prepared evidence is preserved",
        }
    if request.work_type in rules.auto_plan_types and request.severity in ("low", "medium"):
        return {"action": "auto-plan", "reason": "low-risk work type is eligible for auto-planning"}
    return {
        "action": "approval-required",
        "reason": "default: human approval before planning",
    }


class WorkQueue:
    """Deduplicating queue of normalized work requests."""

    def __init__(self) -> None:
        self._requests: dict[str, WorkRequest] = {}
        self._log: list[str] = []

    def submit(self, request: WorkRequest) -> WorkRequest:
        """Add or converge a work request; duplicates merge, never fork."""
        _validate_request(request)
        existing = self._requests.get(request.request_id)
        if existing is None:
            self._requests[request.request_id] = request
            self._log.append(f"created {request.request_id} from {request.source_identifier}")
            return request
        merged_evidence = tuple(dict.fromkeys((*existing.evidence, *request.evidence)))
        merged_links = tuple(dict.fromkeys((*existing.linked_work, *request.linked_work)))
        updated = replace(
            existing,
            evidence=merged_evidence,
            linked_work=merged_links,
            event_timestamp=request.event_timestamp or existing.event_timestamp,
            version=existing.version + 1,
            lifecycle_state=(
                "deduplicated" if existing.lifecycle_state == "new" else existing.lifecycle_state
            ),
        )
        self._requests[request.request_id] = updated
        self._log.append(
            f"converged {request.source_identifier} into {existing.request_id} "
            f"(version {updated.version})"
        )
        return updated

    def add_evidence(
        self,
        request_id: str,
        evidence: str,
        *,
        contradictory: bool = False,
    ) -> WorkRequest:
        """Attach new evidence; contradictions send the work back to triage."""
        existing = self._requests.get(request_id)
        if existing is None:
            raise IntakeError(f"unknown work request: {request_id}")
        updated = replace(
            existing,
            evidence=tuple(dict.fromkeys((*existing.evidence, evidence))),
            version=existing.version + 1,
            lifecycle_state="new" if contradictory else existing.lifecycle_state,
        )
        self._requests[request_id] = updated
        if contradictory:
            self._log.append(f"contradictory evidence returned {request_id} to triage: {evidence}")
        return updated

    def set_state(self, request_id: str, state: str) -> WorkRequest:
        if state not in LIFECYCLE_STATES:
            raise IntakeError(f"unknown lifecycle state: {state}")
        existing = self._requests.get(request_id)
        if existing is None:
            raise IntakeError(f"unknown work request: {request_id}")
        updated = replace(existing, lifecycle_state=state)
        self._requests[request_id] = updated
        return updated

    def get(self, request_id: str) -> WorkRequest:
        request = self._requests.get(request_id)
        if request is None:
            raise IntakeError(f"unknown work request: {request_id}")
        return request

    def report(self) -> dict[str, Any]:
        """Machine-readable queue snapshot grouped by lifecycle state."""
        by_state: dict[str, list[str]] = {}
        for request in self._requests.values():
            by_state.setdefault(request.lifecycle_state, []).append(request.request_id)
        return {
            "schemaVersion": 1,
            "total": len(self._requests),
            "byState": {state: sorted(ids) for state, ids in sorted(by_state.items())},
            "requests": [request.as_dict() for _, request in sorted(self._requests.items())],
            "log": list(self._log),
        }
