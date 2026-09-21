from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from agentic_sdlc.event_ledger import EventActor, LifecycleStage, WorkUnitRef
from agentic_sdlc.project_registry import load_project_registry
from agentic_sdlc.usage_ledger import (
    ESTIMATE_DIMENSIONS,
    USAGE_SCHEMA_TYPES,
    BillingMode,
    EstimatorCalibration,
    InfrastructureUsage,
    MonetaryStatus,
    PricingSnapshot,
    TokenCounts,
    UsageActual,
    UsageError,
    UsageEstimate,
    UsageLedger,
    UsageRecord,
    UsageResult,
    load_pricing_document,
    load_pricing_snapshot,
    load_usage_record,
    monetary_equivalent,
    secret_bearing_usage_fields,
    usage_id_for,
)

T0 = "2026-09-21T10:00:00Z"

PAYG = load_pricing_snapshot(
    {
        "pricingId": "anthropic-payg-2026-09",
        "provider": "anthropic",
        "model": "claude-opus-5",
        "billingMode": "payg",
        "version": "2026-09-01",
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
        "version": "2026-09",
        "usdPerMillion": {"input": 15, "output": 75},
        "plan": {
            "name": "Claude Max 20x",
            "capacityUnit": "five-hour-window-share",
            "monthlyUsd": 200,
        },
    }
)
CODEX_PLAN = load_pricing_snapshot(
    {
        "pricingId": "codex-plan",
        "provider": "codex",
        "model": "gpt-5-codex",
        "billingMode": "subscription",
        "version": "2026-09",
        "plan": {
            "name": "ChatGPT Pro",
            "capacityUnit": "requests",
            "capacityUnitsPerMillionTokens": 10,
        },
    }
)

SMALL = TokenCounts(input=1000, output=500, cache_read=0, cache_write=0)
LARGE = TokenCounts(input=2000, output=1000, cache_read=0, cache_write=0)


def _estimate(
    tokens: TokenCounts = SMALL, runtime: int | None = 600, pricing=PAYG
) -> UsageEstimate:
    return UsageEstimate(
        tokens=tokens,
        runtime_seconds=runtime,
        monetary=monetary_equivalent(tokens, pricing),
        basis="test baseline",
    )


def _actual(
    tokens: TokenCounts = LARGE,
    *,
    runtime: int | None = 900,
    result: UsageResult = UsageResult.COMPLETED,
    pricing=PAYG,
    plan_capacity_units: float | None = None,
    **timing: str,
) -> UsageActual:
    return UsageActual(
        tokens=tokens,
        runtime_seconds=runtime,
        monetary=monetary_equivalent(tokens, pricing, plan_capacity_units=plan_capacity_units),
        result=result,
        source="provider-usage-api",
        **timing,
    )


def _record(
    run_id: str = "run-1",
    *,
    attempt: int = 1,
    project_id: str = "alpha",
    work_unit_id: str = "github:example/alpha:issue:1",
    stage: LifecycleStage = LifecycleStage.IMPLEMENTATION,
    model: str = "claude-opus-5",
    provider: str = "anthropic",
    task_class: str = "implementation",
    complexity: str = "medium",
    estimate: UsageEstimate | None = None,
    actual: UsageActual | None = None,
    infrastructure: InfrastructureUsage | None = None,
    recorded_at: str = T0,
    **extra: Any,
) -> UsageRecord:
    return UsageRecord(
        usage_id=usage_id_for(project_id, work_unit_id, run_id, attempt),
        work_unit=WorkUnitRef(
            project_id=project_id,
            work_unit_id=work_unit_id,
            repository="example/alpha",
            issue_ref="example/alpha#1",
            change_request_ref="example/alpha!12",
        ),
        stage=stage,
        run_id=run_id,
        attempt=attempt,
        task_class=task_class,
        mission_id="implementation-worker",
        mission_version="1.0.0",
        actor=EventActor(
            name="claude",
            kind="agent",
            agent_id="claude-code",
            provider=provider,
            model=model,
            model_alias="claude",
            worker="gha-7",
        ),
        complexity_class=complexity,
        recorded_at=recorded_at,
        estimate=estimate,
        actual=actual,
        infrastructure=infrastructure,
        **extra,
    )


# -- estimate / actual / unknown semantics ------------------------------------


