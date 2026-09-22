"""Parallelism, queueing, runner and subscription-capacity measurement.

The usage ledger records when each run attempt was queued, started and
finished, and on which worker, model and provider. This module reads that
history as a factory instrument: how much work actually ran at once, where
time was lost waiting, which runners and models were saturated, and how much
of the busy time was useful rather than rework.

Three rules carry over from the ledger it reads:

- **Unknown is never a percentage.** A utilization figure appears only when
  the capacity it divides by was declared or observed. Plan capacity that a
  provider does not expose is reported as unknown beside the usage proxies
  that *are* observable, never as an invented fraction.
- **Every number shows its working.** Each metric carries the seconds and
  counts it was derived from, so a reader can recompute it by hand.
- **Findings explain, they never act.** Bottlenecks and recommendations are
  evidence with an explanation attached. Nothing here changes routing,
  concurrency, policy or spend.

The window is supplied by the caller rather than inferred from the records,
so a quiet period is visible as idle capacity instead of vanishing from the
denominator.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from .usage_ledger import UsageRecord, UsageResult, parse_timestamp

__all__ = [
    "CAPACITY_SCHEMA_VERSION",
    "BottleneckKind",
    "CapacityError",
    "CapacityInputs",
    "ObservationWindow",
    "PlanCapacityObservation",
    "RunnerPool",
    "WaitCause",
    "build_capacity_report",
    "concurrency_timeline",
    "load_capacity_inputs",
]

CAPACITY_SCHEMA_VERSION = 1

#: A wait is called resource-blocked only when the pool was saturated for at
#: least this share of it; otherwise the cause stays unclassified.
_RESOURCE_WAIT_SHARE = 0.5

#: How many waits must share one blocker before fan-in is worth reporting.
_FAN_IN_THRESHOLD = 3


class CapacityError(ValueError):
    """Raised when a capacity observation is internally inconsistent."""


class WaitCause(StrEnum):
    """Why a run attempt sat between being queued and starting."""

    DEPENDENCY = "dependency"
    RESOURCE = "resource"
    UNCLASSIFIED = "unclassified"


class BottleneckKind(StrEnum):
    RUNNER_SHORTAGE = "runner-shortage"
    SERIALIZED_CONCURRENCY_GROUP = "serialized-concurrency-group"
    MODEL_CAPACITY_THROTTLING = "model-capacity-throttling"
    DEPENDENCY_FAN_IN = "dependency-fan-in"
    REPEATED_REVIEW_CYCLES = "repeated-review-cycles"
    HANDOFF_DELAY = "handoff-delay"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CapacityError(message)


def _instant(value: str, label: str) -> datetime:
    """Parse a timestamp, reporting a bad one in this module's own error type.

    The parser is shared with the usage ledger so instants are compared the
    same way everywhere; only the error class is translated, so a caller of
    this module catches one exception type.
    """
    try:
        return parse_timestamp(value, label)
    except ValueError as error:
        raise CapacityError(str(error)) from error


def _positive_int(value: Any, label: str) -> int:
    _require(
        isinstance(value, int) and not isinstance(value, bool) and value > 0,
        f"{label} must be an integer greater than zero",
    )
    return int(value)


def _ratio(numerator: float, denominator: float) -> float | None:
    """A ratio exists only when something real sits in the denominator."""
    return round(numerator / denominator, 4) if denominator > 0 else None


# -- window, pools and plan capacity ------------------------------------------


@dataclass(frozen=True)
class ObservationWindow:
    """The interval every utilization figure is measured against."""

    start: str
    end: str

    def __post_init__(self) -> None:
        _require(
            self.started_at < self.ended_at,
            f"observation window must end after it starts ({self.start} .. {self.end})",
        )

    @property
    def started_at(self) -> datetime:
        return _instant(self.start, "observation window start")

    @property
    def ended_at(self) -> datetime:
        return _instant(self.end, "observation window end")

    @property
    def seconds(self) -> int:
        return int((self.ended_at - self.started_at).total_seconds())

    def as_dict(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, "seconds": self.seconds}


@dataclass(frozen=True)
class RunnerPool:
    """A declared set of runner slots, hosted or self-hosted.

    ``slots`` is what makes utilization computable: without it a pool's busy
    seconds have no denominator and the pool is reported as unknown.
    """

    pool_id: str
    slots: int
    workers: tuple[str, ...] = ()
    kind: str = "unspecified"

    def __post_init__(self) -> None:
        _require(bool(self.pool_id.strip()), "runner pool: poolId is required")
        _positive_int(self.slots, f"runner pool {self.pool_id}: slots")
        _require(
            len(set(self.workers)) == len(self.workers),
            f"runner pool {self.pool_id}: duplicate worker",
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "poolId": self.pool_id,
            "slots": self.slots,
            "kind": self.kind,
            "workers": list(self.workers),
        }


@dataclass(frozen=True)
class PlanCapacityObservation:
    """A subscription plan's capacity, as the provider actually reported it."""

    provider: str
    plan: str
    capacity_unit: str
    observed_at: str
    units_total: float | None = None
    units_used: float | None = None

    def __post_init__(self) -> None:
        _require(bool(self.provider.strip()), "plan capacity: provider is required")
        _require(bool(self.capacity_unit.strip()), "plan capacity: capacityUnit is required")
        _instant(self.observed_at, "plan capacity observedAt")
        for name in ("units_total", "units_used"):
            value = getattr(self, name)
            _require(
                value is None or (isinstance(value, int | float) and value >= 0),
                f"plan capacity {name} must be a number >= 0 or null",
            )
        if self.units_total is not None and self.units_used is not None:
            _require(
                self.units_used <= self.units_total,
                f"plan capacity {self.provider}: used exceeds the declared total",
            )

    @property
    def used_fraction(self) -> float | None:
        if self.units_total is None or self.units_used is None or self.units_total == 0:
            return None
        return round(self.units_used / self.units_total, 4)

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "plan": self.plan,
            "capacityUnit": self.capacity_unit,
            "observedAt": self.observed_at,
            "unitsTotal": self.units_total,
            "unitsUsed": self.units_used,
            "unitsRemaining": (
                None
                if self.units_total is None or self.units_used is None
                else round(self.units_total - self.units_used, 6)
            ),
            "usedFraction": self.used_fraction,
            "status": "observed" if self.used_fraction is not None else "partial",
        }


