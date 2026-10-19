"""Tests for the Control Center read model and its GitHub adapter."""

from __future__ import annotations

import json

import pytest

from agentic_sdlc.control_center import (
    AWAITING_HUMAN_STATES,
    BACKLOG_SCHEMA_VERSION,
    CONTROL_CENTER_SCHEMA_VERSION,
    EXECUTING_STATES,
    INCIDENT_KINDS,
    INCIDENT_SCHEMA_VERSION,
    REMEDIATION_CATALOGUE,
    STATE_BASIS_DECLARED,
    STATE_BASIS_LEDGER,
    STATE_BASIS_UNKNOWN,
    BacklogKind,
    ControlCenterError,
    IncidentSeverity,
    RemediationAuthority,
    build_control_center_snapshot,
    load_backlog_document,
    load_incident_document,
    read_backlog_document,
    read_control_center_inputs,
    read_incident_document,
    read_optional_backlog_document,
    read_optional_incident_document,
    remediations_for,
    secret_bearing_control_center_fields,
    work_unit_status,
)
from agentic_sdlc.event_ledger import (
    EventLedger,
    FactoryState,
    LifecycleStage,
    load_lifecycle_event,
)
from agentic_sdlc.github_backlog import (
    DISPATCH_RULES,
    LifecycleLabels,
    backlog_document_from_issues,
    incident_document,
    incidents_from_change_requests,
    incidents_from_issues,
    parent_links,
)
from agentic_sdlc.project_registry import load_project_registry

NOW = "2026-10-11T21:30:00Z"

EMPTY = load_project_registry({"schemaVersion": 1, "projects": []})

REGISTRY = load_project_registry(
    {
        "schemaVersion": 1,
        "projects": [
            {
                "projectId": "forge",
                "displayName": "Forge",
                "group": "platform",
                "defaultBranch": "main",
                "repositories": [
                    {"provider": "github", "identifier": "acme/forge", "role": "primary"}
                ],
                "environments": [{"name": "production", "kind": "production"}],
                "capabilities": ["planning", "implementation"],
            }
        ],
    }
)


def _backlog(**overrides):
    item = {
        "itemId": "acme/forge#1",
        "kind": "task",
        "title": "Add a thing",
        "projectId": "forge",
    }
    item.update(overrides)
    return item


def _document(*items):
    return {"schemaVersion": BACKLOG_SCHEMA_VERSION, "items": list(items)}


def _incident(**overrides):
    entry = {
        "incidentId": "i-1",
        "projectId": "forge",
        "kind": "dispatch-blocked",
        "severity": "blocker",
        "summary": "Nothing is running this",
    }
    entry.update(overrides)
    return entry


def _incidents(*entries):
    # The adapter's own exports always declare their GitHub source; rule
    # rewriting and retirement require that provenance.
    return {
        "schemaVersion": INCIDENT_SCHEMA_VERSION,
        "source": "github",
        "incidents": list(entries),
    }


def _event(**overrides):
    event = {
        "schemaVersion": 1,
        "eventId": overrides.pop("eventId", "e-1"),
        "idempotencyKey": overrides.pop("idempotencyKey", "k-1"),
        "workUnit": {"projectId": "forge", "workUnitId": "wu-1"},
        "stage": "intake",
        "state": "intake",
        "occurredAt": NOW,
        "recordedAt": NOW,
        "actor": {"name": "planner", "kind": "agent"},
        "provenance": {"source": "test"},
    }
    event.update(overrides)
    return event


_STAGE_FOR = {
    "intake": "intake",
    "triaged": "intake",
    "specified": "specification",
    "planned": "planning",
    "approval-pending": "planning",
    "approved": "planning",
    "dispatched": "dispatch",
    "implementing": "implementation",
    "verifying": "deterministic-verification",
    "reviewing": "independent-review",
    "ready-for-human-merge": "merge",
    "blocked": "blocked",
    "merged": "merge",
    "live": "live",
    "repair-needed": "repair",
}


def _chain(states, *, work_unit="wu-1", prefix="a", **last):
    """Build a legal event chain, so the ledger's own transition table holds."""
    events = [
        _event(
            eventId=f"{prefix}-{index}",
            idempotencyKey=f"{prefix}k-{index}",
            workUnit={"projectId": "forge", "workUnitId": work_unit},
            stage=_STAGE_FOR[state],
            state=state,
        )
        for index, state in enumerate(states)
    ]
    events[-1].update(last)
    return events


TO_PLANNED = ("intake", "triaged", "specified", "planned")
TO_IMPLEMENTING = TO_PLANNED + ("approved", "dispatched", "implementing")


def _ledger(*events):
    ledger = EventLedger(registry=REGISTRY)
    for event in events:
        ledger.append(load_lifecycle_event(event))
    return ledger


# --- backlog documents -----------------------------------------------------


def test_backlog_document_round_trips_every_declared_field():
    items = load_backlog_document(
        _document(
            _backlog(
                itemId="acme/forge#9",
                kind="epic",
                parentId="",
                workUnitId="wu-1",
                issueRef="acme/forge#9",
                declaredState="approved",
                labels=["agent-ready", "agent-ready"],
                url="https://github.com/acme/forge/issues/9",
                updatedAt=NOW,
            )
        )
    )
    assert len(items) == 1
    item = items[0]
    assert item.kind is BacklogKind.EPIC
    assert item.labels == ("agent-ready",)
    assert item.as_dict()["declaredState"] == "approved"


def test_backlog_document_rejects_unsupported_schema_version():
    with pytest.raises(ControlCenterError, match="unsupported backlog schemaVersion"):
        load_backlog_document({"schemaVersion": 2, "items": []})


def test_backlog_document_rejects_unknown_keys():
    with pytest.raises(ControlCenterError, match="unknown keys"):
        load_backlog_document(_document(_backlog(owner="someone")))


def test_backlog_document_rejects_credential_bearing_keys():
    with pytest.raises(ControlCenterError, match="credential-bearing"):
        load_backlog_document(_document(_backlog(apiKey="x")))


def test_backlog_document_rejects_unknown_kind():
    with pytest.raises(ControlCenterError, match="must be one of epic, story, task"):
        load_backlog_document(_document(_backlog(kind="initiative")))


def test_backlog_document_rejects_declared_state_that_is_not_a_factory_state():
    with pytest.raises(ControlCenterError, match="not a factory state"):
        load_backlog_document(_document(_backlog(declaredState="almost-done")))


def test_backlog_document_rejects_non_https_url():
    with pytest.raises(ControlCenterError, match="must be an https URL"):
        load_backlog_document(_document(_backlog(url="http://example.com/1")))


def test_backlog_document_rejects_bad_timestamp():
    with pytest.raises(ControlCenterError, match="RFC 3339"):
        load_backlog_document(_document(_backlog(updatedAt="yesterday")))


def test_backlog_document_rejects_duplicate_item_ids():
    with pytest.raises(ControlCenterError, match="duplicate backlog itemId"):
        load_backlog_document(_document(_backlog(), _backlog()))


def test_backlog_document_rejects_a_parent_in_another_project():
    with pytest.raises(ControlCenterError, match="parent in another project"):
        load_backlog_document(
            _document(
                _backlog(itemId="acme/forge#1", kind="epic"),
                _backlog(itemId="other#2", projectId="other", parentId="acme/forge#1"),
            )
        )


def test_backlog_document_rejects_a_parent_cycle():
    with pytest.raises(ControlCenterError, match="parent cycle"):
        load_backlog_document(
            _document(
                _backlog(itemId="a", parentId="b"),
                _backlog(itemId="b", parentId="a"),
            )
        )


def test_backlog_document_rejects_a_missing_title():
    with pytest.raises(ControlCenterError, match="title is required"):
        load_backlog_document(_document(_backlog(title="  ")))


def test_backlog_document_rejects_an_invalid_identifier():
    with pytest.raises(ControlCenterError, match="not a valid identifier"):
        load_backlog_document(_document(_backlog(itemId="has spaces")))


def test_backlog_document_rejects_a_non_array_items_field():
    with pytest.raises(ControlCenterError, match="items must be an array"):
        load_backlog_document({"schemaVersion": 1, "items": {}})


def test_reading_a_backlog_document_requires_the_file(tmp_path):
    with pytest.raises(ControlCenterError, match="not found"):
        read_backlog_document(tmp_path / "absent.json")


def test_optional_backlog_document_is_empty_when_absent(tmp_path):
    assert read_optional_backlog_document(None) == ()
    assert read_optional_backlog_document(tmp_path / "absent.json") == ()


def test_optional_backlog_document_reads_a_present_file(tmp_path):
    path = tmp_path / "backlog.json"
    path.write_text(json.dumps(_document(_backlog())), encoding="utf-8")
    assert len(read_optional_backlog_document(path)) == 1


# --- incident documents ----------------------------------------------------


def test_incident_document_exposes_declared_remediations():
    incidents = load_incident_document(_incidents(_incident()))
    payload = incidents[0].as_dict()
    assert payload["remediations"]
    assert payload["remediations"][0]["actionId"] == "complete-dispatch-preconditions"


def test_incident_document_rejects_an_undeclared_kind():
    with pytest.raises(ControlCenterError, match="not a declared incident kind"):
        load_incident_document(_incidents(_incident(kind="gremlins")))


def test_incident_document_rejects_an_unknown_severity():
    with pytest.raises(ControlCenterError, match="blocker, degraded or advisory"):
        load_incident_document(_incidents(_incident(severity="catastrophic")))


def test_incident_document_rejects_duplicate_ids():
    with pytest.raises(ControlCenterError, match="duplicate incidentId"):
        load_incident_document(_incidents(_incident(), _incident()))


def test_incident_document_rejects_unsupported_schema_version():
    with pytest.raises(ControlCenterError, match="unsupported incident schemaVersion"):
        load_incident_document({"schemaVersion": 7, "incidents": []})


def test_reading_an_incident_document_requires_the_file(tmp_path):
    with pytest.raises(ControlCenterError, match="not found"):
        read_incident_document(tmp_path / "absent.json")


def test_optional_incident_document_is_empty_when_absent(tmp_path):
    assert read_optional_incident_document(None) == ()
    assert read_optional_incident_document(tmp_path / "absent.json") == ()


def test_every_incident_kind_declares_at_least_one_remediation():
    for kind in INCIDENT_KINDS:
        assert REMEDIATION_CATALOGUE[kind]


def test_an_unknown_kind_falls_back_to_classification_not_a_guess():
    actions = remediations_for("not-a-kind")
    assert [action.action_id for action in actions] == ["classify-incident"]


def test_human_only_remediations_always_name_a_handoff():
    for actions in REMEDIATION_CATALOGUE.values():
        for action in actions:
            if action.authority is RemediationAuthority.HUMAN_ONLY:
                assert action.handoff


def test_merge_authority_is_never_offered_to_an_agent():
    actions = REMEDIATION_CATALOGUE["awaiting-human-merge"]
    assert all(action.authority is RemediationAuthority.HUMAN_ONLY for action in actions)


def test_secret_bearing_fields_are_named_for_schema_review():
    assert "token" in secret_bearing_control_center_fields()


# --- snapshots -------------------------------------------------------------


def test_snapshot_declares_its_schema_and_digests_only_content():
    first = build_control_center_snapshot(generated_at=NOW, registry=REGISTRY)
    later = build_control_center_snapshot(generated_at="2026-10-12T00:00:00Z", registry=REGISTRY)
    assert first.schema_version == CONTROL_CENTER_SCHEMA_VERSION
    assert first.snapshot_sha256 == later.snapshot_sha256
    assert first.as_document()["snapshotSha256"] == first.snapshot_sha256


def test_snapshot_rejects_a_generated_at_that_is_not_a_timestamp():
    with pytest.raises(ControlCenterError, match="RFC 3339"):
        build_control_center_snapshot(generated_at="now")


def test_a_registered_project_without_data_reports_no_data_rather_than_healthy():
    snapshot = build_control_center_snapshot(generated_at=NOW, registry=REGISTRY)
    view = snapshot.get("forge")
    assert view.registered
    assert view.health == "no-data"
    assert view.repositories == ("github:acme/forge",)
    assert view.environments == ("production:production",)


def test_data_for_an_unregistered_project_is_surfaced_not_dropped():
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        backlog=load_backlog_document(_document(_backlog(projectId="ghost"))),
    )
    view = snapshot.get("ghost")
    assert not view.registered
    assert view.health == "unregistered"
    assert any("not declared in the registry" in warning for warning in snapshot.warnings)


def test_snapshot_rejects_a_requested_project_it_has_never_seen():
    with pytest.raises(ControlCenterError, match="no such project"):
        build_control_center_snapshot(generated_at=NOW, registry=REGISTRY, project_ids=("absent",))


def test_snapshot_filters_to_the_requested_projects():
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        backlog=load_backlog_document(_document(_backlog(projectId="ghost"))),
        project_ids=("ghost",),
    )
    assert [view.project_id for view in snapshot.projects] == ["ghost"]


def test_backlog_state_comes_from_the_ledger_when_a_work_unit_is_known():
    ledger = _ledger(*_chain(TO_PLANNED))
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=ledger.projections(),
        backlog=load_backlog_document(_document(_backlog(workUnitId="wu-1"))),
    )
    node = snapshot.get("forge").backlog[0]
    assert node.state == FactoryState.PLANNED.value
    assert node.state_basis == STATE_BASIS_LEDGER
    assert node.status is not None


def test_a_declared_state_is_used_only_when_the_ledger_is_silent():
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        backlog=load_backlog_document(_document(_backlog(declaredState="approved"))),
    )
    node = snapshot.get("forge").backlog[0]
    assert (node.state, node.state_basis) == ("approved", STATE_BASIS_DECLARED)


def test_an_item_with_no_evidence_is_unknown_and_never_fabricated():
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        backlog=load_backlog_document(_document(_backlog())),
    )
    node = snapshot.get("forge").backlog[0]
    assert (node.state, node.state_basis) == ("unknown", STATE_BASIS_UNKNOWN)


def test_the_backlog_is_rendered_as_an_epic_tree():
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        backlog=load_backlog_document(
            _document(
                _backlog(itemId="epic", kind="epic", title="An epic"),
                _backlog(itemId="story", kind="story", parentId="epic", title="A story"),
                _backlog(itemId="task", kind="task", parentId="story", title="A task"),
            )
        ),
    )
    view = snapshot.get("forge")
    assert [node.item.item_id for node in view.backlog] == ["epic"]
    assert view.backlog[0].descendant_count == 2
    assert view.backlog_count == 3


def test_an_item_whose_parent_is_outside_the_snapshot_is_rooted_and_flagged():
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        backlog=load_backlog_document(_document(_backlog(parentId="elsewhere"))),
    )
    view = snapshot.get("forge")
    assert view.backlog[0].parent_missing
    assert any("outside this snapshot" in warning for warning in view.warnings)


def test_active_and_attention_units_are_separated_by_who_must_act():
    ledger = _ledger(
        *_chain(TO_IMPLEMENTING, activity="writing the patch"),
        *_chain(
            TO_PLANNED + ("approval-pending",),
            work_unit="wu-2",
            prefix="b",
        ),
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW, registry=REGISTRY, projections=ledger.projections()
    )
    view = snapshot.get("forge")
    assert [item.work_unit_id for item in view.active] == ["wu-1"]
    assert [item.work_unit_id for item in view.attention] == ["wu-2"]
    assert view.active[0].activity == "writing the patch"
    assert view.health == "needs-attention"


def test_state_and_stage_counts_cover_every_projected_unit():
    ledger = _ledger(
        *_chain(("intake",)),
        *_chain(("intake",), work_unit="wu-2", prefix="b"),
    )
    view = build_control_center_snapshot(
        generated_at=NOW, registry=REGISTRY, projections=ledger.projections()
    ).get("forge")
    assert view.state_counts == {FactoryState.INTAKE.value: 2}
    assert view.stage_counts == {LifecycleStage.INTAKE.value: 2}
    assert view.work_unit_count == 2


def test_a_blocker_incident_makes_the_project_blocked():
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        incidents=load_incident_document(_incidents(_incident())),
    )
    view = snapshot.get("forge")
    assert view.health == "blocked"
    assert view.blocker_count == 1


def test_an_advisory_incident_leaves_the_project_degraded_not_blocked():
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        incidents=load_incident_document(
            _incidents(_incident(kind="awaiting-human-merge", severity="advisory"))
        ),
    )
    assert snapshot.get("forge").health == "degraded"


def test_an_incident_naming_an_unknown_work_unit_is_warned_about():
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        incidents=load_incident_document(_incidents(_incident(workUnitId="wu-absent"))),
    )
    assert any("unknown work unit" in warning for warning in snapshot.get("forge").warnings)


def test_an_incident_naming_an_unknown_backlog_item_is_warned_about():
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        incidents=load_incident_document(_incidents(_incident(backlogItemId="absent"))),
    )
    assert any("unknown backlog item" in warning for warning in snapshot.get("forge").warnings)


def test_snapshot_rejects_two_projections_of_the_same_work_unit():
    ledger = _ledger(_event())
    projection = ledger.projections()[0]
    with pytest.raises(ControlCenterError, match="duplicate projection"):
        build_control_center_snapshot(
            generated_at=NOW, registry=REGISTRY, projections=(projection, projection)
        )


def test_snapshot_totals_aggregate_every_project():
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        backlog=load_backlog_document(
            _document(_backlog(), _backlog(itemId="ghost#1", projectId="ghost"))
        ),
        incidents=load_incident_document(_incidents(_incident())),
        sources=("registry:x", "registry:x"),
    )
    totals = snapshot.totals
    assert totals["projects"] == 2
    assert totals["backlogItems"] == 2
    assert totals["blockers"] == 1
    assert totals["unregistered"] == 1
    assert snapshot.sources == ("registry:x",)


def test_requesting_a_project_the_snapshot_lacks_is_an_error():
    snapshot = build_control_center_snapshot(generated_at=NOW, registry=REGISTRY)
    with pytest.raises(ControlCenterError, match="no project"):
        snapshot.get("ghost")


def test_awaiting_human_states_are_exactly_the_two_human_gates():
    gates = frozenset({FactoryState.APPROVAL_PENDING, FactoryState.READY_FOR_HUMAN_MERGE})
    assert gates == AWAITING_HUMAN_STATES


def test_work_unit_status_keeps_durable_and_ephemeral_facts_both():
    ledger = _ledger(
        *_chain(
            TO_IMPLEMENTING,
            activity="running the suite",
            actor={"name": "builder", "kind": "agent", "model": "m", "worker": "w"},
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-1",
                "repository": "acme/forge",
                "issueRef": "acme/forge#4",
                "changeRequestRef": "acme/forge!5",
                "runRef": "run-7",
            },
        )
    )
    status = work_unit_status(ledger.projections()[0])
    payload = status.as_dict()
    assert payload["state"] == "implementing"
    assert payload["model"] == "m"
    assert payload["changeRequestRef"] == "acme/forge!5"
    assert not status.needs_attention


def test_a_blocked_unit_needs_attention_without_awaiting_a_human_gate():
    ledger = _ledger(*_chain(("intake", "triaged", "blocked"), reason="waiting on a runner"))
    status = work_unit_status(ledger.projections()[0])
    assert status.needs_attention
    assert not status.awaits_human


def test_reading_inputs_tolerates_every_source_being_absent():
    registry, backlog, incidents = read_control_center_inputs()
    assert registry.is_empty
    assert backlog == ()
    assert incidents == ()


# --- the GitHub adapter ----------------------------------------------------


def _issue(number, title, labels=(), state="open", reason=None, subtotal=0):
    return {
        "number": number,
        "title": title,
        "state": state,
        "state_reason": reason,
        "labels": [{"name": name} for name in labels],
        "repository_url": "https://api.github.com/repos/acme/forge",
        "html_url": f"https://github.com/acme/forge/issues/{number}",
        "updated_at": NOW,
        "sub_issues_summary": {"total": subtotal, "completed": 0},
    }


def _export(*issues, parents=None):
    return backlog_document_from_issues(
        issues, project_id="forge", repository="acme/forge", parents=parents
    )


def test_the_adapter_output_is_a_valid_backlog_document():
    document = _export(_issue(1, "EPIC: do it"), _issue(2, "P1: a part"))
    items = load_backlog_document(document)
    assert [item.kind for item in items] == [BacklogKind.EPIC, BacklogKind.TASK]


def test_an_epic_is_recognised_from_the_title_convention():
    document = _export(_issue(1, "FORGE Epic: adaptive intelligence"))
    assert document["items"][0]["kind"] == "epic"


def test_an_issue_owning_sub_issues_is_a_story_when_it_is_not_an_epic():
    document = _export(_issue(1, "A parent", subtotal=2))
    assert document["items"][0]["kind"] == "story"


def test_pull_requests_returned_by_the_issues_endpoint_are_skipped():
    issue = _issue(2, "a change request")
    issue["pull_request"] = {"url": "https://example.test"}
    document = _export(_issue(1, "a task"), issue)
    assert [item["itemId"] for item in document["items"]] == ["acme/forge#1"]


def test_parent_links_come_from_the_sub_issue_payload():
    links = parent_links(
        [
            {
                "number": 2,
                "parent_issue_url": "https://api.github.com/repos/acme/forge/issues/1",
            }
        ]
    )
    assert links == {2: 1}
    document = _export(_issue(1, "EPIC: parent"), _issue(2, "child"), parents=links)
    items = load_backlog_document(document)
    assert items[1].parent_id == "acme/forge#1"


def test_parent_links_reject_two_parents_for_one_child():
    with pytest.raises(ControlCenterError, match="two parents"):
        parent_links(
            [
                {
                    "number": 2,
                    "parent_issue_url": "https://api.github.com/repos/acme/forge/issues/1",
                },
                {
                    "number": 2,
                    "parent_issue_url": "https://api.github.com/repos/acme/forge/issues/3",
                },
            ]
        )


def test_a_parent_outside_the_exported_set_is_kept_and_flagged_downstream():
    document = _export(_issue(2, "child"), parents={2: 99})
    assert document["items"][0]["parentId"] == "acme/forge#99"
    snapshot = build_control_center_snapshot(
        generated_at=NOW, registry=EMPTY, backlog=load_backlog_document(document)
    )
    view = snapshot.get("forge")
    assert view.backlog[0].parent_missing
    assert any("outside this snapshot" in warning for warning in view.warnings)


def test_labels_declare_the_state_through_the_published_table():
    document = _export(
        _issue(1, "approved", ["implementation-approved", "agent-ready"]),
        _issue(2, "spec only", ["spec-drafted"]),
        _issue(3, "triaged", ["human-review-required"]),
        _issue(4, "bare", []),
    )
    states = [item.get("declaredState") for item in document["items"]]
    assert states == ["approved", "specified", "triaged", None]


def test_a_closed_issue_maps_its_close_reason_to_a_terminal_state():
    document = _export(
        _issue(1, "done", state="closed", reason="completed"),
        _issue(2, "dropped", state="closed", reason="not_planned"),
        _issue(3, "dupe", state="closed", reason="duplicate"),
    )
    assert [item["declaredState"] for item in document["items"]] == [
        "merged",
        "cancelled",
        "superseded",
    ]


def test_an_approved_issue_without_agent_ready_is_a_dispatch_blocker():
    incidents = incidents_from_issues(
        [_issue(1, "approved", ["implementation-approved", "human-review-required"])],
        project_id="forge",
        repository="acme/forge",
        observed_at=NOW,
    )
    loaded = load_incident_document(_incidents(*incidents))
    assert loaded[0].kind == "dispatch-blocked"
    assert loaded[0].severity is IncidentSeverity.BLOCKER
    assert loaded[0].backlog_item_id == "acme/forge#1"


def test_an_untriaged_issue_is_only_an_advisory():
    incidents = incidents_from_issues(
        [_issue(1, "bare")], project_id="forge", repository="acme/forge", observed_at=NOW
    )
    assert incidents[0]["severity"] == "advisory"


def test_a_fully_labelled_issue_raises_only_the_dispatch_evidence_advisory():
    incidents = incidents_from_issues(
        [
            _issue(
                1,
                "ready",
                ["implementation-approved", "human-review-required", "agent-ready"],
            )
        ],
        project_id="forge",
        repository="acme/forge",
        observed_at=NOW,
    )
    assert len(incidents) == 1
    assert incidents[0]["severity"] == "advisory"
    assert "confirm a run" in incidents[0]["summary"]


def test_closed_issues_raise_no_dispatch_incident():
    incidents = incidents_from_issues(
        [_issue(1, "done", state="closed", reason="completed")],
        project_id="forge",
        repository="acme/forge",
        observed_at=NOW,
    )
    assert incidents == []


def test_each_issue_reports_only_its_highest_precedence_rule():
    incidents = incidents_from_issues(
        [_issue(1, "spec", ["spec-drafted", "human-review-required"])],
        project_id="forge",
        repository="acme/forge",
        observed_at=NOW,
    )
    assert len(incidents) == 1
    assert incidents[0]["kind"] == "awaiting-spec-review"


def test_every_dispatch_rule_names_a_declared_incident_kind():
    for rule in DISPATCH_RULES:
        assert rule.kind in INCIDENT_KINDS


def test_change_request_incidents_distinguish_conflict_ci_and_merge_wait():
    incidents = incidents_from_change_requests(
        [
            {
                "number": 1,
                "title": "conflicted",
                "state": "open",
                "mergeable_state": "dirty",
                "draft": False,
                "base": {"repo": {"full_name": "acme/forge"}},
            },
            {
                "number": 2,
                "title": "red",
                "state": "open",
                "mergeable_state": "blocked",
                "draft": False,
                "base": {"repo": {"full_name": "acme/forge"}},
            },
            {
                "number": 3,
                "title": "ready",
                "state": "open",
                "mergeable_state": "clean",
                "draft": False,
                "base": {"repo": {"full_name": "acme/forge"}},
            },
            {
                "number": 4,
                "title": "draft",
                "state": "open",
                "draft": True,
                "mergeable_state": "clean",
                "base": {"repo": {"full_name": "acme/forge"}},
            },
        ],
        project_id="forge",
        repository="acme/forge",
        observed_at=NOW,
        failing={2: "test"},
    )
    kinds = {item["incidentId"]: item["kind"] for item in incidents}
    assert kinds == {
        "acme/forge!1:merge-conflict": "merge-conflict",
        "acme/forge!2:ci-failed": "deterministic-ci-failed",
        # The branch-protection impediment survives the declared CI failure:
        # both remediation requirements are real.
        "acme/forge!2:merge-state-blocked": "unclassified",
        "acme/forge!3:awaiting-merge": "awaiting-human-merge",
    }
    assert load_incident_document(_incidents(*incidents))


