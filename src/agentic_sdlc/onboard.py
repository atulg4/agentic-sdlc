"""One-command Forge onboarding for a consumer repository.

`scaffold` installs a generic, fail-closed profile. `onboard` installs the *production* shape the
consumer repos actually run (self-hosted runners, preflight/notify jobs, `claude-ready` labels,
provider routing) and, with `--apply`, configures the GitHub repository itself (labels, ruleset,
variables). `doctor` verifies a repository and lists the remaining manual steps.
"""

from __future__ import annotations

import base64
import importlib.resources
import json
import math
import re
import shlex
import subprocess
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .executors import (
    AuthMode,
    ExecutorError,
    RouteRequest,
    TaskClass,
    load_executors,
    load_routing_policy,
    request_from_mission,
    route_executor,
)
from .leases import IN_PROGRESS_LABEL
from .missions import MissionError, load_registry
from .openai_compatible import PROVIDERS as OPENAI_COMPATIBLE_PROVIDERS
from .openai_compatible import ROUTED_MIN_CONTEXT_WINDOW
from .policy import load_policy

GhRunner = Callable[..., str]  # (args: Sequence[str], input: str | None = None) -> stdout

_PROJECT = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_RUNNER_LABEL = re.compile(r"^[A-Za-z0-9_.-]+$")

IMPLEMENTERS = ("route", "claude", "codex", "cloud-routine")
#: `[agents] implementation_mode` in agentic-sdlc.toml: the single source of truth for whether an
#: Actions workflow or a Claude Code cloud routine implements approved issues.
IMPLEMENTATION_MODES = ("actions", "cloud-routine")
DEFAULT_RUNS_ON = ("self-hosted", "linux", "x64")
#: Where a PUBLIC repository's CI runs by default: every fork pull request executes its own code
#: in that job, so it must be a fresh GitHub-hosted VM, never a persistent self-hosted runner.
PUBLIC_CI_RUNS_ON = ("ubuntu-latest",)
DEFAULT_QUALITY = "python -m ruff check --select E9,F63,F7,F82 ."
HUMAN_REVIEW_LABEL = "human-review-required"
IMPLEMENTATION_LABEL = "implementation-approved"
RULESET_NAME = "Protect main"
REQUIRED_CHECK = "test"
#: The GitHub Actions app: the only integration that reports ci.yml's `test` job. A required
#: status check pinned (`integration_id`) to any other app can never be satisfied by it.
GITHUB_ACTIONS_INTEGRATION_ID = 15368
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
    #: Runner for ci.yml (the only pull_request-triggered workflow); () = `runs_on`, or
    #: PUBLIC_CI_RUNS_ON for a public repository.
    ci_runs_on: tuple[str, ...] = ()
    #: The repository is public: fork pull requests reach ci.yml.
    public: bool = False
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
        if not self.runs_on or not all(
            _RUNNER_LABEL.fullmatch(x) for x in (*self.runs_on, *self.ci_runs_on)
        ):
            raise OnboardError("runs_on labels must be simple tokens")
        windows = _windows_labels((*self.runs_on, *self.ci_runs_on))
        if windows:
            raise OnboardError(f"{WINDOWS_UNSUPPORTED} (got {', '.join(windows)})")
        if self.actions_implement and not _linux_labels(self.runs_on):
            raise OnboardError(
                f"{LINUX_IMPLEMENTATION_REQUIRED} (got --runs-on {','.join(self.runs_on)})"
            )
        if self.public and not _github_hosted(set(self.ci_labels)):
            raise OnboardError(
                "a public repository's CI runs fork pull-request code: it must use a "
                f"GitHub-hosted runner (e.g. --ci-runs-on {PUBLIC_CI_RUNS_ON[0]}), not "
                + ",".join(self.ci_labels)
            )
        if not self.test_command.strip():
            raise OnboardError("a test command is required; the gate fails closed without one")
        if (
            not self.ready_label.strip()
            or len(self.ready_label) > 50
            or any(ord(c) < 32 or c == "," for c in self.ready_label)
        ):
            # GitHub's 50-character limit; a comma would split the work-request front matter
            # and a control character the generated `if:` blocks.
            raise OnboardError("ready label must be 1-50 characters without commas or controls")
        if len({self.ready_label, HUMAN_REVIEW_LABEL, IMPLEMENTATION_LABEL}) != 3:
            raise OnboardError("ready label must differ from the review/approval labels")
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", self.default_branch):
            raise OnboardError("default branch contains unsupported characters")

    @property
    def name(self) -> str:
        return self.project_id.split("/")[1]

    @property
    def ci_labels(self) -> tuple[str, ...]:
        """Runner labels of ci.yml's `test` job."""
        if self.ci_runs_on:
            return self.ci_runs_on
        return PUBLIC_CI_RUNS_ON if self.public else self.runs_on

    @property
    def routed(self) -> bool:
        return self.implementer == "route"

    @property
    def actions_implement(self) -> bool:
        """False when a Claude Code cloud routine implements instead of a GitHub Actions job."""
        return self.implementer != "cloud-routine"

    @property
    def implementation_mode(self) -> str:
        """The policy's `[agents] implementation_mode` (IMPLEMENTATION_MODES)."""
        return "actions" if self.actions_implement else "cloud-routine"

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


def repository_is_public(project_id: str, gh: GhRunner = run_gh) -> bool:
    """Whether GitHub reports the repository as public (fork pull requests can reach its CI)."""
    info = json.loads(gh(["api", f"repos/{project_id}"]) or "null")
    if not isinstance(info, dict) or ("private" not in info and "visibility" not in info):
        raise OnboardError(f"could not read the visibility of {project_id}")
    return _is_public(info)


def _is_public(repo_info: dict) -> bool:
    if "visibility" in repo_info:
        return str(repo_info["visibility"]).lower() == "public"
    return repo_info.get("private") is False


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


def _yaml_str(value: object) -> str:
    """THE way a string reaches a generated YAML file: a double-quoted scalar (JSON strings are
    valid YAML). `true`, `null`, `on`, `123` stay strings; `: `, `#`, `{`, `*` cannot change the
    document's structure. Every string placeholder in the workflow templates goes through here."""
    return json.dumps(str(value), ensure_ascii=False)


def _yaml_labels(labels: Sequence[str]) -> str:
    """A YAML flow sequence of quoted strings (runner labels stay labels, never true/null/123)."""
    return "[" + ", ".join(_yaml_str(label) for label in labels) + "]"


def _yaml_run(command: str) -> str:
    """A shell command as one quoted scalar (`_yaml_str`), newlines folded to spaces."""
    return _yaml_str(command.replace("\n", " ").strip())


def _expr_str(value: str) -> str:
    """A GitHub expression string literal: single-quoted, `'` doubled."""
    return "'" + str(value).replace("'", "''") + "'"


def auto_plan_condition(ready: str, human_review: str, implementation: str) -> str:
    """The `if:` of agent-auto-plan.yml's `plan` job. Only the ready label triggers: an issue
    opened with both labels fires one `labeled` run per label, and both would plan."""
    return (
        f"github.event.label.name == {_expr_str(ready)} &&\n"
        "github.event.issue.pull_request == null &&\n"
        f"contains(github.event.issue.labels.*.name, {_expr_str(ready)}) &&\n"
        f"contains(github.event.issue.labels.*.name, {_expr_str(human_review)}) &&\n"
        f"!contains(github.event.issue.labels.*.name, {_expr_str(implementation)})"
    )


def auto_implement_condition(ready: str, human_review: str, implementation: str) -> str:
    """The `if:` of agent-auto-implement.yml's `preflight` job: any approval label triggers, and
    every one of them must be on the issue, so a ready label alone never starts implementation."""
    return (
        f"(github.event.label.name == {_expr_str(implementation)} ||\n"
        f" github.event.label.name == {_expr_str(ready)} ||\n"
        f" github.event.label.name == {_expr_str(human_review)}) &&\n"
        "github.event.issue.pull_request == null &&\n"
        f"contains(github.event.issue.labels.*.name, {_expr_str(implementation)}) &&\n"
        f"contains(github.event.issue.labels.*.name, {_expr_str(ready)}) &&\n"
        f"contains(github.event.issue.labels.*.name, {_expr_str(human_review)})"
    )


def _yaml_folded(text: str, indent: str) -> str:
    """`text` as a YAML folded block scalar (`>-`) at `indent`: readable conditions in the
    generated files. Callers guarantee no line is blank or control-character laden."""
    return ">-\n" + "\n".join(indent + line for line in text.splitlines())


def _normalized_expr(text: object) -> str:
    """An expression with its whitespace collapsed: how two `if:` conditions are compared."""
    return " ".join(str(text).split())


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
# Where implementation runs: "actions" (agent-implement.yml / agent-auto-implement.yml) or
# "cloud-routine" (docs/forge/cloud-implementer.md). `sdlcctl doctor` reads this, never infers it.
implementation_mode = "{spec.implementation_mode}"

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
`{", ".join(spec.ci_labels)}`.
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
                # A literal model: the Claude adapter in reusable-implement.yml passes the routed
                # model straight to --model and, unlike openai_compatible, resolves no
                # `configured-by-VAR` (same model as that workflow's legacy `agent: claude` route).
                "model": "claude-opus-5",
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


