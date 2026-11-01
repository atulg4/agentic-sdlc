"""The deterministic gates the cloud routine runs (Codex review 5483165195): `check-diff` before
pushing (4241434509) and `check-request` before implementing (4241434511)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from agentic_sdlc import cli
from agentic_sdlc.onboard import GhCommandError, OnboardError

PROJECT = "example/project"  # the `policy_file` fixture's project id
LABELS = ["agent-ready", "human-review-required", "implementation-approved"]


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=repository, check=True, capture_output=True, text=True
    ).stdout.strip()


#: The repository whose `main` plays GitHub's default-branch head for PROJECT (Codex
#: 4241839926: check-diff and `check-request --base` verify the base against GitHub).
_REMOTE: dict[str, Path | str] = {}


def _remote_head() -> str:
    from agentic_sdlc.git_diff import resolve_commit

    if "head" in _REMOTE:  # GitHub's head pinned by the test
        return str(_REMOTE["head"])
    return resolve_commit(_REMOTE["repository"], "main")


def github_answer(args):
    """GitHub's answer to the base-verification calls, or None for any other call."""
    if args[:2] == ["api", f"repos/{PROJECT}"]:
        return json.dumps({"full_name": PROJECT, "default_branch": "main"})
    if args[:2] == ["api", f"repos/{PROJECT}/git/ref/heads/main"]:
        head = _remote_head()
        return json.dumps({"ref": "refs/heads/main", "object": {"type": "commit", "sha": head}})
    return None


def _github_only(args, input=None):
    answer = github_answer(list(args))
    if answer is None:
        raise OnboardError(f"unexpected gh call {list(args)}")
    return answer


@pytest.fixture(autouse=True)
def _fake_github(monkeypatch):
    """Every test here talks to a fake GitHub (`github_answer`); a test that needs more
    replaces `cli.run_gh` with a fake that falls back to it."""
    _REMOTE.clear()
    monkeypatch.setattr(cli, "run_gh", _github_only)


def _repo(tmp_path: Path, policy_file: Path) -> Path:
    """A repository whose `main` commits `policy_file` as its agentic-sdlc.toml (check-diff reads
    the policy committed on --base: Codex 4241614590), with the issue branch checked out."""
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "test@example.com")
    _git(repository, "config", "user.name", "Test")
    (repository / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repository / "agentic-sdlc.toml").write_bytes(policy_file.read_bytes())
    _git(repository, "add", "--", ".")
    _git(repository, "commit", "-qm", "base")
    _git(repository, "switch", "-q", "-c", "forge/issue-7")
    _REMOTE["repository"] = repository
    return repository


def _commit(repository: Path, files: dict[str, str], message: str = "change") -> None:
    for name, text in files.items():
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repository, "add", "--", ".")
    _git(repository, "commit", "-qm", message)


def _check_diff(repository: Path, policy_file: Path, *extra: str) -> int:
    """`policy_file` is committed on main by `_repo`; check-diff reads it from there, after
    verifying main against (fake) GitHub, whose head is `repository`'s main."""
    _REMOTE["repository"] = repository
    return cli.main(
        [
            "check-diff",
            "--project",
            PROJECT,
            "--config",
            "agentic-sdlc.toml",
            "--repository",
            str(repository),
            "--base",
            "main",
            *extra,
        ]
    )


# ---------------------------------------------------------------- check-diff (Codex 4241434509)


def test_check_diff_passes_a_committed_change_within_policy(tmp_path, policy_file, capsys):
    repository = _repo(tmp_path, policy_file)
    _commit(repository, {"module.py": "VALUE = 2\n"})
    out = tmp_path / "decision.json"
    assert _check_diff(repository, policy_file, "--output", str(out)) == 0
    document = json.loads(out.read_text())
    assert document["allowed"] is True and document["diff"]["paths"] == ["module.py"]
    assert "diff policy passed" in capsys.readouterr().err


def test_check_diff_refuses_a_forbidden_path(tmp_path, policy_file, capsys):
    repository = _repo(tmp_path, policy_file)
    _commit(repository, {".github/workflows/x.yml": "on: push\n"})
    assert _check_diff(repository, policy_file) == 2
    err = capsys.readouterr().err
    assert "REFUSED" in err and "forbidden paths changed: .github/workflows/x.yml" in err


def test_check_diff_refuses_a_change_over_the_caps(tmp_path, policy_file, capsys):
    repository = _repo(tmp_path, policy_file)
    _commit(repository, {f"f{i}.py": "x = 1\n" for i in range(4)})  # max_changed_files = 3
    assert _check_diff(repository, policy_file) == 2
    assert "changed-file cap exceeded: 4 > 3" in capsys.readouterr().err
    (tmp_path / "lines").mkdir()
    repository = _repo(tmp_path / "lines", policy_file)
    _commit(repository, {"big.py": "x = 1\n" * 101})  # max_diff_lines = 100
    assert _check_diff(repository, policy_file) == 2
    assert "diff-line cap exceeded" in capsys.readouterr().err


def test_check_diff_refuses_an_empty_change(tmp_path, policy_file, capsys):
    repository = _repo(tmp_path, policy_file)
    (repository / "module.py").write_text("VALUE = 3\n")  # uncommitted: not pushed
    assert _check_diff(repository, policy_file) == 2
    assert "no files changed" in capsys.readouterr().err


def test_check_diff_judges_the_commit_not_the_working_tree(tmp_path, policy_file):
    """An uncommitted edit can neither hide a committed forbidden change nor add one that the
    push does not carry."""
    repository = _repo(tmp_path, policy_file)
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
    # the end state no longer has it, but the pushed history does (Codex 4241839922)
    assert _check_diff(repository, policy_file) == 2
    _git(repository, "switch", "-q", "-c", "clean", "main")
    _commit(repository, {"module.py": "VALUE = 2\n"})
    assert _check_diff(repository, policy_file) == 0
    (repository / ".env").write_text("SECRET=1\n")  # forbidden, but never committed
    assert _check_diff(repository, policy_file) == 0


def test_check_diff_compares_with_the_merge_base(tmp_path, policy_file):
    """Changes the default branch gained after the fork are not the change being pushed."""
    repository = _repo(tmp_path, policy_file)
    _commit(repository, {"module.py": "VALUE = 2\n"})
    _git(repository, "switch", "-q", "main")
    _commit(repository, {".github/workflows/ci.yml": "on: push\n", "other.py": "y = 1\n"})
    _git(repository, "switch", "-q", "forge/issue-7")
    out = tmp_path / "decision.json"
    assert _check_diff(repository, policy_file, "--output", str(out)) == 0
    assert json.loads(out.read_text())["diff"]["paths"] == ["module.py"]


@pytest.mark.parametrize("base", ["--output=/tmp/x", "-p", "no-such-ref"])
def test_check_diff_refuses_an_unusable_base(tmp_path, policy_file, base, capsys):
    repository = _repo(tmp_path, policy_file)
    _commit(repository, {"module.py": "VALUE = 2\n"})
    argv = ["check-diff", "--project", PROJECT, "--repository", str(repository)]
    assert cli.main([*argv, f"--base={base}"]) == 2
    assert "ERROR" in capsys.readouterr().err


def test_check_diff_runs_no_external_diff_or_textconv(tmp_path, policy_file):
    repository = _repo(tmp_path, policy_file)
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


def test_check_diff_ignores_replace_refs(tmp_path, policy_file, capsys):
    """A `refs/replace/*` object must not stand in for the commit a push publishes (Codex
    4241532610): with HEAD replaced by a benign commit, check-diff still judges the original."""
    repository = _repo(tmp_path, policy_file)
    _commit(repository, {".github/workflows/x.yml": "on: push\n"})
    forbidden = _git(repository, "rev-parse", "HEAD")
    _git(repository, "switch", "-q", "-c", "decoy", "main")
    _commit(repository, {"module.py": "VALUE = 2\n"})
    benign = _git(repository, "rev-parse", "HEAD")
    _git(repository, "switch", "-q", "forge/issue-7")
    _git(repository, "replace", forbidden, benign)
    # git itself now shows the benign tree for HEAD -- the trap
    assert _git(repository, "diff", "--name-only", "main", "HEAD") == "module.py"
    assert _check_diff(repository, policy_file) == 2
    assert "forbidden paths changed: .github/workflows/x.yml" in capsys.readouterr().err