def test_combining_incident_groups_rejects_duplicate_ids():
    group = incidents_from_issues(
        [_issue(1, "bare")], project_id="forge", repository="acme/forge", observed_at=NOW
    )
    with pytest.raises(ControlCenterError, match="duplicate incidentId"):
        incident_document(group, group)


def test_combining_incident_groups_produces_a_loadable_document():
    document = incident_document(
        incidents_from_issues(
            [_issue(1, "bare")],
            project_id="forge",
            repository="acme/forge",
            observed_at=NOW,
        ),
        incidents_from_change_requests(
            [
                {
                    "number": 9,
                    "title": "ready",
                    "state": "open",
                    "mergeable_state": "clean",
                    "draft": False,
                    "base": {"repo": {"full_name": "acme/forge"}},
                }
            ],
            project_id="forge",
            repository="acme/forge",
            observed_at=NOW,
        ),
    )
    assert len(load_incident_document(document)) == 2


def test_the_adapter_requires_a_project_and_a_repository():
    with pytest.raises(ControlCenterError, match="project_id is required"):
        backlog_document_from_issues([], project_id="", repository="acme/forge")
    with pytest.raises(ControlCenterError, match="OWNER/REPO"):
        backlog_document_from_issues([], project_id="forge", repository="")


# --- the Codex review round: regressions pinned by test -------------------


def test_an_item_with_only_an_issue_reference_joins_the_ledger_state():
    ledger = _ledger(
        *_chain(
            TO_IMPLEMENTING,
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-1",
                "issueRef": "acme/forge#7",
            },
        )
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=ledger.projections(),
        backlog=load_backlog_document(
            _document(_backlog(issueRef="acme/forge#7", declaredState="approved"))
        ),
    )
    node = snapshot.get("forge").backlog[0]
    assert node.state == FactoryState.IMPLEMENTING.value
    assert node.state_basis == STATE_BASIS_LEDGER
    assert node.status is not None


def test_an_ambiguous_issue_reference_is_never_joined_and_is_warned_about():
    ledger = _ledger(
        *_chain(
            ("intake",),
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-1",
                "issueRef": "acme/forge#7",
            },
        ),
        *_chain(
            ("intake",),
            work_unit="wu-2",
            prefix="b",
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-2",
                "issueRef": "acme/forge#7",
            },
        ),
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=ledger.projections(),
        backlog=load_backlog_document(
            _document(_backlog(issueRef="acme/forge#7", declaredState="approved"))
        ),
    )
    view = snapshot.get("forge")
    node = view.backlog[0]
    assert (node.state, node.state_basis) == ("approved", STATE_BASIS_DECLARED)
    assert any("more than one work unit" in warning for warning in view.warnings)


def test_a_live_unit_is_settled_not_running():
    ledger = _ledger(
        *_chain(
            TO_IMPLEMENTING + ("verifying", "reviewing", "ready-for-human-merge", "merged", "live"),
        )
    )
    view = build_control_center_snapshot(
        generated_at=NOW, registry=REGISTRY, projections=ledger.projections()
    ).get("forge")
    assert view.active == ()
    assert view.attention == ()
    assert view.health == "idle"


def test_a_planned_unit_is_backlog_not_running():
    ledger = _ledger(*_chain(TO_PLANNED))
    view = build_control_center_snapshot(
        generated_at=NOW, registry=REGISTRY, projections=ledger.projections()
    ).get("forge")
    assert view.active == ()
    assert view.health == "idle"


def test_executing_states_exclude_backlog_and_settled_states():
    settled = {
        FactoryState.INTAKE,
        FactoryState.PLANNED,
        FactoryState.APPROVED,
        FactoryState.MERGED,
        FactoryState.LIVE,
    }
    assert not (EXECUTING_STATES & settled)


def test_the_digest_ignores_when_incidents_were_observed():
    def snapshot_at(observed):
        return build_control_center_snapshot(
            generated_at=NOW,
            registry=REGISTRY,
            incidents=load_incident_document(_incidents(_incident(observedAt=observed))),
        )

    first = snapshot_at("2026-10-11T10:00:00Z")
    later = snapshot_at("2026-10-12T10:00:00Z")
    assert first.snapshot_sha256 == later.snapshot_sha256
    changed = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        incidents=load_incident_document(_incidents(_incident(summary="A different problem"))),
    )
    assert changed.snapshot_sha256 != first.snapshot_sha256


def test_an_approval_with_neither_dispatch_label_is_a_blocker():
    incidents = incidents_from_issues(
        [_issue(1, "approved but unlabelled", ["implementation-approved"])],
        project_id="forge",
        repository="acme/forge",
        observed_at=NOW,
    )
    assert len(incidents) == 1
    assert incidents[0]["kind"] == "dispatch-blocked"
    assert incidents[0]["severity"] == "blocker"
    assert "neither" in incidents[0]["summary"]


def test_a_draft_change_request_is_never_a_blocker():
    incidents = incidents_from_change_requests(
        [
            {
                "number": 1,
                "title": "wip",
                "state": "open",
                "draft": True,
                "mergeable_state": "dirty",
                "base": {"repo": {"full_name": "acme/forge"}},
            },
            {
                "number": 2,
                "title": "wip red",
                "state": "open",
                "draft": True,
                "mergeable_state": "blocked",
                "base": {"repo": {"full_name": "acme/forge"}},
            },
        ],
        project_id="forge",
        repository="acme/forge",
        observed_at=NOW,
        failing={2: "test"},
    )
    assert incidents == []


def test_a_change_request_without_a_mergeability_verdict_is_not_ready():
    incidents = incidents_from_change_requests(
        [
            {
                "number": 1,
                "title": "list payload only",
                "state": "open",
                "draft": False,
                "base": {"repo": {"full_name": "acme/forge"}},
            }
        ],
        project_id="forge",
        repository="acme/forge",
        observed_at=NOW,
    )
    assert incidents == []


def test_a_failing_check_is_reported_even_without_a_mergeability_verdict():
    incidents = incidents_from_change_requests(
        [
            {
                "number": 1,
                "title": "red",
                "state": "open",
                "draft": False,
                "base": {"repo": {"full_name": "acme/forge"}},
            }
        ],
        project_id="forge",
        repository="acme/forge",
        observed_at=NOW,
        failing={1: "test"},
    )
    assert [item["kind"] for item in incidents] == ["deterministic-ci-failed"]


def test_a_declared_sub_issue_parent_needs_no_response_field():
    links = parent_links(([{"number": 2}, {"number": 3}], 1))
    assert links == {2: 1, 3: 1}


def test_a_child_contradicting_the_declared_parent_is_rejected():
    with pytest.raises(ControlCenterError, match="declared for"):
        parent_links(
            (
                [
                    {
                        "number": 2,
                        "parent_issue_url": "https://api.github.com/repos/acme/forge/issues/9",
                    }
                ],
                1,
            )
        )


def test_a_declared_parent_agreeing_with_the_reference_links_once():
    links = parent_links(
        (
            [{"number": 2, "parent_issue_url": "https://api.github.com/repos/a/b/issues/1"}],
            1,
        )
    )
    assert links == {2: 1}


def test_only_ready_merge_states_wait_on_the_merge_authority():
    pulls = [
        {
            "number": 1,
            "title": "clean",
            "state": "open",
            "mergeable_state": "clean",
            "draft": False,
            "base": {"repo": {"full_name": "acme/forge"}},
        },
        {
            "number": 2,
            "title": "hooks",
            "state": "open",
            "mergeable_state": "has_hooks",
            "draft": False,
            "base": {"repo": {"full_name": "acme/forge"}},
        },
        {
            "number": 3,
            "title": "blocked",
            "state": "open",
            "mergeable_state": "blocked",
            "draft": False,
            "base": {"repo": {"full_name": "acme/forge"}},
        },
        {
            "number": 4,
            "title": "behind",
            "state": "open",
            "mergeable_state": "behind",
            "draft": False,
            "base": {"repo": {"full_name": "acme/forge"}},
        },
        {
            "number": 5,
            "title": "unstable",
            "state": "open",
            "mergeable_state": "unstable",
            "draft": False,
            "base": {"repo": {"full_name": "acme/forge"}},
        },
        {
            "number": 6,
            "title": "unknown",
            "state": "open",
            "mergeable_state": "unknown",
            "draft": False,
            "base": {"repo": {"full_name": "acme/forge"}},
        },
    ]
    incidents = incidents_from_change_requests(
        pulls, project_id="forge", repository="acme/forge", observed_at=NOW
    )
    by_id = {item["incidentId"]: item for item in incidents}
    assert by_id["acme/forge!1:awaiting-merge"]["kind"] == "awaiting-human-merge"
    assert by_id["acme/forge!2:awaiting-merge"]["kind"] == "awaiting-human-merge"
    for number, state, kind in (
        (3, "blocked", "unclassified"),
        (4, "behind", "branch-behind"),
        (5, "unstable", "unclassified"),
    ):
        entry = by_id[f"acme/forge!{number}:merge-state-{state}"]
        assert entry["kind"] == kind
        assert entry["severity"] == "degraded"
        assert state in entry["summary"]
    assert "acme/forge!6:merge-state-unknown" not in by_id
    assert len(incidents) == 5


def test_a_behind_change_request_carries_the_branch_refresh_remediation():
    assert "branch-behind" in INCIDENT_KINDS
    actions = remediations_for("branch-behind")
    assert [action.action_id for action in actions] == ["update-change-request-branch"]
    assert actions[0].authority is RemediationAuthority.AGENT


def test_a_non_boolean_draft_flag_is_a_controlled_error():
    for bad in ("false", 1, [], {}):
        with pytest.raises(ControlCenterError, match="draft must be a boolean"):
            incidents_from_change_requests(
                [
                    {
                        "number": 1,
                        "title": "odd draft",
                        "state": "open",
                        "draft": bad,
                        "mergeable_state": "dirty",
                        "base": {"repo": {"full_name": "acme/forge"}},
                    }
                ],
                project_id="forge",
                repository="acme/forge",
                observed_at=NOW,
            )


def test_a_missing_close_reason_declares_no_terminal_state():
    document = _export(
        _issue(1, "legacy close", state="closed", reason=None),
        _issue(2, "odd reason", state="closed", reason="reopened"),
    )
    assert all("declaredState" not in item for item in document["items"])


def test_a_spec_draft_is_held_for_spec_review_not_implementation_approval():
    alone = incidents_from_issues(
        [_issue(1, "draft only", ["spec-drafted"])],
        project_id="forge",
        repository="acme/forge",
        observed_at=NOW,
    )
    with_review = incidents_from_issues(
        [_issue(2, "draft held", ["spec-drafted", "human-review-required"])],
        project_id="forge",
        repository="acme/forge",
        observed_at=NOW,
    )
    for incidents in (alone, with_review):
        assert len(incidents) == 1
        assert incidents[0]["kind"] == "awaiting-spec-review"
        assert "spec review" in incidents[0]["summary"]
        remediations = load_incident_document(_incidents(*incidents))[0].as_dict()["remediations"]
        assert remediations[0]["actionId"] == "release-or-reject-the-spec"


def test_a_released_spec_waits_on_implementation_approval():
    incidents = incidents_from_issues(
        [_issue(1, "released", ["spec-drafted", "spec-approved", "human-review-required"])],
        project_id="forge",
        repository="acme/forge",
        observed_at=NOW,
    )
    assert len(incidents) == 1
    assert incidents[0]["summary"] == (
        "Specification drafted and waiting on a human implementation "
        "approval: dispatch needs implementation-approved and agent-ready"
    )


def test_full_length_github_names_fit_generated_identifiers():
    owner = "o" * 39
    repository = f"{owner}/{'r' * 100}"
    long_issue = dict(
        _issue(123456, "long names"),
        repository_url=f"https://api.github.com/repos/{repository}",
        html_url=f"https://github.com/{repository}/issues/123456",
    )
    document = backlog_document_from_issues(
        [long_issue],
        project_id="forge",
        repository=repository,
    )
    # itemId carries the repository verbatim; the loader must accept it.
    items = load_backlog_document(
        {**document, "items": [{**document["items"][0], "projectId": "forge"}]}
    )
    assert items[0].item_id == f"{repository}#123456"
    incidents = incidents_from_issues(
        [long_issue],
        project_id="forge",
        repository=repository,
        observed_at=NOW,
    )
    assert load_incident_document(_incidents(*incidents))


def test_every_declared_remediation_command_parses_against_the_cli():
    import shlex

    from agentic_sdlc.cli import build_parser

    parser = build_parser()
    commands = [
        action.command
        for actions in REMEDIATION_CATALOGUE.values()
        for action in actions
        if action.command
    ]
    assert commands
    import re

    for command in commands:
        # Angle-bracket placeholders stand in for operator-supplied values;
        # substitute a value every argument type accepts before parsing.
        argv = [re.sub(r"<[^>]+>", "1", token) for token in shlex.split(command)]
        assert argv[0] == "sdlcctl"
        try:
            parser.parse_args(argv[1:])
        except SystemExit as error:  # argparse rejects by exiting
            raise AssertionError(f"catalogue command does not parse: {command}") from error


def test_a_parent_chain_deeper_than_the_budget_is_rejected():
    items = [_backlog(itemId="n0")]
    items += [_backlog(itemId=f"n{index}", parentId=f"n{index - 1}") for index in range(1, 40)]
    with pytest.raises(ControlCenterError, match="exceeds 32 levels"):
        load_backlog_document(_document(*items))


def test_a_parent_chain_within_the_budget_loads_and_serializes():
    items = [_backlog(itemId="n0", kind="epic")]
    items += [_backlog(itemId=f"n{index}", parentId=f"n{index - 1}") for index in range(1, 30)]
    snapshot = build_control_center_snapshot(
        generated_at=NOW, registry=REGISTRY, backlog=load_backlog_document(_document(*items))
    )
    view = snapshot.get("forge")
    assert view.backlog[0].descendant_count == 29
    assert snapshot.as_document()["snapshotSha256"]


def test_portfolio_inputs_combine_one_document_per_project(tmp_path):
    for name, project in (("a", "forge"), ("b", "ghost")):
        (tmp_path / f"backlog-{name}.json").write_text(
            json.dumps(_document(_backlog(itemId=f"{project}#1", projectId=project))),
            encoding="utf-8",
        )
        (tmp_path / f"incidents-{name}.json").write_text(
            json.dumps(_incidents(_incident(incidentId=f"i-{project}", projectId=project))),
            encoding="utf-8",
        )
    registry, backlog, incidents = read_control_center_inputs(
        backlog_paths=(str(tmp_path / "backlog-a.json"), str(tmp_path / "backlog-b.json")),
        incidents_paths=(
            str(tmp_path / "incidents-a.json"),
            str(tmp_path / "incidents-b.json"),
        ),
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW, registry=registry, backlog=backlog, incidents=incidents
    )
    assert {view.project_id for view in snapshot.projects} == {"forge", "ghost"}
    assert snapshot.totals["incidents"] == 2


def test_the_adapter_rejects_an_export_beyond_the_item_budget():
    issues = [_issue(number, f"issue {number}") for number in range(1, 5002)]
    with pytest.raises(ControlCenterError, match="5000-item budget"):
        backlog_document_from_issues(issues, project_id="forge", repository="acme/forge")


def test_labels_are_case_folded_like_the_workflow_expressions():
    document = _export(_issue(1, "mixed case", ["Implementation-Approved", "AGENT-READY"]))
    assert document["items"][0]["declaredState"] == "approved"
    incidents = incidents_from_issues(
        [_issue(2, "mixed", ["Implementation-Approved", "Human-Review-Required"])],
        project_id="forge",
        repository="acme/forge",
        observed_at=NOW,
    )
    assert incidents[0]["kind"] == "dispatch-blocked"
    assert incidents[0]["severity"] == "blocker"


def test_an_unreleased_spec_outranks_a_stale_approval_label():
    document = _export(
        _issue(1, "redrafted", ["implementation-approved", "agent-ready", "spec-drafted"]),
        _issue(
            2,
            "released",
            ["implementation-approved", "agent-ready", "spec-drafted", "spec-approved"],
        ),
    )
    states = [item["declaredState"] for item in document["items"]]
    assert states == ["specified", "approved"]


def test_plan_ready_labels_without_approval_raise_the_ordering_advisory():
    incidents = incidents_from_issues(
        [_issue(1, "plan ready", ["agent-ready", "human-review-required"])],
        project_id="forge",
        repository="acme/forge",
        observed_at=NOW,
    )
    assert len(incidents) == 1
    assert incidents[0]["severity"] == "advisory"
    assert "plan run" in incidents[0]["summary"]


def test_ledger_proof_of_dispatch_retires_the_label_advisory():
    advisory = _incident(
        incidentId="acme/forge#7:eligible",
        kind="dispatch-blocked",
        severity="advisory",
        backlogItemId="acme/forge#7",
    )
    ledger = _ledger(
        *_chain(
            TO_IMPLEMENTING,
            workUnit={"projectId": "forge", "workUnitId": "wu-1", "issueRef": "acme/forge#7"},
        )
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=ledger.projections(),
        backlog=load_backlog_document(
            _document(_backlog(itemId="acme/forge#7", issueRef="acme/forge#7"))
        ),
        incidents=load_incident_document(_incidents(advisory)),
    )
    view = snapshot.get("forge")
    assert view.incidents == ()
    assert view.health == "running"


def test_the_label_advisory_stays_while_the_ledger_shows_pre_dispatch_state():
    advisory = _incident(
        incidentId="acme/forge#7:eligible",
        kind="dispatch-blocked",
        severity="advisory",
        backlogItemId="acme/forge#7",
    )
    ledger = _ledger(
        *_chain(
            TO_PLANNED,
            workUnit={"projectId": "forge", "workUnitId": "wu-1", "issueRef": "acme/forge#7"},
        )
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=ledger.projections(),
        backlog=load_backlog_document(
            _document(_backlog(itemId="acme/forge#7", issueRef="acme/forge#7"))
        ),
        incidents=load_incident_document(_incidents(advisory)),
    )
    assert len(snapshot.get("forge").incidents) == 1


def test_dispatch_proof_retires_even_a_label_derived_blocker():
    blocker = _incident(
        incidentId="acme/forge#7:blocked",
        kind="dispatch-blocked",
        severity="blocker",
        backlogItemId="acme/forge#7",
    )
    proven = _ledger(
        *_chain(
            TO_IMPLEMENTING,
            workUnit={"projectId": "forge", "workUnitId": "wu-1", "issueRef": "acme/forge#7"},
        )
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=proven.projections(),
        backlog=load_backlog_document(
            _document(_backlog(itemId="acme/forge#7", issueRef="acme/forge#7"))
        ),
        incidents=load_incident_document(_incidents(blocker)),
    )
    assert snapshot.get("forge").incidents == ()
    assert snapshot.get("forge").health == "running"


def test_a_label_derived_blocker_stays_while_evidence_is_pre_dispatch():
    blocker = _incident(
        incidentId="acme/forge#7:blocked",
        kind="dispatch-blocked",
        severity="blocker",
        backlogItemId="acme/forge#7",
    )
    planned = _ledger(
        *_chain(
            TO_PLANNED,
            workUnit={"projectId": "forge", "workUnitId": "wu-1", "issueRef": "acme/forge#7"},
        )
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=planned.projections(),
        backlog=load_backlog_document(
            _document(_backlog(itemId="acme/forge#7", issueRef="acme/forge#7"))
        ),
        incidents=load_incident_document(_incidents(blocker)),
    )
    assert len(snapshot.get("forge").incidents) == 1


def test_a_released_spec_still_gets_the_plan_ordering_advisory():
    incidents = incidents_from_issues(
        [
            _issue(
                1,
                "released, plan-ready",
                ["agent-ready", "human-review-required", "spec-drafted", "spec-approved"],
            )
        ],
        project_id="forge",
        repository="acme/forge",
        observed_at=NOW,
    )
    assert len(incidents) == 1
    assert "plan run" in incidents[0]["summary"]


def test_planning_evidence_retires_the_plan_advisory_but_not_the_implement_one():
    from agentic_sdlc.control_center import PLAN_EVIDENCE_RULE

    plan_advisory = _incident(
        incidentId=f"acme/forge#7:{PLAN_EVIDENCE_RULE}",
        kind="dispatch-blocked",
        severity="advisory",
        backlogItemId="acme/forge#7",
        evidenceRef="acme/forge#7",
    )
    implement_advisory = _incident(
        incidentId="acme/forge#7:eligible-awaiting-dispatch-evidence",
        kind="dispatch-blocked",
        severity="advisory",
        backlogItemId="acme/forge#7",
        evidenceRef="acme/forge#7",
    )
    ledger = _ledger(
        *_chain(
            TO_PLANNED,
            workUnit={"projectId": "forge", "workUnitId": "wu-1", "issueRef": "acme/forge#7"},
        )
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=ledger.projections(),
        backlog=load_backlog_document(
            _document(_backlog(itemId="acme/forge#7", issueRef="acme/forge#7"))
        ),
        incidents=load_incident_document(_incidents(plan_advisory, implement_advisory)),
    )
    view = snapshot.get("forge")
    assert [item.incident_id for item in view.incidents] == [
        "acme/forge#7:eligible-awaiting-dispatch-evidence"
    ]


def test_the_plan_advisory_stays_while_the_ledger_shows_pre_planning_state():
    from agentic_sdlc.control_center import PLAN_EVIDENCE_RULE

    plan_advisory = _incident(
        incidentId=f"acme/forge#7:{PLAN_EVIDENCE_RULE}",
        kind="dispatch-blocked",
        severity="advisory",
        backlogItemId="acme/forge#7",
    )
    ledger = _ledger(
        *_chain(
            ("intake", "triaged", "specified"),
            workUnit={"projectId": "forge", "workUnitId": "wu-1", "issueRef": "acme/forge#7"},
        )
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=ledger.projections(),
        backlog=load_backlog_document(
            _document(_backlog(itemId="acme/forge#7", issueRef="acme/forge#7"))
        ),
        incidents=load_incident_document(_incidents(plan_advisory)),
    )
    assert len(snapshot.get("forge").incidents) == 1


def test_a_blocked_unit_retires_no_ordering_advisory():
    from agentic_sdlc.control_center import PLAN_EVIDENCE_RULE

    plan_advisory = _incident(
        incidentId=f"acme/forge#7:{PLAN_EVIDENCE_RULE}",
        kind="dispatch-blocked",
        severity="advisory",
        backlogItemId="acme/forge#7",
    )
    implement_advisory = _incident(
        incidentId="acme/forge#7:eligible-awaiting-dispatch-evidence",
        kind="dispatch-blocked",
        severity="advisory",
        backlogItemId="acme/forge#7",
    )
    ledger = _ledger(
        *_chain(
            ("intake", "triaged", "specified", "blocked"),
            workUnit={"projectId": "forge", "workUnitId": "wu-1", "issueRef": "acme/forge#7"},
        )
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=ledger.projections(),
        backlog=load_backlog_document(
            _document(_backlog(itemId="acme/forge#7", issueRef="acme/forge#7"))
        ),
        incidents=load_incident_document(_incidents(plan_advisory, implement_advisory)),
    )
    assert len(snapshot.get("forge").incidents) == 2


def test_a_repair_needed_unit_is_not_running():
    ledger = _ledger(*_chain(TO_IMPLEMENTING + ("verifying", "repair-needed")))
    view = build_control_center_snapshot(
        generated_at=NOW, registry=REGISTRY, projections=ledger.projections()
    ).get("forge")
    assert view.active == ()
    assert view.health == "idle"


def test_failing_checks_require_the_pull_payload(tmp_path):
    from agentic_sdlc.cli import main as cli_main

    issues = tmp_path / "issues.json"
    issues.write_text(json.dumps([]), encoding="utf-8")
    assert (
        cli_main(
            [
                "github-backlog",
                "--issues",
                str(issues),
                "--failing",
                "148=test",
                "--project-id",
                "forge",
                "--repository",
                "acme/forge",
            ]
        )
        == 2
    )


def test_a_plain_tuple_payload_is_not_mistaken_for_an_explicit_pair():
    children = (
        {"number": 2, "parent_issue_url": "https://api.github.com/repos/a/b/issues/1"},
        {"number": 3, "parent_issue_url": "https://api.github.com/repos/a/b/issues/1"},
        {"number": 4, "parent_issue_url": "https://api.github.com/repos/a/b/issues/1"},
    )
    assert parent_links(children) == {2: 1, 3: 1, 4: 1}


def test_failing_checks_must_name_change_requests_in_the_pull_payload(tmp_path):
    from agentic_sdlc.cli import main as cli_main

    issues = tmp_path / "issues.json"
    issues.write_text(json.dumps([]), encoding="utf-8")
    pulls = tmp_path / "pulls.json"
    pulls.write_text(
        json.dumps(
            [
                {
                    "number": 7,
                    "title": "x",
                    "state": "open",
                    "draft": False,
                    "base": {"repo": {"full_name": "acme/forge"}},
                }
            ]
        ),
        encoding="utf-8",
    )
    assert (
        cli_main(
            [
                "github-backlog",
                "--issues",
                str(issues),
                "--pulls",
                str(pulls),
                "--failing",
                "148=test",
                "--project-id",
                "forge",
                "--repository",
                "acme/forge",
            ]
        )
        == 2
    )


def test_a_malformed_observed_at_fails_the_export_not_the_next_command(tmp_path):
    from agentic_sdlc.cli import main as cli_main

    issues = tmp_path / "issues.json"
    issues.write_text(
        json.dumps(
            [
                {
                    "number": 1,
                    "title": "bare",
                    "state": "open",
                    "labels": [],
                    "repository_url": "https://api.github.com/repos/acme/forge",
                }
            ]
        ),
        encoding="utf-8",
    )
    incidents = tmp_path / "incidents.json"
    assert (
        cli_main(
            [
                "github-backlog",
                "--issues",
                str(issues),
                "--observed-at",
                "not-a-time",
                "--project-id",
                "forge",
                "--repository",
                "acme/forge",
                "--incidents-output",
                str(incidents),
            ]
        )
        == 2
    )
    assert not incidents.exists()


