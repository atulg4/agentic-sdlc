from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from agentic_sdlc.executors import load_executors, load_routing_policy
from agentic_sdlc.onboard import (
    IMPLEMENTATION_LABEL,
    RULESET_NAME,
    OnboardError,
    OnboardSpec,
    apply_repo_settings,
    copy_variables,
    doctor,
    render_onboarding,
    repository_default_branch,
    resolve_platform_ref,
    ruleset_mismatches,
    ruleset_payload,
    write_onboarding,
)
from agentic_sdlc.policy import load_policy
from agentic_sdlc.task_spec import check_task_spec

SHA = "e" * 40


def spec(**overrides) -> OnboardSpec:
    base = dict(
        project_id="owner/comic",
        platform_repository="owner/agentic-sdlc",
        platform_ref=SHA,
        test_command="pytest tests -q -m 'not e2e'",
        forbidden_paths=("data/**", "*.png"),
        protected_paths=("comicmaestro/app.py",),
    )
    base.update(overrides)
    return OnboardSpec(**base)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "consumer"
    (repo / ".git").mkdir(parents=True)
    return repo


class FakeGh:
    """Records gh invocations and answers from a canned table keyed by the first few args."""

    def __init__(self, answers: dict[str, str] | None = None, fail: set[str] | None = None):
        self.calls: list[tuple[tuple[str, ...], str | None]] = []
        self.answers = answers or {}
        self.fail = fail or set()

    def __call__(self, args, input=None):
        self.calls.append((tuple(args), input))
        key = " ".join(args)
        for prefix, value in self.answers.items():
            if key.startswith(prefix):
                return value
        for prefix in self.fail:
            if key.startswith(prefix):
                raise OnboardError("boom")
        return ""


# ---------------------------------------------------------------- rendering


def test_renders_every_file_with_placeholders_resolved():
    files = render_onboarding(spec())
    assert {
        ".github/workflows/agent-plan.yml",
        ".github/workflows/agent-implement.yml",
        ".github/workflows/agent-auto-implement.yml",
        ".github/workflows/agent-auto-plan.yml",
        ".github/workflows/ci.yml",
        "agentic-sdlc.toml",
        "AGENTS.md",
        "CLAUDE.md",
        ".github/ISSUE_TEMPLATE/agent-work-request.md",
        ".forge/executors.json",
        ".forge/routing-policy.json",
    } <= set(files)
    for name, content in files.items():
        for token in (
            "PLATFORM_REPOSITORY",
            "PLATFORM_COMMIT_SHA",
            "RUNS_ON_",
            "_COMMAND",
            "IMPLEMENT_JOBS",
            "PREFLIGHT_EXTRA",
        ):
            assert token not in content, (name, token)
        if name.endswith(".yml"):
            yaml.safe_load(content)  # every workflow must be valid YAML


def test_workflows_use_self_hosted_runners_and_the_pinned_platform():
    files = render_onboarding(spec())
    plan = files[".github/workflows/agent-plan.yml"]
    assert f"owner/agentic-sdlc/.github/workflows/reusable-plan.yml@{SHA}" in plan
    assert 'runs_on: \'["self-hosted","linux","x64"]\'' in plan
    assert "runs-on: [self-hosted, linux, x64]" in plan
    assert (
        "CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}" in plan
    )  # the secret plan needs
    assert "OPENAI_API_KEY" not in plan


def test_auto_implement_requires_all_three_labels_and_uses_the_event_issue():
    auto = render_onboarding(spec())[".github/workflows/agent-auto-implement.yml"]
    doc = yaml.safe_load(auto)
    cond = doc["jobs"]["preflight"]["if"]
    for label in ("claude-ready", "human-review-required", IMPLEMENTATION_LABEL):
        assert f"contains(github.event.issue.labels.*.name, '{label}')" in cond
    assert (
        doc["jobs"]["implement"]["with"]["issue_number"]
        == "${{ format('{0}', github.event.issue.number) }}"
    )
    assert doc["jobs"]["implement"]["with"]["agent"] == "route"
    assert doc["jobs"]["implement"]["with"]["executor_registry_path"] == ".forge/executors.json"
    assert doc["jobs"]["implement"]["needs"] == "preflight"


def test_claude_implementer_has_no_routing_files_or_deepseek_preflight():
    files = render_onboarding(spec(implementer="claude"))
    assert ".forge/executors.json" not in files
    impl = files[".github/workflows/agent-implement.yml"]
    doc = yaml.safe_load(impl)
    assert doc["jobs"]["implement"]["with"]["agent"] == "claude"
    assert doc["jobs"]["implement"]["with"]["executor_registry_path"] == ""
    assert 'test -n "$DEEPSEEK_API_KEY"' not in impl
    assert 'test -n "$CLAUDE_CODE_OAUTH_TOKEN"' in impl
    assert "[routing]" not in files["agentic-sdlc.toml"]


def test_policy_loads_and_carries_commands_and_paths(tmp_path):
    files = render_onboarding(spec())
    path = tmp_path / "agentic-sdlc.toml"
    path.write_text(files["agentic-sdlc.toml"])
    policy = load_policy(path)
    assert policy.ready_label == "claude-ready"
    toml = files["agentic-sdlc.toml"]
    assert '"data/**"' in toml and '"*.png"' in toml and '".env*"' in toml
    assert '"comicmaestro/app.py"' in toml
    assert "test = \"pytest tests -q -m 'not e2e'\"" in toml
    assert 'gates = ["setup", "quality", "test"]' in toml


def test_routing_files_validate_and_permit_only_this_repo():
    files = render_onboarding(spec())
    executors = load_executors(json.loads(files[".forge/executors.json"]))
    assert executors and all(e.permitted_repositories == ("owner/comic",) for e in executors)
    load_routing_policy(json.loads(files[".forge/routing-policy.json"]))
    assert (
        json.loads(files[".forge/routing-policy.json"])["policyVersion"] == "comic-multiprovider-v1"
    )