def test_unknown_token_counts_are_explicit_not_fabricated() -> None:
    assert TokenCounts().status == "unknown"
    assert TokenCounts().total is None
    partial = TokenCounts(input=10, output=5)
    assert partial.status == "partial"
    assert partial.total is None
    assert SMALL.status == "known"
    assert SMALL.total == 1500

    document = partial.as_dict()
    assert document == {
        "input": 10,
        "output": 5,
        "cacheRead": None,
        "cacheWrite": None,
        "total": None,
        "status": "partial",
    }


def test_record_with_only_an_estimate_then_an_actual_round_trips() -> None:
    record = _record(estimate=_estimate())
    document = record.as_dict()
    assert document["actual"] is None
    assert document["estimate"]["monetary"]["status"] == "payg"
    assert load_usage_record(document) == record

    filled = replace(record, actual=_actual(started_at=T0, finished_at="2026-09-21T10:15:00Z"))
    reloaded = load_usage_record(filled.as_dict())
    assert reloaded == filled
    assert reloaded.actual is not None
    assert reloaded.actual.runtime_seconds == 900
    assert reloaded.as_dict() == filled.as_dict()


def test_actual_derives_runtime_and_wait_from_timestamps_and_rejects_backwards_time() -> None:
    actual = UsageActual(
        queued_at="2026-09-21T09:58:00Z",
        started_at=T0,
        finished_at="2026-09-21T10:11:30Z",
    )
    assert actual.runtime_seconds == 690
    assert actual.wait_seconds == 120

    explicit = UsageActual(runtime_seconds=5, started_at=T0, finished_at="2026-09-21T10:11:30Z")
    assert explicit.runtime_seconds == 5, "an observed runtime is never overwritten"

    with pytest.raises(UsageError, match="timestamps run backwards"):
        UsageActual(started_at="2026-09-21T10:11:30Z", finished_at=T0)
    with pytest.raises(UsageError, match="RFC 3339"):
        UsageActual(started_at="yesterday")


def test_estimate_error_is_computed_only_where_both_sides_are_known() -> None:
    estimate = _estimate(TokenCounts(input=1000, output=500, cache_read=0, cache_write=None), 600)
    actual = _actual(
        TokenCounts(input=1200, output=400, cache_read=0, cache_write=10), runtime=None
    )
    error = _record(estimate=estimate, actual=actual).estimate_error

    assert error.status == "available"
    assert error.dimensions["input"]["absolute"] == 200
    assert error.dimensions["input"]["percentage"] == 20.0
    assert error.dimensions["output"]["percentage"] == -20.0
    assert error.dimensions["cacheRead"]["percentage"] is None
    assert error.dimensions["cacheRead"]["percentageBasis"] == "estimate was zero"
    assert "cacheWrite" not in error.dimensions
    assert "runtimeSeconds" not in error.dimensions
    assert "totalTokens" not in error.dimensions, "a partial estimate has no total"
    assert "paygEquivalentUsd" not in error.dimensions, "unknown cost is not compared"

    assert _record(estimate=_estimate()).estimate_error.status == "unavailable"
    assert _record(actual=_actual()).estimate_error.reason == "no estimate recorded"
    empty = _record(estimate=UsageEstimate(), actual=UsageActual()).estimate_error
    assert empty.status == "unavailable"


def test_estimate_error_covers_total_tokens_and_cost_when_fully_known() -> None:
    error = _record(estimate=_estimate(), actual=_actual()).estimate_error
    assert error.dimensions["totalTokens"]["percentage"] == 100.0
    assert error.dimensions["paygEquivalentUsd"]["estimated"] == 0.0525
    assert error.dimensions["paygEquivalentUsd"]["actual"] == 0.105
    assert error.dimensions["runtimeSeconds"]["percentage"] == 50.0


# -- subscription versus pay-as-you-go ----------------------------------------


def test_payg_pricing_bills_only_when_every_priced_component_is_known() -> None:
    billed = monetary_equivalent(SMALL, PAYG)
    assert billed.status is MonetaryStatus.PAYG
    assert billed.billed_usd == 0.0525
    assert billed.payg_equivalent_usd == 0.0525
    assert billed.plan_capacity_units is None

    partial = monetary_equivalent(TokenCounts(input=1000, output=500), PAYG)
    assert partial.status is MonetaryStatus.UNKNOWN
    assert partial.billed_usd is None
    assert "cacheRead, cacheWrite" in partial.basis

    assert monetary_equivalent(TokenCounts(), None).status is MonetaryStatus.UNKNOWN
    with pytest.raises(UsageError, match="no plan capacity unit"):
        monetary_equivalent(SMALL, PAYG, plan_capacity_units=1)


