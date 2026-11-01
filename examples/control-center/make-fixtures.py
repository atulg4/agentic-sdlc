"""Rebuild this directory's example documents with the real producers.

Run from the repository root:

    python examples/control-center/make-fixtures.py

Nothing here is hand-written JSON. The snapshot is built by
``build_control_center_snapshot`` and carries its own digest; the telemetry
bundle's sections are ``UsageLedger.aggregate`` and ``build_capacity_report``
output verbatim. A fixture therefore cannot drift from the documents the CLI
actually emits, which is what makes the console's browser suite worth
trusting: it renders real producer output, not a convenient shape.

``project-registry.json``, ``backlog.json`` and ``incidents.json`` are inputs
and are left alone.
"""

import json
from pathlib import Path

from agentic_sdlc.capacity_metrics import ObservationWindow, build_capacity_report
from agentic_sdlc.control_center import (
    build_control_center_snapshot,
    load_backlog_document,
    load_incident_document,
)
from agentic_sdlc.event_ledger import (
    STAGE_FOR_STATE,
    EventActor,
    EventLedger,
    EventProvenance,
    FactoryState,
    LifecycleEvent,
    LifecycleStage,
    WorkUnitRef,
)
from agentic_sdlc.project_registry import load_project_registry
from agentic_sdlc.usage_ledger import (
    BillingMode,
    PricingSnapshot,
    TokenCounts,
    UsageActual,
    UsageEstimate,
    UsageLedger,
    UsageRecord,
    UsageResult,
    monetary_equivalent,
    usage_id_for,
)

OUT = Path("examples/control-center")
REGISTRY = load_project_registry(json.loads((OUT / "project-registry.json").read_text()))
PROJECTS = [project.project_id for project in REGISTRY.projects]

# A unit per interesting disposition: in flight, awaiting a person, blocked,
# repaired more than once, and live.
#: One unit per interesting disposition - in flight, awaiting a person,
#: blocked, repaired more than once, and live - written as the state path the
#: factory would actually walk. ``EventLedger`` validates every transition, so
#: a path that could not happen fails this script rather than producing a
#: fixture the console would have to pretend about.
PLAN = {
    "wu-101": "intake triaged specified planned approved dispatched implementing",
    "wu-102": "intake triaged specified planned approved dispatched implementing"
    " verifying reviewing",
    "wu-103": "intake triaged specified planned approved dispatched implementing"
    " verifying repair-needed repairing re-reviewing ready-for-human-merge",
    "wu-104": "intake triaged blocked",
    "wu-105": "intake triaged specified planned approved dispatched implementing"
    " verifying reviewing ready-for-human-merge merged deploying"
    " deployment-verifying live",
}


MODELS = ["claude-opus-5", "gpt-5-codex", "claude-sonnet-5-5"]
WORKERS = ["gha-runner-3", "gha-runner-7", "codex-cloud-1"]


def instant(day: int, hour: int, minute: int = 0) -> str:
    return f"2026-10-{day:02d}T{hour:02d}:{minute:02d}:00Z"