def test_a_dispatched_unit_is_queued_not_running():
    ledger = _ledger(*_chain(TO_PLANNED + ("approved", "dispatched")))
    view = build_control_center_snapshot(
        generated_at=NOW, registry=REGISTRY, projections=ledger.projections()
    ).get("forge")
    assert view.active == ()
    assert view.health == "idle"


def test_an_untriaged_issue_gets_a_triage_remediation():
    incidents = incidents_from_issues(
        [_issue(1, "bare")], project_id="forge", repository="acme/forge", observed_at=NOW
    )
    loaded = load_incident_document(_incidents(*incidents))
    assert loaded[0].kind == "triage-needed"
    actions = loaded[0].as_dict()["remediations"]
    assert actions[0]["actionId"] == "triage-the-work"
    assert actions[0]["authority"] == "human-only"


def test_closed_change_requests_raise_no_incident():
    incidents = incidents_from_change_requests(
        [
            {
                "number": 1,
                "title": "merged",
                "state": "closed",
                "mergeable_state": "dirty",
                "draft": False,
                "base": {"repo": {"full_name": "acme/forge"}},
            },
            {
                "number": 2,
                "title": "open",
                "state": "open",
                "mergeable_state": "clean",
                "draft": False,
                "base": {"repo": {"full_name": "acme/forge"}},
            },
        ],
        project_id="forge",
        repository="acme/forge",
        observed_at=NOW,
        failing={1: "test"},
    )
    assert [item["incidentId"] for item in incidents] == ["acme/forge!2:awaiting-merge"]


def test_a_cross_document_parent_cycle_is_rejected_at_the_snapshot():
    first = load_backlog_document(_document(_backlog(itemId="a", parentId="b")))
    second = load_backlog_document(_document(_backlog(itemId="b", parentId="a", title="Other")))
    with pytest.raises(ControlCenterError, match="parent cycle"):
        build_control_center_snapshot(generated_at=NOW, registry=REGISTRY, backlog=first + second)


def test_a_non_array_sub_issue_payload_is_a_controlled_error(tmp_path):
    from agentic_sdlc.cli import main as cli_main

    issues = tmp_path / "issues.json"
    issues.write_text(json.dumps([]), encoding="utf-8")
    bad = tmp_path / "sub.json"
    bad.write_text(json.dumps({"number": 2}), encoding="utf-8")
    assert (
        cli_main(
            [
                "github-backlog",
                "--issues",
                str(issues),
                "--sub-issues",
                f"1={bad}",
                "--project-id",
                "forge",
                "--repository",
                "acme/forge",
            ]
        )
        == 2
    )


def test_a_failed_export_writes_neither_output(tmp_path):
    from agentic_sdlc.cli import main as cli_main

    issues = tmp_path / "issues.json"
    issues.write_text(
        json.dumps(
            [
                {
                    "number": 1,
                    "title": "bare",
                    "state": "open",
                    "labels": [],
                    "repository_url": "https://api.github.com/repos/acme/forge",
                }
            ]
        ),
        encoding="utf-8",
    )
    backlog = tmp_path / "backlog.json"
    incidents = tmp_path / "incidents.json"
    assert (
        cli_main(
            [
                "github-backlog",
                "--issues",
                str(issues),
                "--observed-at",
                "not-a-time",
                "--project-id",
                "forge",
                "--repository",
                "acme/forge",
                "--backlog-output",
                str(backlog),
                "--incidents-output",
                str(incidents),
            ]
        )
        == 2
    )
    assert not backlog.exists()
    assert not incidents.exists()


def test_a_digit_shaped_impossible_timestamp_is_rejected():
    with pytest.raises(ControlCenterError, match="not a real instant"):
        build_control_center_snapshot(generated_at="2026-99-99T99:99:99Z", registry=REGISTRY)


def test_a_parent_absent_from_its_own_project_is_a_warned_orphan():
    # Identity is project-scoped, so a same-spelled item in ANOTHER project is
    # an unrelated record, never proof of a cross-project edge: the child
    # roots itself with the missing-parent warning instead of rejecting the
    # whole snapshot. Explicit cross-project parents are still rejected where
    # one document declares both items (load_backlog_document pins that).
    first = load_backlog_document(_document(_backlog(itemId="child", parentId="x")))
    second = load_backlog_document(
        _document(_backlog(itemId="x", projectId="ghost", title="Other"))
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW, registry=REGISTRY, backlog=first + second
    )
    view = snapshot.get("forge")
    assert [node.item.item_id for node in view.backlog] == ["child"]
    assert view.backlog[0].parent_missing
    assert any("parent" in warning and "x" in warning for warning in view.warnings)


def test_a_null_issue_record_is_a_controlled_error(tmp_path):
    from agentic_sdlc.cli import main as cli_main

    issues = tmp_path / "issues.json"
    issues.write_text(json.dumps([None]), encoding="utf-8")
    assert (
        cli_main(
            [
                "github-backlog",
                "--issues",
                str(issues),
                "--project-id",
                "forge",
                "--repository",
                "acme/forge",
            ]
        )
        == 2
    )


def test_colliding_output_paths_are_rejected(tmp_path):
    from agentic_sdlc.cli import main as cli_main

    issues = tmp_path / "issues.json"
    issues.write_text(json.dumps([]), encoding="utf-8")
    same = tmp_path / "out.json"
    assert (
        cli_main(
            [
                "github-backlog",
                "--issues",
                str(issues),
                "--project-id",
                "forge",
                "--repository",
                "acme/forge",
                "--backlog-output",
                str(same),
                "--incidents-output",
                str(same),
            ]
        )
        == 2
    )
    assert not same.exists()


def test_a_review_state_without_activity_is_not_running():
    waiting = _ledger(*_chain(TO_IMPLEMENTING + ("verifying", "reviewing")))
    view = build_control_center_snapshot(
        generated_at=NOW, registry=REGISTRY, projections=waiting.projections()
    ).get("forge")
    assert view.active == ()
    reviewing = _ledger(
        *_chain(TO_IMPLEMENTING + ("verifying", "reviewing"), activity="reading the diff")
    )
    view = build_control_center_snapshot(
        generated_at=NOW, registry=REGISTRY, projections=reviewing.projections()
    ).get("forge")
    assert [unit.work_unit_id for unit in view.active] == ["wu-1"]


def test_an_approved_unit_keeps_its_label_derived_incident_until_dispatch():
    from agentic_sdlc.control_center import PLAN_EVIDENCE_RULE

    plan_advisory = _incident(
        incidentId=f"acme/forge#7:{PLAN_EVIDENCE_RULE}",
        kind="dispatch-blocked",
        severity="advisory",
        backlogItemId="acme/forge#7",
    )
    approved = _ledger(
        *_chain(
            TO_PLANNED + ("approved",),
            workUnit={"projectId": "forge", "workUnitId": "wu-1", "issueRef": "acme/forge#7"},
        )
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=approved.projections(),
        backlog=load_backlog_document(
            _document(_backlog(itemId="acme/forge#7", issueRef="acme/forge#7"))
        ),
        incidents=load_incident_document(_incidents(plan_advisory)),
    )
    assert len(snapshot.get("forge").incidents) == 1


def test_records_without_an_integer_number_are_a_controlled_error(tmp_path):
    from agentic_sdlc.cli import main as cli_main

    issues = tmp_path / "issues.json"
    issues.write_text(json.dumps([{}]), encoding="utf-8")
    assert (
        cli_main(
            [
                "github-backlog",
                "--issues",
                str(issues),
                "--project-id",
                "forge",
                "--repository",
                "acme/forge",
            ]
        )
        == 2
    )


def test_the_snapshot_output_must_not_overwrite_an_input(tmp_path):
    from agentic_sdlc.cli import main as cli_main

    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"schemaVersion": 1, "projects": []}), encoding="utf-8")
    assert (
        cli_main(
            [
                "control-center",
                "--registry",
                str(registry),
                "--output",
                str(registry),
            ]
        )
        == 2
    )
    assert "projects" in json.loads(registry.read_text(encoding="utf-8"))


def test_approved_and_agent_ready_without_human_review_is_a_blocker():
    incidents = incidents_from_issues(
        [_issue(1, "stalled", ["implementation-approved", "agent-ready"])],
        project_id="forge",
        repository="acme/forge",
        observed_at=NOW,
    )
    assert len(incidents) == 1
    assert incidents[0]["severity"] == "blocker"
    assert "human-review-required" in incidents[0]["summary"]


def test_a_released_spec_without_dispatch_labels_is_surfaced():
    incidents = incidents_from_issues(
        [_issue(1, "released only", ["spec-drafted", "spec-approved"])],
        project_id="forge",
        repository="acme/forge",
        observed_at=NOW,
    )
    assert len(incidents) == 1
    assert incidents[0]["kind"] == "awaiting-human-approval"
    assert "released" in incidents[0]["summary"]


def test_a_self_parent_sub_issue_listing_is_rejected():
    with pytest.raises(ControlCenterError, match="its own parent"):
        parent_links(([{"number": 7}], 7))


def test_malformed_labels_are_a_controlled_error():
    # Truthy and falsey malformed values alike: only a missing or null field
    # may default to no labels.
    for bad in (1, 0, False, "", "x", {}):
        with pytest.raises(ControlCenterError, match="labels must be an array"):
            backlog_document_from_issues(
                [
                    {
                        "number": 1,
                        "title": "bad labels",
                        "state": "open",
                        "labels": bad,
                        "repository_url": "https://api.github.com/repos/acme/forge",
                    }
                ],
                project_id="forge",
                repository="acme/forge",
            )


def test_a_null_or_absent_labels_field_fails_closed():
    # The GitHub export contract always supplies the array ([] when genuinely
    # unlabeled); absence or null is a partial payload, and reading it as
    # "unlabeled" would fabricate a triage diagnosis.
    for broken in (
        {
            "number": 1,
            "title": "null labels",
            "state": "open",
            "labels": None,
            "repository_url": "https://api.github.com/repos/acme/forge",
        },
        {
            "number": 2,
            "title": "no labels",
            "state": "open",
            "repository_url": "https://api.github.com/repos/acme/forge",
        },
    ):
        with pytest.raises(ControlCenterError, match=r"labels must be an array"):
            backlog_document_from_issues([broken], project_id="forge", repository="acme/forge")
    document = backlog_document_from_issues(
        [
            {
                "number": 3,
                "title": "explicitly unlabeled",
                "state": "open",
                "labels": [],
                "repository_url": "https://api.github.com/repos/acme/forge",
            }
        ],
        project_id="forge",
        repository="acme/forge",
    )
    assert [item["labels"] for item in document["items"]] == [[]]


def test_adapter_outputs_must_not_overwrite_inputs(tmp_path):
    from agentic_sdlc.cli import main as cli_main

    issues = tmp_path / "issues.json"
    issues.write_text(json.dumps([]), encoding="utf-8")
    assert (
        cli_main(
            [
                "github-backlog",
                "--issues",
                str(issues),
                "--project-id",
                "forge",
                "--repository",
                "acme/forge",
                "--backlog-output",
                str(issues),
            ]
        )
        == 2
    )
    assert json.loads(issues.read_text(encoding="utf-8")) == []


def test_an_unresolved_explicit_work_unit_never_joins_by_issue_ref():
    # The item names wu-missing; its issueRef matches a DIFFERENT, dispatched
    # unit. The explicit reference is authoritative: the item keeps its
    # declared state, warns about the unknown unit, and an incident linked
    # only through the item is not retired on the unrelated unit's progress.
    ledger = _ledger(
        *_chain(
            TO_IMPLEMENTING,
            workUnit={"projectId": "forge", "workUnitId": "wu-1", "issueRef": "acme/forge#7"},
        )
    )
    stale = _incident(
        incidentId="acme/forge#7:blocked",
        kind="dispatch-blocked",
        severity="blocker",
        backlogItemId="acme/forge#7",
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=ledger.projections(),
        backlog=load_backlog_document(
            _document(
                _backlog(
                    itemId="acme/forge#7",
                    workUnitId="wu-missing",
                    issueRef="acme/forge#7",
                    declaredState="approved",
                )
            )
        ),
        incidents=load_incident_document(_incidents(stale)),
    )
    view = snapshot.get("forge")
    assert view.backlog[0].state == "approved"
    assert view.backlog[0].state_basis == STATE_BASIS_DECLARED
    assert any("names an unknown work unit: wu-missing" in warning for warning in view.warnings)
    assert [item.incident_id for item in view.incidents] == ["acme/forge#7:blocked"]


def test_a_malformed_record_number_is_a_controlled_error():
    # The adapter validates numbers itself: int() would silently coerce 1.9,
    # True or "1" and attach the record's items and incidents to a DIFFERENT
    # real issue when called without the CLI's own boundary check.
    for bad in (1.9, True, "1", 0, -3, None):
        record = {
            "number": bad,
            "title": "odd",
            "state": "open",
            "labels": [],
            "repository_url": "https://api.github.com/repos/acme/forge",
        }
        with pytest.raises(ControlCenterError, match="number must be a positive integer"):
            backlog_document_from_issues([record], project_id="forge", repository="acme/forge")
        with pytest.raises(ControlCenterError, match="number must be a positive integer"):
            incidents_from_issues(
                [record], project_id="forge", repository="acme/forge", observed_at=NOW
            )
        with pytest.raises(ControlCenterError, match="number must be a positive integer"):
            incidents_from_change_requests(
                [
                    {
                        "number": bad,
                        "title": "odd",
                        "state": "open",
                        "draft": False,
                        "mergeable_state": "clean",
                        "base": {"repo": {"full_name": "acme/forge"}},
                    }
                ],
                project_id="forge",
                repository="acme/forge",
                observed_at=NOW,
            )


def test_an_absent_draft_verdict_is_a_controlled_error():
    base = {"repo": {"full_name": "acme/forge"}}
    for pull in (
        {
            "number": 1,
            "title": "no draft",
            "state": "open",
            "mergeable_state": "clean",
            "base": base,
        },
        {
            "number": 1,
            "title": "null draft",
            "state": "open",
            "draft": None,
            "mergeable_state": "clean",
            "base": base,
        },
    ):
        with pytest.raises(ControlCenterError, match="draft must be a boolean"):
            incidents_from_change_requests(
                [pull], project_id="forge", repository="acme/forge", observed_at=NOW
            )


def test_a_hard_linked_output_alias_is_rejected(tmp_path):
    # A different pathname hard-linked to an input shares its inode; writing
    # through it would replace the input document itself.
    import os

    from agentic_sdlc.cli import main as cli_main

    issues = tmp_path / "issues.json"
    issues.write_text(json.dumps([]), encoding="utf-8")
    alias = tmp_path / "innocent-output.json"
    os.link(issues, alias)
    assert (
        cli_main(
            [
                "github-backlog",
                "--issues",
                str(issues),
                "--project-id",
                "forge",
                "--repository",
                "acme/forge",
                "--backlog-output",
                str(alias),
            ]
        )
        == 2
    )
    assert json.loads(issues.read_text(encoding="utf-8")) == []
    backlog = tmp_path / "backlog.json"
    backlog.write_text(
        json.dumps({"schemaVersion": 1, "items": [], "source": "github-issues"}),
        encoding="utf-8",
    )
    snapshot_alias = tmp_path / "snapshot.json"
    os.link(backlog, snapshot_alias)
    assert (
        cli_main(
            [
                "control-center",
                "--backlog",
                str(backlog),
                "--output",
                str(snapshot_alias),
            ]
        )
        == 2
    )
    assert json.loads(backlog.read_text(encoding="utf-8"))["items"] == []


def test_a_boolean_sub_issue_number_is_a_controlled_error():
    with pytest.raises(ControlCenterError, match="number must be a positive integer"):
        parent_links(
            [{"number": True, "parent_issue_url": "https://api.github.com/repos/a/b/issues/1"}]
        )


def test_a_dangling_symlink_scratch_name_is_never_followed(tmp_path):
    import os

    from agentic_sdlc.cli import main as cli_main

    issues = tmp_path / "issues.json"
    issues.write_text(json.dumps([]), encoding="utf-8")
    backlog_out = tmp_path / "out.json"
    victim = tmp_path / "victim.json"
    trap = tmp_path / f"out.json.staged-{os.getpid()}-0"
    trap.symlink_to(victim)
    assert (
        cli_main(
            [
                "github-backlog",
                "--issues",
                str(issues),
                "--project-id",
                "forge",
                "--repository",
                "acme/forge",
                "--backlog-output",
                str(backlog_out),
            ]
        )
        == 0
    )
    assert not victim.exists()
    assert trap.is_symlink()
    assert not backlog_out.is_symlink()
    assert json.loads(backlog_out.read_text(encoding="utf-8"))["items"] == []


def test_an_unrecognized_mergeability_verdict_is_a_controlled_error():
    with pytest.raises(ControlCenterError, match="unrecognized mergeable_state"):
        incidents_from_change_requests(
            [
                {
                    "number": 1,
                    "title": "odd",
                    "state": "open",
                    "mergeable_state": "typo",
                    "draft": False,
                    "base": {"repo": {"full_name": "acme/forge"}},
                }
            ],
            project_id="forge",
            repository="acme/forge",
            observed_at=NOW,
        )


def test_the_work_unit_id_contract_follows_the_ledger():
    # Version-1 ledgers accept any non-empty workUnitId with no length cap,
    # and existing ledgers already hold such ids, so the snapshot loaders
    # apply the same contract: every unit the ledger accepted stays
    # addressable from backlog items and incidents, and joins its status.
    long_id = "w" * 600
    ledger = _ledger(
        *_chain(
            TO_IMPLEMENTING,
            work_unit=long_id,
            workUnit={"projectId": "forge", "workUnitId": long_id, "issueRef": "acme/forge#7"},
        )
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=ledger.projections(),
        backlog=load_backlog_document(
            _document(_backlog(itemId="acme/forge#7", workUnitId=long_id))
        ),
    )
    view = snapshot.get("forge")
    assert view.backlog[0].state == "implementing"
    assert not any("unknown work unit" in warning for warning in view.warnings)


def test_the_retry_remediation_supplies_a_retry_state_input():
    actions = remediations_for("infrastructure-failure")
    command = next(
        action.command for action in actions if action.action_id == "decide-infrastructure-retry"
    )
    assert "--state <retry-state-file>" in command


def test_an_explicitly_named_registry_must_exist(tmp_path):
    # A typo'd --registry must fail the snapshot, never publish an empty or
    # wholly unregistered portfolio; only an OMITTED registry is optional.
    from agentic_sdlc.cli import main as cli_main
    from agentic_sdlc.control_center import read_control_center_inputs
    from agentic_sdlc.project_registry import ProjectRegistryError

    missing = tmp_path / "missing-registry.json"
    with pytest.raises(ProjectRegistryError, match="unable to read project registry"):
        read_control_center_inputs(registry_path=str(missing))
    registry, backlog, incidents = read_control_center_inputs()
    assert registry.project_ids() == ()
    assert (
        cli_main(
            [
                "control-center",
                "--registry",
                str(missing),
                "--output",
                str(tmp_path / "snapshot.json"),
            ]
        )
        == 2
    )
    assert not (tmp_path / "snapshot.json").exists()


def test_an_absent_issue_or_pull_state_is_a_controlled_error():
    # Without an explicit state the adapter has no evidence the record is
    # open, so it fails closed instead of fabricating current work.
    for record in (
        {
            "number": 1,
            "title": "no state",
            "labels": [],
            "repository_url": "https://api.github.com/repos/acme/forge",
        },
        {
            "number": 1,
            "title": "null state",
            "labels": [],
            "state": None,
            "repository_url": "https://api.github.com/repos/acme/forge",
        },
    ):
        with pytest.raises(ControlCenterError, match="state must be"):
            incidents_from_issues(
                [record], project_id="forge", repository="acme/forge", observed_at=NOW
            )
        with pytest.raises(ControlCenterError, match="state must be"):
            backlog_document_from_issues([record], project_id="forge", repository="acme/forge")
    for pull in (
        {
            "number": 1,
            "title": "no state",
            "mergeable_state": "clean",
            "draft": False,
            "base": {"repo": {"full_name": "acme/forge"}},
        },
        {
            "number": 1,
            "title": "null state",
            "state": None,
            "mergeable_state": "clean",
            "draft": False,
            "base": {"repo": {"full_name": "acme/forge"}},
        },
    ):
        with pytest.raises(ControlCenterError, match="state must be"):
            incidents_from_change_requests(
                [pull], project_id="forge", repository="acme/forge", observed_at=NOW
            )


def test_review_labelled_work_missing_the_ready_label_is_reported():
    incidents = incidents_from_issues(
        [_issue(1, "stalled", ["human-review-required"])],
        project_id="forge",
        repository="acme/forge",
        observed_at=NOW,
    )
    assert [item["incidentId"] for item in incidents] == [
        "acme/forge#1:review-required-without-agent-ready"
    ]
    assert incidents[0]["kind"] == "dispatch-blocked"
    assert incidents[0]["severity"] == "degraded"
    # A drafted spec stays the spec rules' concern, not this rule's.
    spec = incidents_from_issues(
        [_issue(2, "spec", ["human-review-required", "spec-drafted"])],
        project_id="forge",
        repository="acme/forge",
        observed_at=NOW,
    )
    assert [item["incidentId"] for item in spec] == ["acme/forge#2:spec-awaiting-human-review"]


def test_the_config_cannot_be_overwritten_by_an_output(tmp_path):
    from agentic_sdlc.cli import main as cli_main

    config = tmp_path / "agentic-sdlc.toml"
    config.write_text(
        "\n".join(
            (
                "version = 1",
                "[project]",
                'id = "acme/forge"',
                'provider = "github"',
                'default_branch = "main"',
            )
        )
        + "\n",
        encoding="utf-8",
    )
    original = config.read_text(encoding="utf-8")
    issues = tmp_path / "issues.json"
    issues.write_text(json.dumps([]), encoding="utf-8")
    assert (
        cli_main(
            [
                "github-backlog",
                "--config",
                str(config),
                "--issues",
                str(issues),
                "--project-id",
                "forge",
                "--repository",
                "acme/forge",
                "--backlog-output",
                str(config),
            ]
        )
        == 2
    )
    assert config.read_text(encoding="utf-8") == original


def test_an_unknown_work_unit_never_retires_on_another_units_proof():
    # The incident names wu-missing; the backlog item it also names resolved
    # to wu-1, which provably dispatched. That other unit's proof must not
    # retire this incident, and the unknown-unit warning must still appear.
    stale = _incident(
        incidentId="acme/forge#7:blocked",
        kind="dispatch-blocked",
        severity="blocker",
        workUnitId="wu-missing",
        backlogItemId="acme/forge#7",
    )
    ledger = _ledger(
        *_chain(
            TO_IMPLEMENTING,
            workUnit={"projectId": "forge", "workUnitId": "wu-1", "issueRef": "acme/forge#7"},
        )
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=ledger.projections(),
        backlog=load_backlog_document(
            _document(_backlog(itemId="acme/forge#7", workUnitId="wu-1"))
        ),
        incidents=load_incident_document(_incidents(stale)),
    )
    view = snapshot.get("forge")
    assert [item.incident_id for item in view.incidents] == ["acme/forge#7:blocked"]
    assert any("unknown work unit" in warning for warning in view.warnings)


def test_a_cross_repository_sub_issue_is_rejected():
    foreign_repo = [
        {
            "number": 7,
            "repository_url": "https://api.github.com/repos/other/repo",
            "parent_issue_url": "https://api.github.com/repos/acme/forge/issues/1",
        }
    ]
    with pytest.raises(ControlCenterError, match="belongs to other/repo"):
        parent_links(foreign_repo, repository="acme/forge")
    foreign_parent = [
        {
            "number": 7,
            "repository_url": "https://api.github.com/repos/acme/forge",
            "parent_issue_url": "https://api.github.com/repos/other/repo/issues/1",
        }
    ]
    with pytest.raises(ControlCenterError, match="another repository"):
        parent_links((foreign_parent, 1), repository="acme/forge")
    local = [
        {
            "number": 7,
            "repository_url": "https://api.github.com/repos/acme/forge",
            "parent_issue_url": "https://api.github.com/repos/acme/forge/issues/1",
        }
    ]
    assert parent_links(local, repository="acme/forge") == {7: 1}
    # Without the repository pin, behavior is unchanged for callers that
    # already resolved locality themselves.
    assert parent_links(foreign_repo) == {7: 1}


def test_a_malformed_repository_is_a_controlled_error():
    for bad in ("typo", "owner/", "/repo", "owner repo", "owner/repo/extra", ""):
        with pytest.raises(ControlCenterError, match="OWNER/REPO"):
            backlog_document_from_issues([], project_id="forge", repository=bad)
        with pytest.raises(ControlCenterError, match="OWNER/REPO"):
            incidents_from_issues([], project_id="forge", repository=bad, observed_at=NOW)
        with pytest.raises(ControlCenterError, match="OWNER/REPO"):
            incidents_from_change_requests([], project_id="forge", repository=bad, observed_at=NOW)


def test_configured_label_names_drive_the_dispatch_rules():
    issues = [_issue(1, "renamed ready", ["claude-ready", "human-review-required"])]
    # With the platform defaults, claude-ready is an unknown label: the item
    # reads as review-labelled work missing its ready label, not as the plan
    # advisory the project's real labels would produce.
    defaults = incidents_from_issues(
        issues, project_id="forge", repository="acme/forge", observed_at=NOW
    )
    assert [item["incidentId"] for item in defaults] == [
        "acme/forge#1:review-required-without-agent-ready"
    ]
    configured = LifecycleLabels(ready="claude-ready")
    incidents = incidents_from_issues(
        issues,
        project_id="forge",
        repository="acme/forge",
        observed_at=NOW,
        labels=configured,
    )
    assert [item["incidentId"] for item in incidents] == [
        "acme/forge#1:plan-ready-awaiting-dispatch-evidence"
    ]
    assert "claude-ready" in incidents[0]["summary"]
    document = backlog_document_from_issues(
        [_issue(2, "approved", ["implementation-approved", "claude-ready"])],
        project_id="forge",
        repository="acme/forge",
        labels=configured,
    )
    assert document["items"][0]["declaredState"] == "approved"