#: Every placeholder the workflow templates carry; each is a WHOLE YAML value.
WORKFLOW_PLACEHOLDERS = (
    "PLATFORM_REPOSITORY",
    "PLATFORM_COMMIT_SHA",
    "REUSABLE_PLAN_USES",
    "REUSABLE_IMPLEMENT_USES",
    "RUNS_ON_JSON",
    "CI_RUNS_ON_YAML",
    "RUNS_ON_YAML",
    "ISSUE_NUMBER_EXPR",
    "AGENT",
    "EXECUTOR_REGISTRY_PATH",
    "ROUTING_POLICY_PATH",
    "DEFAULT_BRANCH",
    "SETUP_COMMAND",
    "QUALITY_COMMAND",
    "TEST_COMMAND",
    "AUTO_PLAN_IF",
    "AUTO_IMPLEMENT_IF",
)


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
        if name == "agent-auto-implement.yml":
            jobs = jobs.replace("  preflight:\n", "  preflight:\n    if: AUTO_IMPLEMENT_IF\n", 1)
        text = text.replace("IMPLEMENT_JOBS\n", jobs)
    labels = (spec.ready_label, HUMAN_REVIEW_LABEL, IMPLEMENTATION_LABEL)
    platform = spec.platform_repository
    # Every placeholder is a WHOLE YAML value: strings go through `_yaml_str`, label lists
    # through `_yaml_labels`, conditions as folded blocks of quoted expression literals.
    replacements = {
        "PLATFORM_REPOSITORY": _yaml_str(platform),
        "PLATFORM_COMMIT_SHA": _yaml_str(spec.platform_ref),
        "REUSABLE_PLAN_USES": _yaml_str(
            f"{platform}/.github/workflows/reusable-plan.yml@{spec.platform_ref}"
        ),
        "REUSABLE_IMPLEMENT_USES": _yaml_str(
            f"{platform}/.github/workflows/reusable-implement.yml@{spec.platform_ref}"
        ),
        "RUNS_ON_JSON": _yaml_str(json.dumps(list(spec.runs_on), separators=(",", ":"))),
        "CI_RUNS_ON_YAML": _yaml_labels(spec.ci_labels),
        "RUNS_ON_YAML": _yaml_labels(spec.runs_on),
        "ISSUE_NUMBER_EXPR": _yaml_str(issue_expr or "${{ inputs.issue_number }}"),
        "AGENT": _yaml_str(spec.implementer),
        "EXECUTOR_REGISTRY_PATH": _yaml_str(".forge/executors.json" if spec.routed else ""),
        "ROUTING_POLICY_PATH": _yaml_str(".forge/routing-policy.json" if spec.routed else ""),
        "DEFAULT_BRANCH": _yaml_str(spec.default_branch),
        "SETUP_COMMAND": _yaml_run(spec.setup_command),
        "QUALITY_COMMAND": _yaml_run(spec.quality_command),
        "TEST_COMMAND": _yaml_run(spec.test_command),
        "AUTO_PLAN_IF": _yaml_folded(auto_plan_condition(*labels), " " * 6),
        "AUTO_IMPLEMENT_IF": _yaml_folded(auto_implement_condition(*labels), " " * 6),
    }
    if set(replacements) != set(WORKFLOW_PLACEHOLDERS):
        raise OnboardError("workflow placeholders and their renderings disagree")
    # One pass, whole words: a substituted value is never re-scanned for placeholder names.
    pattern = re.compile(r"\b(" + "|".join(sorted(replacements, key=len, reverse=True)) + r")\b")
    return pattern.sub(lambda m: replacements[m.group(1)], text)


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
    # The placeholders sit in Python string literals ("PROJECT_ID"): substitute the whole
    # literal with a JSON string, which is a valid Python string literal.
    guard = _resource("hooks/forge_commit_guard.py").replace(
        '"PROJECT_ID"', json.dumps(spec.project_id)
    )
    start = (
        _resource("hooks/forge_session_start.py")
        .replace('"PROJECT_ID"', json.dumps(spec.project_id))
        .replace('"DEFAULT_BRANCH_NAME"', json.dumps(spec.default_branch))
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
    common = (
        r"\b(PLATFORM_REPOSITORY|PLATFORM_COMMIT_SHA|(?:CI_)?RUNS_ON_(?:JSON|YAML)|"
        r"(?:SETUP|QUALITY|TEST)_COMMAND|ISSUE_NUMBER_EXPR|IMPLEMENT_JOBS|PREFLIGHT_EXTRA)\b"
    )
    workflow = re.compile(
        r"\b(" + "|".join((*WORKFLOW_PLACEHOLDERS, "IMPLEMENT_JOBS", "PREFLIGHT_EXTRA")) + r")\b"
    )
    for name, content in files.items():
        pattern = workflow if name.startswith(".github/workflows/") else re.compile(common)
        leftover = pattern.search(content)
        if leftover:
            raise OnboardError(f"unrendered placeholder {leftover.group(0)} in {name}")
    return files


# Files only some implementer modes install. On a forced re-onboard into a different mode the
# ones the new mode does not render are removed, so a stale workflow cannot keep an Actions
# implementer live under `cloud-routine` (doctor reads the mode from the policy, and flags a
# leftover file from the other mode as ambiguous).
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
        checks = [
            c
            for c in rules["required_status_checks"].get("required_status_checks") or []
            if isinstance(c, dict)
        ]
        contexts = {c.get("context") for c in checks}
        if REQUIRED_CHECK not in contexts:
            problems.append(f"status check '{REQUIRED_CHECK}' not required")
        foreign = sorted(
            {
                str(c.get("integration_id"))
                for c in checks
                if c.get("context") == REQUIRED_CHECK and _foreign_integration(c)
            }
        )
        if foreign:
            problems.append(
                f"status check '{REQUIRED_CHECK}' only accepts integration "
                f"{', '.join(foreign)}, not GitHub Actions ({GITHUB_ACTIONS_INTEGRATION_ID}): "
                "ci.yml's job can never satisfy it"
            )
        if not rules["required_status_checks"].get("strict_required_status_checks_policy"):
            problems.append("status checks are not strict (branch must be up to date)")
    return problems


def _foreign_integration(check: dict) -> bool:
    """A required status check bound to an app other than GitHub Actions."""
    bound = check.get("integration_id")
    return bound is not None and bound != GITHUB_ACTIONS_INTEGRATION_ID


#: Ruleset fields a PUT accepts; everything else a GET returns (id, source, _links, ...) is
#: read-only.
_RULESET_WRITABLE = ("name", "target", "enforcement", "bypass_actors", "conditions", "rules")
#: Integer parameters where a larger value is stricter.
_RULESET_MAX_PARAMS = ("required_approving_review_count",)


def _merge_rule_parameters(existing: dict, forge: dict) -> dict:
    """Forge's parameters merged into an existing rule's, never weakening either: every existing
    key survives, booleans stay true when either side sets them, counts take the larger value,
    and the required status checks are the union (an existing check keeps its integration)."""
    merged = dict(existing)
    for key, want in forge.items():
        have = merged.get(key)
        if key not in merged:
            merged[key] = want
        elif isinstance(want, bool):
            merged[key] = bool(have) or want
        elif key in _RULESET_MAX_PARAMS:
            try:
                merged[key] = max(int(have), int(want))
            except (TypeError, ValueError):
                merged[key] = want
        elif key == "required_status_checks":
            checks = [c for c in have if isinstance(c, dict)] if isinstance(have, list) else []
            # Forge's own `test` context is reported by GitHub Actions: a binding to another
            # app is dropped (never preserved -- it could not be satisfied), and duplicates fold.
            kept: list[dict] = []
            for check in checks:
                if check.get("context") == REQUIRED_CHECK:
                    if _foreign_integration(check):
                        check = {k: v for k, v in check.items() if k != "integration_id"}
                    if any(k.get("context") == check.get("context") for k in kept):
                        continue
                kept.append(check)
            contexts = {c.get("context") for c in kept}
            merged[key] = kept + [
                c for c in want if isinstance(c, dict) and c.get("context") not in contexts
            ]
    return merged


def merged_ruleset(actual: dict, spec: OnboardSpec) -> dict:
    """The PUT body that makes an existing `Protect main` ruleset enforce Forge's requirements
    while keeping every stronger control it already has.

    Every existing rule is kept (required signatures, linear history, code scanning, ...); a rule
    Forge also sets is merged parameter by parameter (`_merge_rule_parameters`); a Forge rule it
    lacks is added. Forge's own requirements win where they are the stricter side: enforcement
    active, no bypass actors, the default branch included and nothing excluded.
    """
    want = ruleset_payload(spec)
    out = {k: actual[k] for k in _RULESET_WRITABLE if k in actual}
    out.update(
        name=want["name"],
        target=want["target"],
        enforcement=want["enforcement"],
        bypass_actors=[],
    )
    conditions = dict(actual.get("conditions") or {})
    ref_name = dict(conditions.get("ref_name") or {})
    include = [str(x) for x in ref_name.get("include") or []]
    if "~DEFAULT_BRANCH" not in include and f"refs/heads/{spec.default_branch}" not in include:
        include.append("~DEFAULT_BRANCH")
    conditions["ref_name"] = {**ref_name, "include": include, "exclude": []}
    out["conditions"] = conditions
    rules: list[dict] = []
    index: dict[str, int] = {}
    for rule in actual.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        kind = str(rule.get("type"))
        if kind in index:  # a duplicate type: fold it into the first, keeping the stricter
            first = rules[index[kind]]
            first["parameters"] = _merge_rule_parameters(
                first.get("parameters") or {}, rule.get("parameters") or {}
            )
            continue
        index[kind] = len(rules)
        rules.append(json.loads(json.dumps(rule)))
    for rule in want["rules"]:
        kind = rule["type"]
        if kind not in index:
            index[kind] = len(rules)
            rules.append(json.loads(json.dumps(rule)))
        elif "parameters" in rule:
            current = rules[index[kind]]
            current["parameters"] = _merge_rule_parameters(
                current.get("parameters") or {}, rule["parameters"]
            )
    out["rules"] = rules
    return out


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
                input=json.dumps(merged_ruleset(detail, spec)),
            )
            log.append(
                f"ruleset '{RULESET_NAME}' updated, existing rules kept ({'; '.join(problems)})"
            )
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


def registry_model_vars(registry: dict) -> list[str]:
    """Repo variables an executor registry names through `configured-by-VAR` models."""
    names = []
    for entry in registry.get("executors") or []:
        found = re.fullmatch(r"configured-by-([A-Z0-9_]+)", str((entry or {}).get("model", "")))
        if found and found.group(1) not in names:
            names.append(found.group(1))
    return names


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
#: API-key secret per routed provider: every OpenAI-compatible provider's own key, plus Codex.
#: Anthropic depends on the executor's auth mode (`_executor_secret`).
ROUTED_PROVIDER_SECRETS = {
    "codex": "OPENAI_API_KEY",
    **{name: config.api_key_env for name, config in OPENAI_COMPATIBLE_PROVIDERS.items()},
}
ANTHROPIC_AUTH_SECRETS = {
    AuthMode.OAUTH: "CLAUDE_CODE_OAUTH_TOKEN",
    AuthMode.API_KEY: "ANTHROPIC_API_KEY",
}

#: Every runner label GitHub hosts, with its OS family: the finite list in GitHub's "GitHub-hosted
#: runners reference" (standard and larger macOS labels), NOT a version-shaped pattern -- a label
#: GitHub does not provide (`ubuntu-99.04`, `macos-99`) queues forever. Windows labels are listed
#: only to be recognised and rejected (`_windows_labels`): the generated workflows are bash/Unix.
GITHUB_HOSTED_LABELS: dict[str, str] = {
    **dict.fromkeys(
        (
            "ubuntu-latest",
            "ubuntu-24.04",
            "ubuntu-22.04",
            "ubuntu-24.04-arm",
            "ubuntu-22.04-arm",
            "ubuntu-slim",
        ),
        "linux",
    ),
    **dict.fromkeys(
        (
            "macos-latest",
            "macos-26",
            "macos-15",
            "macos-14",
            "macos-13",
            "macos-15-intel",
            "macos-latest-large",
            "macos-15-large",
            "macos-14-large",
            "macos-13-large",
            "macos-latest-xlarge",
            "macos-26-xlarge",
            "macos-15-xlarge",
            "macos-14-xlarge",
            "macos-13-xlarge",
        ),
        "macos",
    ),
    **dict.fromkeys(
        ("windows-latest", "windows-2025", "windows-2022", "windows-2019", "windows-11-arm"),
        "windows",
    ),
}

#: Providers reusable-implement.yml can generate a patch with once routed: Codex, Claude Code and
#: every provider the OpenAI-compatible adapter configures. Anything else stops at "needs an
#: installed patch-generation adapter".
ROUTED_ADAPTER_PROVIDERS = frozenset({"codex", "anthropic", *OPENAI_COMPATIBLE_PROVIDERS})
ANTHROPIC_AUTH_MODES = frozenset({AuthMode.OAUTH, AuthMode.API_KEY})


def _adapter_gap(executor) -> str:
    """Why the implement workflow cannot run this executor ('' when it can)."""
    if executor.provider not in ROUTED_ADAPTER_PROVIDERS:
        return f"provider {executor.provider} has no workflow adapter"
    if executor.provider == "anthropic":
        if executor.auth_mode not in ANTHROPIC_AUTH_MODES:
            return f"Claude adapter has no auth mode {executor.auth_mode.value}"
        if executor.model.startswith("configured-by-"):
            return "Claude adapter passes the model verbatim; configured-by- is not resolved"
    return ""


#: The request reusable-implement.yml's "Route executor" step hands `route-executor` on every
#: implementation run (`--mission-id`, `--task-class`, `--required-tool-capability`,
#: `--min-context-window`, and the `route_budget_usd` input's default). The CLI turns the mission
#: into risk and required capabilities exactly as `implementation_route_request` does below;
#: test_onboard pins these constants to the workflow text.
ROUTE_MISSION_ID = "implementation-worker"
ROUTE_TASK_CLASS = TaskClass.IMPLEMENTATION
ROUTE_TOOL_CAPABILITIES = ("structured-output",)
ROUTE_MIN_CONTEXT_WINDOW = ROUTED_MIN_CONTEXT_WINDOW
ROUTE_DEFAULT_BUDGET_USD = 10.0