def test_check_diff_ignores_grafts(tmp_path, policy_file, capsys):
    """A graft that re-parents HEAD onto itself hides its change the same way."""
    repository = _repo(tmp_path, policy_file)
    _commit(repository, {".github/workflows/x.yml": "on: push\n"})
    head = _git(repository, "rev-parse", "HEAD")
    grafts = repository / ".git" / "info" / "grafts"
    grafts.parent.mkdir(parents=True, exist_ok=True)
    grafts.write_text(f"{head}\n", encoding="utf-8")  # HEAD becomes a root commit
    assert _check_diff(repository, policy_file) == 2
    assert "forbidden paths changed: .github/workflows/x.yml" in capsys.readouterr().err


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
        #: dependency number -> its update time; `on_dep_read[n]` runs right after the n-th read
        #: of any dependency is answered
        self.dep_updated: dict[int, str] = {}
        self.dep_reads = 0
        self.on_dep_read: dict[int, object] = {}
        self.calls: list[list[str]] = []

    def __call__(self, args, input=None):
        args = list(args)
        answer = github_answer(args)
        if answer is not None:
            return answer
        self.calls.append(args)
        number = int(args[1].rsplit("/", 1)[1])
        if number == 7:
            return json.dumps(self.issue)
        state = self.states.get(number)
        if isinstance(state, Exception):
            raise state
        if state is None:
            raise GhCommandError("gh api failed: Not Found (HTTP 404)", http_status=404)
        assert args[2:] == []
        answer = json.dumps(
            {
                "number": number,
                "state": state,
                "updated_at": self.dep_updated.get(number, "2026-01-01T00:00:00Z"),
            }
        )
        self.dep_reads += 1
        change = self.on_dep_read.get(self.dep_reads)
        if change is not None:
            change(self)
        return answer


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


def test_check_request_attests_its_result_under_a_key(monkeypatch, policy_file, tmp_path):
    # With --attestation-key the written result carries the HMAC mark that
    # dispatch-packet --authorization verifies: a hand-authored result has no
    # way to mint it without the secret.
    from agentic_sdlc.cli import _attest_document

    gh = FakeGh(VALID.format(deps="None"))
    monkeypatch.setattr(cli, "run_gh", gh)
    secret = b"an-attestation-secret-of-at-least-32-bytes"
    key_file = tmp_path / "attestation.key"
    key_file.write_bytes(secret)
    assert (
        cli.main(
            [
                "check-request",
                "--project",
                PROJECT,
                "--issue",
                "7",
                "--config",
                str(policy_file),
                "--output",
                str(tmp_path / "request.json"),
                "--attestation-key",
                str(key_file),
            ]
        )
        == 0
    )
    result = json.loads((tmp_path / "request.json").read_text())
    assert result["attestation"] == _attest_document(result, secret)


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


# ------------------------------------------- check-diff runs a trusted git (Codex 4241614592)

FAKE_GIT = """#!/bin/sh
# A planted git: fabricates a benign diff for any `diff`, delegates everything else.
touch "{marker}"
for a in "$@"; do
  case "$a" in
    diff)
      case "$*" in
        *--name-status*) printf 'M\\000module.py\\000' ;;
        *--numstat*) printf '1\\t1\\tmodule.py\\000' ;;
        *) printf 'diff --git a/module.py b/module.py\\n' ;;
      esac
      exit 0 ;;
  esac
done
exec {real} "$@"
"""


def _plant_git(tmp_path: Path, monkeypatch) -> Path:
    """Put a fabricating `git` in a writable directory at the head of PATH; return its marker."""
    real = shutil.which("git")
    assert real
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    marker = tmp_path / "planted-git-ran"
    fake = bin_dir / "git"
    fake.write_text(FAKE_GIT.format(marker=marker, real=real), encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return marker


def test_check_diff_never_runs_a_git_planted_on_path(tmp_path, policy_file, monkeypatch, capsys):
    repository = _repo(tmp_path, policy_file)
    _commit(repository, {".github/workflows/x.yml": "on: push\n"})
    marker = _plant_git(tmp_path, monkeypatch)
    # the trap: a PATH lookup finds the planted git, which reports a benign change
    shown = subprocess.run(
        ["git", "diff", "--name-status", "-z", "main", "HEAD"],
        cwd=repository,
        capture_output=True,
        check=True,
    ).stdout
    assert shown == b"M\0module.py\0" and marker.exists()
    marker.unlink()
    assert _check_diff(repository, policy_file) == 2
    assert "forbidden paths changed: .github/workflows/x.yml" in capsys.readouterr().err
    assert not marker.exists()  # never executed
    # inspect-diff (the Actions path) collects its diff through the same trusted git
    inspect = [
        "inspect-diff", "--config", str(policy_file), "--repository", str(repository),
        "--base", "main", "--patch-output", str(tmp_path / "p"),
        "--paths-output", str(tmp_path / "n"), "--decision-output", str(tmp_path / "d"),
    ]  # fmt: skip
    cli.main(inspect)
    assert not marker.exists()


def test_check_diff_fails_closed_without_a_trusted_git(tmp_path, policy_file, monkeypatch, capsys):
    from agentic_sdlc import trusted_git

    repository = _repo(tmp_path, policy_file)
    _commit(repository, {"module.py": "VALUE = 2\n"})
    marker = _plant_git(tmp_path, monkeypatch)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))  # only the planted one is on PATH
    monkeypatch.setattr(trusted_git, "SYSTEM_GIT_PATHS", ())
    assert _check_diff(repository, policy_file) == 2
    assert "no trusted git executable" in capsys.readouterr().err
    assert not marker.exists()


def test_check_diff_ignores_an_explicit_git_override(tmp_path, policy_file, monkeypatch, capsys):
    repository = _repo(tmp_path, policy_file)
    _commit(repository, {".github/workflows/x.yml": "on: push\n"})
    marker = _plant_git(tmp_path, monkeypatch)
    monkeypatch.setenv("AGENTIC_SDLC_GIT", str(tmp_path / "bin" / "git"))
    assert _check_diff(repository, policy_file) == 2
    err = capsys.readouterr().err
    assert "AGENTIC_SDLC_GIT is ignored by `sdlcctl check-diff`" in err
    assert "forbidden paths changed: .github/workflows/x.yml" in err  # the real git's diff
    assert not marker.exists()


def test_publication_gates_are_strict_whatever_the_policy_and_environment_say(
    tmp_path, policy_file, monkeypatch, capsys
):
    """Codex 4244027559: `trust_mode = "local"` in the policy (committed AND in the working
    tree the agent can edit), CLAUDE_CODE_REMOTE / GITHUB_ACTIONS unset or "false", and an
    AGENTIC_SDLC_GIT naming a planted git: check-diff still resolves git in strict trust, so the
    planted one never runs. A developer-facing command (inspect-diff) does honour local."""
    from agentic_sdlc import trusted_exec

    text = policy_file.read_text().replace("[automation]\n", '[automation]\ntrust_mode = "local"\n')
    policy_file.write_text(text)
    repository = _repo(tmp_path, policy_file)
    _commit(repository, {".github/workflows/x.yml": "on: push\n"})
    monkeypatch.chdir(repository)  # ./agentic-sdlc.toml: the working-tree policy says local
    marker = _plant_git(tmp_path, monkeypatch)
    monkeypatch.setenv("AGENTIC_SDLC_GIT", str(tmp_path / "bin" / "git"))
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "false")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert _check_diff(repository, policy_file) == 2
    assert "forbidden paths changed: .github/workflows/x.yml" in capsys.readouterr().err
    assert not marker.exists()  # never executed
    assert trusted_exec.trust_mode() == trusted_exec.STRICT
    # every gate command runs under gate trust, whatever the policy says
    for argv in (
        ["check-request", "--project", PROJECT, "--issue", "7", "--config", str(policy_file)],
        ["authorize-labeler", "--project", PROJECT, "--issue", "7"],
        ["claim", "--project", PROJECT, "--issue", "7", "--session", "s", "--agent", "a",
         "--branch", "forge/issue-7"],
        ["renew", "--project", PROJECT, "--issue", "7", "--session", "s"],
        ["release", "--project", PROJECT, "--issue", "7", "--session", "s"],
    ):  # fmt: skip
        seen = []
        monkeypatch.setattr(cli, "use_gate_trust", seen.append)
        monkeypatch.setattr(cli, "use_trust_mode", seen.append)
        args = cli.build_parser().parse_args(argv)
        cli._use_trust_for(args)
        assert seen == [True, trusted_exec.STRICT], argv
    monkeypatch.setattr(cli, "use_gate_trust", trusted_exec.use_gate_trust)
    monkeypatch.setattr(cli, "use_trust_mode", trusted_exec.use_trust_mode)
    # the developer-facing inspect-diff keeps the policy's local mode and the override
    inspect = [
        "inspect-diff", "--config", "agentic-sdlc.toml", "--repository", str(repository),
        "--base", "main", "--patch-output", str(tmp_path / "p"),
        "--paths-output", str(tmp_path / "n"), "--decision-output", str(tmp_path / "d"),
    ]  # fmt: skip
    cli.main(inspect)
    assert trusted_exec.trust_mode() == trusted_exec.LOCAL
    assert marker.exists()


