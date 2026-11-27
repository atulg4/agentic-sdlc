"""Deterministic, read-only schedule forecast for Forge work graphs.

Capacity metrics (``capacity_metrics.py``) explain where time *went*; this
module estimates where it is *going*: per-unit duration ranges from observed
history, the critical path through the remaining work, an ETA range per unit
and per project, and a typed reason for every unit that cannot run.

It is observational only. It never dispatches, reorders, leases or changes
policy, it never reads the wall clock (``now`` is an input), and identical
inputs always produce identical output.

Inputs:

- a **forecast document** holding, per project, the work-graph units (the
  ``WorkUnitSpec`` fields, validated by ``validate_graph`` against the
  project's policy) plus each unit's observed ``status``, ``startedAt`` and
  raw blocking ``signals``;
- the **usage ledger**, whose finished run attempts supply observed runtimes.

Durations are keyed by (project, mission id): the mission id is the one key a
work-graph unit and a usage record share.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from .policy import ProjectPolicy
from .usage_ledger import UsageRecord
from .work_graph import WorkGraph, WorkGraphError, WorkUnitSpec, validate_graph

__all__ = [
    "FORECAST_SCHEMA_VERSION",
    "QUANTILES",
    "BlockingReason",
    "DurationEstimate",
    "ForecastError",
    "ForecastUnit",
    "build_schedule_forecast",
    "classify_signal",
    "estimate_duration",
    "load_forecast_document",
    "nearest_rank",
]

FORECAST_SCHEMA_VERSION = 1
DEFAULT_MIN_SAMPLES = 5
QUANTILES: tuple[tuple[str, float], ...] = (("p50", 0.50), ("p80", 0.80), ("p95", 0.95))

BASIS_HISTORY = "history"
BASIS_POOLED = "pooled-history"
BASIS_DECLARED = "declared-limit"

_STATUSES = frozenset({"pending", "running", "completed", "failed", "blocked"})


class ForecastError(ValueError):
    """Raised when a forecast document or request is malformed."""


class BlockingReason(StrEnum):
    """Why a unit cannot run now. Declaration order is precedence order."""

    DEPENDENCY_BLOCKED = "dependency_blocked"
    POLICY_BLOCKED = "policy_blocked"
    HUMAN_APPROVAL_REQUIRED = "human_approval_required"
    PROVIDER_CAPACITY_EXHAUSTED = "provider_capacity_exhausted"
    SUBSCRIPTION_QUOTA_EXHAUSTED = "subscription_quota_exhausted"
    RUNNER_CAPACITY_EXHAUSTED = "runner_capacity_exhausted"
    BUDGET_BLOCKED = "budget_blocked"
    QUALITY_FLOOR_UNMET = "quality_floor_unmet"
    CONFLICTING_CHANGE_SCOPE = "conflicting_change_scope"
    EXCLUSIVE_RESOURCE_LOCKED = "exclusive_resource_locked"
    ENVIRONMENT_UNAVAILABLE = "environment_unavailable"
    TEST_OR_VERIFICATION_FAILED = "test_or_verification_failed"
    SECURITY_OR_COMPLIANCE_BLOCKED = "security_or_compliance_blocked"
    UNKNOWN = "unknown"


_R = BlockingReason
_PRECEDENCE = {reason: index for index, reason in enumerate(BlockingReason)}

#: Exact-value signals: (source, value) as the producing module emits it.
_VALUE_SIGNALS: Mapping[tuple[str, str], BlockingReason] = {
    # orchestration.WorkUnitState
    ("orchestration-state", "approval-pending"): _R.HUMAN_APPROVAL_REQUIRED,
    # executors.RuntimeStatus
    ("executor-runtime", "quota-exhausted"): _R.SUBSCRIPTION_QUOTA_EXHAUSTED,
    ("executor-runtime", "capacity-exhausted"): _R.PROVIDER_CAPACITY_EXHAUSTED,
    # capacity_metrics.WaitCause: "resource" is assigned only when the pool was
    # saturated for the whole wait, so it is a runner-capacity block.
    ("capacity-wait", "resource"): _R.RUNNER_CAPACITY_EXHAUSTED,
    ("capacity-wait", "dependency"): _R.DEPENDENCY_BLOCKED,
    # deterministic verification outcome
    ("verification", "failed"): _R.TEST_OR_VERIFICATION_FAILED,
}

#: Error-message signals: (source, fragment) the producing module's error text
#: is known to contain.
_MESSAGE_SIGNALS: tuple[tuple[str, str, BlockingReason], ...] = (
    ("orchestration-error", "is dependency-blocked by", _R.DEPENDENCY_BLOCKED),
    ("lease-error", "exclusively leased", _R.EXCLUSIVE_RESOURCE_LOCKED),
    ("work-graph-error", "overlapping write scopes", _R.CONFLICTING_CHANGE_SCOPE),
)


def classify_signal(source: str, value: str) -> BlockingReason:
    """Map one observed signal to a reason; anything unrecognized is UNKNOWN."""

    exact = _VALUE_SIGNALS.get((source, value))
    if exact is not None:
        return exact
    for signal_source, fragment, reason in _MESSAGE_SIGNALS:
        if source == signal_source and fragment in value:
            return reason
    return _R.UNKNOWN


@dataclass(frozen=True)
class ForecastUnit:
    """One work-graph unit plus what is observed about it right now."""

    spec: WorkUnitSpec
    status: str = "pending"
    started_at: str = ""
    signals: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class DurationEstimate:
    seconds: Mapping[str, int]
    sample_size: int
    basis: str

    def as_dict(self) -> dict[str, Any]:
        return {**self.seconds, "sampleSize": self.sample_size, "basis": self.basis}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ForecastError(message)


def _instant(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as error:
        raise ForecastError(f"{label} must be an RFC 3339 timestamp: {value!r}") from error
    _require(parsed.tzinfo is not None, f"{label} must carry a UTC offset: {value!r}")
    return parsed


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def nearest_rank(samples: Sequence[int], quantile: float) -> int:
    """Nearest-rank quantile: the ceil(q * n)-th smallest sample (1-based)."""

    _require(bool(samples), "nearest-rank quantile needs at least one sample")
    ordered = sorted(samples)
    rank = max(1, math.ceil(quantile * len(ordered)))
    return ordered[rank - 1]


def _observed_runtimes(records: Iterable[UsageRecord]) -> dict[tuple[str, str], list[int]]:
    """Finished attempts' runtimes, keyed by (project, mission id)."""

    runtimes: dict[tuple[str, str], list[int]] = {}
    for record in records:
        actual = record.actual
        if actual is None or actual.runtime_seconds is None or not actual.finished_at:
            continue
        key = (record.project_id, record.mission_id)
        runtimes.setdefault(key, []).append(actual.runtime_seconds)
    return runtimes