def implementation_route_request(
    policy, project_id: str, budget_usd: float = ROUTE_DEFAULT_BUDGET_USD
) -> RouteRequest:
    """The `RouteRequest` an implementation run of this repository sends the router."""
    mission = load_registry(None, policy).get(ROUTE_MISSION_ID)
    return request_from_mission(
        mission,
        repository=project_id,
        task_class=ROUTE_TASK_CLASS,
        budget_usd=budget_usd,
        required_tool_capabilities=ROUTE_TOOL_CAPABILITIES,
        min_context_window=ROUTE_MIN_CONTEXT_WINDOW,
    )


def _route_budget(value: object) -> float | None:
    """A `route_budget_usd` as `route-executor --budget-usd` reads it: the string GitHub passes,
    a finite non-negative number (None: the run would fail or route on a value doctor cannot
    know -- an expression, a word, a boolean, NaN)."""
    if isinstance(value, bool):
        return None
    text = _input_text(value)
    if text is None or "${{" in text:
        return None
    try:
        budget = float(text)
    except ValueError:
        return None
    return budget if math.isfinite(budget) and budget >= 0 else None


def _caller_route_budgets(callers: Sequence[Path]) -> tuple[set[float], list[str]]:
    """(budgets, problems): the `route_budget_usd` each implement call hands
    reusable-implement.yml (the input's default when it passes none), and every explicit value
    doctor cannot read -- which fails the diagnosis instead of being replaced by the default."""
    budgets: set[float] = set()
    problems: list[str] = []
    for caller in callers:
        for name, job in _implement_calls(caller):
            passed = job.get("with") if isinstance(job.get("with"), dict) else {}
            raw = passed.get("route_budget_usd", ROUTE_DEFAULT_BUDGET_USD)
            budget = _route_budget(raw)
            if budget is None:
                problems.append(f"{caller.name}:{name} route_budget_usd {raw!r}")
            else:
                budgets.add(budget)
    return budgets, problems


#: reusable-implement.yml's `agent` input default (test_onboard pins it to the workflow).
REUSABLE_IMPLEMENT_DEFAULT_AGENT = "codex"
REUSABLE_IMPLEMENT_AGENTS = frozenset({"codex", "claude", "route"})


def _implement_agents(callers: Sequence[Path]) -> tuple[dict[str, str], list[str]]:
    """(`file:job` -> agent, problems): the parsed `with.agent` of every reusable-implement.yml
    call (its default when absent). An expression or an agent the workflow rejects is a
    problem: doctor cannot say which adapter -- and which credentials -- the run needs."""
    agents: dict[str, str] = {}
    problems: list[str] = []
    for caller in callers:
        for name, job in _implement_calls(caller):
            passed = job.get("with") if isinstance(job.get("with"), dict) else {}
            raw = passed.get("agent", REUSABLE_IMPLEMENT_DEFAULT_AGENT)
            text = _input_text(raw)
            if text is None or text.strip() not in REUSABLE_IMPLEMENT_AGENTS:
                problems.append(f"{caller.name}:{name} agent {raw!r}")
            else:
                agents[f"{caller.name}:{name}"] = text.strip()
    return agents, problems


def route_candidates(executors, routing_policy, request: RouteRequest) -> dict[str, list[str]]:
    """executor id -> the router's own rejection reasons for `request` (empty = selectable).

    Calls `executors.route_executor()` itself: the ONE eligibility rule behind every routed doctor
    check (selectable executor, workflow adapter, required secrets and variables), so READY
    cannot drift from what the router does.
    """
    decision = route_executor(request, executors, routing_policy)
    return {
        str(c["executorId"]): [str(r) for r in c.get("rejectionReasons") or []]
        for c in decision.candidates
    }


def _executor_secret(executor) -> str | None:
    """The repository secret the implement workflow hands this executor's adapter."""
    if executor.provider == "anthropic":
        return ANTHROPIC_AUTH_SECRETS.get(executor.auth_mode)
    return ROUTED_PROVIDER_SECRETS.get(executor.provider)


#: The Publisher App's EXACT installation permissions (docs/github-publisher-app.md: Contents
#: read-only, Issues and Pull requests read/write, everything else "No access"). Its stored key
#: can mint a token with anything the installation holds, so a broader grant -- Contents write,
#: Workflows, Administration -- is a finding, not a pass. `metadata: read` is mandatory for every
#: GitHub App and the only other permission tolerated.
PUBLISHER_PERMISSIONS = {"contents": "read", "issues": "write", "pull_requests": "write"}
PUBLISHER_IMPLICIT_PERMISSIONS = {"metadata": "read"}


def publisher_permission_problems(perms: dict) -> list[str]:
    """How an installation's permissions differ from PUBLISHER_PERMISSIONS (empty = exact)."""
    perms = perms if isinstance(perms, dict) else {}
    problems = [
        f"{name}: {perms.get(name, 'none')} (needs exactly {level})"
        for name, level in PUBLISHER_PERMISSIONS.items()
        if perms.get(name) != level
    ]
    for name, level in sorted(perms.items()):
        if name in PUBLISHER_PERMISSIONS or str(level).lower() in {"none", ""}:
            continue
        if PUBLISHER_IMPLICIT_PERMISSIONS.get(name) == level:
            continue
        problems.append(f"{name}: {level} (remove: outside the publisher boundary)")
    return problems


def _workflow_doc(path: Path) -> dict | None:
    import yaml  # deferred: the CLI must import without site dependencies

    try:
        doc = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        return None
    return doc if isinstance(doc, dict) else None


def _workflow_jobs(path: Path) -> dict[str, dict]:
    jobs = (_workflow_doc(path) or {}).get("jobs")
    if not isinstance(jobs, dict):
        return {}
    return {str(k): v for k, v in jobs.items() if isinstance(v, dict)}


_SECRET_REF = re.compile(r"^\$\{\{\s*(secrets|vars)\.([A-Za-z0-9_]+)\s*\}\}$")


def _preflight_requirements(callers: Sequence[Path]) -> tuple[set[str], set[str]]:
    """(secrets, variables) each installed caller's preflight fails without: an env entry bound to
    `secrets.X`/`vars.X` that its script checks with `test -n "$NAME"` (a parsed command)."""
    secrets: set[str] = set()
    variables: set[str] = set()
    for caller in callers:
        for step in (_workflow_jobs(caller).get("preflight") or {}).get("steps") or []:
            if not isinstance(step, dict):
                continue
            # The parsed commands, not the text: a comment or an echo checks nothing. Any
            # `test -n "$NAME"` / `[ -n "$NAME" ]` counts, enforced or not (over-requiring a
            # credential is the safe side).
            checked = {
                argv[2].removeprefix("$").strip("{}")
                for argv, _ in _shell_flow(str(step.get("run", "")), step.get("shell"))
                if len(argv) >= 3
                and (argv[0] == "test" or (argv[0] == "[" and argv[-1] == "]"))
                and argv[1] == "-n"
            }
            env = step.get("env")
            for name, value in (env if isinstance(env, dict) else {}).items():
                ref = _SECRET_REF.match(str(value).strip())
                if ref and str(name) in checked:
                    (secrets if ref.group(1) == "secrets" else variables).add(ref.group(2))
    return secrets, variables


def reusable_calls(caller: Path) -> list[tuple[str, str, str, str]]:
    """(job, repository, workflow file, ref) of every job whose parsed `uses` calls a remote
    reusable workflow (`owner/repo/.github/workflows/<file>@<ref>`)."""
    calls = []
    for name, job in _workflow_jobs(caller).items():
        found = re.fullmatch(
            r"([^/\s]+/[^/\s]+)/\.github/workflows/([^@\s/]+)@(\S+)", str(job.get("uses", ""))
        )
        if found:
            calls.append((name, found.group(1), found.group(2), found.group(3)))
    return calls


def _implement_calls(caller: Path) -> list[tuple[str, dict]]:
    """(job name, job) of every job whose parsed `uses` calls a remote reusable-implement.yml."""
    return [
        (name, job)
        for name, job in _workflow_jobs(caller).items()
        for found in [_REMOTE_REUSABLE.fullmatch(str(job.get("uses", "")).strip())]
        if found and found.group(2) == "reusable-implement.yml"
    ]


def _unforwarded_secrets(caller: Path, needed: set[str]) -> list[str]:
    """`job: names` for each reusable-implement.yml call of `caller` that does not pass every
    secret in `needed`. Each call is judged on its OWN `secrets:` (another job's forwarding or
    `secrets: inherit` reaches nothing here); `secrets: inherit` on the call passes them all."""
    gaps = []
    for name, job in _implement_calls(caller):
        passed = job.get("secrets")
        if passed == "inherit":
            continue
        forwarded = {str(k) for k in passed} if isinstance(passed, dict) else set()
        if needed - forwarded:
            gaps.append(f"{caller.name}:{name}: {', '.join(sorted(needed - forwarded))}")
    return gaps


def _github_hosted(labels: set[str]) -> bool:
    """Exactly one label, and one GitHub actually hosts (any OS: a fresh VM either way)."""
    return len(labels) == 1 and next(iter(labels)) in GITHUB_HOSTED_LABELS


def _windows_labels(labels) -> list[str]:
    """Labels that put a job on Windows: a hosted Windows image, or a self-hosted runner's
    `windows` OS label. The generated workflows run `set -euo pipefail` and `.venv/bin`."""
    return sorted(
        str(x)
        for x in labels
        if GITHUB_HOSTED_LABELS.get(str(x)) == "windows" or str(x).lower() == "windows"
    )


WINDOWS_UNSUPPORTED = (
    "Windows runners are not supported: the generated workflows run bash with Unix paths "
    "(`set -euo pipefail`, `.venv/bin`); use an ubuntu-*/macos-* GitHub-hosted label or a "
    "self-hosted Linux/macOS runner"
)


LINUX_IMPLEMENTATION_REQUIRED = (
    "the implementation workflows run on Linux only: reusable-implement.yml installs bubblewrap "
    "for Claude Code with apt-get, which macOS lacks; use a GitHub-hosted ubuntu-* label or "
    "self-hosted labels that include `linux` (macOS is fine for ci.yml via --ci-runs-on)"
)


def _linux_labels(labels) -> bool:
    """The labels select a Linux runner: one GitHub-hosted Linux label, or a self-hosted set that
    names `linux` (the OS label every self-hosted Linux runner carries) and no other OS."""
    names = {str(x) for x in labels}
    lowered = {x.lower() for x in names}
    if lowered & {"macos", "windows"} or any(
        GITHUB_HOSTED_LABELS.get(x) in {"macos", "windows"} for x in names
    ):
        return False
    if _github_hosted(names):
        return GITHUB_HOSTED_LABELS[next(iter(names))] == "linux"
    return "linux" in lowered


def _runner_on_windows(runner: dict) -> bool:
    """A registered runner whose OS is Windows (the runners API's `os`, or its `windows` default
    label), whatever labels a workflow asked for."""
    labels = {str(lbl.get("name", "")).lower() for lbl in runner.get("labels") or []}
    return str(runner.get("os") or "").lower().startswith("windows") or "windows" in labels


def _runner_available(labels: set[str], runners: Sequence[dict]) -> bool:
    """A standard GitHub-hosted label, or an online NON-Windows runner carrying every label.
    Anything else -- self-hosted, a typo, an unresolved expression -- would queue forever. A
    Windows target, or a match only on a Windows runner, is never available: the generated jobs
    run bash with Unix paths."""
    if _windows_labels(labels):
        return False
    if _github_hosted(labels):
        return True
    return bool(labels) and any(
        r.get("status") == "online"
        and not _runner_on_windows(r)
        and labels <= {str(lbl.get("name")) for lbl in r.get("labels") or []}
        for r in runners
    )


