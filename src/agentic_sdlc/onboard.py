"""One-command Forge onboarding for a consumer repository.

`scaffold` installs a generic, fail-closed profile. `onboard` installs the *production* shape the
consumer repos actually run (self-hosted runners, preflight/notify jobs, `claude-ready` labels,
provider routing) and, with `--apply`, configures the GitHub repository itself (labels, ruleset,
variables). `doctor` verifies a repository and lists the remaining manual steps.
"""

from __future__ import annotations

import importlib.resources
import json
import re
import shlex
import subprocess
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .executors import ExecutorError, load_executors, load_routing_policy
from .leases import IN_PROGRESS_LABEL
from .policy import load_policy

GhRunner = Callable[..., str]  # (args: Sequence[str], input: str | None = None) -> stdout

_PROJECT = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_RUNNER_LABEL = re.compile(r"^[A-Za-z0-9_.-]+$")

IMPLEMENTERS = ("route", "claude", "codex", "cloud-routine")
DEFAULT_RUNS_ON = ("self-hosted", "linux", "x64")
DEFAULT_QUALITY = "python -m ruff check --select E9,F63,F7,F82 ."
HUMAN_REVIEW_LABEL = "human-review-required"
IMPLEMENTATION_LABEL = "implementation-approved"
RULESET_NAME = "Protect main"
REQUIRED_CHECK = "test"
PUBLISHER_APP_SLUG_HINT = "agentic-sdlc"

LABELS = {
    "claude-ready": ("0e8a16", "Specified precisely enough for the implementation worker"),
    HUMAN_REVIEW_LABEL: ("5319e7", "A human must review before merge"),
    IMPLEMENTATION_LABEL: ("1d76db", "Owner explicitly approved agent implementation"),
    "agentic-sdlc": ("0052cc", "Managed by the Forge agentic SDLC"),
    "claude-blocked": ("b60205", "Agent could not proceed; needs the owner"),
    "in-progress": ("fbca04", "Leased: an agent/session is actively implementing this"),
}

BASE_FORBIDDEN = (
    ".github/**",
    ".github/workflows/**",
    ".gitlab-ci.yml",
    ".gitlab/**",
    "agentic-sdlc.toml",
    ".forge/**",
    ".claude/**",
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
    "*.db",
    "**/*.db",
    "*.sqlite",
    "**/*.sqlite",
    "*.sqlite3",
    "**/*.sqlite3",
)
BASE_FORBIDDEN_TASKS = (
    r"\bdeploy\b.{0,40}\bprod",
    r"\b(?:print|return|expose|retrieve)\b.{0,40}\b(?:credential|password|secret|token)",
    r"\bdisable\b.{0,40}\b(?:auth|test|safety)",
)


class OnboardError(ValueError):
    """Raised when a repository cannot be onboarded safely."""


@dataclass(frozen=True)
class OnboardSpec:
    project_id: str
    platform_repository: str
    platform_ref: str
    test_command: str
    setup_command: str = "python -m pip install -r requirements.txt"
    quality_command: str = DEFAULT_QUALITY
    implementer: str = "route"
    runs_on: tuple[str, ...] = DEFAULT_RUNS_ON
    default_branch: str = "main"
    ready_label: str = "claude-ready"
    forbidden_paths: tuple[str, ...] = ()
    protected_paths: tuple[str, ...] = ()
    max_changed_files: int = 20
    max_diff_lines: int = 2500

    def __post_init__(self) -> None:
        if not _PROJECT.fullmatch(self.project_id) or not _PROJECT.fullmatch(
            self.platform_repository
        ):
            raise OnboardError("project identifiers must use owner/name format")
        if not _SHA.fullmatch(self.platform_ref):
            raise OnboardError(
                "platform ref must be a 40-character commit SHA (use resolve_platform_ref)"
            )
        if self.implementer not in IMPLEMENTERS:
            raise OnboardError(f"implementer must be one of {', '.join(IMPLEMENTERS)}")
        if not self.runs_on or not all(_RUNNER_LABEL.fullmatch(x) for x in self.runs_on):
            raise OnboardError("runs_on labels must be simple tokens")
        if not self.test_command.strip():
            raise OnboardError("a test command is required; the gate fails closed without one")
        if len({self.ready_label, HUMAN_REVIEW_LABEL, IMPLEMENTATION_LABEL}) != 3:
            raise OnboardError("ready label must differ from the review/approval labels")
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", self.default_branch):
            raise OnboardError("default branch contains unsupported characters")

    @property
    def name(self) -> str:
        return self.project_id.split("/")[1]

    @property
    def routed(self) -> bool:
        return self.implementer == "route"

    @property
    def actions_implement(self) -> bool:
        """False when a Claude Code cloud routine implements instead of a GitHub Actions job."""
        return self.implementer != "cloud-routine"

    @property
    def labels(self) -> dict[str, tuple[str, str]]:
        out = dict(LABELS)
        if self.ready_label != "claude-ready":
            out[self.ready_label] = out.pop("claude-ready")
        return out


# ---------------------------------------------------------------- gh plumbing


def run_gh(args: Sequence[str], input: str | None = None) -> str:
    """Run `gh` and return stdout; raises OnboardError with stderr on failure."""
    try:
        proc = subprocess.run(
            ["gh", *args], check=False, capture_output=True, text=True, input=input
        )
    except FileNotFoundError as exc:  # pragma: no cover - environment
        raise OnboardError("GitHub CLI `gh` is not installed") from exc
    if proc.returncode != 0:
        raise OnboardError(
            f"gh {' '.join(shlex.quote(a) for a in args)} failed: {proc.stderr.strip()[:400]}"
        )
    return proc.stdout


def resolve_platform_ref(platform_repository: str, ref: str, gh: GhRunner = run_gh) -> str:
    """Accept a SHA as-is; otherwise resolve a branch/tag on the platform repo to its commit SHA."""
    if _SHA.fullmatch(ref):
        return ref
    sha = gh(["api", f"repos/{platform_repository}/commits/{ref}", "--jq", ".sha"]).strip()
    if not _SHA.fullmatch(sha):
        raise OnboardError(f"could not resolve {platform_repository}@{ref} to a commit SHA")
    return sha


def repository_default_branch(project_id: str, gh: GhRunner = run_gh) -> str:
    """The repository's default branch as GitHub reports it (the workflows compare against it)."""
    branch = gh(["api", f"repos/{project_id}", "--jq", ".default_branch"]).strip()
    if not branch:
        raise OnboardError(f"could not read the default branch of {project_id}")
    return branch