def test_subscription_usage_is_labeled_and_never_billed() -> None:
    observed = monetary_equivalent(SMALL, CLAUDE_MAX, plan_capacity_units=0.04)
    assert observed.status is MonetaryStatus.SUBSCRIPTION
    assert observed.billed_usd is None
    assert observed.payg_equivalent_usd == 0.0525, "reference equivalent, from input+output only"
    assert observed.plan_capacity_units == 0.04
    assert observed.plan_capacity_unit == "five-hour-window-share"
    assert "not billed" in observed.basis and "observed" in observed.basis

    unknown_capacity = monetary_equivalent(TokenCounts(input=1000, output=500), CLAUDE_MAX)
    assert unknown_capacity.status is MonetaryStatus.SUBSCRIPTION
    assert unknown_capacity.payg_equivalent_usd == 0.0525
    assert unknown_capacity.plan_capacity_units is None
    assert "plan capacity units unknown" in unknown_capacity.basis

    estimated_capacity = monetary_equivalent(SMALL, CODEX_PLAN)
    assert estimated_capacity.payg_equivalent_usd is None
    assert "no reference rates" in estimated_capacity.basis
    assert estimated_capacity.plan_capacity_units == 0.015
    assert "estimated from tokens" in estimated_capacity.basis
    assert monetary_equivalent(TokenCounts(input=1), CODEX_PLAN).plan_capacity_units is None


def test_subscription_records_cannot_be_rewritten_as_billed() -> None:
    document = monetary_equivalent(SMALL, CLAUDE_MAX).as_dict()
    document["billedUsd"] = 0.05
    with pytest.raises(UsageError, match="never billed per run"):
        load_usage_record(
            _record(estimate=_estimate()).as_dict()
            | {"estimate": _estimate().as_dict() | {"monetary": document}}
        )

    document = monetary_equivalent(SMALL, PAYG).as_dict()
    document["status"] = "unknown"
    with pytest.raises(UsageError, match="status unknown cannot carry monetary values"):
        load_usage_record(
            _record(estimate=_estimate()).as_dict()
            | {"estimate": _estimate().as_dict() | {"monetary": document}}
        )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda doc: doc.update(billingMode="prepaid"), "unknown billingMode"),
        (lambda doc: doc.update(usdPerMillion={}), "requires input and output"),
        (lambda doc: doc.update(plan={"name": "x", "capacityUnit": "u"}), "cannot declare a plan"),
        (lambda doc: doc.update(version=""), "version is required"),
        (lambda doc: doc.update(apiKey="x"), "credentials, secrets or prompts"),
        (lambda doc: doc["usdPerMillion"].update(input=-1), "finite number >= 0"),
    ],
)
def test_pricing_loader_fails_closed(mutate: Any, message: str) -> None:
    document = PAYG.as_dict()
    mutate(document)
    with pytest.raises(UsageError, match=message):
        load_pricing_snapshot(document)


def test_subscription_pricing_requires_a_named_plan_and_documents_reject_duplicates() -> None:
    document = CLAUDE_MAX.as_dict()
    document["plan"]["capacityUnit"] = ""
    with pytest.raises(UsageError, match="requires plan.name and plan.capacityUnit"):
        load_pricing_snapshot(document)

    pricing = load_pricing_document(
        {"schemaVersion": 1, "pricing": [PAYG.as_dict(), CLAUDE_MAX.as_dict()]}
    )
    assert set(pricing) == {PAYG.pricing_id, CLAUDE_MAX.pricing_id}
    assert pricing[CLAUDE_MAX.pricing_id].billing_mode is BillingMode.SUBSCRIPTION
    with pytest.raises(UsageError, match="duplicate pricingId"):
        load_pricing_document({"schemaVersion": 1, "pricing": [PAYG.as_dict(), PAYG.as_dict()]})
    with pytest.raises(UsageError, match="unsupported pricing document schemaVersion"):
        load_pricing_document({"schemaVersion": 2, "pricing": []})


# -- ledger: identity, immutability, idempotency, isolation ---------------------


