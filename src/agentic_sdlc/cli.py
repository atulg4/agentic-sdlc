"""Command-line interface for pipeline policy checks."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

from .artifact import ArtifactError, create_manifest, verify_manifest, write_manifest
from .capacity_metrics import (
    CapacityError,
    ObservationWindow,
    build_capacity_report,
    load_capacity_inputs,
)
from .dashboard_efficiency import build_infrastructure_blocker_panel
from .event_ledger import EventLedger, LedgerError, LifecycleStage, load_lifecycle_event
from .events import EventError, normalize_event
from .executors import (
    ExecutorError,
    RouteRequest,
    TaskClass,
    load_executors,
    load_routing_policy,
    route_executor,
)
from .gates import GateError, run_gates, write_report
from .git_diff import GitDiffError, collect_git_diff
from .infra_recovery import (
    FailureClass,
    InfraRecoveryError,
    RetryAction,
    classify_failure,
    decide_retry,
    load_retry_state,
    render_retry_comment,
    retry_state_from_comments,
    write_retry_state,
)
from .knowledge import KnowledgeError, load_sources
from .leases import (
    DEFAULT_TTL_MINUTES,
    ClaimResult,
    LeaseError,
    claim,
    list_claims,
    release,
    render_claims,
    renew,
)
from .missions import (
    MissionError,
    create_dispatch_envelope,
    load_agents,
    load_registry,
)
from .models import RiskLevel
from .onboard import (
    OnboardError,
    OnboardSpec,
    apply_repo_settings,
    canonical_project_id,
    copy_variables,
    doctor,
    registry_model_vars,
    repository_default_branch,
    repository_fork_exposed,
    resolve_platform_ref,
    reusable_calls,
    run_gh,
    write_onboarding,
)
from .orchestration import OrchestrationError, Orchestrator
from .policy import evaluate_diff, evaluate_task, load_policy, load_policy_bytes
from .project_registry import (
    ProjectRegistryError,
    read_optional_project_registry,
    read_project_registry,
)
from .scaffold import ScaffoldError, scaffold_project
from .spec_stage import merge_spec, parse_draft
from .task_spec import TaskSpecError, check_task_spec, draft_request, parse_task, render_prompt
from .usage_ledger import (
    EstimatorCalibration,
    TokenCounts,
    UsageError,
    UsageLedger,
    load_pricing_document,
    load_usage_record,
)


def _budget_usd(value: str) -> float:
    """Parse a budget, rejecting NaN and infinity which disable spend limits."""
    try:
        parsed = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"budget must be a number: {value}") from error
    if not math.isfinite(parsed):
        raise argparse.ArgumentTypeError(f"budget must be a finite number: {value}")
    if parsed < 0:
        raise argparse.ArgumentTypeError(f"budget cannot be negative: {value}")
    return parsed


def _write(data: Any, output: str | None) -> None:
    rendered = json.dumps(data, indent=2, sort_keys=True) + "\n"
    if output:
        Path(output).write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)


def _validate_task(args: argparse.Namespace) -> int:
    body = Path(args.task).read_text(encoding="utf-8")
    task = parse_task(args.title, body, tuple(args.label))
    decision = evaluate_task(task, load_policy(args.config), args.mode)
    _write(decision.as_dict(), args.output)
    return 0 if decision.allowed else 2


def _evaluate_diff(args: argparse.Namespace) -> int:
    paths = tuple(Path(args.paths_file).read_text(encoding="utf-8").splitlines())
    decision = evaluate_diff(
        paths,
        args.added,
        args.deleted,
        load_policy(args.config),
        patch_bytes=args.patch_bytes,
    )
    _write(decision.as_dict(), args.output)
    return 0 if decision.allowed else 2


def _normalize_event(args: argparse.Namespace) -> int:
    payload = json.loads(Path(args.event).read_text(encoding="utf-8"))
    event = normalize_event(args.provider, payload)
    _write(event.as_dict(), args.output)
    return 0


def _render_prompt(args: argparse.Namespace) -> int:
    body = Path(args.task).read_text(encoding="utf-8")
    if args.mode == "spec":
        # Spec mode exists precisely for requests that fail the section contract.
        task = draft_request(args.title, body, tuple(args.label))
    else:
        task = parse_task(args.title, body, tuple(args.label))
    rendered = render_prompt(task, args.mode)
    Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    return 0


def _spec_input(args: argparse.Namespace) -> tuple[str, str, tuple[str, ...]]:
    if args.request:
        document = json.loads(Path(args.request).read_text(encoding="utf-8"))
        return _request_fields(args.provider, document)
    if not args.task or args.title is None:
        raise ValueError("spec commands need --request, or --task with --title")
    return args.title, Path(args.task).read_text(encoding="utf-8"), tuple(args.label)


def _body_sha256(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _spec_check(args: argparse.Namespace) -> int:
    """Diagnose the intake contract; exit 0 when ready, 1 when sections are deficient."""
    title, body, labels = _spec_input(args)
    check = check_task_spec(title, body)
    document = check.as_dict()
    document["bodySha256"] = _body_sha256(body)
    _write(document, args.output)
    if args.prompt_output and check.draftable:
        prompt = render_prompt(draft_request(title, body, labels), "spec")
        Path(args.prompt_output).write_text(prompt + "\n", encoding="utf-8")
    return 0 if check.ready else 1


def _merge_spec(args: argparse.Namespace) -> int:
    """Merge a spec-mode draft into the issue body it was drafted from."""
    title, body, _ = _spec_input(args)
    if args.expected_body_sha256 and _body_sha256(body) != args.expected_body_sha256:
        raise ValueError("issue body changed after the spec draft was requested")
    draft = parse_draft(Path(args.draft).read_text(encoding="utf-8"))
    if draft.verdict != "drafted":
        _write({"schemaVersion": 1, **draft.as_dict(), "changed": False}, args.result)
        return 0
    merged = merge_spec(
        title,
        body,
        draft.sections,
        open_questions=draft.open_questions,
        issue_number=args.issue_number,
    )
    Path(args.output).write_text(merged.body, encoding="utf-8")
    _write(merged.as_dict(), args.result)
    return 0


def _request_fields(provider: str, document: dict[str, Any]) -> tuple[str, str, tuple[str, ...]]:
    if provider == "github":
        labels = tuple(
            str(item.get("name", "")) if isinstance(item, dict) else str(item)
            for item in document.get("labels", [])
        )
        return str(document.get("title", "")), str(document.get("body") or ""), labels
    labels = tuple(str(item) for item in document.get("labels", []))
    return (
        str(document.get("title", "")),
        str(document.get("description") or document.get("body") or ""),
        labels,
    )


def _prepare_request(args: argparse.Namespace) -> int:
    document = json.loads(Path(args.request).read_text(encoding="utf-8"))
    title, body, labels = _request_fields(args.provider, document)
    task = parse_task(title, body, labels)
    policy = load_policy(args.config)
    if policy.provider != args.provider:
        raise ValueError("policy provider does not match request provider")
    # Exact on purpose: $GITHUB_REPOSITORY is GitHub's canonical spelling, onboard --apply
    # writes that spelling, and doctor fails a policy id that differs from it even by case.
    if args.expected_project_id and policy.project_id != args.expected_project_id:
        raise ValueError("policy project ID does not match execution repository")
    if args.expected_default_branch and policy.default_branch != args.expected_default_branch:
        raise ValueError("policy default branch does not match execution repository")
    decision = evaluate_task(task, policy, args.mode)
    _write(decision.as_dict(), args.decision_output)
    if args.metadata_output:
        _write(
            {
                "title": task.title,
                "dependencies": list(task.dependencies),
                "labels": list(task.labels),
            },
            args.metadata_output,
        )
    if not decision.allowed:
        return 2
    Path(args.task_output).write_text(body + "\n", encoding="utf-8")
    Path(args.prompt_output).write_text(
        render_prompt(task, args.mode, policy=policy) + "\n", encoding="utf-8"
    )
    return 0


def _inspect_diff(args: argparse.Namespace) -> int:
    snapshot = collect_git_diff(args.repository, args.base)
    Path(args.patch_output).write_bytes(snapshot.patch)
    Path(args.paths_output).write_text("\n".join(snapshot.paths) + "\n", encoding="utf-8")
    decision = evaluate_diff(
        snapshot.paths,
        snapshot.added_lines,
        snapshot.deleted_lines,
        load_policy(args.config),
        patch_bytes=len(snapshot.patch),
    )
    document = decision.as_dict()
    document["diff"] = {
        "files": len(snapshot.paths),
        "addedLines": snapshot.added_lines,
        "deletedLines": snapshot.deleted_lines,
        "patchBytes": len(snapshot.patch),
        "paths": list(snapshot.paths),
    }
    _write(document, args.decision_output)
    if decision.allowed and snapshot.paths:
        return 0
    # The workflow step that runs this aborts on exit 2 before the decision
    # artifact is uploaded, so the rejection reason must be visible in the log.
    reasons = list(decision.reasons) if not decision.allowed else []
    if not snapshot.paths:
        reasons.append("no files changed: the agent left an empty working tree")
    print(
        "ERROR: inspect-diff rejected the generated patch: " + "; ".join(reasons),
        file=sys.stderr,
    )
    return 2


def _create_manifest(args: argparse.Namespace) -> int:
    document = create_manifest(
        args.patch,
        base_sha=args.base_sha,
        repository=args.repository,
        request_number=args.request_number,
    )
    write_manifest(document, args.output)
    return 0


def _verify_artifact(args: argparse.Namespace) -> int:
    verify_manifest(
        args.patch,
        args.manifest,
        expected_base_sha=args.base_sha,
        expected_repository=args.repository,
        expected_request_number=args.request_number,
    )
    return 0


def _validate_missions(args: argparse.Namespace) -> int:
    registry = load_registry(args.missions, load_policy(args.config))
    _write(registry.as_dict(), args.output)
    return 0


def _validate_harness(args: argparse.Namespace) -> int:
    # Source-only reusable jobs must not acquire dependencies for unrelated CLI paths.
    from .harness import (
        HarnessError,
        document_digest,
        executor_profile_snapshot,
        load_manifest,
        read_document,
        validate_effective_risk,
        validate_executor_binding,
    )

    manifest = load_manifest(read_document(args.manifest))
    data = manifest.as_dict()
    # One snapshot is both hashed and parsed, so the checked digest is the enforced policy.
    policy_bytes = Path(args.config).read_bytes()
    policy = load_policy_bytes(policy_bytes)
    if hashlib.sha256(policy_bytes).hexdigest() != data["policy"]["projectPolicyDigest"]:
        raise HarnessError("project policy digest mismatch")
    if policy.project_id != data["work"]["projectId"]:
        raise HarnessError("project policy repository mismatch")
    registry = load_registry(args.missions, policy)
    if document_digest(registry.as_dict()) != data["policy"]["missionRegistryDigest"]:
        raise HarnessError("mission registry digest mismatch")
    mission = registry.get(data["mission"]["id"])
    validate_effective_risk(manifest, mission, expected_risk=RiskLevel(args.effective_risk))
    executor = validate_executor_binding(manifest, read_document(args.executors))
    _write(
        {
            "schemaVersion": 1,
            "manifestDigest": manifest.digest,
            "canonicalization": "RFC8785",
            "dispatchAuthorized": False,
            "checks": [
                "manifest-schema-and-semantics",
                "project-policy-and-mission-bindings",
                "caller-supplied-effective-risk",
                "executor-registry-and-profile-bindings",
            ],
            "notVerified": [
                "approval-and-source-provenance",
                "task-path-risk-computation",
                "skill-recipe-context-and-tool-profile-bindings",
                "actual-path-permissions-and-reviewer-independence",
                "routing-policy-and-live-capacity",
                "shared-budget-reservations-and-usage-recording",
                "patch-verification-attestation-and-review",
            ],
            "executorProfileSnapshot": executor_profile_snapshot(executor),
        },
        args.output,
    )
    return 0


def _dispatch_mission(args: argparse.Namespace) -> int:
    registry = load_registry(args.missions, load_policy(args.config))
    agents = load_agents(json.loads(Path(args.agents).read_text(encoding="utf-8")))
    history: dict[str, str] = {}
    if args.history:
        raw = json.loads(Path(args.history).read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in raw.items()
        ):
            raise MissionError("history must map mission IDs to agent IDs")
        history = raw
    agent = registry.select_agent(args.mission_id, agents, history=history)
    envelope = create_dispatch_envelope(
        registry.get(args.mission_id),
        agent,
        work_ref=args.work_ref,
        prompt=Path(args.prompt).read_text(encoding="utf-8"),
        input_refs=tuple(args.input_ref),
    )
    _write(envelope, args.output)
    return 0


def _validate_executors(args: argparse.Namespace) -> int:
    document = json.loads(Path(args.executors).read_text(encoding="utf-8"))
    executors = load_executors(document)
    _write(
        {"schemaVersion": 1, "executors": [executor.as_dict() for executor in executors]},
        args.output,
    )
    return 0


def _route_executor(args: argparse.Namespace) -> int:
    executors = load_executors(json.loads(Path(args.executors).read_text(encoding="utf-8")))
    policy = None
    if args.routing_policy:
        policy = load_routing_policy(
            json.loads(Path(args.routing_policy).read_text(encoding="utf-8"))
        )
    mission = load_registry(args.missions, load_policy(args.config)).get(args.mission_id)
    request = RouteRequest(
        repository=args.repository,
        task_class=TaskClass(args.task_class),
        risk=mission.risk,
        required_capabilities=mission.capabilities,
        required_tool_capabilities=tuple(args.required_tool_capability),
        min_context_window=args.min_context_window,
        budget_usd=args.budget_usd,
    )
    decision = route_executor(request, executors, policy)
    _write(decision.as_dict(), args.output)
    return 0 if decision.selected_executor_id else 2


def _orchestrate(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    if state_path.is_file():
        document = json.loads(state_path.read_text(encoding="utf-8"))
        engine = Orchestrator.from_dict(document)
    else:
        engine = Orchestrator(max_repair_cycles=args.max_repair_cycles)

    if args.action == "create":
        engine.create_unit(
            args.unit,
            event_key=args.event_key,
            timestamp=args.timestamp,
            concurrency_group=args.concurrency_group or "",
            depends_on=tuple(args.depends_on),
        )
    elif args.action == "transition":
        engine.transition(
            args.unit,
            args.to,
            actor=args.actor,
            actor_kind=args.actor_kind,
            event_key=args.event_key,
            timestamp=args.timestamp,
            reason=args.reason,
        )
    elif args.action == "start-run":
        envelope = json.loads(Path(args.envelope).read_text(encoding="utf-8"))
        engine.start_run(
            args.unit,
            args.kind,
            envelope,
            event_key=args.event_key,
            timestamp=args.timestamp,
        )
    elif args.action == "finish-run":
        engine.finish_run(
            args.unit,
            args.run_id,
            result=args.result,
            timestamp=args.timestamp,
            commit_sha=args.commit_sha,
        )
    elif args.action == "verification":
        engine.record_verification(
            args.unit,
            passed=args.result == "succeeded",
            event_key=args.event_key,
            timestamp=args.timestamp,
        )
    elif args.action == "review":
        engine.record_review(
            args.unit,
            args.run_id,
            findings=args.findings,
            event_key=args.event_key,
            timestamp=args.timestamp,
        )
    _write(engine.as_dict(), str(state_path))
    if args.status_output:
        Path(args.status_output).write_text(engine.status_document(args.unit), encoding="utf-8")
    return 0


def _validate_registry(args: argparse.Namespace) -> int:
    registry = read_project_registry(args.registry)
    _write(registry.as_dict(), args.output)
    return 0


def _record_event(args: argparse.Namespace) -> int:
    registry = read_optional_project_registry(args.registry)
    ledger_path = Path(args.ledger)
    if ledger_path.is_file():
        ledger = EventLedger.from_dict(
            json.loads(ledger_path.read_text(encoding="utf-8")), registry=registry
        )
    else:
        ledger = EventLedger(registry=registry)
    document = json.loads(Path(args.event).read_text(encoding="utf-8"))
    event = ledger.append(load_lifecycle_event(document))
    _write(ledger.as_dict(), str(ledger_path))
    if args.output:
        _write(event.as_dict(), args.output)
    if args.projection_output:
        projection = ledger.projection(event.project_id, event.work_unit_id)
        _write(projection.as_dict(), args.projection_output)
    return 0


def _project_state(args: argparse.Namespace) -> int:
    registry = read_optional_project_registry(args.registry)
    ledger = EventLedger.from_dict(
        json.loads(Path(args.ledger).read_text(encoding="utf-8")), registry=registry
    )
    project_ids = tuple(args.project_id) or None
    if args.unit:
        if project_ids is None or len(project_ids) != 1:
            raise LedgerError("--unit requires exactly one --project-id")
        _write(ledger.projection(project_ids[0], args.unit).as_dict(), args.output)
        return 0
    _write(
        {
            "schemaVersion": 1,
            "projections": [item.as_dict() for item in ledger.projections(project_ids=project_ids)],
            "aggregate": ledger.aggregate(project_ids=project_ids),
        },
        args.output,
    )
    return 0


def _read_usage_ledger(path: str, registry_path: str | None, *, create: bool) -> UsageLedger:
    registry = read_optional_project_registry(registry_path)
    ledger_path = Path(path)
    if ledger_path.is_file():
        return UsageLedger.from_dict(
            json.loads(ledger_path.read_text(encoding="utf-8")), registry=registry
        )
    if not create:
        raise UsageError(f"usage ledger not found: {path}")
    return UsageLedger(registry=registry)


def _record_usage(args: argparse.Namespace) -> int:
    ledger = _read_usage_ledger(args.ledger, args.registry, create=True)
    document = json.loads(Path(args.record).read_text(encoding="utf-8"))
    record = ledger.append(load_usage_record(document))
    _write(ledger.as_dict(), args.ledger)
    if args.output:
        _write(record.as_dict(), args.output)
    if args.error_output:
        _write(record.estimate_error.as_dict(), args.error_output)
    return 0


def _usage_report(args: argparse.Namespace) -> int:
    ledger = _read_usage_ledger(args.ledger, args.registry, create=False)
    _write(
        ledger.aggregate(
            group_by=tuple(args.group_by) or ("project",),
            project_ids=tuple(args.project_id) or None,
            since=args.since,
            until=args.until,
        ),
        args.output,
    )
    return 0


def _read_calibration(path: str | None, *, min_samples: int | None) -> EstimatorCalibration:
    if path and Path(path).is_file():
        calibration = EstimatorCalibration.from_dict(
            json.loads(Path(path).read_text(encoding="utf-8"))
        )
        if min_samples is not None:
            calibration.min_samples = min_samples
        return calibration
    return EstimatorCalibration(min_samples=min_samples if min_samples is not None else 3)


def _calibrate_usage(args: argparse.Namespace) -> int:
    ledger = _read_usage_ledger(args.ledger, args.registry, create=False)
    calibration = _read_calibration(args.calibration, min_samples=args.min_samples)
    learned = calibration.observe_all(ledger.records)
    _write(calibration.as_dict(), args.calibration)
    _write(
        {
            "schemaVersion": 1,
            "learnedRecords": learned,
            "observedRecords": len(calibration.observed_usage_ids),
            "ledgerRecords": len(ledger),
        },
        args.output,
    )
    return 0


def _estimate_usage(args: argparse.Namespace) -> int:
    calibration = _read_calibration(args.calibration, min_samples=None)
    pricing = None
    if args.pricing_id:
        if not args.pricing:
            raise UsageError("--pricing-id requires --pricing")
        snapshots = load_pricing_document(
            json.loads(Path(args.pricing).read_text(encoding="utf-8"))
        )
        if args.pricing_id not in snapshots:
            raise UsageError(f"unknown pricingId: {args.pricing_id}")
        pricing = snapshots[args.pricing_id]
        if pricing.model != args.model:
            raise UsageError(
                f"pricing snapshot {args.pricing_id} prices model {pricing.model}, "
                f"not {args.model}; an estimate priced at another model's rates is "
                "not a cost for this run"
            )
    estimate = calibration.estimate(
        baseline_tokens=TokenCounts(
            input=args.baseline_input_tokens,
            output=args.baseline_output_tokens,
            cache_read=args.baseline_cache_read_tokens,
            cache_write=args.baseline_cache_write_tokens,
        ),
        baseline_runtime_seconds=args.baseline_runtime_seconds,
        model=args.model,
        stage=LifecycleStage(args.stage),
        task_class=args.task_class,
        complexity=args.complexity,
        pricing=pricing,
        plan_capacity_units=args.plan_capacity_units,
    )
    _write(estimate.as_dict(), args.output)
    return 0


def _capacity_report(args: argparse.Namespace) -> int:
    ledger = _read_usage_ledger(args.usage, args.registry, create=False)
    inputs = None
    if args.inputs:
        inputs = load_capacity_inputs(json.loads(Path(args.inputs).read_text(encoding="utf-8")))
    _write(
        build_capacity_report(
            ledger.records,
            window=ObservationWindow(args.window_start, args.window_end),
            inputs=inputs,
            project_ids=tuple(args.project_id) or None,
        ),
        args.output,
    )
    return 0


def _validate_knowledge(args: argparse.Namespace) -> int:
    sources = load_sources(args.knowledge)
    _write(
        {"schemaVersion": 1, "sources": [source.as_dict() for source in sources]},
        args.output,
    )
    return 0


def _classify_failure(args: argparse.Namespace) -> int:
    log = Path(args.log).read_text(encoding="utf-8", errors="replace") if args.log else ""
    failure_class = classify_failure(
        conclusion=args.conclusion,
        log=log,
        review_verdict=args.review_verdict,
        policy_decision=args.policy_decision,
    )
    _write({"failureClass": failure_class.value}, args.output)
    return 0


def _decide_infra_retry(args: argparse.Namespace) -> int:
    if bool(args.state) == bool(args.comments):
        raise InfraRecoveryError("exactly one of --state or --comments is required")
    if args.comments:
        comments = json.loads(Path(args.comments).read_text(encoding="utf-8"))
        state = retry_state_from_comments(comments)
    else:
        state = load_retry_state(args.state)
    decision = decide_retry(
        repository=args.repository,
        pull_request_number=args.pull_request_number,
        run_id=args.run_id,
        head_sha=args.head_sha,
        current_head_sha=args.current_head_sha,
        failure_class=FailureClass(args.failure_class),
        state=state,
        failed_job_ids=tuple(args.failed_job_id),
        max_attempts=args.max_attempts,
        base_delay_seconds=args.base_delay_seconds,
        event_key=args.event_key,
        last_error_summary=args.last_error_summary,
    )
    if args.event_key:
        if decision.action is RetryAction.RETRY_FAILED_JOBS:
            state.record_retry(decision, event_key=args.event_key, timestamp=args.timestamp)
        elif decision.action is RetryAction.BLOCK:
            state.record_exhaustion(decision, event_key=args.event_key, timestamp=args.timestamp)
    if args.state:
        write_retry_state(state, args.state)
    if args.comment_output and decision.action in {
        RetryAction.RETRY_FAILED_JOBS,
        RetryAction.BLOCK,
    }:
        Path(args.comment_output).write_text(
            render_retry_comment(decision, event_key=args.event_key, timestamp=args.timestamp)
            + "\n",
            encoding="utf-8",
        )
    if args.panel_output:
        _write(
            build_infrastructure_blocker_panel(
                decision.as_dict(), observed_at=args.timestamp or None
            ),
            args.panel_output,
        )
    _write(decision.as_dict(), args.output)
    return 0 if decision.action is not RetryAction.BLOCK else 2


def _run_gates(args: argparse.Namespace) -> int:
    report = run_gates(args.config, args.repository)
    write_report(report, args.output)
    return 0 if report["passed"] else 2


def _scaffold(args: argparse.Namespace) -> int:
    destination = Path(args.destination).resolve()
    created = scaffold_project(
        destination,
        provider=args.provider,
        project_id=args.project_id,
        platform_repository=args.platform_repository,
        platform_ref=args.platform_ref,
        default_branch=args.default_branch,
        automation_level=args.automation_level,
    )
    _write(
        {"created": [str(path.relative_to(destination)) for path in created]},
        args.output,
    )
    return 0


def _onboard(args: argparse.Namespace) -> int:
    destination = Path(args.destination).resolve()
    # The policy must name the repository as GitHub spells it ($GITHUB_REPOSITORY, compared
    # exactly by prepare-request); --apply reads it anyway. Offline, doctor checks the spelling.
    project_id = canonical_project_id(args.project_id) if args.apply else args.project_id
    platform_ref = resolve_platform_ref(args.platform_repository, args.platform_ref)
    default_branch = args.default_branch
    if not default_branch:
        default_branch = repository_default_branch(project_id) if args.apply else "main"
    if args.visibility == "auto":
        try:
            fork_exposed = repository_fork_exposed(project_id)
        except OnboardError as exc:
            raise OnboardError(
                f"{exc}; pass --visibility public|private (public keeps fork pull-request CI "
                "off self-hosted runners)"
            ) from exc
    else:
        fork_exposed = args.visibility == "public"
    spec = OnboardSpec(
        project_id=project_id,
        platform_repository=args.platform_repository,
        platform_ref=platform_ref,
        test_command=args.test,
        setup_command=args.setup,
        quality_command=args.quality,
        python_version=args.python_version,
        implementer=args.implementer,
        runs_on=tuple(args.runs_on.split(",")),
        ci_runs_on=tuple(args.ci_runs_on.split(",")) if args.ci_runs_on else (),
        fork_exposed=fork_exposed,
        default_branch=default_branch,
        forbidden_paths=tuple(args.forbidden or ()),
        protected_paths=tuple(args.protected or ()),
    )
    assignments: dict[str, str] = {}
    for item in args.var or ():  # validate CLI input before any file is written
        key, sep, value = item.partition("=")
        if not sep:
            raise OnboardError(f"--var expects KEY=VALUE, got {item!r}")
        assignments[key] = value
    written = write_onboarding(destination, spec, force=args.force)
    result: dict[str, Any] = {
        "platform_ref": platform_ref,
        "written": [str(path.relative_to(destination)) for path in written],
    }
    if args.apply:
        variables: dict[str, str] = {}
        if args.copy_vars_from:
            names = ["PUBLISHER_APP_CLIENT_ID"]
            registry = destination / ".forge/executors.json"
            if spec.routed and registry.exists():
                names += registry_model_vars(json.loads(registry.read_text(encoding="utf-8")))
            variables.update(copy_variables(args.copy_vars_from, names))
        variables.update(assignments)
        result["applied"] = apply_repo_settings(spec, variables=variables)
        report = doctor(destination, spec.project_id, spec.platform_repository)
        result["doctor"] = report.as_dict()
        print(report.render(), file=sys.stderr)
    _write(result, args.output)
    return 0


def _doctor(args: argparse.Namespace) -> int:
    destination = Path(args.destination).resolve()
    project_id = args.project_id
    platform = args.platform_repository
    if not project_id or not platform:
        policy = load_policy(destination / "agentic-sdlc.toml")
        project_id = project_id or policy.project_id
        if not platform:
            found = [
                repository
                for _, repository, workflow, _ in reusable_calls(
                    destination / ".github/workflows/agent-plan.yml"
                )
                if workflow == "reusable-plan.yml"
            ]
            if not found:
                raise OnboardError(
                    "cannot infer the platform repository; pass --platform-repository"
                )
            platform = found[0]
    report = doctor(destination, project_id, platform, remote=not args.local)
    print(report.render())
    _write(report.as_dict(), args.output) if args.output else None
    return 0 if report.ok else 2


def _lease_ttl(args: argparse.Namespace) -> int:
    if args.ttl_minutes is not None:  # an explicit 0 must reach validation, not the default
        return args.ttl_minutes
    config = Path(args.config) if args.config else Path("agentic-sdlc.toml")
    if config.exists():
        return load_policy(config).lease_ttl_minutes
    return DEFAULT_TTL_MINUTES


# The lease subcommands follow the CLI's convention: the JSON result goes to stdout (or to
# --output), and human status lines go to stderr, so stdout always parses as one JSON document.


def _status(message: str) -> None:
    print(message, file=sys.stderr)


def _claim(args: argparse.Namespace) -> int:
    result = claim(
        args.project,
        args.issue,
        agent=args.agent,
        session=args.session,
        branch=args.branch,
        ttl_minutes=_lease_ttl(args),
        gh=run_gh,
        assignee=args.assignee,
    )
    _write(result.as_dict(), args.output)
    if not result.ok:
        _status(f"REFUSED: {result.reason}")
        return 2
    verb = "renewed" if result.renewed else "claimed"
    _status(
        f"{verb} #{args.issue} for session {args.session} until {result.lease.expires.isoformat()}"
    )
    return 0


def _renew(args: argparse.Namespace) -> int:
    lease = renew(
        args.project, args.issue, session=args.session, ttl_minutes=_lease_ttl(args), gh=run_gh
    )
    _write(ClaimResult(True, lease, renewed=True).as_dict(), args.output)
    _status(f"renewed #{args.issue} until {lease.expires.isoformat()}")
    return 0


def _release(args: argparse.Namespace) -> int:
    release(
        args.project,
        args.issue,
        session=args.session,
        gh=run_gh,
        force=args.force,
        note=args.note or "",
    )
    _write({"issue": args.issue, "released": True, "session": args.session}, args.output)
    _status(f"released #{args.issue}")
    return 0


def _claims(args: argparse.Namespace) -> int:
    rows = list_claims(args.project, run_gh)
    _status(render_claims(rows))
    _write(
        [
            {
                "issue": r.lease.issue,
                "agent": r.lease.agent,
                "session": r.lease.session,
                "branch": r.lease.branch,
                "expires": r.lease.expires.isoformat(),
                "expired": r.expired,
            }
            for r in rows
        ],
        args.output,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sdlcctl")
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate-task")
    validate.add_argument("--config", required=True)
    validate.add_argument("--task", required=True)
    validate.add_argument("--title", required=True)
    validate.add_argument("--label", action="append", default=[])
    validate.add_argument("--mode", choices=("plan", "implement", "review"), default="plan")
    validate.add_argument("--output")
    validate.set_defaults(handler=_validate_task)

    diff = commands.add_parser("evaluate-diff")
    diff.add_argument("--config", required=True)
    diff.add_argument("--paths-file", required=True)
    diff.add_argument("--added", required=True, type=int)
    diff.add_argument("--deleted", required=True, type=int)
    diff.add_argument("--patch-bytes", type=int, default=0)
    diff.add_argument("--output")
    diff.set_defaults(handler=_evaluate_diff)

    event = commands.add_parser("normalize-event")
    event.add_argument("--provider", choices=("github", "gitlab"), required=True)
    event.add_argument("--event", required=True)
    event.add_argument("--output")
    event.set_defaults(handler=_normalize_event)

    prompt = commands.add_parser("render-prompt")
    prompt.add_argument("--task", required=True)
    prompt.add_argument("--title", required=True)
    prompt.add_argument("--label", action="append", default=[])
    prompt.add_argument("--mode", choices=("plan", "implement", "review", "spec"), required=True)
    prompt.add_argument("--output", required=True)
    prompt.set_defaults(handler=_render_prompt)

    spec_check = commands.add_parser("spec-check")
    spec_check.add_argument("--provider", choices=("github", "gitlab"), default="github")
    spec_check.add_argument("--request")
    spec_check.add_argument("--task")
    spec_check.add_argument("--title")
    spec_check.add_argument("--label", action="append", default=[])
    spec_check.add_argument("--prompt-output")
    spec_check.add_argument("--output")
    spec_check.set_defaults(handler=_spec_check)

    merge = commands.add_parser("merge-spec")
    merge.add_argument("--provider", choices=("github", "gitlab"), default="github")
    merge.add_argument("--request")
    merge.add_argument("--task")
    merge.add_argument("--title")
    merge.add_argument("--label", action="append", default=[])
    merge.add_argument("--draft", required=True)
    merge.add_argument("--issue-number", type=int)
    merge.add_argument("--expected-body-sha256")
    merge.add_argument("--output", required=True)
    merge.add_argument("--result")
    merge.set_defaults(handler=_merge_spec)

    prepare = commands.add_parser("prepare-request")
    prepare.add_argument("--provider", choices=("github", "gitlab"), required=True)
    prepare.add_argument("--config", required=True)
    prepare.add_argument("--request", required=True)
    prepare.add_argument("--mode", choices=("plan", "implement", "review"), required=True)
    prepare.add_argument("--task-output", required=True)
    prepare.add_argument("--prompt-output", required=True)
    prepare.add_argument("--decision-output", required=True)
    prepare.add_argument("--metadata-output")
    prepare.add_argument("--expected-project-id")
    prepare.add_argument("--expected-default-branch")
    prepare.set_defaults(handler=_prepare_request)

    inspect = commands.add_parser("inspect-diff")
    inspect.add_argument("--config", required=True)
    inspect.add_argument("--repository", default=".")
    inspect.add_argument("--base", default="HEAD")
    inspect.add_argument("--patch-output", required=True)
    inspect.add_argument("--paths-output", required=True)
    inspect.add_argument("--decision-output", required=True)
    inspect.set_defaults(handler=_inspect_diff)

    manifest = commands.add_parser("create-manifest")
    manifest.add_argument("--patch", required=True)
    manifest.add_argument("--base-sha", required=True)
    manifest.add_argument("--repository", required=True)
    manifest.add_argument("--request-number", required=True, type=int)
    manifest.add_argument("--output", required=True)
    manifest.set_defaults(handler=_create_manifest)

    verify = commands.add_parser("verify-artifact")
    verify.add_argument("--patch", required=True)
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--base-sha")
    verify.add_argument("--repository")
    verify.add_argument("--request-number", type=int)
    verify.set_defaults(handler=_verify_artifact)

    missions = commands.add_parser("validate-missions")
    missions.add_argument("--config", required=True)
    missions.add_argument("--missions")
    missions.add_argument("--output")
    missions.set_defaults(handler=_validate_missions)

    harness = commands.add_parser("validate-harness")
    harness.add_argument("--manifest", required=True)
    harness.add_argument("--config", required=True)
    harness.add_argument("--missions")
    harness.add_argument("--executors", required=True)
    harness.add_argument(
        "--effective-risk", choices=tuple(item.value for item in RiskLevel), required=True
    )
    harness.add_argument("--output")
    harness.set_defaults(handler=_validate_harness)

    dispatch = commands.add_parser("dispatch-mission")
    dispatch.add_argument("--config", required=True)
    dispatch.add_argument("--missions")
    dispatch.add_argument("--mission-id", required=True)
    dispatch.add_argument("--agents", required=True)
    dispatch.add_argument("--history")
    dispatch.add_argument("--work-ref", required=True)
    dispatch.add_argument("--input-ref", action="append", default=[])
    dispatch.add_argument("--prompt", required=True)
    dispatch.add_argument("--output")
    dispatch.set_defaults(handler=_dispatch_mission)

    executors = commands.add_parser("validate-executors")
    executors.add_argument("--executors", required=True)
    executors.add_argument("--output")
    executors.set_defaults(handler=_validate_executors)

    route = commands.add_parser("route-executor")
    route.add_argument("--config", required=True)
    route.add_argument("--missions")
    route.add_argument("--mission-id", required=True)
    route.add_argument("--executors", required=True)
    route.add_argument("--routing-policy")
    route.add_argument("--repository", required=True)
    route.add_argument(
        "--task-class",
        choices=tuple(item.value for item in TaskClass),
        required=True,
    )
    route.add_argument("--required-tool-capability", action="append", default=[])
    route.add_argument("--min-context-window", type=int, default=0)
    route.add_argument("--budget-usd", type=_budget_usd, required=True)
    route.add_argument("--output")
    route.set_defaults(handler=_route_executor)

    orchestrate = commands.add_parser("orchestrate")
    orchestrate.add_argument(
        "--action",
        choices=("create", "transition", "start-run", "finish-run", "verification", "review"),
        required=True,
    )
    orchestrate.add_argument("--state", required=True)
    orchestrate.add_argument("--unit", required=True)
    orchestrate.add_argument("--event-key", default="")
    orchestrate.add_argument("--timestamp", required=True)
    orchestrate.add_argument("--to")
    orchestrate.add_argument("--actor", default="system")
    orchestrate.add_argument("--actor-kind", choices=("human", "agent", "system"), default="system")
    orchestrate.add_argument("--reason", default="")
    orchestrate.add_argument("--concurrency-group")
    orchestrate.add_argument("--depends-on", action="append", default=[])
    orchestrate.add_argument("--kind")
    orchestrate.add_argument("--envelope")
    orchestrate.add_argument("--run-id")
    orchestrate.add_argument("--result", choices=("succeeded", "failed"))
    orchestrate.add_argument("--findings", type=int, default=0)
    orchestrate.add_argument("--commit-sha", default="")
    orchestrate.add_argument("--max-repair-cycles", type=int, default=2)
    orchestrate.add_argument("--status-output")
    orchestrate.set_defaults(handler=_orchestrate)

    registry = commands.add_parser("validate-registry")
    registry.add_argument("--registry", required=True)
    registry.add_argument("--output")
    registry.set_defaults(handler=_validate_registry)

    record_event = commands.add_parser("record-event")
    record_event.add_argument("--ledger", required=True)
    record_event.add_argument("--event", required=True)
    record_event.add_argument("--registry")
    record_event.add_argument("--projection-output")
    record_event.add_argument("--output")
    record_event.set_defaults(handler=_record_event)

    project_state = commands.add_parser("project-state")
    project_state.add_argument("--ledger", required=True)
    project_state.add_argument("--registry")
    project_state.add_argument("--project-id", action="append", default=[])
    project_state.add_argument("--unit")
    project_state.add_argument("--output")
    project_state.set_defaults(handler=_project_state)

    record_usage = commands.add_parser("record-usage")
    record_usage.add_argument("--ledger", required=True)
    record_usage.add_argument("--record", required=True)
    record_usage.add_argument("--registry")
    record_usage.add_argument("--error-output")
    record_usage.add_argument("--output")
    record_usage.set_defaults(handler=_record_usage)

    usage_report = commands.add_parser("usage-report")
    usage_report.add_argument("--ledger", required=True)
    usage_report.add_argument("--registry")
    usage_report.add_argument("--project-id", action="append", default=[])
    usage_report.add_argument("--group-by", action="append", default=[])
    usage_report.add_argument("--since")
    usage_report.add_argument("--until")
    usage_report.add_argument("--output")
    usage_report.set_defaults(handler=_usage_report)

    calibrate_usage = commands.add_parser("calibrate-usage")
    calibrate_usage.add_argument("--ledger", required=True)
    calibrate_usage.add_argument("--calibration", required=True)
    calibrate_usage.add_argument("--registry")
    calibrate_usage.add_argument("--min-samples", type=int)
    calibrate_usage.add_argument("--output")
    calibrate_usage.set_defaults(handler=_calibrate_usage)

    estimate_usage = commands.add_parser("estimate-usage")
    estimate_usage.add_argument("--calibration")
    estimate_usage.add_argument("--pricing")
    estimate_usage.add_argument("--pricing-id")
    estimate_usage.add_argument("--model", required=True)
    estimate_usage.add_argument(
        "--stage", choices=tuple(item.value for item in LifecycleStage), required=True
    )
    estimate_usage.add_argument("--task-class", default="")
    estimate_usage.add_argument("--complexity", default="")
    estimate_usage.add_argument("--baseline-input-tokens", type=int)
    estimate_usage.add_argument("--baseline-output-tokens", type=int)
    estimate_usage.add_argument("--baseline-cache-read-tokens", type=int)
    estimate_usage.add_argument("--baseline-cache-write-tokens", type=int)
    estimate_usage.add_argument("--baseline-runtime-seconds", type=int)
    estimate_usage.add_argument("--plan-capacity-units", type=float)
    estimate_usage.add_argument("--output")
    estimate_usage.set_defaults(handler=_estimate_usage)

    capacity = commands.add_parser("capacity-report")
    capacity.add_argument("--usage", required=True)
    capacity.add_argument("--window-start", required=True)
    capacity.add_argument("--window-end", required=True)
    capacity.add_argument("--inputs")
    capacity.add_argument("--registry")
    capacity.add_argument("--project-id", action="append", default=[])
    capacity.add_argument("--output")
    capacity.set_defaults(handler=_capacity_report)

    knowledge = commands.add_parser("validate-knowledge")
    knowledge.add_argument("--knowledge", required=True)
    knowledge.add_argument("--output")
    knowledge.set_defaults(handler=_validate_knowledge)

    classify_failure_parser = commands.add_parser("classify-failure")
    classify_failure_parser.add_argument("--conclusion", default="")
    classify_failure_parser.add_argument("--log")
    classify_failure_parser.add_argument("--review-verdict", default="")
    classify_failure_parser.add_argument("--policy-decision", default="")
    classify_failure_parser.add_argument("--output")
    classify_failure_parser.set_defaults(handler=_classify_failure)

    infra_retry = commands.add_parser("decide-infra-retry")
    infra_retry.add_argument("--state")
    infra_retry.add_argument("--comments")
    infra_retry.add_argument("--repository", required=True)
    infra_retry.add_argument("--pull-request-number", type=int, required=True)
    infra_retry.add_argument("--run-id", type=int, required=True)
    infra_retry.add_argument("--head-sha", required=True)
    infra_retry.add_argument("--current-head-sha", required=True)
    infra_retry.add_argument(
        "--failure-class",
        choices=tuple(item.value for item in FailureClass),
        required=True,
    )
    infra_retry.add_argument("--failed-job-id", action="append", default=[], type=int)
    infra_retry.add_argument("--max-attempts", type=int, default=3)
    infra_retry.add_argument("--base-delay-seconds", type=int, default=60)
    infra_retry.add_argument("--event-key", default="")
    infra_retry.add_argument("--timestamp", default="")
    infra_retry.add_argument("--last-error-summary", default="")
    infra_retry.add_argument("--comment-output")
    infra_retry.add_argument("--panel-output")
    infra_retry.add_argument("--output")
    infra_retry.set_defaults(handler=_decide_infra_retry)

    gates = commands.add_parser("run-gates")
    gates.add_argument("--config", required=True)
    gates.add_argument("--repository", default=".")
    gates.add_argument("--output", required=True)
    gates.set_defaults(handler=_run_gates)

    scaffold = commands.add_parser("scaffold")
    scaffold.add_argument("--destination", required=True)
    scaffold.add_argument("--provider", choices=("github", "gitlab"), required=True)
    scaffold.add_argument("--project-id", required=True)
    scaffold.add_argument("--platform-repository", required=True)
    scaffold.add_argument("--platform-ref", required=True)
    scaffold.add_argument("--default-branch", default="main")
    scaffold.add_argument("--automation-level", choices=(1, 2, 3), type=int, default=1)
    scaffold.add_argument("--output")
    scaffold.set_defaults(handler=_scaffold)

    onboard = commands.add_parser(
        "onboard",
        help="install the production Forge profile and (optionally) configure the GitHub repo",
    )
    onboard.add_argument("--destination", required=True, help="path to the consumer git checkout")
    onboard.add_argument("--project-id", required=True, help="owner/name on GitHub")
    onboard.add_argument("--platform-repository", default="atulg4/agentic-sdlc")
    onboard.add_argument(
        "--platform-ref", default="main", help="branch, tag or 40-char SHA; branches are resolved"
    )
    onboard.add_argument("--test", required=True, help="test command run as the verification gate")
    onboard.add_argument("--setup", default="python -m pip install -r requirements.txt")
    onboard.add_argument("--quality", default="python -m ruff check --select E9,F63,F7,F82 .")
    onboard.add_argument(
        "--python-version",
        default="3.12",
        help="interpreter ci.yml's pinned actions/setup-python installs ([ci] python_version)",
    )
    onboard.add_argument(
        "--implementer", choices=("route", "claude", "codex", "cloud-routine"), default="route"
    )
    onboard.add_argument(
        "--runs-on", default="self-hosted,linux,x64", help="comma-separated runner labels"
    )
    onboard.add_argument(
        "--ci-runs-on",
        help="runner labels for ci.yml (pull_request-triggered); defaults to --runs-on, or "
        "ubuntu-latest for a fork-exposed repository, which may not use self-hosted labels here",
    )
    onboard.add_argument(
        "--visibility",
        choices=("auto", "public", "private"),
        default="auto",
        help="fork exposure; auto reads visibility and fork policy from GitHub (CI stays "
        "self-hosted only for a private/internal repository with forking or fork pull-request "
        "workflows proven disabled); private asserts that offline and doctor verifies it",
    )
    onboard.add_argument(
        "--default-branch",
        help="defaults to the repository's GitHub default branch with --apply, else main",
    )
    onboard.add_argument(
        "--forbidden", action="append", help="extra forbidden path glob (repeatable)"
    )
    onboard.add_argument("--protected", action="append", help="protected path glob (repeatable)")
    onboard.add_argument("--force", action="store_true", help="overwrite files the profile owns")
    onboard.add_argument(
        "--apply", action="store_true", help="create labels, ruleset and variables via gh"
    )
    onboard.add_argument(
        "--copy-vars-from", help="owner/name of an onboarded repo to copy non-secret variables from"
    )
    onboard.add_argument(
        "--var", action="append", help="repo variable KEY=VALUE to set (repeatable)"
    )
    onboard.add_argument("--output")
    onboard.set_defaults(handler=_onboard)

    doc = commands.add_parser(
        "doctor", help="verify a consumer repo is ready for Forge; exit 2 if not"
    )
    doc.add_argument("--destination", default=".")
    doc.add_argument("--project-id", help="defaults to [project].id in agentic-sdlc.toml")
    doc.add_argument("--platform-repository", help="defaults to the repo pinned in agent-plan.yml")
    doc.add_argument("--local", action="store_true", help="skip GitHub API checks")
    doc.add_argument("--output")
    doc.set_defaults(handler=_doctor)

    def _lease_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--project", required=True, help="owner/name on GitHub")
        parser.add_argument("--issue", type=int, required=True)
        parser.add_argument("--session", required=True, help="unique id of this agent run/session")
        parser.add_argument("--config", help="agentic-sdlc.toml (for lease_ttl_minutes)")
        parser.add_argument("--ttl-minutes", type=int)
        parser.add_argument("--output")

    lease_claim = commands.add_parser(
        "claim", help="lease an issue for this agent/session; exit 2 if taken"
    )
    _lease_args(lease_claim)
    lease_claim.add_argument(
        "--agent", required=True, help="who is working, e.g. forge-actions, cloud-routine"
    )
    lease_claim.add_argument(
        "--branch", required=True, help="branch the work lands on, e.g. forge/issue-12"
    )
    lease_claim.add_argument("--assignee", help="GitHub login to assign (optional)")
    lease_claim.set_defaults(handler=_claim)

    lease_renew = commands.add_parser("renew", help="extend a lease held by this session")
    _lease_args(lease_renew)
    lease_renew.set_defaults(handler=_renew)

    lease_release = commands.add_parser("release", help="release a lease held by this session")
    _lease_args(lease_release)
    lease_release.add_argument(
        "--force", action="store_true", help="release another session's lease"
    )
    lease_release.add_argument("--note")
    lease_release.set_defaults(handler=_release)

    lease_list = commands.add_parser("claims", help="list live and expired leases in a repository")
    lease_list.add_argument("--project", required=True)
    lease_list.add_argument("--output")
    lease_list.set_defaults(handler=_claims)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (
        TaskSpecError,
        ArtifactError,
        CapacityError,
        EventError,
        ExecutorError,
        GateError,
        GitDiffError,
        InfraRecoveryError,
        KnowledgeError,
        LedgerError,
        MissionError,
        OrchestrationError,
        ProjectRegistryError,
        ScaffoldError,
        LeaseError,
        UsageError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