def test_lifecycle_label_names_are_validated():
    with pytest.raises(ControlCenterError, match="non-empty string"):
        LifecycleLabels(ready="")
    with pytest.raises(ControlCenterError, match="non-empty string"):
        LifecycleLabels(ready=None)  # type: ignore[arg-type]
    with pytest.raises(ControlCenterError, match="distinct"):
        LifecycleLabels(ready="same", human_review="SAME")
    with pytest.raises(ControlCenterError, match="distinct"):
        LifecycleLabels(ready="spec-drafted")


def test_the_cli_reads_configured_labels_from_the_policy(tmp_path):
    from agentic_sdlc.cli import main as cli_main

    config = tmp_path / "agentic-sdlc.toml"
    config.write_text(
        "\n".join(
            (
                "version = 1",
                "[project]",
                'id = "acme/forge"',
                'provider = "github"',
                'default_branch = "main"',
                "[automation]",
                'ready_label = "claude-ready"',
                'human_review_label = "human-review-required"',
            )
        )
        + "\n",
        encoding="utf-8",
    )
    issues = tmp_path / "issues.json"
    issues.write_text(
        json.dumps(
            [
                {
                    "number": 1,
                    "title": "renamed ready",
                    "state": "open",
                    "labels": ["claude-ready", "human-review-required"],
                    "repository_url": "https://api.github.com/repos/acme/forge",
                }
            ]
        ),
        encoding="utf-8",
    )
    incidents_out = tmp_path / "incidents.json"
    backlog_out = tmp_path / "backlog.json"
    assert (
        cli_main(
            [
                "github-backlog",
                "--config",
                str(config),
                "--issues",
                str(issues),
                "--project-id",
                "forge",
                "--repository",
                "acme/forge",
                "--backlog-output",
                str(backlog_out),
                "--incidents-output",
                str(incidents_out),
            ]
        )
        == 0
    )
    written = json.loads(incidents_out.read_text(encoding="utf-8"))
    assert [item["incidentId"] for item in written["incidents"]] == [
        "acme/forge#1:plan-ready-awaiting-dispatch-evidence"
    ]


def test_the_config_must_belong_to_the_exported_repository(tmp_path):
    # Another repository's labels (or a non-GitHub policy) would silently
    # misread every issue, so the policy is bound to this export.
    from agentic_sdlc.cli import main as cli_main

    issues = tmp_path / "issues.json"
    issues.write_text(json.dumps([]), encoding="utf-8")

    def run(config) -> int:
        return cli_main(
            [
                "github-backlog",
                "--config",
                str(config),
                "--issues",
                str(issues),
                "--project-id",
                "forge",
                "--repository",
                "acme/forge",
                "--backlog-output",
                str(tmp_path / "backlog.json"),
                # The bare open issue below yields an untriaged advisory, and
                # a computed incident always needs a destination.
                "--incidents-output",
                str(tmp_path / "incidents.json"),
            ]
        )

    def write_config(path, *, project: str, provider: str):
        path.write_text(
            "\n".join(
                (
                    "version = 1",
                    "[project]",
                    f'id = "{project}"',
                    f'provider = "{provider}"',
                    'default_branch = "main"',
                )
            )
            + "\n",
            encoding="utf-8",
        )
        return path

    foreign = write_config(tmp_path / "foreign.toml", project="other/repo", provider="github")
    assert run(foreign) == 2
    gitlab = write_config(tmp_path / "gitlab.toml", project="acme/forge", provider="gitlab")
    assert run(gitlab) == 2
    matching = write_config(tmp_path / "match.toml", project="ACME/forge", provider="github")
    issues.write_text(
        json.dumps(
            [
                {
                    "number": 1,
                    "title": "spelled",
                    "state": "open",
                    "labels": [],
                    "repository_url": "https://api.github.com/repos/acme/forge",
                }
            ]
        ),
        encoding="utf-8",
    )
    assert run(matching) == 0
    # The policy's spelling is canonical: a case-differing --repository must
    # not leak into references, or ledger joins by exact string would miss.
    written = json.loads((tmp_path / "backlog.json").read_text(encoding="utf-8"))
    assert written["items"][0]["itemId"] == "acme/forge#1"


def test_staged_replacement_preserves_destination_mode(tmp_path):
    import stat

    from agentic_sdlc.cli import main as cli_main

    issues = tmp_path / "issues.json"
    issues.write_text(json.dumps([]), encoding="utf-8")
    backlog_out = tmp_path / "backlog.json"
    backlog_out.write_text("old", encoding="utf-8")
    backlog_out.chmod(0o600)
    assert (
        cli_main(
            [
                "github-backlog",
                "--issues",
                str(issues),
                "--project-id",
                "forge",
                "--repository",
                "acme/forge",
                "--backlog-output",
                str(backlog_out),
            ]
        )
        == 0
    )
    assert json.loads(backlog_out.read_text(encoding="utf-8"))["items"] == []
    assert stat.S_IMODE(backlog_out.stat().st_mode) == 0o600


def test_a_ledger_style_work_unit_id_is_accepted_and_joins():
    # The ledger accepts any bounded non-empty string as a work-unit id, so
    # the Control Center must address the same ids from backlog items and
    # incidents instead of applying the tracker-reference character set.
    unit = "wu@2026/10#12:alpha β"
    ledger = _ledger(
        *_chain(
            TO_IMPLEMENTING,
            work_unit=unit,
            workUnit={"projectId": "forge", "workUnitId": unit, "issueRef": "acme/forge#7"},
        )
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=ledger.projections(),
        backlog=load_backlog_document(_document(_backlog(itemId="acme/forge#7", workUnitId=unit))),
        incidents=load_incident_document(
            _incidents(
                _incident(
                    incidentId="acme/forge#7:ci",
                    kind="deterministic-ci-failed",
                    severity="blocker",
                    workUnitId=unit,
                )
            )
        ),
    )
    view = snapshot.get("forge")
    assert view.backlog[0].state == "implementing"
    assert not any("unknown work unit" in warning for warning in view.warnings)


def test_direct_work_unit_evidence_retires_without_a_backlog():
    # An incident naming only a work unit must retire on that unit's own
    # ledger state even when no backlog document was supplied at all.
    stale = _incident(
        incidentId="acme/forge#7:blocked",
        kind="dispatch-blocked",
        severity="blocker",
        workUnitId="wu-1",
    )
    ledger = _ledger(
        *_chain(
            TO_IMPLEMENTING,
            workUnit={"projectId": "forge", "workUnitId": "wu-1", "issueRef": "acme/forge#7"},
        )
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=ledger.projections(),
        incidents=load_incident_document(_incidents(stale)),
    )
    assert snapshot.get("forge").incidents == ()


def test_a_non_string_pull_state_is_a_controlled_error():
    for bad in (False, 1, [], {}, "merged", ""):
        with pytest.raises(ControlCenterError, match="state must be"):
            incidents_from_change_requests(
                [
                    {
                        "number": 1,
                        "title": "odd state",
                        "state": bad,
                        "mergeable_state": "dirty",
                        "draft": False,
                        "base": {"repo": {"full_name": "acme/forge"}},
                    }
                ],
                project_id="forge",
                repository="acme/forge",
                observed_at=NOW,
            )


def test_a_bare_sub_issue_record_without_a_parent_is_rejected():
    for record in (
        {"number": 5},
        {"number": 5, "parent_issue_url": None},
    ):
        with pytest.raises(ControlCenterError, match="no usable parent_issue_url"):
            parent_links([record])
    # A string that is not the recognized API shape is its own controlled
    # error: a web URL's trailing number must never become a parent.
    with pytest.raises(ControlCenterError, match="not a recognized GitHub issue URL"):
        parent_links([{"number": 5, "parent_issue_url": "https://api.github.com/not-an-issue"}])


def test_a_failed_replacement_restores_the_previous_pair(tmp_path, monkeypatch):
    # Staging alone is not enough: when the SECOND os.replace fails, the
    # first destination was already replaced and must be rolled back so the
    # pair on disk still reads as one export, the previous one.
    import os

    from agentic_sdlc.cli import main as cli_main

    issues = tmp_path / "issues.json"
    issues.write_text(json.dumps([]), encoding="utf-8")
    backlog_out = tmp_path / "backlog.json"
    backlog_out.write_text("stale backlog", encoding="utf-8")
    incidents_out = tmp_path / "incidents.json"
    incidents_out.write_text("stale incidents", encoding="utf-8")
    real_replace = os.replace
    tripped = []

    def flaky(source, destination):
        if not tripped and str(destination) == str(incidents_out) and ".staged-" in str(source):
            tripped.append(True)
            raise OSError("simulated replace failure")
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", flaky)
    assert (
        cli_main(
            [
                "github-backlog",
                "--issues",
                str(issues),
                "--project-id",
                "forge",
                "--repository",
                "acme/forge",
                "--backlog-output",
                str(backlog_out),
                "--incidents-output",
                str(incidents_out),
            ]
        )
        == 2
    )
    assert backlog_out.read_text(encoding="utf-8") == "stale backlog"
    assert incidents_out.read_text(encoding="utf-8") == "stale incidents"
    assert not list(tmp_path.glob("*.staged-*"))
    assert not list(tmp_path.glob("*.backup-*"))


def test_an_output_naming_an_existing_directory_is_rejected_before_any_write(tmp_path):
    from agentic_sdlc.cli import main as cli_main

    issues = tmp_path / "issues.json"
    issues.write_text(json.dumps([]), encoding="utf-8")
    backlog_out = tmp_path / "backlog.json"
    backlog_out.write_text("stale backlog", encoding="utf-8")
    incidents_out = tmp_path / "incidents-directory"
    incidents_out.mkdir()
    assert (
        cli_main(
            [
                "github-backlog",
                "--issues",
                str(issues),
                "--project-id",
                "forge",
                "--repository",
                "acme/forge",
                "--backlog-output",
                str(backlog_out),
                "--incidents-output",
                str(incidents_out),
            ]
        )
        == 2
    )
    assert backlog_out.read_text(encoding="utf-8") == "stale backlog"
    assert not list(tmp_path.glob("*.staged-*"))


def test_adapter_outputs_are_staged_as_a_pair(tmp_path):
    # A failed incident write must not leave a freshly replaced backlog next
    # to a stale incident file: the pair would describe two different exports.
    from agentic_sdlc.cli import main as cli_main

    issues = tmp_path / "issues.json"
    issues.write_text(json.dumps([]), encoding="utf-8")
    backlog_out = tmp_path / "backlog.json"
    backlog_out.write_text("stale backlog", encoding="utf-8")
    incidents_out = tmp_path / "missing-directory" / "incidents.json"
    assert (
        cli_main(
            [
                "github-backlog",
                "--issues",
                str(issues),
                "--project-id",
                "forge",
                "--repository",
                "acme/forge",
                "--backlog-output",
                str(backlog_out),
                "--incidents-output",
                str(incidents_out),
            ]
        )
        == 2
    )
    assert backlog_out.read_text(encoding="utf-8") == "stale backlog"
    assert not incidents_out.exists()
    assert not list(tmp_path.glob("*.staged-*"))


def test_a_conflicted_pull_with_a_failing_check_reports_both_blockers():
    incidents = incidents_from_change_requests(
        [
            {
                "number": 7,
                "title": "both",
                "state": "open",
                "mergeable_state": "dirty",
                "draft": False,
                "base": {"repo": {"full_name": "acme/forge"}},
            }
        ],
        project_id="forge",
        repository="acme/forge",
        observed_at=NOW,
        failing={7: "test"},
    )
    assert [item["kind"] for item in incidents] == [
        "merge-conflict",
        "deterministic-ci-failed",
    ]


def test_a_malformed_mergeability_value_is_a_controlled_error():
    with pytest.raises(ControlCenterError, match="mergeable_state must be a string"):
        incidents_from_change_requests(
            [
                {
                    "number": 1,
                    "title": "bad",
                    "state": "open",
                    "mergeable_state": [],
                    "draft": False,
                    "base": {"repo": {"full_name": "acme/forge"}},
                }
            ],
            project_id="forge",
            repository="acme/forge",
            observed_at=NOW,
        )


def test_sub_issue_records_without_numbers_are_a_controlled_error(tmp_path):
    from agentic_sdlc.cli import main as cli_main

    issues = tmp_path / "issues.json"
    issues.write_text(json.dumps([]), encoding="utf-8")
    bad = tmp_path / "sub.json"
    bad.write_text(json.dumps([{"parent_issue_url": "x"}]), encoding="utf-8")
    assert (
        cli_main(
            [
                "github-backlog",
                "--issues",
                str(issues),
                "--sub-issues",
                f"1={bad}",
                "--project-id",
                "forge",
                "--repository",
                "acme/forge",
            ]
        )
        == 2
    )


def test_ledger_proof_retires_a_stale_triage_incident():
    stale = _incident(
        incidentId="acme/forge#7:untriaged",
        kind="triage-needed",
        severity="advisory",
        backlogItemId="acme/forge#7",
    )
    ledger = _ledger(
        *_chain(
            TO_IMPLEMENTING,
            workUnit={"projectId": "forge", "workUnitId": "wu-1", "issueRef": "acme/forge#7"},
        )
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=ledger.projections(),
        backlog=load_backlog_document(
            _document(_backlog(itemId="acme/forge#7", issueRef="acme/forge#7"))
        ),
        incidents=load_incident_document(_incidents(stale)),
    )
    assert snapshot.get("forge").incidents == ()


def test_a_triage_incident_stays_while_the_ledger_shows_only_intake():
    stale = _incident(
        incidentId="acme/forge#7:untriaged",
        kind="triage-needed",
        severity="advisory",
        backlogItemId="acme/forge#7",
    )
    ledger = _ledger(
        *_chain(
            ("intake",),
            workUnit={"projectId": "forge", "workUnitId": "wu-1", "issueRef": "acme/forge#7"},
        )
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=ledger.projections(),
        backlog=load_backlog_document(
            _document(_backlog(itemId="acme/forge#7", issueRef="acme/forge#7"))
        ),
        incidents=load_incident_document(_incidents(stale)),
    )
    assert len(snapshot.get("forge").incidents) == 1


def test_approval_proof_retires_a_stale_approval_wait():
    stale = _incident(
        incidentId="acme/forge#7:spec",
        kind="awaiting-human-approval",
        severity="degraded",
        backlogItemId="acme/forge#7",
    )
    ledger = _ledger(
        *_chain(
            TO_PLANNED + ("approved",),
            workUnit={"projectId": "forge", "workUnitId": "wu-1", "issueRef": "acme/forge#7"},
        )
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=ledger.projections(),
        backlog=load_backlog_document(
            _document(_backlog(itemId="acme/forge#7", issueRef="acme/forge#7"))
        ),
        incidents=load_incident_document(_incidents(stale)),
    )
    assert snapshot.get("forge").incidents == ()


def test_approval_proof_does_not_retire_a_released_spec_label_loss():
    # The released-spec rule reports MISSING dispatch labels, which an
    # approved unit still needs before anything can fire: approval proof
    # leaves it standing, and only dispatch proof retires it.
    stale = _incident(
        incidentId="acme/forge#7:released-spec-without-dispatch-labels",
        kind="awaiting-human-approval",
        severity="degraded",
        backlogItemId="acme/forge#7",
        evidenceRef="acme/forge#7",
    )
    backlog = load_backlog_document(
        _document(_backlog(itemId="acme/forge#7", issueRef="acme/forge#7"))
    )
    approved = _ledger(
        *_chain(
            TO_PLANNED + ("approved",),
            workUnit={"projectId": "forge", "workUnitId": "wu-1", "issueRef": "acme/forge#7"},
        )
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=approved.projections(),
        backlog=backlog,
        incidents=load_incident_document(_incidents(stale)),
    )
    kept = snapshot.get("forge").incidents
    assert [item.incident_id for item in kept] == [
        "acme/forge#7:released-spec-without-dispatch-labels"
    ]
    dispatched = _ledger(
        *_chain(
            TO_PLANNED + ("approved", "dispatched"),
            workUnit={"projectId": "forge", "workUnitId": "wu-1", "issueRef": "acme/forge#7"},
        )
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=dispatched.projections(),
        backlog=backlog,
        incidents=load_incident_document(_incidents(stale)),
    )
    assert snapshot.get("forge").incidents == ()


def test_a_malformed_parents_map_is_a_controlled_error():
    # Key equality would let {True: 2} or {1.0: 2} attach the real issue #1.
    for bad_map in ({True: 2}, {1.0: 2}, {0: 2}, {1: True}, {1: 0}):
        with pytest.raises(ControlCenterError, match="parents map"):
            backlog_document_from_issues(
                [_issue(1, "child"), _issue(2, "parent")],
                project_id="forge",
                repository="acme/forge",
                parents=bad_map,
            )


def test_a_lifecycle_restart_voids_crossed_gate_history():
    # dispatched -> blocked -> triaged is a legal restart: the new lifecycle
    # must pass every gate again, so the fresh label incident stays.
    fresh = _incident(
        incidentId="acme/forge#7:blocked-labels",
        kind="dispatch-blocked",
        severity="blocker",
        backlogItemId="acme/forge#7",
    )
    ledger = _ledger(
        *_chain(
            TO_PLANNED + ("approved", "dispatched", "blocked", "triaged"),
            workUnit={"projectId": "forge", "workUnitId": "wu-1", "issueRef": "acme/forge#7"},
        )
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=ledger.projections(),
        backlog=load_backlog_document(
            _document(_backlog(itemId="acme/forge#7", issueRef="acme/forge#7"))
        ),
        incidents=load_incident_document(_incidents(fresh)),
    )
    assert [item.incident_id for item in snapshot.get("forge").incidents] == [
        "acme/forge#7:blocked-labels"
    ]


def test_planning_history_retires_the_plan_advisory_after_a_block():
    from agentic_sdlc.control_center import PLAN_EVIDENCE_RULE

    # planned -> approval-pending -> blocked: planning provably ran, so the
    # advisory telling the operator to confirm the plan run is stale even
    # though the unit now sits in a pre-plan-reachable state.
    advisory = _incident(
        incidentId=f"acme/forge#7:{PLAN_EVIDENCE_RULE}",
        kind="awaiting-dispatch-evidence",
        severity="advisory",
        backlogItemId="acme/forge#7",
        evidenceRef="acme/forge#7",
    )
    ledger = _ledger(
        *_chain(
            TO_PLANNED + ("approval-pending", "blocked"),
            workUnit={"projectId": "forge", "workUnitId": "wu-1", "issueRef": "acme/forge#7"},
        )
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=ledger.projections(),
        backlog=load_backlog_document(
            _document(_backlog(itemId="acme/forge#7", issueRef="acme/forge#7"))
        ),
        incidents=load_incident_document(_incidents(advisory)),
    )
    assert snapshot.get("forge").incidents == ()


def test_incident_only_foreign_references_warn():
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        incidents=load_incident_document(
            _incidents(
                _incident(
                    incidentId="acme/other!7:merge-conflict",
                    projectId="forge",
                    kind="merge-conflict",
                    severity="blocker",
                    evidenceRef="acme/other!7",
                )
            )
        ),
    )
    assert any(
        "references repository acme/other" in warning for warning in snapshot.get("forge").warnings
    )


def test_the_payload_spelling_canonicalizes_the_repository(tmp_path):
    from agentic_sdlc.cli import main as cli_main

    issues = tmp_path / "issues.json"
    issues.write_text(
        json.dumps(
            [
                {
                    "number": 1,
                    "title": "cased",
                    "state": "open",
                    "labels": [],
                    "repository_url": "https://api.github.com/repos/Acme/Forge",
                }
            ]
        ),
        encoding="utf-8",
    )
    backlog_out = tmp_path / "backlog.json"
    assert (
        cli_main(
            [
                "github-backlog",
                "--issues",
                str(issues),
                "--project-id",
                "forge",
                "--repository",
                "acme/forge",
                "--backlog-output",
                str(backlog_out),
                # The bare open issue yields an untriaged advisory, and a
                # computed incident always needs a destination.
                "--incidents-output",
                str(tmp_path / "incidents.json"),
            ]
        )
        == 0
    )
    written = json.loads(backlog_out.read_text(encoding="utf-8"))
    assert written["items"][0]["itemId"] == "Acme/Forge#1"
    foreign = tmp_path / "foreign.json"
    foreign.write_text(
        json.dumps(
            [
                {
                    "number": 1,
                    "title": "foreign",
                    "state": "open",
                    "labels": [],
                    "repository_url": "https://api.github.com/repos/other/repo",
                }
            ]
        ),
        encoding="utf-8",
    )
    assert (
        cli_main(
            [
                "github-backlog",
                "--issues",
                str(foreign),
                "--project-id",
                "forge",
                "--repository",
                "acme/forge",
                "--backlog-output",
                str(tmp_path / "out.json"),
            ]
        )
        == 2
    )


def test_proven_approval_replaces_the_plan_advisory_guidance():
    from agentic_sdlc.control_center import PLAN_EVIDENCE_RULE

    advisory = _incident(
        incidentId=f"acme/forge#7:{PLAN_EVIDENCE_RULE}",
        kind="awaiting-dispatch-evidence",
        severity="advisory",
        backlogItemId="acme/forge#7",
        evidenceRef="acme/forge#7",
        # The adapter's hold names the CONFIGURED labels; here the policy
        # renamed the implementation label, and the replacement must keep
        # that literal spelling so the operator applies a label that fires.
        summary=(
            "Both planning labels are present, but the bundled plan trigger fires "
            "only on the claude-ready label event: confirm a plan run started, and "
            "re-apply claude-ready to re-kick if none did; implementation dispatch "
            "then fires on the go-implement label event"
        ),
    )
    approved = _ledger(
        *_chain(
            TO_PLANNED + ("approved",),
            workUnit={"projectId": "forge", "workUnitId": "wu-1", "issueRef": "acme/forge#7"},
        )
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=approved.projections(),
        backlog=load_backlog_document(
            _document(_backlog(itemId="acme/forge#7", issueRef="acme/forge#7"))
        ),
        incidents=load_incident_document(_incidents(advisory)),
    )
    kept = snapshot.get("forge").incidents
    assert [item.incident_id for item in kept] == [f"acme/forge#7:{PLAN_EVIDENCE_RULE}"]
    # The replacement leads with the proven approval and quotes the hold, so
    # the configured implementation label survives into the guidance.
    assert kept[0].summary.startswith("Plan and approval are proven by the ledger")
    assert "go-implement" in kept[0].summary
    assert "re-apply the implementation approval label it names" in kept[0].summary
    # Not an advisory any more: dispatch provably cannot occur.
    assert kept[0].kind == "dispatch-blocked"
    assert kept[0].severity.value == "blocker"


def test_foreign_repository_data_under_a_registered_project_warns():
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        backlog=load_backlog_document(
            _document(_backlog(itemId="acme/other#7", issueRef="acme/other#7"))
        ),
    )
    view = snapshot.get("forge")
    assert any(
        "references repository acme/other" in warning and "does not declare" in warning
        for warning in view.warnings
    )


def test_crossed_gate_evidence_survives_a_later_pre_gate_state():
    # approved -> dispatched -> blocked: the unit sits in a state reachable
    # before dispatch, but its durable transitions prove dispatch fired, so
    # the stale label incident must still retire.
    stale = _incident(
        incidentId="acme/forge#7:blocked-labels",
        kind="dispatch-blocked",
        severity="blocker",
        backlogItemId="acme/forge#7",
    )
    ledger = _ledger(
        *_chain(
            TO_PLANNED + ("approved", "dispatched", "blocked"),
            workUnit={"projectId": "forge", "workUnitId": "wu-1", "issueRef": "acme/forge#7"},
        )
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=ledger.projections(),
        backlog=load_backlog_document(
            _document(_backlog(itemId="acme/forge#7", issueRef="acme/forge#7"))
        ),
        incidents=load_incident_document(_incidents(stale)),
    )
    assert snapshot.get("forge").incidents == ()


def test_a_malformed_declared_parent_is_a_controlled_error():
    for bad in (True, 0, -2, 1.0, "1"):
        with pytest.raises(ControlCenterError, match="declared parent number"):
            parent_links(([{"number": 2}], bad))
    with pytest.raises(ControlCenterError, match="must be an object"):
        parent_links(([1, 2], 1))


def test_a_foreign_payload_behind_a_valid_record_is_still_rejected():
    from agentic_sdlc.github_backlog import canonical_repository

    records = [
        {"number": 1, "repository_url": "https://api.github.com/repos/acme/forge"},
        {"number": 2, "repository_url": "https://api.github.com/repos/other/repo"},
    ]
    with pytest.raises(ControlCenterError, match="belongs to other/repo"):
        canonical_repository(records, "acme/forge")


def test_an_issue_without_its_repository_identity_is_rejected():
    # Both builders require every issue to prove which repository it belongs
    # to: absent, unrecognizable, and foreign repository_url are all errors.
    absent = _issue(1, "no identity")
    del absent["repository_url"]
    unrecognized = _issue(1, "bad identity")
    unrecognized["repository_url"] = "not-a-repository-url"
    foreign = _issue(1, "foreign identity")
    foreign["repository_url"] = "https://api.github.com/repos/other/repo"
    cases = [
        (absent, "repository_url is required"),
        (unrecognized, "not a recognized repository URL"),
        (foreign, "belongs to other/repo"),
    ]
    for issue, message in cases:
        with pytest.raises(ControlCenterError, match=message):
            _export(issue)
        with pytest.raises(ControlCenterError, match=message):
            incidents_from_issues(
                [issue], project_id="forge", repository="acme/forge", observed_at=NOW
            )


def test_nested_repository_references_participate_in_ownership_checks():
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        incidents=load_incident_document(
            _incidents(
                _incident(
                    incidentId="other/group/assets#7:untriaged",
                    projectId="forge",
                    kind="triage-needed",
                    severity="advisory",
                    evidenceRef="other/group/assets#7",
                )
            )
        ),
    )
    assert any(
        "references repository other/group/assets" in warning
        for warning in snapshot.get("forge").warnings
    )


