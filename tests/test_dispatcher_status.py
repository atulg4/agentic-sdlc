from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agentic_sdlc.checkpoints import (
    CheckpointError,
    CheckpointRecord,
    CheckpointSnapshot,
    SQLiteCheckpointStore,
    read_checkpoint_snapshot,
)
from agentic_sdlc.dispatcher import AutonomousIntakeDispatcher
from agentic_sdlc.dispatcher_status import (
    QUEUE_BUCKETS,
    DispatcherStatusError,
    build_dispatcher_status,
    queue_buckets,
)
from agentic_sdlc.event_ledger import FactoryState
from agentic_sdlc.models import WorkEvent, WorkKind
from agentic_sdlc.orchestration import Orchestrator

T1 = "2026-10-29T09:00:00Z"
T2 = "2026-10-29T09:05:00Z"
T3 = "2026-10-29T09:10:00Z"
NOW = "2026-10-29T09:30:00Z"


def _event(
    number: int = 51, *, action: str = "opened", labels: tuple[str, ...] = ("forge-managed",)
) -> WorkEvent:
    return WorkEvent(
        provider="github",
        kind=WorkKind.ISSUE,
        action=action,
        repository="example/forge",
        number=number,
        title=f"Work item {number}",
        body="untrusted body",
        labels=labels,
        actor="someone",
        raw={"token": "ghp_must_never_be_reported"},
    )


def _dispatch(path: Path, *events: tuple[WorkEvent, str]) -> None:
    dispatcher = AutonomousIntakeDispatcher(Orchestrator(), checkpoints=SQLiteCheckpointStore(path))
    for event, timestamp in events:
        dispatcher.dispatch(event, timestamp=timestamp)


def _status(path: Path, **kwargs: object) -> dict:
    options = {"now": NOW, "stale_after_seconds": 3600, **kwargs}
    return build_dispatcher_status(read_checkpoint_snapshot(path), **options)  # type: ignore[arg-type]


def test_never_written_heartbeat_is_reported_missing_and_stale(tmp_path: Path) -> None:
    path = tmp_path / "checkpoints.sqlite3"
    SQLiteCheckpointStore(path)

    heartbeat = _status(path)["heartbeat"]

    assert heartbeat == {
        "lastHeartbeat": None,
        "ageSeconds": None,
        "stale": True,
        "condition": "missing",
    }


@pytest.mark.parametrize(
    ("now", "condition", "age", "stale"),
    [
        ("2026-10-29T09:30:00Z", "fresh", 1500, False),
        ("2026-10-29T09:15:00+00:00", "fresh", 600, False),
        ("2026-10-29T11:05:01Z", "stale", 7201, True),
        ("2026-10-29T09:00:00Z", "future", -300, True),
    ],
)
def test_heartbeat_age_and_condition(
    tmp_path: Path, now: str, condition: str, age: int, stale: bool
) -> None:
    path = tmp_path / "checkpoints.sqlite3"
    _dispatch(path, (_event(), T2))

    heartbeat = _status(path, now=now, stale_after_seconds=7200)["heartbeat"]

    assert heartbeat["lastHeartbeat"] == T2
    assert heartbeat["condition"] == condition
    assert heartbeat["ageSeconds"] == age
    assert heartbeat["stale"] is stale


def test_unreadable_heartbeat_is_stale_not_healthy(tmp_path: Path) -> None:
    path = tmp_path / "checkpoints.sqlite3"
    SQLiteCheckpointStore(path).touch_heartbeat("yesterday-ish")

    heartbeat = _status(path)["heartbeat"]

    assert heartbeat["condition"] == "unreadable"
    assert heartbeat["stale"] is True
    assert heartbeat["ageSeconds"] is None


def test_duplicate_and_replayed_events_count_each_unit_once(tmp_path: Path) -> None:
    path = tmp_path / "checkpoints.sqlite3"
    _dispatch(
        path,
        (_event(51), T1),
        (_event(51), T2),  # exact duplicate: replays the stored decision
        (_event(51, action="edited"), T2),  # new event for a known unit: checkpoint-replay
        (_event(52), T3),
    )

    units = _status(path)["dispatcher"]["units"]

    assert units == {"total": 2, "byState": {"triaged": 2}}


def test_blocked_events_are_listed_latest_first_with_reasons(tmp_path: Path) -> None:
    path = tmp_path / "checkpoints.sqlite3"
    _dispatch(
        path,
        (_event(60, labels=("forge-managed", "forge-paused")), T1),
        (_event(61, labels=("forge-managed", "security-review-required")), T3),
        (_event(62, labels=("bug",)), T2),  # no forge-managed marker: ignored
    )

    dispatcher = _status(path)["dispatcher"]

    assert dispatcher["units"] == {"total": 0, "byState": {}}
    assert dispatcher["rejectedEvents"] == {"total": 3, "byState": {"blocked": 2, "ignored": 1}}
    assert [item["reason"] for item in dispatcher["blockedEvents"]] == [
        "autonomous intake blocked by label: security-review-required",
        "autonomous intake blocked by label: forge-paused",
    ]
    assert [item["timestamp"] for item in dispatcher["blockedEvents"]] == [T3, T1]
    assert all(item["eventKey"].startswith("forge-event:") for item in dispatcher["blockedEvents"])
    assert dispatcher["blockedEventsTruncated"] is False