# ---------------------------------------------------------------- rendering


def _resource(relative: str) -> str:
    root = importlib.resources.files("agentic_sdlc")
    return root.joinpath("templates", *relative.split("/")).read_text(encoding="utf-8")


def _toml_str(value: str) -> str:
    """A valid TOML basic string (JSON string escapes are a subset TOML accepts)."""
    return json.dumps(value, ensure_ascii=False)


def _toml_list(items: Sequence[str], indent: str = "  ") -> str:
    if not items:
        return "[]"
    body = "".join(f"{indent}{_toml_str(item)},\n" for item in items)
    return f"[\n{body}]"


def _yaml_run(command: str) -> str:
    """A double-quoted YAML scalar (JSON strings are valid YAML), so `: `, `#`, `{`, `*` etc.
    in a shell command cannot change the workflow's structure."""
    return json.dumps(command.replace("\n", " ").strip())


def render_policy(spec: OnboardSpec) -> str:
    forbidden = tuple(dict.fromkeys((*BASE_FORBIDDEN, *spec.forbidden_paths)))
    routing = (
        (
            '\n[routing]\nexecutors = ".forge/executors.json"\n'
            'policy = ".forge/routing-policy.json"\n'
        )
        if spec.routed
        else ""
    )
    implementer = {"route": "router", "cloud-routine": "claude"}.get(
        spec.implementer, spec.implementer
    )
    patterns = "".join(f"  '{p}',\n" for p in BASE_FORBIDDEN_TASKS)
    return f'''\
version = 1

[project]
id = "{spec.project_id}"
provider = "github"
default_branch = "{spec.default_branch}"

[agents]
planner = "codex"
implementer = "{implementer}"
reviewer = "codex"

[automation]
default_mode = "plan"
ready_label = "{spec.ready_label}"
human_review_label = "{HUMAN_REVIEW_LABEL}"
implementation_label = "{IMPLEMENTATION_LABEL}"
lease_ttl_minutes = 240
{routing}
[policy]
human_merge_required = true
max_changed_files = {spec.max_changed_files}
max_diff_lines = {spec.max_diff_lines}
max_patch_bytes = 5000000
forbidden_paths = {_toml_list(forbidden)}
protected_paths = {_toml_list(spec.protected_paths)}
low_risk_paths = ["docs/**", "**/*.md"]
forbidden_task_patterns = [
{patterns}]

[commands]
setup = {_toml_str(spec.setup_command)}
quality = {_toml_str(spec.quality_command)}
test = {_toml_str(spec.test_command)}

[verification]
gates = ["setup", "quality", "test"]
timeout_seconds = {{ setup = 900, quality = 300, test = 900 }}
'''


def render_agents_md(spec: OnboardSpec) -> str:
    return f"""# {spec.project_id} Agent Guide

## Authority

The repository owner defines product intent and remains the final merge authority.
Codex is the primary architect, planner, and independent reviewer. The routed implementer
(or Claude Code) may implement an explicitly approved work request only.

## Required Workflow

0. **Claim before you code.** Every change maps to an issue. Lease it first
   (`sdlcctl claim --project {spec.project_id} --issue <N> --agent <you> --session <id>
   --branch forge/issue-<N>`);
   if the claim is refused, another agent owns it — pick different work. Never work on an
   `in-progress` issue you do not hold. Release (or let the draft PR stand in) when done.
1. Read the relevant code and repository instructions before proposing changes; `git fetch` first.
2. Require complete acceptance criteria, tests, non-goals, and dependencies in the issue.
3. Plan before implementation.
4. Add or update deterministic tests for changed behavior (tests first).
5. Make the smallest scoped implementation; stay within the patch caps in `agentic-sdlc.toml`.
6. Run every configured verification gate locally before opening a change request.
7. Open a draft change request only. Never merge, approve, or deploy.

## Product Invariants

- Verification must not call production systems, paid AI providers, or deployment targets.
- Never commit media, user data, credentials, or `.env*` files; `forbidden_paths` rejects them.
- Touching a `protected_paths` entry adds an architect/security review gate.

## Verification

```bash
{spec.setup_command}
{spec.quality_command}
{spec.test_command}
```

The controlling machine-readable policy is `agentic-sdlc.toml`.
"""


def render_claude_md(spec: OnboardSpec) -> str:
    return f"""# {spec.name} — working agreement for Claude Code

Read `AGENTS.md` first; it is the authoritative agent guide. Highlights:

- **Claim first.** Work only on an issue you lease (`sdlcctl claim … --session <your session id>`);
  the SessionStart hook prints the id and which issues other agents hold. The commit guard blocks
  commits on `*/issue-N` branches without your lease.
- **Tests first.** Every change ships with tests. Run `{spec.test_command}` before
  proposing a change.
- **Merge authority is the repository owner.** Agents open draft PRs only; never merge,
  approve, or deploy.
- **Never touch** `forbidden_paths` from `agentic-sdlc.toml` (workflows, policy, secrets,
  media, data).
- Work requests must follow `.github/ISSUE_TEMPLATE/agent-work-request.md` exactly
  (`## Summary`, `## Acceptance Criteria`, `## Required Tests`, `## Non-Goals`, `## Dependencies`;
  the last two as bullet lists, dependencies as `#N` references or `None`).
"""


