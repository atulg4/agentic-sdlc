from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from agentic_sdlc.codex_subscription import (
    CodexSubscriptionError,
    describe_run,
    run_subscription,
)
from agentic_sdlc.executors import AuthMode, ExecutionType, ExecutorProfile, TaskClass


@pytest.fixture
def executor():
    return ExecutorProfile(
        executor_id="codex-personal",
        provider="codex",
        adapter="codex-cli",
        adapter_version="1",
        execution_type=ExecutionType.SUBSCRIPTION_RUNNER,
        auth_mode=AuthMode.OAUTH,
        model="gpt-6-astra",
        model_alias="codex",
        model_family="gpt-6",
        task_classes=(TaskClass.IMPLEMENTATION,),
        capabilities=("edit-code",),
    )


@pytest.fixture
def worktree(tmp_path):
    directory = tmp_path / "checkout"
    directory.mkdir()
    (directory / ".git").mkdir()
    return directory


def stub(tmp_path: Path, body: str, auth: str = "Logged in using ChatGPT") -> Path:
    program = tmp_path / "codex-stub"
    program.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys, time\n"
        "if 'login' in sys.argv:\n"
        f"    print({auth!r})\n"
        "    sys.exit(0)\n" + body
    )
    program.chmod(0o700)
    return program


def test_dry_run_does_not_spawn_or_create_state(executor, worktree, tmp_path):
    target = tmp_path / "run"
    report = describe_run(executor, worktree=worktree, run_dir=target, codex_path="missing")
    assert report["status"] == "planned"
    assert report["executionSurface"] == "authenticated-remote-client"
    assert report["observedModel"] is None
    assert not target.exists()


def test_completed_turn_requires_exit_zero_and_records_only_observed_metadata(
    executor, worktree, tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-private-input")
    monkeypatch.setenv("CODEX_API_KEY", "sk-private-codex")
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "private-access")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://unapproved.invalid")
    program = stub(
        tmp_path,
        "assert '--ignore-user-config' in sys.argv\n"
        "assert '--ephemeral' in sys.argv\n"
        "assert 'forced_login_method=\"chatgpt\"' in sys.argv\n"
        "assert 'model_provider=\"openai\"' in sys.argv\n"
        "assert 'workspace-write' not in sys.argv\n"
        "assert not any(k in os.environ for k in "
        "['OPENAI_API_KEY', 'CODEX_API_KEY', 'CODEX_ACCESS_TOKEN', 'OPENAI_BASE_URL'])\n"
        "assert sys.stdin.read() == 'private prompt'\n"
        "print(json.dumps({'type': 'item.completed', 'item': "
        "{'type': 'agent_message', 'text': 'sk-private-output'}}))\n"
        "print(json.dumps({'type': 'turn.completed', 'usage': "
        "{'input_tokens': 10, 'output_tokens': 2, 'cached_input_tokens': 3}}))\n",
    )
    report = run_subscription(
        executor,
        worktree=worktree,
        prompt="private prompt",
        run_dir=tmp_path / "run",
        codex_path=str(program),
    )
    assert report["status"] == "completed"
    assert report["requestedModel"] == "gpt-6-astra"
    assert report["observedModel"] is None
    assert report["usage"] == {
        "input": 7,
        "output": 2,
        "cacheRead": 3,
        "cacheWrite": None,
        "total": None,
        "status": "partial",
    }
    assert report["processAlive"] is False
    assert report["providerInputTokens"] == 10
    assert report["resultAvailable"] is True
    result = tmp_path / "run" / "result.txt"
    assert result.read_text() == "sk-private-output"
    assert result.stat().st_mode & 0o777 == 0o600
    saved = (tmp_path / "run" / "status.json").read_text()
    assert "private" not in saved
    assert json.loads(saved) == report


@pytest.mark.parametrize(
    "body",
    [
        "print(json.dumps({'type': 'turn.started'}))\n",
        "print(json.dumps({'type': 'turn.completed'})); sys.exit(7)\n",
        "print(json.dumps({'type': 'turn.failed', 'error': {'message': 'secret'}}))\n",
        "print(json.dumps({'type': 'error', 'message': 'secret'}))\n"
        "print(json.dumps({'type': 'turn.completed'}))\n",
        "print('invalid private output')\n",
    ],
)
def test_failed_or_incomplete_stream_never_reports_success(executor, worktree, tmp_path, body):
    report = run_subscription(
        executor,
        worktree=worktree,
        prompt="task",
        run_dir=tmp_path / "run",
        codex_path=str(stub(tmp_path, body)),
    )
    assert report["status"] == "failed"
    assert report["processAlive"] is False
    assert "secret" not in json.dumps(report)
    assert "private" not in json.dumps(report)


def test_api_auth_refused_before_exec(executor, worktree, tmp_path):
    report = run_subscription(
        executor,
        worktree=worktree,
        prompt="task",
        run_dir=tmp_path / "run",
        codex_path=str(stub(tmp_path, "raise AssertionError('must not run')\n", "Using API key")),
    )
    assert report["status"] == "blocked"
    assert report["failureKind"] == "subscription-auth-required"