def estimate_duration(
    spec: WorkUnitSpec,
    *,
    project_id: str,
    runtimes: Mapping[tuple[str, str], Sequence[int]],
    min_samples: int = DEFAULT_MIN_SAMPLES,
) -> DurationEstimate:
    """History for this mission, else pooled project history, else the declared limit.

    A class with fewer than ``min_samples`` observations never yields its own
    (narrow) range: it falls back to the project's pooled distribution and,
    when that is thin too, to the unit's declared timeout for every quantile.
    """

    own = list(runtimes.get((project_id, spec.mission_id), ()))
    if len(own) >= min_samples:
        samples, basis = own, BASIS_HISTORY
    else:
        pooled = [
            value
            for (project, _mission), values in sorted(runtimes.items())
            if project == project_id
            for value in values
        ]
        if len(pooled) >= min_samples:
            samples, basis = pooled, BASIS_POOLED
        else:
            limit = spec.timeout_seconds
            return DurationEstimate(
                seconds={name: limit for name, _ in QUANTILES},
                sample_size=0,
                basis=BASIS_DECLARED,
            )
    return DurationEstimate(
        seconds={name: nearest_rank(samples, q) for name, q in QUANTILES},
        sample_size=len(samples),
        basis=basis,
    )


# -- document loading ------------------------------------------------------

