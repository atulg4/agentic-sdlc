"""Tests for the Control Center's telemetry overlay."""

from __future__ import annotations

import json

import pytest

from agentic_sdlc.capacity_metrics import CapacityInputs, ObservationWindow, RunnerPool
from agentic_sdlc.control_center import ControlCenterError, build_control_center_snapshot
from agentic_sdlc.control_center_stream import StreamLog
from agentic_sdlc.control_center_telemetry import (
    EFFECTIVENESS_DOCUMENT_KEYS,
    TELEMETRY_SCHEMA_VERSION,
    build_telemetry_overlay,
    export_control_center,
    filter_usage_window,
    load_effectiveness_document,
)
from agentic_sdlc.event_ledger import (
    EventActor,
    EventLedger,
    LifecycleStage,
    WorkUnitRef,
    load_lifecycle_event,
)
from agentic_sdlc.project_registry import load_project_registry
from agentic_sdlc.usage_ledger import (
    InfrastructureUsage,
    TokenCounts,
    UsageActual,
    UsageEstimate,
    UsageRecord,
    UsageResult,
    load_pricing_snapshot,
    monetary_equivalent,
    usage_id_for,
)

NOW = "2026-10-29T09:00:00Z"
WINDOW = ObservationWindow("2026-10-29T00:00:00Z", "2026-10-30T00:00:00Z")

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

PAYG = load_pricing_snapshot(
    {
        "pricingId": "anthropic-payg",
        "provider": "anthropic",
        "model": "claude-opus-5",
        "billingMode": "payg",
        "version": "2026-10-01",
        "usdPerMillion": {"input": 15, "output": 75, "cacheRead": 1.5, "cacheWrite": 18.75},
    }
)
CLAUDE_MAX = load_pricing_snapshot(
    {
        "pricingId": "claude-max",
        "provider": "anthropic",
        "model": "claude-opus-5",
        "billingMode": "subscription",
        "version": "2026-10",
        "usdPerMillion": {"input": 15, "output": 75},
        "plan": {"name": "Claude Max", "capacityUnit": "five-hour-window-share"},
    }
)
TOKENS = TokenCounts(input=400_000, output=100_000, cache_read=0, cache_write=0)


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


def _snapshot():
    ledger = EventLedger(registry=REGISTRY)
    ledger.append(load_lifecycle_event(_event()))
    return build_control_center_snapshot(
        generated_at=NOW, registry=REGISTRY, projections=ledger.projections()
    )


def _record(
    run_id: str = "run-1",
    *,
    project_id: str = "forge",
    work_unit_id: str = "wu-1",
    pricing=PAYG,
    runtime: int | None = 1800,
    estimate_runtime: int | None = 1200,
    tokens: TokenCounts | None = TOKENS,
    infrastructure_usd: float | None = 0.4,
    stage: LifecycleStage = LifecycleStage.IMPLEMENTATION,
    started_at: str = "2026-10-29T08:30:00Z",
    finished_at: str = NOW,
) -> UsageRecord:
    counts = tokens if tokens is not None else TokenCounts()
    return UsageRecord(
        usage_id=usage_id_for(project_id, work_unit_id, run_id),
        work_unit=WorkUnitRef(
            project_id=project_id,
            work_unit_id=work_unit_id,
            issue_ref=f"acme/{project_id}#1",
            change_request_ref=f"acme/{project_id}!12",
        ),
        stage=stage,
        run_id=run_id,
        recorded_at=NOW,
        task_class="implementation",
        actor=EventActor(
            name="claude",
            kind="agent",
            agent_id="claude-code",
            provider="anthropic",
            model="claude-opus-5",
            model_alias="claude-opus",
            worker="gha-runner-7",
        ),
        estimate=UsageEstimate(
            tokens=TOKENS,
            runtime_seconds=estimate_runtime,
            monetary=monetary_equivalent(TOKENS, PAYG),
            basis="test baseline",
        ),
        actual=UsageActual(
            tokens=counts,
            runtime_seconds=runtime,
            monetary=monetary_equivalent(counts, pricing),
            result=UsageResult.COMPLETED,
            source="provider-usage-api",
            started_at=started_at,
            finished_at=finished_at,
        ),
        infrastructure=(
            None
            if infrastructure_usd is None
            else InfrastructureUsage(
                runner_class="ubuntu-latest", cost_usd=infrastructure_usd, basis="runner price"
            )
        ),
    )