def render_cloud_routine_md(spec: OnboardSpec) -> str:
    return f"""# Cloud implementer routine — {spec.project_id}

Implementation for this repository is done by a **Claude Code cloud routine** (a scheduled cloud
agent on claude.ai), not by a GitHub Actions job, so no self-hosted runner is required for it.
The routine must obey `AGENTS.md`, `CLAUDE.md` and `agentic-sdlc.toml` exactly as an Actions
implementer would. Suggested routine prompt (create it with the `schedule` skill or the
claude.ai routines UI):

```
Repository: {spec.project_id}. Every run:
1. List open issues that carry ALL of: `{spec.ready_label}`, `{HUMAN_REVIEW_LABEL}`,
   `{IMPLEMENTATION_LABEL}`
   and have no open PR referencing them. Take the oldest one; if none, stop.
1b. Claim it before touching code:
   `sdlcctl claim --project {spec.project_id} --issue <n> --agent cloud-routine
    --session <run id> --branch forge/issue-<n>`.
   If the claim is refused (exit 2), skip that issue and take the next one.
2. Validate the issue body has the required sections (Summary, Acceptance Criteria, Required Tests,
   Non-Goals, Dependencies). If not, comment what is missing, add label `claude-blocked`, stop.
3. On a new branch `forge/issue-<n>`, implement the smallest change that satisfies the acceptance
   criteria, tests first. Never touch `forbidden_paths` from agentic-sdlc.toml; stay within
   max_changed_files={spec.max_changed_files} and max_diff_lines={spec.max_diff_lines}.
4. Run: {spec.setup_command} && {spec.quality_command} && {spec.test_command}. All must pass.
5. Open a DRAFT pull request titled "<issue title> (#<n>)" with a summary and test plan;
   link the issue. Then `sdlcctl release` the lease (the open PR now marks the work); on any
   failure, release it too so another run can retry.
   Never merge, approve, deploy, or edit workflows/policy files.
```

Required GitHub state (created by `sdlcctl onboard --apply`): the three labels, the
`{RULESET_NAME}` ruleset requiring the `{REQUIRED_CHECK}` check and review-thread resolution.
CI (`.github/workflows/ci.yml`) provides the `{REQUIRED_CHECK}` check and runs on
`{", ".join(spec.runs_on)}`.
"""


def default_executors(spec: OnboardSpec) -> dict:
    common = {
        "adapter": "openai-compatible",
        "adapterVersion": "1.0.0",
        "executionType": "direct-api",
        "authMode": "api-key",
        "taskClasses": ["implementation", "repair"],
        "capabilities": ["edit-code", "author-tests", "run-commands", "repair"],
        "dataResidency": "unspecified",
        "permittedRepositories": [spec.project_id],
    }

    def ex(eid, provider, model_env, alias, family, ctx, conc, risk, quality, cost, tools):
        return {
            "executorId": eid,
            "provider": provider,
            "model": f"configured-by-{model_env}",
            "modelAlias": alias,
            "modelFamily": family,
            "toolCapabilities": tools,
            "contextWindow": ctx,
            "maxConcurrency": conc,
            "maxRisk": risk,
            "qualityLowerBound": quality,
            "directCostUsd": cost,
            **common,
        }

    fc = ["structured-output", "function-calling"]
    lc = [*fc, "long-context"]
    return {
        "schemaVersion": 1,
        "executors": [
            ex(
                "deepseek-v4-flash",
                "deepseek",
                "DEEPSEEK_MODEL_FLASH",
                "deepseek-v4-flash",
                "deepseek",
                1_000_000,
                2,
                "medium",
                0.76,
                0.25,
                fc,
            ),
            ex(
                "deepseek-v4-pro",
                "deepseek",
                "DEEPSEEK_MODEL_PRO",
                "deepseek-v4-pro",
                "deepseek",
                1_000_000,
                2,
                "high",
                0.86,
                0.75,
                fc,
            ),
            ex(
                "glm-5-3",
                "zai",
                "ZAI_MODEL_GLM",
                "glm-5.3",
                "glm",
                256_000,
                1,
                "high",
                0.88,
                0.9,
                lc,
            ),
            ex(
                "kimi-k3",
                "kimi",
                "KIMI_MODEL_K3",
                "kimi-k3",
                "kimi",
                1_000_000,
                1,
                "critical",
                0.95,
                3.0,
                lc,
            ),
            {
                "executorId": "claude-subscription",
                "provider": "anthropic",
                "adapter": "claude-code",
                "adapterVersion": "1.0.0",
                "executionType": "subscription-runner",
                "authMode": "oauth",
                "model": "configured-by-CLAUDE_MODEL",
                "modelAlias": "claude",
                "modelFamily": "claude",
                "taskClasses": ["implementation", "repair", "review"],
                "capabilities": [
                    "edit-code",
                    "author-tests",
                    "run-commands",
                    "repair",
                    "review-code",
                ],
                "toolCapabilities": ["structured-output", "long-context"],
                "contextWindow": 200_000,
                "maxConcurrency": 1,
                "maxRisk": "critical",
                "qualityLowerBound": 0.94,
                "directCostUsd": 0,
                "shadowCostUsd": 2.5,
                "subscriptionMonthlyUsd": 200,
                "subscriptionCapacityRemaining": 1,
                "dataResidency": "unspecified",
                "permittedRepositories": [spec.project_id],
            },
        ],
    }


def default_routing_policy(spec: OnboardSpec) -> dict:
    return {
        "policyVersion": f"{spec.name.lower()}-multiprovider-v1",
        "qualityFloors": {"low": 0.7, "medium": 0.82, "high": 0.9, "critical": 0.94},
        "allowedProviders": ["anthropic", "deepseek", "kimi", "zai"],
        "preferredModelAliases": {
            "implementation:low": [
                "deepseek-v4-flash",
                "deepseek-v4-pro",
                "glm-5.3",
                "kimi-k3",
                "claude",
            ],
            "implementation:medium": [
                "deepseek-v4-pro",
                "deepseek-v4-flash",
                "glm-5.3",
                "kimi-k3",
                "claude",
            ],
            "implementation:high": ["glm-5.3", "deepseek-v4-pro", "kimi-k3", "claude"],
            "implementation:critical": ["kimi-k3", "glm-5.3", "claude"],
            "repair": ["deepseek-v4-pro", "glm-5.3", "kimi-k3", "claude"],
        },
        "requireNoTrainingStorage": False,
        "allowedDataResidency": ["unspecified", "us"],
    }


