"""Run trusted project verification commands with the shell semantics of the generated CI.

Each gate is ONE shell command line, run exactly as the generated `ci.yml` runs it: a GitHub
Actions `run:` step with no `shell:` key on Linux is `bash -e {0}` (`sh -e {0}` when no bash is
found), so `run-gates` runs `bash -e -c <command>` (or `sh -e -c`), in its own process, from the
repository root. `cd sub && make test`, `VAR=x pytest` and `true && false` (which fails) mean
here what they mean in CI (Codex 4243193109: tokenizing the line and executing the first word
let `true && false` pass and turned `VAR=x cmd` into a missing program). What differs from CI is
only the environment: an allowlist, never the runner's credentials or control files.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
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


def _load(path: str | Path) -> tuple[dict[str, str], dict[str, Any]]:
    with Path(path).open("rb") as handle:
        document = tomllib.load(handle)
    raw_commands = document.get("commands", {})
    verification = document.get("verification", {})
    if not isinstance(raw_commands, dict) or not isinstance(verification, dict):
        raise GateError("commands and verification must be TOML tables")
    commands = {name: value for name, value in raw_commands.items() if isinstance(value, str)}
    return commands, verification


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
    commands, verification = _load(config)
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