@dataclass(frozen=True)
class CapacityInputs:
    """Evidence the records themselves cannot carry.

    A usage record says a run waited; it does not say what it waited for. These
    optional declarations are what turn a wait into a classified cause and a
    busy worker into a utilization figure. Every one of them is optional, and
    what is not declared is reported as unknown rather than guessed.
    """

    pools: tuple[RunnerPool, ...] = ()
    plan_capacity: tuple[PlanCapacityObservation, ...] = ()
    #: usage id -> the reference it was waiting on (from the ledger's
    #: ``dependency-wait`` stage, whose events name the blocking unit).
    dependency_blocked: Mapping[str, str] | None = None
    #: usage id -> the concurrency group that serialized it.
    concurrency_groups: Mapping[str, str] | None = None
    #: provider -> the concurrent-run ceiling its plan or contract imposes.
    provider_limits: Mapping[str, int] | None = None

    def __post_init__(self) -> None:
        seen_pools: set[str] = set()
        owners: dict[str, str] = {}
        for pool in self.pools:
            _require(pool.pool_id not in seen_pools, f"duplicate runner pool {pool.pool_id}")
            seen_pools.add(pool.pool_id)
            for worker in pool.workers:
                _require(
                    worker not in owners,
                    f"worker {worker} is declared by both {owners.get(worker)} and {pool.pool_id}",
                )
                owners[worker] = pool.pool_id
        for provider, limit in (self.provider_limits or {}).items():
            _positive_int(limit, f"provider limit for {provider}")

    def pool_for(self, worker: str) -> RunnerPool | None:
        for pool in self.pools:
            if worker in pool.workers:
                return pool
        return None


# -- intervals and timelines ---------------------------------------------------


@dataclass(frozen=True)
class _Interval:
    start: datetime
    end: datetime
    record: UsageRecord

    @property
    def seconds(self) -> int:
        return int((self.end - self.start).total_seconds())


@dataclass(frozen=True)
class _Segment:
    start: datetime
    end: datetime
    active: int

    @property
    def seconds(self) -> int:
        return int((self.end - self.start).total_seconds())

    def as_dict(self) -> dict[str, Any]:
        return {
            "from": self.start.isoformat(),
            "to": self.end.isoformat(),
            "active": self.active,
            "seconds": self.seconds,
        }


def concurrency_timeline(
    intervals: Sequence[tuple[datetime, datetime]],
    window: ObservationWindow,
) -> tuple[_Segment, ...]:
    """Sweep overlapping intervals into contiguous constant-concurrency segments.

    Segments cover the whole window, idle stretches included, so a reader can
    add the seconds back up and get the window. An interval that ends exactly
    where another begins never counts as two concurrent runs.
    """
    clipped = [
        (max(start, window.started_at), min(end, window.ended_at))
        for start, end in intervals
        if end > window.started_at and start < window.ended_at
    ]
    events: list[tuple[datetime, int]] = []
    for start, end in clipped:
        if end > start:
            events.append((start, 1))
            events.append((end, -1))
    # -1 sorts before +1 at the same instant, so a handover is not a peak.
    events.sort(key=lambda item: (item[0], item[1]))

    segments: list[_Segment] = []
    active = 0
    cursor = window.started_at
    for moment, delta in events:
        if moment > cursor:
            segments.append(_Segment(cursor, moment, active))
            cursor = moment
        active += delta
    if cursor < window.ended_at:
        segments.append(_Segment(cursor, window.ended_at, active))
    return tuple(segments)