def _render_workflow(name: str, spec: OnboardSpec, issue_expr: str | None = None) -> str:
    text = _resource(f"github/forge/{name}")
    if "IMPLEMENT_JOBS" in text:
        jobs = _resource("github/forge/_implement-jobs.yml")
        extra = ""
        if spec.routed:
            extra = (
                '          test -n "$DEEPSEEK_API_KEY"\n          test -n "$DEEPSEEK_MODEL_FLASH"\n'
                '          test -n "$DEEPSEEK_MODEL_PRO"\n'
            )
        elif spec.implementer == "claude":
            extra = '          test -n "$CLAUDE_CODE_OAUTH_TOKEN"\n'
        elif spec.implementer == "codex":
            extra = '          test -n "$OPENAI_API_KEY"\n'
        jobs = jobs.replace("PREFLIGHT_EXTRA\n", extra)
        if issue_expr is None:
            issue_expr = "${{ inputs.issue_number }}"
        jobs = jobs.replace("ISSUE_NUMBER_EXPR", issue_expr)
        if name == "agent-auto-implement.yml":
            jobs = jobs.replace(
                "  preflight:\n",
                (
                    "  preflight:\n"
                    "    if: >-\n"
                    "      (github.event.label.name == 'IMPLEMENTATION_LABEL' ||\n"
                    "       github.event.label.name == 'READY_LABEL' ||\n"
                    "       github.event.label.name == 'human-review-required') &&\n"
                    "      github.event.issue.pull_request == null &&\n"
                    "      contains(github.event.issue.labels.*.name, 'IMPLEMENTATION_LABEL') &&\n"
                    "      contains(github.event.issue.labels.*.name, 'READY_LABEL') &&\n"
                    "      contains(github.event.issue.labels.*.name, 'human-review-required')\n"
                ),
                1,
            )
        text = text.replace("IMPLEMENT_JOBS\n", jobs)
    replacements = {
        "PLATFORM_REPOSITORY": spec.platform_repository,
        "PLATFORM_COMMIT_SHA": spec.platform_ref,
        "RUNS_ON_JSON": json.dumps(list(spec.runs_on), separators=(",", ":")),
        "RUNS_ON_YAML": "[" + ", ".join(spec.runs_on) + "]",
        "IMPLEMENTATION_LABEL": IMPLEMENTATION_LABEL,
        "READY_LABEL": spec.ready_label,
        "AGENT": spec.implementer,
        "EXECUTOR_REGISTRY_PATH": ".forge/executors.json" if spec.routed else "",
        "ROUTING_POLICY_PATH": ".forge/routing-policy.json" if spec.routed else "",
        "DEFAULT_BRANCH": spec.default_branch,
        "SETUP_COMMAND": _yaml_run(spec.setup_command),
        "QUALITY_COMMAND": _yaml_run(spec.quality_command),
        "TEST_COMMAND": _yaml_run(spec.test_command),
    }
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text


def render_work_request(spec: OnboardSpec) -> str:
    """The generic work-request template, labelled with this profile's ready label."""
    text = _resource("work-request.md")
    rendered, count = re.subn(
        r"^labels: .*$",
        f"labels: {spec.ready_label}, {HUMAN_REVIEW_LABEL}",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise OnboardError("work-request template has no labels line")
    return rendered


def render_hooks(spec: OnboardSpec) -> dict[str, str]:
    """Claude Code hooks: fetch-and-report at session start; block commits without a lease."""
    settings = {
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "python3 .claude/hooks/forge_session_start.py",
                        }
                    ]
                }
            ],
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "python3 .claude/hooks/forge_commit_guard.py",
                        }
                    ],
                }
            ],
        }
    }
    guard = _resource("hooks/forge_commit_guard.py").replace("PROJECT_ID", spec.project_id)
    start = (
        _resource("hooks/forge_session_start.py")
        .replace("PROJECT_ID", spec.project_id)
        .replace("DEFAULT_BRANCH_NAME", spec.default_branch)
    )
    return {
        ".claude/settings.json": json.dumps(settings, indent=2) + "\n",
        ".claude/hooks/forge_commit_guard.py": guard,
        ".claude/hooks/forge_session_start.py": start,
    }


def render_onboarding(spec: OnboardSpec) -> dict[str, str]:
    """Return {relative path: content} for everything the consumer repo needs."""
    auto_issue = "${{ format('{0}', github.event.issue.number) }}"
    files = {
        **render_hooks(spec),
        "agentic-sdlc.toml": render_policy(spec),
        "AGENTS.md": render_agents_md(spec),
        "CLAUDE.md": render_claude_md(spec),
        ".github/ISSUE_TEMPLATE/agent-work-request.md": render_work_request(spec),
        ".github/workflows/ci.yml": _render_workflow("ci.yml", spec),
        ".github/workflows/agent-plan.yml": _render_workflow("agent-plan.yml", spec),
        ".github/workflows/agent-auto-plan.yml": _render_workflow("agent-auto-plan.yml", spec),
    }
    if spec.actions_implement:
        files[".github/workflows/agent-implement.yml"] = _render_workflow(
            "agent-implement.yml", spec
        )
        files[".github/workflows/agent-auto-implement.yml"] = _render_workflow(
            "agent-auto-implement.yml", spec, issue_expr=auto_issue
        )
    else:
        files["docs/forge/cloud-implementer.md"] = render_cloud_routine_md(spec)
    if spec.routed:
        files[".forge/executors.json"] = json.dumps(default_executors(spec), indent=2) + "\n"
        files[".forge/routing-policy.json"] = (
            json.dumps(default_routing_policy(spec), indent=2) + "\n"
        )
    for name, content in files.items():
        leftover = re.search(
            r"\b(PLATFORM_REPOSITORY|PLATFORM_COMMIT_SHA|RUNS_ON_(?:JSON|YAML)|(?:SETUP|QUALITY|TEST)_COMMAND|ISSUE_NUMBER_EXPR|IMPLEMENT_JOBS|PREFLIGHT_EXTRA)\b",
            content,
        )
        if leftover:
            raise OnboardError(f"unrendered placeholder {leftover.group(0)} in {name}")
    return files


# Files only some implementer modes install. On a forced re-onboard into a different mode the
# ones the new mode does not render are removed, so a stale workflow cannot keep an Actions
# implementer live under `cloud-routine`, nor a stale routine doc make doctor misread the mode.
MODE_OWNED_FILES = (
    ".github/workflows/agent-implement.yml",
    ".github/workflows/agent-auto-implement.yml",
    "docs/forge/cloud-implementer.md",
    ".forge/executors.json",
    ".forge/routing-policy.json",
)


def write_onboarding(
    root: str | Path, spec: OnboardSpec, *, force: bool = False
) -> tuple[Path, ...]:
    base = Path(root).resolve()
    if not base.is_dir() or not (base / ".git").exists():
        raise OnboardError("destination must be an existing Git repository")
    files = render_onboarding(spec)
    existing = sorted(rel for rel in files if (base / rel).exists())
    if existing and not force:
        raise OnboardError(
            "refusing to overwrite existing files (use --force): " + ", ".join(existing)
        )
    written = []
    for rel, content in files.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(path)
    if force:
        for rel in MODE_OWNED_FILES:
            if rel not in files and (base / rel).is_file():
                (base / rel).unlink()
    return tuple(sorted(written))


# ---------------------------------------------------------------- repository settings


