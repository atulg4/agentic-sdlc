"""One-command Forge onboarding for a consumer repository.

`scaffold` installs a generic, fail-closed profile. `onboard` installs the *production* shape the
consumer repos actually run (self-hosted runners, preflight/notify jobs, `claude-ready` labels,
provider routing) and, with `--apply`, configures the GitHub repository itself (labels, ruleset,
variables). `doctor` verifies a repository and lists the remaining manual steps.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib.resources
import json
import math
import os
import re
import secrets
import shlex
import stat
import subprocess
import tempfile
import tomllib
import unicodedata
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

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
from .leases import IN_PROGRESS_LABEL, LEASE_BOT_LOGINS_ENV, publisher_bot_login_problem
from .missions import MissionError, load_registry
from .openai_compatible import PROVIDERS as OPENAI_COMPATIBLE_PROVIDERS
from .openai_compatible import ROUTED_MIN_CONTEXT_WINDOW
from .policy import branch_name_problem, load_policy
from .trusted_exec import (
    LOCAL,
    STRICT,
    TRUST_MODES,
    UntrustedExecutableError,
    agent_context,
    trust_mode_problem,
    trusted_gh,
)

GhRunner = Callable[..., str]  # (args: Sequence[str], input: str | None = None) -> stdout

_PROJECT = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_RUNNER_LABEL = re.compile(r"^[A-Za-z0-9_.-]+$")

IMPLEMENTERS = ("route", "claude", "codex", "cloud-routine")
#: `[agents] implementation_mode` in agentic-sdlc.toml: the single source of truth for whether an
#: Actions workflow or a Claude Code cloud routine implements approved issues.
IMPLEMENTATION_MODES = ("actions", "cloud-routine")
#: Where the Forge jobs run by default (Codex 4243193108): a fresh GitHub-hosted VM per job.
#: The reusable implement/repair jobs apply a MODEL-PRODUCED patch and run the repository's setup,
#: quality and test commands on it, and ci.yml runs every pull request's code, the agent's
#: included; on a persistent self-hosted runner such code could persist into later jobs that
#: hold model credentials and Publisher App secrets. Self-hosted labels need `--ephemeral-runners`
#: (`ephemeral_runners`), the owner's acknowledgement that each runner is ephemeral or contained.
DEFAULT_RUNS_ON = ("ubuntu-latest",)
#: The owner's acknowledgement that self-hosted runners are ephemeral (one job, then discarded)
#: or otherwise contained; without it onboard refuses self-hosted labels and doctor warns.
EPHEMERAL_RUNNERS_FLAG = "--ephemeral-runners"
SELF_HOSTED_NEEDS_ACK = (
    "the Forge jobs run model-produced patches and pull-request code (reusable implement/repair "
    "verify, ci.yml): on a persistent self-hosted runner that code can persist into later jobs "
    "holding model credentials and Publisher App secrets. Use a GitHub-hosted label (the "
    f"default, {DEFAULT_RUNS_ON[0]}), or pass {EPHEMERAL_RUNNERS_FLAG} to acknowledge that every "
    "self-hosted runner is ephemeral (one job, then discarded) or otherwise contained"
)
#: GitHub's ceiling on `required_approving_review_count`.
MAX_REQUIRED_APPROVALS = 10
#: Where a FORK-EXPOSED repository's CI runs by default (`fork_pull_request_exposure`): every
#: fork pull request executes its own code in that job, so it must be a fresh GitHub-hosted VM,
#: never a persistent self-hosted runner.
PUBLIC_CI_RUNS_ON = ("ubuntu-latest",)
DEFAULT_QUALITY = "python -m ruff check --select E9,F63,F7,F82 ."
#: The interpreter ci.yml's pinned actions/setup-python installs (`[ci] python_version`).
DEFAULT_PYTHON_VERSION = "3.12"
#: How `doctor` recognises a Publisher App installation when no bot login is configured yet
#: (diagnosis only: lease markers are trusted from the EXACT configured login, never this).
PUBLISHER_APP_SLUG_HINT = "agentic-sdlc"
#: Where `onboard` looks for the hook interpreter, in preference order (`--hook-python` names
#: another, vetted the same way). Never a PATH search.
HOOK_PYTHON_CANDIDATES = ("/usr/bin/python3", "/bin/python3")
DEFAULT_HOOK_PYTHON = HOOK_PYTHON_CANDIDATES[0]
_PYTHON_VERSION = re.compile(r"^\d+\.\d+(?:\.\d+)?$")
#: The policy's gate commands, in the order ci.yml runs them.
GATE_COMMANDS = ("setup", "quality", "test")
HUMAN_REVIEW_LABEL = "human-review-required"
IMPLEMENTATION_LABEL = "implementation-approved"
RULESET_NAME = "Protect main"
REQUIRED_CHECK = "test"
#: The GitHub Actions app: the only integration that reports ci.yml's `test` job. A required
#: status check pinned (`integration_id`) to any other app can never be satisfied by it.
GITHUB_ACTIONS_INTEGRATION_ID = 15368

#: The label a cloud routine puts on an issue it cannot work on (an invalid body); step 1 of the
#: routine never selects an issue that carries it (Codex 4241196636).
BLOCKED_LABEL = "claude-blocked"
LABELS = {
    "claude-ready": ("0e8a16", "Specified precisely enough for the implementation worker"),
    HUMAN_REVIEW_LABEL: ("5319e7", "A human must review before merge"),
    IMPLEMENTATION_LABEL: ("1d76db", "Owner explicitly approved agent implementation"),
    "agentic-sdlc": ("0052cc", "Managed by the Forge agentic SDLC"),
    BLOCKED_LABEL: ("b60205", "Agent could not proceed; needs the owner"),
    "in-progress": ("fbca04", "Leased: an agent/session is actively implementing this"),
}

#: Labels Forge fixes by name (all but the configurable ready label), case-folded as GitHub
#: compares them. The ready label may collide with none of them, and they are distinct already.
_FIXED_LABEL_KEYS = frozenset(name.casefold() for name in LABELS if name != "claude-ready")
assert len(_FIXED_LABEL_KEYS) == len(LABELS) - 1, "Forge's fixed labels collide case-insensitively"
assert IN_PROGRESS_LABEL.casefold() in _FIXED_LABEL_KEYS

#: Binary media the default policy forbids: images, video, audio.
MEDIA_EXTENSIONS = (
    *("png", "jpg", "jpeg", "gif", "webp", "bmp", "ico", "tif", "tiff", "heic", "avif"),
    *("mp4", "mov", "avi", "mkv", "webm", "m4v", "wmv", "flv"),
    *("mp3", "wav", "flac", "ogg", "m4a", "aac", "opus", "wma", "aiff"),
)
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
    # Media (Codex 4239397981): AGENTS.md says it is never committed and that `forbidden_paths`
    # rejects it, so the default policy has to. evaluate_diff matches forbidden paths
    # case-insensitively (`LOGO.PNG`). SVG is text source, reviewed like code: not listed.
    *(pattern for ext in MEDIA_EXTENSIONS for pattern in (f"*.{ext}", f"**/*.{ext}")),
)
FORBIDDEN_BASELINE_CHECK = "policy keeps every generated forbidden path"
BASE_FORBIDDEN_TASKS = (
    r"\bdeploy\b.{0,40}\bprod",
    r"\b(?:print|return|expose|retrieve)\b.{0,40}\b(?:credential|password|secret|token)",
    r"\bdisable\b.{0,40}\b(?:auth|test|safety)",
)


class OnboardError(ValueError):
    """Raised when a repository cannot be onboarded safely."""


class GhCommandError(OnboardError):
    """A trusted `gh` call that exited non-zero. `http_status` is the HTTP status of GitHub's
    answer when -- and only when -- `gh` reported one unambiguously (`gh_error_http_status`);
    None for a timeout, a transport failure, or any output that is not a clean API error."""

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


#: The last line `gh api` prints on stderr for an HTTP error answer: `gh: <message> (HTTP nnn)`.
_GH_HTTP_ERROR_LINE = re.compile(r"^gh: .*\(HTTP ([1-5][0-9]{2})\)$")


def gh_error_http_status(stderr: str, stdout: str = "") -> int | None:
    """The HTTP status of a failed `gh api` call, read from the RESPONSE, never the request
    (Codex 4243386515: a repository or branch literally named `404` puts "404" in every echoed
    request path). That is the status `gh` reports on the last line of stderr
    (`gh: Not Found (HTTP 404)`); when the error body `gh` prints on stdout carries GitHub's own
    `status` field, it must agree. None whenever the two disagree or either is not that shape:
    callers treat None as "unknown", i.e. fail closed."""
    lines = [line.strip() for line in (stderr or "").strip().splitlines() if line.strip()]
    match = _GH_HTTP_ERROR_LINE.match(lines[-1]) if lines else None
    if match is None:
        return None
    status = int(match.group(1))
    try:
        body = json.loads(stdout) if (stdout or "").strip() else None
    except ValueError:
        body = None
    if isinstance(body, dict) and "status" in body and str(body["status"]) != str(status):
        return None
    return status


def gh_http_status(exc: BaseException) -> int | None:
    """The HTTP status a failed trusted `gh` call reported (`GhCommandError.http_status`), else
    None -- for any other exception, whatever its message says."""
    return exc.http_status if isinstance(exc, GhCommandError) else None


def gate_command_problem(command: object) -> str:
    """Why a policy gate command cannot be rendered into ci.yml unchanged ('' = it can). A gate
    is ONE line: a newline is a shell command separator, so folding it into a space would change
    what runs (`true\npytest` -> `true pytest`), and a YAML block would make the rendered step
    something doctor has to re-prove. `${{` is a GitHub expression, evaluated before the shell
    sees the command, so CI would not run the command the policy states."""
    text = str(command)
    if "\n" in text or "\r" in text:
        return "must be a single line (join commands with `&&` instead of newlines)"
    if _unrenderable_char(text):
        return "must not contain control characters"
    if "${{" in text:
        return "must not contain a GitHub expression (`${{`)"
    return ""


def _unrenderable_char(text: str) -> bool:
    """A character no generated file can carry verbatim: a control character other than tab
    (TOML forbids DEL raw), a line or paragraph separator (YAML and `str.splitlines` break a line
    there), or a lone surrogate (not encodable as UTF-8)."""
    return any((unicodedata.category(c) in {"Cc", "Cs", "Zl", "Zp"} and c != "\t") for c in text)


# ---------------------------------------------------------------- the policy fields
# ONE validator per field onboard writes into agentic-sdlc.toml. `OnboardSpec` runs it on what
# the caller passed, `doctor` on what the policy file holds (`policy_doc_values`), so a value
# onboard accepts doctor accepts, and a value doctor rejects onboard never writes.


def _str_problem(value: object) -> str:
    if not isinstance(value, str):
        return "must be a string"
    if not value.strip():
        return "must not be empty"
    if _unrenderable_char(value):
        return "must not contain control characters"
    return ""


def _project_id_problem(value: object) -> str:
    return _str_problem(value) or (
        "" if _PROJECT.fullmatch(str(value)) else "must use owner/name format"
    )


def _branch_problem(value: object) -> str:
    """A branch name: renderable and a valid Git branch name by `git check-ref-format --branch`'s
    rules, minus only the characters a downstream sink cannot carry (`policy.branch_name_problem`,
    which the policy loader applies too). Codex 4242545214: `release+next`, `feature@two` are
    valid.

    In process only (Codex 4243659475): this no longer also asks the installed git, which ran a
    bare `git` from PATH with the caller's whole environment (GH_TOKEN included) -- a planted one
    could read the token. `policy.git_branch_format_problem` implements git's rules itself, and
    tests/test_onboard.py checks it against the real `git check-ref-format --branch` over a
    generated corpus of names."""
    return _str_problem(value) or branch_name_problem(str(value))


def _bot_login_field_problem(value: object) -> str:
    """'' (none configured: no App bot's lease markers are trusted) or `<app-slug>[bot]`."""
    if value == "":
        return ""
    return publisher_bot_login_problem(value)


def _ready_label_problem(value: object) -> str:
    problem = _str_problem(value)
    if problem:
        return problem
    label = str(value)
    if label != label.strip():
        # load_policy strips it, so the label conditions would not match the written policy.
        return "must not start or end with whitespace"
    if len(label) > 50 or "," in label:
        # GitHub's 50-character limit; a comma would split the work-request front matter.
        return "must be 1-50 characters without commas"
    if label.casefold() in _FIXED_LABEL_KEYS:
        # GitHub label names and Actions string comparisons ignore case: `Implementation-Approved`
        # IS the approval label, and the issue template would apply it without the owner.
        return "must differ (case-insensitively) from Forge's fixed labels: " + ", ".join(
            sorted(_FIXED_LABEL_KEYS)
        )
    return ""


def _gate_problem(value: object) -> str:
    """A gate command: required (an empty step is not a gate), one line (`gate_command_problem`)."""
    return _str_problem(value) or gate_command_problem(value)


def _python_version_problem(value: object) -> str:
    if isinstance(value, str) and _PYTHON_VERSION.fullmatch(value):
        return ""
    return "must look like 3.12 or 3.12.4"


def _paths_problem(value: object) -> str:
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) and item and not _unrenderable_char(item) for item in value
    ):
        return "must be a list of non-empty strings without control characters"
    return ""


def _cap_problem(maximum: int) -> Callable[[object], str]:
    def check(value: object) -> str:
        if type(value) is not int or not 1 <= value <= maximum:
            return f"must be an integer between 1 and {maximum}"
        return ""

    return check


def _bool_problem(value: object) -> str:
    return "" if isinstance(value, bool) else "must be true or false"


def _approvals_problem(value: object) -> str:
    if type(value) is not int or not 0 <= value <= MAX_REQUIRED_APPROVALS:
        return f"must be an integer between 0 and {MAX_REQUIRED_APPROVALS}"
    return ""


#: field -> (OnboardSpec attribute, policy table, policy key, validator). The caps' maxima are
#: load_policy's (`policy.py`).
POLICY_FIELDS: dict[str, tuple[str, str, str, Callable[[object], str]]] = {
    "project id": ("project_id", "project", "id", _project_id_problem),
    "default branch": ("default_branch", "project", "default_branch", _branch_problem),
    "ready label": ("ready_label", "automation", "ready_label", _ready_label_problem),
    "setup": ("setup_command", "commands", "setup", _gate_problem),
    "quality": ("quality_command", "commands", "quality", _gate_problem),
    "test": ("test_command", "commands", "test", _gate_problem),
    "python version": ("python_version", "ci", "python_version", _python_version_problem),
    "forbidden paths": ("forbidden_paths", "policy", "forbidden_paths", _paths_problem),
    "protected paths": ("protected_paths", "policy", "protected_paths", _paths_problem),
    "max changed files": (
        "max_changed_files",
        "policy",
        "max_changed_files",
        _cap_problem(10_000),
    ),
    "max diff lines": ("max_diff_lines", "policy", "max_diff_lines", _cap_problem(10_000_000)),
    "publisher bot login": (
        "publisher_bot_login",
        "automation",
        "publisher_bot_login",
        _bot_login_field_problem,
    ),
    "trust mode": ("trust_mode", "automation", "trust_mode", trust_mode_problem),
    "ephemeral runners": (
        "ephemeral_runners",
        "automation",
        "ephemeral_runners",
        _bool_problem,
    ),
    "required approvals": (
        "required_approvals",
        "policy",
        "required_approvals",
        _approvals_problem,
    ),
}
#: Policy fields a policy may omit (written before the field existed): their default -- what
#: that older onboard did (strict executables, no runner acknowledgement, no required approval).
_POLICY_FIELD_DEFAULTS = {
    "python version": DEFAULT_PYTHON_VERSION,
    "publisher bot login": "",
    "trust mode": STRICT,
    "ephemeral runners": False,
    "required approvals": 0,
}
_MISSING = object()


def policy_field_location(field: str) -> str:
    """`[table] key` of a POLICY_FIELDS field, as problems name it."""
    _, table, key, _ = POLICY_FIELDS[field]
    return f"[{table}] {key}"


def policy_field_problem(field: str, value: object = _MISSING) -> str:
    """Why onboard cannot write `value` for a POLICY_FIELDS field ('' = it can), named by its
    policy location. No value = missing."""
    if value is _MISSING:
        return f"policy has no {policy_field_location(field)}"
    problem = POLICY_FIELDS[field][3](value)
    return f"policy {policy_field_location(field)} {problem}" if problem else ""


def policy_value_problems(values: dict[str, object]) -> list[str]:
    """Every POLICY_FIELDS value that onboard cannot write (and doctor therefore rejects), one
    problem each. A field absent from `values` is missing."""
    found = (policy_field_problem(f, values.get(f, _MISSING)) for f in POLICY_FIELDS)
    return [problem for problem in found if problem]


def policy_doc_values(policy_doc: object) -> dict[str, object]:
    """The POLICY_FIELDS values a parsed policy holds (absent fields omitted, defaulted ones
    filled), for `policy_value_problems`."""
    doc = policy_doc if isinstance(policy_doc, dict) else {}
    values: dict[str, object] = {}
    for name, (_, table, key, _) in POLICY_FIELDS.items():
        section = doc.get(table)
        if isinstance(section, dict) and key in section:
            values[name] = section[key]
        elif name in _POLICY_FIELD_DEFAULTS:
            values[name] = _POLICY_FIELD_DEFAULTS[name]
    return values


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
    #: PUBLIC_CI_RUNS_ON for a fork-exposed repository.
    ci_runs_on: tuple[str, ...] = ()
    #: Fork pull requests may reach ci.yml: the repository is public, or private/internal without
    #: forking or fork pull-request workflows PROVEN disabled (`fork_pull_request_exposure`).
    fork_exposed: bool = False
    default_branch: str = "main"
    ready_label: str = "claude-ready"
    forbidden_paths: tuple[str, ...] = ()
    protected_paths: tuple[str, ...] = ()
    max_changed_files: int = 20
    max_diff_lines: int = 2500
    python_version: str = DEFAULT_PYTHON_VERSION
    #: The Publisher App's bot login (`<app-slug>[bot]`), the only App bot whose lease markers
    #: count besides `github-actions[bot]` (Codex 4242753917); '' = none.
    publisher_bot_login: str = ""
    #: The interpreter the Claude Code hooks run (`trusted_hook_python`, Codex 4242753911).
    hook_python: str = DEFAULT_HOOK_PYTHON
    #: `[automation] trust_mode` (`trusted_exec`): "strict" (git, gh and the hook interpreter
    #: only root can replace) or "local" (a developer's own machine). Agent contexts -- GitHub
    #: Actions, Claude Code cloud sessions -- are strict at run time whatever this says.
    trust_mode: str = STRICT
    #: `[automation] ephemeral_runners`: the owner acknowledged that self-hosted runners are
    #: ephemeral or contained (`--ephemeral-runners`); required for any self-hosted label.
    ephemeral_runners: bool = False
    #: `[policy] required_approvals`: the ruleset's `required_approving_review_count`.
    required_approvals: int = 0

    def __post_init__(self) -> None:
        if not _HOOK_PYTHON_PATH.fullmatch(self.hook_python):
            raise OnboardError(
                f"hook interpreter must be an absolute path, got {self.hook_python!r}"
            )
        if not _PROJECT.fullmatch(self.platform_repository):
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
        if not _linux_labels(self.runs_on):
            raise OnboardError(f"{LINUX_RUNNER_REQUIRED} (got --runs-on {','.join(self.runs_on)})")
        short = _short_job_labels((*self.runs_on, *self.ci_runs_on))
        if short:
            raise OnboardError(f"{SHORT_JOB_UNSUPPORTED} (got {', '.join(short)})")
        if self.fork_exposed and not _github_hosted(set(self.ci_labels)):
            raise OnboardError(
                "a fork-exposed (public, or forkable private/internal) repository's CI runs fork "
                "pull-request code: it must use a "
                f"GitHub-hosted runner (e.g. --ci-runs-on {PUBLIC_CI_RUNS_ON[0]}), not "
                + ",".join(self.ci_labels)
            )
        # The SAME validator doctor runs on the written policy (POLICY_FIELDS).
        values = self.policy_values()
        problems = [f"{f}: {p}" for f in POLICY_FIELDS if (p := policy_field_problem(f, values[f]))]
        if problems:
            raise OnboardError("; ".join(problems))
        # Last, so every other runner rule reports first (Codex 4243193108).
        self_hosted = [
            ",".join(labels)
            for labels in dict.fromkeys((tuple(self.runs_on), tuple(self.ci_labels)))
            if not _github_hosted(set(labels))
        ]
        if self_hosted and not self.ephemeral_runners:
            raise OnboardError(
                f"self-hosted runner labels ({'; '.join(self_hosted)}) need an explicit "
                f"acknowledgement: {SELF_HOSTED_NEEDS_ACK}"
            )

    def policy_values(self) -> dict[str, object]:
        """The spec's value of every POLICY_FIELDS field (what `render_policy` writes)."""
        return {field: getattr(self, spec[0]) for field, spec in POLICY_FIELDS.items()}

    @property
    def name(self) -> str:
        return self.project_id.split("/")[1]

    @property
    def ci_labels(self) -> tuple[str, ...]:
        """Runner labels of ci.yml's `test` job."""
        if self.ci_runs_on:
            return self.ci_runs_on
        return PUBLIC_CI_RUNS_ON if self.fork_exposed else self.runs_on

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


#: Codex 4240421134: every `gh` call is bounded, so a stalled network, authentication or API
#: call fails closed (OnboardError) instead of hanging claim/renew/release/authorize-labeler/
#: onboard/doctor forever. Seconds per call; `AGENTIC_SDLC_GH_TIMEOUT` overrides it (1-3600).
GH_TIMEOUT_SECONDS = 60
GH_TIMEOUT_ENV = "AGENTIC_SDLC_GH_TIMEOUT"


def gh_timeout() -> float:
    """The per-call `gh` timeout: GH_TIMEOUT_ENV when it is a number in 1-3600, else the
    default (an unusable value never means "no timeout")."""
    raw = os.environ.get(GH_TIMEOUT_ENV, "").strip()
    try:
        value = float(raw)
    except ValueError:
        return GH_TIMEOUT_SECONDS
    return value if 1 <= value <= 3600 else GH_TIMEOUT_SECONDS