class _Track:
    """One set of intervals, with the questions a bottleneck detector asks."""

    def __init__(self, intervals: Sequence[_Interval], window: ObservationWindow) -> None:
        self._window = window
        self._intervals = tuple(intervals)
        self._segments = concurrency_timeline(
            [(item.start, item.end) for item in intervals], window
        )

    @property
    def segments(self) -> tuple[_Segment, ...]:
        return self._segments

    @property
    def peak(self) -> int:
        return max((segment.active for segment in self._segments), default=0)

    @property
    def busy_seconds(self) -> int:
        return sum(segment.seconds * segment.active for segment in self._segments)

    @property
    def occupied_seconds(self) -> int:
        """Seconds with at least one run active, however many were running."""
        return sum(segment.seconds for segment in self._segments if segment.active > 0)

    def seconds_at_or_above(
        self, level: int, start: datetime | None = None, end: datetime | None = None
    ) -> int:
        lower = start or self._window.started_at
        upper = end or self._window.ended_at
        total = 0
        for segment in self._segments:
            if segment.active < level:
                continue
            overlap_start = max(segment.start, lower)
            overlap_end = min(segment.end, upper)
            if overlap_end > overlap_start:
                total += int((overlap_end - overlap_start).total_seconds())
        return total

    def as_dict(self, *, with_segments: bool = False) -> dict[str, Any]:
        window_seconds = self._window.seconds
        document: dict[str, Any] = {
            "peak": self.peak,
            "mean": _ratio(self.busy_seconds, window_seconds),
            "busySeconds": self.busy_seconds,
            "occupiedSeconds": self.occupied_seconds,
            "idleSeconds": window_seconds - self.occupied_seconds,
            "windowSeconds": window_seconds,
            "runs": len(self._intervals),
        }
        if with_segments:
            document["segments"] = [
                segment.as_dict() for segment in self._segments if segment.active > 0
            ]
        return document


# -- reading the records -------------------------------------------------------


def _times(record: UsageRecord) -> tuple[datetime | None, datetime | None, datetime | None]:
    """The queued, started and finished instants a record actually observed."""
    actual = record.actual
    if actual is None:
        return (None, None, None)
    label = f"usage record {record.usage_id}"
    queued = _instant(actual.queued_at, f"{label} queuedAt") if actual.queued_at else None
    started = _instant(actual.started_at, f"{label} startedAt") if actual.started_at else None
    finished = _instant(actual.finished_at, f"{label} finishedAt") if actual.finished_at else None
    return (queued, started, finished)


def _clip(
    start: datetime, end: datetime, window: ObservationWindow
) -> tuple[datetime, datetime] | None:
    if end <= window.started_at or start >= window.ended_at:
        return None
    return (max(start, window.started_at), min(end, window.ended_at))


@dataclass(frozen=True)
class _Reading:
    """Everything one pass over the records produced."""

    busy: tuple[_Interval, ...]
    waits: tuple[_Interval, ...]
    coverage: dict[str, int]
    in_window: tuple[UsageRecord, ...]


def _read(records: Sequence[UsageRecord], window: ObservationWindow) -> _Reading:
    busy: list[_Interval] = []
    waits: list[_Interval] = []
    in_window: list[UsageRecord] = []
    counts = {
        "records": len(records),
        "runsTimed": 0,
        "runsUntimed": 0,
        "runsOutsideWindow": 0,
        "runsClipped": 0,
        "waitsTimed": 0,
        "waitsUntimed": 0,
        "waitsOutsideWindow": 0,
    }
    for record in records:
        queued, started, finished = _times(record)
        if started is not None and finished is not None:
            span = _clip(started, finished, window)
            if span is None:
                counts["runsOutsideWindow"] += 1
            else:
                if span != (started, finished):
                    counts["runsClipped"] += 1
                counts["runsTimed"] += 1
                busy.append(_Interval(span[0], span[1], record))
                in_window.append(record)
        else:
            counts["runsUntimed"] += 1
        if queued is not None and started is not None:
            # A run that started the instant it was queued waited zero seconds,
            # which is an observation, not a missing one.
            if started >= window.started_at and queued <= window.ended_at:
                counts["waitsTimed"] += 1
                lower = max(queued, window.started_at)
                upper = min(started, window.ended_at)
                if upper > lower:
                    waits.append(_Interval(lower, upper, record))
            else:
                counts["waitsOutsideWindow"] += 1
        else:
            counts["waitsUntimed"] += 1
    return _Reading(tuple(busy), tuple(waits), counts, tuple(in_window))


def _grouped(intervals: Sequence[_Interval], key: Any) -> dict[str, list[_Interval]]:
    buckets: dict[str, list[_Interval]] = {}
    for interval in intervals:
        buckets.setdefault(key(interval.record), []).append(interval)
    return buckets


# -- wait decomposition --------------------------------------------------------


def _pool_tracks(
    busy: Sequence[_Interval], inputs: CapacityInputs, window: ObservationWindow
) -> dict[str, _Track]:
    tracks: dict[str, _Track] = {}
    for pool in inputs.pools:
        owned = [item for item in busy if item.record.actor.worker in pool.workers]
        tracks[pool.pool_id] = _Track(owned, window)
    return tracks


