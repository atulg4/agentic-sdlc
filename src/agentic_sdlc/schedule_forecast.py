"""Deterministic, read-only scheduling forecasts: ETA, critical path, blocking reasons.

The usage ledger and the work graph already know how long stages have taken
and which units depend on which. This module turns that history into a
forecast a human or a dashboard can act on: when a unit can realistically
start, how long it is likely to take, which units sit on the longest
remaining path, and — when a unit is not running — a typed reason why.

Two rules carry over from ``capacity_metrics.py``, the module this one is
modeled on:

- **A narrow range is never emitted from few samples.** Below a configurable
  minimum sample count, the forecast widens to a broader, explicitly labeled
  basis rather than reporting a falsely precise range from one or two points.
- **Unknown is never silently treated as ready.** A unit this module cannot
  explain the state of is reported with ``blockingReason: "unknown"`` and
  excluded from the active/queued counts, rather than guessed into either
  bucket.

This module never dispatches, reorders, or changes concurrency, leases,
routing, or policy. It only reads a work graph and usage history and reports
what they already imply.

Scope of what is derived versus declared: ``dependency_blocked`` comes
straight from the work graph; ``exclusive_resource_locked`` comes from an
actually held lease on a declared owned path or shared resource — the same
resources ``work_graph.GraphExecutor`` leases, so this module invents no
exclusion a composition strategy does not itself impose; ``unknown`` and
``test_or_verification_failed`` come from the unit's own usage-record
history. The remaining :class:`BlockingReason` values, including
``conflicting_change_scope``, name causes this module cannot itself observe
— human approval, policy, provider/subscription/runner capacity, budget,
quality floor, environment, security/compliance, and real fan-in conflicts
each live in a different subsystem (the event ledger, executors, gates,
policy, the composition/fan-in step itself). A caller that has that
evidence declares it through ``declared_blocks``, the same "declare what the
records cannot carry" pattern ``CapacityInputs`` uses; what is not declared
is never guessed.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from .event_ledger import LifecycleStage
from .usage_ledger import UsageRecord, UsageResult, parse_timestamp
from .work_graph import DEFAULT_COMPOSITION, WorkGraph, WorkUnitSpec

__all__ = [
    "DEFAULT_MIN_SAMPLE_SIZE",
    "SCHEDULE_FORECAST_SCHEMA_VERSION",
    "BlockingReason",
    "DurationBasis",
    "DurationForecast",
    "ScheduleForecastError",
    "build_schedule_forecast",
    "load_work_graph",
    "load_work_graph_document",
    "quantile",
]

SCHEDULE_FORECAST_SCHEMA_VERSION = 1

#: Below this many observed samples, a class's own history is not trusted for
#: a quantile range; the forecast widens to a broader basis instead.
DEFAULT_MIN_SAMPLE_SIZE = 5

#: The stage this module forecasts a not-yet-started unit's duration against.
#: Units are multi-stage (implementation, verification, review, repair...);
#: this module forecasts only the stage a unit is currently in, or, if it has
#: not started, the implementation stage as a proxy for its remaining work.
#: Composing a full multi-stage duration is listed as a known limitation.
_DEFAULT_DURATION_STAGE = LifecycleStage.IMPLEMENTATION

#: Stages whose exhausted-retry failure is reported as a verification/review
#: block rather than the generic "unknown" fallback.
_VERIFICATION_STAGES = frozenset(
    {LifecycleStage.DETERMINISTIC_VERIFICATION, LifecycleStage.INDEPENDENT_REVIEW}
)

#: A unit's run is considered merged — done, contributing no remaining
#: duration — once a completed usage record exists for this stage.
_DONE_STAGE = LifecycleStage.MERGE


class ScheduleForecastError(ValueError):
    """Raised when a work graph or forecast input is invalid or inconsistent."""


class BlockingReason(StrEnum):
    """Why a not-yet-done unit is not currently runnable."""

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


class DurationBasis(StrEnum):
    """Where a duration forecast's quantiles actually came from."""

    HISTORY = "history"
    POOLED_HISTORY = "pooled-history"
    DECLARED_LIMIT = "declared-limit"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ScheduleForecastError(message)


