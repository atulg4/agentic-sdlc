"""The owner's three onboarding defaults (Codex review 5484921080):

A. the executable trust mode -- strict (root-owned git/gh/hook python) in agent contexts, local
   on a developer's own machine;
B. GitHub-hosted runners by default for the jobs that run model-produced patches, self-hosted
   only with an explicit ephemeral/containment acknowledgement (4243193108);
C. the ruleset's required approvals, defaulting from the repository's collaborators
   (4243193100)."""

from __future__ import annotations

import importlib.util
import json
import os
import tomllib
from pathlib import Path

import pytest
import yaml

from agentic_sdlc import onboard, trusted_exec
from agentic_sdlc.onboard import (
    APPROVALS_CHECK,
    RUNNER_ISOLATION_CHECK,
    TRUST_MODE_CHECK,
    OnboardError,
    OnboardSpec,
    Unreadable,
    approvals_check,
    default_required_approvals,
    doctor,
    hook_python_problem,
    other_writers,
    render_onboarding,
    ruleset_mismatches,
    ruleset_payload,
    runner_isolation_check,
    trust_mode_check,
    trusted_hook_python,
    write_onboarding,
)
from agentic_sdlc.trusted_exec import (
    LOCAL,
    STRICT,
    UntrustedExecutableError,
    candidate_reason,
    local_candidate_reason,
    resolve_trusted,
    trust_mode,
    use_trust_mode,
)

SHA = "e" * 40
NO_AGENT = {"PATH": "/usr/bin:/bin"}  # an environment with no agent-context marker


def spec(**overrides) -> OnboardSpec:
    base = dict(
        project_id="owner/comic",
        platform_repository="owner/agentic-sdlc",
        platform_ref=SHA,
        test_command="pytest -q",
    )
    base.update(overrides)
    return OnboardSpec(**base)


@pytest.fixture
def no_agent_context(monkeypatch):
    for name, _ in trusted_exec.AGENT_CONTEXT_MARKERS:
        monkeypatch.delenv(name, raising=False)


def _program(directory: Path, name: str, body: str = "exit 0", mode: int = 0o755) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(mode)
    return path


def _interpreter(directory: Path, name: str = "python3", version: str = "3 12") -> Path:
    """A program answering the hook-interpreter probe as a CPython `version` would."""
    return _program(directory, name, f'echo "$4" cpython {version}')


# ---------------------------------------------------------------- A: the trust mode


def test_the_trust_mode_is_strict_unless_the_policy_says_local_outside_agent_contexts():
    assert trust_mode(NO_AGENT) == STRICT  # no policy chose one
    use_trust_mode(LOCAL)
    assert trust_mode(NO_AGENT) == LOCAL
    for marker in ({"GITHUB_ACTIONS": "true"}, {"CLAUDE_CODE_REMOTE": "true"}):
        assert trust_mode({**NO_AGENT, **marker}) == STRICT  # agent contexts: always strict
    assert trust_mode({**NO_AGENT, "GITHUB_ACTIONS": "false"}) == LOCAL
    use_trust_mode(None)
    assert trust_mode(NO_AGENT) == STRICT
    with pytest.raises(ValueError):
        use_trust_mode("lenient")


def test_a_user_owned_program_passes_local_vetting_and_never_strict(tmp_path):
    mine = _program(tmp_path / "bin", "gh")
    assert local_candidate_reason(str(mine)) is None
    assert candidate_reason(str(mine)) is not None  # not root-owned, or under a writable tree
    shared = _program(tmp_path / "shared", "gh", mode=0o775)
    assert "writable by its group or by others" in local_candidate_reason(str(shared))
    assert local_candidate_reason("gh") is not None  # never a relative name
    other_owner = _program(tmp_path / "other", "gh")
    if os.geteuid() == 0:
        os.chown(other_owner, 12345, -1)
        assert "owned by neither root nor this user" in local_candidate_reason(str(other_owner))


