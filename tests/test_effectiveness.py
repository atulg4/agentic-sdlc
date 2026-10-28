from __future__ import annotations

import json
from typing import Any

import pytest

from agentic_sdlc.capacity_metrics import ObservationWindow
from agentic_sdlc.effectiveness import (
    COMPARABLE_METRICS,
    EFFECTIVENESS_SCHEMA_TYPES,
    MAX_TEXT_LENGTH,
    OUTCOME_SCHEMA_VERSION,
    AcceptanceCriterion,
    DeploymentStatus,
    EffectivenessError,
    EffectivenessLedger,
    LearningKind,
    LearningLedger,
    LearningRecord,
    ObservationKind,
    OutcomeObservation,
    OutcomeProvenance,
    OutcomeRecord,
    OutcomeResult,
    build_effectiveness_report,
    compare_projects,
    learning_id_for,
    load_learning_record,
    load_outcome_record,
    outcome_id_for,
    outcomes_from_usage,
    secret_bearing_effectiveness_fields,
)
from agentic_sdlc.event_ledger import EventActor, LifecycleStage, WorkUnitRef
from agentic_sdlc.project_registry import load_project_registry
from agentic_sdlc.usage_ledger import (
    InfrastructureUsage,
    MonetaryStatus,
    TokenCounts,
    UsageActual,
    UsageEstimate,
    UsageRecord,
    UsageResult,
    load_pricing_snapshot,
    monetary_equivalent,
    usage_id_for,
)

T0 = "2026-10-01T09:00:00Z"
T1 = "2026-10-01T17:00:00Z"
T2 = "2026-10-03T09:00:00Z"

WINDOW = ObservationWindow("2026-10-01T00:00:00Z", "2026-10-03T00:00:00Z")

PAYG = load_pricing_snapshot(
    {
        "pricingId": "anthropic-payg-2026-10",
        "provider": "anthropic",
        "model": "claude-opus-5",
        "billingMode": "payg",
        "version": "2026-10-01",
        "source": "published price list",
        "usdPerMillion": {"input": 15, "output": 75, "cacheRead": 1.5, "cacheWrite": 18.75},
    }
)
CLAUDE_MAX = load_pricing_snapshot(
    {
        "pricingId": "claude-max-20x",
        "provider": "anthropic",
        "model": "claude-opus-5",
        "billingMode": "subscription",
        "version": "2026-10",
        "usdPerMillion": {"input": 15, "output": 75},
        "plan": {
            "name": "Claude Max 20x",
            "capacityUnit": "five-hour-window-share",
            "monthlyUsd": 200,
        },
    }
)
TOKENS = TokenCounts(input=400_000, output=100_000, cache_read=0, cache_write=0)


def _work_unit(
    project_id: str = "alpha", unit: str = "github:example/alpha:issue:1"
) -> WorkUnitRef:
    return WorkUnitRef(
        project_id=project_id,
        work_unit_id=unit,
        repository=f"example/{project_id}",
        issue_ref=f"example/{project_id}#1",
        change_request_ref=f"example/{project_id}!12",
        workflow_ref=".github/workflows/reusable-implement.yml",
        run_ref="34619405233",
    )


def _provenance(**overrides: Any) -> OutcomeProvenance:
    defaults: dict[str, Any] = {
        "source": "forge-ci",
        "lifecycle_event_id": "alpha:0012",
        "commit_sha": "d" * 40,
        "release_ref": "v0.2.0",
        "evidence_refs": ("gate-report.json", "review-report.json"),
    }
    defaults.update(overrides)
    return OutcomeProvenance(**defaults)


def _outcome(
    *,
    project_id: str = "alpha",
    unit: str = "github:example/alpha:issue:1",
    release_ref: str = "v0.2.0",
    result: OutcomeResult = OutcomeResult.DELIVERED,
    deployment_status: DeploymentStatus = DeploymentStatus.VERIFIED,
    task_class: str = "implementation",
    **overrides: Any,
) -> OutcomeRecord:
    fields: dict[str, Any] = {
        "outcome_id": outcome_id_for(project_id, unit, release_ref),
        "work_unit": _work_unit(project_id, unit),
        "intended_outcome": "Ledger-backed effectiveness scoring for every project",
        "recorded_at": T2,
        "provenance": _provenance(release_ref=release_ref),
        "result": result,
        "deployment_status": deployment_status,
        "task_class": task_class,
        "delivered_capability": "sdlcctl effectiveness-report",
        "acceptance_criteria": (
            AcceptanceCriterion(text="value metrics never fabricate", met=True),
            AcceptanceCriterion(text="learnings are append-only", met=True),
        ),
        "first_pass_verification": True,
        "first_pass_review_accepted": True,
        "repair_rounds": 0,
        "review_rounds": 1,
        "regression_escaped": False,
        "reopened": False,
        "reverted": False,
        "rolled_back": False,
        "defect_followups": 0,
        "intake_at": T0,
        "merged_at": T1,
        "live_at": T2,
    }
    fields.update(overrides)
    return OutcomeRecord(**fields)


