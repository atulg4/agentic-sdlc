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
    return {"schemaVersion": INCIDENT_SCHEMA_VERSION, "incidents": list(entries)}


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
    "ready-for-human-merge": "independent-review",
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
                {"number": 2, "parent_issue_url": ".../repos/acme/forge/issues/1"},
                {"number": 2, "parent_issue_url": ".../repos/acme/forge/issues/3"},
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
            },
            {
                "number": 2,
                "title": "red",
                "state": "open",
                "mergeable_state": "blocked",
                "draft": False,
            },
            {
                "number": 3,
                "title": "ready",
                "state": "open",
                "mergeable_state": "clean",
                "draft": False,
            },
            {
                "number": 4,
                "title": "draft",
                "state": "open",
                "draft": True,
                "mergeable_state": "clean",
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
            },
            {
                "number": 2,
                "title": "wip red",
                "state": "open",
                "draft": True,
                "mergeable_state": "blocked",
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
        [{"number": 1, "title": "list payload only", "state": "open", "draft": False}],
        project_id="forge",
        repository="acme/forge",
        observed_at=NOW,
    )
    assert incidents == []


def test_a_failing_check_is_reported_even_without_a_mergeability_verdict():
    incidents = incidents_from_change_requests(
        [{"number": 1, "title": "red", "state": "open", "draft": False}],
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
                [{"number": 2, "parent_issue_url": ".../repos/acme/forge/issues/9"}],
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
        },
        {
            "number": 2,
            "title": "hooks",
            "state": "open",
            "mergeable_state": "has_hooks",
            "draft": False,
        },
        {
            "number": 3,
            "title": "blocked",
            "state": "open",
            "mergeable_state": "blocked",
            "draft": False,
        },
        {
            "number": 4,
            "title": "behind",
            "state": "open",
            "mergeable_state": "behind",
            "draft": False,
        },
        {
            "number": 5,
            "title": "unstable",
            "state": "open",
            "mergeable_state": "unstable",
            "draft": False,
        },
        {
            "number": 6,
            "title": "unknown",
            "state": "open",
            "mergeable_state": "unknown",
            "draft": False,
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
        "Specification drafted and waiting on a human implementation approval"
    )


def test_full_length_github_names_fit_generated_identifiers():
    owner = "o" * 39
    repository = f"{owner}/{'r' * 100}"
    document = backlog_document_from_issues(
        [_issue(123456, "long names")],
        project_id="forge",
        repository=repository,
    )
    # itemId carries the repository verbatim; the loader must accept it.
    items = load_backlog_document(
        {**document, "items": [{**document["items"][0], "projectId": "forge"}]}
    )
    assert items[0].item_id == f"{repository}#123456"
    incidents = incidents_from_issues(
        [_issue(123456, "long names")],
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
    )
    implement_advisory = _incident(
        incidentId="acme/forge#7:eligible-awaiting-dispatch-evidence",
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
        json.dumps([{"number": 7, "title": "x", "state": "open", "draft": False}]), encoding="utf-8"
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
            },
            {
                "number": 2,
                "title": "open",
                "state": "open",
                "mergeable_state": "clean",
                "draft": False,
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
        json.dumps([{"number": 1, "title": "bare", "state": "open", "labels": []}]),
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


def test_a_cross_project_parent_across_documents_is_rejected():
    first = load_backlog_document(_document(_backlog(itemId="child", parentId="x")))
    second = load_backlog_document(
        _document(_backlog(itemId="x", projectId="ghost", title="Other"))
    )
    with pytest.raises(ControlCenterError, match="parent in another project"):
        build_control_center_snapshot(generated_at=NOW, registry=REGISTRY, backlog=first + second)


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
                [{"number": 1, "title": "bad labels", "state": "open", "labels": bad}],
                project_id="forge",
                repository="acme/forge",
            )


def test_a_null_or_absent_labels_field_defaults_to_no_labels():
    document = backlog_document_from_issues(
        [
            {"number": 1, "title": "null labels", "state": "open", "labels": None},
            {"number": 2, "title": "no labels", "state": "open"},
        ],
        project_id="forge",
        repository="acme/forge",
    )
    assert [item["labels"] for item in document["items"]] == [[], []]


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
        record = {"number": bad, "title": "odd", "state": "open", "labels": []}
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
                    }
                ],
                project_id="forge",
                repository="acme/forge",
                observed_at=NOW,
            )


def test_an_absent_draft_verdict_is_a_controlled_error():
    for pull in (
        {"number": 1, "title": "no draft", "state": "open", "mergeable_state": "clean"},
        {
            "number": 1,
            "title": "null draft",
            "state": "open",
            "draft": None,
            "mergeable_state": "clean",
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
        {"number": 1, "title": "no state", "labels": []},
        {"number": 1, "title": "null state", "labels": [], "state": None},
    ):
        with pytest.raises(ControlCenterError, match="state must be"):
            incidents_from_issues(
                [record], project_id="forge", repository="acme/forge", observed_at=NOW
            )
        with pytest.raises(ControlCenterError, match="state must be"):
            backlog_document_from_issues([record], project_id="forge", repository="acme/forge")
    for pull in (
        {"number": 1, "title": "no state", "mergeable_state": "clean", "draft": False},
        {
            "number": 1,
            "title": "null state",
            "state": None,
            "mergeable_state": "clean",
            "draft": False,
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
    with pytest.raises(ControlCenterError, match="another repository"):
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
        json.dumps([{"number": 1, "title": "spelled", "state": "open", "labels": []}]),
        encoding="utf-8",
    )
    assert run(matching) == 0
    # The policy's spelling is canonical: a case-differing --repository must
    # not leak into references, or ledger joins by exact string would miss.
    written = json.loads((tmp_path / "backlog.json").read_text(encoding="utf-8"))
    assert written["items"][0]["itemId"] == "ACME/forge#1"


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
                    }
                ],
                project_id="forge",
                repository="acme/forge",
                observed_at=NOW,
            )


def test_a_bare_sub_issue_record_without_a_parent_is_rejected():
    for record in (
        {"number": 5},
        {"number": 5, "parent_issue_url": "https://api.github.com/not-an-issue"},
        {"number": 5, "parent_issue_url": None},
    ):
        with pytest.raises(ControlCenterError, match="no usable parent_issue_url"):
            parent_links([record])


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
            [{"number": 1, "title": "bad", "state": "open", "mergeable_state": [], "draft": False}],
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


def test_a_malformed_declared_parent_is_a_controlled_error():
    for bad in (True, 0, -2):
        with pytest.raises(ControlCenterError, match="declared parent number"):
            parent_links(([{"number": 2}], bad))


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
                [{"number": 1, "title": "odd", "state": bad, "labels": []}],
                project_id="forge",
                repository="acme/forge",
                observed_at=NOW,
            )
        with pytest.raises(ControlCenterError, match="state must be"):
            backlog_document_from_issues(
                [{"number": 1, "title": "odd", "state": bad, "labels": []}],
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
            [{"number": 1, "title": "bad entry", "state": "open", "labels": [{}]}],
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