def test_issue_template_passes_the_spec_gate_once_filled():
    template = render_onboarding(spec())[".github/ISSUE_TEMPLATE/agent-work-request.md"]
    for heading in (
        "## Summary",
        "## Acceptance Criteria",
        "## Required Tests",
        "## Non-Goals",
        "## Dependencies",
    ):
        assert heading in template
    body = (
        "## Summary\nDo the thing.\n\n## Acceptance Criteria\n- it works\n\n"
        "## Required Tests\n- test_it\n\n"
        "## Non-Goals\n- nothing else\n\n## Dependencies\nNone\n"
    )
    assert check_task_spec("Do the thing", body).ready


def test_ci_workflow_runs_the_three_commands_under_the_test_job():
    doc = yaml.safe_load(render_onboarding(spec())[".github/workflows/ci.yml"])
    steps = {s["name"]: s.get("run") for s in doc["jobs"]["test"]["steps"] if "name" in s}
    assert steps["Test"] == "pytest tests -q -m 'not e2e'"
    assert steps["Setup"].startswith("python -m pip install")
    assert doc["jobs"]["test"]["runs-on"] == ["self-hosted", "linux", "x64"]


def test_spec_validation_fails_closed():
    with pytest.raises(OnboardError):
        spec(platform_ref="main")
    with pytest.raises(OnboardError):
        spec(test_command="  ")
    with pytest.raises(OnboardError):
        spec(implementer="gemini")
    with pytest.raises(OnboardError):
        spec(runs_on=("self hosted",))


def test_write_refuses_to_overwrite_unless_forced(tmp_path):
    repo = _repo(tmp_path)
    (repo / "AGENTS.md").write_text("owner content\n")
    with pytest.raises(OnboardError, match="AGENTS.md"):
        write_onboarding(repo, spec())
    written = write_onboarding(repo, spec(), force=True)
    assert repo / ".github/workflows/agent-plan.yml" in written
    assert (repo / "AGENTS.md").read_text() != "owner content\n"


# ---------------------------------------------------------------- gh-backed pieces


def test_resolve_platform_ref_accepts_sha_and_resolves_branches():
    gh = FakeGh({"api repos/owner/agentic-sdlc/commits/main": SHA + "\n"})
    assert resolve_platform_ref("owner/agentic-sdlc", SHA, gh) == SHA and gh.calls == []
    assert resolve_platform_ref("owner/agentic-sdlc", "main", gh) == SHA
    with pytest.raises(OnboardError):
        resolve_platform_ref("owner/agentic-sdlc", "nope", FakeGh({"api": "not-a-sha"}))


def test_apply_repo_settings_creates_labels_ruleset_and_variables_idempotently():
    gh = FakeGh({"api repos/owner/comic/rulesets": "[]"})
    log = apply_repo_settings(spec(), gh, variables={"PUBLISHER_APP_CLIENT_ID": "Iv1"})
    label_calls = [c for c, _ in gh.calls if c[:2] == ("label", "create")]
    assert {c[2] for c in label_calls} >= {
        "claude-ready",
        "human-review-required",
        IMPLEMENTATION_LABEL,
    }
    assert all("--force" in c for c in label_calls)
    post = next((c, i) for c, i in gh.calls if c[:3] == ("api", "-X", "POST"))
    payload = json.loads(post[1])
    assert payload["name"] == RULESET_NAME and payload["bypass_actors"] == []
    rules = {r["type"]: r for r in payload["rules"]}
    assert rules["pull_request"]["parameters"]["required_review_thread_resolution"] is True
    assert rules["required_status_checks"]["parameters"]["required_status_checks"] == [
        {"context": "test"}
    ]
    assert (
        "variable",
        "set",
        "PUBLISHER_APP_CLIENT_ID",
        "--repo",
        "owner/comic",
        "--body",
        "Iv1",
    ) in [c for c, _ in gh.calls]
    assert any("created" in line for line in log)

    gh2 = FakeGh(
        {
            "api repos/owner/comic/rulesets/5": json.dumps({"id": 5, **ruleset_payload(spec())}),
            "api repos/owner/comic/rulesets": json.dumps([{"id": 5, "name": RULESET_NAME}]),
        }
    )
    log2 = apply_repo_settings(spec(), gh2)
    assert not any(c[:3] == ("api", "-X", "POST") for c, _ in gh2.calls)
    assert any("already present" in line for line in log2)


def test_ruleset_payload_matches_the_protect_main_shape():
    p = ruleset_payload(spec())
    assert p["conditions"]["ref_name"]["include"] == ["~DEFAULT_BRANCH"]
    assert {r["type"] for r in p["rules"]} == {
        "deletion",
        "non_fast_forward",
        "pull_request",
        "required_status_checks",
    }


def test_copy_variables_skips_missing_ones():
    gh = FakeGh(
        {"variable get DEEPSEEK_MODEL_FLASH": "deepseek-v4-flash\n"},
        fail={"variable get DEEPSEEK_MODEL_PRO"},
    )
    assert copy_variables(
        "owner/music",
        ["DEEPSEEK_MODEL_FLASH", "DEEPSEEK_MODEL_PRO", "ZAI_MODEL_GLM", "KIMI_MODEL_K3"],
        gh,
    ) == {"DEEPSEEK_MODEL_FLASH": "deepseek-v4-flash"}


# ---------------------------------------------------------------- doctor


def _secret_pages(*names: str, per_page: int = 30) -> str:
    """`gh api .../actions/secrets --paginate --slurp`: an array of {"secrets": [...]} pages."""
    rows = [{"name": n} for n in names]
    pages = [rows[i : i + per_page] for i in range(0, len(rows), per_page)] or [[]]
    return json.dumps([{"total_count": len(rows), "secrets": page} for page in pages])