def _learning(
    *,
    kind: LearningKind = LearningKind.DETERMINISTIC_CHECK_CANDIDATE,
    statement: str = "review keeps flagging missing docstrings on new public functions",
    recorded_at: str = T2,
    **overrides: Any,
) -> LearningRecord:
    fields: dict[str, Any] = {
        "learning_id": learning_id_for(kind.value, statement, recorded_at),
        "kind": kind,
        "statement": statement,
        "recorded_at": recorded_at,
        "provenance": _provenance(source="effectiveness-review"),
        "work_unit": _work_unit(),
        "cause": "no deterministic docstring gate runs before independent review",
        "recommended_adjustment": "add a ruff docstring rule to the pre-review gate",
        "model": "claude-opus-5",
        "stage": LifecycleStage.INDEPENDENT_REVIEW,
        "task_class": "implementation",
        "sample_size": 7,
    }
    fields.update(overrides)
    return LearningRecord(**fields)


def _usage(
    run_id: str = "run-1",
    *,
    project_id: str = "alpha",
    unit: str = "github:example/alpha:issue:1",
    runtime: int | None = 1800,
    tokens: TokenCounts | None = TOKENS,
    pricing: Any = PAYG,
    estimate_runtime: int | None = 1200,
    infrastructure_usd: float | None = 0.4,
    task_class: str = "implementation",
    result: UsageResult = UsageResult.COMPLETED,
) -> UsageRecord:
    counts = tokens if tokens is not None else TokenCounts()
    return UsageRecord(
        usage_id=usage_id_for(project_id, unit, run_id),
        work_unit=_work_unit(project_id, unit),
        stage=LifecycleStage.IMPLEMENTATION,
        run_id=run_id,
        recorded_at=T1,
        task_class=task_class,
        actor=EventActor(
            name="claude",
            kind="agent",
            agent_id="claude-code",
            provider="anthropic",
            model="claude-opus-5",
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
            result=result,
            source="provider-usage-api",
        ),
        infrastructure=(
            None
            if infrastructure_usd is None
            else InfrastructureUsage(
                runner_class="ubuntu-latest", cost_usd=infrastructure_usd, basis="runner price"
            )
        ),
    )


# -- schema and round-trip ----------------------------------------------------


def test_outcome_record_round_trips_through_its_document() -> None:
    record = _outcome(
        observations=(
            OutcomeObservation(
                observation_id="obs-1",
                kind=ObservationKind.CAPABILITY_VERIFIED,
                observed_at=T2,
                observer="atulg4",
                observer_kind="human",
                healthy=True,
                environment="production",
                note="effectiveness report rendered for both projects",
                evidence_refs=("run/34619405233",),
            ),
        )
    )

    document = record.as_dict()
    reloaded = load_outcome_record(document)

    assert reloaded == record
    assert reloaded.as_dict() == document
    assert document["schemaVersion"] == OUTCOME_SCHEMA_VERSION
    assert document["accepted"] is True
    assert document["acceptanceCriteriaMet"] is True
    assert document["timeToLiveSeconds"] == 2 * 86400


def test_learning_record_round_trips_and_keeps_provenance() -> None:
    record = _learning()

    document = record.as_dict()
    reloaded = load_learning_record(document)

    assert reloaded == record
    assert document["provenance"]["source"] == "effectiveness-review"
    assert document["provenance"]["evidenceRefs"] == [
        "gate-report.json",
        "review-report.json",
    ]
    assert document["advisory"] is True
    assert document["sampleSize"] == 7


def test_outcome_identity_is_stable_and_scoped() -> None:
    first = outcome_id_for("alpha", "issue:1", "v1")
    assert first == outcome_id_for("alpha", "issue:1", "v1")
    assert first != outcome_id_for("beta", "issue:1", "v1")
    assert first != outcome_id_for("alpha", "issue:1", "v2")
    with pytest.raises(EffectivenessError, match="project is required"):
        outcome_id_for("", "issue:1")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda doc: doc.update(surprise=1), "unknown keys"),
        (lambda doc: doc.update(apiToken="x"), "must not carry credentials"),
        (lambda doc: doc.update(promptText="hello"), "must not carry credentials"),
        (lambda doc: doc.update(schemaVersion=99), "unsupported schemaVersion"),
        (lambda doc: doc.update(result="half-done"), "unknown OutcomeResult"),
        (lambda doc: doc.update(deploymentStatus="orbit"), "unknown DeploymentStatus"),
        (lambda doc: doc.update(intendedOutcome=""), "intendedOutcome is required"),
        (lambda doc: doc.update(recordedAt="yesterday"), "must be an RFC 3339 timestamp"),
        (lambda doc: doc.update(repairRounds=-1), "non-negative integer"),
        (lambda doc: doc.update(reverted="yes"), "must be a boolean or null"),
        (lambda doc: doc.update(intendedOutcome="x" * (MAX_TEXT_LENGTH + 1)), "exceeds"),
        (lambda doc: doc["workUnit"].update(projectId=""), "projectId is required"),
        (lambda doc: doc["provenance"].update(source=""), "source is required"),
    ],
)
def test_outcome_loader_fails_closed(mutate: Any, message: str) -> None:
    document = _outcome().as_dict()
    mutate(document)

    with pytest.raises(EffectivenessError, match=message):
        load_outcome_record(document)


