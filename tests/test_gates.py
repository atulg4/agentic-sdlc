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


# Codex 4243659485: the gates must run under the interpreter CI installs ([ci] python_version).


def _python_config(path: Path, python_version: str | None) -> Path:
    ci = f'[ci]\npython_version = "{python_version}"\n\n' if python_version else ""
    path.write_text(
        ci + '[commands]\ntest = "true"\n\n[verification]\ngates = ["test"]\n',
        encoding="utf-8",
    )
    return path


def _fake_python3(directory: Path, version: str) -> None:
    directory.mkdir()
    script = directory / "python3"
    script.write_text(f"#!/bin/sh\necho {version}\n", encoding="utf-8")
    script.chmod(0o755)


def test_gates_run_when_python3_matches_ci_python_version(monkeypatch, tmp_path: Path) -> None:
    _fake_python3(tmp_path / "bin", "3.12.7")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")
    report = run_gates(_python_config(tmp_path / "policy.toml", "3.12"), tmp_path)
    assert report["passed"] is True


def test_gates_warn_but_run_when_python3_is_another_version(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    # A warning, not a failure: the platform's verify job installs no Python yet, so failing
    # closed would fail every verification whose policy pins another version.
    _fake_python3(tmp_path / "bin", "3.11.9")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")
    marker = tmp_path / "ran"
    config = tmp_path / "policy.toml"
    config.write_text(
        '[ci]\npython_version = "3.12.4"\n\n'
        f'[commands]\ntest = "touch {marker}"\n\n[verification]\ngates = ["test"]\n',
        encoding="utf-8",
    )
    run_gates(config, tmp_path)
    assert marker.exists()  # the gate ran
    err = capsys.readouterr().err
    assert "::warning::" in err
    assert "Python 3.11.9" in err and "python_version is 3.12.4" in err


def test_gates_warn_without_python3_when_a_version_is_set(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    (tmp_path / "empty").mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    run_gates(_python_config(tmp_path / "policy.toml", "3.12"), tmp_path)
    err = capsys.readouterr().err
    assert "::warning::" in err and "no python3" in err


def test_gates_only_warn_when_ci_python_version_is_unset(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    _fake_python3(tmp_path / "bin", "3.9.1")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")
    report = run_gates(_python_config(tmp_path / "policy.toml", None), tmp_path)
    assert report["passed"] is True
    assert "python_version is not set" in capsys.readouterr().err


def test_gates_reject_a_malformed_ci_python_version(tmp_path: Path) -> None:
    with pytest.raises(GateError, match="python_version"):
        run_gates(_python_config(tmp_path / "policy.toml", "latest"), tmp_path)


def test_the_real_interpreter_is_probed(tmp_path: Path) -> None:
    from agentic_sdlc.gates import gate_python_version

    found, version = gate_python_version({"PATH": f"{Path(sys.executable).parent}"}, tmp_path)
    assert version.split(".")[:2] == [str(sys.version_info[0]), str(sys.version_info[1])]
    assert Path(found).name == "python3"


# Codex 4246082577: the default setup/quality commands invoke `python`, not `python3`; both are
# probed, and a mismatch on either is a warning (never a failure).


def _fake_interpreters(directory: Path, **versions: str) -> dict[str, str]:
    directory.mkdir()
    for name, version in versions.items():
        script = directory / name
        script.write_text(f"#!/bin/sh\necho {version}\n", encoding="utf-8")
        script.chmod(0o755)
    return {"PATH": str(directory)}


def test_a_python_other_than_python3_is_warned_about(tmp_path: Path) -> None:
    from agentic_sdlc.gates import check_gate_python

    env = _fake_interpreters(tmp_path / "bin", python3="3.12.7", python="3.11.2")
    warning = check_gate_python("3.12", env, tmp_path)
    assert warning is not None
    assert f"{tmp_path / 'bin' / 'python'} (Python 3.11.2)" in warning
    assert "python3 (Python" not in warning  # the matching one is not blamed
    assert "python_version is 3.12" in warning


def test_python_and_python3_both_matching_is_quiet(tmp_path: Path) -> None:
    from agentic_sdlc.gates import check_gate_python

    env = _fake_interpreters(tmp_path / "bin", python3="3.12.7", python="3.12.1")
    assert check_gate_python("3.12", env, tmp_path) is None


def test_only_one_interpreter_name_on_path_is_enough(tmp_path: Path) -> None:
    from agentic_sdlc.gates import check_gate_python

    assert (
        check_gate_python("3.12", _fake_interpreters(tmp_path / "a", python3="3.12.7"), tmp_path)
        is None
    )
    assert (
        check_gate_python("3.12", _fake_interpreters(tmp_path / "b", python="3.12.7"), tmp_path)
        is None
    )
    warning = check_gate_python(
        "3.12", _fake_interpreters(tmp_path / "c", python="3.10.4"), tmp_path
    )
    assert warning is not None and "Python 3.10.4" in warning


def test_an_unreadable_python_is_warned_about(tmp_path: Path) -> None:
    from agentic_sdlc.gates import check_gate_python

    env = _fake_interpreters(tmp_path / "bin", python3="3.12.7", python="not-a-version")
    warning = check_gate_python("3.12", env, tmp_path)
    assert warning is not None and "did not report a Python version" in warning


def test_the_gates_still_run_when_python_mismatches(monkeypatch, tmp_path: Path, capsys) -> None:
    env = _fake_interpreters(tmp_path / "bin", python3="3.12.7", python="3.9.0")
    monkeypatch.setenv("PATH", env["PATH"])
    report = run_gates(_python_config(tmp_path / "policy.toml", "3.12"), tmp_path)
    assert report["passed"] is True  # a warning, never a failure
    err = capsys.readouterr().err
    assert "::warning::" in err and "Python 3.9.0" in err


def test_unset_python_version_lists_both_interpreters(tmp_path: Path) -> None:
    from agentic_sdlc.gates import check_gate_python

    env = _fake_interpreters(tmp_path / "bin", python3="3.12.7", python="3.11.2")
    warning = check_gate_python(None, env, tmp_path)
    assert warning is not None and "python_version is not set" in warning
    assert "Python 3.12.7" in warning and "Python 3.11.2" in warning
