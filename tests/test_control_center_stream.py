"""Tests for the Control Center's live delivery contract."""

from __future__ import annotations

import json

import pytest

from agentic_sdlc.control_center import (
    ControlCenterError,
    build_control_center_snapshot,
)
from agentic_sdlc.control_center_stream import (
    DEFAULT_RETAINED_FRAMES,
    MAX_PAYLOAD_KEYS,
    MAX_TEXT_LENGTH,
    STREAM_SCHEMA_VERSION,
    ChangeKind,
    FrameKind,
    ResetReason,
    StreamCursor,
    StreamFrame,
    StreamLog,
    build_reconnect_response,
    render_sse_frame,
    render_sse_stream,
)
from agentic_sdlc.event_ledger import EventLedger, load_lifecycle_event
from agentic_sdlc.project_registry import load_project_registry

NOW = "2026-10-29T09:00:00Z"

REGISTRY = load_project_registry(
    {
        "schemaVersion": 1,
        "projects": [
            {
                "projectId": project_id,
                "displayName": project_id.title(),
                "repositories": [{"provider": "github", "identifier": f"acme/{project_id}"}],
            }
            for project_id in ("forge", "atlas")
        ],
    }
)


def _event(**overrides):
    event = {
        "schemaVersion": 1,
        "eventId": overrides.pop("eventId", "e-1"),
        "idempotencyKey": overrides.pop("idempotencyKey", "k-1"),
        "workUnit": {"projectId": "forge", "workUnitId": "wu-1"},
        "stage": "intake",
        "state": "intake",
        "occurredAt": NOW,
        "actor": {"name": "planner", "kind": "agent"},
        "provenance": {"source": "test"},
    }
    event.update(overrides)
    return event


def _snapshot(*, generated_at: str = NOW, with_unit: bool = True):
    projections = ()
    if with_unit:
        ledger = EventLedger(registry=REGISTRY)
        ledger.append(load_lifecycle_event(_event()))
        projections = ledger.projections()
    return build_control_center_snapshot(
        generated_at=generated_at, registry=REGISTRY, projections=projections
    )


def _log(*, retain: int = DEFAULT_RETAINED_FRAMES, frames: int = 0, project: str = "forge"):
    log = StreamLog(retain=retain)
    for index in range(1, frames + 1):
        log.record(
            FrameKind.CHANGE,
            event_id=f"f-{index}",
            project_id=project,
            change=ChangeKind.WORK_UNIT,
            occurred_at=NOW,
            work_unit_id="wu-1",
            payload={"state": "implementing"},
        )
    return log


# --- frames ----------------------------------------------------------------


def test_frame_serializes_every_declared_field() -> None:
    frame = StreamFrame(
        sequence=4,
        event_id="f-4",
        kind=FrameKind.CHANGE,
        occurred_at=NOW,
        project_id="forge",
        change=ChangeKind.WORK_UNIT,
        work_unit_id="wu-1",
        payload={"state": "reviewing", "repairCycles": 1},
    )

    document = frame.as_dict()

    assert document == {
        "schemaVersion": STREAM_SCHEMA_VERSION,
        "sequence": 4,
        "eventId": "f-4",
        "kind": "change",
        "change": "work-unit",
        "projectId": "forge",
        "workUnitId": "wu-1",
        "resetReason": "",
        "occurredAt": NOW,
        "payload": {"state": "reviewing", "repairCycles": 1},
    }


def test_a_change_frame_must_say_what_changed_and_where() -> None:
    with pytest.raises(ControlCenterError, match="must say what changed"):
        StreamFrame(sequence=1, event_id="f-1", kind=FrameKind.CHANGE, occurred_at=NOW)
    with pytest.raises(ControlCenterError, match="must name the project"):
        StreamFrame(
            sequence=1,
            event_id="f-1",
            kind=FrameKind.CHANGE,
            occurred_at=NOW,
            change=ChangeKind.WORK_UNIT,
        )
    with pytest.raises(ControlCenterError, match="must state why"):
        StreamFrame(sequence=1, event_id="f-1", kind=FrameKind.RESET, occurred_at=NOW)
    with pytest.raises(ControlCenterError, match="eventId is required"):
        StreamFrame(sequence=1, event_id="", kind=FrameKind.HEARTBEAT, occurred_at=NOW)


# --- ordering and idempotency ----------------------------------------------


def test_the_log_assigns_contiguous_sequences() -> None:
    log = _log(frames=3)

    assert [frame.sequence for frame in log.frames] == [1, 2, 3]
    assert log.next_sequence == 4
    assert log.cursor.sequence == 3


