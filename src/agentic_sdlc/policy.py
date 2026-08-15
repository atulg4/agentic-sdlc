"""Deterministic policy evaluation for tasks and proposed diffs."""

from __future__ import annotations

import fnmatch
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .models import PolicyDecision, RiskLevel, TaskSpec

_PROJECT_ID = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+$")
_BRANCH = re.compile(r"^[A-Za-z0-9._/-]+$")
_BASELINE_FORBIDDEN_PATHS = (
    ".github/**",
    ".github/workflows/**",
    ".gitlab-ci.yml",
    ".gitlab/**",
    "agentic-sdlc.toml",
    "missions.toml",
    "**/missions.toml",
    "knowledge.toml",
    "**/knowledge.toml",
    "intake.toml",
    "**/intake.toml",
    "CODEOWNERS",
    "**/CODEOWNERS",
    "AGENTS.md",
    "**/AGENTS.md",
    "CLAUDE.md",
    "**/CLAUDE.md",
    ".env*",
    "**/.env*",
    "*.pem",
    "**/*.pem",
    "*.key",
    "**/*.key",
)


@dataclass(frozen=True)
class ProjectPolicy:
    provider: str
    project_id: str
    default_branch: str
    ready_label: str
    human_review_label: str
    implementation_label: str
    human_merge_required: bool
    max_changed_files: int
    max_diff_lines: int
    max_patch_bytes: int
    forbidden_paths: tuple[str, ...]
    protected_paths: tuple[str, ...]
    low_risk_paths: tuple[str, ...]
    forbidden_task_patterns: tuple[str, ...]


def _table(document: dict[str, object], name: str) -> dict[str, object]:
    value = document.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a TOML table")
    return value


def _string(table: dict[str, object], name: str, default: str) -> str:
    value = table.get(name, default)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _integer(
    table: dict[str, object],
    name: str,
    default: int,
    *,
    maximum: int,
) -> int:
    value = table.get(name, default)
    if type(value) is not int or value < 1 or value > maximum:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}")
    return value


def _patterns(
    table: dict[str, object],
    name: str,
    default: tuple[str, ...] = (),
) -> tuple[str, ...]:
    value = table.get(name, list(default))
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{name} must be an array of non-empty strings")
    return tuple(value)


def load_policy(path: str | Path) -> ProjectPolicy:
    with Path(path).open("rb") as handle:
        data = tomllib.load(handle)
    if data.get("version") != 1:
        raise ValueError("policy version must be 1")
    project = _table(data, "project")
    automation = _table(data, "automation")
    policy = _table(data, "policy")

    provider = _string(project, "provider", "github")
    project_id = _string(project, "id", "unknown/unknown")
    default_branch = _string(project, "default_branch", "main")
    if provider not in {"github", "gitlab"}:
        raise ValueError("project.provider must be github or gitlab")
    if not _PROJECT_ID.fullmatch(project_id):
        raise ValueError("project.id must use namespace/name format")
    if not _BRANCH.fullmatch(default_branch):
        raise ValueError("project.default_branch contains unsupported characters")

    ready_label = _string(automation, "ready_label", "agent-ready")
    human_review_label = _string(
        automation,
        "human_review_label",
        "human-review-required",
    )
    implementation_label = _string(
        automation,
        "implementation_label",
        "implementation-approved",
    )
    if len({ready_label, human_review_label, implementation_label}) != 3:
        raise ValueError("automation approval labels must be distinct")

    human_merge_required = policy.get("human_merge_required", True)
    if type(human_merge_required) is not bool:
        raise ValueError("policy.human_merge_required must be a boolean")
    forbidden_paths = tuple(
        dict.fromkeys((*_BASELINE_FORBIDDEN_PATHS, *_patterns(policy, "forbidden_paths")))
    )
    forbidden_task_patterns = _patterns(policy, "forbidden_task_patterns")
    for pattern in forbidden_task_patterns:
        try:
            re.compile(pattern)
        except re.error as error:
            raise ValueError(f"invalid forbidden task pattern: {pattern}") from error

    return ProjectPolicy(
        provider=provider,
        project_id=project_id,
        default_branch=default_branch,
        ready_label=ready_label,
        human_review_label=human_review_label,
        implementation_label=implementation_label,
        human_merge_required=human_merge_required,
        max_changed_files=_integer(policy, "max_changed_files", 30, maximum=10_000),
        max_diff_lines=_integer(policy, "max_diff_lines", 3000, maximum=10_000_000),
        max_patch_bytes=_integer(policy, "max_patch_bytes", 5_000_000, maximum=100_000_000),
        forbidden_paths=forbidden_paths,
        protected_paths=_patterns(policy, "protected_paths"),
        low_risk_paths=_patterns(policy, "low_risk_paths", ("docs/**", "**/*.md")),
        forbidden_task_patterns=forbidden_task_patterns,
    )


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    normalized = path.removeprefix("./")
    return any(fnmatch.fnmatch(normalized, pattern) for pattern in patterns)


