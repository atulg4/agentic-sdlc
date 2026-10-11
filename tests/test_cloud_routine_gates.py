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
from agentic_sdlc.onboard import OnboardError

PROJECT = "example/project"  # the `policy_file` fixture's project id
LABELS = ["agent-ready", "human-review-required", "implementation-approved"]


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=repository, check=True, capture_output=True, text=True
    ).stdout.strip()


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
    return repository


def _commit(repository: Path, files: dict[str, str], message: str = "change") -> None:
    for name, text in files.items():
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repository, "add", "--", ".")
    _git(repository, "commit", "-qm", message)


def _check_diff(repository: Path, policy_file: Path, *extra: str) -> int:
    """`policy_file` is committed on main by `_repo`; check-diff reads it from there."""
    return cli.main(
        [
            "check-diff",
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
    argv = ["check-diff", "--repository", str(repository)]
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


def test_an_explicit_git_override_is_vetted_too(tmp_path, policy_file, monkeypatch, capsys):
    repository = _repo(tmp_path, policy_file)
    _commit(repository, {".github/workflows/x.yml": "on: push\n"})
    marker = _plant_git(tmp_path, monkeypatch)
    monkeypatch.setenv("AGENTIC_SDLC_GIT", str(tmp_path / "bin" / "git"))
    assert _check_diff(repository, policy_file) == 2
    assert "AGENTIC_SDLC_GIT" in capsys.readouterr().err
    assert not marker.exists()


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
    argv = ["check-diff", "--repository", str(repository), "--base", "main"]
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
    argv = ["check-diff", "--repository", str(repository), "--base", "main"]
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


def _plant_gh(tmp_path: Path, monkeypatch) -> Path:
    """A fabricating `gh` at the head of PATH that would authorize anything; its marker."""
    bin_dir = tmp_path / "ghbin"
    bin_dir.mkdir()
    marker = tmp_path / "planted-gh-ran"
    fake = bin_dir / "gh"
    fake.write_text(f'#!/bin/sh\ntouch "{marker}"\necho "{{}}"\n', encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return marker


def test_run_gh_never_runs_a_gh_planted_on_path(tmp_path, monkeypatch):
    from agentic_sdlc import onboard, trusted_exec

    marker = _plant_gh(tmp_path, monkeypatch)
    monkeypatch.setattr(trusted_exec, "SYSTEM_GH_PATHS", ())
    with pytest.raises(OnboardError, match="no trusted gh executable"):
        onboard.run_gh(["api", f"repos/{PROJECT}/issues/7"])
    assert not marker.exists()


def test_check_request_fails_closed_without_a_trusted_gh(
    tmp_path, policy_file, monkeypatch, capsys
):
    from agentic_sdlc import trusted_exec

    marker = _plant_gh(tmp_path, monkeypatch)
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