def test_trusted_git_vets_the_file_and_every_directory_above_it(tmp_path):
    from agentic_sdlc.trusted_git import git_candidate_reason, trusted_git

    system = trusted_git()
    assert git_candidate_reason(system) is None
    planted = tmp_path / "git"
    planted.write_text("#!/bin/sh\n", encoding="utf-8")
    planted.chmod(0o755)
    assert git_candidate_reason(str(planted)) is not None  # under a writable / non-root tree
    link = tmp_path / "link-to-git"
    link.symlink_to(system)  # the real git, but reached through a writable directory
    assert "link-to-git" in (git_candidate_reason(str(link)) or "")
    assert git_candidate_reason("git") is not None  # never a relative name


# ------------------------------------ check-diff reads the committed policy (Codex 4241614590)


def _loosen(path: Path, **values: str) -> None:
    text = path.read_text(encoding="utf-8")
    for key, value in values.items():
        text = re.sub(rf"(?m)^{key} = .*$", f"{key} = {value}", text)
    path.write_text(text, encoding="utf-8")


def test_check_diff_ignores_a_loosened_uncommitted_policy(tmp_path, policy_file, capsys):
    repository = _repo(tmp_path, policy_file)
    _commit(repository, {f"f{i}.py": "x = 1\n" for i in range(4)})  # max_changed_files = 3
    _loosen(repository / "agentic-sdlc.toml", max_changed_files="100")  # working tree only
    assert _check_diff(repository, policy_file) == 2
    assert "changed-file cap exceeded: 4 > 3" in capsys.readouterr().err
    _git(repository, "add", "agentic-sdlc.toml")  # staged, still not committed
    assert _check_diff(repository, policy_file) == 2
    # committing it on the branch publishes a forbidden change, and the base policy still rules
    _git(repository, "commit", "-qm", "loosen")
    assert _check_diff(repository, policy_file) == 2
    err = capsys.readouterr().err
    assert "forbidden paths changed: agentic-sdlc.toml" in err
    assert "changed-file cap exceeded" in err


def test_check_diff_ignores_an_uncommitted_forbidden_paths_edit(tmp_path, policy_file, capsys):
    _loosen(policy_file, forbidden_paths='["vault/**"]')
    repository = _repo(tmp_path, policy_file)
    _commit(repository, {"vault/key.txt": "k\n"})
    _loosen(repository / "agentic-sdlc.toml", forbidden_paths="[]")
    assert _check_diff(repository, policy_file) == 2
    assert "forbidden paths changed: vault/key.txt" in capsys.readouterr().err


def test_check_diff_fails_closed_on_a_missing_or_broken_base_policy(tmp_path, policy_file, capsys):
    repository = _repo(tmp_path, policy_file)
    _commit(repository, {"module.py": "VALUE = 2\n"})
    argv = ["check-diff", "--project", PROJECT, "--repository", str(repository), "--base", "main"]
    assert cli.main([*argv, "--config", "absent.toml"]) == 2
    assert "absent.toml could not be read from main" in capsys.readouterr().err
    assert cli.main([*argv, "--config", "../outside.toml"]) == 2
    assert cli.main([*argv, "--config", str(tmp_path / "elsewhere.toml")]) == 2
    # the working tree's copy is valid, but main's is not: main's decides
    (tmp_path / "broken").mkdir()
    broken = tmp_path / "broken" / "agentic-sdlc.toml"
    broken.write_text("version = 2\n", encoding="utf-8")
    repository = _repo(tmp_path / "broken", broken)
    (repository / "agentic-sdlc.toml").write_bytes(policy_file.read_bytes())
    _commit(repository, {"module.py": "VALUE = 2\n"})
    assert _check_diff(repository, policy_file) == 2
    assert "is not usable" in capsys.readouterr().err


def test_check_diff_accepts_the_policy_path_absolute_inside_the_repository(tmp_path, policy_file):
    repository = _repo(tmp_path, policy_file)
    _commit(repository, {"module.py": "VALUE = 2\n"})
    argv = ["check-diff", "--project", PROJECT, "--repository", str(repository), "--base", "main"]
    assert cli.main([*argv, "--config", str(repository / "agentic-sdlc.toml")]) == 0


# ----------------------- check-request: content digest, state, committed policy (Codex 5483344896)


def test_check_request_prints_the_content_digest(monkeypatch, policy_file, tmp_path, capsys):
    gh = FakeGh(VALID.format(deps="None"))
    assert _check_request(monkeypatch, policy_file, gh, tmp_path) == 0
    digest = cli.request_digest("Do the thing", VALID.format(deps="None"))
    assert digest.startswith("sha256:") and len(digest) == len("sha256:") + 64
    assert f"content digest: {digest}" in capsys.readouterr().err
    assert json.loads((tmp_path / "request.json").read_text())["contentDigest"] == digest


def _expect(monkeypatch, policy_file, gh, digest):
    monkeypatch.setattr(cli, "run_gh", gh)
    argv = ["check-request", "--project", PROJECT, "--issue", "7", "--config", str(policy_file)]
    return cli.main([*argv, "--expect-digest", digest])


def test_check_request_refuses_a_request_edited_since_its_digest(monkeypatch, policy_file, capsys):
    """Codex 4241614582: an edit followed by re-applied labels re-authorizes, so the final check
    compares the content itself with what step 2 checked."""
    body = VALID.format(deps="None")
    digest = cli.request_digest("Do the thing", body)
    assert _expect(monkeypatch, policy_file, FakeGh(body), digest) == 0
    edited = FakeGh(body.replace("Do the thing.", "Do another thing."))
    assert _expect(monkeypatch, policy_file, edited, digest) == cli.REQUEST_CHANGED == 5
    assert "CHANGED: #7" in capsys.readouterr().err
    retitled = FakeGh(body)
    retitled.issue["title"] = "Do the thing, differently"
    assert _expect(monkeypatch, policy_file, retitled, digest) == 5
    relabelled = FakeGh(body, labels=[*LABELS, "extra"])  # labels are authorized separately
    assert _expect(monkeypatch, policy_file, relabelled, digest) == 0
    assert _expect(monkeypatch, policy_file, FakeGh(body), "sha256:not-a-digest") == 5


def test_check_request_refuses_a_closed_issue(monkeypatch, policy_file, capsys):
    gh = FakeGh(VALID.format(deps="None"))
    gh.issue["state"] = "closed"
    assert _check_request(monkeypatch, policy_file, gh) == cli.ISSUE_CLOSED == 6
    assert "CLOSED: #7 is closed" in capsys.readouterr().err
    gh.issue["state"] = None
    assert _check_request(monkeypatch, policy_file, gh) == 2


def test_check_request_reopened_dependency_stops_the_final_check(monkeypatch, policy_file):
    """Codex 4241614596: the pre-push re-run sees a dependency reopened after step 2."""
    body = VALID.format(deps="- #3")
    digest = cli.request_digest("Do the thing", body)
    assert _expect(monkeypatch, policy_file, FakeGh(body, states={3: "closed"}), digest) == 0
    assert _expect(monkeypatch, policy_file, FakeGh(body, states={3: "open"}), digest) == 4


def test_check_request_with_base_reads_the_committed_policy(
    monkeypatch, policy_file, tmp_path, capsys
):
    repository = _repo(tmp_path, policy_file)
    _loosen(repository / "agentic-sdlc.toml", forbidden_task_patterns="[]")  # uncommitted
    gh = FakeGh("Place a live trade now.\n\n" + VALID.format(deps="None"))
    monkeypatch.setattr(cli, "run_gh", gh)
    argv = ["check-request", "--project", PROJECT, "--issue", "7", "--repository", str(repository)]
    assert cli.main([*argv, "--config", str(repository / "agentic-sdlc.toml")]) == 0  # the trap
    assert cli.main([*argv, "--config", "agentic-sdlc.toml", "--base", "main"]) == 3
    assert cli.main([*argv, "--config", "absent.toml", "--base", "main"]) == 2