def _instant(value: str, label: str) -> datetime:
    try:
        return parse_timestamp(value, label)
    except ValueError as error:
        raise ScheduleForecastError(str(error)) from error


def _round_seconds(value: float) -> float | int:
    return int(value) if float(value).is_integer() else round(value, 3)


def quantile(samples: Sequence[float], q: float) -> float:
    """Nearest-rank quantile: the smallest sample at or above the ``q`` share.

    ``samples`` need not be sorted. For a sorted copy of ``n`` samples, the
    result is ``samples[ceil(q * n) - 1]``, clamped to a valid index. This is
    the nearest-rank method: deterministic, dependency-free, and never
    interpolates a value that was not actually observed.
    """
    _require(0 < q <= 1, f"quantile must be in (0, 1]: {q}")
    _require(bool(samples), "quantile requires at least one sample")
    ordered = sorted(samples)
    index = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
    return ordered[index]


@dataclass(frozen=True)
class DurationForecast:
    """A unit or class's expected duration, with the evidence it rests on."""

    p50: float
    p80: float
    p95: float
    sample_size: int
    basis: DurationBasis

    def __post_init__(self) -> None:
        _require(self.sample_size >= 0, "duration forecast sampleSize must be >= 0")
        _require(
            self.p50 <= self.p80 <= self.p95,
            f"duration forecast quantiles must be non-decreasing: "
            f"p50={self.p50} p80={self.p80} p95={self.p95}",
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "p50": _round_seconds(self.p50),
            "p80": _round_seconds(self.p80),
            "p95": _round_seconds(self.p95),
            "sampleSize": self.sample_size,
            "basis": self.basis.value,
        }


def _finished_durations(records: Sequence[UsageRecord]) -> list[float]:
    return [
        float(record.actual.runtime_seconds)
        for record in records
        if record.actual is not None and record.actual.runtime_seconds is not None
    ]


def _duration_forecast(
    tiers: Sequence[tuple[Sequence[float], DurationBasis]],
    declared_max_seconds: float,
    *,
    min_sample_size: int,
) -> DurationForecast:
    """Pick the narrowest basis that still has enough samples to trust.

    ``tiers`` is tried narrowest-first (the unit's own stage history, then
    its project's stage-wide pool, then its project-wide pool, ...). Below
    ``min_sample_size`` real observations, a tier is not used at all — not
    even to compute a degenerate, falsely narrow range — and the forecast
    falls through to the next, broader tier.
    """
    for samples, basis in tiers:
        if len(samples) >= min_sample_size:
            return DurationForecast(
                p50=quantile(samples, 0.50),
                p80=quantile(samples, 0.80),
                p95=quantile(samples, 0.95),
                sample_size=len(samples),
                basis=basis,
            )
    # No real evidence: scale the declared ceiling into a widening range
    # instead of repeating one number across p50/p80/p95, which would read as
    # a confident, zero-width forecast with no data behind it at all.
    return DurationForecast(
        p50=declared_max_seconds * 0.50,
        p80=declared_max_seconds * 0.80,
        p95=declared_max_seconds * 0.95,
        sample_size=0,
        basis=DurationBasis.DECLARED_LIMIT,
    )


# -- unit status, derived from its own usage-record history -------------------


@dataclass(frozen=True)
class _UnitStatus:
    done: bool
    active: bool
    stuck: BlockingReason | None  # TEST_OR_VERIFICATION_FAILED or UNKNOWN, else None
    current_stage: LifecycleStage
    #: When active, the in-flight attempt's observed start — used to credit
    #: elapsed time against its forecast rather than restarting the clock.
    started_at: str = ""