def ruleset_payload(spec: OnboardSpec) -> dict:
    return {
        "name": RULESET_NAME,
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {
                "type": "pull_request",
                "parameters": {
                    "required_approving_review_count": 0,
                    "dismiss_stale_reviews_on_push": True,
                    "require_code_owner_review": False,
                    "require_last_push_approval": False,
                    "required_review_thread_resolution": True,
                },
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "required_status_checks": [{"context": REQUIRED_CHECK}],
                },
            },
        ],
    }


def ruleset_mismatches(actual: dict, spec: OnboardSpec) -> list[str]:
    """What an existing ruleset lacks relative to `ruleset_payload` (empty = it enforces it)."""
    want = ruleset_payload(spec)
    problems: list[str] = []
    if actual.get("target") != want["target"]:
        problems.append(f"target is {actual.get('target')!r}")
    if actual.get("enforcement") != "active":
        problems.append(f"enforcement is {actual.get('enforcement')!r}")
    if actual.get("bypass_actors"):
        problems.append("has bypass actors")
    include = ((actual.get("conditions") or {}).get("ref_name") or {}).get("include") or []
    if "~DEFAULT_BRANCH" not in include and f"refs/heads/{spec.default_branch}" not in include:
        problems.append("does not target the default branch")
    exclude = ((actual.get("conditions") or {}).get("ref_name") or {}).get("exclude") or []
    if exclude:  # an exclusion match makes the condition fail; the payload excludes nothing
        problems.append("excludes " + ", ".join(map(str, exclude)))
    rules = {r.get("type"): r.get("parameters") or {} for r in actual.get("rules") or []}
    for rule in want["rules"]:
        if rule["type"] not in rules:
            problems.append(f"missing rule {rule['type']}")
    if "pull_request" in rules and not rules["pull_request"].get(
        "required_review_thread_resolution"
    ):
        problems.append("review-thread resolution not required")
    if "required_status_checks" in rules:
        contexts = {
            c.get("context")
            for c in rules["required_status_checks"].get("required_status_checks") or []
        }
        if REQUIRED_CHECK not in contexts:
            problems.append(f"status check '{REQUIRED_CHECK}' not required")
        if not rules["required_status_checks"].get("strict_required_status_checks_policy"):
            problems.append("status checks are not strict (branch must be up to date)")
    return problems


def apply_repo_settings(
    spec: OnboardSpec, gh: GhRunner = run_gh, *, variables: dict[str, str] | None = None
) -> list[str]:
    """Idempotently create labels, the protect-main ruleset and repo variables. Returns a log."""
    log: list[str] = []
    for name, (color, description) in spec.labels.items():
        gh(
            [
                "label",
                "create",
                name,
                "--repo",
                spec.project_id,
                "--color",
                color,
                "--description",
                description,
                "--force",
            ]
        )
        log.append(f"label {name}")
    existing = _repo_rulesets(spec.project_id, gh)
    current = next((r for r in existing if r.get("name") == RULESET_NAME), None)
    if current is not None:
        detail = json.loads(
            gh(["api", f"repos/{spec.project_id}/rulesets/{current.get('id')}"]) or "{}"
        )
        problems = ruleset_mismatches(detail, spec)
        if problems:
            gh(
                [
                    "api",
                    "-X",
                    "PUT",
                    f"repos/{spec.project_id}/rulesets/{current.get('id')}",
                    "--input",
                    "-",
                ],
                input=json.dumps(ruleset_payload(spec)),
            )
            log.append(f"ruleset '{RULESET_NAME}' updated ({'; '.join(problems)})")
        else:
            log.append(f"ruleset '{RULESET_NAME}' already present")
    else:
        gh(
            ["api", "-X", "POST", f"repos/{spec.project_id}/rulesets", "--input", "-"],
            input=json.dumps(ruleset_payload(spec)),
        )
        log.append(f"ruleset '{RULESET_NAME}' created")
    for key, value in (variables or {}).items():
        gh(["variable", "set", key, "--repo", spec.project_id, "--body", value])
        log.append(f"variable {key}")
    return log


def copy_variables(
    source_project: str, names: Sequence[str], gh: GhRunner = run_gh
) -> dict[str, str]:
    """Read non-secret repo variables from an already-onboarded repo (model names, app id)."""
    out: dict[str, str] = {}
    for name in names:
        try:
            value = gh(["variable", "get", name, "--repo", source_project]).strip()
        except OnboardError:
            continue
        if value:
            out[name] = value
    return out


# ---------------------------------------------------------------- doctor


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""
    manual: bool = False  # True = only the owner can fix (secrets, app install, runner)

    def as_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "detail": self.detail, "manual": self.manual}


@dataclass
class DoctorReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "checks": [c.as_dict() for c in self.checks]}

    def render(self) -> str:
        lines = []
        for c in self.checks:
            mark = "PASS" if c.ok else ("TODO" if c.manual else "FAIL")
            lines.append(f"[{mark}] {c.name}" + (f" — {c.detail}" if c.detail else ""))
        lines.append("READY" if self.ok else "NOT READY")
        return "\n".join(lines)


def _safe_json(gh: GhRunner, args: Sequence[str]):
    try:
        return json.loads(gh(args) or "null")
    except (OnboardError, json.JSONDecodeError):
        return None


def _paged_items(gh: GhRunner, path: str, key: str | None = None) -> list[dict]:
    """Every item of a paginated list endpoint. --slurp wraps the pages in one array; each page
    is either a JSON array (key=None) or an object carrying the items under `key`."""
    pages = _safe_json(gh, ["api", path, "--paginate", "--slurp"])
    items: list = []
    for page in pages if isinstance(pages, list) else []:
        if key is None:
            items += page if isinstance(page, list) else [page]
        elif isinstance(page, dict):
            items += page.get(key) or []
    return [item for item in items if isinstance(item, dict)]


def _paged_names(gh: GhRunner, path: str, key: str) -> set[str]:
    return {str(item.get("name")) for item in _paged_items(gh, path, key)}


def _repo_rulesets(project_id: str, gh: GhRunner) -> list[dict]:
    """Rulesets owned by the repository itself: every page, without inherited org rulesets
    (a repository admin cannot update those through the repository endpoint)."""
    rows = _paged_items(gh, f"repos/{project_id}/rulesets?includes_parents=false")
    return [r for r in rows if r.get("source_type", "Repository") == "Repository"]