def evaluate_task(
    task: TaskSpec,
    policy: ProjectPolicy,
    mode: str = "plan",
) -> PolicyDecision:
    reasons = []
    required_labels = {policy.ready_label, policy.human_review_label}
    if mode == "implement":
        required_labels.add(policy.implementation_label)
    missing = required_labels - set(task.labels)
    if missing:
        reasons.append("missing required labels: " + ", ".join(sorted(missing)))

    text = f"{task.title}\n{task.raw_body}"
    for pattern in policy.forbidden_task_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            reasons.append(f"task matched forbidden policy pattern: {pattern}")

    gates = ["deterministic-ci", "independent-agent-review"]
    if policy.human_merge_required:
        gates.append("human-merge-approval")
    return PolicyDecision(
        allowed=not reasons,
        risk=RiskLevel.CRITICAL if reasons else RiskLevel.MEDIUM,
        reasons=tuple(reasons) if reasons else ("task specification is eligible",),
        required_gates=tuple(gates),
        automatic_merge_allowed=False,
    )


def evaluate_diff(
    paths: tuple[str, ...],
    added_lines: int,
    deleted_lines: int,
    policy: ProjectPolicy,
    patch_bytes: int = 0,
) -> PolicyDecision:
    if min(added_lines, deleted_lines, patch_bytes) < 0:
        raise ValueError("diff counts and patch bytes cannot be negative")
    reasons = []
    gates = ["deterministic-ci", "independent-agent-review"]
    total_lines = added_lines + deleted_lines

    if len(paths) > policy.max_changed_files:
        reasons.append(f"changed-file cap exceeded: {len(paths)} > {policy.max_changed_files}")
    if total_lines > policy.max_diff_lines:
        reasons.append(f"diff-line cap exceeded: {total_lines} > {policy.max_diff_lines}")
    if patch_bytes > policy.max_patch_bytes:
        reasons.append(f"patch-byte cap exceeded: {patch_bytes} > {policy.max_patch_bytes}")

    forbidden = sorted(path for path in paths if _matches(path, policy.forbidden_paths))
    if forbidden:
        reasons.append("forbidden paths changed: " + ", ".join(forbidden))

    protected = sorted(path for path in paths if _matches(path, policy.protected_paths))
    if protected:
        gates.append("architect-security-review")

    if reasons:
        risk = RiskLevel.CRITICAL
    elif protected:
        risk = RiskLevel.HIGH
    elif paths and all(_matches(path, policy.low_risk_paths) for path in paths):
        risk = RiskLevel.LOW
    else:
        risk = RiskLevel.MEDIUM

    if policy.human_merge_required:
        gates.append("human-merge-approval")
    detail = list(reasons)
    if protected:
        detail.append("protected paths require architect/security review: " + ", ".join(protected))
    if not detail:
        detail.append(f"diff is within policy limits ({len(paths)} files, {total_lines} lines)")

    automatic_merge_allowed = (
        not policy.human_merge_required
        and not reasons
        and not protected
        and risk is RiskLevel.LOW
    )

    return PolicyDecision(
        allowed=not reasons,
        risk=risk,
        reasons=tuple(detail),
        required_gates=tuple(dict.fromkeys(gates)),
        automatic_merge_allowed=automatic_merge_allowed,
    )
