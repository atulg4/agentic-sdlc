"""Outcome effectiveness, learning ledger and value-per-agent-hour scoring.

The lifecycle ledger says what the factory *did*; the usage ledger says what it
*consumed*. This module answers the only question that matters to the person
paying for it: did a real engineering outcome arrive, and what did it take?

One ``OutcomeRecord`` links an intended outcome and its acceptance criteria to
the project, work unit, issue, change request and release that delivered it,
carries the deployment status and verification evidence, and accumulates
post-merge observations of what actually happened in the world. One
``LearningRecord`` captures a lesson — what caused delay or rework, which
model and stage combination performed well or badly, which deterministic check
could replace a recurring AI review finding — with its provenance attached.

Four rules keep the scoring honest:

- **Value is never inferred from effort.** The numerator of every value metric
  is a count of *accepted outcomes*. Tokens, runtime, lines of code, commits
  and change-request counts appear only as denominators or context, never as
  evidence that something valuable was produced.
- **Unknown is unknown.** A rate whose denominator is zero, or whose inputs
  were never observed, reports ``status: "unknown"`` with the reason — never a
  zero, and never an invented figure. Outcomes with no data stay in the
  sample and are counted as unobserved.
- **Subscriptions are not dollars.** Subscription usage is reported as a
  labeled pay-as-you-go *equivalent*, separately from billed spend, exactly as
  the usage ledger records it.
- **Learnings advise, they never act.** Every learning and recommendation is
  evidence with an explanation. Nothing here changes routing, estimation,
  verification or spend; a separately approved policy change must adopt it.

Outcome records may be completed as a work unit progresses — a merge, then a
deployment, then an observation — but a known value is never silently
rewritten: a contradicting append fails closed. Learning records are strictly
append-only and versioned; a revision supersedes its predecessor by ID rather
than replacing it.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, fields, replace
from enum import StrEnum
from typing import Any

from .capacity_metrics import ObservationWindow
from .event_ledger import LedgerError, LifecycleStage, WorkUnitRef, load_work_unit_ref
from .project_registry import EMPTY_PROJECT_REGISTRY, ProjectRegistry
from .usage_ledger import MonetaryStatus, UsageRecord, UsageResult, parse_timestamp

__all__ = [
    "ACCEPTED_RESULTS",
    "EFFECTIVENESS_SCHEMA_VERSION",
    "EFFECTIVENESS_SCHEMA_TYPES",
    "LEARNING_LEDGER_SCHEMA_VERSION",
    "LEARNING_SCHEMA_VERSION",
    "MAX_TEXT_LENGTH",
    "OUTCOME_LEDGER_SCHEMA_VERSION",
    "OUTCOME_SCHEMA_VERSION",
    "COMPARABLE_METRICS",
    "ESTIMATE_COMPARISONS",
    "LIMITATIONS",
    "AcceptanceCriterion",
    "DeploymentStatus",
    "EffectivenessError",
    "EffectivenessLedger",
    "LearningKind",
    "LearningLedger",
    "LearningRecord",
    "ObservationKind",
    "OutcomeObservation",
    "OutcomeProvenance",
    "OutcomeRecord",
    "OutcomeResult",
    "build_effectiveness_report",
    "compare_projects",
    "learning_id_for",
    "load_learning_record",
    "load_outcome_record",
    "outcome_id_for",
    "outcomes_from_usage",
    "secret_bearing_effectiveness_fields",
]

OUTCOME_SCHEMA_VERSION = 1
OUTCOME_LEDGER_SCHEMA_VERSION = 1
LEARNING_SCHEMA_VERSION = 1
LEARNING_LEDGER_SCHEMA_VERSION = 1
EFFECTIVENESS_SCHEMA_VERSION = 1

#: Free text is bounded so a raw prompt or transcript cannot be smuggled into an
#: outcome or learning record.
MAX_TEXT_LENGTH = 512

_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")
_SECRET_KEY = re.compile(
    r"credential|secret|token(?!s$)|password|passphrase|api_?key|authorization|bearer"
    r"|private_?key|prompt(?!sha256$)|messages|completion_text",
    re.I,
)
_ACTOR_KINDS = frozenset({"human", "agent", "system"})

#: Seconds in the units the value metrics are expressed per.
_SECONDS_PER_HOUR = 3600.0
_SECONDS_PER_DAY = 86400.0
_TOKENS_PER_MILLION = 1_000_000.0


class EffectivenessError(ValueError):
    """Raised when an outcome, learning, or effectiveness input is invalid."""


class OutcomeResult(StrEnum):
    """Whether the intended outcome actually arrived."""

    DELIVERED = "delivered"
    PARTIALLY_DELIVERED = "partially-delivered"
    NOT_DELIVERED = "not-delivered"
    REVERTED = "reverted"
    ABANDONED = "abandoned"
    UNKNOWN = "unknown"


#: Only a fully delivered outcome counts toward value. Partial delivery is
#: visible in the result breakdown but is never scored as an accepted outcome,
#: because "some of it works" is not a delivered capability.
ACCEPTED_RESULTS = frozenset({OutcomeResult.DELIVERED})


class DeploymentStatus(StrEnum):
    """How far a delivered change got toward being live, as observed."""

    NOT_APPLICABLE = "not-applicable"
    NOT_DEPLOYED = "not-deployed"
    DEPLOYING = "deploying"
    DEPLOYED = "deployed"
    VERIFIED = "verified"
    ROLLED_BACK = "rolled-back"
    UNKNOWN = "unknown"


class ObservationKind(StrEnum):
    """What a post-merge or post-deploy observation observed."""

    FEATURE_EXERCISED = "feature-exercised"
    TESTS_HEALTHY = "tests-healthy"
    TELEMETRY_HEALTHY = "telemetry-healthy"
    CAPABILITY_VERIFIED = "user-visible-capability-verified"
    NO_OBSERVABLE_ADOPTION = "no-observable-adoption-yet"
    REGRESSION_OBSERVED = "regression-observed"
    DEFECT_FOLLOWUP = "defect-followup"


class LearningKind(StrEnum):
    """What kind of lesson a learning record carries."""

    DELAY_CAUSE = "delay-cause"
    REWORK_CAUSE = "rework-cause"
    MODEL_STAGE_PERFORMANCE = "model-stage-performance"
    DETERMINISTIC_CHECK_CANDIDATE = "deterministic-check-candidate"
    ESTIMATOR_ADJUSTMENT = "estimator-adjustment"
    ROUTING_ADJUSTMENT = "routing-adjustment"


# -- validation helpers -------------------------------------------------------


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise EffectivenessError(message)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    _require(isinstance(value, Mapping), f"{label} must be an object")
    assert isinstance(value, Mapping)
    for key in value:
        _require(isinstance(key, str), f"{label} keys must be strings")
        _require(
            _SECRET_KEY.search(key) is None,
            f"{label} must not carry credentials, secrets or prompt text: {key}",
        )
    return value


def _known_keys(entry: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(entry) - allowed)
    if unknown:
        raise EffectivenessError(f"{label} declares unknown keys: " + ", ".join(unknown))


def _text(entry: Mapping[str, Any], key: str, label: str, *, required: bool = False) -> str:
    value = entry.get(key, "")
    if value is None:
        value = ""
    _require(isinstance(value, str), f"{label}: {key} must be a string")
    assert isinstance(value, str)
    value = value.strip()
    _require(not required or bool(value), f"{label}: {key} is required")
    _require(
        len(value) <= MAX_TEXT_LENGTH,
        f"{label}: {key} exceeds {MAX_TEXT_LENGTH} characters; record a reference, "
        "not a transcript",
    )
    return value


def _bounded(value: str, label: str) -> str:
    _require(
        len(value) <= MAX_TEXT_LENGTH,
        f"{label} exceeds {MAX_TEXT_LENGTH} characters; record a reference, not a transcript",
    )
    return value


def _timestamp(value: str, label: str) -> str:
    _require(bool(_TIMESTAMP.fullmatch(value)), f"{label} must be an RFC 3339 timestamp: {value!r}")
    return value


def _optional_timestamp(entry: Mapping[str, Any], key: str, label: str) -> str:
    value = _text(entry, key, label)
    if value:
        _timestamp(value, f"{label}: {key}")
    return value


def _optional_bool(entry: Mapping[str, Any], key: str, label: str) -> bool | None:
    value = entry.get(key)
    if value is None:
        return None
    _require(isinstance(value, bool), f"{label}: {key} must be a boolean or null")
    assert isinstance(value, bool)
    return value


def _optional_count(entry: Mapping[str, Any], key: str, label: str) -> int | None:
    value = entry.get(key)
    if value is None:
        return None
    _require(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0,
        f"{label}: {key} must be a non-negative integer or null",
    )
    assert isinstance(value, int)
    return value


def _non_negative_count(value: int | None, label: str) -> None:
    if value is None:
        return
    _require(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0,
        f"{label} must be a non-negative integer or null",
    )


def _string_tuple(value: Any, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    _require(isinstance(value, list), f"{label} must be an array of strings")
    assert isinstance(value, list)
    items = []
    for item in value:
        _require(isinstance(item, str) and item.strip(), f"{label} must be non-empty strings")
        assert isinstance(item, str)
        items.append(_bounded(item.strip(), label))
    return tuple(items)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical(document: Any) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _work_unit_ref(document: Any, label: str) -> WorkUnitRef:
    """Parse a shared work-unit reference, reporting it as this module's error.

    The reference shape is shared with the lifecycle and usage ledgers, so the
    parsing is too; only the exception type is translated, so one module raises
    one error type.
    """
    try:
        return load_work_unit_ref(document)
    except LedgerError as error:
        raise EffectivenessError(f"{label}: {error}") from error


def _enum(enum_type: type[StrEnum], value: str, label: str) -> Any:
    try:
        return enum_type(value)
    except ValueError as error:
        raise EffectivenessError(f"{label}: unknown {enum_type.__name__}: {value}") from error


# -- outcome records ----------------------------------------------------------


@dataclass(frozen=True)
class AcceptanceCriterion:
    """One acceptance criterion, and whether delivery actually met it."""

    text: str
    met: bool | None = None
    evidence_ref: str = ""

    def __post_init__(self) -> None:
        _require(bool(self.text.strip()), "acceptance criterion: text is required")
        _bounded(self.text, "acceptance criterion text")

    def as_dict(self) -> dict[str, Any]:
        return {"text": self.text, "met": self.met, "evidenceRef": self.evidence_ref}


@dataclass(frozen=True)
class OutcomeObservation:
    """One post-merge or post-deploy observation of what actually happened."""

    observation_id: str
    kind: ObservationKind
    observed_at: str
    observer: str
    observer_kind: str = "system"
    healthy: bool | None = None
    environment: str = ""
    note: str = ""
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require(bool(self.observation_id.strip()), "observation: observationId is required")
        _require(bool(self.observer.strip()), "observation: observer is required")
        _require(
            self.observer_kind in _ACTOR_KINDS,
            f"observation: unknown observer kind: {self.observer_kind}",
        )
        _timestamp(self.observed_at, "observation: observedAt")
        _bounded(self.note, "observation note")

    def as_dict(self) -> dict[str, Any]:
        return {
            "observationId": self.observation_id,
            "kind": self.kind.value,
            "observedAt": self.observed_at,
            "observer": self.observer,
            "observerKind": self.observer_kind,
            "healthy": self.healthy,
            "environment": self.environment,
            "note": self.note,
            "evidenceRefs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class OutcomeProvenance:
    """Where an outcome or learning came from, and what evidence backs it."""

    source: str
    lifecycle_event_id: str = ""
    commit_sha: str = ""
    release_ref: str = ""
    policy_version: str = ""
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require(bool(self.source.strip()), "provenance: source is required")

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "lifecycleEventId": self.lifecycle_event_id,
            "commitSha": self.commit_sha,
            "releaseRef": self.release_ref,
            "policyVersion": self.policy_version,
            "evidenceRefs": list(self.evidence_refs),
        }


def outcome_id_for(project_id: str, work_unit_id: str, release_ref: str = "") -> str:
    """A stable, content-derived identity for one outcome of one work unit."""
    _require(bool(project_id.strip()), "outcome identity: project is required")
    _require(bool(work_unit_id.strip()), "outcome identity: work unit is required")
    identity = _canonical([project_id.strip(), work_unit_id.strip(), release_ref.strip()])
    return "outcome-" + _sha256_text(identity)[:16]


@dataclass(frozen=True)
class OutcomeRecord:
    """What one work unit was meant to deliver, and what it actually delivered."""

    outcome_id: str
    work_unit: WorkUnitRef
    intended_outcome: str
    recorded_at: str
    provenance: OutcomeProvenance
    result: OutcomeResult = OutcomeResult.UNKNOWN
    deployment_status: DeploymentStatus = DeploymentStatus.UNKNOWN
    acceptance_criteria: tuple[AcceptanceCriterion, ...] = ()
    delivered_capability: str = ""
    task_class: str = ""
    observations: tuple[OutcomeObservation, ...] = ()
    first_pass_verification: bool | None = None
    first_pass_review_accepted: bool | None = None
    repair_rounds: int | None = None
    review_rounds: int | None = None
    regression_escaped: bool | None = None
    reopened: bool | None = None
    reverted: bool | None = None
    rolled_back: bool | None = None
    defect_followups: int | None = None
    intake_at: str = ""
    merged_at: str = ""
    live_at: str = ""
    schema_version: int = OUTCOME_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require(bool(self.outcome_id.strip()), "outcome record: outcomeId is required")
        _require(
            bool(self.intended_outcome.strip()),
            "outcome record: intendedOutcome is required; an outcome with no intent "
            "cannot be scored against anything",
        )
        _bounded(self.intended_outcome, "outcome record intendedOutcome")
        _bounded(self.delivered_capability, "outcome record deliveredCapability")
        _timestamp(self.recorded_at, "outcome record: recordedAt")
        for name in ("intake_at", "merged_at", "live_at"):
            value = getattr(self, name)
            if value:
                _timestamp(value, f"outcome record: {name}")
        for name in ("repair_rounds", "review_rounds", "defect_followups"):
            _non_negative_count(getattr(self, name), f"outcome record {name}")
        seen: set[str] = set()
        for observation in self.observations:
            _require(
                observation.observation_id not in seen,
                f"outcome record: duplicate observationId: {observation.observation_id}",
            )
            seen.add(observation.observation_id)

    @property
    def project_id(self) -> str:
        return self.work_unit.project_id

    @property
    def work_unit_id(self) -> str:
        return self.work_unit.work_unit_id

    @property
    def accepted(self) -> bool:
        """An accepted outcome is delivered and still standing.

        A reverted or rolled-back change is not an outcome, however green its
        pipeline was, so neither can be scored as value delivered.
        """
        if self.result not in ACCEPTED_RESULTS:
            return False
        if self.reverted or self.rolled_back:
            return False
        return self.deployment_status is not DeploymentStatus.ROLLED_BACK

    @property
    def has_outcome_data(self) -> bool:
        """Whether this record says anything about delivery at all."""
        return self.result is not OutcomeResult.UNKNOWN

    @property
    def time_to_live_seconds(self) -> int | None:
        """Intake to live, in seconds; ``None`` unless both instants were observed."""
        if not self.intake_at or not self.live_at:
            return None
        start = parse_timestamp(self.intake_at, "outcome record: intakeAt")
        end = parse_timestamp(self.live_at, "outcome record: liveAt")
        seconds = (end - start).total_seconds()
        _require(seconds >= 0, "outcome record: liveAt precedes intakeAt")
        return int(seconds)

    @property
    def acceptance_criteria_met(self) -> bool | None:
        """True when every criterion was met, False when any was not, else unknown."""
        if not self.acceptance_criteria:
            return None
        verdicts = [item.met for item in self.acceptance_criteria]
        if any(verdict is False for verdict in verdicts):
            return False
        if any(verdict is None for verdict in verdicts):
            return None
        return True

    def observations_of(self, kind: ObservationKind) -> tuple[OutcomeObservation, ...]:
        return tuple(item for item in self.observations if item.kind is kind)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "outcomeId": self.outcome_id,
            "workUnit": self.work_unit.as_dict(),
            "intendedOutcome": self.intended_outcome,
            "acceptanceCriteria": [item.as_dict() for item in self.acceptance_criteria],
            "acceptanceCriteriaMet": self.acceptance_criteria_met,
            "deliveredCapability": self.delivered_capability,
            "result": self.result.value,
            "accepted": self.accepted,
            "deploymentStatus": self.deployment_status.value,
            "taskClass": self.task_class,
            "observations": [item.as_dict() for item in self.observations],
            "firstPassVerification": self.first_pass_verification,
            "firstPassReviewAccepted": self.first_pass_review_accepted,
            "repairRounds": self.repair_rounds,
            "reviewRounds": self.review_rounds,
            "regressionEscaped": self.regression_escaped,
            "reopened": self.reopened,
            "reverted": self.reverted,
            "rolledBack": self.rolled_back,
            "defectFollowups": self.defect_followups,
            "intakeAt": self.intake_at,
            "mergedAt": self.merged_at,
            "liveAt": self.live_at,
            "timeToLiveSeconds": self.time_to_live_seconds,
            "recordedAt": self.recorded_at,
            "provenance": self.provenance.as_dict(),
        }


_CRITERION_KEYS = frozenset({"text", "met", "evidenceRef"})
_OBSERVATION_KEYS = frozenset(
    {
        "observationId",
        "kind",
        "observedAt",
        "observer",
        "observerKind",
        "healthy",
        "environment",
        "note",
        "evidenceRefs",
    }
)
_PROVENANCE_KEYS = frozenset(
    {"source", "lifecycleEventId", "commitSha", "releaseRef", "policyVersion", "evidenceRefs"}
)
_OUTCOME_KEYS = frozenset(
    {
        "schemaVersion",
        "outcomeId",
        "workUnit",
        "intendedOutcome",
        "acceptanceCriteria",
        "acceptanceCriteriaMet",
        "deliveredCapability",
        "result",
        "accepted",
        "deploymentStatus",
        "taskClass",
        "observations",
        "firstPassVerification",
        "firstPassReviewAccepted",
        "repairRounds",
        "reviewRounds",
        "regressionEscaped",
        "reopened",
        "reverted",
        "rolledBack",
        "defectFollowups",
        "intakeAt",
        "mergedAt",
        "liveAt",
        "timeToLiveSeconds",
        "recordedAt",
        "provenance",
    }
)


def _load_criterion(raw: Any, label: str) -> AcceptanceCriterion:
    entry = _mapping(raw, label)
    _known_keys(entry, _CRITERION_KEYS, label)
    return AcceptanceCriterion(
        text=_text(entry, "text", label, required=True),
        met=_optional_bool(entry, "met", label),
        evidence_ref=_text(entry, "evidenceRef", label),
    )


def _load_observation(raw: Any, label: str) -> OutcomeObservation:
    entry = _mapping(raw, label)
    _known_keys(entry, _OBSERVATION_KEYS, label)
    return OutcomeObservation(
        observation_id=_text(entry, "observationId", label, required=True),
        kind=_enum(ObservationKind, _text(entry, "kind", label, required=True), label),
        observed_at=_timestamp(
            _text(entry, "observedAt", label, required=True), f"{label}: observedAt"
        ),
        observer=_text(entry, "observer", label, required=True),
        observer_kind=_text(entry, "observerKind", label) or "system",
        healthy=_optional_bool(entry, "healthy", label),
        environment=_text(entry, "environment", label),
        note=_text(entry, "note", label),
        evidence_refs=_string_tuple(entry.get("evidenceRefs"), f"{label}: evidenceRefs"),
    )


def _load_provenance(raw: Any, label: str) -> OutcomeProvenance:
    entry = _mapping(raw, label)
    _known_keys(entry, _PROVENANCE_KEYS, label)
    return OutcomeProvenance(
        source=_text(entry, "source", label, required=True),
        lifecycle_event_id=_text(entry, "lifecycleEventId", label),
        commit_sha=_text(entry, "commitSha", label),
        release_ref=_text(entry, "releaseRef", label),
        policy_version=_text(entry, "policyVersion", label),
        evidence_refs=_string_tuple(entry.get("evidenceRefs"), f"{label}: evidenceRefs"),
    )


def _version(entry: Mapping[str, Any], current: int, label: str) -> int:
    value = entry.get("schemaVersion", current)
    _require(
        isinstance(value, int) and not isinstance(value, bool),
        f"{label}: schemaVersion must be an integer",
    )
    assert isinstance(value, int)
    _require(value == current, f"{label}: unsupported schemaVersion: {value}")
    return value


def _list(entry: Mapping[str, Any], key: str, label: str) -> list[Any]:
    value = entry.get(key, [])
    if value is None:
        return []
    _require(isinstance(value, list), f"{label}: {key} must be an array")
    assert isinstance(value, list)
    return value


def load_outcome_record(document: Any) -> OutcomeRecord:
    """Parse one outcome record fail-closed."""
    label = "outcome record"
    entry = _mapping(document, label)
    _known_keys(entry, _OUTCOME_KEYS, label)
    _version(entry, OUTCOME_SCHEMA_VERSION, label)
    return OutcomeRecord(
        outcome_id=_text(entry, "outcomeId", label, required=True),
        work_unit=_work_unit_ref(entry.get("workUnit", {}), f"{label} workUnit"),
        intended_outcome=_text(entry, "intendedOutcome", label, required=True),
        recorded_at=_timestamp(
            _text(entry, "recordedAt", label, required=True), f"{label}: recordedAt"
        ),
        provenance=_load_provenance(entry.get("provenance", {}), f"{label} provenance"),
        result=_enum(
            OutcomeResult, _text(entry, "result", label) or OutcomeResult.UNKNOWN.value, label
        ),
        deployment_status=_enum(
            DeploymentStatus,
            _text(entry, "deploymentStatus", label) or DeploymentStatus.UNKNOWN.value,
            label,
        ),
        acceptance_criteria=tuple(
            _load_criterion(item, f"{label} acceptanceCriteria[{index}]")
            for index, item in enumerate(_list(entry, "acceptanceCriteria", label))
        ),
        delivered_capability=_text(entry, "deliveredCapability", label),
        task_class=_text(entry, "taskClass", label),
        observations=tuple(
            _load_observation(item, f"{label} observations[{index}]")
            for index, item in enumerate(_list(entry, "observations", label))
        ),
        first_pass_verification=_optional_bool(entry, "firstPassVerification", label),
        first_pass_review_accepted=_optional_bool(entry, "firstPassReviewAccepted", label),
        repair_rounds=_optional_count(entry, "repairRounds", label),
        review_rounds=_optional_count(entry, "reviewRounds", label),
        regression_escaped=_optional_bool(entry, "regressionEscaped", label),
        reopened=_optional_bool(entry, "reopened", label),
        reverted=_optional_bool(entry, "reverted", label),
        rolled_back=_optional_bool(entry, "rolledBack", label),
        defect_followups=_optional_count(entry, "defectFollowups", label),
        intake_at=_optional_timestamp(entry, "intakeAt", label),
        merged_at=_optional_timestamp(entry, "mergedAt", label),
        live_at=_optional_timestamp(entry, "liveAt", label),
    )


# -- learning records ---------------------------------------------------------


def learning_id_for(kind: str, statement: str, recorded_at: str) -> str:
    """A stable, content-derived identity for one learning."""
    _require(bool(statement.strip()), "learning identity: statement is required")
    identity = _canonical([kind.strip(), statement.strip(), recorded_at.strip()])
    return "learning-" + _sha256_text(identity)[:16]


@dataclass(frozen=True)
class LearningRecord:
    """One versioned, append-only lesson with its provenance attached.

    A learning is always advisory. It may recommend an estimator or routing
    adjustment, but adopting one is a separate, human-approved policy change —
    nothing in this module applies a recommendation.
    """

    learning_id: str
    kind: LearningKind
    statement: str
    recorded_at: str
    provenance: OutcomeProvenance
    work_unit: WorkUnitRef | None = None
    project_id: str = ""
    cause: str = ""
    recommended_adjustment: str = ""
    model: str = ""
    stage: LifecycleStage | None = None
    task_class: str = ""
    sample_size: int | None = None
    supersedes: str = ""
    schema_version: int = LEARNING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require(bool(self.learning_id.strip()), "learning record: learningId is required")
        _require(bool(self.statement.strip()), "learning record: statement is required")
        for name in ("statement", "cause", "recommended_adjustment"):
            _bounded(getattr(self, name), f"learning record {name}")
        _timestamp(self.recorded_at, "learning record: recordedAt")
        _non_negative_count(self.sample_size, "learning record sampleSize")
        _require(
            bool(self.project_id.strip()) or self.work_unit is not None,
            "learning record: a learning must name the project or work unit it came from",
        )
        _require(
            self.supersedes != self.learning_id,
            "learning record: a learning cannot supersede itself",
        )

    @property
    def scope_project_id(self) -> str:
        """The project this learning belongs to, from its work unit or its own field."""
        if self.work_unit is not None:
            return self.work_unit.project_id
        return self.project_id

    @property
    def advisory(self) -> bool:
        """Always true: a learning is evidence, never an applied policy change."""
        return True

    def as_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "learningId": self.learning_id,
            "kind": self.kind.value,
            "statement": self.statement,
            "cause": self.cause,
            "recommendedAdjustment": self.recommended_adjustment,
            "advisory": self.advisory,
            "workUnit": self.work_unit.as_dict() if self.work_unit else None,
            "projectId": self.project_id,
            "scopeProjectId": self.scope_project_id,
            "model": self.model,
            "stage": self.stage.value if self.stage else "",
            "taskClass": self.task_class,
            "sampleSize": self.sample_size,
            "supersedes": self.supersedes,
            "recordedAt": self.recorded_at,
            "provenance": self.provenance.as_dict(),
        }


_LEARNING_KEYS = frozenset(
    {
        "schemaVersion",
        "learningId",
        "kind",
        "statement",
        "cause",
        "recommendedAdjustment",
        "advisory",
        "workUnit",
        "projectId",
        "scopeProjectId",
        "model",
        "stage",
        "taskClass",
        "sampleSize",
        "supersedes",
        "recordedAt",
        "provenance",
    }
)


def load_learning_record(document: Any) -> LearningRecord:
    """Parse one learning record fail-closed."""
    label = "learning record"
    entry = _mapping(document, label)
    _known_keys(entry, _LEARNING_KEYS, label)
    _version(entry, LEARNING_SCHEMA_VERSION, label)
    advisory = entry.get("advisory", True)
    _require(
        advisory is True,
        "learning record: advisory cannot be false; a learning is evidence, and adopting "
        "it is a separate approved policy change",
    )
    work_unit_raw = entry.get("workUnit")
    stage = _text(entry, "stage", label)
    return LearningRecord(
        learning_id=_text(entry, "learningId", label, required=True),
        kind=_enum(LearningKind, _text(entry, "kind", label, required=True), label),
        statement=_text(entry, "statement", label, required=True),
        recorded_at=_timestamp(
            _text(entry, "recordedAt", label, required=True), f"{label}: recordedAt"
        ),
        provenance=_load_provenance(entry.get("provenance", {}), f"{label} provenance"),
        work_unit=(_work_unit_ref(work_unit_raw, f"{label} workUnit") if work_unit_raw else None),
        project_id=_text(entry, "projectId", label),
        cause=_text(entry, "cause", label),
        recommended_adjustment=_text(entry, "recommendedAdjustment", label),
        model=_text(entry, "model", label),
        stage=_enum(LifecycleStage, stage, label) if stage else None,
        task_class=_text(entry, "taskClass", label),
        sample_size=_optional_count(entry, "sampleSize", label),
        supersedes=_text(entry, "supersedes", label),
    )


# -- ledgers ------------------------------------------------------------------

#: Fields an append may fill in when the stored record left them unobserved.
_FILLABLE_FIELDS = (
    "delivered_capability",
    "task_class",
    "first_pass_verification",
    "first_pass_review_accepted",
    "repair_rounds",
    "review_rounds",
    "regression_escaped",
    "reopened",
    "reverted",
    "rolled_back",
    "defect_followups",
    "intake_at",
    "merged_at",
    "live_at",
)

_UNSET: Mapping[str, Any] = {
    "delivered_capability": "",
    "task_class": "",
    "intake_at": "",
    "merged_at": "",
    "live_at": "",
}


def _is_unset(name: str, value: Any) -> bool:
    return value == _UNSET[name] if name in _UNSET else value is None


class EffectivenessLedger:
    """Outcome records across every Forge project, immutable once observed.

    An outcome is completed as a work unit progresses: the merge is known
    before the deployment, and an observation of real behavior arrives later
    still. Appending the same ``outcomeId`` again fills what was unobserved and
    adds new observations, but a value that was already recorded can never be
    quietly replaced — a contradicting append fails closed.
    """

    def __init__(self, *, registry: ProjectRegistry = EMPTY_PROJECT_REGISTRY) -> None:
        self._registry = registry
        self._records: dict[str, OutcomeRecord] = {}

    @property
    def registry(self) -> ProjectRegistry:
        return self._registry

    @property
    def records(self) -> tuple[OutcomeRecord, ...]:
        return tuple(self._records[key] for key in sorted(self._records))

    def __len__(self) -> int:
        return len(self._records)

    def get(self, outcome_id: str) -> OutcomeRecord:
        record = self._records.get(outcome_id)
        _require(record is not None, f"unknown outcome: {outcome_id}")
        assert record is not None
        return record

    def append(self, record: OutcomeRecord) -> OutcomeRecord:
        """Record an outcome, or complete one already recorded."""
        if not self._registry.is_empty:
            _require(
                record.project_id in self._registry,
                f"unregistered project: {record.project_id}",
            )
        existing = self._records.get(record.outcome_id)
        if existing is None:
            self._records[record.outcome_id] = record
            return record
        merged = self._merge(existing, record)
        self._records[record.outcome_id] = merged
        return merged

    def _merge(self, existing: OutcomeRecord, incoming: OutcomeRecord) -> OutcomeRecord:
        _require(
            existing.work_unit.project_id == incoming.work_unit.project_id
            and existing.work_unit.work_unit_id == incoming.work_unit.work_unit_id,
            f"outcome {existing.outcome_id} already belongs to another work unit",
        )
        _require(
            existing.intended_outcome == incoming.intended_outcome,
            f"outcome {existing.outcome_id}: intendedOutcome is already recorded as "
            f"{existing.intended_outcome!r} and cannot be rewritten",
        )
        updates: dict[str, Any] = {}
        for name in _FILLABLE_FIELDS:
            stored = getattr(existing, name)
            fresh = getattr(incoming, name)
            if _is_unset(name, fresh):
                continue
            if _is_unset(name, stored):
                updates[name] = fresh
                continue
            _require(
                stored == fresh,
                f"outcome {existing.outcome_id}: {name} is already recorded as {stored!r} "
                f"and cannot be rewritten to {fresh!r}",
            )
        if incoming.result is not OutcomeResult.UNKNOWN:
            _require(
                existing.result is OutcomeResult.UNKNOWN or existing.result is incoming.result,
                f"outcome {existing.outcome_id}: result is already recorded as "
                f"{existing.result.value} and cannot be rewritten to {incoming.result.value}",
            )
            updates["result"] = incoming.result
        if incoming.deployment_status is not DeploymentStatus.UNKNOWN:
            updates["deployment_status"] = incoming.deployment_status
        if incoming.acceptance_criteria:
            if existing.acceptance_criteria:
                _require(
                    tuple(item.text for item in existing.acceptance_criteria)
                    == tuple(item.text for item in incoming.acceptance_criteria),
                    f"outcome {existing.outcome_id}: acceptance criteria are already "
                    "recorded and their text cannot be rewritten",
                )
            updates["acceptance_criteria"] = incoming.acceptance_criteria
        known = {item.observation_id: item for item in existing.observations}
        added = []
        for observation in incoming.observations:
            stored_observation = known.get(observation.observation_id)
            if stored_observation is None:
                added.append(observation)
                continue
            _require(
                stored_observation == observation,
                f"outcome {existing.outcome_id}: observation "
                f"{observation.observation_id} is already recorded differently",
            )
        if added:
            updates["observations"] = existing.observations + tuple(added)
        if not updates:
            return existing
        return replace(existing, **updates)

    def extend(self, records: Iterable[OutcomeRecord]) -> tuple[OutcomeRecord, ...]:
        return tuple(self.append(record) for record in records)

    def query(
        self,
        *,
        project_id: str | None = None,
        project_ids: Sequence[str] | None = None,
        work_unit_id: str | None = None,
        task_class: str | None = None,
        result: OutcomeResult | str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> tuple[OutcomeRecord, ...]:
        """Filter outcomes; project scoping is exact so projects never leak."""
        scope = _scope(project_id, project_ids)
        wanted = OutcomeResult(result) if result is not None else None
        return tuple(
            record
            for record in self.records
            if (scope is None or record.project_id in scope)
            and (work_unit_id is None or record.work_unit_id == work_unit_id)
            and (task_class is None or record.task_class == task_class)
            and (wanted is None or record.result is wanted)
            and (since is None or record.recorded_at >= since)
            and (until is None or record.recorded_at <= until)
        )

    def project_ids(self) -> tuple[str, ...]:
        return tuple(sorted({record.project_id for record in self._records.values()}))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": OUTCOME_LEDGER_SCHEMA_VERSION,
            "outcomeSchemaVersion": OUTCOME_SCHEMA_VERSION,
            "outcomes": [record.as_dict() for record in self.records],
        }

    @classmethod
    def from_dict(
        cls, document: Any, *, registry: ProjectRegistry = EMPTY_PROJECT_REGISTRY
    ) -> EffectivenessLedger:
        label = "outcome ledger"
        entry = _mapping(document, label)
        _known_keys(entry, frozenset({"schemaVersion", "outcomeSchemaVersion", "outcomes"}), label)
        _version(entry, OUTCOME_LEDGER_SCHEMA_VERSION, label)
        ledger = cls(registry=registry)
        for item in _list(entry, "outcomes", label):
            ledger.append(load_outcome_record(item))
        return ledger


class LearningLedger:
    """Strictly append-only, versioned learning records.

    Unlike an outcome, a learning is never completed in place: a revision is a
    new record that supersedes its predecessor by ID, so the reasoning that was
    acted on at the time stays readable afterwards.
    """

    def __init__(self, *, registry: ProjectRegistry = EMPTY_PROJECT_REGISTRY) -> None:
        self._registry = registry
        self._records: list[LearningRecord] = []
        self._by_id: dict[str, LearningRecord] = {}

    @property
    def registry(self) -> ProjectRegistry:
        return self._registry

    @property
    def records(self) -> tuple[LearningRecord, ...]:
        return tuple(self._records)

    def __len__(self) -> int:
        return len(self._records)

    def get(self, learning_id: str) -> LearningRecord:
        record = self._by_id.get(learning_id)
        _require(record is not None, f"unknown learning: {learning_id}")
        assert record is not None
        return record

    def append(self, record: LearningRecord) -> LearningRecord:
        """Append one learning. Replay is a no-op; a changed payload fails closed."""
        if not self._registry.is_empty:
            _require(
                record.scope_project_id in self._registry,
                f"unregistered project: {record.scope_project_id}",
            )
        if record.supersedes:
            _require(
                record.supersedes in self._by_id,
                f"learning {record.learning_id} supersedes unknown learning {record.supersedes}",
            )
        existing = self._by_id.get(record.learning_id)
        if existing is not None:
            _require(
                existing.as_dict() == record.as_dict(),
                f"learning {record.learning_id} is already recorded with a different "
                "payload; a revision must supersede it rather than replace it",
            )
            return existing
        self._records.append(record)
        self._by_id[record.learning_id] = record
        return record

    def extend(self, records: Iterable[LearningRecord]) -> tuple[LearningRecord, ...]:
        return tuple(self.append(record) for record in records)

    def superseded_ids(self) -> frozenset[str]:
        return frozenset(record.supersedes for record in self._records if record.supersedes)

    def current(
        self,
        *,
        project_id: str | None = None,
        project_ids: Sequence[str] | None = None,
        kind: LearningKind | str | None = None,
    ) -> tuple[LearningRecord, ...]:
        """Learnings that nothing later supersedes, in the order recorded."""
        superseded = self.superseded_ids()
        scope = _scope(project_id, project_ids)
        wanted = LearningKind(kind) if kind is not None else None
        return tuple(
            record
            for record in self._records
            if record.learning_id not in superseded
            and (scope is None or record.scope_project_id in scope)
            and (wanted is None or record.kind is wanted)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": LEARNING_LEDGER_SCHEMA_VERSION,
            "learningSchemaVersion": LEARNING_SCHEMA_VERSION,
            "advisory": True,
            "learnings": [record.as_dict() for record in self._records],
        }

    @classmethod
    def from_dict(
        cls, document: Any, *, registry: ProjectRegistry = EMPTY_PROJECT_REGISTRY
    ) -> LearningLedger:
        label = "learning ledger"
        entry = _mapping(document, label)
        _known_keys(
            entry,
            frozenset({"schemaVersion", "learningSchemaVersion", "advisory", "learnings"}),
            label,
        )
        _version(entry, LEARNING_LEDGER_SCHEMA_VERSION, label)
        ledger = cls(registry=registry)
        for item in _list(entry, "learnings", label):
            ledger.append(load_learning_record(item))
        return ledger


def _scope(project_id: str | None, project_ids: Sequence[str] | None) -> frozenset[str] | None:
    _require(
        project_id is None or project_ids is None,
        "pass project_id or project_ids, not both",
    )
    if project_id is not None:
        return frozenset({project_id})
    if project_ids is not None:
        return frozenset(project_ids)
    return None


# -- metrics ------------------------------------------------------------------


def _rate(name: str, numerator: int, denominator: int, *, reason: str = "") -> dict[str, Any]:
    """A rate that reports itself as unknown rather than dividing by zero."""
    if denominator <= 0:
        return {
            "metric": name,
            "status": "unknown",
            "value": None,
            "numerator": numerator,
            "denominator": denominator,
            "reason": reason or "no outcome in the sample recorded this signal",
        }
    return {
        "metric": name,
        "status": "observed",
        "value": round(numerator / denominator, 6),
        "numerator": numerator,
        "denominator": denominator,
        "reason": "",
    }


def _mean(name: str, values: Sequence[int], unit: str = "") -> dict[str, Any]:
    if not values:
        return {
            "metric": name,
            "status": "unknown",
            "value": None,
            "sampleSize": 0,
            "unit": unit,
            "reason": "no outcome in the sample recorded this signal",
        }
    return {
        "metric": name,
        "status": "observed",
        "value": round(sum(values) / len(values), 6),
        "sampleSize": len(values),
        "unit": unit,
        "reason": "",
    }


def _percentile(values: Sequence[int], fraction: float) -> int:
    """Nearest-rank percentile: deterministic, and never interpolates a value."""
    ordered = sorted(values)
    rank = max(1, min(len(ordered), math.ceil(fraction * len(ordered))))
    return ordered[rank - 1]


def _distribution(name: str, values: Sequence[int], unit: str) -> dict[str, Any]:
    if not values:
        return {
            "metric": name,
            "status": "unknown",
            "sampleSize": 0,
            "unit": unit,
            "reason": "no outcome in the sample observed both ends of this interval",
        }
    return {
        "metric": name,
        "status": "observed",
        "sampleSize": len(values),
        "unit": unit,
        "min": min(values),
        "p50": _percentile(values, 0.5),
        "p90": _percentile(values, 0.9),
        "max": max(values),
        "reason": "",
    }


def _flag_rate(name: str, outcomes: Sequence[OutcomeRecord], attribute: str) -> dict[str, Any]:
    """A rate over an optional boolean, counting only outcomes that observed it."""
    observed = [record for record in outcomes if getattr(record, attribute) is not None]
    hits = sum(1 for record in observed if getattr(record, attribute))
    return _rate(name, hits, len(observed))


@dataclass(frozen=True)
class _ValueInputs:
    """The denominators every value metric divides by, and how known they are.

    A denominator is reported only when **every** record in the sample observed
    it. A partial sum would understate the effort a sample consumed and so
    overstate the factory's measured value, which is the one direction an
    honest metric must never fail in — so incomplete coverage reads as unknown
    with the number of records that lacked it.
    """

    agent_seconds: int | None
    tokens: int | None
    payg_usd: float | None
    subscription_equivalent_usd: float | None
    infrastructure_usd: float | None
    window_seconds: float | None
    record_count: int
    missing: Mapping[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "usageRecords": self.record_count,
            "agentSeconds": self.agent_seconds,
            "agentHours": (
                None
                if self.agent_seconds is None
                else round(self.agent_seconds / _SECONDS_PER_HOUR, 6)
            ),
            "tokens": self.tokens,
            "paygUsd": self.payg_usd,
            "subscriptionEquivalentUsd": self.subscription_equivalent_usd,
            "infrastructureUsd": self.infrastructure_usd,
            "windowSeconds": self.window_seconds,
            "unobservedRecords": dict(sorted(self.missing.items())),
        }


def _value_inputs(records: Sequence[UsageRecord], window: ObservationWindow | None) -> _ValueInputs:
    totals = {
        "agentSeconds": 0.0,
        "tokens": 0.0,
        "paygUsd": 0.0,
        "subscriptionEquivalentUsd": 0.0,
        "infrastructureUsd": 0.0,
    }
    missing = dict.fromkeys(totals, 0)

    for record in records:
        actual = record.actual
        runtime = None if actual is None else actual.runtime_seconds
        if runtime is None:
            missing["agentSeconds"] += 1
        else:
            totals["agentSeconds"] += runtime

        tokens = None if actual is None else actual.tokens.total
        if tokens is None:
            missing["tokens"] += 1
        else:
            totals["tokens"] += tokens

        monetary = None if actual is None else actual.monetary
        payg = None
        subscription = None
        if monetary is not None and monetary.status is MonetaryStatus.PAYG:
            payg = (
                monetary.billed_usd
                if monetary.billed_usd is not None
                else monetary.payg_equivalent_usd
            )
        elif monetary is not None and monetary.status is MonetaryStatus.SUBSCRIPTION:
            subscription = monetary.payg_equivalent_usd
        for name, amount in (("paygUsd", payg), ("subscriptionEquivalentUsd", subscription)):
            if amount is None:
                missing[name] += 1
            else:
                totals[name] += amount

        infrastructure = record.infrastructure
        cost = None if infrastructure is None else infrastructure.cost_usd
        if cost is None:
            missing["infrastructureUsd"] += 1
        else:
            totals["infrastructureUsd"] += cost

    def known(name: str) -> float | None:
        """A total only counts when the whole sample observed it."""
        if not records or missing[name]:
            return None
        return round(totals[name], 6)

    agent_seconds = known("agentSeconds")
    tokens_total = known("tokens")
    return _ValueInputs(
        agent_seconds=None if agent_seconds is None else int(agent_seconds),
        tokens=None if tokens_total is None else int(tokens_total),
        payg_usd=known("paygUsd"),
        subscription_equivalent_usd=known("subscriptionEquivalentUsd"),
        infrastructure_usd=known("infrastructureUsd"),
        window_seconds=window.seconds if window is not None else None,
        record_count=len(records),
        missing=dict(missing),
    )


def _per(
    name: str,
    accepted: int,
    denominator: float | None,
    unit: str,
    *,
    missing_reason: str,
    status_label: str = "observed",
) -> dict[str, Any]:
    """One value metric: accepted outcomes over an observed denominator.

    The numerator is always the accepted-outcome count. Effort — tokens,
    runtime, spend — can only ever be the denominator, so no amount of
    consumption can raise the measured value of the factory.
    """
    if denominator is None:
        return {
            "metric": name,
            "status": "unknown",
            "value": None,
            "numerator": accepted,
            "denominator": None,
            "unit": unit,
            "reason": missing_reason,
        }
    if denominator <= 0:
        return {
            "metric": name,
            "status": "unknown",
            "value": None,
            "numerator": accepted,
            "denominator": denominator,
            "unit": unit,
            "reason": "the observed denominator is zero, so a rate would be undefined",
        }
    return {
        "metric": name,
        "status": status_label,
        "value": round(accepted / denominator, 6),
        "numerator": accepted,
        "denominator": round(denominator, 6),
        "unit": unit,
        "reason": "",
    }


def _value_report(
    accepted: int, inputs: _ValueInputs, window: ObservationWindow | None
) -> dict[str, Any]:
    agent_hours = None if inputs.agent_seconds is None else inputs.agent_seconds / _SECONDS_PER_HOUR
    millions = None if inputs.tokens is None else inputs.tokens / _TOKENS_PER_MILLION
    wall_clock_days = (
        None if inputs.window_seconds is None else inputs.window_seconds / _SECONDS_PER_DAY
    )
    infrastructure = inputs.infrastructure_usd
    return {
        "acceptedOutcomes": accepted,
        "perAgentHour": _per(
            "acceptedOutcomesPerAgentHour",
            accepted,
            agent_hours,
            "outcomes/agent-hour",
            missing_reason=(
                "no usage record in the sample observed a runtime, or some did not; a "
                "partial sum would overstate value"
            ),
        ),
        "perMillionTokens": _per(
            "acceptedOutcomesPerMillionTokens",
            accepted,
            millions,
            "outcomes/1M tokens",
            missing_reason=(
                "not every usage record in the sample observed a complete token count; a "
                "partial sum would overstate value"
            ),
        ),
        "perPaygDollar": _per(
            "acceptedOutcomesPerPaygDollar",
            accepted,
            inputs.payg_usd,
            "outcomes/USD billed",
            missing_reason=(
                "not every usage record in the sample carried pay-as-you-go pricing; a "
                "partial sum would overstate value"
            ),
        ),
        "perSubscriptionEquivalentDollar": _per(
            "acceptedOutcomesPerSubscriptionEquivalentDollar",
            accepted,
            inputs.subscription_equivalent_usd,
            "outcomes/USD subscription-equivalent",
            missing_reason=(
                "not every usage record in the sample carried a subscription reference "
                "rate; a partial sum would overstate value"
            ),
            status_label="subscription",
        ),
        "perInfrastructureDollar": _per(
            "acceptedOutcomesPerInfrastructureDollar",
            accepted,
            infrastructure,
            "outcomes/USD infrastructure",
            missing_reason=(
                "not every usage record in the sample observed an infrastructure cost; a "
                "partial sum would overstate value"
            ),
        ),
        "perWallClockDay": _per(
            "acceptedOutcomesPerWallClockDay",
            accepted,
            wall_clock_days,
            "outcomes/day",
            missing_reason="no observation window was supplied, so elapsed time is unknown",
        ),
        "inputs": inputs.as_dict(),
    }


#: The estimate dimensions worth comparing against delivered outcomes: what the
#: work consumed, how long it took, and what it cost.
ESTIMATE_COMPARISONS: tuple[str, ...] = ("totalTokens", "runtimeSeconds", "paygEquivalentUsd")


def _estimate_error_dimension(
    errors: Sequence[Mapping[str, Any]], dimension: str
) -> dict[str, Any]:
    """Signed mean percentage error of the estimate against the actual.

    A positive value means the actual overran the estimate. Records missing
    either side, and estimates of zero (which have no percentage), are counted
    as unobserved rather than treated as perfect predictions.
    """
    values: list[float] = []
    unobserved = 0
    for entry in errors:
        dimension_error = entry.get(dimension)
        if dimension_error is None or dimension_error.get("percentage") is None:
            unobserved += 1
            continue
        values.append(float(dimension_error["percentage"]))
    if not values:
        return {
            "status": "unknown",
            "value": None,
            "unit": "percent",
            "sampleSize": 0,
            "unobservedRecords": unobserved,
            "reason": "no record in the sample had both an estimate and an actual here",
        }
    return {
        "status": "observed",
        "value": round(sum(values) / len(values), 6),
        "unit": "percent",
        "sampleSize": len(values),
        "unobservedRecords": unobserved,
        "reason": "",
    }


def _estimate_vs_actual(records: Sequence[UsageRecord]) -> dict[str, Any]:
    """Estimate-versus-actual error overall, by task class and by project."""
    # Each record's estimate error is derived once and then sliced, rather than
    # recomputed for every dimension and every grouping.
    errors = [(record, record.estimate_error.dimensions) for record in records]

    def dimensions(
        sample: Sequence[tuple[UsageRecord, Mapping[str, Any]]],
    ) -> dict[str, Any]:
        measured = [item for _, item in sample]
        return {
            dimension: _estimate_error_dimension(measured, dimension)
            for dimension in ESTIMATE_COMPARISONS
        }

    by_task_class = {
        task_class: dimensions(
            [item for item in errors if (item[0].task_class or "unclassified") == task_class]
        )
        for task_class in sorted({record.task_class or "unclassified" for record in records})
    }
    by_project = {
        project_id: dimensions([item for item in errors if item[0].project_id == project_id])
        for project_id in sorted({record.project_id for record in records})
    }
    return {
        "overall": dimensions(errors),
        "byTaskClass": by_task_class,
        "byProject": by_project,
    }


def _quality_report(outcomes: Sequence[OutcomeRecord]) -> dict[str, Any]:
    scored = [record for record in outcomes if record.has_outcome_data]
    accepted = [record for record in outcomes if record.accepted]
    criteria_observed = [
        record for record in outcomes if record.acceptance_criteria_met is not None
    ]
    return {
        "firstPassVerificationRate": _flag_rate(
            "firstPassVerificationRate", outcomes, "first_pass_verification"
        ),
        "firstPassReviewAcceptanceRate": _flag_rate(
            "firstPassReviewAcceptanceRate", outcomes, "first_pass_review_accepted"
        ),
        "meanRepairRounds": _mean(
            "meanRepairRounds",
            [record.repair_rounds for record in outcomes if record.repair_rounds is not None],
            "rounds",
        ),
        "meanReviewRounds": _mean(
            "meanReviewRounds",
            [record.review_rounds for record in outcomes if record.review_rounds is not None],
            "rounds",
        ),
        "regressionEscapeRate": _flag_rate("regressionEscapeRate", outcomes, "regression_escaped"),
        "reopenRate": _flag_rate("reopenRate", outcomes, "reopened"),
        "revertRate": _flag_rate("revertRate", outcomes, "reverted"),
        "rollbackRate": _flag_rate("rollbackRate", outcomes, "rolled_back"),
        "defectFollowupRate": _rate(
            "defectFollowupRate",
            sum(
                1
                for record in outcomes
                if record.defect_followups is not None and record.defect_followups > 0
            ),
            sum(1 for record in outcomes if record.defect_followups is not None),
        ),
        "acceptanceCriteriaMetRate": _rate(
            "acceptanceCriteriaMetRate",
            sum(1 for record in criteria_observed if record.acceptance_criteria_met),
            len(criteria_observed),
        ),
        "acceptedOutcomeRate": _rate(
            "acceptedOutcomeRate",
            len(accepted),
            len(scored),
            reason="no outcome in the sample has a recorded result",
        ),
        "timeToLiveSeconds": _distribution(
            "timeToLiveSeconds",
            [
                record.time_to_live_seconds
                for record in outcomes
                if record.time_to_live_seconds is not None
            ],
            "seconds",
        ),
    }


def _observation_report(outcomes: Sequence[OutcomeRecord]) -> dict[str, Any]:
    counts: dict[str, int] = {kind.value: 0 for kind in ObservationKind}
    observed_units = 0
    for record in outcomes:
        if record.observations:
            observed_units += 1
        for observation in record.observations:
            counts[observation.kind.value] += 1
    return {
        "byKind": counts,
        "observedOutcomes": observed_units,
        "unobservedOutcomes": len(outcomes) - observed_units,
    }


LIMITATIONS: tuple[str, ...] = (
    "the numerator of every value metric is a count of accepted outcomes; tokens, "
    "runtime, spend, lines of code, commits and change-request counts are only ever "
    "denominators or context",
    "an accepted outcome is one recorded as delivered and neither reverted nor rolled "
    "back; partial delivery is reported separately and never scored as value",
    "outcomes with no recorded result stay in the sample and are counted as unobserved, "
    "so an inconvenient run cannot be dropped to improve a rate",
    "a rate whose denominator is zero, or whose inputs were not observed on every "
    "record in the sample, reads as unknown with its reason; it is never reported as "
    "zero, because a partial denominator would overstate value",
    "subscription figures are a labeled pay-as-you-go equivalent, reported separately "
    "from billed spend, and are never presented as dollars billed",
    "wall-clock value requires an explicit observation window, so a quiet period stays "
    "in the denominator instead of vanishing",
    "time to live is measured only where both intake and live instants were observed",
    "learnings and recommendations are advisory evidence; adopting one is a separate, "
    "human-approved policy change",
)


def build_effectiveness_report(
    outcomes: Sequence[OutcomeRecord],
    *,
    usage_records: Sequence[UsageRecord] = (),
    window: ObservationWindow | None = None,
    learnings: Sequence[LearningRecord] = (),
    project_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Score one sample of outcomes against what producing them consumed.

    The sample is supplied by the caller rather than inferred, and nothing is
    dropped from it: an outcome that recorded no result is visible as
    unobserved, so a rate can never be improved by excluding the runs that
    would spoil it.
    """
    scope = None if project_ids is None else frozenset(project_ids)
    scoped = tuple(record for record in outcomes if scope is None or record.project_id in scope)
    scoped_usage = tuple(
        record for record in usage_records if scope is None or record.project_id in scope
    )
    scoped_learnings = tuple(
        record for record in learnings if scope is None or record.scope_project_id in scope
    )

    by_result = {item.value: 0 for item in OutcomeResult}
    by_deployment = {item.value: 0 for item in DeploymentStatus}
    for record in scoped:
        by_result[record.result.value] += 1
        by_deployment[record.deployment_status.value] += 1
    accepted = sum(1 for record in scoped if record.accepted)
    inputs = _value_inputs(scoped_usage, window)

    by_project: dict[str, Any] = {}
    for project_id in sorted({record.project_id for record in scoped}):
        project_outcomes = [record for record in scoped if record.project_id == project_id]
        project_usage = [record for record in scoped_usage if record.project_id == project_id]
        project_accepted = sum(1 for record in project_outcomes if record.accepted)
        by_project[project_id] = {
            "outcomes": len(project_outcomes),
            "accepted": project_accepted,
            "quality": _quality_report(project_outcomes),
            "value": _value_report(project_accepted, _value_inputs(project_usage, window), window),
            "learnings": sum(1 for item in scoped_learnings if item.scope_project_id == project_id),
        }

    return {
        "schemaVersion": EFFECTIVENESS_SCHEMA_VERSION,
        "window": window.as_dict() if window is not None else None,
        "sampleCount": len(scoped),
        "outcomes": {
            "byResult": by_result,
            "byDeploymentStatus": by_deployment,
            "accepted": accepted,
            "withRecordedResult": sum(1 for record in scoped if record.has_outcome_data),
            "withoutRecordedResult": sum(1 for record in scoped if not record.has_outcome_data),
        },
        "quality": _quality_report(scoped),
        "observations": _observation_report(scoped),
        "value": _value_report(accepted, inputs, window),
        "estimateVsActual": _estimate_vs_actual(scoped_usage),
        "learnings": {
            "count": len(scoped_learnings),
            "byKind": {
                kind.value: sum(1 for item in scoped_learnings if item.kind is kind)
                for kind in LearningKind
            },
            "advisory": True,
        },
        "byProject": by_project,
        "limitations": list(LIMITATIONS),
    }


