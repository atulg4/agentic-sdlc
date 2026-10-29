"""Read-only dispatcher liveness and queue status for the Control Center.

The intake dispatcher persists a heartbeat and one checkpoint per event it
decides (``checkpoints.py``), and the lifecycle ledger records where every work
unit is now (``event_ledger.py``). Nothing joined the two, so an operator could
not tell a quiet factory from a dead dispatcher. This module answers that from
durable state only: it never reads the wall clock (``now`` is an input), never
writes, and never infers health it cannot see.

Rejected decisions (``ignored`` / ``blocked``) are recorded before the
dispatcher derives a unit id, so they carry ``unit_id == ""``: they are
reported per event, never folded into the per-unit counts.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from .checkpoints import CheckpointSnapshot
from .event_ledger import FactoryState, carries_credential_token

__all__ = [
    "DISPATCHER_STATUS_SCHEMA_VERSION",
    "QUEUE_BUCKETS",
    "DispatcherStatusError",
    "build_dispatcher_status",
    "queue_buckets",
]

DISPATCHER_STATUS_SCHEMA_VERSION = 1

_F = FactoryState

#: Every factory state belongs to exactly one operator-facing bucket. Buckets
#: come from ``byState``, not ``byStage``: the merge stage covers both
#: ``ready-for-human-merge`` (waiting) and ``merged`` (done), and the rollback
#: stage covers both ``rolling-back`` (running) and ``rolled-back`` (done).
QUEUE_BUCKETS: Mapping[str, str] = {
    _F.INTAKE.value: "queued",
    _F.TRIAGED.value: "queued",
    _F.SPECIFIED.value: "queued",
    _F.PLANNED.value: "queued",
    _F.APPROVED.value: "queued",
    _F.DISPATCHED.value: "queued",
    _F.IMPLEMENTING.value: "running",
    _F.VERIFYING.value: "running",
    _F.REVIEWING.value: "running",
    _F.REPAIR_NEEDED.value: "running",
    _F.REPAIRING.value: "running",
    _F.RE_REVIEWING.value: "running",
    _F.DEPLOYING.value: "running",
    _F.DEPLOYMENT_VERIFYING.value: "running",
    _F.ROLLING_BACK.value: "running",
    _F.APPROVAL_PENDING.value: "awaitingHuman",
    _F.READY_FOR_HUMAN_MERGE.value: "awaitingHuman",
    _F.BLOCKED.value: "blocked",
    _F.MERGED.value: "finished",
    _F.LIVE.value: "finished",
    _F.ROLLED_BACK.value: "finished",
    _F.FAILED.value: "finished",
    _F.CANCELLED.value: "finished",
    _F.SUPERSEDED.value: "finished",
}
_BUCKET_ORDER = ("queued", "running", "awaitingHuman", "blocked", "finished", "unknown")
_REDACTED = "[redacted: credential-shaped content]"


class DispatcherStatusError(ValueError):
    """Raised when a status request is malformed."""


def _instant(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as error:
        raise DispatcherStatusError(f"{label} must be an RFC 3339 timestamp: {value!r}") from error
    if parsed.tzinfo is None:
        raise DispatcherStatusError(f"{label} must carry a UTC offset: {value!r}")
    return parsed


def _safe_reason(reason: str) -> str:
    return _REDACTED if carries_credential_token(reason) else reason


def _heartbeat(last: str, now: datetime, stale_after_seconds: int) -> dict[str, Any]:
    if not last.strip():
        return {"lastHeartbeat": None, "ageSeconds": None, "stale": True, "condition": "missing"}
    try:
        observed = _instant(last, "heartbeat")
    except DispatcherStatusError:
        # A heartbeat that cannot be read proves nothing about liveness.
        return {"lastHeartbeat": last, "ageSeconds": None, "stale": True, "condition": "unreadable"}
    age = int((now - observed).total_seconds())
    if age < 0:
        # A heartbeat from the future means a skewed clock, not a live dispatcher.
        condition = "future"
    elif age > stale_after_seconds:
        condition = "stale"
    else:
        condition = "fresh"
    return {
        "lastHeartbeat": last,
        "ageSeconds": age,
        "stale": condition != "fresh",
        "condition": condition,
    }


def queue_buckets(by_state: Mapping[str, int]) -> dict[str, Any]:
    """Fold a ledger ``byState`` rollup into operator buckets.

    A state this module does not know is counted as ``unknown`` and named in
    ``unknownStates``; it is never presented as queued or running work.
    """

    counts = dict.fromkeys(_BUCKET_ORDER, 0)
    unknown_states: set[str] = set()
    for state, count in by_state.items():
        bucket = QUEUE_BUCKETS.get(state)
        if bucket is None:
            bucket = "unknown"
            unknown_states.add(state)
        counts[bucket] += int(count)
    return {**counts, "unknownStates": sorted(unknown_states)}


def build_dispatcher_status(
    snapshot: CheckpointSnapshot,
    *,
    now: str,
    stale_after_seconds: int,
    blocked_event_limit: int = 20,
    ledger_aggregate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Summarize dispatcher liveness, decisions and (optionally) lifecycle queues."""

    if stale_after_seconds <= 0:
        raise DispatcherStatusError("stale_after_seconds must be positive")
    if blocked_event_limit < 0:
        raise DispatcherStatusError("blocked_event_limit cannot be negative")
    observed_at = _instant(now, "now")

    # The snapshot is in journal order, so the last record seen for a unit is
    # its latest checkpoint; replays and duplicate events count the unit once.
    latest_by_unit = {record.unit_id: record for record in snapshot.records if record.unit_id}
    unit_states: dict[str, int] = {}
    for record in latest_by_unit.values():
        unit_states[record.state] = unit_states.get(record.state, 0) + 1

    rejected = [record for record in snapshot.records if not record.accepted]
    rejected_states: dict[str, int] = {}
    for record in rejected:
        rejected_states[record.state] = rejected_states.get(record.state, 0) + 1
    blocked = sorted(
        (record for record in rejected if record.state == "blocked"),
        key=lambda record: (record.timestamp, record.event_key),
        reverse=True,
    )

    document: dict[str, Any] = {
        "schemaVersion": DISPATCHER_STATUS_SCHEMA_VERSION,
        "observedAt": now,
        "staleAfterSeconds": stale_after_seconds,
        "heartbeat": _heartbeat(snapshot.last_heartbeat, observed_at, stale_after_seconds),
        "dispatcher": {
            "units": {
                "total": len(latest_by_unit),
                "byState": dict(sorted(unit_states.items())),
            },
            "rejectedEvents": {
                "total": len(rejected),
                "byState": dict(sorted(rejected_states.items())),
            },
            "blockedEvents": [
                {
                    "eventKey": record.event_key,
                    "reason": _safe_reason(record.reason),
                    "timestamp": record.timestamp,
                }
                for record in blocked[:blocked_event_limit]
            ],
            "blockedEventsTruncated": len(blocked) > blocked_event_limit,
        },
    }
    if ledger_aggregate is not None:
        totals = ledger_aggregate.get("totals", {})
        document["lifecycle"] = {
            "workUnits": int(totals.get("workUnits", 0)),
            **queue_buckets(totals.get("byState", {})),
        }
    return document