def test_usage_id_is_stable_per_run_attempt() -> None:
    first = usage_id_for("alpha", "unit", "run-1")
    assert first == usage_id_for("alpha ", "unit", "run-1", 1)
    assert first != usage_id_for("alpha", "unit", "run-1", 2)
    assert first != usage_id_for("beta", "unit", "run-1")
    with pytest.raises(UsageError, match="attempt numbers start at 1"):
        usage_id_for("alpha", "unit", "run-1", 0)


def test_ledger_replay_is_a_noop_filling_is_allowed_and_rewriting_fails() -> None:
    ledger = UsageLedger()
    estimated = _record(estimate=_estimate())
    assert ledger.append(estimated) == estimated
    assert ledger.append(estimated) == estimated
    assert len(ledger) == 1

    later = replace(estimated, estimate=None, actual=_actual())
    merged = ledger.append(later)
    assert merged.estimate == estimated.estimate
    assert merged.actual == later.actual
    assert len(ledger) == 1

    # Replaying either half of the record after the merge changes nothing.
    assert ledger.append(estimated) == merged
    assert ledger.append(later) == merged

    with pytest.raises(UsageError, match="estimate is immutable"):
        ledger.append(replace(estimated, estimate=_estimate(LARGE)))
    with pytest.raises(UsageError, match="actual is immutable"):
        ledger.append(replace(later, actual=_actual(SMALL)))
    with pytest.raises(UsageError, match="identity and references are immutable"):
        ledger.append(replace(estimated, stage=LifecycleStage.REPAIR))

    infra = replace(estimated, estimate=None, infrastructure=InfrastructureUsage(cost_usd=0.4))
    assert ledger.append(infra).infrastructure == infra.infrastructure
    with pytest.raises(UsageError, match="infrastructure is immutable"):
        ledger.append(replace(infra, infrastructure=InfrastructureUsage(cost_usd=0.5)))


def test_ledger_round_trips_and_fails_closed_on_unknown_versions() -> None:
    ledger = UsageLedger()
    ledger.append(_record(estimate=_estimate(), actual=_actual()))
    ledger.append(_record("run-2", project_id="beta", estimate=_estimate(pricing=CLAUDE_MAX)))
    document = ledger.as_dict()

    reloaded = UsageLedger.from_dict(document)
    assert reloaded.as_dict() == document
    assert reloaded.get(ledger.records[0].usage_id) == ledger.records[0]

    document["schemaVersion"] = 99
    with pytest.raises(UsageError, match="unsupported usage ledger schemaVersion"):
        UsageLedger.from_dict(document)
    with pytest.raises(UsageError, match="unknown usage record"):
        ledger.get("usage-missing")


def test_ledger_scopes_projects_and_honours_the_registry() -> None:
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
    ledger = UsageLedger(registry=registry)
    ledger.append(_record(estimate=_estimate()))
    with pytest.raises(UsageError, match="unregistered project: beta"):
        ledger.append(_record(project_id="beta"))

    open_ledger = UsageLedger()
    open_ledger.append(_record(estimate=_estimate()))
    open_ledger.append(_record(project_id="beta", work_unit_id="shared-id"))
    open_ledger.append(_record(work_unit_id="shared-id"))
    assert len(open_ledger.query(project_id="alpha")) == 2
    assert len(open_ledger.query(project_id="beta", work_unit_id="shared-id")) == 1
    assert len(open_ledger.query(stage="implementation", model="claude-opus-5")) == 3
    assert open_ledger.query(since="2026-09-22T00:00:00Z") == ()
    with pytest.raises(UsageError, match="pass project_id or project_ids"):
        open_ledger.query(project_id="alpha", project_ids=["beta"])


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda doc: doc.update(surprise=1), "unknown keys"),
        (lambda doc: doc.update(schemaVersion=2), "unsupported usage record schemaVersion"),
        (lambda doc: doc.update(stage="teleport"), "unknown stage"),
        (lambda doc: doc.update(attempt=0), "attempt must be an integer >= 1"),
        (lambda doc: doc.update(recordedAt="today"), "RFC 3339"),
        (lambda doc: doc.update(runId=""), "runId is required"),
        (lambda doc: doc["actor"].update(kind="robot"), "unknown actor kind"),
        (lambda doc: doc["workUnit"].update(projectId=""), "projectId is required"),
        (lambda doc: doc["estimate"]["tokens"].update(input=-5), "integer >= 0 or null"),
        (lambda doc: doc["estimate"]["tokens"].update(input=True), "integer >= 0 or null"),
        (lambda doc: doc["actual"].update(result="exploded"), "unknown result"),
        (lambda doc: doc["actual"].update(finishedAt="2026-09-21T09:00:00Z"), "run backwards"),
        (
            lambda doc: doc["estimate"]["calibration"].append({"dimension": "x"}),
            "unknown dimension",
        ),
    ],
)
def test_record_loader_fails_closed(mutate: Any, message: str) -> None:
    document = _record(estimate=_estimate(), actual=_actual(started_at=T0)).as_dict()
    mutate(document)
    with pytest.raises(UsageError, match=message):
        load_usage_record(document)