# What each generated caller must call.
CALLER_TARGETS = {
    "agent-plan.yml": "reusable-plan.yml",
    "agent-auto-plan.yml": "reusable-plan.yml",
    "agent-implement.yml": "reusable-implement.yml",
    "agent-auto-implement.yml": "reusable-implement.yml",
}
# Installation permissions the publisher/lease tokens request in reusable-implement.yml.
#: API-key secret per routed provider (the Anthropic executor uses the OAuth token instead).
ROUTED_PROVIDER_SECRETS = {
    "deepseek": "DEEPSEEK_API_KEY",
    "zai": "ZAI_API_KEY",
    "kimi": "KIMI_API_KEY",
}

PUBLISHER_PERMISSIONS = {
    "contents": ("read", "write"),
    "issues": ("write",),
    "pull_requests": ("write",),
}


def _ci_runs_on(base: Path) -> set[str] | None:
    """Labels of ci.yml's `test` job runner (None when the file/job cannot be read)."""
    import yaml  # deferred: the CLI must import without site dependencies

    try:
        doc = yaml.safe_load((base / ".github/workflows/ci.yml").read_text())
        job = (doc.get("jobs") or {}).get(REQUIRED_CHECK) or {}
    except (OSError, yaml.YAMLError, AttributeError):
        return None
    runs_on = job.get("runs-on")
    if isinstance(runs_on, str):
        return {runs_on.strip()}
    if isinstance(runs_on, list):
        return {str(x).strip() for x in runs_on}
    if isinstance(runs_on, dict):
        labels = runs_on.get("labels") or []
        return {str(x).strip() for x in (labels if isinstance(labels, list) else [labels])}
    return None


def _ci_problems(base: Path) -> list[str]:
    """ci.yml must run on pull_request, as a job named `test`, the policy's three commands."""
    import yaml  # deferred: the CLI must import without site dependencies

    try:
        doc = yaml.safe_load((base / ".github/workflows/ci.yml").read_text())
        commands = tomllib.loads((base / "agentic-sdlc.toml").read_text()).get("commands") or {}
    except (OSError, yaml.YAMLError, tomllib.TOMLDecodeError) as exc:
        return [str(exc)[:200]]
    if not isinstance(doc, dict):
        return ["not a workflow mapping"]
    triggers = doc.get("on", doc.get(True))  # YAML 1.1 reads a bare `on` key as True
    problems = []
    if not (
        triggers == "pull_request"
        or (isinstance(triggers, list | dict) and "pull_request" in triggers)
    ):
        problems.append("not triggered on pull_request")
    job = (doc.get("jobs") or {}).get(REQUIRED_CHECK)
    if not isinstance(job, dict):
        return [*problems, f"no '{REQUIRED_CHECK}' job"]
    runs = " \n".join(
        str(step.get("run", "")) for step in job.get("steps") or [] if isinstance(step, dict)
    )
    for gate in ("setup", "quality", "test"):
        command = str(commands.get(gate, "")).replace("\n", " ").strip()
        if not command:
            problems.append(f"policy has no [commands] {gate}")
        elif command not in runs:
            problems.append(f"'{REQUIRED_CHECK}' job does not run the {gate} command {command!r}")
    return problems


