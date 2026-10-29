"""The Control Center's live delivery contract: ordered frames and resume.

The snapshot in :mod:`control_center` answers "what is true now". It cannot
answer "what changed since I last looked", which is what a console needs to
update a dense backlog table without re-rendering it, and what a reconnecting
browser needs after a dropped connection.

This module is that contract, expressed as data rather than as a server. It
defines the frames a push transport carries (server-sent events by default),
the cursor a client resumes from, and the rules that keep a reconnect honest:

- **Ordering is contiguous.** Sequence numbers are assigned by the log and have
  no gaps, so a client that receives 7 after 5 knows it missed 6 rather than
  silently rendering a hole.
- **Replay is idempotent.** A frame already applied is recognised by its event
  ID and changes nothing, so a redelivered event, a retried flush, or a
  double-subscribed client cannot double-count.
- **A cursor that cannot be served says so.** The retained window is bounded,
  so a client resuming from before it is told to reset and handed a full
  snapshot, rather than being quietly fast-forwarded past changes it never saw.
- **Staleness is labeled, never hidden.** Every reconnect response carries the
  freshness of the data behind it and the last-good instant, so a console can
  show "as of 09:12" instead of implying live truth.
- **No credentials on the wire.** Frame payloads are scanned for
  credential-shaped values and secret-shaped keys, and are bounded, so a
  browser subscribed to the stream can never be handed a provider token.

Nothing here opens a socket, holds a credential, or mutates anything: a
transport built on it stays a read-only view by construction.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .control_center import (
    ControlCenterError,
    ControlCenterSnapshot,
    secret_bearing_control_center_fields,
)
from .event_ledger import carries_credential_token

__all__ = [
    "DEFAULT_RETAINED_FRAMES",
    "MAX_PAYLOAD_KEYS",
    "MAX_RETAINED_FRAMES",
    "MAX_TEXT_LENGTH",
    "STREAM_SCHEMA_VERSION",
    "ChangeKind",
    "FrameKind",
    "ResetReason",
    "StreamCursor",
    "StreamFrame",
    "StreamLog",
    "build_reconnect_response",
    "load_stream_frame",
    "render_sse_frame",
    "render_sse_stream",
]

STREAM_SCHEMA_VERSION = 1

#: Frames retained for resume by default. A console that polls every few
#: seconds resumes from well inside this; one that was closed overnight is told
#: to reset instead of being handed a partial history.
DEFAULT_RETAINED_FRAMES = 1000
MAX_RETAINED_FRAMES = 100_000

#: Payload bounds. A frame carries references and short labels, never a diff,
#: a log, or a prompt, so a transport cannot be used to ship bulk content.
MAX_TEXT_LENGTH = 512
MAX_PAYLOAD_KEYS = 32

#: Freshness beyond which data behind a frame reads as stale.
DEFAULT_STALE_AFTER_SECONDS = 900

_SECRET_KEYS = frozenset(name.lower() for name in secret_bearing_control_center_fields())


class FrameKind(StrEnum):
    """What a frame tells the client to do."""

    #: Replace everything: the full snapshot document travels in the payload.
    SNAPSHOT = "snapshot"
    #: Apply one ordered change to the view the client already holds.
    CHANGE = "change"
    #: Nothing changed; the cursor and freshness are still current.
    HEARTBEAT = "heartbeat"
    #: The client's cursor cannot be served; it must take the snapshot that
    #: follows and discard what it held.
    RESET = "reset"


class ChangeKind(StrEnum):
    """What a change frame is about."""

    WORK_UNIT = "work-unit"
    INCIDENT = "incident"
    PROJECT_HEALTH = "project-health"
    TELEMETRY = "telemetry"
    BACKLOG = "backlog"


class ResetReason(StrEnum):
    """Why a cursor could not be resumed. Always stated, never implied."""

    #: The cursor points before the retained window: frames it never saw are
    #: already gone, so resuming would skip them silently.
    BEYOND_RETENTION = "cursor-beyond-retained-window"
    #: The cursor is ahead of the log — a restarted or replaced producer.
    AHEAD_OF_LOG = "cursor-ahead-of-log"
    #: The cursor's content digest does not match what the client reconciled
    #: against, so the deltas would apply to the wrong base.
    SNAPSHOT_MISMATCH = "cursor-snapshot-mismatch"
    #: No cursor was supplied: a first connection.
    NO_CURSOR = "no-cursor"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ControlCenterError(message)


def _text(value: Any, label: str, *, required: bool = False) -> str:
    if value is None:
        value = ""
    _require(isinstance(value, str), f"{label} must be a string")
    assert isinstance(value, str)
    value = value.strip()
    _require(not required or bool(value), f"{label} is required")
    _require(
        len(value) <= MAX_TEXT_LENGTH,
        f"{label} exceeds {MAX_TEXT_LENGTH} characters; a frame carries references, not content",
    )
    _require(
        not carries_credential_token(value),
        f"{label} carries a credential-shaped value (withheld); a stream frame "
        "reaches the browser and must never carry one",
    )
    return value


def _sequence(value: Any, label: str) -> int:
    _require(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0,
        f"{label} must be a non-negative integer",
    )
    assert isinstance(value, int)
    return value


def _scan_payload(value: Any, label: str, depth: int = 0) -> Any:
    """Validate one payload value: bounded, credential-free, JSON-shaped."""
    _require(depth <= 6, f"{label} nests too deeply for a stream frame")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        _require(
            value == value and value not in (float("inf"), float("-inf")),
            f"{label} must be finite",
        )
        return value
    if isinstance(value, str):
        return _text(value, label)
    if isinstance(value, Mapping):
        _require(len(value) <= MAX_PAYLOAD_KEYS, f"{label} declares too many keys")
        scanned: dict[str, Any] = {}
        for key, item in value.items():
            _require(isinstance(key, str), f"{label} keys must be strings")
            assert isinstance(key, str)
            _require(
                key.lower() not in _SECRET_KEYS,
                f"{label} must not carry a credential field: {key}",
            )
            scanned[key] = _scan_payload(item, f"{label}.{key}", depth + 1)
        return scanned
    if isinstance(value, (list, tuple)):
        _require(len(value) <= MAX_PAYLOAD_KEYS, f"{label} declares too many entries")
        return [
            _scan_payload(item, f"{label}[{index}]", depth + 1) for index, item in enumerate(value)
        ]
    raise ControlCenterError(f"{label} is not a JSON value a stream frame can carry")


def _mapping_document(value: Any, label: str) -> Mapping[str, Any]:
    _require(isinstance(value, Mapping), f"{label} must be an object")
    assert isinstance(value, Mapping)
    return value


def _known_keys(entry: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(entry) - allowed)
    _require(not unknown, f"{label} declares unknown keys: " + ", ".join(unknown))


def load_stream_frame(document: Any) -> StreamFrame:
    """Parse one stored frame fail-closed."""
    label = "stream frame"
    entry = _mapping_document(document, label)
    _known_keys(
        entry,
        frozenset(
            {
                "schemaVersion",
                "sequence",
                "eventId",
                "kind",
                "change",
                "projectId",
                "workUnitId",
                "resetReason",
                "occurredAt",
                "payload",
            }
        ),
        label,
    )
    version = entry.get("schemaVersion", STREAM_SCHEMA_VERSION)
    _require(
        version == STREAM_SCHEMA_VERSION,
        f"unsupported {label} schemaVersion: {version!r}",
    )

    def _enum(enum_type: Any, value: str) -> Any:
        try:
            return enum_type(value)
        except ValueError as error:
            raise ControlCenterError(f"{label}: unknown {enum_type.__name__}: {value}") from error

    change = _text(entry.get("change"), f"{label} change")
    reset_reason = _text(entry.get("resetReason"), f"{label} resetReason")
    payload = entry.get("payload") or {}
    _require(isinstance(payload, Mapping), f"{label} payload must be an object")
    assert isinstance(payload, Mapping)
    return StreamFrame(
        sequence=_sequence(entry.get("sequence"), f"{label} sequence"),
        event_id=_text(entry.get("eventId"), f"{label} eventId", required=True),
        kind=_enum(FrameKind, _text(entry.get("kind"), f"{label} kind", required=True)),
        occurred_at=_text(entry.get("occurredAt"), f"{label} occurredAt"),
        project_id=_text(entry.get("projectId"), f"{label} projectId"),
        change=_enum(ChangeKind, change) if change else None,
        work_unit_id=_text(entry.get("workUnitId"), f"{label} workUnitId"),
        reset_reason=_enum(ResetReason, reset_reason) if reset_reason else None,
        payload=dict(payload),
    )


@dataclass(frozen=True)
class StreamCursor:
    """Where a client has read to, and what it reconciled against.

    The digest matters as much as the sequence: a client holding content from a
    different snapshot cannot have deltas applied to it, however current its
    sequence number looks.
    """

    sequence: int
    snapshot_sha256: str = ""

    def __post_init__(self) -> None:
        _sequence(self.sequence, "cursor sequence")
        _require(
            not self.snapshot_sha256 or len(self.snapshot_sha256) == 64,
            "cursor snapshotSha256 must be a sha256 digest",
        )

    def encode(self) -> str:
        """The cursor as one opaque token, for an SSE ``Last-Event-ID``."""
        return (
            f"{self.sequence}-{self.snapshot_sha256}"
            if self.snapshot_sha256
            else str(self.sequence)
        )

    @classmethod
    def decode(cls, token: str) -> StreamCursor:
        """Parse a resume token fail-closed.

        A malformed token is refused rather than read as sequence zero: zero
        would silently replay the whole retained window as if the client were
        new, and a client that *is* new says so by sending nothing at all.
        """
        _require(isinstance(token, str), "resume token must be a string")
        raw = token.strip()
        _require(bool(raw), "resume token is empty")
        sequence, _, digest = raw.partition("-")
        _require(sequence.isdigit(), f"resume token is malformed: {raw!r}")
        return cls(sequence=int(sequence), snapshot_sha256=digest)

    def as_dict(self) -> dict[str, Any]:
        """The cursor as a document.

        The encoded form is published as ``lastEventId`` — what SSE calls it,
        and what the client echoes back. Naming it ``token`` would collide with
        every credential scanner, including this module's own, for a value that
        is not a credential at all.
        """
        return {
            "sequence": self.sequence,
            "snapshotSha256": self.snapshot_sha256,
            "lastEventId": self.encode(),
        }


@dataclass(frozen=True)
class StreamFrame:
    """One immutable, ordered frame a push transport carries."""

    sequence: int
    event_id: str
    kind: FrameKind
    occurred_at: str
    project_id: str = ""
    change: ChangeKind | None = None
    work_unit_id: str = ""
    reset_reason: ResetReason | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = STREAM_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _sequence(self.sequence, "frame sequence")
        object.__setattr__(self, "event_id", _text(self.event_id, "frame eventId", required=True))
        object.__setattr__(self, "occurred_at", _text(self.occurred_at, "frame occurredAt"))
        object.__setattr__(self, "project_id", _text(self.project_id, "frame projectId"))
        object.__setattr__(self, "work_unit_id", _text(self.work_unit_id, "frame workUnitId"))
        object.__setattr__(self, "payload", _scan_payload(dict(self.payload), "frame payload"))
        if self.kind is FrameKind.CHANGE:
            _require(
                self.change is not None,
                "a change frame must say what changed",
            )
            _require(
                bool(self.project_id),
                "a change frame must name the project it belongs to, or a "
                "project-scoped console cannot route it",
            )
        if self.kind is FrameKind.RESET:
            _require(
                self.reset_reason is not None,
                "a reset frame must state why the cursor could not be resumed",
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "sequence": self.sequence,
            "eventId": self.event_id,
            "kind": self.kind.value,
            "change": self.change.value if self.change else "",
            "projectId": self.project_id,
            "workUnitId": self.work_unit_id,
            "resetReason": self.reset_reason.value if self.reset_reason else "",
            "occurredAt": self.occurred_at,
            "payload": dict(self.payload),
        }


def render_sse_frame(frame: StreamFrame) -> str:
    """One frame as a server-sent-events block.

    The ``id`` line is the resume token the browser echoes back as
    ``Last-Event-ID``, so reconnect needs no client-side bookkeeping. Data is
    emitted as a single line of JSON because a raw newline would terminate the
    field and split one frame into two.
    """
    data = json.dumps(frame.as_dict(), sort_keys=True, separators=(",", ":"))
    _require("\n" not in data and "\r" not in data, "a rendered frame must not contain a newline")
    token = StreamCursor(
        sequence=frame.sequence,
        snapshot_sha256=str(frame.payload.get("snapshotSha256", "") or ""),
    ).encode()
    return f"id: {token}\nevent: {frame.kind.value}\ndata: {data}\n\n"


def render_sse_stream(frames: Iterable[StreamFrame]) -> str:
    return "".join(render_sse_frame(frame) for frame in frames)


class StreamLog:
    """A bounded, append-only, contiguous log of frames clients resume from.

    The log assigns every sequence number itself, which is what makes the
    numbering contiguous and therefore gap-detectable. Retention is bounded, so
    the log also knows which cursors it can no longer serve and says so rather
    than skipping history.
    """

    def __init__(self, *, retain: int = DEFAULT_RETAINED_FRAMES) -> None:
        _require(
            isinstance(retain, int)
            and not isinstance(retain, bool)
            and 1 <= retain <= MAX_RETAINED_FRAMES,
            f"retain must be between 1 and {MAX_RETAINED_FRAMES}",
        )
        self._retain = retain
        self._frames: list[StreamFrame] = []
        self._event_ids: dict[str, StreamFrame] = {}
        self._assigned = 0
        self._dropped = 0

    @property
    def retain(self) -> int:
        return self._retain

    @property
    def frames(self) -> tuple[StreamFrame, ...]:
        return tuple(self._frames)

    def __len__(self) -> int:
        return len(self._frames)

    @property
    def next_sequence(self) -> int:
        return self._assigned + 1

    @property
    def cursor(self) -> StreamCursor:
        """The cursor a client is at once it has applied every retained frame."""
        return StreamCursor(sequence=self._assigned)

    @property
    def earliest_resumable(self) -> int:
        """The oldest sequence a resume can still be served from.

        A client at this sequence has already applied it and needs what comes
        after, so anything below it has been dropped from the window.
        """
        return self._frames[0].sequence - 1 if self._frames else self._assigned

    @property
    def dropped(self) -> int:
        return self._dropped

    def record(
        self,
        kind: FrameKind | str,
        *,
        event_id: str,
        occurred_at: str = "",
        project_id: str = "",
        change: ChangeKind | str | None = None,
        work_unit_id: str = "",
        reset_reason: ResetReason | str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> StreamFrame:
        """Append one frame, assigning its sequence. Replay is a no-op."""
        existing = self._event_ids.get(event_id.strip())
        if existing is not None:
            return existing
        frame = StreamFrame(
            sequence=self.next_sequence,
            event_id=event_id,
            kind=FrameKind(kind),
            occurred_at=occurred_at,
            project_id=project_id,
            change=ChangeKind(change) if change is not None else None,
            work_unit_id=work_unit_id,
            reset_reason=ResetReason(reset_reason) if reset_reason is not None else None,
            payload=payload or {},
        )
        return self.append(frame)

    def append(self, frame: StreamFrame) -> StreamFrame:
        """Append a frame that already carries its sequence.

        Used when rehydrating a log. The sequence must be exactly the next one:
        accepting a gap would make the numbering non-contiguous and defeat the
        gap detection every client relies on.
        """
        existing = self._event_ids.get(frame.event_id)
        if existing is not None:
            _require(
                existing.as_dict() == frame.as_dict(),
                f"frame {frame.event_id} is already recorded with a different payload",
            )
            return existing
        _require(
            frame.sequence == self.next_sequence,
            f"frame {frame.event_id} has sequence {frame.sequence}, but the log's "
            f"next sequence is {self.next_sequence}; a stream must stay contiguous",
        )
        self._frames.append(frame)
        self._event_ids[frame.event_id] = frame
        self._assigned = frame.sequence
        while len(self._frames) > self._retain:
            retired = self._frames.pop(0)
            self._event_ids.pop(retired.event_id, None)
            self._dropped += 1
        return frame

    def extend(self, frames: Iterable[StreamFrame]) -> tuple[StreamFrame, ...]:
        return tuple(self.append(frame) for frame in frames)

    def since(
        self,
        cursor: StreamCursor,
        *,
        project_ids: Sequence[str] | None = None,
    ) -> tuple[StreamFrame, ...]:
        """Frames after a cursor, in order, optionally scoped to projects.

        Project scoping keeps a project-scoped console from seeing another
        project's frames. Frames with no project (a heartbeat, a snapshot)
        belong to every scope.
        """
        _require(self.can_resume(cursor), "cursor cannot be resumed; take a snapshot instead")
        scope = None if project_ids is None else frozenset(project_ids)
        return tuple(
            frame
            for frame in self._frames
            if frame.sequence > cursor.sequence
            and (scope is None or not frame.project_id or frame.project_id in scope)
        )

    def can_resume(self, cursor: StreamCursor) -> bool:
        return self.resume_problem(cursor) is None

    def resume_problem(self, cursor: StreamCursor) -> ResetReason | None:
        """Why this cursor cannot be resumed, or ``None`` when it can."""
        if cursor.sequence > self._assigned:
            return ResetReason.AHEAD_OF_LOG
        if cursor.sequence < self.earliest_resumable:
            return ResetReason.BEYOND_RETENTION
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": STREAM_SCHEMA_VERSION,
            "retain": self._retain,
            "assigned": self._assigned,
            "dropped": self._dropped,
            "earliestResumable": self.earliest_resumable,
            "cursor": self.cursor.as_dict(),
            "frames": [frame.as_dict() for frame in self._frames],
        }

    @classmethod
    def from_dict(cls, document: Any) -> StreamLog:
        """Rehydrate a log fail-closed, preserving its sequence numbering.

        The retained frames are replayed in order, so contiguity is re-checked
        on the way in; ``dropped`` and the assigned high-water mark are restored
        from the document, because a log whose window has already slid cannot
        re-derive them from the frames it still holds.
        """
        entry = _mapping_document(document, "stream log")
        _known_keys(
            entry,
            frozenset(
                {
                    "schemaVersion",
                    "retain",
                    "assigned",
                    "dropped",
                    "earliestResumable",
                    "cursor",
                    "frames",
                }
            ),
            "stream log",
        )
        version = entry.get("schemaVersion")
        _require(
            version == STREAM_SCHEMA_VERSION,
            f"unsupported stream log schemaVersion: {version!r}",
        )
        retain = entry.get("retain", DEFAULT_RETAINED_FRAMES)
        log = cls(retain=retain)
        raw_frames = entry.get("frames", [])
        _require(isinstance(raw_frames, list), "stream log frames must be an array")
        assert isinstance(raw_frames, list)
        frames = [load_stream_frame(item) for item in raw_frames]
        if frames:
            # A slid window starts above 1: seed the counter so the first
            # replayed frame lands on its own recorded sequence.
            log._assigned = frames[0].sequence - 1
        log.extend(frames)
        assigned = entry.get("assigned", log._assigned)
        _require(
            isinstance(assigned, int)
            and not isinstance(assigned, bool)
            and assigned >= log._assigned,
            "stream log assigned must be an integer at or above its last frame",
        )
        log._assigned = assigned
        dropped = entry.get("dropped", 0)
        _require(
            isinstance(dropped, int) and not isinstance(dropped, bool) and dropped >= 0,
            "stream log dropped must be a non-negative integer",
        )
        log._dropped = dropped
        return log


def _freshness(
    *,
    observed_at: str,
    age_seconds: int | None,
    stale_after_seconds: int,
) -> dict[str, Any]:
    """How much to trust the data behind a frame, with its last-good instant."""
    _require(
        isinstance(stale_after_seconds, int)
        and not isinstance(stale_after_seconds, bool)
        and stale_after_seconds > 0,
        "staleAfterSeconds must be a positive integer",
    )
    observed = _text(observed_at, "freshness observedAt")
    if age_seconds is None:
        return {
            "status": "unknown",
            "lastGoodAt": observed or None,
            "ageSeconds": None,
            "staleAfterSeconds": stale_after_seconds,
            "reason": "no age was observed, so freshness cannot be asserted",
        }
    age = _sequence(age_seconds, "freshness ageSeconds")
    _require(
        bool(observed),
        "observed freshness requires the instant the data was last good",
    )
    return {
        "status": "stale" if age > stale_after_seconds else "observed",
        "lastGoodAt": observed,
        "ageSeconds": age,
        "staleAfterSeconds": stale_after_seconds,
        "reason": "",
    }


def build_reconnect_response(
    log: StreamLog,
    snapshot: ControlCenterSnapshot,
    *,
    cursor: StreamCursor | None = None,
    observed_at: str = "",
    age_seconds: int | None = None,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
    project_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """What a transport sends a client that has just (re)connected.

    Three outcomes, and the client is always told which:

    - **No cursor** — a first connection: one snapshot frame, and the cursor to
      resume from next time.
    - **A resumable cursor** — only the change frames after it.
    - **A cursor that cannot be served** — a reset frame stating why, then a
      snapshot frame. This is the fallback for missed events: a client that was
      away longer than the retained window is never fast-forwarded past
      changes it did not see.
    """
    snapshot_document = snapshot.as_document()
    digest = str(snapshot_document.get("snapshotSha256", ""))
    freshness = _freshness(
        observed_at=observed_at or snapshot.generated_at,
        age_seconds=age_seconds,
        stale_after_seconds=stale_after_seconds,
    )

    reason = ResetReason.NO_CURSOR if cursor is None else log.resume_problem(cursor)
    # The client reconciled against different content, yet no frame explains the
    # difference: applying nothing would leave it showing a view this snapshot
    # contradicts, so it must take the snapshot instead.
    if (
        reason is None
        and cursor is not None
        and cursor.snapshot_sha256
        and cursor.snapshot_sha256 != digest
        and not log.since(cursor, project_ids=project_ids)
    ):
        reason = ResetReason.SNAPSHOT_MISMATCH

    if reason is None:
        assert cursor is not None
        frames = log.since(cursor, project_ids=project_ids)
        return {
            "schemaVersion": STREAM_SCHEMA_VERSION,
            "mode": "resumed",
            "resumedFrom": cursor.as_dict(),
            "cursor": StreamCursor(sequence=log.cursor.sequence, snapshot_sha256=digest).as_dict(),
            "freshness": freshness,
            "frames": [frame.as_dict() for frame in frames],
            "snapshot": None,
        }

    snapshot_frame = StreamFrame(
        sequence=log.cursor.sequence,
        event_id=f"snapshot-{digest[:16] or 'empty'}-{log.cursor.sequence}",
        kind=FrameKind.SNAPSHOT,
        occurred_at=snapshot.generated_at,
        payload={"snapshotSha256": digest},
    )
    frames = []
    if reason is not ResetReason.NO_CURSOR:
        frames.append(
            StreamFrame(
                sequence=log.cursor.sequence,
                event_id=f"reset-{reason.value}-{log.cursor.sequence}",
                kind=FrameKind.RESET,
                occurred_at=snapshot.generated_at,
                reset_reason=reason,
                payload={"earliestResumable": log.earliest_resumable, "droppedFrames": log.dropped},
            )
        )
    frames.append(snapshot_frame)
    return {
        "schemaVersion": STREAM_SCHEMA_VERSION,
        "mode": "snapshot",
        "resetReason": reason.value,
        "resumedFrom": cursor.as_dict() if cursor else None,
        "cursor": StreamCursor(sequence=log.cursor.sequence, snapshot_sha256=digest).as_dict(),
        "freshness": freshness,
        "frames": [frame.as_dict() for frame in frames],
        "snapshot": snapshot_document,
    }