def test_learning_loader_fails_closed_and_refuses_to_be_non_advisory() -> None:
    document = _learning().as_dict()
    document["advisory"] = False

    with pytest.raises(EffectivenessError, match="advisory cannot be false"):
        load_learning_record(document)

    document = _learning().as_dict()
    document["kind"] = "hunch"
    with pytest.raises(EffectivenessError, match="unknown LearningKind"):
        load_learning_record(document)

    document = _learning().as_dict()
    document["workUnit"] = None
    document["projectId"] = ""
    with pytest.raises(EffectivenessError, match="must name the project or work unit"):
        load_learning_record(document)


def test_observation_requires_a_known_kind_and_observer() -> None:
    with pytest.raises(EffectivenessError, match="unknown observer kind"):
        OutcomeObservation(
            observation_id="obs-1",
            kind=ObservationKind.TESTS_HEALTHY,
            observed_at=T2,
            observer="robot",
            observer_kind="robot",
        )
    with pytest.raises(EffectivenessError, match="observer is required"):
        OutcomeObservation(
            observation_id="obs-1",
            kind=ObservationKind.TESTS_HEALTHY,
            observed_at=T2,
            observer="",
        )


def test_outcome_ledger_document_round_trips() -> None:
    ledger = EffectivenessLedger()
    ledger.append(_outcome())
    ledger.append(_outcome(project_id="beta", unit="github:example/beta:issue:9"))

    document = ledger.as_dict()
    reloaded = EffectivenessLedger.from_dict(document)

    assert reloaded.as_dict() == document
    assert len(reloaded) == 2
    assert reloaded.project_ids() == ("alpha", "beta")

    document["schemaVersion"] = 99
    with pytest.raises(EffectivenessError, match="unsupported schemaVersion"):
        EffectivenessLedger.from_dict(document)


# -- outcome aggregation ------------------------------------------------------


def _mixed_sample() -> tuple[OutcomeRecord, ...]:
    """A success, a failure, a revert, a rollback and an outcome with no data."""
    return (
        _outcome(release_ref="v1-success"),
        _outcome(
            release_ref="v1-failure",
            result=OutcomeResult.NOT_DELIVERED,
            deployment_status=DeploymentStatus.NOT_DEPLOYED,
            first_pass_verification=False,
            first_pass_review_accepted=False,
            repair_rounds=2,
            review_rounds=3,
            regression_escaped=False,
            live_at="",
        ),
        _outcome(
            release_ref="v1-revert",
            result=OutcomeResult.REVERTED,
            deployment_status=DeploymentStatus.DEPLOYED,
            reverted=True,
            regression_escaped=True,
            defect_followups=2,
            repair_rounds=1,
        ),
        _outcome(
            release_ref="v1-rollback",
            result=OutcomeResult.DELIVERED,
            deployment_status=DeploymentStatus.ROLLED_BACK,
            rolled_back=True,
            reopened=True,
        ),
        OutcomeRecord(
            outcome_id=outcome_id_for("alpha", "github:example/alpha:issue:1", "v1-nodata"),
            work_unit=_work_unit(),
            intended_outcome="A capability whose delivery has not been assessed yet",
            recorded_at=T2,
            provenance=_provenance(release_ref="v1-nodata"),
            task_class="implementation",
        ),
    )


def test_aggregation_counts_successes_failures_reverts_and_no_data() -> None:
    report = build_effectiveness_report(_mixed_sample(), window=WINDOW)

    assert report["sampleCount"] == 5
    assert report["outcomes"]["byResult"]["delivered"] == 2
    assert report["outcomes"]["byResult"]["not-delivered"] == 1
    assert report["outcomes"]["byResult"]["reverted"] == 1
    assert report["outcomes"]["byResult"]["unknown"] == 1
    assert report["outcomes"]["byDeploymentStatus"]["rolled-back"] == 1
    # Only the standing delivery is accepted: the rollback and the revert are not.
    assert report["outcomes"]["accepted"] == 1
    assert report["outcomes"]["withRecordedResult"] == 4
    assert report["outcomes"]["withoutRecordedResult"] == 1
    # The unassessed outcome stays in the sample rather than improving a rate.
    assert report["quality"]["acceptedOutcomeRate"]["denominator"] == 4


def test_a_rolled_back_delivery_is_never_scored_as_an_accepted_outcome() -> None:
    rolled_back = _outcome(deployment_status=DeploymentStatus.ROLLED_BACK, rolled_back=True)
    reverted = _outcome(result=OutcomeResult.REVERTED, reverted=True)
    partial = _outcome(result=OutcomeResult.PARTIALLY_DELIVERED)

    assert rolled_back.accepted is False
    assert reverted.accepted is False
    assert partial.accepted is False
    assert _outcome().accepted is True