def _unit_status(records: Sequence[UsageRecord], spec: WorkUnitSpec) -> _UnitStatus:
    """What a unit's own usage records say about where it is right now.

    Only this unit's records are read; dependency and resource state are the
    caller's job. A unit with no records at all is reported as queued (not
    active, not done, not stuck) — ready to run once its dependencies clear.
    """
    if not records:
        return _UnitStatus(
            done=False, active=False, stuck=None, current_stage=_DEFAULT_DURATION_STAGE
        )

    done = any(
        record.stage is _DONE_STAGE
        and record.actual is not None
        and record.actual.result is UsageResult.COMPLETED
        for record in records
    )
    if done:
        return _UnitStatus(done=True, active=False, stuck=None, current_stage=_DONE_STAGE)

    # Compare parsed instants, not raw strings: differing but equivalent RFC
    # 3339 offsets (``+02:00`` vs ``Z``) do not sort the same way as text.
    latest = max(
        records, key=lambda record: (_instant(record.recorded_at, "recordedAt"), record.attempt)
    )
    if latest.actual is None or latest.actual.result is UsageResult.UNKNOWN:
        # No outcome recorded yet: a run is in flight, whether or not a
        # finish timestamp happens to be populated.
        started_at = latest.actual.started_at if latest.actual is not None else ""
        return _UnitStatus(
            done=False, active=True, stuck=None, current_stage=latest.stage, started_at=started_at
        )

    if latest.actual.result is UsageResult.COMPLETED:
        # This attempt finished cleanly but has not reached the done stage
        # yet; the unit is between stages, which this module treats the same
        # as queued for its next stage.
        return _UnitStatus(
            done=False, active=False, stuck=None, current_stage=_DEFAULT_DURATION_STAGE
        )

    attempts_for_stage = max(
        (record.attempt for record in records if record.stage == latest.stage), default=1
    )
    # One initial attempt plus max_retries retries, matching MissionLedger's
    # own "attempts >= max_retries + 1" exhaustion rule in missions.py.
    exhausted = attempts_for_stage > spec.max_retries
    if not exhausted:
        # A retry is still available; this reads as active, not blocked —
        # the repair/retry loop owns dispatching the next attempt.
        return _UnitStatus(done=False, active=True, stuck=None, current_stage=latest.stage)
    reason = (
        BlockingReason.TEST_OR_VERIFICATION_FAILED
        if latest.stage in _VERIFICATION_STAGES
        else BlockingReason.UNKNOWN
    )
    return _UnitStatus(done=False, active=False, stuck=reason, current_stage=latest.stage)


# -- work graph loading (DAG shape only; no path/policy re-validation) --------


def _topological_order(units: Mapping[str, WorkUnitSpec]) -> tuple[str, ...]:
    remaining = {unit_id: set(spec.depends_on) for unit_id, spec in units.items()}
    order: list[str] = []
    while remaining:
        ready = sorted(unit_id for unit_id, deps in remaining.items() if not deps)
        if not ready:
            cycle = ", ".join(sorted(remaining))
            raise ScheduleForecastError(f"work graph contains a dependency cycle among: {cycle}")
        for unit_id in ready:
            order.append(unit_id)
            del remaining[unit_id]
        for deps in remaining.values():
            deps.difference_update(ready)
    return tuple(order)


def _ancestor_sets(
    units: Mapping[str, WorkUnitSpec], order: Sequence[str]
) -> dict[str, frozenset[str]]:
    ancestors: dict[str, frozenset[str]] = {}
    for unit_id in order:
        merged: set[str] = set()
        for dependency in units[unit_id].depends_on:
            merged.add(dependency)
            merged.update(ancestors[dependency])
        ancestors[unit_id] = frozenset(merged)
    return ancestors


