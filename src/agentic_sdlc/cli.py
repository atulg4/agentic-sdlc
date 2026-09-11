"""Command-line interface for pipeline policy checks."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

from .artifact import ArtifactError, create_manifest, verify_manifest, write_manifest
from .dashboard_efficiency import build_infrastructure_blocker_panel
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
from .missions import (
    MissionError,
    create_dispatch_envelope,
    load_agents,
    load_registry,
)
from .orchestration import OrchestrationError, Orchestrator
from .policy import evaluate_diff, evaluate_task, load_policy
from .scaffold import ScaffoldError, scaffold_project
from .task_spec import TaskSpecError, parse_task, render_prompt


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


def _write(data: dict[str, Any], output: str | None) -> None:
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
    task = parse_task(args.title, body, tuple(args.label))
    rendered = render_prompt(task, args.mode)
    Path(args.output).write_text(rendered + "\n", encoding="utf-8")
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
    prompt.add_argument("--mode", choices=("plan", "implement", "review"), required=True)
    prompt.add_argument("--output", required=True)
    prompt.set_defaults(handler=_render_prompt)

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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (
        TaskSpecError,
        ArtifactError,
        EventError,
        ExecutorError,
        GateError,
        GitDiffError,
        InfraRecoveryError,
        KnowledgeError,
        MissionError,
        OrchestrationError,
        ScaffoldError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