def test_replaying_a_frame_is_a_no_op() -> None:
    log = _log(frames=2)
    before = log.as_dict()

    replayed = log.record(
        FrameKind.CHANGE,
        event_id="f-1",
        project_id="forge",
        change=ChangeKind.WORK_UNIT,
        occurred_at=NOW,
        work_unit_id="wu-1",
        payload={"state": "implementing"},
    )

    assert replayed.sequence == 1
    assert len(log) == 2
    assert log.as_dict() == before


def test_reusing_an_event_id_with_a_different_payload_fails_closed() -> None:
    log = _log(frames=1)

    with pytest.raises(ControlCenterError, match="already recorded with a different payload"):
        log.append(
            StreamFrame(
                sequence=1,
                event_id="f-1",
                kind=FrameKind.CHANGE,
                occurred_at=NOW,
                project_id="forge",
                change=ChangeKind.INCIDENT,
            )
        )


def test_a_gap_in_the_sequence_fails_closed() -> None:
    """Accepting a gap would defeat the gap detection clients rely on."""
    log = _log(frames=1)

    with pytest.raises(ControlCenterError, match="must stay contiguous"):
        log.append(
            StreamFrame(
                sequence=7,
                event_id="f-7",
                kind=FrameKind.CHANGE,
                occurred_at=NOW,
                project_id="forge",
                change=ChangeKind.WORK_UNIT,
            )
        )


def test_retention_is_bounded_and_says_what_it_dropped() -> None:
    log = _log(retain=3, frames=5)

    assert len(log) == 3
    assert [frame.sequence for frame in log.frames] == [3, 4, 5]
    assert log.dropped == 2
    # A client at sequence 2 needs frame 3, which is still here.
    assert log.earliest_resumable == 2
    assert log.can_resume(StreamCursor(sequence=2))
    assert not log.can_resume(StreamCursor(sequence=1))


def test_the_log_rejects_an_unusable_retention_bound() -> None:
    with pytest.raises(ControlCenterError, match="retain must be between"):
        StreamLog(retain=0)


# --- cursors ----------------------------------------------------------------


def test_cursor_tokens_round_trip() -> None:
    cursor = StreamCursor(sequence=12, snapshot_sha256="a" * 64)

    assert StreamCursor.decode(cursor.encode()) == cursor
    assert StreamCursor.decode("7") == StreamCursor(sequence=7)
    assert cursor.as_dict()["lastEventId"] == f"12-{'a' * 64}"


def test_a_malformed_resume_token_fails_closed_rather_than_reading_as_zero() -> None:
    """Sequence zero would replay the whole window as if the client were new."""
    for token in ("", "   ", "abc", "-1", "1.2"):
        with pytest.raises(ControlCenterError):
            StreamCursor.decode(token)


def test_a_cursor_digest_must_be_a_digest() -> None:
    with pytest.raises(ControlCenterError, match="must be a sha256 digest"):
        StreamCursor(sequence=1, snapshot_sha256="short")


# --- reconnect --------------------------------------------------------------


def test_a_first_connection_is_handed_the_snapshot() -> None:
    log = _log(frames=2)

    response = build_reconnect_response(log, _snapshot(), observed_at=NOW, age_seconds=5)

    assert response["mode"] == "snapshot"
    assert response["resetReason"] == ResetReason.NO_CURSOR.value
    assert response["resumedFrom"] is None
    assert response["snapshot"]["snapshotSha256"]
    # No reset frame: nothing was lost, the client simply had nothing yet.
    assert [frame["kind"] for frame in response["frames"]] == ["snapshot"]
    assert response["cursor"]["sequence"] == 2


def test_a_resumable_cursor_receives_only_what_followed_it() -> None:
    log = _log(frames=2)
    snapshot = _snapshot()
    first = build_reconnect_response(log, snapshot, observed_at=NOW, age_seconds=1)
    cursor = StreamCursor.decode(first["cursor"]["lastEventId"])

    log.record(
        FrameKind.CHANGE,
        event_id="f-3",
        project_id="forge",
        change=ChangeKind.WORK_UNIT,
        occurred_at=NOW,
        payload={"state": "reviewing"},
    )
    resumed = build_reconnect_response(log, snapshot, cursor=cursor, observed_at=NOW, age_seconds=1)

    assert resumed["mode"] == "resumed"
    assert resumed["snapshot"] is None
    assert [frame["eventId"] for frame in resumed["frames"]] == ["f-3"]
    assert resumed["resumedFrom"]["sequence"] == 2
    assert resumed["cursor"]["sequence"] == 3