def _fires_on_issue_labeled(doc: dict) -> bool:
    """The workflow runs when a label is added to an issue: an `issues` trigger whose activity
    types (all of them when unlisted) include `labeled`."""
    triggers = doc.get("on", doc.get(True))
    if isinstance(triggers, str):
        return triggers == "issues"
    if isinstance(triggers, list):
        return "issues" in triggers
    if not isinstance(triggers, dict) or "issues" not in triggers:
        return False
    spec = triggers["issues"]
    if spec is None:
        return True
    if not isinstance(spec, dict):
        return False
    types = spec.get("types")
    if types is None:
        return True
    return types == "labeled" or (isinstance(types, list) and "labeled" in types)


def _triggers(doc: dict) -> set[str]:
    """Event names a workflow triggers on (YAML 1.1 reads a bare `on` key as True)."""
    triggers = doc.get("on", doc.get(True))
    if isinstance(triggers, str):
        return {triggers}
    if isinstance(triggers, list | dict):
        return {str(t) for t in triggers}
    return set()


#: Platform reusable workflows whose EVERY job runs on `${{ fromJSON(inputs.runs_on) }}`
#: (tests/test_onboard.py pins this against the files): called from the platform repository at a
#: pinned SHA with an explicit `with.runs_on`, they run exactly there.
PLATFORM_RUNS_ON_WORKFLOWS = frozenset(
    {
        "reusable-ci-repair.yml",
        "reusable-implement.yml",
        "reusable-orchestrate.yml",
        "reusable-plan.yml",
        "reusable-pre-review.yml",
        "reusable-protected-merge.yml",
        "reusable-repair.yml",
        "reusable-review.yml",
        "reusable-spec.yml",
        "reusable-transient-retry.yml",
    }
)
_INPUT_REF = re.compile(r"^\$\{\{\s*inputs\.([A-Za-z0-9_-]+)\s*\}\}$")
_FROMJSON_INPUT_REF = re.compile(
    r"^\$\{\{\s*fromJSON\(\s*inputs\.([A-Za-z0-9_-]+)\s*\)\s*\}\}$", re.IGNORECASE
)
_REMOTE_REUSABLE = re.compile(r"([^/\s]+/[^/\s]+)/\.github/workflows/([^@\s/]+)@(\S+)")

#: A job's runner target: its labels, or why they cannot be determined (a str).
RunnerTarget = set[str] | str