#: Variables that would let the caller re-point `gh` at another repository or another host (one
#: it can answer from: `GH_HOST` + an enterprise token). Every call here names its repository
#: explicitly (`repos/<owner>/<name>/...`, GraphQL `owner`/`name`), so none of them is needed,
#: and the host is pinned to github.com.
_GH_REDIRECT_ENV = ("GH_REPO", "GH_HOST", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
GH_PINNED_HOST = "github.com"
#: Where `gh` finds its token: the first of these that is set (gh's own precedence).
GH_TOKEN_ENV = ("GH_TOKEN", "GITHUB_TOKEN")
#: The caller's variables the trusted `gh` still gets (Codex 4241839914): the egress proxy
#: settings and nothing else. A proxy is safe to honour while the TLS trust store is not the
#: caller's: `SSL_CERT_FILE` / `SSL_CERT_DIR` are dropped, so `api.github.com` is verified
#: against the system store (root-owned, like the trusted executable itself) and a proxy the
#: caller runs can only relay the encrypted stream -- it cannot read or fabricate an answer.
#: Stripping the proxy instead would break every call where egress goes through one.
GH_PROXY_ENV = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy")
#: Variables that make the dynamic loader run code of the caller's choosing in any program.
_LOADER_ENV_PREFIXES = ("LD_", "DYLD_")
#: The fixed search path a trusted program runs with.
SAFE_PATH = "/usr/bin:/bin"


def _gh_token(program: str, timeout: float) -> str:
    """The token `gh` authenticates with: GH_TOKEN, else GITHUB_TOKEN, else what the user's own
    `gh auth login` stored (`gh auth token`, a local read: no request is made). Empty when
    there is none -- the call then runs unauthenticated and GitHub's answer decides."""
    for key in GH_TOKEN_ENV:
        value = os.environ.get(key, "")
        if value:
            return value
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in _GH_REDIRECT_ENV and not k.startswith(_LOADER_ENV_PREFIXES)
    }
    env["PATH"] = SAFE_PATH
    try:
        proc = subprocess.run(
            [program, "auth", "token", "--hostname", GH_PINNED_HOST],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    token = proc.stdout.strip() if proc.returncode == 0 else ""
    return token if token and not any(c.isspace() for c in token) else ""


def _gh_env(config_dir: str, token: str) -> dict[str, str]:
    """The whole environment of a trusted `gh` call, BUILT from an allowlist (Codex 4241839908,
    4241839914), never inherited: no loader variable (`LD_PRELOAD`, `DYLD_*`, ...) injects code;
    `GH_CONFIG_DIR` and HOME are `config_dir`, a fresh private empty directory, so no config of
    the caller's (`http_unix_socket`, aliases, extensions, a stored host) applies; no custom CA
    (`SSL_CERT_FILE`, `SSL_CERT_DIR`) can make a fabricated answer verify; the host is pinned;
    the token is `token`; the proxy settings are the only caller variables kept
    (`GH_PROXY_ENV`)."""
    env = {key: os.environ[key] for key in GH_PROXY_ENV if os.environ.get(key)}
    env.update(
        PATH=SAFE_PATH,
        HOME=config_dir,
        GH_CONFIG_DIR=config_dir,
        GH_HOST=GH_PINNED_HOST,
        GH_PROMPT_DISABLED="1",
        GH_NO_UPDATE_NOTIFIER="1",
        GH_SPINNER_DISABLED="1",
        NO_COLOR="1",
        LC_ALL="C.UTF-8",
    )
    if token:
        env["GH_TOKEN"] = token
    return env


def run_gh(args: Sequence[str], input: str | None = None) -> str:
    """Run `gh` and return stdout; raises OnboardError with stderr on failure, and when the call
    does not finish within `gh_timeout()` seconds.

    Never a bare `gh` (Codex 4241745403): a PATH lookup could find one this session planted to
    fabricate the label, permission, issue and dependency answers `authorize-labeler`,
    `check-request` and `claim`/`renew` decide on. The executable is `trusted_gh()`'s -- one only
    root can replace -- and with none, the call fails closed (OnboardError: exit 2). It runs in
    `_gh_env`'s allowlisted environment with a fresh, private, empty config directory that is
    removed afterwards (Codex 4241839914)."""
    try:
        program = trusted_gh()
    except UntrustedExecutableError as exc:
        raise OnboardError(str(exc)) from exc
    timeout = gh_timeout()
    token = _gh_token(program, timeout)
    try:
        with tempfile.TemporaryDirectory(prefix="agentic-sdlc-gh-") as config_dir:
            os.chmod(config_dir, 0o700)
            proc = subprocess.run(
                [program, *args],
                check=False,
                capture_output=True,
                text=True,
                input=input,
                timeout=timeout,
                env=_gh_env(config_dir, token),
                cwd=config_dir,
            )
    except FileNotFoundError as exc:  # pragma: no cover - environment
        raise OnboardError("GitHub CLI `gh` is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise OnboardError(
            f"gh {' '.join(shlex.quote(a) for a in args)} did not answer within {timeout:g}s; "
            "not concluding"
        ) from exc
    if proc.returncode != 0:
        raise GhCommandError(
            f"gh {' '.join(shlex.quote(a) for a in args)} failed: {proc.stderr.strip()[:400]}",
            http_status=gh_error_http_status(proc.stderr, proc.stdout),
        )
    return proc.stdout


@dataclass(frozen=True)
class Unreadable:
    """A GitHub API answer that could not be read: the call failed, or it answered something
    that is not the documented shape. It is never "empty", "absent", "public" or "none
    configured": only an explicit successful answer proves those. `not_found` is True when the
    call failed with a 404 -- "absent" ONLY where that is the endpoint's documented meaning.
    It has no truth value and cannot be iterated, so a caller that forgets to handle it fails
    loudly instead of silently reading it as an empty answer."""

    why: str
    not_found: bool = False

    def __bool__(self) -> bool:
        raise TypeError(f"unhandled unreadable GitHub answer ({self.why})")

    def __iter__(self):
        raise TypeError(f"unhandled unreadable GitHub answer ({self.why})")


def _safe_json(gh: GhRunner, args: Sequence[str]) -> object:
    """The parsed JSON `gh` answers, or `Unreadable` (a failed call, an empty or non-JSON
    answer). Callers decide what an unreadable answer means; it is never an empty one."""
    where = " ".join(a for a in args if not a.startswith("-"))[:160]
    try:
        out = gh(args)
    except OnboardError as exc:
        # GitHub's answer, never "404" anywhere in the message (Codex 4244027557)
        return Unreadable(f"{where}: {str(exc)[-160:]}", gh_http_status(exc) == 404)
    if not out or not out.strip():
        return Unreadable(f"{where}: empty answer")
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return Unreadable(f"{where}: not JSON")


def _paged_items(gh: GhRunner, path: str, key: str | None = None) -> list[dict] | Unreadable:
    """Every item of a paginated list endpoint, or `Unreadable`. --slurp wraps the pages in one
    array; each page is a JSON array (key=None) or an object carrying the items under `key`.
    Any page of another shape, or any item that is not an object, makes the whole list
    unreadable: a partial or malformed inventory is never read as "nothing there"."""
    pages = _safe_json(gh, ["api", path, "--paginate", "--slurp"])
    if isinstance(pages, Unreadable):
        return pages
    if not isinstance(pages, list):
        return Unreadable(f"{path}: not a list of pages")
    items: list = []
    for page in pages:
        rows = page if key is None else page.get(key) if isinstance(page, dict) else None
        if not isinstance(rows, list):
            return Unreadable(f"{path}: a page is not a list" + (f" of {key!r}" if key else ""))
        items += rows
    if not all(isinstance(item, dict) for item in items):
        return Unreadable(f"{path}: an item is not an object")
    return items


def _paged_names(gh: GhRunner, path: str, key: str) -> set[str] | Unreadable:
    items = _paged_items(gh, path, key)
    if isinstance(items, Unreadable):
        return items
    return {str(item.get("name")) for item in items}


def _q(ref: str) -> str:
    """A git ref (branch, tag, SHA) as ONE URL path component or query value: `/` and `%` in a
    valid ref name (`feature/onboarding`) must not split the REST path or read as an escape."""
    return quote(ref, safe="")


def resolve_platform_ref(platform_repository: str, ref: str, gh: GhRunner = run_gh) -> str:
    """Accept a SHA as-is; otherwise resolve a branch/tag on the platform repo to its commit SHA."""
    if _SHA.fullmatch(ref):
        return ref
    sha = gh(["api", f"repos/{platform_repository}/commits/{_q(ref)}", "--jq", ".sha"]).strip()
    if not _SHA.fullmatch(sha):
        raise OnboardError(f"could not resolve {platform_repository}@{ref} to a commit SHA")
    return sha


FORK_PR_WORKFLOWS_PRIVATE = "actions/permissions/fork-pr-workflows-private-repos"


def fork_pull_request_exposure(repo_info: object, gh: GhRunner, project_id: str) -> tuple[str, str]:
    """("safe" | "exposed" | "unknown", why): can fork pull requests run workflows here?

    A PUBLIC repository always can. A private or internal one can too when forking is allowed
    (repository/organization/enterprise policy) and fork pull-request workflows are enabled for
    private repositories -- an enterprise member or read collaborator then runs PR code on
    whatever runner the job targets. Safe only when GitHub PROVES one of the two is off:
    `allow_forking == false` on the repository, or `run_workflows_from_fork_pull_requests ==
    false` from the fork-PR workflow policy. Anything unread (a 403/404 on that endpoint, a
    missing field, an unknown visibility) is "unknown", never safe."""
    visibility = _visibility(repo_info)
    if visibility is None:
        return "unknown", "the repository's visibility could not be read"
    if visibility == "public":
        return "exposed", "public repository: anyone can fork it and open a pull request"
    if visibility not in ("private", "internal"):
        return "unknown", f"unrecognised visibility {visibility!r}"
    assert isinstance(repo_info, dict)
    if repo_info.get("allow_forking") is False:
        return "safe", f"{visibility} repository with forking disabled"
    policy = _safe_json(gh, ["api", f"repos/{project_id}/{FORK_PR_WORKFLOWS_PRIVATE}"])
    runs = policy.get("run_workflows_from_fork_pull_requests") if isinstance(policy, dict) else None
    if runs is False:
        return "safe", f"{visibility} repository with fork pull-request workflows disabled"
    forking = repo_info.get("allow_forking")
    if runs is True:
        return "exposed", (
            f"{visibility} repository runs workflows from fork pull requests"
            + (" and allows forking" if forking is True else "")
        )
    return "unknown", (
        f"{visibility} repository: "
        + ("forking is allowed" if forking is True else "whether forking is allowed is unknown")
        + " and the fork pull-request workflow policy could not be read"
    )


def repository_fork_exposed(project_id: str, gh: GhRunner = run_gh) -> bool:
    """Whether onboarding must treat the repository as fork-exposed (`fork_pull_request_exposure`
    not proven "safe"). Raises when even the visibility cannot be read: ask, never guess."""
    info = _safe_json(gh, ["api", f"repos/{project_id}"])
    if _visibility(info) is None:
        raise OnboardError(f"could not read the visibility of {project_id}")
    return fork_pull_request_exposure(info, gh, project_id)[0] != "safe"


# `owner/name` as GitHub answers it in `full_name`.
_FULL_NAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")


def _canonical_full_name(repo_info: object, project_id: str) -> str | None:
    """GitHub's own `owner/name` for `project_id` from `GET repos/{project_id}`: the same name in
    GitHub's spelling (logins and repository names are case-insensitive, so the API answers any
    spelling), or -- after a rename or transfer, which the API follows as a redirect -- the
    CURRENT name (Codex 4239868019: every run passes that current `$GITHUB_REPOSITORY`, so the
    stale slug can never be the canonical one). None when unread or malformed."""
    name = repo_info.get("full_name") if isinstance(repo_info, dict) else None
    if not isinstance(name, str) or not _FULL_NAME.fullmatch(name):
        return None
    return name


REPO_WRITABLE_CHECK = "repository is writable (not archived or disabled)"


def repository_writable(info: object, project_id: str) -> tuple[bool | None, str]:
    """Whether GitHub accepts writes to the repository `GET repos/{project_id}` describes
    (Codex 4240170627): an ARCHIVED repository's issues, pull requests, comments, branches,
    labels and rulesets are read-only, and a DISABLED one serves nothing, so no claim, plan
    comment, publisher push or draft PR can work there. (True, '') only when the answer says
    `archived: false` AND `disabled: false` explicitly; (False, why) when either is true;
    (None, why) when either is missing, not a boolean, or the answer is unreadable."""
    if not isinstance(info, dict):
        why = info.why if isinstance(info, Unreadable) else "not a repository object"
        return None, f"could not read GET repos/{project_id} ({why})"
    archived, disabled = info.get("archived"), info.get("disabled")
    if archived is True:
        return False, (
            f"{project_id} is archived: GitHub makes its issues, pull requests, comments, "
            "branches and labels read-only → unarchive it (Settings → General → Danger Zone → "
            "Unarchive this repository)"
        )
    if disabled is True:
        return False, (
            f"{project_id} is disabled by GitHub: it accepts no writes → resolve the "
            "suspension with GitHub (the repository's banner names the reason)"
        )
    if archived is False and disabled is False:
        return True, ""
    return None, (
        f"GET repos/{project_id} did not report archived/disabled as booleans "
        f"(archived={archived!r}, disabled={disabled!r}) → confirm the repository is writable"
    )


def require_writable_repository(info: object, project_id: str) -> None:
    """Raise unless `info` proves the repository writable (`repository_writable`): `--apply`
    refuses an archived, disabled or unconfirmed repository before its first mutation."""
    writable, why = repository_writable(info, project_id)
    if writable is not True:
        raise OnboardError(f"{why}; nothing was changed")


def canonical_project_id(project_id: str, gh: GhRunner = run_gh) -> str:
    """`project_id` as GitHub names it now (`full_name`): every workflow run passes the canonical
    `$GITHUB_REPOSITORY` to `prepare-request`, which compares it EXACTLY with the policy's id, so
    the policy must carry that name -- GitHub's spelling, and the new name of a renamed or
    transferred repository (the caller tells the user). Unreadable, or an answer without a
    `full_name`: raises -- `--apply` writes this id into the policy and configures the
    repository by it, so it never proceeds on a guess. An archived or disabled repository (or
    one not proven writable) raises too, before `--apply` writes a file (Codex 4240170627)."""
    info = _safe_json(gh, ["api", f"repos/{project_id}"])
    if not isinstance(info, dict):
        why = info.why if isinstance(info, Unreadable) else "not a repository object"
        raise OnboardError(f"could not read {project_id} from GitHub ({why}); nothing was changed")
    canonical = _canonical_full_name(info, project_id)
    if canonical is None:
        raise OnboardError(
            f"could not read {project_id} from GitHub (no valid full_name in the answer); "
            "nothing was changed"
        )
    require_writable_repository(info, project_id)
    return canonical


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


def _branch_filter_literal(branch: str) -> str:
    """`branch` as a GitHub workflow branch-filter pattern matching exactly that one branch:
    every filter-special character (`*?+[]\\!`) backslash-escaped (the filter pattern cheat
    sheet's escape). Git refuses `*?[\\` in branch names; `release+next` needs `\\+`, or the
    filter would read `+` as "one or more `e`" and never match the branch itself."""
    return "".join("\\" + c if c in _GLOB_SPECIAL else c for c in branch)


def _yaml_labels(labels: Sequence[str]) -> str:
    """A YAML flow sequence of quoted strings (runner labels stay labels, never true/null/123)."""
    return "[" + ", ".join(_yaml_str(label) for label in labels) + "]"


def _yaml_run(command: str) -> str:
    """A gate command as one quoted scalar (`_yaml_str`), byte for byte. Never normalized:
    `gate_command_problem` refuses the multiline commands a normalization would change."""
    problem = gate_command_problem(command)
    if problem:
        raise OnboardError(f"gate command {command!r} {problem}")
    return _yaml_str(command)


def _md_code(value: object) -> str:
    """`value` as a Markdown inline code span that no backtick inside it can close: fenced by a
    backtick run longer than any it contains, padded when it starts or ends with one."""
    text = str(value).replace("\n", " ")
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") or not text.strip() else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _md_block(lines: Sequence[str], info: str = "") -> str:
    """`lines` as a fenced Markdown code block whose fence no line can close (longer than any
    backtick run inside)."""
    longest = max((len(run) for line in lines for run in re.findall(r"`+", line)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{info}\n" + "".join(f"{line}\n" for line in lines) + fence


def _expr_str(value: str) -> str:
    """A GitHub expression string literal: single-quoted, `'` doubled."""
    return "'" + str(value).replace("'", "''") + "'"


def auto_plan_condition(ready: str, human_review: str, implementation: str) -> str:
    """The `if:` of agent-auto-plan.yml's `preflight` job. Either planning label triggers (Codex
    4241196642: removing `human-review-required` cancels planning, and re-adding it must start it
    again), and both must be on the issue. An issue opened with both labels fires one `labeled`
    run per label: they share one concurrency group, so they run one after the other, and the
    `dedupe` job skips the second when a plan was already posted for the current approval."""
    return (
        "github.event.action == 'labeled' &&\n"
        f"(github.event.label.name == {_expr_str(ready)} ||\n"
        f" github.event.label.name == {_expr_str(human_review)}) &&\n"
        "github.event.issue.pull_request == null &&\n"
        f"contains(github.event.issue.labels.*.name, {_expr_str(ready)}) &&\n"
        f"contains(github.event.issue.labels.*.name, {_expr_str(human_review)}) &&\n"
        f"!contains(github.event.issue.labels.*.name, {_expr_str(implementation)})"
    )


def auto_implement_condition(ready: str, human_review: str, implementation: str) -> str:
    """The `if:` of agent-auto-implement.yml's `authorize` job: any approval label triggers, and
    every one of them must be on the issue, so a ready label alone never starts implementation."""
    return (
        "github.event.action == 'labeled' &&\n"
        f"(github.event.label.name == {_expr_str(implementation)} ||\n"
        f" github.event.label.name == {_expr_str(ready)} ||\n"
        f" github.event.label.name == {_expr_str(human_review)}) &&\n"
        "github.event.issue.pull_request == null &&\n"
        f"contains(github.event.issue.labels.*.name, {_expr_str(implementation)}) &&\n"
        f"contains(github.event.issue.labels.*.name, {_expr_str(ready)}) &&\n"
        f"contains(github.event.issue.labels.*.name, {_expr_str(human_review)})"
    )


def auto_concurrency_group(kind: str, triggers: Sequence[str], required: Sequence[str]) -> str:
    """The workflow `concurrency.group` of agent-auto-{kind}.yml (Codex 4240628982). A `labeled`
    event of a label that TRIGGERS the work, an `unlabeled` event of a label the work REQUIRES
    and an `edited` event (Codex 4241196649: a title or body edit invalidates the approval)
    share one group per issue, so removing an approval label or editing the issue cancels a run
    that is queued or running (`cancel-in-progress` is true for `unlabeled` and `edited` runs
    only, `AUTO_CANCEL_IN_PROGRESS`); every other label event -- the lease's own `in-progress`
    label included -- gets a group of its own and cancels nothing. A `closed` event joins the
    issue's group too (implementation: Codex 4242753923; planning: Codex 4242925431): closing
    the issue cancels a queued or running run, and the `closed` run itself starts nothing
    (`authorize`/`preflight` run only on `labeled`)."""

    def named(labels: Sequence[str]) -> str:
        return " || ".join(f"github.event.label.name == {_expr_str(label)}" for label in labels)

    return (
        f"${{{{ ((github.event.action == 'labeled' && ({named(triggers)})) ||\n"
        f"(github.event.action == 'unlabeled' && ({named(required)})) ||\n"
        "github.event.action == 'edited' ||\n"
        "github.event.action == 'closed') &&\n"
        f"format('forge-auto-{kind}-{{0}}', github.event.issue.number) ||\n"
        f"format('forge-auto-{kind}-{{0}}-run-{{1}}',\n"
        "github.event.issue.number, github.run_id) }}"
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
    patterns = "".join(f"  {_toml_str(p)},\n" for p in BASE_FORBIDDEN_TASKS)
    return f"""\
version = 1

[project]
id = {_toml_str(spec.project_id)}
provider = "github"
default_branch = {_toml_str(spec.default_branch)}

[agents]
planner = {_toml_str(GENERATED_PLANNER)}
implementer = {_toml_str(implementer)}
reviewer = "codex"
# Where implementation runs: "actions" (agent-implement.yml / agent-auto-implement.yml) or
# "cloud-routine" (docs/forge/cloud-implementer.md). `sdlcctl doctor` reads this, never infers it.
implementation_mode = {_toml_str(spec.implementation_mode)}

[automation]
default_mode = "plan"
ready_label = {_toml_str(spec.ready_label)}
human_review_label = {_toml_str(HUMAN_REVIEW_LABEL)}
implementation_label = {_toml_str(IMPLEMENTATION_LABEL)}
lease_ttl_minutes = 240
# The Publisher App's bot login, `<app-slug>[bot]`: the only GitHub App whose lease markers
# count (besides github-actions[bot]). Empty: none does. Set it with `sdlcctl onboard --force
# --publisher-bot-login <app-slug>[bot]`.
publisher_bot_login = {_toml_str(spec.publisher_bot_login)}
# Which git, gh and hook interpreter Forge runs: "strict" (only ones root alone can replace) or
# "local" (a developer's own machine: the first on PATH, owned by root or the user). GitHub Actions
# and Claude Code cloud sessions are always strict. Change with `sdlcctl onboard --force
# --trust-mode`.
trust_mode = {_toml_str(spec.trust_mode)}
# true: the owner acknowledged that the self-hosted runners the Forge jobs use are ephemeral or
# contained (`--ephemeral-runners`); they run model-produced patches and pull-request code.
ephemeral_runners = {"true" if spec.ephemeral_runners else "false"}
{routing}
[policy]
human_merge_required = true
# Approvals the "{RULESET_NAME}" ruleset requires before a merge (`--required-approvals`). 0 means
# GitHub does not enforce the human-merge boundary: whoever can merge, merges.
required_approvals = {spec.required_approvals}
max_changed_files = {spec.max_changed_files}
max_diff_lines = {spec.max_diff_lines}
max_patch_bytes = 5000000
forbidden_paths = {_toml_list(forbidden)}
protected_paths = {_toml_list(spec.protected_paths)}
low_risk_paths = ["docs/**", "**/*.md"]
forbidden_task_patterns = [
{patterns}]

[commands]
# Rendered verbatim into ci.yml's `test` job, which doctor requires to match exactly: change a
# command here and re-run `sdlcctl onboard --force`. One line each.
setup = {_toml_str(spec.setup_command)}
quality = {_toml_str(spec.quality_command)}
test = {_toml_str(spec.test_command)}

[ci]
# The interpreter ci.yml's pinned actions/setup-python installs.
python_version = {_toml_str(spec.python_version)}

[verification]
gates = ["setup", "quality", "test"]
timeout_seconds = {{ setup = 900, quality = 300, test = 900 }}
"""


def render_agents_md(spec: OnboardSpec) -> str:
    return f"""# {spec.project_id} Agent Guide

## Authority

The repository owner defines product intent and remains the final merge authority.
Claude Code is the planner: the plan workflows (`agent-plan.yml`, `agent-auto-plan.yml`) run it
through the platform's `reusable-plan.yml`. Codex is the independent reviewer. The configured
implementer may implement an explicitly approved work request only.

## Required Workflow

0. **Claim before you code.** Every change maps to an issue. Lease it first
   (`sdlcctl claim --project {spec.project_id} --issue <N> --agent <you> --session <id>
   --branch forge/issue-<N>`);
   if the claim fails, run `sdlcctl release` with the same `--session` (best-effort: it retracts
   a claim marker left queued) and pick different work. Never work on an
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
- Never commit media, user data, credentials, or `.env*` files. `forbidden_paths` rejects
  image, video and audio files (SVG is source, reviewed like code), key files, SQLite
  databases and `.env*`; other user data is on you.
- Touching a `protected_paths` entry adds an architect/security review gate.

## Verification

{_md_block((spec.setup_command, spec.quality_command, spec.test_command), "bash")}

The controlling machine-readable policy is `agentic-sdlc.toml`.
"""


def render_claude_md(spec: OnboardSpec) -> str:
    return f"""# {spec.name} — working agreement for Claude Code

Read `AGENTS.md` first; it is the authoritative agent guide. Highlights:

- **Claim first.** Work only on an issue you lease (`sdlcctl claim … --session <your session id>`);
  the SessionStart hook prints the id and which issues other agents hold. The commit guard blocks
  every git command but read-only ones on `*/issue-N` branches without your lease.
- **Tests first.** Every change ships with tests. Run {_md_code(spec.test_command)} before
  proposing a change.
- **Merge authority is the repository owner.** Agents open draft PRs only; never merge,
  approve, or deploy.
- **Never touch** `forbidden_paths` from `agentic-sdlc.toml` (workflows, policy, secrets,
  media, data).
- Work requests must follow `.github/ISSUE_TEMPLATE/agent-work-request.md` exactly
  (`## Summary`, `## Acceptance Criteria`, `## Required Tests`, `## Non-Goals`, `## Dependencies`;
  the last two as bullet lists, dependencies as `#N` references or `None`).
"""


def _routine_search(spec: OnboardSpec) -> str:
    """The GitHub issue search step 1 of the cloud routine runs: every approval label, no
    `claude-blocked`, oldest first. (Open PRs are excluded by `sdlcctl claim`, which refuses an
    issue with one.)"""
    labels = (spec.ready_label, HUMAN_REVIEW_LABEL, IMPLEMENTATION_LABEL)
    terms = [f"label:{json.dumps(label)}" for label in labels]
    return " ".join([*terms, f"-label:{json.dumps(BLOCKED_LABEL)}", "sort:created-asc"])


#: How many candidates step 1 of the cloud routine lists (Codex 4241327879): `gh issue list`
#: returns 30 by default, and an issue that is leased elsewhere or fails authorization is
#: skipped WITHOUT leaving the search (only `claude-blocked` does), so 30 such issues at the head
#: of the queue would starve every run. 1000 is also the most results GitHub's issue search
#: returns for one query; a backlog of more than 1000 approved candidates is out of scope (the
#: rest are reached as the oldest ones are implemented or retired).
ROUTINE_CANDIDATE_LIMIT = 1000


def cloud_routine_setup_script(spec: OnboardSpec) -> str:
    """The cloud routine environment's setup script (Codex 4242373521): `sdlcctl` from the
    platform repository at the pinned commit (`platform_ref`, a full SHA), the same source and
    ref the Actions workflows check out."""
    source = f"git+https://github.com/{spec.platform_repository}@{spec.platform_ref}"
    return (
        "python3 -m pip install --user --disable-pip-version-check "
        + shlex.quote(f"agentic-sdlc @ {source}")
        + "\n"
    )


def render_cloud_routine_md(spec: OnboardSpec) -> str:
    search = shlex.quote(_routine_search(spec))
    lease = f"--project {spec.project_id} --issue <n> --session <run id>"
    release = f"`sdlcctl release {lease}`"
    renew = f"`sdlcctl renew {lease} --config agentic-sdlc.toml`"
    # Shell words (Codex 4242545214): a Git-valid branch name can hold `&`, `;`, `(`, `'` ...
    # Fully qualified (Codex 4242643519): `git fetch origin @` fetches the remote's HEAD, not a
    # branch named `@`, and leaves origin/@ stale; a bare name would also prefer a same-named
    # tag (refs/tags/ is searched before refs/heads/). `refs/heads/<name>` is that branch alone
    # and still updates origin/<name> through the configured refspec.
    branch = shlex.quote(f"refs/heads/{spec.default_branch}")
    base = shlex.quote(f"origin/{spec.default_branch}")
    check_request = (
        f"sdlcctl check-request --project {spec.project_id} --issue <n> "
        f"--config agentic-sdlc.toml --base {base}"
    )
    check_diff = (
        f"sdlcctl check-diff --project {spec.project_id} --config agentic-sdlc.toml --base {base}"
    )
    final_gate = (
        f"{check_request} --expect-digest <digest> --authorize-labels"
        f" --label {shlex.quote(IMPLEMENTATION_LABEL)} --label {shlex.quote(spec.ready_label)}"
        f" --label {shlex.quote(HUMAN_REVIEW_LABEL)}"
    )
    prompt = f"""Repository: {spec.project_id}. Every run:
0. Run `sdlcctl --version`. It must print `commit {spec.platform_ref}` -- the platform commit
   this document was generated for, installed by the environment's setup script (see
   "Environment setup script" in docs/forge/cloud-implementer.md). If `sdlcctl` is missing or
   names any other commit, STOP: claim nothing, touch no issue, and report that the routine
   environment's setup script is missing or stale (Codex 4242373521).
1. List open issues that carry ALL of: {_md_code(spec.ready_label)}, {_md_code(HUMAN_REVIEW_LABEL)},
   {_md_code(IMPLEMENTATION_LABEL)}
   and do NOT carry {_md_code(BLOCKED_LABEL)}, and have no open PR referencing them:
   `gh issue list --repo {spec.project_id} --state open --limit {ROUTINE_CANDIDATE_LIMIT}
    --search {search}`.
   This is the candidate list, oldest first; if it is empty, stop. Work through it IN ORDER,
   starting with the oldest: whenever a step below says "take the next issue", move on to the
   next candidate of THIS list (do not list again), and stop only when one issue has been
   claimed and handled or the list is exhausted -- a skipped issue (not authorized,
   unverifiable, or leased by another agent) never ends the run while candidates remain.
1a. Authorize the approval before claiming (labels alone are no authority: Triage users can
   apply them):
   `sdlcctl authorize-labeler --project {spec.project_id} --issue <n>
    --label {shlex.quote(IMPLEMENTATION_LABEL)} --label {shlex.quote(spec.ready_label)}
    --label {shlex.quote(HUMAN_REVIEW_LABEL)}`.
   Proceed ONLY on exit 0 (whoever applied EACH of the three labels last has
   write/maintain/admin, and the title and body were last edited BEFORE each label was last
   applied: Codex 4241196649). Exit 3: not authorized -- do not claim; unless you already
   commented so on this issue, comment that a maintainer must review it and re-apply the
   labels, then take the next issue.
   Any other exit: authorization could not be verified -- do not claim, take the next issue.
1b. Claim it before touching code. First name this run's branch ONCE: <branch> is
   `forge/issue-<n>-<stamp>`, where <stamp> is the UTC time now as digits only
   (`date -u +%Y%m%d%H%M%S`). It is new for every run, so a branch an earlier run pushed (say
   its PR could not be opened) never blocks this one (Codex 4241532615); never push to or
   reuse another run's branch. Then claim:
   `sdlcctl claim --project {spec.project_id} --issue <n> --agent cloud-routine
    --session <run id> --branch <branch>`.
   If the claim fails (ANY non-zero exit), first run {release} with this same <run id>,
   best-effort, whatever it exits (Codex 4243193105: a claim that lost the race may have left
   its marker queued when retracting it failed, and a queued marker becomes authoritative once
   the holder releases; release retracts it, and is harmless for a session that never held
   the issue), then skip that issue and take the next one.
2. Check the request deterministically (the same validator and dependency rule as the Actions
   implement path: Codex 4241434511), with the policy as committed on the default branch
   (Codex 4241614590): `git fetch origin {branch}`, then
   `{check_request}`.
   It prints `content digest: sha256:...`; keep that value as <digest> -- it names the request
   exactly as checked here, and step 4d refuses to publish if the title or body changes after
   this (Codex 4241614582). Exit 0: continue. Exit 3: not a valid work request -- retire it
   so it cannot starve the queue (Codex 4241196636): comment the reason it printed,
   add label {_md_code(BLOCKED_LABEL)} (step 1 skips it from then on), then {release}; then go
   back to step 1a with the next candidate of the step 1 list. Exit 4: a dependency issue is
   still open -- {release} (do NOT block it: it becomes ready once its dependencies close)
   and go back to step 1a with the next candidate. Any other exit: it could not be verified
   -- {release} and go back to step 1a with the next candidate.
3. `git fetch origin {branch}` and create branch <branch> from
   `{base}`. Implement the smallest change that satisfies the acceptance
   criteria, tests first, and commit it. Never touch `forbidden_paths` from agentic-sdlc.toml;
   stay within max_changed_files={spec.max_changed_files} and
   max_diff_lines={spec.max_diff_lines}.
3a. Renew the lease (Codex 4241434506: a lease that expires while you work can be taken over):
   {renew}.
   It succeeds ONLY while this run still holds a live lease. On any non-zero exit STOP: do NOT
   push or open a PR -- another agent may own the issue now; run {release} (it refuses if
   another session holds the lease, which is fine) and end the run. It also fails once the
   issue has been closed (Codex 4241614587): every renewal re-reads the issue's state, and
   once the issue has an open pull request (Codex 4243193102: one opened or linked by someone
   else since the claim) -- that PR already represents the work.
4. Run each, in order; all must pass:
   {spec.setup_command}
   {spec.quality_command}
   {spec.test_command}
4a. Renew the lease again, exactly as in step 3a (same command; on failure stop the same way).
4b. Check the diff policy on exactly what will be pushed (Codex 4241434509: the same
   `evaluate_diff` the Actions path applies -- forbidden paths, file, line and byte caps). Commit
   everything first; then `git fetch origin {branch}` and
   `{check_diff}`.
   It diffs the committed HEAD against its merge base with the default branch -- every commit
   of the series, not only the end state (Codex 4241839922) -- under the policy committed on
   the default branch (never your working-tree copy: Codex 4241614590), after proving that
   origin/{spec.default_branch} is the commit GitHub reports as that branch's head (Codex
   4241839926: a local ref can be re-pointed). On any non-zero exit do NOT push or open a PR:
   comment the reasons it printed on the issue, {release}, and stop. Make no commit after this
   check.
4c. Verify the lease: run the step 3a renew command once more (it fails if the issue was closed
   meanwhile). On any non-zero exit do NOT push or open a PR; stop as in step 3a.
4d. The final gate, LAST, immediately before pushing -- ONE command that decides everything
   from one snapshot of the issue (Codex 4242264478: two separate checks let an edit plus a
   label re-apply, or a reopened dependency, land between them and pass both):
   `{final_gate}`.
   It re-checks the request (Codex 4241614596, 4241614582: a dependency reopened, the issue
   closed, the request edited) and re-authorizes the approval labels as step 1a does (Codex
   4240628982, 4241839918: labels removed and re-applied, or applied by someone without write
   access, while you worked), then reads the issue and its history again and requires them
   unchanged. Proceed to step 5 ONLY on exit 0, and run nothing between this check and the
   push. On any other exit do NOT push or open a PR: {release} and stop. Exit 5 means the
   title or body changed since step 2: comment (once) that the request changed while it was
   being implemented and a later run will start over from the new text. Exit 7: comment (once)
   that a maintainer must review it and re-apply the labels. Exit 4: a dependency was reopened.
   Exit 6: the issue is no longer open. Exit 8: the issue kept changing while it was checked.
   Exit 9: a pull request for the issue was opened or linked meanwhile (Codex 4243193102) --
   that PR now represents the work; do not open a second one.
5. Push the checked commit at once -- `git push origin HEAD:refs/heads/<branch>`, never
   forced -- and
   open a DRAFT pull request titled "<issue title> (#<n>)" with a summary
   and test plan; link the issue. Then {release} (the open PR now marks the
   work); on any failure, release it too so another run can retry.
   Never merge, approve, deploy, or edit workflows/policy files.
"""
    return f"""# Cloud implementer routine — {spec.project_id}

Implementation for this repository is done by a **Claude Code cloud routine** (a scheduled cloud
agent on claude.ai), not by a GitHub Actions job, so no self-hosted runner is required for it.
The routine must obey `AGENTS.md`, `CLAUDE.md` and `agentic-sdlc.toml` exactly as an Actions
implementer would -- including the labeler check the Actions path runs: before claiming an
issue it runs `sdlcctl authorize-labeler`, which reads the issue's label events and requires a
definitive write/maintain/admin permission for whoever applied EACH label of the approval set
({_md_code(IMPLEMENTATION_LABEL)}, {_md_code(spec.ready_label)},
{_md_code(HUMAN_REVIEW_LABEL)}) most recently -- so a Triage user cannot complete the set after
a writer approved (a 404 is unauthorized; any other failure is unverifiable and the issue is
skipped) -- and requires the issue's title and body to have been last edited BEFORE each of
those labels was last applied, so an edit after approval is never implemented unapproved (an
edit time that cannot be read is unverifiable). The Actions path's other deterministic gates
are commands too, and the routine is gated on their exit codes: `sdlcctl check-request`
parses the issue with the same work-request validator and policy as `prepare-request` and
requires every dependency issue to be closed;
`sdlcctl check-diff` applies the same diff policy as `inspect-diff` (forbidden paths and the
patch caps) to exactly what is about to be pushed -- every path any commit of the series
touches, not only the end state -- under the policy COMMITTED on the default branch (an
uncommitted, loosened `agentic-sdlc.toml` changes nothing), whose head must be the commit
GitHub reports (a re-pointed local `origin/<branch>` is refused), running a git only root
can replace (never a `git` from a writable PATH entry) in an environment of its own;
`sdlcctl renew` -- after implementation, after verification and immediately before pushing --
keeps the lease alive and
fails unless this run still holds it and the issue is still open, so the routine never pushes
over another agent's lease or for a closed issue. The LAST gate, immediately before pushing
and with nothing run between it and the push, is ONE command:
`check-request --expect-digest <digest from step 2> --authorize-labels`. It reads the issue and
its label history once and judges that single snapshot by every rule -- issue open, title and
body unchanged since step 2, a valid request, every dependency closed, and each approval label
last applied by a writer after the last edit -- then reads the issue and its history again and
requires the update time, state, content, labels and events unchanged (a changed title or body
fails at once; any other change is judged afresh, a bounded number of times). So an edit plus
a label re-apply, a reopened dependency or a closed issue at any point before the push stops it:
there is no gap between two separate checks for them to land in.
Keep `lease_ttl_minutes` longer than the longest stage. An issue whose body is not a valid
work request is labelled {_md_code(BLOCKED_LABEL)} and its lease released; step 1 never
selects a blocked issue.
## Environment setup script

A fresh cloud environment has no `sdlcctl`, and every gate below is an `sdlcctl` command. Paste
this into the setup script of the routine's cloud environment (claude.ai: the environment's
settings); it installs the platform at exactly the commit this repository's workflows pin:

{_md_block(cloud_routine_setup_script(spec).splitlines(), "sh")}

`--user` installs `sdlcctl` into `~/.local/bin`, which must be on the routine's PATH. If
`{spec.platform_repository}` is private, the environment needs read access to it. Step 0 of the
prompt checks the install (`sdlcctl --version` must print `commit {spec.platform_ref}`) and
stops the run before anything is claimed when it does not.

**Trust boundary** (Codex 4244027563): the routine's agent runs as the same user that owns this
install -- in Claude Code cloud environments, root -- so it can replace `sdlcctl` (or the git
and gh it calls) after step 0 has checked the version. Installing elsewhere does not change
that: no location is out of reach of an agent running as root, and as root ownership cannot
tell a system file from one the session wrote. The `sdlcctl` gates, like the hooks, are a
guardrail against mistakes and casual misuse, not a sandbox. What enforces the workflow is
server-side: the `Protect main` ruleset (no bypass) and its required review (at
`required_approvals = 0` the human-merge boundary is not enforced; `doctor` says so).

## Routine prompt

Suggested routine prompt (create it with the `schedule` skill or the claude.ai routines UI):

{_md_block(prompt.splitlines())}

Once the routine is scheduled AND its environment runs the setup script above, record it:
`sdlcctl confirm-cloud-routine` (sets `[automation] {CLOUD_ROUTINE_CONFIRMED} = true` in
agentic-sdlc.toml). `sdlcctl doctor` cannot see claude.ai routines or their environments and
reports NOT READY until then; `onboard --force` drops the confirmation (it may move the pin), so
re-check the routine and its setup script against this document and confirm again.

Required GitHub state (created by `sdlcctl onboard --apply`): the three labels, the
`{RULESET_NAME}` ruleset requiring the `{REQUIRED_CHECK}` check and review-thread resolution.
CI (`.github/workflows/ci.yml`) provides the `{REQUIRED_CHECK}` check and runs on
`{", ".join(spec.ci_labels)}`.
"""


# ---- The cloud routine itself (Codex 4239868034). Onboarding writes only a document with a
# suggested prompt; the scheduled routine lives on claude.ai, where nothing here can verify that
# it exists. Until the owner records that it does -- `sdlcctl confirm-cloud-routine`, which sets
# `[automation] cloud_routine_confirmed = true` -- doctor reports it as a manual TODO and the
# repository is NOT READY: approved issues would otherwise wait forever for a worker that was
# never created. `onboard --force` rewrites the policy (and may change the routine prompt), so it
# drops the confirmation: re-check the routine against the new document and confirm again.
CLOUD_ROUTINE_CHECK = "cloud implementer routine is scheduled"
CLOUD_ROUTINE_CONFIRMED = "cloud_routine_confirmed"


# ---- Planner identity (Codex 4240306502). Every generated plan caller invokes the platform's
# reusable-plan.yml, whose plan job runs Claude Code (anthropics/claude-code-action), so the
# policy records the planner as "claude" -- the `[agents]` spelling used for Claude everywhere
# (`implementer = "claude"`). Nothing in the platform enforces planner != implementer; a Claude
# implementer therefore also reads "claude", which is simply accurate.
GENERATED_PLANNER = "claude"
PLANNER_CHECK = "policy planner matches the plan workflow"
#: The `[agents]` identity behind each AI action a plan workflow can run (owner/repo of `uses:`).
PLAN_AGENT_ACTIONS = {
    "anthropics/claude-code-action": "claude",
    "openai/codex-action": "codex",
}


def plan_workflow_agents(doc: object) -> set[str]:
    """The `[agents]` identities of the AI actions `doc` (a parsed workflow) runs in any step.
    Non-mapping jobs and steps are skipped: they run nothing."""
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    found: set[str] = set()
    for job in jobs.values() if isinstance(jobs, dict) else ():
        steps = job.get("steps") if isinstance(job, dict) else None
        for step in steps if isinstance(steps, list) else ():
            uses = step.get("uses") if isinstance(step, dict) else None
            action = str(uses or "").strip().split("@", 1)[0].lower()
            if action in PLAN_AGENT_ACTIONS:
                found.add(PLAN_AGENT_ACTIONS[action])
    return found


def planner_check(policy_doc: object, plan_doc: object = None) -> Check:
    """`[agents] planner` must name the agent the plan workflow actually runs: the agent found
    in `plan_doc` (reusable-plan.yml as fetched at the pin) when given, else the one the
    generated callers invoke (GENERATED_PLANNER)."""
    agents = policy_doc.get("agents") if isinstance(policy_doc, dict) else None
    planner = agents.get("planner") if isinstance(agents, dict) else None
    if plan_doc is None:
        runs, where = {GENERATED_PLANNER}, "the generated plan callers' reusable-plan.yml"
    else:
        runs, where = plan_workflow_agents(plan_doc), "reusable-plan.yml at the pinned ref"
    if not runs:
        return Check(
            PLANNER_CHECK,
            False,
            f"{where} runs no known agent action ({', '.join(sorted(PLAN_AGENT_ACTIONS))}), so "
            f"[agents] planner = {planner!r} cannot be verified",
        )
    if planner in runs:
        return Check(PLANNER_CHECK, True, f"[agents] planner = {planner!r} ({where})")
    return Check(
        PLANNER_CHECK,
        False,
        f"[agents] planner = {planner!r}, but {where} runs "
        + " and ".join(repr(a) for a in sorted(runs))
        + f" → set planner = {sorted(runs)[0]!r} in agentic-sdlc.toml (or re-run "
        "`sdlcctl onboard --force`)",
    )


def cloud_routine_check(policy_doc: object) -> Check:
    automation = policy_doc.get("automation") if isinstance(policy_doc, dict) else None
    confirmed = automation.get(CLOUD_ROUTINE_CONFIRMED) if isinstance(automation, dict) else None
    if confirmed is True:
        return Check(
            CLOUD_ROUTINE_CHECK,
            True,
            f"owner-confirmed ([automation] {CLOUD_ROUTINE_CONFIRMED} = true); doctor cannot see "
            "claude.ai routines, so keep it and its environment setup script matching "
            "docs/forge/cloud-implementer.md",
        )
    return Check(
        CLOUD_ROUTINE_CHECK,
        False,
        "no scheduled implementer routine can be verified from here, and none is confirmed → "
        "create the routine on claude.ai from the prompt in docs/forge/cloud-implementer.md, "
        "give its environment the setup script there (it installs the pinned `sdlcctl`), "
        "then record it with `sdlcctl confirm-cloud-routine` (sets [automation] "
        f"{CLOUD_ROUTINE_CONFIRMED} = true in agentic-sdlc.toml)",
        manual=True,
    )


def confirm_cloud_routine(root: str | Path) -> Path:
    """Record the owner's confirmation that the cloud implementer routine is scheduled:
    `[automation] cloud_routine_confirmed = true` in `root`/agentic-sdlc.toml (only for a
    cloud-routine policy). The rest of the file is kept byte for byte.

    Codex 4240628986: the policy must be a regular file inside `root` -- a symlink anywhere on the
    way (dangling or not), a non-regular file or a path that resolves outside the repository is
    refused, exactly as `write_onboarding` refuses its managed paths -- and it is read without
    following a link and replaced atomically (`_replace_file`), never written through one."""
    base = Path(root).resolve()
    path = base / "agentic-sdlc.toml"
    state = _managed_path_state(base, "agentic-sdlc.toml")
    if state != "file":
        raise OnboardError(f"refusing to confirm through {path}: agentic-sdlc.toml {state}")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise OnboardError(f"refusing to confirm through {path}: not a regular file")
            text = handle.read().decode("utf-8")
        doc = tomllib.loads(text)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise OnboardError(f"cannot read {path}: {exc}") from exc
    agents = doc.get("agents") if isinstance(doc.get("agents"), dict) else {}
    if agents.get("implementation_mode") != "cloud-routine":
        raise OnboardError(
            f"{path}: [agents] implementation_mode is not 'cloud-routine'; nothing to confirm"
        )
    if not isinstance(doc.get("automation"), dict):
        raise OnboardError(f"{path}: no [automation] table to record the confirmation in")
    lines = text.splitlines(keepends=True)
    header = next((i for i, line in enumerate(lines) if line.strip() == "[automation]"), None)
    if header is None:
        raise OnboardError(f"{path}: [automation] is not a plain table header; edit it by hand")
    end = next(
        (i for i in range(header + 1, len(lines)) if lines[i].lstrip().startswith("[")),
        len(lines),
    )
    setting = f"{CLOUD_ROUTINE_CONFIRMED} = true\n"
    key = re.compile(rf"^\s*{CLOUD_ROUTINE_CONFIRMED}\s*=")
    existing = [i for i in range(header + 1, end) if key.match(lines[i])]
    if existing:
        lines[existing[0]] = setting
        for i in reversed(existing[1:]):
            del lines[i]
    else:
        lines.insert(header + 1, setting)
    updated = "".join(lines)
    try:
        written = tomllib.loads(updated)
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover - defensive
        raise OnboardError(f"{path}: could not record the confirmation ({exc})") from exc
    if written.get("automation", {}).get(CLOUD_ROUTINE_CONFIRMED) is not True:
        raise OnboardError(f"{path}: could not record the confirmation")  # pragma: no cover
    _replace_file(path, updated)
    return path


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
    "PYTHON_VERSION",
    "AUTO_PLAN_IF",
    "AUTO_IMPLEMENT_IF",
    "APPROVAL_LABEL_VALUE",
    "REQUIRED_LABELS_VALUE",
    "AUTO_CONCURRENCY_GROUP",
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
            # Every job that can reach the configured runner waits on the hosted `authorize`,
            # `notify_failure` included: it runs on RUNS_ON, which an unverified labeler must
            # never reach. A FAILED `authorize` is reported instead by the hosted
            # `authorization_failed` job (Codex 4239667541), which needs no authorization.
            gate = "    if: needs.authorize.outputs.authorized == 'true'\n"
            for old, new in (
                ("  preflight:\n", "  preflight:\n    needs: authorize\n" + gate),
                ("    needs: preflight\n", "    needs: [authorize, preflight]\n" + gate),
                (
                    "    needs: [preflight, implement]\n",
                    "    needs: [authorize, preflight, implement]\n",
                ),
                (
                    "      ${{ always() &&\n",
                    "      ${{ always() && needs.authorize.outputs.authorized == 'true' &&\n",
                ),
                # Codex 4239965189: the reusable workflow re-checks `trigger_actor`; that is the
                # actor who applied the approval label last (`authorize` verified it), not the
                # sender of whichever label event happened to complete the set.
                (
                    "      trigger_actor: ${{ github.actor }}\n",
                    "      trigger_actor: ${{ needs.authorize.outputs.approver }}\n",
                ),
            ):
                if jobs.count(old) != 1:
                    raise OnboardError(f"implement jobs template: expected one {old.strip()!r}")
                jobs = jobs.replace(old, new)
        text = text.replace("IMPLEMENT_JOBS\n", jobs)
    if "LABELER_AUTHORIZATION" in text:
        work = "Planning" if name == "agent-auto-plan.yml" else "Implementation"
        authorization = _resource("github/forge/_authorize-labeler.yml").replace(
            "AUTHORIZED_WORK", work
        )
        # Codex 4239667541: a hosted reporter for a FAILED authorization job, right after it.
        report = (
            _resource("github/forge/_authorization-failure.yml")
            .replace("AUTHORIZE_JOB", _LABEL_CONDITION_JOBS[name])
            .replace("AUTHORIZED_WORK", work)
        )
        text = text.replace("LABELER_AUTHORIZATION\n", authorization + report)
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
        # A GitHub branch FILTER, not a name: `+`, `!`, `]` would be glob syntax there.
        "DEFAULT_BRANCH": _yaml_str(_branch_filter_literal(spec.default_branch)),
        "SETUP_COMMAND": _yaml_run(spec.setup_command),
        "QUALITY_COMMAND": _yaml_run(spec.quality_command),
        "TEST_COMMAND": _yaml_run(spec.test_command),
        "PYTHON_VERSION": _yaml_str(spec.python_version),
        "AUTO_PLAN_IF": _yaml_folded(auto_plan_condition(*labels), " " * 6),
        "AUTO_IMPLEMENT_IF": _yaml_folded(auto_implement_condition(*labels), " " * 6),
        # Codex 4240628982: removing an approval label cancels a queued or running run.
        "AUTO_CONCURRENCY_GROUP": _yaml_folded(
            auto_concurrency_group(
                "plan",
                [spec.ready_label, HUMAN_REVIEW_LABEL],
                [spec.ready_label, HUMAN_REVIEW_LABEL],
            )
            if name == "agent-auto-plan.yml"
            else auto_concurrency_group(
                "implement",
                [IMPLEMENTATION_LABEL, spec.ready_label, HUMAN_REVIEW_LABEL],
                [IMPLEMENTATION_LABEL, spec.ready_label, HUMAN_REVIEW_LABEL],
            ),
            " " * 4,
        ),
        # The label whose most recent applier `_authorize-labeler.yml` authorizes: the ready
        # label for planning (its trigger), implementation approval for implementing.
        "APPROVAL_LABEL_VALUE": _yaml_str(
            spec.ready_label if name == "agent-auto-plan.yml" else IMPLEMENTATION_LABEL
        ),
        # Codex 4240421144: every label the run's condition requires present, the approval label
        # first; whoever applied EACH most recently must hold write authority (`sdlcctl
        # authorize-labeler` applies the same rule for a cloud routine).
        "REQUIRED_LABELS_VALUE": _yaml_str(
            json.dumps(
                [spec.ready_label, HUMAN_REVIEW_LABEL]
                if name == "agent-auto-plan.yml"
                else [IMPLEMENTATION_LABEL, spec.ready_label, HUMAN_REVIEW_LABEL],
                separators=(",", ":"),
            )
        ),
    }
    if set(replacements) != set(WORKFLOW_PLACEHOLDERS):
        raise OnboardError("workflow placeholders and their renderings disagree")
    # At this point `text` is template material only (the structural inserts are templates
    # too): any structural marker still in it, or any placeholder without a value, is a
    # template bug caught BEFORE user data goes in.
    return _substitute_template(f"github/forge/{name}", text, replacements)


#: Every placeholder word any template carries: the workflow values plus the structural
#: markers `_render_workflow` expands from other templates.
TEMPLATE_PLACEHOLDERS = (
    *WORKFLOW_PLACEHOLDERS,
    "IMPLEMENT_JOBS",
    "PREFLIGHT_EXTRA",
    "LABELER_AUTHORIZATION",
    "AUTHORIZED_WORK",
    "AUTHORIZE_JOB",
)


def _substitute_template(
    name: str,
    template: str,
    replacements: Mapping[str, str],
    pattern: re.Pattern[str] | None = None,
    required: Sequence[str] = (),
) -> str:
    """Fill `template` in ONE regex pass and prove it complete from the SOURCE, never the output.

    Every placeholder `pattern` (default: the `TEMPLATE_PLACEHOLDERS` words) finds in the
    template must have a value, and every `required` one must occur; otherwise the template is
    broken and nothing is rendered. A substituted value is never re-scanned, so a user value
    that merely contains a placeholder word (`echo TEST_COMMAND`) is rendered as given."""
    if pattern is None:
        pattern = re.compile(
            r"\b(" + "|".join(sorted(TEMPLATE_PLACEHOLDERS, key=len, reverse=True)) + r")\b"
        )
    found = {m.group(1) for m in pattern.finditer(template)}
    missing = sorted(found - set(replacements))
    if missing:
        raise OnboardError(f"unrendered placeholder {missing[0]} in template {name}")
    absent = sorted(set(required) - found)
    if absent:
        raise OnboardError(f"template {name} lacks its placeholder {absent[0]}")
    return pattern.sub(lambda m: replacements[m.group(1)], template)


def render_work_request(spec: OnboardSpec) -> str:
    """The generic work-request template, labelled with this profile's ready label."""
    # No placeholder words belong in this template: prove it on the source, not the output.
    text = _substitute_template("work-request.md", _resource("work-request.md"), {})
    rendered, count = re.subn(
        r"^labels: .*$",
        # A flow sequence of quoted scalars: a label's `:`, `#`, quotes or leading `-` stay text.
        lambda _: f"labels: {_yaml_labels((spec.ready_label, HUMAN_REVIEW_LABEL))}",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise OnboardError("work-request template has no labels line")
    return rendered


def render_hooks(spec: OnboardSpec) -> dict[str, str]:
    """Claude Code hooks: fetch-and-report at session start; block commits without a lease."""
    # The placeholders sit in Python string literals ("PROJECT_ID"): substitute the whole
    # literal with a JSON string, which is a valid Python string literal. One pass, so a
    # project id cannot be mistaken for the branch placeholder after it is substituted.
    literal = re.compile(r'"(PROJECT_ID|DEFAULT_BRANCH_NAME|PUBLISHER_BOT_LOGIN|TRUST_MODE)"')
    values = {
        "PROJECT_ID": json.dumps(spec.project_id),
        "DEFAULT_BRANCH_NAME": json.dumps(spec.default_branch),
    }
    guard_values = {
        "PROJECT_ID": values["PROJECT_ID"],
        "PUBLISHER_BOT_LOGIN": json.dumps(spec.publisher_bot_login),
        "TRUST_MODE": json.dumps(spec.trust_mode),
    }
    guard = _substitute_template(
        "hooks/forge_commit_guard.py",
        _resource("hooks/forge_commit_guard.py"),
        guard_values,
        literal,
        required=tuple(guard_values),
    )
    start = _substitute_template(
        "hooks/forge_session_start.py",
        _resource("hooks/forge_session_start.py"),
        values,
        literal,
        required=tuple(values),
    )
    # Each handler pins its script's SHA-256 and runs the vetted interpreter (Codex 4242753911,
    # 4242753904; see `forge_hook_entry`).
    settings = {
        "hooks": {
            "SessionStart": [
                {
                    "hooks": [
                        forge_hook_entry(
                            "SessionStart", hook_script_digest(start), spec.hook_python
                        )
                    ]
                }
            ],
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        forge_hook_entry("PreToolUse", hook_script_digest(guard), spec.hook_python)
                    ],
                }
            ],
        }
    }
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
    # Completeness is proven per SOURCE template as each file renders (`_substitute_template`);
    # the rendered files are never rescanned, since they carry user values verbatim.
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


def _managed_path_state(base: Path, rel: str) -> str:
    """`"absent"` or `"file"` for a managed path that is safe to write, else why it is not.

    Never follow a link: every component from `base` down is `lstat`ed, and a symlink anywhere
    (dangling or not) is refused, as is a parent that is not a directory, a destination that
    exists but is not a regular file, or a path that leaves the repository. A write through a
    link could create or overwrite a file outside the checkout."""
    parts = Path(rel).parts
    if not parts or Path(rel).is_absolute() or any(p in ("", ".", "..") for p in parts):
        return "is not a plain repository-relative path"
    current = base
    for index, part in enumerate(parts):
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            return "absent"
        if stat.S_ISLNK(mode):
            return f"{current.relative_to(base)} is a symlink"
        last = index == len(parts) - 1
        if not last and not stat.S_ISDIR(mode):
            return f"{current.relative_to(base)} is not a directory"
        if last and not stat.S_ISREG(mode):
            return "exists but is not a regular file"
    if not current.resolve().is_relative_to(base):
        return "resolves outside the repository"
    return "file"


def _replace_file(path: Path, content: str) -> None:
    """Write `content` to `path` atomically: a fresh temporary file in the same directory
    (O_EXCL, never through a link), then `os.replace`, which swaps the directory entry itself. An
    existing file's permission bits carry over."""
    try:
        mode = stat.S_IMODE(os.lstat(path).st_mode)
    except FileNotFoundError:
        mode = None
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o666)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


