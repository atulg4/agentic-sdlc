"""Bounded operator invocation of the official, account-authenticated Codex CLI.

The caller owns task authorization, issue leases, checkout isolation, verification
and publication. This adapter does not grant any of those authorities. It launches
one existing subscription client and persists a metadata-only status observation.
"""

from __future__ import annotations

import json
import math
import os
import re
import selectors
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .executors import AuthMode, ExecutionType, ExecutorProfile
from .usage_ledger import TokenCounts

MAX_OUTPUT_BYTES = 5 * 1024 * 1024
MAX_PROMPT_BYTES = 256 * 1024
MAX_LINE_BYTES = 256 * 1024
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,100}\Z")
_ENVIRONMENT = (
    "HOME",
    "PATH",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "TERM",
    "TMPDIR",
    "CODEX_HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "https_proxy",
    "http_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "CODEX_CA_CERTIFICATE",
)
_CONFIG = (
    'forced_login_method="chatgpt"',
    'model_provider="openai"',
    'model_providers.openai.base_url="https://api.openai.com/v1"',
    "model_providers.openai.requires_openai_auth=true",
    'chatgpt_base_url="https://chatgpt.com/backend-api"',
)


class CodexSubscriptionError(ValueError):
    """Invalid request for the operator subscription client."""


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def describe_run(
    executor: ExecutorProfile,
    *,
    worktree: Path,
    run_dir: Path,
    timeout_seconds: float = 900,
    sandbox: str = "read-only",
    codex_path: str = "codex",
) -> dict[str, Any]:
    """Validate and describe a run without spawning a process or changing files."""
    if (
        executor.provider != "codex"
        or executor.execution_type is not ExecutionType.SUBSCRIPTION_RUNNER
        or executor.auth_mode is not AuthMode.OAUTH
    ):
        raise CodexSubscriptionError(
            "requires a Codex subscription-runner with oauth authentication"
        )
    if not _MODEL.fullmatch(executor.model):
        raise CodexSubscriptionError("executor model must be a bounded model identifier")
    if sandbox not in {"read-only", "workspace-write"}:
        raise CodexSubscriptionError("sandbox must be read-only or workspace-write")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 7200
    ):
        raise CodexSubscriptionError("timeout_seconds must be positive and at most 7200")
    checkout = Path(worktree).resolve()
    if not checkout.is_dir() or not (checkout / ".git").exists():
        raise CodexSubscriptionError("worktree must be an existing Git checkout")
    if Path(run_dir).resolve().is_relative_to(checkout):
        raise CodexSubscriptionError("run_dir must be outside the agent checkout")
    if not isinstance(codex_path, str) or not codex_path or "\x00" in codex_path:
        raise CodexSubscriptionError("codex_path must name the operator-installed Codex executable")
    return {
        "schemaVersion": 1,
        "status": "planned",
        "executionSurface": "authenticated-remote-client",
        "billingMode": "subscription",
        "requestedModel": executor.model,
        "observedModel": None,
        "sandbox": sandbox,
        "timeoutSeconds": timeout_seconds,
        "processAlive": False,
        "heartbeatAt": None,
        "startedAt": None,
        "finishedAt": None,
        "exitCode": None,
        "failureKind": None,
        "resultAvailable": False,
        "providerInputTokens": None,
        "usage": TokenCounts().as_dict(),
    }


def _write_status(run_dir: Path, report: dict[str, Any]) -> None:
    temporary = run_dir / ".status.tmp"
    temporary.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(run_dir / "status.json")


def _terminate(process: subprocess.Popen) -> None:
    # A child can outlive its parent or retain its pipes, so always signal the
    # process group, even when poll() already reports that the parent exited.
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=1)
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=2)


def _drain(
    process: subprocess.Popen,
    *,
    deadline: float,
    receive: Callable[[str, bytes], None],
    heartbeat: Callable[[], None],
    limit: int,
) -> str | None:
    """Drain both pipes with bounded memory/runtime and update process liveness."""
    total = 0
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map() or process.poll() is None:
            if time.monotonic() >= deadline:
                return "timeout"
            heartbeat()
            for key, _ in selector.select(timeout=min(0.25, max(0, deadline - time.monotonic()))):
                data = os.read(key.fileobj.fileno(), 8192)
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                total += len(data)
                if total > limit:
                    return "output-limit"
                receive(key.data, data)
    return None