def test_a_cursor_beyond_retention_resets_and_falls_back_to_the_snapshot() -> None:
    """The fallback for missed events: never fast-forward past unseen changes."""
    log = _log(retain=2, frames=5)

    response = build_reconnect_response(
        log, _snapshot(), cursor=StreamCursor(sequence=1), observed_at=NOW, age_seconds=5
    )

    assert response["mode"] == "snapshot"
    assert response["resetReason"] == ResetReason.BEYOND_RETENTION.value
    assert [frame["kind"] for frame in response["frames"]] == ["reset", "snapshot"]
    reset = response["frames"][0]
    assert reset["resetReason"] == ResetReason.BEYOND_RETENTION.value
    assert reset["payload"]["droppedFrames"] == 3
    assert reset["payload"]["earliestResumable"] == log.earliest_resumable
    assert response["snapshot"] is not None


def test_a_cursor_ahead_of_the_log_resets() -> None:
    log = _log(frames=2)

    response = build_reconnect_response(
        log, _snapshot(), cursor=StreamCursor(sequence=99), observed_at=NOW, age_seconds=5
    )

    assert response["resetReason"] == ResetReason.AHEAD_OF_LOG.value
    assert response["snapshot"] is not None


def test_a_cursor_reconciled_against_other_content_resets() -> None:
    """Deltas cannot be applied to a base the snapshot contradicts."""
    log = _log(frames=2)
    snapshot = _snapshot()

    response = build_reconnect_response(
        log,
        snapshot,
        cursor=StreamCursor(sequence=2, snapshot_sha256="b" * 64),
        observed_at=NOW,
        age_seconds=5,
    )

    assert response["resetReason"] == ResetReason.SNAPSHOT_MISMATCH.value
    assert response["snapshot"] is not None


def test_a_stale_digest_explained_by_pending_frames_still_resumes() -> None:
    """A digest differs precisely because changes followed; those explain it."""
    log = _log(frames=2)
    snapshot = _snapshot()
    cursor = StreamCursor(sequence=1, snapshot_sha256="b" * 64)

    response = build_reconnect_response(
        log, snapshot, cursor=cursor, observed_at=NOW, age_seconds=5
    )

    assert response["mode"] == "resumed"
    assert [frame["eventId"] for frame in response["frames"]] == ["f-2"]


# --- project isolation ------------------------------------------------------


def test_frames_are_scoped_per_project() -> None:
    log = StreamLog()
    for project in ("forge", "atlas"):
        log.record(
            FrameKind.CHANGE,
            event_id=f"f-{project}",
            project_id=project,
            change=ChangeKind.WORK_UNIT,
            occurred_at=NOW,
        )
    log.record(FrameKind.HEARTBEAT, event_id="beat", occurred_at=NOW)

    scoped = log.since(StreamCursor(sequence=0), project_ids=("forge",))

    assert [frame.event_id for frame in scoped] == ["f-forge", "beat"]
    assert "f-atlas" not in {frame.event_id for frame in scoped}
    response = build_reconnect_response(
        log,
        _snapshot(),
        cursor=StreamCursor(sequence=0),
        observed_at=NOW,
        age_seconds=1,
        project_ids=("atlas",),
    )
    assert [frame["eventId"] for frame in response["frames"]] == ["f-atlas", "beat"]


def test_resuming_an_unservable_cursor_through_since_fails_closed() -> None:
    log = _log(retain=2, frames=4)

    with pytest.raises(ControlCenterError, match="take a snapshot instead"):
        log.since(StreamCursor(sequence=1))


# --- freshness --------------------------------------------------------------


def test_freshness_is_labeled_with_its_last_good_instant() -> None:
    log = _log(frames=1)
    snapshot = _snapshot()

    fresh = build_reconnect_response(log, snapshot, observed_at=NOW, age_seconds=10)
    stale = build_reconnect_response(
        log, snapshot, observed_at=NOW, age_seconds=5000, stale_after_seconds=900
    )
    unknown = build_reconnect_response(log, snapshot, observed_at=NOW)

    assert fresh["freshness"]["status"] == "observed"
    assert fresh["freshness"]["lastGoodAt"] == NOW
    assert stale["freshness"]["status"] == "stale"
    assert stale["freshness"]["ageSeconds"] == 5000
    assert unknown["freshness"]["status"] == "unknown"
    assert unknown["freshness"]["ageSeconds"] is None
    assert unknown["freshness"]["reason"]


def test_an_unusable_staleness_bound_fails_closed() -> None:
    with pytest.raises(ControlCenterError, match="positive integer"):
        build_reconnect_response(
            _log(), _snapshot(), observed_at=NOW, age_seconds=1, stale_after_seconds=0
        )


# --- the wire form ----------------------------------------------------------