_SPEC_FIELDS: Mapping[str, tuple[str, type]] = {
    "unitId": ("unit_id", str),
    "missionId": ("mission_id", str),
    "missionVersion": ("mission_version", str),
    "outputArtifact": ("output_artifact", str),
    "dependsOn": ("depends_on", tuple),
    "ownedPaths": ("owned_paths", tuple),
    "readPaths": ("read_paths", tuple),
    "writePaths": ("write_paths", tuple),
    "sharedResources": ("shared_resources", tuple),
    "verificationGates": ("verification_gates", tuple),
    "concurrencyGroup": ("concurrency_group", str),
    "composition": ("composition", str),
    "provider": ("provider", str),
    "timeoutSeconds": ("timeout_seconds", int),
    "maxRetries": ("max_retries", int),
    "maxTokens": ("max_tokens", int),
}
_OBSERVED_FIELDS = frozenset({"status", "startedAt", "signals"})


def _load_unit(entry: Any, label: str) -> ForecastUnit:
    _require(isinstance(entry, Mapping), f"{label} must be an object")
    unknown = sorted(set(entry) - set(_SPEC_FIELDS) - _OBSERVED_FIELDS)
    _require(not unknown, f"{label}: unknown keys: {', '.join(unknown)}")
    values: dict[str, Any] = {}
    for key, (attribute, kind) in _SPEC_FIELDS.items():
        if key not in entry:
            continue
        raw = entry[key]
        if kind is tuple:
            _require(
                isinstance(raw, list) and all(isinstance(item, str) for item in raw),
                f"{label}.{key} must be an array of strings",
            )
            values[attribute] = tuple(raw)
        elif kind is int:
            _require(
                isinstance(raw, int) and not isinstance(raw, bool),
                f"{label}.{key} must be an integer",
            )
            values[attribute] = raw
        else:
            _require(isinstance(raw, str), f"{label}.{key} must be a string")
            values[attribute] = raw
    for required in ("unitId", "missionId", "missionVersion", "outputArtifact"):
        _require(required in entry, f"{label}: {required} is required")
    status = entry.get("status", "pending")
    _require(status in _STATUSES, f"{label}.status must be one of {sorted(_STATUSES)}")
    started_at = entry.get("startedAt", "")
    _require(isinstance(started_at, str), f"{label}.startedAt must be a string")
    if started_at:
        _instant(started_at, f"{label}.startedAt")
    signals_raw = entry.get("signals", [])
    _require(isinstance(signals_raw, list), f"{label}.signals must be an array")
    signals = []
    for index, signal in enumerate(signals_raw):
        where = f"{label}.signals[{index}]"
        _require(
            isinstance(signal, Mapping) and set(signal) == {"source", "value"},
            f"{where} must be an object with exactly source and value",
        )
        _require(
            isinstance(signal["source"], str) and isinstance(signal["value"], str),
            f"{where}: source and value must be strings",
        )
        signals.append((signal["source"], signal["value"]))
    return ForecastUnit(
        spec=WorkUnitSpec(**values),
        status=status,
        started_at=started_at,
        signals=tuple(signals),
    )


