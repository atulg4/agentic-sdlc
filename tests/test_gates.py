from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agentic_sdlc.gates import GateError, run_gates, write_report


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


def test_gates_accept_documented_nonzero_code(tmp_path: Path) -> None:
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


# Codex 4243193109: a gate runs with the generated ci.yml's shell semantics (`bash -e -c`), not
# tokenized and executed as argv.


def _single(tmp_path: Path, command: str) -> dict:
    config = tmp_path / "policy.toml"
    config.write_text(
        f'[commands]\nonly = {json.dumps(command)}\n\n[verification]\ngates = ["only"]\n',
        encoding="utf-8",
    )
    return run_gates(config, tmp_path)


def test_a_compound_gate_fails_when_any_part_fails(tmp_path: Path) -> None:
    report = _single(tmp_path, "true && false")
    assert report["passed"] is False
    assert report["gates"][0]["exitCode"] == 1
    assert _single(tmp_path, "false; true")["passed"] is False  # `-e`, as in CI's `bash -e`
    assert _single(tmp_path, "true && true")["passed"] is True


def test_a_gate_can_change_directory_first(tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    report = _single(tmp_path, f"cd sub && {sys.executable} -c \"open('here', 'w').close()\"")
    assert report["passed"] is True
    assert (sub / "here").exists() and not (tmp_path / "here").exists()


def test_a_gate_can_set_a_variable_for_its_command(tmp_path: Path) -> None:
    script = "import os, pathlib; pathlib.Path('seen').write_text(os.environ['GATE_VAR'])"
    report = _single(tmp_path, f"GATE_VAR=x {sys.executable} -c {json.dumps(script)}")
    assert report["passed"] is True
    assert (tmp_path / "seen").read_text() == "x"


def test_a_gate_runs_as_one_shell_line(tmp_path: Path) -> None:
    report = _single(tmp_path, "true")
    command = report["gates"][0]["command"]
    assert command[1:] == ["-e", "-c", "true"]
    assert Path(command[0]).name in {"bash", "sh"}
    with pytest.raises(GateError, match="single line"):
        _single(tmp_path, "true\nfalse")