def _classify_wait(
    wait: _Interval,
    inputs: CapacityInputs,
    pool_tracks: Mapping[str, _Track],
) -> tuple[WaitCause, str]:
    """Name the cause of one wait, or admit it is not known.

    Dependency evidence is explicit, because a record cannot know what blocked
    it. Resource blocking is derived: if the pool that eventually ran the work
    was full for most of the wait, the wait was for a slot. Anything else stays
    unclassified rather than being attributed to the nearest plausible cause.
    """
    blocked_by = (inputs.dependency_blocked or {}).get(wait.record.usage_id)
    if blocked_by:
        return (WaitCause.DEPENDENCY, blocked_by)
    pool = inputs.pool_for(wait.record.actor.worker)
    if pool is not None and wait.seconds > 0:
        track = pool_tracks.get(pool.pool_id)
        if track is not None:
            saturated = track.seconds_at_or_above(pool.slots, wait.start, wait.end)
            if saturated >= wait.seconds * _RESOURCE_WAIT_SHARE:
                return (WaitCause.RESOURCE, pool.pool_id)
    return (WaitCause.UNCLASSIFIED, "")


def _handoff_gaps(records: Sequence[UsageRecord]) -> dict[str, int]:
    """Idle time between one stage finishing and the next being queued."""
    by_unit: dict[str, list[tuple[datetime, datetime]]] = {}
    for record in records:
        queued, _, finished = _times(record)
        if queued is None or finished is None:
            continue
        key = f"{record.project_id}:{record.work_unit_id}"
        by_unit.setdefault(key, []).append((queued, finished))
    gaps: dict[str, int] = {}
    for key, pairs in by_unit.items():
        ordered = sorted(pairs, key=lambda item: item[0])
        total = 0
        previous_finish: datetime | None = None
        for queued, finished in ordered:
            if previous_finish is not None and queued > previous_finish:
                total += int((queued - previous_finish).total_seconds())
            previous_finish = (
                finished if previous_finish is None else max(previous_finish, finished)
            )
        if total > 0:
            gaps[key] = total
    return gaps


def _wait_report(
    waits: Sequence[_Interval],
    inputs: CapacityInputs,
    pool_tracks: Mapping[str, _Track],
    records: Sequence[UsageRecord],
    coverage: Mapping[str, int],
) -> tuple[dict[str, Any], dict[str, list[_Interval]]]:
    by_cause: dict[str, dict[str, Any]] = {
        cause.value: {"seconds": 0, "waits": 0, "blockers": {}} for cause in WaitCause
    }
    classified: dict[str, list[_Interval]] = {cause.value: [] for cause in WaitCause}
    by_stage: dict[str, int] = {}
    for wait in waits:
        cause, blocker = _classify_wait(wait, inputs, pool_tracks)
        bucket = by_cause[cause.value]
        bucket["seconds"] += wait.seconds
        bucket["waits"] += 1
        if blocker:
            bucket["blockers"][blocker] = bucket["blockers"].get(blocker, 0) + 1
        classified[cause.value].append(wait)
        stage = wait.record.stage.value
        by_stage[stage] = by_stage.get(stage, 0) + wait.seconds
    gaps = _handoff_gaps(records)
    for bucket in by_cause.values():
        bucket["blockers"] = dict(sorted(bucket["blockers"].items()))
    return (
        {
            "totalSeconds": sum(wait.seconds for wait in waits),
            "waitsTimed": coverage["waitsTimed"],
            "waitsUntimed": coverage["waitsUntimed"],
            "byCause": by_cause,
            "byStageSeconds": dict(sorted(by_stage.items())),
            "handoff": {
                "totalSeconds": sum(gaps.values()),
                "byWorkUnitSeconds": dict(sorted(gaps.items())),
            },
        },
        classified,
    )


# -- runners, models and plan capacity -----------------------------------------


def _runner_report(
    busy: Sequence[_Interval], inputs: CapacityInputs, window: ObservationWindow
) -> dict[str, Any]:
    by_worker = {
        worker or "unattributed": _Track(items, window).as_dict()
        for worker, items in sorted(_grouped(busy, lambda r: r.actor.worker).items())
    }
    for worker, document in by_worker.items():
        document["utilization"] = (
            None if worker == "unattributed" else _ratio(document["busySeconds"], window.seconds)
        )
        document["pool"] = inputs.pool_for(worker).pool_id if inputs.pool_for(worker) else None

    tracks = _pool_tracks(busy, inputs, window)
    by_pool: dict[str, Any] = {}
    for pool in inputs.pools:
        track = tracks[pool.pool_id]
        available = pool.slots * window.seconds
        _require(
            track.peak <= pool.slots,
            f"runner pool {pool.pool_id}: {track.peak} runs were active at once on "
            f"{pool.slots} declared slot(s); the declared slot count cannot be right",
        )
        document = track.as_dict()
        document.update(
            {
                "slots": pool.slots,
                "kind": pool.kind,
                "workers": list(pool.workers),
                "availableSlotSeconds": available,
                "utilization": _ratio(track.busy_seconds, available),
                "saturatedSeconds": track.seconds_at_or_above(pool.slots),
                "status": "observed",
            }
        )
        by_pool[pool.pool_id] = document

    declared = {worker for pool in inputs.pools for worker in pool.workers}
    undeclared = sorted(
        {item.record.actor.worker for item in busy if item.record.actor.worker} - declared
    )
    return {
        "byWorker": by_worker,
        "byPool": by_pool,
        "undeclaredWorkers": undeclared,
        "undeclaredWorkerUtilization": (
            "unknown" if undeclared else "all observed workers belong to a declared pool"
        ),
    }