PUBLISHER_PERMS = {"contents": "read", "issues": "write", "pull_requests": "write"}


def _pages(key: str, rows: list, per_page: int = 30) -> str:
    """`gh api <list endpoint> --paginate --slurp`: an array of {key: [...]} pages."""
    chunks = [rows[i : i + per_page] for i in range(0, len(rows), per_page)] or [[]]
    return json.dumps([{"total_count": len(rows), key: chunk} for chunk in chunks])


def _installs(*ids: int, permissions: dict | None = None) -> str:
    rows = [
        {
            "id": i,
            "app_slug": "agentic-sdlc-publisher",
            "permissions": permissions or PUBLISHER_PERMS,
        }
        for i in ids
    ]
    return _pages("installations", rows)


def _var_pages(*names: str, per_page: int = 30) -> str:
    """`gh api .../actions/variables --paginate --slurp`: an array of {"variables": [...]}."""
    rows = [{"name": n, "value": f"{n.lower()}-value"} for n in names]
    pages = [rows[i : i + per_page] for i in range(0, len(rows), per_page)] or [[]]
    return json.dumps([{"total_count": len(rows), "variables": page} for page in pages])


def _healthy_gh() -> FakeGh:
    return FakeGh(
        {
            "api repos/owner/agentic-sdlc/contents": '{"path": "reusable-implement.yml"}',
            "api repos/owner/comic/rulesets/11": json.dumps({"id": 11, **ruleset_payload(spec())}),
            "label list": json.dumps(
                [
                    {"name": n}
                    for n in (
                        "claude-ready",
                        "human-review-required",
                        IMPLEMENTATION_LABEL,
                        "in-progress",
                    )
                ]
            ),
            "api repos/owner/comic/rulesets": json.dumps(
                [{"id": 11, "name": RULESET_NAME, "enforcement": "active"}]
            ),
            # the generated registry routes to deepseek, zai and kimi: every fallback configured
            "api repos/owner/comic/actions/variables": _var_pages(
                "PUBLISHER_APP_CLIENT_ID",
                "DEEPSEEK_MODEL_FLASH",
                "DEEPSEEK_MODEL_PRO",
                "ZAI_MODEL_GLM",
                "KIMI_MODEL_K3",
            ),
            "api repos/owner/comic/actions/secrets": _secret_pages(
                "PUBLISHER_APP_PRIVATE_KEY",
                "CLAUDE_CODE_OAUTH_TOKEN",
                "DEEPSEEK_API_KEY",
                "ZAI_API_KEY",
                "KIMI_API_KEY",
            ),
            "api repos/owner/comic/actions/runners": _pages(
                "runners",
                [
                    {
                        "status": "online",
                        "labels": [{"name": n} for n in ("self-hosted", "linux", "x64")],
                    }
                ],
            ),
            "api /user/installations/": json.dumps(
                [
                    {"repositories": [{"full_name": "owner/comic"}]},
                    {"repositories": [{"full_name": "owner/music"}]},
                ]
            ),
            "api repos/owner/comic": json.dumps({"default_branch": "main"}),
            "api /user/installations": _installs(7),
        }
    )