def test_a_pull_detail_from_another_repository_is_rejected():
    foreign = {
        "number": 7,
        "title": "foreign",
        "state": "open",
        "draft": False,
        "mergeable_state": "dirty",
        "base": {"repo": {"full_name": "other/repo"}},
    }
    with pytest.raises(ControlCenterError, match="belongs to other/repo"):
        incidents_from_change_requests(
            [foreign], project_id="forge", repository="acme/forge", observed_at=NOW
        )
    local = dict(foreign, base={"repo": {"full_name": "ACME/forge"}})
    incidents = incidents_from_change_requests(
        [local], project_id="forge", repository="acme/forge", observed_at=NOW
    )
    assert [item["kind"] for item in incidents] == ["merge-conflict"]


def test_proven_approval_restates_the_spec_hold_as_dispatch_blocked():
    stale = _incident(
        incidentId="acme/forge#7:released-spec-without-dispatch-labels",
        kind="awaiting-human-approval",
        severity="degraded",
        backlogItemId="acme/forge#7",
        evidenceRef="acme/forge#7",
        # The adapter's hold names the project's CONFIGURED labels; here the
        # policy renamed the ready label, so the replacement must keep that
        # literal spelling rather than substitute default conventions.
        summary=(
            "Specification released, but no dispatch label follows it: the work "
            "waits on implementation-approved with claude-ready and "
            "human-review-required"
        ),
    )
    approved = _ledger(
        *_chain(
            TO_PLANNED + ("approved",),
            workUnit={"projectId": "forge", "workUnitId": "wu-1", "issueRef": "acme/forge#7"},
        )
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=approved.projections(),
        backlog=load_backlog_document(
            _document(_backlog(itemId="acme/forge#7", issueRef="acme/forge#7"))
        ),
        incidents=load_incident_document(_incidents(stale)),
    )
    kept = snapshot.get("forge").incidents
    assert [item.kind for item in kept] == ["dispatch-blocked"]
    # The replacement blocks dispatch, so it must carry the blocker
    # severity rather than inherit the hold's advisory one.
    assert kept[0].severity.value == "blocker"
    assert "Approval is proven" in kept[0].summary
    # The hold is quoted verbatim, so every configured label name survives,
    # including the renamed ready label an operator actually has to apply.
    assert "claude-ready" in kept[0].summary
    assert "human-review-required" in kept[0].summary
    document_incident = snapshot.get("forge").as_dict()["incidents"][0]
    assert any(
        action["actionId"] == "re-run-dispatch" for action in document_incident["remediations"]
    )


def test_the_console_catalogue_matches_the_declared_catalogue():
    import re as _re
    from pathlib import Path

    from agentic_sdlc.control_center import REMEDIATION_CATALOGUE

    html = Path("examples/control-center/control-center.html").read_text(encoding="utf-8")
    match = _re.search(r"const REMEDIATIONS = (\{.*?\n\});", html, _re.S)
    assert match is not None, "the console must embed the declared catalogue"
    embedded = json.loads(match.group(1))
    expected = {
        kind: [action.as_dict() for action in actions]
        for kind, actions in REMEDIATION_CATALOGUE.items()
    }
    assert embedded == expected


def test_a_prose_epic_mention_is_not_an_epic():
    document = _export(_issue(1, "Fix epic timeout handling"))
    assert document["items"][0]["kind"] == "task"


def test_a_pull_without_its_target_repository_is_rejected():
    for base in (None, "x", {}, {"repo": {}}, {"repo": {"full_name": "  "}}):
        pull = {
            "number": 1,
            "title": "partial",
            "state": "open",
            "draft": False,
            "mergeable_state": "clean",
        }
        if base is not None:
            pull["base"] = base
        with pytest.raises(ControlCenterError, match="base.repo.full_name is required"):
            incidents_from_change_requests(
                [pull], project_id="forge", repository="acme/forge", observed_at=NOW
            )


def test_underscore_and_dot_segments_participate_in_ownership_checks():
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        incidents=load_incident_document(
            _incidents(
                _incident(
                    incidentId="foreign-underscore:untriaged",
                    projectId="forge",
                    kind="triage-needed",
                    severity="advisory",
                    evidenceRef="_other/repo#7",
                )
            )
        ),
    )
    assert any(
        "references repository _other/repo" in warning for warning in snapshot.get("forge").warnings
    )


def test_a_bare_slash_evidence_reference_never_warns():
    # evidenceRef accepts arbitrary text: a bare "build/123" is not a
    # repository reference, which needs the #/! number shape.
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        incidents=load_incident_document(
            _incidents(
                _incident(
                    incidentId="run-7-infra",
                    projectId="forge",
                    kind="infrastructure-failure",
                    severity="degraded",
                    evidenceRef="build/123",
                )
            )
        ),
    )
    assert not any("does not declare" in warning for warning in snapshot.get("forge").warnings)


def test_non_repository_evidence_references_never_warn():
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        incidents=load_incident_document(
            _incidents(
                _incident(
                    incidentId="run-7-infra",
                    projectId="forge",
                    kind="infrastructure-failure",
                    severity="degraded",
                    evidenceRef="https://ci.example/runs/7",
                )
            )
        ),
    )
    assert not any("does not declare" in warning for warning in snapshot.get("forge").warnings)


def test_a_malformed_failing_key_is_a_controlled_error():
    # Dictionary equality would let True or 1.0 match change request 1.
    for bad in (True, 1.0, 0, "1"):
        with pytest.raises(ControlCenterError, match="failing-check key"):
            incidents_from_change_requests(
                [
                    {
                        "number": 1,
                        "title": "green",
                        "state": "open",
                        "draft": False,
                        "mergeable_state": "clean",
                        "base": {"repo": {"full_name": "acme/forge"}},
                    }
                ],
                project_id="forge",
                repository="acme/forge",
                observed_at=NOW,
                failing={bad: "test"},
            )


def test_approval_proof_does_not_retire_the_spec_approval_hold():
    # spec-without-approval also reports missing dispatch labels: an approved
    # unit still lacks the ready label, so only dispatch proof retires it.
    stale = _incident(
        incidentId="acme/forge#7:spec-without-approval",
        kind="awaiting-human-approval",
        severity="degraded",
        backlogItemId="acme/forge#7",
        evidenceRef="acme/forge#7",
    )
    backlog = load_backlog_document(
        _document(_backlog(itemId="acme/forge#7", issueRef="acme/forge#7"))
    )
    approved = _ledger(
        *_chain(
            TO_PLANNED + ("approved",),
            workUnit={"projectId": "forge", "workUnitId": "wu-1", "issueRef": "acme/forge#7"},
        )
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=approved.projections(),
        backlog=backlog,
        incidents=load_incident_document(_incidents(stale)),
    )
    assert [item.incident_id for item in snapshot.get("forge").incidents] == [
        "acme/forge#7:spec-without-approval"
    ]
    dispatched = _ledger(
        *_chain(
            TO_PLANNED + ("approved", "dispatched"),
            workUnit={"projectId": "forge", "workUnitId": "wu-1", "issueRef": "acme/forge#7"},
        )
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=dispatched.projections(),
        backlog=backlog,
        incidents=load_incident_document(_incidents(stale)),
    )
    assert snapshot.get("forge").incidents == ()


def test_a_malformed_issue_state_is_a_controlled_error():
    for bad in (False, 1, "merged", ""):
        with pytest.raises(ControlCenterError, match="state must be"):
            incidents_from_issues(
                [
                    {
                        "number": 1,
                        "title": "odd",
                        "state": bad,
                        "labels": [],
                        "repository_url": "https://api.github.com/repos/acme/forge",
                    }
                ],
                project_id="forge",
                repository="acme/forge",
                observed_at=NOW,
            )
        with pytest.raises(ControlCenterError, match="state must be"):
            backlog_document_from_issues(
                [
                    {
                        "number": 1,
                        "title": "odd",
                        "state": bad,
                        "labels": [],
                        "repository_url": "https://api.github.com/repos/acme/forge",
                    }
                ],
                project_id="forge",
                repository="acme/forge",
            )


def test_scratch_paths_never_collide_with_sibling_outputs(tmp_path):
    # An output deliberately named like another output's staging or backup
    # path must survive the group write intact.
    import os

    from agentic_sdlc.cli import main as cli_main

    for tag in ("staged", "backup"):
        base = tmp_path / tag
        base.mkdir()
        issues = base / "issues.json"
        issues.write_text(json.dumps([]), encoding="utf-8")
        backlog_out = base / "out.json"
        backlog_out.write_text("old backlog", encoding="utf-8")
        incidents_out = base / f"out.json.{tag}-{os.getpid()}-0"
        incidents_out.write_text("old incidents", encoding="utf-8")
        assert (
            cli_main(
                [
                    "github-backlog",
                    "--issues",
                    str(issues),
                    "--project-id",
                    "forge",
                    "--repository",
                    "acme/forge",
                    "--backlog-output",
                    str(backlog_out),
                    "--incidents-output",
                    str(incidents_out),
                ]
            )
            == 0
        )
        assert json.loads(backlog_out.read_text(encoding="utf-8"))["items"] == []
        assert json.loads(incidents_out.read_text(encoding="utf-8"))["incidents"] == []
        leftovers = {entry.name for entry in base.iterdir()} - {
            issues.name,
            backlog_out.name,
            incidents_out.name,
        }
        assert leftovers == set()


def test_a_failing_check_on_a_behind_branch_reports_both_requirements():
    incidents = incidents_from_change_requests(
        [
            {
                "number": 7,
                "title": "both",
                "state": "open",
                "mergeable_state": "behind",
                "draft": False,
                "base": {"repo": {"full_name": "acme/forge"}},
            }
        ],
        project_id="forge",
        repository="acme/forge",
        observed_at=NOW,
        failing={7: "test"},
    )
    assert [item["kind"] for item in incidents] == [
        "deterministic-ci-failed",
        "branch-behind",
    ]


def test_the_dispatch_evidence_advisory_carries_its_own_remediation():
    incidents = incidents_from_issues(
        [
            _issue(
                1,
                "ready",
                ["implementation-approved", "human-review-required", "agent-ready"],
            )
        ],
        project_id="forge",
        repository="acme/forge",
        observed_at=NOW,
    )
    loaded = load_incident_document(_incidents(*incidents))
    assert loaded[0].kind == "awaiting-dispatch-evidence"
    actions = loaded[0].as_dict()["remediations"]
    assert actions[0]["actionId"] == "confirm-the-run"


def test_a_fresh_spec_hold_survives_planning_evidence():
    hold = _incident(
        incidentId="acme/forge#7:spec-hold",
        kind="awaiting-spec-review",
        severity="degraded",
        backlogItemId="acme/forge#7",
    )
    planned = _ledger(
        *_chain(
            TO_PLANNED,
            workUnit={"projectId": "forge", "workUnitId": "wu-1", "issueRef": "acme/forge#7"},
        )
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=planned.projections(),
        backlog=load_backlog_document(
            _document(_backlog(itemId="acme/forge#7", issueRef="acme/forge#7"))
        ),
        incidents=load_incident_document(_incidents(hold)),
    )
    assert len(snapshot.get("forge").incidents) == 1


def test_a_malformed_label_entry_is_a_controlled_error():
    with pytest.raises(ControlCenterError, match="string name"):
        backlog_document_from_issues(
            [
                {
                    "number": 1,
                    "title": "bad entry",
                    "state": "open",
                    "labels": [{}],
                    "repository_url": "https://api.github.com/repos/acme/forge",
                }
            ],
            project_id="forge",
            repository="acme/forge",
        )


def test_combined_per_project_incidents_respect_the_budget():
    half_one = load_incident_document(
        {
            "schemaVersion": 1,
            "incidents": [dict(_incident(incidentId=f"a-{index}")) for index in range(1500)],
        }
    )
    half_two = load_incident_document(
        {
            "schemaVersion": 1,
            "incidents": [dict(_incident(incidentId=f"b-{index}")) for index in range(1500)],
        }
    )
    with pytest.raises(ControlCenterError, match="more than 2000 incidents"):
        build_control_center_snapshot(
            generated_at=NOW, registry=REGISTRY, incidents=half_one + half_two
        )


def test_an_unstable_state_is_not_double_counted_with_its_failing_check():
    incidents = incidents_from_change_requests(
        [
            {
                "number": 7,
                "title": "red",
                "state": "open",
                "mergeable_state": "unstable",
                "draft": False,
                "base": {"repo": {"full_name": "acme/forge"}},
            }
        ],
        project_id="forge",
        repository="acme/forge",
        observed_at=NOW,
        failing={7: "test"},
    )
    assert [item["kind"] for item in incidents] == ["deterministic-ci-failed"]


def test_repeated_failing_flags_aggregate_check_names(tmp_path):
    from agentic_sdlc.cli import _failing_checks

    assert _failing_checks(["7=test", "7=lint"]) == {7: "test, lint"}


def test_a_boolean_record_number_is_a_controlled_error(tmp_path):
    from agentic_sdlc.cli import main as cli_main

    issues = tmp_path / "issues.json"
    issues.write_text(json.dumps([{"number": True, "title": "bool"}]), encoding="utf-8")
    assert (
        cli_main(
            [
                "github-backlog",
                "--issues",
                str(issues),
                "--project-id",
                "forge",
                "--repository",
                "acme/forge",
            ]
        )
        == 2
    )


def test_a_triage_incident_survives_approval_without_labels():
    stale = _incident(
        incidentId="acme/forge#7:untriaged",
        kind="triage-needed",
        severity="advisory",
        backlogItemId="acme/forge#7",
    )
    approved = _ledger(
        *_chain(
            TO_PLANNED + ("approved",),
            workUnit={"projectId": "forge", "workUnitId": "wu-1", "issueRef": "acme/forge#7"},
        )
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=approved.projections(),
        backlog=load_backlog_document(
            _document(_backlog(itemId="acme/forge#7", issueRef="acme/forge#7"))
        ),
        incidents=load_incident_document(_incidents(stale)),
    )
    assert len(snapshot.get("forge").incidents) == 1


def test_an_over_budget_failing_check_aggregate_is_rejected():
    with pytest.raises(ControlCenterError, match="too many failing check names"):
        incidents_from_change_requests(
            [
                {
                    "number": 7,
                    "title": "red",
                    "state": "open",
                    "mergeable_state": "blocked",
                    "draft": False,
                    "base": {"repo": {"full_name": "acme/forge"}},
                }
            ],
            project_id="forge",
            repository="acme/forge",
            observed_at=NOW,
            failing={7: ", ".join(f"check-{index:03d}" for index in range(60))},
        )


def test_a_boolean_or_float_schema_version_is_rejected():
    with pytest.raises(ControlCenterError, match="unsupported backlog schemaVersion"):
        load_backlog_document({"schemaVersion": True, "items": []})
    with pytest.raises(ControlCenterError, match="unsupported incident schemaVersion"):
        load_incident_document({"schemaVersion": 1.0, "incidents": []})


def test_a_zero_sub_issue_parent_is_a_controlled_error(tmp_path):
    from agentic_sdlc.cli import main as cli_main

    issues = tmp_path / "issues.json"
    issues.write_text(json.dumps([]), encoding="utf-8")
    children = tmp_path / "sub.json"
    children.write_text(json.dumps([{"number": 2}]), encoding="utf-8")
    assert (
        cli_main(
            [
                "github-backlog",
                "--issues",
                str(issues),
                "--sub-issues",
                f"0={children}",
                "--project-id",
                "forge",
                "--repository",
                "acme/forge",
            ]
        )
        == 2
    )


def test_a_pinned_sub_issue_listing_requires_repository_identity():
    # A partial foreign payload must not be PRESUMED local: with the export
    # pinned to a repository, every record proves where it belongs.
    with pytest.raises(ControlCenterError, match="repository_url is required"):
        parent_links(([{"number": 7}], 1), repository="acme/forge")
    with pytest.raises(ControlCenterError, match="not a recognized repository URL"):
        parent_links(([{"number": 7, "repository_url": "nope"}], 1), repository="acme/forge")
    links = parent_links(
        ([{"number": 7, "repository_url": "https://api.github.com/repos/acme/forge"}], 1),
        repository="acme/forge",
    )
    assert links == {7: 1}
    # An unpinned listing keeps today's lenient contract.
    assert parent_links(([{"number": 7}], 1)) == {7: 1}


def test_a_malformed_sub_issue_total_is_a_controlled_error():
    for bad in ("0", True, -1, 1.5):
        issue = _issue(1, "weird")
        issue["sub_issues_summary"] = {"total": bad}
        with pytest.raises(ControlCenterError, match="sub_issues_summary.total"):
            _export(issue)
    issue = _issue(1, "weird")
    issue["sub_issues_summary"] = "two"
    with pytest.raises(ControlCenterError, match="sub_issues_summary must be an object"):
        _export(issue)
    # The count must be explicit: a present summary with no total is a
    # partial payload, never affirmative no-children evidence.
    partial = _issue(1, "weird")
    partial["sub_issues_summary"] = {"completed": 0}
    with pytest.raises(ControlCenterError, match="sub_issues_summary.total"):
        _export(partial)
    zero = _issue(1, "childless")
    zero["sub_issues_summary"] = {"total": 0, "completed": 0}
    assert _export(zero)["items"][0]["kind"] == "task"


def test_spec_holds_name_the_configured_labels():
    from agentic_sdlc.control_center import RELEASED_SPEC_RULE, SPEC_WITHOUT_APPROVAL_RULE
    from agentic_sdlc.github_backlog import dispatch_rules

    labels = LifecycleLabels(
        ready="claude-ready", human_review="needs-human", implementation="go-implement"
    )
    rules = {rule.rule_id: rule for rule in dispatch_rules(labels)}
    released = rules[RELEASED_SPEC_RULE].summary
    assert "go-implement" in released
    assert "claude-ready" in released
    assert "needs-human" in released
    drafted = rules[SPEC_WITHOUT_APPROVAL_RULE].summary
    assert "go-implement" in drafted
    assert "claude-ready" in drafted


def test_ownership_checks_are_provider_qualified():
    gitlab_registry = load_project_registry(
        {
            "schemaVersion": 1,
            "projects": [
                {
                    "projectId": "forge",
                    "displayName": "Forge",
                    "group": "platform",
                    "defaultBranch": "main",
                    "repositories": [
                        {"provider": "gitlab", "identifier": "acme/forge", "role": "primary"}
                    ],
                    "environments": [{"name": "production", "kind": "production"}],
                    "capabilities": ["planning"],
                }
            ],
        }
    )
    github_document = dict(_document(_backlog()), source="github-issues")
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=gitlab_registry,
        backlog=load_backlog_document(github_document),
    )
    assert any(
        "github data references repository acme/forge" in warning
        and "declares only for gitlab" in warning
        for warning in snapshot.get("forge").warnings
    )
    # Incident documents carry the same qualification.
    github_incidents = dict(_incidents(_incident(backlogItemId="acme/forge#1")), source="github")
    incident_view = build_control_center_snapshot(
        generated_at=NOW,
        registry=gitlab_registry,
        incidents=load_incident_document(github_incidents),
    )
    assert any(
        "github data references repository acme/forge" in warning
        for warning in incident_view.get("forge").warnings
    )
    # The same identifier declared FOR github is owned.
    owned = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        backlog=load_backlog_document(github_document),
    )
    assert not any(
        "references repository acme/forge" in warning for warning in owned.get("forge").warnings
    )
    # A document with no recognizable source stays name-only.
    unqualified = build_control_center_snapshot(
        generated_at=NOW,
        registry=gitlab_registry,
        backlog=load_backlog_document(_document(_backlog())),
    )
    assert not any(
        "references repository" in warning for warning in unqualified.get("forge").warnings
    )


def test_registry_valid_prefixes_are_accepted_in_tracker_identifiers():
    # GitLab allows _ and . anywhere in a repository path, and the registry
    # accepts them, so the tracker-reference grammar must too: a project on
    # _group/repo could otherwise never supply tracker-linked backlog state.
    items = load_backlog_document(
        _document(_backlog(itemId="_group/repo#7", issueRef="_group/repo#7"))
    )
    assert items[0].item_id == "_group/repo#7"
    loaded = load_incident_document(
        _incidents(
            _incident(
                incidentId="_group/repo#7:untriaged",
                kind="triage-needed",
                severity="advisory",
                backlogItemId="_group/repo#7",
            )
        )
    )
    assert loaded[0].incident_id == "_group/repo#7:untriaged"
    # The reference still participates in ownership checks.
    snapshot = build_control_center_snapshot(generated_at=NOW, registry=REGISTRY, backlog=items)
    assert any(
        "references repository _group/repo" in warning for warning in snapshot.get("forge").warnings
    )


TO_MERGED = TO_IMPLEMENTING + ("verifying", "reviewing", "ready-for-human-merge", "merged")


def test_a_merged_change_request_retires_its_incidents():
    merged = _ledger(
        *_chain(
            TO_MERGED,
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-1",
                "issueRef": "acme/forge#4",
                "changeRequestRef": "acme/forge!7",
            },
        )
    )
    stale = _incident(
        incidentId="acme/forge!7:awaiting-merge",
        kind="awaiting-human-merge",
        severity="advisory",
        evidenceRef="acme/forge!7",
    )
    conflict = _incident(
        incidentId="acme/forge!7:merge-conflict",
        kind="merge-conflict",
        severity="blocker",
        evidenceRef="acme/forge!7",
    )
    other = _incident(
        incidentId="acme/forge!8:awaiting-merge",
        kind="awaiting-human-merge",
        severity="advisory",
        evidenceRef="acme/forge!8",
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=merged.projections(),
        incidents=load_incident_document(_incidents(stale, conflict, other)),
    )
    kept = snapshot.get("forge").incidents
    # Both incidents naming the provably merged change request retire; the
    # one naming a different change request stays.
    assert [item.incident_id for item in kept] == ["acme/forge!8:awaiting-merge"]


def test_an_unmerged_change_request_keeps_its_incidents():
    running = _ledger(
        *_chain(
            TO_IMPLEMENTING,
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-1",
                "changeRequestRef": "acme/forge!7",
            },
        )
    )
    waiting = _incident(
        incidentId="acme/forge!7:awaiting-merge",
        kind="awaiting-human-merge",
        severity="advisory",
        evidenceRef="acme/forge!7",
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=running.projections(),
        incidents=load_incident_document(_incidents(waiting)),
    )
    kept = snapshot.get("forge").incidents
    assert [item.incident_id for item in kept] == ["acme/forge!7:awaiting-merge"]


def test_long_registry_valid_references_fit_the_identifier_budget():
    # The registry accepts long repository identifiers; the tracker suffix
    # must still fit, so the reference budget is the read model's own text
    # budget rather than the repository length.
    repository = "g" * 127 + "/" + "r" * 127
    reference = f"{repository}#1"
    items = load_backlog_document(_document(_backlog(itemId=reference, issueRef=reference)))
    assert items[0].item_id == reference


def test_a_shared_change_request_reference_retires_only_on_unambiguous_proof():
    ledger = _ledger(
        *_chain(
            TO_MERGED,
            work_unit="wu-1",
            prefix="m",
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-1",
                "changeRequestRef": "acme/forge!7",
            },
        ),
        *_chain(
            TO_IMPLEMENTING,
            work_unit="wu-2",
            prefix="r",
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-2",
                "changeRequestRef": "acme/forge!7",
            },
        ),
    )
    named_unmerged = _incident(
        incidentId="acme/forge!7:ci-failed",
        kind="deterministic-ci-failed",
        severity="blocker",
        evidenceRef="acme/forge!7",
        workUnitId="wu-2",
    )
    named_merged = _incident(
        incidentId="acme/forge!7:merge-conflict",
        kind="merge-conflict",
        severity="blocker",
        evidenceRef="acme/forge!7",
        workUnitId="wu-1",
    )
    anonymous = _incident(
        incidentId="acme/forge!7:awaiting-merge",
        kind="awaiting-human-merge",
        severity="advisory",
        evidenceRef="acme/forge!7",
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=ledger.projections(),
        incidents=load_incident_document(_incidents(named_unmerged, named_merged, anonymous)),
    )
    kept = sorted(item.incident_id for item in snapshot.get("forge").incidents)
    # The incident naming the merged unit retires on that unit's own history;
    # the one naming the still-running unit stays, and the anonymous one
    # stays too because the shared reference is ambiguous.
    assert kept == ["acme/forge!7:awaiting-merge", "acme/forge!7:ci-failed"]


def test_the_console_bounds_collections_before_validating():
    from pathlib import Path

    html = Path("examples/control-center/control-center.html").read_text(encoding="utf-8")
    # Budgets are enforced inside the validator, before anything allocates
    # over or iterates a snapshot collection.
    assert "holds more than 500 projects" in html
    assert "const BUDGETS = { backlog: 5000" in html
    assert "holds more than ${budget} entries" in html
    assert "holds more than 5000 children" in html


def test_issue_links_must_identify_the_declared_issue():
    foreign = _issue(1, "lure")
    foreign["html_url"] = "https://evil.example/acme/forge/issues/1"
    with pytest.raises(ControlCenterError, match="not a recognized GitHub issue URL"):
        _export(foreign)
    wrong_number = _issue(1, "mismatch")
    wrong_number["html_url"] = "https://github.com/acme/forge/issues/2"
    with pytest.raises(ControlCenterError, match="does not identify acme/forge#1"):
        _export(wrong_number)
    wrong_repository = _issue(1, "mismatch")
    wrong_repository["html_url"] = "https://github.com/other/repo/issues/1"
    with pytest.raises(ControlCenterError, match="does not identify acme/forge#1"):
        _export(wrong_repository)
    absent = _issue(1, "no link")
    del absent["html_url"]
    assert "url" not in _export(absent)["items"][0]


def test_the_snapshot_enforces_the_shared_project_budget():
    items = [
        _backlog(itemId=f"acme/forge#{index}", projectId=f"p{index}", issueRef="")
        for index in range(501)
    ]
    with pytest.raises(ControlCenterError, match="more than the read model's 500-project budget"):
        build_control_center_snapshot(
            generated_at=NOW,
            registry=EMPTY,
            backlog=load_backlog_document(_document(*items)),
        )


