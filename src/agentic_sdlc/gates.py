"""Run trusted project verification commands with the shell semantics of the generated CI.

Each gate is ONE shell command line, run exactly as the generated `ci.yml` runs it: a GitHub
Actions `run:` step with no `shell:` key on Linux is `bash -e {0}` (`sh -e {0}` when no bash is
found), so `run-gates` runs `bash -e -c <command>` (or `sh -e -c`), in its own process, from the
repository root. `cd sub && make test`, `VAR=x pytest` and `true && false` (which fails) mean
here what they mean in CI (Codex 4243193109: tokenizing the line and executing the first word
let `true && false` pass and turned `VAR=x cmd` into a missing program). What differs from CI is
only the environment: an allowlist, never the runner's credentials or control files.

The interpreter must be CI's too (Codex 4243659485): before any gate runs, the `python3` on the
gate environment's PATH is asked its version, and a major.minor other than the policy's
`[ci] python_version` (what ci.yml's setup-python installs) is reported as a warning, as is an
unset version or a missing python3. A warning, not a failure: the platform's reusable-implement
`verify` job installs `[ci] python_version` with setup-python before `run-gates`, but other
callers (and a policy without the version) run whatever interpreter they have. A malformed
version still fails.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any


class GateError(ValueError):
    """Raised when verification configuration is invalid."""


_BASE_ENVIRONMENT = {
    "CI",
    "COMSPEC",
    "CONDA_PREFIX",
    "GITHUB_ACTIONS",
    "GITHUB_BASE_REF",
    "GITHUB_HEAD_REF",
    "GITHUB_REF",
    "GITHUB_REPOSITORY",
    "GITHUB_SHA",
    "GITHUB_WORKSPACE",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "PATH",
    "PATHEXT",
    "RUNNER_ARCH",
    "RUNNER_OS",
    "SHELL",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "TZ",
    "USER",
    "VIRTUAL_ENV",
    "WINDIR",
}
_FORBIDDEN_ENVIRONMENT = {
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
    "ACTIONS_RUNTIME_TOKEN",
    "ANTHROPIC_API_KEY",
    "GH_TOKEN",
    "GITHUB_ENV",
    "GITHUB_OUTPUT",
    "GITHUB_PATH",
    "GITHUB_STEP_SUMMARY",
    "GITHUB_TOKEN",
    "OPENAI_API_KEY",
    "PLATFORM_READ_TOKEN",
    "PUBLISHER_APP_PRIVATE_KEY",
}


def _load(path: str | Path) -> tuple[dict[str, str], dict[str, Any], str | None]:
    """The policy's `[commands]`, `[verification]` and `[ci] python_version` (None: unset)."""
    with Path(path).open("rb") as handle:
        document = tomllib.load(handle)
    raw_commands = document.get("commands", {})
    verification = document.get("verification", {})
    ci = document.get("ci", {})
    if not isinstance(raw_commands, dict) or not isinstance(verification, dict):
        raise GateError("commands and verification must be TOML tables")
    if not isinstance(ci, dict):
        raise GateError("ci must be a TOML table")
    commands = {name: value for name, value in raw_commands.items() if isinstance(value, str)}
    python_version = ci.get("python_version")
    if python_version is not None and (
        not isinstance(python_version, str) or not _PYTHON_VERSION.fullmatch(python_version)
    ):
        raise GateError("ci.python_version must look like 3.12 or 3.12.4")
    return commands, verification, python_version


#: `[ci] python_version`'s shape (onboard writes and doctor checks the same one).
_PYTHON_VERSION = re.compile(r"^\d+\.\d+(?:\.\d+)?$")
#: Printed by the gate interpreter, run isolated (`-I`): nothing of the repository is imported.
_VERSION_PROBE = "import sys; print('%d.%d.%d' % sys.version_info[:3])"


#: The interpreter names a gate command line may invoke: onboard's default setup and quality
#: commands call `python`, the policy engine and many hand-written commands `python3`, and the
#: two can be different installations on one PATH (Codex 4246082577).
GATE_PYTHON_NAMES = ("python3", "python")


