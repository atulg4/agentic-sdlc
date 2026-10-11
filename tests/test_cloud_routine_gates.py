"""The deterministic gates the cloud routine runs (Codex review 5483165195): `check-diff` before
pushing (4241434509) and `check-request` before implementing (4241434511)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agentic_sdlc import cli
from agentic_sdlc.onboard import OnboardError

PROJECT = "example/project"  # the `policy_file` fixture's project id
LABELS = ["agent-ready", "human-review-required", "implementation-approved"]


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=repository, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "test@example.com")
    _git(repository, "config", "user.name", "Test")
    (repository / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repository, "add", "--", ".")
    _git(repository, "commit", "-qm", "base")
    _git(repository, "switch", "-q", "-c", "forge/issue-7")
    return repository


def _commit(repository: Path, files: dict[str, str], message: str = "change") -> None:
    for name, text in files.items():
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repository, "add", "--", ".")
    _git(repository, "commit", "-qm", message)


def _check_diff(repository: Path, policy_file: Path, *extra: str) -> int:
    return cli.main(
        [
            "check-diff",
            "--config",
            str(policy_file),
            "--repository",
            str(repository),
            "--base",
            "main",
            *extra,
        ]
    )


# ---------------------------------------------------------------- check-diff (Codex 4241434509)


def test_check_diff_passes_a_committed_change_within_policy(tmp_path, policy_file, capsys):
    repository = _repo(tmp_path)
    _commit(repository, {"module.py": "VALUE = 2\n"})
    out = tmp_path / "decision.json"
    assert _check_diff(repository, policy_file, "--output", str(out)) == 0
    document = json.loads(out.read_text())
    assert document["allowed"] is True and document["diff"]["paths"] == ["module.py"]
    assert "diff policy passed" in capsys.readouterr().err


def test_check_diff_refuses_a_forbidden_path(tmp_path, policy_file, capsys):
    repository = _repo(tmp_path)
    _commit(repository, {".github/workflows/x.yml": "on: push\n"})
    assert _check_diff(repository, policy_file) == 2
    err = capsys.readouterr().err
    assert "REFUSED" in err and "forbidden paths changed: .github/workflows/x.yml" in err


def test_check_diff_refuses_a_change_over_the_caps(tmp_path, policy_file, capsys):
    repository = _repo(tmp_path)
    _commit(repository, {f"f{i}.py": "x = 1\n" for i in range(4)})  # max_changed_files = 3
    assert _check_diff(repository, policy_file) == 2
    assert "changed-file cap exceeded: 4 > 3" in capsys.readouterr().err
    (tmp_path / "lines").mkdir()
    repository = _repo(tmp_path / "lines")
    _commit(repository, {"big.py": "x = 1\n" * 101})  # max_diff_lines = 100
    assert _check_diff(repository, policy_file) == 2
    assert "diff-line cap exceeded" in capsys.readouterr().err


def test_check_diff_refuses_an_empty_change(tmp_path, policy_file, capsys):
    repository = _repo(tmp_path)
    (repository / "module.py").write_text("VALUE = 3\n")  # uncommitted: not pushed
    assert _check_diff(repository, policy_file) == 2
    assert "no files changed" in capsys.readouterr().err


def test_check_diff_judges_the_commit_not_the_working_tree(tmp_path, policy_file):
    """An uncommitted edit can neither hide a committed forbidden change nor add one that the
    push does not carry."""
    repository = _repo(tmp_path)
    _commit(repository, {"module.py": "VALUE = 2\n", ".github/workflows/x.yml": "on: push\n"})
    (repository / ".github/workflows/x.yml").unlink()  # gone from the tree, still committed
    assert _check_diff(repository, policy_file) == 2
    # the contrast: inspect-diff reads the working tree, so the committed workflow is invisible
    inspect = [
        "inspect-diff", "--config", str(policy_file), "--repository", str(repository),
        "--base", "main", "--patch-output", str(tmp_path / "p"),
        "--paths-output", str(tmp_path / "n"), "--decision-output", str(tmp_path / "d"),
    ]  # fmt: skip
    assert cli.main(inspect) == 0
    _git(repository, "commit", "-qam", "drop the workflow")
    assert _check_diff(repository, policy_file) == 0
    (repository / ".env").write_text("SECRET=1\n")  # forbidden, but never committed
    assert _check_diff(repository, policy_file) == 0


def test_check_diff_compares_with_the_merge_base(tmp_path, policy_file):
    """Changes the default branch gained after the fork are not the change being pushed."""
    repository = _repo(tmp_path)
    _commit(repository, {"module.py": "VALUE = 2\n"})
    _git(repository, "switch", "-q", "main")
    _commit(repository, {".github/workflows/ci.yml": "on: push\n", "other.py": "y = 1\n"})
    _git(repository, "switch", "-q", "forge/issue-7")
    out = tmp_path / "decision.json"
    assert _check_diff(repository, policy_file, "--output", str(out)) == 0
    assert json.loads(out.read_text())["diff"]["paths"] == ["module.py"]


@pytest.mark.parametrize("base", ["--output=/tmp/x", "-p", "no-such-ref"])
def test_check_diff_refuses_an_unusable_base(tmp_path, policy_file, base, capsys):
    repository = _repo(tmp_path)
    _commit(repository, {"module.py": "VALUE = 2\n"})
    argv = ["check-diff", "--config", str(policy_file), "--repository", str(repository)]
    assert cli.main([*argv, f"--base={base}"]) == 2
    assert "ERROR" in capsys.readouterr().err


def test_check_diff_runs_no_external_diff_or_textconv(tmp_path, policy_file):
    repository = _repo(tmp_path)
    marker = tmp_path / "ran"
    program = tmp_path / "evil"
    program.write_text(f'#!/bin/sh\ntouch {marker}\ncat "$1" 2>/dev/null\n')
    program.chmod(0o755)
    _git(repository, "config", "diff.external", str(program))
    _git(repository, "config", "diff.evil.textconv", str(program))
    (repository / ".gitattributes").write_text("*.py diff=evil\n")
    _commit(repository, {"module.py": "VALUE = 2\n"})
    assert _check_diff(repository, policy_file) == 0
    assert not marker.exists()


# ------------------------------------------------------------- check-request (Codex 4241434511)

VALID = """## Summary