def build() -> None:
    ledger = EventLedger(registry=REGISTRY)
    usage = UsageLedger(registry=REGISTRY)
    sequence = 0
    for project_index, project_id in enumerate(PROJECTS):
        for unit_index, (unit_id, path) in enumerate(PLAN.items()):
            if (project_index + unit_index) % 3 == 2:
                continue  # not every project runs every unit
            model = MODELS[(project_index + unit_index) % len(MODELS)]
            worker = WORKERS[(project_index + unit_index) % len(WORKERS)]
            ref = WorkUnitRef(
                project_id=project_id,
                work_unit_id=unit_id,
                issue_ref=f"acme/{project_id}#{100 + unit_index}",
                change_request_ref=f"acme/{project_id}!{200 + unit_index}",
                run_ref=f"run-{project_index}{unit_index}",
            )
            for step, name in enumerate(path.split()):
                factory_state = FactoryState(name)
                sequence += 1
                day = 26 + min(step // 4, 3)
                ledger.append(
                    LifecycleEvent(
                        event_id=f"e-{project_id}-{unit_id}-{step}",
                        idempotency_key=f"k-{project_id}-{unit_id}-{step}",
                        work_unit=ref,
                        stage=STAGE_FOR_STATE[factory_state],
                        state=factory_state,
                        occurred_at=instant(day, 6 + (step % 12), (step * 7) % 60),
                        actor=EventActor(
                            name="forge-agent",
                            kind="agent",
                            agent_id="forge-implementer",
                            provider="anthropic" if "claude" in model else "openai",
                            model=model,
                            worker=worker,
                        ),
                        provenance=EventProvenance(source="example-fixture"),
                        activity=f"{factory_state.value} on {unit_id}",
                        reason=(
                            "waiting on an upstream dependency"
                            if factory_state is FactoryState.BLOCKED
                            else ""
                        ),
                        depends_on=(("wu-101",) if unit_id == "wu-104" else ()),
                    )
                )
            for run in range(1 + unit_index % 3):
                run_id = f"{unit_id}-run-{run}"
                tokens = TokenCounts(
                    input=12_000 + run * 3_000 + unit_index * 500,
                    output=3_400 + run * 900,
                    cache_read=4_000,
                    cache_write=1_200,
                )
                pricing = PricingSnapshot(
                    pricing_id=f"{model}-payg-2026-10",
                    provider="anthropic" if "claude" in model else "openai",
                    model=model,
                    billing_mode=BillingMode.PAYG,
                    version="2026-10",
                    source="example price card",
                    effective_at="2026-10-01T00:00:00Z",
                    input_usd_per_million=3.0,
                    output_usd_per_million=15.0,
                    cache_read_usd_per_million=0.3,
                    cache_write_usd_per_million=3.75,
                )
                started = instant(26 + run, 8 + unit_index, 0)
                finished = instant(26 + run, 8 + unit_index, 40)
                usage.append(
                    UsageRecord(
                        usage_id=usage_id_for(project_id, unit_id, run_id),
                        work_unit=ref,
                        stage=LifecycleStage.IMPLEMENTATION,
                        run_id=run_id,
                        recorded_at=finished,
                        task_class="implementation",
                        actor=EventActor(
                            name="forge-agent",
                            kind="agent",
                            agent_id="forge-implementer",
                            provider="anthropic" if "claude" in model else "openai",
                            model=model,
                            worker=worker,
                        ),
                        estimate=UsageEstimate(
                            tokens=TokenCounts(input=11_000, output=3_000),
                            runtime_seconds=1_800,
                            monetary=monetary_equivalent(
                                TokenCounts(input=11_000, output=3_000), pricing
                            ),
                            basis="example baseline",
                        ),
                        actual=UsageActual(
                            tokens=tokens,
                            runtime_seconds=2_400,
                            monetary=monetary_equivalent(tokens, pricing),
                            result=UsageResult.COMPLETED,
                            source="provider-usage-api",
                            started_at=started,
                            finished_at=finished,
                        ),
                    )
                )

    backlog = load_backlog_document(json.loads((OUT / "backlog.json").read_text()))
    incidents = load_incident_document(json.loads((OUT / "incidents.json").read_text()))
    snapshot = build_control_center_snapshot(
        generated_at="2026-10-29T09:00:00Z",
        registry=REGISTRY,
        projections=ledger.projections(),
        backlog=backlog,
        incidents=incidents,
        sources=[
            "registry:project-registry.json",
            "ledger:event-ledger.json",
            "backlog:backlog.json",
            "incidents:incidents.json",
        ],
    )
    (OUT / "snapshot.json").write_text(json.dumps(snapshot.as_document(), indent=1) + "\n")

    window = ObservationWindow("2026-10-26T00:00:00Z", "2026-10-30T00:00:00Z")
    bundle = {
        "schemaVersion": 1,
        "generatedAt": "2026-10-29T09:00:00Z",
        "byModel": usage.aggregate(group_by=("model",)),
        "byDay": usage.aggregate(group_by=("day",)),
        "byWorkUnit": usage.aggregate(group_by=("project", "workUnit")),
        "capacity": build_capacity_report(usage.records, window=window),
    }
    (OUT / "telemetry.json").write_text(json.dumps(bundle, indent=1) + "\n")
    (OUT / "event-ledger.json").write_text(json.dumps(ledger.as_dict(), indent=1) + "\n")
    print("totals:", snapshot.as_document()["totals"])
    print("models:", [bucket["key"] for bucket in bundle["byModel"]["buckets"]])
    print("days:", [bucket["key"] for bucket in bundle["byDay"]["buckets"]])


if __name__ == "__main__":
    build()