def _effectiveness_document(**overrides):
    document = {
        "schemaVersion": 1,
        "sampleCount": 2,
        "window": WINDOW.as_dict(),
        "outcomes": {"accepted": 1, "byResult": {"delivered": 1, "reverted": 1}},
        "quality": {"firstPassVerificationRate": {"value": 0.5, "status": "observed"}},
        "observations": {"byKind": {"tests-healthy": 1}},
        "value": {"acceptedOutcomes": 1, "perAgentHour": {"value": 2.0, "status": "observed"}},
        "estimateVsActual": {"overall": {}},
        "learnings": {"count": 1, "advisory": True},
        "byProject": {"forge": {"accepted": 1}, "atlas": {"accepted": 0}},
        "limitations": ["value is never inferred from effort"],
    }
    document.update(overrides)
    return document


# --- composition ------------------------------------------------------------


def test_the_overlay_composes_cost_capacity_and_parallelism() -> None:
    overlay = build_telemetry_overlay(
        _snapshot(),
        usage_records=(_record("run-1"), _record("run-2")),
        window=WINDOW,
        registry=REGISTRY,
        observed_at=NOW,
        age_seconds=30,
    )

    assert overlay["schemaVersion"] == TELEMETRY_SCHEMA_VERSION
    assert overlay["cost"]["status"] == "observed"
    assert overlay["cost"]["byProject"]["buckets"][0]["key"]["projectId"] == "forge"
    assert set(overlay["cost"]) >= {"byProject", "byModel", "byStage"}
    assert overlay["capacity"]["status"] == "observed"
    assert "planCapacity" in overlay["capacity"]
    assert overlay["parallelism"]["status"] == "observed"
    assert set(overlay["parallelism"]) >= {"concurrency", "queue", "runners", "bottlenecks"}
    assert overlay["limitations"]


def test_the_overlay_adds_no_arithmetic_the_layers_below_do_not() -> None:
    """The work-unit rollup must agree with the cost view it is drawn from."""
    records = (_record("run-1"), _record("run-2"))
    overlay = build_telemetry_overlay(_snapshot(), usage_records=records, registry=REGISTRY)

    unit = overlay["workUnits"]["forge/wu-1"]
    project_bucket = overlay["cost"]["byProject"]["buckets"][0]

    assert unit["runCount"] == 2
    assert unit["cost"]["records"] == project_bucket["records"]
    assert unit["cost"]["runs"] == project_bucket["runs"]


def test_the_drill_down_carries_every_run_behind_one_backlog_row() -> None:
    overlay = build_telemetry_overlay(
        _snapshot(),
        usage_records=(
            _record("run-2", started_at="2026-10-29T08:40:00Z"),
            _record("run-1", started_at="2026-10-29T08:00:00Z", finished_at="2026-10-29T08:30:00Z"),
        ),
        registry=REGISTRY,
    )
    unit = overlay["workUnits"]["forge/wu-1"]

    # Ordered by occurrence, so a timeline renders without re-sorting.
    assert [run["runId"] for run in unit["runs"]] == ["run-1", "run-2"]
    first = unit["runs"][0]
    assert first["model"] == "claude-opus-5"
    assert first["worker"] == "gha-runner-7"
    assert first["issueRef"] == "acme/forge#1"
    assert first["changeRequestRef"] == "acme/forge!12"
    assert first["stage"] == "implementation"
    assert first["estimate"] is not None and first["actual"] is not None
    # Estimated 1200s, ran 1800s: a 50% overrun, from the usage ledger itself.
    assert first["estimateVsActual"]["dimensions"]["runtimeSeconds"]["percentage"] == 50.0
    assert unit["stages"] == ["implementation"]


def test_capacity_inputs_flow_through_to_the_runner_view() -> None:
    overlay = build_telemetry_overlay(
        _snapshot(),
        usage_records=(_record("run-1"),),
        window=WINDOW,
        capacity_inputs=CapacityInputs(
            pools=(RunnerPool(pool_id="gha", slots=4, workers=("gha-runner-7",)),)
        ),
        registry=REGISTRY,
    )

    assert overlay["parallelism"]["runners"]


# --- unknown versus observed ------------------------------------------------


def test_a_section_with_no_data_reads_unknown_with_its_reason() -> None:
    overlay = build_telemetry_overlay(_snapshot(), registry=REGISTRY)

    for section in ("cost", "capacity", "parallelism", "effectiveness"):
        assert overlay[section]["status"] == "unknown", section
        assert overlay[section]["reason"], section
    assert overlay["workUnits"] == {}


