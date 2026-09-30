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
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .executors import ExecutorError, load_executors, load_routing_policy
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


# ---------------------------------------------------------------- rendering


def _resource(relative: str) -> str:
    root = importlib.resources.files("agentic_sdlc")
    return root.joinpath("templates", *relative.split("/")).read_text(encoding="utf-8")


def _toml_list(items: Sequence[str], indent: str = "  ") -> str:
    if not items:
        return "[]"
    body = "".join(f'{indent}"{item}",\n' for item in items)
    return f"[\n{body}]"


def _yaml_run(command: str) -> str:
    return command.replace("\n", " ").strip()


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
setup = "{spec.setup_command}"
quality = "{spec.quality_command}"
test = "{spec.test_command}"

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
        ".github/ISSUE_TEMPLATE/agent-work-request.md": _resource("work-request.md"),
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
    existing = json.loads(gh(["api", f"repos/{spec.project_id}/rulesets"]) or "[]")
    if any(r.get("name") == RULESET_NAME for r in existing):
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
        ".github/workflows/ci.yml",
    ]
    cloud = (base / "docs/forge/cloud-implementer.md").exists()
    if not cloud:
        required += [
            ".github/workflows/agent-implement.yml",
            ".github/workflows/agent-auto-implement.yml",
        ]
    missing = [r for r in required if not (base / r).exists()]
    add(Check("required files present", not missing, ", ".join(missing) if missing else ""))

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

    routed = (base / ".forge/executors.json").exists()
    if routed:
        try:
            executors = load_executors(json.loads((base / ".forge/executors.json").read_text()))
            load_routing_policy(json.loads((base / ".forge/routing-policy.json").read_text()))
            permitted = all(project_id in e.permitted_repositories for e in executors)
            add(
                Check(
                    "routing files valid and permit this repo",
                    permitted,
                    "" if permitted else "an executor does not list this repository",
                )
            )
        except (OSError, ExecutorError, json.JSONDecodeError, AttributeError) as exc:
            add(Check("routing files valid and permit this repo", False, str(exc)[:200]))

    ref = None
    plan = base / ".github/workflows/agent-plan.yml"
    if plan.exists():
        m = re.search(
            rf"{re.escape(platform_repository)}/\.github/workflows/reusable-plan\.yml@([0-9a-f]{{40}})",
            plan.read_text(),
        )
        ref = m.group(1) if m else None
        add(Check("workflows pin the platform to a commit SHA", ref is not None))
        if policy is not None:
            auto = base / ".github/workflows/agent-auto-implement.yml"
            if auto.exists():
                consistent = f"'{policy.ready_label}'" in auto.read_text()
                add(
                    Check(
                        "workflow label conditions match the policy labels",
                        consistent,
                        ""
                        if consistent
                        else f"auto-implement does not reference '{policy.ready_label}'",
                    )
                )
    if not remote:
        return rep

    # --- platform reachable at that pin
    if ref:
        found = _safe_json(
            gh,
            [
                "api",
                f"repos/{platform_repository}/contents/.github/workflows/reusable-implement.yml?ref={ref}",
                "--jq",
                "{path: .path}",
            ],
        )
        add(
            Check("platform ref reachable (reusable workflows exist at pin)", bool(found), ref[:12])
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
    )
    lacking = sorted(want - names)
    add(
        Check(
            "Forge labels exist",
            not lacking,
            ", ".join(lacking) if lacking else ", ".join(sorted(want)),
        )
    )

    # --- ruleset
    rulesets = _safe_json(gh, ["api", f"repos/{project_id}/rulesets"]) or []
    protect = next(
        (r for r in rulesets if r.get("name") == RULESET_NAME and r.get("enforcement") == "active"),
        None,
    )
    add(Check(f"ruleset '{RULESET_NAME}' active on default branch", protect is not None))

    # --- variables & secrets (names only)
    variables = {
        v.get("name")
        for v in (
            _safe_json(gh, ["api", f"repos/{project_id}/actions/variables", "--jq", ".variables"])
            or []
        )
    }
    secrets = {
        s.get("name")
        for s in (
            _safe_json(gh, ["api", f"repos/{project_id}/actions/secrets", "--jq", ".secrets"]) or []
        )
    }
    need_vars = set() if cloud else {"PUBLISHER_APP_CLIENT_ID"}
    need_secrets = {"CLAUDE_CODE_OAUTH_TOKEN"} | (set() if cloud else {"PUBLISHER_APP_PRIVATE_KEY"})
    if routed:
        need_vars |= {"DEEPSEEK_MODEL_FLASH", "DEEPSEEK_MODEL_PRO"}
        need_secrets |= {"DEEPSEEK_API_KEY"}
    mv, ms = sorted(need_vars - variables), sorted(need_secrets - secrets)
    add(Check("repo variables set", not mv, ", ".join(mv) if mv else ", ".join(sorted(need_vars))))
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
    self_hosted = "self-hosted" in plan.read_text() if plan.exists() else True
    runners = (
        (_safe_json(gh, ["api", f"repos/{project_id}/actions/runners", "--jq", ".runners"]) or [])
        if self_hosted
        else []
    )
    online = [r for r in runners if r.get("status") == "online"]
    if not self_hosted:
        add(Check("runner", True, "GitHub-hosted runners (billed minutes on private repos)"))
    else:
        add(
            Check(
                "self-hosted runner online for this repo",
                bool(online),
                f"{len(online)} online / {len(runners)} registered"
                if runners
                else "none registered → gh api -X POST "
                f"repos/{project_id}/actions/runners/registration-token, "
                "then config.sh on a runner VM",
                manual=True,
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
    installs = _safe_json(gh, ["api", "/user/installations", "--jq", ".installations"]) or []
    app = next(
        (i for i in installs if PUBLISHER_APP_SLUG_HINT in str(i.get("app_slug", "")).lower()), None
    )
    if app is None:
        add(
            Check(
                "Publisher GitHub App installed on this repo",
                False,
                "no installation of the Agentic SDLC Publisher app visible to this user",
                manual=True,
            )
        )
    else:
        repos = (
            _safe_json(
                gh,
                [
                    "api",
                    f"/user/installations/{app['id']}/repositories",
                    "--paginate",
                    "--jq",
                    "[.repositories[].full_name]",
                ],
            )
            or []
        )
        flat = [
            x
            for item in (repos if isinstance(repos, list) else [])
            for x in (item if isinstance(item, list) else [item])
        ]
        ok = project_id in flat
        add(
            Check(
                "Publisher GitHub App installed on this repo",
                ok,
                "" if ok else f"add {project_id} under the app's 'Only select repositories'",
                manual=True,
            )
        )
    return rep