def run_subscription(
    executor: ExecutorProfile,
    *,
    worktree: Path,
    prompt: str,
    run_dir: Path,
    timeout_seconds: float = 900,
    sandbox: str = "read-only",
    codex_path: str = "codex",
) -> dict[str, Any]:
    """Run once and return a persisted terminal observation, never a code verdict.

    The run directory is an exclusive reservation. An interrupted or stale run
    requires operator reconciliation and a new run ID, rather than implicit retry.
    Raw CLI streams, prompts, auth output and provider error bodies are discarded.
    The final agent message is retained privately as result.txt and can contain
    task/source content. It is excluded from status.json and public telemetry.
    """
    report = describe_run(
        executor,
        worktree=worktree,
        run_dir=run_dir,
        timeout_seconds=timeout_seconds,
        sandbox=sandbox,
        codex_path=codex_path,
    )
    if not isinstance(prompt, str) or not prompt.strip():
        raise CodexSubscriptionError("prompt must be non-empty text")
    prompt_bytes = prompt.encode("utf-8")
    if len(prompt_bytes) > MAX_PROMPT_BYTES:
        raise CodexSubscriptionError("prompt exceeds the bounded input size")
    directory = Path(run_dir)
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError as error:
        raise CodexSubscriptionError(
            "run_dir already exists; reconcile the previous run"
        ) from error
    environment = {key: os.environ[key] for key in _ENVIRONMENT if key in os.environ}
    config = [argument for value in _CONFIG for argument in ("-c", value)]
    deadline = time.monotonic() + timeout_seconds
    report.update(status="checking-auth", startedAt=_timestamp())
    process = None

    def heartbeat() -> None:
        report["heartbeatAt"] = _timestamp()
        _write_status(directory, report)

    def spawn(command: list[str], stdin: Any = subprocess.DEVNULL) -> subprocess.Popen:
        return subprocess.Popen(
            command,
            cwd=Path(worktree).resolve(),
            env=environment,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

    try:
        _write_status(directory, report)
        process = spawn([codex_path, "login", "status", *config])
        report["processAlive"] = True
        auth_output = bytearray()
        failure = _drain(
            process,
            deadline=min(deadline, time.monotonic() + 15),
            receive=lambda _stream, data: auth_output.extend(data),
            heartbeat=heartbeat,
            limit=65536,
        )
        if failure:
            _terminate(process)
            report.update(status="blocked", failureKind=failure)
        elif (
            process.wait() != 0
            or "Logged in using ChatGPT"
            not in auth_output.decode("utf-8", errors="replace").splitlines()
        ):
            report.update(status="blocked", failureKind="subscription-auth-required")
        else:
            _run_turn(
                spawn,
                heartbeat,
                report,
                config,
                codex_path,
                executor.model,
                prompt_bytes,
                sandbox,
                deadline,
                directory,
            )
    except (OSError, subprocess.SubprocessError):
        report.update(status="failed", failureKind="client-unavailable")
    except BaseException:
        report.update(status="interrupted", failureKind="operator-interrupted")
        raise
    finally:
        if process is not None and process.poll() is None:
            _terminate(process)
        if process is not None:
            process.stdout.close()
            process.stderr.close()
        report.update(processAlive=False, heartbeatAt=_timestamp(), finishedAt=_timestamp())
        _write_status(directory, report)
    return report


def _run_turn(
    spawn: Callable[..., subprocess.Popen],
    heartbeat: Callable[[], None],
    report: dict[str, Any],
    config: list[str],
    codex_path: str,
    model: str,
    prompt: bytes,
    sandbox: str,
    deadline: float,
    run_dir: Path,
) -> None:
    buffer = bytearray()
    completed = False
    stream_failed = False
    result = None

    def receive(stream: str, data: bytes) -> None:
        nonlocal completed, stream_failed, result
        if stream != "stdout":
            return
        buffer.extend(data)
        while b"\n" in buffer:
            line, _, remainder = buffer.partition(b"\n")
            buffer[:] = remainder
            if len(line) > MAX_LINE_BYTES:
                raise CodexSubscriptionError("client output line exceeds bound")
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                stream_failed = True
                continue
            if not isinstance(event, dict):
                stream_failed = True
                continue
            item = event.get("item")
            if (
                event.get("type") == "item.completed"
                and isinstance(item, dict)
                and item.get("type") == "agent_message"
                and isinstance(item.get("text"), str)
            ):
                result = item["text"]
            if event.get("type") in {"turn.failed", "error"}:
                stream_failed = True
            if event.get("type") == "turn.completed":
                completed = True
                usage = event.get("usage")
                if isinstance(usage, dict):
                    observed = [
                        usage.get(key)
                        for key in ("input_tokens", "output_tokens", "cached_input_tokens")
                    ]
                    if all(
                        value is None or type(value) is int and value >= 0 for value in observed
                    ):
                        total_input, output, cached_input = observed
                        uncached_input = None
                        if total_input is not None and cached_input is not None:
                            if cached_input > total_input:
                                stream_failed = True
                                continue
                            uncached_input = total_input - cached_input
                        report["providerInputTokens"] = total_input
                        report["usage"] = TokenCounts(
                            input=uncached_input,
                            output=output,
                            cache_read=cached_input,
                        ).as_dict()
        if len(buffer) > MAX_LINE_BYTES:
            raise CodexSubscriptionError("client output line exceeds bound")

    # A temporary, unlinked stdin file avoids blocking the supervisor when the
    # client does not immediately read a pipe. The prompt never enters argv/logs.
    with tempfile.TemporaryFile() as input_file:
        input_file.write(prompt)
        input_file.seek(0)
        command = [
            codex_path,
            "exec",
            "--ignore-user-config",
            "--ephemeral",
            "--json",
            "--sandbox",
            sandbox,
            "--model",
            model,
            *config,
            "-",
        ]
        process = spawn(command, input_file)
        report.update(status="running", processAlive=True)

        def turn_heartbeat() -> None:
            report["processAlive"] = process.poll() is None
            heartbeat()

        try:
            failure = _drain(
                process,
                deadline=deadline,
                receive=receive,
                heartbeat=turn_heartbeat,
                limit=MAX_OUTPUT_BYTES,
            )
            if failure:
                _terminate(process)
            if buffer.strip():
                receive("stdout", b"\n")
            report["exitCode"] = process.wait()
            success = not failure and not stream_failed and completed and process.returncode == 0
            report.update(
                status="completed" if success else "failed",
                failureKind=None if success else failure or "incomplete-or-failed-turn",
            )
            if success and result is not None:
                with (run_dir / "result.txt").open("x", encoding="utf-8") as result_file:
                    os.fchmod(result_file.fileno(), 0o600)
                    result_file.write(result)
                report["resultAvailable"] = True
        except CodexSubscriptionError:
            report.update(status="failed", failureKind="output-limit")
        finally:
            _terminate(process)
            process.stdout.close()
            process.stderr.close()