def test_first_pass_repair_and_regression_metrics() -> None:
    quality = build_effectiveness_report(_mixed_sample(), window=WINDOW)["quality"]

    # Four outcomes recorded a first-pass verdict; three of them passed.
    assert quality["firstPassVerificationRate"] == {
        "metric": "firstPassVerificationRate",
        "status": "observed",
        "value": 0.75,
        "numerator": 3,
        "denominator": 4,
        "reason": "",
    }
    assert quality["firstPassReviewAcceptanceRate"]["value"] == 0.75
    assert quality["meanRepairRounds"]["value"] == 0.75
    assert quality["meanRepairRounds"]["sampleSize"] == 4
    assert quality["regressionEscapeRate"]["value"] == 0.25
    assert quality["revertRate"]["value"] == 0.25
    assert quality["rollbackRate"]["value"] == 0.25
    assert quality["reopenRate"]["value"] == 0.25
    assert quality["defectFollowupRate"]["value"] == 0.25
    assert quality["acceptanceCriteriaMetRate"]["value"] == 1.0


def test_time_to_live_reports_a_distribution_only_where_both_ends_were_observed() -> None:
    quality = build_effectiveness_report(_mixed_sample(), window=WINDOW)["quality"]
    distribution = quality["timeToLiveSeconds"]

    assert distribution["status"] == "observed"
    # The not-delivered outcome and the unassessed one never went live, so they
    # are absent from the distribution rather than counted as zero.
    assert distribution["sampleSize"] == 3
    assert distribution["p50"] == 2 * 86400
    assert distribution["unit"] == "seconds"

    empty = build_effectiveness_report((_outcome(intake_at="", live_at=""),))
    assert empty["quality"]["timeToLiveSeconds"]["status"] == "unknown"
    assert empty["quality"]["timeToLiveSeconds"]["sampleSize"] == 0


def test_live_before_intake_fails_closed() -> None:
    record = _outcome(intake_at=T2, live_at=T0)
    with pytest.raises(EffectivenessError, match="liveAt precedes intakeAt"):
        assert record.time_to_live_seconds


def test_post_deploy_observations_are_counted_by_kind() -> None:
    record = _outcome(
        observations=(
            OutcomeObservation(
                observation_id="obs-1",
                kind=ObservationKind.FEATURE_EXERCISED,
                observed_at=T2,
                observer="telemetry",
                healthy=True,
            ),
            OutcomeObservation(
                observation_id="obs-2",
                kind=ObservationKind.NO_OBSERVABLE_ADOPTION,
                observed_at=T2,
                observer="telemetry",
                healthy=None,
                note="no user has opened the report yet",
            ),
        )
    )

    report = build_effectiveness_report((record, _outcome(release_ref="v2")))

    assert report["observations"]["byKind"]["feature-exercised"] == 1
    assert report["observations"]["byKind"]["no-observable-adoption-yet"] == 1
    assert report["observations"]["byKind"]["regression-observed"] == 0
    assert report["observations"]["observedOutcomes"] == 1
    assert report["observations"]["unobservedOutcomes"] == 1
    assert record.observations_of(ObservationKind.FEATURE_EXERCISED)[0].healthy is True


# -- value metrics ------------------------------------------------------------


def test_value_metrics_divide_accepted_outcomes_by_observed_effort() -> None:
    report = build_effectiveness_report(
        (_outcome(), _outcome(release_ref="v2")),
        usage_records=(_usage("run-1"), _usage("run-2")),
        window=WINDOW,
    )
    value = report["value"]

    assert value["acceptedOutcomes"] == 2
    # 2 runs x 1800s = 1 agent-hour, so two accepted outcomes per agent-hour.
    assert value["inputs"]["agentHours"] == 1.0
    assert value["perAgentHour"]["value"] == 2.0
    assert value["perAgentHour"]["unit"] == "outcomes/agent-hour"
    # 2 runs x 500k tokens = 1M tokens.
    assert value["inputs"]["tokens"] == 1_000_000
    assert value["perMillionTokens"]["value"] == 2.0
    # The window is two days long and is supplied, never inferred.
    assert value["perWallClockDay"]["value"] == 1.0
    assert value["perInfrastructureDollar"]["value"] == 2.5
    assert value["perPaygDollar"]["status"] == "observed"


def test_every_value_metric_counts_accepted_outcomes_as_its_numerator() -> None:
    """Effort can only ever be a denominator, so consumption cannot raise value."""
    report = build_effectiveness_report(
        _mixed_sample(), usage_records=(_usage("run-1"),), window=WINDOW
    )
    value = report["value"]
    accepted = value["acceptedOutcomes"]

    assert accepted == 1
    metrics = [key for key in value if key.startswith("per")]
    assert metrics
    for key in metrics:
        assert value[key]["numerator"] == accepted, key


