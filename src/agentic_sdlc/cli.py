"""Command-line interface for pipeline policy checks."""

from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import hmac
import json
import math
import os
import re
import shlex
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .artifact import ArtifactError, create_manifest, verify_manifest, write_manifest
from .capacity_metrics import (
    CapacityError,
    ObservationWindow,
    build_capacity_report,
    load_capacity_inputs,
)
from .context_builder import (
    DEFAULT_PACKET_BYTES,
    ContextBuilderError,
    VerificationEvidence,
    _safe_exclusion_path,
    build_context_packet,
    dispatch_envelope_for_packet,
    mission_role,
    verification_from_dict,
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
from .git_diff import (
    GitDiffError,
    collect_commit_diff,
    collect_git_diff,
    collect_series_patch,
    committed_path,
    derivation_tree,
    read_committed_file,
    repository_identity,
    resolve_commit,
)
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
from .knowledge import (
    KnowledgeError,
    load_sources,
    load_sources_bytes,
    record_from_dict,
    sanitize_payload,
)
from .leases import (
    DEFAULT_TTL_MINUTES,
    LEASE_BOT_LOGINS_ENV,
    ClaimResult,
    LeaseError,
    authorize_labeler,
    claim,
    claim_inventory,
    issue_event_keys,
    issue_events,
    open_prs_for_issue,
    publisher_bot_login_problem,
    release,
    render_claims,
    renew,
    use_policy_bot_logins,
)
from .missions import (
    MissionError,
    create_dispatch_envelope,
    load_agents,
    load_registry,
    load_registry_bytes,
)
from .models import RiskLevel
from .onboard import (
    MAX_REQUIRED_APPROVALS,
    OnboardError,
    OnboardSpec,
    apply_repo_settings,
    canonical_project_id,
    confirm_cloud_routine,
    copy_variables,
    default_required_approvals,
    doctor,
    gh_http_status,
    installed_bot_login,
    installed_trust_mode,
    policy_trust_mode,
    registry_model_vars,
    repository_default_branch,
    repository_fork_exposed,
    resolve_platform_ref,
    reusable_calls,
    run_gh,
    trusted_hook_python,
    variable_name_problem,
    write_onboarding,
)
from .orchestration import OrchestrationError, Orchestrator
from .policy import (
    ProjectPolicy,
    evaluate_diff,
    evaluate_task,
    load_policy,
    load_policy_bytes,
)
from .project_registry import (
    ProjectRegistryError,
    read_optional_project_registry,
    read_project_registry,
)
from .scaffold import ScaffoldError, scaffold_project
from .spec_stage import merge_spec, parse_draft
from .task_spec import TaskSpecError, check_task_spec, draft_request, parse_task, render_prompt
from .trusted_exec import GH_OVERRIDE_ENV as _GH_OVERRIDE_ENV
from .trusted_exec import (
    STRICT,
    TRUST_MODES,
    use_gate_trust,
    use_trust_mode,
)
from .trusted_git import OVERRIDE_ENV as _GIT_OVERRIDE_ENV
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


def _check_diff(args: argparse.Namespace) -> int:
    """The diff policy (`evaluate_diff`, as `inspect-diff` applies it on the Actions path) over
    the COMMITTED change `--head` brings onto `--base` -- what a push of `--head` publishes
    (Codex 4241434509: the cloud routine runs it right before pushing). Exit 0 only when the
    policy allows it and something changed; 2 otherwise, the reasons on stderr.

    The policy is the one COMMITTED on `--base` (Codex 4241614590), never the working-tree file:
    the agent being checked could loosen an uncommitted copy (drop `.github/**`, raise the caps)
    and the push would publish the forbidden commit without that edit. The baseline forbidden
    paths apply whatever the base policy lists (`load_policy_bytes`)."""
    base = _verified_base(args.repository, args.project, args.base)
    policy = _committed_policy(args.repository, base, args.config, args.base)
    if policy.provider != "github" or policy.project_id != args.project:
        raise ValueError(
            f"the policy committed on {args.base} is not {args.project}'s (provider "
            f"{policy.provider!r}, project {policy.project_id!r}); not concluding"
        )
    snapshot = collect_commit_diff(args.repository, base, args.head)
    # Codex 4242095558: the caps apply to the larger of the end state and the per-commit sums,
    # so a large commit a later one shrank still counts (`collect_commit_diff`).
    decision = evaluate_diff(
        snapshot.paths,
        snapshot.cap_added_lines,
        snapshot.cap_deleted_lines,
        policy,
        patch_bytes=snapshot.cap_patch_bytes,
    )
    document = decision.as_dict()
    document["diff"] = {
        "base": args.base,
        "baseSha": base,
        "head": args.head,
        "files": len(snapshot.paths),
        "addedLines": snapshot.cap_added_lines,
        "deletedLines": snapshot.cap_deleted_lines,
        "patchBytes": snapshot.cap_patch_bytes,
        "net": {
            "addedLines": snapshot.added_lines,
            "deletedLines": snapshot.deleted_lines,
            "patchBytes": len(snapshot.patch),
        },
        "history": {
            "addedLines": snapshot.history_added_lines,
            "deletedLines": snapshot.history_deleted_lines,
            "patchBytes": snapshot.history_patch_bytes,
        },
        "paths": list(snapshot.paths),
    }
    _write(document, args.output)
    changed = snapshot.paths if snapshot.net_paths is None else snapshot.net_paths
    if decision.allowed and changed:
        _status(f"diff policy passed: {len(snapshot.paths)} file(s) changed")
        return 0
    reasons = list(decision.reasons) if not decision.allowed else []
    if not changed:
        reasons.append(f"no files changed: {args.head} adds nothing to {args.base}")
    _status("REFUSED: check-diff rejected the change: " + "; ".join(reasons))
    return 2


_FULL_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _verified_base(repository: str, project: str, base: str) -> str:
    """The commit `base` names in `repository`, PROVEN to be the head of `project`'s default
    branch as GitHub reports it now (Codex 4241839926). `--base origin/<default>` is a local
    ref: the agent being checked can point it at any commit -- one whose policy it loosened,
    or one that already holds the forbidden change -- with a single `git update-ref`, and the
    policy and the diff would then be read from there. The default branch and its head come
    from the trusted `gh` (`run_gh`); a commit that is not that head, or an answer that cannot
    be read, is an error (exit 2), never a fallback to the local ref. Callers read the policy
    and diff from the returned object name, not from `base`."""
    local = resolve_commit(repository, base)
    document = json.loads(run_gh(["api", f"repos/{project}"]) or "null")
    if not isinstance(document, dict):
        raise ValueError(f"the repository {project} could not be read from GitHub")
    full_name = document.get("full_name")
    branch = document.get("default_branch")
    if not isinstance(full_name, str) or full_name.lower() != project.lower():
        raise ValueError(f"GitHub answered for {full_name!r}, not {project}; not concluding")
    if not isinstance(branch, str) or not branch:
        raise ValueError(f"the default branch of {project} could not be read from GitHub")
    ref = json.loads(
        run_gh(["api", f"repos/{project}/git/ref/heads/{quote(branch, safe='/')}"]) or "null"
    )
    target = ref.get("object") if isinstance(ref, dict) else None
    remote = target.get("sha") if isinstance(target, dict) else None
    if (
        not isinstance(ref, dict)
        or ref.get("ref") != f"refs/heads/{branch}"
        or not isinstance(target, dict)
        or target.get("type") != "commit"
        or not isinstance(remote, str)
        or not _FULL_SHA.fullmatch(remote)
    ):
        raise ValueError(f"the head of {project}'s {branch} could not be read from GitHub")
    if local != remote:
        raise ValueError(
            f"--base {base} is {local[:12]}, but the default branch {branch} of {project} is at "
            f"{remote[:12]} on GitHub; fetch it again (`git fetch origin "
            f"{shlex.quote('refs/heads/' + branch)}`) and use that; not concluding"
        )
    _status(f"base verified: {base} is {project}'s {branch} head {remote[:12]} on GitHub")
    return remote


def _committed_policy(
    repository: str, base: str, config: str, label: str | None = None
) -> ProjectPolicy:
    """The policy file `config` (a path inside `repository`, relative to its top level) as the
    commit `base` holds it (`label` names it in messages). Missing or unparseable: an error
    (exit 2), never a looser policy."""
    shown = label or base
    path = committed_path(repository, config)
    raw = read_committed_file(repository, base, path, label=shown)
    try:
        return load_policy_bytes(raw)
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        raise ValueError(f"the policy {path} committed on {shown} is not usable: {exc}") from exc


#: `check-request` exit codes beside 0 (ready) and 2 (could not be verified).
REQUEST_INVALID = 3
DEPENDENCY_OPEN = 4
#: The title or body differs from the content digest given with `--expect-digest`.
REQUEST_CHANGED = 5
#: The issue is no longer open.
ISSUE_CLOSED = 6


def request_digest(title: str, body: str) -> str:
    """The content digest of a work request: its title and body exactly as GitHub returns them
    (labels excluded -- they are authorized separately). `check-request` prints it; passed back
    with `--expect-digest` it proves the request is still the one the work was built from
    (Codex 4241614582)."""
    canonical = json.dumps([title, body], ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


#: `check-request --authorize-labels`: a label of the approval set was not last applied by a
#: writer, was removed, or the issue was edited after it was applied.
LABELS_NOT_AUTHORIZED = 7
#: `check-request --authorize-labels`: the issue (its labels, history or update time) kept changing
#: while it was checked, so no single snapshot passed every check.
REQUEST_UNSTABLE = 8
#: `check-request --authorize-labels`: the issue has an open pull request (opened, linked or
#: cross-referenced after the claim: Codex 4243193102) -- pushing would duplicate that work.
PULL_REQUEST_OPEN = 9
#: How many fresh snapshots `check-request --authorize-labels` judges before giving up on an issue
#: that changes under it (a changed title or body is never retried: it fails at once).
CHECK_REQUEST_ATTEMPTS = 3


def _read_issue(project: str, issue: int) -> dict[str, Any]:
    document = json.loads(run_gh(["api", f"repos/{project}/issues/{issue}"]) or "null")
    if not isinstance(document, dict) or "pull_request" in document:
        raise ValueError(f"#{issue} is not a readable issue of {project}")
    return document


def _issue_snapshot(document: dict[str, Any], events: list[dict]) -> dict[str, Any]:
    """What must not move between the two reads of `check-request --authorize-labels`: the
    issue's update time, state, content digest, current labels and its whole event history."""
    title, body, labels = _request_fields("github", document)
    updated = document.get("updated_at")
    if not isinstance(updated, str) or not updated:
        raise ValueError(f"#{document.get('number')}: no readable updated_at; not concluding")
    return {
        "updated_at": updated,
        "state": document.get("state"),
        "content digest": request_digest(title, body),
        "labels": sorted(name.casefold() for name in labels),
        "events": issue_event_keys(events),
    }


def _open_pr_keys(project: str, issue: int) -> list[str]:
    """The issue's open pull requests (`open_prs_for_issue`: the inventory `claim` reads,
    linked and cross-referenced PRs included), as sorted URLs. Unreadable raises: never "none"."""
    prs = open_prs_for_issue(project, issue, run_gh)
    return sorted(str(pr.get("url") or f"#{pr.get('number')}") for pr in prs)


def _refuse_open_prs(issue: int, prs: list[str], result: dict[str, Any]) -> tuple[int, str]:
    listed = ", ".join(prs)
    result.update(ready=False, openPullRequests=prs)
    result["reasons"] = [f"the issue already has an open pull request: {listed}"]
    return PULL_REQUEST_OPEN, f"PR OPEN: #{issue} already has an open pull request: {listed}"


def _dependency_states(project: str, dependencies: Iterable[int]) -> dict[str, dict[str, str]]:
    """{"N": {"state", "updatedAt"}} for each dependency issue, read now. The state must be open
    or closed (anything else is an error, never "closed"); the update time is kept so a
    dependency reopened and closed again between two reads still reads as a change (Codex
    4242373526)."""
    observed: dict[str, dict[str, str]] = {}
    for dependency in dependencies:
        document = json.loads(run_gh(["api", f"repos/{project}/issues/{dependency}"]))
        state = document.get("state") if isinstance(document, dict) else None
        if state not in ("open", "closed"):
            raise ValueError(f"dependency #{dependency}: unreadable state {state!r}")
        updated = document.get("updated_at")
        observed[str(dependency)] = {
            "state": state,
            "updatedAt": updated if isinstance(updated, str) else "",
        }
    return observed


def _judge_request(
    args: argparse.Namespace,
    policy: ProjectPolicy,
    document: dict[str, Any],
    events: list[dict] | None,
    approval_labels: list[str],
) -> tuple[int, dict[str, Any], str]:
    """One snapshot of the issue (`document`, and with `--authorize-labels` its `events`) judged
    by every check-request rule: (exit code, result document, status line). Nothing here reads
    the issue itself again; only the dependencies and the labelers' permissions are read."""
    state = document.get("state")
    if state not in ("open", "closed"):
        raise ValueError(f"#{args.issue}: unreadable state {state!r}")
    title, body, labels = _request_fields("github", document)
    digest = request_digest(title, body)
    result: dict[str, Any] = {
        "issue": args.issue,
        # The project is part of what the result attests: a consumer binding
        # work to this record must not accept a same-numbered issue from
        # another project.
        "project": args.project,
        "ready": False,
        "dependencies": [],
        "contentDigest": digest,
    }
    _status(f"content digest: {digest}")
    if state != "open":
        result["reasons"] = [f"issue #{args.issue} is {state}"]
        return ISSUE_CLOSED, result, f"CLOSED: #{args.issue} is {state}"
    if args.expect_digest is not None and args.expect_digest != digest:
        result["reasons"] = [
            f"the title or body changed: expected {args.expect_digest}, now {digest}"
        ]
        return (
            REQUEST_CHANGED,
            result,
            f"CHANGED: #{args.issue}: the title or body is not the request the work was built "
            f"from (expected {args.expect_digest}, now {digest})",
        )
    try:
        task = parse_task(title, body, labels)
    except TaskSpecError as exc:
        result["reasons"] = [f"not a valid work request: {exc}"]
        return REQUEST_INVALID, result, f"INVALID: #{args.issue}: {exc}"
    decision = evaluate_task(task, policy, "implement")
    result["dependencies"] = list(task.dependencies)
    if not decision.allowed:
        result["reasons"] = list(decision.reasons)
        return REQUEST_INVALID, result, f"INVALID: #{args.issue}: " + "; ".join(decision.reasons)
    if events is not None:
        verdict = authorize_labeler(args.project, args.issue, approval_labels, run_gh, events)
        result["authorization"] = verdict.as_dict()
        if not verdict.authorized:
            result["reasons"] = [verdict.reason]
            return LABELS_NOT_AUTHORIZED, result, f"NOT AUTHORIZED: #{args.issue}: {verdict.reason}"
    observed = _dependency_states(args.project, task.dependencies)
    result["dependencyStates"] = observed
    still_open = [n for n in task.dependencies if observed[str(n)]["state"] != "closed"]
    if still_open:
        listed = ", ".join(f"#{n}" for n in still_open)
        result["reasons"] = [f"dependency issue(s) not closed: {listed}"]
        result["openDependencies"] = still_open
        return (
            DEPENDENCY_OPEN,
            result,
            f"WAITING: #{args.issue}: dependency issue(s) not closed: {listed}",
        )
    result.update(ready=True, reasons=list(decision.reasons))
    what = "a valid work request with every dependency closed"
    if events is not None:
        what += " and every approval label last applied by a writer after the last edit"
    return 0, result, f"ready: #{args.issue} is {what}"


def _check_request(args: argparse.Namespace) -> int:
    """Is GitHub issue `--issue` a work request the Actions implement path would run (Codex
    4241434511)? Its body is parsed by the same validator (`parse_task`) and judged by the same
    policy (`evaluate_task`, mode implement) as `prepare-request`, and every dependency it names
    must be CLOSED, as the Actions "Require every dependency issue to be closed" step requires.
    Exit 0: ready. 3: not a valid or eligible request. 4: a dependency is still open. 5: the
    title or body is not the content `--expect-digest` names (it was edited: Codex 4241614582).
    6: the issue is not open. 2 (an error): anything that could not be read -- never "no
    dependencies". It prints the request's content digest on every run that reads the issue.
    With `--base`, the policy is the one committed there in `--repository`, not the working-tree
    file the agent could loosen (Codex 4241614590).

    With `--authorize-labels` it is the routine's ONE final gate (Codex 4242264478: a separate
    digest check and labeler check let an edit plus a label re-apply, or a reopened dependency,
    land between them and pass both). It reads the issue and its event history once, judges
    that one snapshot by every rule above AND by `authorize-labeler`'s (exit 7: a label of the
    approval set not last applied by a writer, removed, or applied before the last edit), then
    reads the issue, its history and every dependency again: the update time, state, content
    digest, labels, events and each dependency's state and update time must all be unchanged, or
    the snapshot is stale. Each of the two reads also reads the issue's open pull requests
    (`open_prs_for_issue`, linked and cross-referenced ones included: Codex 4243193102); any open
    PR in either read refuses (exit 9), and an unreadable inventory is an error (exit 2). A
    changed title or body fails at
    once (exit 5); any other change is judged afresh from a new snapshot, at most
    `CHECK_REQUEST_ATTEMPTS` times, then exit 8 (it would not hold still)."""
    authorizing = bool(args.authorize_labels)
    if args.label and not authorizing:
        raise ValueError("--label only applies with --authorize-labels")
    if args.base:
        base = _verified_base(args.repository, args.project, args.base)
        policy = _committed_policy(args.repository, base, args.config, args.base)
    else:
        policy = load_policy(args.config)
    if policy.provider != "github":
        raise ValueError("policy provider is not github")
    if policy.project_id != args.project:
        raise ValueError("policy project ID does not match --project")
    approval_labels = list(args.label or []) or [
        policy.implementation_label,
        policy.ready_label,
        policy.human_review_label,
    ]
    for attempt in range(1, CHECK_REQUEST_ATTEMPTS + 1):
        document = _read_issue(args.project, args.issue)
        events = issue_events(args.project, args.issue, run_gh) if authorizing else None
        code, result, message = _judge_request(args, policy, document, events, approval_labels)
        if code != 0 or events is None:
            break
        # Codex 4243193102: a PR opened (or linked) for the issue since the claim is work this
        # push would duplicate. The open-PR inventory is read with each snapshot, and is part of
        # what must hold still between the two reads.
        prs = _open_pr_keys(args.project, args.issue)
        if prs:
            code, message = _refuse_open_prs(args.issue, prs, result)
            break
        # The dependencies are part of the snapshot (Codex 4242373526): one reopened between
        # the reads is a change like any other, judged afresh on the next attempt.
        before = _issue_snapshot(document, events)
        before["dependencies"] = result["dependencyStates"]
        before["open pull requests"] = prs
        again = _read_issue(args.project, args.issue)
        after = _issue_snapshot(again, issue_events(args.project, args.issue, run_gh))
        after["dependencies"] = _dependency_states(args.project, result["dependencies"])
        after["open pull requests"] = _open_pr_keys(args.project, args.issue)
        if after["open pull requests"]:
            _status(f"#{args.issue} gained an open pull request while it was checked")
            code, message = _refuse_open_prs(args.issue, after["open pull requests"], result)
            break
        if before == after:
            result["stable"] = {"updatedAt": after["updated_at"], "attempts": attempt}
            message += f" (stable across two reads at {after['updated_at']})"
            break
        moved = [key for key in before if before[key] != after[key]]
        _status(f"#{args.issue} changed while it was checked ({', '.join(moved)})")
        result.update(ready=False, changedWhileChecking=moved)
        if before["content digest"] != after["content digest"]:
            code = REQUEST_CHANGED
            result["contentDigest"] = after["content digest"]
            result["reasons"] = [
                f"the title or body changed while it was checked: now {after['content digest']}"
            ]
            message = f"CHANGED: #{args.issue}: the title or body changed while it was checked"
            break
        code = REQUEST_UNSTABLE
        result["reasons"] = [f"the issue changed while it was checked: {', '.join(moved)}"]
        message = (
            f"UNSTABLE: #{args.issue} changed during each of {CHECK_REQUEST_ATTEMPTS} checks "
            f"({', '.join(moved)}); not concluding"
        )
    # When the judgement was made, for consumers that must refuse stale
    # authorization: labels, dependencies, and pull requests all move after
    # a check, so the result's age is part of what it attests.
    result["checkedAt"] = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
    if getattr(args, "attestation_key", None):
        # Dispatch consumes this result as its authorization gate: the mark
        # binds every field, checkedAt included, to the shared secret, so a
        # caller cannot author an acceptable result by hand.
        result[_ATTESTATION_FIELD] = _attest_document(
            result, _attestation_key_bytes(args.attestation_key)
        )
    _write(result, args.output)
    _status(message)
    return code


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


def _candidate_rel(args: argparse.Namespace, value: str, flag: str) -> Path:
    """`value` relative to the candidate repository, derived LEXICALLY.

    Never through ``Path.resolve``: following symlinks would let an
    uncommitted symlink planted at the configured path select another
    committed file, so containment is judged on the normalized spelling and
    the committed object at that exact path is what gets read.
    """
    repo_root = Path(os.path.abspath(args.candidate_repository))
    # A RELATIVE value is repository-relative by contract — a central
    # orchestrator addressing another checkout by path must not have its
    # config resolved against the process working directory.
    joined = value if os.path.isabs(value) else os.path.join(repo_root, value)
    candidate = Path(os.path.normpath(joined))
    try:
        return candidate.relative_to(repo_root)
    except ValueError:
        raise ContextBuilderError(
            f"{flag} must name a path inside the candidate repository; "
            "candidate paths read repository-owned configuration committed "
            "at the pinned revision"
        ) from None


def _committed_configuration(args: argparse.Namespace, pinned: str, value: str, flag: str) -> bytes:
    """The bytes of a configuration file as COMMITTED at the pinned revision."""
    rel = _candidate_rel(args, value, flag)
    try:
        return read_committed_file(
            args.candidate_repository, pinned, rel.as_posix(), label="the pinned tree"
        )
    except GitDiffError as error:
        raise ContextBuilderError(
            f"the {flag} configuration is not committed at the pinned revision; "
            "commit it so the packet is built under repository-owned definitions"
        ) from error


def _derived_verification(
    args: argparse.Namespace, request: dict, policy: ProjectPolicy
) -> VerificationEvidence:
    """Reviewer verification DERIVED from the candidate repository itself.

    The diff summary comes from ``collect_commit_diff`` over the resolved
    base and head commits and the gate report from ``run_gates`` run here,
    in the trusted caller — request JSON never impersonates pipeline
    evidence, the base is authenticated outside the candidate repository,
    and the gates run under the configuration committed at that base.
    """
    if not args.candidate_base or not args.candidate_head:
        raise ContextBuilderError(
            "--candidate-base and --candidate-head are required with --candidate-repository"
        )
    base = resolve_commit(args.candidate_repository, args.candidate_base)
    head = resolve_commit(args.candidate_repository, args.candidate_head)
    # The base is AUTHENTICATED against a repository the candidate cannot
    # write: every ref inside the candidate repository — remote-tracking
    # origin/<branch> included — is mutable by the producing run itself
    # (`git update-ref`), so a caller-chosen later ancestor could pose as
    # the review target and hide already-landed changes from the verified
    # diff. The review target therefore resolves in the caller's separately
    # maintained repository.
    if not getattr(args, "review_target_repository", None):
        raise ContextBuilderError(
            "--review-target-repository is required to derive reviewer "
            "verification: the trusted repository whose "
            f"origin/{policy.default_branch} defines the review base — a ref "
            "inside the candidate repository is candidate-mutable and "
            "authenticates nothing"
        )
    # A flag is a caller declaration; identity is not. The target must be a
    # SEPARATE object store: the candidate itself, a symlink alias of it, a
    # worktree sharing its store, or a repository inside the candidate's own
    # tree is candidate-mutable, and its refs would authenticate nothing.
    candidate_root = os.path.realpath(args.candidate_repository)
    target_root = os.path.realpath(args.review_target_repository)
    target_identity = repository_identity(args.review_target_repository)
    if (
        target_identity == repository_identity(args.candidate_repository)
        or target_identity == candidate_root
        or target_identity.startswith(candidate_root + os.sep)
        # Containment refuses in BOTH directions: a candidate checkout
        # nested inside the target's worktree can reach the parent's .git
        # and move its refs, so the "authoritative" base would be
        # candidate-mutable again.
        or candidate_root == target_root
        or candidate_root.startswith(target_root + os.sep)
    ):
        raise ContextBuilderError(
            "--review-target-repository must be a separate repository: the "
            "candidate itself, a symlink alias, a worktree sharing its object "
            "store, or a repository containing or contained by the candidate "
            "tree is candidate-mutable and authenticates nothing"
        )
    try:
        expected_base = resolve_commit(
            args.review_target_repository, f"origin/{policy.default_branch}"
        )
    except GitDiffError as error:
        raise ContextBuilderError(
            "the review target repository has no "
            f"origin/{policy.default_branch}; fetch the authoritative review "
            "target before deriving verification"
        ) from error
    if base != expected_base:
        raise ContextBuilderError(
            "--candidate-base must be the current authoritative review target "
            f"(origin/{policy.default_branch} in the review target repository "
            f"is {expected_base})"
        )
    # Gate definitions come from the AUTHENTICATED BASE, never the head: the
    # configuration committed at the head is itself part of the untrusted
    # candidate, so a candidate commit replacing the configured gates with
    # `true` would otherwise record a passing report under its own weakened
    # rules. The base SHA is content-addressed, so reading it from the
    # candidate's object database reads trusted bytes.
    config_rel = _candidate_rel(args, args.config, "--config")
    try:
        base_config = read_committed_file(
            args.candidate_repository, base, config_rel.as_posix(), label="the review base"
        )
    except GitDiffError as error:
        raise ContextBuilderError(
            "the gate configuration is not committed at the authenticated "
            "review base; derived gates run only repository-owned, "
            "base-trusted definitions"
        ) from error
    # The branch name that authenticated the base is itself policy read at
    # the candidate HEAD: a head that renames the default branch could re-aim
    # the resolution at a stale origin/<other> ref in the review target, so
    # the base-committed policy must agree on the branch.
    if load_policy_bytes(base_config).default_branch != policy.default_branch:
        raise ContextBuilderError(
            "the candidate head changes the policy's default branch; that "
            "re-aims base authentication, so land the configuration change "
            "through the human-controlled path before reviewing candidates "
            "against it"
        )
    # Candidate gates are candidate-controlled COMMANDS: run bare they
    # execute as the caller's OS user, and a disposable clone restricts
    # nothing outside itself (dotfiles, credentials, the original checkout
    # through /proc, background processes that survive the clone's
    # deletion). The caller must either name the isolation wrapper the
    # commands run under or explicitly accept host execution for a
    # candidate it already trusts.
    if not getattr(args, "gate_sandbox", None):
        raise ContextBuilderError(
            "--gate-sandbox is required: derived gates execute "
            "candidate-controlled commands, so name the isolation wrapper "
            "they run under (a container or sandbox command prefix), or pass "
            "--gate-sandbox trusted-host to accept execution on this host "
            "for a candidate you already trust"
        )
    if args.gate_sandbox == "trusted-host":
        sandbox_prefix: tuple[str, ...] = ()
    else:
        sandbox_prefix = tuple(shlex.split(args.gate_sandbox))
        if not sandbox_prefix:
            raise ContextBuilderError(
                "--gate-sandbox must name a non-empty wrapper command, or the literal trusted-host"
            )
    snapshot = collect_commit_diff(args.candidate_repository, base, head)
    # The reviewer reads the SERIES, not only its end state: a commit that
    # adds content a later commit removes leaves the endpoint tree diff
    # empty while a push of the head still publishes the blob, so the
    # evidence patch carries the per-commit patches and the digest binds
    # those bytes.
    series_patch = collect_series_patch(args.candidate_repository, base, head)
    # The caller's own checkout is not evidence: it can be dirty, carry
    # ignored files a gate would read, or have status output suppressed by
    # repository-local configuration — and a gate run leaves caches behind
    # that would fail the next derivation. Gates therefore run in a
    # disposable tree materialized from the object database at the candidate
    # head (no checkout, so no local smudge filter or hook runs), observing
    # exactly the committed content and leaving the caller's state untouched.
    # The tree's own origin/<default branch> is PINNED to the authenticated
    # base: the mirror would otherwise import the candidate's value for that
    # ref, and a gate diffing `origin/$GITHUB_BASE_REF...HEAD` against a
    # candidate-moved ref could see an empty diff while the packet's
    # separately computed diff is not.
    with derivation_tree(
        args.candidate_repository,
        head,
        pin_refs={f"refs/remotes/origin/{policy.default_branch}": base},
    ) as tree:
        gate_config = tree.parent / "gate-config-at-base.toml"
        gate_config.write_bytes(base_config)
        report = run_gates(
            gate_config,
            tree,
            # The ambient workflow's revision variables describe the
            # caller's checkout; gates in the isolated tree see the
            # candidate's instead. GITHUB_BASE_REF keeps Actions' BRANCH
            # NAME semantics — a gate diffing `origin/$GITHUB_BASE_REF...`
            # must find the ref — and the resolved base SHA travels in its
            # own variable.
            environment_overrides={
                "GITHUB_SHA": head,
                "GITHUB_BASE_REF": policy.default_branch,
                "AGENTIC_SDLC_BASE_SHA": base,
                "GITHUB_REF": None,
                "GITHUB_HEAD_REF": None,
            },
            command_prefix=sandbox_prefix,
        )
    # Wall-clock timings are not evidence, and they make everything bound to
    # the report nondeterministic: the rendered size, the budget accounting,
    # the evidence selection, and the packet digest would all differ between
    # the artifact build and the dispatch rebuild. Zeroed HERE, so derived
    # packets are deterministic end to end and the artifact comparison below
    # stays exact.
    report = {
        "passed": report.get("passed"),
        "gates": [{**gate, "durationSeconds": 0.0} for gate in report.get("gates", [])],
    }
    findings = request.get("priorFindings", [])
    if not isinstance(findings, list) or not all(isinstance(item, str) for item in findings):
        raise ContextBuilderError("packet request priorFindings must be an array of strings")
    # The reviewer reads the change itself, and reads EVERY byte the digest
    # binds: a replacement-character decode would render two distinct
    # non-UTF-8 changes identically, so bytes that are not UTF-8 travel
    # base64-encoded instead of lossily decoded.
    try:
        patch_text = series_patch.decode("utf-8")
        patch_encoding = "utf-8"
    except UnicodeDecodeError:
        patch_text = base64.b64encode(series_patch).decode("ascii")
        patch_encoding = "base64"
    return VerificationEvidence(
        head_sha=head,
        base_sha=base,
        diff_paths=snapshot.paths,
        added_lines=snapshot.added_lines,
        deleted_lines=snapshot.deleted_lines,
        history_added_lines=snapshot.history_added_lines,
        history_deleted_lines=snapshot.history_deleted_lines,
        patch_sha256=hashlib.sha256(series_patch).hexdigest(),
        gate_report=report,
        prior_findings=tuple(findings),
        patch=patch_text,
        patch_encoding=patch_encoding,
    )


_ATTESTATION_FIELD = "attestation"


def _attestation_key_bytes(path: str) -> bytes:
    """The shared attestation secret: at least 32 bytes of key material."""
    try:
        key = Path(path).read_bytes().strip()
    except OSError as error:
        raise ContextBuilderError(f"the attestation key could not be read: {error}") from None
    if len(key) < 32:
        raise ContextBuilderError(
            "the attestation key must hold at least 32 bytes of secret key material"
        )
    return key


def _attest_document(document: Mapping, key: bytes) -> str:
    """HMAC-SHA256 over the document's canonical JSON, attestation field excluded."""
    body = {name: value for name, value in document.items() if name != _ATTESTATION_FIELD}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hmac.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def _verify_attested_input(args: argparse.Namespace, document, what: str, producer: str) -> None:
    """Accept a caller-supplied trust artifact only under a valid attestation.

    These documents are plain JSON an invoker could author: without a mark
    from the shared secret, a fabricated run history or authorization result
    satisfies every structural check this CLI can run. The secret never
    travels in the artifact, so holding or reading a file confers no ability
    to mint one; only the trusted producer commands, run with the same key,
    emit documents dispatch accepts.
    """
    if not getattr(args, "attestation_key", None):
        raise ContextBuilderError(
            f"dispatch requires --attestation-key to verify the {what}; "
            f"produce the artifact with {producer} under the same key"
        )
    key = _attestation_key_bytes(args.attestation_key)
    mark = document.get(_ATTESTATION_FIELD) if isinstance(document, Mapping) else None
    if not isinstance(mark, str) or not hmac.compare_digest(mark, _attest_document(document, key)):
        raise ContextBuilderError(
            f"the {what} carries no valid attestation under --attestation-key; "
            f"regenerate it with {producer} and the same key"
        )


def _check_dispatch_authorization(args: argparse.Namespace, policy, packet) -> None:
    """Bind a write-capable dispatch to the trusted label-authorization gate.

    Request labels are caller-supplied text: a caller could spell the
    approval label names into the request without any authorized writer ever
    applying them to the work item. The `check-request --authorize-labels`
    result is the trusted record of that gate, so dispatch requires it and
    verifies it names this exact project, work item, and request content,
    passed its authorization, and held still across its two reads.
    """
    if not getattr(args, "authorization", None):
        raise ContextBuilderError(
            "write-capable dispatch requires --authorization: the "
            "check-request --authorize-labels result proving the approval "
            "labels were applied to the work item by authorized writers"
        )
    document = json.loads(Path(args.authorization).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ContextBuilderError("the authorization result must be a JSON object")
    # Structure proves nothing about provenance: the result is plain JSON an
    # invoker could author with every expected field, so it is accepted only
    # under a valid mark from the shared attestation key the trusted
    # check-request run signed it with.
    _verify_attested_input(
        args,
        document,
        "authorization result",
        "check-request --authorize-labels --attestation-key",
    )
    verdict = document.get("authorization")
    stable = document.get("stable")
    if (
        document.get("ready") is not True
        or not isinstance(verdict, dict)
        or verdict.get("authorized") is not True
        or not isinstance(stable, dict)
    ):
        raise ContextBuilderError(
            "the authorization result does not record a ready, label-authorized, "
            "stable check; re-run check-request --authorize-labels"
        )
    work_ref = f"{document.get('project')}#{document.get('issue')}"
    if document.get("project") != policy.project_id or work_ref != packet.work_ref:
        raise ContextBuilderError(
            f"the authorization result names {work_ref}, not the packet's "
            f"work item {packet.work_ref}; authorize the dispatched work item"
        )
    # The verdict must cover the POLICY's approval set, not a caller-selected
    # one: `check-request --authorize-labels --label harmless` reports ready
    # on an issue that merely carries the policy labels while its verdict
    # proves writers applied only "harmless".
    required_labels = {
        policy.implementation_label.casefold(),
        policy.ready_label.casefold(),
        policy.human_review_label.casefold(),
    }
    checked = verdict.get("labelers")
    checked_labels = (
        {name.casefold() for name in checked} if isinstance(checked, Mapping) else set()
    )
    if checked_labels != required_labels:
        raise ContextBuilderError(
            "the authorization verdict does not cover exactly the policy's "
            "approval labels; re-run check-request --authorize-labels without "
            "narrowing --label"
        )
    digest = request_digest(packet.task.title, packet.task.raw_body)
    if document.get("contentDigest") != digest:
        raise ContextBuilderError(
            "the authorization result was taken for different request content; "
            "re-run check-request --authorize-labels against the current title "
            "and body"
        )
    # A stale but genuine result can authorize work the CURRENT issue state
    # no longer permits (a removed label, a reopened dependency, a fresh
    # pull request), so the result must be recent.
    checked_at = document.get("checkedAt")
    try:
        checked = datetime.datetime.fromisoformat(str(checked_at))
    except (TypeError, ValueError):
        checked = None
    if checked is None or checked.tzinfo is None:
        raise ContextBuilderError(
            "the authorization result records no checkedAt instant; re-run "
            "check-request --authorize-labels with the current CLI"
        )
    age = (datetime.datetime.now(datetime.UTC) - checked).total_seconds()
    if age < 0 or age > args.authorization_max_age_seconds:
        raise ContextBuilderError(
            "the authorization result is older than "
            f"{args.authorization_max_age_seconds} seconds (or from the "
            "future); re-run check-request --authorize-labels and dispatch "
            "promptly"
        )


def _readback_repository_records(repository: str, pin: str, records: tuple) -> None:
    """Read every pinned repository payload back from the trusted tree.

    The record's self-declared content hash proves nothing about the TREE: a
    fabricated payload with a recomputed hash would hand the agent
    attacker-chosen source under a self-consistent citation, so each record
    pinned at `pin` is read back from that commit and compared through the
    adapter's own sanitization (a file legitimately containing boundary
    marker text is not misread as fabricated). Off-pin or malformed records
    are left to the builder, which rejects them itself.
    """
    for record in records:
        if record.source_type != "git-repository":
            continue
        if record.revision != pin:
            continue  # the builder rejects off-pin records itself
        suffix = "@" + record.revision
        if not record.external_id.endswith(suffix):
            continue  # the builder rejects the malformed shape itself
        path = record.external_id[: -len(suffix)]
        # Paths are retrieval-controlled text: a token-shaped filename is
        # withheld from every error this readback can raise, exactly as
        # the builder withholds it from exclusion notes.
        shown = _safe_exclusion_path(path)
        try:
            committed = read_committed_file(repository, pin, path, label="the pinned tree")
        except GitDiffError as error:
            raise ContextBuilderError(
                f"repository evidence for {shown} could not be read from "
                "the pinned candidate tree; re-retrieve it from the "
                "pinned revision"
            ) from error
        try:
            expected = sanitize_payload(committed.decode("utf-8"))
        except UnicodeDecodeError:
            expected = None
        if expected != record.payload:
            raise ContextBuilderError(
                f"repository evidence for {shown} does not match the pinned "
                "candidate tree; re-retrieve it from the pinned revision"
            )


def _build_packet_from_request(
    args: argparse.Namespace, *, trusted_verification_only: bool = False
):
    """Rebuild and validate a packet from its request inputs.

    Shared by ``context-packet`` and ``dispatch-packet``, so dispatching runs
    the same evidence authorization, verification requirement, and budgeting
    that produced the packet artifact.
    """
    request = json.loads(Path(args.request).read_text(encoding="utf-8"))
    if not isinstance(request, dict):
        raise ContextBuilderError("packet request must be a JSON object")
    repository_revision = request.get("repositoryRevision", "")
    if not isinstance(repository_revision, str):
        raise ContextBuilderError("packet request repositoryRevision must be a string")
    if getattr(args, "candidate_repository", None):
        # The policy that shapes the packet — limits, forbidden paths, task
        # policy — is read from the COMMITTED candidate tree, exactly like
        # the gate configuration: an uncommitted caller-side edit must not
        # loosen the packet while the gate report presents the committed
        # definitions. The pin is role-independent: reviewer derivations name
        # --candidate-head, builder paths pin repositoryRevision.
        pin_source = args.candidate_head or repository_revision
        if not pin_source:
            raise ContextBuilderError(
                "--candidate-repository requires --candidate-head or a pinned "
                "repositoryRevision to locate the committed configuration"
            )
        pinned = resolve_commit(args.candidate_repository, pin_source)
        policy = load_policy_bytes(_committed_configuration(args, pinned, args.config, "--config"))
        # The mission registry and knowledge sources are the same
        # human-reviewed, baseline-forbidden configuration the policy is: an
        # uncommitted edit must not widen a consumer mission or a source's
        # permissions while the artifact and rebuild agree on it.
        if args.missions:
            registry = load_registry_bytes(
                _committed_configuration(args, pinned, args.missions, "--missions"), policy
            )
        else:
            registry = load_registry(None, policy)
        sources = {
            source.source_id: source
            for source in load_sources_bytes(
                _committed_configuration(args, pinned, args.knowledge, "--knowledge")
            )
        }
    else:
        policy = load_policy(args.config)
        registry = load_registry(args.missions, policy)
        sources = {source.source_id: source for source in load_sources(args.knowledge)}

    def _request_text(key: str) -> str:
        value = request.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ContextBuilderError(f"packet request field {key} must be a non-empty string")
        return value

    task_entry = request.get("task")
    if not isinstance(task_entry, dict):
        raise ContextBuilderError("packet request task must be an object")
    labels = task_entry.get("labels", [])
    if not isinstance(labels, list) or not all(isinstance(item, str) for item in labels):
        raise ContextBuilderError("packet request task labels must be an array of strings")
    title = task_entry.get("title")
    body = task_entry.get("body")
    if not isinstance(title, str) or not isinstance(body, str):
        raise ContextBuilderError("packet request task title and body must be strings")
    task = parse_task(title, body, tuple(labels))

    records_entry = request.get("records", [])
    if not isinstance(records_entry, list):
        raise ContextBuilderError("packet request records must be an array")
    records = tuple(record_from_dict(entry) for entry in records_entry)
    failures = request.get("retrievalFailures", [])
    if not isinstance(failures, list) or not all(isinstance(item, str) for item in failures):
        raise ContextBuilderError("packet request retrievalFailures must be an array of strings")
    mission = registry.get(_request_text("missionId"))
    role = mission_role(mission)
    verification = None
    if getattr(args, "candidate_repository", None):
        if request.get("verification") is not None:
            raise ContextBuilderError(
                "the packet request must not carry verification when the CLI "
                "derives it from the candidate repository"
            )
        if role == "reviewer":
            verification = _derived_verification(args, request, policy)
            pin = verification.head_sha
        else:
            # Builders and testers must not receive verification evidence,
            # but their repository records still need a trusted tree: the
            # candidate repository verifies the request's pinned revision.
            if args.candidate_base or args.candidate_head:
                raise ContextBuilderError(
                    "--candidate-base and --candidate-head derive reviewer "
                    f"verification; a {role} mission takes --candidate-repository "
                    "alone"
                )
            if (
                not repository_revision
                or resolve_commit(args.candidate_repository, repository_revision)
                != repository_revision
            ):
                raise ContextBuilderError(
                    "repositoryRevision must be the full commit SHA of the "
                    "trusted pinned tree in the candidate repository"
                )
            # The pin must be the CURRENT trusted checkout, not any
            # resolvable historical SHA: policy, missions, knowledge and
            # repository evidence all load from the pin, so a request naming
            # an older revision would dispatch a write-capable envelope
            # under stale, possibly weaker configuration and source.
            current = resolve_commit(args.candidate_repository, "HEAD")
            if repository_revision != current:
                raise ContextBuilderError(
                    "repositoryRevision must be the candidate repository's "
                    f"current checkout ({current}); a historical pin would "
                    "dispatch under stale configuration and source"
                )
            pin = repository_revision
        _readback_repository_records(args.candidate_repository, pin, records)
    elif request.get("verification") is not None:
        if trusted_verification_only:
            # Schema-valid request JSON is not pipeline evidence: a dispatch
            # could otherwise render a fabricated gate report or diff summary
            # beneath the trusted-pipeline heading.
            raise ContextBuilderError(
                "dispatch derives reviewer verification from the candidate "
                "repository; pass --candidate-repository, --candidate-base and "
                "--candidate-head instead of request verification"
            )
        verification = verification_from_dict(request["verification"])
    elif trusted_verification_only:
        # Every dispatch pins its configuration, evidence or not: a request
        # with no records would otherwise read policy, missions, and
        # knowledge from the mutable working tree, and a repository record's
        # self-declared content hash proves nothing about any tree without
        # readback.
        raise ContextBuilderError(
            "dispatch-packet requires --candidate-repository: every dispatch "
            "pins repository-owned configuration at the trusted revision, and "
            "pinned payloads are read back from it"
        )
    if trusted_verification_only:
        # Only repository evidence has a trusted readback today: every other
        # source type would dispatch exactly the payload the request chose,
        # under a self-consistent hash and a copied scope, so it is refused
        # on this path until an attested retrieval exists for it.
        unattested = sorted(
            {record.source_type for record in records if record.source_type != "git-repository"}
        )
        if unattested:
            raise ContextBuilderError(
                "dispatch accepts repository evidence only (read back from the "
                "pinned tree); no attested retrieval exists for "
                + ", ".join(unattested)
                + " records — retrieve them through the trusted pipeline, not "
                "request JSON"
            )
    packet = build_context_packet(
        mission,
        task,
        policy,
        work_ref=_request_text("workRef"),
        records=records,
        sources=sources,
        verification=verification,
        budget_bytes=request.get("budgetBytes", DEFAULT_PACKET_BYTES),
        retrieval_failures=tuple(failures),
        repository_revision=repository_revision,
    )
    return registry, mission, packet, policy


def _context_packet(args: argparse.Namespace) -> int:
    _registry, _mission, packet, _policy = _build_packet_from_request(args)
    _write(packet.as_dict(), args.output)
    return 0


def _dispatch_packet(args: argparse.Namespace) -> int:
    """Dispatch through the packet contract, never around it.

    The packet is rebuilt and re-validated from the same request inputs that
    produced the artifact, so dispatching runs evidence authorization, the
    reviewer-verification requirement, and the budget checks; the resulting
    envelope carries the packet digest as ``contextPackDigest`` and the
    rendered packet as its prompt.
    """
    registry, mission, packet, policy = _build_packet_from_request(
        args, trusted_verification_only=True
    )
    if mission_role(mission) != "reviewer":
        # The official dispatch path binds write-capable work to the same
        # eligibility decision the implementation-readiness path enforces: a
        # request without the approval labels, or matching a forbidden task
        # pattern, must not reach an envelope just because it came in as a
        # packet.
        decision = evaluate_task(packet.task, policy, "implement")
        if not decision.allowed:
            raise ContextBuilderError(
                "dispatch blocked by task policy: " + "; ".join(decision.reasons)
            )
        # evaluate_task proves only that the configured label NAMES appear in
        # the caller-supplied request: approval itself is the trusted
        # `check-request --authorize-labels` gate, whose result must name
        # this exact work item and content.
        _check_dispatch_authorization(args, policy, packet)
    if args.packet:
        artifact = json.loads(Path(args.packet).read_text(encoding="utf-8"))

        # The WHOLE document is compared, never just its digest field, and
        # through canonical JSON encodings: Python equality conflates JSON
        # booleans and numbers (False == 0), which would let a schema-invalid
        # artifact read as matching.
        def _encoded(document: object) -> str:
            return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

        # Derived gate reports carry zeroed durations, so every rebuild —
        # derived or not — is deterministic and the comparison stays EXACT,
        # digest field included: a digest-inconsistent audit artifact is
        # rejected whatever path produced it.
        if _encoded(artifact) != _encoded(packet.as_dict()):
            raise ContextBuilderError(
                "the packet artifact does not match the packet rebuilt from the "
                "request; rebuild the packet before dispatching"
            )
    # The roster is repository-owned configuration like the policy and the
    # mission registry: an uncommitted caller-side roster could relabel a
    # producing agent under a fresh agentId to evade the history exclusions,
    # or route authorized write work to an undeclared executor — and the
    # resulting envelope would then be SIGNED as a trusted dispatch. It is
    # read from the committed tree at the packet's pinned revision.
    agents = load_agents(
        json.loads(
            _committed_configuration(
                args, packet.repository_revision, args.agents, "--agents"
            ).decode("utf-8")
        )
    )
    history: dict[str, list[str]] = {}
    producing_agents: set[str] = set()
    unit_runs: tuple = ()
    if args.history:
        # The history is the ORCHESTRATOR STATE, not a bare mission→agent
        # map: a caller-authored map could record "" (or any other agent) for
        # a mission the selected reviewer actually performed. The state is
        # replayed through the orchestrator's own validation, and the run
        # records of THIS work item — their mission and agent identity —
        # define the exclusions.
        document = json.loads(Path(args.history).read_text(encoding="utf-8"))
        # ``Orchestrator.from_dict`` validates shape, not provenance: a
        # fabricated state could record an innocent agent as the producer
        # and omit the real one, so the state is accepted only under a valid
        # mark from the key the trusted ``orchestrate`` runs signed it with.
        _verify_attested_input(args, document, "run history", "orchestrate --attestation-key")
        engine = Orchestrator.from_dict(document)
        try:
            unit = engine.get(packet.work_ref)
        except OrchestrationError as error:
            raise MissionError(
                f"the run history records no work unit {packet.work_ref}; pass "
                "the orchestrator state that dispatched this work item"
            ) from error
        unit_runs = tuple(unit.runs)
        for run in unit_runs:
            # EVERY agent that performed the mission is excluded, not only
            # the last assignee: a work item can carry several runs of one
            # producing mission by different agents.
            agents_for = history.setdefault(run.mission_id, [])
            if run.agent_id not in agents_for:
                agents_for.append(run.agent_id)
            # Producing status comes from the run's PERSISTED kind, never a
            # registry lookup: a mission removed or rewritten after the run
            # must not launder its agents back into review eligibility.
            if run.kind in ("implementation", "repair"):
                producing_agents.add(run.agent_id)
    if mission_role(mission) == "reviewer":
        # A review necessarily follows a producing run, so an absent history
        # — or one recording no producing run for this work item — is not
        # authoritative: with nothing to exclude, the roster's first eligible
        # agent could review its own earlier work despite the mission's
        # independence contract.
        produced = [other for other in mission.independent_of if other in history]
        if not args.history or not produced:
            raise MissionError(
                "reviewer dispatch requires --history: the orchestrator state "
                "recording the producing runs of this work item (none of the "
                "missions this review is independent of has a recorded run)"
            )
        # The state must be the history of THIS candidate: a genuine but
        # older state from before a repair would omit the agent who produced
        # the head, so some producing run must record the candidate head as
        # the commit it produced.
        head_sha = packet.verification.head_sha if packet.verification else ""
        if not any(
            run.kind in ("implementation", "repair") and run.commit_sha == head_sha
            for run in unit_runs
        ):
            raise MissionError(
                "the run history records no producing run for candidate head "
                f"{head_sha}; finish the producing run with its commit_sha so "
                "review exclusions bind to the candidate under review"
            )
    # Roster order defines fallbacks: an eligible agent whose declared
    # context window cannot fit the packet is skipped for the next eligible
    # one instead of blocking a dispatch a fallback could carry.
    roster = list(agents)
    reviewer = mission_role(mission) == "reviewer"
    while True:
        agent = registry.select_agent(
            mission.mission_id,
            tuple(roster),
            history=history,
            excluded_agents=frozenset(producing_agents) if reviewer else frozenset(),
        )
        try:
            envelope = dispatch_envelope_for_packet(
                packet, mission, agent, input_refs=tuple(args.input_ref)
            )
            break
        except ContextBuilderError as error:
            if "context window" not in str(error):
                raise
            roster = [entry for entry in roster if entry.agent_id != agent.agent_id]
            if not roster:
                raise ContextBuilderError(
                    f"{error}; no other eligible agent declares a sufficient context window"
                ) from error
    if getattr(args, "attestation_key", None):
        # The envelope seeds the attested run history: `orchestrate
        # start-run` under the same key accepts only envelopes a trusted
        # dispatch signed, so run attribution can never be hand-authored.
        envelope[_ATTESTATION_FIELD] = _attest_document(
            envelope, _attestation_key_bytes(args.attestation_key)
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


#: The run kinds each packet role's dispatches may seed: builders produce,
#: testers verify, reviewers review. A producing envelope recorded under a
#: non-producing kind would launder its agent past review exclusions.
_ROLE_RUN_KINDS = {
    "builder": frozenset({"implementation", "repair"}),
    "tester": frozenset({"verification"}),
    "reviewer": frozenset({"review"}),
}


def _require_attested_envelope(envelope, key: bytes, *, unit: str, kind: str) -> None:
    """An attested state records only runs a trusted dispatch produced.

    The envelope names the run's agent and mission — exactly what review
    exclusions are later derived from — so a hand-authored envelope must not
    seed the state: its attestation must verify under the shared key, and its
    self-digest must match its content so a signed envelope cannot be
    re-labeled either. The envelope must also be FOR this record: a genuine
    envelope replayed into another work item, or recorded under a kind its
    packet role does not permit, would fabricate run attribution exactly like
    a forged one.
    """
    mark = envelope.get(_ATTESTATION_FIELD) if isinstance(envelope, Mapping) else None
    if not isinstance(mark, str) or not hmac.compare_digest(mark, _attest_document(envelope, key)):
        raise OrchestrationError(
            "the dispatch envelope carries no valid attestation under "
            "--attestation-key; produce it with dispatch-packet "
            "--attestation-key and the same key"
        )
    body = {
        name: value
        for name, value in envelope.items()
        if name not in (_ATTESTATION_FIELD, "envelopeSha256")
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != envelope.get("envelopeSha256"):
        raise OrchestrationError(
            "the dispatch envelope's envelopeSha256 does not match its "
            "content; regenerate the envelope with dispatch-packet"
        )
    if envelope.get("workRef") != unit:
        raise OrchestrationError(
            f"the dispatch envelope names work item {envelope.get('workRef')!r}, "
            f"not --unit {unit!r}; an attested run records only the envelope's "
            "own work item"
        )
    permitted = _ROLE_RUN_KINDS.get(str(envelope.get("packetRole") or ""), frozenset())
    if kind not in permitted:
        raise OrchestrationError(
            f"the dispatch envelope's packet role "
            f"{envelope.get('packetRole')!r} does not permit a {kind} run; an "
            "attested run records only the kind its dispatch declared"
        )


def _orchestrate(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    key = (
        _attestation_key_bytes(args.attestation_key)
        if getattr(args, "attestation_key", None)
        else None
    )
    if state_path.is_file():
        document = json.loads(state_path.read_text(encoding="utf-8"))
        if key is not None:
            # An attested pipeline round-trips its own marks: state edited
            # outside orchestrate, or written under another key, is refused
            # here exactly as dispatch would refuse it.
            mark = document.get(_ATTESTATION_FIELD) if isinstance(document, dict) else None
            if not isinstance(mark, str) or not hmac.compare_digest(
                mark, _attest_document(document, key)
            ):
                raise OrchestrationError(
                    "the orchestration state carries no valid attestation "
                    "under --attestation-key; it was edited outside "
                    "orchestrate or written under a different key"
                )
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
        if key is not None:
            _require_attested_envelope(envelope, key, unit=args.unit, kind=args.kind)
        engine.start_run(
            args.unit,
            args.kind,
            envelope,
            event_key=args.event_key,
            timestamp=args.timestamp,
        )
    elif args.action == "finish-run":
        # A completion value is caller input: in an attested pipeline the
        # recorded commit must carry COMPLETION PROVENANCE — the execution
        # harness's attest-completion record binding this unit, run and
        # commit, written when the run produced it. Mere existence of the
        # commit proves nothing about who produced it, and an innocent
        # agent's genuinely signed run finished at another agent's head
        # would otherwise exclude the wrong reviewer.
        if key is not None and args.commit_sha:
            if not args.completion:
                raise OrchestrationError(
                    "an attested finish-run with --commit-sha requires "
                    "--completion: the attest-completion record binding this "
                    "unit, run and commit, written by the execution harness "
                    "at run completion"
                )
            completion = json.loads(Path(args.completion).read_text(encoding="utf-8"))
            mark = completion.get(_ATTESTATION_FIELD) if isinstance(completion, dict) else None
            if not isinstance(mark, str) or not hmac.compare_digest(
                mark, _attest_document(completion, key)
            ):
                raise OrchestrationError(
                    "the completion record carries no valid attestation under "
                    "--attestation-key; produce it with attest-completion and "
                    "the same key"
                )
            bound = (
                completion.get("unit"),
                completion.get("runId"),
                completion.get("commitSha"),
            )
            if bound != (args.unit, args.run_id, args.commit_sha):
                raise OrchestrationError(
                    "the completion record does not bind this exact unit, run "
                    "and commit; regenerate it with attest-completion for the "
                    "run that produced the commit"
                )
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
    state_document = engine.as_dict()
    if key is not None:
        state_document[_ATTESTATION_FIELD] = _attest_document(state_document, key)
    _write(state_document, str(state_path))
    if args.status_output:
        Path(args.status_output).write_text(engine.status_document(args.unit), encoding="utf-8")
    return 0


def _attest_completion(args: argparse.Namespace) -> int:
    """Record WHICH commit a run produced, signed at the moment it happened.

    Written by the execution harness — the operator-side component that
    actually watched the agent produce the commit — never by the agent
    itself, which holds no key. ``orchestrate finish-run`` under the same key
    accepts a commit only with this record, so a later caller cannot finish
    an innocent agent's run at another agent's head. The commit must exist
    in the trusted checkout when the record is written.
    """
    key = _attestation_key_bytes(args.attestation_key)
    if resolve_commit(args.commit_repository, args.commit_sha) != args.commit_sha:
        raise OrchestrationError(
            "--commit-sha must be the full object name of a commit in --commit-repository"
        )
    document = {
        "schemaVersion": 1,
        "unit": args.unit,
        "runId": args.run_id,
        "commitSha": args.commit_sha,
        "recordedAt": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
    }
    document[_ATTESTATION_FIELD] = _attest_document(document, key)
    _write(document, args.output)
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
        publisher_bot_login=args.publisher_bot_login,
    )
    _write(
        {"created": [str(path.relative_to(destination)) for path in created]},
        args.output,
    )
    return 0


def _onboard(args: argparse.Namespace) -> int:
    destination = Path(args.destination).resolve()
    # Validate CLI input before any file is written or anything remote is read or changed
    # (Codex 4242545209: `--var '=value'` used to get as far as `gh variable set ''`).
    assignments: dict[str, str] = {}
    for item in args.var or ():
        key, sep, value = item.partition("=")
        if not sep:
            raise OnboardError(f"--var expects KEY=VALUE, got {item!r}")
        problem = variable_name_problem(key)
        if problem:
            raise OnboardError(f"--var {item!r}: the variable name {problem}")
        assignments[key] = value
    # Codex 4242753917: the Publisher App's bot, the only App bot whose lease markers count.
    # Not given: a forced re-onboard keeps the installed policy's.
    bot_login = args.publisher_bot_login
    if bot_login is None:
        bot_login = installed_bot_login(destination)
    elif publisher_bot_login_problem(bot_login):
        raise OnboardError(f"--publisher-bot-login {publisher_bot_login_problem(bot_login)}")
    if bot_login and args.apply:
        assignments.setdefault(LEASE_BOT_LOGINS_ENV, bot_login)
    # The trust mode (strict: only executables root alone can replace; local: a developer's
    # own machine). Not given: a forced re-onboard keeps the installed policy's, else strict.
    trust = args.trust_mode or installed_trust_mode(destination) or STRICT
    use_trust_mode(trust)  # this run's own gh (--apply) and git resolve under it too
    # Codex 4242753911: the hooks run a vetted interpreter, chosen now and pinned by path.
    hook_python = trusted_hook_python(args.hook_python, trust)
    # The policy must name the repository as GitHub spells it ($GITHUB_REPOSITORY, compared
    # exactly by prepare-request); --apply reads it anyway. Offline, doctor checks the spelling.
    project_id = canonical_project_id(args.project_id) if args.apply else args.project_id
    if project_id.casefold() != args.project_id.casefold():
        # Codex 4239868019: a renamed or transferred repository is onboarded under its new name.
        print(
            f"NOTE: GitHub answers {args.project_id} as {project_id} (renamed or transferred); "
            f"onboarding as {project_id}, the name every workflow run passes as "
            "$GITHUB_REPOSITORY.",
            file=sys.stderr,
        )
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
    # Codex 4243193100 / 4243386518: required approvals. Not given: in the Actions modes 1 (the
    # Publisher App authors the PR, so the owner can approve it); in cloud-routine mode, with
    # --apply, 1 when another collaborator with write access can approve (the routine opens the
    # PR as the owner, who cannot approve their own), else 0; cloud-routine offline, 0.
    approvals = args.required_approvals
    if approvals is None:
        cloud = args.implementer == "cloud-routine"
        if args.apply or not cloud:
            approvals, why = default_required_approvals(project_id, run_gh, cloud=cloud)
        else:
            approvals, why = 0, "onboard ran without --apply, so collaborators were not read"
        print(
            f"NOTE: --required-approvals {approvals} ({why}).",
            file=sys.stderr,
        )
    if approvals == 0:
        print(
            "WARNING: the ruleset will require no approving review, so GitHub does not enforce "
            "the human-merge boundary"
            + (
                " -- in cloud-routine mode the routine acts as the owner and could merge its own "
                "PR once CI passes"
                if args.implementer == "cloud-routine"
                else ""
            )
            + ". Pass --required-approvals N (N >= 1 needs a reviewer other than the PR's author).",
            file=sys.stderr,
        )
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
        publisher_bot_login=bot_login,
        hook_python=hook_python,
        trust_mode=trust,
        ephemeral_runners=args.ephemeral_runners,
        required_approvals=approvals,
    )
    written = write_onboarding(destination, spec, force=args.force)
    result: dict[str, Any] = {
        "project_id": project_id,
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


def _confirm_cloud_routine(args: argparse.Namespace) -> int:
    path = confirm_cloud_routine(Path(args.destination).resolve())
    _status(
        f"recorded in {path}: the cloud implementer routine is scheduled (its environment must "
        "run the setup script in docs/forge/cloud-implementer.md: `sdlcctl --version` there "
        "has to print the pinned commit)"
    )
    return 0


def _explicit_lease_policy(args: argparse.Namespace) -> ProjectPolicy | None:
    """The policy an explicit `--config` names, or None when it was omitted. A path that was
    given but is missing or unreadable is an error (exit 2) before any GitHub call, never a
    quiet fall-back to the defaults (Codex 4242643524): a typo would otherwise lease with the
    default TTL instead of the one the policy sets."""
    if args.config is None:
        return None
    try:
        return load_policy(args.config)
    except (OSError, ValueError) as exc:
        raise LeaseError(f"--config {args.config} could not be read ({exc})") from exc


#: Where `_committed_lease_policy` reads the policy on the repository's default branch.
COMMITTED_POLICY_PATH = "agentic-sdlc.toml"


def _committed_lease_policy(project: str) -> ProjectPolicy | None:
    """The policy committed at `COMMITTED_POLICY_PATH` on `project`'s default branch, read with
    the trusted `gh` (Codex 4242925440): a lease command run where no policy file is at hand
    -- e.g. a `release` with no `--config` in a directory holding no agentic-sdlc.toml --
    still learns the Publisher bot whose claim it has to release. None only when GitHub
    answers 404 for that file (the repository commits no policy) -- the status of the
    response as `run_gh` parsed it (`gh_http_status`), never a "404" anywhere in the message,
    which echoes the request path (Codex 4243386515). Any other failure, or an
    answer that is not a readable policy, raises LeaseError: never read as "no Publisher bot"."""
    try:
        repo = json.loads(run_gh(["api", f"repos/{project}"]) or "null")
    except Exception as exc:  # gh failed (OnboardError) or answered something not JSON
        raise LeaseError(f"cannot read the default branch of {project} ({exc})") from exc
    branch = repo.get("default_branch") if isinstance(repo, dict) else None
    if not isinstance(branch, str) or not branch:
        raise LeaseError(f"cannot read the default branch of {project}")
    where = f"{project}@{branch}:{COMMITTED_POLICY_PATH}"
    try:
        answer = run_gh(
            [
                "api",
                f"repos/{project}/contents/{COMMITTED_POLICY_PATH}?ref={quote(branch, safe='')}",
            ]
        )
    except Exception as exc:
        if gh_http_status(exc) == 404:  # GitHub's answer, not the path (Codex 4243386515)
            return None
        raise LeaseError(f"cannot read the committed policy {where} ({exc})") from exc
    try:
        document = json.loads(answer or "null")
        if (
            not isinstance(document, dict)
            or document.get("type") != "file"
            or document.get("encoding") != "base64"
            or not isinstance(document.get("content"), str)
        ):
            raise ValueError("not a base64-encoded file")
        return load_policy_bytes(base64.b64decode(document["content"]))
    except (ValueError, UnicodeDecodeError) as exc:  # JSON, base64, TOML or policy errors
        raise LeaseError(f"the committed policy {where} cannot be read ({exc})") from exc


def _check_lease_policy_project(policy: ProjectPolicy, project: str | None, source: str) -> None:
    """Refuse a lease policy that is not `project`'s (Codex 4243076063): its TTL and Publisher
    bot belong to another repository, so leasing `project` with them would trust the wrong App
    bot. GitHub repository names compare case-insensitively (`owner/Repo` is `owner/repo`)."""
    if project is None:
        return
    if policy.provider != "github" or policy.project_id.casefold() != project.casefold():
        raise LeaseError(
            f"{source} is not {project}'s policy (provider {policy.provider!r}, project "
            f"{policy.project_id!r}); not leasing with it"
        )


def _lease_policy(
    args: argparse.Namespace, *, committed_unreadable_ok: bool = False
) -> ProjectPolicy | None:
    """The policy `--config` names (which must be readable), else -- only when `--config` was
    omitted -- ./agentic-sdlc.toml if it exists, else the policy committed on the repository's
    default branch (`_committed_lease_policy`, Codex 4242925440), else None. Its
    `[automation] publisher_bot_login` becomes the Publisher bot whose lease markers count in
    this process (FORGE_LEASE_BOT_LOGINS still wins; with neither, no App bot does -- Codex
    4242753917). FORGE_LEASE_BOT_LOGINS overrides only the bot: the committed policy is read
    even when it is set, for its TTL (Codex 4243076065).

    A policy whose project or provider is not `--project`'s is an error (LeaseError: exit 2,
    Codex 4243076063) before any lease call. A committed policy that cannot be read, or is
    another project's, is an error too -- unless `committed_unreadable_ok`, for `release`,
    which then proceeds trusting no App bot and says so: `release` settles its own session's
    claims whoever posted them (`leases.release`), so an unknown Publisher bot cannot make it
    skip one."""
    project = getattr(args, "project", None)
    policy = _explicit_lease_policy(args)
    if policy is not None:
        _check_lease_policy_project(policy, project, f"--config {args.config}")
    elif args.config is None:
        config = Path("agentic-sdlc.toml")
        if config.exists():
            policy = load_policy(config)
            _check_lease_policy_project(policy, project, f"./{config}")
        elif project:
            try:
                policy = _committed_lease_policy(project)
                if policy is not None:
                    _check_lease_policy_project(
                        policy, project, f"the committed {COMMITTED_POLICY_PATH}"
                    )
            except LeaseError as exc:
                if not committed_unreadable_ok:
                    raise
                policy = None
                _status(f"WARNING: {exc}; trusting no Publisher App bot for this release")
    use_policy_bot_logins(policy.publisher_bot_login if policy is not None else "")
    return policy


def _lease_ttl(args: argparse.Namespace) -> int:
    """`--ttl-minutes`, else the TTL of the lease policy (`_lease_policy`), else the default."""
    policy = _lease_policy(args)
    if args.ttl_minutes is not None:  # an explicit 0 must reach validation, not the default
        return args.ttl_minutes
    if policy is not None:
        return policy.lease_ttl_minutes
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
    # the Publisher bot; a bad explicit --config is still an error
    _lease_policy(args, committed_unreadable_ok=True)
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


def _approval_labels(args: argparse.Namespace) -> list[str]:
    """The labels `authorize-labeler` checks: every `--label` given (the first is the approval
    label), else the policy's whole approval set -- implementation, ready and human-review labels
    from `--config` (Codex 4240421144). A policy that cannot be read is an error (exit 2), never a
    smaller set."""
    if args.label:
        # --label decides; an explicit --config is still not ignored when it cannot be read
        # (Codex 4242643524)
        _explicit_lease_policy(args)
        return list(args.label)
    config = args.config if args.config is not None else "agentic-sdlc.toml"
    try:
        policy = load_policy(config)
    except (OSError, ValueError) as exc:
        raise LeaseError(
            f"cannot read the approval labels from {config} ({exc}); pass every label of "
            "the approval set with --label; not authorizing"
        ) from exc
    return [policy.implementation_label, policy.ready_label, policy.human_review_label]


def _authorize_labeler(args: argparse.Namespace) -> int:
    """Exit 0: every label of the approval set was last applied by a writer. Exit 3:
    definitively not (an unauthorized labeler, or a label was removed). Exit 2 (an error):
    unverifiable."""
    verdict = authorize_labeler(args.project, args.issue, _approval_labels(args), run_gh)
    _write(verdict.as_dict(), args.output)
    if not verdict.authorized:
        _status(f"NOT AUTHORIZED: #{args.issue}: {verdict.reason}")
        return 3
    _status(f"authorized #{args.issue}: {verdict.reason}")
    return 0


def _claims(args: argparse.Namespace) -> int:
    # its Publisher bot (Codex 4242753917): ./agentic-sdlc.toml, else the committed policy
    _lease_policy(argparse.Namespace(config=None, project=args.project))
    inventory = claim_inventory(args.project, run_gh)
    rows = inventory.rows
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
    if not inventory.complete:
        # A partial list must never read as the whole one: issues not listed may be leased.
        _status("INCOMPLETE: more leases may exist -- " + "; ".join(inventory.gaps))
        return 3
    return 0


def installed_version() -> str:
    """`sdlcctl <version> commit <sha>`: the installed distribution's version and, when pip
    recorded one (a `git+` install, as the cloud routine's setup script does), the exact commit
    it was installed from -- what step 0 of the routine prompt compares with its pin (Codex
    4242373521). `commit unknown` otherwise (say, an editable checkout)."""
    from importlib import metadata

    from . import __version__

    try:
        dist = metadata.distribution("agentic-sdlc")
    except metadata.PackageNotFoundError:
        return f"sdlcctl {__version__} commit unknown"
    commit = ""
    try:
        recorded = json.loads(dist.read_text("direct_url.json") or "{}")
    except (OSError, ValueError):
        recorded = {}
    vcs = recorded.get("vcs_info") if isinstance(recorded, dict) else None
    if isinstance(vcs, dict) and isinstance(vcs.get("commit_id"), str):
        commit = vcs["commit_id"]
    return f"sdlcctl {dist.version} commit {commit or 'unknown'}"


class _VersionAction(argparse.Action):
    def __init__(self, option_strings, dest=argparse.SUPPRESS, default=argparse.SUPPRESS, **kw):
        super().__init__(option_strings, dest=dest, default=default, nargs=0, **kw)

    def __call__(self, parser, namespace, values, option_string=None):
        print(installed_version())
        parser.exit()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sdlcctl")
    parser.add_argument(
        "--version",
        action=_VersionAction,
        help="print the installed version and the commit it was installed from",
    )
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

    check_diff = commands.add_parser(
        "check-diff",
        help="apply the diff policy to the committed change HEAD brings onto --base; "
        "exit 0 only if it passes (2: rejected, empty or unreadable)",
    )
    check_diff.add_argument(
        "--config",
        default="agentic-sdlc.toml",
        help="the policy file's path in the repository (relative to its top level); it is read "
        "as committed on --base, never from the working tree",
    )
    check_diff.add_argument(
        "--project",
        required=True,
        help="owner/name on GitHub: --base must be the head of its default branch there",
    )
    check_diff.add_argument("--repository", default=".")
    check_diff.add_argument(
        "--base",
        required=True,
        help="the default branch as fetched, e.g. origin/main; it must resolve to the commit "
        "GitHub reports as that branch's head (exit 2 otherwise)",
    )
    check_diff.add_argument("--head", default="HEAD", help="the commit that will be pushed")
    check_diff.add_argument("--output")
    check_diff.set_defaults(handler=_check_diff)

    check_request = commands.add_parser(
        "check-request",
        help="exit 0 only if the issue is an open, valid work request whose dependencies are "
        "all closed (3: invalid request, 4: a dependency is open, 5: content changed from "
        "--expect-digest, 6: issue not open, 7: approval labels not authorized, 8: issue "
        "kept changing and 9: an open pull request for the issue (all three with "
        "--authorize-labels), 2: could not verify)",
    )
    check_request.add_argument("--project", required=True, help="owner/name on GitHub")
    check_request.add_argument("--issue", type=int, required=True)
    check_request.add_argument(
        "--config",
        default="agentic-sdlc.toml",
        help="the policy file; with --base, its path in --repository as committed there",
    )
    check_request.add_argument(
        "--base",
        help="read the policy as committed on this revision, e.g. origin/main; it must "
        "resolve to the head of --project's default branch on GitHub (exit 2 otherwise)",
    )
    check_request.add_argument("--repository", default=".")
    check_request.add_argument(
        "--expect-digest",
        help="the content digest an earlier check-request printed; exit 5 if the title or "
        "body no longer matches it",
    )
    check_request.add_argument(
        "--authorize-labels",
        action="store_true",
        help="also require what authorize-labeler requires (exit 7 if not), all judged on one "
        "snapshot of the issue that a second read must find unchanged (exit 8 if it kept "
        "changing; exit 5 if its title or body changed) and no open pull request for the "
        "issue in either read (exit 9): the routine's one final gate",
    )
    check_request.add_argument(
        "--label",
        action="append",
        help="with --authorize-labels: a label of the approval set (repeat for each; the first "
        "is the approval label); default: the implementation, ready and human-review labels "
        "of the policy (as committed on --base when given)",
    )
    check_request.add_argument("--output")
    check_request.add_argument(
        "--attestation-key",
        help="secret key file: the written result carries an HMAC attestation "
        "dispatch-packet --authorization verifies",
    )
    check_request.set_defaults(handler=_check_request)

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

    packet = commands.add_parser("context-packet")
    packet.add_argument("--config", required=True)
    packet.add_argument("--missions")
    packet.add_argument("--knowledge", required=True)
    packet.add_argument("--request", required=True)
    packet.add_argument("--candidate-repository")
    packet.add_argument("--candidate-base")
    packet.add_argument("--candidate-head")
    packet.add_argument(
        "--review-target-repository",
        help="trusted repository (not the candidate's) whose origin/<default "
        "branch> authenticates --candidate-base",
    )
    packet.add_argument(
        "--gate-sandbox",
        help="isolation wrapper command prefix derived gates run under, or "
        "the literal trusted-host to accept host execution",
    )
    packet.add_argument("--output")
    packet.set_defaults(handler=_context_packet)

    dispatch_packet = commands.add_parser("dispatch-packet")
    dispatch_packet.add_argument("--config", required=True)
    dispatch_packet.add_argument("--missions")
    dispatch_packet.add_argument("--knowledge", required=True)
    dispatch_packet.add_argument("--request", required=True)
    dispatch_packet.add_argument("--candidate-repository")
    dispatch_packet.add_argument("--candidate-base")
    dispatch_packet.add_argument("--candidate-head")
    dispatch_packet.add_argument(
        "--review-target-repository",
        help="trusted repository (not the candidate's) whose origin/<default "
        "branch> authenticates --candidate-base",
    )
    dispatch_packet.add_argument(
        "--gate-sandbox",
        help="isolation wrapper command prefix derived gates run under, or "
        "the literal trusted-host to accept host execution",
    )
    dispatch_packet.add_argument("--agents", required=True)
    dispatch_packet.add_argument("--history")
    dispatch_packet.add_argument("--authorization")
    dispatch_packet.add_argument(
        "--attestation-key",
        help="secret key file verifying the --history and --authorization "
        "artifacts' HMAC attestations",
    )
    dispatch_packet.add_argument("--authorization-max-age-seconds", type=int, default=900)
    dispatch_packet.add_argument("--packet")
    dispatch_packet.add_argument("--input-ref", action="append", default=[])
    dispatch_packet.add_argument("--output")
    dispatch_packet.set_defaults(handler=_dispatch_packet)

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
    orchestrate.add_argument(
        "--completion",
        help="with --attestation-key and --commit-sha: the attest-completion "
        "record binding this unit, run and commit",
    )
    orchestrate.add_argument("--max-repair-cycles", type=int, default=2)
    orchestrate.add_argument("--status-output")
    orchestrate.add_argument(
        "--attestation-key",
        help="secret key file: the written state carries an HMAC attestation "
        "dispatch-packet --history verifies, and existing state is verified on read",
    )

    attest_completion = commands.add_parser("attest-completion")
    attest_completion.add_argument("--unit", required=True)
    attest_completion.add_argument("--run-id", required=True)
    attest_completion.add_argument("--commit-sha", required=True)
    attest_completion.add_argument(
        "--commit-repository",
        required=True,
        help="the trusted checkout the finished run's commit must exist in",
    )
    attest_completion.add_argument("--attestation-key", required=True)
    attest_completion.add_argument("--output")
    attest_completion.set_defaults(handler=_attest_completion)
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
    scaffold.add_argument(
        "--publisher-bot-login",
        default="",
        help="the Publisher App's bot login, `<app-slug>[bot]`; required on GitHub at "
        "automation level 2 or 3 (written to [automation] publisher_bot_login)",
    )
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
        "--runs-on",
        default="ubuntu-latest",
        help="comma-separated runner labels of the Forge jobs, which run model-produced patches "
        "(default: GitHub-hosted ubuntu-latest); self-hosted labels need --ephemeral-runners",
    )
    onboard.add_argument(
        "--ephemeral-runners",
        action="store_true",
        help="acknowledge that the self-hosted runners in --runs-on/--ci-runs-on are ephemeral "
        "(one job, then discarded) or otherwise contained; required for any self-hosted label",
    )
    onboard.add_argument(
        "--trust-mode",
        choices=TRUST_MODES,
        help="strict (default): git, gh and the hook python must be files only root can "
        "replace; local: a developer's own machine (first on PATH, owned by root or you). "
        "GitHub Actions, Claude Code cloud sessions and the publication-decision commands "
        "(check-diff, check-request, authorize-labeler, claim, renew, release, "
        "confirm-cloud-routine) are strict regardless. Default: the installed policy's on "
        "--force, else strict",
    )
    onboard.add_argument(
        "--required-approvals",
        type=int,
        choices=range(0, MAX_REQUIRED_APPROVALS + 1),
        metavar="N",
        help="approving reviews the ruleset requires (0-10). Default with --apply: 1 when "
        "another collaborator with write access exists, else 0; offline: 0",
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
    onboard.add_argument(
        "--force",
        action="store_true",
        help="overwrite files the profile owns (AGENTS.md, CLAUDE.md and .claude/settings.json "
        "are merged: content added around the managed part is kept)",
    )
    onboard.add_argument(
        "--apply", action="store_true", help="create labels, ruleset and variables via gh"
    )
    onboard.add_argument(
        "--copy-vars-from", help="owner/name of an onboarded repo to copy non-secret variables from"
    )
    onboard.add_argument(
        "--var", action="append", help="repo variable KEY=VALUE to set (repeatable)"
    )
    onboard.add_argument(
        "--publisher-bot-login",
        help="the Publisher App's bot login, <app-slug>[bot]: the only GitHub App whose lease "
        "markers count ([automation] publisher_bot_login; with --apply also the "
        "FORGE_LEASE_BOT_LOGINS variable). Omitted: a forced re-onboard keeps the installed one",
    )
    onboard.add_argument(
        "--hook-python",
        help="absolute path of the Python 3 the Claude Code hooks run (default: the first of "
        "/usr/bin/python3, /bin/python3); must be a file only root can replace",
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

    confirm = commands.add_parser(
        "confirm-cloud-routine",
        help="record that the cloud implementer routine is scheduled on claude.ai "
        "([automation] cloud_routine_confirmed = true); doctor stays NOT READY until then",
    )
    confirm.add_argument("--destination", default=".")
    confirm.set_defaults(handler=_confirm_cloud_routine)

    def _lease_args(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--project", required=True, help="owner/name on GitHub")
        parser.add_argument("--issue", type=int, required=True)
        parser.add_argument("--session", required=True, help="unique id of this agent run/session")
        parser.add_argument(
            "--config",
            help="the policy file for lease_ttl_minutes and publisher_bot_login; given "
            "explicitly, it must be readable (exit 2 otherwise). Omitted: ./agentic-sdlc.toml "
            "if it exists, else the agentic-sdlc.toml committed on the default branch "
            "(unreadable: exit 2; `release` warns and still releases its own session's "
            "claims), else the defaults. A policy for another project is refused (exit 2); "
            "FORGE_LEASE_BOT_LOGINS overrides only its publisher_bot_login",
        )
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

    authorize = commands.add_parser(
        "authorize-labeler",
        help="exit 0 only if a write/maintain/admin user applied each approval label last "
        "(3: not authorized, 2: could not verify)",
    )
    authorize.add_argument("--project", required=True, help="owner/name on GitHub")
    authorize.add_argument("--issue", type=int, required=True)
    authorize.add_argument(
        "--label",
        action="append",
        help="a label of the approval set (repeat for each; the first is the approval label); "
        "default: the implementation, ready and human-review labels of --config",
    )
    authorize.add_argument(
        "--config",
        help="the policy file for the default approval set (default: agentic-sdlc.toml); "
        "given explicitly, it must be readable even with --label",
    )
    authorize.add_argument("--output")
    authorize.set_defaults(handler=_authorize_labeler)

    lease_list = commands.add_parser("claims", help="list live and expired leases in a repository")
    lease_list.add_argument("--project", required=True)
    lease_list.add_argument("--output")
    lease_list.set_defaults(handler=_claims)
    return parser


def _policy_trust_mode(args: argparse.Namespace) -> str | None:
    """The `[automation] trust_mode` of the policy this command reads -- `--config` when it
    names a readable file, else `<destination>/agentic-sdlc.toml` (onboard, doctor), else
    ./agentic-sdlc.toml -- or None when there is none. In an agent context (GitHub Actions, a
    Claude Code cloud session) the mode is strict whatever this returns (`trust_mode`)."""
    import tomllib

    candidates = []
    config = getattr(args, "config", None)
    if isinstance(config, str) and config:
        candidates.append(Path(config))
    destination = getattr(args, "destination", None)
    if isinstance(destination, str) and destination:
        candidates.append(Path(destination) / "agentic-sdlc.toml")
    candidates.append(Path("agentic-sdlc.toml"))
    for path in candidates:
        try:
            document = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            continue
        return policy_trust_mode(document)
    return None


#: The publication-decision commands: what the cloud routine and the Actions workflows run as
#: gates. They always resolve git and gh in STRICT trust and ignore the override variables
#: (`use_gate_trust`, Codex 4244027559) -- neither the policy's `trust_mode` (possibly a
#: working-tree file the agent edited) nor the environment (the agent's own shell can unset
#: CLAUDE_CODE_REMOTE) may loosen how they pick the programs whose output they decide on.
GATE_COMMANDS = frozenset(
    {
        "check-diff",
        "check-request",
        "authorize-labeler",
        "claim",
        "renew",
        "release",
        "confirm-cloud-routine",
        # Packet derivation and attested orchestration DERIVE trust
        # artifacts: resolving git through a working-tree trust_mode or an
        # override variable would let a candidate-planted executable
        # fabricate the commits, committed files, and identities those
        # artifacts are then signed over.
        "context-packet",
        "dispatch-packet",
        "orchestrate",
        "attest-completion",
    }
)


def _use_trust_for(args: argparse.Namespace) -> None:
    """Record the trust mode this command runs under: STRICT with no overrides for a
    `GATE_COMMANDS` one (an ignored override is reported on stderr), else the policy's."""
    gate = getattr(args, "command", None) in GATE_COMMANDS
    use_gate_trust(gate)
    if not gate:
        use_trust_mode(_policy_trust_mode(args))
        return
    use_trust_mode(STRICT)
    for name in (_GIT_OVERRIDE_ENV, _GH_OVERRIDE_ENV):
        if os.environ.get(name):
            print(
                f"WARNING: {name} is ignored by `sdlcctl {args.command}`: a publication "
                "decision always resolves git and gh in strict trust mode",
                file=sys.stderr,
            )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _use_trust_for(args)
        return int(args.handler(args))
    except (
        TaskSpecError,
        ArtifactError,
        CapacityError,
        ContextBuilderError,
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