def test_utilization_is_unknown_without_an_explicit_window() -> None:
    """A quiet period must stay in the denominator, so the window is required."""
    overlay = build_telemetry_overlay(
        _snapshot(), usage_records=(_record("run-1"),), registry=REGISTRY
    )

    assert overlay["cost"]["status"] == "observed"
    assert overlay["capacity"]["status"] == "unknown"
    assert "observation window" in overlay["capacity"]["reason"]
    assert overlay["parallelism"]["status"] == "unknown"


def test_the_billing_mix_keeps_subscription_separable_from_billed() -> None:
    overlay = build_telemetry_overlay(
        _snapshot(),
        usage_records=(
            _record("run-1", pricing=PAYG),
            _record("run-2", pricing=CLAUDE_MAX),
            _record("run-3", tokens=TokenCounts(), pricing=None),
        ),
        registry=REGISTRY,
    )

    assert overlay["billingMix"] == {"payg": 1, "subscription": 1, "unknown": 1}
    limitations = " ".join(overlay["limitations"])
    assert "never presented as billed dollars" in limitations
    assert "only where a provider reported it" in limitations


def test_freshness_is_labeled_with_its_last_good_instant() -> None:
    snapshot = _snapshot()

    observed = build_telemetry_overlay(snapshot, observed_at=NOW, age_seconds=10)
    stale = build_telemetry_overlay(
        snapshot, observed_at=NOW, age_seconds=5000, stale_after_seconds=900
    )
    unknown = build_telemetry_overlay(snapshot)

    assert observed["freshness"]["status"] == "observed"
    assert observed["freshness"]["lastGoodAt"] == NOW
    assert stale["freshness"]["status"] == "stale"
    assert unknown["freshness"]["status"] == "unknown"
    assert unknown["freshness"]["ageSeconds"] is None
    assert unknown["freshness"]["reason"]


# --- project isolation ------------------------------------------------------


def test_project_scoping_narrows_every_section_at_once() -> None:
    overlay = build_telemetry_overlay(
        _snapshot(),
        usage_records=(
            _record("run-1", project_id="forge", work_unit_id="wu-1"),
            _record("run-2", project_id="atlas", work_unit_id="wu-9"),
        ),
        window=WINDOW,
        effectiveness=_effectiveness_document(),
        registry=REGISTRY,
        project_ids=("forge",),
        observed_at=NOW,
        age_seconds=5,
    )

    assert overlay["projectIds"] == ["forge"]
    assert list(overlay["workUnits"]) == ["forge/wu-1"]
    assert list(overlay["effectiveness"]["byProject"]) == ["forge"]
    serialized = json.dumps(overlay)
    assert "atlas" not in serialized
    assert "wu-9" not in serialized


def test_an_unscoped_overlay_keeps_every_project() -> None:
    overlay = build_telemetry_overlay(
        _snapshot(),
        usage_records=(
            _record("run-1", project_id="forge"),
            _record("run-2", project_id="atlas", work_unit_id="wu-9"),
        ),
        effectiveness=_effectiveness_document(),
        registry=REGISTRY,
    )

    assert overlay["projectIds"] is None
    assert sorted(overlay["workUnits"]) == ["atlas/wu-9", "forge/wu-1"]
    assert sorted(overlay["effectiveness"]["byProject"]) == ["atlas", "forge"]


# --- the effectiveness document ---------------------------------------------


def test_the_effectiveness_report_is_surfaced_as_published() -> None:
    document = _effectiveness_document()
    overlay = build_telemetry_overlay(_snapshot(), effectiveness=document, registry=REGISTRY)
    section = overlay["effectiveness"]

    assert section["status"] == "observed"
    assert section["sampleCount"] == 2
    assert section["quality"] == document["quality"]
    assert section["value"] == document["value"]
    assert section["learnings"]["advisory"] is True
    assert section["limitations"] == document["limitations"]


def test_an_absent_effectiveness_report_never_infers_delivery_from_runs() -> None:
    overlay = build_telemetry_overlay(
        _snapshot(), usage_records=(_record("run-1"),), registry=REGISTRY
    )

    assert overlay["effectiveness"]["status"] == "unknown"
    assert "never infers delivery from runs" in overlay["effectiveness"]["reason"]


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"schemaVersion": 1, "surprise": 1}, "unknown sections"),
        ({"schemaVersion": 2}, "schemaVersion must be 1"),
        ({}, "schemaVersion must be 1"),
        ({"schemaVersion": 1, "quality": {"token": "x"}}, "credential field"),
        (
            {"schemaVersion": 1, "quality": {"u": "https://x.example?t=ghp_0123456789abcdefghij"}},
            "credential-shaped value",
        ),
        ("not a document", "must be an object"),
    ],
)
def test_the_effectiveness_loader_fails_closed(document, message: str) -> None:
    with pytest.raises(ControlCenterError, match=message):
        load_effectiveness_document(document)