def gate_python_version(
    environment: dict[str, str], root: Path, name: str = "python3"
) -> tuple[str, str]:
    """(path, `X.Y.Z`) of the `name` interpreter the gates run: the first on the gate
    environment's PATH, asked for its version in that same environment. GateError when there is
    none or it does not answer as a Python."""
    found = shutil.which(name, path=environment.get("PATH", os.defpath))
    if not found:
        raise GateError(f"no {name} on the verification gates' PATH")
    try:
        probe = subprocess.run(
            [found, "-I", "-c", _VERSION_PROBE],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise GateError(f"{found} could not be run to read its version: {error}") from error
    version = probe.stdout.strip()
    if probe.returncode != 0 or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise GateError(f"{found} did not report a Python version (exit {probe.returncode})")
    return found, version


def check_gate_python(
    python_version: str | None, environment: dict[str, str], root: Path
) -> str | None:
    """A warning (never a failure) when an interpreter the gates may run -- `python3` or
    `python`, each the first on the gate PATH -- is not the policy's `[ci] python_version`
    (major.minor), the interpreter the repository's own ci.yml installs with setup-python (Codex
    4243659485, 4246082577); when the two names resolve to different versions; when neither
    can be read; or when the policy sets no version. None when every one found matches.

    A name absent from the PATH is not reported on its own: a gate that invokes it fails
    outright, which needs no warning."""
    found: list[tuple[str, str]] = []
    errors: list[str] = []
    for name in GATE_PYTHON_NAMES:
        try:
            found.append(gate_python_version(environment, root, name))
        except GateError as error:
            if shutil.which(name, path=environment.get("PATH", os.defpath)):
                errors.append(str(error))  # present but unreadable: say so
    if not found:
        error = "; ".join(errors) or (
            "no " + " or ".join(GATE_PYTHON_NAMES) + " on the verification gates' PATH"
        )
        if python_version is None:
            return f"[ci] python_version is not set ({error})"
        return f"cannot confirm the gates run under Python {python_version}: {error}"
    running = ", ".join(f"{path} (Python {version})" for path, version in found)
    if python_version is None:
        return (
            f"[ci] python_version is not set; the gates run under {running}, which nothing "
            "checks against the interpreter CI installs"
        )
    wanted = python_version.split(".")[:2]
    off = [(path, version) for path, version in found if version.split(".")[:2] != wanted]
    if off:
        return (
            "the verification gates run under "
            + ", ".join(f"{path} (Python {version})" for path, version in off)
            + f", but the policy's [ci] python_version is {python_version}: results may differ "
            f"from CI; install Python {'.'.join(wanted)} (actions/setup-python) before run-gates "
            "so that both python3 and python resolve to it, or change [ci] python_version"
        )
    if errors:
        return f"cannot confirm the gates run under Python {python_version}: " + "; ".join(errors)
    return None


def _environment(verification: dict[str, Any]) -> dict[str, str]:
    allowlist = verification.get("environment_allowlist", [])
    if not isinstance(allowlist, list) or not all(isinstance(name, str) for name in allowlist):
        raise GateError("verification.environment_allowlist must be an array of names")
    forbidden = sorted(set(allowlist) & _FORBIDDEN_ENVIRONMENT)
    if forbidden:
        raise GateError("verification environment exposes protected names: " + ", ".join(forbidden))
    names = _BASE_ENVIRONMENT | set(allowlist)
    return {name: os.environ[name] for name in names if name in os.environ}


#: Where the gate shell is looked up first (never a PATH search alone: a workspace-controlled
#: PATH entry must not decide which shell interprets the gate).
_SHELL_CANDIDATES = ("/bin/bash", "/usr/bin/bash", "/bin/sh", "/usr/bin/sh")


def gate_shell() -> list[str]:
    """The argv prefix the generated CI's default `run:` shell uses on Linux (`bash -e`, else
    `sh -e`), ending in `-c`; the gate's command line follows it as ONE argument."""
    for candidate in _SHELL_CANDIDATES:
        if Path(candidate).is_file():
            return [candidate, "-e", "-c"]
    for name in ("bash", "sh"):
        found = shutil.which(name, path="/usr/bin:/bin")
        if found:
            return [found, "-e", "-c"]
    raise GateError("no POSIX shell (bash or sh) to run the verification gates with")


def run_gates(
    config: str | Path,
    repository: str | Path,
) -> dict[str, Any]:
    commands, verification, python_version = _load(config)
    names = verification.get("gates", list(commands))
    allowed_exit_codes = verification.get("allowed_exit_codes", {})
    timeouts = verification.get("timeout_seconds", {})
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise GateError("verification.gates must be an array of command names")
    if not isinstance(allowed_exit_codes, dict) or not isinstance(timeouts, dict):
        raise GateError("verification exit codes and timeouts must be TOML tables")
    environment = _environment(verification)

    results = []
    root = Path(repository)
    warning = check_gate_python(python_version, environment, root)
    if warning:
        print(f"::warning::{warning}", file=sys.stderr, flush=True)
    for name in names:
        command = commands.get(name)
        if command is None:
            raise GateError(f"verification gate has no command: {name}")
        if not command.strip():
            raise GateError(f"verification command is empty: {name}")
        if "\n" in command or "\r" in command or "\0" in command:
            raise GateError(f"verification command must be a single line: {name}")
        arguments = [*gate_shell(), command]
        accepted = allowed_exit_codes.get(name, [0])
        if not isinstance(accepted, list) or not all(isinstance(code, int) for code in accepted):
            raise GateError(f"allowed exit codes are invalid: {name}")
        timeout = int(timeouts.get(name, 1800))
        if timeout < 1 or timeout > 7200:
            raise GateError(f"verification timeout is outside 1-7200 seconds: {name}")

        print(f"::group::Agentic SDLC gate: {name}", flush=True)
        started = time.monotonic()
        try:
            result = subprocess.run(
                arguments,
                cwd=root,
                env=environment,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            print("::endgroup::", flush=True)
            raise GateError(f"verification gate timed out: {name}") from error
        duration = round(time.monotonic() - started, 3)
        print("::endgroup::", flush=True)
        results.append(
            {
                "name": name,
                "command": arguments,
                "exitCode": result.returncode,
                "durationSeconds": duration,
                "passed": result.returncode in accepted,
            }
        )
        if result.returncode not in accepted:
            break
    return {"passed": bool(results) and all(item["passed"] for item in results), "gates": results}


def write_report(report: dict[str, Any], output: str | Path) -> None:
    Path(output).write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