def test_value_metrics_report_unknown_rather_than_fabricating() -> None:
    no_usage = build_effectiveness_report((_outcome(),))
    value = no_usage["value"]

    for key in ("perAgentHour", "perMillionTokens", "perInfrastructureDollar"):
        assert value[key]["status"] == "unknown"
        assert value[key]["value"] is None
        assert value[key]["reason"]
    # No window supplied means elapsed time is unknown, not zero days.
    assert value["perWallClockDay"]["status"] == "unknown"
    assert value["inputs"]["agentSeconds"] is None
    assert value["inputs"]["tokens"] is None


def test_value_metrics_never_divide_by_zero() -> None:
    zero_effort = build_effectiveness_report(
        (_outcome(),),
        usage_records=(
            _usage(
                "run-1",
                runtime=0,
                tokens=TokenCounts(input=0, output=0, cache_read=0, cache_write=0),
                infrastructure_usd=0.0,
            ),
        ),
        window=WINDOW,
    )
    value = zero_effort["value"]

    for key in ("perAgentHour", "perMillionTokens", "perInfrastructureDollar"):
        assert value[key]["status"] == "unknown"
        assert value[key]["value"] is None
        assert "undefined" in value[key]["reason"]


def test_unobserved_token_counts_are_reported_not_assumed() -> None:
    report = build_effectiveness_report(
        (_outcome(),),
        usage_records=(
            _usage("run-1", tokens=TokenCounts(input=1000)),
            _usage("run-2", runtime=None),
        ),
        window=WINDOW,
    )
    inputs = report["value"]["inputs"]

    # One record never reported a complete token count and another never reported
    # a runtime, so neither denominator is offered: a partial sum would divide two
    # accepted outcomes by half the effort and overstate the factory.
    assert inputs["unobservedRecords"]["tokens"] == 1
    assert inputs["tokens"] is None
    assert inputs["unobservedRecords"]["agentSeconds"] == 1
    assert inputs["agentSeconds"] is None
    assert report["value"]["perMillionTokens"]["status"] == "unknown"
    assert "overstate value" in report["value"]["perAgentHour"]["reason"]


def test_subscription_value_is_labeled_and_never_mixed_into_billed_dollars() -> None:
    report = build_effectiveness_report(
        (_outcome(),),
        usage_records=(_usage("run-1", pricing=CLAUDE_MAX),),
        window=WINDOW,
    )
    value = report["value"]

    assert value["perSubscriptionEquivalentDollar"]["status"] == "subscription"
    assert value["perSubscriptionEquivalentDollar"]["value"] is not None
    # Subscription usage is not billed spend, so the PAYG metric stays unknown.
    assert value["perPaygDollar"]["status"] == "unknown"
    assert value["inputs"]["paygUsd"] is None
    assert value["inputs"]["subscriptionEquivalentUsd"] is not None


def test_estimate_versus_actual_spans_task_classes_and_projects() -> None:
    report = build_effectiveness_report(
        (_outcome(), _outcome(project_id="beta", unit="github:example/beta:issue:9")),
        usage_records=(
            _usage("run-1", estimate_runtime=1200, runtime=1800),
            _usage(
                "run-2",
                project_id="beta",
                unit="github:example/beta:issue:9",
                task_class="repair",
                estimate_runtime=1200,
                runtime=600,
            ),
        ),
        window=WINDOW,
    )
    comparison = report["estimateVsActual"]

    # +50% on one run and -50% on the other averages out overall...
    assert comparison["overall"]["runtimeSeconds"]["value"] == 0.0
    assert comparison["overall"]["runtimeSeconds"]["unit"] == "percent"
    # ...but each task class and project keeps its own signed error.
    assert comparison["byTaskClass"]["implementation"]["runtimeSeconds"]["value"] == 50.0
    assert comparison["byTaskClass"]["repair"]["runtimeSeconds"]["value"] == -50.0
    assert comparison["byProject"]["alpha"]["runtimeSeconds"]["value"] == 50.0
    assert comparison["byProject"]["beta"]["runtimeSeconds"]["value"] == -50.0


def test_estimate_versus_actual_is_unknown_without_both_sides() -> None:
    report = build_effectiveness_report(
        (_outcome(),), usage_records=(_usage("run-1", estimate_runtime=None),)
    )
    runtime = report["estimateVsActual"]["overall"]["runtimeSeconds"]

    assert runtime["status"] == "unknown"
    assert runtime["value"] is None
    assert runtime["unobservedRecords"] == 1


# -- learning ledger ----------------------------------------------------------


def test_learning_ledger_is_append_only_and_versioned() -> None:
    ledger = LearningLedger()
    first = _learning()
    ledger.append(first)

    # Replaying the same learning is a no-op.
    assert ledger.append(first) is first
    assert len(ledger) == 1

    # Rewriting one in place is refused; a revision must supersede it.
    rewritten = LearningRecord(
        learning_id=first.learning_id,
        kind=first.kind,
        statement=first.statement,
        recorded_at=first.recorded_at,
        provenance=first.provenance,
        work_unit=first.work_unit,
        cause="a different cause entirely",
    )
    with pytest.raises(EffectivenessError, match="must supersede it rather than replace it"):
        ledger.append(rewritten)

    revision = _learning(
        statement="a ruff docstring rule now covers what review kept flagging",
        recorded_at="2026-10-05T09:00:00Z",
        supersedes=first.learning_id,
    )
    ledger.append(revision)

    assert len(ledger) == 2
    assert ledger.get(first.learning_id) == first
    assert [item.learning_id for item in ledger.current()] == [revision.learning_id]
    assert ledger.superseded_ids() == frozenset({first.learning_id})


