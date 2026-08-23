"""Durable, restart-safe checkpoints for autonomous Forge dispatch.

The store intentionally persists only normalized control-plane metadata. Raw provider
payloads, issue bodies, credentials, and model secrets are never written here.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

__all__ = [
    "CheckpointError",
    "CheckpointRecord",
    "CheckpointStore",
    "SQLiteCheckpointStore",
]


class CheckpointError(RuntimeError):
    """Raised when dispatcher checkpoint persistence cannot be trusted."""


@dataclass(frozen=True)
class CheckpointRecord:
    event_key: str
    unit_id: str
    state: str
    accepted: bool
    reason: str
    timestamp: str


class CheckpointStore(Protocol):
    """Minimal persistence contract required by the intake dispatcher."""

    def touch_heartbeat(self, timestamp: str) -> None: ...

    def last_heartbeat(self) -> str: ...

    def get_event(self, event_key: str) -> CheckpointRecord | None: ...

    def get_unit(self, unit_id: str) -> CheckpointRecord | None: ...

    def record(self, checkpoint: CheckpointRecord) -> None: ...


class SQLiteCheckpointStore:
    """SQLite-backed dispatcher checkpoint journal.

    SQLite is used deliberately: it is in the Python standard library, supports
    transactional upserts, and survives process restarts without introducing a
    network credential or external service dependency.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not str(self.path).strip():
            raise CheckpointError("checkpoint path is required")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        try:
            with self._connect() as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS dispatcher_checkpoints (
                        event_key TEXT PRIMARY KEY,
                        unit_id TEXT NOT NULL,
                        state TEXT NOT NULL,
                        accepted INTEGER NOT NULL CHECK (accepted IN (0, 1)),
                        reason TEXT NOT NULL,
                        timestamp TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS dispatcher_checkpoints_unit_idx
                    ON dispatcher_checkpoints(unit_id, timestamp DESC)
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS dispatcher_metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                    """
                )
        except (OSError, sqlite3.Error) as exc:
            raise CheckpointError(f"unable to initialize dispatcher checkpoints: {exc}") from exc

    def touch_heartbeat(self, timestamp: str) -> None:
        if not timestamp.strip():
            raise CheckpointError("heartbeat timestamp is required")
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO dispatcher_metadata(key, value)
                    VALUES('last_heartbeat', ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (timestamp,),
                )
        except sqlite3.Error as exc:
            raise CheckpointError(f"unable to persist dispatcher heartbeat: {exc}") from exc

    def last_heartbeat(self) -> str:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT value FROM dispatcher_metadata WHERE key = 'last_heartbeat'"
                ).fetchone()
        except sqlite3.Error as exc:
            raise CheckpointError(f"unable to read dispatcher heartbeat: {exc}") from exc
        return "" if row is None else str(row["value"])

    @staticmethod
    def _row_to_record(row: sqlite3.Row | None) -> CheckpointRecord | None:
        if row is None:
            return None
        return CheckpointRecord(
            event_key=str(row["event_key"]),
            unit_id=str(row["unit_id"]),
            state=str(row["state"]),
            accepted=bool(row["accepted"]),
            reason=str(row["reason"]),
            timestamp=str(row["timestamp"]),
        )

    def get_event(self, event_key: str) -> CheckpointRecord | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM dispatcher_checkpoints WHERE event_key = ?",
                    (event_key,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise CheckpointError(f"unable to read dispatcher event checkpoint: {exc}") from exc
        return self._row_to_record(row)

    def get_unit(self, unit_id: str) -> CheckpointRecord | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT * FROM dispatcher_checkpoints
                    WHERE unit_id = ?
                    ORDER BY timestamp DESC, rowid DESC
                    LIMIT 1
                    """,
                    (unit_id,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise CheckpointError(f"unable to read dispatcher unit checkpoint: {exc}") from exc
        return self._row_to_record(row)

    def record(self, checkpoint: CheckpointRecord) -> None:
        if not checkpoint.event_key.strip() or not checkpoint.timestamp.strip():
            raise CheckpointError("checkpoint event key and timestamp are required")
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO dispatcher_checkpoints(
                        event_key, unit_id, state, accepted, reason, timestamp
                    ) VALUES(?, ?, ?, ?, ?, ?)
                    ON CONFLICT(event_key) DO NOTHING
                    """,
                    (
                        checkpoint.event_key,
                        checkpoint.unit_id,
                        checkpoint.state,
                        int(checkpoint.accepted),
                        checkpoint.reason,
                        checkpoint.timestamp,
                    ),
                )
        except sqlite3.Error as exc:
            raise CheckpointError(f"unable to persist dispatcher checkpoint: {exc}") from exc