def _build_graph(raw_units: Mapping[str, Any], label: str) -> WorkGraph:
    units: dict[str, WorkUnitSpec] = {}
    for unit_id, raw in raw_units.items():
        _require(isinstance(raw, Mapping), f"{label}: unit {unit_id} must be an object")
        units[unit_id] = WorkUnitSpec(
            unit_id=unit_id,
            mission_id=str(raw.get("missionId", "")),
            mission_version=str(raw.get("missionVersion", "")),
            output_artifact=str(raw.get("outputArtifact", "")),
            depends_on=tuple(raw.get("dependsOn", ())),
            owned_paths=tuple(raw.get("ownedPaths", ())),
            read_paths=tuple(raw.get("readPaths", ("**",))),
            write_paths=tuple(raw.get("writePaths", ())),
            shared_resources=tuple(raw.get("sharedResources", ())),
            verification_gates=tuple(raw.get("verificationGates", ("deterministic-ci",))),
            concurrency_group=str(raw.get("concurrencyGroup", "")),
            composition=str(raw.get("composition", DEFAULT_COMPOSITION)),
            provider=str(raw.get("provider", "")),
            timeout_seconds=int(raw.get("timeoutSeconds", 3600)),
            max_retries=int(raw.get("maxRetries", 1)),
            max_tokens=int(raw.get("maxTokens", 500_000)),
        )
    missing = sorted(
        dependency
        for spec in units.values()
        for dependency in spec.depends_on
        if dependency not in units
    )
    if missing:
        raise ScheduleForecastError(f"{label}: missing dependencies: {', '.join(missing)}")
    order = _topological_order(units)
    ancestors = _ancestor_sets(units, order)
    return WorkGraph(units=units, order=order, ancestors=ancestors)


def load_work_graph(document: Any) -> WorkGraph:
    """Build one project's :class:`WorkGraph` from a plain JSON document.

    This module only needs the DAG's shape — dependencies, concurrency
    groups, and composition/write-scope declarations — not the path-policy
    and ownership checks ``work_graph.validate_graph`` enforces at dispatch
    time, which belong to the pipeline that actually schedules writes. A
    cycle or a missing dependency is still refused here, since neither a
    critical path nor an ETA means anything over a graph that cannot run.
    """
    _require(isinstance(document, Mapping), "work graph document must be an object")
    version = document.get("schemaVersion", 1)
    _require(version == 1, f"unsupported work graph schemaVersion: {version!r}")
    raw_units = document.get("units")
    _require(isinstance(raw_units, Mapping) and bool(raw_units), "work graph: units is required")
    return _build_graph(raw_units, "work graph")


def load_work_graph_document(document: Any) -> dict[str, WorkGraph]:
    """Build every project's :class:`WorkGraph` from one forecast input file.

    A work graph is inherently single-project (one parent work request's
    DAG), so the file this module's CLI reads bundles one per project:
    ``{"schemaVersion": 1, "projects": {"<projectId>": {"units": {...}}}}``.
    """
    _require(isinstance(document, Mapping), "work graph document must be an object")
    version = document.get("schemaVersion", 1)
    _require(version == 1, f"unsupported work graph schemaVersion: {version!r}")
    raw_projects = document.get("projects")
    _require(
        isinstance(raw_projects, Mapping) and bool(raw_projects),
        "work graph document: projects is required",
    )
    graphs: dict[str, WorkGraph] = {}
    for project_id, raw_project in raw_projects.items():
        _require(
            isinstance(raw_project, Mapping),
            f"work graph document: project {project_id} must be an object",
        )
        raw_units = raw_project.get("units")
        label = f"work graph document: project {project_id}"
        _require(isinstance(raw_units, Mapping) and bool(raw_units), f"{label}: units is required")
        graphs[project_id] = _build_graph(raw_units, label)
    return graphs


# -- blocking-reason derivation -------------------------------------------------


def _blocking_reason(
    unit_id: str,
    graph: WorkGraph,
    done: Mapping[str, bool],
    stuck: Mapping[str, BlockingReason],
    declared_blocks: Mapping[str, BlockingReason],
    held_resources: Mapping[str, str],
) -> BlockingReason | None:
    """``None`` means runnable. Order matters: each rule is evidence this
    module actually has; later rules never override an earlier, firmer one.

    An explicit, shared composition strategy is deliberately *not* treated
    as mutual exclusion: ``GraphExecutor.start`` leases only ``ownedPaths``
    and ``sharedResources``, never ``writePaths``, so two units a validated
    graph lets share a composition strategy over overlapping write scope are
    free to run concurrently and compose afterward. Only an actual held
    lease — the same resources the real executor leases — blocks a unit
    here; this module invents no serialization a composition strategy does
    not itself impose. A caller with real fan-in-conflict evidence declares
    ``conflicting_change_scope`` directly.
    """
    if done.get(unit_id, False):
        return None
    for ancestor in graph.ancestors[unit_id]:
        if not done.get(ancestor, False):
            return BlockingReason.DEPENDENCY_BLOCKED
    if unit_id in stuck:
        return stuck[unit_id]
    declared = declared_blocks.get(unit_id)
    if declared is not None:
        return declared
    spec = graph.get(unit_id)
    for resource in (*spec.owned_paths, *spec.shared_resources):
        holder = held_resources.get(resource, "")
        if holder and holder != unit_id:
            return BlockingReason.EXCLUSIVE_RESOURCE_LOCKED
    return None