def _model_report(busy: Sequence[_Interval], window: ObservationWindow) -> dict[str, Any]:
    def describe(buckets: dict[str, list[_Interval]]) -> dict[str, Any]:
        described: dict[str, Any] = {}
        for name, items in sorted(buckets.items()):
            document = _Track(items, window).as_dict()
            stage_mix: dict[str, int] = {}
            for item in items:
                stage = item.record.stage.value
                stage_mix[stage] = stage_mix.get(stage, 0) + 1
            document["stageMix"] = dict(sorted(stage_mix.items()))
            described[name or "unattributed"] = document
        return described

    return {
        "byModel": describe(_grouped(busy, lambda r: r.actor.model)),
        "byProvider": describe(_grouped(busy, lambda r: r.actor.provider)),
    }


def _plan_capacity_report(
    busy: Sequence[_Interval],
    inputs: CapacityInputs,
    window: ObservationWindow,
) -> dict[str, Any]:
    """Declared plan capacity where a provider reports it, proxies where it does not."""
    observed = {item.provider: item for item in inputs.plan_capacity}
    providers = sorted(
        {item.record.actor.provider for item in busy if item.record.actor.provider} | set(observed)
    )
    report: dict[str, Any] = {}
    for provider in providers:
        items = [item for item in busy if item.record.actor.provider == provider]
        track = _Track(items, window)
        consumed: dict[str, float] = {}
        unknown_units = 0
        for item in items:
            monetary = item.record.actual.monetary if item.record.actual else None
            if monetary is None or not monetary.plan_capacity_unit:
                continue
            if monetary.plan_capacity_units is None:
                unknown_units += 1
                continue
            unit = monetary.plan_capacity_unit
            consumed[unit] = round(consumed.get(unit, 0.0) + monetary.plan_capacity_units, 6)
        proxies = {
            "peakConcurrentRuns": track.peak,
            "busySeconds": track.busy_seconds,
            "runs": len(items),
            "observedCapacityUnits": dict(sorted(consumed.items())),
            "runsWithUnknownCapacityUnits": unknown_units,
        }
        if provider in observed:
            document = observed[provider].as_dict()
            document["usageProxies"] = proxies
            report[provider] = document
        else:
            report[provider] = {
                "provider": provider,
                "status": "unknown",
                "reason": "no plan capacity observation was supplied for this provider",
                "unitsTotal": None,
                "unitsUsed": None,
                "usedFraction": None,
                "usageProxies": proxies,
            }
    return report


# -- useful work ---------------------------------------------------------------


def _useful_work_report(busy: Sequence[_Interval]) -> dict[str, Any]:
    """Busy time is not the same as useful time; both are reported."""
    by_result: dict[str, dict[str, int]] = {}
    first_attempt = {"seconds": 0, "runs": 0}
    retries = {"seconds": 0, "runs": 0}
    useful_seconds = 0
    total_seconds = 0
    for item in busy:
        record = item.record
        result = record.actual.result.value if record.actual else UsageResult.UNKNOWN.value
        bucket = by_result.setdefault(result, {"seconds": 0, "runs": 0})
        bucket["seconds"] += item.seconds
        bucket["runs"] += 1
        target = first_attempt if record.attempt == 1 else retries
        target["seconds"] += item.seconds
        target["runs"] += 1
        total_seconds += item.seconds
        if result == UsageResult.COMPLETED.value and record.attempt == 1:
            useful_seconds += item.seconds
    return {
        "rawBusySeconds": total_seconds,
        "usefulSeconds": useful_seconds,
        "reworkSeconds": total_seconds - useful_seconds,
        "usefulWorkRatio": _ratio(useful_seconds, total_seconds),
        "formula": (
            "busy seconds of runs that completed on their first attempt / all busy "
            "seconds; failed, cancelled, abandoned, inconclusive and retried runs stay "
            "in the denominator"
        ),
        "numeratorSeconds": useful_seconds,
        "denominatorSeconds": total_seconds,
        "byResult": {name: by_result[name] for name in sorted(by_result)},
        "firstAttempt": first_attempt,
        "retries": retries,
    }


# -- bottlenecks and recommendations -------------------------------------------


@dataclass(frozen=True)
class _Finding:
    kind: BottleneckKind
    scope: str
    impact_seconds: int
    explanation: str
    evidence: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "scope": self.scope,
            "impactSeconds": self.impact_seconds,
            "explanation": self.explanation,
            "evidence": dict(sorted(self.evidence.items())),
        }