def test_every_section_of_the_report_is_declared() -> None:
    assert set(_effectiveness_document()) <= EFFECTIVENESS_DOCUMENT_KEYS
    assert load_effectiveness_document(_effectiveness_document())


# --- time-range filtering ---------------------------------------------------


def test_time_bounds_compare_instants_not_strings() -> None:
    records = (
        _record(
            "early",
            started_at="2026-10-29T00:30:00+02:00",
            finished_at="2026-10-29T01:00:00+02:00",
        ),
        _record("late", started_at="2026-10-29T08:30:00Z", finished_at="2026-10-29T09:00:00Z"),
    )

    after = filter_usage_window(records, since="2026-10-29T00:00:00Z")
    before = filter_usage_window(records, until="2026-10-29T00:00:00Z")

    # 01:00+02:00 is 23:00Z the previous day: earlier than the bound.
    assert [item.run_id for item in after] == ["late"]
    assert [item.run_id for item in before] == ["early"]
    assert len(filter_usage_window(records)) == 2


def test_an_inverted_window_fails_closed() -> None:
    with pytest.raises(ControlCenterError, match="must not come after"):
        filter_usage_window(
            (_record("run-1"),), since="2026-10-30T00:00:00Z", until="2026-10-29T00:00:00Z"
        )


def test_filtering_feeds_a_historical_overlay() -> None:
    records = (
        _record("old", started_at="2026-10-01T08:30:00Z", finished_at="2026-10-01T09:00:00Z"),
        _record("new", started_at="2026-10-29T08:30:00Z", finished_at=NOW),
    )

    overlay = build_telemetry_overlay(
        _snapshot(),
        usage_records=filter_usage_window(records, since="2026-10-28T00:00:00Z"),
        registry=REGISTRY,
    )

    assert [run["runId"] for run in overlay["workUnits"]["forge/wu-1"]["runs"]] == ["new"]


# --- export -----------------------------------------------------------------


def test_the_export_is_self_contained_and_digest_checkable() -> None:
    snapshot = _snapshot()
    overlay = build_telemetry_overlay(
        snapshot, usage_records=(_record("run-1"),), registry=REGISTRY
    )
    log = StreamLog()

    document = export_control_center(snapshot, overlay=overlay, stream=log.as_dict())

    assert document["schemaVersion"] == TELEMETRY_SCHEMA_VERSION
    assert document["exportedFrom"] == "forge-control-center"
    assert document["snapshot"]["snapshotSha256"] == snapshot.snapshot_sha256
    assert document["telemetry"]["cost"]["status"] == "observed"
    assert document["stream"]["cursor"]["sequence"] == 0
    # Round-trips through JSON, so an exported file is analysable offline.
    assert json.loads(json.dumps(document))["generatedAt"] == NOW


def test_an_export_without_telemetry_is_still_valid() -> None:
    snapshot = _snapshot()

    document = export_control_center(snapshot)

    assert document["telemetry"] is None
    assert document["stream"] is None
    assert document["snapshot"]["projects"]


# --- credentials ------------------------------------------------------------


def test_the_overlay_can_never_serialize_a_credential_to_a_browser() -> None:
    snapshot = _snapshot()
    overlay = build_telemetry_overlay(
        snapshot,
        usage_records=(_record("run-1"),),
        window=WINDOW,
        effectiveness=_effectiveness_document(),
        registry=REGISTRY,
        observed_at=NOW,
        age_seconds=5,
    )
    serialized = json.dumps(export_control_center(snapshot, overlay=overlay)).lower()

    for forbidden in ("ghp_", "authorization", "bearer", "password", "passphrase", "privatekey"):
        assert forbidden not in serialized


def test_a_credential_in_a_consumed_report_is_refused_not_rendered() -> None:
    with pytest.raises(ControlCenterError, match="credential"):
        build_telemetry_overlay(
            _snapshot(),
            effectiveness=_effectiveness_document(
                quality={"evidence": "https://x.example?t=ghp_0123456789abcdefghij"}
            ),
            registry=REGISTRY,
        )


# --- the CLI ----------------------------------------------------------------


def _write(path, document) -> str:
    path.write_text(json.dumps(document), encoding="utf-8")
    return str(path)


