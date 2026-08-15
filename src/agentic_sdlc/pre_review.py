"""Cheap deterministic checks that must pass before independent AI review.

The checks intentionally avoid executing consumer code. They inspect the trusted
policy configuration, the exact Git diff, and workflow structure so common
security/configuration defects are rejected before spending an AI review cycle.
"""

from __future__ import annotations

import re
import shlex
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .git_diff import collect_git_diff

_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
_SHELL_BUILTINS = frozenset({".", "cd", "export", "set", "source", "ulimit"})
_SHELL_PUNCTUATION = frozenset({"&", "&&", "|", "||", ";", ">", ">>", "<", "<<"})
_DEFAULT_FORBIDDEN_ADDITIONS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "openai/codex-action",
    "anthropic_api_key:",
)


class PreReviewError(ValueError):
    """Raised when deterministic pre-review configuration cannot be evaluated."""


@dataclass(frozen=True)
class PreReviewFinding:
    code: str
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


def _load_config(path: str | Path) -> dict[str, Any]:
    with Path(path).open("rb") as handle:
        document = tomllib.load(handle)
    if not isinstance(document, dict):
        raise PreReviewError("policy document must be a TOML table")
    return document


def _shell_tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _gate_command_findings(config_path: str | Path) -> list[PreReviewFinding]:
    document = _load_config(config_path)
    commands = document.get("commands", {})
    verification = document.get("verification", {})
    if not isinstance(commands, dict) or not isinstance(verification, dict):
        raise PreReviewError("commands and verification must be TOML tables")
    gates = verification.get("gates", list(commands))
    if not isinstance(gates, list) or not all(isinstance(name, str) for name in gates):
        raise PreReviewError("verification.gates must be an array of command names")

    findings: list[PreReviewFinding] = []
    for name in gates:
        command = commands.get(name)
        if not isinstance(command, str) or not command.strip():
            findings.append(
                PreReviewFinding(
                    "gate-command-missing",
                    str(config_path),
                    f"verification gate {name!r} has no non-empty command",
                )
            )
            continue
        try:
            arguments = shlex.split(command, posix=True)
            tokens = _shell_tokens(command)
        except ValueError as exc:
            findings.append(
                PreReviewFinding(
                    "gate-command-parse",
                    str(config_path),
                    f"verification gate {name!r} is not parseable: {exc}",
                )
            )
            continue
        if not arguments:
            findings.append(
                PreReviewFinding(
                    "gate-command-empty",
                    str(config_path),
                    f"verification gate {name!r} is empty",
                )
            )
            continue
        if _ENV_ASSIGNMENT.fullmatch(arguments[0]):
            findings.append(
                PreReviewFinding(
                    "gate-command-env-prefix",
                    str(config_path),
                    f"verification gate {name!r} starts with an environment assignment; "
                    "use a checked-in wrapper instead",
                )
            )
        if arguments[0] in _SHELL_BUILTINS:
            findings.append(
                PreReviewFinding(
                    "gate-command-shell-builtin",
                    str(config_path),
                    f"verification gate {name!r} requires shell builtin {arguments[0]!r}; "
                    "use a checked-in wrapper instead",
                )
            )
        punctuation = sorted(token for token in tokens if token in _SHELL_PUNCTUATION)
        if punctuation or "$(" in command or "`" in command:
            detail = ", ".join(punctuation) if punctuation else "command substitution"
            findings.append(
                PreReviewFinding(
                    "gate-command-shell-syntax",
                    str(config_path),
                    f"verification gate {name!r} requires unsupported shell syntax ({detail}); "
                    "Forge executes gates with shell=False, so use a checked-in wrapper",
                )
            )
    return findings