SETTINGS_FILE = ".claude/settings.json"


def policy_bot_login(policy_doc: object) -> str:
    """The policy's `[automation] publisher_bot_login` ('' when absent or not a string)."""
    doc = policy_doc if isinstance(policy_doc, dict) else {}
    automation = doc.get("automation")
    value = automation.get("publisher_bot_login") if isinstance(automation, dict) else None
    return value if isinstance(value, str) else ""


def installed_bot_login(base: Path) -> str:
    """The `publisher_bot_login` of the policy installed under `base` ('' when none can be
    read): a forced re-onboard without `--publisher-bot-login` keeps it."""
    try:
        policy_doc = tomllib.loads((base / "agentic-sdlc.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return ""
    return policy_bot_login(policy_doc)


def _installed_policy_doc(base: Path) -> dict:
    try:
        doc = tomllib.loads((base / "agentic-sdlc.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return {}
    return doc if isinstance(doc, dict) else {}


def policy_trust_mode(policy_doc: object) -> str:
    """`[automation] trust_mode` of a parsed policy: "local" only when it says exactly that;
    anything else (absent, unreadable, invalid) is "strict"."""
    automation = policy_doc.get("automation") if isinstance(policy_doc, dict) else None
    value = automation.get("trust_mode") if isinstance(automation, dict) else None
    return LOCAL if value == LOCAL else STRICT


def installed_trust_mode(base: Path) -> str | None:
    """The trust mode the policy installed under `base` names (None: none can be read or it
    names none): a forced re-onboard without `--trust-mode` keeps it."""
    automation = _installed_policy_doc(base).get("automation")
    value = automation.get("trust_mode") if isinstance(automation, dict) else None
    return value if value in TRUST_MODES else None


def policy_ephemeral_runners(policy_doc: object) -> bool:
    automation = policy_doc.get("automation") if isinstance(policy_doc, dict) else None
    return isinstance(automation, dict) and automation.get("ephemeral_runners") is True


def policy_required_approvals(policy_doc: object) -> int:
    """`[policy] required_approvals` (0 when absent or not a valid count)."""
    table = policy_doc.get("policy") if isinstance(policy_doc, dict) else None
    value = table.get("required_approvals") if isinstance(table, dict) else None
    return value if not _approvals_problem(value) else 0


def installed_required_approvals(base: Path) -> int | None:
    """The `required_approvals` of the installed policy, None when it names none."""
    table = _installed_policy_doc(base).get("policy")
    value = table.get("required_approvals") if isinstance(table, dict) else None
    return value if not _approvals_problem(value) else None


def other_writers(
    project_id: str, gh: GhRunner, *, me: str | None = None
) -> list[str] | Unreadable:
    """Collaborators with write, maintain or admin access OTHER than the repository's owner and
    the authenticated user `me` (Codex 4243193100): who could approve a pull request this
    identity opens. `repos/{p}/collaborators?permission=push` lists users with at least write
    access (direct, outside and organization members), every page; bots are not reviewers."""
    rows = _paged_items(gh, f"repos/{project_id}/collaborators?permission=push&per_page=100")
    if isinstance(rows, Unreadable):
        return rows
    owner = project_id.split("/")[0].casefold()
    skip = {owner, (me or "").casefold()}
    logins = []
    for row in rows:
        login = row.get("login") if isinstance(row, dict) else None
        if not isinstance(login, str) or not login or row.get("type") == "Bot":
            continue
        if login.casefold() not in skip:
            logins.append(login)
    return sorted(set(logins), key=str.casefold)


def authenticated_login(gh: GhRunner) -> str | None:
    """The login `gh` is authenticated as (`GET user`), None when unreadable."""
    answer = _safe_json(gh, ["api", "user"])
    login = answer.get("login") if isinstance(answer, dict) else None
    return login if isinstance(login, str) and login else None


def default_required_approvals(project_id: str, gh: GhRunner, *, cloud: bool) -> tuple[int, str]:
    """(count, why) for `--required-approvals` left unset at `onboard --apply`.

    Actions implementer modes (`cloud=False`: route, claude, codex): 1, read nothing -- the
    pull request is opened by the Publisher App (`publish_draft` runs with its token), so the
    repository owner is NOT its author and can approve it (Codex 4243386518).

    Cloud-routine mode (`cloud=True`): the routine opens the PR as the owner, who cannot approve
    their own pull request. 1 when another collaborator with write access can approve, else 0
    (1 would leave every PR unmergeable). Unreadable: 0, said so."""
    if not cloud:
        return 1, (
            "the Publisher App authors the implementation pull requests, so the owner can "
            "approve them"
        )
    found = other_writers(project_id, gh, me=authenticated_login(gh))
    if isinstance(found, Unreadable):
        return 0, f"the collaborators of {project_id} could not be read ({found.why})"
    if found:
        return 1, f"{len(found)} other collaborator(s) with write access can approve"
    return 0, (
        f"no collaborator of {project_id} but the owner has write access, and in cloud-routine "
        "mode the owner authors the pull requests"
    )


HUMAN_MERGE_NOT_ENFORCED = (
    "the ruleset requires no approving review, so GitHub does not enforce the human-merge "
    "boundary: in cloud-routine mode the routine acts with the owner's identity and could mark "
    "its draft ready and merge it once CI passes. Add a second collaborator with write access "
    "(or a separate reviewer identity) and re-run `sdlcctl onboard --force --apply "
    "--required-approvals 1`"
)


def _previous_guides(base: Path, spec: OnboardSpec) -> list[dict[str, str]]:
    """The guide texts the INSTALLED policy renders (what doctor's containment check compares,
    `rendered_managed_files`), under the policy's own project id and this one: the managed
    fragment a forced re-onboard replaces. Empty when the installed policy cannot be read."""
    try:
        policy_doc = tomllib.loads((base / "agentic-sdlc.toml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return []
    project = policy_doc.get("project") if isinstance(policy_doc.get("project"), dict) else {}
    ids = dict.fromkeys(i for i in (project.get("id"), spec.project_id) if isinstance(i, str))
    renders = []
    for project_id in ids:
        try:
            rendered, _ = rendered_managed_files(
                policy_doc, project_id, spec.platform_repository, cloud=False
            )
        except (OnboardError, TypeError, ValueError):
            continue
        renders.append(rendered)
    return renders


def _merge_guide(rel: str, have: str, want: str, previous: list[dict[str, str]]) -> str:
    """A forced re-onboard of a guide (AGENTS.md, CLAUDE.md): doctor accepts repository notes
    AROUND the generated text, so only that text is replaced. Already containing `want`: kept
    as is. Containing a previous render (`_previous_guides`): that fragment is replaced in place.
    No Forge guide in it at all (a pre-existing file): `want` is appended after the owner's
    content. A Forge guide that was edited in place cannot be told apart from the notes around
    it, so that refuses rather than delete anything (fail closed)."""
    if want in have:
        return have
    for rendered in previous:
        old = rendered.get(rel)
        if old and old in have:
            return have.replace(old, want, 1)
    headings = {want.splitlines()[0]}
    headings.update(r[rel].splitlines()[0] for r in previous if r.get(rel))
    if any(line in headings for line in have.splitlines()):
        raise OnboardError(
            f"{rel} holds an edited Forge guide that cannot be separated from the notes around "
            "it; restore its generated text (or move your notes aside and delete the file), then "
            "re-run `sdlcctl onboard --force`"
        )
    if not have.strip():
        return want
    return have.rstrip("\n") + "\n\n" + want


def _merge_settings(have: str, want: str) -> str:
    """A forced re-onboard of `.claude/settings.json`: the managed Forge hook groups are merged
    into the existing settings, keeping every unrelated key and hook (`hook_problems` accepts
    them). Every existing handler running a Forge script (`_hook_runs`) -- a stale, duplicated or
    deviating copy -- is dropped, a group left empty by that is dropped, and the managed groups
    are appended, so a re-run yields the same file. `disableAllHooks: true` would switch the
    managed hooks off, so it goes. Unparseable or misshapen settings are refused, never
    replaced."""
    try:
        settings = json.loads(have)
    except json.JSONDecodeError as exc:
        raise OnboardError(
            f"{SETTINGS_FILE} is not valid JSON ({str(exc)[:80]}); fix or remove it, then "
            "re-run `sdlcctl onboard --force` (it is merged, never overwritten)"
        ) from exc
    if not isinstance(settings, dict):
        raise OnboardError(f"{SETTINGS_FILE} is not a JSON object; fix or remove it")
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        raise OnboardError(f"{SETTINGS_FILE} `hooks` is not an object; fix or remove it")
    managed = json.loads(want)["hooks"]
    scripts = tuple(HOOK_SCRIPTS.values())
    merged: dict = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            if event in managed:
                raise OnboardError(
                    f"{SETTINGS_FILE} `hooks.{event}` is not a list; fix or remove it"
                )
            merged[event] = groups
            continue
        kept = []
        for group in groups:
            entries = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(entries, list):
                kept.append(group)
                continue
            rest = [e for e in entries if not any(_hook_runs(e, s) for s in scripts)]
            if len(rest) == len(entries):
                kept.append(group)
            elif rest:
                kept.append({**group, "hooks": rest})
        merged[event] = kept
    for event, groups in managed.items():
        merged[event] = [*merged.get(event, []), *groups]
    settings["hooks"] = merged
    if settings.get("disableAllHooks") is True:
        del settings["disableAllHooks"]
    return json.dumps(settings, indent=2) + "\n"


def write_onboarding(
    root: str | Path, spec: OnboardSpec, *, force: bool = False
) -> tuple[Path, ...]:
    base = Path(root).resolve()
    if not base.is_dir() or not (base / ".git").exists():
        raise OnboardError("destination must be an existing Git repository")
    # The hooks run this interpreter by absolute path: refuse one only root cannot replace
    # before anything is written (Codex 4242753911).
    problem = hook_python_problem(spec.hook_python, spec.trust_mode)
    if problem:
        raise OnboardError(f"the hook interpreter {problem}; pass --hook-python with a trusted one")
    files = render_onboarding(spec)
    owned = [rel for rel in MODE_OWNED_FILES if rel not in files] if force else []
    states = {rel: _managed_path_state(base, rel) for rel in (*files, *owned)}
    unsafe = sorted(
        f"{rel} ({why})" for rel, why in states.items() if why not in ("absent", "file")
    )
    if unsafe:
        raise OnboardError("refusing to write unsafe managed paths: " + "; ".join(unsafe))
    existing = sorted(rel for rel in files if states[rel] == "file")
    if existing and not force:
        raise OnboardError(
            "refusing to overwrite existing files (use --force): " + ", ".join(existing)
        )
    # Tunable files keep what the repository added around the managed part (Codex 4239780830):
    # every merge is computed before anything is written, so a refusal writes nothing.
    previous = _previous_guides(base, spec) if existing else {}
    for rel in existing:
        path = base / rel
        if rel in CONTAINED_MANAGED_FILES:
            files[rel] = _merge_guide(rel, path.read_text(encoding="utf-8"), files[rel], previous)
        elif rel == SETTINGS_FILE:
            files[rel] = _merge_settings(path.read_text(encoding="utf-8"), files[rel])
    written = []
    for rel, content in files.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        _replace_file(path, content)
        written.append(path)
    for rel in owned:
        if states[rel] == "file":
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
                    # `[policy] required_approvals` (Codex 4243193100): 0 leaves the merge to
                    # whoever can merge -- in cloud-routine mode, the agent acting as the owner.
                    "required_approving_review_count": spec.required_approvals,
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
                    # Bound to GitHub Actions: an unbound context accepts a `test` status from ANY
                    # integration or credential with commit-status access, which could then
                    # satisfy the gate without running ci.yml's policy gates.
                    "required_status_checks": [
                        {"context": REQUIRED_CHECK, "integration_id": GITHUB_ACTIONS_INTEGRATION_ID}
                    ],
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
    rules = {
        r.get("type"): r.get("parameters") or {}
        for r in actual.get("rules") or []
        if isinstance(r, dict)
    }
    # Every parameter Forge's payload sets, iterated rather than hand-listed, so no field can
    # drift unchecked: a boolean Forge sets true must be true, a count at least Forge's, a list
    # a superset of Forge's (required status checks by context, bound to GitHub Actions).
    for rule in want["rules"]:
        kind = rule["type"]
        if kind not in rules:
            problems.append(f"missing rule {kind}")
            continue
        have = rules[kind] if isinstance(rules[kind], dict) else {}
        for key, wanted in (rule.get("parameters") or {}).items():
            problems += _parameter_shortfall(kind, key, wanted, have.get(key))
    return problems


#: The messages doctor has always printed for the best-known parameters.
_PARAMETER_MESSAGES = {
    "required_review_thread_resolution": "review-thread resolution not required",
    "strict_required_status_checks_policy": (
        "status checks are not strict (branch must be up to date)"
    ),
    "dismiss_stale_reviews_on_push": (
        "stale approvals are not dismissed on push (an approval survives new commits)"
    ),
}


def _parameter_shortfall(kind: str, key: str, wanted: object, have: object) -> list[str]:
    """Why the existing value `have` of rule `kind`'s parameter `key` is weaker than Forge's
    `wanted` (empty = it is at least as strict)."""
    if isinstance(wanted, bool):
        if wanted and have is not True:
            return [_PARAMETER_MESSAGES.get(key, f"{kind} {key} is {have!r}, must be true")]
        return []
    if isinstance(wanted, int):
        if isinstance(have, bool) or not isinstance(have, int) or have < wanted:
            return [f"{kind} {key} is {have!r}, must be at least {wanted}"]
        return []
    if key == "required_status_checks":
        checks = [c for c in have if isinstance(c, dict)] if isinstance(have, list) else []
        problems = []
        for want_check in wanted if isinstance(wanted, list) else []:
            context = want_check.get("context")
            matching = [c for c in checks if c.get("context") == context]
            if not matching:
                problems.append(f"status check '{context}' not required")
                continue
            foreign = sorted(
                {str(c.get("integration_id")) for c in matching if _foreign_integration(c)}
            )
            if foreign:
                problems.append(
                    f"status check '{context}' only accepts integration "
                    f"{', '.join(foreign)}, not GitHub Actions ({GITHUB_ACTIONS_INTEGRATION_ID}): "
                    "ci.yml's job can never satisfy it"
                )
            elif "integration_id" in want_check and any(
                c.get("integration_id") is None for c in matching
            ):
                problems.append(_unbound_check_problem(context))
        return problems
    if isinstance(wanted, list):
        listed = have if isinstance(have, list) else []
        lacking = [item for item in wanted if item not in listed]
        return [f"{kind} {key} lacks {lacking!r}"] if lacking else []
    if isinstance(wanted, dict):
        nested = have if isinstance(have, dict) else {}
        return [
            problem
            for sub, value in wanted.items()
            for problem in _parameter_shortfall(kind, f"{key}.{sub}", value, nested.get(sub))
        ]
    return [] if have == wanted else [f"{kind} {key} is {have!r}, must be {wanted!r}"]


def _unbound_check_problem(context: str) -> str:
    return (
        f"status check '{context}' is not bound to GitHub Actions "
        f"(integration {GITHUB_ACTIONS_INTEGRATION_ID}): any app or credential with "
        "commit-status access can report it and satisfy the gate without ci.yml running"
    )


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
    and the required status checks are the union (an existing check keeps its integration,
    except Forge's own `test`, which is always bound to GitHub Actions)."""
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
            # Forge's own `test` context is reported by GitHub Actions and bound to it: an
            # unbound requirement (any integration may report it) or a binding to another app
            # (it could never be satisfied) is rebound to GitHub Actions, and duplicates fold.
            kept: list[dict] = []
            for check in checks:
                if check.get("context") == REQUIRED_CHECK:
                    check = {**check, "integration_id": GITHUB_ACTIONS_INTEGRATION_ID}
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
    # Codex 4242545209: a name GitHub refuses would fail at `gh variable set`, AFTER the labels
    # and ruleset below were already changed. Refuse it before the first remote call.
    problem = variable_names_problem(variables or {})
    if problem:
        raise OnboardError(f"{problem}; nothing was changed")
    # Codex 4240170627: an archived or disabled repository accepts none of the writes below (nor
    # any Forge run's): refuse before the first one, and when writability cannot be confirmed.
    require_writable_repository(
        _safe_json(gh, ["api", f"repos/{spec.project_id}"]), spec.project_id
    )
    # Read every inventory a mutation depends on BEFORE the first mutation: an unreadable
    # ruleset list is not "no `Protect main`" (creating one would duplicate it), and an
    # unreadable ruleset is not an empty one (merging into it would drop its rules).
    existing = _repo_rulesets(spec.project_id, gh)
    if isinstance(existing, Unreadable):
        raise OnboardError(
            f"could not read the rulesets of {spec.project_id} ({existing.why}); nothing was "
            "changed → retry --apply"
        )
    current = next((r for r in existing if r.get("name") == RULESET_NAME), None)
    detail: object = None
    if current is not None:
        detail = _safe_json(gh, ["api", f"repos/{spec.project_id}/rulesets/{current.get('id')}"])
        if not isinstance(detail, dict) or not isinstance(detail.get("rules"), list):
            why = detail.why if isinstance(detail, Unreadable) else "not a ruleset object"
            raise OnboardError(
                f"could not read ruleset '{RULESET_NAME}' of {spec.project_id} ({why}); nothing "
                "was changed → retry --apply"
            )
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
    if isinstance(detail, dict):
        assert current is not None
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


_VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def variable_name_problem(name: str) -> str:
    """Why GitHub would refuse `name` as an Actions variable name ('' = it would not): only
    alphanumerics and `_`, not starting with a digit, and no `GITHUB_` prefix (any case)."""
    if not name:
        return "must not be empty"
    if not _VARIABLE_NAME.fullmatch(name):
        return "may contain only letters, digits and '_', and must not start with a digit"
    if name.upper().startswith("GITHUB_"):
        return "must not start with 'GITHUB_' (reserved by GitHub)"
    return ""


def variable_names_problem(names: Iterable[str]) -> str:
    """The first invalid Actions variable name in `names`, as an error message ('' = none)."""
    for name in names:
        problem = variable_name_problem(name)
        if problem:
            return f"repo variable name {name!r} {problem}"
    return ""


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
    #: A passing check the owner must still read: rendered WARN, never makes doctor NOT READY.
    warning: bool = False

    def as_dict(self) -> dict:
        out = {"name": self.name, "ok": self.ok, "detail": self.detail, "manual": self.manual}
        if self.warning:
            out["warning"] = True
        return out


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
            mark = ("WARN" if c.warning else "PASS") if c.ok else ("TODO" if c.manual else "FAIL")
            lines.append(f"[{mark}] {c.name}" + (f" — {c.detail}" if c.detail else ""))
        lines.append("READY" if self.ok else "NOT READY")
        return "\n".join(lines)


def _remote_file_state(gh: GhRunner, args: Sequence[str]) -> str:
    """The remote file's state: "absent" (the contents API answered 404, as `run_gh` parsed the
    response: `gh_http_status`), "present" (it served the path) or "unknown" (any other failure
    or answer: callers that need absence fail closed on it)."""
    try:
        data = json.loads(gh(args) or "null")
    except OnboardError as exc:
        # The status GitHub answered, never a "404" in the echoed request: a repository or
        # branch named `404` would read every 5xx or timeout as absent (Codex 4244027557).
        return "absent" if gh_http_status(exc) == 404 else "unknown"
    except json.JSONDecodeError:
        return "unknown"
    return "present" if isinstance(data, (dict, list)) and data else "unknown"


TRUST_MODE_CHECK = "executable trust mode"
RUNNER_ISOLATION_CHECK = "self-hosted runners are acknowledged ephemeral"
APPROVALS_CHECK = "ruleset requires a human approval"


def trust_mode_check(mode: str) -> Check:
    """Doctor reports the policy's trust mode; local is a warning: strict is what protects agent
    contexts (and agent contexts are strict at run time anyway)."""
    context = agent_context()
    if mode == LOCAL:
        return Check(
            TRUST_MODE_CHECK,
            True,
            '[automation] trust_mode = "local": git, gh and the hook interpreter may be any '
            "the user owns (first on PATH). Strict mode -- only executables root alone can "
            "replace -- is what protects agent contexts; GitHub Actions, Claude Code cloud "
            "sessions and the publication-decision commands (check-diff, check-request, "
            "authorize-labeler, claim, renew, release) use it regardless"
            + (f" (this run is one: {context})" if context else "")
            + ". The pinned hook interpreter must exist wherever the hooks run",
            warning=True,
        )
    return Check(
        TRUST_MODE_CHECK,
        True,
        '[automation] trust_mode = "strict": git, gh and the hook interpreter only root can '
        "replace",
    )


def _installed_runner_labels(base: Path) -> list[tuple[str, tuple[str, ...]]]:
    """(where, labels) for every runner the installed Forge callers target: each reusable call's
    `with.runs_on` (JSON) and every job's `runs-on`. Unparseable values are skipped (the
    runner checks report them)."""
    found: list[tuple[str, tuple[str, ...]]] = []
    for path in sorted((base / ".github/workflows").glob("*.yml")):
        doc = _workflow_doc(path)
        jobs = doc.get("jobs") if isinstance(doc, dict) else None
        for name, job in (jobs or {}).items() if isinstance(jobs, dict) else ():
            if not isinstance(job, dict):
                continue
            values = []
            passed = job.get("with")
            if isinstance(passed, dict) and isinstance(passed.get("runs_on"), str):
                with contextlib.suppress(ValueError):
                    values.append(json.loads(passed["runs_on"]))
            if "runs-on" in job:
                values.append(job["runs-on"])
            for value in values:
                labels = [value] if isinstance(value, str) else value
                if isinstance(labels, list) and all(isinstance(x, str) for x in labels):
                    if any("${{" in x for x in labels):
                        continue
                    found.append((f"{path.name}:{name}", tuple(labels)))
    return found


def runner_isolation_check(base: Path, policy_doc: object) -> Check:
    """Codex 4243193108: a self-hosted label on a Forge job without the owner's acknowledgement
    (`[automation] ephemeral_runners = true`) that the runners are ephemeral or contained."""
    self_hosted = sorted(
        {
            f"{where} ({','.join(labels)})"
            for where, labels in _installed_runner_labels(base)
            if not _github_hosted(set(labels))
        }
    )
    if not self_hosted:
        return Check(RUNNER_ISOLATION_CHECK, True, "every Forge job runs on a GitHub-hosted runner")
    if policy_ephemeral_runners(policy_doc):
        return Check(
            RUNNER_ISOLATION_CHECK,
            True,
            "self-hosted, acknowledged ephemeral/contained ([automation] ephemeral_runners = "
            "true): " + ", ".join(self_hosted),
        )
    return Check(
        RUNNER_ISOLATION_CHECK,
        True,
        "WARNING: persistent self-hosted runners are not acknowledged as ephemeral: "
        + ", ".join(self_hosted)
        + f" -- {SELF_HOSTED_NEEDS_ACK} (re-run `sdlcctl onboard --force "
        f"{EPHEMERAL_RUNNERS_FLAG}` once they are)",
        warning=True,
    )


def approvals_check(count: int, *, cloud: bool, writers: list[str] | Unreadable | None) -> Check:
    """Codex 4243193100: the ruleset's required approvals (the policy's `required_approvals`,
    which the ruleset check holds the ruleset to). 0 in cloud-routine mode is a loud warning: the
    human-merge boundary is not enforced server-side. `writers`: the other collaborators with
    write access (who could approve), when read -- cloud-routine mode only, where the owner
    authors the PR. In the Actions modes the Publisher App authors it, so the owner can approve
    and 0 is always a warning with the fix (Codex 4243386518)."""
    if count >= 1:
        return Check(APPROVALS_CHECK, True, f"[policy] required_approvals = {count}")
    detail = "[policy] required_approvals = 0"
    if cloud:
        if isinstance(writers, list) and writers:
            detail += (
                f"; {', '.join(writers)} could approve -- re-run `sdlcctl onboard --force "
                "--apply --required-approvals 1`"
            )
        return Check(
            APPROVALS_CHECK, True, f"WARNING: {detail}: {HUMAN_MERGE_NOT_ENFORCED}", warning=True
        )
    return Check(
        APPROVALS_CHECK,
        True,
        f"WARNING: {detail}: the Publisher App authors the implementation pull requests, so the "
        "owner (or any collaborator with write access) can approve them -- re-run `sdlcctl "
        "onboard --force --apply --required-approvals 1` to make GitHub require that review "
        "(today merges are gated only by a human pressing merge)",
        warning=True,
    )


PLATFORM_ACCESS_CHECK = "private platform lets this repository call its workflows"
FORK_RUNNER_CHECK = "fork pull requests cannot reach self-hosted runners"


def _visibility(repo_info: object) -> str | None:
    """The visibility GitHub reports (public, private or internal); None if unknown."""
    if not isinstance(repo_info, dict):
        return None
    if isinstance(repo_info.get("visibility"), str):
        return str(repo_info["visibility"]).lower()
    if isinstance(repo_info.get("private"), bool):
        return "private" if repo_info["private"] else "public"
    return None


def platform_access_check(
    gh: GhRunner,
    project_id: str,
    platform_repository: str,
    consumer_info: dict | None = None,
    platform_visibility: str | None = None,
) -> Check:
    """A private (or internal) platform repository's reusable workflows can be called only from
    the repositories its Actions access setting admits: `user` (repositories of the same user),
    `organization` (of the same organization) or `enterprise` (of the same enterprise) -- and
    never from a PUBLIC repository, whatever the setting: public callers can use only public
    reusable workflows. Reading the workflow file proves only that the reviewer can;
    `PLATFORM_READ_TOKEN` is used after a called workflow starts and cannot resolve the caller's
    `uses:`. Unreadable (403/404: it needs admin on the platform) or unverifiable is an unmet
    MANUAL requirement, never READY. `consumer_info` is `GET repos/{project_id}` (read when
    omitted). `platform_visibility` is the platform's own (private or internal; None: unknown):
    an INTERNAL caller can use only internal (or public) reusable workflows, never a private
    platform's, so that pairing fails and an unknown platform visibility leaves it unverified."""
    if consumer_info is None:
        consumer_info = _safe_json(gh, ["api", f"repos/{project_id}"])
    visibility = _visibility(consumer_info)
    if visibility == "public":
        return Check(
            PLATFORM_ACCESS_CHECK,
            False,
            f"{project_id} is public and {platform_repository} is not: GitHub lets a public "
            "repository call only PUBLIC reusable workflows, so every platform call is rejected "
            "before any job starts, whatever the access setting → make the platform public, or "
            f"make {project_id} private",
        )
    if visibility == "internal" and platform_visibility == "private":
        return Check(
            PLATFORM_ACCESS_CHECK,
            False,
            f"{project_id} is internal and {platform_repository} is private: GitHub lets an "
            "internal repository call only INTERNAL or public reusable workflows, so every "
            "platform call is rejected before any job starts, whatever the access setting → "
            f"make the platform internal, or make {project_id} private",
        )
    consumer_owner = project_id.split("/")[0]
    platform_owner = platform_repository.split("/")[0]
    same_owner = consumer_owner.casefold() == platform_owner.casefold()
    wanted = "user or organization" if same_owner else "enterprise"
    fix = (
        f"on {platform_repository}: Settings → Actions → General → Access → 'Accessible from "
        f"repositories owned by / in {platform_owner}' (or: gh api -X PUT "
        f"repos/{platform_repository}/actions/permissions/access -f access_level="
        f"{'organization' if same_owner else 'enterprise'}; use access_level=user for a "
        "user-owned platform)"
    )
    access = _safe_json(gh, ["api", f"repos/{platform_repository}/actions/permissions/access"])
    level = access.get("access_level") if isinstance(access, dict) else None
    if not isinstance(level, str):
        return Check(
            PLATFORM_ACCESS_CHECK,
            False,
            f"could not read the Actions access setting of {platform_repository} (403/404: "
            f"needs admin on it); {project_id} can call its reusable workflows only when it "
            f"is set to {wanted} → confirm {fix}",
            manual=True,
        )
    if same_owner and level in {"user", "organization", "enterprise"}:
        if visibility is None:
            return Check(
                PLATFORM_ACCESS_CHECK,
                False,
                f"access_level {level}, but the visibility of {project_id} could not be read: a "
                "PUBLIC caller cannot use a private platform's reusable workflows → confirm "
                f"{project_id} is private or internal",
                manual=True,
            )
        if visibility == "internal" and platform_visibility != "internal":
            return Check(
                PLATFORM_ACCESS_CHECK,
                False,
                f"access_level {level}, but the visibility of {platform_repository} could not "
                f"be read: an internal caller ({project_id}) can use only an INTERNAL "
                "platform's reusable workflows, never a private one's → confirm "
                f"{platform_repository} is internal",
                manual=True,
            )
        return Check(PLATFORM_ACCESS_CHECK, True, f"access_level {level} ({visibility} caller)")
    if not same_owner and level == "enterprise":
        return Check(
            PLATFORM_ACCESS_CHECK,
            False,
            f"access_level enterprise admits {consumer_owner} only inside the same enterprise "
            f"as {platform_owner}, which doctor cannot verify → confirm it, or move "
            f"{project_id} or {platform_repository} under one owner",
            manual=True,
        )
    return Check(
        PLATFORM_ACCESS_CHECK,
        False,
        f"access_level {level}: {project_id} cannot call the reusable workflows of "
        f"{platform_repository} (every caller fails before any job starts) → set {fix}",
        manual=True,
    )


ACTIONS_PERMISSIONS_CHECK = "repository Actions policy allows the generated workflows"
GITHUB_OWNED_ACTION_OWNERS = frozenset({"actions", "github"})


def workflow_uses(doc: object) -> set[str]:
    """Every remote action and reusable workflow a parsed workflow names in `uses:` (job-level
    calls and step actions); local `./` paths and `docker://` images are not policy-checked."""
    found: set[str] = set()
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    for job in jobs.values() if isinstance(jobs, dict) else ():
        if not isinstance(job, dict):
            continue
        values = [job.get("uses")]
        steps = job.get("steps")
        values += (
            [st.get("uses") for st in steps if isinstance(st, dict)]
            if isinstance(steps, list)
            else []
        )
        for value in values:
            text = str(value or "").strip()
            if text and not text.startswith(("./", "docker://")):
                found.add(text)
    return found


def _action_pattern_matches(pattern: str, action: str) -> bool:
    """GitHub's allowed-actions pattern (`owner/*`, `owner/repo@*`, `owner/repo@v1`,
    `owner/repo/.github/workflows/x.yml@*`): `*` is the only wildcard; a pattern without `@`
    names the action at any ref. Owners and repositories are case-insensitive."""
    pattern = pattern.strip()
    if not pattern:
        return False
    target = action if "@" in pattern else action.split("@", 1)[0]
    regex = ".*".join(re.escape(part) for part in pattern.casefold().split("*"))
    return re.fullmatch(regex, target.casefold()) is not None


def _action_allowed(action: str, selected: dict, consumer_owner: str) -> bool | None:
    """Is `action` allowed by a `selected` policy? None when only a verified-creator allowance
    could admit it, which the API does not let doctor verify.

    GitHub's semantics: an action runs when an allowance admits it (the owner's own, GitHub-owned,
    a positive pattern, a verified creator) AND no `!` pattern blocks it -- a `!`-prefixed entry
    of `patterns_allowed` carves a subset out of a broader allowance, whatever its order in the
    list (`["anthropics/*", "!anthropics/claude-code-action@*"]` blocks the Claude action). An
    exclusion is applied to every allowance, the owner's own included: doctor fails closed."""
    raw = selected.get("patterns_allowed")
    entries = [str(p).strip() for p in (raw if isinstance(raw, list) else [])]
    blocked = [p[1:] for p in entries if p.startswith("!")]
    if any(_action_pattern_matches(p, action) for p in blocked):
        return False
    owner = action.split("/", 1)[0].casefold()
    if owner == consumer_owner.casefold():
        return True  # the repository owner's own actions and workflows are always allowed
    if selected.get("github_owned_allowed") is True and owner in GITHUB_OWNED_ACTION_OWNERS:
        return True
    allowed = [p for p in entries if not p.startswith("!")]
    if any(_action_pattern_matches(p, action) for p in allowed):
        return True
    return None if selected.get("verified_allowed") is True else False


def actions_permissions_check(
    gh: GhRunner, project_id: str, uses: set[str], uninspected: Sequence[str] = ()
) -> Check:
    """The consumer's own Actions policy (`GET repos/{o}/{r}/actions/permissions`) must let every
    generated workflow run: Actions enabled, and `all` actions allowed -- or `selected` with an
    allowlist that admits every action and reusable workflow in `uses`. `local_only` cannot run
    them (they use GitHub's and third-party actions). Unreadable is a manual TODO, never READY.
    `uninspected` names reusable workflows in the call graph whose actions could not be
    collected: under `selected` they cannot be proven admitted, so that is a manual TODO too."""
    fix = f"Settings → Actions → General on {project_id}"
    perms = _safe_json(gh, ["api", f"repos/{project_id}/actions/permissions"])
    if not isinstance(perms, dict) or not isinstance(perms.get("enabled"), bool):
        return Check(
            ACTIONS_PERMISSIONS_CHECK,
            False,
            f"could not read the Actions permissions of {project_id} → confirm in {fix} that "
            "Actions are enabled and allow: " + ", ".join(sorted(uses)),
            manual=True,
        )
    if not perms["enabled"]:
        return Check(
            ACTIONS_PERMISSIONS_CHECK, False, f"Actions are disabled → enable them in {fix}"
        )
    allowed = perms.get("allowed_actions")
    if allowed == "all":
        return Check(ACTIONS_PERMISSIONS_CHECK, True, "Actions enabled, all actions allowed")
    if allowed == "local_only":
        return Check(
            ACTIONS_PERMISSIONS_CHECK,
            False,
            "allowed_actions local_only: the generated workflows use "
            + ", ".join(sorted(uses))
            + f" → allow them in {fix}",
        )
    if allowed != "selected":
        return Check(
            ACTIONS_PERMISSIONS_CHECK,
            False,
            f"allowed_actions {allowed!r} is not one doctor can evaluate → confirm in {fix}",
            manual=True,
        )
    selected = _safe_json(gh, ["api", f"repos/{project_id}/actions/permissions/selected-actions"])
    if not isinstance(selected, dict):
        return Check(
            ACTIONS_PERMISSIONS_CHECK,
            False,
            f"allowed_actions selected, but the allowlist of {project_id} could not be read → "
            "confirm it allows: " + ", ".join(sorted(uses)),
            manual=True,
        )
    owner = project_id.split("/")[0]
    verdicts = {action: _action_allowed(action, selected, owner) for action in sorted(uses)}
    denied = [a for a, ok in verdicts.items() if ok is False]
    unverified = [a for a, ok in verdicts.items() if ok is None]
    if denied:
        return Check(
            ACTIONS_PERMISSIONS_CHECK,
            False,
            "not allowed by the selected-actions policy: "
            + ", ".join(denied)
            + f" → add them to the allowlist in {fix}",
        )
    if unverified:
        return Check(
            ACTIONS_PERMISSIONS_CHECK,
            False,
            "admitted only if their creators are Marketplace-verified (doctor cannot check): "
            + ", ".join(unverified)
            + f" → confirm, or add them to the allowlist in {fix}",
            manual=True,
        )
    if uninspected:
        return Check(
            ACTIONS_PERMISSIONS_CHECK,
            False,
            "the selected-actions policy admits every collected dependency, but the actions of "
            "some called reusable workflows could not be collected: "
            + "; ".join(uninspected)
            + f" → confirm in {fix} that everything they use is allowed",
            manual=True,
        )
    return Check(
        ACTIONS_PERMISSIONS_CHECK, True, f"selected actions allow all {len(uses)} dependencies"
    )


PLATFORM_CONTRACT_CHECK = "pinned reusable workflows accept the generated calls"


def _workflow_call_interface(doc: object) -> tuple[dict, dict] | None:
    """(inputs, secrets) a reusable workflow declares under `on.workflow_call`; None if it
    declares no `workflow_call` trigger (it cannot be called at all)."""
    if not isinstance(doc, dict):
        return None
    triggers = doc.get("on", doc.get(True))
    if isinstance(triggers, str):
        triggers = {triggers: None}
    elif isinstance(triggers, list):
        triggers = {str(t): None for t in triggers}
    if not isinstance(triggers, dict) or "workflow_call" not in triggers:
        return None
    call = triggers.get("workflow_call")
    call = call if isinstance(call, dict) else {}
    inputs = call.get("inputs") if isinstance(call.get("inputs"), dict) else {}
    secrets = call.get("secrets") if isinstance(call.get("secrets"), dict) else {}
    return {str(k): v for k, v in inputs.items()}, {str(k): v for k, v in secrets.items()}


_WORKFLOW_CALL_INPUT_TYPES = ("string", "number", "boolean")
_WHOLE_EXPRESSION = re.compile(r"^\$\{\{(.*)\}\}$", re.DOTALL)
# Contexts GitHub documents as numbers: safe to pass, bare, to a `type: number` input.
_NUMBER_CONTEXTS = frozenset(
    {
        "github.run_number",
        "github.run_attempt",
        "github.event.number",
        "github.event.issue.number",
        "github.event.pull_request.number",
        "github.event.workflow_run.run_number",
        "github.event.workflow_run.run_attempt",
        "strategy.job-index",
        "strategy.job-total",
        "strategy.max-parallel",
    }
)


def _input_value_types(value: object) -> frozenset[str] | None:
    """The workflow_call input types a caller's `with:` value satisfies; None when it is an
    expression whose type doctor cannot determine. A literal YAML bool is a boolean, an int or
    float a number, a string a string; an expression is a string unless the whole value is
    `${{ fromJSON(...) }}` (any type: GitHub parses it) or a context GitHub documents as a
    number. Any other whole-value expression is undetermined (None): its result reaches a
    `string` input as text, but doctor cannot prove it is a number or a boolean."""
    if isinstance(value, bool):
        return frozenset({"boolean"})
    if isinstance(value, (int, float)):
        return frozenset({"number"})
    if not isinstance(value, str):
        return frozenset()  # null, a list or a mapping fits no input type
    whole = _WHOLE_EXPRESSION.fullmatch(value.strip())
    if whole is None or "${{" in whole.group(1):
        return frozenset({"string"})  # a literal, or text with expressions interpolated into it
    body = whole.group(1).strip()
    if re.fullmatch(r"fromJSON\(.*\)", body, re.DOTALL | re.IGNORECASE) and body.count("(") == 1:
        return frozenset(_WORKFLOW_CALL_INPUT_TYPES)
    if body in _NUMBER_CONTEXTS:
        return frozenset({"number"})
    return None


def reusable_call_mismatches(where: str, job: dict, callee: str, callee_doc: object) -> list[str]:
    """Why GitHub would reject `job`'s call of `callee` (parsed `callee_doc`): an input or secret
    the caller passes that the callee does not declare, one the callee requires that the caller
    does not pass (`secrets: inherit` passes every secret), a callee input with no valid `type`
    (GitHub rejects the callee itself), and a passed value that does not fit the declared type
    (or whose type doctor cannot determine, for a `number`/`boolean` input)."""
    interface = _workflow_call_interface(callee_doc)
    if interface is None:
        return [f"{where}: {callee} at the pin has no on.workflow_call trigger"]
    inputs, secrets = interface
    problems = []
    for name, spec in sorted(inputs.items()):
        declared = spec.get("type") if isinstance(spec, dict) else None
        if declared not in _WORKFLOW_CALL_INPUT_TYPES:
            problems.append(
                f"{where}: {callee} declares input {name!r} with "
                + (f"type {declared!r}" if declared is not None else "no type")
                + " (workflow_call inputs need string, number or boolean)"
            )
    given_inputs = job.get("with") if isinstance(job.get("with"), dict) else {}
    passed = {str(k): v for k, v in given_inputs.items()}
    for name in sorted(passed):
        if name not in inputs:
            problems.append(f"{where}: passes input {name!r}, which {callee} does not declare")
            continue
        spec = inputs[name]
        declared = spec.get("type") if isinstance(spec, dict) else None
        if declared not in _WORKFLOW_CALL_INPUT_TYPES:
            continue  # reported above
        value = passed[name]
        fits = _input_value_types(value)
        if fits is None and declared == "string":
            continue  # an expression's result reaches a string input as text
        if fits is None:
            problems.append(
                f"{where}: passes input {name!r} as {value!r}, whose type doctor cannot "
                f"determine, but {callee} declares it {declared}"
            )
        elif declared not in fits:
            problems.append(
                f"{where}: passes input {name!r} as {value!r} ("
                + ("/".join(sorted(fits)) or type(value).__name__)
                + f"), but {callee} declares it {declared}"
            )
    for name, spec in sorted(inputs.items()):
        if isinstance(spec, dict) and spec.get("required") is True and name not in passed:
            problems.append(f"{where}: does not pass input {name!r}, which {callee} requires")
    given = job.get("secrets")
    if given != "inherit":
        given = given if isinstance(given, dict) else {}
        for name in sorted(str(k) for k in given):
            if name not in secrets:
                problems.append(f"{where}: passes secret {name!r}, which {callee} does not declare")
        for name, spec in sorted(secrets.items()):
            if isinstance(spec, dict) and spec.get("required") is True and name not in given:
                problems.append(f"{where}: does not pass secret {name!r}, which {callee} requires")
    return problems


def _decoded_blob(blob: object) -> bytes | None:
    """The bytes of a contents-API answer for a regular file (None otherwise)."""
    if not isinstance(blob, dict) or blob.get("encoding") != "base64":
        return None
    try:
        return base64.b64decode(str(blob.get("content") or ""))
    except ValueError:
        return None


def _repo_rulesets(project_id: str, gh: GhRunner) -> list[dict] | Unreadable:
    """Rulesets owned by the repository itself: every page, without inherited org rulesets
    (a repository admin cannot update those through the repository endpoint); `Unreadable`
    when the inventory cannot be read in full."""
    rows = _paged_items(gh, f"repos/{project_id}/rulesets?includes_parents=false")
    if isinstance(rows, Unreadable):
        return rows
    return [r for r in rows if r.get("source_type", "Repository") == "Repository"]


EFFECTIVE_RULES_CHECK = "effective default-branch rules (org/enterprise rulesets too) fit Forge"
ISSUES_ENABLED_CHECK = "GitHub Issues are enabled"


_MATRIX_REF = re.compile(r"\$\{\{\s*matrix\.([A-Za-z_][A-Za-z0-9_-]*)\s*\}\}")


def _matrix_scalar(value: object) -> str | None:
    """How GitHub renders a matrix value inside a job name; None for a value doctor cannot
    render (an expression, a list or a mapping)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str) and "${{" not in value:
        return value
    return None


def _matrix_combinations(matrix: object) -> list[dict] | None:
    """The finite combinations of a literal `strategy.matrix` (product of the value lists, minus
    `exclude`, then `include` extending matching combinations or adding new ones); None when any
    part is an expression or not literal data."""
    if not isinstance(matrix, dict):
        return None
    combos: list[dict] = [{}]
    for key, values in matrix.items():
        if key in ("include", "exclude"):
            continue
        if not isinstance(values, list) or not values:
            return None
        combos = [{**c, str(key): v} for c in combos for v in values]
    for part in ("exclude", "include"):
        entries = matrix.get(part, [])
        if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
            return None
    base_keys = {str(k) for k in matrix if k not in ("include", "exclude")}
    for entry in matrix.get("exclude", []):
        entry = {str(k): v for k, v in entry.items()}
        combos = [c for c in combos if any(c.get(k) != v for k, v in entry.items())]
    if not base_keys:
        combos = []
    originals = [dict(c) for c in combos]
    for entry in matrix.get("include", []):
        entry = {str(k): v for k, v in entry.items()}
        matched = False
        for original, combo in zip(originals, combos, strict=False):  # appended ones extend nothing
            if all(original.get(k, v) == v for k, v in entry.items() if k in base_keys):
                combo.update(entry)
                matched = True
        if not matched:
            combos.append(dict(entry))
    return combos


def _matrix_job_names(name: str, strategy: object) -> list[str] | None:
    """Every name a job named with `${{ matrix.* }}` expressions reports, over a finite literal
    matrix -- ONE PER COMBINATION, duplicates kept: `test-${{ matrix.os }}` over one OS and two
    Python versions is two jobs both reporting `test-ubuntu`, i.e. two producers of that context
    (`ambiguous_context`). None when the name holds any other expression or the matrix is not
    literal."""
    if "${{" in _MATRIX_REF.sub("", name):
        return None
    matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
    combos = _matrix_combinations(matrix)
    if not combos:
        return None
    names: list[str] = []  # one per combination: a name two combinations share is two jobs
    for combo in combos:
        parts: dict[str, str] = {}
        for key in _MATRIX_REF.findall(name):
            rendered = _matrix_scalar(combo[key]) if key in combo else ""  # absent renders empty
            if rendered is None:
                return None
            parts[key] = rendered
        names.append(_MATRIX_REF.sub(lambda m, parts=parts: parts[m.group(1)], name))
    return sorted(names)


_GLOB_SPECIAL = frozenset("*?+[]\\!")
_GLOB_RANGE_ENDS = ("az", "AZ", "09")


def _glob_class(body: str) -> str | None:
    """A `[...]` filter class as a regex class: GitHub admits only alphanumeric characters and
    the ranges a-z, A-Z and 0-9 (or sub-ranges of one of them). None: anything else."""
    out, i = "", 0
    if not body:
        return None
    while i < len(body):
        char = body[i]
        if not (char.isascii() and char.isalnum()):
            return None
        if body[i + 1 : i + 2] == "-" and i + 2 < len(body):
            end = body[i + 2]
            if not any(
                lo <= char <= end <= hi and (char.isdigit() == end.isdigit())
                for lo, hi in _GLOB_RANGE_ENDS
            ):
                return None
            out, i = out + f"{char}-{end}", i + 3
        else:
            out, i = out + char, i + 1
    return f"[{out}]"


def _branch_glob_matches(pattern: str, branch: str) -> bool | None:
    """GitHub's branch filter pattern (workflow syntax, filter pattern cheat sheet), exactly:
    `*` zero or more characters but not `/`, `**` zero or more of any character, `?` zero or one
    of the PRECEDING character, `+` one or more of the preceding character, `[...]` one
    alphanumeric character listed or in a range (a-z, A-Z, 0-9), `\\` escapes the next special
    character; anything else is literal. None: a pattern doctor cannot evaluate exactly (a
    leading `!` negation, a `?`/`+` with no plain preceding character, a malformed class,
    `***`, a trailing `\\`) -- the caller then treats the workflow as not proven to run."""
    if not pattern or pattern.startswith("!"):
        return None
    regex = ""
    i = 0
    quantifiable = False  # does the regex end in ONE plain character `?`/`+` may apply to?
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("***", i):
            return None
        if pattern.startswith("**", i):
            regex, i, quantifiable = regex + ".*", i + 2, False
        elif char == "*":
            regex, i, quantifiable = regex + "[^/]*", i + 1, False
        elif char in "?+":
            if not quantifiable:
                return None
            regex, i, quantifiable = regex + char, i + 1, False
        elif char == "[":
            close = pattern.find("]", i + 1)
            cls = _glob_class(pattern[i + 1 : close]) if close > i else None
            if cls is None:
                return None
            regex, i, quantifiable = regex + cls, close + 1, False
        elif char == "]":
            return None
        elif char == "\\":
            nxt = pattern[i + 1 : i + 2]
            if nxt not in _GLOB_SPECIAL:
                return None
            regex, i, quantifiable = regex + re.escape(nxt), i + 2, False
        else:
            regex, i, quantifiable = regex + re.escape(char), i + 1, True
    return re.fullmatch(regex, branch) is not None


def _reports_on_protected_pull_requests(doc: dict, default_branch: str) -> bool:
    """Whether a workflow runs for EVERY pull request into the protected branch: a
    `pull_request` trigger with no `paths`/`paths-ignore` filter (a PR outside it never reports),
    no `branches-ignore` and no negated pattern (fail closed), a `branches` filter only when one
    of its globs matches the default branch, and activity types (if listed) covering every head
    update. Only such a workflow's jobs produce required contexts; a schedule-, dispatch- or
    call-only workflow reports nothing on the PR. `pull_request_target` does not count either:
    it runs on the BASE branch's last commit (`GITHUB_SHA`), so its checks attach to that commit,
    never to the PR head or test-merge commit a required check is evaluated on. Only
    `pull_request` attaches to the revision being protected."""
    triggers = doc.get("on", doc.get(True))
    for event in ("pull_request",):
        if event not in _triggers(doc):
            continue
        spec = triggers.get(event) if isinstance(triggers, dict) else None
        if spec is None:
            return True
        if not isinstance(spec, dict):
            continue
        if any(key in spec for key in ("paths", "paths-ignore", "branches-ignore")):
            continue
        if "branches" in spec:
            branches = spec["branches"]
            listed = [branches] if isinstance(branches, str) else branches
            if not isinstance(listed, list):
                continue
            patterns = [str(b) for b in listed]
            if any(p.startswith("!") for p in patterns) or not any(
                _branch_glob_matches(p, default_branch) is True for p in patterns
            ):
                continue
        if "types" in spec:
            types = spec["types"]
            listed = [types] if isinstance(types, str) else types
            if not isinstance(listed, list) or not all(
                t in [str(x) for x in listed] for t in PULL_REQUEST_REQUIRED_TYPES
            ):
                continue
        return True
    return False


def _job_context_names(job_id: str, job: dict) -> list[str]:
    """Every exact context name one job reports. A plain job: its `name:` (else its id). A job
    named with `${{ matrix.* }}` expressions: that name evaluated for each combination of a finite
    literal matrix (`_matrix_job_names`). Any other matrix job: GitHub appends the combination's
    values, `name (v1, v2, ...)` -- the axes' values in axis order, then the values `include`
    adds -- so one context per literal combination (`_matrix_combinations`). Anything doctor
    cannot enumerate (another expression in the name, a non-literal matrix or value) contributes
    nothing: a required context must be proven, never matched by a prefix."""
    name = str(job.get("name") or job_id)
    strategy = job.get("strategy")
    if "${{" in name:
        rendered = _matrix_job_names(name, strategy) or ()
        return [r for r in rendered if r.strip()]
    matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
    if not matrix:
        return [name]
    names: list[str] = []  # one per combination, duplicates kept (each is its own job)
    for combo in _matrix_combinations(matrix) or ():
        values = [_matrix_scalar(value) for value in combo.values()]
        if not combo or any(value is None for value in values):
            return []
        names.append(f"{name} ({', '.join(str(v) for v in values)})")
    return sorted(names)


def _callee_doc(
    base: Path,
    uses: str,
    platform_repository: str | None,
    load_callee: CalleeLoader | None,
    seen: tuple[object, ...],
) -> tuple[dict | None, object]:
    """The reusable workflow a `uses:` names, as GitHub would run it -- a local
    `./.github/workflows/x.yml` from the same workflow set, or a platform workflow pinned to a
    full SHA as fetched at that SHA -- with its cycle key; (None, None) for anything else."""
    if uses.startswith("./"):
        workflows = (base / ".github/workflows").resolve()
        target = (base / uses[2:]).resolve()
        if "@" in uses or target.parent != workflows or target in seen or not target.is_file():
            return None, None
        return _workflow_doc(target), target
    found = _REMOTE_REUSABLE.fullmatch(uses)
    if not (
        found
        and platform_repository
        and found.group(1) == platform_repository
        and _SHA.fullmatch(found.group(3))
        and load_callee is not None
    ):
        return None, None
    key = (found.group(2), found.group(3))
    if key in seen:
        return None, None
    called = load_callee(*key)
    return (called if isinstance(called, dict) else None), key


def _workflow_job_contexts(
    base: Path,
    doc: dict,
    platform_repository: str | None,
    load_callee: CalleeLoader | None,
    seen: tuple[object, ...],
) -> list[str]:
    """The exact contexts the jobs of one workflow report (`_job_context_names`), once per job
    that reports it -- two jobs of one workflow reporting the same name are two producers. A job
    that calls a reusable workflow reports `caller / callee-job` for each job of the callee as
    GitHub would run it (`_callee_doc`), recursively; an unresolvable callee contributes nothing
    (fail closed), and so does a matrix caller (its `name (...) / ...` contexts are not
    enumerated)."""
    exact: list[str] = []
    jobs = doc.get("jobs")
    for job_id, job in (jobs if isinstance(jobs, dict) else {}).items():
        if not isinstance(job, dict):
            continue
        names = _job_context_names(str(job_id), job)
        if not job.get("uses"):
            exact.extend(names)
            continue
        strategy = job.get("strategy")
        if isinstance(strategy, dict) and strategy.get("matrix"):
            continue
        called, key = _callee_doc(
            base, str(job["uses"]).strip(), platform_repository, load_callee, seen
        )
        if called is None:
            continue
        inner = _workflow_job_contexts(base, called, platform_repository, load_callee, (*seen, key))
        for name in names:
            exact.extend(f"{name} / {context}" for context in inner)
    return exact


def _reports_on_merge_groups(doc: dict, default_branch: str) -> bool:
    """Whether a workflow runs for EVERY merge group a merge queue on `default_branch` creates:
    a `merge_group` trigger with no `branches-ignore` and no negated pattern (fail closed), a
    `branches` filter only when one of its globs matches the default branch, and activity types
    (if listed) including `checks_requested`."""
    if "merge_group" not in _triggers(doc):
        return False
    triggers = doc.get("on", doc.get(True))
    spec = triggers.get("merge_group") if isinstance(triggers, dict) else None
    if spec is None:
        return True
    if not isinstance(spec, dict) or "branches-ignore" in spec:
        return False
    if "branches" in spec:
        branches = spec["branches"]
        listed = [branches] if isinstance(branches, str) else branches
        if not isinstance(listed, list):
            return False
        patterns = [str(b) for b in listed]
        if any(p.startswith("!") for p in patterns) or not any(
            _branch_glob_matches(p, default_branch) is True for p in patterns
        ):
            return False
    if "types" in spec:
        types = spec["types"]
        listed = [types] if isinstance(types, str) else types
        if not isinstance(listed, list) or "checks_requested" not in [str(x) for x in listed]:
            return False
    return True


@dataclass(frozen=True)
class ProducedContexts:
    """What a workflow set reports: the exact contexts of the workflows that run on every pull
    request into the default branch, those of the workflows that run on every merge group (a
    merge queue's checks), and whether any workflow declares `merge_group` at all.

    `pull_request_producers` / `merge_group_producers` keep WHO reports each context on that
    event: one entry (the workflow file) per job that reports it. A required context with more
    than one producer for the same event is ambiguous (`ambiguous_context`); an empty mapping
    (a hand-built value) records no producers, so nothing is ambiguous."""

    pull_request: frozenset[str] = frozenset()
    merge_group: frozenset[str] = frozenset()
    merge_group_declared: bool = False
    pull_request_producers: Mapping[str, tuple[str, ...]] = field(
        default_factory=dict, compare=False, hash=False
    )
    merge_group_producers: Mapping[str, tuple[str, ...]] = field(
        default_factory=dict, compare=False, hash=False
    )


def workflow_check_contexts(
    base: Path,
    default_branch: str = "main",
    platform_repository: str | None = None,
    load_callee: CalleeLoader | None = None,
    inactive: frozenset[str] = frozenset(),
) -> ProducedContexts:
    """What the workflows under `base` can report as status-check contexts into `default_branch`
    (`_workflow_job_contexts`: job names, every literal matrix combination, and `caller /
    callee-job` for every job of a resolvable reusable workflow), separately ON A PULL REQUEST
    (`_reports_on_protected_pull_requests`) and IN A MERGE GROUP (`_reports_on_merge_groups`).
    A workflow whose file name is in `inactive` (disabled in Actions) reports nothing: it does
    not respond to its triggers. Doctor passes the DEFAULT BRANCH's workflow set, not the
    checkout."""
    pull_request: dict[str, list[str]] = {}
    merge_group: dict[str, list[str]] = {}
    declared = False
    workflows = base / ".github/workflows"
    paths = sorted([*workflows.glob("*.yml"), *workflows.glob("*.yaml")])
    for path in paths:
        if path.name in inactive:
            continue
        doc = _workflow_doc(path) or {}
        declared = declared or "merge_group" in _triggers(doc)
        on_pull_request = _reports_on_protected_pull_requests(doc, default_branch)
        on_merge_group = _reports_on_merge_groups(doc, default_branch)
        if not (on_pull_request or on_merge_group):
            continue
        found = _workflow_job_contexts(
            base, doc, platform_repository, load_callee, (path.resolve(),)
        )
        for context in found:
            if on_pull_request:
                pull_request.setdefault(context, []).append(path.name)
            if on_merge_group:
                merge_group.setdefault(context, []).append(path.name)
    return ProducedContexts(
        frozenset(pull_request),
        frozenset(merge_group),
        declared,
        {c: tuple(p) for c, p in pull_request.items()},
        {c: tuple(p) for c, p in merge_group.items()},
    )


def context_produced(context: str, produced: ProducedContexts) -> bool:
    """Does `produced` (`workflow_check_contexts`) report `context` on pull requests? Exactly:
    no prefix ever proves a context (a matrix job's contexts are enumerated)."""
    return context in produced.pull_request


def ambiguous_context(context: str, produced: ProducedContexts) -> str | None:
    """Why required `context` is ambiguous in `produced`, or None: more than one workflow job
    reports that exact name for the same triggering event (pull_request, merge_group -- judged
    separately). GitHub then cannot tell which result the requirement means and the duplicate
    can leave pull requests blocked; the context must be reported by exactly one job."""
    reasons = []
    for event, producers in (
        ("pull_request", produced.pull_request_producers),
        ("merge_group", produced.merge_group_producers),
    ):
        found = producers.get(context, ())
        if len(found) > 1:
            reasons.append(f"{len(found)} jobs on {event} ({', '.join(sorted(found))})")
    if not reasons:
        return None
    return (
        f"required status check '{context}' is reported by "
        + " and ".join(reasons)
        + " → rename or remove the duplicate job name so exactly one job reports it"
    )


# Rule types a Forge PR always satisfies (no parameter of theirs can block it): deleting or
# force-pushing the branch, creating it, linear history (Forge PRs merge like any other), PR
# reviews (the draft PR waits for a human anyway), and tag names (the publisher pushes no tags).
_COMPATIBLE_RULE_TYPES = frozenset(
    {
        "creation",
        "deletion",
        "non_fast_forward",
        "required_linear_history",
        "pull_request",
        "tag_name_pattern",
    }
)
# Metadata-pattern rules: which field of what the publisher pushes each one restricts.
_PATTERN_RULE_FIELDS = {
    "commit_message_pattern": ("message", "commit message"),
    "commit_author_email_pattern": ("author_email", "commit author email"),
    "committer_email_pattern": ("committer_email", "committer email"),
    "branch_name_pattern": ("branch", "branch name"),
}


@dataclass(frozen=True)
class PublisherValue:
    """One value the publisher pushes, as its shell template: `literals[0] $var[0] literals[1]
    ... literals[-1]`, each variable a run-time number (`_PUBLISHER_VARIABLES`). Its domain is
    every value of those variables -- infinitely many -- so a rule is judged for ALL of them,
    never for a finite sample (`_value_verdict`). `samples` are real values of the domain: one
    that a rule rejects is a counterexample, never a proof that the rest pass."""

    literals: tuple[str, ...]
    variables: tuple[str, ...] = ()

    @property
    def constant(self) -> bool:
        return not self.variables

    def render(self, fill: Callable[[str], str]) -> str:
        out = self.literals[0]
        for var, lit in zip(self.variables, self.literals[1:], strict=True):
            out += fill(var) + lit
        return out

    @property
    def template(self) -> str:
        return self.render(lambda var: "${" + var + "}")

    @property
    def example(self) -> str:
        """The value with every variable 1 (issue #1, run 1): a real value of the domain."""
        return self.render(lambda var: "1")

    @property
    def samples(self) -> tuple[str, ...]:
        out = [""]
        for i, lit in enumerate(self.literals):
            out = [o + lit for o in out]
            if i < len(self.variables):
                out = [o + v for o in out for v in _PUBLISHER_SAMPLES[self.variables[i]]]
        return tuple(dict.fromkeys(out))

    def loose_regex(self) -> str:
        """A regex for a SUPERSET of the domain (each variable any non-empty digit run)."""
        out = re.escape(self.literals[0])
        for lit in self.literals[1:]:
            out += "[0-9]+" + re.escape(lit)
        return out

    def __str__(self) -> str:
        return self.template


@dataclass(frozen=True)
class PublisherCommit:
    """What the Actions publisher (`reusable-implement.yml` at the pin) pushes, as templates
    over its run-time variables: the commit message, its author and committer email, and the
    branch name. A metadata-pattern rule must hold for EVERY value of each."""

    message: PublisherValue
    author_email: PublisherValue
    committer_email: PublisherValue
    branch: PublisherValue


# The run-time variables a publisher template may use. Each is a positive decimal number (an
# issue number, a run id); the proofs below rely only on "a non-empty run of digits". The
# samples are real values, used only to find counterexamples.
_PUBLISHER_SAMPLES = {
    "ISSUE_NUMBER": ("1", "2", "42", "12345"),
    "GITHUB_RUN_ID": ("1", "9876543210"),
    # the cloud routine's branch stamp: `date -u +%Y%m%d%H%M%S` (CLOUD_ROUTINE_BRANCH)
    "RUN_STAMP": ("1", "20261011093000"),
}
#: The variables a WORKFLOW's publisher template may use (`_publisher_value`); RUN_STAMP is the
#: cloud routine's own and never appears in a workflow.
_WORKFLOW_VARIABLES = frozenset({"ISSUE_NUMBER", "GITHUB_RUN_ID"})
_SHELL_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
_GIT_IDENTITY_ENV = frozenset(
    {
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_EMAIL",
        "EMAIL",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
    }
)


def _publisher_value(template: str) -> PublisherValue | None:
    """A shell word as a `PublisherValue`; None if it uses anything else (an unknown variable,
    a substitution, an escape) that doctor cannot evaluate."""
    parts = _SHELL_VAR.split(template)
    # split() yields: literal, braced-name, bare-name, literal, ...
    literals = tuple(parts[0::3])
    names = tuple(braced or bare for braced, bare in zip(parts[1::3], parts[2::3], strict=True))
    if any(ch in lit for lit in literals for ch in "$`\\") or any(
        n not in _WORKFLOW_VARIABLES for n in names
    ):
        return None
    return PublisherValue(literals, names)


def publisher_commit(doc: object) -> PublisherCommit | str:
    """`PublisherCommit` read from the parsed `reusable-implement.yml`: the ONE step that runs
    `git push`, its `git config user.email`, `git commit -m` and pushed ref. A str says why it
    cannot be read exactly (doctor then leaves metadata-pattern rules a manual TODO)."""
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    if not isinstance(jobs, dict):
        return "reusable-implement.yml declares no jobs"
    pushing = []
    for job_id, job in jobs.items():
        for step in (job.get("steps") if isinstance(job, dict) else None) or []:
            run = step.get("run") if isinstance(step, dict) else None
            if isinstance(run, str):
                commands = _shell_commands(run)
                if any(c[:2] == ("git", "push") for c in commands):
                    pushing.append((job_id, job, step, run, commands))
    if len(pushing) != 1:
        return f"expected one step that runs `git push`, found {len(pushing)}"
    job_id, job, step, run, commands = pushing[0]
    envs = [doc.get("env"), job.get("env"), step.get("env")]
    if any(isinstance(env, dict) and _GIT_IDENTITY_ENV & set(env) for env in envs):
        return f"job {job_id} sets the git identity through the environment"
    emails = [c[3] for c in commands if c[:3] == ("git", "config", "user.email") and len(c) == 4]
    commits = [c for c in commands if c[:2] == ("git", "commit")]
    pushes = [c for c in commands if c[:2] == ("git", "push")]
    if len(emails) != 1 or len(commits) != 1 or len(pushes) != 1:
        return (
            f"job {job_id}: expected one `git config user.email`, `git commit` and `git push`, "
            f"found {len(emails)}, {len(commits)} and {len(pushes)}"
        )
    if len(commits[0]) != 4 or commits[0][2] != "-m":
        return f"job {job_id}: the commit is not `git commit -m MESSAGE`"
    if len(pushes[0]) != 4 or not pushes[0][3].startswith("HEAD:refs/heads/"):
        return f"job {job_id}: the push is not `git push REMOTE HEAD:refs/heads/BRANCH`"
    branch = pushes[0][3].removeprefix("HEAD:refs/heads/")
    named = re.fullmatch(r"\$\{([A-Za-z_]\w*)\}|\$([A-Za-z_]\w*)", branch)
    if named:
        var = named.group(1) or named.group(2)
        found = re.findall(rf"^\s*{var}=(\"[^\"]*\"|'[^']*'|\S*)\s*$", run, re.MULTILINE)
        if len(found) != 1:
            return f"job {job_id}: the pushed branch ${var} is not assigned exactly once"
        branch = found[0][1:-1] if found[0][:1] in ("'", '"') else found[0]
    message = _publisher_value(commits[0][3])
    author = _publisher_value(emails[0])
    pushed = _publisher_value(branch)
    if message is None or author is None or pushed is None:
        return (
            f"job {job_id}: the commit message, email or branch uses a value doctor cannot expand"
        )
    return PublisherCommit(message, author, author, pushed)


@dataclass(frozen=True)
class PublisherPusher:
    """The actor whose credential performs the publisher's `git push` of its agent branch: the
    one a classic push allowlist or a ruleset bypass covering agent/** must admit."""

    label: str
    app_slug: str


#: `actions/checkout` persists the token it was given in the local git config, and `git push`
#: then authenticates with it: with the default `github.token` the push is made by the GitHub
#: Actions app (`github-actions[bot]`), not by the Publisher App (whose token only opens the PR).
GITHUB_ACTIONS_PUSHER = PublisherPusher(
    f"github-actions[bot] (the GitHub Actions app, integration {GITHUB_ACTIONS_INTEGRATION_ID}, "
    "via the checkout's persisted github.token)",
    "github-actions",
)
_DEFAULT_TOKEN_EXPRESSIONS = frozenset({"${{github.token}}", "${{secrets.github_token}}"})
#: Actions that may run in the publishing job before the push without touching git's
#: credentials (any other `uses:` step could rewrite them, so the pusher is then unknown).
_CREDENTIAL_NEUTRAL_ACTIONS = (
    "actions/create-github-app-token@",
    "actions/download-artifact@",
    "actions/upload-artifact@",
)
#: `git config` keys a publishing step may set without changing how `git push` authenticates.
_CREDENTIAL_NEUTRAL_GIT_CONFIG = frozenset({"user.name", "user.email", "core.hookspath"})
_CREDENTIAL_ENV = re.compile(r"\bGIT_[A-Z_]*=|ASKPASS|SSH_AUTH_SOCK")


def _credential_env_keys(env: object) -> list[str]:
    if not isinstance(env, dict):
        return []
    return [
        str(k)
        for k in env
        if str(k).upper().startswith("GIT_") or str(k).upper() in ("SSH_ASKPASS", "SSH_AUTH_SOCK")
    ]


def publisher_push_actor(doc: object) -> PublisherPusher | str:
    """Who performs the publisher's `git push`, read from the parsed `reusable-implement.yml`:
    the credential the ONE `actions/checkout` before the pushing step persists (`token:`,
    `persist-credentials:`), provided nothing between the checkout and the push can replace it
    (another action, a credential/remote/`-c` git config, a GIT_*/ASKPASS environment). Only the
    default `github.token` is recognised (`GITHUB_ACTIONS_PUSHER`); anything else is a str
    saying why the pusher is unknown, which doctor reports as a manual TODO."""
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    if not isinstance(jobs, dict):
        return "reusable-implement.yml declares no jobs"
    pushing = []
    for job_id, job in jobs.items():
        steps = job.get("steps") if isinstance(job, dict) else None
        for index, step in enumerate(steps if isinstance(steps, list) else []):
            run = step.get("run") if isinstance(step, dict) else None
            if isinstance(run, str) and any(c[:2] == ("git", "push") for c in _shell_commands(run)):
                pushing.append((job_id, job, steps, index))
    if len(pushing) != 1:
        return f"expected one step that runs `git push`, found {len(pushing)}"
    job_id, job, steps, push_index = pushing[0]
    where = f"job {job_id}"
    for env in (doc.get("env"), job.get("env"), steps[push_index].get("env")):
        if keys := _credential_env_keys(env):
            return f"{where} sets {', '.join(keys)} for the push"
    checkouts = [
        i
        for i, step in enumerate(steps[:push_index])
        if isinstance(step, dict) and str(step.get("uses") or "").startswith("actions/checkout@")
    ]
    if len(checkouts) != 1:
        return f"{where}: expected one actions/checkout before the push, found {len(checkouts)}"
    checkout = steps[checkouts[0]]
    options = checkout.get("with") if checkout.get("with") is not None else {}
    if not isinstance(options, dict):
        return f"{where}: the checkout's `with:` could not be read"
    if "ssh-key" in options:
        return f"{where}: the checkout authenticates with an SSH key"
    persist = options.get("persist-credentials", True)
    if persist is not True and str(persist).strip().lower() != "true":
        return f"{where}: the checkout does not persist its credentials ({persist!r})"
    token = options.get("token")
    if token is not None and re.sub(r"\s+", "", str(token)).lower() not in (
        _DEFAULT_TOKEN_EXPRESSIONS
    ):
        return f"{where}: the checkout persists the token {token!r}, not github.token"
    for step in steps[: push_index + 1]:
        if not isinstance(step, dict):
            return f"{where}: a step could not be read"
        name = step.get("name") or step.get("id") or "?"
        uses = str(step.get("uses") or "")
        if uses and not uses.startswith(("actions/checkout@", *_CREDENTIAL_NEUTRAL_ACTIONS)):
            return (
                f"{where}: step {name!r} ({uses}) runs before the push and may change git's "
                "credentials"
            )
        run = step.get("run")
        if not isinstance(run, str):
            continue
        if _CREDENTIAL_ENV.search(run):
            return f"{where}: step {name!r} sets a git credential variable"
        for command in _shell_commands(run):
            if command[:1] == ("gh",) and command[1:3] == ("auth", "setup-git"):
                return f"{where}: step {name!r} runs `gh auth setup-git`"
            if command[:1] != ("git",) or len(command) < 2:
                continue
            if command[1] in ("-c", "--config-env", "remote", "credential"):
                return f"{where}: step {name!r} runs `git {command[1]}`"
            if command[1] == "config" and (
                len(command) < 3 or command[2].lower() not in _CREDENTIAL_NEUTRAL_GIT_CONFIG
            ):
                return f"{where}: step {name!r} runs `{' '.join(command[:3])}`"
    pushes = [c for c in _shell_commands(steps[push_index]["run"]) if c[:2] == ("git", "push")]
    if len(pushes) != 1 or len(pushes[0]) < 3 or pushes[0][2] != "origin":
        return f"{where}: the push is not `git push origin ...` (the checkout's remote)"
    return GITHUB_ACTIONS_PUSHER


def _portable_regex(pattern: str) -> re.Pattern[str] | None:
    """`pattern` compiled, when Python evaluates it as GitHub's ruleset engine does: literals,
    `.`, classes, groups `(...)`/`(?:...)`, alternation, quantifiers, anchors `^`/`$` and the
    escapes \\d \\D \\w \\W \\s \\S or an escaped punctuation character. Anything else
    (lookaround, backreferences, inline flags, POSIX classes, \\A \\Z \\b ...) is None:
    engines disagree there, so doctor does not guess."""
    i = 0
    while i < len(pattern):
        char, nxt = pattern[i], pattern[i + 1 : i + 2]
        if char == "\\":
            if not nxt or (nxt.isalnum() and nxt not in "dDwWsS") or not nxt.isascii():
                return None
            i += 2
            continue
        if char == "(" and nxt == "?" and pattern[i + 2 : i + 3] != ":":
            return None
        i += 1
    if any(t in pattern for t in ("[:", "[=", "[.", "{,", "&&", "~~", "--", "||")):
        return None
    try:
        return re.compile(pattern)
    except re.error:
        return None


def _pattern_verdict(params: dict, value: str) -> bool | None:
    """Does `value` satisfy a metadata-pattern rule's parameters (`operator` starts_with /
    ends_with / contains / regex, `pattern`, `negate`)? None: doctor cannot evaluate it."""
    pattern, operator = params.get("pattern"), params.get("operator")
    negate = params.get("negate", False)
    if not isinstance(pattern, str) or not isinstance(negate, bool):
        return None
    if operator == "starts_with":
        hit = value.startswith(pattern)
    elif operator == "ends_with":
        hit = value.endswith(pattern)
    elif operator == "contains":
        hit = pattern in value
    elif operator == "regex":
        compiled = _portable_regex(pattern)
        if compiled is None:
            return None
        hit = compiled.search(value) is not None
    else:
        return None
    return hit != negate


def _operator_hits_every_value(params: dict, value: PublisherValue) -> bool | None:
    """Does the rule's operator (before `negate`) hit EVERY value of `value`'s domain (True),
    NO value (False), or does that depend on the run-time numbers / is it unprovable (None)?
    Each variable is a non-empty run of digits. A constant value is evaluated exactly; a
    `regex` over a variable value is never proven (a manual TODO)."""
    pattern, operator = params.get("pattern"), params.get("operator")
    if not isinstance(pattern, str):
        return None
    if value.constant:
        hit = _pattern_verdict({**params, "negate": False}, value.example)
        return hit
    head, tail = value.literals[0], value.literals[-1]
    if operator == "starts_with":
        if head.startswith(pattern):
            return True
        if not pattern.startswith(head):
            return False  # differs inside the constant head
        # the head is a proper prefix of the pattern: next comes a digit of the variable
        return False if not pattern[len(head)].isdigit() else None
    if operator == "ends_with":
        if tail.endswith(pattern):
            return True
        if not pattern.endswith(tail):
            return False
        return False if not pattern[len(pattern) - len(tail) - 1].isdigit() else None
    if operator == "contains":
        if any(pattern in lit for lit in value.literals):
            return True
        # with no digit, an occurrence cannot overlap a variable's digits, so it would lie
        # inside one constant part -- and it does not
        return False if not any(ch.isdigit() for ch in pattern) else None
    return None


def _value_verdict(params: dict, value: PublisherValue) -> tuple[bool | None, str]:
    """(verdict, witness) of a metadata-pattern rule over EVERY value of `value`: False with a
    real value it rejects (a counterexample among the samples, or a proof that every value is
    rejected); True only when proven for the whole domain; None: doctor cannot prove it (e.g.
    `^Implement issue #(1|42)$` passes every sample yet rejects issue #2)."""
    unknown = False
    for sample in value.samples:
        ok = _pattern_verdict(params, sample)
        if ok is False:
            return False, sample
        unknown = unknown or ok is None
    if unknown:
        return None, value.template
    negate = params.get("negate", False)
    hits = _operator_hits_every_value(params, value)
    if hits is None:
        return None, value.template
    return (hits != negate), value.example


def _pattern_rule_problem(
    source: str, kind: str, params: dict, publisher: PublisherCommit | str
) -> str | None:
    attr, what = _PATTERN_RULE_FIELDS[kind]
    shown = f"{params.get('operator')} {params.get('pattern')!r}" + (
        " (negated)" if params.get("negate") is True else ""
    )
    if isinstance(publisher, str):
        return (
            f"{source}: restricts the {what} ({shown}), and the publisher's {what} could not be "
            f"read ({publisher}) → verify Forge's commits satisfy it"
        )
    value: PublisherValue = getattr(publisher, attr)
    verdict, witness = _value_verdict(params, value)
    if verdict is False:
        scope = (
            " (if the ruleset also targets the publisher's branches)"
            if kind == "branch_name_pattern"
            else ""
        )
        return (
            f"{source}: requires the {what} to match {shown}; the publisher's {what} "
            f"{witness!r} does not, so its push is rejected{scope}"
        )
    if verdict is None:
        over = ", ".join(dict.fromkeys(value.variables))
        return (
            f"{source}: restricts the {what} ({shown}) in a way doctor cannot evaluate exactly "
            f"for every value → verify the publisher's {what} {value.template!r} satisfies it"
            + (f" for every {over}" if over else "")
        )
    return None


def effective_rule_problems(
    rules: Sequence[dict],
    produced: ProducedContexts,
    publisher: PublisherCommit | str = "the publisher's commit format was not read",
) -> list[str]:
    """The requirements in the default branch's EFFECTIVE rules (`rules/branches/{branch}`:
    repository, organization and enterprise rulesets alike) that a Forge PR cannot meet, or that
    doctor cannot verify: required status contexts no installed workflow job reports (or bound to
    an app other than GitHub Actions, which those jobs report through), required deployments,
    required workflows, a merge queue no workflow serves -- or a required context no workflow
    reports in the merge group, where the queue waits for it -- required signatures, update
    restrictions and code-scanning gates. Commit-metadata and branch-name pattern rules are
    evaluated against what the publisher pushes (`publisher`; a str: unreadable, so a TODO).
    A rule type doctor does not know is never silently compatible: it is reported."""
    queued = any(str(rule.get("type") or "") == "merge_queue" for rule in rules)
    problems: list[str] = []
    for rule in rules:
        kind = str(rule.get("type") or "")
        params = rule.get("parameters") if isinstance(rule.get("parameters"), dict) else {}
        source = (
            f"{rule.get('ruleset_source_type') or 'ruleset'} "
            f"{rule.get('ruleset_source') or ''} (ruleset {rule.get('ruleset_id', '?')})"
        ).replace("  ", " ")
        if kind == "required_status_checks":
            for check in params.get("required_status_checks") or []:
                context = str((check or {}).get("context") or "")
                if context and not context_produced(context, produced):
                    problems.append(
                        f"{source}: requires status check '{context}', which no installed "
                        "workflow job reports"
                    )
                elif context and (ambiguous := ambiguous_context(context, produced)):
                    problems.append(f"{source}: {ambiguous}")
                elif context and queued and context not in produced.merge_group:
                    problems.append(
                        f"{source}: requires status check '{context}' and a merge queue, but no "
                        "installed workflow job reports it on merge_group, so the queue never "
                        "receives it"
                    )
                elif context and _foreign_integration(check or {}):
                    # The installed workflows report it through GitHub Actions; a check bound
                    # to another app can never be satisfied by them (as for repository rulesets).
                    problems.append(
                        f"{source}: requires status check '{context}' from integration "
                        f"{check.get('integration_id')}, not GitHub Actions "
                        f"({GITHUB_ACTIONS_INTEGRATION_ID}): the installed workflow job can "
                        "never satisfy it"
                    )
                elif context == REQUIRED_CHECK and (check or {}).get("integration_id") is None:
                    # Forge's gate: unbound, any integration can satisfy it in ci.yml's stead.
                    problems.append(f"{source}: {_unbound_check_problem(context)}")
        elif kind == "required_deployments":
            envs = ", ".join(params.get("required_deployment_environments") or []) or "?"
            problems.append(
                f"{source}: requires deployments ({envs}) before merge; Forge deploys nothing"
            )
        elif kind == "workflows":
            paths = ", ".join(
                str((w or {}).get("path") or "?") for w in params.get("workflows") or []
            )
            problems.append(
                f"{source}: requires workflows ({paths or '?'}) to pass; verify they run on "
                "Forge PRs"
            )
        elif kind == "merge_queue":
            if not produced.merge_group_declared:
                problems.append(
                    f"{source}: requires a merge queue, but no installed workflow runs on "
                    "merge_group, so its required checks never report"
                )
        elif kind == "required_signatures":
            problems.append(
                f"{source}: requires signed commits; verify the Forge publisher's commits are "
                "verified (manual)"
            )
        elif kind == "update":
            problems.append(f"{source}: restricts updates; merging a Forge PR needs a bypass")
        elif kind == "code_scanning":
            tools = ", ".join(
                str((t or {}).get("tool") or "?") for t in params.get("code_scanning_tools") or []
            )
            problems.append(
                f"{source}: requires code scanning results ({tools or '?'}); verify a scanner "
                "reports on Forge PRs"
            )
        elif kind in _PATTERN_RULE_FIELDS:
            if found := _pattern_rule_problem(source, kind, params, publisher):
                problems.append(found)
        elif kind not in _COMPATIBLE_RULE_TYPES:
            problems.append(
                f"{source}: rule type {kind or '?'!r} is not one doctor understands → verify "
                "Forge's pull requests and pushes satisfy it"
            )
    return problems


PUSH_RULESETS_CHECK = "push rulesets admit every patch the Forge policy allows"
#: Enforcement values that never reject a push: off, or only reported (Enterprise "evaluate").
_NON_BLOCKING_ENFORCEMENT = ("disabled", "evaluate")


def _ruleset_name(summary: dict) -> str:
    return (
        f"{summary.get('source_type') or 'ruleset'} {summary.get('source') or ''} ruleset "
        f"{summary.get('name')!r} ({summary.get('id', '?')})"
    ).replace("  ", " ")


def _push_ruleset_candidate(summary: dict) -> bool:
    """A listed ruleset that may be an ACTIVE push ruleset (its detail decides when the summary
    does not name a target)."""
    return summary.get("enforcement") not in _NON_BLOCKING_ENFORCEMENT and summary.get(
        "target"
    ) in ("push", None)


def _forge_forbids_glob(pattern: object, forbidden: Sequence[str]) -> bool:
    """True only when a push ruleset's restricted path `pattern` is PROVABLY inside the Forge
    policy's `forbidden_paths`, so no patch Forge accepts can touch a path it restricts.

    Proof is textual: the same pattern is a forbidden path. Forge matches with fnmatch where `*`
    crosses `/` and case is folded, a superset of what the same glob matches on GitHub. Braces,
    classes and escapes are never proved (their syntax differs between the two matchers)."""
    if not isinstance(pattern, str) or not pattern or set(pattern) & set("{}[]\\!"):
        return False
    return pattern.casefold() in {f.casefold() for f in forbidden}


def push_ruleset_problems(
    rulesets: Sequence[tuple[dict, object]], forbidden: Sequence[str] | None
) -> list[str]:
    """What the ACTIVE `target: push` rulesets (repository, organization, enterprise) among
    `rulesets` (summary, detail) can do to a Forge patch. A push ruleset rejects any push --
    the implementer's or the publisher's -- whose commits add a restricted path or extension,
    a path over a length or a file over a size, whoever the actor (bypass lists are not
    evaluated). Only a path or extension restriction the Forge policy's `forbidden_paths`
    provably covers is safe; a length or size limit cannot be proved against the policy's
    caps (a patch's size is not its files' sizes), so it is a manual TODO, as is any rule
    doctor does not know and any ruleset that could not be read. `forbidden` None: the policy
    could not be read, so nothing is provable. Disabled and evaluate-mode rulesets never
    block."""
    problems: list[str] = []
    for summary, detail in rulesets:
        if not _push_ruleset_candidate(summary):
            continue
        name = _ruleset_name(summary)
        if not isinstance(detail, dict):
            problems.append(
                f"{name}: could not be read → check whether it is an active push ruleset "
                "restricting paths, extensions, path length or file size"
            )
            continue
        if detail.get("enforcement") in _NON_BLOCKING_ENFORCEMENT or detail.get("target") not in (
            "push",
        ):
            continue
        rules = detail.get("rules")
        if not isinstance(rules, list) or not all(isinstance(r, dict) for r in rules):
            problems.append(f"{name}: its push rules could not be read → verify them by hand")
            continue
        for rule in rules:
            kind = rule.get("type")
            params = rule.get("parameters") if isinstance(rule.get("parameters"), dict) else {}
            if kind == "file_path_restriction":
                paths = params.get("restricted_file_paths")
                unproved = (
                    [
                        str(p)
                        for p in paths
                        if forbidden is None or not _forge_forbids_glob(p, forbidden)
                    ]
                    if isinstance(paths, list)
                    else ["<unreadable>"]
                )
                if unproved:
                    problems.append(
                        f"{name}: restricts file paths {', '.join(map(repr, unproved))} that the "
                        "Forge policy's forbidden_paths does not provably exclude → add them to "
                        "forbidden_paths or verify no Forge patch touches them"
                    )
            elif kind == "file_extension_restriction":
                exts = params.get("restricted_file_extensions")
                unproved = []
                for ext in exts if isinstance(exts, list) else ["<unreadable>"]:
                    tail = ext.removeprefix("*") if isinstance(ext, str) else ""
                    covered = (
                        forbidden is not None
                        and tail.startswith(".")
                        and _forge_forbids_glob("*" + tail, forbidden)
                    )
                    if not covered:
                        unproved.append(str(ext))
                if unproved:
                    problems.append(
                        f"{name}: restricts file extensions {', '.join(map(repr, unproved))} "
                        "that the Forge policy's forbidden_paths does not provably exclude "
                        "(as `*<ext>`) → add them to forbidden_paths or verify no Forge patch "
                        "adds such a file"
                    )
            elif kind == "max_file_path_length":
                problems.append(
                    f"{name}: limits file paths to {params.get('max_file_path_length')!r} "
                    "characters, which the Forge policy does not cap → verify no Forge patch "
                    "adds a longer path"
                )
            elif kind == "max_file_size":
                problems.append(
                    f"{name}: limits files to {params.get('max_file_size')!r} MB; the Forge "
                    "policy caps patch size, not file size → verify no Forge patch pushes a "
                    "larger file"
                )
            else:
                problems.append(
                    f"{name}: push rule {kind!r} is not one doctor can evaluate → verify it "
                    "admits Forge's patches"
                )
    return problems


PUBLISHER_BRANCH_RULES_CHECK = "effective rules on the publisher's agent branches allow its push"
# Rule types that cannot reject the publisher's push of a NEW branch carrying one new commit:
# deleting or force-pushing it (it does neither), linear history (one commit on top of the
# base), tag names (it pushes no tags).
_PUBLISHER_PUSH_COMPATIBLE = frozenset(
    {"deletion", "non_fast_forward", "required_linear_history", "tag_name_pattern"}
)
_REF_GLOB_UNSUPPORTED = frozenset("?[]{}\\!")
_SENTINEL = "\x00"  # stands for a whole variable: only a wildcard can consume it


def _ref_glob_regexes(pattern: str) -> list[str] | None:
    """A ruleset ref pattern (fnmatch) as regexes under each reading doctor allows for `**`:
    any characters (`.*`), or Ruby's FNM_PATHNAME reading (`**/` zero or more directories,
    any other `**` like `*`). `*` never crosses `/`. None: syntax beyond literals, `*` and `**`
    (`?`, classes, braces, escapes, `***`), which doctor does not evaluate."""
    if any(ch in _REF_GLOB_UNSUPPORTED for ch in pattern) or "***" in pattern:
        return None
    readings = []
    for recursive in (".*", None):
        out, i = "", 0
        while i < len(pattern):
            if pattern.startswith("**", i):
                if recursive:
                    out, i = out + recursive, i + 2
                elif pattern.startswith("**/", i):
                    out, i = out + "(?:[^/]*/)*", i + 3
                else:
                    out, i = out + "[^/]*", i + 2
            elif pattern[i] == "*":
                out, i = out + "[^/]*", i + 1
            else:
                out, i = out + re.escape(pattern[i]), i + 1
        readings.append(out)
    return readings


def _ref_pattern_scope(pattern: str, ref: PublisherValue, default_branch: str) -> bool | None:
    """Does a ruleset ref pattern match EVERY ref of `ref`'s domain (True), NONE (False), or
    some but not others / unprovable (None)? `ref` is `refs/heads/<branch template>`, each
    variable a non-empty digit run. ALL: the pattern matches the template with each variable
    replaced by a sentinel only a wildcard can consume (a wildcard that takes the sentinel
    takes any digit run). NONE: a literal pattern outside the domain, a constant prefix or
    suffix that disagrees, or a sentinel mismatch with no digit in the pattern (then every
    digit run lies inside one wildcard's span and the sentinel would match too). Judged
    case-sensitively and case-insensitively; a disagreement is None."""
    verdicts = {
        _ref_pattern_scope_case(pattern, ref, default_branch, fold) for fold in (False, True)
    }
    return verdicts.pop() if len(verdicts) == 1 else None


def _ref_pattern_scope_case(
    pattern: str, ref: PublisherValue, default_branch: str, fold: bool
) -> bool | None:
    low = (lambda x: x.lower()) if fold else (lambda x: x)
    if pattern == "~ALL":
        return True
    # `~DEFAULT_BRANCH` names the branch itself, never a glob: a Git-valid name may hold `{`,
    # `}`, `!` (Codex 4242545214), which fnmatch would read as syntax. Match it literally.
    literal = pattern == "~DEFAULT_BRANCH"
    if literal:
        pattern = "refs/heads/" + default_branch
    pattern = low(pattern)
    ref = PublisherValue(tuple(low(lit) for lit in ref.literals), ref.variables)
    if not pattern.startswith("refs/"):
        return None  # GitHub may read it relative to refs/heads/: do not guess
    readings = [re.escape(pattern)] if literal else _ref_glob_regexes(pattern)
    if ref.constant:
        if readings is None:
            return None
        matched = {re.fullmatch(r, ref.example, re.DOTALL) is not None for r in readings}
        return matched.pop() if len(matched) == 1 else None
    if "*" not in pattern and readings is not None:
        # one literal ref: inside (a superset of) the domain it names ONE value of it
        return None if re.fullmatch(ref.loose_regex(), pattern) else False
    head, tail = ref.literals[0], ref.literals[-1]
    fixed_head = re.split(r"[*?\[\]{}\\!]", pattern, maxsplit=1)[0]
    fixed_tail = re.split(r"[*?\[\]{}\\!]", pattern)[-1]
    k = min(len(fixed_head), len(head))
    if fixed_head[:k] != head[:k]:
        return False  # the constant prefixes disagree
    if len(fixed_head) > len(head) and not fixed_head[len(head)].isdigit():
        return False  # the pattern wants a non-digit where every ref has a digit
    k = min(len(fixed_tail), len(tail))
    if fixed_tail[len(fixed_tail) - k :] != tail[len(tail) - k :]:
        return False
    if len(fixed_tail) > len(tail) and not fixed_tail[len(fixed_tail) - len(tail) - 1].isdigit():
        return False
    if readings is None:
        return None
    abstract = ref.render(lambda var: _SENTINEL)
    matched = {re.fullmatch(r, abstract, re.DOTALL) is not None for r in readings}
    if matched == {True}:
        return True
    if matched == {False} and not any(ch.isdigit() for ch in pattern):
        return False
    return None


def _ruleset_branch_scope(
    conditions: object, ref: PublisherValue, default_branch: str
) -> bool | None:
    """Does a ruleset's `ref_name` condition cover every ref of the domain (True), none
    (False), or depend on the run-time numbers (None)? Conditions on anything else
    (repository name, id or property) are the same for every agent branch."""
    ref_name = conditions.get("ref_name") if isinstance(conditions, dict) else None
    if ref_name is None:
        return True  # not branch-scoped: the same for every agent branch
    if not isinstance(ref_name, dict):
        return None

    def any_of(key: str) -> bool | None:
        listed = ref_name.get(key) or []
        if not isinstance(listed, list) or not all(isinstance(p, str) for p in listed):
            return None
        scopes = [_ref_pattern_scope(p, ref, default_branch) for p in listed]
        if True in scopes:
            return True
        return False if all(x is False for x in scopes) else None

    include, exclude = any_of("include"), any_of("exclude")
    if exclude is True or include is False:
        return False
    if include is True and exclude is False:
        return True
    return None


def publisher_branch_scope_problems(
    rulesets: Sequence[tuple[dict, object]],
    branch: PublisherValue,
    default_branch: str,
    *,
    who: str = "the publisher",
    namespace: str = "agent/**",
) -> list[str]:
    """The rulesets (summary, detail) whose branch targeting matches SOME of the publisher's
    branches but not others -- `agent/issue-<N>-<RUN>` for every issue and run -- or cannot be
    proven either way. The effective rules of one sample branch speak for every branch only
    when no such ruleset exists; each one is a manual TODO. A detail that could not be read
    (not a dict) is one too."""
    ref = PublisherValue(
        ("refs/heads/" + branch.literals[0], *branch.literals[1:]), branch.variables
    )
    problems: list[str] = []
    for summary, detail in rulesets:
        name = (
            f"{summary.get('source_type') or 'ruleset'} {summary.get('source') or ''} ruleset "
            f"{summary.get('name')!r} ({summary.get('id', '?')})"
        ).replace("  ", " ")
        if summary.get("enforcement") == "disabled" or summary.get("target") in ("tag", "push"):
            continue
        if not isinstance(detail, dict):
            problems.append(
                f"{name}: could not be read → check whether it targets {namespace} branches"
            )
            continue
        if detail.get("enforcement") == "disabled" or detail.get("target") in ("tag", "push"):
            continue
        if _ruleset_branch_scope(detail.get("conditions"), ref, default_branch) is None:
            problems.append(
                f"{name}: targets some of {who}'s branches {branch.template!r} but "
                "doctor cannot prove it targets all or none of them (it depends on the issue or "
                f"run number) → verify it does not block {who}'s push"
            )
    return problems


def _pusher_label(pusher: PublisherPusher | str) -> str:
    if isinstance(pusher, PublisherPusher):
        return pusher.label
    return f"an actor doctor could not determine ({pusher})"


def publisher_branch_rule_problems(
    rules: Sequence[dict],
    publisher: PublisherCommit,
    branch: str,
    pusher: PublisherPusher | str = "the pushing credential was not read",
) -> list[str]:
    """What in the EFFECTIVE rules of `branch` (`rules/branches/{branch}`: one value of the
    `agent/...` branch the Actions publisher creates and pushes) can reject that push: creating
    the branch, updating it, required status checks that are enforced on creation, signatures,
    pull requests, deployments, workflows, code scanning, a merge queue -- and the
    commit-metadata and branch-name pattern rules, evaluated against what the publisher pushes.
    A rule type doctor does not know is never silently compatible: it is reported."""
    problems: list[str] = []
    for rule in rules:
        kind = str(rule.get("type") or "")
        params = rule.get("parameters") if isinstance(rule.get("parameters"), dict) else {}
        source = (
            f"{rule.get('ruleset_source_type') or 'ruleset'} "
            f"{rule.get('ruleset_source') or ''} (ruleset {rule.get('ruleset_id', '?')})"
        ).replace("  ", " ")
        if kind in _PUBLISHER_PUSH_COMPATIBLE:
            continue
        if kind in _PATTERN_RULE_FIELDS:
            if found := _pattern_rule_problem(source, kind, params, publisher):
                problems.append(f"{found} [on {branch}]")
        elif kind == "creation":
            problems.append(
                f"{source}: restricts creating {branch}, which the publisher's push creates → "
                f"exempt agent/** or give the pushing actor a bypass: {_pusher_label(pusher)}"
            )
        elif kind == "update":
            problems.append(
                f"{source}: restricts updates to {branch}, which the publisher pushes → exempt "
                f"agent/** or give the pushing actor a bypass: {_pusher_label(pusher)}"
            )
        elif kind == "required_status_checks" and params.get("do_not_enforce_on_create") is True:
            continue  # the publisher only ever creates its branch
        else:
            problems.append(
                f"{source}: rule {kind or '?'!r} applies to {branch} and can reject the "
                "publisher's push → exempt agent/** or verify the push satisfies it"
            )
    return problems


#: Classic branch-protection rules of the repository, with what each one requires (GraphQL:
#: `rules/branches` and `branches/{b}/protection` show only rulesets / one existing branch, so a
#: wildcard rule over agent/** that does not exist yet is visible only here).
CLASSIC_PROTECTION_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    branchProtectionRules(first: 100) {
      pageInfo { hasNextPage }
      nodes {
        pattern
        requiresStatusChecks
        requiredStatusCheckContexts
        requiresCommitSignatures
        requiresApprovingReviews
        requiresCodeOwnerReviews
        requiresDeployments
        restrictsPushes
        blocksCreations
        lockBranch
        pushAllowances(first: 100) {
          pageInfo { hasNextPage }
          nodes { actor { __typename ... on App { slug } } }
        }
      }
    }
  }
}
"""


def classic_publisher_branch_problems(
    answer: object,
    branch: PublisherValue,
    pusher: PublisherPusher | str = "the pushing credential was not read",
    *,
    who: str = "the publisher",
    namespace: str = "agent/**",
) -> list[str]:
    """What the repository's CLASSIC branch-protection rules do to the publisher's push of a
    new `agent/issue-<N>-<RUN>` branch (Codex 4239397979). A rule's pattern (fnmatch with
    FNM_PATHNAME, relative to refs/heads/) is judged over the whole branch domain, like a
    ruleset's: one matching every agent branch is reported for each requirement the push cannot
    meet (required checks or reviews, signatures, deployments, a push allowlist without the
    actor that pushes, a lock); one matching some of them, or one doctor cannot evaluate, is a
    manual TODO; one matching none is ignored. An unreadable answer is a manual TODO.

    The allowlist is judged against `pusher` (`publisher_push_actor`): the credential that
    actually runs `git push` -- the GitHub Actions app for the checkout's `github.token`, NOT
    the Publisher App, whose token only opens the pull request (Codex 4239510543). An unknown
    pusher (a str) never passes an allowlist."""
    data = answer.get("data") if isinstance(answer, dict) else None
    repo = data.get("repository") if isinstance(data, dict) else None
    rules = repo.get("branchProtectionRules") if isinstance(repo, dict) else None
    nodes = rules.get("nodes") if isinstance(rules, dict) else None
    if not isinstance(nodes, list) or (isinstance(answer, dict) and answer.get("errors")):
        why = answer.why if isinstance(answer, Unreadable) else "unreadable answer"
        return [
            f"could not read the classic branch-protection rules ({why}; GraphQL "
            f"repository.branchProtectionRules) → check that no rule pattern matches {namespace} "
            "branches"
        ]
    problems: list[str] = []
    if (rules.get("pageInfo") or {}).get("hasNextPage"):
        problems.append(
            "more than 100 classic branch-protection rules: the rest were not inspected → check "
            f"that none matches {namespace} branches"
        )
    ref = PublisherValue(
        ("refs/heads/" + branch.literals[0], *branch.literals[1:]), branch.variables
    )
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("pattern"), str):
            problems.append("a classic branch-protection rule could not be read → check it")
            continue
        pattern = node["pattern"]
        name = f"classic branch protection {pattern!r}"
        scope = _ref_pattern_scope("refs/heads/" + pattern, ref, "")
        if scope is False:
            continue
        needs = []
        if node.get("requiresStatusChecks"):
            contexts = ", ".join(str(c) for c in node.get("requiredStatusCheckContexts") or [])
            needs.append(f"status checks{f' ({contexts})' if contexts else ''}")
        if node.get("requiresApprovingReviews") or node.get("requiresCodeOwnerReviews"):
            needs.append("pull-request reviews (no direct push)")
        if node.get("requiresCommitSignatures"):
            needs.append("signed commits")
        if node.get("requiresDeployments"):
            needs.append("successful deployments")
        if node.get("lockBranch"):
            needs.append("a locked branch")
        if node.get("restrictsPushes") or node.get("blocksCreations"):
            allowances = node.get("pushAllowances")
            actors = allowances.get("nodes") if isinstance(allowances, dict) else None
            readable = isinstance(actors, list) and not (
                (allowances.get("pageInfo") or {}).get("hasNextPage")
            )
            if not readable:
                needs.append("a push/creation allowlist that could not be read")
            elif not isinstance(pusher, PublisherPusher):
                needs.append(
                    f"a push/creation allowlist, and which actor {who} pushes as could "
                    f"not be determined ({pusher})"
                )
            elif not any(
                isinstance(a, dict)
                and isinstance(a.get("actor"), dict)
                and a["actor"].get("__typename") == "App"
                and str(a["actor"].get("slug") or "").lower() == pusher.app_slug
                for a in actors
            ):
                needs.append(f"a push/creation allowlist without the pushing actor {pusher.label}")
        if scope is None:
            problems.append(
                f"{name}: matches some of {who}'s branches {branch.template!r} or "
                f"cannot be evaluated → verify it does not block {who}'s push"
                + (f" (it requires {'; '.join(needs)})" if needs else "")
            )
        elif needs:
            problems.append(
                f"{name}: matches every one of {who}'s branches {branch.template!r} and "
                f"requires {'; '.join(needs)}, which {who}'s push cannot meet → exempt {namespace} "
                "from the rule or narrow its pattern"
            )
    return problems


# ---- Codex 4241196640: in cloud-routine mode the routine itself pushes
# `forge/issue-<N>-<stamp>` with the repository owner's credentials (a user, not an App). A
# ruleset or classic protection rule
# targeting that namespace is invisible in the default branch's rules, so the same branch-domain
# checks run against the routine's branch; the actor is a user, so an App allowlist or bypass
# cannot be matched against it: a push restriction is an owner TODO, as is any rule doctor
# cannot evaluate. The routine's commit message and identity are the owner's: unknowable here.
CLOUD_BRANCH_RULES_CHECK = (
    "effective rules on the cloud routine's forge/issue-<N>-<stamp> branches allow its push"
)
#: `forge/issue-<n>-<stamp>`: a run-specific name (Codex 4241532615), so a branch left behind by
#: a run whose PR could not be opened never makes a later run's non-forced push fail.
CLOUD_ROUTINE_BRANCH = PublisherValue(("forge/issue-", "-", ""), ("ISSUE_NUMBER", "RUN_STAMP"))
CLOUD_ROUTINE_WHO = "the cloud routine"
CLOUD_ROUTINE_NAMESPACE = "forge/**"
CLOUD_ROUTINE_PUSHER = (
    "the repository owner's own credentials -- a user account, which doctor cannot match "
    "against an allowlist or bypass"
)


def _branch_ruleset_details(
    gh: GhRunner, project_id: str, listed: list[dict]
) -> list[tuple[dict, object]]:
    """(summary, detail) of every listed ruleset that can target branches (not disabled, not a
    tag or push ruleset); a detail that cannot be read is whatever `_safe_json` returned."""
    details: list[tuple[dict, object]] = []
    for summary in listed:
        skip = summary.get("enforcement") == "disabled" or summary.get("target") in ("tag", "push")
        detail = (
            None
            if skip
            else _safe_json(gh, ["api", f"repos/{project_id}/rulesets/{summary.get('id')}"])
        )
        details.append((summary, detail))
    return details


def _cloud_branch_rules_check(
    gh: GhRunner, project_id: str, listed: object, effective_branch: str
) -> Check:
    """CLOUD_BRANCH_RULES_CHECK: rulesets (branch targeting over every issue number and stamp),
    classic protection patterns and the effective rules of one `forge/issue-<N>-<stamp>` branch,
    judged for the cloud routine's push. Anything unreadable is a manual TODO."""
    problems: list[str] = []
    who, namespace = CLOUD_ROUTINE_WHO, CLOUD_ROUTINE_NAMESPACE
    if isinstance(listed, Unreadable):
        problems.append(
            f"could not list the rulesets that apply to {project_id} ({listed.why}) → check "
            f"that no org or enterprise ruleset targets {namespace} branches"
        )
    else:
        problems += publisher_branch_scope_problems(
            _branch_ruleset_details(gh, project_id, listed),
            CLOUD_ROUTINE_BRANCH,
            effective_branch,
            who=who,
            namespace=namespace,
        )
    owner, _, name = project_id.partition("/")
    problems += classic_publisher_branch_problems(
        _safe_json(
            gh,
            [
                "api",
                "graphql",
                "-f",
                f"query={CLASSIC_PROTECTION_QUERY}",
                "-F",
                f"owner={owner}",
                "-F",
                f"name={name}",
            ],
        ),
        CLOUD_ROUTINE_BRANCH,
        CLOUD_ROUTINE_PUSHER,
        who=who,
        namespace=namespace,
    )
    branch = CLOUD_ROUTINE_BRANCH.example
    rules = _paged_items(gh, f"repos/{project_id}/rules/branches/{_q(branch)}")
    if isinstance(rules, Unreadable):
        problems.append(
            f"could not read the rules that apply to {branch} "
            f"(GET repos/{project_id}/rules/branches/{{branch}}) → check org and enterprise "
            f"rulesets targeting {namespace} by hand"
        )
    else:
        problems += cloud_branch_rule_problems(rules, branch)
    problems = list(dict.fromkeys(problems))
    return Check(
        CLOUD_BRANCH_RULES_CHECK,
        not problems,
        "; ".join(problems)
        if problems
        else f"no rule blocks the cloud routine's push to any {CLOUD_ROUTINE_BRANCH.template}",
        manual=True,
    )


def cloud_branch_rule_problems(rules: Sequence[dict], branch: str) -> list[str]:
    """What in the EFFECTIVE rules of `branch` (one `forge/issue-<N>-<stamp>` branch the routine
    creates and pushes as the owner) can reject that push. Only rules that cannot reject a new
    branch carrying new commits pass, plus a branch-name pattern proven for every issue number
    and stamp and required checks not enforced on creation; everything else is reported, and the
    commit-metadata patterns (the owner's own message and identity) are owner TODOs."""
    problems: list[str] = []
    for rule in rules:
        kind = str(rule.get("type") or "")
        params = rule.get("parameters") if isinstance(rule.get("parameters"), dict) else {}
        source = (
            f"{rule.get('ruleset_source_type') or 'ruleset'} "
            f"{rule.get('ruleset_source') or ''} (ruleset {rule.get('ruleset_id', '?')})"
        ).replace("  ", " ")
        fix = f"exempt {CLOUD_ROUTINE_NAMESPACE} or give the repository owner a bypass"
        if kind in _PUBLISHER_PUSH_COMPATIBLE:
            continue
        if kind == "required_status_checks" and params.get("do_not_enforce_on_create") is True:
            continue  # the routine creates its branch
        if kind == "branch_name_pattern":
            shown = f"{params.get('operator')} {params.get('pattern')!r}"
            verdict, witness = _value_verdict(params, CLOUD_ROUTINE_BRANCH)
            if verdict is True:
                continue
            problems.append(
                f"{source}: requires the branch name to match {shown}; the cloud routine's "
                + (
                    f"branch {witness!r} does not, so its push is rejected"
                    if verdict is False
                    else f"branch {CLOUD_ROUTINE_BRANCH.template!r} cannot be proven to for "
                    "every issue number"
                )
                + f" → {fix}"
            )
        elif kind in _PATTERN_RULE_FIELDS:
            what = _PATTERN_RULE_FIELDS[kind][1]
            problems.append(
                f"{source}: restricts the {what} ({params.get('operator')} "
                f"{params.get('pattern')!r}) of what is pushed to {branch}; the cloud routine "
                f"commits with the owner's own {what.split()[-1]}, which doctor cannot read → "
                "verify the routine's commits satisfy it"
            )
        elif kind == "creation":
            problems.append(
                f"{source}: restricts creating {branch}, which the cloud routine's push creates "
                f"as {CLOUD_ROUTINE_PUSHER} → {fix}"
            )
        elif kind == "update":
            problems.append(
                f"{source}: restricts updates to {branch}, which the cloud routine pushes as "
                f"{CLOUD_ROUTINE_PUSHER} → {fix}"
            )
        elif kind == "required_signatures":
            problems.append(
                f"{source}: requires signed commits on {branch}; the cloud routine's commits "
                "must be signed with a key verified for the owner → configure signing for the "
                f"routine or {fix}"
            )
        else:
            problems.append(
                f"{source}: rule {kind or '?'!r} applies to {branch} and can reject the cloud "
                f"routine's push → {fix}, or verify the push satisfies it"
            )
    return problems


LEGACY_PROTECTION_CHECK = "classic branch protection on the default branch fits Forge"


def _enabled(value: object) -> bool:
    """A classic-protection toggle (`{"enabled": true}`) is on."""
    return isinstance(value, dict) and value.get("enabled") is True


def legacy_protection_problems(
    protection: dict, produced: ProducedContexts, merge_queue: bool | None = False
) -> list[str]:
    """What a CLASSIC branch-protection rule (`branches/{branch}/protection`, enforced alongside
    rulesets) requires that a Forge PR cannot meet, by the same rules as
    `effective_rule_problems`: required status contexts no installed PR workflow job reports, or
    bound to an app other than GitHub Actions; required signatures; push restrictions; a locked
    branch. Linear history, reviews and conversation resolution are compatible.

    `merge_queue` is the EFFECTIVE queue state of the branch (a `merge_queue` rule in its
    effective rules; None: unknown). A queued PR merges only once every required check --
    classic ones too -- reports in the merge group, so with a queue each classic context must
    also be produced on `merge_group`; with the state unknown, that cannot be verified."""
    problems: list[str] = []
    checks = protection.get("required_status_checks")
    if isinstance(checks, dict):
        bound: dict[str, set[object]] = {}
        for item in checks.get("checks") or []:
            if isinstance(item, dict) and item.get("context"):
                bound.setdefault(str(item["context"]), set()).add(item.get("app_id"))
        for context in checks.get("contexts") or []:
            bound.setdefault(str(context), set())
        for context, apps in sorted(bound.items()):
            if not context_produced(context, produced):
                problems.append(
                    f"requires status check '{context}', which no installed pull-request "
                    "workflow job reports"
                )
            elif ambiguous := ambiguous_context(context, produced):
                problems.append(ambiguous)
            elif merge_queue and context not in produced.merge_group:
                problems.append(
                    f"requires status check '{context}' and the branch has a merge queue, but "
                    "no installed workflow job reports it on merge_group, so the queue never "
                    "receives it"
                )
            elif merge_queue is None and context not in produced.merge_group:
                problems.append(
                    f"requires status check '{context}', and whether a merge queue also waits "
                    "for it cannot be verified (the effective rules are unreadable) and no "
                    "installed workflow job reports it on merge_group"
                )
            elif any(app not in (None, -1, GITHUB_ACTIONS_INTEGRATION_ID) for app in apps):
                foreign = ", ".join(sorted(str(a) for a in apps if a not in (None, -1)))
                problems.append(
                    f"requires status check '{context}' from app {foreign}, not GitHub Actions "
                    f"({GITHUB_ACTIONS_INTEGRATION_ID}): the installed workflow job can never "
                    "satisfy it"
                )
            elif context == REQUIRED_CHECK and apps != {GITHUB_ACTIONS_INTEGRATION_ID}:
                # Forge's gate must be bound to GitHub Actions: a `checks[]` entry with no app
                # (null / -1: any source), or only a bare `contexts` entry whose binding cannot
                # be read, lets any integration satisfy it in ci.yml's stead.
                problems.append(_unbound_check_problem(context))
    if _enabled(protection.get("required_signatures")):
        problems.append(
            "requires signed commits; verify the Forge publisher's commits are verified (manual)"
        )
    if isinstance(protection.get("restrictions"), dict):
        problems.append(
            "restricts who can push; merging a Forge PR needs the publisher app in the allow list"
        )
    if _enabled(protection.get("lock_branch")):
        problems.append("locks the branch (read-only): no Forge PR can merge")
    return problems


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
#: This list MUST track GitHub's CURRENT hosted-runner labels: a retired image's label (e.g.
#: `macos-13`, `macos-13-large`, `macos-13-xlarge`, retired 2025-12-04) is accepted by onboard and
#: counted as available by doctor, yet its job never starts. Remove labels as GitHub retires them
#: (github.blog changelog, "... runner image is closing down"). Last reviewed: 2026-10-10.
GITHUB_HOSTED_LABELS: dict[str, str] = {
    **dict.fromkeys(
        (
            "ubuntu-latest",
            "ubuntu-26.04",
            "ubuntu-24.04",
            "ubuntu-22.04",
            "ubuntu-26.04-arm",
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
            "macos-15-intel",
            "macos-latest-large",
            "macos-15-large",
            "macos-14-large",
            "macos-latest-xlarge",
            "macos-26-xlarge",
            "macos-15-xlarge",
            "macos-14-xlarge",
        ),
        "macos",
    ),
    **dict.fromkeys(
        ("windows-latest", "windows-2025", "windows-2022", "windows-2019", "windows-11-arm"),
        "windows",
    ),
}
#: Hosted labels that are LARGER runners, not standard images (Codex 4239397961): they need an
#: eligible Team/Enterprise plan with the runner made available to the repository, so a label
#: alone proves nothing. Doctor passes one only when a registered online runner of the
#: repository carries it; otherwise it is an owner TODO, never an automatic pass.
GITHUB_LARGER_RUNNER_LABELS = frozenset(
    label for label in GITHUB_HOSTED_LABELS if label.endswith(("-large", "-xlarge"))
)
#: Hosted labels with a hard per-job time limit, in minutes (Codex 4239397966): `ubuntu-slim`
#: stops every job at 15 minutes. Every Forge workload may run longer -- ci.yml's test job
#: allows 30 minutes, reusable-implement.yml 60 and 120 -- so such a label is rejected for
#: `--runs-on` and `--ci-runs-on` alike, and doctor fails a workflow that targets one.
SHORT_JOB_LIMIT_LABELS: dict[str, int] = {"ubuntu-slim": 15}

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
        steps = (_workflow_jobs(caller).get("preflight") or {}).get("steps")
        for step in steps if isinstance(steps, list) else ():
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
    """Exactly one label, and one GitHub actually hosts (any OS: a fresh VM either way). Says
    nothing about AVAILABILITY: a larger runner may not be provisioned (`_standard_hosted`)."""
    return len(labels) == 1 and next(iter(labels)) in GITHUB_HOSTED_LABELS


def _standard_hosted(labels) -> bool:
    """One standard GitHub-hosted label: available to every repository with no setup. Not a
    larger runner (plan and provisioning dependent) and not a label whose hard job-time limit is
    below the Forge workloads' timeouts."""
    names = {str(x) for x in labels}
    return (
        _github_hosted(names)
        and not names & GITHUB_LARGER_RUNNER_LABELS
        and not names & set(SHORT_JOB_LIMIT_LABELS)
    )


def _short_job_labels(labels) -> list[str]:
    """Labels whose hard job-time limit is below what the Forge workflows allow a job."""
    return sorted(str(x) for x in labels if str(x) in SHORT_JOB_LIMIT_LABELS)


SHORT_JOB_UNSUPPORTED = (
    "ubuntu-slim stops every job after 15 minutes, below the Forge workflows' own timeouts "
    "(ci.yml test 30 min, reusable-implement.yml 60/120 min): use ubuntu-latest or another "
    "standard label"
)


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


LINUX_RUNNER_REQUIRED = (
    "the Forge plan and implementation workflows run on Linux only: reusable-implement.yml "
    "installs bubblewrap for Claude Code with apt-get, and reusable-plan.yml hashes the checkout "
    "with GNU `sha256sum`, neither of which macOS has; use a GitHub-hosted ubuntu-* label or "
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
    Anything else -- self-hosted, a larger runner not proven provisioned, a typo, an unresolved
    expression -- may queue forever; a short-limit label (`ubuntu-slim`) kills the job. A
    Windows target, or a match only on a Windows runner, is never available: the generated jobs
    run bash with Unix paths."""
    if _windows_labels(labels) or _short_job_labels(labels):
        return False
    if _standard_hosted(labels):
        return True
    # A larger hosted runner (or self-hosted labels): only a registered online runner proves it.
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


_INPUT_REF = re.compile(r"^\$\{\{\s*inputs\.([A-Za-z0-9_-]+)\s*\}\}$")
_FROMJSON_INPUT_REF = re.compile(
    r"^\$\{\{\s*fromJSON\(\s*inputs\.([A-Za-z0-9_-]+)\s*\)\s*\}\}$", re.IGNORECASE
)
_REMOTE_REUSABLE = re.compile(r"([^/\s]+/[^/\s]+)/\.github/workflows/([^@\s/]+)@(\S+)")

#: A job's runner target: its labels, or why they cannot be determined (a str).
RunnerTarget = set[str] | str
#: (callee file name, pinned SHA) -> the platform's reusable workflow parsed at that SHA (None:
#: unreadable). Runner diagnosis reads the PINNED callee, never a list of trusted file names.
CalleeLoader = Callable[[str, str], object]


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


def _pinned_callee_targets(
    where: str,
    name: str,
    called: dict,
    call_inputs: dict[str, str | None],
    platform_repository: str,
    load_callee: CalleeLoader | None,
    seen: tuple[object, ...],
) -> dict[str, RunnerTarget]:
    """Where each job of a platform reusable workflow, as fetched at its pinned SHA, runs: its
    `runs-on` with the call's inputs substituted (the caller's `with:` over the callee's
    defaults). A nested pinned platform call is followed the same way; anything else is a str."""
    jobs = called.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        return {where: f"{name} at its pin has no readable jobs"}
    targets: dict[str, RunnerTarget] = {}
    for job_name, job in jobs.items():
        at = f"{where} > {name}:{job_name}"
        if not isinstance(job, dict):
            targets[at] = "unreadable job"
        elif "uses" in job:
            targets.update(
                _remote_call_targets(at, job, platform_repository, call_inputs, load_callee, seen)
            )
        elif "runs-on" in job:
            labels = _resolved_labels(job["runs-on"], call_inputs)
            targets[at] = (
                labels if labels is not None else _unresolved_target(job["runs-on"], "runs-on")
            )
        else:
            targets[at] = "no runs-on"
    return targets


def _remote_call_targets(
    where: str,
    job: dict,
    platform_repository: str,
    inputs: dict[str, str | None] | None,
    load_callee: CalleeLoader | None,
    seen: tuple[object, ...],
) -> dict[str, RunnerTarget]:
    """Runner targets of a job that calls a REMOTE reusable workflow: a Forge platform workflow
    pinned to a full SHA is read at that SHA (`load_callee`) and its jobs analyzed; anything
    else -- a third-party workflow, a branch/tag ref, a callee that cannot be fetched -- fails
    closed as a str."""
    uses = str(job.get("uses", "")).strip()
    passed = job.get("with") if isinstance(job.get("with"), dict) else {}
    found = _REMOTE_REUSABLE.fullmatch(uses)
    if not (found and found.group(1) == platform_repository and _SHA.fullmatch(found.group(3))):
        return {where: f"calls {uses}: its runners cannot be verified from this repository"}
    name, sha = found.group(2), found.group(3)
    if (name, sha) in seen:
        return {where: f"reusable workflow cycle through {name}@{sha[:12]}"}
    called = load_callee(name, sha) if load_callee is not None else None
    if not isinstance(called, dict):
        return {
            where: f"calls {name}@{sha[:12]}, which could not be read at that pin: its runners "
            "cannot be verified"
        }
    return _pinned_callee_targets(
        where,
        name,
        called,
        _call_inputs(called, passed, inputs),
        platform_repository,
        load_callee,
        (*seen, (name, sha)),
    )


def _reusable_call_targets(
    base: Path,
    where: str,
    job: dict,
    platform_repository: str,
    inputs: dict[str, str | None] | None,
    seen: tuple[object, ...],
    load_callee: CalleeLoader | None = None,
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
            load_callee,
        )
        return {f"{where} > {k}": v for k, v in nested.items()}
    return _remote_call_targets(where, job, platform_repository, inputs, load_callee, seen)


def platform_callee_loader(
    gh: GhRunner, platform_repository: str, prefetched: dict[tuple[str, str], object]
) -> CalleeLoader:
    """A cached `CalleeLoader` over the platform's contents API: a callee doctor already fetched
    at a pin is reused; any other (file, SHA) is read once. Unreadable -> None (fails closed)."""
    cache = dict(prefetched)

    def load(name: str, sha: str) -> object:
        if (name, sha) not in cache:
            doc = None
            if _WORKFLOW_FILE.fullmatch(name):
                raw = _decoded_blob(
                    _safe_json(
                        gh,
                        [
                            "api",
                            f"repos/{platform_repository}/contents/.github/workflows/"
                            f"{name}?ref={_q(sha)}",
                        ],
                    )
                )
                if raw is not None:
                    import yaml  # deferred: the CLI must import without site dependencies

                    try:
                        doc = yaml.safe_load(raw.decode("utf-8", "replace"))
                    except yaml.YAMLError:
                        doc = None
            cache[(name, sha)] = doc
        return cache[(name, sha)]

    return load


def _job_runner_targets(
    base: Path,
    path: Path,
    platform_repository: str,
    inputs: dict[str, str | None] | None = None,
    seen: tuple[object, ...] = (),
    load_callee: CalleeLoader | None = None,
) -> dict[str, RunnerTarget]:
    """`file:job` -> where each job of a workflow runs, following local reusable workflows
    (`./.github/workflows/x.yml`) recursively with their `with:` inputs substituted, and Forge
    platform reusable workflows pinned to a SHA through the jobs of the callee AS FETCHED at
    that SHA (`load_callee`), inputs substituted likewise. Anything else -- a third-party
    reusable workflow, a callee that cannot be fetched, an expression that is not a resolvable
    input, a runner group, a cycle -- is a str saying why it cannot be proven."""
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
                _reusable_call_targets(
                    base, where, job, platform_repository, inputs, seen, load_callee
                )
            )
        elif "runs-on" in job:
            labels = _resolved_labels(job["runs-on"], inputs)
            targets[where] = (
                labels if labels is not None else _unresolved_target(job["runs-on"], "runs-on")
            )
        else:
            targets[where] = "no runs-on"
    return targets


def _fork_code_trigger(trigger: str, merge_group_only: bool = False) -> bool:
    """Does a workflow triggered by `trigger` run code a fork pull request supplied? Every
    `pull_request*` event, and `merge_group` (Codex 4240421149): a merge group's SHA contains the
    queued pull requests' changes, fork ones included. `merge_group_only` counts only the latter:
    the fork-PR workflow policy gates `pull_request*` runs, never a merge queue's."""
    if trigger == "merge_group":
        return True
    return not merge_group_only and trigger.startswith("pull_request")


def _pull_request_off_hosted(
    base: Path,
    platform_repository: str,
    load_callee: CalleeLoader | None = None,
    merge_group_only: bool = False,
) -> list[str]:
    """Every job a workflow that runs fork pull-request code (`_fork_code_trigger`: any
    `pull_request*` event, or `merge_group`) runs that is not PROVEN to run on a single
    GitHub-hosted label -- including the jobs of the reusable workflows it calls
    (`_job_runner_targets`). On a fork-exposed repository those jobs run fork code, so an
    unresolvable target fails closed."""
    found = []
    workflows = base / ".github/workflows"
    paths = sorted([*workflows.glob("*.yml"), *workflows.glob("*.yaml")])
    for path in paths:
        doc = _workflow_doc(path)
        if doc is None or not any(_fork_code_trigger(t, merge_group_only) for t in _triggers(doc)):
            continue
        targets = _job_runner_targets(base, path, platform_repository, load_callee=load_callee)
        for where, target in targets.items():
            if isinstance(target, str):
                found.append(f"{where} ({target})")
            elif not _github_hosted(target):
                found.append(f"{where} {sorted(target)}")
    return found


#: How deep doctor follows nested platform reusable workflows (GitHub itself allows ten levels
#: of nesting, top-level caller included); a deeper graph fails closed. The generated caller in
#: the consumer repository is level 1 and is not part of the graph, so the roots (the platform
#: workflows it calls) are level `_CALLEE_GRAPH_ROOT_LEVEL`: at most nine called levels pass.
_CALLEE_GRAPH_MAX_DEPTH = 10
_CALLEE_GRAPH_ROOT_LEVEL = 2


@dataclass
class CalleeGraph:
    """Every platform reusable workflow reachable from the top-level callees, each as parsed at
    its pinned SHA (`docs`, roots included), every nested reusable call found in them (`calls`:
    where, the calling job, callee file, its parsed doc or None when it could not be read) and
    why any part of the graph could not be inspected (`uninspectable`: fail closed). `unfollowed`
    is the subset that is a nested CALL doctor could not follow (its contract is unverified)."""

    docs: dict[tuple[str, str], dict] = field(default_factory=dict)
    calls: list[tuple[str, dict, str, object]] = field(default_factory=list)
    uninspectable: list[str] = field(default_factory=list)
    unfollowed: list[str] = field(default_factory=list)
    #: (file, sha) -> the pinned platform workflows it calls: what `reachable` walks.
    edges: dict[tuple[str, str], set[tuple[str, str]]] = field(default_factory=dict)

    def reachable(self, roots) -> set[tuple[str, str]]:
        """Every workflow (file, sha) in the trees of `roots`, the roots included."""
        seen: set[tuple[str, str]] = set()
        todo = list(roots)
        while todo:
            node = todo.pop()
            if node not in seen:
                seen.add(node)
                todo += self.edges.get(node, ())
        return seen


#: GitHub runs at most 50 unique reusable workflows from one top-level workflow file, nested
#: trees included; a larger tree is rejected when the caller runs (Codex 4239397986).
_REUSABLE_WORKFLOWS_PER_CALLER = 50


def platform_callee_graph(
    roots: dict[str, object],
    root_ref: str,
    platform_repository: str,
    load_callee: CalleeLoader,
    max_depth: int = _CALLEE_GRAPH_MAX_DEPTH,
) -> CalleeGraph:
    """Walk the reusable-workflow calls of the platform workflows `roots` (file -> parsed doc at
    `root_ref`) transitively: a job calling a platform workflow pinned to a full SHA is loaded
    at that SHA (`load_callee`) and walked in turn. A callee that cannot be read, a call doctor
    cannot follow (a branch/tag ref, another repository's workflow, a `./` call inside the
    platform), a cycle, or a graph deeper than `max_depth` is recorded as uninspectable. Depth
    counts the generated top-level caller (outside the graph) as level 1, so roots are level 2:
    GitHub's limit of ten levels includes that caller."""
    graph = CalleeGraph()
    stack: list[tuple[str, str, dict, int, tuple[tuple[str, str], ...]]] = []
    # The deepest level each workflow has been expanded at. A workflow reached again along a
    # DEEPER path is expanded again (Codex 4239397976): GitHub's nesting limit applies to every
    # path, and one shared callee met shallow first used to hide an edge past level ten behind
    # it. Documents are loaded once (`graph.docs`); a re-expansion at a depth no deeper than one
    # already done can find nothing new and is skipped, so the walk stays bounded by
    # files x max_depth.
    expanded: dict[tuple[str, str], int] = {}
    seen_calls: set[tuple[str, tuple[str, str]]] = set()
    for name, doc in roots.items():
        if isinstance(doc, dict):
            graph.docs[(name, root_ref)] = doc
            stack.append((name, root_ref, doc, _CALLEE_GRAPH_ROOT_LEVEL, ((name, root_ref),)))
        else:
            graph.uninspectable.append(f"{name} could not be read at {root_ref[:12]}")

    def unfollowed(why: str) -> None:
        graph.uninspectable.append(why)
        graph.unfollowed.append(why)

    while stack:
        name, sha, doc, depth, path = stack.pop()
        if expanded.get((name, sha), 0) >= depth:
            continue
        expanded[(name, sha)] = depth
        edges = graph.edges.setdefault((name, sha), set())
        jobs = doc.get("jobs")
        for job_name, job in jobs.items() if isinstance(jobs, dict) else ():
            if not isinstance(job, dict) or "uses" not in job:
                continue
            uses = str(job.get("uses") or "").strip()
            where = f"{name}@{sha[:12]}:{job_name}"
            found = _REMOTE_REUSABLE.fullmatch(uses)
            if not (
                found and found.group(1) == platform_repository and _SHA.fullmatch(found.group(3))
            ):
                unfollowed(
                    f"{where} calls {uses}, which doctor cannot follow (only platform workflows "
                    "pinned to a full SHA are inspected)"
                )
                continue
            target = (found.group(2), found.group(3))
            edges.add(target)
            if target in path:
                unfollowed(f"{where}: reusable workflow cycle through {target[0]}@{target[1][:12]}")
                continue
            if depth + 1 > max_depth:
                unfollowed(f"{where}: calls nested deeper than {max_depth} levels")
                continue
            called = graph.docs.get(target)
            if called is None and target not in graph.edges:
                loaded = load_callee(*target)
                called = loaded if isinstance(loaded, dict) else None
                if called is None:
                    graph.edges[target] = set()  # unreadable: never loaded again
            if (where, target) not in seen_calls:
                seen_calls.add((where, target))
                graph.calls.append((where, job, target[0], called))
            if called is None:
                graph.uninspectable.append(
                    f"{where}: {target[0]} could not be read at {target[1][:12]}"
                )
                continue
            graph.docs[target] = called
            stack.append((*target, called, depth + 1, (*path, target)))
    graph.uninspectable = list(dict.fromkeys(graph.uninspectable))
    graph.unfollowed = list(dict.fromkeys(graph.unfollowed))
    return graph


def reusable_workflow_limit_problems(
    caller_docs: Sequence[tuple[str, object]],
    calls: Sequence[tuple[str, dict, str]],
    graph: CalleeGraph,
    ref: str,
) -> list[str]:
    """Each top-level caller whose tree holds more than `_REUSABLE_WORKFLOWS_PER_CALLER` unique
    reusable workflows (Codex 4239397986): its platform callees (walked at `ref`) with
    everything they reach, plus every other reusable workflow it calls directly. Counted per
    caller: GitHub's limit is per top-level workflow file, not per repository."""
    problems = []
    for label, doc in caller_docs:
        roots = {(callee, ref) for where, _, callee in calls if where.startswith(f"{label}:")}
        others: set[str] = set()
        jobs = doc.get("jobs") if isinstance(doc, dict) else None
        for job in jobs.values() if isinstance(jobs, dict) else ():
            uses = str((job.get("uses") or "") if isinstance(job, dict) else "").strip()
            if uses and not any(uses.split("@")[0].endswith(f"/{c}") for c, _ in roots):
                others.add(uses)
        count = len(graph.reachable(roots)) + len(others)
        if count > _REUSABLE_WORKFLOWS_PER_CALLER:
            problems.append(
                f"{label} reaches {count} unique reusable workflows (nested trees included); "
                f"GitHub runs at most {_REUSABLE_WORKFLOWS_PER_CALLER} from one workflow file"
            )
    return problems


#: GITHUB_TOKEN permission scopes and levels (`none` < `read` < `write`): GitHub's finite
#: `permissions` syntax. A scope outside it (a typo such as `issuse`, or `metadata`, which a
#: workflow cannot request) makes GitHub reject the workflow before any job runs.
TOKEN_SCOPES = (
    "actions",
    "attestations",
    "checks",
    "contents",
    "deployments",
    "discussions",
    "id-token",
    "issues",
    "models",
    "packages",
    "pages",
    "pull-requests",
    "repository-projects",
    "security-events",
    "statuses",
)
_TOKEN_LEVELS = {"none": 0, "read": 1, "write": 2}
#: Scopes that accept only some levels (Codex 4239780832): an OIDC token can only be minted or
#: not (`id-token: read` is invalid), and GitHub Models is read-only (`models: write` is invalid).
_TOKEN_SCOPE_LEVELS = {"id-token": ("write", "none"), "models": ("read", "none")}
_TOKEN_LEVEL_NAMES = {v: k for k, v in _TOKEN_LEVELS.items()}
TOKEN_PERMISSIONS_CHECK = "GITHUB_TOKEN permissions hold across reusable-workflow calls"
#: The repository default for a workflow/job that sets no `permissions`
#: (`actions/permissions/workflow`'s `default_workflow_permissions`).
_DEFAULT_TOKEN_PERMISSIONS = {
    "read": {"contents": 1, "packages": 1, "models": 1},
    "write": {
        **{scope: 2 for scope in TOKEN_SCOPES if scope not in ("id-token", "models")},
        "models": 1,
    },
}
TokenPermissions = dict[str, int]


def token_permissions(value: object) -> TokenPermissions | str:
    """A `permissions:` value as {scope: level} (a scope it does not name is `none`), or why it
    is not valid GitHub `permissions` syntax. `read-all`/`write-all` grant every scope; `{}`
    none of them. Only a scope of `TOKEN_SCOPES` at a level that scope accepts
    (`_TOKEN_SCOPE_LEVELS`) is valid; anything else is returned as the reason, which callers
    report as a contract mismatch -- GitHub rejects such a workflow outright."""
    if value in ("read-all", "write-all"):
        level = _TOKEN_LEVELS[str(value).split("-")[0]]
        return dict.fromkeys(TOKEN_SCOPES, level)
    if not isinstance(value, dict):
        return f"invalid permissions {value!r}"
    out: TokenPermissions = {}
    for scope, level in value.items():
        if not isinstance(scope, str) or scope not in TOKEN_SCOPES:
            return f"invalid permission scope {scope!r}"
        allowed = _TOKEN_SCOPE_LEVELS.get(scope, tuple(_TOKEN_LEVELS))
        if not isinstance(level, str) or level not in allowed:
            return f"invalid permission {scope}: {level!r} (allowed: {'|'.join(allowed)})"
        out[scope] = _TOKEN_LEVELS[level]
    return out


def default_token_permissions(answer: object) -> TokenPermissions | str:
    """The repository's default GITHUB_TOKEN permissions from `GET
    repos/{repo}/actions/permissions/workflow`, or why they are unknown."""
    if isinstance(answer, Unreadable):
        return f"the repository's default workflow permissions could not be read ({answer.why})"
    default = answer.get("default_workflow_permissions") if isinstance(answer, dict) else None
    if default not in _DEFAULT_TOKEN_PERMISSIONS:
        return f"unrecognised default workflow permissions {default!r}"
    return dict(_DEFAULT_TOKEN_PERMISSIONS[default])


def token_permission_problems(
    caller_docs: Sequence[tuple[str, object]],
    calls: Sequence[tuple[str, dict, str]],
    graph: CalleeGraph,
    ref: str,
    platform_repository: str,
    repo_default: Callable[[], TokenPermissions | str],
) -> tuple[list[str], list[str]]:
    """(mismatches, unknowns) of GITHUB_TOKEN permissions through every reusable-call chain.

    A called workflow's token can only keep or lose permissions: the calling job's
    `permissions` (else its workflow's, else the repository default from `repo_default`) is the
    CEILING of every job of the callee, and of everything that job calls in turn. A callee job
    (or the callee's workflow-level default it inherits) requesting more than its ceiling is a
    mismatch: GitHub refuses the run or the job's API calls fail on a downgraded token. So is a
    `permissions` value that is not valid GitHub syntax (`token_permissions`: an unknown scope,
    a level the scope does not accept): GitHub rejects that workflow before any job runs. A
    repository default doctor cannot read, or a callee missing from `graph`, is an unknown
    (fail closed). The walk follows `graph`'s pinned platform calls (the edges
    `platform_callee_graph` resolved); calls it could not follow are already reported there."""
    mismatches: list[str] = []
    unknowns: list[str] = []
    docs_by_label = dict(caller_docs)
    seen: set[tuple[str, str, frozenset[tuple[str, int]]]] = set()

    def walk(name: str, sha: str, ceiling: TokenPermissions, chain: str, path: tuple) -> None:
        key = (name, sha, frozenset(ceiling.items()))
        if key in seen or (name, sha) in path or len(path) >= _CALLEE_GRAPH_MAX_DEPTH:
            return  # done, a cycle or too deep: the graph reports the last two
        seen.add(key)
        doc = graph.docs.get((name, sha))
        if not isinstance(doc, dict):
            unknowns.append(f"{chain} → {name}@{sha[:12]} could not be read")
            return
        jobs = doc.get("jobs")
        if not isinstance(jobs, dict):
            unknowns.append(f"{name}@{sha[:12]} has no readable jobs")
            return
        for job_name, job in jobs.items():
            where = f"{name}@{sha[:12]}:{job_name}"
            if not isinstance(job, dict):
                unknowns.append(f"{where}: unreadable job")
                continue
            source = job if "permissions" in job else doc if "permissions" in doc else None
            effective = ceiling
            if source is not None:
                requested = token_permissions(source["permissions"])
                if isinstance(requested, str):
                    mismatches.append(f"{where}: {requested}; GitHub rejects the workflow")
                    continue
                over = [s for s, level in requested.items() if level > ceiling.get(s, 0)]
                for scope in sorted(over):
                    mismatches.append(
                        f"{where} requests {scope}: {_TOKEN_LEVEL_NAMES[requested[scope]]} but "
                        f"{chain} grants at most "
                        f"{scope}: {_TOKEN_LEVEL_NAMES[ceiling.get(scope, 0)]}"
                    )
                effective = {s: min(level, ceiling.get(s, 0)) for s, level in requested.items()}
            found = _REMOTE_REUSABLE.fullmatch(str(job.get("uses") or "").strip())
            if found and found.group(1) == platform_repository and _SHA.fullmatch(found.group(3)):
                walk(found.group(2), found.group(3), effective, where, (*path, (name, sha)))

    for where, job, callee in calls:
        label = where.rsplit(":", 1)[0]
        doc = docs_by_label.get(label)
        doc = doc if isinstance(doc, dict) else {}
        if "permissions" in job or "permissions" in doc:
            ceiling = token_permissions((job if "permissions" in job else doc)["permissions"])
            if isinstance(ceiling, str):
                mismatches.append(f"{where}: {ceiling}; GitHub rejects the workflow")
                continue
        else:
            ceiling = repo_default()
        if isinstance(ceiling, str):
            unknowns.append(f"{where}: the token it passes on cannot be determined ({ceiling})")
            continue
        walk(callee, ref, ceiling, where, ())
    return list(dict.fromkeys(mismatches)), list(dict.fromkeys(unknowns))


_WORKFLOW_FILE = re.compile(r"^[A-Za-z0-9._-]+\.ya?ml$")


# Codex 4240170623: the contents API lists at most 1,000 entries of a directory and says nothing
# when it stops there, so a `pull_request` workflow past that prefix went unseen. The Git Trees
# API lists a tree in full or says `truncated: true`; one level at a time (`.github`, then
# `workflows`) keeps each answer to that one directory.
_REGULAR_FILE_MODES = frozenset({"100644", "100755"})
_TREE_ENTRY_LIMIT = 100_000  # GitHub's documented cap on one tree answer: at it, fail closed


def _tree_entries(gh: GhRunner, project_id: str, sha: str) -> tuple[list[dict] | None, str]:
    """The entries of tree `sha`, complete -- or (None, why): unreadable, `truncated` anything
    but an explicit false, an entry that is not an object, or as many entries as GitHub caps."""
    answer = _safe_json(gh, ["api", f"repos/{project_id}/git/trees/{_q(sha)}"])
    if not isinstance(answer, dict):
        return None, f"could not read git tree {sha[:12]}"
    entries = answer.get("tree")
    if answer.get("truncated") is not False or not isinstance(entries, list):
        return None, f"git tree {sha[:12]} was not listed in full (truncated or unreadable)"
    if len(entries) >= _TREE_ENTRY_LIMIT or not all(isinstance(e, dict) for e in entries):
        return None, f"git tree {sha[:12]} was not listed in full (at the API limit)"
    return entries, ""


def _workflow_tree_entries(
    gh: GhRunner, project_id: str, ref: str
) -> tuple[list[dict] | None, str]:
    """Every entry of `.github/workflows` at `ref`, through the Git Trees API: `ref`'s commit,
    its root tree, `.github`, then `workflows` -- each listed in full or (None, why)."""
    commit = _safe_json(gh, ["api", f"repos/{project_id}/commits/{_q(ref)}"])
    tree = commit.get("commit") if isinstance(commit, dict) else None
    tree = tree.get("tree") if isinstance(tree, dict) else None
    sha = tree.get("sha") if isinstance(tree, dict) else None
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        return None, f"could not resolve the tree of {ref}"
    for part in (".github", "workflows"):
        entries, why = _tree_entries(gh, project_id, sha)
        if entries is None:
            return None, f"could not list .github/workflows on {ref}: {why}"
        match = [e for e in entries if e.get("path") == part]
        if len(match) != 1 or match[0].get("type") != "tree":
            return None, f"could not list .github/workflows on {ref}: no {part} directory"
        sha = match[0].get("sha")
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            return None, f"could not list .github/workflows on {ref}: unreadable {part} tree"
    entries, why = _tree_entries(gh, project_id, sha)
    if entries is None:
        return None, f"could not list .github/workflows on {ref}: {why}"
    return entries, ""


def remote_workflow_files(
    gh: GhRunner, project_id: str, ref: str | None
) -> tuple[dict[str, bytes] | None, str]:
    """({file name: bytes}, '') of every workflow in `.github/workflows` on `ref` of the
    repository -- the set GitHub actually runs, local reusable workflows included (they must live
    in that same directory) -- or (None, why) when that set cannot be read in full. A file whose
    name or content cannot be read makes the whole set unreadable: a partial set proves nothing."""
    if not ref:
        return None, "the repository's default branch is unknown"
    listing, why = _workflow_tree_entries(gh, project_id, ref)
    if listing is None:
        return None, why
    files: dict[str, bytes] = {}
    for entry in listing:
        if entry.get("type") != "blob" or entry.get("mode") not in _REGULAR_FILE_MODES:
            continue  # a directory, a submodule or a symlink: not a workflow GitHub runs
        name = str(entry.get("path") or "")
        if not name.endswith((".yml", ".yaml")):
            continue  # GitHub runs only .yml/.yaml files
        if not _WORKFLOW_FILE.fullmatch(name):
            return None, f"unexpected workflow file name {name!r} on {ref}"
        blob = _safe_json(
            gh, ["api", f"repos/{project_id}/contents/.github/workflows/{name}?ref={_q(ref)}"]
        )
        if not isinstance(blob, dict) or blob.get("encoding") != "base64":
            return None, f"could not read .github/workflows/{name} on {ref}"
        try:
            files[name] = base64.b64decode(str(blob.get("content") or ""))
        except ValueError:
            return None, f"could not decode .github/workflows/{name} on {ref}"
    return files, ""


WORKFLOW_ACTIVE = "active"
WORKFLOWS_ACTIVE_CHECK = "Forge workflows are active in GitHub Actions"
_WORKFLOW_STATE_PAGE = 100
_WORKFLOW_STATE_MAX_PAGES = 50


def remote_workflow_states(gh: GhRunner, project_id: str) -> tuple[dict[str, str] | None, str]:
    """({file name: state}, '') of every workflow file under `.github/workflows` that GitHub
    Actions knows (`GET repos/{owner}/{repo}/actions/workflows`, every page), or (None, why)
    when the list cannot be read in full. Only `active` responds to its triggers: a
    `disabled_manually`, `disabled_inactivity`, `disabled_fork` ... workflow runs nothing, so it
    reports no context and occupies no runner."""
    states: dict[str, str] = {}
    endpoint = f"repos/{project_id}/actions/workflows"
    for page in range(1, _WORKFLOW_STATE_MAX_PAGES + 1):
        answer = _safe_json(gh, ["api", f"{endpoint}?per_page={_WORKFLOW_STATE_PAGE}&page={page}"])
        rows = answer.get("workflows") if isinstance(answer, dict) else None
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            return None, f"could not read GET {endpoint} (page {page})"
        for row in rows:
            path = str(row.get("path") or "")
            name = path.removeprefix(".github/workflows/")
            if name != path and _WORKFLOW_FILE.fullmatch(name):
                states[name] = str(row.get("state") or "unknown")
        total = answer.get("total_count")
        if len(rows) < _WORKFLOW_STATE_PAGE or (
            isinstance(total, int) and page * _WORKFLOW_STATE_PAGE >= total
        ):
            return states, ""
    return None, f"GET {endpoint} has more than {_WORKFLOW_STATE_MAX_PAGES} pages"


@contextlib.contextmanager
def _materialized_workflows(files: dict[str, bytes]) -> Iterator[Path]:
    """A scratch checkout holding a workflow set read from GitHub (`remote_workflow_files`), so
    local reusable calls resolve within that same set."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="forge-remote-workflows-") as scratch:
        root = Path(scratch)
        workflows = root / ".github/workflows"
        workflows.mkdir(parents=True)
        for name, content in files.items():
            (workflows / name).write_bytes(content)
        yield root


def _remote_pull_request_off_hosted(
    files: dict[str, bytes],
    platform_repository: str,
    load_callee: CalleeLoader | None = None,
    merge_group_only: bool = False,
) -> list[str]:
    """`_pull_request_off_hosted` over a workflow set read from GitHub (`remote_workflow_files`)."""
    with _materialized_workflows(files) as root:
        return _pull_request_off_hosted(root, platform_repository, load_callee, merge_group_only)


def remote_check_contexts(
    files: dict[str, bytes],
    default_branch: str,
    platform_repository: str | None = None,
    load_callee: CalleeLoader | None = None,
    inactive: frozenset[str] = frozenset(),
) -> ProducedContexts:
    """`workflow_check_contexts` over a workflow set read from GitHub (`remote_workflow_files`):
    the contexts PRs into `default_branch` (and merge groups) can actually receive."""
    with _materialized_workflows(files) as root:
        return workflow_check_contexts(
            root, default_branch, platform_repository, load_callee, inactive
        )


def fork_runner_exposure(
    base: Path,
    gh: GhRunner,
    project_id: str,
    platform_repository: str,
    ref: str | None,
    load_callee: CalleeLoader | None = None,
    remote: tuple[dict[str, bytes] | None, str] | None = None,
    merge_group_only: bool = False,
) -> list[str]:
    """Every job of a fork-exposed repository that runs fork pull-request code (`pull_request*`
    and `merge_group` workflows; only `merge_group` ones when `merge_group_only`) not proven
    GitHub-hosted. The authority is the default branch's workflow set as GitHub serves it (a
    stale or partial checkout must not hide a workflow that is live there; `remote` is that set
    when the caller already read it); the local checkout's workflows are checked IN ADDITION,
    never instead. An unreadable remote set fails closed."""
    files, why = remote if remote is not None else remote_workflow_files(gh, project_id, ref)
    if files is None:
        return [f"default-branch workflows unverifiable ({why}); doctor fails closed"]
    exposed = _remote_pull_request_off_hosted(
        files, platform_repository, load_callee, merge_group_only
    )
    exposed += [
        f"local checkout: {item}"
        for item in _pull_request_off_hosted(
            base, platform_repository, load_callee, merge_group_only
        )
        if item not in exposed
    ]
    return exposed


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


def policy_gate_commands(policy_doc: dict) -> tuple[dict[str, str], list[str]]:
    """The policy's `[commands]` setup/quality/test, and why any cannot be rendered into ci.yml
    (the shared POLICY_FIELDS validator: missing, empty, or not a single line). Never
    normalized."""
    values = policy_doc_values(policy_doc)
    commands: dict[str, str] = {}
    problems: list[str] = []
    for gate in GATE_COMMANDS:
        problem = policy_field_problem(gate, values.get(gate, _MISSING))
        if problem:
            problems.append(problem)
        else:
            commands[gate] = str(values[gate])
    return commands, problems


def policy_python_version(policy_doc: dict) -> str:
    """`[ci] python_version` (DEFAULT_PYTHON_VERSION when the policy predates it)."""
    table = policy_doc.get("ci") if isinstance(policy_doc, dict) else None
    value = table.get("python_version") if isinstance(table, dict) else None
    return str(value) if value is not None else DEFAULT_PYTHON_VERSION


def _policy_ready_label(policy_doc: dict) -> str:
    """The policy's `[automation] ready_label` (onboard's default when absent). The workflows are
    rendered with it: agent-auto-plan.yml's authorization step names the label whose most recent
    applier it authorizes (Codex 4239965189), so a rebuilt template must carry the policy's."""
    automation = policy_doc.get("automation")
    automation = automation if isinstance(automation, dict) else {}
    return str(automation.get("ready_label") or "claude-ready")


def _template_spec(policy_doc: dict, **fields) -> OnboardSpec:
    """An OnboardSpec carrying the policy's gate commands and Python version (raises
    OnboardError when the policy's commands cannot be rendered)."""
    commands, problems = policy_gate_commands(policy_doc)
    if problems:
        raise OnboardError("; ".join(problems))
    return OnboardSpec(
        setup_command=commands["setup"],
        quality_command=commands["quality"],
        test_command=commands["test"],
        python_version=policy_python_version(policy_doc),
        **fields,
    )


def expected_ci_steps(policy_doc: dict) -> list:
    """The parsed `steps` of the `test` job `onboard` renders for this policy: the ONLY steps
    ci.yml's test job may have (raises OnboardError when they cannot be rendered)."""
    import yaml  # deferred: the CLI must import without site dependencies

    spec = _template_spec(
        policy_doc,
        project_id="forge/consumer",
        platform_repository="forge/platform",
        platform_ref="0" * 40,
    )
    doc = yaml.safe_load(_render_workflow("ci.yml", spec))
    return json.loads(json.dumps(doc["jobs"][REQUIRED_CHECK]["steps"]))


def _ci_problems(base: Path) -> list[str]:
    """ci.yml must run on pull_request, as a job named `test`, whose steps are EXACTLY the steps
    `onboard` renders from the policy's commands (`expected_ci_steps`): the test job is fully
    managed, so no hand edit can change what the gates run. The structural analysis that
    follows (the policy's three commands are enforced invocations; nothing in the job changes
    what they do, `_gate_environment_problems`) is defense in depth behind that equality."""
    import yaml  # deferred: the CLI must import without site dependencies

    try:
        doc = yaml.safe_load((base / ".github/workflows/ci.yml").read_text())
        policy_doc = tomllib.loads((base / "agentic-sdlc.toml").read_text())
    except (OSError, yaml.YAMLError, tomllib.TOMLDecodeError) as exc:
        return [str(exc)[:200]]
    commands, command_problems = policy_gate_commands(policy_doc)
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
    if command_problems:
        problems += command_problems
    else:
        try:
            want_steps = expected_ci_steps(policy_doc)
        except OnboardError as exc:
            problems.append(f"the template's steps cannot be rendered ({exc})")
        else:
            have_steps = json.loads(json.dumps(job.get("steps"), default=str))
            found = _first_difference(want_steps, have_steps, f"jobs.{REQUIRED_CHECK}.steps")
            if found:
                problems.append(
                    f"'{REQUIRED_CHECK}' job steps differ from the ones onboard renders from the "
                    f"policy commands, at {found}: the CI test job is fully managed -- change "
                    "[commands] in agentic-sdlc.toml and re-run `sdlcctl onboard --force`"
                )
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
    for gate in GATE_COMMANDS:
        command = commands.get(gate)
        if command is None:
            continue  # reported by policy_gate_commands
        wanted = _shell_commands(command)
        wanted_by_gate[gate] = wanted
        if not wanted or not all(
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
#: actions/checkout inputs that change WHICH code is checked out (and so tested).
CHECKOUT_FORBIDDEN_INPUTS = frozenset({"ref", "repository"})
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
            inputs = step.get("with")
            if action == "actions/checkout":
                # The pull request's merge revision is what the check vouches for: any other
                # ref or repository tests code that is not the change.
                moved = (
                    sorted(k for k in inputs if str(k) in CHECKOUT_FORBIDDEN_INPUTS)
                    if isinstance(inputs, dict)
                    else ([] if inputs is None else ["<unreadable with:>"])
                )
                if moved:
                    problems.append(
                        f"{label} checks out with {', '.join(map(str, moved))}: the gates must "
                        "run on the pull request's own revision"
                    )
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
        if parsed.github_path:
            problems.append(
                f"{label} mentions $GITHUB_PATH: a directory added there can put another "
                "program in place of a gate tool for every later step"
            )
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
#: Builtins after which the script (or the function) may not continue: `exit 0; pytest`.
_SHELL_TERMINATORS = {"exit", "exec", "return"}
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
    #: Any mention of `$GITHUB_PATH` (a write there prepends to every later step's PATH).
    github_path: bool = False


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
    out = ShellScript(
        [], set(), set(), set(), [], False, "GITHUB_ENV" in script, "GITHUB_PATH" in script
    )
    terminated = False  # an `exit`/`exec`/`return` was seen: nothing after it is proven reached
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
                        "reached": not terminated
                        and not condition
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
                program = (
                    argv[1] if argv[0] in {"builtin", "command"} and len(argv) > 1 else argv[0]
                )
                if program in _SHELL_TERMINATORS:
                    # Fails closed: wherever it sits (a condition, a branch, a function body, a
                    # subshell), it may end the script, so later commands are not proven to run.
                    terminated = True
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
#: Interpreter names an OLD (pre-Codex 4242753911) handler ran its script with. Recognised only
#: so a forced re-onboard replaces such a handler; doctor rejects every one of them.
_LEGACY_HOOK_INTERPRETERS = {"python3", "python"}
#: How a Forge handler may name its script's directory. Only the project-anchored forms count
#: for `doctor` (Codex 4242373510: a bare relative path is resolved against whatever directory
#: the session's shell is in, so a `cd` broke every Bash call); "" is still RECOGNISED, so a
#: forced re-onboard replaces an old relative copy instead of keeping it beside the new one.
_HOOK_DIR_PREFIXES = ("", "$CLAUDE_PROJECT_DIR/", "${CLAUDE_PROJECT_DIR}/")
#: The directory placeholder Claude Code expands in a hook command: the project root the
#: session started in (code.claude.com/docs/en/hooks; double-quoted in a shell-form command).
HOOK_PROJECT_DIR = "${CLAUDE_PROJECT_DIR}"
#: The commit guard's handler `timeout`, in seconds. The guard bounds its own run below it
#: (`GUARD_DEADLINE` in forge_commit_guard.py), and with `onFailure: "block"` a timeout blocks.
GUARD_HOOK_TIMEOUT = 300
#: What follows the guard's command: any failure to run it -- no interpreter, a missing file, a
#: crash past its own handler -- becomes exit 2, which blocks on every Claude Code version,
#: including those that predate `onFailure`.
_GUARD_FAIL_CLOSED = ("||", "exit", "2")

# ---- the hook interpreter (Codex 4242753911) and the script pin (Codex 4242753904)
#
# A handler that says `python3` is a PATH lookup in the session's environment, and the cloud
# setup puts the user-writable `~/.local/bin` on that PATH: an agent could drop a `python3` there
# that exits 0 and the guard would never run. So the handler names an ABSOLUTE interpreter that
# `onboard` vetted (only root can replace it or any directory above it, `trusted_exec`), starts
# it under a fixed PATH and in isolated mode (`-I`: no PYTHON* variables, no user site, no
# script directory on sys.path), and runs a fixed inline loader instead of the script itself.
# The loader reads the script ONCE, refuses (exit 2) unless its SHA-256 is the one onboard
# recorded in the handler, and executes exactly those bytes as `__main__`. The session's own
# PATH is handed through as data (`FORGE_SESSION_PATH`) and restored for the script: the guard
# models what a Bash command's bare program names resolve to from it.
#
# This is a guardrail, not a sandbox: an agent that can write the checkout can also rewrite
# .claude/settings.json (handler and hash together) or the hook's environment. The enforcement
# is server-side (the ruleset, Publisher-only pushes, required review); see docs/onboarding.md.

#: The PATH the hook interpreter starts under.
HOOK_SAFE_PATH = "/usr/bin:/bin"
#: Carries the session's PATH past the fixed one; the loader restores it for the script.
HOOK_SESSION_PATH_ENV = "FORGE_SESSION_PATH"
_HOOK_PREFIX = (f'{HOOK_SESSION_PATH_ENV}="$PATH"', f"PATH={HOOK_SAFE_PATH}")
#: An interpreter path the handler can carry unquoted (it is a shell word).
_HOOK_PYTHON_PATH = re.compile(r"^/[A-Za-z0-9._+/-]+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
#: The inline loader (one line, no single quote: it is single-quoted in the handler).
HOOK_LOADER = (
    'import hashlib,os,sys,types;p,h=sys.argv[1:3];b=open(p,"rb").read();'
    "d=hashlib.sha256(b).hexdigest();"
    'd==h or (sys.stderr.write("Forge hook: "+p+" is not the copy onboard installed (sha256 "'
    '+d+"); blocking. Restore it or re-run sdlcctl onboard --force.\\n"),sys.exit(2));'
    f'os.environ["PATH"]=os.environ.pop("{HOOK_SESSION_PATH_ENV}","");'
    'sys.argv=[p];m=types.ModuleType("__main__");m.__file__=p;sys.modules["__main__"]=m;'
    'exec(compile(b,p,"exec"),m.__dict__)'
)


#: The oldest Python the hooks may run under: the package's `requires-python` (pyproject.toml;
#: a test keeps the two equal). The hook scripts use what that version has.
HOOK_PYTHON_MIN_VERSION = (3, 11)
#: The implementation the hook scripts are written and tested for.
HOOK_PYTHON_IMPLEMENTATION = "cpython"
#: How long the interpreter probe may take, in seconds.
HOOK_PYTHON_PROBE_TIMEOUT = 15.0
#: What the probe runs (`-I -c`): the nonce (argv[1]) back, then the implementation and version.
_HOOK_PYTHON_PROBE = (
    "import sys;print(sys.argv[1],sys.implementation.name,sys.version_info[0],sys.version_info[1])"
)
_HOOK_PYTHON_PROBED: dict[tuple, str] = {}


def _hook_python_probe_problem(path: str) -> str:
    """Why the executable at `path` is not a Python the hooks can run ('' = it is), found by
    RUNNING it the way the handler does (Codex 4242925452): isolated (`-I -c`), under
    HOOK_SAFE_PATH, with a fresh nonce it must echo, a CPython of at least
    HOOK_PYTHON_MIN_VERSION. A root-owned program that is not that interpreter -- `/bin/true`
    exits 0 without running anything, so the guard would never run -- is refused, as is a
    Python too old for the hook scripts. Cached per file identity (device, inode, size, mtime)."""
    try:
        info = os.stat(path)
    except OSError as exc:
        return f"{path} cannot be read ({exc.strerror or exc})"
    key = (path, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    if key in _HOOK_PYTHON_PROBED:
        return _HOOK_PYTHON_PROBED[key]
    nonce = secrets.token_hex(8)
    want = ".".join(map(str, HOOK_PYTHON_MIN_VERSION))
    try:
        proc = subprocess.run(
            [path, "-I", "-c", _HOOK_PYTHON_PROBE, nonce],
            check=False,
            capture_output=True,
            text=True,
            timeout=HOOK_PYTHON_PROBE_TIMEOUT,
            env={"PATH": HOOK_SAFE_PATH, "LC_ALL": "C"},
            cwd="/",
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{path} could not be run as Python ({exc})"
    words = proc.stdout.split()
    if (
        proc.returncode != 0
        or len(words) != 4
        or words[0] != nonce
        or not all(w.isdigit() for w in words[2:])
    ):
        problem = (
            f"{path} is not a Python interpreter (running `{path} -I -c ...` did not answer "
            f"as one: exit {proc.returncode}, output {proc.stdout.strip()[:80]!r})"
        )
    elif words[1] != HOOK_PYTHON_IMPLEMENTATION:
        problem = f"{path} is {words[1]}, not {HOOK_PYTHON_IMPLEMENTATION} {want}+"
    elif (int(words[2]), int(words[3])) < HOOK_PYTHON_MIN_VERSION:
        problem = f"{path} is Python {words[2]}.{words[3]}; the hooks need {want} or newer"
    else:
        problem = ""
    _HOOK_PYTHON_PROBED[key] = problem
    return problem


def hook_python_problem(path: str, mode: str = STRICT) -> str:
    """Why `path` may not be the hook interpreter ('' = it may): an absolute path the handler
    can carry as one shell word, to an executable only root can replace (`trusted_exec`; in
    trust mode local: owned by root or this user, not group/other-writable), that IS a CPython of
    at least HOOK_PYTHON_MIN_VERSION when run the way the handler runs it
    (`_hook_python_probe_problem`, Codex 4242925452) -- in both modes."""
    if not _HOOK_PYTHON_PATH.fullmatch(path or ""):
        return f"{path!r} is not an absolute path made of [A-Za-z0-9._+/-]"
    # Codex 4244145211: in an agent context the effective mode is strict whatever the policy or
    # --trust-mode asked for, exactly as `trust_mode()` decides for git and gh.
    if agent_context():
        mode = STRICT
    if mode == LOCAL:
        from .trusted_exec import local_candidate_reason

        reason = local_candidate_reason(path)
        if reason:
            return f"{reason} (trust mode local: owned by root or you, not group/other-writable)"
        return _hook_python_probe_problem(path)
    from .trusted_exec import candidate_reason

    reason = candidate_reason(path)
    if reason:
        return f"{reason} (it must be a file only root can replace)"
    return _hook_python_probe_problem(path)


def _path_pythons() -> list[str]:
    """Absolute `python3` candidates on PATH, in PATH order (trust mode local only)."""
    return [
        os.path.join(entry, "python3")
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if entry and os.path.isabs(entry)
    ]


def trusted_hook_python(explicit: str | None = None, mode: str = STRICT) -> str:
    """The interpreter onboard writes into the hook handlers (an absolute path, pinned): `explicit`
    (`--hook-python`) when given, else the first of HOOK_PYTHON_CANDIDATES that
    `hook_python_problem` accepts -- and in trust mode local, then the first acceptable `python3`
    on PATH (a Homebrew CPython 3.11+, say). Raises OnboardError when there is none: in strict
    mode onboarding never falls back to a PATH lookup."""
    if explicit:
        problem = hook_python_problem(explicit, mode)
        if problem:
            raise OnboardError(f"--hook-python {problem}")
        return explicit
    rejected = []
    candidates = list(HOOK_PYTHON_CANDIDATES)
    if mode == LOCAL:
        candidates += _path_pythons()
    for candidate in dict.fromkeys(candidates):
        if not os.path.lexists(candidate):
            if candidate in HOOK_PYTHON_CANDIDATES:
                rejected.append(f"{candidate} does not exist")
            continue
        if mode == LOCAL and not _HOOK_PYTHON_PATH.fullmatch(candidate):
            continue
        problem = hook_python_problem(candidate, mode)
        if not problem:
            return candidate
        rejected.append(problem)
    if mode == LOCAL:
        raise OnboardError(
            "no usable interpreter for the Claude Code hooks (" + "; ".join(rejected) + "). "
            "Trust mode local takes the system python3 or the first python3 on PATH that is a "
            f"CPython {'.'.join(map(str, HOOK_PYTHON_MIN_VERSION))}+ owned by root or you; "
            "install one, or pass --hook-python /absolute/path/to/python3."
        )
    raise OnboardError(
        "no trusted interpreter for the Claude Code hooks (" + "; ".join(rejected) + "). The "
        "hooks must run a Python 3 that only root can replace -- never one found on PATH. "
        "Install the system python3, or pass --hook-python /absolute/path/to/python3 (a "
        "root-owned file in root-owned, non-writable directories). On a developer's own "
        "machine (e.g. a Homebrew Python on macOS), `--trust-mode local` accepts one you own."
    )


def hook_script_digest(content: str | bytes) -> str:
    """The SHA-256 the handler pins its script to (of the bytes onboard writes)."""
    data = content.encode("utf-8") if isinstance(content, str) else content
    return hashlib.sha256(data).hexdigest()


def forge_hook_entry(
    event: str, digest: str, python: str = DEFAULT_HOOK_PYTHON
) -> dict[str, object]:
    """THE hook handler `onboard` writes for the Forge script of `event` (`HOOK_SCRIPTS`), and
    the only shape `doctor` accepts for one: `python` (vetted, absolute) under a fixed PATH in
    isolated mode runs HOOK_LOADER, which executes the script only if its SHA-256 is `digest`
    (Codex 4242753911, 4242753904). The script is named under the project root
    (`HOOK_PROJECT_DIR`), never relative to the session's working directory (Codex 4242373510).
    The commit guard fails CLOSED (Codex 4242373515): `onFailure: "block"` (Claude Code's
    documented handler field: a command that cannot start, an exit other than 0 or 2, a timeout
    or invalid output blocks the tool call instead of letting it through), an explicit
    `timeout` above the guard's own deadline, and `|| exit 2` for clients without `onFailure`.
    Any other handler field changes how it runs -- `async` runs it in the background, where it
    can no longer block (or report) anything; `once` stops it after one run -- so a Forge
    handler that carries one is not the hook onboard installed."""
    if not _SHA256.fullmatch(digest):
        raise OnboardError(f"hook script digest must be a SHA-256 hex string, got {digest!r}")
    if not _HOOK_PYTHON_PATH.fullmatch(python):
        raise OnboardError(f"hook interpreter must be an absolute path, got {python!r}")
    script = HOOK_SCRIPTS[event]
    command = " ".join(
        (
            *_HOOK_PREFIX,
            python,
            "-I",
            "-c",
            shlex.quote(HOOK_LOADER),
            f'"{HOOK_PROJECT_DIR}/{script}"',
            digest,
        )
    )
    if event != "PreToolUse":
        return {"type": "command", "command": command}
    return {
        "type": "command",
        "command": f"{command} {' '.join(_GUARD_FAIL_CLOSED)}",
        "timeout": GUARD_HOOK_TIMEOUT,
        "onFailure": "block",
    }


#: The keys of the hook GROUP that holds a Forge handler (`matcher` is checked by
#: `_matcher_covers`; a group carries no other field that onboard writes).
_FORGE_HOOK_GROUP_KEYS = frozenset({"matcher", "hooks"})


@dataclass(frozen=True)
class _HookCommand:
    """A parsed handler command that runs a Forge script: `pinned` when it is the
    onboard-written form (vetting interpreter `python`, script hash `digest`), else an old
    `python3 <script>` handler. `relative`: the script is not anchored at the project root.
    `tail`: what follows the script (and digest)."""

    pinned: bool
    python: str
    digest: str
    relative: bool
    tail: tuple[str, ...]


def _hook_command(entry: object, script: str) -> _HookCommand | None:
    """The parsed command of a hook handler that runs `script` -- in the pinned form or as an
    old `python3 <script>` handler (optionally under $CLAUDE_PROJECT_DIR, optionally followed
    by `|| exit 2`) -- else None."""
    if not isinstance(entry, dict) or entry.get("type") != "command":
        return None
    try:
        argv = shlex.split(str(entry.get("command", "")))
    except ValueError:
        return None
    paths = {prefix + script for prefix in _HOOK_DIR_PREFIXES}
    tails = {(), _GUARD_FAIL_CLOSED}
    if len(argv) >= 2 and argv[0] in _LEGACY_HOOK_INTERPRETERS and argv[1] in paths:
        if tuple(argv[2:]) in tails:
            return _HookCommand(False, argv[0], "", argv[1] == script, tuple(argv[2:]))
        return None
    prefix = (f"{HOOK_SESSION_PATH_ENV}=$PATH", f"PATH={HOOK_SAFE_PATH}")
    if (
        len(argv) >= 8
        and tuple(argv[:2]) == prefix
        and argv[3:6] == ["-I", "-c", HOOK_LOADER]
        and argv[6] in paths
        and tuple(argv[8:]) in tails
    ):
        return _HookCommand(True, argv[2], argv[7], argv[6] == script, tuple(argv[8:]))
    return None


def _hook_runs(entry: object, script: str) -> bool:
    """A hook handler that runs `script` in any spelling `_hook_command` recognises."""
    return _hook_command(entry, script) is not None


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


def _forge_entry_deviation(
    group: dict, entry: dict, event: str, digest: str | None, mode: str = STRICT
) -> str:
    """How a handler that runs a Forge script differs from `forge_hook_entry` ('' = it does
    not): any extra, missing or different handler field, an extra group field, a script not
    anchored at the project root, an interpreter resolved through PATH or one only root cannot
    replace, a script hash other than the installed script's (`digest`; None: the script cannot
    be read), or (the guard) no `|| exit 2`. Only the command's spelling may vary
    (`$CLAUDE_PROJECT_DIR` with or without braces or quotes, the vetted interpreter's path)."""
    script = HOOK_SCRIPTS[event]
    parsed = _hook_command(entry, script)
    assert parsed is not None
    want = forge_hook_entry(event, "0" * 64)
    found = []
    for key in sorted(set(entry) | set(want)):
        if key in ("type", "command"):
            continue
        if key not in want:
            found.append(str(key))
        elif entry.get(key) != want[key]:
            found.append(f"{key} not {json.dumps(want[key])}")
    found += sorted(f"group {k}" for k in set(group) - _FORGE_HOOK_GROUP_KEYS)
    if parsed.relative:
        found.append(f"a path relative to the working directory (not {HOOK_PROJECT_DIR})")
    if not parsed.pinned:
        found.append(
            f"`{parsed.python}` found on PATH instead of a vetted absolute interpreter with the "
            "script's SHA-256 pinned"
        )
    else:
        problem = hook_python_problem(parsed.python, mode)
        if problem:
            found.append(f"an untrusted interpreter ({problem})")
        if digest is None:
            found.append(f"{script} cannot be read to check its pinned SHA-256")
        elif parsed.digest != digest:
            found.append(f"a pinned SHA-256 that is not {script}'s")
    if parsed.tail != (_GUARD_FAIL_CLOSED if event == "PreToolUse" else ()):
        found.append("`|| exit 2`" if event == "PreToolUse" else "trailing words")
    return ", ".join(found)


def _installed_digest(base: Path, script: str) -> str | None:
    """The SHA-256 of the installed `script` (a regular file under `base`), else None."""
    path = base / script
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return hook_script_digest(path.read_bytes())
    except OSError:
        return None


def hook_problems(settings_path: Path, mode: str = STRICT) -> list[str]:
    """Why Claude Code would NOT run the Forge hooks from this settings file as onboard installed
    them (empty = it will): the parsed `hooks` structure must hold a SessionStart group firing on
    startup that runs the session-start script, and a PreToolUse group matching Bash that runs the
    commit guard, each handler exactly `forge_hook_entry` -- synchronous, so the guard's exit 2
    blocks the commit; a vetted absolute interpreter; and the SHA-256 of the script installed
    beside it (which doctor separately requires to be the rendered template, so the recorded
    hash IS the shipped template's). A mention anywhere else in the file -- a string, a disabled
    or malformed entry -- is no hook. Unrelated hooks (other scripts, other events) may sit
    beside them."""
    try:
        settings = json.loads(settings_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return [f".claude/settings.json is not valid JSON ({str(exc)[:80]})"]
    if not isinstance(settings, dict):
        return [".claude/settings.json is not a JSON object"]
    if settings.get("disableAllHooks") is True:
        return [".claude/settings.json sets disableAllHooks"]
    base = settings_path.parent.parent
    hooks = settings.get("hooks")
    hooks = hooks if isinstance(hooks, dict) else {}
    problems = []
    for event, script in HOOK_SCRIPTS.items():
        tool = "Bash" if event == "PreToolUse" else None
        groups = hooks.get(event)
        groups = [g for g in groups if isinstance(g, dict)] if isinstance(groups, list) else []
        digest = _installed_digest(base, script)
        found = False
        for group in groups:
            entries = group.get("hooks") if isinstance(group.get("hooks"), list) else []
            for entry in entries:
                if not _hook_runs(entry, script):
                    continue
                deviation = _forge_entry_deviation(group, entry, event, digest, mode)
                if deviation:
                    # Wherever it sits: an `async` guard never blocks, even beside a good one.
                    problems.append(
                        f".claude/settings.json {event} hook running {script} has "
                        f"what onboard does not write ({deviation}); it must be exactly the "
                        "generated handler (an `async` hook cannot block; re-run `sdlcctl "
                        "onboard --force`)"
                    )
                    continue
                if _matcher_covers(group.get("matcher"), tool):
                    found = True
        if not found:
            where = f"{event} (matcher Bash)" if tool else event
            problems.append(f".claude/settings.json has no {where} hook running {script}")
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
#:   - every job's `runs-on` (except `_HOSTED_JOBS`), and a reusable call's `with.runs_on` (the
#:     runner checks);
#:   - a reusable-implement call's `with.route_budget_usd` (absent or present; the budget check);
#: ci.yml's `test` job `steps` are NOT tunable: the template is rendered from the policy's own
#: `[commands]` and `[ci] python_version`, so the steps must equal it exactly (customize them only
#: through the policy, then re-run onboard).
#:   - the automatic callers' label `if:` (the label-condition check: exactly the generated
#:     condition for the POLICY's labels);
#:   - the platform pin: the template is rendered at the caller's own pinned SHA (the pin check:
#:     a SHA on the platform repository, one SHA across all callers).
TUNABLE_KNOBS = (
    "jobs.*.runs-on",
    "jobs.*.with.runs_on",
    "jobs.*.with.route_budget_usd",
    "agent-auto-plan.yml: jobs.preflight.if",
    "agent-auto-implement.yml: jobs.authorize.if",
    "platform ref pin",
)
_LABEL_CONDITION_JOBS = {
    "agent-auto-plan.yml": "preflight",
    "agent-auto-implement.yml": "authorize",
}
#: Jobs whose `runs-on` is NOT tunable: the automatic callers authorize the labeler on a
#: GitHub-hosted runner before anything is scheduled on the (self-hosted) RUNS_ON labels, and
#: report a FAILED authorization from one too (`authorization_failed`, Codex 4239667541).
AUTHORIZATION_FAILED_JOB = "authorization_failed"
_HOSTED_JOBS = {
    "agent-auto-plan.yml": frozenset({"preflight", "dedupe", AUTHORIZATION_FAILED_JOB}),
    "agent-auto-implement.yml": frozenset({"authorize", AUTHORIZATION_FAILED_JOB}),
}


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
        if "runs-on" in job and job_name not in _HOSTED_JOBS.get(name, frozenset()):
            job["runs-on"] = _TUNED
        passed = job.get("with")
        if isinstance(passed, dict):
            if "runs_on" in passed:
                passed["runs_on"] = _TUNED
            passed.pop("route_budget_usd", None)
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
    """The SHA the caller's first platform reusable-workflow call is pinned to. A job that is not
    a mapping (null, a scalar, a list: a botched merge) calls nothing and is skipped, as in
    `_workflow_jobs()`; the drift check reports it (Codex 4240306501)."""
    jobs = doc.get("jobs") if isinstance(doc, dict) else None
    for job in jobs.values() if isinstance(jobs, dict) else ():
        if not isinstance(job, dict):
            continue
        found = _REMOTE_REUSABLE.fullmatch(str(job.get("uses") or "").strip())
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
            spec = _template_spec(
                policy_doc,
                project_id=project_id,
                platform_repository=platform_repository,
                platform_ref=pin,
                implementer=implementer,
                default_branch=default_branch,
                ready_label=_policy_ready_label(policy_doc),
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


#: Forge-managed files doctor compares byte for byte with what `onboard` renders. The hooks are
#: the lease enforcement itself: a guard replaced by `sys.exit(0)` must not pass as installed.
EXACT_MANAGED_FILES = (
    ".claude/hooks/forge_commit_guard.py",
    ".claude/hooks/forge_session_start.py",
    ".github/ISSUE_TEMPLATE/agent-work-request.md",
)
#: Forge-managed guides doctor requires to CONTAIN the rendered text verbatim: repository notes
#: may be added around it, but no generated line may be edited or dropped.
CONTAINED_MANAGED_FILES = ("AGENTS.md", "CLAUDE.md")
#: The cloud-routine doc is exact apart from the CI runner it names (a tunable knob of ci.yml,
#: checked by the runner checks).
ROUTINE_DOC = "docs/forge/cloud-implementer.md"
_ROUTINE_CI_RUNNER = re.compile(r"runs on\s+`[^`]*`")


def _routine_doc_masked(text: str) -> str:
    return _ROUTINE_CI_RUNNER.sub("runs on `<tunable>`", text)


def installed_platform_pin(base: Path, platform_repository: str) -> str | None:
    """The commit the installed managed workflows pin the platform to (the first caller that
    names one), or None. The cloud routine doc names it too (its setup script and step 0)."""
    for name in MANAGED_WORKFLOWS + MANAGED_IMPLEMENT_WORKFLOWS:
        path = base / ".github/workflows" / name
        doc = _workflow_doc(path) if path.is_file() else None
        pin = _caller_pin(doc, platform_repository) if doc is not None else None
        if pin:
            return pin
    return None


def rendered_managed_files(
    policy_doc: dict,
    project_id: str,
    platform_repository: str,
    *,
    cloud: bool,
    platform_ref: str | None = None,
) -> tuple[dict[str, str], list[str]]:
    """({path: rendered text}, problems) for every non-workflow Forge-managed file `onboard`
    writes, rendered from the repository's own policy (ready label, default branch, caps, gate
    commands) and `platform_ref`, the pin of the installed workflows (the routine doc names it).
    A file that cannot be rendered is a problem, never silently skipped."""
    project = policy_doc.get("project") if isinstance(policy_doc.get("project"), dict) else {}
    caps = policy_doc.get("policy") if isinstance(policy_doc.get("policy"), dict) else {}
    fields = dict(
        project_id=project_id,
        platform_repository=platform_repository,
        platform_ref=platform_ref or "0" * 40,
        default_branch=str(project.get("default_branch") or "main"),
        ready_label=_policy_ready_label(policy_doc),
        implementer="cloud-routine" if cloud else "claude",
        trust_mode=policy_trust_mode(policy_doc),  # rendered into the guard
    )
    rendered: dict[str, str] = {}
    login = policy_bot_login(policy_doc)
    if login and not _bot_login_field_problem(login):
        fields["publisher_bot_login"] = login  # rendered into the guard (Codex 4242753917)
    problems: list[str] = []
    try:
        # The hooks and the issue template depend on the project, branch and label only.
        basic = OnboardSpec(test_command="-", **fields)
        hooks = render_hooks(basic)
        # .claude/settings.json is tunable (other hooks, permissions): `hook_problems` checks it.
        rendered.update({k: v for k, v in hooks.items() if k in EXACT_MANAGED_FILES})
        rendered[".github/ISSUE_TEMPLATE/agent-work-request.md"] = render_work_request(basic)
    except OnboardError as exc:
        problems.append(f"hooks and issue template cannot be rebuilt ({exc})")
    try:
        full = _template_spec(
            policy_doc,
            max_changed_files=int(caps.get("max_changed_files", 20)),
            max_diff_lines=int(caps.get("max_diff_lines", 2500)),
            **fields,
        )
        rendered["AGENTS.md"] = render_agents_md(full)
        rendered["CLAUDE.md"] = render_claude_md(full)
        if cloud:
            rendered[ROUTINE_DOC] = render_cloud_routine_md(full)
    except (OnboardError, TypeError, ValueError) as exc:
        problems.append(f"AGENTS.md, CLAUDE.md and the routine doc cannot be rebuilt ({exc})")
    return rendered, problems


def managed_file_problem(relative: str, installed: bytes, rendered: dict[str, str]) -> str | None:
    """Why an installed copy of a managed file is not what `onboard` renders (None: it is, or
    the file is not one compared here)."""
    want = rendered.get(relative)
    if want is None:
        return None
    try:
        have = installed.decode("utf-8")
    except UnicodeDecodeError:
        return f"{relative} is not UTF-8 text"
    if relative in CONTAINED_MANAGED_FILES:
        ok = want in have
    elif relative == ROUTINE_DOC:
        ok = _routine_doc_masked(have) == _routine_doc_masked(want)
    else:
        ok = have == want
    return None if ok else f"{relative} differs from the generated file"


def managed_file_drift(
    base: Path, policy_doc: dict, project_id: str, platform_repository: str, *, cloud: bool
) -> list[str]:
    """Every installed non-workflow Forge-managed file against its rendered form
    (`rendered_managed_files`): the hook scripts and issue template exactly, the guides by
    containment, the routine doc exactly apart from its CI runner. Missing files are reported by
    "required files present"; `.claude/settings.json` is checked structurally (`hook_problems`)
    and the routing registries semantically (the routing check), since both are tunable."""
    rendered, problems = rendered_managed_files(
        policy_doc,
        project_id,
        platform_repository,
        cloud=cloud,
        platform_ref=installed_platform_pin(base, platform_repository),
    )
    for relative in rendered:
        path = base / relative
        if _regular_file_problem(base, relative) is None:  # else "required files present" fails
            problem = managed_file_problem(relative, path.read_bytes(), rendered)
            if problem:
                problems.append(problem)
    return problems


#: The `[routing]` table onboard writes; the implement callers pass these paths.
ROUTING_TABLE = {"executors": ".forge/executors.json", "policy": ".forge/routing-policy.json"}


def policy_field_problems(policy, policy_doc: dict, *, cloud: bool) -> list[str]:
    """Every policy field onboard writes that the generated workflows or `prepare-request`
    depend on, against the value they need (one problem per mismatch):

    - `[project] provider` = "github": every plan/implement caller runs
      `prepare-request --provider github`, which rejects any other provider.
    - `[automation] human_review_label` / `implementation_label` = the fixed labels the
      generated callers, routine doc and label conditions are rendered with.
    - `[agents] implementer`: one onboard writes (it picks the adapter the implement callers are
      rebuilt with); "claude" under cloud-routine, which is what onboard writes there.
    - `[routing] executors` / `policy`: the registry paths the routed callers pass.

    Checked separately: `[project] id` (= the repository, "policy project id matches the
    repository"), `[project] default_branch` (= the remote default branch, which
    prepare-request is handed as --expected-default-branch), `[agents] implementation_mode`
    ("implementation mode is configured"), and the ready label (the label-condition check)."""
    problems = []
    if policy.provider != "github":
        problems.append(
            f"[project] provider is {policy.provider!r}; the generated workflows run "
            "prepare-request --provider github, which rejects it"
        )
    for name, have, want in (
        ("human_review_label", policy.human_review_label, HUMAN_REVIEW_LABEL),
        ("implementation_label", policy.implementation_label, IMPLEMENTATION_LABEL),
    ):
        if have != want:
            problems.append(
                f"[automation] {name} is {have!r}; the generated workflows use {want!r}"
            )
    agents = policy_doc.get("agents") if isinstance(policy_doc.get("agents"), dict) else {}
    implementer = agents.get("implementer")
    if cloud and implementer != "claude":
        problems.append(f"[agents] implementer is {implementer!r}; cloud-routine writes 'claude'")
    elif not cloud and implementer not in POLICY_IMPLEMENTERS:
        problems.append(
            f"[agents] implementer is {implementer!r}; expected one of "
            + ", ".join(sorted(POLICY_IMPLEMENTERS))
        )
    routing = policy_doc.get("routing")
    if routing is not None:
        table = routing if isinstance(routing, dict) else {}
        for key, want in ROUTING_TABLE.items():
            if table.get(key) != want:
                problems.append(
                    f"[routing] {key} is {table.get(key)!r}; the routed callers read {want!r}"
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


def _regular_file_problem(base: Path, relative: str) -> str | None:
    """Why `relative` is not a regular file inside the checkout `base` (None when it is), by the
    same `lstat` rule `write_onboarding` writes under (`_managed_path_state`): the path and every
    parent component must not be a symlink -- even one resolving to an identical file elsewhere
    in the checkout, since that target is an unmanaged path a later patch may change (Codex
    4240952121) -- every parent must be a directory and the leaf a regular file."""
    state = _managed_path_state(base, relative)
    if state == "file":
        return None
    if state == "absent":
        return relative
    if state == "exists but is not a regular file":
        return f"{relative} (not a regular file)"
    return f"{relative} ({state}; a managed path is never followed through a link)"


def _generated_workflows(
    base: Path,
    policy_doc: dict,
    project_id: str,
    platform_repository: str,
    *,
    cloud: bool,
    default_branch: str,
    pin: str,
) -> dict[str, dict]:
    """{name: parsed workflow} of what `onboard` renders for this policy at `pin`; the installed
    managed workflows when the templates cannot be rebuilt (no readable policy)."""
    import yaml  # deferred: the CLI must import without site dependencies

    names = MANAGED_WORKFLOWS + (() if cloud else MANAGED_IMPLEMENT_WORKFLOWS)
    agents = policy_doc.get("agents") if isinstance(policy_doc.get("agents"), dict) else {}
    implementer = (
        "cloud-routine" if cloud else POLICY_IMPLEMENTERS.get(str(agents.get("implementer")))
    )
    if implementer is not None and policy_doc:
        try:
            spec = _template_spec(
                policy_doc,
                project_id=project_id,
                platform_repository=platform_repository,
                platform_ref=pin,
                implementer=implementer,
                default_branch=default_branch,
                ready_label=_policy_ready_label(policy_doc),
            )
            rendered = render_onboarding(spec)
            return {
                name: yaml.safe_load(rendered[f".github/workflows/{name}"])
                for name in names
                if f".github/workflows/{name}" in rendered
            }
        except (OnboardError, KeyError, yaml.YAMLError):
            pass
    docs = {}
    for name in names:
        doc = _workflow_doc(base / ".github/workflows" / name)
        if doc is not None:
            docs[name] = doc
    return docs


def _runner_target_checks(
    base: Path,
    callers: Sequence[Path],
    platform_repository: str,
    load_callee: CalleeLoader | None,
    add: Callable[[Check], None],
) -> tuple[dict[str, set[str]], RunnerTarget | None, set[str] | None]:
    """The runner checks that need no GitHub API (Codex 4241050282: `doctor --local` runs them
    too): every job of every caller -- reusable calls followed, platform calls through
    `load_callee` -- and ci.yml's `test` job must target Unix (Linux for the Forge workflows),
    resolvable labels whose hard job-time limit fits the workloads. Returns the remaining label
    targets (availability is then checked against the repository's runners, remote only), ci.yml's
    target and its labels."""
    all_targets: dict[str, RunnerTarget] = {}
    for caller in callers:
        all_targets.update(
            _job_runner_targets(base, caller, platform_repository, load_callee=load_callee)
        )
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
    # Every Forge plan/implement workflow is Linux-only (reusable-implement.yml installs
    # bubblewrap with apt-get, reusable-plan.yml hashes with GNU sha256sum), in both modes: each
    # job of every caller, and the runner each hands a platform workflow. Only ci.yml (checked
    # below) may run on macOS.
    off_linux = {
        where: lbls
        for where, lbls in label_targets.items()
        if where not in on_windows and not _linux_labels(lbls)
    }
    if off_linux:
        add(
            Check(
                "Forge workflows run on Linux runners",
                False,
                LINUX_RUNNER_REQUIRED
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
    # ubuntu-slim kills a job at 15 minutes (Codex 4239397966): a failure, not a runner to
    # register. ci.yml's own check below reports its test job.
    too_short = {w: lbls for w, lbls in targets.items() if _short_job_labels(lbls)}
    if too_short:
        add(
            Check(
                "runners allow the workflows' job timeouts",
                False,
                SHORT_JOB_UNSUPPORTED
                + ": "
                + "; ".join(f"{w} {sorted(lbls)}" for w, lbls in sorted(too_short.items())),
            )
        )
    targets = {w: lbls for w, lbls in targets.items() if w not in too_short}
    return targets, ci_target, ci_labels


#: A hosted-runner-shaped label (`ubuntu-99.04`, `macos-99`, `windows-2099`): GitHub assigns
#: these names to its own images, so one GITHUB_HOSTED_LABELS does not list is a typo or an
#: image that does not exist (or no longer does) -- its job queues forever -- unless the set
#: also says `self-hosted`.
_HOSTED_LABEL_SHAPE = re.compile(r"(?i)(?:ubuntu|macos|windows)-[a-z0-9.-]+")
LOCAL_RUNNER_CHECK = "runner labels are recognised (availability is checked remotely)"
#: How `doctor --local` models a Forge platform reusable workflow it cannot fetch: the contract
#: every one of them declares -- a `runs_on` input (JSON, default "ubuntu-latest") that every
#: job runs on. The real callee at its pin is read in remote mode.
_LOCAL_PLATFORM_CALLEE = {
    "on": {
        "workflow_call": {"inputs": {"runs_on": {"type": "string", "default": '"ubuntu-latest"'}}}
    },
    "jobs": {"every-job": {"runs-on": "${{ fromJSON(inputs.runs_on) }}"}},
}


def local_runner_label_check(
    targets: dict[str, set[str]], ci_labels: set[str] | None
) -> Check | None:
    """`doctor --local`'s stand-in for the runner-availability checks: no runner list can be read,
    so a label set passes only when GitHub hosts it as a standard image or it names
    `self-hosted` (registration is confirmed by remote doctor). A hosted-runner-shaped label
    GitHub does not host fails (Codex 4241050282); a larger hosted runner or a custom label
    set without `self-hosted` is a TODO only remote doctor can clear."""
    every = dict(targets)
    if (
        ci_labels is not None
        and not _windows_labels(ci_labels)
        and not _short_job_labels(ci_labels)
    ):
        every["ci.yml:test"] = ci_labels
    unknown: list[str] = []
    unverified: list[str] = []
    for where, labels in sorted(every.items()):
        names = {str(x) for x in labels}
        if _standard_hosted(names):
            continue
        self_hosted = "self-hosted" in {x.lower() for x in names}
        bogus = sorted(
            x for x in names if _HOSTED_LABEL_SHAPE.fullmatch(x) and x not in GITHUB_HOSTED_LABELS
        )
        if bogus and not self_hosted:
            unknown.append(f"{where} {sorted(names)} ({', '.join(bogus)})")
        elif not self_hosted:
            unverified.append(f"{where} {sorted(names)}")
    if not every:
        return None
    if unknown:
        return Check(
            LOCAL_RUNNER_CHECK,
            False,
            "not a GitHub-hosted runner label (a job targeting it queues forever): "
            + "; ".join(unknown)
            + " → use a current label such as ubuntu-latest, or add `self-hosted` for a runner of "
            "your own",
        )
    if unverified:
        return Check(
            LOCAL_RUNNER_CHECK,
            False,
            "a larger hosted runner or custom runner labels, which only `sdlcctl doctor` (not "
            "--local) can match against this repository's runners: " + "; ".join(unverified),
            manual=True,
        )
    return Check(
        LOCAL_RUNNER_CHECK,
        True,
        "standard GitHub-hosted labels or `self-hosted` sets (registration is checked by remote "
        "doctor)",
    )


LEASE_BOT_CHECK = "lease markers trust the exact Publisher bot"


def lease_bot_check(login: str, *, cloud: bool) -> Check:
    """Codex 4242753917: lease markers from an App bot count only when the bot is named exactly
    (`[automation] publisher_bot_login`, FORGE_LEASE_BOT_LOGINS). In Actions mode the Publisher
    App posts the claims, so with none named its own claims are ignored and every
    implementation run is refused at the claim."""
    fix = (
        "re-run `sdlcctl onboard --force --publisher-bot-login <app-slug>[bot]` with the "
        "Publisher App's slug (with --apply it also sets the "
        f"{LEASE_BOT_LOGINS_ENV} repository variable)"
    )
    problem = _bot_login_field_problem(login)
    if problem:
        return Check(LEASE_BOT_CHECK, False, f"[automation] publisher_bot_login {problem} → {fix}")
    if login:
        return Check(LEASE_BOT_CHECK, True, f"{login} (and github-actions[bot])")
    if cloud:
        return Check(
            LEASE_BOT_CHECK,
            True,
            "no App bot is trusted (the cloud routine claims as the owner; "
            "[automation] publisher_bot_login is empty)",
        )
    return Check(
        LEASE_BOT_CHECK,
        False,
        "TODO: [automation] publisher_bot_login is empty, so no App bot's lease markers count "
        "and the Publisher App's own claims are ignored (every Actions implementation is "
        f"refused at its claim) → {fix}",
        manual=True,
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
    missing = [problem for r in required if (problem := _regular_file_problem(base, r))]
    settings = base / ".claude/settings.json"
    installed_policy = _installed_policy_doc(base)
    trust = policy_trust_mode(installed_policy)
    if settings.exists():
        missing += hook_problems(settings, trust)
    add(Check("required files present", not missing, ", ".join(missing) if missing else ""))
    if (base / "agentic-sdlc.toml").exists():
        add(trust_mode_check(trust))
        add(runner_isolation_check(base, installed_policy))
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
    repo_info = _safe_json(gh, ["api", f"repos/{project_id}"]) if remote else None
    if policy is not None:
        # Every plan/implement run passes $GITHUB_REPOSITORY -- GitHub's canonical spelling --
        # as --expected-project-id, and prepare-request compares it EXACTLY: the policy must
        # carry the canonical `full_name`, not merely a spelling the API also answers to.
        # Codex 4239868019: after a rename or transfer the API follows the redirect and answers
        # with the NEW `full_name`; that, not the slug doctor was given, is what runs pass.
        found = _canonical_full_name(repo_info, project_id) if remote else None
        canonical = found or project_id
        unread = remote and found is None
        same = not unread and policy.project_id == canonical
        if unread:
            detail = (
                f"could not read GET repos/{project_id} ("
                + (
                    repo_info.why
                    if isinstance(repo_info, Unreadable)
                    else "no valid full_name in the answer"
                )
                + ") → GitHub's name for the repository is unverified; rerun doctor"
            )
        elif same:
            detail = ""
        elif policy.project_id.casefold() == canonical.casefold():
            detail = (
                f"policy names {policy.project_id}; use {canonical} (GitHub's spelling, which "
                "every run passes as $GITHUB_REPOSITORY and prepare-request compares exactly)"
            )
        elif canonical.casefold() != project_id.casefold():
            detail = (
                f"policy names {policy.project_id}, but GitHub answers {project_id} as "
                f"{canonical} (renamed or transferred): every run passes "
                f"$GITHUB_REPOSITORY={canonical}, which prepare-request compares exactly → set "
                f'[project].id = "{canonical}" (re-run `sdlcctl onboard --force --project-id '
                f"{canonical}`)"
            )
        else:
            detail = f"policy names {policy.project_id}, not {canonical}"
        add(
            Check(
                "policy project id matches the repository",
                same,
                detail,
                manual=unread,
            )
        )

    if remote:
        # Every work request, label event, lease and issue-number operation is an ISSUE: a
        # repository with Issues disabled can be READY in every other respect and still unusable.
        issues = repo_info.get("has_issues") if isinstance(repo_info, dict) else None
        add(
            Check(
                ISSUES_ENABLED_CHECK,
                issues is True,
                "enabled"
                if issues is True
                else f"GitHub Issues are disabled on {project_id} → enable them (Settings → "
                "General → Features → Issues): work requests, labels and leases are issues"
                if issues is False
                else f"could not read whether GitHub Issues are enabled on {project_id} "
                f"(GET repos/{project_id}: has_issues) → confirm they are",
                manual=issues is not False,
            )
        )
        # Codex 4240170627: an archived (or disabled) repository is read-only -- claims, plan
        # comments, publisher pushes and draft PRs all fail -- whatever else is configured.
        writable, why = repository_writable(repo_info, project_id)
        add(
            Check(
                REPO_WRITABLE_CHECK,
                writable is True,
                "archived: false, disabled: false" if writable is True else why,
                manual=writable is None,
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
    if policy is not None:
        add(
            Check(
                "policy fields match what the generated automation requires",
                not (fields := policy_field_problems(policy, policy_doc, cloud=cloud)),
                "; ".join(fields)
                if fields
                else "provider github, approval labels, implementer and routing paths",
            )
        )
        # Codex 4241050285: the forbidden paths onboarding installs are the control plane
        # (workflows, the policy, .forge registries, .claude hooks, agent docs) plus secrets,
        # databases and media. A hand edit or merge that drops one makes that path writable to
        # every Forge patch, so each must survive into the EFFECTIVE (load_policy) list.
        # protected_paths has no generated baseline (only what the onboarding spec names).
        missing = [p for p in BASE_FORBIDDEN if p not in policy.forbidden_paths]
        add(
            Check(
                FORBIDDEN_BASELINE_CHECK,
                not missing,
                (
                    "agentic-sdlc.toml [policy] forbidden_paths no longer contains "
                    + ", ".join(missing)
                    + " → restore them (re-run onboard, or add them back by hand): without "
                    "them Forge patches may change those files"
                )
                if missing
                else f"all {len(BASE_FORBIDDEN)} generated forbidden_paths entries are enforced",
            )
        )
    if policy_doc:
        # The validator OnboardSpec runs: a value onboard would refuse is never "ready".
        value_problems = policy_value_problems(policy_doc_values(policy_doc))
        add(
            Check(
                "policy values are ones onboard accepts",
                not value_problems,
                "; ".join(value_problems)
                if value_problems
                else ", ".join(policy_field_location(f) for f in POLICY_FIELDS),
            )
        )
    if toml_path.exists():
        add(lease_bot_check(policy_bot_login(policy_doc), cloud=cloud))
    # Route mode is what the installed callers/policy ask for, not whether the registry exists;
    # an agent doctor cannot read may be `route`, so it is checked as one (fails closed).
    routed = (
        any((base / f).exists() for f in routing_files)
        or "routing" in policy_doc
        or "route" in agents.values()
        or bool(agent_problems)
    )
    missing_routing = [p for f in routing_files if (p := _regular_file_problem(base, f))]
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
                ("agent-auto-plan.yml", "preflight", auto_plan_condition(*labels)),
                ("agent-auto-implement.yml", "authorize", auto_implement_condition(*labels)),
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
    if policy is not None:
        file_drift = managed_file_drift(
            base, policy_doc, project_id, platform_repository, cloud=cloud
        )
        add(
            Check(
                "managed files match the generated files",
                not file_drift,
                "; ".join(file_drift) + " → re-run `sdlcctl onboard --force`"
                if file_drift
                else "hooks and issue template exact; AGENTS.md/CLAUDE.md contain the guide"
                + ("; routine doc exact" if cloud else ""),
            )
        )
    if plan.exists():
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
        # Codex 4240306498: the routine confirmation is local policy state, not a GitHub API
        # check, so `--local` must not report an unconfirmed cloud-routine repo READY.
        if cloud:
            add(cloud_routine_check(policy_doc))
        if policy is not None:
            add(planner_check(policy_doc))
        # Codex 4241050282: the runner targets are in the local workflow files (drift masks them
        # as tunable), so their syntax, hosted labels, Linux and timeout rules are checked here
        # too; only whether a runner is registered and online needs the API.
        local_targets, local_ci, local_ci_labels = _runner_target_checks(
            base, callers, platform_repository, lambda name, sha: _LOCAL_PLATFORM_CALLEE, add
        )
        if isinstance(local_ci, str):
            add(Check("ci.yml test job runs on an available runner", False, local_ci))
        elif local_ci_labels is not None and (
            _windows_labels(local_ci_labels) or _short_job_labels(local_ci_labels)
        ):
            add(
                Check(
                    "ci.yml test job runs on an available runner",
                    False,
                    f"labels {sorted(local_ci_labels)}: "
                    + (
                        WINDOWS_UNSUPPORTED
                        if _windows_labels(local_ci_labels)
                        else SHORT_JOB_UNSUPPORTED
                    ),
                )
            )
        label_check = local_runner_label_check(local_targets, local_ci_labels)
        if label_check is not None:
            add(label_check)
        if (base / "agentic-sdlc.toml").exists():
            add(
                approvals_check(
                    policy_required_approvals(installed_policy), cloud=cloud, writers=None
                )
            )
        return rep

    # --- platform reachable at that pin, and its reusable workflows accept the generated calls
    generated = _generated_workflows(
        base,
        policy_doc,
        project_id,
        platform_repository,
        cloud=cloud,
        default_branch=policy.default_branch if policy is not None else "main",
        pin=ref or "0" * 40,
    )
    dependencies: set[str] = set()
    caller_docs = [(f"{name} (generated)", doc) for name, doc in generated.items()]
    caller_docs += [(c.name, _workflow_doc(c) or {}) for c in callers]
    for _, doc in caller_docs:
        dependencies |= {u for u in workflow_uses(doc) if not u.endswith("@" + "0" * 40)}
    callees: dict[str, object] = {}
    # Why some platform workflow's actions could not be collected: the allowlist check then
    # cannot prove they are admitted (fail closed).
    uninspected: list[str] = []
    if ref:
        probes = ["reusable-plan.yml"]
        if not cloud:
            probes.append("reusable-implement.yml")
        calls: list[tuple[str, dict, str]] = []  # (where, job, callee file)
        for label, doc in caller_docs:
            jobs = doc.get("jobs") if isinstance(doc, dict) else None
            for job_name, job in jobs.items() if isinstance(jobs, dict) else ():
                if not isinstance(job, dict):
                    continue  # not a call; the drift and runner checks report the job
                found = _REMOTE_REUSABLE.fullmatch(str(job.get("uses") or "").strip())
                if found and found.group(1) == platform_repository:
                    calls.append((f"{label}:{job_name}", job, found.group(2)))
                    if found.group(2) not in probes:
                        probes.append(found.group(2))
        absent: list[str] = []
        unread_probes: list[str] = []
        for name in probes:
            blob = _safe_json(
                gh,
                [
                    "api",
                    f"repos/{platform_repository}/contents/.github/workflows/{name}?ref={_q(ref)}",
                ],
            )
            if isinstance(blob, Unreadable):
                if blob.not_found:  # the contents API's documented "no such file"
                    absent.append(name)
                else:
                    unread_probes.append(f"{name} ({blob.why})")
                continue
            raw = _decoded_blob(blob)
            if raw is None:
                callees[name] = None
                continue
            import yaml  # deferred: the CLI must import without site dependencies

            try:
                callees[name] = yaml.safe_load(raw.decode("utf-8", "replace"))
            except yaml.YAMLError:
                callees[name] = None
        if policy is not None:
            fetched_plan = callees.get("reusable-plan.yml")
            add(planner_check(policy_doc, fetched_plan if isinstance(fetched_plan, dict) else None))
        add(
            Check(
                "platform ref reachable (reusable workflows exist at pin)",
                not absent and not unread_probes,
                ref[:12]
                + (f": missing {', '.join(absent)}" if absent else "")
                + (f"; could not read {', '.join(unread_probes)}" if unread_probes else ""),
                manual=not absent and bool(unread_probes),
            )
        )
        # The platform reusable workflows as fetched at the pin: runner diagnosis reads their
        # jobs, and every platform workflow they call in turn (pinned) is walked too -- its
        # actions must be allowed and each nested call must match its callee's contract.
        load_callee = platform_callee_loader(
            gh, platform_repository, {(name, ref): doc for name, doc in callees.items()}
        )
        graph = platform_callee_graph(callees, ref, platform_repository, load_callee)
        for doc in graph.docs.values():
            dependencies |= workflow_uses(doc)
        uninspected += [f"{name} could not be read at {ref[:12]}" for name in absent]
        uninspected += [f"{entry} could not be read" for entry in unread_probes]
        uninspected += graph.uninspectable
        mismatches: list[str] = []
        for where, job, callee in calls:
            if callee in absent:
                continue  # reported above
            if callees.get(callee) is None:
                mismatches.append(f"{where}: could not read {callee} at {ref[:12]}")
                continue
            mismatches += reusable_call_mismatches(where, job, callee, callees[callee])
        for where, job, callee, callee_doc in graph.calls:
            if callee_doc is None:
                mismatches.append(f"{where}: could not read {callee} at its pin")
                continue
            mismatches += reusable_call_mismatches(where, job, callee, callee_doc)
        mismatches += [f"{why}: its call contract cannot be verified" for why in graph.unfollowed]
        mismatches += reusable_workflow_limit_problems(caller_docs, calls, graph, ref)
        default_permissions: list[TokenPermissions | str] = []

        def repo_default() -> TokenPermissions | str:
            if not default_permissions:  # read once, and only when some caller inherits it
                default_permissions.append(
                    default_token_permissions(
                        _safe_json(gh, ["api", f"repos/{project_id}/actions/permissions/workflow"])
                    )
                )
            return default_permissions[0]

        over_ceiling, unknown_ceiling = token_permission_problems(
            caller_docs,
            [c for c in calls if c[2] not in absent],
            graph,
            ref,
            platform_repository,
            repo_default,
        )
        mismatches += over_ceiling
        mismatches = list(dict.fromkeys(mismatches))
        add(
            Check(
                TOKEN_PERMISSIONS_CHECK,
                not unknown_ceiling,
                "; ".join(unknown_ceiling) + " → verify the token each call passes on by hand"
                if unknown_ceiling
                else "every reusable-workflow job requests no more than its caller grants"
                if not over_ceiling
                else "see the reusable-workflow contract check",
                manual=True,
            )
        )
        add(
            Check(
                PLATFORM_CONTRACT_CHECK,
                not mismatches,
                "; ".join(mismatches)
                + " → pin a platform ref whose reusable workflows match (onboard --platform-ref)"
                if mismatches
                else "every generated and installed call matches the inputs and secrets "
                f"declared at {ref[:12]}",
            )
        )
    else:
        load_callee = platform_callee_loader(gh, platform_repository, {})
        if policy is not None:
            add(planner_check(policy_doc))

    # --- a private platform must also admit this repository as a caller (Actions access)
    if repo_info is None:
        repo_info = _safe_json(gh, ["api", f"repos/{project_id}"])
    platform_private = _safe_json(gh, ["api", f"repos/{platform_repository}", "--jq", ".private"])
    # Only `private: false` PROVES a public platform. Anything else -- unreadable, or not a
    # boolean -- may be private or internal: the access check is an unmet manual requirement and
    # PLATFORM_READ_TOKEN stays required (fail closed).
    platform_public = platform_private is False
    if not platform_public and platform_private is not True:
        why = (
            platform_private.why
            if isinstance(platform_private, Unreadable)
            else f"unexpected answer {platform_private!r}"
        )
        add(
            Check(
                PLATFORM_ACCESS_CHECK,
                False,
                f"could not read whether {platform_repository} is public ({why}): if it is "
                f"private or internal, {project_id} must be admitted by its Actions access "
                "setting and PLATFORM_READ_TOKEN is required → confirm its visibility and rerun "
                "doctor",
                manual=True,
            )
        )
    elif platform_private is True:  # private or internal (both report private: true)
        platform_visibility = _safe_json(
            gh, ["api", f"repos/{platform_repository}", "--jq", "{visibility}"]
        )
        add(
            platform_access_check(
                gh,
                project_id,
                platform_repository,
                repo_info if isinstance(repo_info, dict) else {},
                _visibility(platform_visibility),
            )
        )

    # --- this repository's own Actions policy must let every generated workflow run
    if ref:
        dependencies |= {
            f"{platform_repository}/.github/workflows/{name}@{ref}"
            for name in ("reusable-plan.yml", *(() if cloud else ("reusable-implement.yml",)))
        }
    add(actions_permissions_check(gh, project_id, dependencies, uninspected))

    # --- default branch in the policy is the repository's actual default branch
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
    # Every required path is checked remotely even when it is not a file here (the local
    # check fails it already): it must be a regular file on the default branch too.
    managed = [
        r
        for r in dict.fromkeys(
            [*required, *(routing_files if routed else ()), "docs/forge/cloud-implementer.md"]
        )
        if r in required or (base / r).is_file()
    ]
    # The remote copies are also compared with their rendered form directly (not only with
    # this checkout), so a disabled hook on the default branch fails even if it was never pulled.
    remote_rendered = (
        rendered_managed_files(
            policy_doc,
            project_id,
            platform_repository,
            cloud=cloud,
            platform_ref=installed_platform_pin(base, platform_repository),
        )[0]
        if policy is not None
        else {}
    )
    unpushed = []
    for relative in managed:
        blob = _safe_json(
            gh, ["api", f"repos/{project_id}/contents/{relative}?ref={_q(remote_branch or 'HEAD')}"]
        )
        if isinstance(blob, Unreadable) and not blob.not_found:
            unpushed.append(f"{relative} (unreadable: {blob.why})")
            continue
        if isinstance(blob, list):
            unpushed.append(f"{relative} (a directory, not a file)")
            continue
        if isinstance(blob, dict) and blob.get("type") != "file":
            unpushed.append(f"{relative} (not a regular file: type {blob.get('type')!r})")
            continue
        remote_bytes = _decoded_blob(blob)
        if remote_bytes is None:
            unpushed.append(f"{relative} (missing)")
        elif _regular_file_problem(base, relative):
            unpushed.append(f"{relative} (not a regular file in this checkout)")
        elif remote_bytes != (base / relative).read_bytes():
            unpushed.append(f"{relative} (differs)")
        elif managed_file_problem(relative, remote_bytes, remote_rendered):
            unpushed.append(f"{relative} (differs from the generated file; onboard --force)")
    # A file another implementation mode (or routing) owns must be gone from the default branch
    # too: `onboard --force` into cloud-routine deletes the Actions implementer workflows only
    # locally, and until that deletion is pushed approval labels still launch them remotely.
    owned = {*required, *(routing_files if routed else ())}
    for relative in MODE_OWNED_FILES:
        if relative in owned:
            continue
        state = _remote_file_state(
            gh, ["api", f"repos/{project_id}/contents/{relative}?ref={_q(remote_branch or 'HEAD')}"]
        )
        if state == "present":
            unpushed.append(
                f"{relative} (present, but the {mode} mode does not own it: push the "
                "onboard --force changes first)"
            )
        elif state == "unknown":
            unpushed.append(f"{relative} (unverifiable: must be absent in the {mode} mode)")
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
    label_rows = _paged_items(gh, f"repos/{project_id}/labels?per_page=100")
    want = (
        {policy.ready_label, policy.human_review_label, policy.implementation_label}
        if policy
        else {"claude-ready", HUMAN_REVIEW_LABEL, IMPLEMENTATION_LABEL}
    ) | {IN_PROGRESS_LABEL}  # claim() adds it; a missing label fails every lease
    if isinstance(label_rows, Unreadable):
        add(
            Check(
                "Forge labels exist",
                False,
                f"could not read the labels of {project_id} ({label_rows.why}) → confirm "
                + ", ".join(sorted(want))
                + " exist (onboard --apply creates them)",
                manual=True,
            )
        )
    else:
        lacking = sorted(want - {x.get("name") for x in label_rows})
        add(
            Check(
                "Forge labels exist",
                not lacking,
                ", ".join(lacking) if lacking else ", ".join(sorted(want)),
            )
        )

    # --- ruleset
    rulesets = _repo_rulesets(project_id, gh)
    protect = (
        None
        if isinstance(rulesets, Unreadable)
        else next((r for r in rulesets if r.get("name") == RULESET_NAME), None)
    )
    if isinstance(rulesets, Unreadable):
        add(
            Check(
                f"ruleset '{RULESET_NAME}' active on default branch",
                False,
                f"could not read the rulesets of {project_id} ({rulesets.why}) → confirm "
                f"'{RULESET_NAME}' exists and is active",
                manual=True,
            )
        )
    elif protect is None:
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
                    required_approvals=policy_required_approvals(installed_policy),
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
    add(
        approvals_check(
            policy_required_approvals(installed_policy),
            cloud=cloud,
            # who could approve matters only where the owner authors the PR (cloud-routine)
            writers=other_writers(project_id, gh, me=authenticated_login(gh)) if cloud else None,
        )
    )

    # --- the EFFECTIVE rules of the default branch: rulesets inherited from the organization or
    # enterprise apply too, and a requirement no Forge PR can meet leaves every PR unmergeable
    # however correct `Protect main` is. Diagnosis only: `--apply` manages the repository's own.
    effective_branch = policy.default_branch if policy else "main"
    # Required contexts come from the workflows GitHub runs -- the default branch's set, read
    # once here and reused by the fork check -- never from this checkout, whose unpushed
    # workflows are reported only as drift. An unreadable set leaves them a manual TODO.
    remote_sets: dict[str, tuple[dict[str, bytes] | None, str]] = {}

    def remote_set(ref: str | None) -> tuple[dict[str, bytes] | None, str]:
        if (ref or "") not in remote_sets:
            remote_sets[ref or ""] = remote_workflow_files(gh, project_id, ref)
        return remote_sets[ref or ""]

    rules_files, rules_why = remote_set(effective_branch)
    # A disabled workflow does not respond to its triggers: it reports no context. Only ACTIVE
    # workflows are evidence; an unreadable state list leaves the contexts a manual TODO.
    states, states_why = remote_workflow_states(gh, project_id)
    if rules_files is not None and states is None:
        rules_files, rules_why = None, f"workflow states unreadable: {states_why}"
    inactive = frozenset(
        name
        for name in (rules_files or {})
        if states is not None and states.get(name) != WORKFLOW_ACTIVE
    )
    produced = (
        remote_check_contexts(
            rules_files, effective_branch, platform_repository, load_callee, inactive
        )
        if rules_files is not None
        else None
    )
    # The same set with its non-active workflows counted: which required context a disabled
    # workflow would report (enable it), as opposed to one nothing reports.
    produced_if_enabled = (
        remote_check_contexts(rules_files, effective_branch, platform_repository, load_callee)
        if rules_files is not None and inactive
        else produced
    )
    local_produced = workflow_check_contexts(
        base, effective_branch, platform_repository, load_callee, inactive
    )
    managed_names = [
        name
        for name in (*MANAGED_WORKFLOWS, *MANAGED_IMPLEMENT_WORKFLOWS)
        if (base / ".github/workflows" / name).exists() or name in (rules_files or {})
    ]
    if states is None:
        add(
            Check(
                WORKFLOWS_ACTIVE_CHECK,
                False,
                f"{states_why} → confirm in the Actions tab that "
                + ", ".join(managed_names)
                + " are enabled",
                manual=True,
            )
        )
    else:
        not_active = [
            f"{name} ({states.get(name, 'not registered in Actions')})"
            for name in managed_names
            if states.get(name) != WORKFLOW_ACTIVE
        ]
        add(
            Check(
                WORKFLOWS_ACTIVE_CHECK,
                not not_active,
                "not active: "
                + ", ".join(not_active)
                + f" → gh workflow enable <file> -R {project_id} (a workflow not registered yet "
                "appears once it is on the default branch)"
                if not_active
                else ", ".join(managed_names) + " active",
                manual=bool(not_active),
            )
        )

    def context_problems(required_contexts: list[str], problems: list[str]) -> list[str]:
        """`problems` for the required contexts, judged on the default branch's workflows; a
        context only this checkout reports is drift (push it); an unreadable set, a TODO."""
        if produced is None:
            missing = sorted(c for c in required_contexts if c)
            return (
                [
                    f"required status checks {', '.join(repr(c) for c in missing)} cannot be "
                    f"verified: default-branch workflows unreadable ({rules_why}) → confirm a "
                    f"workflow on {effective_branch} reports each on pull requests"
                ]
                if missing
                else []
            ) + problems
        drift = [
            f"local checkout: '{c}' is reported only by a workflow not on {effective_branch}"
            " → push it"
            for c in sorted(set(required_contexts))
            if c and not context_produced(c, produced) and context_produced(c, local_produced)
        ]
        disabled = [
            f"'{c}' is reported only by a workflow that is not active in GitHub Actions ("
            + ", ".join(sorted(inactive))
            + ") → enable it"
            for c in sorted(set(required_contexts))
            if c
            and not context_produced(c, produced)
            and produced_if_enabled is not None
            and context_produced(c, produced_if_enabled)
        ]
        return problems + drift + disabled

    # --slurp pages, each a list of rule objects; any other shape is unreadable, never "no rules"
    effective_rules = _paged_items(gh, f"repos/{project_id}/rules/branches/{_q(effective_branch)}")
    effective_queue: bool | None = None  # unknown until the effective rules are read
    # What the Actions publisher pushes (commit message, emails, branch), read from the pinned
    # reusable-implement.yml: metadata-pattern rules are judged against it.
    publisher: PublisherCommit | str
    # ... and WHO pushes it: the credential the pinned checkout persists for `git push`.
    pusher: PublisherPusher | str
    if cloud:
        publisher = pusher = "the cloud routine commits as its owner's account, in its own format"
    elif not ref:
        publisher = pusher = "the platform pin is unknown"
    else:
        implement_doc = load_callee("reusable-implement.yml", ref)
        if implement_doc is None:
            publisher = pusher = f"reusable-implement.yml is unreadable at {ref[:12]}"
        else:
            publisher = publisher_commit(implement_doc)
            pusher = publisher_push_actor(implement_doc)
    if isinstance(effective_rules, Unreadable):
        # `Protect main` (generated) requires REQUIRED_CHECK whatever else applies: its
        # ambiguity is judged even when the effective rules cannot be read.
        protect_ambiguous = (
            ambiguous_context(REQUIRED_CHECK, produced) if produced is not None else None
        )
        add(
            Check(
                EFFECTIVE_RULES_CHECK,
                False,
                f"could not read the rules that apply to {effective_branch} "
                f"(GET repos/{project_id}/rules/branches/{effective_branch}) → check org and "
                "enterprise rulesets by hand"
                + (f"; ruleset '{RULESET_NAME}': {protect_ambiguous}" if protect_ambiguous else ""),
                manual=True,
            )
        )
    else:
        rules = effective_rules
        effective_queue = any(r.get("type") == "merge_queue" for r in rules)
        required_contexts = [
            str((check or {}).get("context") or "")
            for rule in rules
            if rule.get("type") == "required_status_checks"
            for check in ((rule.get("parameters") or {}).get("required_status_checks") or [])
            if isinstance(check, dict)
        ]
        unverifiable = ("required_status_checks", "merge_queue")
        checked = (
            rules
            if produced is not None
            else [r for r in rules if r.get("type") not in unverifiable]
        )
        problems = effective_rule_problems(
            checked, produced or ProducedContexts(merge_group_declared=True), publisher
        )
        if produced is None and any(r.get("type") == "merge_queue" for r in rules):
            problems.append(
                f"a merge queue is required, and whether a workflow on {effective_branch} runs on "
                f"merge_group cannot be verified ({rules_why})"
            )
        problems = context_problems(required_contexts, problems)
        inherited = sorted(
            {
                f"{r.get('ruleset_source_type')} {r.get('ruleset_source')}"
                for r in rules
                if r.get("ruleset_source_type") not in (None, "Repository")
            }
        )
        add(
            Check(
                EFFECTIVE_RULES_CHECK,
                not problems,
                "; ".join(problems)
                if problems
                else f"{len(rules)} rules on {effective_branch}"
                + (f", inherited from {', '.join(inherited)}" if inherited else ""),
                manual=True,
            )
        )

    # Every ruleset that applies to the repository (org/enterprise ones included): listed once.
    listed = _paged_items(gh, f"repos/{project_id}/rulesets?includes_parents=true")

    # --- push rulesets reject a push by its FILES (path, extension, path length, size) for
    # every actor and every branch, and none of them shows up in a branch's effective rules.
    if isinstance(listed, Unreadable):
        push_problems = [
            f"could not list the rulesets that apply to {project_id} ({listed.why}) → check "
            "for active push rulesets restricting paths, extensions, path length or file size"
        ]
    else:
        forbidden = policy.forbidden_paths if policy is not None else None
        push_problems = push_ruleset_problems(
            [
                (
                    summary,
                    _safe_json(gh, ["api", f"repos/{project_id}/rulesets/{summary.get('id')}"]),
                )
                for summary in listed
                if _push_ruleset_candidate(summary)
            ],
            forbidden,
        )
    add(
        Check(
            PUSH_RULESETS_CHECK,
            not push_problems,
            "; ".join(dict.fromkeys(push_problems))
            if push_problems
            else "no active push ruleset restricts what a Forge patch may contain",
            manual=True,
        )
    )

    # --- Codex 4241196640: the cloud routine creates and pushes forge/issue-<N> as the owner:
    # the same branch-domain checks, against its branch and (user) actor.
    if cloud:
        add(_cloud_branch_rules_check(gh, project_id, listed, effective_branch))

    # --- the publisher creates and pushes agent/... branches: rulesets targeting only that
    # namespace (org/enterprise ones included) are invisible in the default branch's rules.
    if not cloud:
        if isinstance(publisher, str):
            add(
                Check(
                    PUBLISHER_BRANCH_RULES_CHECK,
                    False,
                    f"the publisher's branch name could not be read ({publisher}) → check that "
                    "no ruleset restricts creating or pushing Forge's agent/** branches",
                    manual=True,
                )
            )
        else:
            # The effective rules of ONE branch speak for every agent/issue-<N>-<RUN> only if
            # no ruleset (repository, organization or enterprise) targets some of them and not
            # others: each ruleset's ref_name targeting is judged over the whole domain first.
            branch_problems: list[str] = []
            if isinstance(listed, Unreadable):
                branch_problems.append(
                    f"could not list the rulesets that apply to {project_id} ({listed.why}) → "
                    "check that no org or enterprise ruleset targets only some agent/** branches"
                )
            else:
                branch_problems += publisher_branch_scope_problems(
                    _branch_ruleset_details(gh, project_id, listed),
                    publisher.branch,
                    effective_branch,
                )
            owner, _, name = project_id.partition("/")
            branch_problems += classic_publisher_branch_problems(
                _safe_json(
                    gh,
                    [
                        "api",
                        "graphql",
                        "-f",
                        f"query={CLASSIC_PROTECTION_QUERY}",
                        "-F",
                        f"owner={owner}",
                        "-F",
                        f"name={name}",
                    ],
                ),
                publisher.branch,
                pusher,
            )
            branch = publisher.branch.example
            branch_rules = _paged_items(gh, f"repos/{project_id}/rules/branches/{_q(branch)}")
            if isinstance(branch_rules, Unreadable):
                branch_problems.append(
                    f"could not read the rules that apply to {branch} "
                    f"(GET repos/{project_id}/rules/branches/{{branch}}) → check org and "
                    "enterprise rulesets targeting agent/** by hand"
                )
            else:
                branch_problems += publisher_branch_rule_problems(
                    branch_rules, publisher, branch, pusher
                )
            branch_problems = list(dict.fromkeys(branch_problems))
            add(
                Check(
                    PUBLISHER_BRANCH_RULES_CHECK,
                    not branch_problems,
                    "; ".join(branch_problems)
                    if branch_problems
                    else "no rule blocks the publisher's push to any " + publisher.branch.template,
                    manual=True,
                )
            )

    # --- classic branch protection is enforced alongside rulesets and is NOT in rules/branches
    try:
        legacy = json.loads(
            gh(
                [
                    "api",
                    f"repos/{project_id}/branches/{_q(effective_branch)}/protection",
                ]
            )
            or "null"
        )
        legacy_why = "" if isinstance(legacy, dict) else "unreadable answer"
    except OnboardError as exc:
        legacy = None
        legacy_why = "absent" if gh_http_status(exc) == 404 else str(exc)[:120]
    except json.JSONDecodeError:
        legacy, legacy_why = None, "unreadable answer"
    if legacy_why == "absent":
        add(
            Check(
                LEGACY_PROTECTION_CHECK, True, f"no classic protection rule on {effective_branch}"
            )
        )
    elif not isinstance(legacy, dict):
        add(
            Check(
                LEGACY_PROTECTION_CHECK,
                False,
                f"cannot read branch protection of {effective_branch} ({legacy_why}; needs admin: "
                f"GET repos/{project_id}/branches/{effective_branch}/protection) → check its "
                "required checks, signatures and push restrictions by hand",
                manual=True,
            )
        )
    else:
        legacy_checks = legacy.get("required_status_checks")
        legacy_contexts = (
            [str(c) for c in legacy_checks.get("contexts") or []]
            + [
                str(item.get("context"))
                for item in legacy_checks.get("checks") or []
                if isinstance(item, dict) and item.get("context")
            ]
            if isinstance(legacy_checks, dict)
            else []
        )
        legacy_problems = context_problems(
            legacy_contexts,
            legacy_protection_problems(
                legacy if produced is not None else {**legacy, "required_status_checks": None},
                produced or ProducedContexts(),
                effective_queue,
            ),
        )
        add(
            Check(
                LEGACY_PROTECTION_CHECK,
                not legacy_problems,
                "; ".join(legacy_problems)
                if legacy_problems
                else f"classic protection on {effective_branch} is compatible",
                manual=True,
            )
        )

    # --- variables & secrets (names only). Workflows also see the organization's secrets and
    # variables shared with this repository (Codex 4243386524): an organization-owned repository
    # unions the inherited inventories in (a repository variable wins over an organization one of
    # the same name, as in GitHub). An inherited inventory that cannot be read leaves what is
    # missing at repository scope UNVERIFIABLE -- a warning, never "missing".
    variable_rows = _paged_items(gh, f"repos/{project_id}/actions/variables", "variables")
    secret_names = _paged_names(gh, f"repos/{project_id}/actions/secrets", "secrets")
    org_owned = (
        isinstance(repo_info, dict)
        and isinstance(repo_info.get("owner"), dict)
        and repo_info["owner"].get("type") == "Organization"
    )
    org_variable_rows: list[dict] | Unreadable = (
        _paged_items(gh, f"repos/{project_id}/actions/organization-variables", "variables")
        if org_owned
        else []
    )
    org_secret_names: set[str] | Unreadable = (
        _paged_names(gh, f"repos/{project_id}/actions/organization-secrets", "secrets")
        if org_owned
        else set()
    )
    org_vars_unread = org_variable_rows.why if isinstance(org_variable_rows, Unreadable) else ""
    org_secrets_unread = org_secret_names.why if isinstance(org_secret_names, Unreadable) else ""
    variable_values = (
        {}
        if isinstance(org_variable_rows, Unreadable)
        else {str(v.get("name")): str(v.get("value") or "") for v in org_variable_rows}
    )
    if not isinstance(variable_rows, Unreadable):
        variable_values.update(
            {str(v.get("name")): str(v.get("value") or "") for v in variable_rows}
        )
    variables = {name for name, value in variable_values.items() if value.strip()}
    empty_vars = sorted(name for name, value in variable_values.items() if not value.strip())
    secrets = (set() if isinstance(secret_names, Unreadable) else set(secret_names)) | (
        set() if isinstance(org_secret_names, Unreadable) else org_secret_names
    )
    need_vars = set() if cloud else {"PUBLISHER_APP_CLIENT_ID"}
    need_secrets = {"CLAUDE_CODE_OAUTH_TOKEN"} | (set() if cloud else {"PUBLISHER_APP_PRIVATE_KEY"})
    if not platform_public:  # GITHUB_TOKEN cannot check out another private repository
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
    # set nowhere we could read, and possibly inherited from an organization we could not read
    unverifiable_vars = [n for n in mv if n not in variable_values] if org_vars_unread else []
    if isinstance(variable_rows, Unreadable) and need_vars:
        add(
            Check(
                "repo variables set (non-empty)",
                False,
                f"could not read the variables of {project_id} ({variable_rows.why}) → confirm "
                + ", ".join(sorted(need_vars))
                + " are set",
                manual=True,
            )
        )
    elif mv and len(unverifiable_vars) == len(mv):
        add(
            Check(
                "repo variables set (non-empty)",
                True,
                "WARNING: not set at repository scope: "
                + ", ".join(mv)
                + f"; the organization variables {project_id} inherits could not be read "
                f"({org_vars_unread}) → confirm they are set there, or set them on the repository",
                warning=True,
            )
        )
    else:
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
    if ms and org_secrets_unread and not isinstance(secret_names, Unreadable):
        add(
            Check(
                "repo secrets set (by name)",
                True,
                "WARNING: not set at repository scope: "
                + ", ".join(ms)
                + f"; the organization secrets {project_id} inherits could not be read "
                f"({org_secrets_unread}) → confirm they are shared with it, or gh secret set NAME "
                f"--repo {project_id}",
                warning=True,
            )
        )
    else:
        add(
            Check(
                "repo secrets set (by name)",
                not ms,
                (
                    f"could not read the secret names of {project_id} ({secret_names.why}) → "
                    "confirm: " + ", ".join(ms)
                    if isinstance(secret_names, Unreadable)
                    else "missing: " + ", ".join(ms) + " → gh secret set NAME --repo " + project_id
                )
                if ms
                else ", ".join(sorted(need_secrets)),
                manual=True,
            )
        )

    # --- runners: every job of every active caller, and ci.yml's `test` job, must run somewhere
    # that exists, or the run (or every PR's required check) sits queued forever.
    # The same resolver as the fork-exposure check: local reusable workflows are followed
    # into their own jobs, pinned platform calls through the callee fetched at that SHA;
    # anything it cannot prove (a runner group, an expression, a third-party workflow, an
    # unreadable callee) is a str reason.
    targets, ci_target, ci_labels = _runner_target_checks(
        base, callers, platform_repository, load_callee, add
    )
    need_runners = any(not _standard_hosted(t) for t in [*targets.values(), ci_labels or set()])
    runner_rows = (
        _paged_items(gh, f"repos/{project_id}/actions/runners", "runners") if need_runners else []
    )
    # Unreadable proves no runner: every self-hosted target stays stranded (fails closed).
    runners_unread = runner_rows.why if isinstance(runner_rows, Unreadable) else ""
    runners = [] if isinstance(runner_rows, Unreadable) else runner_rows
    stranded = {
        where: labels for where, labels in targets.items() if not _runner_available(labels, runners)
    }
    if targets and all(_standard_hosted(t) for t in targets.values()):
        add(Check("runner", True, "GitHub-hosted runners (billed minutes on private repos)"))
    elif targets:
        online = [r for r in runners if r.get("status") == "online"]
        add(
            Check(
                "self-hosted runner online for this repo",
                not stranded,
                (
                    f"could not read the runners of {project_id} ({runners_unread}) → confirm an "
                    "online runner carries "
                    + "; ".join(f"{w} {sorted(lbls)}" for w, lbls in sorted(stranded.items()))
                    if runners_unread
                    else "no online non-Windows runner for "
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
        ci_short = bool(_short_job_labels(ci_labels))
        ci_larger = sorted(ci_labels & GITHUB_LARGER_RUNNER_LABELS)
        add(
            Check(
                "ci.yml test job runs on an available runner",
                ci_ok,
                f"labels {sorted(ci_labels)}: {WINDOWS_UNSUPPORTED}"
                if ci_windows
                else f"labels {sorted(ci_labels)}: {SHORT_JOB_UNSUPPORTED}"
                if ci_short
                else ", ".join(sorted(ci_labels))
                if _standard_hosted(ci_labels)
                else f"{', '.join(ci_larger)} is a larger GitHub-hosted runner: "
                + (
                    "an online runner of the repository carries it"
                    if ci_ok
                    else "it needs an eligible GitHub Team/Enterprise plan with the runner made "
                    f"available to {project_id}, which doctor cannot verify"
                    + (f" (runner list unreadable: {runners_unread})" if runners_unread else "")
                    + " → confirm, or use a standard label such as macos-latest"
                )
                if ci_larger
                else f"labels {sorted(ci_labels)}: "
                + (
                    "online runner found"
                    if ci_ok
                    else f"the runner list is unreadable ({runners_unread}) → confirm an online "
                    "runner carries all of them"
                    if runners_unread
                    else "not a GitHub-hosted label and no online non-Windows runner carries "
                    "all of them"
                ),
                manual=not ci_ok and not ci_windows and not ci_short,
            )
        )

    # --- fork pull-request jobs run fork code: never on a persistent runner. Public repositories,
    # and private/internal ones unless forking or fork-PR workflows are PROVEN off; an unread
    # policy fails closed as an owner TODO.
    fork_state, fork_why = fork_pull_request_exposure(repo_info, gh, project_id)
    forking_off = isinstance(repo_info, dict) and repo_info.get("allow_forking") is False
    if fork_state == "safe" and not forking_off:
        # Codex 4240421149: disabling fork pull-request WORKFLOWS stops `pull_request*` runs only;
        # a fork PR merged through the merge queue still runs in every `merge_group` workflow.
        queued = fork_runner_exposure(
            base,
            gh,
            project_id,
            platform_repository,
            actual_branch,
            load_callee,
            remote=remote_set(actual_branch),
            merge_group_only=True,
        )
        add(
            Check(
                FORK_RUNNER_CHECK,
                not queued,
                f"{fork_why}, but fork pull requests merged through the merge queue would "
                "execute on runners not proven GitHub-hosted: "
                + "; ".join(queued)
                + f" → set runs-on to a GitHub-hosted label (e.g. {PUBLIC_CI_RUNS_ON[0]}), or "
                "disable forking for this repository"
                if queued
                else f"{fork_why}: fork pull requests run no workflows "
                "(and no merge_group job runs off GitHub-hosted runners)",
            )
        )
    elif fork_state == "safe":
        add(Check(FORK_RUNNER_CHECK, True, f"{fork_why}: fork pull requests run no workflows"))
    else:
        exposed = fork_runner_exposure(
            base,
            gh,
            project_id,
            platform_repository,
            actual_branch,
            load_callee,
            remote=remote_set(actual_branch),
        )
        add(
            Check(
                FORK_RUNNER_CHECK,
                not exposed,
                f"{fork_why}; fork pull requests "
                + ("would" if fork_state == "exposed" else "may")
                + " execute on runners not proven GitHub-hosted: "
                + "; ".join(exposed)
                + f" → set runs-on to a GitHub-hosted label (e.g. {PUBLIC_CI_RUNS_ON[0]})"
                + (
                    ", or disable forking / fork pull-request workflows for this repository"
                    if fork_state == "unknown" or fork_why.startswith(("private", "internal"))
                    else ""
                )
                if exposed
                else f"{fork_why}; every pull_request- and merge_group-triggered job is "
                "GitHub-hosted",
                manual=bool(exposed) and fork_state == "unknown",
            )
        )

    # --- publisher app installed on this repo (Actions implementer publishes PRs through it)
    if cloud:
        add(
            Check(
                "Publisher GitHub App", True, "not needed: the cloud routine opens PRs as the owner"
            )
        )
        add(cloud_routine_check(policy_doc))
        return rep
    installs = _paged_items(gh, "/user/installations", "installations")
    if isinstance(installs, Unreadable):
        add(
            Check(
                "Publisher GitHub App installed on this repo",
                False,
                f"could not read the app installations visible to this user ({installs.why}) → "
                f"confirm the Agentic SDLC Publisher app is installed on {project_id}",
                manual=True,
            )
        )
        return rep
    bot_login = policy_bot_login(policy_doc)
    pinned_slug = bot_login[: -len("[bot]")] if bot_login.endswith("[bot]") else ""
    apps = [
        i
        for i in installs
        if PUBLISHER_APP_SLUG_HINT in str(i.get("app_slug", "")).lower()
        or (pinned_slug and str(i.get("app_slug", "")).lower() == pinned_slug)
    ]
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
    # Codex 4242753917: the bot whose lease markers count must BE the App the client ID names,
    # and a FORGE_LEASE_BOT_LOGINS variable, when set, must name the same bot.
    if apps and bot_login:
        bots = sorted({f"{str(a['app_slug']).lower()}[bot]" for a in apps})
        variable = variable_values.get(LEASE_BOT_LOGINS_ENV, "").strip()
        wrong = []
        if bot_login.casefold() not in bots:
            wrong.append(
                f"[automation] publisher_bot_login is {bot_login}, but the Publisher App "
                f"(PUBLISHER_APP_CLIENT_ID) posts as {', '.join(bots)}"
            )
        if variable and {v.strip().casefold() for v in variable.split(",") if v.strip()} != {
            bot_login.casefold()
        }:
            wrong.append(f"the {LEASE_BOT_LOGINS_ENV} variable is {variable!r}, not {bot_login}")
        add(
            Check(
                "lease bot login is the Publisher App's",
                not wrong,
                "; ".join(wrong) + " → re-run `sdlcctl onboard --force --apply "
                "--publisher-bot-login <app-slug>[bot]`"
                if wrong
                else bot_login,
            )
        )
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
        holding = []
        unread_installs = []
        for app in apps:
            repos = _paged_items(
                gh, f"/user/installations/{app['id']}/repositories", "repositories"
            )
            if isinstance(repos, Unreadable):
                unread_installs.append(f"installation {app['id']} ({repos.why})")
            elif project_id in {str(r.get("full_name")) for r in repos}:
                holding.append(app)
        if not holding and unread_installs:
            detail = (
                "could not read the repositories of "
                + ", ".join(unread_installs)
                + f" → confirm {project_id} is among the app's repositories"
            )
        elif not holding:
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