def test_doctor_is_green_for_a_fully_configured_repo(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", _healthy_gh())
    assert report.ok, report.render()
    assert "READY" in report.render()


def test_doctor_flags_missing_secrets_runner_and_app_as_manual_todos(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _healthy_gh()
    gh.answers["api repos/owner/comic/actions/secrets"] = _secret_pages()
    gh.answers["api repos/owner/comic/actions/runners"] = _pages("runners", [])
    gh.answers["api /user/installations/"] = json.dumps(
        [{"repositories": [{"full_name": "owner/music"}]}]
    )
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    assert not report.ok
    failed = {c.name: c for c in report.checks if not c.ok}
    assert set(failed) == {
        "ci.yml test job runs on an available runner",
        "repo secrets set (by name)",
        "self-hosted runner online for this repo",
        "Publisher GitHub App installed on this repo",
    }
    assert all(c.manual for c in failed.values())
    assert "gh secret set" in failed["repo secrets set (by name)"].detail
    assert "registration-token" in failed["self-hosted runner online for this repo"].detail
    assert "[TODO]" in report.render()


def test_doctor_local_only_catches_label_mismatch_and_unpinned_platform(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    toml = repo / "agentic-sdlc.toml"
    toml.write_text(
        toml.read_text().replace('ready_label = "claude-ready"', 'ready_label = "agent-ready"')
    )
    plan = repo / ".github/workflows/agent-plan.yml"
    plan.write_text(plan.read_text().replace(SHA, "main"))
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False)
    names = {c.name for c in report.checks if not c.ok}
    assert "workflows pin the platform to a commit SHA" in names
    assert "workflow label conditions match the policy labels" in names


def test_cloud_routine_mode_skips_actions_implementer_and_documents_the_routine(tmp_path):
    files = render_onboarding(spec(implementer="cloud-routine", runs_on=("ubuntu-latest",)))
    assert ".github/workflows/agent-implement.yml" not in files
    assert ".github/workflows/agent-auto-implement.yml" not in files
    assert ".forge/executors.json" not in files
    assert ".github/workflows/agent-plan.yml" in files and ".github/workflows/ci.yml" in files
    routine = files["docs/forge/cloud-implementer.md"]
    assert "implementation-approved" in routine and "pytest tests -q -m 'not e2e'" in routine
    assert 'implementer = "claude"' in files["agentic-sdlc.toml"]
    assert yaml.safe_load(files[".github/workflows/ci.yml"])["jobs"]["test"]["runs-on"] == [
        "ubuntu-latest"
    ]


def test_doctor_in_cloud_routine_mode_needs_no_runner_app_or_publisher_secret(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec(implementer="cloud-routine", runs_on=("ubuntu-latest",)))
    gh = _healthy_gh()
    gh.answers["api repos/owner/comic/actions/secrets"] = _secret_pages("CLAUDE_CODE_OAUTH_TOKEN")
    gh.answers["api repos/owner/comic/actions/variables"] = _var_pages()
    gh.answers["api repos/owner/comic/actions/runners"] = _pages("runners", [])
    gh.answers["api /user/installations"] = _installs()
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    assert report.ok, report.render()


def test_cli_onboard_and_doctor_local(tmp_path, monkeypatch, capsys):
    from agentic_sdlc.cli import main

    repo = _repo(tmp_path)
    out = tmp_path / "out.json"
    code = main(
        [
            "onboard",
            "--destination",
            str(repo),
            "--project-id",
            "owner/comic",
            "--platform-repository",
            "owner/agentic-sdlc",
            "--platform-ref",
            SHA,
            "--test",
            "pytest -q",
            "--implementer",
            "cloud-routine",
            "--runs-on",
            "ubuntu-latest",
            "--forbidden",
            "data/**",
            "--output",
            str(out),
        ]
    )
    assert code == 0
    result = json.loads(out.read_text())
    assert result["platform_ref"] == SHA and "agentic-sdlc.toml" in result["written"]
    assert (repo / "docs/forge/cloud-implementer.md").exists()
    assert main(["doctor", "--destination", str(repo), "--local"]) == 0
    assert "READY" in capsys.readouterr().out
    # second run without --force refuses to clobber
    assert (
        main(
            [
                "onboard",
                "--destination",
                str(repo),
                "--project-id",
                "owner/comic",
                "--platform-repository",
                "owner/agentic-sdlc",
                "--platform-ref",
                SHA,
                "--test",
                "pytest -q",
            ]
        )
        == 2
    )
    assert "refusing to overwrite" in capsys.readouterr().err


def test_onboarding_installs_claim_first_rule_and_claude_code_hooks(tmp_path):
    files = render_onboarding(spec())
    settings = json.loads(files[".claude/settings.json"])
    assert settings["hooks"]["PreToolUse"][0]["matcher"] == "Bash"
    assert "forge_commit_guard.py" in settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert "forge_session_start.py" in settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    guard = files[".claude/hooks/forge_commit_guard.py"]
    assert 'PROJECT = "owner/comic"' in guard and "PROJECT_ID" not in guard
    start = files[".claude/hooks/forge_session_start.py"]
    assert 'DEFAULT_BRANCH = "main"' in start
    assert "Claim before you code" in files["AGENTS.md"]
    assert "sdlcctl claim" in files["CLAUDE.md"]
    assert "lease_ttl_minutes = 240" in files["agentic-sdlc.toml"]
    routine = render_onboarding(spec(implementer="cloud-routine"))[
        "docs/forge/cloud-implementer.md"
    ]
    assert "sdlcctl claim" in routine and "sdlcctl release" in routine


def test_commit_guard_blocks_only_unleased_issue_branch_commits(tmp_path):
    import importlib.util

    path = tmp_path / "guard.py"
    path.write_text(render_onboarding(spec())[".claude/hooks/forge_commit_guard.py"])
    module_spec = importlib.util.spec_from_file_location("guard", path)
    guard = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(guard)
    from datetime import UTC, datetime, timedelta

    live_mine = {
        "agent": "claude-code",
        "session": "s1",
        "expires": datetime.now(UTC) + timedelta(hours=1),
    }
    live_other = {
        "agent": "cloud-routine",
        "session": "r9",
        "expires": datetime.now(UTC) + timedelta(hours=1),
    }
    commit = {"tool_name": "Bash", "session_id": "s1", "tool_input": {"command": "git commit -m x"}}

    assert guard.decide(commit, "main", lambda n: None) == (0, "")  # not an issue branch
    assert (
        guard.decide(
            {**commit, "tool_input": {"command": "git status"}}, "forge/issue-7", lambda n: None
        )[0]
        == 0
    )
    assert guard.decide(commit, "forge/issue-7", lambda n: live_mine)[0] == 0  # my lease
    code, msg = guard.decide(commit, "forge/issue-7", lambda n: None)
    assert code == 2 and "sdlcctl claim" in msg and "--issue 7" in msg  # no lease
    code, msg = guard.decide(commit, "claude/issue-7-thing", lambda n: live_other)
    assert code == 2 and "r9" in msg  # someone else's
    assert guard.decide({**commit, "tool_name": "Edit"}, "forge/issue-7", lambda n: None)[0] == 0


# ---------------------------------------------------------------- review follow-ups


def test_commands_with_quotes_and_backslashes_render_a_loadable_policy(tmp_path):
    test = 'pytest -k "happy path" tests\\unit'
    repo = _repo(tmp_path)
    write_onboarding(repo, spec(test_command=test, forbidden_paths=('data/"x"/**',)))
    import tomllib

    doc = tomllib.loads((repo / "agentic-sdlc.toml").read_text())
    assert doc["commands"]["test"] == test
    assert 'data/"x"/**' in doc["policy"]["forbidden_paths"]
    load_policy(repo / "agentic-sdlc.toml")


def test_codex_implementer_forwards_and_requires_openai_key(tmp_path):
    files = render_onboarding(spec(implementer="codex"))
    implement = yaml.safe_load(files[".github/workflows/agent-implement.yml"])
    assert implement["jobs"]["implement"]["secrets"]["OPENAI_API_KEY"] == (
        "${{ secrets.OPENAI_API_KEY }}"
    )
    assert 'test -n "$OPENAI_API_KEY"' in files[".github/workflows/agent-implement.yml"]
    repo = _repo(tmp_path)
    write_onboarding(repo, spec(implementer="codex"))
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", _healthy_gh())
    secrets = next(c for c in report.checks if c.name == "repo secrets set (by name)")
    assert not secrets.ok and "OPENAI_API_KEY" in secrets.detail


def test_issue_template_carries_the_profile_ready_label():
    template = render_onboarding(spec(ready_label="forge-ready"))[
        ".github/ISSUE_TEMPLATE/agent-work-request.md"
    ]
    assert "labels: forge-ready, human-review-required" in template
    assert "agent-ready" not in template


def test_forced_mode_switch_removes_the_previous_modes_files(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    assert (repo / ".github/workflows/agent-auto-implement.yml").exists()
    write_onboarding(repo, spec(implementer="cloud-routine"), force=True)
    assert not (repo / ".github/workflows/agent-auto-implement.yml").exists()
    assert not (repo / ".github/workflows/agent-implement.yml").exists()
    assert not (repo / ".forge/executors.json").exists()
    write_onboarding(repo, spec(implementer="claude"), force=True)
    assert not (repo / "docs/forge/cloud-implementer.md").exists()
    assert (repo / ".github/workflows/agent-auto-implement.yml").exists()


def test_incomplete_same_name_ruleset_is_updated_and_fails_doctor(tmp_path):
    stale = {"id": 5, "name": RULESET_NAME, "target": "branch", "enforcement": "active"}
    stale["conditions"] = {"ref_name": {"include": ["~DEFAULT_BRANCH"]}}
    stale["rules"] = [{"type": "deletion"}]
    assert "missing rule required_status_checks" in ruleset_mismatches(stale, spec())
    gh = FakeGh(
        {
            "api repos/owner/comic/rulesets/5": json.dumps(stale),
            "api repos/owner/comic/rulesets": json.dumps([stale]),
        }
    )
    log = apply_repo_settings(spec(), gh)
    put = [(c, i) for c, i in gh.calls if c[:3] == ("api", "-X", "PUT")]
    assert put and put[0][0][3] == "repos/owner/comic/rulesets/5"
    assert json.loads(put[0][1]) == ruleset_payload(spec())
    assert any("updated" in line for line in log)

    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    healthy = _healthy_gh()
    healthy.answers["api repos/owner/comic/rulesets/11"] = json.dumps(stale)
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", healthy)
    failed = {c.name for c in report.checks if not c.ok}
    assert failed == {f"ruleset '{RULESET_NAME}' active on default branch"}


def test_doctor_probes_the_plan_workflow_and_implement_only_when_installed(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec(implementer="cloud-routine", runs_on=("ubuntu-latest",)))
    gh = _healthy_gh()
    doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    probes = [c[1] for c, _ in gh.calls if c[1].startswith("repos/owner/agentic-sdlc/contents")]
    assert len(probes) == 1 and "reusable-plan.yml" in probes[0]

    repo2 = _repo(tmp_path / "two")
    write_onboarding(repo2, spec())
    gh2 = _healthy_gh()
    del gh2.answers["api repos/owner/agentic-sdlc/contents"]
    gh2.answers["api repos/owner/agentic-sdlc/contents/.github/workflows/reusable-plan.yml"] = (
        '{"path": "x"}'
    )
    report = doctor(repo2, "owner/comic", "owner/agentic-sdlc", gh2)
    reach = next(c for c in report.checks if c.name.startswith("platform ref reachable"))
    assert not reach.ok and "reusable-implement.yml" in reach.detail


def test_doctor_reads_every_installation_page(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _healthy_gh()
    gh.answers["api /user/installations/"] = json.dumps(
        [
            {"repositories": [{"full_name": "owner/music"}]},
            {"repositories": [{"full_name": "owner/comic"}]},
        ]
    )
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    assert report.ok, report.render()
    call = next(c for c, _ in gh.calls if c[1].startswith("/user/installations/"))
    assert "--slurp" in call and "--paginate" in call and "--jq" not in call


def test_doctor_flags_a_policy_branch_that_is_not_the_repo_default(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _healthy_gh()
    gh.answers["api repos/owner/comic"] = json.dumps({"default_branch": "master"})
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    failed = {c.name: c for c in report.checks if not c.ok}
    assert set(failed) == {"policy default_branch matches the repository"}
    assert "master" in failed["policy default_branch matches the repository"].detail


def test_repository_default_branch_reads_github():
    gh = FakeGh({"api repos/owner/comic --jq .default_branch": "master\n"})
    assert repository_default_branch("owner/comic", gh) == "master"
    with pytest.raises(OnboardError):
        repository_default_branch("owner/comic", FakeGh())


def test_doctor_requires_a_runner_carrying_every_workflow_label(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _healthy_gh()
    gh.answers["api repos/owner/comic/actions/runners"] = _pages(
        "runners",
        [{"status": "online", "labels": [{"name": n} for n in ("self-hosted", "linux", "ARM64")]}],
    )
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    failed = {c.name for c in report.checks if not c.ok}
    assert failed == {
        "self-hosted runner online for this repo",
        "ci.yml test job runs on an available runner",  # ci.yml targets the same labels
    }


def test_doctor_requires_every_caller_to_pin_the_same_platform_sha(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    auto = repo / ".github/workflows/agent-auto-implement.yml"
    auto.write_text(
        auto.read_text().replace(f"reusable-implement.yml@{SHA}", "reusable-implement.yml@main")
    )
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False)
    pin = next(c for c in report.checks if c.name == "workflows pin the platform to a commit SHA")
    assert not pin.ok and "agent-auto-implement.yml" in pin.detail


def test_workflow_commands_with_yaml_significant_text_stay_valid_yaml():
    test = "python -c \"print('key: value')\" # not a comment"
    setup = "- echo {a: [b]} & echo *star"
    doc = yaml.safe_load(
        render_onboarding(spec(test_command=test, setup_command=setup))[".github/workflows/ci.yml"]
    )
    steps = {s["name"]: s.get("run") for s in doc["jobs"]["test"]["steps"] if "name" in s}
    assert steps["Test"] == test and steps["Setup"] == setup


def test_ruleset_without_strict_status_checks_is_a_mismatch():
    lax = ruleset_payload(spec())
    lax["rules"][-1]["parameters"]["strict_required_status_checks_policy"] = False
    assert "status checks are not strict (branch must be up to date)" in ruleset_mismatches(
        lax, spec()
    )
    assert ruleset_mismatches(ruleset_payload(spec()), spec()) == []


def test_doctor_requires_the_auto_plan_workflow(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    (repo / ".github/workflows/agent-auto-plan.yml").unlink()
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", _healthy_gh())
    files = next(c for c in report.checks if c.name == "required files present")
    assert not report.ok and not files.ok and "agent-auto-plan.yml" in files.detail


def test_doctor_flags_a_policy_project_id_for_another_repository(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec(project_id="owner/other"))
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False)
    check = next(c for c in report.checks if c.name == "policy project id matches the repository")
    assert not check.ok and "owner/other" in check.detail and not report.ok


def test_doctor_reads_every_page_of_repository_secrets(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _healthy_gh()
    filler = [f"SECRET_{i:02d}" for i in range(30)]
    gh.answers["api repos/owner/comic/actions/secrets"] = _secret_pages(
        *filler,
        "PUBLISHER_APP_PRIVATE_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "DEEPSEEK_API_KEY",
        "ZAI_API_KEY",
        "KIMI_API_KEY",
    )
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    assert report.ok, report.render()
    call = next(c for c, _ in gh.calls if c[1].startswith("repos/owner/comic/actions/secrets"))
    assert "--slurp" in call and "--paginate" in call


def test_commit_guard_reads_paginated_comments_and_ignores_untrusted_markers(tmp_path, monkeypatch):
    import importlib.util
    import subprocess
    from datetime import UTC, datetime, timedelta

    path = tmp_path / "guard.py"
    path.write_text(render_onboarding(spec())[".claude/hooks/forge_commit_guard.py"])
    module_spec = importlib.util.spec_from_file_location("guard_pages", path)
    guard = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(guard)
    expires = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def row(body, association="OWNER"):
        return {"body": body, "author_association": association, "user": {"type": "User"}}

    mine = row(f"<!-- forge-claim agent=a session=s1 branch=b expires={expires} -->")
    forged = row("<!-- forge-release session=s1 -->", association="NONE")
    pages = [[row("chatter")] * 30, [mine, forged]]

    def fake_run(args, **kwargs):
        out = json.dumps(pages) if "--slurp" in args else "".join(json.dumps(p) for p in pages)
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")

    monkeypatch.setattr(guard.subprocess, "run", fake_run)
    lease = guard.lease_for(7)
    assert lease is not None and lease["session"] == "s1"


def test_ruleset_excluding_the_default_branch_is_a_mismatch():
    excluding = ruleset_payload(spec())
    excluding["conditions"]["ref_name"]["exclude"] = ["refs/heads/main"]
    assert "excludes refs/heads/main" in ruleset_mismatches(excluding, spec())


def test_doctor_reads_every_page_of_repository_variables(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _healthy_gh()
    filler = [f"VAR_{i:02d}" for i in range(30)]
    gh.answers["api repos/owner/comic/actions/variables"] = _var_pages(
        *filler,
        "PUBLISHER_APP_CLIENT_ID",
        "DEEPSEEK_MODEL_FLASH",
        "DEEPSEEK_MODEL_PRO",
        "ZAI_MODEL_GLM",
        "KIMI_MODEL_K3",
    )
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    assert report.ok, report.render()
    call = next(c for c, _ in gh.calls if c[1].startswith("repos/owner/comic/actions/variables"))
    assert "--slurp" in call and "--paginate" in call


def test_doctor_checks_every_matching_publisher_installation(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _healthy_gh()
    del gh.answers["api /user/installations/"]
    gh.answers = {
        "api /user/installations/7/": json.dumps([{"repositories": [{"full_name": "a/x"}]}]),
        "api /user/installations/8/": json.dumps(
            [{"repositories": [{"full_name": "owner/comic"}]}]
        ),
        **gh.answers,
    }
    gh.answers["api /user/installations"] = _installs(7, 8)
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    assert report.ok, report.render()


def test_doctor_requires_routing_files_when_the_repo_is_routed(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())  # default profile routes
    (repo / ".forge/executors.json").unlink()
    (repo / ".forge/routing-policy.json").unlink()
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False)
    check = next(c for c in report.checks if c.name == "routing files valid and permit this repo")
    assert (
        not check.ok and "executors.json" in check.detail and "routing-policy.json" in check.detail
    )


def test_doctor_requires_the_in_progress_lease_label(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _healthy_gh()
    gh.answers["label list"] = json.dumps(
        [{"name": n} for n in ("claude-ready", "human-review-required", IMPLEMENTATION_LABEL)]
    )
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    failed = {c.name: c for c in report.checks if not c.ok}
    assert (
        set(failed) == {"Forge labels exist"}
        and "in-progress" in failed["Forge labels exist"].detail
    )


def test_auto_plan_reports_failures_on_the_issue():
    doc = yaml.safe_load(render_onboarding(spec())[".github/workflows/agent-auto-plan.yml"])
    notify = doc["jobs"]["notify_failure"]
    assert notify["needs"] == ["plan"] or notify["needs"] == "plan"
    assert "always()" in notify["if"] and "needs.plan.result == 'failure'" in notify["if"]
    assert notify["permissions"] == {"issues": "write"}
    step = notify["steps"][0]
    assert step["env"]["ISSUE_NUMBER"] == "${{ github.event.issue.number }}"
    assert "planning did not complete" in step["with"]["script"]


def test_commit_guard_sees_commit_after_value_taking_git_options(tmp_path):
    import importlib.util

    path = tmp_path / "guard.py"
    path.write_text(render_onboarding(spec())[".claude/hooks/forge_commit_guard.py"])
    module_spec = importlib.util.spec_from_file_location("guard_opts", path)
    guard = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(guard)

    def code(command):
        payload = {"tool_name": "Bash", "session_id": "s1", "tool_input": {"command": command}}
        return guard.decide(payload, "forge/issue-7", lambda n: None)[0]

    for blocked in (
        "git -C . commit -m x",
        "git -c user.name=bot commit -m x",
        'git -C "my dir" -c a.b=c commit -m x',
        "git --git-dir .git --work-tree . commit -m x",
        "git --namespace ns --exec-path=/usr/lib/git-core commit",
        "cd sub && git --no-pager -C .. commit -am x",
    ):
        assert code(blocked) == 2, blocked
    for allowed in ("git -C . status", "git -c commit.gpgsign=false log", "git log --grep commit"):
        assert code(allowed) == 0, allowed


def _guard_module(tmp_path, name):
    import importlib.util

    path = tmp_path / f"{name}.py"
    path.write_text(render_onboarding(spec())[f".claude/hooks/{name}.py"])
    module_spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def test_doctor_requires_each_caller_to_call_its_own_reusable_workflow(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    plan = repo / ".github/workflows/agent-plan.yml"
    plan.write_text(plan.read_text().replace("reusable-plan.yml@", "reusable-implement.yml@"))
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False)
    pin = next(c for c in report.checks if c.name == "workflows pin the platform to a commit SHA")
    assert not pin.ok and "agent-plan.yml" in pin.detail and "reusable-plan.yml" in pin.detail


def test_doctor_requires_every_approval_label_in_the_auto_implement_condition(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    auto = repo / ".github/workflows/agent-auto-implement.yml"
    text = auto.read_text()
    weakened = text.replace(
        f" &&\n      contains(github.event.issue.labels.*.name, '{IMPLEMENTATION_LABEL}')", ""
    )
    assert weakened != text
    auto.write_text(weakened)
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False)
    check = next(
        c for c in report.checks if c.name == "workflow label conditions match the policy labels"
    )
    assert not check.ok and IMPLEMENTATION_LABEL in check.detail


def test_rulesets_are_paginated_and_exclude_inherited_ones(tmp_path):
    org_owned = {"id": 99, "name": RULESET_NAME, "source_type": "Organization"}
    page2 = {"id": 5, "name": RULESET_NAME, "source_type": "Repository"}
    filler = [{"id": 100 + i, "name": f"other {i}"} for i in range(30)]
    gh = FakeGh(
        {
            "api repos/owner/comic/rulesets/5": json.dumps({"id": 5, **ruleset_payload(spec())}),
            "api repos/owner/comic/rulesets?includes_parents=false": json.dumps([filler, [page2]]),
            "api repos/owner/comic/rulesets": json.dumps([org_owned]),
        }
    )
    log = apply_repo_settings(spec(), gh)
    assert any("already present" in line for line in log)
    assert not any(c[:3] in {("api", "-X", "POST"), ("api", "-X", "PUT")} for c, _ in gh.calls)
    listing = next(c for c, _ in gh.calls if c[1].startswith("repos/owner/comic/rulesets?"))
    assert "--paginate" in listing and "--slurp" in listing

    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    healthy = _healthy_gh()
    healthy.answers = {
        "api repos/owner/comic/rulesets?includes_parents=false": json.dumps([filler, [page2]]),
        "api repos/owner/comic/rulesets/5": json.dumps({"id": 5, **ruleset_payload(spec())}),
        **healthy.answers,
    }
    healthy.answers["api repos/owner/comic/rulesets"] = json.dumps([org_owned])
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", healthy)
    assert report.ok, report.render()


def test_doctor_reads_every_page_of_runners(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _healthy_gh()
    offline = [{"status": "offline", "labels": []}] * 30
    good = {"status": "online", "labels": [{"name": n} for n in ("self-hosted", "linux", "x64")]}
    gh.answers["api repos/owner/comic/actions/runners"] = _pages("runners", [*offline, good])
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    assert report.ok, report.render()


def test_doctor_reads_every_page_of_user_installations(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _healthy_gh()
    others = [{"id": 1000 + i, "app_slug": f"other-{i}"} for i in range(30)]
    publisher = {"id": 7, "app_slug": "agentic-sdlc-publisher", "permissions": PUBLISHER_PERMS}
    gh.answers["api /user/installations"] = _pages("installations", [*others, publisher])
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    assert report.ok, report.render()


def test_doctor_requires_publisher_installation_permissions(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _healthy_gh()
    gh.answers["api /user/installations"] = _installs(
        7, permissions={"contents": "read", "issues": "read", "pull_requests": "write"}
    )
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    failed = {c.name: c for c in report.checks if not c.ok}
    assert set(failed) == {"Publisher GitHub App installed on this repo"}
    assert "issues" in failed["Publisher GitHub App installed on this repo"].detail


def test_doctor_requires_claude_hooks_settings_and_claude_md(tmp_path):
    for name in (
        ".claude/settings.json",
        ".claude/hooks/forge_commit_guard.py",
        ".claude/hooks/forge_session_start.py",
        "CLAUDE.md",
    ):
        repo = _repo(tmp_path / name.replace("/", "_"))
        write_onboarding(repo, spec())
        (repo / name).unlink()
        report = doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False)
        files = next(c for c in report.checks if c.name == "required files present")
        assert not files.ok and name in files.detail, name


def test_doctor_validates_the_ci_workflow_contents(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    ci = repo / ".github/workflows/ci.yml"
    good = ci.read_text()

    def ci_check():
        report = doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False)
        return next(c for c in report.checks if c.name == "ci.yml runs the policy gates as 'test'")

    assert ci_check().ok, ci_check().detail
    ci.write_text(good.replace("  pull_request:\n", ""))
    assert not ci_check().ok and "pull_request" in ci_check().detail
    ci.write_text(good.replace("\n  test:\n", "\n  tests:\n"))
    assert not ci_check().ok and "'test' job" in ci_check().detail
    ci.write_text(good.replace("pytest tests -q -m 'not e2e'", "true"))
    assert not ci_check().ok and "not e2e" in ci_check().detail


def test_doctor_requires_platform_read_token_for_a_private_platform(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _healthy_gh()
    gh.answers = {"api repos/owner/agentic-sdlc --jq": "true\n", **gh.answers}
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    secrets = next(c for c in report.checks if c.name == "repo secrets set (by name)")
    assert not secrets.ok and "PLATFORM_READ_TOKEN" in secrets.detail
    gh.answers["api repos/owner/comic/actions/secrets"] = _secret_pages(
        "PUBLISHER_APP_PRIVATE_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "DEEPSEEK_API_KEY",
        "ZAI_API_KEY",
        "KIMI_API_KEY",
        "PLATFORM_READ_TOKEN",
    )
    assert doctor(repo, "owner/comic", "owner/agentic-sdlc", gh).ok


def test_commit_guard_treats_a_timezone_less_expiry_as_utc(tmp_path, monkeypatch):
    import subprocess
    from datetime import UTC, datetime, timedelta

    guard = _guard_module(tmp_path, "forge_commit_guard")
    naive = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    body = f"<!-- forge-claim agent=a session=s1 branch=b expires={naive} -->"
    pages = [[{"body": body, "author_association": "OWNER", "user": {"type": "User"}}]]

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(pages), stderr="")

    monkeypatch.setattr(guard.subprocess, "run", fake_run)
    lease = guard.lease_for(7)
    assert lease is not None and lease["expires"].tzinfo is not None


def test_session_start_separates_expired_leases_from_live_ones(tmp_path):
    start = _guard_module(tmp_path, "forge_session_start")
    issues = [
        {"number": 1, "title": "live one", "assignees": [{"login": "a"}]},
        {"number": 2, "title": "crashed agent", "assignees": []},
    ]
    live, stale = start.classify_leased(issues, lambda n: {"session": "s"} if n == 1 else None)
    assert [line.split()[0] for line in live] == ["#1"]
    assert [line.split()[0] for line in stale] == ["#2"]


def test_cli_onboard_validates_var_before_writing_any_file(tmp_path, monkeypatch):
    from agentic_sdlc import cli

    monkeypatch.setattr(cli, "apply_repo_settings", lambda *a, **k: pytest.fail("applied"))
    repo = _repo(tmp_path)
    code = cli.main(
        [
            "onboard",
            "--destination",
            str(repo),
            "--project-id",
            "owner/comic",
            "--platform-repository",
            "owner/agentic-sdlc",
            "--platform-ref",
            SHA,
            "--test",
            "pytest -q",
            "--default-branch",
            "main",
            "--apply",
            "--var",
            "NO_EQUALS_SIGN",
        ]
    )
    assert code != 0
    assert not (repo / "agentic-sdlc.toml").exists()


def test_doctor_rejects_a_mixed_cloud_and_actions_profile(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())  # Actions implementer
    (repo / "docs/forge").mkdir(parents=True)
    (repo / "docs/forge/cloud-implementer.md").write_text("# cloud\n")
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False)
    bad = {c.name: c for c in report.checks if not c.ok}
    assert "implementation profile is unambiguous" in bad
    assert "agent-auto-implement.yml" in bad["implementation profile is unambiguous"].detail


def test_doctor_requires_every_routed_fallback_to_be_configured(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())  # route mode: deepseek + zai + kimi + claude executors
    gh = _healthy_gh()
    gh.answers["api repos/owner/comic/actions/variables"] = _var_pages(
        "PUBLISHER_APP_CLIENT_ID", "DEEPSEEK_MODEL_FLASH", "DEEPSEEK_MODEL_PRO"
    )
    gh.answers["api repos/owner/comic/actions/secrets"] = _secret_pages(
        "PUBLISHER_APP_PRIVATE_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "DEEPSEEK_API_KEY"
    )
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    bad = {c.name: c.detail for c in report.checks if not c.ok}
    assert "ZAI_API_KEY" in bad["repo secrets set (by name)"]
    assert "KIMI_API_KEY" in bad["repo secrets set (by name)"]
    assert "ZAI_MODEL_GLM" in bad["repo variables set (non-empty)"]
    assert "KIMI_MODEL_K3" in bad["repo variables set (non-empty)"]


def test_doctor_rejects_empty_required_variables(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec(implementer="claude"))
    gh = _healthy_gh()
    gh.answers["api repos/owner/comic/actions/variables"] = json.dumps(
        [{"total_count": 1, "variables": [{"name": "PUBLISHER_APP_CLIENT_ID", "value": ""}]}]
    )
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    bad = {c.name: c.detail for c in report.checks if not c.ok}
    assert "PUBLISHER_APP_CLIENT_ID" in bad["repo variables set (non-empty)"]
    assert "empty value" in bad["repo variables set (non-empty)"]


def test_doctor_accepts_a_registry_with_one_usable_executor(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    reg = json.loads((repo / ".forge/executors.json").read_text())
    reg["executors"][1]["permittedRepositories"] = ["owner/other"]  # shared registry entry
    (repo / ".forge/executors.json").write_text(json.dumps(reg))
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False)
    routing = next(c for c in report.checks if c.name == "routing files valid and permit this repo")
    assert routing.ok and "4 of 5" in routing.detail


def test_doctor_checks_the_ci_test_job_runner_target(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec(implementer="claude"))
    ci = repo / ".github/workflows/ci.yml"
    ci.write_text(
        ci.read_text().replace(
            "runs-on: [self-hosted, linux, x64]", "runs-on: [self-hosted, linux, arm64]"
        )
    )
    gh = _healthy_gh()  # runner carries self-hosted/linux/x64 only
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    check = next(
        c for c in report.checks if c.name == "ci.yml test job runs on an available runner"
    )
    assert not check.ok and "arm64" in check.detail