def test_timeout_terminates_and_preserves_terminal_status(executor, worktree, tmp_path):
    report = run_subscription(
        executor,
        worktree=worktree,
        prompt="task",
        run_dir=tmp_path / "run",
        timeout_seconds=0.1,
        codex_path=str(stub(tmp_path, "time.sleep(20)\n")),
    )
    assert report["status"] == "failed"
    assert report["failureKind"] == "timeout"
    assert not report["processAlive"]


def test_existing_run_never_restarts(executor, worktree, tmp_path):
    target = tmp_path / "run"
    target.mkdir()
    (target / "status.json").write_text('{"status":"running"}')
    with pytest.raises(CodexSubscriptionError, match="already exists"):
        run_subscription(
            executor, worktree=worktree, prompt="task", run_dir=target, codex_path="missing"
        )
    assert (target / "status.json").read_text() == '{"status":"running"}'


@pytest.mark.parametrize(
    "changes",
    [
        {"auth_mode": AuthMode.API_KEY},
        {"provider": "openai"},
        {"execution_type": ExecutionType.SUBSCRIPTION_CLOUD},
    ],
)
def test_unsupported_executor_rejected(executor, worktree, tmp_path, changes):
    with pytest.raises(CodexSubscriptionError, match="subscription-runner"):
        describe_run(replace(executor, **changes), worktree=worktree, run_dir=tmp_path / "run")


def test_status_directory_outside_agent_checkout(executor, worktree):
    with pytest.raises(CodexSubscriptionError, match="outside"):
        describe_run(executor, worktree=worktree, run_dir=worktree / "run")


def test_output_limit_stops_process(executor, worktree, tmp_path, monkeypatch):
    monkeypatch.setattr("agentic_sdlc.codex_subscription.MAX_OUTPUT_BYTES", 4096)
    report = run_subscription(
        executor,
        worktree=worktree,
        prompt="task",
        run_dir=tmp_path / "run",
        codex_path=str(stub(tmp_path, "print('x' * 10000)\ntime.sleep(20)\n")),
    )
    assert report["status"] == "failed"
    assert report["failureKind"] == "output-limit"


def test_long_complete_line_is_bounded(executor, worktree, tmp_path, monkeypatch):
    monkeypatch.setattr("agentic_sdlc.codex_subscription.MAX_LINE_BYTES", 500)
    report = run_subscription(
        executor,
        worktree=worktree,
        prompt="task",
        run_dir=tmp_path / "run",
        codex_path=str(
            stub(
                tmp_path,
                "print(json.dumps({'type': 'item.completed', "
                "'item': {'type': 'agent_message', 'text': 'x' * 1000}}))\n"
                "print(json.dumps({'type': 'turn.completed'}))\n",
            )
        ),
    )
    assert report["status"] == "failed"
    assert report["failureKind"] == "output-limit"
    assert not (tmp_path / "run" / "result.txt").exists()


def test_missing_cache_split_remains_unknown(executor, worktree, tmp_path):
    report = run_subscription(
        executor,
        worktree=worktree,
        prompt="task",
        run_dir=tmp_path / "run",
        codex_path=str(
            stub(
                tmp_path,
                "print(json.dumps({'type': 'turn.completed', "
                "'usage': {'input_tokens': 10, 'output_tokens': 2}}))\n",
            )
        ),
    )
    assert report["status"] == "completed"
    assert report["providerInputTokens"] == 10
    assert report["usage"]["input"] is None
    assert report["usage"]["cacheRead"] is None


def test_running_process_has_live_heartbeat(executor, worktree, tmp_path):
    program = stub(
        tmp_path,
        "time.sleep(0.5)\nprint(json.dumps({'type': 'turn.completed'}))\n",
    )
    target = tmp_path / "run"
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            run_subscription,
            executor,
            worktree=worktree,
            prompt="task",
            run_dir=target,
            codex_path=str(program),
        )
        deadline = time.monotonic() + 3
        running = None
        while time.monotonic() < deadline:
            try:
                observation = json.loads((target / "status.json").read_text())
            except FileNotFoundError:
                observation = {}
            if observation.get("status") == "running":
                running = observation
                break
            time.sleep(0.01)
        assert running is not None
        assert running["processAlive"] is True
        assert running["heartbeatAt"] is not None
        assert future.result(timeout=3)["status"] == "completed"


@pytest.mark.skipif(sys.platform != "linux", reason="observes Linux process lifecycle via /proc")
def test_timeout_stops_child_after_client_exits(executor, worktree, tmp_path):
    child_pid_file = tmp_path / "child.pid"
    program = stub(
        tmp_path,
        "import signal\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    time.sleep(20)\n"
        "else:\n"
        f"    open({str(child_pid_file)!r}, 'w').write(str(child))\n"
        "    sys.exit(0)\n",
    )
    report = run_subscription(
        executor,
        worktree=worktree,
        prompt="task",
        run_dir=tmp_path / "run",
        timeout_seconds=0.3,
        codex_path=str(program),
    )
    assert report["failureKind"] == "timeout"
    child_stat = Path("/proc") / child_pid_file.read_text() / "stat"
    deadline = time.monotonic() + 2
    while child_stat.exists() and child_stat.read_text().split()[2] != "Z":
        assert time.monotonic() < deadline
        time.sleep(0.01)