def test_control_center_cli_writes_snapshot_telemetry_stream_and_export(tmp_path) -> None:
    from agentic_sdlc.cli import main

    registry = _write(tmp_path / "registry.json", REGISTRY.as_dict())
    ledger = EventLedger(registry=REGISTRY)
    ledger.append(load_lifecycle_event(_event()))
    ledger_path = _write(tmp_path / "ledger.json", ledger.as_dict())
    usage = _write(
        tmp_path / "usage.json",
        {
            "schemaVersion": 1,
            "usageSchemaVersion": 1,
            "records": [_record("run-1").as_dict()],
        },
    )
    effectiveness = _write(tmp_path / "effectiveness.json", _effectiveness_document())
    snapshot_out = tmp_path / "snapshot.json"
    telemetry_out = tmp_path / "telemetry.json"
    stream_out = tmp_path / "stream.json"
    export_out = tmp_path / "export.json"

    assert (
        main(
            [
                "control-center",
                "--registry",
                registry,
                "--ledger",
                ledger_path,
                "--usage",
                usage,
                "--effectiveness",
                effectiveness,
                "--window-start",
                "2026-10-29T00:00:00Z",
                "--window-end",
                "2026-10-30T00:00:00Z",
                "--observed-at",
                NOW,
                "--age-seconds",
                "30",
                "--generated-at",
                NOW,
                "--output",
                str(snapshot_out),
                "--telemetry-output",
                str(telemetry_out),
                "--stream-output",
                str(stream_out),
                "--export-output",
                str(export_out),
            ]
        )
        == 0
    )

    snapshot = json.loads(snapshot_out.read_text(encoding="utf-8"))
    telemetry = json.loads(telemetry_out.read_text(encoding="utf-8"))
    stream = json.loads(stream_out.read_text(encoding="utf-8"))
    export = json.loads(export_out.read_text(encoding="utf-8"))

    assert snapshot["snapshotSha256"]
    assert telemetry["cost"]["status"] == "observed"
    assert telemetry["capacity"]["status"] == "observed"
    assert telemetry["effectiveness"]["status"] == "observed"
    assert telemetry["freshness"]["status"] == "observed"
    # A first connection is handed the snapshot and the cursor to resume from.
    assert stream["mode"] == "snapshot"
    assert stream["cursor"]["lastEventId"]
    assert export["snapshot"]["snapshotSha256"] == snapshot["snapshotSha256"]
    assert export["telemetry"]["cost"]["status"] == "observed"


def test_control_center_cli_resumes_from_a_stored_frame_log(tmp_path) -> None:
    from agentic_sdlc.cli import main
    from agentic_sdlc.control_center_stream import ChangeKind, FrameKind, StreamLog

    log = StreamLog()
    log.record(
        FrameKind.CHANGE,
        event_id="f-1",
        project_id="forge",
        change=ChangeKind.WORK_UNIT,
        occurred_at=NOW,
        work_unit_id="wu-1",
        payload={"state": "implementing"},
    )
    stream_in = _write(tmp_path / "frames.json", log.as_dict())
    stream_out = tmp_path / "reconnect.json"

    assert (
        main(
            [
                "control-center",
                "--generated-at",
                NOW,
                "--stream",
                stream_in,
                "--resume-from",
                "0",
                "--stream-output",
                str(stream_out),
            ]
        )
        == 0
    )

    response = json.loads(stream_out.read_text(encoding="utf-8"))
    assert response["mode"] == "resumed"
    assert [frame["eventId"] for frame in response["frames"]] == ["f-1"]


def test_control_center_cli_fails_closed_on_telemetry_inputs(tmp_path) -> None:
    from agentic_sdlc.cli import main

    base = ["control-center", "--generated-at", NOW]
    # A window needs both bounds.
    assert main([*base, "--window-start", NOW, "--telemetry-output", str(tmp_path / "t.json")]) == 2
    # A resume with nowhere to report the frames is refused.
    assert main([*base, "--resume-from", "1"]) == 2
    # A malformed resume token is refused rather than read as sequence zero.
    assert (
        main([*base, "--resume-from", "nonsense", "--stream-output", str(tmp_path / "s.json")]) == 2
    )
    # A credential in a consumed report is refused, not rendered.
    bad = _write(
        tmp_path / "bad.json",
        _effectiveness_document(quality={"u": "https://x.example?t=ghp_0123456789abcdefghij"}),
    )
    assert (
        main([*base, "--effectiveness", bad, "--telemetry-output", str(tmp_path / "t2.json")]) == 2
    )