def test_blocked_event_list_is_bounded_and_says_so(tmp_path: Path) -> None:
    path = tmp_path / "checkpoints.sqlite3"
    _dispatch(
        path,
        *((_event(n, labels=("forge-managed", "forge-paused")), T1) for n in range(70, 75)),
    )

    dispatcher = _status(path, blocked_event_limit=2)["dispatcher"]

    assert len(dispatcher["blockedEvents"]) == 2
    assert dispatcher["blockedEventsTruncated"] is True
    assert dispatcher["rejectedEvents"]["total"] == 5


def test_credential_shaped_reasons_are_redacted() -> None:
    snapshot = CheckpointSnapshot(
        last_heartbeat=T1,
        records=(
            CheckpointRecord(
                event_key="forge-event:abc",
                unit_id="",
                state="blocked",
                accepted=False,
                reason="autonomous intake blocked by label: ghp%5Fleaked",
                timestamp=T1,
            ),
        ),
    )

    document = build_dispatcher_status(snapshot, now=NOW, stale_after_seconds=3600)

    assert document["dispatcher"]["blockedEvents"][0]["reason"].startswith("[redacted")
    assert "leaked" not in json.dumps(document)


def test_output_never_carries_raw_payload_or_body(tmp_path: Path) -> None:
    path = tmp_path / "checkpoints.sqlite3"
    _dispatch(path, (_event(51), T1), (_event(60, labels=("forge-managed", "forge-paused")), T2))

    rendered = json.dumps(_status(path))

    assert "ghp_" not in rendered
    assert "untrusted body" not in rendered
    assert "someone" not in rendered


def test_status_is_deterministic_for_pinned_inputs(tmp_path: Path) -> None:
    path = tmp_path / "checkpoints.sqlite3"
    _dispatch(path, (_event(51), T1), (_event(60, labels=("forge-managed", "forge-paused")), T2))

    assert json.dumps(_status(path), sort_keys=True) == json.dumps(_status(path), sort_keys=True)


def test_every_factory_state_has_exactly_one_bucket() -> None:
    assert set(QUEUE_BUCKETS) == {state.value for state in FactoryState}


def test_queue_buckets_split_waiting_from_done_and_name_unknown_states() -> None:
    buckets = queue_buckets(
        {
            "triaged": 2,
            "implementing": 1,
            "rolling-back": 1,
            "ready-for-human-merge": 1,
            "approval-pending": 1,
            "blocked": 3,
            "merged": 4,
            "rolled-back": 1,
            "teleporting": 2,
        }
    )

    assert buckets == {
        "queued": 2,
        "running": 2,
        "awaitingHuman": 2,
        "blocked": 3,
        "finished": 5,
        "unknown": 2,
        "unknownStates": ["teleporting"],
    }


def test_ledger_aggregate_adds_lifecycle_buckets(tmp_path: Path) -> None:
    path = tmp_path / "checkpoints.sqlite3"
    SQLiteCheckpointStore(path)

    document = _status(
        path,
        ledger_aggregate={"totals": {"workUnits": 3, "byState": {"reviewing": 2, "blocked": 1}}},
    )

    assert document["lifecycle"]["workUnits"] == 3
    assert document["lifecycle"]["running"] == 2
    assert document["lifecycle"]["blocked"] == 1
    assert "lifecycle" not in _status(path)


def test_missing_database_fails_closed_and_creates_nothing(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "missing.sqlite3"

    with pytest.raises(CheckpointError, match="not found"):
        read_checkpoint_snapshot(path)

    assert not path.exists()
    assert not path.parent.exists()


def test_foreign_sqlite_database_fails_closed_without_being_modified(tmp_path: Path) -> None:
    path = tmp_path / "other.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER)")
    before = path.read_bytes()

    with pytest.raises(CheckpointError, match="not a dispatcher checkpoint database"):
        read_checkpoint_snapshot(path)

    assert path.read_bytes() == before


def test_non_database_file_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("not sqlite at all, just text that is long enough", encoding="utf-8")

    with pytest.raises(CheckpointError):
        read_checkpoint_snapshot(path)


def test_reading_a_snapshot_never_writes_the_journal(tmp_path: Path) -> None:
    path = tmp_path / "checkpoints.sqlite3"
    _dispatch(path, (_event(51), T1))
    store = SQLiteCheckpointStore(path)
    before = (store.last_heartbeat(), store.get_unit("github:example/forge:issue:51"))

    snapshot = read_checkpoint_snapshot(path)

    assert snapshot.last_heartbeat == T1
    assert (store.last_heartbeat(), store.get_unit("github:example/forge:issue:51")) == before


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"now": "2026-10-29T09:30:00"}, "UTC offset"),
        ({"now": "soon"}, "RFC 3339"),
        ({"stale_after_seconds": 0}, "positive"),
        ({"blocked_event_limit": -1}, "negative"),
    ],
)
def test_malformed_requests_are_rejected(tmp_path: Path, kwargs: dict, message: str) -> None:
    path = tmp_path / "checkpoints.sqlite3"
    SQLiteCheckpointStore(path)

    with pytest.raises(DispatcherStatusError, match=message):
        _status(path, **kwargs)