# -- no secrets, no prompts ------------------------------------------------------


def test_usage_schema_has_no_field_that_can_hold_a_secret_or_prompt() -> None:
    assert secret_bearing_usage_fields() == ()
    assert len(USAGE_SCHEMA_TYPES) == 8

    record = _record(
        estimate=_estimate(pricing=CLAUDE_MAX),
        actual=_actual(pricing=CLAUDE_MAX, plan_capacity_units=0.1),
        infrastructure=InfrastructureUsage(runner_class="ubuntu-4core", runner_seconds=900),
    )
    serialized = json.dumps(record.as_dict()).lower()
    for forbidden in ("credential", "secret", 'token"', "password", "authorization", "prompt"):
        assert forbidden not in serialized


@pytest.mark.parametrize(
    "key",
    ["apiToken", "oauthSecret", "bearer", "prompt", "systemPrompt", "messages", "privateKey"],
)
def test_loader_rejects_secret_and_prompt_shaped_keys(key: str) -> None:
    document = _record(estimate=_estimate()).as_dict()
    document[key] = "x"
    with pytest.raises(UsageError, match="credentials, secrets or prompts"):
        load_usage_record(document)

    nested = _record(estimate=_estimate()).as_dict()
    nested["estimate"][key] = "x"
    with pytest.raises(UsageError, match="credentials, secrets or prompts"):
        load_usage_record(nested)


def test_free_text_is_bounded_so_prompts_cannot_be_smuggled() -> None:
    document = _record(estimate=_estimate()).as_dict()
    document["estimate"]["basis"] = "x" * 257
    with pytest.raises(UsageError, match="never prompts or transcripts"):
        load_usage_record(document)


# -- aggregation -----------------------------------------------------------------


def _mixed_ledger() -> UsageLedger:
    ledger = UsageLedger()
    ledger.append(
        _record(
            "run-1",
            estimate=_estimate(),
            actual=_actual(started_at=T0, finished_at="2026-09-21T10:15:00Z"),
            infrastructure=InfrastructureUsage(runner_seconds=900, ci_minutes=15, cost_usd=0.12),
        )
    )
    ledger.append(
        _record(
            "run-2",
            stage=LifecycleStage.INDEPENDENT_REVIEW,
            task_class="review",
            model="gpt-5-codex",
            provider="codex",
            estimate=_estimate(pricing=CODEX_PLAN),
            actual=_actual(
                TokenCounts(),
                runtime=300,
                pricing=CODEX_PLAN,
                plan_capacity_units=3,
                started_at="2026-09-22T08:00:00Z",
                finished_at="2026-09-22T08:05:00Z",
            ),
            review_round=1,
        )
    )
    ledger.append(
        _record(
            "run-3",
            project_id="beta",
            work_unit_id="github:example/beta:issue:9",
            estimate=_estimate(pricing=CLAUDE_MAX),
            actual=_actual(
                pricing=CLAUDE_MAX,
                plan_capacity_units=0.1,
                started_at="2026-10-01T09:00:00Z",
                finished_at="2026-10-01T09:20:00Z",
            ),
        )
    )
    ledger.append(_record("run-4", project_id="beta", work_unit_id="github:example/beta:issue:9"))
    return ledger