# -- cross-project comparison -------------------------------------------------

#: Metrics ``compare_projects`` can normalize. Each is a rate in [0, 1] over
#: outcomes, so a task-class mix can be standardized across projects.
COMPARABLE_METRICS: Mapping[str, str] = {
    "acceptedOutcomeRate": "accepted",
    "firstPassVerificationRate": "first_pass_verification",
    "firstPassReviewAcceptanceRate": "first_pass_review_accepted",
    "regressionEscapeRate": "regression_escaped",
    "revertRate": "reverted",
    "rollbackRate": "rolled_back",
}


def _metric_counts(outcomes: Sequence[OutcomeRecord], metric: str) -> tuple[int, int]:
    """Numerator and denominator of one comparable metric over a sample."""
    attribute = COMPARABLE_METRICS[metric]
    if attribute == "accepted":
        scored = [record for record in outcomes if record.has_outcome_data]
        return sum(1 for record in scored if record.accepted), len(scored)
    observed = [record for record in outcomes if getattr(record, attribute) is not None]
    return sum(1 for record in observed if getattr(record, attribute)), len(observed)


def compare_projects(
    outcomes: Sequence[OutcomeRecord],
    *,
    metric: str = "acceptedOutcomeRate",
    project_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Compare projects on one metric, normalized for their task-class mix.

    A project working mostly on small, low-risk units is not more effective
    than one working on hard ones. The raw rate is reported beside a rate
    standardized to the pooled task-class mix of the compared projects, so the
    comparison reflects performance rather than workload. Where a project has
    no data for part of that mix, the covered weight is reported and the
    normalized figure is marked partial instead of being extrapolated.
    """
    _require(
        metric in COMPARABLE_METRICS,
        f"unknown comparison metric: {metric}; expected one of "
        + ", ".join(sorted(COMPARABLE_METRICS)),
    )
    scope = None if project_ids is None else frozenset(project_ids)
    scoped = tuple(record for record in outcomes if scope is None or record.project_id in scope)

    pooled_counts: dict[str, int] = {}
    for record in scoped:
        task_class = record.task_class or "unclassified"
        pooled_counts[task_class] = pooled_counts.get(task_class, 0) + 1
    pooled_total = sum(pooled_counts.values())
    mix = {
        task_class: round(count / pooled_total, 6)
        for task_class, count in sorted(pooled_counts.items())
    }

    projects: dict[str, Any] = {}
    for project_id in sorted({record.project_id for record in scoped}):
        project_outcomes = [record for record in scoped if record.project_id == project_id]
        raw_numerator, raw_denominator = _metric_counts(project_outcomes, metric)
        per_class: dict[str, Any] = {}
        weighted = 0.0
        covered = 0.0
        for task_class, weight in mix.items():
            in_class = [
                record
                for record in project_outcomes
                if (record.task_class or "unclassified") == task_class
            ]
            numerator, denominator = _metric_counts(in_class, metric)
            entry = _rate(metric, numerator, denominator)
            entry["taskClass"] = task_class
            entry["pooledWeight"] = weight
            per_class[task_class] = entry
            if entry["value"] is not None:
                weighted += weight * float(entry["value"])
                covered += weight
        normalized: dict[str, Any]
        if covered <= 0:
            normalized = {
                "status": "unknown",
                "value": None,
                "coveredWeight": 0.0,
                "reason": "this project observed the metric in none of the pooled task classes",
            }
        else:
            normalized = {
                # Rescaled over the covered weight so a project with partial
                # coverage is not penalized for the classes it never worked on.
                "status": "observed" if covered >= 0.999999 else "partial",
                "value": round(weighted / covered, 6),
                "coveredWeight": round(covered, 6),
                "reason": (
                    ""
                    if covered >= 0.999999
                    else "normalized over the task classes this project observed, not the "
                    "whole pooled mix"
                ),
            }
        projects[project_id] = {
            "outcomes": len(project_outcomes),
            "raw": _rate(metric, raw_numerator, raw_denominator),
            "byTaskClass": per_class,
            "normalized": normalized,
            "taskClassMix": {
                task_class: round(
                    sum(
                        1
                        for record in project_outcomes
                        if (record.task_class or "unclassified") == task_class
                    )
                    / len(project_outcomes),
                    6,
                )
                for task_class in mix
            }
            if project_outcomes
            else {},
        }

    return {
        "schemaVersion": EFFECTIVENESS_SCHEMA_VERSION,
        "metric": metric,
        "pooledTaskClassMix": mix,
        "projects": projects,
        "limitations": [
            "the normalized rate standardizes each project to the pooled task-class mix "
            "of the compared projects; it is a like-for-like comparison, not an absolute "
            "measure",
            "a project with no data in part of the pooled mix is marked partial and "
            "normalized only over the classes it observed, never extrapolated",
            "outcomes without a task class are compared as the 'unclassified' class "
            "rather than being dropped",
            *LIMITATIONS[:4],
        ],
    }


# -- credential-free telemetry ------------------------------------------------

#: Every dataclass that makes up one serialized outcome or learning record.
EFFECTIVENESS_SCHEMA_TYPES = (
    OutcomeRecord,
    OutcomeObservation,
    OutcomeProvenance,
    AcceptanceCriterion,
    LearningRecord,
)


def secret_bearing_effectiveness_fields() -> tuple[str, ...]:
    """Outcome- and learning-schema field names that could hold a secret.

    Effectiveness records name evidence; they never carry a credential or a
    prompt. This is structurally empty, and exposed so the guarantee is a test
    rather than a comment.
    """
    return tuple(
        f"{schema.__name__}.{item.name}"
        for schema in EFFECTIVENESS_SCHEMA_TYPES
        for item in fields(schema)
        if _SECRET_KEY.search(item.name) is not None
    )


def outcomes_from_usage(
    records: Sequence[UsageRecord],
) -> dict[str, dict[str, Any]]:
    """Summarize, per work unit, what the usage ledger already knows.

    This is a convenience for a caller assembling outcome records: it reports
    the run counts and results the usage ledger observed, and never guesses an
    outcome from them. Whether a capability was delivered is a judgement backed
    by evidence, not something derivable from how many runs happened.
    """
    summary: dict[str, dict[str, Any]] = {}
    for record in records:
        key = f"{record.project_id}/{record.work_unit_id}"
        entry = summary.setdefault(
            key,
            {
                "projectId": record.project_id,
                "workUnitId": record.work_unit_id,
                "runs": 0,
                "attempts": 0,
                "byResult": {item.value: 0 for item in UsageResult},
                "stages": [],
            },
        )
        entry["runs"] += 1
        entry["attempts"] = max(int(entry["attempts"]), record.attempt)
        result = record.actual.result if record.actual is not None else UsageResult.UNKNOWN
        entry["byResult"][result.value] += 1
        stages = entry["stages"]
        assert isinstance(stages, list)
        if record.stage.value not in stages:
            stages.append(record.stage.value)
    for entry in summary.values():
        stages = entry["stages"]
        assert isinstance(stages, list)
        stages.sort()
    return dict(sorted(summary.items()))