def _input_text(value: object) -> str | None:
    """A workflow input's value as the string GitHub passes (None: not a plain scalar)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float | str):
        return str(value)
    return None


def _resolve_input(value: object, inputs: dict[str, str | None] | None) -> tuple[object, bool]:
    """(value, resolved): a whole-value `${{ inputs.X }}` or `${{ fromJSON(inputs.X) }}`
    substituted from `inputs`; any other expression is unresolvable."""
    if not isinstance(value, str) or "${{" not in value:
        return value, True
    text = value.strip()
    for pattern, parse in ((_INPUT_REF, False), (_FROMJSON_INPUT_REF, True)):
        found = pattern.match(text)
        if found and inputs is not None and inputs.get(found.group(1)) is not None:
            raw = str(inputs[found.group(1)])
            if not parse:
                return raw, True
            try:
                return json.loads(raw), True
            except json.JSONDecodeError:
                return None, False
    return None, False


def _resolved_labels(value: object, inputs: dict[str, str | None] | None) -> set[str] | None:
    """The labels a `runs-on` value names after input substitution (None: unresolvable, or a
    runner group, which cannot be proven GitHub-hosted)."""
    value, ok = _resolve_input(value, inputs)
    if not ok:
        return None
    if isinstance(value, str):
        return None if "${{" in value else {value.strip()}
    if isinstance(value, list):
        labels: set[str] = set()
        for item in value:
            item, ok = _resolve_input(item, inputs)
            if not ok or not isinstance(item, str) or "${{" in item:
                return None
            labels.add(item.strip())
        return labels or None
    if isinstance(value, dict) and "labels" in value and "group" not in value:
        return _resolved_labels(value["labels"], inputs)
    return None


def _unresolved_target(value: object, what: str) -> str:
    """Why a runner target is not a provable label set. A runner GROUP is named as such: GitHub
    also requires the runner to be in that group, and group membership is an organization
    setting the repository runners API does not report -- so it fails closed rather than
    passing on a same-labelled runner outside the group."""
    if isinstance(value, dict) and "group" in value:
        return (
            f"runner group {value['group']!r}: membership cannot be verified through the "
            "repository API; target runner labels instead"
        )
    return f"{what} {value!r} unresolvable"


def _call_inputs(
    called: dict, passed: dict, inputs: dict[str, str | None] | None
) -> dict[str, str | None]:
    """The inputs a reusable workflow receives: its `workflow_call` defaults, overridden by the
    caller's `with:` (resolved against the caller's own inputs; None = unresolvable)."""
    triggers = called.get("on", called.get(True))
    call = triggers.get("workflow_call") if isinstance(triggers, dict) else None
    declared = call.get("inputs") if isinstance(call, dict) else None
    out: dict[str, str | None] = {}
    for name, spec in (declared if isinstance(declared, dict) else {}).items():
        if isinstance(spec, dict) and "default" in spec:
            default, ok = _resolve_input(spec["default"], None)
            out[str(name)] = _input_text(default) if ok else None
    for name, value in passed.items():
        value, ok = _resolve_input(value, inputs)
        out[str(name)] = _input_text(value) if ok else None
    return out


def _reusable_call_targets(
    base: Path,
    where: str,
    job: dict,
    platform_repository: str,
    inputs: dict[str, str | None] | None,
    seen: tuple[Path, ...],
) -> dict[str, RunnerTarget]:
    """Runner targets of a job that `uses:` a reusable workflow."""
    uses = str(job.get("uses", "")).strip()
    passed = job.get("with") if isinstance(job.get("with"), dict) else {}
    if uses.startswith("./"):
        workflows = (base / ".github/workflows").resolve()
        target = (base / uses[2:]).resolve()
        if (
            "@" in uses
            or target.parent != workflows
            or target.suffix not in {".yml", ".yaml"}
            or not target.is_file()
        ):
            return {where: f"local reusable workflow {uses} not found"}
        if target in seen:
            return {where: f"reusable workflow cycle through {uses}"}
        called = _workflow_doc(target)
        if called is None:
            return {where: f"{uses} is not a readable workflow"}
        nested = _job_runner_targets(
            base,
            target,
            platform_repository,
            _call_inputs(called, passed, inputs),
            seen,
        )
        return {f"{where} > {k}": v for k, v in nested.items()}
    found = _REMOTE_REUSABLE.fullmatch(uses)
    if (
        found
        and found.group(1) == platform_repository
        and found.group(2) in PLATFORM_RUNS_ON_WORKFLOWS
        and _SHA.fullmatch(found.group(3))
    ):
        key = f"{where} (runs_on)"
        if "runs_on" not in passed:
            return {key: f"calls {found.group(2)} without an explicit runs_on"}
        raw, ok = _resolve_input(passed["runs_on"], inputs)
        value: object = passed["runs_on"]
        labels = None
        if ok and isinstance(raw, str):
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                value = raw
            else:
                labels = _resolved_labels(value, None)
        return {key: labels if labels is not None else _unresolved_target(value, "runs_on")}
    return {where: f"calls {uses}: its runners cannot be verified from this repository"}


def _job_runner_targets(
    base: Path,
    path: Path,
    platform_repository: str,
    inputs: dict[str, str | None] | None = None,
    seen: tuple[Path, ...] = (),
) -> dict[str, RunnerTarget]:
    """`file:job` -> where each job of a workflow runs, following local reusable workflows
    (`./.github/workflows/x.yml`) recursively with their `with:` inputs substituted, and Forge
    platform reusable workflows through their `runs_on` input. Anything else -- a third-party
    reusable workflow, an expression that is not a resolvable input, a runner group, a cycle --
    is a str saying why it cannot be proven."""
    seen = (*seen, path.resolve())
    doc = _workflow_doc(path)
    jobs = doc.get("jobs") if doc is not None else None
    if not isinstance(jobs, dict) or not jobs:
        return {path.name: "no readable jobs"}
    targets: dict[str, RunnerTarget] = {}
    for name, job in jobs.items():
        where = f"{path.name}:{name}"
        if not isinstance(job, dict):
            targets[where] = "unreadable job"
        elif "uses" in job:
            targets.update(
                _reusable_call_targets(base, where, job, platform_repository, inputs, seen)
            )
        elif "runs-on" in job:
            labels = _resolved_labels(job["runs-on"], inputs)
            targets[where] = (
                labels if labels is not None else _unresolved_target(job["runs-on"], "runs-on")
            )
        else:
            targets[where] = "no runs-on"
    return targets


def _pull_request_off_hosted(base: Path, platform_repository: str) -> list[str]:
    """Every job a pull_request-triggered workflow (any `pull_request*` event) runs that is not
    PROVEN to run on a single GitHub-hosted label -- including the jobs of the reusable workflows
    it calls (`_job_runner_targets`). On a public repository those jobs run fork code, so an
    unresolvable target fails closed."""
    found = []
    workflows = base / ".github/workflows"
    paths = sorted([*workflows.glob("*.yml"), *workflows.glob("*.yaml")])
    for path in paths:
        doc = _workflow_doc(path)
        if doc is None or not any(t.startswith("pull_request") for t in _triggers(doc)):
            continue
        for where, target in _job_runner_targets(base, path, platform_repository).items():
            if isinstance(target, str):
                found.append(f"{where} ({target})")
            elif not _github_hosted(target):
                found.append(f"{where} {sorted(target)}")
    return found


def _ci_runs_on(base: Path) -> RunnerTarget | None:
    """Where ci.yml's `test` job runs: its labels, or why they cannot be proven (None when the
    file or the job cannot be read -- the ci.yml gate check reports that)."""
    doc = _workflow_doc(base / ".github/workflows/ci.yml")
    jobs = doc.get("jobs") if doc is not None else None
    job = jobs.get(REQUIRED_CHECK) if isinstance(jobs, dict) else None
    if not isinstance(job, dict):
        return None
    if "runs-on" not in job:
        return "no runs-on"
    labels = _resolved_labels(job["runs-on"], None)
    return labels if labels is not None else _unresolved_target(job["runs-on"], "runs-on")


def _ci_problems(base: Path) -> list[str]:
    """ci.yml must run on pull_request, as a job named `test`, the policy's three commands --
    and nothing in the job may change what those commands do (`_gate_environment_problems`)."""
    import yaml  # deferred: the CLI must import without site dependencies

    try:
        doc = yaml.safe_load((base / ".github/workflows/ci.yml").read_text())
        commands = tomllib.loads((base / "agentic-sdlc.toml").read_text()).get("commands") or {}
    except (OSError, yaml.YAMLError, tomllib.TOMLDecodeError) as exc:
        return [str(exc)[:200]]
    if not isinstance(doc, dict):
        return ["not a workflow mapping"]
    problems = []
    if "pull_request" not in _triggers(doc):
        problems.append("not triggered on pull_request")
    else:
        try:
            project = tomllib.loads((base / "agentic-sdlc.toml").read_text()).get("project")
        except (OSError, tomllib.TOMLDecodeError):
            project = None
        branch = project.get("default_branch") if isinstance(project, dict) else None
        problems += _pull_request_coverage_problems(doc, str(branch or "main"))
    jobs = doc.get("jobs")
    job = jobs.get(REQUIRED_CHECK) if isinstance(jobs, dict) else None
    if not isinstance(job, dict):
        return [*problems, f"no '{REQUIRED_CHECK}' job"]
    # A skipped or failure-tolerant job still reports a passing check to the ruleset.
    problems += [
        f"'{REQUIRED_CHECK}' job has {key}: its gates cannot fail the check"
        for key in ("if", "continue-on-error")
        if _job_key_set(job, key)
    ]
    steps = job.get("steps")
    steps = [s for s in steps if isinstance(s, dict)] if isinstance(steps, list) else []
    shell = _default_shell(doc, job)
    scripts = [
        (step, _shell_parse(str(step.get("run", "")), step.get("shell", shell)))
        for step in steps
        if "run" in step
    ]
    # Only unconditional steps whose failure fails the job count; inside them, only commands
    # whose exit status the shell enforces (see _shell_parse).
    executed = [
        argv
        for step, parsed in scripts
        if not _job_key_set(step, "if") and not _job_key_set(step, "continue-on-error")
        for argv, enforced in parsed.commands
        if enforced
    ]
    wanted_by_gate = {}
    for gate in ("setup", "quality", "test"):
        command = str(commands.get(gate, "")).replace("\n", " ").strip()
        wanted = _shell_commands(command)
        wanted_by_gate[gate] = wanted
        if not command:
            problems.append(f"policy has no [commands] {gate}")
        elif not wanted or not all(
            any(_gate_invocation(argv, want) for argv in executed) for want in wanted
        ):
            problems.append(f"'{REQUIRED_CHECK}' job does not run the {gate} command {command!r}")
    tools = _gate_tools(argv for wanted in wanted_by_gate.values() for argv in wanted)
    problems += _gate_environment_problems(doc, job, steps, scripts, tools)
    return problems


#: `pull_request` activity types a required check must run on: a PR's head changing.
PULL_REQUEST_REQUIRED_TYPES = ("opened", "synchronize", "reopened")


def _pull_request_coverage_problems(doc: dict, default_branch: str) -> list[str]:
    """ci.yml's `pull_request` trigger must start the `test` job for EVERY pull request into the
    protected branch, or the ruleset waits forever on a check that is never reported: no
    `paths`/`paths-ignore` filter, no `branches-ignore`, a `branches` filter only when it lists
    the default branch by its exact name, and activity types covering every head update."""
    triggers = doc.get("on", doc.get(True))
    spec = triggers.get("pull_request") if isinstance(triggers, dict) else None
    if spec is None:
        return []  # `on: pull_request`, `on: [pull_request]` or `pull_request:` with no filters
    if not isinstance(spec, dict):
        return [f"pull_request trigger {spec!r} is not a readable mapping (fails closed)"]
    problems = [
        f"pull_request trigger has a {key} filter: pull requests outside it never report "
        f"'{REQUIRED_CHECK}' and wait on the ruleset forever"
        for key in ("paths", "paths-ignore", "branches-ignore")
        if key in spec
    ]
    if "branches" in spec:
        branches = spec["branches"]
        listed = [branches] if isinstance(branches, str) else branches
        if not isinstance(listed, list) or default_branch not in [str(b) for b in listed]:
            problems.append(
                f"pull_request branches filter {branches!r} does not name the default branch "
                f"{default_branch!r} exactly"
            )
    if "types" in spec:
        types = spec["types"]
        listed = [types] if isinstance(types, str) else types
        lacking = [
            t
            for t in PULL_REQUEST_REQUIRED_TYPES
            if not isinstance(listed, list) or t not in [str(x) for x in listed]
        ]
        if lacking:
            problems.append(f"pull_request types omit {', '.join(lacking)}")
    return problems


def _default_shell(doc: dict, job: dict) -> object:
    """`defaults.run.shell` of the job, else of the workflow (None: GitHub's bash default)."""
    for node in (job, doc):
        defaults = node.get("defaults")
        run = defaults.get("run") if isinstance(defaults, dict) else None
        if isinstance(run, dict) and "shell" in run:
            return run["shell"]
    return None


#: Environment variables that change what a Python gate runs or whether it can fail, whatever
#: the tool: the interpreter's import path and startup, a shell's startup file (`BASH_ENV` is
#: sourced by every non-interactive bash -- it can redefine `pytest`), preloaded libraries.
GATE_ENV_ALWAYS = frozenset(
    {
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "PYTHONUSERBASE",
        "PYTHONINSPECT",
        "PYTHONWARNINGS",
        "BASH_ENV",
        "ENV",
        "SHELLOPTS",
        "BASHOPTS",
        "LD_PRELOAD",
    }
)
#: `<TOOL>_*` variables of a gate tool that only tune output or caching, never what runs.
GATE_ENV_BENIGN = frozenset(
    {
        "PIP_DISABLE_PIP_VERSION_CHECK",
        "PIP_NO_CACHE_DIR",
        "PIP_CACHE_DIR",
        "PIP_PROGRESS_BAR",
        "PIP_ROOT_USER_ACTION",
        "RUFF_CACHE_DIR",
        "RUFF_NO_CACHE",
        "RUFF_OUTPUT_FORMAT",
    }
)
#: Actions a `test` job may use before its gates: they export only toolchain locations
#: (`pythonLocation`, `LD_LIBRARY_PATH`, cache keys), never a gate tool's options. Any other
#: action can export arbitrary variables through `$GITHUB_ENV`, which doctor cannot read.
GATE_TRUSTED_ACTIONS = frozenset(
    {"actions/checkout", "actions/setup-python", "actions/cache", "astral-sh/setup-uv"}
)
_PYTHON_PROGRAMS = re.compile(r"^python(?:3(?:\.\d+)?)?$")


def _gate_tools(commands) -> set[str]:
    """The tools the policy's gate commands run: each program, and the module of `python -m X`
    (`python -m ruff check` -> ruff), as the prefix of their environment variables (RUFF)."""
    tools: set[str] = set()
    for argv in commands:
        if not argv:
            continue
        program = Path(argv[0]).name
        if _PYTHON_PROGRAMS.match(program):
            if len(argv) > 2 and argv[1] == "-m":
                tools.add(argv[2])
        else:
            tools.add(program)
    return {re.sub(r"[^A-Za-z0-9]", "_", t).upper() for t in tools if t}


def _gate_env_altering(name: str, tools: set[str]) -> bool:
    """`name` changes what a gate tool does: any `*ADDOPTS*` (pytest's PYTEST_ADDOPTS, ...), a
    gate tool's own `<TOOL>_*` configuration, or an interpreter/shell startup variable."""
    name = str(name)
    if name in GATE_ENV_BENIGN:
        return False
    return (
        "ADDOPTS" in name.upper()
        or name in GATE_ENV_ALWAYS
        or any(name.upper().startswith(f"{tool}_") for tool in tools)
    )


def _gate_environment_problems(
    doc: dict, job: dict, steps: list[dict], scripts: list, tools: set[str]
) -> list[str]:
    """What in the `test` job can change the environment or the meaning of its gate commands
    without being a gate invocation doctor could see: a gate-altering variable set by the
    workflow/job/step `env:` or by any script (prefix, assignment, export); a `$GITHUB_ENV`
    write (it sets every later step's environment); a function or alias named like a gate
    tool; a sourced file other than a virtualenv's `activate`, or `eval`; an untrusted action."""
    problems: list[str] = []
    gate_programs = {t.lower().replace("_", "-") for t in tools} | {t.lower() for t in tools}
    for where, node in (("workflow", doc), (f"job '{REQUIRED_CHECK}'", job)):
        env = node.get("env")
        if env is not None and not isinstance(env, dict):
            problems.append(f"{where} env is not a readable mapping (fails closed)")
            continue
        bad = sorted(str(k) for k in (env or {}) if _gate_env_altering(str(k), tools))
        if bad:
            problems.append(f"{where} env sets {', '.join(bad)}, which changes the gates")
    for index, step in enumerate(steps):
        label = f"step {step.get('name') or index + 1!s}"
        env = step.get("env")
        if env is not None and not isinstance(env, dict):
            problems.append(f"{label} env is not a readable mapping (fails closed)")
        else:
            bad = sorted(str(k) for k in (env or {}) if _gate_env_altering(str(k), tools))
            if bad:
                problems.append(f"{label} env sets {', '.join(bad)}, which changes the gates")
        uses = step.get("uses")
        if uses is not None:
            action = str(uses).split("@", 1)[0].strip()
            if action not in GATE_TRUSTED_ACTIONS:
                problems.append(
                    f"{label} uses {action}, which may export variables the gates inherit "
                    f"(trusted: {', '.join(sorted(GATE_TRUSTED_ACTIONS))})"
                )
    for step, parsed in scripts:
        label = f"step {step.get('name') or steps.index(step) + 1!s}"
        bad = sorted(n for n in parsed.assigned if _gate_env_altering(n, tools))
        if bad:
            problems.append(f"{label} sets {', '.join(bad)}, which changes the gates")
        if parsed.github_env:
            problems.append(f"{label} writes $GITHUB_ENV: later steps' gates inherit it")
        shadow = sorted((parsed.functions | parsed.aliases) & gate_programs)
        if shadow:
            problems.append(f"{label} defines {', '.join(shadow)} as a function/alias")
        foreign = [s for s in parsed.sourced if not s.endswith("/bin/activate")]
        if foreign or parsed.evals:
            problems.append(
                f"{label} runs {'eval' if parsed.evals else 'source ' + foreign[0]!r}: "
                "what it defines cannot be verified (fails closed)"
            )
    return problems


#: Arguments a CI step may append to a policy gate command. The gate must be invoked EXACTLY as
#: the policy states, plus only these: they change how results are printed or stop at the first
#: failure, never what runs or whether a failure fails. An allowlist, not a denylist of bad flags,
#: because the bad set is open-ended and tool-specific (`--help`, `--version`, `--collect-only`,
#: `--co`, `--setup-plan`, `-k <expr>`, `--ignore`, `--exit-zero`, `--dry-run`, `--fix`, a plugin's
#: own options ...): anything not listed here fails the gate check.
BENIGN_GATE_EXTRA_ARGS = frozenset(
    {"-q", "-qq", "-v", "-vv", "--quiet", "--verbose", "--no-header", "-x", "--exitfirst"}
)
BENIGN_GATE_EXTRA_PATTERNS = tuple(
    re.compile(p)
    for p in (
        r"--maxfail=\d+",
        r"--durations=\d+",
        r"--tb=(?:auto|long|short|line|native|no)",
        r"--colou?r=(?:yes|no|auto|always|never)",
        r"-r[fEsxXpPaAN]+",
    )
)


def _gate_invocation(argv: Sequence[str], want: Sequence[str]) -> bool:
    """`argv` runs the policy command `want`: the same words, then only benign extras."""
    want = tuple(want)
    if tuple(argv[: len(want)]) != want:
        return False
    return all(
        extra in BENIGN_GATE_EXTRA_ARGS
        or any(p.fullmatch(extra) for p in BENIGN_GATE_EXTRA_PATTERNS)
        for extra in argv[len(want) :]
    )


_SHELL_SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", "|&", ";;"}
_SHELL_KEYWORDS = {"then", "else", "do", "{", "}", "time"}
_SHELL_CONDITIONS = {"if", "elif", "while", "until", "!"}
_SHELL_OPENERS = {"if", "while", "until", "for", "case", "select"}
_SHELL_CLOSERS = {"fi", "done", "esac"}
#: Builtins whose `NAME=value` / `NAME` operands set (or export) a variable.
_SHELL_DECLARERS = {"export", "declare", "typeset", "readonly", "local"}
_ASSIGNMENT_WORD = re.compile(r"^([A-Za-z_]\w*)=")
_NAME_WORD = re.compile(r"^[A-Za-z_]\w*$")
#: Shells whose run steps abort on a failing command: GitHub runs an unspecified or `bash` shell
#: as `bash -eo pipefail`, and `sh` as `sh -e` (no pipefail).
_ERREXIT_SHELLS = {None, "bash", "sh"}


def _job_key_set(node: dict, key: str) -> bool:
    """`continue-on-error:` with anything but false, or `if:` with anything but true (an `if`
    can skip the gate, and a skipped job or step passes)."""
    if key not in node:
        return False
    neutral = "true" if key == "if" else "false"
    return str(node[key]).strip().lower() != neutral


@dataclass
class ShellScript:
    """What `_shell_parse` reads from a `run:` script.

    `commands` holds each simple command with whether its failure fails the step. The rest is
    what can change what a later command DOES without being that command: variables set
    (`VAR=x cmd`, `VAR=x`, `export VAR=x`), functions and aliases defined (`pytest() {...}`
    shadows the real pytest), files sourced or `eval`ed, and any mention of `$GITHUB_ENV`
    (a write there sets the environment of every later step).
    """

    commands: list[tuple[tuple[str, ...], bool]]
    assigned: set[str]
    functions: set[str]
    aliases: set[str]
    sourced: list[str]
    evals: bool
    github_env: bool


def _shell_parse(script: str, shell: object = None) -> ShellScript:
    """The simple commands a `run:` script executes, each with whether its failure fails the
    step, plus what the script sets up for later commands (`ShellScript`).

    With errexit (GitHub's default `bash -eo pipefail`, or `sh -e`) a command is enforced unless it
    is a condition (`if`/`while`/`until`/`!`) or inside a conditional/loop body, backgrounded with
    `&`, part of an `&&`/`||` list other than its last member (errexit ignores those), after `||`
    (it may never run), piped onward without pipefail, or after `set +e`. Whatever the shell, the
    script's LAST and-or list is the step's exit status: its `&&` members are enforced too.

    Commands inside a shell function body (`name() { ... }`, `function name { ... }`) are NEVER
    enforced: defining a function runs nothing, and whether a later call reaches the body (and
    with which errexit state -- bash ignores `set -e` inside a function called as a condition) is
    not modelled. A gate must be a top-level command.
    """
    shell_name = None if shell is None else str(shell).strip()
    errexit = shell_name in _ERREXIT_SHELLS
    out = ShellScript([], set(), set(), set(), [], False, "GITHUB_ENV" in script)
    records: list[dict] = []
    depth = 0
    braces: list[str] = []  # "function" / "group" per open `{`
    pending_function = False
    previous = ";"
    for line in script.replace("\\\n", " ").splitlines():
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        try:
            tokens = list(lexer)
        except ValueError:  # unbalanced quotes: no command we can vouch for
            continue
        if not tokens:
            continue
        argv: list[str] = []
        for token in [*tokens, None]:
            if token == "()":  # `name()` / `function name()`: a function header, runs nothing
                name = [w for w in argv if w != "function"]
                if name:
                    out.functions.add(name[-1])
                pending_function = True
                argv = []
                continue
            if token is not None and token not in _SHELL_SEPARATORS:
                argv.append(token)
                continue
            condition = False
            while argv and (
                argv[0] in _SHELL_KEYWORDS
                or argv[0] in _SHELL_CONDITIONS
                or argv[0] in _SHELL_CLOSERS
                or argv[0] == "function"
                or _ASSIGNMENT_WORD.match(argv[0])
            ):
                head = argv.pop(0)  # keywords and `VAR=value` prefixes are not the command
                assignment = _ASSIGNMENT_WORD.match(head)
                if assignment:
                    out.assigned.add(assignment.group(1))
                elif head == "function" and argv:
                    out.functions.add(argv.pop(0))
                    pending_function = True
                elif head == "{":
                    braces.append("function" if pending_function else "group")
                    pending_function = False
                elif head == "}" and braces:
                    braces.pop()
                condition = condition or head in _SHELL_CONDITIONS
                if head in _SHELL_OPENERS:
                    depth += 1
                elif head in _SHELL_CLOSERS:
                    depth = max(0, depth - 1)
            nested = depth > 0
            in_function = "function" in braces
            if argv and argv[0] in _SHELL_OPENERS:  # for/case/select open a body, run nothing
                depth += 1
                argv = []
            following = ";" if token is None else token
            if argv:
                if argv[0] in _SHELL_DECLARERS:
                    for word in argv[1:]:
                        found = _ASSIGNMENT_WORD.match(word) or _NAME_WORD.match(word)
                        if found:
                            out.assigned.add(found.group(1) if found.groups() else word)
                elif argv[0] == "alias":
                    out.aliases |= {w.split("=", 1)[0] for w in argv[1:] if "=" in w}
                elif argv[0] in {"source", "."}:
                    out.sourced.append(argv[1] if len(argv) > 1 else "")
                elif argv[0] == "eval":
                    out.evals = True
                if argv[0] == "set" and "+e" in argv[1:]:
                    errexit = False
                elif argv[0] == "set" and any(
                    a.startswith("-") and "e" in a.lstrip("-") for a in argv[1:]
                ):
                    errexit = True
                records.append(
                    {
                        "argv": tuple(argv),
                        "reached": not condition
                        and not nested
                        and not in_function
                        and previous != "||",
                        "previous": previous,
                        "following": following,
                        "errexit": errexit,
                        "piped": following in {"|", "|&"}
                        and shell_name != "bash"
                        and shell_name is not None,
                    }
                )
            argv = []
            if token is None:  # a line ending in an operator continues the list
                previous = previous if tokens[-1] in {"&&", "||", "|"} else ";"
            else:
                previous = token
    # The trailing and-or list: its status is the script's, so the step's.
    last = len(records) - 1
    while last > 0 and records[last]["previous"] in {"&&", "||", "|", "|&"}:
        last -= 1
    for index, r in enumerate(records):
        base = r["reached"] and r["following"] != "&" and not r["piped"]
        if index >= last and (index == len(records) - 1 or r["following"] in {"&&", "|", "|&"}):
            enforced = base
        else:
            enforced = (
                base
                and r["errexit"]
                and r["previous"] != "&&"
                and r["following"] not in {"&&", "||"}
            )
        out.commands.append((r["argv"], enforced))
    return out


def _shell_flow(script: str, shell: object = None) -> list[tuple[tuple[str, ...], bool]]:
    """The simple commands a `run:` script executes, each with whether its failure fails the
    step (see `_shell_parse`)."""
    return _shell_parse(script, shell).commands


def _shell_commands(script: str) -> list[tuple[str, ...]]:
    """The simple commands a `run:` script executes, as argv tuples. A gate counts only as a
    command: `echo pytest`, `# pytest` and `"pytest"` are an argument, a comment and a string."""
    return [argv for argv, _ in _shell_flow(script)]


HOOK_SCRIPTS = {
    "SessionStart": ".claude/hooks/forge_session_start.py",
    "PreToolUse": ".claude/hooks/forge_commit_guard.py",
}
_HOOK_INTERPRETERS = {"python3", "python"}
_HOOK_DIR_PREFIXES = ("", "$CLAUDE_PROJECT_DIR/", "${CLAUDE_PROJECT_DIR}/")


def _hook_runs(entry: object, script: str) -> bool:
    """A hook handler that runs `python3 <script>` (optionally under $CLAUDE_PROJECT_DIR)."""
    if not isinstance(entry, dict) or entry.get("type") != "command":
        return False
    try:
        argv = shlex.split(str(entry.get("command", "")))
    except ValueError:
        return False
    return (
        len(argv) == 2
        and argv[0] in _HOOK_INTERPRETERS
        and argv[1] in {prefix + script for prefix in _HOOK_DIR_PREFIXES}
    )


def _matcher_covers(matcher: object, tool: str | None) -> bool:
    """Whether a hook group's matcher selects `tool` (None: SessionStart, whose matcher names
    the start source; the group must fire on a fresh `startup`)."""
    if matcher is None or matcher in ("", "*"):
        return True
    if not isinstance(matcher, str):
        return False
    if tool is None:
        return "startup" in {part.strip() for part in matcher.split("|")}
    try:
        return re.fullmatch(matcher, tool) is not None
    except re.error:
        return False


def hook_problems(settings_path: Path) -> list[str]:
    """Why Claude Code would NOT run the Forge hooks from this settings file (empty = it will):
    the parsed `hooks` structure must hold a SessionStart group firing on startup that runs the
    session-start script, and a PreToolUse group matching Bash that runs the commit guard. A
    mention anywhere else in the file -- a string, a disabled or malformed entry -- is no hook."""
    try:
        settings = json.loads(settings_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return [f".claude/settings.json is not valid JSON ({str(exc)[:80]})"]
    if not isinstance(settings, dict):
        return [".claude/settings.json is not a JSON object"]
    if settings.get("disableAllHooks") is True:
        return [".claude/settings.json sets disableAllHooks"]
    hooks = settings.get("hooks")
    hooks = hooks if isinstance(hooks, dict) else {}
    problems = []
    for event, script in HOOK_SCRIPTS.items():
        tool = "Bash" if event == "PreToolUse" else None
        groups = hooks.get(event)
        ok = isinstance(groups, list) and any(
            isinstance(group, dict)
            and _matcher_covers(group.get("matcher"), tool)
            and isinstance(group.get("hooks"), list)
            and any(_hook_runs(h, script) for h in group["hooks"])
            for group in groups
        )
        if not ok:
            where = f"{event} (matcher Bash)" if tool else event
            problems.append(f".claude/settings.json has no {where} hook running python3 {script}")
    return problems


# ---------------------------------------------------------------- managed workflow drift

#: The Forge-managed workflows, each compared whole against what `onboard` renders for the
#: repository's policy (`managed_workflow_drift`). The implement callers only in Actions mode.
MANAGED_WORKFLOWS = ("agent-plan.yml", "agent-auto-plan.yml", "ci.yml")
MANAGED_IMPLEMENT_WORKFLOWS = ("agent-implement.yml", "agent-auto-implement.yml")
#: Policy `[agents] implementer` -> the `onboard --implementer` that wrote it (Actions mode).
POLICY_IMPLEMENTERS = {"router": "route", "claude": "claude", "codex": "codex"}
#: What a tunable knob is replaced with on both sides before the comparison.
_TUNED = "<tunable>"
#: The ONLY parts of a managed workflow that may differ from the generated template; each is
#: validated by its own check instead. Documented in docs/onboarding.md ("Managed workflows").
#:   - every job's `runs-on`, and a reusable call's `with.runs_on` (the runner checks);
#:   - a reusable-implement call's `with.route_budget_usd` (absent or present; the budget check);
#:   - ci.yml's `test` job `steps` (the gate check: exact policy commands, trusted actions only,
#:     no environment that changes a gate);
#:   - the automatic callers' label `if:` (the label-condition check: exactly the generated
#:     condition for the POLICY's labels);
#:   - the platform pin: the template is rendered at the caller's own pinned SHA (the pin check:
#:     a SHA on the platform repository, one SHA across all callers).
TUNABLE_KNOBS = (
    "jobs.*.runs-on",
    "jobs.*.with.runs_on",
    "jobs.*.with.route_budget_usd",
    "ci.yml: jobs.test.steps",
    "agent-auto-plan.yml: jobs.plan.if",
    "agent-auto-implement.yml: jobs.preflight.if",
    "platform ref pin",
)
_LABEL_CONDITION_JOBS = {"agent-auto-plan.yml": "plan", "agent-auto-implement.yml": "preflight"}


def _mask_tunables(name: str, doc: object) -> object:
    """A deep copy of a parsed workflow with every TUNABLE_KNOBS value replaced by `_TUNED`
    (route_budget_usd removed: the template omits it and the workflow defaults it)."""
    doc = json.loads(json.dumps(doc, default=str))  # deep copy; YAML's `on: True` key -> "true"
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    if not isinstance(jobs, dict):
        return doc
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        if "runs-on" in job:
            job["runs-on"] = _TUNED
        passed = job.get("with")
        if isinstance(passed, dict):
            if "runs_on" in passed:
                passed["runs_on"] = _TUNED
            passed.pop("route_budget_usd", None)
        if name == "ci.yml" and job_name == REQUIRED_CHECK and "steps" in job:
            job["steps"] = _TUNED
        if _LABEL_CONDITION_JOBS.get(name) == job_name:
            job["if"] = _TUNED
    return doc


def _first_difference(want: object, have: object, path: str = "") -> str | None:
    """The first path (`jobs.implement.with.issue_number`) where two parsed documents differ."""
    where = path or "<document>"
    if isinstance(want, dict) and isinstance(have, dict):
        for key in sorted(set(want) | set(have), key=str):
            label = "on" if key == "true" and path == "" else str(key)
            sub = f"{path}.{label}" if path else label
            if key not in have:
                return f"{sub} (missing)"
            if key not in want:
                return f"{sub} (not in the template)"
            found = _first_difference(want[key], have[key], sub)
            if found:
                return found
        return None
    if isinstance(want, list) and isinstance(have, list):
        for index, (a, b) in enumerate(zip(want, have, strict=False)):
            found = _first_difference(a, b, f"{path}[{index}]")
            if found:
                return found
        if len(want) != len(have):
            return f"{where} ({len(have)} items, template has {len(want)})"
        return None
    if want != have:
        return f"{where} ({have!r}, template {want!r})" if len(repr(have)) < 120 else where
    return None


def _caller_pin(doc: dict, platform_repository: str) -> str | None:
    """The SHA the caller's first platform reusable-workflow call is pinned to."""
    jobs = doc.get("jobs")
    for job in jobs.values() if isinstance(jobs, dict) else ():
        found = _REMOTE_REUSABLE.fullmatch(str((job or {}).get("uses", "")).strip())
        if found and found.group(1) == platform_repository and _SHA.fullmatch(found.group(3)):
            return found.group(3)
    return None


def managed_workflow_drift(
    base: Path,
    policy_doc: dict,
    project_id: str,
    platform_repository: str,
    *,
    cloud: bool,
    default_branch: str,
) -> list[str]:
    """Every installed Forge-managed workflow must parse to exactly what `onboard` renders for
    this repository's policy, apart from TUNABLE_KNOBS. One structural comparison instead of a
    check per field: triggers and their filters, job conditions, `needs`, every `with:` input
    (the issue number, actor, config path, agent, registry paths), `secrets:`, permissions,
    concurrency and steps are all covered, including fields no specific check knows about."""
    agents = policy_doc.get("agents") if isinstance(policy_doc.get("agents"), dict) else {}
    implementer = (
        "cloud-routine" if cloud else POLICY_IMPLEMENTERS.get(str(agents.get("implementer")))
    )
    if implementer is None:
        return [
            f"[agents] implementer {agents.get('implementer')!r} is not one onboard writes "
            f"({', '.join(sorted(POLICY_IMPLEMENTERS))}); the managed workflows cannot be "
            "rebuilt to compare against"
        ]
    names = MANAGED_WORKFLOWS + (() if cloud else MANAGED_IMPLEMENT_WORKFLOWS)
    problems = []
    for name in names:
        path = base / ".github/workflows" / name
        if not path.exists():
            continue  # "required files present" reports it
        installed = _workflow_doc(path)
        if installed is None:
            problems.append(f"{name} is not a readable workflow")
            continue
        pin = _caller_pin(installed, platform_repository) or "0" * 40
        try:
            spec = OnboardSpec(
                project_id=project_id,
                platform_repository=platform_repository,
                platform_ref=pin,
                test_command="-",  # ci.yml's steps are a tunable knob
                implementer=implementer,
                default_branch=default_branch,
            )
            rendered = render_onboarding(spec)[f".github/workflows/{name}"]
        except (OnboardError, KeyError) as exc:
            problems.append(f"{name}: the generated template cannot be rebuilt ({exc})")
            continue
        import yaml  # deferred: the CLI must import without site dependencies

        want = _mask_tunables(name, yaml.safe_load(rendered))
        found = _first_difference(want, _mask_tunables(name, installed))
        if found:
            problems.append(
                f"managed workflow {name} differs from the generated template at {found}; "
                "re-run `sdlcctl onboard --force`"
            )
    return problems


def implementation_mode(toml_path: Path, actions_callers: Sequence[str]) -> tuple[str, str]:
    """(mode, problem) from the policy's `[agents] implementation_mode`; problem is '' when the
    mode is explicit and valid. A policy written before the field existed keeps working while its
    Actions callers are installed (they are the old source of truth), and fails otherwise: a
    stray docs file never decides that a cloud routine implements."""
    try:
        agents = tomllib.loads(toml_path.read_text()).get("agents")
    except (OSError, tomllib.TOMLDecodeError):
        agents = None
    mode = agents.get("implementation_mode") if isinstance(agents, dict) else None
    if mode in IMPLEMENTATION_MODES:
        return str(mode), ""
    if mode is not None:
        return "actions", (
            f"[agents] implementation_mode is {mode!r}; expected one of "
            + ", ".join(IMPLEMENTATION_MODES)
        )
    if actions_callers:
        return "actions", ""
    return "actions", (
        "agentic-sdlc.toml has no [agents] implementation_mode and no Actions implement caller "
        'is installed: set implementation_mode = "cloud-routine" (or re-run `sdlcctl onboard '
        "--force`) for a cloud routine, or install agent-implement.yml/agent-auto-implement.yml"
    )


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
    actions_impl = [
        n
        for n in ("agent-implement.yml", "agent-auto-implement.yml")
        if (base / ".github/workflows" / n).exists()
    ]
    routine_doc = "docs/forge/cloud-implementer.md"
    # The mode is what the policy says, not which files happen to exist.
    mode, mode_problem = implementation_mode(base / "agentic-sdlc.toml", actions_impl)
    if (base / "agentic-sdlc.toml").exists():
        add(
            Check(
                "implementation mode is configured",
                not mode_problem,
                mode_problem or f"[agents] implementation_mode = {mode!r}",
            )
        )
    cloud = mode == "cloud-routine" and not mode_problem
    if cloud:
        required.append(routine_doc)
    else:
        required += [
            ".github/workflows/agent-implement.yml",
            ".github/workflows/agent-auto-implement.yml",
        ]
    missing = [r for r in required if not (base / r).exists()]
    settings = base / ".claude/settings.json"
    if settings.exists():
        missing += hook_problems(settings)
    add(Check("required files present", not missing, ", ".join(missing) if missing else ""))
    if cloud and actions_impl:
        add(
            Check(
                "implementation profile is unambiguous",
                False,
                "implementation_mode is cloud-routine but "
                + ", ".join(actions_impl)
                + " are installed — a cloud-routine repo must not keep Actions implementer "
                "workflows (their preflight needs the Publisher App); remove one side",
            )
        )
    elif not cloud and (base / routine_doc).exists():
        add(
            Check(
                "implementation profile is unambiguous",
                False,
                f"{routine_doc} is present but the policy's implementation mode is Actions "
                f"({', '.join(actions_impl) or 'no implement caller installed'}) — remove the "
                'stale routine doc or set implementation_mode = "cloud-routine"',
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
    present_implement_callers = [c for c in implement_callers if c.exists()]
    # The adapter each implement call asks for: its parsed `with.agent`, never the file text.
    agents, agent_problems = _implement_agents(present_implement_callers)
    if agent_problems:
        add(
            Check(
                "implement callers name a known agent",
                False,
                "unreadable or unsupported (expected one of "
                + ", ".join(sorted(REUSABLE_IMPLEMENT_AGENTS))
                + "): "
                + "; ".join(agent_problems),
            )
        )
    try:
        policy_doc = tomllib.loads(toml_path.read_text()) if toml_path.exists() else {}
    except (OSError, tomllib.TOMLDecodeError):
        policy_doc = {}  # "agentic-sdlc.toml loads" fails above
    # Route mode is what the installed callers/policy ask for, not whether the registry exists;
    # an agent doctor cannot read may be `route`, so it is checked as one (fails closed).
    routed = (
        any((base / f).exists() for f in routing_files)
        or "routing" in policy_doc
        or "route" in agents.values()
        or bool(agent_problems)
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
            routing_policy = load_routing_policy(
                json.loads((base / ".forge/routing-policy.json").read_text())
            )
            if policy is None:
                raise ExecutorError("agentic-sdlc.toml must load to build the route request")
            # The exact request each installed implement caller makes (one per distinct
            # route_budget_usd), evaluated by the router itself.
            readable_budgets, budget_problems = _caller_route_budgets(present_implement_callers)
            if budget_problems:
                add(
                    Check(
                        "implement callers pass a readable route_budget_usd",
                        False,
                        "route-executor --budget-usd needs a finite non-negative number; "
                        "doctor cannot route on " + "; ".join(budget_problems),
                    )
                )
            budgets = sorted(readable_budgets or {ROUTE_DEFAULT_BUDGET_USD})
            requests = [implementation_route_request(policy, project_id, b) for b in budgets]
            verdicts = [route_candidates(executors, routing_policy, r) for r in requests]
            routable = [e for e in executors if any(not v[e.executor_id] for v in verdicts)]
            # The router may pick any of these; one the implement workflow cannot run fails at
            # "adapter is not installed" after doctor said READY.
            unsupported = [
                f"{e.executor_id} ({reason})"
                for e in routable
                for reason in [_adapter_gap(e)]
                if reason
            ]
            stuck = [
                budget
                for budget, verdict in zip(budgets, verdicts, strict=True)
                if not any(not verdict[e.executor_id] and not _adapter_gap(e) for e in executors)
            ]
            rejected = "; ".join(
                f"{e.executor_id}: {', '.join(verdicts[0][e.executor_id])}"
                for e in executors
                if verdicts[0][e.executor_id]
            )
            what = (
                f"the {ROUTE_MISSION_ID} mission ({requests[0].risk.value} risk, "
                f"{ROUTE_TASK_CLASS.value})"
            )
            add(
                Check(
                    "routing files valid and permit this repo",
                    not stuck,
                    f"{len(routable)} of {len(executors)} executors are selectable for {what} "
                    "in this repository"
                    if not stuck
                    else f"no executor the router can select for {what} here has a workflow "
                    f"adapter (budget ${', $'.join(f'{b:g}' for b in stuck)})"
                    + (f" ({rejected})" if rejected else ""),
                )
            )
            add(
                Check(
                    "routed executors have a workflow adapter",
                    not unsupported,
                    "; ".join(unsupported)
                    if unsupported
                    else "adapters: " + ", ".join(sorted(ROUTED_ADAPTER_PROVIDERS)),
                )
            )
            # Every executor the router may fall back to must be configured, or it advances into
            # a missing key after the first recoverable failure.
            for e in routable:
                secret = _executor_secret(e)
                if secret:
                    routed_secrets.add(secret)
                var = re.fullmatch(r"configured-by-([A-Z0-9_]+)", e.model or "")
                if var:
                    routed_vars.add(var.group(1))
        except (
            OSError,
            ExecutorError,
            MissionError,
            json.JSONDecodeError,
            AttributeError,
        ) as exc:
            add(Check("routing files valid and permit this repo", False, str(exc)[:200]))
        # A secret the repository holds is still empty inside the reusable workflow unless the
        # caller passes it on.
        unforwarded = [
            gap
            for caller in present_implement_callers
            for gap in _unforwarded_secrets(caller, routed_secrets)
        ]
        add(
            Check(
                "implement callers forward every routed secret",
                not unforwarded,
                "not forwarded: " + "; ".join(unforwarded)
                if unforwarded
                else ", ".join(sorted(routed_secrets)),
            )
        )

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
            # The parsed jobs' `uses`, not the file text: a commented-out call is no call.
            calls = reusable_calls(caller)
            expected = CALLER_TARGETS.get(caller.name)
            if not calls:
                problems.append(f"{caller.name} calls no reusable workflow")
            elif expected and not any(wf == expected for _, _, wf, _ in calls):
                problems.append(f"{caller.name} calls no {expected}")
            for job, repo_part, workflow, pin in calls:
                target = f"{repo_part}/.github/workflows/{workflow}@{pin}"
                if expected and workflow != expected:
                    problems.append(f"{caller.name}:{job} calls {workflow}, expected {expected}")
                elif repo_part != platform_repository or not _SHA.fullmatch(pin):
                    problems.append(f"{caller.name}:{job} uses {target}")
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
            labels = (policy.ready_label, policy.human_review_label, policy.implementation_label)
            # The exact generated condition, compared after whitespace normalization: any other
            # expression -- a dropped label, an `|| true`, a different label -- is not proven to
            # require every approval label, so a ready label alone could start credentialed work.
            mismatched = []
            for file, job_name, expected in (
                ("agent-auto-plan.yml", "plan", auto_plan_condition(*labels)),
                ("agent-auto-implement.yml", "preflight", auto_implement_condition(*labels)),
            ):
                path = base / ".github/workflows" / file
                if not path.exists():
                    continue
                job = _workflow_jobs(path).get(job_name)
                actual = job.get("if") if isinstance(job, dict) else None
                if actual is None or _normalized_expr(actual) != _normalized_expr(expected):
                    mismatched.append(
                        f"{file}:{job_name} if: must be exactly {_normalized_expr(expected)!r}"
                    )
            add(
                Check(
                    "workflow label conditions match the policy labels",
                    not mismatched,
                    "; ".join(mismatched),
                )
            )
        if policy is not None:
            drift = managed_workflow_drift(
                base,
                policy_doc,
                project_id,
                platform_repository,
                cloud=cloud,
                default_branch=policy.default_branch,
            )
            add(
                Check(
                    "managed workflows match the generated templates",
                    not drift,
                    "; ".join(drift) if drift else "tunable only: " + ", ".join(TUNABLE_KNOBS),
                )
            )
        # The automatic callers do nothing unless adding a label to an issue starts them.
        inert = [
            caller.name
            for caller in callers
            if caller.name in {"agent-auto-plan.yml", "agent-auto-implement.yml"}
            and not _fires_on_issue_labeled(_workflow_doc(caller) or {})
        ]
        add(
            Check(
                "automatic callers trigger on issue labels",
                not inert,
                "not triggered by `on: issues: types: [labeled]`: " + ", ".join(inert)
                if inert
                else "",
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
    repo_info = _safe_json(gh, ["api", f"repos/{project_id}"])
    actual_branch = repo_info.get("default_branch") if isinstance(repo_info, dict) else None
    if policy is not None:
        add(
            Check(
                "policy default_branch matches the repository",
                actual_branch == policy.default_branch,
                f"policy {policy.default_branch!r}, GitHub {actual_branch!r}",
            )
        )

    # --- the managed files must be on the default branch: the workflows, CI and hooks GitHub
    # runs are the pushed ones, not this checkout (`onboard --apply` writes them locally only).
    remote_branch = actual_branch
    managed = [
        r
        for r in dict.fromkeys(
            [*required, *(routing_files if routed else ()), "docs/forge/cloud-implementer.md"]
        )
        if (base / r).is_file()
    ]
    unpushed = []
    for relative in managed:
        blob = _safe_json(
            gh, ["api", f"repos/{project_id}/contents/{relative}?ref={remote_branch or 'HEAD'}"]
        )
        remote_bytes = None
        if isinstance(blob, dict) and blob.get("encoding") == "base64":
            try:
                remote_bytes = base64.b64decode(str(blob.get("content") or ""))
            except ValueError:
                remote_bytes = None
        if remote_bytes is None:
            unpushed.append(f"{relative} (missing)")
        elif remote_bytes != (base / relative).read_bytes():
            unpushed.append(f"{relative} (differs)")
    add(
        Check(
            "managed files are on the default branch",
            bool(remote_branch) and not unpushed,
            (
                f"{remote_branch or 'default branch'}: "
                + ", ".join(unpushed)
                + " → commit and push these files first"
            )
            if unpushed or not remote_branch
            else f"{len(managed)} files match {remote_branch}",
        )
    )

    # --- labels
    names = {x.get("name") for x in _paged_items(gh, f"repos/{project_id}/labels?per_page=100")}
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
    # Whatever an installed implement caller's preflight insists on must exist too, or every run
    # stops there; and whatever the router may select must be configured.
    pre_secrets, pre_vars = _preflight_requirements(implement_callers)
    need_secrets |= pre_secrets
    need_vars |= pre_vars
    if routed:
        need_vars |= routed_vars
        need_secrets |= routed_secrets
    if "codex" in agents.values():  # the parsed `with.agent` (or its codex default)
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

    # --- runners: every job of every active caller, and ci.yml's `test` job, must run somewhere
    # that exists, or the run (or every PR's required check) sits queued forever.
    # The same resolver as the public-repository check: local reusable workflows are followed
    # into their own jobs, platform calls through their `runs_on` input; anything it cannot
    # prove (a runner group, an expression, a third-party workflow) is a str reason.
    all_targets: dict[str, RunnerTarget] = {}
    for caller in callers:
        all_targets.update(_job_runner_targets(base, caller, platform_repository))
    ci_target = _ci_runs_on(base)
    ci_labels = ci_target if isinstance(ci_target, set) else None
    unprovable = {w: why for w, why in all_targets.items() if isinstance(why, str)}
    label_targets = {w: t for w, t in all_targets.items() if isinstance(t, set)}
    # (ci.yml's own runner check below reports a Windows CI target.)
    on_windows = {where: lbls for where, lbls in label_targets.items() if _windows_labels(lbls)}
    if on_windows:
        add(
            Check(
                "workflows target Unix runners",
                False,
                WINDOWS_UNSUPPORTED
                + ": "
                + "; ".join(f"{w} {sorted(lbls)}" for w, lbls in sorted(on_windows.items())),
            )
        )
    # reusable-implement.yml is Linux-only (bubblewrap via apt-get): the runner each implement
    # caller hands it.
    off_linux = {
        where: lbls
        for where, lbls in label_targets.items()
        if where.split(":", 1)[0] in MANAGED_IMPLEMENT_WORKFLOWS
        and where.endswith(" (runs_on)")
        and where not in on_windows
        and not _linux_labels(lbls)
    }
    if not cloud and off_linux:
        add(
            Check(
                "implementation runs on Linux runners",
                False,
                LINUX_IMPLEMENTATION_REQUIRED
                + ": "
                + "; ".join(f"{w} {sorted(lbls)}" for w, lbls in sorted(off_linux.items())),
            )
        )
    if unprovable:
        add(
            Check(
                "workflow runner targets are verifiable",
                False,
                "; ".join(f"{w}: {why}" for w, why in sorted(unprovable.items())),
            )
        )
    # Windows targets are reported above, not as a runner to register.
    targets = {w: lbls for w, lbls in label_targets.items() if w not in on_windows}
    need_runners = any(not _github_hosted(t) for t in [*targets.values(), ci_labels or set()])
    runners = (
        _paged_items(gh, f"repos/{project_id}/actions/runners", "runners") if need_runners else []
    )
    stranded = {
        where: labels for where, labels in targets.items() if not _runner_available(labels, runners)
    }
    if targets and all(_github_hosted(t) for t in targets.values()):
        add(Check("runner", True, "GitHub-hosted runners (billed minutes on private repos)"))
    elif targets:
        online = [r for r in runners if r.get("status") == "online"]
        add(
            Check(
                "self-hosted runner online for this repo",
                not stranded,
                (
                    "no online non-Windows runner for "
                    + "; ".join(f"{w} {sorted(lbls)}" for w, lbls in sorted(stranded.items()))
                    + f" ({len(online)} online / {len(runners)} registered)"
                    if runners
                    else "none registered → gh api -X POST "
                    f"repos/{project_id}/actions/runners/registration-token, "
                    "then config.sh on a runner VM"
                )
                if stranded
                else f"{len(online)} online, carrying "
                + ", ".join(sorted({str(sorted(lbls)) for lbls in targets.values()})),
                manual=True,
            )
        )
    if isinstance(ci_target, str):
        add(Check("ci.yml test job runs on an available runner", False, ci_target))
    elif ci_labels is not None:
        ci_ok = _runner_available(ci_labels, runners)
        ci_windows = bool(_windows_labels(ci_labels))
        add(
            Check(
                "ci.yml test job runs on an available runner",
                ci_ok,
                f"labels {sorted(ci_labels)}: {WINDOWS_UNSUPPORTED}"
                if ci_windows
                else ", ".join(sorted(ci_labels))
                if _github_hosted(ci_labels)
                else f"labels {sorted(ci_labels)}: "
                + (
                    "online runner found"
                    if ci_ok
                    else "not a GitHub-hosted label and no online non-Windows runner carries "
                    "all of them"
                ),
                manual=not ci_ok and not ci_windows,
            )
        )

    # --- a public repository's pull-request jobs run fork code: never on a persistent runner
    known = isinstance(repo_info, dict) and ("private" in repo_info or "visibility" in repo_info)
    if known and _is_public(repo_info):
        exposed = _pull_request_off_hosted(base, platform_repository)
        add(
            Check(
                "public repository runs pull requests on GitHub-hosted runners",
                not exposed,
                "fork pull requests would execute on runners not proven GitHub-hosted: "
                + "; ".join(exposed)
                + f" → set runs-on to a GitHub-hosted label (e.g. {PUBLIC_CI_RUNS_ON[0]})"
                if exposed
                else "every pull_request-triggered job is GitHub-hosted",
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
    # create-github-app-token pairs PUBLISHER_APP_CLIENT_ID with the private key: only the App
    # that client ID belongs to counts, not any similarly named installation.
    client_id = variable_values.get("PUBLISHER_APP_CLIENT_ID", "").strip()
    meta = {
        slug: _safe_json(gh, ["api", f"apps/{slug}"]) for slug in {str(a["app_slug"]) for a in apps}
    }
    named = apps
    apps = [
        a
        for a in apps
        if isinstance(meta[str(a["app_slug"])], dict)
        and str(meta[str(a["app_slug"])].get("client_id") or "") == client_id
        and client_id
    ]
    if named and not apps:
        add(
            Check(
                "Publisher GitHub App installed on this repo",
                False,
                "PUBLISHER_APP_CLIENT_ID does not match the client ID of any installed "
                + "/".join(sorted(meta))
                + " app",
                manual=True,
            )
        )
    elif not apps:
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
            shortfalls = [
                publisher_permission_problems(app.get("permissions") or {}) for app in holding
            ]
            weak = min(shortfalls, key=len)
            detail = (
                "set the app's repository permissions to exactly contents: read, issues: write, "
                "pull_requests: write — " + "; ".join(weak)
                if weak
                else ""
            )
        add(
            Check(
                "Publisher GitHub App installed on this repo",
                bool(holding) and not detail,
                detail,
                manual=True,
            )
        )
    return rep