def test_local_resolution_takes_the_first_match_on_path_and_refuses_an_unsafe_one(tmp_path):
    first = _program(tmp_path / "first", "gh")
    second = _program(tmp_path / "second", "gh")
    path = f"relative{os.pathsep}{first.parent}{os.pathsep}{second.parent}"
    use_trust_mode(LOCAL)
    env = {"PATH": path}
    assert resolve_trusted("gh", (), "UNSET_OVERRIDE", env) == os.path.realpath(first)
    first.chmod(0o777)  # the first match is unsafe: refused, never skipped for the second
    with pytest.raises(UntrustedExecutableError, match="found first"):
        resolve_trusted("gh", (), "UNSET_OVERRIDE", env)
    # the override is honoured in local mode (vetted the same way)
    assert resolve_trusted("gh", (), "OVERRIDE", {**env, "OVERRIDE": str(second)}) == (
        os.path.realpath(second)
    )
    # the same environment in an agent context is strict: the user-owned copies are refused
    with pytest.raises(UntrustedExecutableError):
        resolve_trusted("gh", (), "UNSET_OVERRIDE", {"PATH": str(second.parent), "CI": "1",
                                                    "GITHUB_ACTIONS": "true"})  # fmt: skip


def test_a_user_owned_hook_interpreter_is_accepted_in_local_and_refused_in_strict(
    tmp_path, monkeypatch
):
    python = _interpreter(tmp_path / "homebrew" / "bin")
    assert hook_python_problem(str(python), LOCAL) == ""
    assert hook_python_problem(str(python), STRICT) != ""
    with pytest.raises(OnboardError, match="--hook-python"):
        trusted_hook_python(str(python), STRICT)
    assert trusted_hook_python(str(python), LOCAL) == str(python)
    # local mode still probes: an old or non-CPython interpreter is refused
    old = _interpreter(tmp_path / "old", version="3 9")
    assert "3.11 or newer" in hook_python_problem(str(old), LOCAL)
    # local mode falls back to the first acceptable python3 on PATH (a Homebrew 3.12, say)
    monkeypatch.setattr(onboard, "HOOK_PYTHON_CANDIDATES", (str(tmp_path / "none"),))
    monkeypatch.setenv("PATH", f"{old.parent}{os.pathsep}{python.parent}")
    assert trusted_hook_python(None, LOCAL) == str(python)
    with pytest.raises(OnboardError, match="--trust-mode local"):
        trusted_hook_python(None, STRICT)  # strict never searches PATH
    # onboarding pins it (an absolute path) and records the mode
    repo = tmp_path / "consumer"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(OnboardError, match="hook interpreter"):
        write_onboarding(repo, spec(hook_python=str(python)))
    write_onboarding(repo, spec(hook_python=str(python), trust_mode=LOCAL))
    policy = tomllib.loads((repo / "agentic-sdlc.toml").read_text())
    assert policy["automation"]["trust_mode"] == LOCAL
    settings = json.loads((repo / ".claude/settings.json").read_text())
    assert str(python) in json.dumps(settings)
    assert onboard.hook_problems(repo / ".claude/settings.json", LOCAL) == []
    assert onboard.hook_problems(repo / ".claude/settings.json", STRICT) != []


