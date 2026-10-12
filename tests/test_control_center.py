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
    backlog_document_from_issues,
    incident_document,
    incidents_from_change_requests,
    incidents_from_issues,
    parent_links,
)
from agentic_sdlc.project_registry import load_project_registry

NOW = "2026-10-11T21:30:00Z"

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


def test_a_parent_outside_the_exported_set_is_left_unlinked():
    document = _export(_issue(2, "child"), parents={2: 99})
    assert "parentId" not in document["items"][0]


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


def test_a_fully_labelled_issue_raises_no_dispatch_incident():
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
    assert incidents == []


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
    assert incidents[0]["kind"] == "awaiting-human-approval"


def test_every_dispatch_rule_names_a_declared_incident_kind():
    for rule in DISPATCH_RULES:
        assert rule.kind in INCIDENT_KINDS


def test_change_request_incidents_distinguish_conflict_ci_and_merge_wait():
    incidents = incidents_from_change_requests(
        [
            {"number": 1, "title": "conflicted", "mergeable_state": "dirty"},
            {"number": 2, "title": "red", "mergeable_state": "blocked"},
            {"number": 3, "title": "ready", "mergeable_state": "clean"},
            {"number": 4, "title": "draft", "draft": True, "mergeable_state": "clean"},
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
            [{"number": 9, "title": "ready", "mergeable_state": "clean"}],
            project_id="forge",
            repository="acme/forge",
            observed_at=NOW,
        ),
    )
    assert len(load_incident_document(document)) == 2


def test_the_adapter_requires_a_project_and_a_repository():
    with pytest.raises(ControlCenterError, match="project_id is required"):
        backlog_document_from_issues([], project_id="", repository="acme/forge")
    with pytest.raises(ControlCenterError, match="repository is required"):
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
            {"number": 1, "title": "wip", "draft": True, "mergeable_state": "dirty"},
            {"number": 2, "title": "wip red", "draft": True, "mergeable_state": "blocked"},
        ],
        project_id="forge",
        repository="acme/forge",
        observed_at=NOW,
        failing={2: "test"},
    )
    assert incidents == []


def test_a_change_request_without_a_mergeability_verdict_is_not_ready():
    incidents = incidents_from_change_requests(
        [{"number": 1, "title": "list payload only"}],
        project_id="forge",
        repository="acme/forge",
        observed_at=NOW,
    )
    assert incidents == []


def test_a_failing_check_is_reported_even_without_a_mergeability_verdict():
    incidents = incidents_from_change_requests(
        [{"number": 1, "title": "red"}],
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