def test_aggregation_by_project_keeps_unknowns_and_separates_cost_kinds() -> None:
    report = _mixed_ledger().aggregate(group_by=("project",))
    assert report["groupBy"] == ["projectId"]
    alpha, beta = report["buckets"]
    assert alpha["key"] == {"projectId": "alpha"}
    assert alpha["records"] == 2 and alpha["runs"] == 2

    # PAYG dollars are billed; the subscription run adds capacity units, not dollars.
    assert alpha["actual"]["billedUsd"] == {"value": 0.105, "knownRecords": 1, "unknownRecords": 1}
    assert alpha["actual"]["paygEquivalentUsd"]["value"] == 0.105
    assert alpha["actual"]["planCapacityUnits"] == {
        "requests": {"value": 3, "knownRecords": 1, "unknownRecords": 0}
    }
    assert alpha["actual"]["monetaryStatus"] == {"payg": 1, "subscription": 1}
    assert alpha["actual"]["tokens"]["input"] == {
        "value": 2000,
        "knownRecords": 1,
        "unknownRecords": 1,
    }
    assert alpha["actual"]["totalTokens"]["value"] == 3000
    assert alpha["actual"]["runtimeSeconds"] == {
        "value": 1200,
        "knownRecords": 2,
        "unknownRecords": 0,
    }
    assert alpha["estimated"]["totalTokens"] == {
        "value": 3000,
        "knownRecords": 2,
        "unknownRecords": 0,
    }
    assert alpha["infrastructure"]["costUsd"] == {
        "value": 0.12,
        "knownRecords": 1,
        "unknownRecords": 1,
    }
    assert alpha["infrastructure"]["ciMinutes"]["value"] == 15
    assert "costUsd" not in alpha["actual"], "AI cost equivalent never absorbs infrastructure cost"
    assert alpha["estimateError"]["meanAbsolutePercentage"]["input"] == {
        "value": 100.0,
        "samples": 1,
    }

    assert beta["records"] == 2
    assert beta["byResult"] == {"completed": 1, "unknown": 1}
    assert beta["actual"]["billedUsd"] == {"value": None, "knownRecords": 0, "unknownRecords": 2}
    assert beta["actual"]["paygEquivalentUsd"]["value"] == 0.105
    assert beta["actual"]["planCapacityUnits"]["five-hour-window-share"]["value"] == 0.1
    assert report["totals"]["records"] == 4
    assert report["totals"]["actual"]["billedUsd"]["value"] == 0.105
    assert any("never combined" in item for item in report["limitations"])


def test_aggregation_by_change_request_stage_model_and_calendar() -> None:
    ledger = _mixed_ledger()
    by_pr = ledger.aggregate(group_by=("changeRequest", "stage"), project_id="alpha")
    assert [bucket["key"] for bucket in by_pr["buckets"]] == [
        {"changeRequestRef": "example/alpha!12", "stage": "implementation"},
        {"changeRequestRef": "example/alpha!12", "stage": "independent-review"},
    ]
    review = by_pr["buckets"][1]
    assert review["actual"]["tokens"]["output"] == {
        "value": None,
        "knownRecords": 0,
        "unknownRecords": 1,
    }

    by_model = ledger.aggregate(group_by=("model", "billingMode"))
    assert [bucket["key"] for bucket in by_model["buckets"]] == [
        {"model": "claude-opus-5", "billingMode": ""},
        {"model": "claude-opus-5", "billingMode": "payg"},
        {"model": "claude-opus-5", "billingMode": "subscription"},
        {"model": "gpt-5-codex", "billingMode": "subscription"},
    ]

    calendar = ledger.aggregate(group_by=("month", "week", "day"))
    assert [bucket["key"] for bucket in calendar["buckets"]] == [
        {"month": "2026-09", "week": "2026-W39", "day": "2026-09-21"},
        {"month": "2026-09", "week": "2026-W39", "day": "2026-09-22"},
        {"month": "2026-10", "week": "2026-W40", "day": "2026-10-01"},
    ]
    assert calendar["buckets"][0]["records"] == 2, "an unfinished run dates from its record time"

    by_round = ledger.aggregate(group_by=("reviewRound",), since="2026-09-22T00:00:00Z")
    assert [bucket["key"] for bucket in by_round["buckets"]] == [
        {"reviewRound": ""},
        {"reviewRound": "1"},
    ]
    with pytest.raises(UsageError, match="unknown group dimension"):
        ledger.aggregate(group_by=("colour",))
    with pytest.raises(UsageError, match="at least one dimension"):
        ledger.aggregate(group_by=())