def _guard(tmp_path: Path, mode: str):
    path = tmp_path / f"guard_{mode}.py"
    path.write_text(render_onboarding(spec(trust_mode=mode))[".claude/hooks/forge_commit_guard.py"])
    module_spec = importlib.util.spec_from_file_location(f"guard_{mode}", path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def test_the_commit_guard_carries_the_mode_onboard_rendered(
    tmp_path, monkeypatch, no_agent_context
):
    strict = _guard(tmp_path, STRICT)
    local = _guard(tmp_path, LOCAL)
    assert strict.TRUST_MODE == STRICT and local.TRUST_MODE == LOCAL
    assert strict._trust_mode() == STRICT and local._trust_mode() == LOCAL
    git = _program(tmp_path / "brew" / "bin", "git")
    monkeypatch.setenv("PATH", f"{git.parent}{os.pathsep}/usr/bin:/bin")
    monkeypatch.delenv("AGENTIC_SDLC_GIT", raising=False)
    monkeypatch.setattr(local, "_TRUSTED", {})
    assert local._trusted_program("git") == os.path.realpath(git)
    assert strict._trust_candidate_reason(str(git)) is not None
    # an agent context is strict whatever was rendered
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    assert local._trust_mode() == STRICT
    monkeypatch.setattr(local, "_TRUSTED", {})
    try:
        found = local._trusted_program("git")
    except local.UntrustedProgram:
        found = None
    assert found != os.path.realpath(git)
    # the session-start hook imports the guard: it shares the rendered mode
    files = render_onboarding(spec(trust_mode=LOCAL))
    assert 'TRUST_MODE = "local"' in files[".claude/hooks/forge_commit_guard.py"]


def test_doctor_reports_the_trust_mode_and_warns_in_local(monkeypatch, no_agent_context):
    check = trust_mode_check(LOCAL)
    assert check.ok and check.warning and "Strict mode" in check.detail
    assert "[WARN] executable trust mode" in onboard.DoctorReport([check]).render()
    assert "READY" in onboard.DoctorReport([check]).render().splitlines()[-1]
    strict = trust_mode_check(STRICT)
    assert strict.ok and not strict.warning and strict.name == TRUST_MODE_CHECK


def test_the_policy_records_the_three_choices_and_doctor_accepts_them(tmp_path):
    text = render_onboarding(
        spec(
            trust_mode=LOCAL,
            required_approvals=2,
            runs_on=("self-hosted", "linux"),
            ephemeral_runners=True,
        )  # fmt: skip
    )["agentic-sdlc.toml"]
    doc = tomllib.loads(text)
    assert doc["automation"]["trust_mode"] == LOCAL
    assert doc["automation"]["ephemeral_runners"] is True
    assert doc["policy"]["required_approvals"] == 2
    assert onboard.policy_value_problems(onboard.policy_doc_values(doc)) == []
    doc["automation"]["trust_mode"] = "lenient"
    doc["policy"]["required_approvals"] = 11
    problems = " ".join(onboard.policy_value_problems(onboard.policy_doc_values(doc)))
    assert "trust_mode must be one of strict, local" in problems
    assert "required_approvals must be an integer between 0 and 10" in problems
    # a policy written before the fields existed reads as what that onboard did
    for table, key in (("automation", "trust_mode"), ("automation", "ephemeral_runners"),
                       ("policy", "required_approvals")):  # fmt: skip
        doc[table].pop(key)
    values = onboard.policy_doc_values(doc)
    assert (values["trust mode"], values["ephemeral runners"], values["required approvals"]) == (
        STRICT,
        False,
        0,
    )


def test_cli_onboard_records_the_trust_mode_and_keeps_it_on_force(tmp_path, monkeypatch):
    from agentic_sdlc import cli

    python = _interpreter(tmp_path / "homebrew" / "bin")
    repo = tmp_path / "consumer"
    (repo / ".git").mkdir(parents=True)
    base = ["onboard", "--destination", str(repo), "--project-id", "owner/comic"]
    base += ["--platform-repository", "owner/agentic-sdlc", "--platform-ref", SHA]
    base += ["--test", "pytest -q", "--visibility", "private", "--hook-python", str(python)]
    assert cli.main(base) == 2  # strict refuses a user-owned interpreter
    assert cli.main([*base, "--trust-mode", "local"]) == 0
    assert tomllib.loads((repo / "agentic-sdlc.toml").read_text())["automation"]["trust_mode"] == (
        LOCAL
    )
    assert cli.main([*base, "--force"]) == 0  # kept from the installed policy
    assert tomllib.loads((repo / "agentic-sdlc.toml").read_text())["automation"]["trust_mode"] == (
        LOCAL
    )


def test_the_cli_reads_the_policy_trust_mode(tmp_path, monkeypatch, no_agent_context):
    from agentic_sdlc import cli

    policy = tmp_path / "agentic-sdlc.toml"
    policy.write_text('[automation]\ntrust_mode = "local"\n')
    seen = []
    monkeypatch.setattr(cli, "use_trust_mode", seen.append)
    cli.main(["check-request", "--project", "o/r", "--issue", "1", "--config", str(policy)])
    assert seen == [LOCAL]
    policy.write_text('[automation]\ntrust_mode = "anything-else"\n')
    cli.main(["check-request", "--project", "o/r", "--issue", "1", "--config", str(policy)])
    assert seen[-1] == STRICT


# ---------------------------------------------------------------- B: runners


def test_the_forge_jobs_default_to_a_github_hosted_runner():
    files = render_onboarding(spec())
    assert onboard.DEFAULT_RUNS_ON == ("ubuntu-latest",)
    ci = yaml.safe_load(files[".github/workflows/ci.yml"])
    assert ci["jobs"]["test"]["runs-on"] == ["ubuntu-latest"]
    for name in ("agent-implement.yml", "agent-auto-implement.yml"):
        doc = yaml.safe_load(files[f".github/workflows/{name}"])
        passed = [
            json.loads(job["with"]["runs_on"])
            for job in doc["jobs"].values()
            if isinstance(job.get("with"), dict) and "runs_on" in job["with"]
        ]
        assert passed and all(labels == ["ubuntu-latest"] for labels in passed), name
    assert tomllib.loads(files["agentic-sdlc.toml"])["automation"]["ephemeral_runners"] is False


@pytest.mark.parametrize(
    "labels",
    [
        {"runs_on": ("self-hosted", "linux", "x64")},
        {"ci_runs_on": ("self-hosted", "linux")},
        {"runs_on": ("linux", "x64")},  # a label set GitHub does not host is self-hosted
    ],
)
def test_self_hosted_labels_need_the_ephemeral_acknowledgement(labels):
    with pytest.raises(OnboardError, match="--ephemeral-runners"):
        spec(**labels)
    assert spec(**labels, ephemeral_runners=True)
    # the existing runner rules still apply with the acknowledgement
    with pytest.raises(OnboardError, match="Linux only"):
        spec(runs_on=("self-hosted", "macos"), ephemeral_runners=True)


def test_cli_onboard_refuses_self_hosted_without_the_acknowledgement(tmp_path):
    from agentic_sdlc import cli

    repo = tmp_path / "consumer"
    (repo / ".git").mkdir(parents=True)
    base = ["onboard", "--destination", str(repo), "--project-id", "owner/comic"]
    base += ["--platform-repository", "owner/agentic-sdlc", "--platform-ref", SHA]
    base += ["--test", "pytest -q", "--visibility", "private"]
    base += ["--runs-on", "self-hosted,linux,x64"]
    assert cli.main(base) == 2
    assert not (repo / "agentic-sdlc.toml").exists()
    assert cli.main([*base, "--ephemeral-runners"]) == 0
    assert tomllib.loads((repo / "agentic-sdlc.toml").read_text())["automation"][
        "ephemeral_runners"
    ]


def _workflows(repo: Path, labels: list[str]) -> None:
    flows = repo / ".github/workflows"
    flows.mkdir(parents=True, exist_ok=True)
    (flows / "ci.yml").write_text(
        yaml.safe_dump({"on": "pull_request", "jobs": {"test": {"runs-on": labels}}})
    )
    call = {"uses": "o/p/.github/workflows/reusable-implement.yml@x"}
    call["with"] = {"runs_on": json.dumps(labels)}
    (flows / "agent-implement.yml").write_text(yaml.safe_dump({"jobs": {"implement": call}}))


def test_doctor_warns_about_persistent_self_hosted_runners(tmp_path):
    _workflows(tmp_path, ["ubuntu-latest"])
    hosted = runner_isolation_check(tmp_path, {})
    assert hosted.ok and not hosted.warning and hosted.name == RUNNER_ISOLATION_CHECK
    _workflows(tmp_path, ["self-hosted", "linux", "x64"])
    warned = runner_isolation_check(tmp_path, {})
    assert warned.ok and warned.warning
    assert "ci.yml:test" in warned.detail and "agent-implement.yml:implement" in warned.detail
    assert "--ephemeral-runners" in warned.detail
    acknowledged = runner_isolation_check(tmp_path, {"automation": {"ephemeral_runners": True}})
    assert acknowledged.ok and not acknowledged.warning


# ---------------------------------------------------------------- C: required approvals


def test_the_ruleset_requires_the_policy_count_and_doctor_holds_it_to_that():
    assert _approvals(ruleset_payload(spec())) == 0
    payload = ruleset_payload(spec(required_approvals=1))
    assert _approvals(payload) == 1
    assert ruleset_mismatches(payload, spec(required_approvals=1)) == []
    weaker = ruleset_payload(spec())
    assert "must be at least 1" in " ".join(ruleset_mismatches(weaker, spec(required_approvals=1)))
    with pytest.raises(OnboardError, match="required_approvals"):
        spec(required_approvals=11)


def _approvals(payload: dict) -> int:
    (rule,) = [r for r in payload["rules"] if r["type"] == "pull_request"]
    return rule["parameters"]["required_approving_review_count"]


def _collaborators_gh(pages, me="owner", fail=False):
    calls = []

    def gh(args, input=None):
        calls.append(list(args))
        if args[:2] == ["api", "user"]:
            return json.dumps({"login": me})
        assert args[1] == "repos/owner/comic/collaborators?permission=push&per_page=100"
        assert "--paginate" in args and "--slurp" in args
        if fail:
            raise OnboardError("gh: HTTP 403")
        return json.dumps(pages)

    gh.calls = calls
    return gh


def test_other_writers_reads_every_page_and_skips_the_owner_me_and_bots():
    pages = [
        [{"login": "Owner", "type": "User"}, {"login": "alice", "type": "User"}],
        [{"login": "ci-bot", "type": "Bot"}, {"login": "me-too", "type": "User"}],
    ]
    assert other_writers("owner/comic", _collaborators_gh(pages), me="me-too") == ["alice"]
    assert isinstance(other_writers("owner/comic", _collaborators_gh([], fail=True)), Unreadable)


@pytest.mark.parametrize(
    ("pages", "fail", "count"),
    [
        ([[{"login": "owner", "type": "User"}, {"login": "bob", "type": "User"}]], False, 1),
        ([[{"login": "owner", "type": "User"}]], False, 0),  # a solo owner: 0, never 1
        ([], True, 0),  # unreadable: 0, said so
    ],
)
def test_the_default_required_approvals_follows_the_collaborators(pages, fail, count):
    found, why = default_required_approvals(
        "owner/comic", _collaborators_gh(pages, fail=fail), cloud=True
    )
    assert found == count
    assert ("could not be read" in why) is fail


# Codex 4243386518: in the Actions modes the Publisher App authors the PR, so the owner can
# approve it: the default is 1 whoever the collaborators are, and nothing is read for it.
@pytest.mark.parametrize(
    ("pages", "fail"),
    [([[{"login": "owner", "type": "User"}]], False), ([], True), ([], False)],
)
def test_actions_modes_default_to_one_approval(pages, fail):
    gh = _collaborators_gh(pages, fail=fail)
    found, why = default_required_approvals("owner/comic", gh, cloud=False)
    assert found == 1 and "Publisher App" in why
    assert gh.calls == []


def test_doctor_warns_loudly_when_no_approval_is_required_in_cloud_routine_mode():
    loud = approvals_check(0, cloud=True, writers=None)
    assert loud.ok and loud.warning and loud.name == APPROVALS_CHECK
    assert "human-merge boundary" in loud.detail and "not enforce" in loud.detail
    hinted = approvals_check(0, cloud=True, writers=["alice"])
    assert hinted.warning and "alice could approve" in hinted.detail
    # Actions modes: the owner can approve the App-authored PR, so 0 always warns with the fix
    solo = approvals_check(0, cloud=False, writers=[])
    assert (
        solo.warning and "Publisher App" in solo.detail and "--required-approvals 1" in solo.detail
    )
    assert approvals_check(0, cloud=False, writers=None).warning
    enforced = approvals_check(1, cloud=True, writers=None)
    assert enforced.ok and not enforced.warning


def test_cli_onboard_offline_uses_zero_and_warns(tmp_path, capsys):
    from agentic_sdlc import cli

    repo = tmp_path / "consumer"
    (repo / ".git").mkdir(parents=True)
    base = ["onboard", "--destination", str(repo), "--project-id", "owner/comic"]
    base += ["--platform-repository", "owner/agentic-sdlc", "--platform-ref", SHA]
    base += ["--test", "pytest -q", "--visibility", "private", "--implementer", "cloud-routine"]
    assert cli.main(base) == 0
    err = capsys.readouterr().err
    assert "--required-approvals 0" in err and "WARNING" in err and "cloud-routine" in err
    assert (
        tomllib.loads((repo / "agentic-sdlc.toml").read_text())["policy"]["required_approvals"] == 0
    )
    assert cli.main([*base, "--force", "--required-approvals", "1"]) == 0
    assert "WARNING" not in capsys.readouterr().err
    assert (
        tomllib.loads((repo / "agentic-sdlc.toml").read_text())["policy"]["required_approvals"] == 1
    )
    with pytest.raises(SystemExit):  # argparse: 0-10 only
        cli.main([*base, "--force", "--required-approvals", "11"])


@pytest.mark.parametrize("implementer", ["route", "claude", "codex"])
def test_cli_onboard_actions_modes_default_to_one_approval(tmp_path, capsys, implementer):
    from agentic_sdlc import cli

    repo = tmp_path / "consumer"
    (repo / ".git").mkdir(parents=True)
    base = ["onboard", "--destination", str(repo), "--project-id", "owner/comic"]
    base += ["--platform-repository", "owner/agentic-sdlc", "--platform-ref", SHA]
    base += ["--test", "pytest -q", "--visibility", "private", "--implementer", implementer]
    assert cli.main(base) == 0
    err = capsys.readouterr().err
    assert "--required-approvals 1" in err and "Publisher App" in err and "WARNING" not in err
    assert (
        tomllib.loads((repo / "agentic-sdlc.toml").read_text())["policy"]["required_approvals"] == 1
    )


def test_doctor_local_reports_the_three_choices(tmp_path, no_agent_context):
    repo = tmp_path / "consumer"
    (repo / ".git").mkdir(parents=True)
    write_onboarding(repo, spec(implementer="cloud-routine"))
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", remote=False)
    by_name = {c.name: c for c in report.checks}
    assert not by_name[TRUST_MODE_CHECK].warning
    assert not by_name[RUNNER_ISOLATION_CHECK].warning
    assert by_name[APPROVALS_CHECK].warning
    assert "[WARN] ruleset requires a human approval" in report.render()


def test_the_cli_main_never_needs_a_policy_to_pick_a_mode(tmp_path, monkeypatch):
    from agentic_sdlc import cli

    monkeypatch.chdir(tmp_path)
    assert cli._policy_trust_mode(type("A", (), {})()) is None