_ACTIONS: Mapping[BottleneckKind, str] = {
    BottleneckKind.RUNNER_SHORTAGE: (
        "consider adding runner slots to this pool, or check whether the work can be "
        "spread across pools"
    ),
    BottleneckKind.SERIALIZED_CONCURRENCY_GROUP: (
        "review whether this concurrency group still needs to serialize, or whether the "
        "work can be split into independent groups"
    ),
    BottleneckKind.MODEL_CAPACITY_THROTTLING: (
        "review the plan capacity or concurrent-run ceiling for this provider, or route "
        "some task classes to another eligible executor"
    ),
    BottleneckKind.DEPENDENCY_FAN_IN: (
        "review whether this blocking unit can be landed earlier or split, so the work "
        "waiting behind it can start sooner"
    ),
    BottleneckKind.REPEATED_REVIEW_CYCLES: (
        "review what these repair and re-review rounds keep finding, and whether a "
        "deterministic check could catch it before review"
    ),
    BottleneckKind.HANDOFF_DELAY: (
        "review how the next stage is triggered for these work units, since the delay is "
        "between stages rather than inside one"
    ),
}


def _bottlenecks(
    busy: Sequence[_Interval],
    waits: Sequence[_Interval],
    classified: Mapping[str, Sequence[_Interval]],
    inputs: CapacityInputs,
    pool_tracks: Mapping[str, _Track],
    plan_capacity: Mapping[str, Any],
    handoff: Mapping[str, int],
    window: ObservationWindow,
) -> tuple[_Finding, ...]:
    """Name what the numbers show, with the seconds that show it."""
    findings: list[_Finding] = []

    for pool in inputs.pools:
        track = pool_tracks[pool.pool_id]
        saturated = track.seconds_at_or_above(pool.slots)
        if saturated == 0:
            continue
        blocked = sum(
            track.seconds_at_or_above(pool.slots, wait.start, wait.end)
            for wait in waits
            if inputs.pool_for(wait.record.actor.worker) is pool
        )
        if blocked > 0:
            findings.append(
                _Finding(
                    kind=BottleneckKind.RUNNER_SHORTAGE,
                    scope=f"pool:{pool.pool_id}",
                    impact_seconds=blocked,
                    explanation=(
                        f"pool {pool.pool_id} was at all {pool.slots} slots for "
                        f"{saturated}s of the window, and work sat queued for {blocked}s "
                        "of that time"
                    ),
                    evidence={
                        "poolId": pool.pool_id,
                        "slots": pool.slots,
                        "saturatedSeconds": saturated,
                        "queuedWhileSaturatedSeconds": blocked,
                        "windowSeconds": window.seconds,
                    },
                )
            )

    groups = inputs.concurrency_groups or {}
    if groups:
        by_group: dict[str, list[_Interval]] = {}
        for item in busy:
            group = groups.get(item.record.usage_id)
            if group:
                by_group.setdefault(group, []).append(item)
        for group, items in sorted(by_group.items()):
            track = _Track(items, window)
            if track.peak > 1:
                continue
            blocked = sum(
                track.seconds_at_or_above(1, wait.start, wait.end)
                for wait in waits
                if groups.get(wait.record.usage_id) == group
            )
            if blocked > 0:
                findings.append(
                    _Finding(
                        kind=BottleneckKind.SERIALIZED_CONCURRENCY_GROUP,
                        scope=f"group:{group}",
                        impact_seconds=blocked,
                        explanation=(
                            f"concurrency group {group} never ran more than one unit at a "
                            f"time, and its own work waited {blocked}s while it was busy"
                        ),
                        evidence={
                            "group": group,
                            "peakConcurrency": track.peak,
                            "queuedWhileGroupBusySeconds": blocked,
                            "runs": len(items),
                        },
                    )
                )

    for provider, limit in sorted((inputs.provider_limits or {}).items()):
        items = [item for item in busy if item.record.actor.provider == provider]
        track = _Track(items, window)
        at_limit = track.seconds_at_or_above(limit)
        if at_limit == 0:
            continue
        blocked = sum(
            track.seconds_at_or_above(limit, wait.start, wait.end)
            for wait in waits
            if wait.record.actor.provider == provider
        )
        if blocked > 0:
            findings.append(
                _Finding(
                    kind=BottleneckKind.MODEL_CAPACITY_THROTTLING,
                    scope=f"provider:{provider}",
                    impact_seconds=blocked,
                    explanation=(
                        f"provider {provider} ran at its declared ceiling of {limit} "
                        f"concurrent runs for {at_limit}s, and work waited {blocked}s of "
                        "that time"
                    ),
                    evidence={
                        "provider": provider,
                        "concurrentRunLimit": limit,
                        "atLimitSeconds": at_limit,
                        "queuedWhileAtLimitSeconds": blocked,
                    },
                )
            )

    for provider, document in sorted(plan_capacity.items()):
        if document.get("usedFraction") == 1.0:
            findings.append(
                _Finding(
                    kind=BottleneckKind.MODEL_CAPACITY_THROTTLING,
                    # A distinct scope from the concurrency ceiling above, so the two
                    # recommendations stay separately addressable.
                    scope=f"provider:{provider}:plan",
                    impact_seconds=0,
                    explanation=(
                        f"provider {provider} reported its plan capacity fully consumed "
                        f"({document['unitsUsed']} of {document['unitsTotal']} "
                        f"{document['capacityUnit']})"
                    ),
                    evidence={
                        "provider": provider,
                        "unitsUsed": document["unitsUsed"],
                        "unitsTotal": document["unitsTotal"],
                        "capacityUnit": document["capacityUnit"],
                    },
                )
            )

    blockers: dict[str, list[_Interval]] = {}
    for wait in classified.get(WaitCause.DEPENDENCY.value, ()):
        blocker = (inputs.dependency_blocked or {}).get(wait.record.usage_id, "")
        if blocker:
            blockers.setdefault(blocker, []).append(wait)
    for blocker, items in sorted(blockers.items()):
        if len(items) < _FAN_IN_THRESHOLD:
            continue
        seconds = sum(item.seconds for item in items)
        findings.append(
            _Finding(
                kind=BottleneckKind.DEPENDENCY_FAN_IN,
                scope=f"dependency:{blocker}",
                impact_seconds=seconds,
                explanation=(f"{len(items)} runs waited a combined {seconds}s on {blocker}"),
                evidence={
                    "blockingRef": blocker,
                    "waitingRuns": len(items),
                    "blockedSeconds": seconds,
                    "threshold": _FAN_IN_THRESHOLD,
                },
            )
        )

    repeated = [
        item
        for item in busy
        if (item.record.repair_cycle or 0) >= 1 or (item.record.review_round or 0) >= 2
    ]
    if repeated:
        seconds = sum(item.seconds for item in repeated)
        units = sorted({f"{i.record.project_id}:{i.record.work_unit_id}" for i in repeated})
        findings.append(
            _Finding(
                kind=BottleneckKind.REPEATED_REVIEW_CYCLES,
                scope="global",
                impact_seconds=seconds,
                explanation=(
                    f"{len(repeated)} runs across {len(units)} work units were repairs or "
                    f"re-reviews, costing {seconds}s of runtime"
                ),
                evidence={
                    "runs": len(repeated),
                    "workUnits": units,
                    "seconds": seconds,
                },
            )
        )

    if handoff:
        total = sum(handoff.values())
        worst = max(handoff.items(), key=lambda item: (item[1], item[0]))
        findings.append(
            _Finding(
                kind=BottleneckKind.HANDOFF_DELAY,
                scope="global",
                impact_seconds=total,
                explanation=(
                    f"{total}s passed between one stage finishing and the next being "
                    f"queued, the worst being {worst[0]} at {worst[1]}s"
                ),
                evidence={
                    "totalSeconds": total,
                    "workUnits": len(handoff),
                    "worstWorkUnit": worst[0],
                    "worstSeconds": worst[1],
                },
            )
        )

    return tuple(
        sorted(findings, key=lambda item: (-item.impact_seconds, item.kind.value, item.scope))
    )