def test_a_repository_identifier_beyond_the_registry_budget_is_rejected():
    from agentic_sdlc.project_registry import ProjectRegistryError

    document = {
        "schemaVersion": 1,
        "projects": [
            {
                "projectId": "forge",
                "displayName": "Forge",
                "group": "platform",
                "defaultBranch": "main",
                "repositories": [
                    {
                        "provider": "gitlab",
                        "identifier": "g" * 300 + "/repo",
                        "role": "primary",
                    }
                ],
                "environments": [{"name": "production", "kind": "production"}],
                "capabilities": ["planning"],
            }
        ],
    }
    with pytest.raises(ProjectRegistryError, match="at most 255 characters"):
        load_project_registry(document)


def test_merge_proof_spares_unclassified_incidents_that_are_not_merge_holds():
    ledger = _ledger(
        *_chain(
            TO_MERGED,
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-1",
                "changeRequestRef": "acme/forge!7",
            },
        )
    )
    merge_state = _incident(
        incidentId="acme/forge!7:merge-state-blocked",
        kind="unclassified",
        severity="degraded",
        evidenceRef="acme/forge!7",
    )
    regression = _incident(
        incidentId="acme/forge!7:post-merge-regression",
        kind="unclassified",
        severity="degraded",
        evidenceRef="acme/forge!7",
        summary="Nightly data check regressed after this change request merged",
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=ledger.projections(),
        incidents=load_incident_document(_incidents(merge_state, regression)),
    )
    kept = [item.incident_id for item in snapshot.get("forge").incidents]
    # The adapter's own merge-state record retires; an unclassified incident
    # merely CITING the merged change request stays.
    assert kept == ["acme/forge!7:post-merge-regression"]


def test_the_console_derives_health_and_bounds_top_level_collections():
    from pathlib import Path

    html = Path("examples/control-center/control-center.html").read_text(encoding="utf-8")
    assert "function deriveProjectFacts(project)" in html
    assert "for (const project of document_.projects) deriveProjectFacts(project);" in html
    # Top-level collections share the validator's budget enforcement.
    assert (
        'arrayField(value, "warnings", "snapshot", 5000) || '
        'arrayField(value, "sources", "snapshot", 1200)' in html
    )


def test_pull_references_carry_the_callers_canonical_spelling():
    pull = {
        "number": 7,
        "state": "open",
        "draft": False,
        "title": "cased",
        "mergeable_state": "clean",
        "base": {"repo": {"full_name": "Acme/Forge"}},
    }
    incidents = incidents_from_change_requests(
        [pull], project_id="forge", repository="acme/forge", observed_at=NOW
    )
    # Every id the adapter emits — issue item ids, label incident ids — uses
    # the caller's canonical spelling, and change-request retirement joins by
    # exact string equality, so the detail payload's own casing must never
    # leak into the exported reference and split the join.
    assert [item["evidenceRef"] for item in incidents] == ["acme/forge!7"]
    assert incidents[0]["incidentId"].startswith("acme/forge!7:")


def test_a_web_shaped_parent_url_cannot_reparent_a_child():
    foreign_web = [
        {
            "number": 7,
            "repository_url": "https://api.github.com/repos/acme/forge",
            "parent_issue_url": "https://github.com/other/repo/issues/1",
        }
    ]
    with pytest.raises(ControlCenterError, match="not a recognized GitHub issue URL"):
        parent_links((foreign_web, 2), repository="acme/forge")
    with pytest.raises(ControlCenterError, match="not a recognized GitHub issue URL"):
        parent_links(foreign_web)


def test_missing_parents_participate_in_ownership_checks():
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        backlog=load_backlog_document(
            _document(
                _backlog(itemId="acme/forge#2", issueRef="acme/forge#2", parentId="other/repo#7")
            )
        ),
    )
    assert any(
        "references repository other/repo" in warning for warning in snapshot.get("forge").warnings
    )


def test_warnings_are_bounded_with_a_truncation_marker():
    from agentic_sdlc.control_center import MAX_WARNINGS, _bounded_warnings

    entries = [f"warning {index}" for index in range(MAX_WARNINGS + 7)]
    bounded = _bounded_warnings(entries)
    assert len(bounded) == MAX_WARNINGS
    assert bounded[-1] == "8 more warnings were truncated; resolve the ones above to surface them"
    assert bounded[:-1] == tuple(entries[: MAX_WARNINGS - 1])
    assert _bounded_warnings(entries[:3]) == tuple(entries[:3])


def test_the_snapshot_enforces_the_shared_source_budget():
    with pytest.raises(ControlCenterError, match="1200-source budget"):
        build_control_center_snapshot(
            generated_at=NOW,
            registry=REGISTRY,
            sources=[f"backlog:file-{index}.json" for index in range(1201)],
        )


def test_the_console_derives_work_units_from_the_validated_state_counts():
    from pathlib import Path

    html = Path("examples/control-center/control-center.html").read_text(encoding="utf-8")
    assert "counts.byState must be an object" in html
    assert "for (const key of Object.keys(byState)) workUnits += byState[key];" in html
    # The commit-ready draft form enforces the registry's shared length cap.
    assert "at most 255 characters - the registry rejects longer ones" in html


def test_api_references_must_carry_the_github_origin():
    evil_repo = _issue(1, "spoof")
    evil_repo["repository_url"] = "https://evil.example/repos/acme/forge"
    with pytest.raises(ControlCenterError, match="not a recognized repository URL"):
        _export(evil_repo)
    evil_parent = [
        {
            "number": 7,
            "repository_url": "https://api.github.com/repos/acme/forge",
            "parent_issue_url": "https://evil.example/repos/acme/forge/issues/1",
        }
    ]
    with pytest.raises(ControlCenterError, match="not a recognized GitHub issue URL"):
        parent_links((evil_parent, 2), repository="acme/forge")
    with pytest.raises(ControlCenterError, match="not a recognized GitHub issue URL"):
        parent_links(evil_parent)


def test_registry_projects_carry_the_shared_collection_budgets():
    from agentic_sdlc.project_registry import ProjectRegistryError

    def _project(**overrides):
        entry = {
            "projectId": "forge",
            "displayName": "Forge",
            "group": "platform",
            "defaultBranch": "main",
            "repositories": [{"provider": "github", "identifier": "acme/forge", "role": "primary"}],
            "environments": [{"name": "production", "kind": "production"}],
            "capabilities": ["planning"],
        }
        entry.update(overrides)
        return {"schemaVersion": 1, "projects": [entry]}

    crowded = [{"provider": "github", "identifier": "acme/forge", "role": "primary"}] + [
        {"provider": "github", "identifier": f"acme/forge-{index}", "role": "mirror"}
        for index in range(501)
    ]
    with pytest.raises(ProjectRegistryError, match="at most 500 repositories"):
        load_project_registry(_project(repositories=crowded))
    many_environments = [{"name": f"env-{index}", "kind": "staging"} for index in range(101)]
    with pytest.raises(ProjectRegistryError, match="at most 100 environments"):
        load_project_registry(_project(environments=many_environments))


def test_the_snapshot_enforces_per_project_collection_budgets():
    first = load_backlog_document(
        _document(
            *(_backlog(itemId=f"acme/forge#{index}", issueRef="") for index in range(1, 2502))
        )
    )
    second = load_backlog_document(
        _document(
            *(_backlog(itemId=f"acme/forge#{index}", issueRef="") for index in range(2502, 5003))
        )
    )
    with pytest.raises(ControlCenterError, match="combines more than 5000 backlog items"):
        build_control_center_snapshot(
            generated_at=NOW, registry=REGISTRY, backlog=(*first, *second)
        )
    incident_groups = [
        load_incident_document(
            _incidents(
                *(
                    _incident(incidentId=f"i-{base}-{index}", backlogItemId="")
                    for index in range(1000)
                )
            )
        )
        for base in range(3)
    ]
    with pytest.raises(ControlCenterError, match="combines more than 2000 incidents"):
        build_control_center_snapshot(
            generated_at=NOW,
            registry=REGISTRY,
            incidents=(item for group in incident_groups for item in group),
        )
    crowded_ledger = _ledger(
        *(
            _event(
                eventId=f"wu-event-{index}",
                idempotencyKey=f"wu-key-{index}",
                workUnit={"projectId": "forge", "workUnitId": f"wu-{index}"},
            )
            for index in range(5001)
        )
    )
    with pytest.raises(ControlCenterError, match="5000-unit budget"):
        build_control_center_snapshot(
            generated_at=NOW, registry=REGISTRY, projections=crowded_ledger.projections()
        )


def test_the_console_caps_the_summed_state_counts():
    from pathlib import Path

    html = Path("examples/control-center/control-center.html").read_text(encoding="utf-8")
    assert "counts.byState sums past the 5000-unit budget" in html
    # Per-state values are shape-checked, and the SUM is held to the same
    # shared budget the producer enforces per project.
    assert "stateTotal += count;" in html


def test_issue_links_never_point_at_change_requests():
    pull_link = _issue(1, "lure")
    pull_link["html_url"] = "https://github.com/acme/forge/pull/1"
    with pytest.raises(ControlCenterError, match="not a recognized GitHub issue URL"):
        _export(pull_link)


def test_sub_issue_children_missing_from_the_export_fail_closed():
    links = parent_links(
        (
            [{"number": 2, "repository_url": "https://api.github.com/repos/acme/forge"}],
            1,
        ),
        repository="acme/forge",
    )
    with pytest.raises(ControlCenterError, match=r"missing from the issues export: #2"):
        backlog_document_from_issues(
            [_issue(1, "parent only")],
            project_id="forge",
            repository="acme/forge",
            parents=links,
        )
    # With the child exported too, the edge renders as declared.
    document = backlog_document_from_issues(
        [_issue(1, "parent"), _issue(2, "child")],
        project_id="forge",
        repository="acme/forge",
        parents=links,
    )
    assert document["items"][1]["parentId"] == "acme/forge#1"


def test_narrowing_skips_unselected_projects_budgets():
    heavy = [
        load_backlog_document(
            _document(
                *(
                    _backlog(
                        itemId=f"acme/other#{index}",
                        projectId="other",
                        issueRef="",
                    )
                    for index in range(start, start + 2501)
                )
            )
        )
        for start in (1, 2502)
    ]
    small = load_backlog_document(_document(_backlog()))
    combined = (*heavy[0], *heavy[1], *small)
    # Unfiltered, the oversized project fails the combined budget...
    with pytest.raises(ControlCenterError, match="combines more than 5000 backlog items"):
        build_control_center_snapshot(generated_at=NOW, registry=REGISTRY, backlog=combined)
    # ...but narrowing to the selected project still produces its snapshot.
    snapshot = build_control_center_snapshot(
        generated_at=NOW, registry=REGISTRY, backlog=combined, project_ids=("forge",)
    )
    assert [view.project_id for view in snapshot.projects] == ["forge"]


def test_backlog_identity_is_scoped_per_project():
    github_item = _backlog(itemId="acme/forge#1", projectId="forge", issueRef="")
    gitlab_item = _backlog(itemId="acme/forge#1", projectId="lab", issueRef="")
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        backlog=(
            *load_backlog_document(_document(github_item)),
            *load_backlog_document(_document(gitlab_item)),
        ),
    )
    assert {view.project_id for view in snapshot.projects} == {"forge", "lab"}
    duplicate = _incident(incidentId="i-1", projectId="forge")
    with pytest.raises(ControlCenterError, match="duplicate incidentId in project forge"):
        build_control_center_snapshot(
            generated_at=NOW,
            registry=REGISTRY,
            incidents=(
                *load_incident_document(_incidents(duplicate)),
                *load_incident_document(_incidents(duplicate)),
            ),
        )
    # The same incident id under ANOTHER project coexists.
    other = _incident(incidentId="i-1", projectId="lab")
    twins = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        incidents=(
            *load_incident_document(_incidents(duplicate)),
            *load_incident_document(_incidents(other)),
        ),
    )
    assert {view.project_id for view in twins.projects} == {"forge", "lab"}


def test_the_console_requires_collections_and_bounds_its_views():
    from pathlib import Path

    html = Path("examples/control-center/control-center.html").read_text(encoding="utf-8")
    assert "is required.`;" in html
    assert "backlog holds more than 5000 nodes or is cyclic" in html
    assert '("github:" + match[1]).toLowerCase()' in html
    assert ".limit(bound).onSnapshot(" in html
    assert '}, 1000, "at");' in html


def test_supplied_documents_require_their_collection_fields():
    with pytest.raises(ControlCenterError, match="requires an items array"):
        load_backlog_document({"schemaVersion": BACKLOG_SCHEMA_VERSION})
    with pytest.raises(ControlCenterError, match="requires an incidents array"):
        load_incident_document({"schemaVersion": INCIDENT_SCHEMA_VERSION})
    # Intentional empties stay representable, explicitly.
    assert load_backlog_document({"schemaVersion": BACKLOG_SCHEMA_VERSION, "items": []}) == ()
    assert load_incident_document({"schemaVersion": INCIDENT_SCHEMA_VERSION, "incidents": []}) == ()


def test_the_console_matches_the_producers_identity_and_unit_contracts():
    from pathlib import Path

    html = Path("examples/control-center/control-center.html").read_text(encoding="utf-8")
    # Duplicate checks are project-scoped, like the producer's.
    assert "Duplicate backlog itemId in ${where}" in html
    assert "Duplicate incidentId in ${where}" in html
    # String collections are validated per entry before any renderer folds.
    assert "entries must be strings" in html
    # Visible units are bounded by the validated state counts.
    assert "lists more visible work units than its state counts hold" in html
    # Notes outside the bounded subscription are backfilled exactly.
    assert "async function backfillNotes(document_)" in html
    assert "backfillNotes(document_);" in html


def test_the_snapshot_enforces_portfolio_wide_budgets():
    # Per-project limits alone would let a wide portfolio materialize
    # millions of records; the aggregate budget rejects it with a reason.
    groups = [
        load_incident_document(
            _incidents(
                *(
                    _incident(
                        incidentId=f"p{project}-i{index}",
                        projectId=f"p{project}",
                        backlogItemId="",
                    )
                    for index in range(2000)
                )
            )
        )
        for project in range(6)
    ]
    with pytest.raises(ControlCenterError, match="more than 10000 incidents in all"):
        build_control_center_snapshot(
            generated_at=NOW,
            registry=EMPTY,
            incidents=(item for group in groups for item in group),
        )


def test_a_malformed_document_source_is_rejected():
    with pytest.raises(ControlCenterError, match="source must be a string"):
        load_backlog_document(
            {"schemaVersion": BACKLOG_SCHEMA_VERSION, "source": {"token": "x"}, "items": []}
        )
    with pytest.raises(ControlCenterError, match="source must be a string"):
        load_incident_document(
            {"schemaVersion": INCIDENT_SCHEMA_VERSION, "source": 7, "incidents": []}
        )


def test_a_null_document_source_is_rejected():
    # None is not "source absent": text normalization would silently read it
    # as "", so the loaders reject it before normalizing.
    with pytest.raises(ControlCenterError, match="source must be a string"):
        load_backlog_document(
            {"schemaVersion": BACKLOG_SCHEMA_VERSION, "source": None, "items": []}
        )
    with pytest.raises(ControlCenterError, match="source must be a string"):
        load_incident_document(
            {"schemaVersion": INCIDENT_SCHEMA_VERSION, "source": None, "incidents": []}
        )


def test_rule_retirement_requires_github_provenance():
    from agentic_sdlc.control_center import PLAN_EVIDENCE_RULE

    # The id matches the adapter's exact spelling, but the document never
    # declared a GitHub source, so the record is some other system's and is
    # neither rewritten nor retired by ledger evidence.
    neutral = _incident(
        incidentId=f"acme/forge#7:{PLAN_EVIDENCE_RULE}",
        kind="dispatch-blocked",
        severity="advisory",
        backlogItemId="acme/forge#7",
        evidenceRef="acme/forge#7",
        summary="A provider-neutral record that matches the adapter spelling",
    )
    ledger = _ledger(
        *_chain(
            TO_PLANNED,
            workUnit={"projectId": "forge", "workUnitId": "wu-1", "issueRef": "acme/forge#7"},
        )
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=ledger.projections(),
        backlog=load_backlog_document(
            _document(_backlog(itemId="acme/forge#7", issueRef="acme/forge#7"))
        ),
        incidents=load_incident_document(
            {"schemaVersion": INCIDENT_SCHEMA_VERSION, "incidents": [neutral]}
        ),
    )
    kept = snapshot.get("forge").incidents
    assert [item.incident_id for item in kept] == [f"acme/forge#7:{PLAN_EVIDENCE_RULE}"]
    assert kept[0].summary == "A provider-neutral record that matches the adapter spelling"


def test_merge_state_retirement_requires_github_provenance():
    # The id uses the adapter's merge-state spelling, but the document never
    # declared a GitHub source: only the GitHub adapter defines that
    # spelling, so the record survives merge proof.
    record = _incident(
        incidentId="acme/forge!5:merge-state-blocked",
        kind="unclassified",
        severity="degraded",
        evidenceRef="acme/forge!5",
        summary="An unclassified record using the adapter's merge-state spelling",
    )
    merged = _ledger(
        *_chain(
            TO_MERGED,
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-1",
                "changeRequestRef": "acme/forge!5",
            },
        )
    )
    kept = (
        build_control_center_snapshot(
            generated_at=NOW,
            registry=REGISTRY,
            projections=merged.projections(),
            incidents=load_incident_document(
                {"schemaVersion": INCIDENT_SCHEMA_VERSION, "incidents": [record]}
            ),
        )
        .get("forge")
        .incidents
    )
    assert [item.incident_id for item in kept] == ["acme/forge!5:merge-state-blocked"]
    # With the GitHub source declared, the same record retires on merge proof.
    retired = (
        build_control_center_snapshot(
            generated_at=NOW,
            registry=REGISTRY,
            projections=merged.projections(),
            incidents=load_incident_document(_incidents(record)),
        )
        .get("forge")
        .incidents
    )
    assert retired == ()


def test_generated_at_is_normalized_before_serialization():
    snapshot = build_control_center_snapshot(generated_at=f"  {NOW} ", registry=EMPTY)
    assert snapshot.generated_at == NOW
    assert snapshot.as_dict()["generatedAt"] == NOW


def test_a_malformed_ledger_repository_is_warned_not_skipped():
    ledger = _ledger(
        *_chain(
            TO_IMPLEMENTING,
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-1",
                "repository": "https://user:secret@github.com/acme/forge",
                "issueRef": "acme/forge#7",
            },
        )
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=ledger.projections(),
        # The unit joins a backlog item, so the backlog tree captures its
        # status too: redaction must happen before the tree is built.
        backlog=load_backlog_document(
            _document(_backlog(itemId="acme/forge#7", issueRef="acme/forge#7"))
        ),
    )
    view = snapshot.get("forge")
    # The warning reports the problem WITHOUT the raw value: a malformed
    # identifier is often a clone URL, and URLs can embed credentials the
    # snapshot must never republish.
    assert any("not owner/name shaped" in warning for warning in view.warnings)
    # The value is withheld EVERYWHERE in the serialized snapshot - the
    # active/attention statuses and the backlog nodes' captured statuses.
    assert "user:secret" not in json.dumps(snapshot.as_dict())
    assert [unit.repository for unit in view.active] == [""]


def test_merge_proof_binds_to_the_referenced_change_request():
    # The unit merged change request !1; a later event re-points it at !2.
    # Historical merge states must not read as proof that !2 merged.
    ledger = _ledger(
        *_chain(
            TO_MERGED,
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-1",
                "changeRequestRef": "acme/forge!1",
            },
        ),
        _event(
            eventId="e-swap",
            idempotencyKey="k-swap",
            state="merged",
            stage="merge",
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-1",
                "changeRequestRef": "acme/forge!2",
            },
            activity="re-pointed at a follow-up change request",
        ),
    )
    blocked = _incident(
        incidentId="acme/forge!2:merge-state-blocked",
        kind="unclassified",
        severity="degraded",
        evidenceRef="acme/forge!2",
        summary="The follow-up change request is blocked",
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=ledger.projections(),
        incidents=load_incident_document(_incidents(blocked)),
    )
    kept = [item.incident_id for item in snapshot.get("forge").incidents]
    assert kept == ["acme/forge!2:merge-state-blocked"]


def test_credential_bearing_inputs_are_rejected_at_load():
    # A backlog url or incident evidenceRef with URL userinfo would be
    # republished by the snapshot and rendered as a clickable link.
    with pytest.raises(ControlCenterError, match="url must not embed credentials"):
        load_backlog_document(
            _document(
                _backlog(
                    itemId="acme/forge#7",
                    url="https://user:secret@github.com/acme/forge/issues/7",
                )
            )
        )
    with pytest.raises(ControlCenterError, match="evidenceRef must not embed credentials"):
        load_incident_document(_incidents(_incident(evidenceRef="https://token@ci.example/run/9")))
    # Userinfo is not the only carrier: a token-shaped credential in the
    # query string or fragment is the secret itself, and the rejection
    # withholds the value instead of echoing it into stderr.
    with pytest.raises(ControlCenterError) as token_error:
        load_backlog_document(
            _document(
                _backlog(
                    itemId="acme/forge#7",
                    url=(
                        "https://tracker.example/issue/1"
                        "?access=ghp_0123456789abcdefghij0123456789abcdef"
                    ),
                )
            )
        )
    assert "token-shaped credential" in str(token_error.value)
    assert "ghp_" not in str(token_error.value)


def test_a_malformed_pull_request_marker_is_rejected():
    # GitHub's real marker is an object; a null or string value is a
    # transformed payload, and skipping on it would silently drop records.
    for marker in (None, "yes"):
        with pytest.raises(ControlCenterError, match="pull_request must be an object"):
            backlog_document_from_issues(
                [_issue(1, "ok") | {"pull_request": marker}],
                project_id="forge",
                repository="acme/forge",
            )
    with pytest.raises(ControlCenterError, match="pull_request must be an object"):
        incidents_from_issues(
            [_issue(1, "ok", labels=("implementation-approved",)) | {"pull_request": None}],
            project_id="forge",
            repository="acme/forge",
            observed_at=NOW,
        )


def test_credential_bearing_references_are_redacted():
    ledger = _ledger(
        *_chain(
            TO_IMPLEMENTING,
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-1",
                "runRef": "https://user:secret@ci.example/run/7",
                "issueRef": "acme/forge#7",
            },
        )
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW, registry=REGISTRY, projections=ledger.projections()
    )
    view = snapshot.get("forge")
    assert "user:secret" not in json.dumps(snapshot.as_dict())
    assert [unit.run_ref for unit in view.active] == [""]
    # Username-only userinfo is a credential too (tokens travel that way).
    tokened = _ledger(
        *_chain(
            TO_IMPLEMENTING,
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-1",
                "runRef": "https://ghp-token-here@ci.example/run/7",
            },
        )
    )
    redacted = build_control_center_snapshot(
        generated_at=NOW, registry=REGISTRY, projections=tokened.projections()
    )
    assert "ghp-token-here" not in json.dumps(redacted.as_dict())
    # The untouched reference survives; only the credential-bearing one is
    # withheld, with a warning naming the count.
    assert [unit.issue_ref for unit in view.active] == ["acme/forge#7"]
    assert any("embedding URL credentials" in warning for warning in view.warnings)


def test_the_adapter_rejects_a_non_string_title():
    # str() on an object would serialize nested provider fields - potentially
    # credential-bearing - into the snapshot, bypassing the loaders' checks.
    with pytest.raises(ControlCenterError, match="title must be a string"):
        backlog_document_from_issues(
            [_issue(1, "ok") | {"title": {"token": "secret"}}],
            project_id="forge",
            repository="acme/forge",
        )
    with pytest.raises(ControlCenterError, match="title must be a string"):
        incidents_from_change_requests(
            [
                {
                    "number": 5,
                    "title": ["not", "a", "string"],
                    "state": "open",
                    "mergeable_state": "dirty",
                    "draft": False,
                    "base": {"repo": {"full_name": "acme/forge"}},
                }
            ],
            project_id="forge",
            repository="acme/forge",
            observed_at=NOW,
        )


def test_the_adapter_rejects_a_duplicated_change_request_number():
    base = {
        "title": "same change request twice",
        "state": "open",
        "draft": False,
        "base": {"repo": {"full_name": "acme/forge"}},
    }
    with pytest.raises(ControlCenterError, match="appears more than once"):
        incidents_from_change_requests(
            [
                {**base, "number": 5, "mergeable_state": "dirty"},
                {**base, "number": 5, "mergeable_state": "clean"},
            ],
            project_id="forge",
            repository="acme/forge",
            observed_at=NOW,
        )
    # Numbers are recorded before the closed/draft skips, so a duplicate
    # cannot hide behind a skipped record either.
    with pytest.raises(ControlCenterError, match="appears more than once"):
        incidents_from_change_requests(
            [
                {**base, "number": 5, "state": "closed", "mergeable_state": "dirty"},
                {**base, "number": 5, "mergeable_state": "clean"},
            ],
            project_id="forge",
            repository="acme/forge",
            observed_at=NOW,
        )
    with pytest.raises(ControlCenterError, match="appears more than once"):
        incidents_from_change_requests(
            [
                {**base, "number": 5, "draft": True, "mergeable_state": "dirty"},
                {**base, "number": 5, "mergeable_state": "clean"},
            ],
            project_id="forge",
            repository="acme/forge",
            observed_at=NOW,
        )


def test_rule_retirement_requires_the_adapter_identity():
    from agentic_sdlc.control_center import PLAN_EVIDENCE_RULE

    # The id merely ENDS in the rule spelling but does not tie to the named
    # backlog item, so it is not the adapter's record and is never rewritten.
    lookalike = _incident(
        incidentId=f"custom:{PLAN_EVIDENCE_RULE}",
        kind="awaiting-dispatch-evidence",
        severity="advisory",
        backlogItemId="acme/forge#7",
        summary="A provider-neutral advisory that merely echoes the rule name",
    )
    approved = _ledger(
        *_chain(
            TO_PLANNED + ("approved",),
            workUnit={"projectId": "forge", "workUnitId": "wu-1", "issueRef": "acme/forge#7"},
        )
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=approved.projections(),
        backlog=load_backlog_document(
            _document(_backlog(itemId="acme/forge#7", issueRef="acme/forge#7"))
        ),
        incidents=load_incident_document(_incidents(lookalike)),
    )
    kept = snapshot.get("forge").incidents
    assert [item.incident_id for item in kept] == [f"custom:{PLAN_EVIDENCE_RULE}"]
    assert kept[0].kind == "awaiting-dispatch-evidence"
    assert kept[0].summary == "A provider-neutral advisory that merely echoes the rule name"