def doctor(
    root: str | Path,
    project_id: str,
    platform_repository: str,
    gh: GhRunner = run_gh,
    *,
    remote: bool = True,
) -> DoctorReport:
    base = Path(root).resolve()
    rep = DoctorReport()
    add = rep.checks.append

    # --- files
    required = [
        "agentic-sdlc.toml",
        "AGENTS.md",
        ".github/ISSUE_TEMPLATE/agent-work-request.md",
        ".github/workflows/agent-plan.yml",
        ".github/workflows/agent-auto-plan.yml",
        ".github/workflows/ci.yml",
        "CLAUDE.md",
        ".claude/settings.json",
        ".claude/hooks/forge_commit_guard.py",
        ".claude/hooks/forge_session_start.py",
    ]
    cloud = (base / "docs/forge/cloud-implementer.md").exists()
    if not cloud:
        required += [
            ".github/workflows/agent-implement.yml",
            ".github/workflows/agent-auto-implement.yml",
        ]
    missing = [r for r in required if not (base / r).exists()]
    settings = base / ".claude/settings.json"
    if settings.exists():
        text = settings.read_text()
        missing += [
            f".claude/settings.json does not run {hook}"
            for hook in ("forge_session_start.py", "forge_commit_guard.py")
            if hook not in text
        ]
    add(Check("required files present", not missing, ", ".join(missing) if missing else ""))
    actions_impl = [
        n
        for n in ("agent-implement.yml", "agent-auto-implement.yml")
        if (base / ".github/workflows" / n).exists()
    ]
    if cloud and actions_impl:
        add(
            Check(
                "implementation profile is unambiguous",
                False,
                "docs/forge/cloud-implementer.md is present alongside "
                + ", ".join(actions_impl)
                + " — a cloud-routine repo must not keep Actions implementer workflows "
                "(their preflight needs the Publisher App); remove one side",
            )
        )
    if (base / ".github/workflows/ci.yml").exists() and (base / "agentic-sdlc.toml").exists():
        ci = _ci_problems(base)
        add(Check(f"ci.yml runs the policy gates as '{REQUIRED_CHECK}'", not ci, "; ".join(ci)))

    policy = None
    try:
        policy = load_policy(base / "agentic-sdlc.toml")
        add(
            Check(
                "agentic-sdlc.toml loads",
                True,
                "labels "
                f"{policy.ready_label}/{policy.human_review_label}/{policy.implementation_label}",
            )
        )
    except Exception as exc:  # noqa: BLE001 - report any loader failure
        add(Check("agentic-sdlc.toml loads", False, str(exc)[:200]))
    if policy is not None:
        # Every plan/implement run passes $GITHUB_REPOSITORY as --expected-project-id.
        same = policy.project_id == project_id
        add(
            Check(
                "policy project id matches the repository",
                same,
                "" if same else f"policy names {policy.project_id}, not {project_id}",
            )
        )

    routed_secrets: set[str] = set()
    routed_vars: set[str] = set()
    routing_files = (".forge/executors.json", ".forge/routing-policy.json")
    toml_path = base / "agentic-sdlc.toml"
    implement_callers = [
        base / ".github/workflows" / n for n in ("agent-implement.yml", "agent-auto-implement.yml")
    ]
    # Route mode is what the installed callers/policy ask for, not whether the registry exists.
    routed = (
        any((base / f).exists() for f in routing_files)
        or (toml_path.exists() and re.search(r"^\[routing\]", toml_path.read_text(), re.M))
        or any(
            c.exists() and re.search(r"^\s*agent:\s*route\s*$", c.read_text(), re.M)
            for c in implement_callers
        )
    )
    missing_routing = [f for f in routing_files if not (base / f).exists()]
    if routed and missing_routing:
        add(
            Check(
                "routing files valid and permit this repo",
                False,
                "route mode but missing " + ", ".join(missing_routing),
            )
        )
    elif routed:
        try:
            executors = load_executors(json.loads((base / ".forge/executors.json").read_text()))
            load_routing_policy(json.loads((base / ".forge/routing-policy.json").read_text()))
            usable = [e for e in executors if project_id in e.permitted_repositories]
            add(
                Check(
                    "routing files valid and permit this repo",
                    bool(usable),
                    f"{len(usable)} of {len(executors)} executors permit this repository"
                    if usable
                    else "no executor lists this repository",
                )
            )
            # Every enabled fallback must be configured, or the router advances into a
            # missing key after the first recoverable DeepSeek failure.
            for e in usable:
                if e.provider not in ROUTED_PROVIDER_SECRETS:
                    continue  # the Anthropic executor runs on the OAuth token, no repo config
                routed_secrets.add(ROUTED_PROVIDER_SECRETS[e.provider])
                var = re.fullmatch(r"configured-by-([A-Z0-9_]+)", e.model or "")
                if var:
                    routed_vars.add(var.group(1))
        except (OSError, ExecutorError, json.JSONDecodeError, AttributeError) as exc:
            add(Check("routing files valid and permit this repo", False, str(exc)[:200]))

    ref = None
    plan = base / ".github/workflows/agent-plan.yml"
    callers = [
        base / ".github/workflows" / name
        for name in (
            "agent-plan.yml",
            "agent-auto-plan.yml",
            "agent-implement.yml",
            "agent-auto-implement.yml",
        )
        if (base / ".github/workflows" / name).exists()
    ]
    if plan.exists():
        pins: set[str] = set()
        problems: list[str] = []
        for caller in callers:
            uses = re.findall(
                r"uses:\s*(\S+/\.github/workflows/reusable-[\w-]+\.yml@\S+)", caller.read_text()
            )
            if not uses:
                problems.append(f"{caller.name} calls no reusable workflow")
            for target in uses:
                repo_part, _, rest = target.partition("/.github/workflows/")
                workflow, _, pin = rest.rpartition("@")
                expected = CALLER_TARGETS.get(caller.name)
                if expected and workflow != expected:
                    problems.append(f"{caller.name} calls {workflow}, expected {expected}")
                elif repo_part != platform_repository or not _SHA.fullmatch(pin):
                    problems.append(f"{caller.name} uses {target}")
                else:
                    pins.add(pin)
        if len(pins) > 1:
            problems.append(
                "callers pin different SHAs: " + ", ".join(sorted(p[:12] for p in pins))
            )
        ref = next(iter(pins)) if len(pins) == 1 and not problems else None
        add(
            Check(
                "workflows pin the platform to a commit SHA",
                ref is not None,
                "; ".join(problems),
            )
        )
        if policy is not None:
            auto = base / ".github/workflows/agent-auto-implement.yml"
            if auto.exists():
                import yaml  # deferred: the CLI must import without site dependencies

                try:
                    preflight = (yaml.safe_load(auto.read_text()) or {})["jobs"]["preflight"]
                    condition = str(preflight.get("if", ""))
                except (yaml.YAMLError, KeyError, TypeError, AttributeError):
                    condition = ""
                # Every approval label must both trigger and be required on the issue, or a
                # ready label alone could start credentialed implementation.
                lacking = [
                    label
                    for label in (
                        policy.ready_label,
                        policy.human_review_label,
                        policy.implementation_label,
                    )
                    if f"github.event.label.name == '{label}'" not in condition
                    or f"contains(github.event.issue.labels.*.name, '{label}')" not in condition
                ]
                add(
                    Check(
                        "workflow label conditions match the policy labels",
                        not lacking,
                        ""
                        if not lacking
                        else "auto-implement preflight does not require " + ", ".join(lacking),
                    )
                )
    if not remote:
        return rep

    # --- platform reachable at that pin
    if ref:
        probes = ["reusable-plan.yml"]
        if not cloud:
            probes.append("reusable-implement.yml")
        absent = [
            name
            for name in probes
            if not _safe_json(
                gh,
                [
                    "api",
                    f"repos/{platform_repository}/contents/.github/workflows/{name}?ref={ref}",
                    "--jq",
                    "{path: .path}",
                ],
            )
        ]
        add(
            Check(
                "platform ref reachable (reusable workflows exist at pin)",
                not absent,
                ref[:12] + (f": missing {', '.join(absent)}" if absent else ""),
            )
        )

    # --- default branch in the policy is the repository's actual default branch
    if policy is not None:
        repo_info = _safe_json(gh, ["api", f"repos/{project_id}"])
        actual_branch = repo_info.get("default_branch") if isinstance(repo_info, dict) else None
        add(
            Check(
                "policy default_branch matches the repository",
                actual_branch == policy.default_branch,
                f"policy {policy.default_branch!r}, GitHub {actual_branch!r}",
            )
        )

    # --- labels
    labels = (
        _safe_json(gh, ["label", "list", "--repo", project_id, "--json", "name", "--limit", "200"])
        or []
    )
    names = {x.get("name") for x in labels}
    want = (
        {policy.ready_label, policy.human_review_label, policy.implementation_label}
        if policy
        else {"claude-ready", HUMAN_REVIEW_LABEL, IMPLEMENTATION_LABEL}
    ) | {IN_PROGRESS_LABEL}  # claim() adds it; a missing label fails every lease
    lacking = sorted(want - names)
    add(
        Check(
            "Forge labels exist",
            not lacking,
            ", ".join(lacking) if lacking else ", ".join(sorted(want)),
        )
    )

    # --- ruleset
    rulesets = _repo_rulesets(project_id, gh)
    protect = next((r for r in rulesets if r.get("name") == RULESET_NAME), None)
    if protect is None:
        add(Check(f"ruleset '{RULESET_NAME}' active on default branch", False, "not found"))
    else:
        detail = _safe_json(gh, ["api", f"repos/{project_id}/rulesets/{protect.get('id')}"])
        branch = policy.default_branch if policy else "main"
        problems = (
            ruleset_mismatches(
                detail,
                OnboardSpec(
                    project_id=project_id,
                    platform_repository=platform_repository,
                    platform_ref="0" * 40,
                    test_command="-",
                    default_branch=branch,
                ),
            )
            if isinstance(detail, dict)
            else ["could not read the ruleset"]
        )
        add(
            Check(
                f"ruleset '{RULESET_NAME}' active on default branch",
                not problems,
                "; ".join(problems),
            )
        )

    # --- variables & secrets (names only)
    variable_values = {
        str(v.get("name")): str(v.get("value") or "")
        for v in _paged_items(gh, f"repos/{project_id}/actions/variables", "variables")
    }
    variables = {name for name, value in variable_values.items() if value.strip()}
    empty_vars = sorted(name for name, value in variable_values.items() if not value.strip())
    secrets = _paged_names(gh, f"repos/{project_id}/actions/secrets", "secrets")
    need_vars = set() if cloud else {"PUBLISHER_APP_CLIENT_ID"}
    need_secrets = {"CLAUDE_CODE_OAUTH_TOKEN"} | (set() if cloud else {"PUBLISHER_APP_PRIVATE_KEY"})
    platform_private = _safe_json(gh, ["api", f"repos/{platform_repository}", "--jq", ".private"])
    if platform_private is True:  # GITHUB_TOKEN cannot check out another private repository
        need_secrets |= {"PLATFORM_READ_TOKEN"}
    if routed:
        need_vars |= routed_vars or {"DEEPSEEK_MODEL_FLASH", "DEEPSEEK_MODEL_PRO"}
        need_secrets |= routed_secrets or {"DEEPSEEK_API_KEY"}
    implement_wf = base / ".github/workflows/agent-implement.yml"
    if implement_wf.exists() and re.search(
        r"^\s*agent:\s*codex\s*$", implement_wf.read_text(), re.MULTILINE
    ):
        need_secrets |= {"OPENAI_API_KEY"}
    mv, ms = sorted(need_vars - variables), sorted(need_secrets - secrets)
    empty_required = [name for name in mv if name in empty_vars]
    add(
        Check(
            "repo variables set (non-empty)",
            not mv,
            (
                ", ".join(mv)
                + (f" (empty value: {', '.join(empty_required)})" if empty_required else "")
            )
            if mv
            else ", ".join(sorted(need_vars)),
        )
    )
    add(
        Check(
            "repo secrets set (by name)",
            not ms,
            ("missing: " + ", ".join(ms) + " → gh secret set NAME --repo " + project_id)
            if ms
            else ", ".join(sorted(need_secrets)),
            manual=True,
        )
    )

    # --- runner (only when workflows target self-hosted labels)
    plan_text = plan.read_text() if plan.exists() else ""
    self_hosted = "self-hosted" in plan_text if plan.exists() else True
    wanted_labels: set[str] = set()
    m = re.search(r"^\s*runs-on:\s*\[([^\]]*)\]", plan_text, re.MULTILINE)
    if m:
        wanted_labels = {x.strip().strip("'\"") for x in m.group(1).split(",") if x.strip()}
    runners = (
        _paged_items(gh, f"repos/{project_id}/actions/runners", "runners") if self_hosted else []
    )
    online = [
        r
        for r in runners
        if r.get("status") == "online"
        and wanted_labels <= {str(lbl.get("name")) for lbl in r.get("labels") or []}
    ]
    if not self_hosted:
        add(Check("runner", True, "GitHub-hosted runners (billed minutes on private repos)"))
    else:
        add(
            Check(
                "self-hosted runner online for this repo",
                bool(online),
                f"{len(online)} online with labels {sorted(wanted_labels)} / "
                f"{len(runners)} registered"
                if runners
                else "none registered → gh api -X POST "
                f"repos/{project_id}/actions/runners/registration-token, "
                "then config.sh on a runner VM",
                manual=True,
            )
        )

    # --- ci.yml's `test` job must run somewhere that exists, or every PR blocks on a queued check
    ci_labels = _ci_runs_on(base)
    if ci_labels is not None:
        if "self-hosted" in ci_labels:
            all_runners = runners or _paged_items(
                gh, f"repos/{project_id}/actions/runners", "runners"
            )
            ci_ok = any(
                r.get("status") == "online"
                and ci_labels <= {str(lbl.get("name")) for lbl in r.get("labels") or []}
                for r in all_runners
            )
            add(
                Check(
                    "ci.yml test job runs on an available runner",
                    ci_ok,
                    f"labels {sorted(ci_labels)}: "
                    + ("online runner found" if ci_ok else "no online runner carries all of them"),
                    manual=not ci_ok,
                )
            )
        else:
            add(
                Check(
                    "ci.yml test job runs on an available runner",
                    True,
                    ", ".join(sorted(ci_labels)),
                )
            )

    # --- publisher app installed on this repo (Actions implementer publishes PRs through it)
    if cloud:
        add(
            Check(
                "Publisher GitHub App", True, "not needed: the cloud routine opens PRs as the owner"
            )
        )
        return rep
    installs = _paged_items(gh, "/user/installations", "installations")
    apps = [i for i in installs if PUBLISHER_APP_SLUG_HINT in str(i.get("app_slug", "")).lower()]
    if not apps:
        add(
            Check(
                "Publisher GitHub App installed on this repo",
                False,
                "no installation of the Agentic SDLC Publisher app visible to this user",
                manual=True,
            )
        )
    else:
        # The app may be installed on several accounts; the repo can be under any of them.
        holding = [
            app
            for app in apps
            if project_id
            in {
                str(r.get("full_name"))
                for r in _paged_items(
                    gh, f"/user/installations/{app['id']}/repositories", "repositories"
                )
            }
        ]
        if not holding:
            detail = f"add {project_id} under the app's 'Only select repositories'"
        else:
            shortfalls = []
            for app in holding:
                perms = app.get("permissions") or {}
                shortfalls.append(
                    [
                        f"{name}: {perms.get(name, 'none')} (needs {'/'.join(allowed)})"
                        for name, allowed in PUBLISHER_PERMISSIONS.items()
                        if perms.get(name) not in allowed
                    ]
                )
            weak = min(shortfalls, key=len)
            detail = ("grant the app " + "; ".join(weak)) if weak else ""
        add(
            Check(
                "Publisher GitHub App installed on this repo",
                bool(holding) and not detail,
                detail,
                manual=True,
            )
        )
    return rep