# -- forward pass: earliest start, critical path -------------------------------


def _forward_pass(
    graph: WorkGraph,
    durations: Mapping[str, DurationForecast],
    done: Mapping[str, bool],
    elapsed: Mapping[str, float],
    quantile_name: str,
) -> tuple[dict[str, float], dict[str, float], dict[str, str | None]]:
    """One weighting's start/finish offsets (seconds from ``now``) and, for
    each unit, which predecessor put it on the longest path to get there.

    ``elapsed`` carries, for a unit already observed running, how much of
    its current stage has already passed: its remaining duration is credited
    for that time rather than restarting the forecast's clock at ``now`` on
    every refresh, and its start offset is its real (possibly negative, i.e.
    already-past) start rather than a dependency-derived one — the real
    world already dispatched it, independent of what the forward pass would
    otherwise infer from its dependencies.
    """
    start_offset: dict[str, float] = {}
    finish: dict[str, float] = {}
    critical_predecessor: dict[str, str | None] = {}
    for unit_id in graph.order:
        spec = graph.get(unit_id)
        duration_value = getattr(durations[unit_id], quantile_name)
        is_done = done.get(unit_id, False)
        if unit_id in elapsed:
            # Already running: its start already happened — independent of
            # what the forward pass would otherwise infer from dependencies,
            # since the real world already dispatched it. Its finish credits
            # that elapsed time against the *full* duration rather than
            # restarting the clock at "now" on every refresh.
            start = -elapsed[unit_id]
            predecessor = None
            finish_value = 0.0 if is_done else max(0.0, duration_value - elapsed[unit_id])
        elif not spec.depends_on:
            start = 0.0
            predecessor = None
            finish_value = 0.0 if is_done else duration_value
        else:
            # Ties break on the smaller unit id, so the reported path is
            # reproducible rather than depending on dict/set iteration order.
            best_finish = max(finish[dependency] for dependency in spec.depends_on)
            predecessor = min(
                dependency for dependency in spec.depends_on if finish[dependency] == best_finish
            )
            start = best_finish
            finish_value = start if is_done else start + duration_value
        start_offset[unit_id] = start
        finish[unit_id] = finish_value
        critical_predecessor[unit_id] = predecessor
        critical_predecessor[unit_id] = predecessor
    return start_offset, finish, critical_predecessor


def _critical_path(
    finish: Mapping[str, float], predecessor: Mapping[str, str | None]
) -> tuple[str, ...]:
    if not finish:
        return ()
    best_finish = max(finish.values())
    best = min(unit_id for unit_id, value in finish.items() if value == best_finish)
    path = [best]
    cursor: str | None = best
    while True:
        cursor = predecessor.get(cursor)
        if cursor is None:
            break
        path.append(cursor)
    return tuple(reversed(path))


# -- the report ------------------------------------------------------------------