def test_the_console_scopes_notes_and_bounds_its_work():
    from pathlib import Path

    html = Path("examples/control-center/control-center.html").read_text(encoding="utf-8")
    # Note identity carries the project, with a legacy-id fallback on reads.
    assert "function noteKey(projectId, provider, incidentId)" in html
    assert "noteKey(incident.projectId, incident.sourceProvider, incident.incidentId)" in html
    # Backfilled notes survive subscription deliveries and are bounded.
    assert "for (const [key, entry] of backfilledEntries)" in html
    assert "if (wanted.length >= 200) break outer;" in html
    # Drafts window is recency-ordered; visible units are deduplicated; the
    # portfolio-wide budgets mirror the producer's.
    assert '}, 500, "at");' in html
    assert "lists work unit ${unit.workUnitId} more than once" in html
    assert "more than 50000 backlog items in all" in html
    assert "more than 10000 incidents in all" in html
    assert "more than 50000 work units in all" in html


def test_the_console_checks_membership_and_mirrors_its_writes():
    from pathlib import Path

    html = Path("examples/control-center/control-center.html").read_text(encoding="utf-8")
    # A saved note lands in the local overlay too, so it stays visible when
    # it falls outside the bounded subscription window.
    assert "backfilledEntries.set(key, entry);" in html
    assert "state.notes.set(key, entry);" in html
    # Exact reads (legacy fallback included) only accept a record naming the
    # requested project and incident.
    assert "entry.incidentId !== incidentId" in html
    assert "|| entry.projectId !== projectId" in html
    # A stored note that declares a provider attaches only to that provider.
    assert "|| (entry.sourceProvider && entry.sourceProvider !== provider)" in html
    # Provider-scoped keys carry three components, parsed in full before a
    # fetched record is validated against them.
    assert '[keyProject, keyProvider, keyIncident] = key.split("\\u0000")' in html
    assert '(entry.sourceProvider || "") === (keyProvider || "")' in html
    # Legacy providerless fallbacks attach only to unambiguous identities,
    # and the caches are pruned to the adopted snapshot.
    assert "if (!entry && soleProvider) {" in html
    assert "identityProviders.get(identity)?.size === 1" in html
    assert (
        "if (!liveIdentities.has(key)) { backfilledEntries.delete(key); state.notes.delete(key); }"
        in html
    )
    # generatedAt is required and must parse as a timestamp.
    assert "generatedAt must be an RFC 3339 timestamp." in html
    # Every backlog node carries its children array.
    assert "if (!Array.isArray(node.children)) return" in html
    # active/attention membership mirrors the producer's split exactly.
    assert "outside the attention states" in html
    assert "outside the executing, no-attention states" in html
    assert '"deployment-verifying", "rolling-back"' in html
    # Draft saves clamp their timestamp past the newest visible draft.
    assert "newest >= now ? newest + 1000 : now" in html


def test_the_console_binds_incidents_and_enforces_full_membership():
    from pathlib import Path

    html = Path("examples/control-center/control-center.html").read_text(encoding="utf-8")
    # Every incident binds to its containing project.
    assert "declares a different projectId" in html
    # Membership mirrors the producer's derivations, not the serialized flag
    # alone: attention states are closed, review gates need activity.
    assert '"approval-pending", "ready-for-human-merge", "blocked", "failed"' in html
    assert "outside the attention states" in html
    assert "without recorded activity" in html
    # The producer-owned top-level collections are required, not optional.
    assert "The snapshot requires its ${key} array" in html
    # Note backfill sweeps every incident in passes, each key attempted once.
    assert "if (!wanted.length) break;" in html
    # Unit references render as chips, so a digest-consistent snapshot must
    # not carry a credential-bearing or unbounded reference value.
    assert 'for (const field of ["issueRef", "changeRequestRef", "runRef"])' in html
    assert "${field} must be a bounded string." in html
    assert "${field} must not embed credentials." in html
    # Embedded ledger-basis backlog statuses carry the same reference rule:
    # the adopted document (and its "Copy the current snapshot" serialization)
    # must never retain a credential-bearing status reference.
    assert html.count('for (const field of ["issueRef", "changeRequestRef", "runRef"])') == 2
    assert "status ${field} must be a bounded string." in html
    assert "status ${field} must not embed credentials." in html
    # Incident and backlog references carry the same bounded no-userinfo rule.
    assert 'for (const field of ["evidenceRef", "backlogItemId"])' in html
    assert 'for (const key of ["itemId", "issueRef", "parentId"])' in html
    assert "${key} must not embed credentials." in html
    # Active membership requires the flag to be exactly false, and every
    # visible unit's stage must agree with its state (dependency-wait is the
    # one declared exception).
    assert "unit.needsAttention !== false" in html
    assert "stage disagrees with its state." in html
    assert '"deployment-verifying": "deployment-verification"' in html
    # A ledger-basis backlog status binds to THIS item's unit.
    assert "status names another work unit." in html
    assert "status is not joined by its issue reference." in html
    # Identity is provider-scoped like the producer's: same-spelled ids from
    # two providers coexist, and evidence links require the incident's own
    # provider to be GitHub before linking there.
    assert '(node.sourceProvider || "") + "\\u0000" + node.itemId' in html
    assert '(incident.sourceProvider || "") + "\\u0000" + incident.incidentId' in html
    assert '"workUnitId", "sourceProvider"' in html
    assert 'incident.sourceProvider === "github" && match' in html
    # A recovered feed clears its own failure banner on the next delivery.
    assert "if (state.feedErrors.delete(collection)) render();" in html
    # Parent placement and disclosure state are provider-scoped like every
    # other identity, and dependency waits never read as running.
    assert "const [node, depth, placedUnder, placedProvider] = stack.pop();" in html
    assert "sits under another provider's parent." in html
    assert "function openKey(projectId, node)" in html
    assert "treeNode(projectId, kid)" in html
    assert "during a dependency wait." in html
    assert "dependencyWaitByState" in html
    assert "(project.counts.byState[key] || 0) - waiting" in html
    # byStage is fully derived from byState and the dependency-wait
    # exemptions via the canonical state->stage map, and must equal that
    # census exactly — no freely reassignable stage mass, visible or not.
    assert "counts.byStage must be an object." in html
    assert "const canonicalStage = {" in html
    assert "disagrees with the canonical stage census." in html
    # Dependency-held units are surfaced on their own rail, reconciled
    # exactly against the serialized exemptions, and the project reads
    # waiting, never idle.
    assert "waiting lists work unit" in html
    assert "must list every dependency-waiting unit its counts declare." in html
    assert "Waiting on dependencies (" in html
    assert 'waiting.length ? "waiting" :' in html
    # Review-gate capacity excludes dependency-held units, and NUL — the
    # note/disclosure key delimiter — is rejected in every identity field.
    assert "(project.counts.byState[key] || 0) - held" in html
    assert "delimiter-free projectId" in html
    assert "delimiter-free string incidentId" in html
    assert "sourceProvider must be a short delimiter-free string." in html
    assert "must be delimiter-free." in html
    # Every formerly live key is exact-read verified, cached or not.
    assert ".filter((key) => !live.has(key));" in html
    # A live note that was never backfilled is seeded into the cache before
    # the verification reads, so a failed read cannot drop it.
    assert "if (!backfilledEntries.has(key) && state.notes.has(key))" in html
    # The notes window is recency-ordered like drafts, and Discard deletes
    # the DELIVERED draft document, never a path derived from its projectId.
    assert '}, 1000, "at");' in html
    assert 'state.db.doc("projectDrafts/" + draft.docId).delete()' in html
    # Note saves clamp their timestamp past the newest visible note, so a
    # skewed clock cannot park a fresh note behind the recency cutoff.
    assert "for (const source of [state.notes, backfilledEntries])" in html
    assert html.count("newest >= now ? newest + 1000 : now") == 2
    # A terminated collection subscription is surfaced, never silent — on
    # setup throws too — and the banner survives snapshot adoption (which
    # clears state.error while the feed stays dead).
    assert 'Live " + collection + " updates stopped' in html
    assert "function feedStopped(collection, error)" in html
    assert "feedErrors: new Map()" in html
    assert "for (const message of state.feedErrors.values())" in html
    # Both timestamp clamps are bounded — notes AND project drafts: an absurd
    # stored timestamp cannot overflow the Date range and break every save.
    assert html.count("parsed <= ceiling") == 2
    assert "for (const draft of state.drafts)" in html
    # The recency windows exclude rows beyond the same one-day ceiling the
    # save clamps enforce, and the ceiling tracks the clock: far-future store
    # rows cannot occupy the bounded feed while fresh saves land outside it.
    assert '.where(recencyField, "<=", ceiling).orderBy(recencyField, "desc")' in html
    assert "const ceiling = new Date(Date.now() + 86_400_000).toISOString();" in html
    assert "}, 3_600_000);" in html
    # A backfill read that resumes after a notes delivery made the key live
    # leaves the subscription's newer data alone.
    assert "if (liveNoteKeys.has(key)) return;" in html


def test_the_console_keeps_the_verified_document_and_exact_memberships():
    from pathlib import Path

    html = Path("examples/control-center/control-center.html").read_text(encoding="utf-8")
    # Both lists are reconciled per state against the validated counts.
    assert "active must list every unit its ${key} count holds." in html
    assert "attention must list every unit its ${key} count holds." in html
    assert "active lists more ${key} units than its state counts hold." in html
    assert (
        '"implementing", "verifying", "repairing", "deploying", '
        '"deployment-verifying", "rolling-back"' in html
    )
    # A stale async notes delivery never overwrites a newer one.
    assert "const delivery = ++notesDelivery;" in html
    assert "if (delivery !== notesDelivery) return;" in html
    # generatedAt components are validated; Date.parse normalization is out.
    assert "generatedAt must be a real calendar instant." in html
    assert "leap ? 29 : 28" in html
    # The scoped note document deterministically wins over a legacy twin.
    assert "const scoped = entry.docId === (await docId(key));" in html
    assert "if (!scopedKeys.has(key)) next.set(key, entry);" in html
    # Derivation happens on a clone; the copy action serializes the verified
    # document so it still matches its own digest when loaded back.
    assert "state.snapshotPristine = document_;" in html
    assert "document_ = structuredClone(document_);" in html
    assert "JSON.stringify(state.snapshotPristine, null, 2)" in html


def test_the_console_pins_state_vocabulary_and_note_freshness():
    from pathlib import Path

    html = Path("examples/control-center/control-center.html").read_text(encoding="utf-8")
    # byState keys belong to the producer's closed FactoryState vocabulary.
    assert "names an unknown state: ${key}" in html
    assert '"rolled-back", "blocked", "failed", "cancelled", "superseded"' in html
    # awaitsHuman is validated against the state, never trusted.
    assert "awaitsHuman disagrees with its state" in html
    # A present incident observedAt must be a real instant.
    assert "observedAt must be a real RFC 3339 instant." in html
    # Deleting the store document clears the pristine copy too.
    assert "state.snapshotPristine = null;" in html
    # Out-of-window notes are re-read on every adoption, and a deleted note
    # drops its stale overlay entry.
    assert "liveNoteKeys = live;" in html
    assert "} else if (backfilledEntries.delete(key)) {" in html
    # Each sweep carries its own generation and attempted set, so an older
    # sweep's late reads never overwrite a newer adoption's view.
    assert "const generation = ++backfillGeneration;" in html
    assert "if (generation !== backfillGeneration) return;" in html
    # A key that vanished from the live window is exact-read VERIFIED before
    # any drop, since eviction from the bounded query is not deletion.
    assert "deleted or merely EVICTED" in html
    assert "entry.incidentId === keyIncident && entry.projectId === keyProject" in html
    # The delivery sequence is re-checked immediately before each cache
    # mutation (two adoption gates plus the per-read gate), so a superseded
    # delivery's late read never restores or deletes a newer verification.
    assert html.count("if (delivery !== notesDelivery) return;") == 3
    # A failed exact read keeps the key retry-eligible even though it left
    # liveNoteKeys, and a key delivered live again leaves the retry set.
    assert "const retryNoteKeys = new Set();" in html
    assert "if (delivery === notesDelivery) retryNoteKeys.add(key);" in html
    assert "new Set([...liveNoteKeys, ...retryNoteKeys])" in html
    assert "for (const key of retryNoteKeys) if (live.has(key)) retryNoteKeys.delete(key);" in html
    # Exact reads attach only records naming this project and incident.
    assert "entry.incidentId !== incidentId" in html
    assert "|| entry.projectId !== projectId" in html
    # A stored note that declares a provider attaches only to that provider.
    assert "|| (entry.sourceProvider && entry.sourceProvider !== provider)" in html
    # Viewer-writable store rows bypass the snapshot's identity checks, so
    # each identity component is validated delimiter-free before it may feed
    # noteKey; a NUL-bearing projectId cannot collide with another key.
    assert "const identity = [entry.projectId, entry.sourceProvider, entry.incidentId];" in html
    assert 'typeof part === "string" && !part.includes("\\u0000")' in html


def test_the_console_validates_backlog_provenance_and_scoped_liveness():
    from pathlib import Path

    html = Path("examples/control-center/control-center.html").read_text(encoding="utf-8")
    # Only a scoped live document stops the exact backfill read.
    assert "if (scoped) { scopedKeys.add(key); live.add(key); }" in html
    # Placement must agree with the serialized parent link.
    assert "sits under ${placedUnder} but names another parent" in html
    assert "names a parent without a parent-missing flag" in html
    # Rendered backlog URLs must not embed credentials — neither userinfo
    # nor a token-shaped credential in the query string or fragment.
    assert "url must not embed credentials" in html
    assert "url carries a token-shaped credential" in html
    # Every node names its containing project, exactly as the producer
    # groups them, so a digest-consistent rewrite cannot move work between
    # projects; projectId is string-validated with the other node fields.
    assert "names another project." in html
    assert '"sourceProvider", "projectId"' in html
    # Declared-basis states belong to the closed vocabulary too.
    assert "names an unknown declared state" in html
    # State provenance is a closed, reconciled contract.
    assert "state disagrees with its ledger status" in html
    assert "state disagrees with its declared state" in html
    assert "names an unknown state basis" in html
    assert "carries a status without ledger provenance" in html


def test_historical_merge_references_still_retire_their_incidents():
    # The unit merged !1 and was later re-pointed at !2: a stale incident for
    # the already-merged ORIGINAL request (adapter incidents name no work
    # unit) must still find its carrier and retire, while !2 stays blocked.
    swap = _event(
        eventId="e-swap",
        idempotencyKey="k-swap",
        state="merged",
        stage="merge",
        workUnit={
            "projectId": "forge",
            "workUnitId": "wu-1",
            "changeRequestRef": "acme/forge!2",
        },
        activity="re-pointed at a follow-up change request",
    )
    ledger = _ledger(
        *_chain(
            TO_MERGED,
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-1",
                "changeRequestRef": "acme/forge!1",
            },
        ),
        swap,
    )
    stale = _incident(
        incidentId="acme/forge!1:merge-state-blocked",
        kind="unclassified",
        severity="degraded",
        evidenceRef="acme/forge!1",
        summary="The merged change request still reports blocked",
    )
    blocked = _incident(
        incidentId="acme/forge!2:merge-state-blocked",
        kind="unclassified",
        severity="degraded",
        evidenceRef="acme/forge!2",
        summary="The follow-up change request is blocked",
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=ledger.projections(),
        incidents=load_incident_document(_incidents(stale, blocked)),
    )
    kept = [item.incident_id for item in snapshot.get("forge").incidents]
    assert kept == ["acme/forge!2:merge-state-blocked"]
    # The all-carriers rule is preserved: a second unit CURRENTLY carrying !1
    # without merge proof keeps the incident, whatever wu-1's history says.
    contested = _ledger(
        *_chain(
            TO_MERGED,
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-1",
                "changeRequestRef": "acme/forge!1",
            },
        ),
        swap,
        *_chain(
            TO_IMPLEMENTING,
            work_unit="wu-2",
            prefix="b",
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-2",
                "changeRequestRef": "acme/forge!1",
            },
        ),
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=contested.projections(),
        incidents=load_incident_document(_incidents(stale)),
    )
    kept = [item.incident_id for item in snapshot.get("forge").incidents]
    assert kept == ["acme/forge!1:merge-state-blocked"]


def test_issue_reference_joins_are_provider_qualified():
    # Issue references are only unique within one tracker provider: a
    # GitHub-exported item must not display a GitLab unit's ledger progress
    # just because the reference strings coincide.
    gitlab_registry = load_project_registry(
        {
            "schemaVersion": 1,
            "projects": [
                {
                    "projectId": "forge",
                    "displayName": "Forge",
                    "group": "platform",
                    "defaultBranch": "main",
                    "repositories": [
                        {"provider": "gitlab", "identifier": "acme/forge", "role": "primary"}
                    ],
                    "environments": [{"name": "production", "kind": "production"}],
                    "capabilities": ["planning", "implementation"],
                }
            ],
        }
    )
    ledger = _ledger(
        *_chain(
            TO_IMPLEMENTING,
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-1",
                "issueRef": "acme/forge#1",
                "repository": "acme/forge",
            },
        )
    )
    github_backlog = load_backlog_document(
        _document(_backlog(issueRef="acme/forge#1", declaredState="approved"))
        | {"source": "github"}
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=gitlab_registry,
        projections=ledger.projections(),
        backlog=github_backlog,
    )
    view = snapshot.get("forge")
    node = view.backlog[0]
    # The foreign item is preserved and warned about, but never joined.
    assert (node.state, node.state_basis) == ("approved", STATE_BASIS_DECLARED)
    assert node.status is None
    assert any("declares only for gitlab" in warning for warning in view.warnings)
    # An agreeing provider keeps the join; so does a document whose provider
    # is unknown, since disagreement is proven, never guessed.
    agreeing = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=ledger.projections(),
        backlog=github_backlog,
    )
    assert agreeing.get("forge").backlog[0].state_basis == STATE_BASIS_LEDGER
    unknown_provider = build_control_center_snapshot(
        generated_at=NOW,
        registry=gitlab_registry,
        projections=ledger.projections(),
        backlog=load_backlog_document(
            _document(_backlog(issueRef="acme/forge#1", declaredState="approved"))
        ),
    )
    assert unknown_provider.get("forge").backlog[0].state_basis == STATE_BASIS_LEDGER


def test_failing_check_names_must_be_nonempty_bounded_strings():
    # The name is interpolated into the incident summary: a non-string value
    # would be coerced whole (nested credentials and all), and a blank one
    # would fabricate a CI blocker with no named check.
    pull = {
        "number": 7,
        "title": "red",
        "state": "open",
        "draft": False,
        "mergeable_state": "unstable",
        "base": {"repo": {"full_name": "acme/forge"}},
    }
    for name in (None, 7, {"token": "ghp-secret"}, ["test"], "", "   "):
        with pytest.raises(ControlCenterError, match="must be a non-empty string"):
            incidents_from_change_requests(
                [pull],
                project_id="forge",
                repository="acme/forge",
                observed_at=NOW,
                failing={7: name},
            )
    with pytest.raises(ControlCenterError, match="narrow --failing"):
        incidents_from_change_requests(
            [pull],
            project_id="forge",
            repository="acme/forge",
            observed_at=NOW,
            failing={7: "x" * 513},
        )


def test_named_incidents_retire_on_historical_merge_proof():
    # wu-1 merged !1 and was later re-pointed at !2: an incident NAMING wu-1
    # about !1 retires on the recorded historical proof, exactly as the same
    # incident does when it names no unit — the proof binds the reference in
    # effect at the merge, so the reference need not still be current.
    swap = _event(
        eventId="e-swap",
        idempotencyKey="k-swap",
        state="merged",
        stage="merge",
        workUnit={
            "projectId": "forge",
            "workUnitId": "wu-1",
            "changeRequestRef": "acme/forge!2",
        },
        activity="re-pointed at a follow-up change request",
    )
    ledger = _ledger(
        *_chain(
            TO_MERGED,
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-1",
                "changeRequestRef": "acme/forge!1",
            },
        ),
        swap,
        *_chain(
            TO_IMPLEMENTING,
            work_unit="wu-2",
            prefix="b",
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-2",
                "changeRequestRef": "acme/forge!3",
            },
        ),
    )
    retired = _incident(
        incidentId="acme/forge!1:merge-state-blocked",
        kind="unclassified",
        severity="degraded",
        evidenceRef="acme/forge!1",
        workUnitId="wu-1",
        summary="The merged change request still reports blocked",
    )
    # A named unit WITHOUT proof for the reference keeps its incident.
    kept = _incident(
        incidentId="acme/forge!3:merge-state-blocked",
        kind="unclassified",
        severity="degraded",
        evidenceRef="acme/forge!3",
        workUnitId="wu-2",
        summary="The in-flight change request is blocked",
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=ledger.projections(),
        incidents=load_incident_document(_incidents(retired, kept)),
    )
    assert [item.incident_id for item in snapshot.get("forge").incidents] == [
        "acme/forge!3:merge-state-blocked"
    ]


def test_credential_bearing_merge_references_are_never_retained():
    # The historical set outlives the current reference's replacement and is
    # serialized by project-state, so a credential-bearing reference in
    # effect at the merge is never recorded into it.
    ledger = _ledger(
        *_chain(
            TO_MERGED,
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-1",
                "changeRequestRef": "https://token@github.com/acme/forge/pull/1",
            },
        )
    )
    projection = ledger.projections()[0]
    assert projection.merged_change_request_refs == ()
    assert projection.as_dict()["durable"]["mergedChangeRequestRefs"] == []


def test_merge_proof_survives_a_lifecycle_restart():
    # merged -> blocked -> triaged restarts the lifecycle, but restarting a
    # work unit cannot unmerge the change request it already merged: the
    # reference-bound proof retires the stale incident anyway.
    ledger = _ledger(
        *_chain(
            TO_MERGED,
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-1",
                "changeRequestRef": "acme/forge!1",
            },
        ),
        _event(
            eventId="e-block",
            idempotencyKey="k-block",
            state="blocked",
            stage="blocked",
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-1",
                "changeRequestRef": "acme/forge!1",
            },
            reason="post-merge regression found in production",
        ),
        _event(
            eventId="e-restart",
            idempotencyKey="k-restart",
            state="triaged",
            stage="intake",
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-1",
                "changeRequestRef": "acme/forge!1",
            },
        ),
    )
    stale = _incident(
        incidentId="acme/forge!1:merge-state-blocked",
        kind="unclassified",
        severity="degraded",
        evidenceRef="acme/forge!1",
        summary="The merged change request still reports blocked",
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=ledger.projections(),
        incidents=load_incident_document(_incidents(stale)),
    )
    assert [item.incident_id for item in snapshot.get("forge").incidents] == []


def test_merge_retirement_is_provider_qualified():
    # A merged GitLab unit carrying the same reference string must not
    # retire a GitHub incident: references are only unique per provider.
    gitlab_registry = load_project_registry(
        {
            "schemaVersion": 1,
            "projects": [
                {
                    "projectId": "forge",
                    "displayName": "Forge",
                    "group": "platform",
                    "defaultBranch": "main",
                    "repositories": [
                        {"provider": "gitlab", "identifier": "acme/forge", "role": "primary"}
                    ],
                    "environments": [{"name": "production", "kind": "production"}],
                    "capabilities": ["planning", "implementation"],
                }
            ],
        }
    )
    ledger = _ledger(
        *_chain(
            TO_MERGED,
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-1",
                "changeRequestRef": "acme/forge!1",
                "repository": "acme/forge",
            },
        )
    )
    stale = _incident(
        incidentId="acme/forge!1:merge-state-blocked",
        kind="unclassified",
        severity="degraded",
        evidenceRef="acme/forge!1",
        summary="The change request reports blocked",
    )
    foreign = build_control_center_snapshot(
        generated_at=NOW,
        registry=gitlab_registry,
        projections=ledger.projections(),
        incidents=load_incident_document(_incidents(stale)),
    )
    kept = [item.incident_id for item in foreign.get("forge").incidents]
    assert kept == ["acme/forge!1:merge-state-blocked"]
    # Under an agreeing provider the same proof retires the incident.
    agreeing = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=ledger.projections(),
        incidents=load_incident_document(_incidents(stale)),
    )
    assert [item.incident_id for item in agreeing.get("forge").incidents] == []

    # An incident NAMING the unit is judged on that unit's own history — but
    # under the same provider qualification: GitLab merge history must not
    # retire a GitHub blocker that merely shares the textual reference.
    named = _incident(
        incidentId="acme/forge!1:merge-state-blocked",
        kind="unclassified",
        severity="degraded",
        evidenceRef="acme/forge!1",
        workUnitId="wu-1",
        summary="The change request reports blocked",
    )
    named_foreign = build_control_center_snapshot(
        generated_at=NOW,
        registry=gitlab_registry,
        projections=ledger.projections(),
        incidents=load_incident_document(_incidents(named)),
    )
    assert [item.incident_id for item in named_foreign.get("forge").incidents] == [
        "acme/forge!1:merge-state-blocked"
    ]
    named_agreeing = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=ledger.projections(),
        incidents=load_incident_document(_incidents(named)),
    )
    assert [item.incident_id for item in named_agreeing.get("forge").incidents] == []