def _iter_uses(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            if key == "uses" and isinstance(nested, str):
                found.append(nested)
            found.extend(_iter_uses(nested))
    elif isinstance(value, list):
        for nested in value:
            found.extend(_iter_uses(nested))
    return found


def _contains_claude_step(job: Any) -> bool:
    if not isinstance(job, dict):
        return False
    return any("anthropics/claude-code-action@" in ref for ref in _iter_uses(job))


def _workflow_findings(root: Path, path: str) -> list[PreReviewFinding]:
    target = root / path
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise PreReviewError(f"cannot read workflow {path}: {exc}") from exc
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return [PreReviewFinding("workflow-yaml-invalid", path, f"invalid workflow YAML: {exc}")]
    if not isinstance(document, dict):
        return [PreReviewFinding("workflow-yaml-shape", path, "workflow root must be a mapping")]

    findings: list[PreReviewFinding] = []
    for reference in _iter_uses(document):
        if reference.startswith("./") or reference.startswith("docker://"):
            continue
        if "@" not in reference:
            findings.append(
                PreReviewFinding(
                    "workflow-unpinned-reference",
                    path,
                    f"external workflow/action reference is not pinned: {reference}",
                )
            )
            continue
        ref = reference.rsplit("@", 1)[1]
        if _FULL_SHA.fullmatch(ref) is None:
            findings.append(
                PreReviewFinding(
                    "workflow-mutable-reference",
                    path,
                    f"external workflow/action must use a full immutable SHA: {reference}",
                )
            )

    agent_bearing = (
        "CLAUDE_CODE_OAUTH_TOKEN" in text
        or "anthropics/claude-code-action@" in text
        or "agentic-sdlc/.github/workflows/reusable-" in text
    )
    if agent_bearing and document.get("permissions") != {}:
        findings.append(
            PreReviewFinding(
                "workflow-default-permissions",
                path,
                "agent-bearing workflow must declare top-level permissions: {}",
            )
        )
    if agent_bearing and "pull_request_target" in text:
        findings.append(
            PreReviewFinding(
                "workflow-pull-request-target",
                path,
                "secret-bearing agent workflows may not use pull_request_target",
            )
        )

    jobs = document.get("jobs", {})
    if isinstance(jobs, dict):
        for job_name, job in jobs.items():
            if not _contains_claude_step(job):
                continue
            permissions = job.get("permissions", {}) if isinstance(job, dict) else {}
            if not isinstance(permissions, dict) or permissions.get("contents") != "read":
                findings.append(
                    PreReviewFinding(
                        "claude-job-contents-permission",
                        path,
                        f"Claude job {job_name!r} must have contents: read",
                    )
                )
                continue
            forbidden = sorted(
                name
                for name, value in permissions.items()
                if value == "write" and name not in {"id-token"}
            )
            if forbidden:
                findings.append(
                    PreReviewFinding(
                        "claude-job-write-permission",
                        path,
                        f"Claude job {job_name!r} has forbidden write permissions: "
                        + ", ".join(forbidden),
                    )
                )
    return findings


def _added_lines(patch: bytes) -> list[tuple[str, str]]:
    current = ""
    additions: list[tuple[str, str]] = []
    for raw in patch.decode("utf-8", errors="replace").splitlines():
        if raw.startswith("+++ b/"):
            current = raw[6:]
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            additions.append((current, raw[1:]))
    return additions


def run_pre_review(
    config: str | Path,
    repository: str | Path,
    base: str,
) -> dict[str, Any]:
    """Run cheap fail-closed checks against the exact base-to-HEAD change."""

    root = Path(repository)
    config_path = Path(config)
    if not config_path.is_absolute():
        config_path = root / config_path
    snapshot = collect_git_diff(root, base)
    findings = _gate_command_findings(config_path)

    changed_workflows = sorted(
        path
        for path in snapshot.paths
        if path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml"))
    )
    for path in changed_workflows:
        if (root / path).is_file():
            findings.extend(_workflow_findings(root, path))

    config_doc = _load_config(config_path)
    pre_review = config_doc.get("pre_review", {})
    if pre_review and not isinstance(pre_review, dict):
        raise PreReviewError("pre_review must be a TOML table")
    configured_forbidden = pre_review.get("forbidden_additions", []) if pre_review else []
    if not isinstance(configured_forbidden, list) or not all(
        isinstance(item, str) and item for item in configured_forbidden
    ):
        raise PreReviewError("pre_review.forbidden_additions must be an array of strings")
    forbidden = tuple(dict.fromkeys((*_DEFAULT_FORBIDDEN_ADDITIONS, *configured_forbidden)))
    for path, line in _added_lines(snapshot.patch):
        for token in forbidden:
            if token in line:
                findings.append(
                    PreReviewFinding(
                        "forbidden-provider-addition",
                        path,
                        "new line reintroduces forbidden provider credential/action "
                        f"token: {token}",
                    )
                )

    required_phrases = pre_review.get("required_phrases", {}) if pre_review else {}
    if not isinstance(required_phrases, dict):
        raise PreReviewError("pre_review.required_phrases must be a TOML table")
    for path, phrases in required_phrases.items():
        if not isinstance(path, str) or not isinstance(phrases, list) or not all(
            isinstance(phrase, str) and phrase for phrase in phrases
        ):
            raise PreReviewError("pre_review.required_phrases values must be arrays of strings")
        target = root / path
        if not target.is_file():
            findings.append(
                PreReviewFinding(
                    "required-policy-file-missing",
                    path,
                    "required policy file is missing",
                )
            )
            continue
        text = target.read_text(encoding="utf-8")
        for phrase in phrases:
            if phrase.lower() not in text.lower():
                findings.append(
                    PreReviewFinding(
                        "required-policy-phrase-missing",
                        path,
                        f"required policy phrase is missing: {phrase}",
                    )
                )

    unique = {(item.code, item.path, item.message): item for item in findings}
    ordered = sorted(unique.values(), key=lambda item: (item.path, item.code, item.message))
    return {
        "schemaVersion": 1,
        "passed": not ordered,
        "changedPaths": list(snapshot.paths),
        "findings": [item.as_dict() for item in ordered],
    }