def _forecast_project(
    project_id: str,
    graph: WorkGraph,
    records: Sequence[UsageRecord],
    *,
    start: datetime,
    min_sample_size: int,
    declared_blocks: Mapping[str, BlockingReason],
    held_resources: Mapping[str, str],
) -> dict[str, Any]:
    """One project's unit forecasts and project-level rollup.

    Every input here is already scoped to this one project: the graph is a
    single parent work request's DAG, and ``records`` has been filtered to
    this project's usage history. Nothing in this function reads another
    project's data, which is what keeps projects isolated from each other.
    """

    def at(offset_seconds: float) -> str:
        return (start + timedelta(seconds=offset_seconds)).isoformat()

    by_unit: dict[str, list[UsageRecord]] = {unit_id: [] for unit_id in graph.units}
    for record in records:
        if record.work_unit_id in by_unit:
            by_unit[record.work_unit_id].append(record)

    statuses = {
        unit_id: _unit_status(unit_records, graph.get(unit_id))
        for unit_id, unit_records in by_unit.items()
    }
    done = {unit_id: status.done for unit_id, status in statuses.items()}
    stuck = {
        unit_id: status.stuck for unit_id, status in statuses.items() if status.stuck is not None
    }
    elapsed: dict[str, float] = {}
    for unit_id, status in statuses.items():
        if status.active and status.started_at:
            elapsed[unit_id] = max(
                0.0, (start - _instant(status.started_at, "startedAt")).total_seconds()
            )

    pooled = _finished_durations(records)
    durations: dict[str, DurationForecast] = {}
    for unit_id, status in statuses.items():
        class_samples = _finished_durations(
            [record for record in by_unit[unit_id] if record.stage is status.current_stage]
        )
        stage_pool = _finished_durations(
            [record for record in records if record.stage is status.current_stage]
        )
        durations[unit_id] = _duration_forecast(
            [
                (class_samples, DurationBasis.HISTORY),
                (stage_pool, DurationBasis.POOLED_HISTORY),
                (pooled, DurationBasis.POOLED_HISTORY),
            ],
            float(graph.get(unit_id).timeout_seconds),
            min_sample_size=min_sample_size,
        )

    blocking: dict[str, BlockingReason | None] = {
        unit_id: _blocking_reason(unit_id, graph, done, stuck, declared_blocks, held_resources)
        for unit_id in graph.units
    }

    # A unit resolves at a time this module can compute only when neither it
    # nor anything upstream of it is blocked for a reason with an unknown
    # resolution time: dependency blocking alone still has a computable
    # finish (as soon as the ancestor chain finishes), but it propagates any
    # indeterminacy *that chain* carries, so a dependent never reports a
    # concrete ETA built on an ancestor's unknown release time.
    indeterminate: dict[str, bool] = {}
    for unit_id in graph.order:
        reason = blocking[unit_id]
        own_indeterminate = reason is not None and reason is not BlockingReason.DEPENDENCY_BLOCKED
        indeterminate[unit_id] = own_indeterminate or any(
            indeterminate[dependency] for dependency in graph.get(unit_id).depends_on
        )

    finish_by_quantile = {
        name: _forward_pass(graph, durations, done, elapsed, name) for name in ("p50", "p80", "p95")
    }

    units_out: dict[str, Any] = {}
    for unit_id in graph.order:
        start50 = finish_by_quantile["p50"][0]
        duration = durations[unit_id]
        reason = blocking[unit_id]
        computable = not indeterminate[unit_id]
        spec = graph.get(unit_id)
        units_out[unit_id] = {
            "unitId": unit_id,
            "projectId": project_id,
            "done": done[unit_id],
            "earliestStart": at(start50[unit_id]) if computable else None,
            "expectedDuration": duration.as_dict(),
            "etaRange": (
                {name: at(finish_by_quantile[name][1][unit_id]) for name in ("p50", "p80", "p95")}
                if computable
                else {"p50": None, "p80": None, "p95": None}
            ),
            "onCriticalPath": False,  # filled in below, once the critical path is known
            "blockingReason": reason.value if reason is not None else None,
            "parallelismGroup": spec.concurrency_group or None,
        }

    critical_paths: dict[str, tuple[str, ...]] = {}
    for name in ("p50", "p80", "p95"):
        _, finish, predecessor = finish_by_quantile[name]
        critical_paths[name] = _critical_path(finish, predecessor)
    for unit_id in critical_paths["p50"]:
        units_out[unit_id]["onCriticalPath"] = True

    project_eta = {}
    for name in ("p50", "p80", "p95"):
        path = critical_paths[name]
        # The project's own ETA is only as firm as the unit it is longest
        # through: if that unit's resolution time is unknown, so is the
        # project's, rather than quietly assuming an indeterminate blocker
        # clears immediately.
        project_eta[name] = (
            at(finish_by_quantile[name][1][path[-1]])
            if path and not indeterminate[path[-1]]
            else None
        )
    blockers_by_reason: dict[str, list[str]] = {}
    active_count = 0
    queued_count = 0
    for unit_id in graph.units:
        reason = blocking[unit_id]
        if done[unit_id]:
            continue
        if reason is None:
            if statuses[unit_id].active:
                active_count += 1
            else:
                queued_count += 1
            continue
        blockers_by_reason.setdefault(reason.value, []).append(unit_id)

    project_out = {
        "units": units_out,
        "etaRange": project_eta,
        "criticalPath": {name: list(path) for name, path in critical_paths.items()},
        "activeCount": active_count,
        "queuedCount": queued_count,
        "blockersByReason": {
            reason: sorted(unit_ids) for reason, unit_ids in sorted(blockers_by_reason.items())
        },
    }
    return project_out


