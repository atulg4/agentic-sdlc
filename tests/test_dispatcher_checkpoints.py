from __future__ import annotations

import sqlite3

import pytest

from agentic_sdlc.checkpoints import CheckpointError, CheckpointRecord, SQLiteCheckpointStore
from agentic_sdlc.dispatcher import AutonomousIntakeDispatcher, DispatchError
from agentic_sdlc.models import WorkEvent, WorkKind
from agentic_sdlc.orchestration import Orchestrator, WorkUnitState

T1 = "2026-08-15T03:30:00Z"
T2 = "2026-08-15T03:31:00Z"


def _event(*, action: str = "opened", raw: dict | None = None) -> WorkEvent:
    return WorkEvent(
        provider="github",
        kind=WorkKind.ISSUE,
        action=action,
        repository="atulg4/example",
        number=51,
        title="Autonomous intake",
        body="untrusted body with secret-like material",
        labels=("forge-managed",),
        actor="atulg4",
        raw=raw or {"token": "must-never-be-persisted"},
    )


def test_checkpoint_survives_process_restart_and_replays_duplicate(tmp_path) -> None:
    path = tmp_path / "forge-checkpoints.sqlite3"
    first_store = SQLiteCheckpointStore(path)
    first = AutonomousIntakeDispatcher(Orchestrator(), checkpoints=first_store).dispatch(
        _event(), timestamp=T1
    )
    assert first.state == WorkUnitState.TRIAGED.value

    second_store = SQLiteCheckpointStore(path)
    replay = AutonomousIntakeDispatcher(Orchestrator(), checkpoints=second_store).dispatch(
        _event(), timestamp=T2
    )
    assert replay == first
    assert second_store.last_heartbeat() == T2


def test_later_event_after_restart_never_resets_known_unit(tmp_path) -> None:
    path = tmp_path / "forge-checkpoints.sqlite3"
    store = SQLiteCheckpointStore(path)
    first = AutonomousIntakeDispatcher(Orchestrator(), checkpoints=store).dispatch(
        _event(), timestamp=T1
    )

    restarted = AutonomousIntakeDispatcher(Orchestrator(), checkpoints=SQLiteCheckpointStore(path))
    later = restarted.dispatch(_event(action="edited"), timestamp=T2)

    assert later.accepted is True
    assert later.unit_id == first.unit_id
    assert later.state == WorkUnitState.TRIAGED.value
    assert later.reason == "checkpoint-replay"


def test_checkpoint_database_never_serializes_raw_payload_body_or_actor(tmp_path) -> None:
    path = tmp_path / "forge-checkpoints.sqlite3"
    secret = "ghp_secret_value_that_must_not_be_written"
    store = SQLiteCheckpointStore(path)
    AutonomousIntakeDispatcher(Orchestrator(), checkpoints=store).dispatch(
        _event(raw={"token": secret}), timestamp=T1
    )

    contents = path.read_bytes()
    assert secret.encode() not in contents
    assert b"untrusted body with secret-like material" not in contents
    assert b"atulg4@example" not in contents


def test_heartbeat_is_durable_even_for_unmanaged_event(tmp_path) -> None:
    path = tmp_path / "forge-checkpoints.sqlite3"
    store = SQLiteCheckpointStore(path)
    event = WorkEvent(**{**_event().__dict__, "labels": ("bug",)})
    decision = AutonomousIntakeDispatcher(Orchestrator(), checkpoints=store).dispatch(
        event, timestamp=T1
    )
    assert decision.accepted is False
    assert SQLiteCheckpointStore(path).last_heartbeat() == T1


def test_checkpoint_record_is_idempotent_for_duplicate_event_key(tmp_path) -> None:
    store = SQLiteCheckpointStore(tmp_path / "forge-checkpoints.sqlite3")
    first = CheckpointRecord("event-1", "unit-1", "triaged", True, "accepted", T1)
    conflicting = CheckpointRecord("event-1", "unit-2", "failed", False, "conflict", T2)
    store.record(first)
    store.record(conflicting)
    assert store.get_event("event-1") == first


def test_checkpoint_schema_contains_only_control_plane_columns(tmp_path) -> None:
    path = tmp_path / "forge-checkpoints.sqlite3"
    SQLiteCheckpointStore(path)
    with sqlite3.connect(path) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(dispatcher_checkpoints)").fetchall()
        }
    assert columns == {"event_key", "unit_id", "state", "accepted", "reason", "timestamp"}
    assert not ({"raw", "body", "actor", "token", "secret"} & columns)


class _BrokenStore:
    def touch_heartbeat(self, timestamp: str) -> None:
        raise CheckpointError("disk unavailable")

    def last_heartbeat(self) -> str:
        return ""

    def get_event(self, event_key: str) -> CheckpointRecord | None:
        return None

    def get_unit(self, unit_id: str) -> CheckpointRecord | None:
        return None

    def record(self, checkpoint: CheckpointRecord) -> None:
        raise AssertionError("must fail during preflight before orchestration")


def test_checkpoint_unavailable_fails_closed_before_creating_work_unit() -> None:
    engine = Orchestrator()
    dispatcher = AutonomousIntakeDispatcher(engine, checkpoints=_BrokenStore())
    with pytest.raises(DispatchError, match="checkpoint preflight failed"):
        dispatcher.dispatch(_event(), timestamp=T1)

    with pytest.raises(Exception, match="unknown work unit"):
        engine.get("github:atulg4/example:issue:51")
