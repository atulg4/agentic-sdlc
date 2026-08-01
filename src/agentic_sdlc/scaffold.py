"""Install a fail-closed Agentic SDLC profile into a consumer repository."""

from __future__ import annotations

import importlib.resources
import re
from pathlib import Path


class ScaffoldError(ValueError):
    """Raised when a consumer repository cannot be scaffolded safely."""


_PROJECT = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+$")
_SHA = re.compile(r"^[0-9a-f]{40}$")


def _resource(relative: str) -> str:
    root = importlib.resources.files("agentic_sdlc")
    return root.joinpath("templates", *relative.split("/")).read_text(encoding="utf-8")


def _policy(project_id: str, provider: str, default_branch: str) -> str:
    return f'''\
version = 1

[project]
id = "{project_id}"
provider = "{provider}"
default_branch = "{default_branch}"

[agents]
planner = "codex"
implementer = "codex"
reviewer = "codex"

[automation]
default_mode = "plan"
ready_label = "agent-ready"
human_review_label = "human-review-required"
implementation_label = "implementation-approved"

[policy]
human_merge_required = true
max_changed_files = 20
max_diff_lines = 2000
max_patch_bytes = 5000000
forbidden_paths = [
  ".github/**",
  ".github/workflows/**",
  ".gitlab-ci.yml",
  ".gitlab/**",
  "agentic-sdlc.toml",
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
]
protected_paths = ["src/auth/**", "migrations/**", "deployment/**"]
low_risk_paths = ["docs/**", "**/*.md"]
forbidden_task_patterns = [
  '\\bdeploy\\b.{{0,40}}\\bprod',
  '\\b(?:production|broker)\\b.{{0,40}}\\b(?:credential|password|token)',
  '\\bdisable\\b.{{0,40}}\\b(?:auth|test|safety)',
]

[commands]

[verification]
gates = []
'''


def _agents(project_id: str) -> str:
    return f"""# {project_id} Agent Guide

## Authority

The repository owner defines product intent and remains the final merge
authority. Codex is the primary architect, planner, and independent reviewer.
Codex or Claude Code may implement an explicitly approved work request.

## Required Workflow

1. Read the relevant code and repository instructions before proposing changes.
2. Require complete acceptance criteria, tests, non-goals, and dependencies.
3. Plan before implementation.
4. Add or update deterministic tests for changed behavior.
5. Make the smallest scoped implementation.
6. Run every configured verification gate.
7. Open a draft change request only. Never merge, approve, or deploy.

The controlling machine-readable policy is `agentic-sdlc.toml`.
"""


def _render(relative: str, platform_repository: str, platform_ref: str) -> str:
    parts = platform_repository.split("/")
    platform_project_path = "/".join(parts[1:]) if "." in parts[0] else platform_repository
    return (
        _resource(relative)
        .replace("PLATFORM_REPOSITORY", platform_repository)
        .replace("PLATFORM_PROJECT_PATH", platform_project_path)
        .replace("PLATFORM_COMMIT_SHA", platform_ref)
    )


def scaffold_project(
    destination: str | Path,
    *,
    provider: str,
    project_id: str,
    platform_repository: str,
    platform_ref: str,
    default_branch: str = "main",
    automation_level: int = 1,
) -> tuple[Path, ...]:
    root = Path(destination).resolve()
    if not root.is_dir() or not (root / ".git").exists():
        raise ScaffoldError("destination must be an existing Git repository")
    if provider not in {"github", "gitlab"}:
        raise ScaffoldError("provider must be github or gitlab")
    if not _PROJECT.fullmatch(project_id) or not _PROJECT.fullmatch(platform_repository):
        raise ScaffoldError("project identifiers must use namespace/name format")
    if not _SHA.fullmatch(platform_ref):
        raise ScaffoldError("platform ref must be an immutable 40-character commit SHA")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", default_branch):
        raise ScaffoldError("default branch contains unsupported characters")
    if automation_level not in {1, 2, 3}:
        raise ScaffoldError("automation level must be 1, 2, or 3")

    files: dict[Path, str] = {
        root / "agentic-sdlc.toml": _policy(project_id, provider, default_branch),
        root / "AGENTS.md": _agents(project_id),
    }
    if provider == "github":
        files[root / ".github/ISSUE_TEMPLATE/agent-work-request.md"] = _resource("work-request.md")
        files[root / ".github/workflows/agent-plan.yml"] = _render(
            "github/agent-plan.yml", platform_repository, platform_ref
        )
        if automation_level >= 2:
            files[root / ".github/workflows/agent-implement.yml"] = _render(
                "github/agent-implement.yml", platform_repository, platform_ref
            )
            files[root / ".github/workflows/agent-review.yml"] = _render(
                "github/agent-review.yml", platform_repository, platform_ref
            )
        if automation_level == 3:
            files[root / ".github/workflows/agent-auto-plan.yml"] = _render(
                "github/agent-auto-plan.yml", platform_repository, platform_ref
            )
            files[root / ".github/workflows/agent-auto-implement.yml"] = _render(
                "github/agent-auto-implement.yml", platform_repository, platform_ref
            )
    else:
        files[root / ".gitlab-ci.agentic-sdlc.yml"] = _render(
            "gitlab/include.yml", platform_repository, platform_ref
        )

    existing = sorted(path for path in files if path.exists())
    if existing:
        shown = ", ".join(str(path.relative_to(root)) for path in existing)
        raise ScaffoldError(f"refusing to overwrite existing files: {shown}")
    for path, content in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return tuple(sorted(files))