def build_schedule_forecast(
    graphs: Mapping[str, WorkGraph],
    usage_records: Sequence[UsageRecord],
    *,
    now: str,
    project_ids: Sequence[str] | None = None,
    min_sample_size: int = DEFAULT_MIN_SAMPLE_SIZE,
    declared_blocks: Mapping[str, Mapping[str, BlockingReason]] | None = None,
    held_resources: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    """Forecast ETA, critical path, and blocking reason for every unit.

    ``graphs`` maps a project id to that project's :class:`WorkGraph` — a
    work graph is one parent work request's DAG, so a multi-project forecast
    is one graph per project. ``now`` is supplied by the caller rather than
    read from the clock, so two calls over the same inputs and the same
    ``now`` always produce the same document. ``declared_blocks`` and
    ``held_resources`` are per-project maps of the evidence this module
    cannot derive from the graph and usage ledger alone — see the module
    docstring; neither is required, and what is not declared reads as
    runnable unless this module's own rules say otherwise. Each project is
    forecast from only its own graph and usage records, so one project's
    history never influences another's quantiles or critical path.
    """
    start = _instant(now, "schedule forecast now")
    scoped_project_ids = tuple(project_ids) if project_ids is not None else tuple(graphs)
    unknown_requested = sorted(set(scoped_project_ids) - set(graphs))
    _require(
        not unknown_requested,
        f"requested project(s) have no work graph: {', '.join(unknown_requested)}",
    )
    declared_blocks = declared_blocks or {}
    held_resources = held_resources or {}

    by_project_out: dict[str, Any] = {}
    for project_id in sorted(scoped_project_ids):
        graph = graphs[project_id]
        project_declared_blocks = declared_blocks.get(project_id, {})
        unknown_declared = sorted(set(project_declared_blocks) - set(graph.units))
        _require(
            not unknown_declared,
            f"project {project_id}: declared_blocks references unknown unit(s): "
            + ", ".join(unknown_declared),
        )
        project_records = [record for record in usage_records if record.project_id == project_id]
        by_project_out[project_id] = _forecast_project(
            project_id,
            graph,
            project_records,
            start=start,
            min_sample_size=min_sample_size,
            declared_blocks=project_declared_blocks,
            held_resources=held_resources.get(project_id, {}),
        )

    return {
        "schemaVersion": SCHEDULE_FORECAST_SCHEMA_VERSION,
        "now": now,
        "byProject": by_project_out,
        "limitations": [
            "a unit's expectedDuration forecasts only its current (or, if not yet "
            "started, implementation) stage as a proxy for its remaining work, not a "
            "multi-stage composition",
            "below the minimum sample size, a basis is skipped entirely rather than "
            "used to produce a falsely narrow range",
            "policy, human-approval, provider/subscription/runner-capacity, budget, "
            "quality-floor, environment, and security/compliance blocking reasons are "
            "reported only when the caller declares them; this module observes only "
            "dependency, resource, write-scope, and verification/review signals "
            "directly",
            "this module never dispatches, reorders, or changes concurrency, leases, "
            "routing, or policy",
        ],
    }