def test_time_accounting_across_retries_and_cancelled_runs() -> None:
    ledger = UsageLedger()
    ledger.append(
        _record(
            "run-7",
            attempt=1,
            estimate=_estimate(),
            actual=_actual(
                TokenCounts(),
                runtime=None,
                result=UsageResult.CANCELLED,
                queued_at="2026-09-21T09:55:00Z",
                started_at=T0,
                finished_at="2026-09-21T10:02:00Z",
            ),
        )
    )
    ledger.append(
        _record(
            "run-7",
            attempt=2,
            estimate=_estimate(),
            actual=_actual(
                queued_at="2026-09-21T10:02:00Z",
                started_at="2026-09-21T10:03:00Z",
                finished_at="2026-09-21T10:18:00Z",
            ),
        )
    )
    ledger.append(_record("run-8", estimate=_estimate(), actual=_actual(result=UsageResult.FAILED)))

    report = ledger.aggregate(group_by=("run",))
    retried, failed = report["buckets"]
    assert retried["key"] == {"runId": "run-7"}
    assert retried["records"] == 2 and retried["runs"] == 1 and retried["retryAttempts"] == 1
    assert retried["byResult"] == {"cancelled": 1, "completed": 1}
    assert retried["actual"]["runtimeSeconds"]["value"] == 120 + 900
    assert retried["actual"]["waitSeconds"]["value"] == 300 + 60
    assert retried["actual"]["runtimeSecondsByResult"] == {
        "cancelled": {"value": 120, "knownRecords": 1, "unknownRecords": 0},
        "completed": {"value": 900, "knownRecords": 1, "unknownRecords": 0},
    }
    # The cancelled attempt spent time but exposed no tokens: it stays visible as unknown.
    assert retried["actual"]["tokens"]["input"] == {
        "value": 2000,
        "knownRecords": 1,
        "unknownRecords": 1,
    }
    assert failed["byResult"] == {"failed": 1}
    assert failed["actual"]["runtimeSecondsByResult"]["failed"]["value"] == 900
    assert report["totals"]["runs"] == 2 and report["totals"]["records"] == 3


# -- estimator calibration ------------------------------------------------------


def _observed(
    run_id: str,
    ratio: float,
    *,
    task_class: str = "implementation",
    result: UsageResult = UsageResult.COMPLETED,
) -> UsageRecord:
    tokens = TokenCounts(
        input=int(1000 * ratio), output=int(500 * ratio), cache_read=0, cache_write=0
    )
    return _record(
        run_id,
        task_class=task_class,
        estimate=_estimate(),
        actual=_actual(tokens, runtime=int(600 * ratio), result=result),
    )


def test_calibration_learns_from_actuals_and_falls_back_cold_start() -> None:
    calibration = EstimatorCalibration(min_samples=2)
    cold = calibration.estimate(
        baseline_tokens=SMALL,
        baseline_runtime_seconds=600,
        model="claude-opus-5",
        stage=LifecycleStage.IMPLEMENTATION,
        task_class="implementation",
        complexity="medium",
        pricing=PAYG,
    )
    assert cold.tokens == SMALL and cold.runtime_seconds == 600
    assert cold.basis.startswith("cold-start")
    assert {item.key for item in cold.calibration} == {"cold-start"}
    assert cold.monetary.billed_usd == 0.0525

    assert calibration.observe(_observed("run-1", 1.5)) is True
    assert calibration.observe(_observed("run-1", 1.5)) is False, "replay learns nothing"
    below_threshold = calibration.coefficient(
        "input",
        model="claude-opus-5",
        stage="implementation",
        task_class="implementation",
        complexity="medium",
    )
    assert below_threshold.key == "cold-start" and below_threshold.coefficient == 1.0

    assert calibration.observe(_observed("run-2", 2.5)) is True
    learned = calibration.estimate(
        baseline_tokens=SMALL,
        baseline_runtime_seconds=600,
        model="claude-opus-5",
        stage="implementation",
        task_class="implementation",
        complexity="medium",
        pricing=PAYG,
    )
    assert learned.tokens.input == 2000 and learned.tokens.output == 1000
    assert learned.runtime_seconds == 1200
    assert learned.basis.startswith("calibrated:")
    applied = {item.dimension: item for item in learned.calibration}
    assert applied["input"].samples == 2 and applied["input"].coefficient == 2.0
    assert applied["input"].key == (
        "model=claude-opus-5;stage=implementation;taskClass=implementation;complexity=medium"
    )
    # A zero baseline can never be calibrated, so cache dimensions stay cold.
    assert applied["cacheRead"].key == "cold-start"
    assert learned.monetary.payg_equivalent_usd == 0.105

    # An unseen task class falls back to the most specific well-sampled ancestor.
    fallback = calibration.coefficient(
        "output",
        model="claude-opus-5",
        stage="implementation",
        task_class="docs",
        complexity="",
    )
    assert fallback.key == "model=claude-opus-5;stage=implementation"
    assert fallback.samples == 2
    other_model = calibration.coefficient("output", model="gpt-5", stage="implementation")
    assert other_model.key == "stage=implementation"
    assert calibration.coefficient("output", model="gpt-5", stage="review").key == "*"
    assert calibration.coefficient("output", model="x", stage="review").coefficient == 2.0

    unknown_baseline = calibration.estimate(
        baseline_tokens=TokenCounts(),
        baseline_runtime_seconds=None,
        model="claude-opus-5",
        stage="implementation",
    )
    assert unknown_baseline.tokens.status == "unknown"
    assert unknown_baseline.monetary.status is MonetaryStatus.UNKNOWN
    assert unknown_baseline.basis.startswith("no baseline supplied")