Do the thing.

## Acceptance Criteria

- It works.

## Required Tests

- A test.

## Non-Goals

- Nothing else.

## Dependencies

{deps}
"""


class FakeGh:
    def __init__(self, body: str, labels=LABELS, states: dict[int, object] | None = None):
        self.issue = {
            "number": 7,
            "title": "Do the thing",
            "body": body,
            "labels": [{"name": n} for n in labels],
            "state": "open",
        }
        self.states = states or {}
        self.calls: list[list[str]] = []

    def __call__(self, args, input=None):
        args = list(args)
        self.calls.append(args)
        number = int(args[1].rsplit("/", 1)[1])
        if number == 7:
            return json.dumps(self.issue)
        state = self.states.get(number)
        if isinstance(state, Exception):
            raise state
        if state is None:
            raise OnboardError("gh api failed: Not Found (HTTP 404)")
        assert args[2:] == ["--jq", ".state"]
        return f"{state}\n"


def _check_request(monkeypatch, policy_file, gh, tmp_path=None, project=PROJECT):
    monkeypatch.setattr(cli, "run_gh", gh)
    argv = ["check-request", "--project", project, "--issue", "7", "--config", str(policy_file)]
    if tmp_path is not None:
        argv += ["--output", str(tmp_path / "request.json")]
    return cli.main(argv)


def test_check_request_passes_a_valid_request_without_dependencies(
    monkeypatch, policy_file, tmp_path
):
    gh = FakeGh(VALID.format(deps="None"))
    assert _check_request(monkeypatch, policy_file, gh, tmp_path) == 0
    result = json.loads((tmp_path / "request.json").read_text())
    assert result["ready"] is True and result["dependencies"] == []


def test_check_request_passes_when_every_dependency_is_closed(monkeypatch, policy_file):
    gh = FakeGh(VALID.format(deps="- #3\n- #5"), states={3: "closed", 5: "closed"})
    assert _check_request(monkeypatch, policy_file, gh) == 0
    assert [c[1] for c in gh.calls[1:]] == [
        f"repos/{PROJECT}/issues/3",
        f"repos/{PROJECT}/issues/5",
    ]


def test_check_request_waits_for_an_open_dependency(monkeypatch, policy_file, tmp_path, capsys):
    gh = FakeGh(VALID.format(deps="- #3\n- #5"), states={3: "closed", 5: "open"})
    assert _check_request(monkeypatch, policy_file, gh, tmp_path) == cli.DEPENDENCY_OPEN == 4
    result = json.loads((tmp_path / "request.json").read_text())
    assert result["ready"] is False and result["openDependencies"] == [5]
    assert "WAITING: #7: dependency issue(s) not closed: #5" in capsys.readouterr().err


@pytest.mark.parametrize("state", [OnboardError("gh api failed: HTTP 502"), "", "merged", "null"])
def test_check_request_never_reads_an_unreadable_dependency_as_closed(
    monkeypatch, policy_file, state
):
    gh = FakeGh(VALID.format(deps="- #3"), states={3: state})
    assert _check_request(monkeypatch, policy_file, gh) == 2


def test_check_request_never_reads_a_missing_dependency_as_closed(monkeypatch, policy_file):
    gh = FakeGh(VALID.format(deps="- #3"))  # #3 answers 404
    assert _check_request(monkeypatch, policy_file, gh) == 2


@pytest.mark.parametrize(
    ("body", "why"),
    [
        ("## Summary\n\nDo it.\n", "missing or empty sections"),
        (VALID.format(deps="maybe later"), "dependencies must be issue references"),
    ],
)
def test_check_request_refuses_an_invalid_request(monkeypatch, policy_file, body, why, capsys):
    gh = FakeGh(body)
    assert _check_request(monkeypatch, policy_file, gh) == cli.REQUEST_INVALID == 3
    assert why in capsys.readouterr().err
    assert len(gh.calls) == 1  # no dependency is read for an invalid request


def test_check_request_applies_the_implement_policy(monkeypatch, policy_file, capsys):
    gh = FakeGh(VALID.format(deps="None"), labels=LABELS[:2])  # not approved for implementation
    assert _check_request(monkeypatch, policy_file, gh) == 3
    assert "missing required labels: implementation-approved" in capsys.readouterr().err
    gh = FakeGh("Place a live trade now.\n\n" + VALID.format(deps="None"))
    assert _check_request(monkeypatch, policy_file, gh) == 3


def test_check_request_errors_when_nothing_can_be_read(monkeypatch, policy_file, tmp_path):
    def failing(args, input=None):
        raise OnboardError("gh api failed: HTTP 500")

    assert _check_request(monkeypatch, policy_file, failing) == 2
    assert _check_request(monkeypatch, policy_file, lambda args, input=None: "null") == 2
    gh = FakeGh(VALID.format(deps="None"))
    gh.issue["pull_request"] = {"url": "x"}
    assert _check_request(monkeypatch, policy_file, gh) == 2
    # a policy for another repository is never applied to this one
    gh = FakeGh(VALID.format(deps="None"))
    assert _check_request(monkeypatch, policy_file, gh, project="other/repo") == 2
    missing = tmp_path / "absent.toml"
    assert _check_request(monkeypatch, missing, FakeGh(VALID.format(deps="None"))) == 2
