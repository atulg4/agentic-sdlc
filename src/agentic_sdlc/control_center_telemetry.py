"""Telemetry overlay for the Control Center: cost, capacity, parallelism, value.

:mod:`control_center` answers *what state is every work unit in*. It
deliberately knows nothing about what the factory consumed to get there. This
module joins that snapshot to the telemetry the other ledgers already produce,
so one console can answer "what is running, what did it cost, how much
parallelism did we get, and was the result worth it" without any of those
layers learning about the UI.

It **composes** rather than recomputes:

- ``usage_ledger`` rolls up tokens, time, model, worker and monetary
  equivalents, and compares estimates to actuals.
- ``capacity_metrics`` measures concurrency, queueing, runner and plan-capacity
  utilization, useful work and bottlenecks.
- an **effectiveness report document** — the output of
  ``sdlcctl effectiveness-report`` — supplies first-pass rates, repair rounds,
  time-to-live, reverts and value per unit of effort.

The effectiveness side is consumed as a *document* rather than by importing the
module that produces it, for the same reason the snapshot consumes a backlog
document: the console is a view over whatever each adapter published, and a
report that was produced hours ago on another runner is still the report.

Every honesty rule the layers below enforce is preserved here, because this
module adds no arithmetic of its own to break them:

- **Unknown stays unknown.** A section with no data behind it reports
  ``status: "unknown"`` with the reason, never a zero.
- **Subscriptions are not dollars**, and remaining plan capacity appears only
  where a provider actually reported it.
- **Freshness is labeled.** Every overlay carries the instant its data was last
  good, so a console shows "as of" rather than implying live truth.
- **No credentials.** The overlay is serialized to a browser, so every
  consumed document is scanned for credential-shaped values and secret-shaped
  keys.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .capacity_metrics import (
    CapacityInputs,
    ObservationWindow,
    build_capacity_report,
)
from .control_center import (
    ControlCenterError,
    ControlCenterSnapshot,
    secret_bearing_control_center_fields,
)
from .event_ledger import carries_credential_token
from .project_registry import EMPTY_PROJECT_REGISTRY, ProjectRegistry
from .usage_ledger import (
    MonetaryStatus,
    UsageLedger,
    UsageRecord,
    parse_timestamp,
)

__all__ = [
    "EFFECTIVENESS_DOCUMENT_KEYS",
    "TELEMETRY_SCHEMA_VERSION",
    "build_telemetry_overlay",
    "export_control_center",
    "filter_usage_window",
    "load_effectiveness_document",
]

TELEMETRY_SCHEMA_VERSION = 1

DEFAULT_STALE_AFTER_SECONDS = 900

#: The top-level sections of an effectiveness report (#106). Listed so a
#: producer that drifts is caught on read rather than rendered as a blank view.
EFFECTIVENESS_DOCUMENT_KEYS = frozenset(
    {
        "schemaVersion",
        "window",
        "sampleCount",
        "outcomes",
        "quality",
        "observations",
        "value",
        "estimateVsActual",
        "learnings",
        "byProject",
        "limitations",
    }
)

#: Sections surfaced per project, in the order a console reads them.
_EFFECTIVENESS_PROJECT_KEYS = ("outcomes", "accepted", "quality", "value", "learnings")

_SECRET_KEYS = frozenset(name.lower() for name in secret_bearing_control_center_fields())

#: Recursion bound for the credential scan. The overlay is legitimately deep —
#: a work unit holds runs, each holding an estimate, its monetary equivalent and
#: that figure's embedded pricing snapshot — so this guards against pathological
#: structures rather than constraining the composed document.
_MAX_DEPTH = 16


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ControlCenterError(message)


def _scan(value: Any, label: str, depth: int = 0) -> Any:
    """Reject a consumed document that carries a credential or a secret key."""
    _require(depth <= _MAX_DEPTH, f"{label} nests too deeply")
    if isinstance(value, str):
        _require(
            not carries_credential_token(value),
            f"{label} carries a credential-shaped value (withheld); the overlay "
            "is serialized to a browser and must never carry one",
        )
        return value
    if isinstance(value, Mapping):
        for key, item in value.items():
            _require(isinstance(key, str), f"{label} keys must be strings")
            assert isinstance(key, str)
            _require(
                key.lower() not in _SECRET_KEYS,
                f"{label} must not carry a credential field: {key}",
            )
            _scan(item, f"{label}.{key}", depth + 1)
        return value
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _scan(item, f"{label}[{index}]", depth + 1)
        return value
    return value


def load_effectiveness_document(document: Any) -> dict[str, Any]:
    """Parse one effectiveness report fail-closed.

    The report is produced elsewhere, so it is validated on the way in: an
    unsupported version, an unknown section, or anything credential-shaped is
    refused rather than rendered.
    """
    label = "effectiveness report"
    _require(isinstance(document, Mapping), f"{label} must be an object")
    assert isinstance(document, Mapping)
    unknown = sorted(set(document) - EFFECTIVENESS_DOCUMENT_KEYS)
    _require(not unknown, f"{label} declares unknown sections: " + ", ".join(unknown))
    version = document.get("schemaVersion")
    _require(
        version == 1,
        f"{label} schemaVersion must be 1, not {version!r}",
    )
    _scan(document, label)
    return dict(document)


def _unknown(reason: str) -> dict[str, Any]:
    return {"status": "unknown", "reason": reason}


def _freshness(
    *, observed_at: str, age_seconds: int | None, stale_after_seconds: int
) -> dict[str, Any]:
    """How much to trust the overlay, with the instant it was last good."""
    _require(
        isinstance(stale_after_seconds, int)
        and not isinstance(stale_after_seconds, bool)
        and stale_after_seconds > 0,
        "staleAfterSeconds must be a positive integer",
    )
    if age_seconds is None:
        return {
            "status": "unknown",
            "lastGoodAt": observed_at or None,
            "ageSeconds": None,
            "staleAfterSeconds": stale_after_seconds,
            "reason": "no age was observed, so freshness cannot be asserted",
        }
    _require(
        isinstance(age_seconds, int) and not isinstance(age_seconds, bool) and age_seconds >= 0,
        "ageSeconds must be a non-negative integer",
    )
    _require(
        bool(observed_at),
        "observed freshness requires the instant the data was last good",
    )
    return {
        "status": "stale" if age_seconds > stale_after_seconds else "observed",
        "lastGoodAt": observed_at,
        "ageSeconds": age_seconds,
        "staleAfterSeconds": stale_after_seconds,
        "reason": "",
    }


def filter_usage_window(
    records: Sequence[UsageRecord],
    *,
    since: str | None = None,
    until: str | None = None,
) -> tuple[UsageRecord, ...]:
    """Usage records whose occurrence falls inside a half-open time range.

    Bounds are compared as instants, not strings, because RFC 3339 offsets and
    fractional seconds do not sort chronologically as text.
    """
    after = parse_timestamp(since, "window since") if since else None
    before = parse_timestamp(until, "window until") if until else None
    _require(
        after is None or before is None or after <= before,
        "the window's since must not come after its until",
    )
    kept = []
    for record in records:
        occurred = parse_timestamp(record.occurred_at, f"usage {record.usage_id} occurredAt")
        if after is not None and occurred < after:
            continue
        if before is not None and occurred > before:
            continue
        kept.append(record)
    return tuple(kept)


def _cost_section(ledger: UsageLedger, project_ids: Sequence[str] | None) -> dict[str, Any]:
    """Token, time and monetary rollups, by project and by model."""
    if not len(ledger):
        return _unknown("no usage record was supplied for this window")
    return {
        "status": "observed",
        "byProject": ledger.aggregate(group_by=("project",), project_ids=project_ids),
        "byModel": ledger.aggregate(group_by=("model",), project_ids=project_ids),
        "byStage": ledger.aggregate(group_by=("stage",), project_ids=project_ids),
    }


def _billing_mix(records: Sequence[UsageRecord]) -> dict[str, int]:
    """How many records carry each monetary basis, so dollars stay separable."""
    mix = {status.value: 0 for status in MonetaryStatus}
    for record in records:
        actual = record.actual
        status = actual.monetary.status if actual is not None else MonetaryStatus.UNKNOWN
        mix[status.value] += 1
    return mix


def _work_unit_section(ledger: UsageLedger, records: Sequence[UsageRecord]) -> dict[str, Any]:
    """Per-work-unit drill-down: the runs behind one row of the backlog table.

    Each unit carries its runs with their estimate-versus-actual, plus the cost
    rollup for that unit taken from the usage ledger's own aggregation, so the
    drill-down and the cost view can never disagree.
    """
    if not records:
        return {}
    rollup = {}
    for bucket in ledger.aggregate(group_by=("project", "workUnit"))["buckets"]:
        key = bucket.get("key") or {}
        rollup[(str(key.get("projectId", "")), str(key.get("workUnitId", "")))] = bucket
    units: dict[str, dict[str, Any]] = {}
    for record in records:
        key = f"{record.project_id}/{record.work_unit_id}"
        entry = units.setdefault(
            key,
            {
                "projectId": record.project_id,
                "workUnitId": record.work_unit_id,
                "runs": [],
                "stages": [],
            },
        )
        runs = entry["runs"]
        assert isinstance(runs, list)
        runs.append(
            {
                "usageId": record.usage_id,
                "runId": record.run_id,
                "attempt": record.attempt,
                "stage": record.stage.value,
                "taskClass": record.task_class,
                "model": record.actor.model,
                "modelAlias": record.actor.model_alias,
                "provider": record.actor.provider,
                "worker": record.actor.worker,
                "agentId": record.actor.agent_id,
                "issueRef": record.work_unit.issue_ref,
                "changeRequestRef": record.work_unit.change_request_ref,
                "occurredAt": record.occurred_at,
                "estimate": record.estimate.as_dict() if record.estimate else None,
                "actual": record.actual.as_dict() if record.actual else None,
                "infrastructure": (
                    record.infrastructure.as_dict() if record.infrastructure else None
                ),
                "estimateVsActual": record.estimate_error.as_dict(),
            }
        )
        stages = entry["stages"]
        assert isinstance(stages, list)
        if record.stage.value not in stages:
            stages.append(record.stage.value)
    for entry in units.values():
        runs = entry["runs"]
        assert isinstance(runs, list)
        runs.sort(key=lambda item: (str(item["occurredAt"]), str(item["usageId"])))
        stages = entry["stages"]
        assert isinstance(stages, list)
        stages.sort()
        entry["runCount"] = len(runs)
        entry["cost"] = rollup.get((str(entry["projectId"]), str(entry["workUnitId"])))
    return {name: units[name] for name in sorted(units)}


def _effectiveness_section(
    document: Mapping[str, Any] | None, project_ids: Sequence[str] | None
) -> dict[str, Any]:
    """The value and quality view, straight from the report that produced it."""
    if document is None:
        return _unknown(
            "no effectiveness report was supplied; outcome records are recorded "
            "separately and the console never infers delivery from runs"
        )
    scope = None if project_ids is None else frozenset(project_ids)
    by_project = document.get("byProject") or {}
    _require(isinstance(by_project, Mapping), "effectiveness byProject must be an object")
    assert isinstance(by_project, Mapping)
    scoped = {
        name: value for name, value in sorted(by_project.items()) if scope is None or name in scope
    }
    return {
        "status": "observed",
        "sampleCount": document.get("sampleCount"),
        "window": document.get("window"),
        "outcomes": document.get("outcomes"),
        "quality": document.get("quality"),
        "observations": document.get("observations"),
        "value": document.get("value"),
        "estimateVsActual": document.get("estimateVsActual"),
        "learnings": document.get("learnings"),
        "byProject": scoped,
        "limitations": list(document.get("limitations") or ()),
    }


def build_telemetry_overlay(
    snapshot: ControlCenterSnapshot,
    *,
    usage_records: Sequence[UsageRecord] = (),
    window: ObservationWindow | None = None,
    capacity_inputs: CapacityInputs | None = None,
    effectiveness: Mapping[str, Any] | None = None,
    registry: ProjectRegistry = EMPTY_PROJECT_REGISTRY,
    observed_at: str = "",
    age_seconds: int | None = None,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
    project_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Join one snapshot to the telemetry behind it.

    The scope is the caller's: ``project_ids`` narrows every section at once,
    so a project-scoped console cannot see another project's cost, capacity or
    outcomes. Sections with no data behind them read ``unknown`` with a reason
    rather than as zeroes.
    """
    scoped_records = tuple(
        record
        for record in usage_records
        if project_ids is None or record.project_id in set(project_ids)
    )
    ledger = UsageLedger(registry=registry)
    ledger.extend(scoped_records)

    if window is None:
        capacity: dict[str, Any] = _unknown(
            "no observation window was supplied; utilization needs an explicit "
            "window so idle capacity stays in the denominator"
        )
        parallelism: dict[str, Any] = dict(capacity)
    else:
        report = build_capacity_report(
            scoped_records,
            window=window,
            inputs=capacity_inputs,
            project_ids=project_ids,
        )
        capacity = {
            "status": "observed",
            "window": report["window"],
            "models": report["models"],
            "planCapacity": report["planCapacity"],
            "limitations": report["limitations"],
        }
        parallelism = {
            "status": "observed",
            "window": report["window"],
            "concurrency": report["concurrency"],
            "queue": report["queue"],
            "wait": report["wait"],
            "runners": report["runners"],
            "usefulWork": report["usefulWork"],
            "bottlenecks": report["bottlenecks"],
            "recommendations": report["recommendations"],
            "byProject": report["byProject"],
            "limitations": report["limitations"],
        }

    overlay = {
        "schemaVersion": TELEMETRY_SCHEMA_VERSION,
        "generatedAt": snapshot.generated_at,
        "projectIds": sorted(project_ids) if project_ids is not None else None,
        "freshness": _freshness(
            observed_at=observed_at or snapshot.generated_at,
            age_seconds=age_seconds,
            stale_after_seconds=stale_after_seconds,
        ),
        "cost": _cost_section(ledger, project_ids),
        "billingMix": _billing_mix(scoped_records),
        "capacity": capacity,
        "parallelism": parallelism,
        "effectiveness": _effectiveness_section(
            load_effectiveness_document(effectiveness) if effectiveness is not None else None,
            project_ids,
        ),
        "workUnits": _work_unit_section(ledger, scoped_records),
        "limitations": [
            "every figure is composed from the usage, capacity and effectiveness "
            "layers; this overlay adds no arithmetic of its own",
            "a section with no data behind it reads unknown with its reason, never as a zero",
            "subscription usage is a labeled pay-as-you-go equivalent and is never "
            "presented as billed dollars",
            "remaining plan capacity appears only where a provider reported it",
            "utilization requires an explicit observation window, so a quiet period "
            "stays in the denominator",
            "the overlay is a read-only view: it holds no credential and executes nothing",
        ],
    }
    return _scan(overlay, "telemetry overlay")


def export_control_center(
    snapshot: ControlCenterSnapshot,
    *,
    overlay: Mapping[str, Any] | None = None,
    stream: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One self-contained document for offline analysis.

    The snapshot keeps its own content digest, so an exported file can be
    checked against the console that produced it rather than trusted.
    """
    document: dict[str, Any] = {
        "schemaVersion": TELEMETRY_SCHEMA_VERSION,
        "exportedFrom": "forge-control-center",
        "generatedAt": snapshot.generated_at,
        "snapshot": snapshot.as_document(),
        "telemetry": dict(overlay) if overlay is not None else None,
        "stream": dict(stream) if stream is not None else None,
    }
    return _scan(document, "control center export")