def test_calibration_ignores_cancelled_incomplete_and_extreme_observations() -> None:
    calibration = EstimatorCalibration(min_samples=1)
    cancelled = _observed("run-c", 0.5, result=UsageResult.CANCELLED)
    assert calibration.observe(cancelled) is False
    assert calibration.observe(_record("run-e", estimate=_estimate())) is False
    assert calibration.observe(_record("run-a", actual=_actual())) is False
    assert calibration.observed_usage_ids == frozenset()

    assert calibration.observe(_observed("run-x", 500.0)) is True
    clamped = calibration.coefficient("input", model="claude-opus-5", stage="implementation")
    assert clamped.coefficient == 20.0
    assert calibration.observe(_observed("run-y", 0.0001)) is True
    assert calibration.coefficient(
        "input", model="claude-opus-5", stage="implementation"
    ).coefficient == round((20.0 + 0.05) / 2, 6)
    with pytest.raises(UsageError, match="unknown estimate dimension"):
        calibration.coefficient("mood", model="m", stage="s")
    with pytest.raises(UsageError, match="min_samples"):
        EstimatorCalibration(min_samples=0)


def test_calibration_round_trips_and_fails_closed() -> None:
    calibration = EstimatorCalibration(min_samples=2)
    calibration.observe_all([_observed("run-1", 1.2), _observed("run-2", 0.8)])
    document = calibration.as_dict()
    assert document["schemaVersion"] == 1
    assert set(document["coefficients"]["*"]) == set(ESTIMATE_DIMENSIONS) - {
        "cacheRead",
        "cacheWrite",
    }

    reloaded = EstimatorCalibration.from_dict(document)
    assert reloaded.as_dict() == document
    assert reloaded.observe(_observed("run-1", 1.2)) is False
    assert reloaded.coefficient("input", model="claude-opus-5", stage="implementation").samples == 2

    document["schemaVersion"] = 2
    with pytest.raises(UsageError, match="unsupported estimator calibration schemaVersion"):
        EstimatorCalibration.from_dict(document)
    broken = calibration.as_dict()
    broken["coefficients"]["*"]["input"] = {"samples": 0, "meanRatio": 1.0}
    with pytest.raises(UsageError, match="samples must be an integer >= 1"):
        EstimatorCalibration.from_dict(broken)
    broken = calibration.as_dict()
    broken["coefficients"]["*"]["mood"] = {"samples": 1, "meanRatio": 1.0}
    with pytest.raises(UsageError, match="unknown estimate dimension"):
        EstimatorCalibration.from_dict(broken)


def test_pricing_snapshot_is_embedded_in_every_monetary_figure() -> None:
    record = _record(estimate=_estimate(pricing=CLAUDE_MAX))
    document = record.as_dict()
    embedded = document["estimate"]["monetary"]["pricing"]
    assert embedded["pricingId"] == "claude-max-20x" and embedded["version"] == "2026-09"
    assert document["estimate"]["monetary"]["pricingId"] == "claude-max-20x"
    assert isinstance(load_usage_record(document).estimate.monetary.pricing, PricingSnapshot)

    document["estimate"]["monetary"]["pricingId"] = "someone-else"
    with pytest.raises(UsageError, match="does not match the embedded pricing snapshot"):
        load_usage_record(document)