def _recommendations(findings: Sequence[_Finding]) -> tuple[dict[str, Any], ...]:
    """One advisory item per finding. Nothing here changes any policy."""
    return tuple(
        {
            "recommendationId": f"cap-{index + 1:03d}",
            "kind": finding.kind.value,
            "scope": finding.scope,
            "action": _ACTIONS[finding.kind],
            "rationale": finding.explanation,
            "impactSeconds": finding.impact_seconds,
            "evidence": dict(sorted(finding.evidence.items())),
            "advisory": True,
        }
        for index, finding in enumerate(findings)
    )


# -- the report ----------------------------------------------------------------


def build_capacity_report(
    records: Sequence[UsageRecord],
    *,
    window: ObservationWindow,
    inputs: CapacityInputs | None = None,
    project_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Measure one observation window of factory capacity.

    The window is explicit so idle capacity stays in the denominator. Records
    without timing are counted as unobserved rather than dropped, and every
    utilization figure is omitted, as ``null`` with a status, wherever the
    capacity it would divide by was never declared.
    """
    if inputs is None:
        inputs = CapacityInputs()
    scoped = tuple(
        record for record in records if project_ids is None or record.project_id in set(project_ids)
    )
    reading = _read(scoped, window)
    pool_tracks = _pool_tracks(reading.busy, inputs, window)
    overall = _Track(reading.busy, window)
    queue = _Track(reading.waits, window)
    plan_capacity = _plan_capacity_report(reading.busy, inputs, window)
    wait, classified = _wait_report(
        reading.waits, inputs, pool_tracks, reading.in_window, reading.coverage
    )
    findings = _bottlenecks(
        reading.busy,
        reading.waits,
        classified,
        inputs,
        pool_tracks,
        plan_capacity,
        wait["handoff"]["byWorkUnitSeconds"],
        window,
    )

    by_project: dict[str, Any] = {}
    for project_id in sorted({record.project_id for record in reading.in_window}):
        busy = [item for item in reading.busy if item.record.project_id == project_id]
        waits = [item for item in reading.waits if item.record.project_id == project_id]
        by_project[project_id] = {
            "concurrency": _Track(busy, window).as_dict(),
            "queue": _Track(waits, window).as_dict(),
            "waitSeconds": sum(item.seconds for item in waits),
            "usefulWork": _useful_work_report(busy),
        }

    return {
        "schemaVersion": CAPACITY_SCHEMA_VERSION,
        "window": window.as_dict(),
        "coverage": dict(sorted(reading.coverage.items())),
        "concurrency": overall.as_dict(with_segments=True),
        "queue": queue.as_dict(with_segments=True),
        "wait": wait,
        "runners": _runner_report(reading.busy, inputs, window),
        "models": _model_report(reading.busy, window),
        "planCapacity": plan_capacity,
        "usefulWork": _useful_work_report(reading.busy),
        "bottlenecks": [finding.as_dict() for finding in findings],
        "recommendations": list(_recommendations(findings)),
        "byProject": by_project,
        "limitations": [
            "the observation window is supplied by the caller, so idle capacity stays in "
            "every denominator",
            "runs without a recorded start and finish are counted as unobserved, never "
            "assumed to have run",
            "a utilization figure appears only where the capacity it divides by was "
            "declared; undeclared runners and unobserved plan capacity read as unknown",
            "a wait is called resource-blocked only when the pool that ran it was "
            "saturated for most of the wait; anything else stays unclassified",
            "useful work counts only runs that completed on their first attempt; retries "
            "and failures remain in the denominator",
            "bottlenecks and recommendations are evidence with an explanation, and change "
            "no routing, concurrency or spending policy",
        ],
    }


# -- loading declarations ------------------------------------------------------

_INPUT_KEYS = frozenset(
    {
        "schemaVersion",
        "pools",
        "planCapacity",
        "dependencyBlocked",
        "concurrencyGroups",
        "providerLimits",
    }
)
_POOL_KEYS = frozenset({"poolId", "slots", "workers", "kind"})
_PLAN_KEYS = frozenset(
    {"provider", "plan", "capacityUnit", "observedAt", "unitsTotal", "unitsUsed"}
)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    _require(isinstance(value, Mapping), f"{label} must be an object")
    assert isinstance(value, Mapping)
    return value


def _known_keys(entry: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(entry) - allowed)
    if unknown:
        raise CapacityError(f"{label} declares unknown keys: " + ", ".join(unknown))


def _string_map(value: Any, label: str) -> dict[str, str]:
    entry = _mapping(value, label)
    for key, item in entry.items():
        _require(
            isinstance(key, str) and isinstance(item, str) and bool(item.strip()),
            f"{label} must map strings to non-empty strings",
        )
    return dict(entry)


def load_capacity_inputs(document: Any) -> CapacityInputs:
    """Parse the declarations the records cannot carry, fail-closed.

    An unknown key is refused rather than ignored, so a misspelled ``slots`` or
    ``providerLimits`` cannot silently turn a measured utilization back into an
    unknown one.
    """
    entry = _mapping(document, "capacity inputs")
    _known_keys(entry, _INPUT_KEYS, "capacity inputs")
    version = entry.get("schemaVersion", CAPACITY_SCHEMA_VERSION)
    _require(
        version == CAPACITY_SCHEMA_VERSION,
        f"unsupported capacity inputs schemaVersion: {version!r}",
    )

    pools = []
    raw_pools = entry.get("pools", [])
    _require(isinstance(raw_pools, list), "capacity inputs: pools must be an array")
    for item in raw_pools:
        pool = _mapping(item, "runner pool")
        _known_keys(pool, _POOL_KEYS, "runner pool")
        workers = pool.get("workers", [])
        _require(
            isinstance(workers, list) and all(isinstance(name, str) for name in workers),
            "runner pool: workers must be an array of strings",
        )
        pools.append(
            RunnerPool(
                pool_id=str(pool.get("poolId", "")),
                slots=_positive_int(pool.get("slots"), "runner pool: slots"),
                workers=tuple(workers),
                kind=str(pool.get("kind", "unspecified")),
            )
        )

    plan_capacity = []
    raw_plans = entry.get("planCapacity", [])
    _require(isinstance(raw_plans, list), "capacity inputs: planCapacity must be an array")
    for item in raw_plans:
        plan = _mapping(item, "plan capacity")
        _known_keys(plan, _PLAN_KEYS, "plan capacity")
        plan_capacity.append(
            PlanCapacityObservation(
                provider=str(plan.get("provider", "")),
                plan=str(plan.get("plan", "")),
                capacity_unit=str(plan.get("capacityUnit", "")),
                observed_at=str(plan.get("observedAt", "")),
                units_total=plan.get("unitsTotal"),
                units_used=plan.get("unitsUsed"),
            )
        )

    limits: dict[str, int] = {}
    for provider, limit in _mapping(
        entry.get("providerLimits", {}), "capacity inputs: providerLimits"
    ).items():
        limits[str(provider)] = _positive_int(limit, f"provider limit for {provider}")

    return CapacityInputs(
        pools=tuple(pools),
        plan_capacity=tuple(plan_capacity),
        dependency_blocked=_string_map(
            entry.get("dependencyBlocked", {}), "capacity inputs: dependencyBlocked"
        ),
        concurrency_groups=_string_map(
            entry.get("concurrencyGroups", {}), "capacity inputs: concurrencyGroups"
        ),
        provider_limits=limits,
    )