def test_historical_merge_proof_is_bound_to_the_references_repository():
    # The unit merged acme/old!1 and was later re-pointed at a repository
    # another provider owns: the reference's OWN repository decides the
    # provider qualification, so the historical GitLab proof never retires a
    # GitHub incident for acme/old!1 — and after the reverse move the
    # GitHub-proven merge still retires it although the unit points away.
    def _two_repo_registry(old_provider):
        return load_project_registry(
            {
                "schemaVersion": 1,
                "projects": [
                    {
                        "projectId": "forge",
                        "displayName": "Forge",
                        "group": "platform",
                        "defaultBranch": "main",
                        "repositories": [
                            {
                                "provider": old_provider,
                                "identifier": "acme/old",
                                "role": "primary",
                            },
                            {
                                "provider": "github",
                                "identifier": "acme/forge",
                                "role": "secondary",
                            },
                        ],
                        "environments": [{"name": "production", "kind": "production"}],
                        "capabilities": ["planning", "implementation"],
                    }
                ],
            }
        )

    swap = _event(
        eventId="e-swap",
        idempotencyKey="k-swap",
        state="merged",
        stage="merge",
        workUnit={
            "projectId": "forge",
            "workUnitId": "wu-1",
            "changeRequestRef": "acme/forge!2",
            "repository": "acme/forge",
        },
        activity="re-pointed at a follow-up change request",
    )
    ledger = _ledger(
        *_chain(
            TO_MERGED,
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-1",
                "changeRequestRef": "acme/old!1",
                "repository": "acme/old",
            },
        ),
        swap,
    )
    stale = _incident(
        incidentId="acme/old!1:merge-state-blocked",
        kind="unclassified",
        severity="degraded",
        evidenceRef="acme/old!1",
        summary="The merged change request still reports blocked",
    )
    foreign = build_control_center_snapshot(
        generated_at=NOW,
        registry=_two_repo_registry("gitlab"),
        projections=ledger.projections(),
        incidents=load_incident_document(_incidents(stale)),
    )
    kept = [item.incident_id for item in foreign.get("forge").incidents]
    assert kept == ["acme/old!1:merge-state-blocked"]
    agreeing = build_control_center_snapshot(
        generated_at=NOW,
        registry=_two_repo_registry("github"),
        projections=ledger.projections(),
        incidents=load_incident_document(_incidents(stale)),
    )
    assert [item.incident_id for item in agreeing.get("forge").incidents] == []


def test_credential_bearing_unit_identifiers_fail_closed():
    # A work-unit identifier is identity, not a reference: it cannot be
    # redacted without breaking every join, so one embedding URL userinfo
    # refuses the build — and the error withholds the value.
    ledger = _ledger(
        *_chain(
            TO_IMPLEMENTING,
            work_unit="https://x:hunter2@host/unit",
            workUnit={
                "projectId": "forge",
                "workUnitId": "https://x:hunter2@host/unit",
                "repository": "acme/forge",
            },
        )
    )
    with pytest.raises(ControlCenterError, match="work unit identifier embeds URL userinfo"):
        build_control_center_snapshot(
            generated_at=NOW,
            registry=REGISTRY,
            projections=ledger.projections(),
            incidents=load_incident_document(_incidents()),
        )
    try:
        build_control_center_snapshot(
            generated_at=NOW,
            registry=REGISTRY,
            projections=ledger.projections(),
            incidents=load_incident_document(_incidents()),
        )
    except ControlCenterError as error:
        assert "hunter2" not in str(error)


def test_overlong_references_are_withheld():
    # The console bounds every rendered reference at 512 characters, so the
    # producer withholds longer values instead of emitting a snapshot its
    # own viewer rejects.
    long_ref = "acme/forge#" + "9" * 510
    ledger = _ledger(
        *_chain(
            TO_IMPLEMENTING,
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-1",
                "issueRef": long_ref,
            },
        )
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW, registry=REGISTRY, projections=ledger.projections()
    )
    view = snapshot.get("forge")
    assert [unit.issue_ref for unit in view.active] == [""]
    assert long_ref not in json.dumps(snapshot.as_dict())
    assert any("longer than 512 characters" in warning for warning in view.warnings)


def test_multi_provider_repository_names_are_ambiguous():
    # The registry declares acme/forge for BOTH providers and ledger
    # statuses carry no provider, so nothing identifies which provider's
    # unit this is: provider-qualified data neither joins nor retires.
    both_registry = load_project_registry(
        {
            "schemaVersion": 1,
            "projects": [
                {
                    "projectId": "forge",
                    "displayName": "Forge",
                    "group": "platform",
                    "defaultBranch": "main",
                    "repositories": [
                        {"provider": "github", "identifier": "acme/forge", "role": "primary"},
                        {"provider": "gitlab", "identifier": "acme/forge", "role": "secondary"},
                    ],
                    "environments": [{"name": "production", "kind": "production"}],
                    "capabilities": ["planning", "implementation"],
                }
            ],
        }
    )
    implementing = _ledger(
        *_chain(
            TO_IMPLEMENTING,
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-1",
                "issueRef": "acme/forge#1",
                "repository": "acme/forge",
            },
        )
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=both_registry,
        projections=implementing.projections(),
        backlog=load_backlog_document(
            _document(_backlog(issueRef="acme/forge#1", declaredState="approved"))
            | {"source": "github"}
        ),
    )
    node = snapshot.get("forge").backlog[0]
    assert (node.state, node.state_basis) == ("approved", STATE_BASIS_DECLARED)
    merged = _ledger(
        *_chain(
            TO_MERGED,
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-1",
                "changeRequestRef": "acme/forge!1",
                "repository": "acme/forge",
            },
        )
    )
    stale = _incident(
        incidentId="acme/forge!1:merge-state-blocked",
        kind="unclassified",
        severity="degraded",
        evidenceRef="acme/forge!1",
        summary="The change request reports blocked",
    )
    retained = build_control_center_snapshot(
        generated_at=NOW,
        registry=both_registry,
        projections=merged.projections(),
        incidents=load_incident_document(_incidents(stale)),
    )
    kept = [item.incident_id for item in retained.get("forge").incidents]
    assert kept == ["acme/forge!1:merge-state-blocked"]


def test_lookalike_merge_state_ids_are_not_retired():
    # Only the EXACT adapter id spellings count: a suffixed lookalike can
    # identify a distinct post-merge incident and must stay visible.
    ledger = _ledger(
        *_chain(
            TO_MERGED,
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-1",
                "changeRequestRef": "acme/forge!1",
            },
        )
    )
    lookalike = _incident(
        incidentId="acme/forge!1:merge-state-blocked:post-merge",
        kind="unclassified",
        severity="degraded",
        evidenceRef="acme/forge!1",
        summary="A post-merge regression traced to this change request",
    )
    exact = _incident(
        incidentId="acme/forge!1:merge-state-unstable",
        kind="unclassified",
        severity="degraded",
        evidenceRef="acme/forge!1",
        summary="Change request mergeability is unstable",
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=ledger.projections(),
        incidents=load_incident_document(_incidents(lookalike, exact)),
    )
    kept = [item.incident_id for item in snapshot.get("forge").incidents]
    assert kept == ["acme/forge!1:merge-state-blocked:post-merge"]


def test_pulls_canonicalize_the_repository_when_issues_are_empty(tmp_path):
    # An empty issues payload cannot supply the provider spelling; the pulls
    # payload can, and the export must emit it so references join exactly.
    from agentic_sdlc.cli import main as cli_main

    issues = tmp_path / "issues.json"
    issues.write_text("[]", encoding="utf-8")
    pulls = tmp_path / "pulls.json"
    pulls.write_text(
        json.dumps(
            [
                {
                    "number": 1,
                    "title": "green",
                    "state": "open",
                    "draft": False,
                    "mergeable_state": "clean",
                    "base": {"repo": {"full_name": "Acme/Forge"}},
                }
            ]
        ),
        encoding="utf-8",
    )
    backlog_output = tmp_path / "backlog.json"
    incidents_output = tmp_path / "incidents.json"
    assert (
        cli_main(
            [
                "github-backlog",
                "--issues",
                str(issues),
                "--pulls",
                str(pulls),
                "--project-id",
                "forge",
                "--repository",
                "acme/forge",
                "--observed-at",
                NOW,
                "--backlog-output",
                str(backlog_output),
                "--incidents-output",
                str(incidents_output),
            ]
        )
        == 0
    )
    incidents = json.loads(incidents_output.read_text(encoding="utf-8"))
    refs = [item["evidenceRef"] for item in incidents["incidents"]]
    assert refs == ["Acme/Forge!1"]


def test_draft_ownership_is_validated_before_the_skip():
    # A mistargeted or partial export must fail closed even when every pull
    # is a draft: the skip must not hide missing or foreign ownership.
    foreign = {
        "number": 9,
        "title": "wip",
        "state": "open",
        "draft": True,
        "mergeable_state": "unknown",
        "base": {"repo": {"full_name": "other/repo"}},
    }
    with pytest.raises(ControlCenterError, match="belongs to other/repo"):
        incidents_from_change_requests(
            [foreign], project_id="forge", repository="acme/forge", observed_at=NOW
        )
    partial = {
        "number": 9,
        "title": "wip",
        "state": "open",
        "draft": True,
        "mergeable_state": "unknown",
    }
    with pytest.raises(ControlCenterError, match="base.repo.full_name is required"):
        incidents_from_change_requests(
            [partial], project_id="forge", repository="acme/forge", observed_at=NOW
        )


def test_closed_ownership_is_validated_before_the_skip():
    # An all-closed foreign export must fail closed too: ownership is
    # source-level validation, so the closed skip must not let a pulls file
    # fetched from another repository publish an apparently valid empty
    # incident document that conceals the target's open change requests.
    foreign = {
        "number": 9,
        "title": "done elsewhere",
        "state": "closed",
        "draft": False,
        "mergeable_state": "unknown",
        "base": {"repo": {"full_name": "other/repo"}},
    }
    with pytest.raises(ControlCenterError, match="belongs to other/repo"):
        incidents_from_change_requests(
            [foreign], project_id="forge", repository="acme/forge", observed_at=NOW
        )
    missing = {
        "number": 9,
        "title": "done",
        "state": "closed",
        "draft": False,
        "mergeable_state": "unknown",
    }
    with pytest.raises(ControlCenterError, match="base.repo.full_name is required"):
        incidents_from_change_requests(
            [missing], project_id="forge", repository="acme/forge", observed_at=NOW
        )


def test_post_merge_transitions_mint_no_fresh_merge_proof():
    # The unit merged !1, was re-pointed at unmerged !2, and then entered
    # deploying: merge proof binds at the actual merge transition alone, so
    # the follow-up request must stay blocked through every post-merge state.
    swap = _event(
        eventId="e-swap",
        idempotencyKey="k-swap",
        state="merged",
        stage="merge",
        workUnit={
            "projectId": "forge",
            "workUnitId": "wu-1",
            "changeRequestRef": "acme/forge!2",
        },
        activity="re-pointed at a follow-up change request",
    )
    deploying = _event(
        eventId="e-deploy",
        idempotencyKey="k-deploy",
        state="deploying",
        stage="deploy",
        workUnit={"projectId": "forge", "workUnitId": "wu-1"},
    )
    ledger = _ledger(
        *_chain(
            TO_MERGED,
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-1",
                "changeRequestRef": "acme/forge!1",
            },
        ),
        swap,
        deploying,
    )
    blocked = _incident(
        incidentId="acme/forge!2:merge-state-blocked",
        kind="unclassified",
        severity="degraded",
        evidenceRef="acme/forge!2",
        summary="The follow-up change request is blocked",
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=ledger.projections(),
        incidents=load_incident_document(_incidents(blocked)),
    )
    kept = [item.incident_id for item in snapshot.get("forge").incidents]
    assert kept == ["acme/forge!2:merge-state-blocked"]


def test_document_unit_identifiers_fail_closed():
    # A credential-bearing workUnitId from a backlog or incident document is
    # serialized and repeated by the unknown-unit warning whether or not any
    # projection matches it, so it fails closed at load — value withheld.
    with pytest.raises(ControlCenterError, match="workUnitId embeds URL userinfo") as items_error:
        load_backlog_document(_document(_backlog(workUnitId="https://x:hunter2@host/unit")))
    assert "hunter2" not in str(items_error.value)
    with pytest.raises(
        ControlCenterError, match="workUnitId embeds URL userinfo"
    ) as incident_error:
        load_incident_document(_incidents(_incident(workUnitId="https://x:hunter2@host/unit")))
    assert "hunter2" not in str(incident_error.value)


def test_same_path_providers_coexist_in_one_project():
    # One project may own github:acme/forge and gitlab:acme/forge, whose
    # adapters naturally emit the same item and incident ids: identity is
    # provider-scoped, so both providers' work appears instead of a
    # duplicate-ID refusal, and each record carries its provider.
    github_items = load_backlog_document(
        {
            "schemaVersion": BACKLOG_SCHEMA_VERSION,
            "source": "github",
            "items": [_backlog(title="GitHub task")],
        }
    )
    gitlab_items = load_backlog_document(
        {
            "schemaVersion": BACKLOG_SCHEMA_VERSION,
            "source": "gitlab",
            "items": [_backlog(title="GitLab task")],
        }
    )
    github_incident = load_incident_document(
        _incidents(_incident(incidentId="acme/forge#1:untriaged", kind="triage-needed"))
    )
    gitlab_incident = load_incident_document(
        {
            "schemaVersion": INCIDENT_SCHEMA_VERSION,
            "source": "gitlab",
            "incidents": [_incident(incidentId="acme/forge#1:untriaged", kind="triage-needed")],
        }
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=(),
        backlog=tuple(github_items) + tuple(gitlab_items),
        incidents=tuple(github_incident) + tuple(gitlab_incident),
    )
    view = snapshot.get("forge")
    titles = sorted(node.item.title for node in view.backlog)
    assert titles == ["GitHub task", "GitLab task"]
    providers = sorted(node.item.source_provider for node in view.backlog)
    assert providers == ["github", "gitlab"]
    assert sorted(incident.source_provider for incident in view.incidents) == [
        "github",
        "gitlab",
    ]
    document = snapshot.as_dict()
    serialized = json.dumps(document)
    assert '"sourceProvider": "gitlab"' in serialized


def test_credential_bearing_project_identifiers_fail_closed():
    # A ledger project identifier is serialized as a project key, display
    # name and warning text, so one embedding URL userinfo refuses the build
    # with the value withheld.
    # The registry-checked ledger would refuse the unregistered project, so
    # the hostile projection comes through a permissive (registry-less)
    # ledger — exactly the configuration the snapshot must still defend.
    ledger = EventLedger()
    for event in _chain(
        TO_IMPLEMENTING,
        work_unit="wu-1",
        workUnit={
            "projectId": "https://x:hunter2@host/project",
            "workUnitId": "wu-1",
        },
    ):
        event["workUnit"]["projectId"] = "https://x:hunter2@host/project"
        ledger.append(load_lifecycle_event(event))
    with pytest.raises(ControlCenterError, match="project identifier embeds URL userinfo") as err:
        build_control_center_snapshot(
            generated_at=NOW,
            registry=REGISTRY,
            projections=ledger.projections(),
            incidents=load_incident_document(_incidents()),
        )
    assert "hunter2" not in str(err.value)


def test_legacy_incident_ambiguity_spans_all_providers():
    # A provider-less legacy incident whose item id exists under BOTH
    # providers never joins, even when only one provider's item carries
    # ledger evidence: one provider's approval must not retire the other
    # provider's blocker.
    ledger = _ledger(
        *_chain(
            TO_IMPLEMENTING,
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-1",
                "issueRef": "acme/forge#1",
            },
        )
    )
    github_items = load_backlog_document(
        {
            "schemaVersion": BACKLOG_SCHEMA_VERSION,
            "source": "github",
            "items": [_backlog(title="GitHub task", issueRef="acme/forge#1")],
        }
    )
    gitlab_items = load_backlog_document(
        {
            "schemaVersion": BACKLOG_SCHEMA_VERSION,
            "source": "gitlab",
            "items": [_backlog(title="GitLab task", declaredState="blocked")],
        }
    )
    legacy = {
        "schemaVersion": INCIDENT_SCHEMA_VERSION,
        "incidents": [
            _incident(
                incidentId="acme/forge#1:untriaged",
                kind="triage-needed",
                severity="advisory",
                backlogItemId="acme/forge#1",
            )
        ],
    }
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=ledger.projections(),
        backlog=tuple(github_items) + tuple(gitlab_items),
        incidents=load_incident_document(legacy),
    )
    kept = [item.incident_id for item in snapshot.get("forge").incidents]
    assert kept == ["acme/forge#1:untriaged"]


def test_providerless_exact_matches_still_respect_ambiguity():
    # A providerless legacy incident matching a providerless legacy item is
    # STILL ambiguous when another provider carries the same id: the legacy
    # item's ledger evidence must not retire the incident.
    ledger = _ledger(
        *_chain(
            TO_IMPLEMENTING,
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-1",
                "issueRef": "acme/forge#1",
            },
        )
    )
    legacy_items = load_backlog_document(
        _document(_backlog(title="Legacy task", issueRef="acme/forge#1"))
    )
    github_items = load_backlog_document(
        {
            "schemaVersion": BACKLOG_SCHEMA_VERSION,
            "source": "github",
            "items": [_backlog(title="GitHub task", declaredState="blocked")],
        }
    )
    legacy_incident = {
        "schemaVersion": INCIDENT_SCHEMA_VERSION,
        "incidents": [
            _incident(
                incidentId="acme/forge#1:untriaged",
                kind="triage-needed",
                severity="advisory",
                backlogItemId="acme/forge#1",
            )
        ],
    }
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=ledger.projections(),
        backlog=tuple(legacy_items) + tuple(github_items),
        incidents=load_incident_document(legacy_incident),
    )
    kept = [item.incident_id for item in snapshot.get("forge").incidents]
    assert kept == ["acme/forge#1:untriaged"]


def test_dependency_waits_are_not_running_now():
    # A dependency-wait stage means nothing executes, whatever durable state
    # the unit holds: it is excluded from active and counted separately so
    # the console can still reconcile exact membership.
    ledger = _ledger(
        *_chain(
            TO_IMPLEMENTING,
            stage="dependency-wait",
            workUnit={"projectId": "forge", "workUnitId": "wu-1", "repository": "acme/forge"},
        )
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=ledger.projections(),
        incidents=load_incident_document(_incidents()),
    )
    view = snapshot.get("forge")
    assert view.active == ()
    assert view.dependency_wait_counts == {"implementing": 1}
    # Held units are SURFACED, never silently dropped: a project held
    # entirely by upstream dependencies must not read as idle with no unit
    # in flight and no explanation.
    assert [status.work_unit_id for status in view.waiting] == ["wu-1"]
    # Serialized health says WAITING, not idle: non-browser consumers read
    # the same state the console derives.
    assert view.health == "waiting"
    document = snapshot.as_dict()
    project = document["projects"][0]
    counts = project["counts"]
    assert counts["dependencyWaitByState"] == {"implementing": 1}
    assert counts["byState"]["implementing"] == 1
    assert [unit["workUnitId"] for unit in project["waiting"]] == ["wu-1"]
    assert project["health"] == "waiting"


def test_a_repositoryless_status_never_joins_a_conflicting_provider():
    # A ledger status that omits its repository is not thereby in agreement
    # with every provider: the joined reference names acme/forge, and a
    # registry declaring acme/forge ONLY for gitlab is positive disagreement
    # with a github-sourced item — the item keeps its declared state.
    gitlab_registry = load_project_registry(
        {
            "schemaVersion": 1,
            "projects": [
                {
                    "projectId": "forge",
                    "displayName": "Forge",
                    "group": "platform",
                    "defaultBranch": "main",
                    "repositories": [
                        {"provider": "gitlab", "identifier": "acme/forge", "role": "primary"}
                    ],
                    "environments": [{"name": "production", "kind": "production"}],
                    "capabilities": ["planning"],
                }
            ],
        }
    )
    ledger = _ledger(
        *_chain(
            TO_IMPLEMENTING,
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-1",
                "issueRef": "acme/forge#1",
            },
        )
    )
    github_items = load_backlog_document(
        {
            "schemaVersion": BACKLOG_SCHEMA_VERSION,
            "source": "github",
            "items": [
                _backlog(title="GitHub task", issueRef="acme/forge#1", declaredState="specified")
            ],
        }
    )
    conflicted = build_control_center_snapshot(
        generated_at=NOW,
        registry=gitlab_registry,
        projections=ledger.projections(),
        backlog=tuple(github_items),
        incidents=load_incident_document(_incidents()),
    )
    bases = [node.state_basis for node in conflicted.get("forge").backlog]
    assert bases == ["state declared by the backlog source"]
    # The same join under a registry declaring the repository FOR github
    # still adopts the ledger state.
    agreeing = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=ledger.projections(),
        backlog=tuple(github_items),
        incidents=load_incident_document(_incidents()),
    )
    assert [node.state_basis for node in agreeing.get("forge").backlog] != [
        "state declared by the backlog source"
    ]
    # Only the CANONICAL repo#N / repo!N shape supplies registry evidence: a
    # free-form identifier like acme/forge#ticket proves nothing about the
    # repository, so the otherwise exact join is not refused.
    freeform_ledger = _ledger(
        *_chain(
            TO_IMPLEMENTING,
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-1",
                "issueRef": "acme/forge#ticket",
            },
        )
    )
    freeform_items = load_backlog_document(
        {
            "schemaVersion": BACKLOG_SCHEMA_VERSION,
            "source": "github",
            "items": [
                _backlog(
                    title="Free-form task",
                    issueRef="acme/forge#ticket",
                    declaredState="specified",
                )
            ],
        }
    )
    freeform = build_control_center_snapshot(
        generated_at=NOW,
        registry=gitlab_registry,
        projections=freeform_ledger.projections(),
        backlog=tuple(freeform_items),
        incidents=load_incident_document(_incidents()),
    )
    assert [node.state_basis for node in freeform.get("forge").backlog] == [
        "state projected from the lifecycle ledger"
    ]
    # The suffix the loader permits for INCIDENT ids does not make a
    # reference canonical: acme/forge#1:ticket is noncanonical too, so it
    # supplies no registry evidence and the exact join is not refused.
    suffixed_ledger = _ledger(
        *_chain(
            TO_IMPLEMENTING,
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-1",
                "issueRef": "acme/forge#1:ticket",
            },
        )
    )
    suffixed_items = load_backlog_document(
        {
            "schemaVersion": BACKLOG_SCHEMA_VERSION,
            "source": "github",
            "items": [
                _backlog(
                    title="Suffixed task",
                    issueRef="acme/forge#1:ticket",
                    declaredState="specified",
                )
            ],
        }
    )
    suffixed = build_control_center_snapshot(
        generated_at=NOW,
        registry=gitlab_registry,
        projections=suffixed_ledger.projections(),
        backlog=tuple(suffixed_items),
        incidents=load_incident_document(_incidents()),
    )
    assert [node.state_basis for node in suffixed.get("forge").backlog] == [
        "state projected from the lifecycle ledger"
    ]


def test_mismatch_errors_withhold_credentialed_identities():
    # Rejection messages reach stderr and CI logs: a captured repository
    # identity carrying token-shaped text is withheld from the
    # change-request and sub-issue mismatch errors like every rejected URL.
    with pytest.raises(ControlCenterError) as pull_error:
        incidents_from_change_requests(
            [
                {
                    "number": 7,
                    "state": "open",
                    "base": {"repo": {"full_name": "acme/forge?access_token=SUPERSECRET"}},
                }
            ],
            project_id="forge",
            repository="acme/forge",
            observed_at="2026-10-12T10:30:00Z",
        )
    assert "SUPERSECRET" not in str(pull_error.value)
    assert "withheld" in str(pull_error.value)
    with pytest.raises(ControlCenterError) as parent_error:
        parent_links(
            [
                {
                    "number": 3,
                    "repository_url": "https://api.github.com/repos/acme/forge",
                    "parent_issue_url": (
                        "https://api.github.com/repos/access_token=TOPSECRET/repo/issues/1"
                    ),
                }
            ],
            repository="acme/forge",
        )
    assert "TOPSECRET" not in str(parent_error.value)
    assert "withheld" in str(parent_error.value)


def test_contested_issue_references_stay_unresolved():
    # Items of two providers share an issue reference and the ledger status
    # carries no repository. With the registry declaring the repository for
    # BOTH providers, nothing identifies whose unit it is, so neither tree
    # adopts the ledger state; with it declared for exactly ONE provider,
    # the reference itself is registry evidence and only that provider's
    # tree adopts.
    both_registry = load_project_registry(
        {
            "schemaVersion": 1,
            "projects": [
                {
                    "projectId": "forge",
                    "displayName": "Forge",
                    "group": "platform",
                    "defaultBranch": "main",
                    "repositories": [
                        {"provider": "github", "identifier": "acme/forge", "role": "primary"},
                        {"provider": "gitlab", "identifier": "acme/forge", "role": "secondary"},
                    ],
                    "environments": [{"name": "production", "kind": "production"}],
                    "capabilities": ["planning"],
                }
            ],
        }
    )
    ledger = _ledger(
        *_chain(
            TO_IMPLEMENTING,
            workUnit={
                "projectId": "forge",
                "workUnitId": "wu-1",
                "issueRef": "acme/forge#1",
            },
        )
    )
    github_items = load_backlog_document(
        {
            "schemaVersion": BACKLOG_SCHEMA_VERSION,
            "source": "github",
            "items": [
                _backlog(title="GitHub task", issueRef="acme/forge#1", declaredState="specified")
            ],
        }
    )
    gitlab_items = load_backlog_document(
        {
            "schemaVersion": BACKLOG_SCHEMA_VERSION,
            "source": "gitlab",
            "items": [
                _backlog(title="GitLab task", issueRef="acme/forge#1", declaredState="blocked")
            ],
        }
    )
    snapshot = build_control_center_snapshot(
        generated_at=NOW,
        registry=both_registry,
        projections=ledger.projections(),
        backlog=tuple(github_items) + tuple(gitlab_items),
        incidents=load_incident_document(_incidents()),
    )
    bases = {node.item.title: node.state_basis for node in snapshot.get("forge").backlog}
    assert bases == {
        "GitHub task": "state declared by the backlog source",
        "GitLab task": "state declared by the backlog source",
    }
    # REGISTRY declares acme/forge for github alone: the contested join
    # adopts for exactly that provider through the reference's repository,
    # even though the status itself omits one.
    proven = build_control_center_snapshot(
        generated_at=NOW,
        registry=REGISTRY,
        projections=ledger.projections(),
        backlog=tuple(github_items) + tuple(gitlab_items),
        incidents=load_incident_document(_incidents()),
    )
    proven_bases = {node.item.title: node.state_basis for node in proven.get("forge").backlog}
    assert proven_bases == {
        "GitHub task": "state projected from the lifecycle ledger",
        "GitLab task": "state declared by the backlog source",
    }