# ------------------- check-diff ignores the caller's GIT_* environment (Codex 4241745395)


@pytest.mark.parametrize(
    "redirect",
    [
        {"GIT_DIR": "{benign}/.git", "GIT_WORK_TREE": "{benign}"},
        {"GIT_DIR": "{benign}/.git"},
        {"GIT_OBJECT_DIRECTORY": "{benign}/.git/objects", "GIT_COMMON_DIR": "{benign}/.git"},
        {"GIT_CEILING_DIRECTORIES": "{real}", "GIT_INDEX_FILE": "{benign}/.git/index"},
    ],
)
def test_check_diff_never_reads_a_repository_named_by_the_environment(
    tmp_path, policy_file, monkeypatch, capsys, redirect
):
    (tmp_path / "real").mkdir()
    (tmp_path / "benign").mkdir()
    real = _repo(tmp_path / "real", policy_file)
    _commit(real, {".github/workflows/x.yml": "on: push\n"})
    benign = _repo(tmp_path / "benign", policy_file)
    _commit(benign, {"module.py": "VALUE = 2\n"})
    assert _check_diff(benign, policy_file) == 0  # the decoy alone passes
    capsys.readouterr()
    for key, value in redirect.items():
        monkeypatch.setenv(key, value.format(benign=benign, real=real))
    assert _check_diff(real, policy_file) == 2
    assert "forbidden paths changed: .github/workflows/x.yml" in capsys.readouterr().err


@pytest.mark.parametrize("where", ["global", "repository"])
def test_check_diff_sees_a_gitlink_config_would_hide(
    tmp_path, policy_file, monkeypatch, capsys, where
):
    """`diff.ignoreSubmodules=all` hides a gitlink change from `git diff`; a push still
    publishes it. Neither a global config (no longer read) nor the repository's own (the agent's
    to write; overridden on the command line) may hide one."""
    repository = _repo(tmp_path, policy_file)
    _git(
        repository,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{'1' * 40},.github/workflows/vendored",
    )
    (repository / "module.py").write_text("VALUE = 2\n", encoding="utf-8")  # one allowed edit
    _git(repository, "add", "--", "module.py")  # not `add .`: that would drop the gitlink
    _git(repository, "commit", "-qm", "vendor")
    if where == "global":
        config = tmp_path / "global.gitconfig"
        config.write_text("[diff]\n\tignoreSubmodules = all\n", encoding="utf-8")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    else:
        _git(repository, "config", "diff.ignoreSubmodules", "all")
    assert _check_diff(repository, policy_file) == 2
    assert "forbidden paths changed: .github/workflows/vendored" in capsys.readouterr().err


def test_git_environment_keeps_no_caller_git_variable(monkeypatch):
    from agentic_sdlc import git_diff

    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_EXEC_PATH", "GIT_CONFIG_COUNT", "GIT_X"):
        monkeypatch.setenv(key, "/elsewhere")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/elsewhere")
    env = git_diff._object_env()
    assert {k: v for k, v in env.items() if k.startswith("GIT_")} == {
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_GRAFT_FILE": git_diff._NO_GRAFTS,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }


def test_check_diff_refuses_a_repository_that_is_not_a_top_level(tmp_path, policy_file, capsys):
    repository = _repo(tmp_path, policy_file)
    _commit(repository, {"sub/module.py": "VALUE = 2\n"})
    assert _check_diff(repository / "sub", policy_file) == 2
    assert "not the top level" in capsys.readouterr().err
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _check_diff(plain, policy_file) == 2


# ------------------------------- check-diff lists type changes too (Codex 4241745397)


def test_check_diff_sees_a_forbidden_path_turned_into_a_symlink(tmp_path, policy_file, capsys):
    repository = _repo(tmp_path, policy_file)
    (repository / "agentic-sdlc.toml").unlink()
    (repository / "agentic-sdlc.toml").symlink_to("module.py")  # status T, not M
    _commit(repository, {"module.py": "VALUE = 2\n"})  # plus one allowed modification
    status = _git(repository, "diff", "--name-status", "main", "HEAD")
    assert "T\tagentic-sdlc.toml" in status
    assert _check_diff(repository, policy_file) == 2
    assert "forbidden paths changed: agentic-sdlc.toml" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("raw", "paths"),
    [
        (b"T\0a\0M\0b\0", ("a", "b")),
        (b"R100\0old\0new\0C075\0x\0y\0", ("new", "old", "x", "y")),
        (b"", ()),
    ],
)
def test_name_status_lists_every_known_status(raw, paths):
    from agentic_sdlc.git_diff import _paths_from_name_status

    assert _paths_from_name_status(raw) == paths


@pytest.mark.parametrize("raw", [b"U\0a\0", b"X\0a\0", b"Q\0a\0", b"M\0a\0\0M\0b\0", b"R100\0a\0"])
def test_name_status_fails_closed_on_anything_else(raw):
    from agentic_sdlc.git_diff import GitDiffError, _paths_from_name_status

    with pytest.raises(GitDiffError):
        _paths_from_name_status(raw)


# ----------------------------- sdlcctl runs a trusted GitHub CLI (Codex 4241745403)


def _plant_gh(tmp_path: Path, monkeypatch, *, alone: bool = False) -> Path:
    """A fabricating `gh` at the head of PATH that would authorize anything; its marker. With
    `alone`, PATH holds nothing else: as a non-root user the resolver searches PATH, and a
    root-owned system gh further along it (`/usr/bin/gh` on a GitHub-hosted runner) is a
    trusted one it would rightly pick instead."""
    bin_dir = tmp_path / "ghbin"
    bin_dir.mkdir()
    marker = tmp_path / "planted-gh-ran"
    fake = bin_dir / "gh"
    fake.write_text(f'#!/bin/sh\ntouch "{marker}"\necho "{{}}"\n', encoding="utf-8")
    fake.chmod(0o755)
    rest = "" if alone else f"{os.pathsep}{os.environ.get('PATH', '')}"
    monkeypatch.setenv("PATH", f"{bin_dir}{rest}")
    return marker


def test_run_gh_never_runs_a_gh_planted_on_path(tmp_path, monkeypatch):
    from agentic_sdlc import onboard, trusted_exec

    marker = _plant_gh(tmp_path, monkeypatch, alone=True)
    monkeypatch.setattr(trusted_exec, "SYSTEM_GH_PATHS", ())
    with pytest.raises(OnboardError, match="no trusted gh executable"):
        onboard.run_gh(["api", f"repos/{PROJECT}/issues/7"])
    assert not marker.exists()


def test_check_request_fails_closed_without_a_trusted_gh(
    tmp_path, policy_file, monkeypatch, capsys
):
    from agentic_sdlc import onboard, trusted_exec

    monkeypatch.setattr(cli, "run_gh", onboard.run_gh)  # the real one, not the fake GitHub
    marker = _plant_gh(tmp_path, monkeypatch, alone=True)
    monkeypatch.setattr(trusted_exec, "SYSTEM_GH_PATHS", ())
    argv = ["check-request", "--project", PROJECT, "--issue", "7", "--config", str(policy_file)]
    assert cli.main(argv) == 2
    assert "no trusted gh executable" in capsys.readouterr().err
    assert not marker.exists()


def test_an_explicit_gh_override_is_vetted_too(tmp_path, monkeypatch):
    from agentic_sdlc import onboard

    marker = _plant_gh(tmp_path, monkeypatch)
    monkeypatch.setenv("AGENTIC_SDLC_GH", str(tmp_path / "ghbin" / "gh"))
    with pytest.raises(OnboardError, match="AGENTIC_SDLC_GH"):
        onboard.run_gh(["api", "user"])
    assert not marker.exists()


def test_trusted_gh_vets_like_trusted_git(tmp_path):
    from agentic_sdlc.trusted_exec import candidate_reason
    from agentic_sdlc.trusted_git import trusted_git

    planted = tmp_path / "gh"
    planted.write_text("#!/bin/sh\n", encoding="utf-8")
    planted.chmod(0o755)
    assert candidate_reason(str(planted)) is not None
    assert candidate_reason(trusted_git()) is None  # a root-owned system binary passes


