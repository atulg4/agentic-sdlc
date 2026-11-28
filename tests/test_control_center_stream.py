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
    load_stream_frame,
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
    # A scoped reconnect takes a snapshot built with the same scope: frames and
    # snapshot are scoped together, so isolation cannot depend on whether the
    # cursor happens to be resumable.
    response = build_reconnect_response(
        log,
        build_control_center_snapshot(
            generated_at=NOW, registry=REGISTRY, projections=(), project_ids=("atlas",)
        ),
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


# --- regressions ------------------------------------------------------------


def test_a_pending_heartbeat_does_not_explain_a_stale_digest() -> None:
    """A heartbeat says nothing changed, so it cannot account for a difference.

    Treating one as reconciling would resume the client, hand it the current
    digest, and leave its stale content undetected for good.
    """
    log = StreamLog()
    log.record(FrameKind.CHANGE, event_id="f-1", project_id="forge", change=ChangeKind.WORK_UNIT)
    log.record(FrameKind.HEARTBEAT, event_id="hb-1", occurred_at=NOW)
    snapshot = _snapshot()
    cursor = StreamCursor(sequence=1, snapshot_sha256="b" * 64)

    response = build_reconnect_response(
        log, snapshot, cursor=cursor, observed_at=NOW, age_seconds=5
    )

    assert response["mode"] == "snapshot"
    assert response["resetReason"] == ResetReason.SNAPSHOT_MISMATCH.value
    assert response["snapshot"] is not None


def test_recording_a_known_event_id_with_new_content_fails_closed() -> None:
    """``record`` must not silently discard the new content of a replay.

    The same ID with a different payload is a producer bug, and answering with
    the stored frame would make the caller believe its change was recorded.
    """
    log = _log(frames=1)

    with pytest.raises(ControlCenterError, match="already recorded with a different payload"):
        log.record(
            FrameKind.CHANGE,
            event_id="f-1",
            project_id="forge",
            change=ChangeKind.WORK_UNIT,
            occurred_at=NOW,
            work_unit_id="wu-1",
            payload={"state": "done"},
        )

    assert log.frames[0].payload == {"state": "implementing"}


def test_a_scoped_log_document_carries_only_its_projects_frames() -> None:
    log = StreamLog()
    log.record(
        FrameKind.CHANGE, event_id="f-forge", project_id="forge", change=ChangeKind.WORK_UNIT
    )
    log.record(
        FrameKind.CHANGE, event_id="f-atlas", project_id="atlas", change=ChangeKind.WORK_UNIT
    )
    log.record(FrameKind.HEARTBEAT, event_id="hb-1", occurred_at=NOW)

    document = log.as_dict(project_ids=("forge",))

    assert document["scopedTo"] == ["forge"]
    # A heartbeat belongs to no project, so it stays; another project's change
    # never appears in a scoped view.
    assert [frame["eventId"] for frame in document["frames"]] == ["f-forge", "hb-1"]
    assert "atlas" not in json.dumps(document)
    # The resume bookkeeping still describes the whole log: that is what a
    # reader needs to judge a cursor against.
    assert document["assigned"] == 3


def test_a_scoped_log_document_cannot_be_rehydrated() -> None:
    """Its frames are filtered, so its sequence numbering has gaps."""
    log = StreamLog()
    log.record(
        FrameKind.CHANGE, event_id="f-forge", project_id="forge", change=ChangeKind.WORK_UNIT
    )
    log.record(
        FrameKind.CHANGE, event_id="f-atlas", project_id="atlas", change=ChangeKind.WORK_UNIT
    )

    with pytest.raises(ControlCenterError, match="a view for analysis, not a log"):
        StreamLog.from_dict(log.as_dict(project_ids=("forge",)))

    assert StreamLog.from_dict(log.as_dict()).frames == log.frames


# --- second review round (reviewed at 5e2d955) ------------------------------


def test_a_scoped_reconnect_refuses_an_unscoped_snapshot() -> None:
    """Isolation must not depend on whether the cursor happens to resume.

    Frames are filtered by scope, but a first connection and a reset both ship
    the whole snapshot document, so a portfolio snapshot handed to a scoped
    reconnect leaked every other project.
    """
    log = _log(frames=1)
    snapshot = _snapshot()  # carries both forge and atlas

    with pytest.raises(ControlCenterError, match="also carries atlas"):
        build_reconnect_response(
            log, snapshot, cursor=None, observed_at=NOW, age_seconds=5, project_ids=("forge",)
        )
    # Refused rather than filtered: the digest covers the snapshot's own
    # content, so filtering here would make it wrong, and recomputing one
    # would hand the client a document no producer published.
    scoped = build_control_center_snapshot(
        generated_at=NOW, registry=REGISTRY, projections=(), project_ids=("forge",)
    )
    response = build_reconnect_response(
        log, scoped, cursor=None, observed_at=NOW, age_seconds=5, project_ids=("forge",)
    )
    assert [project["projectId"] for project in response["snapshot"]["projects"]] == ["forge"]
    assert response["snapshot"]["snapshotSha256"] == scoped.snapshot_sha256


def test_an_unscoped_reconnect_still_ships_the_whole_portfolio() -> None:
    response = build_reconnect_response(
        _log(frames=1), _snapshot(), cursor=None, observed_at=NOW, age_seconds=5
    )
    assert sorted(p["projectId"] for p in response["snapshot"]["projects"]) == ["atlas", "forge"]


def test_a_high_water_mark_above_the_last_retained_frame_fails_closed() -> None:
    """Sequences between the last frame and the mark can never be served.

    A client resuming from inside the window would be told it was up to date,
    its cursor would jump to the mark, and the next append would leave a gap
    the log's own contiguity rule rejects -- so the log could not be reloaded
    from the document it had just written.
    """
    document = _log(frames=1).as_dict()
    document["assigned"] = 4

    with pytest.raises(ControlCenterError, match="can never be served"):
        StreamLog.from_dict(document)

    # A log that really has slid its window still rehydrates: the mark is the
    # last retained frame, not the count of frames.
    slid = _log(retain=2, frames=5)
    assert [frame.sequence for frame in slid.frames] == [4, 5]
    assert StreamLog.from_dict(slid.as_dict()).as_dict() == slid.as_dict()
    # An empty log may declare a mark above zero: every frame was dropped.
    empty = {**document, "frames": [], "assigned": 4, "earliestResumable": 4}
    assert StreamLog.from_dict(empty).next_sequence == 5


def test_the_resume_id_carries_the_snapshot_digest_forward() -> None:
    """Only a snapshot frame holds a digest, and reconnect needs it.

    Without carrying it, every heartbeat and change after a snapshot published
    a sequence-only id, the browser echoed that back, and the mismatch check
    had nothing to work with -- losing snapshot recovery on exactly the
    Last-Event-ID path the docs tell transports to rely on.
    """
    first = _snapshot()
    changed = _snapshot(with_unit=False)
    assert first.snapshot_sha256 != changed.snapshot_sha256

    log = StreamLog()
    log.record(
        FrameKind.SNAPSHOT,
        event_id="s-1",
        occurred_at=NOW,
        payload={"snapshotSha256": first.snapshot_sha256},
    )
    log.record(FrameKind.HEARTBEAT, event_id="hb-1", occurred_at=NOW)

    ids = [block.splitlines()[0] for block in render_sse_stream(log.frames).strip().split("\n\n")]
    published = StreamCursor(sequence=1, snapshot_sha256=first.snapshot_sha256).encode()
    assert ids[0] == "id: " + published
    assert ids[1].endswith(first.snapshot_sha256), f"the heartbeat dropped the digest: {ids[1]}"

    echoed = StreamCursor.decode(ids[1].removeprefix("id: "))
    recovered = build_reconnect_response(
        log, changed, cursor=echoed, observed_at=NOW, age_seconds=5
    )
    assert recovered["mode"] == "snapshot"
    assert recovered["resetReason"] == ResetReason.SNAPSHOT_MISMATCH.value

    # The same frame rendered without the carried digest is what used to be
    # published, and it cannot detect the mismatch at all.
    rendered = render_sse_frame(log.frames[-1]).splitlines()[0].removeprefix("id: ")
    bare = StreamCursor.decode(rendered)
    assert bare.snapshot_sha256 == ""
    assert (
        build_reconnect_response(log, changed, cursor=bare, observed_at=NOW, age_seconds=5)["mode"]
        == "resumed"
    )


def test_a_credential_shaped_key_is_refused_like_a_credential_value() -> None:
    """A key is serialized to the browser exactly like a value."""
    with pytest.raises(ControlCenterError, match="credential-shaped key"):
        StreamFrame(
            sequence=1,
            event_id="f-1",
            kind=FrameKind.CHANGE,
            occurred_at=NOW,
            project_id="forge",
            change=ChangeKind.WORK_UNIT,
            payload={"ghp_0123456789abcdefghij": "metadata"},
        )
    # Percent-encoding is peeled for keys too, as it already was for values.
    with pytest.raises(ControlCenterError, match="credential-shaped key"):
        StreamFrame(
            sequence=1,
            event_id="f-1",
            kind=FrameKind.CHANGE,
            occurred_at=NOW,
            project_id="forge",
            change=ChangeKind.WORK_UNIT,
            payload={"%67hp_0123456789abcdefghij": "metadata"},
        )


# --- verification round (reviewed at d1829ce) -------------------------------


def test_a_digest_that_is_not_hexadecimal_never_reaches_an_sse_id() -> None:
    """The id line is interpolated unescaped, so length alone is no check.

    A 64-character value carrying newlines appended ``event:`` and ``data:``
    lines the producer never wrote, corrupting the event boundary and the
    resume id the browser echoes back.
    """
    injection = "a" * 20 + "\nevent: spoofed\ndata: {}\n" + "x" * 19
    assert len(injection) == 64

    # Through a snapshot frame's own published digest...
    frame = StreamFrame(
        sequence=1,
        event_id="s-1",
        kind=FrameKind.SNAPSHOT,
        occurred_at=NOW,
        payload={"snapshotSha256": injection},
    )
    with pytest.raises(ControlCenterError, match="64 hexadecimal"):
        render_sse_frame(frame)
    # ...and through the keyword a transport carries the digest forward with.
    heartbeat = StreamFrame(sequence=2, event_id="hb-1", kind=FrameKind.HEARTBEAT, occurred_at=NOW)
    with pytest.raises(ControlCenterError, match="64 hexadecimal"):
        render_sse_frame(heartbeat, snapshot_sha256=injection)
    # A cursor refuses it too, so a resume token cannot smuggle one in.
    with pytest.raises(ControlCenterError, match="64 hexadecimal"):
        StreamCursor(sequence=1, snapshot_sha256=injection)

    # A real digest still renders exactly one event line.
    rendered = render_sse_frame(heartbeat, snapshot_sha256="0" * 63 + "f")
    assert rendered.count("event:") == 1
    assert rendered.count("\nid:") + rendered.startswith("id:") == 1


def test_a_serialized_frame_cannot_rewrite_the_retained_one() -> None:
    """``as_dict`` shared every nested mapping with the frame the log keeps.

    A consumer editing a reconnect or export document could rewrite validated
    history -- adding a credential-shaped key that then rendered without ever
    passing the scanner.
    """
    log = StreamLog()
    log.record(
        FrameKind.CHANGE,
        event_id="f-1",
        project_id="forge",
        change=ChangeKind.WORK_UNIT,
        occurred_at=NOW,
        payload={"nested": {"state": "implementing"}, "list": [{"deep": "value"}]},
    )
    document = log.frames[0].as_dict()
    document["payload"]["nested"]["ghp_0123456789abcdefghij"] = "leaked"
    document["payload"]["list"][0]["deep"] = "rewritten"

    retained = log.frames[0].payload
    assert "ghp_0123456789abcdefghij" not in str(retained)
    assert retained["list"][0]["deep"] == "value"
    assert "ghp_0123456789abcdefghij" not in render_sse_frame(log.frames[0])
    # Two documents from the same frame do not share structure either.
    first, second = log.frames[0].as_dict(), log.frames[0].as_dict()
    first["payload"]["nested"]["state"] = "done"
    assert second["payload"]["nested"]["state"] == "implementing"


def test_a_payload_key_is_bounded_like_a_payload_value() -> None:
    """A key is text the frame carries, retains and renders."""
    with pytest.raises(ControlCenterError, match="key longer than"):
        StreamFrame(
            sequence=1,
            event_id="f-1",
            kind=FrameKind.CHANGE,
            occurred_at=NOW,
            project_id="forge",
            change=ChangeKind.WORK_UNIT,
            payload={"k" * (MAX_TEXT_LENGTH + 1): "v"},
        )
    # At every depth, not just the top level.
    with pytest.raises(ControlCenterError, match="key longer than"):
        StreamFrame(
            sequence=1,
            event_id="f-1",
            kind=FrameKind.CHANGE,
            occurred_at=NOW,
            project_id="forge",
            change=ChangeKind.WORK_UNIT,
            payload={"nested": {"k" * (MAX_TEXT_LENGTH + 1): "v"}},
        )
    # A key at the bound is fine.
    assert StreamFrame(
        sequence=1,
        event_id="f-1",
        kind=FrameKind.CHANGE,
        occurred_at=NOW,
        project_id="forge",
        change=ChangeKind.WORK_UNIT,
        payload={"k" * MAX_TEXT_LENGTH: "v"},
    ).payload


def test_evicted_event_never_receives_a_new_sequence_after_restart() -> None:
    log = _log(retain=1, frames=2)
    for candidate in (log, StreamLog.from_dict(log.as_dict())):
        with pytest.raises(ControlCenterError, match="retired"):
            candidate.record(
                FrameKind.CHANGE,
                event_id="f-1",
                project_id="forge",
                change=ChangeKind.WORK_UNIT,
                occurred_at=NOW,
                work_unit_id="wu-1",
                payload={"state": "implementing"},
            )
        assert candidate.next_sequence == 3
        assert candidate.since(StreamCursor(2)) == ()


def test_legacy_slid_log_can_resume_but_cannot_resequence_unknown_ids() -> None:
    document = _log(retain=1, frames=2).as_dict()
    document.pop("retiredEventDigests", None)
    legacy = StreamLog.from_dict(document)
    assert [frame.event_id for frame in legacy.since(StreamCursor(1))] == ["f-2"]
    with pytest.raises(ControlCenterError, match="replay history"):
        legacy.record(FrameKind.HEARTBEAT, event_id="f-1")


def test_change_on_digest_aware_transport_never_acknowledges_an_old_snapshot() -> None:
    before, after = _snapshot(), _snapshot(with_unit=False)
    log = _log(frames=1)
    initial = StreamCursor(sequence=0, snapshot_sha256=before.snapshot_sha256)
    change = log.frames[0]
    with pytest.raises(ControlCenterError, match="resulting snapshot"):
        render_sse_frame(change, snapshot_sha256=before.snapshot_sha256)
    delivery = build_reconnect_response(log, after, cursor=initial)
    assert delivery["mode"] == "resumed"
    wire = render_sse_stream(
        [load_stream_frame(frame) for frame in delivery["frames"]],
        snapshot_sha256=initial.snapshot_sha256,
    )
    cursor = StreamCursor.decode(wire.splitlines()[0].removeprefix("id: "))
    assert cursor.snapshot_sha256 == after.snapshot_sha256
    assert build_reconnect_response(log, after, cursor=cursor)["mode"] == "resumed"


def test_intermediate_change_without_scoped_digest_recovers_from_snapshot() -> None:
    before, after = _snapshot(), _snapshot(with_unit=False)
    log = _log(frames=2)
    response = build_reconnect_response(log, after, cursor=StreamCursor(0, before.snapshot_sha256))
    assert response["mode"] == "snapshot"
    assert response["snapshot"]["snapshotSha256"] == after.snapshot_sha256


def test_retired_identity_metadata_is_checked_and_excluded_from_scoped_exports() -> None:
    log = _log(retain=1, frames=2)
    assert "retiredEventDigests" not in log.as_dict(project_ids=("forge",))
    doc = log.as_dict()
    doc["retiredEventDigests"] = []
    with pytest.raises(ControlCenterError, match="one sha256 digest per dropped frame"):
        StreamLog.from_dict(doc)
