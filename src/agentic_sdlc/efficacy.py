"""Efficacy scoring, regression detection, and bounded self-correction.

Immutable run outcomes feed versioned, explainable metrics; repeated quality
findings become deduplicated correction proposals; and controlled experiments
compare challenger policies against the baseline on out-of-sample, comparable
cohorts. Nothing here can merge, deploy, expand permissions, or change a
production policy without human approval, and metrics cannot be gamed by
dropping failed, cancelled, expensive, or inconclusive runs.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

__all__ = [
    "CorrectionExperiment",
    "CorrectionProposal",
    "EfficacyError",
    "EfficacyMetric",
    "METRIC_DEFINITIONS",
    "OutcomeLedger",
    "PolicyVersionStore",
    "QualityFinding",
    "RunOutcome",
    "build_report",
    "compare_cohorts",
    "compute_metrics",
    "propose_corrections",
]

METRICS_VERSION = "1.0.0"


class EfficacyError(ValueError):
    """Raised when efficacy accounting or correction rules are violated."""


RESULTS = frozenset({"completed", "failed", "abandoned", "cancelled", "inconclusive"})
HUMAN_ACTIONS = frozenset({"merged", "rejected", "overridden", "none"})
OVERRIDE_CLASSES = frozenset(
    {"", "unclassified", "model-failure", "model-success", "owner-preference", "external-factor"}
)
EXPERIMENT_VARIABLES = frozenset(
    {
        "mission-prompt",
        "routing-rule",
        "context-pack-composition",
        "agent-selection",
        "verification-gate",
        "concurrency-limit",
        "retry-limit",
        "task-decomposition",
    }
)
EXPERIMENT_MODES = frozenset({"shadow", "cohort"})
SAFETY_METRICS = ("policy_violation_rate", "escaped_defect_rate")
QUALITY_METRICS = ("task_completion_rate", "first_pass_review_acceptance_rate")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical(document: Any) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class RunOutcome:
    """One immutable, fully attributed autonomous run outcome."""

    work_ref: str
    mission_id: str
    mission_version: str
    agent_id: str
    model: str
    task_class: str
    cohort: str
    result: str
    context_pack_digest: str = ""
    commit_sha: str = ""
    verification_passed: bool | None = None
    review_findings: int | None = None
    repair_rounds: int = 0
    merged: bool = False
    human_action: str = "none"
    human_override_class: str = ""
    escaped_defect: bool = False
    reopened: bool = False
    policy_violations: int = 0
    tokens_used: int = 0
    runtime_seconds: int = 0
    cycle_time_seconds: int = 0
    finished_at: str = ""

    @property
    def outcome_id(self) -> str:
        identity = _canonical(
            [self.work_ref, self.mission_id, self.agent_id, self.finished_at, self.result]
        )
        return "ro-" + _sha256_text(identity)[:16]

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcomeId": self.outcome_id,
            "workRef": self.work_ref,
            "missionId": self.mission_id,
            "missionVersion": self.mission_version,
            "agentId": self.agent_id,
            "model": self.model,
            "taskClass": self.task_class,
            "cohort": self.cohort,
            "result": self.result,
            "contextPackDigest": self.context_pack_digest,
            "commitSha": self.commit_sha,
            "verificationPassed": self.verification_passed,
            "reviewFindings": self.review_findings,
            "repairRounds": self.repair_rounds,
            "merged": self.merged,
            "humanAction": self.human_action,
            "humanOverrideClass": self.human_override_class,
            "escapedDefect": self.escaped_defect,
            "reopened": self.reopened,
            "policyViolations": self.policy_violations,
            "tokensUsed": self.tokens_used,
            "runtimeSeconds": self.runtime_seconds,
            "cycleTimeSeconds": self.cycle_time_seconds,
            "finishedAt": self.finished_at,
        }


def _validate_outcome(outcome: RunOutcome) -> None:
    if outcome.result not in RESULTS:
        raise EfficacyError(f"unknown result: {outcome.result}")
    if outcome.human_action not in HUMAN_ACTIONS:
        raise EfficacyError(f"unknown human action: {outcome.human_action}")
    if outcome.human_override_class not in OVERRIDE_CLASSES:
        raise EfficacyError(f"unknown override class: {outcome.human_override_class}")
    if outcome.human_action == "overridden" and not outcome.human_override_class:
        raise EfficacyError(
            "human overrides must carry a classification (use 'unclassified' until a "
            "human classifies the override); they are never auto-scored"
        )
    if not outcome.work_ref.strip() or not outcome.mission_id.strip():
        raise EfficacyError("work_ref and mission_id are required")
    if min(outcome.repair_rounds, outcome.policy_violations, outcome.tokens_used) < 0:
        raise EfficacyError("counters cannot be negative")


class OutcomeLedger:
    """Append-only outcome store: nothing is ever dropped or rewritten."""

    def __init__(self) -> None:
        self._outcomes: dict[str, RunOutcome] = {}

    def record(self, outcome: RunOutcome) -> str:
        _validate_outcome(outcome)
        if outcome.outcome_id in self._outcomes:
            if self._outcomes[outcome.outcome_id] == outcome:
                return outcome.outcome_id
            raise EfficacyError(f"outcome {outcome.outcome_id} is immutable")
        self._outcomes[outcome.outcome_id] = outcome
        return outcome.outcome_id

    def outcomes(self) -> tuple[RunOutcome, ...]:
        return tuple(self._outcomes.values())

    def __len__(self) -> int:
        return len(self._outcomes)


@dataclass(frozen=True)
class EfficacyMetric:
    """One versioned metric value with its full derivation exposed."""

    name: str
    value: float
    numerator: int
    denominator: int
    formula: str
    cohort: str = "all"
    confidence_interval: tuple[float, float] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "metricsVersion": METRICS_VERSION,
            "value": self.value,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "sampleCount": self.denominator,
            "formula": self.formula,
            "cohort": self.cohort,
            "confidenceInterval": (
                list(self.confidence_interval) if self.confidence_interval else None
            ),
        }


METRIC_DEFINITIONS: dict[str, str] = {
    "task_completion_rate": (
        "completed-and-merged outcomes / all outcomes (failed, abandoned, cancelled, "
        "and inconclusive runs stay in the denominator; missing data is never success)"
    ),
    "first_pass_verification_rate": ("verification passed with zero repair rounds / all outcomes"),
    "first_pass_review_acceptance_rate": (
        "zero review findings and zero repair rounds / all outcomes with a recorded review"
    ),
    "repair_success_rate": (
        "repaired outcomes that ended completed / outcomes with at least one repair round"
    ),
    "escaped_defect_rate": "outcomes flagged escaped-defect / merged outcomes",
    "reopened_rate": "reopened outcomes / merged outcomes",
    "human_intervention_rate": "human overrides / all outcomes",
    "policy_violation_rate": "outcomes with any policy violation / all outcomes",
    "mean_tokens": "sum of tokens used / all outcomes (cost dimension)",
    "mean_cycle_seconds": "sum of cycle time / all outcomes (speed dimension)",
}
_MIN_CI_SAMPLE = 10


def _wilson(numerator: int, denominator: int) -> tuple[float, float] | None:
    if denominator < _MIN_CI_SAMPLE:
        return None
    z = 1.96
    p = numerator / denominator
    n = denominator
    center = (p + z * z / (2 * n)) / (1 + z * z / n)
    spread = (z / (1 + z * z / n)) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (round(max(0.0, center - spread), 4), round(min(1.0, center + spread), 4))


def _rate(name: str, numerator: int, denominator: int, cohort: str) -> EfficacyMetric:
    value = round(numerator / denominator, 4) if denominator else 0.0
    return EfficacyMetric(
        name=name,
        value=value,
        numerator=numerator,
        denominator=denominator,
        formula=METRIC_DEFINITIONS[name],
        cohort=cohort,
        confidence_interval=_wilson(numerator, denominator),
    )


def compute_metrics(
    outcomes: Sequence[RunOutcome],
    *,
    cohort: str = "all",
) -> dict[str, EfficacyMetric]:
    """Versioned metrics over the full outcome set — nothing is excluded."""
    total = len(outcomes)
    merged = [item for item in outcomes if item.merged]
    reviewed = [item for item in outcomes if item.review_findings is not None]
    repaired = [item for item in outcomes if item.repair_rounds > 0]

    metrics = {
        "task_completion_rate": _rate(
            "task_completion_rate",
            sum(1 for item in outcomes if item.result == "completed" and item.merged),
            total,
            cohort,
        ),
        "first_pass_verification_rate": _rate(
            "first_pass_verification_rate",
            sum(
                1
                for item in outcomes
                if item.verification_passed is True and item.repair_rounds == 0
            ),
            total,
            cohort,
        ),
        "first_pass_review_acceptance_rate": _rate(
            "first_pass_review_acceptance_rate",
            sum(1 for item in reviewed if item.review_findings == 0 and item.repair_rounds == 0),
            len(reviewed),
            cohort,
        ),
        "repair_success_rate": _rate(
            "repair_success_rate",
            sum(1 for item in repaired if item.result == "completed"),
            len(repaired),
            cohort,
        ),
        "escaped_defect_rate": _rate(
            "escaped_defect_rate",
            sum(1 for item in merged if item.escaped_defect),
            len(merged),
            cohort,
        ),
        "reopened_rate": _rate(
            "reopened_rate", sum(1 for item in merged if item.reopened), len(merged), cohort
        ),
        "human_intervention_rate": _rate(
            "human_intervention_rate",
            sum(1 for item in outcomes if item.human_action == "overridden"),
            total,
            cohort,
        ),
        "policy_violation_rate": _rate(
            "policy_violation_rate",
            sum(1 for item in outcomes if item.policy_violations > 0),
            total,
            cohort,
        ),
    }
    metrics["mean_tokens"] = EfficacyMetric(
        name="mean_tokens",
        value=round(sum(item.tokens_used for item in outcomes) / total, 2) if total else 0.0,
        numerator=sum(item.tokens_used for item in outcomes),
        denominator=total,
        formula=METRIC_DEFINITIONS["mean_tokens"],
        cohort=cohort,
    )
    metrics["mean_cycle_seconds"] = EfficacyMetric(
        name="mean_cycle_seconds",
        value=(
            round(sum(item.cycle_time_seconds for item in outcomes) / total, 2) if total else 0.0
        ),
        numerator=sum(item.cycle_time_seconds for item in outcomes),
        denominator=total,
        formula=METRIC_DEFINITIONS["mean_cycle_seconds"],
        cohort=cohort,
    )
    return metrics


def build_report(
    ledger: OutcomeLedger,
    *,
    min_sample: int = 20,
) -> dict[str, Any]:
    """Owner-facing scorecard separated by dimension, never one opaque score."""
    outcomes = ledger.outcomes()
    metrics = compute_metrics(outcomes)
    sample = len(outcomes)
    composite: dict[str, Any] | None = None
    if sample >= min_sample:
        components = {
            "quality": metrics["first_pass_review_acceptance_rate"].value,
            "completion": metrics["task_completion_rate"].value,
            "safety": 1.0 - metrics["policy_violation_rate"].value,
        }
        weights = {"quality": 0.4, "completion": 0.3, "safety": 0.3}
        composite = {
            "value": round(sum(components[k] * weights[k] for k in components), 4),
            "components": components,
            "weights": weights,
            "sampleCount": sample,
            "limitations": (
                "composite is optional and advisory; quality, speed, cost, autonomy, "
                "and safety are reported separately and are not interchangeable"
            ),
        }
    return {
        "schemaVersion": 1,
        "metricsVersion": METRICS_VERSION,
        "sampleCount": sample,
        "dimensions": {
            "quality": [
                metrics["first_pass_review_acceptance_rate"].as_dict(),
                metrics["first_pass_verification_rate"].as_dict(),
                metrics["repair_success_rate"].as_dict(),
            ],
            "safety": [
                metrics["policy_violation_rate"].as_dict(),
                metrics["escaped_defect_rate"].as_dict(),
                metrics["reopened_rate"].as_dict(),
            ],
            "autonomy": [metrics["human_intervention_rate"].as_dict()],
            "speed": [metrics["mean_cycle_seconds"].as_dict()],
            "cost": [metrics["mean_tokens"].as_dict()],
            "completion": [metrics["task_completion_rate"].as_dict()],
        },
        "composite": composite,
        "compositeSuppressedReason": (
            None if composite else f"sample {sample} below minimum {min_sample}"
        ),
    }


def compare_cohorts(
    baseline: Sequence[RunOutcome],
    challenger: Sequence[RunOutcome],
    *,
    min_sample: int = 20,
) -> dict[str, Any]:
    """Compare two cohorts only when they are comparable and well-sampled."""
    if len(baseline) < min_sample or len(challenger) < min_sample:
        raise EfficacyError(
            f"insufficient samples for a superiority claim: baseline={len(baseline)}, "
            f"challenger={len(challenger)}, minimum={min_sample}"
        )
    baseline_classes = {item.task_class for item in baseline}
    challenger_classes = {item.task_class for item in challenger}
    if baseline_classes != challenger_classes:
        raise EfficacyError(
            "cohorts are not comparable: task classes differ "
            f"({sorted(baseline_classes)} vs {sorted(challenger_classes)})"
        )
    return {
        "baseline": {
            name: metric.as_dict()
            for name, metric in compute_metrics(baseline, cohort="baseline").items()
        },
        "challenger": {
            name: metric.as_dict()
            for name, metric in compute_metrics(challenger, cohort="challenger").items()
        },
    }


@dataclass(frozen=True)
class QualityFinding:
    """A recurring quality signal from reviews, defects, or verification."""

    work_ref: str
    mission_id: str
    category: str
    severity: str
    description: str

    @property
    def fingerprint(self) -> str:
        return _sha256_text(_canonical([self.mission_id, self.category, self.description]))[:16]

    def as_dict(self) -> dict[str, Any]:
        return {
            "workRef": self.work_ref,
            "missionId": self.mission_id,
            "category": self.category,
            "severity": self.severity,
            "description": self.description,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class CorrectionProposal:
    """One deduplicated, evidence-backed improvement work item."""

    fingerprint: str
    mission_id: str
    category: str
    description: str
    occurrences: int
    evidence: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "proposalId": "cp-" + self.fingerprint,
            "missionId": self.mission_id,
            "category": self.category,
            "description": self.description,
            "occurrences": self.occurrences,
            "evidence": list(self.evidence),
        }


def propose_corrections(
    findings: Sequence[QualityFinding],
    *,
    min_occurrences: int = 3,
) -> tuple[CorrectionProposal, ...]:
    """Repeated findings converge into one deduplicated proposal each."""
    grouped: dict[str, list[QualityFinding]] = {}
    for finding in findings:
        grouped.setdefault(finding.fingerprint, []).append(finding)
    proposals = []
    for fingerprint, items in sorted(grouped.items()):
        if len(items) < min_occurrences:
            continue
        first = items[0]
        proposals.append(
            CorrectionProposal(
                fingerprint=fingerprint,
                mission_id=first.mission_id,
                category=first.category,
                description=first.description,
                occurrences=len(items),
                evidence=tuple(dict.fromkeys(item.work_ref for item in items)),
            )
        )
    return tuple(proposals)


class PolicyVersionStore:
    """Content-addressed policy versions with an explicit approved set."""

    def __init__(self) -> None:
        self._versions: dict[str, str] = {}
        self._approved: set[str] = set()

    def register(self, document: str, *, approved_by: str = "") -> str:
        digest = "pv-" + _sha256_text(document)[:16]
        self._versions[digest] = document
        if approved_by:
            self._approved.add(digest)
        return digest

    def approve(self, digest: str, *, approved_by: str) -> None:
        if digest not in self._versions:
            raise EfficacyError(f"unknown policy version: {digest}")
        if not approved_by.strip():
            raise EfficacyError("approval requires a named human approver")
        self._approved.add(digest)

    def is_approved(self, digest: str) -> bool:
        return digest in self._approved

    def get(self, digest: str) -> str:
        document = self._versions.get(digest)
        if document is None:
            raise EfficacyError(f"unknown policy version: {digest}")
        return document


@dataclass(frozen=True)
class CorrectionExperiment:
    """A bounded, versioned challenger-vs-baseline experiment."""

    experiment_id: str
    hypothesis: str
    hypothesis_evidence: tuple[str, ...]
    hypothesis_outcome_ids: tuple[str, ...]
    variable: str
    baseline_version: str
    challenger_version: str
    mode: str
    proposed_by: str
    high_risk: bool = False
    status: str = "proposed"
    evaluated_by: str = ""
    decision_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "experimentId": self.experiment_id,
            "hypothesis": self.hypothesis,
            "hypothesisEvidence": list(self.hypothesis_evidence),
            "variable": self.variable,
            "baselineVersion": self.baseline_version,
            "challengerVersion": self.challenger_version,
            "mode": self.mode,
            "proposedBy": self.proposed_by,
            "highRisk": self.high_risk,
            "status": self.status,
            "evaluatedBy": self.evaluated_by,
            "decisionReason": self.decision_reason,
        }


def create_experiment(
    *,
    hypothesis: str,
    hypothesis_evidence: Sequence[str],
    hypothesis_outcomes: Sequence[RunOutcome],
    variable: str,
    baseline_version: str,
    challenger_version: str,
    mode: str = "shadow",
    proposed_by: str,
    high_risk: bool = False,
) -> CorrectionExperiment:
    if variable not in EXPERIMENT_VARIABLES:
        raise EfficacyError(f"experiments change one bounded variable, not: {variable}")
    if mode not in EXPERIMENT_MODES:
        raise EfficacyError(f"unknown experiment mode: {mode}")
    if not hypothesis.strip() or not hypothesis_evidence:
        raise EfficacyError("experiments require an evidence-backed hypothesis")
    identity = _canonical([hypothesis, variable, baseline_version, challenger_version])
    return CorrectionExperiment(
        experiment_id="exp-" + _sha256_text(identity)[:12],
        hypothesis=hypothesis.strip(),
        hypothesis_evidence=tuple(hypothesis_evidence),
        hypothesis_outcome_ids=tuple(item.outcome_id for item in hypothesis_outcomes),
        variable=variable,
        baseline_version=baseline_version,
        challenger_version=challenger_version,
        mode=mode,
        proposed_by=proposed_by,
        high_risk=high_risk,
    )


def evaluate_experiment(
    experiment: CorrectionExperiment,
    baseline_outcomes: Sequence[RunOutcome],
    challenger_outcomes: Sequence[RunOutcome],
    *,
    evaluated_by: str,
    min_sample: int = 20,
) -> CorrectionExperiment:
    """Score a challenger on out-of-sample, comparable cohorts only."""
    if experiment.status != "proposed":
        raise EfficacyError(f"experiment is not awaiting evaluation: {experiment.status}")
    in_sample = {item.outcome_id for item in challenger_outcomes} & set(
        experiment.hypothesis_outcome_ids
    )
    if in_sample:
        raise EfficacyError(
            "evaluation outcomes overlap the observations that generated the "
            "hypothesis; a challenger cannot be scored in-sample"
        )
    comparison = compare_cohorts(baseline_outcomes, challenger_outcomes, min_sample=min_sample)
    baseline = comparison["baseline"]
    challenger = comparison["challenger"]

    quality_regressed = any(
        challenger[name]["value"] < baseline[name]["value"] for name in QUALITY_METRICS
    )
    safety_regressed = any(
        challenger[name]["value"] > baseline[name]["value"] for name in SAFETY_METRICS
    )
    improved = (
        challenger["first_pass_review_acceptance_rate"]["value"]
        > baseline["first_pass_review_acceptance_rate"]["value"]
        or challenger["task_completion_rate"]["value"] > baseline["task_completion_rate"]["value"]
        or challenger["mean_tokens"]["value"] < baseline["mean_tokens"]["value"]
    )
    if safety_regressed or quality_regressed:
        status, reason = (
            "rejected",
            ("challenger degraded safety or quality; cost or speed gains cannot compensate"),
        )
    elif improved:
        status, reason = "evaluated", "challenger improved without safety/quality regression"
    else:
        status, reason = "inconclusive", "no measurable improvement"
    return replace(experiment, status=status, evaluated_by=evaluated_by, decision_reason=reason)


def promote_experiment(
    experiment: CorrectionExperiment,
    store: PolicyVersionStore,
    *,
    approved_by: str,
) -> CorrectionExperiment:
    """Promotion always requires a human; high-risk also requires independence."""
    if experiment.status != "evaluated":
        raise EfficacyError(
            f"only evaluated experiments can be promoted (status: {experiment.status})"
        )
    if not approved_by.strip():
        raise EfficacyError("promotion requires a named human approver")
    if experiment.high_risk and experiment.evaluated_by == experiment.proposed_by:
        raise EfficacyError(
            "a high-risk policy change cannot be evaluated and approved by its own "
            "proposer; independent review is required"
        )
    store.approve(experiment.challenger_version, approved_by=approved_by)
    return replace(
        experiment,
        status="promoted",
        decision_reason=f"promoted by {approved_by}: {experiment.decision_reason}",
    )


def rollback(
    store: PolicyVersionStore,
    *,
    target_version: str,
    reason: str,
) -> str:
    """Automatic rollback restores only a previously approved version."""
    if not store.is_approved(target_version):
        raise EfficacyError(
            "rollback targets must be previously approved policy versions; the "
            "platform never invents a new production policy autonomously"
        )
    if not reason.strip():
        raise EfficacyError("rollback requires a recorded guardrail reason")
    return store.get(target_version)