def test_learning_records_retain_provenance_through_the_ledger_document() -> None:
    ledger = LearningLedger()
    ledger.append(_learning())
    ledger.append(
        _learning(
            kind=LearningKind.ESTIMATOR_ADJUSTMENT,
            statement="implementation runtime estimates run 40 percent low on this project",
        )
    )

    document = ledger.as_dict()
    reloaded = LearningLedger.from_dict(document)

    assert reloaded.as_dict() == document
    assert document["advisory"] is True
    for entry in document["learnings"]:
        assert entry["provenance"]["source"]
        assert entry["provenance"]["evidenceRefs"]
        assert entry["advisory"] is True
    assert len(reloaded) == 2


def test_superseding_an_unknown_learning_fails_closed() -> None:
    ledger = LearningLedger()
    with pytest.raises(EffectivenessError, match="supersedes unknown learning"):
        ledger.append(_learning(supersedes="learning-does-not-exist"))

    with pytest.raises(EffectivenessError, match="cannot supersede itself"):
        statement = "self-referential"
        learning_id = learning_id_for("delay-cause", statement, T2)
        LearningRecord(
            learning_id=learning_id,
            kind=LearningKind.DELAY_CAUSE,
            statement=statement,
            recorded_at=T2,
            provenance=_provenance(),
            project_id="alpha",
            supersedes=learning_id,
        )


def test_learnings_are_reported_as_advisory_in_the_report() -> None:
    report = build_effectiveness_report(
        (_outcome(),),
        learnings=(_learning(), _learning(kind=LearningKind.DELAY_CAUSE, statement="waiting")),
    )

    assert report["learnings"]["count"] == 2
    assert report["learnings"]["advisory"] is True
    assert report["learnings"]["byKind"]["deterministic-check-candidate"] == 1
    assert report["learnings"]["byKind"]["delay-cause"] == 1
    assert any("advisory evidence" in line for line in report["limitations"])


# -- completing an outcome without rewriting it -------------------------------


def test_an_outcome_is_completed_as_the_work_unit_progresses() -> None:
    ledger = EffectivenessLedger()
    outcome_id = outcome_id_for("alpha", "github:example/alpha:issue:1", "v0.2.0")
    ledger.append(
        OutcomeRecord(
            outcome_id=outcome_id,
            work_unit=_work_unit(),
            intended_outcome="Effectiveness scoring",
            recorded_at=T0,
            provenance=_provenance(),
            acceptance_criteria=(AcceptanceCriterion(text="value never fabricated"),),
            intake_at=T0,
        )
    )
    ledger.append(
        OutcomeRecord(
            outcome_id=outcome_id,
            work_unit=_work_unit(),
            intended_outcome="Effectiveness scoring",
            recorded_at=T1,
            provenance=_provenance(),
            result=OutcomeResult.DELIVERED,
            deployment_status=DeploymentStatus.DEPLOYED,
            merged_at=T1,
            first_pass_verification=True,
        )
    )
    ledger.append(
        OutcomeRecord(
            outcome_id=outcome_id,
            work_unit=_work_unit(),
            intended_outcome="Effectiveness scoring",
            recorded_at=T2,
            provenance=_provenance(),
            deployment_status=DeploymentStatus.VERIFIED,
            live_at=T2,
            observations=(
                OutcomeObservation(
                    observation_id="obs-1",
                    kind=ObservationKind.TELEMETRY_HEALTHY,
                    observed_at=T2,
                    observer="telemetry",
                    healthy=True,
                ),
            ),
        )
    )

    completed = ledger.get(outcome_id)

    assert len(ledger) == 1
    assert completed.result is OutcomeResult.DELIVERED
    assert completed.deployment_status is DeploymentStatus.VERIFIED
    assert completed.first_pass_verification is True
    assert completed.intake_at == T0
    assert completed.merged_at == T1
    assert completed.live_at == T2
    assert completed.time_to_live_seconds == 2 * 86400
    assert [item.observation_id for item in completed.observations] == ["obs-1"]


def test_a_recorded_outcome_value_can_never_be_quietly_rewritten() -> None:
    ledger = EffectivenessLedger()
    ledger.append(_outcome())

    with pytest.raises(EffectivenessError, match="result is already recorded"):
        ledger.append(_outcome(result=OutcomeResult.NOT_DELIVERED))

    with pytest.raises(EffectivenessError, match="repair_rounds is already recorded"):
        ledger.append(_outcome(repair_rounds=4))

    with pytest.raises(EffectivenessError, match="intendedOutcome is already recorded"):
        ledger.append(_outcome(intended_outcome="something else entirely"))

    assert len(ledger) == 1
    assert ledger.get(_outcome().outcome_id).result is OutcomeResult.DELIVERED