def load_forecast_document(document: Any) -> dict[str, tuple[ForecastUnit, ...]]:
    """Parse ``{"schemaVersion": 1, "graphs": [{"projectId", "units"}]}``; fail closed."""

    _require(isinstance(document, Mapping), "forecast document must be an object")
    _require(
        document.get("schemaVersion") == FORECAST_SCHEMA_VERSION,
        f"forecast document schemaVersion must be {FORECAST_SCHEMA_VERSION}",
    )
    unknown = sorted(set(document) - {"schemaVersion", "graphs"})
    _require(not unknown, f"forecast document: unknown keys: {', '.join(unknown)}")
    graphs = document.get("graphs")
    _require(isinstance(graphs, list) and graphs, "forecast document needs a graphs array")
    projects: dict[str, tuple[ForecastUnit, ...]] = {}
    for index, graph in enumerate(graphs):
        label = f"graphs[{index}]"
        _require(
            isinstance(graph, Mapping) and set(graph) == {"projectId", "units"},
            f"{label} must be an object with exactly projectId and units",
        )
        project_id = graph["projectId"]
        _require(
            isinstance(project_id, str) and project_id.strip(),
            f"{label}.projectId is required",
        )
        _require(project_id not in projects, f"duplicate graph for project {project_id}")
        units = graph["units"]
        _require(isinstance(units, list), f"{label}.units must be an array")
        projects[project_id] = tuple(
            _load_unit(unit, f"{label}.units[{position}]") for position, unit in enumerate(units)
        )
    return projects


# -- forecasting -----------------------------------------------------------


def _blocking_reasons(
    unit: ForecastUnit, statuses: Mapping[str, str]
) -> tuple[BlockingReason, ...]:
    if unit.status == "completed":
        return ()
    reasons = {classify_signal(source, value) for source, value in unit.signals}
    if unit.status != "running":
        unfinished = [dep for dep in unit.spec.depends_on if statuses[dep] != "completed"]
        if unfinished or unit.status == "blocked":
            reasons.add(_R.DEPENDENCY_BLOCKED)
        if unit.status == "failed" and not reasons:
            # A failed unit with no classified cause cannot be called runnable.
            reasons.add(_R.UNKNOWN)
    return tuple(sorted(reasons, key=_PRECEDENCE.__getitem__))


def _schedule(
    graph: WorkGraph,
    units: Mapping[str, ForecastUnit],
    durations: Mapping[str, DurationEstimate],
    now: datetime,
    quantile: str,
) -> tuple[dict[str, datetime], dict[str, datetime], dict[str, str]]:
    """Earliest start and finish per unit for one quantile, plus the dependency
    that set each start (for tracing the critical path)."""

    starts: dict[str, datetime] = {}
    finishes: dict[str, datetime] = {}
    drivers: dict[str, str] = {}
    for unit_id in graph.order:
        unit = units[unit_id]
        if unit.status == "completed":
            starts[unit_id] = finishes[unit_id] = now
            continue
        duration = timedelta(seconds=durations[unit_id].seconds[quantile])
        if unit.status == "running":
            start = (
                _instant(unit.started_at, f"unit {unit_id} startedAt") if unit.started_at else now
            )
            starts[unit_id] = start
            finishes[unit_id] = max(now, start + duration)
            continue
        start, driver = now, ""
        # Latest-finishing dependency drives the start; ties go to the lowest id.
        for dependency in sorted(unit.spec.depends_on):
            if finishes[dependency] > start:
                start, driver = finishes[dependency], dependency
        starts[unit_id] = start
        finishes[unit_id] = start + duration
        if driver:
            drivers[unit_id] = driver
    return starts, finishes, drivers


def _critical_path(
    graph: WorkGraph,
    units: Mapping[str, ForecastUnit],
    finishes: Mapping[str, datetime],
    drivers: Mapping[str, str],
) -> tuple[str, ...]:
    remaining = [unit_id for unit_id in graph.order if units[unit_id].status != "completed"]
    if not remaining:
        return ()
    # Latest finish ends the path; ties go to the lowest unit id.
    tail = min(remaining, key=lambda unit_id: (-finishes[unit_id].timestamp(), unit_id))
    path = [tail]
    while path[-1] in drivers:
        path.append(drivers[path[-1]])
    return tuple(reversed(path))