def test_sse_rendering_carries_the_resume_token_on_the_id_line() -> None:
    log = _log(frames=1)
    frame = log.frames[0]

    rendered = render_sse_frame(frame)

    assert rendered.startswith("id: 1\nevent: change\ndata: {")
    assert rendered.endswith("\n\n")
    # One data line: a raw newline would split one frame into two.
    body = rendered.split("data: ", 1)[1].rstrip("\n")
    assert "\n" not in body
    assert json.loads(body)["eventId"] == "f-1"


def test_a_snapshot_frame_echoes_the_digest_in_its_resume_token() -> None:
    log = _log(frames=1)
    response = build_reconnect_response(log, _snapshot(), observed_at=NOW, age_seconds=1)
    digest = response["snapshot"]["snapshotSha256"]

    frame = StreamFrame(
        sequence=1,
        event_id="snap",
        kind=FrameKind.SNAPSHOT,
        occurred_at=NOW,
        payload={"snapshotSha256": digest},
    )

    assert render_sse_frame(frame).startswith(f"id: 1-{digest}\nevent: snapshot\n")


def test_rendering_a_stream_concatenates_blocks() -> None:
    log = _log(frames=2)

    rendered = render_sse_stream(log.frames)

    assert rendered.count("\n\n") == 2
    assert rendered.count("event: change") == 2


# --- credentials and bounds -------------------------------------------------


def test_a_frame_can_never_carry_a_credential_to_the_browser() -> None:
    log = StreamLog()

    with pytest.raises(ControlCenterError, match="must not carry a credential field: token"):
        log.record(
            FrameKind.CHANGE,
            event_id="bad-key",
            project_id="forge",
            change=ChangeKind.WORK_UNIT,
            payload={"token": "value"},
        )
    with pytest.raises(ControlCenterError, match="credential-shaped value"):
        log.record(
            FrameKind.CHANGE,
            event_id="bad-value",
            project_id="forge",
            change=ChangeKind.WORK_UNIT,
            payload={"url": "https://x.example?t=ghp_0123456789abcdefghij"},
        )
    # Percent-encoded shapes are peeled before the scan.
    with pytest.raises(ControlCenterError, match="credential-shaped value"):
        log.record(
            FrameKind.CHANGE,
            event_id="bad-encoded",
            project_id="forge",
            change=ChangeKind.WORK_UNIT,
            payload={"url": "https://x.example?t=ghp%5F0123456789abcdefghij"},
        )
    assert len(log) == 0


def test_a_rendered_stream_contains_no_credential() -> None:
    log = _log(frames=2)
    rendered = render_sse_stream(log.frames).lower()

    for forbidden in ("ghp_", "authorization", "password", "secret"):
        assert forbidden not in rendered


def test_frame_payloads_are_bounded() -> None:
    log = StreamLog()
    common = {
        "project_id": "forge",
        "change": ChangeKind.WORK_UNIT,
    }

    with pytest.raises(ControlCenterError, match="exceeds"):
        log.record(
            FrameKind.CHANGE,
            event_id="long",
            payload={"a": "x" * (MAX_TEXT_LENGTH + 1)},
            **common,
        )
    with pytest.raises(ControlCenterError, match="too many keys"):
        log.record(
            FrameKind.CHANGE,
            event_id="wide",
            payload={str(index): index for index in range(MAX_PAYLOAD_KEYS + 1)},
            **common,
        )
    deep: dict = {"a": 1}
    for _ in range(10):
        deep = {"a": deep}
    with pytest.raises(ControlCenterError, match="nests too deeply"):
        log.record(FrameKind.CHANGE, event_id="deep", payload=deep, **common)
    with pytest.raises(ControlCenterError, match="not a JSON value"):
        log.record(FrameKind.CHANGE, event_id="odd", payload={"a": {1, 2}}, **common)


# --- scale ------------------------------------------------------------------


def test_a_large_stream_stays_bounded_and_resumable() -> None:
    log = _log(retain=1000, frames=5000)

    assert len(log) == 1000
    assert log.dropped == 4000
    assert log.cursor.sequence == 5000
    # A client one frame behind still resumes.
    assert len(log.since(StreamCursor(sequence=4999))) == 1
    # The boundary is exact: sequences 4001..5000 are retained, so a client at
    # 4000 needs 4001 and gets it, while 3999 has lost 4000 and must reset.
    assert log.earliest_resumable == 4000
    assert log.can_resume(StreamCursor(sequence=4000))
    assert len(log.since(StreamCursor(sequence=4000))) == 1000
    assert log.resume_problem(StreamCursor(sequence=3999)) is ResetReason.BEYOND_RETENTION
    response = build_reconnect_response(
        log, _snapshot(), cursor=StreamCursor(sequence=3999), observed_at=NOW, age_seconds=1
    )
    assert response["mode"] == "snapshot"
    assert response["resetReason"] == ResetReason.BEYOND_RETENTION.value