def test_an_observation_cannot_be_restated_differently() -> None:
    observation = OutcomeObservation(
        observation_id="obs-1",
        kind=ObservationKind.TESTS_HEALTHY,
        observed_at=T2,
        observer="ci",
        healthy=True,
    )
    ledger = EffectivenessLedger()
    ledger.append(_outcome(observations=(observation,)))

    # Replaying the identical observation is a no-op.
    ledger.append(_outcome(observations=(observation,)))
    assert len(ledger.get(_outcome().outcome_id).observations) == 1

    with pytest.raises(EffectivenessError, match="already recorded differently"):
        ledger.append(
            _outcome(
                observations=(
                    OutcomeObservation(
                        observation_id="obs-1",
                        kind=ObservationKind.TESTS_HEALTHY,
                        observed_at=T2,
                        observer="ci",
                        healthy=False,
                    ),
                )
            )
        )


def test_duplicate_observation_ids_are_rejected_at_construction() -> None:
    observation = OutcomeObservation(
        observation_id="obs-1",
        kind=ObservationKind.TESTS_HEALTHY,
        observed_at=T2,
        observer="ci",
    )
    with pytest.raises(EffectivenessError, match="duplicate observationId"):
        _outcome(observations=(observation, observation))


# -- cross-project isolation and comparison -----------------------------------


def test_projects_are_isolated_in_the_ledger_and_the_report() -> None:
    registry = load_project_registry(
        {
            "schemaVersion": 1,
            "projects": [
                {
                    "projectId": project_id,
                    "displayName": project_id.title(),
                    "repositories": [{"provider": "github", "identifier": f"example/{project_id}"}],
                }
                for project_id in ("alpha", "beta")
            ],
        }
    )
    ledger = EffectivenessLedger(registry=registry)
    ledger.append(_outcome())
    ledger.append(_outcome(project_id="beta", unit="github:example/beta:issue:9"))

    assert len(ledger.query(project_id="beta")) == 1
    assert {record.project_id for record in ledger.query(project_id="beta")} == {"beta"}
    assert len(ledger.query(result=OutcomeResult.DELIVERED)) == 2

    report = build_effectiveness_report(ledger.records, project_ids=("beta",))
    assert report["sampleCount"] == 1
    assert set(report["byProject"]) == {"beta"}

    with pytest.raises(EffectivenessError, match="unregistered project: gamma"):
        ledger.append(_outcome(project_id="gamma", unit="github:example/gamma:issue:1"))
    with pytest.raises(EffectivenessError, match="not both"):
        ledger.query(project_id="alpha", project_ids=["alpha"])


def test_unregistered_projects_are_refused_in_the_learning_ledger_too() -> None:
    registry = load_project_registry(
        {
            "schemaVersion": 1,
            "projects": [
                {
                    "projectId": "alpha",
                    "displayName": "Alpha",
                    "repositories": [{"provider": "github", "identifier": "example/alpha"}],
                }
            ],
        }
    )
    ledger = LearningLedger(registry=registry)
    ledger.append(_learning())

    with pytest.raises(EffectivenessError, match="unregistered project: gamma"):
        ledger.append(
            _learning(
                statement="a cross-cutting lesson from an unregistered project",
                work_unit=None,
                project_id="gamma",
            )
        )


def test_cross_project_comparison_normalizes_for_task_class_mix() -> None:
    """A project doing only easy work is not more effective than one doing hard work."""
    outcomes = []
    # alpha: 4 easy units, all delivered; 4 hard units, none delivered.
    for index in range(4):
        outcomes.append(_outcome(release_ref=f"alpha-easy-{index}", task_class="chore"))
        outcomes.append(
            _outcome(
                release_ref=f"alpha-hard-{index}",
                task_class="implementation",
                result=OutcomeResult.NOT_DELIVERED,
            )
        )
    # beta: the same per-class performance, but a workload of only easy units.
    for index in range(8):
        outcomes.append(
            _outcome(
                project_id="beta",
                unit="github:example/beta:issue:9",
                release_ref=f"beta-easy-{index}",
                task_class="chore",
            )
        )

    comparison = compare_projects(outcomes, metric="acceptedOutcomeRate")

    assert comparison["pooledTaskClassMix"] == {"chore": 0.75, "implementation": 0.25}
    alpha = comparison["projects"]["alpha"]
    beta = comparison["projects"]["beta"]

    # Raw rates make beta look twice as effective as alpha.
    assert alpha["raw"]["value"] == 0.5
    assert beta["raw"]["value"] == 1.0
    # Per task class, the two are identical where they overlap.
    assert alpha["byTaskClass"]["chore"]["value"] == 1.0
    assert beta["byTaskClass"]["chore"]["value"] == 1.0
    assert alpha["byTaskClass"]["implementation"]["value"] == 0.0
    # beta never worked the hard class, so its normalized rate is explicitly partial
    # over the weight it covered rather than extrapolated across the whole mix.
    assert alpha["normalized"]["status"] == "observed"
    assert alpha["normalized"]["value"] == 0.75
    assert alpha["normalized"]["coveredWeight"] == 1.0
    assert beta["normalized"]["status"] == "partial"
    assert beta["normalized"]["coveredWeight"] == 0.75
    assert "not the whole pooled mix" in beta["normalized"]["reason"]
    assert beta["byTaskClass"]["implementation"]["status"] == "unknown"


