from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agentic_sdlc.gates import run_gates, write_report


def _write_config(path: Path, first: str, second: str) -> None:
    path.write_text(
        f"""\
[commands]
first = {json.dumps(first)}
second = {json.dumps(second)}

[verification]
gates = ["first", "second"]
allowed_exit_codes = {{ first = [0, 5], second = [0] }}
timeout_seconds = {{ first = 10, second = 10 }}
""",
        encoding="utf-8",
    )


def test_gates_accept_documented_nonzero_code_without_shell(tmp_path: Path) -> None:
    config = tmp_path / "policy.toml"
    marker = tmp_path / "marker"
    _write_config(
        config,
        f"{sys.executable} -c 'import sys; sys.exit(5)'",
        f"{sys.executable} -c 'from pathlib import Path; Path(\"{marker}\").touch()'",
    )
    report = run_gates(config, tmp_path)

    assert report["passed"] is True
    assert marker.exists()


def test_gates_stop_after_first_failure(tmp_path: Path) -> None:
    config = tmp_path / "policy.toml"
    marker = tmp_path / "marker"
    _write_config(
        config,
        f"{sys.executable} -c 'import sys; sys.exit(9)'",
        f"{sys.executable} -c 'from pathlib import Path; Path(\"{marker}\").touch()'",
    )
    report = run_gates(config, tmp_path)

    assert report["passed"] is False
    assert not marker.exists()
    assert len(report["gates"]) == 1

    output = tmp_path / "report.json"
    write_report(report, output)
    assert json.loads(output.read_text(encoding="utf-8"))["passed"] is False


def test_gates_scrub_credentials_and_ci_control_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "inspect_environment.py"
    output = tmp_path / "environment.json"
    script.write_text(
        """\
import json
import os
from pathlib import Path

Path("environment.json").write_text(json.dumps({
    "secret": "OPENAI_API_KEY" in os.environ,
    "control": "GITHUB_ENV" in os.environ,
    "allowed": os.environ.get("SAFE_TEST_VALUE"),
}))
""",
        encoding="utf-8",
    )
    config = tmp_path / "policy.toml"
    config.write_text(
        f"""\
[commands]
inspect = {json.dumps(f"{sys.executable} {script}")}

[verification]
gates = ["inspect"]
environment_allowlist = ["SAFE_TEST_VALUE"]
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("GITHUB_ENV", str(tmp_path / "github-env"))
    monkeypatch.setenv("SAFE_TEST_VALUE", "visible")

    report = run_gates(config, tmp_path)

    assert report["passed"] is True
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "secret": False,
        "control": False,
        "allowed": "visible",
    }