def test_run_gh_pins_the_host_and_drops_repository_overrides(tmp_path, monkeypatch):
    import subprocess as sp

    from agentic_sdlc import onboard

    monkeypatch.setattr(onboard, "trusted_gh", lambda: "/trusted/gh")
    for key in ("GH_REPO", "GH_HOST", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
        monkeypatch.setenv(key, "evil.example")
    monkeypatch.setenv("GH_TOKEN", "token")
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"], seen["env"] = args, kwargs["env"]
        return sp.CompletedProcess(args, 0, "ok", "")

    monkeypatch.setattr(onboard.subprocess, "run", fake_run)
    assert onboard.run_gh(["api", "user"]) == "ok"
    assert seen["args"] == ["/trusted/gh", "api", "user"]
    env = seen["env"]
    assert env["GH_HOST"] == "github.com" and env["GH_TOKEN"] == "token"
    assert not {"GH_REPO", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"} & set(env)


def test_a_trusted_gh_later_on_path_wins_over_a_planted_one(tmp_path, monkeypatch):
    """As a non-root user PATH is searched; a planted gh ahead of a trusted one is skipped, not
    run, and not a reason to fail (a GitHub-hosted runner: `runner` with a root-owned
    /usr/bin/gh)."""
    from agentic_sdlc import trusted_exec

    planted_dir, system_dir = tmp_path / "planted", tmp_path / "system"
    for directory in (planted_dir, system_dir):
        directory.mkdir()
        (directory / "gh").write_text("#!/bin/sh\n", encoding="utf-8")
        (directory / "gh").chmod(0o755)
    monkeypatch.setattr(trusted_exec, "_euid", lambda: 1001)
    monkeypatch.setattr(
        trusted_exec,
        "candidate_reason",
        lambda c: None if c.startswith(str(system_dir)) else f"{c} is writable by this user",
    )
    path = f"{planted_dir}{os.pathsep}{system_dir}"
    assert trusted_exec.resolve_trusted("gh", (), "UNSET_OVERRIDE", {"PATH": path}) == (
        os.path.realpath(system_dir / "gh")
    )
    with pytest.raises(trusted_exec.UntrustedExecutableError, match="writable by this user"):
        trusted_exec.resolve_trusted("gh", (), "UNSET_OVERRIDE", {"PATH": str(planted_dir)})


# ---------------- trusted programs get an allowlisted environment (Codex 4241839908, 4241839914)

_LOADER = {
    "LD_PRELOAD": "/tmp/evil.so",
    "LD_LIBRARY_PATH": "/tmp/evil",
    "LD_AUDIT": "/tmp/evil-audit.so",
    "DYLD_INSERT_LIBRARIES": "/tmp/evil.dylib",
}


def test_every_git_run_gets_an_allowlisted_environment(tmp_path, policy_file, monkeypatch):
    from agentic_sdlc import git_diff

    repository = _repo(tmp_path, policy_file)
    _commit(repository, {"module.py": "VALUE = 2\n"})
    for key, value in {**_LOADER, "PATH": f"{tmp_path}:/usr/bin:/bin", "HOME": "/x"}.items():
        monkeypatch.setenv(key, value)
    real_run = git_diff.subprocess.run
    seen: list[dict[str, str]] = []

    def spy(*args, **kwargs):
        seen.append(dict(kwargs["env"]))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(git_diff.subprocess, "run", spy)
    assert _check_diff(repository, policy_file) == 0
    assert seen and all(env == git_diff._object_env() for env in seen)
    assert seen[0] == {
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_GRAFT_FILE": git_diff._NO_GRAFTS,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }


def _spy_gh(monkeypatch, answers):
    """Replace subprocess.run in onboard: record (argv, env, cwd, config-dir state) per call."""
    import subprocess as sp

    from agentic_sdlc import onboard

    monkeypatch.setattr(onboard, "trusted_gh", lambda: "/trusted/gh")
    calls = []

    def fake_run(args, **kwargs):
        env = kwargs["env"]
        state = None
        if "GH_CONFIG_DIR" in env:
            where = Path(env["GH_CONFIG_DIR"])
            state = (where.is_dir(), list(where.iterdir()), where.stat().st_mode & 0o777)
        calls.append({"args": list(args), "env": dict(env), "cwd": kwargs.get("cwd"), "dir": state})
        return sp.CompletedProcess(args, 0, answers.get(tuple(args[1:3]), "ok"), "")

    monkeypatch.setattr(onboard.subprocess, "run", fake_run)
    return calls


def test_gh_runs_with_an_allowlisted_environment_and_a_fresh_config_dir(monkeypatch):
    from agentic_sdlc import onboard

    hostile = {
        **_LOADER,
        "GH_CONFIG_DIR": "/tmp/evil-gh",
        "XDG_CONFIG_HOME": "/tmp/evil-xdg",
        "HOME": "/tmp/evil-home",
        "SSL_CERT_FILE": "/tmp/evil-ca.pem",
        "SSL_CERT_DIR": "/tmp/evil-ca",
        "GH_REPO": "evil/repo",
        "PATH": "/tmp/evil-bin:/usr/bin",
        "HTTPS_PROXY": "http://proxy.internal:3128",
        "NO_PROXY": "localhost",
        "GH_TOKEN": "token",
        "GITHUB_TOKEN": "other",
    }
    for key in onboard.GH_PROXY_ENV:
        monkeypatch.delenv(key, raising=False)
    for key, value in hostile.items():
        monkeypatch.setenv(key, value)
    calls = _spy_gh(monkeypatch, {})
    assert onboard.run_gh(["api", "user"]) == "ok"
    assert len(calls) == 1  # a token in the environment: no `gh auth token`
    (call,) = calls
    env = call["env"]
    config_dir = env["GH_CONFIG_DIR"]
    assert config_dir != "/tmp/evil-gh" and env["HOME"] == config_dir == call["cwd"]
    assert call["dir"] == (True, [], 0o700)  # existed, empty and private during the call
    assert not Path(config_dir).exists()  # removed afterwards
    assert env == {
        "PATH": "/usr/bin:/bin",
        "HOME": config_dir,
        "GH_CONFIG_DIR": config_dir,
        "GH_HOST": "github.com",
        "GH_PROMPT_DISABLED": "1",
        "GH_NO_UPDATE_NOTIFIER": "1",
        "GH_SPINNER_DISABLED": "1",
        "NO_COLOR": "1",
        "LC_ALL": "C.UTF-8",
        "GH_TOKEN": "token",
        "HTTPS_PROXY": "http://proxy.internal:3128",
        "NO_PROXY": "localhost",
    }
    assert onboard.run_gh(["api", "user"]) == "ok"
    assert calls[1]["env"]["GH_CONFIG_DIR"] != config_dir  # fresh per call


def test_gh_token_falls_back_to_the_users_stored_login(monkeypatch):
    from agentic_sdlc import onboard

    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    for key, value in _LOADER.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GITHUB_TOKEN", "")
    calls = _spy_gh(monkeypatch, {("auth", "token"): "gho_stored\n"})
    assert onboard.run_gh(["api", "user"]) == "ok"
    lookup, call = calls
    assert lookup["args"] == ["/trusted/gh", "auth", "token", "--hostname", "github.com"]
    assert not any(k.startswith(("LD_", "DYLD_")) for k in lookup["env"])
    assert call["env"]["GH_TOKEN"] == "gho_stored"
    monkeypatch.setenv("GITHUB_TOKEN", "from-env")
    calls.clear()
    assert onboard.run_gh(["api", "user"]) == "ok"
    assert len(calls) == 1 and calls[0]["env"]["GH_TOKEN"] == "from-env"


# ---------------- check-diff judges every commit it pushes (Codex 4241839922)


def test_check_diff_refuses_a_forbidden_file_added_then_deleted(tmp_path, policy_file, capsys):
    repository = _repo(tmp_path, policy_file)
    _commit(repository, {".env.production": "SECRET=1\n", "module.py": "VALUE = 2\n"})
    (repository / ".env.production").unlink()
    _git(repository, "commit", "-qam", "hide it again")
    # the trap: the end state shows only module.py, yet the push publishes the secret
    assert _git(repository, "diff", "--name-only", "main", "HEAD") == "module.py"
    out = tmp_path / "decision.json"
    assert _check_diff(repository, policy_file, "--output", str(out)) == 2
    assert "forbidden paths changed: .env.production" in capsys.readouterr().err
    assert json.loads(out.read_text())["diff"]["paths"] == [".env.production", "module.py"]


def test_check_diff_sees_both_sides_of_a_rename_in_any_commit(tmp_path, policy_file, capsys):
    repository = _repo(tmp_path, policy_file)
    _commit(repository, {".github/workflows/x.yml": "on: push\n"})
    _git(repository, "mv", ".github/workflows/x.yml", "x.yml")
    _git(repository, "commit", "-qm", "move it out")
    _git(repository, "rm", "-q", "x.yml")
    _commit(repository, {"module.py": "VALUE = 2\n"})
    assert _check_diff(repository, policy_file) == 2
    assert "forbidden paths changed: .github/workflows/x.yml" in capsys.readouterr().err


def test_check_diff_refuses_a_merge_commit_in_the_series(tmp_path, policy_file, capsys):
    repository = _repo(tmp_path, policy_file)
    _commit(repository, {"module.py": "VALUE = 2\n"})
    _git(repository, "switch", "-q", "-c", "side", "main")
    _commit(repository, {"other.py": "X = 1\n"})
    _git(repository, "switch", "-q", "forge/issue-7")
    _git(repository, "merge", "-q", "--no-ff", "--no-edit", "side")
    assert _check_diff(repository, policy_file) == 2
    assert "is a merge commit" in capsys.readouterr().err


def test_check_diff_caps_a_large_commit_that_a_later_one_shrank(tmp_path, policy_file, capsys):
    """Codex 4242095558: 10,000 lines added in one commit and shrunk to one line in the next is a
    one-line endpoint diff, but the push publishes the 10,000-line commit and its blob."""
    repository = _repo(tmp_path, policy_file)
    _commit(repository, {"x.py": "x = 1\n" * 10_000})
    _commit(repository, {"x.py": "x = 2\n"}, "shrink it")
    # the trap: the end state is a one-line change, well inside every cap
    assert _git(repository, "diff", "--numstat", "main", "HEAD") == "1\t0\tx.py"
    out = tmp_path / "decision.json"
    assert _check_diff(repository, policy_file, "--output", str(out)) == 2
    err = capsys.readouterr().err
    assert "diff-line cap exceeded: 20001 > 100" in err  # 10,000 + (1 + 10,000)
    assert "patch-byte cap exceeded" in err
    diff = json.loads(out.read_text())["diff"]
    assert diff["net"]["addedLines"] == 1 and diff["history"]["addedLines"] == 10_001
    assert diff["addedLines"] == 10_001 and diff["patchBytes"] > 60_000


def test_check_diff_sums_the_lines_of_every_commit(tmp_path, policy_file, capsys):
    """Each commit alone is under max_diff_lines = 100 and so is the end state; together the
    series publishes 181 changed lines, and the cap is applied to that sum."""
    repository = _repo(tmp_path, policy_file)
    _loosen(repository / "agentic-sdlc.toml", max_patch_bytes="100000")
    _git(repository, "commit", "-qam", "patch bytes are not the point here")
    _git(repository, "branch", "-f", "main", "HEAD")
    _commit(repository, {"x.py": "".join(f"x{i} = {i}\n" for i in range(90))})
    _commit(repository, {"x.py": "x = 2\n"}, "shrink it")
    assert _check_diff(repository, policy_file) == 2
    assert "diff-line cap exceeded: 181 > 100" in capsys.readouterr().err


def test_check_diff_counts_a_binary_change_by_its_size(tmp_path, policy_file, capsys):
    """`--numstat` reports a binary change as `-` lines; counted as zero it would pass any line
    cap, so it counts fail-closed as its size in bytes."""
    repository = _repo(tmp_path, policy_file)
    (repository / "blob.bin").write_bytes(bytes(range(256))[:200])  # NULs: binary to git
    _git(repository, "add", "blob.bin")
    _git(repository, "commit", "-qm", "a blob")
    assert _git(repository, "diff", "--numstat", "main", "HEAD") == "-\t-\tblob.bin"
    out = tmp_path / "decision.json"
    assert _check_diff(repository, policy_file, "--output", str(out)) == 2
    assert "diff-line cap exceeded: 200 > 100" in capsys.readouterr().err
    diff = json.loads(out.read_text())["diff"]
    assert diff["net"]["addedLines"] == 200 and diff["history"]["addedLines"] == 200


def test_check_diff_refuses_a_series_whose_net_change_is_empty(tmp_path, policy_file, capsys):
    repository = _repo(tmp_path, policy_file)
    _commit(repository, {"module.py": "VALUE = 2\n"})
    _commit(repository, {"module.py": "VALUE = 1\n"})  # reverted
    assert _check_diff(repository, policy_file) == 2
    assert "no files changed" in capsys.readouterr().err


# ---------------- --base must be the default branch's head on GitHub (Codex 4241839926)


def _repointed_base(tmp_path: Path, policy_file: Path) -> Path:
    """A repository whose local `main` was moved onto a commit that loosens the policy, with a
    change on top that only the loosened policy allows; GitHub's head is the original main."""
    repository = _repo(tmp_path, policy_file)
    _REMOTE["head"] = _git(repository, "rev-parse", "main")
    _git(repository, "switch", "-q", "-c", "loose", "main")
    _loosen(repository / "agentic-sdlc.toml", max_changed_files="100")
    _commit(repository, {}, "loosen")
    _git(repository, "update-ref", "refs/heads/main", "loose")
    _git(repository, "switch", "-q", "-c", "forge/issue-7b", "loose")
    _commit(repository, {f"f{i}.py": "x = 1\n" for i in range(4)})  # max_changed_files = 3
    return repository


def test_check_diff_refuses_a_base_that_is_not_githubs_head(tmp_path, policy_file, capsys):
    repository = _repointed_base(tmp_path, policy_file)
    assert _check_diff(repository, policy_file) == 2
    err = capsys.readouterr().err
    assert "--base main is" in err and "on GitHub; fetch it again" in err
    _REMOTE["head"] = _git(repository, "rev-parse", "main")  # had GitHub agreed: it passes
    assert _check_diff(repository, policy_file) == 0


def test_check_request_refuses_a_base_that_is_not_githubs_head(
    tmp_path, policy_file, monkeypatch, capsys
):
    repository = _repointed_base(tmp_path, policy_file)
    monkeypatch.setattr(cli, "run_gh", FakeGh(VALID.format(deps="None")))
    argv = ["check-request", "--project", PROJECT, "--issue", "7", "--repository", str(repository)]
    assert cli.main([*argv, "--config", "agentic-sdlc.toml", "--base", "main"]) == 2
    assert "on GitHub; fetch it again" in capsys.readouterr().err


@pytest.mark.parametrize(
    "repo_answer, ref_answer",
    [
        (OnboardError("gh api failed: HTTP 502"), None),
        ({"full_name": "someone/else", "default_branch": "main"}, None),
        ({"full_name": PROJECT, "default_branch": ""}, None),
        ({"full_name": PROJECT, "default_branch": "main"}, OnboardError("HTTP 404")),
        ({"full_name": PROJECT, "default_branch": "main"}, {"object": {"type": "tag"}}),
        ({"full_name": PROJECT, "default_branch": "main"}, "not json"),
        ({"full_name": PROJECT, "default_branch": "dev"}, None),
    ],
)
def test_check_diff_fails_closed_when_github_cannot_vouch_for_the_base(
    tmp_path, policy_file, monkeypatch, capsys, repo_answer, ref_answer
):
    repository = _repo(tmp_path, policy_file)
    _commit(repository, {"module.py": "VALUE = 2\n"})
    head = _git(repository, "rev-parse", "main")

    def gh(args, input=None):
        answer = repo_answer if args[1] == f"repos/{PROJECT}" else ref_answer
        if args[1].endswith("/git/ref/heads/dev"):
            answer = {"ref": "refs/heads/dev", "object": {"type": "commit", "sha": "0" * 40}}
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            answer = {"ref": "refs/heads/main", "object": {"type": "commit", "sha": head}}
        return answer if isinstance(answer, str) else json.dumps(answer)

    monkeypatch.setattr(cli, "run_gh", gh)
    assert _check_diff(repository, policy_file) == 2
    assert "ERROR" in capsys.readouterr().err


def test_check_diff_requires_the_policy_of_the_project_it_verified(
    tmp_path, policy_file, monkeypatch, capsys
):
    repository = _repo(tmp_path, policy_file)
    _commit(repository, {"module.py": "VALUE = 2\n"})
    head = _git(repository, "rev-parse", "main")
    other = "someone/else"

    def gh(args, input=None):
        if args[1] == f"repos/{other}":
            return json.dumps({"full_name": other, "default_branch": "main"})
        return json.dumps({"ref": "refs/heads/main", "object": {"type": "commit", "sha": head}})

    monkeypatch.setattr(cli, "run_gh", gh)
    argv = ["check-diff", "--project", other, "--repository", str(repository), "--base", "main"]
    assert cli.main(argv) == 2
    assert "is not someone/else's" in capsys.readouterr().err


# ------------- the routine's ONE final gate: check-request --authorize-labels (Codex 4242264478)

#: The approval set the policy fixture names (implementation, ready, human-review).
APPROVAL = ["implementation-approved", "agent-ready", "human-review-required"]


def _stamp(minute: int) -> str:
    return f"2026-01-01T00:{minute:02d}:00Z"


class HistoryGh(FakeGh):
    """A FakeGh whose issue also has an update time, a label history, a body-edit time and
    labelers with permissions -- what `authorize-labeler` reads. `on_read[n]` runs right after
    the n-th read (1-based) of the event history is answered, i.e. once the n-th snapshot (the
    issue, then its history) is complete: the change lands between it and the next read.
    `reads` counts the reads of the issue itself."""

    def __init__(self, body: str, **kwargs):
        super().__init__(body, **kwargs)
        self.issue["updated_at"] = _stamp(10)
        self.events: list[dict] = []
        self.edited_at: str | None = None
        self.permissions = {"maintainer": "write", "triager": "triage"}
        self.reads = 0
        self.history_reads = 0
        #: `before_read[n]` runs just before the n-th read of the issue is answered
        self.before_read: dict[int, object] = {}
        self.on_read: dict[int, object] = {}
        self.clock = 10
        #: the repository's open PRs (graphql rows); `on_pr_read[n]` runs right after the n-th
        #: read of that inventory is answered
        self.prs: list[dict] = []
        self.pr_reads = 0
        self.on_pr_read: dict[int, object] = {}
        for label in APPROVAL:
            self.label("labeled", label, "maintainer")

    def tick(self) -> str:
        self.clock += 1
        self.issue["updated_at"] = _stamp(self.clock)
        return _stamp(self.clock)

    def label(self, kind: str, name: str, login: str) -> None:
        at = self.tick()
        self.events.append(
            {
                "id": len(self.events) + 1,
                "event": kind,
                "label": {"name": name},
                "actor": {"login": login},
                "created_at": at,
            }
        )

    def edit(self, body: str) -> None:
        self.issue["body"] = body
        self.edited_at = self.tick()

    def __call__(self, args, input=None):
        args = list(args)
        if github_answer(args) is not None:
            return github_answer(args)
        if args[1] == "graphql" and "pullRequests" in " ".join(args):
            self.calls.append(args)
            info = {"hasNextPage": False, "endCursor": None}
            page = {"pageInfo": info, "nodes": list(self.prs)}
            answer = json.dumps({"data": {"repository": {"pullRequests": page}}})
            self.pr_reads += 1
            change = self.on_pr_read.get(self.pr_reads)
            if change is not None:
                change(self)
            return answer
        if args[1].split("?")[0].endswith("/timeline"):
            self.calls.append(args)
            return "[]"
        if args[1] == "graphql":
            self.calls.append(args)
            node = {"lastEditedAt": self.edited_at, "userContentEdits": {"nodes": []}}
            return json.dumps({"data": {"repository": {"issue": node}}})
        if "/events" in args[1]:
            self.calls.append(args)
            answer = json.dumps([list(self.events)])
            self.history_reads += 1
            change = self.on_read.get(self.history_reads)
            if change is not None:
                change(self)
            return answer
        if "/collaborators/" in args[1]:
            self.calls.append(args)
            login = args[1].split("/collaborators/")[1].split("/")[0]
            return json.dumps({"permission": self.permissions[login]})
        if args[1].endswith("/issues/7"):
            self.reads += 1
            change = self.before_read.get(self.reads)
            if change is not None:
                change(self)
        return super().__call__(args, input)


def _final_gate(monkeypatch, policy_file, gh, digest, tmp_path=None):
    monkeypatch.setattr(cli, "run_gh", gh)
    argv = ["check-request", "--project", PROJECT, "--issue", "7", "--config", str(policy_file)]
    argv += ["--expect-digest", digest, "--authorize-labels"]
    if tmp_path is not None:
        argv += ["--output", str(tmp_path / "gate.json")]
    return cli.main(argv)


def _edit_and_reapprove(gh: HistoryGh) -> None:
    """A maintainer edits the request, then removes and re-applies every approval label: the
    labeler rule alone is satisfied again, the request is not the one implemented."""
    gh.edit(gh.issue["body"].replace("Do the thing.", "Do another thing."))
    for label in APPROVAL:
        gh.label("unlabeled", label, "maintainer")
        gh.label("labeled", label, "maintainer")


def test_final_gate_passes_a_stable_authorized_request(monkeypatch, policy_file, tmp_path, capsys):
    body = VALID.format(deps="- #3")
    gh = HistoryGh(body, states={3: "closed"})
    digest = cli.request_digest("Do the thing", body)
    assert _final_gate(monkeypatch, policy_file, gh, digest, tmp_path) == 0
    assert gh.reads == 2  # judged once, then re-read and found unchanged
    result = json.loads((tmp_path / "gate.json").read_text())
    assert result["ready"] is True and result["authorization"]["authorized"] is True
    assert result["stable"] == {"updatedAt": gh.issue["updated_at"], "attempts": 1}
    assert "stable across two reads" in capsys.readouterr().err


def test_the_old_two_command_gate_passes_an_edit_and_reapply_between_them(monkeypatch, policy_file):
    """The hole (Codex 4242264478): `check-request --expect-digest` then `authorize-labeler` as
    two commands. An edit plus a label re-apply landing between them passes both."""
    body = VALID.format(deps="None")
    gh = HistoryGh(body)
    digest = cli.request_digest("Do the thing", body)
    assert _expect(monkeypatch, policy_file, gh, digest) == 0
    _edit_and_reapprove(gh)
    assert (
        cli.main(
            ["authorize-labeler", "--project", PROJECT, "--issue", "7"]
            + [arg for label in APPROVAL for arg in ("--label", label)]
        )
        == 0
    )
    # the one gate sees it whenever it lands
    assert _final_gate(monkeypatch, policy_file, gh, digest) == cli.REQUEST_CHANGED


def test_final_gate_refuses_an_edit_and_reapply_between_its_reads(
    monkeypatch, policy_file, tmp_path, capsys
):
    body = VALID.format(deps="None")
    gh = HistoryGh(body)
    gh.before_read[2] = _edit_and_reapprove  # after the snapshot was judged, as it is re-read
    digest = cli.request_digest("Do the thing", body)
    assert _final_gate(monkeypatch, policy_file, gh, digest, tmp_path) == cli.REQUEST_CHANGED
    assert gh.reads == 2  # a changed title or body is never retried
    result = json.loads((tmp_path / "gate.json").read_text())
    assert result["ready"] is False
    assert {"content digest", "updated_at", "events"} <= set(result["changedWhileChecking"])
    assert "CHANGED: #7: the title or body changed while it was checked" in capsys.readouterr().err


def test_final_gate_refuses_an_edit_made_after_the_labels(monkeypatch, policy_file, capsys):
    """An edit that keeps the digest (say, reverted) but post-dates the approval is unapproved
    content history: the labeler rule refuses it, with no separate command to skip."""
    body = VALID.format(deps="None")
    gh = HistoryGh(body)
    gh.edit(body)  # edited after every label was applied, to the same text
    digest = cli.request_digest("Do the thing", body)
    assert _final_gate(monkeypatch, policy_file, gh, digest) == cli.LABELS_NOT_AUTHORIZED == 7
    assert "NOT AUTHORIZED: #7" in capsys.readouterr().err


def test_final_gate_refuses_a_label_reapplied_by_a_non_writer_between_reads(
    monkeypatch, policy_file, capsys
):
    """A change that leaves the content alone is judged afresh -- and refused on the new
    snapshot."""
    body = VALID.format(deps="None")
    gh = HistoryGh(body)

    def triage(gh):
        gh.label("unlabeled", "agent-ready", "maintainer")
        gh.label("labeled", "agent-ready", "triager")

    gh.on_read[1] = triage
    digest = cli.request_digest("Do the thing", body)
    assert _final_gate(monkeypatch, policy_file, gh, digest) == cli.LABELS_NOT_AUTHORIZED
    assert gh.reads == 3  # read, re-read (changed), judged afresh
    assert "triager applied 'agent-ready' without repository write authority" in (
        capsys.readouterr().err
    )


def test_final_gate_rejudges_a_benign_change_and_passes_once_stable(monkeypatch, policy_file):
    body = VALID.format(deps="None")
    gh = HistoryGh(body)
    gh.on_read[1] = lambda gh: gh.tick()  # e.g. a comment: only the update time moves
    digest = cli.request_digest("Do the thing", body)
    assert _final_gate(monkeypatch, policy_file, gh, digest) == 0
    assert gh.reads == 4


def test_final_gate_gives_up_on_an_issue_that_keeps_changing(monkeypatch, policy_file, capsys):
    body = VALID.format(deps="None")
    gh = HistoryGh(body)
    for read in range(1, 2 * cli.CHECK_REQUEST_ATTEMPTS + 1):
        gh.on_read[read] = lambda gh: gh.tick()
    digest = cli.request_digest("Do the thing", body)
    assert _final_gate(monkeypatch, policy_file, gh, digest) == cli.REQUEST_UNSTABLE == 8
    assert gh.reads == 2 * cli.CHECK_REQUEST_ATTEMPTS
    assert "UNSTABLE: #7" in capsys.readouterr().err


def test_final_gate_refuses_a_reopened_dependency(monkeypatch, policy_file, capsys):
    body = VALID.format(deps="- #3")
    digest = cli.request_digest("Do the thing", body)
    gh = HistoryGh(body, states={3: "open"})
    assert _final_gate(monkeypatch, policy_file, gh, digest) == cli.DEPENDENCY_OPEN
    assert "WAITING: #7" in capsys.readouterr().err


def test_final_gate_refuses_a_dependency_reopened_between_its_reads(
    monkeypatch, policy_file, capsys
):
    """Codex 4242373526: the dependencies are part of the stable snapshot. #3 is closed when the
    first snapshot is judged and reopened before the re-read: refused, never "stable"."""
    body = VALID.format(deps="- #3")
    digest = cli.request_digest("Do the thing", body)
    gh = HistoryGh(body, states={3: "closed"})

    def reopen(gh):
        gh.states[3] = "open"
        gh.dep_updated[3] = "2026-01-01T01:00:00Z"

    gh.on_dep_read[1] = reopen
    assert _final_gate(monkeypatch, policy_file, gh, digest) == cli.DEPENDENCY_OPEN
    err = capsys.readouterr().err
    assert "changed while it was checked (dependencies)" in err and "WAITING: #7" in err


def test_final_gate_rejudges_a_dependency_reopened_and_closed_between_its_reads(
    monkeypatch, policy_file
):
    """Reopened and closed again between the reads: the state matches, the update time does
    not, so the snapshot is judged afresh rather than called stable."""
    body = VALID.format(deps="- #3")
    digest = cli.request_digest("Do the thing", body)
    gh = HistoryGh(body, states={3: "closed"})
    gh.on_dep_read[1] = lambda gh: gh.dep_updated.update({3: "2026-01-01T01:00:00Z"})
    assert _final_gate(monkeypatch, policy_file, gh, digest) == 0
    assert gh.reads == 4 and gh.dep_reads == 4


def test_final_gate_refuses_a_closed_issue_and_fails_closed_on_an_unreadable_history(
    monkeypatch, policy_file
):
    body = VALID.format(deps="None")
    digest = cli.request_digest("Do the thing", body)
    closed = HistoryGh(body)
    closed.on_read[1] = lambda gh: gh.issue.update(state="closed")
    assert _final_gate(monkeypatch, policy_file, closed, digest) == cli.ISSUE_CLOSED
    no_time = HistoryGh(body)
    del no_time.issue["updated_at"]
    assert _final_gate(monkeypatch, policy_file, no_time, digest) == 2
    unreadable = HistoryGh(body)
    unreadable.permissions["maintainer"] = OnboardError("gh api failed: HTTP 502")
    assert _final_gate(monkeypatch, policy_file, unreadable, digest) == 2


def test_label_needs_authorize_labels(monkeypatch, policy_file):
    gh = HistoryGh(VALID.format(deps="None"))
    monkeypatch.setattr(cli, "run_gh", gh)
    argv = ["check-request", "--project", PROJECT, "--issue", "7", "--config", str(policy_file)]
    assert cli.main([*argv, "--label", "agent-ready"]) == 2


# Codex 4243193102: the final gate reads the issue's open pull requests with each snapshot -- a
# PR opened or linked after the claim already represents the work, so pushing would duplicate it.

_PR = {"number": 20, "url": "https://x/pr/20", "title": "t", "body": "Fixes #7"}


def test_final_gate_refuses_an_issue_that_has_an_open_pr(monkeypatch, policy_file, tmp_path):
    body = VALID.format(deps="None")
    gh = HistoryGh(body)
    gh.prs = [dict(_PR)]
    digest = cli.request_digest("Do the thing", body)
    assert _final_gate(monkeypatch, policy_file, gh, digest, tmp_path) == cli.PULL_REQUEST_OPEN
    result = json.loads((tmp_path / "gate.json").read_text())
    assert result["ready"] is False and result["openPullRequests"] == ["https://x/pr/20"]


def test_final_gate_continuation_tolerates_only_the_units_own_pr(
    monkeypatch, policy_file, tmp_path
):
    # Post-publication review and repair re-authorize against the unit's OWN
    # open PR (the ordering the harness plane supports): --continuation-pr
    # names it, the check passes and records the tolerance in the signed
    # result — while any OTHER open PR, or a near-miss number, still refuses.
    body = VALID.format(deps="None")
    digest = cli.request_digest("Do the thing", body)
    argv = [
        "check-request",
        "--project",
        PROJECT,
        "--issue",
        "7",
        "--config",
        str(policy_file),
        "--expect-digest",
        digest,
        "--authorize-labels",
        "--output",
        str(tmp_path / "gate.json"),
    ]
    gh = HistoryGh(body)
    gh.prs = [dict(_PR)]
    monkeypatch.setattr(cli, "run_gh", gh)
    assert cli.main([*argv, "--continuation-pr", "https://x/pr/20"]) == 0
    result = json.loads((tmp_path / "gate.json").read_text())
    assert result["ready"] is True
    assert result["continuationPullRequest"] == "https://x/pr/20"

    # The bare number matches as a whole trailing segment only: 20 passes,
    # 2 can never excuse 20.
    def _gh_with(prs: list[dict]) -> HistoryGh:
        fresh = HistoryGh(body)
        fresh.prs = prs
        return fresh

    monkeypatch.setattr(cli, "run_gh", _gh_with([dict(_PR)]))
    assert cli.main([*argv, "--continuation-pr", "20"]) == 0
    monkeypatch.setattr(cli, "run_gh", _gh_with([dict(_PR)]))
    assert cli.main([*argv, "--continuation-pr", "2"]) == cli.PULL_REQUEST_OPEN
    # Another unit's PR beside the continuation still refuses, and only the
    # genuinely blocking PR is reported.
    other = {"number": 21, "url": "https://x/pr/21", "title": "o", "body": "Fixes #7"}
    monkeypatch.setattr(cli, "run_gh", _gh_with([dict(_PR), other]))
    assert cli.main([*argv, "--continuation-pr", "https://x/pr/20"]) == cli.PULL_REQUEST_OPEN
    result = json.loads((tmp_path / "gate.json").read_text())
    assert result["openPullRequests"] == ["https://x/pr/21"]
    # The flag is meaningless outside the authorization gate.
    plain = [entry for entry in argv if entry != "--authorize-labels"]
    assert cli.main([*plain, "--continuation-pr", "20"]) == 2


def test_final_gate_refuses_a_pr_opened_between_its_reads(monkeypatch, policy_file, capsys):
    body = VALID.format(deps="None")
    gh = HistoryGh(body)
    gh.on_pr_read[1] = lambda gh: gh.prs.append(dict(_PR))
    digest = cli.request_digest("Do the thing", body)
    assert _final_gate(monkeypatch, policy_file, gh, digest) == cli.PULL_REQUEST_OPEN == 9
    assert gh.pr_reads == 2
    err = capsys.readouterr().err
    assert "gained an open pull request" in err and "PR OPEN: #7" in err


def test_final_gate_reads_the_pr_inventory_twice_and_fails_closed_when_unreadable(
    monkeypatch, policy_file
):
    body = VALID.format(deps="None")
    digest = cli.request_digest("Do the thing", body)
    gh = HistoryGh(body)
    assert _final_gate(monkeypatch, policy_file, gh, digest) == 0
    assert gh.pr_reads == 2

    broken = HistoryGh(body)

    def fail(args, input=None):
        if "pullRequests" in " ".join(args):
            raise OnboardError("gh api failed: HTTP 502")
        return broken(args, input)

    assert _final_gate(monkeypatch, policy_file, fail, digest) == 2