def test_comparison_treats_untagged_outcomes_as_unclassified_rather_than_dropping_them() -> None:
    comparison = compare_projects(
        (_outcome(task_class=""), _outcome(release_ref="v2", task_class="chore")),
    )

    assert comparison["pooledTaskClassMix"] == {"chore": 0.5, "unclassified": 0.5}
    assert comparison["projects"]["alpha"]["byTaskClass"]["unclassified"]["denominator"] == 1


def test_comparison_reports_unknown_for_a_project_with_no_observed_metric() -> None:
    comparison = compare_projects(
        (
            _outcome(regression_escaped=None),
            _outcome(
                project_id="beta",
                unit="github:example/beta:issue:9",
                regression_escaped=True,
            ),
        ),
        metric="regressionEscapeRate",
    )

    assert comparison["projects"]["alpha"]["normalized"]["status"] == "unknown"
    assert comparison["projects"]["alpha"]["normalized"]["value"] is None
    assert comparison["projects"]["beta"]["normalized"]["value"] == 1.0


def test_comparison_rejects_an_unknown_metric() -> None:
    with pytest.raises(EffectivenessError, match="unknown comparison metric"):
        compare_projects((_outcome(),), metric="vibes")
    assert "acceptedOutcomeRate" in COMPARABLE_METRICS


def test_project_scoped_value_metrics_use_only_that_project_effort() -> None:
    report = build_effectiveness_report(
        (_outcome(), _outcome(project_id="beta", unit="github:example/beta:issue:9")),
        usage_records=(
            _usage("run-1", runtime=3600),
            _usage("run-2", project_id="beta", unit="github:example/beta:issue:9", runtime=1800),
        ),
        window=WINDOW,
    )

    assert report["byProject"]["alpha"]["value"]["perAgentHour"]["value"] == 1.0
    assert report["byProject"]["beta"]["value"]["perAgentHour"]["value"] == 2.0
    assert report["value"]["perAgentHour"]["value"] == pytest.approx(2 / 1.5)


# -- usage summary and credential-free telemetry ------------------------------


def test_usage_summary_reports_runs_without_guessing_an_outcome() -> None:
    summary = outcomes_from_usage(
        (
            _usage("run-1"),
            _usage("run-2", result=UsageResult.FAILED),
            _usage("run-3", project_id="beta", unit="github:example/beta:issue:9"),
        )
    )

    alpha = summary["alpha/github:example/alpha:issue:1"]
    assert alpha["runs"] == 2
    assert alpha["byResult"]["completed"] == 1
    assert alpha["byResult"]["failed"] == 1
    assert alpha["stages"] == ["implementation"]
    assert "beta/github:example/beta:issue:9" in summary
    # Nothing in the summary claims an outcome was delivered.
    assert "accepted" not in alpha
    assert "result" not in alpha


def test_effectiveness_dataclasses_have_no_field_that_can_hold_a_secret() -> None:
    assert secret_bearing_effectiveness_fields() == ()
    assert len(EFFECTIVENESS_SCHEMA_TYPES) == 5

    serialized = json.dumps(
        [
            _outcome(
                observations=(
                    OutcomeObservation(
                        observation_id="obs-1",
                        kind=ObservationKind.TESTS_HEALTHY,
                        observed_at=T2,
                        observer="ci",
                    ),
                )
            ).as_dict(),
            _learning().as_dict(),
        ]
    ).lower()
    for forbidden in ("credential", "secret", "password", "authorization", "bearer"):
        assert forbidden not in serialized


def test_free_text_is_bounded_so_a_transcript_cannot_be_smuggled_in() -> None:
    with pytest.raises(EffectivenessError, match="exceeds"):
        _learning(statement="x" * (MAX_TEXT_LENGTH + 1))
    with pytest.raises(EffectivenessError, match="exceeds"):
        AcceptanceCriterion(text="x" * (MAX_TEXT_LENGTH + 1))
    with pytest.raises(EffectivenessError, match="exceeds"):
        OutcomeObservation(
            observation_id="obs-1",
            kind=ObservationKind.TESTS_HEALTHY,
            observed_at=T2,
            observer="ci",
            note="x" * (MAX_TEXT_LENGTH + 1),
        )


def test_the_report_states_its_limitations() -> None:
    report = build_effectiveness_report((_outcome(),))

    assert report["limitations"]
    assert any("accepted outcomes" in line for line in report["limitations"])
    assert any("never presented as dollars billed" in line for line in report["limitations"])
    assert any("counted as unobserved" in line for line in report["limitations"])
    assert MonetaryStatus.SUBSCRIPTION.value == "subscription"