def _forecast_project(
    project_id: str,
    units_in: Sequence[ForecastUnit],
    *,
    policy: ProjectPolicy,
    runtimes: Mapping[tuple[str, str], Sequence[int]],
    now: datetime,
    min_samples: int,
) -> dict[str, Any]:
    try:
        graph = validate_graph(tuple(unit.spec for unit in units_in), policy)
    except WorkGraphError as error:
        raise ForecastError(f"project {project_id}: {error}") from error
    units = {unit.spec.unit_id: unit for unit in units_in}
    statuses = {unit_id: unit.status for unit_id, unit in units.items()}
    durations = {
        unit_id: estimate_duration(
            unit.spec, project_id=project_id, runtimes=runtimes, min_samples=min_samples
        )
        for unit_id, unit in units.items()
    }
    schedules = {name: _schedule(graph, units, durations, now, name) for name, _ in QUANTILES}
    path_p50 = _critical_path(graph, units, schedules["p50"][1], schedules["p50"][2])
    path_p80 = _critical_path(graph, units, schedules["p80"][1], schedules["p80"][2])

    unit_documents = []
    blockers: dict[str, list[str]] = {}
    for unit_id in graph.order:
        unit = units[unit_id]
        reasons = _blocking_reasons(unit, statuses)
        for reason in reasons:
            blockers.setdefault(reason.value, []).append(unit_id)
        unit_documents.append(
            {
                "unitId": unit_id,
                "missionId": unit.spec.mission_id,
                "status": unit.status,
                "earliestStart": _stamp(schedules["p50"][0][unit_id]),
                "expectedDuration": durations[unit_id].as_dict(),
                "etaRange": {name: _stamp(schedules[name][1][unit_id]) for name, _ in QUANTILES},
                "onCriticalPath": unit_id in path_p50,
                "runnable": unit.status == "pending" and not reasons,
                "blockingReason": reasons[0].value if reasons else None,
                "blockingReasons": [reason.value for reason in reasons],
                "parallelismGroup": unit.spec.concurrency_group or None,
            }
        )
    return {
        "projectId": project_id,
        "etaRange": {
            name: _stamp(max(schedules[name][1].values(), default=now)) for name, _ in QUANTILES
        },
        "criticalPath": list(path_p50),
        "criticalPathP80": list(path_p80),
        "active": sum(1 for unit in units.values() if unit.status == "running"),
        "queued": sum(1 for unit in units.values() if unit.status not in {"running", "completed"}),
        "completed": sum(1 for unit in units.values() if unit.status == "completed"),
        "blockers": {reason: sorted(ids) for reason, ids in sorted(blockers.items())},
        "units": unit_documents,
    }


def build_schedule_forecast(
    projects: Mapping[str, Sequence[ForecastUnit]],
    usage_records: Iterable[UsageRecord],
    *,
    policy: ProjectPolicy,
    now: str,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    project_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Forecast every selected project's graph. Read-only and deterministic."""

    _require(min_samples >= 1, "min_samples must be at least 1")
    observed_at = _instant(now, "now")
    selected = sorted(projects) if project_ids is None else sorted(set(project_ids))
    missing = [project_id for project_id in selected if project_id not in projects]
    _require(not missing, f"no graph for project(s): {', '.join(missing)}")
    runtimes = _observed_runtimes(
        record for record in usage_records if record.project_id in set(selected)
    )
    return {
        "schemaVersion": FORECAST_SCHEMA_VERSION,
        "observedAt": now,
        "minSamples": min_samples,
        "quantileMethod": "nearest-rank",
        "projects": [
            _forecast_project(
                project_id,
                projects[project_id],
                policy=policy,
                runtimes=runtimes,
                now=observed_at,
                min_samples=min_samples,
            )
            for project_id in selected
        ],
    }
