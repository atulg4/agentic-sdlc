from __future__ import annotations

import base64
import json
import re
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from agentic_sdlc.executors import load_executors, load_routing_policy
from agentic_sdlc.onboard import (
    IMPLEMENTATION_LABEL,
    REUSABLE_IMPLEMENT_AGENTS,
    REUSABLE_IMPLEMENT_DEFAULT_AGENT,
    RULESET_NAME,
    OnboardError,
    OnboardSpec,
    apply_repo_settings,
    copy_variables,
    doctor,
    merged_ruleset,
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


_CONSUMER: dict[str, Path] = {}  # the consumer checkout the current test created
CONSUMER_CONTENTS = "api repos/owner/comic/contents/"


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "consumer"
    (repo / ".git").mkdir(parents=True)
    _CONSUMER["path"] = repo
    return repo


class FakeGh:
    """Records gh invocations and answers from a canned table keyed by the first few args.

    Unless the table answers it, the consumer's contents API serves the files of the test's
    checkout: by default everything doctor checks locally has also been pushed."""

    def __init__(self, answers: dict[str, str] | None = None, fail: set[str] | None = None):
        self.calls: list[tuple[tuple[str, ...], str | None]] = []
        self.answers = answers or {}
        self.fail = fail or set()

    def __call__(self, args, input=None):
        self.calls.append((tuple(args), input))
        key = " ".join(args)
        if key.startswith(CONSUMER_CONTENTS):
            for prefix, value in self.answers.items():
                if prefix.startswith(CONSUMER_CONTENTS) and key.startswith(prefix):
                    return value
            path = _CONSUMER["path"] / key[len(CONSUMER_CONTENTS) :].split("?")[0]
            if path.is_dir():  # a directory listing, as the contents API returns it
                return json.dumps(
                    [
                        {
                            "name": child.name,
                            "path": str(child.relative_to(_CONSUMER["path"])),
                            "type": "dir" if child.is_dir() else "file",
                        }
                        for child in sorted(path.iterdir())
                    ]
                )
            if not path.is_file():
                raise OnboardError("gh: Not Found (HTTP 404)")
            content = base64.b64encode(path.read_bytes()).decode()
            return json.dumps({"encoding": "base64", "content": content})
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
    doc = yaml.safe_load(plan)
    assert doc["jobs"]["plan"]["uses"] == (
        f"owner/agentic-sdlc/.github/workflows/reusable-plan.yml@{SHA}"
    )
    assert json.loads(doc["jobs"]["plan"]["with"]["runs_on"]) == ["self-hosted", "linux", "x64"]
    assert 'runs-on: ["self-hosted", "linux", "x64"]' in plan
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
            "api repos/owner/comic/labels": json.dumps(
                [
                    [
                        {"name": n}
                        for n in (
                            "claude-ready",
                            "human-review-required",
                            IMPLEMENTATION_LABEL,
                            "in-progress",
                        )
                    ]
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
            "api apps/agentic-sdlc-publisher": json.dumps(
                {"slug": "agentic-sdlc-publisher", "client_id": "publisher_app_client_id-value"}
            ),
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
            "--visibility",
            "private",
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
                "--visibility",
                "private",
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
    assert 'labels: ["forge-ready", "human-review-required"]' in template
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

    def row(body, login="owner"):
        return {"body": body, "user": {"login": login, "type": "User"}}

    mine = row(f"<!-- forge-claim agent=a session=s1 branch=b expires={expires} -->")
    forged = row("<!-- forge-release session=s1 -->", login="rando")  # no write access
    pages = [[row("chatter")] * 30, [mine, forged]]

    def fake_run(args, **kwargs):
        out = json.dumps(pages) if "--slurp" in args else "".join(json.dumps(p) for p in pages)
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")

    monkeypatch.setattr(guard.subprocess, "run", fake_run)
    monkeypatch.setattr(guard, "repo_permission", {"owner": "admin", "rando": "read"}.get)
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
    gh.answers["api repos/owner/comic/labels"] = json.dumps(
        [[{"name": n} for n in ("claude-ready", "human-review-required", IMPLEMENTATION_LABEL)]]
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


def test_commit_guard_parses_path_qualified_and_wrapped_git(tmp_path):
    guard = _guard_module(tmp_path, "forge_commit_guard")

    def code(command):
        payload = {"tool_name": "Bash", "session_id": "s1", "tool_input": {"command": command}}
        return guard.decide(payload, "forge/issue-7", lambda n: None, lambda b: False)[0]

    for blocked in (
        "/usr/bin/git commit -m x",
        "./git commit -m x",
        "/usr/bin/git -C . -c a=b commit -m x",
        "GIT_AUTHOR_NAME=x env -i /usr/local/bin/git commit -m x",
        "make lint; /usr/bin/git commit -am 'a; b'",
        "true && (cd sub && git commit -m x)",
        "bash -c 'git -C . commit -m x'",
        "git commit -m 'unbalanced",
        # Fail-closed posture (Codex 4205123774): text that mentions both words and is not
        # proven a non-commit is checked as one -- harmless on a leased branch.
        "echo 'git commit'",
        "grep -r 'git commit' docs",
    ):
        assert code(blocked) == 2, blocked
    for allowed in (
        "/usr/bin/git status",
        "/usr/bin/git log --grep commit",
        "legit commit",
    ):
        assert code(allowed) == 0, allowed


def test_commit_guard_does_not_let_a_lapsed_session_regain_seniority(tmp_path, monkeypatch):
    import subprocess
    from datetime import UTC, datetime, timedelta

    guard = _guard_module(tmp_path, "forge_commit_guard")
    now = datetime.now(UTC)

    def at(moment):
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")

    def marker(session, expires, posted):
        body = f"<!-- forge-claim agent=a session={session} branch=b expires={at(expires)} -->"
        return {"body": body, "user": {"login": "owner", "type": "User"}, "created_at": at(posted)}

    comments = [
        marker("old", now - timedelta(minutes=30), now - timedelta(hours=4)),
        marker("new", now + timedelta(hours=1), now - timedelta(minutes=10)),
        marker("old", now + timedelta(hours=2), now - timedelta(minutes=5)),  # late renewal
    ]

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps([comments]), stderr="")

    monkeypatch.setattr(guard.subprocess, "run", fake_run)
    monkeypatch.setattr(guard, "repo_permission", lambda login: "write")
    assert guard.lease_for(7)["session"] == "new"


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
    gh.answers["api repos/owner/agentic-sdlc/actions/permissions/access"] = json.dumps(
        {"access_level": "user"}
    )
    assert doctor(repo, "owner/comic", "owner/agentic-sdlc", gh).ok


def test_commit_guard_treats_a_timezone_less_expiry_as_utc(tmp_path, monkeypatch):
    import subprocess
    from datetime import UTC, datetime, timedelta

    guard = _guard_module(tmp_path, "forge_commit_guard")
    naive = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    body = f"<!-- forge-claim agent=a session=s1 branch=b expires={naive} -->"
    pages = [[{"body": body, "user": {"login": "owner", "type": "User"}}]]

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(pages), stderr="")

    monkeypatch.setattr(guard.subprocess, "run", fake_run)
    monkeypatch.setattr(guard, "repo_permission", lambda login: "admin")
    lease = guard.lease_for(7)
    assert lease is not None and lease["expires"].tzinfo is not None


def test_session_start_separates_expired_leases_from_live_ones(tmp_path):
    start = _guard_module(tmp_path, "forge_session_start")
    issues = [
        {"number": 1, "title": "live one", "assignees": [{"login": "a"}]},
        {"number": 2, "title": "crashed agent", "assignees": []},
    ]
    live, stale, mine = start.classify_leased(
        issues, lambda n: {"session": "s"} if n == 1 else None
    )
    assert [line.split()[0] for line in live] == ["#1"]
    assert [line.split()[0] for line in stale] == ["#2"]
    assert mine == []


def test_session_start_reports_its_own_lease_apart_from_other_agents(tmp_path, monkeypatch):
    """Codex 4205654975: on a resume/clear/compaction the session's own live lease is not
    another agent's."""
    import io

    start = _guard_module(tmp_path, "forge_session_start")
    issues = [
        {"number": 5, "title": "mine", "assignees": []},
        {"number": 6, "title": "theirs", "assignees": []},
    ]
    leases = {5: {"session": "me"}, 6: {"session": "other"}}
    live, stale, mine = start.classify_leased(issues, leases.get, "me")
    assert [x.split()[0] for x in mine] == ["#5"] and [x.split()[0] for x in live] == ["#6"]
    assert stale == []

    def run(cmd):
        return json.dumps(issues) if cmd[:3] == ["gh", "issue", "list"] else ""

    monkeypatch.setattr(start, "run", run)
    monkeypatch.setattr(start, "_lease_lookup", lambda: leases.get)
    monkeypatch.setattr(start.sys, "stdin", io.StringIO(json.dumps({"session_id": "me"})))
    out = io.StringIO()
    monkeypatch.setattr(start.sys, "stdout", out)
    assert start.main() == 0
    text = out.getvalue()
    others = text.split("other agents (do NOT work on these):\n")[1].split("[Forge]")[0]
    assert "#6 theirs" in others and "#5" not in others
    assert "Your current lease (this session, me): #5 mine" in text


def test_session_start_lists_every_in_progress_issue_not_the_default_30(tmp_path, monkeypatch):
    import io

    start = _guard_module(tmp_path, "forge_session_start")
    calls = []
    monkeypatch.setattr(start, "run", lambda cmd: calls.append(cmd) or "")
    monkeypatch.setattr(start.sys, "stdin", io.StringIO("{}"))
    assert start.main() == 0
    listing = next(c for c in calls if c[:3] == ["gh", "issue", "list"])
    assert "--limit" in listing and int(listing[listing.index("--limit") + 1]) >= 1000


def test_cli_onboard_validates_var_before_writing_any_file(tmp_path, monkeypatch):
    from agentic_sdlc import cli

    monkeypatch.setattr(cli, "apply_repo_settings", lambda *a, **k: pytest.fail("applied"))
    repo = _repo(tmp_path)
    code = cli.main(
        [
            "onboard",
            "--visibility",
            "private",
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
    assert routing.ok and "3 of 5" in routing.detail  # flash is below the medium floor


def test_doctor_checks_the_ci_test_job_runner_target(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec(implementer="claude"))
    ci = repo / ".github/workflows/ci.yml"
    ci.write_text(
        ci.read_text().replace(
            'runs-on: ["self-hosted", "linux", "x64"]', 'runs-on: ["self-hosted", "linux", "arm64"]'
        )
    )
    gh = _healthy_gh()  # runner carries self-hosted/linux/x64 only
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    check = next(
        c for c in report.checks if c.name == "ci.yml test job runs on an available runner"
    )
    assert not check.ok and "arm64" in check.detail


# ---------------------------------------------------------------- review regressions (PR 139)


def _failed(report) -> dict:
    return {c.name: c for c in report.checks if not c.ok}


def test_commit_guard_keeps_the_earliest_live_claim_after_a_racer_retracts(tmp_path, monkeypatch):
    import subprocess
    from datetime import UTC, datetime, timedelta

    guard = _guard_module(tmp_path, "forge_commit_guard")
    exp = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    posted = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def marker(body):
        return {"body": body, "user": {"login": "owner", "type": "User"}, "created_at": posted}

    winner = marker(f"<!-- forge-claim agent=a session=s1 branch=b expires={exp} -->")
    racer = marker(f"<!-- forge-claim agent=a session=s2 branch=b expires={exp} -->")
    retract = marker("<!-- forge-release session=s2 -->")
    for comments in ([winner, racer], [winner, racer, retract]):

        def fake_run(args, comments=comments, **kwargs):
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps([comments]), stderr="")

        monkeypatch.setattr(guard.subprocess, "run", fake_run)
        monkeypatch.setattr(guard, "repo_permission", lambda login: "maintain")
        lease = guard.lease_for(7)
        assert lease is not None and lease["session"] == "s1"


def _ci_test_step(repo: Path, run: str) -> None:
    ci = repo / ".github/workflows/ci.yml"
    doc = yaml.safe_load(ci.read_text())
    step = next(s for s in doc["jobs"]["test"]["steps"] if s.get("name") == "Test")
    step["run"] = run
    ci.write_text(yaml.safe_dump(doc, sort_keys=False))


@pytest.mark.parametrize(
    "run",
    [
        "echo pytest tests -q -m 'not e2e'",
        "# pytest tests -q -m 'not e2e'",
        "true \"pytest tests -q -m 'not e2e'\"",
    ],
)
def test_doctor_rejects_a_ci_gate_that_only_mentions_the_command(tmp_path, run):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    _ci_test_step(repo, run)
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False)
    assert "ci.yml runs the policy gates as 'test'" in _failed(report)


STEPS_DIFFER = "job steps differ from the ones onboard renders"


def _analysis_only(repo: Path) -> list[str]:
    """_ci_problems without the template-equality finding: the defense-in-depth analysis."""
    from agentic_sdlc.onboard import _ci_problems

    return [p for p in _ci_problems(repo) if STEPS_DIFFER not in p]


def _assert_only_template_drift(repo: Path) -> None:
    """A hand-edited test job the gate analysis accepts still fails doctor: the CI test job is
    fully managed, so its steps must equal the template (the primary guarantee)."""
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False)
    check = _failed(report)["ci.yml runs the policy gates as 'test'"]
    assert STEPS_DIFFER in check.detail and "onboard --force" in check.detail, check.detail
    assert _analysis_only(repo) == []


def test_doctor_accepts_a_ci_gate_run_as_a_command_among_others(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    _ci_test_step(repo, "set -e  # run the tests\nCI=1 pytest tests -q -m 'not e2e' --maxfail=1")
    _assert_only_template_drift(repo)


def test_doctor_rejects_a_client_id_of_another_publisher_app(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _healthy_gh()
    gh.answers["api apps/agentic-sdlc-publisher"] = json.dumps({"client_id": "Iv1.other"})
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))
    assert set(failed) == {"Publisher GitHub App installed on this repo"}
    assert "PUBLISHER_APP_CLIENT_ID" in failed["Publisher GitHub App installed on this repo"].detail


def test_cli_copy_vars_from_copies_every_registry_model_variable(tmp_path, monkeypatch):
    from agentic_sdlc import cli
    from agentic_sdlc.onboard import DoctorReport

    copied: list[str] = []
    monkeypatch.setattr(cli, "copy_variables", lambda src, names: copied.extend(names) or {})
    monkeypatch.setattr(cli, "apply_repo_settings", lambda *a, **k: {})
    monkeypatch.setattr(cli, "doctor", lambda *a, **k: DoctorReport())
    repo = _repo(tmp_path)
    args = ["onboard", "--visibility", "private", "--destination", str(repo)]
    args += ["--project-id", "owner/comic"]
    args += ["--platform-repository", "owner/agentic-sdlc", "--platform-ref", SHA]
    args += ["--test", "pytest -q", "--default-branch", "main", "--apply"]
    assert (
        cli.main([*args, "--copy-vars-from", "owner/music", "--output", str(tmp_path / "o.json")])
        == 0
    )
    registry = json.loads((repo / ".forge/executors.json").read_text())
    wanted = {
        e["model"].removeprefix("configured-by-")
        for e in registry["executors"]
        if e["model"].startswith("configured-by-")
    }
    assert wanted == {
        "DEEPSEEK_MODEL_FLASH",
        "DEEPSEEK_MODEL_PRO",
        "ZAI_MODEL_GLM",
        "KIMI_MODEL_K3",
    }
    assert set(copied) == {"PUBLISHER_APP_CLIENT_ID", *wanted}


@pytest.mark.parametrize(
    "runs_on",
    ["ubuntu-lates", "${{ matrix.os }}", "big-runner-group", "ubuntu-99.04", "macos-99"],
)
def test_doctor_rejects_unknown_hosted_runner_labels(tmp_path, runs_on):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    ci = repo / ".github/workflows/ci.yml"
    doc = yaml.safe_load(ci.read_text())
    doc["jobs"]["test"]["runs-on"] = runs_on
    ci.write_text(yaml.safe_dump(doc, sort_keys=False))
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", _healthy_gh()))
    assert set(failed) == {"ci.yml test job runs on an available runner"}


@pytest.mark.parametrize(
    "runs_on", ["ubuntu-latest", "ubuntu-24.04-arm", "macos-15", "macos-15-xlarge"]
)
def test_doctor_accepts_github_hosted_runner_labels(tmp_path, runs_on):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    ci = repo / ".github/workflows/ci.yml"
    doc = yaml.safe_load(ci.read_text())
    doc["jobs"]["test"]["runs-on"] = runs_on
    ci.write_text(yaml.safe_dump(doc, sort_keys=False))
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", _healthy_gh())
    assert report.ok, report.render()


def test_auto_plan_triggers_on_the_ready_label_only():
    doc = yaml.safe_load(render_onboarding(spec())[".github/workflows/agent-auto-plan.yml"])
    cond = doc["jobs"]["plan"]["if"]
    assert "github.event.label.name == 'claude-ready'" in cond
    assert "github.event.label.name == 'human-review-required'" not in cond
    assert "contains(github.event.issue.labels.*.name, 'human-review-required')" in cond


def test_default_claude_executor_routes_a_literal_model(tmp_path):
    registry = json.loads(render_onboarding(spec())[".forge/executors.json"])
    claude = next(e for e in registry["executors"] if e["provider"] == "anthropic")
    assert not claude["model"].startswith("configured-by-")
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    claude["model"] = "configured-by-CLAUDE_MODEL"
    (repo / ".forge/executors.json").write_text(json.dumps(registry))
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    assert "configured-by-" in failed["routed executors have a workflow adapter"].detail


def test_doctor_reads_every_page_of_labels(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _healthy_gh()
    filler = [{"name": f"area-{i}"} for i in range(250)]
    forge = [{"name": n} for n in ("claude-ready", "human-review-required", IMPLEMENTATION_LABEL)]
    rows = [*filler, *forge, {"name": "in-progress"}]
    gh.answers["api repos/owner/comic/labels"] = json.dumps(
        [rows[i : i + 100] for i in range(0, len(rows), 100)]
    )
    assert doctor(repo, "owner/comic", "owner/agentic-sdlc", gh).ok
    call = next(c for c, _ in gh.calls if c[:2] == ("api", "repos/owner/comic/labels?per_page=100"))
    assert "--paginate" in call


@pytest.mark.parametrize("provider", ["openai", "aws-bedrock", "azure-foundry"])
def test_doctor_rejects_a_routed_provider_without_a_workflow_adapter(tmp_path, provider):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    path = repo / ".forge/executors.json"
    registry = json.loads(path.read_text())
    registry["executors"][1]["provider"] = provider  # deepseek-v4-pro: routed at medium risk
    path.write_text(json.dumps(registry))
    policy = repo / ".forge/routing-policy.json"
    routing = json.loads(policy.read_text())
    routing["allowedProviders"].append(provider)
    policy.write_text(json.dumps(routing))
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    assert provider in failed["routed executors have a workflow adapter"].detail


def test_routed_adapter_providers_match_the_implement_workflow():
    from agentic_sdlc.onboard import ROUTED_ADAPTER_PROVIDERS

    text = (Path(__file__).parents[1] / ".github/workflows/reusable-implement.yml").read_text()
    compat = re.search(
        r"contains\(fromJSON\('(\[[^']*\])'\), needs\.prepare\.outputs\.selected_provider\)", text
    )
    direct = set(re.findall(r"selected_provider == '([\w-]+)'", text))
    assert compat and direct | set(json.loads(compat.group(1))) == ROUTED_ADAPTER_PROVIDERS


# ------------------------------------------- doctor mirrors the router and workflows (PR 139)


def _routed_repo(tmp_path, *extra_executors, allow=()):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    path = repo / ".forge/executors.json"
    registry = json.loads(path.read_text())
    registry["executors"] += list(extra_executors)
    path.write_text(json.dumps(registry))
    policy = repo / ".forge/routing-policy.json"
    routing = json.loads(policy.read_text())
    routing["allowedProviders"] += list(allow)
    policy.write_text(json.dumps(routing))
    return repo


def _extra_executor(**overrides) -> dict:
    claude = next(
        e
        for e in json.loads(render_onboarding(spec())[".forge/executors.json"])["executors"]
        if e["provider"] == "anthropic"
    )
    return {**claude, **overrides}


@pytest.mark.parametrize(
    "change",
    [
        {"available": False},
        {"taskClasses": ["review"]},
        {"permittedRepositories": ["owner/other"]},
        {"runtimeStatus": "quota-exhausted"},
        {"toolCapabilities": ["function-calling"]},
    ],
)
def test_doctor_requires_an_executor_the_router_can_select(tmp_path, change):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    path = repo / ".forge/executors.json"
    registry = json.loads(path.read_text())
    registry["executors"] = [{**e, **change} for e in registry["executors"]]
    path.write_text(json.dumps(registry))
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    assert "no executor the router can select" in (
        failed["routing files valid and permit this repo"].detail
    )


def test_doctor_rejects_a_registry_whose_only_selectable_executor_is_policy_denied(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    policy = repo / ".forge/routing-policy.json"
    routing = json.loads(policy.read_text())
    routing["allowedProviders"] = ["openai"]
    policy.write_text(json.dumps(routing))
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    assert "routing files valid and permit this repo" in failed


def test_doctor_requires_the_openai_key_for_a_routed_codex_executor(tmp_path):
    codex = _extra_executor(
        executorId="codex",
        provider="codex",
        model="gpt-5-codex",
        modelAlias="codex",
        modelFamily="gpt",
        adapter="codex",
        authMode="api-key",
        executionType="direct-api",
    )
    repo = _routed_repo(tmp_path, codex, allow=["codex"])
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", _healthy_gh()))
    assert set(failed) == {"repo secrets set (by name)"}
    assert "OPENAI_API_KEY" in failed["repo secrets set (by name)"].detail


def test_doctor_requires_and_forwards_the_anthropic_key_for_an_api_key_executor(tmp_path):
    api = _extra_executor(executorId="claude-api", authMode="api-key", executionType="direct-api")
    repo = _routed_repo(tmp_path, api)
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", _healthy_gh()))
    assert set(failed) == {"repo secrets set (by name)"}
    assert "ANTHROPIC_API_KEY" in failed["repo secrets set (by name)"].detail
    for name in ("agent-implement.yml", "agent-auto-implement.yml"):
        doc = yaml.safe_load((repo / ".github/workflows" / name).read_text())
        assert "ANTHROPIC_API_KEY" in doc["jobs"]["implement"]["secrets"]
    caller = repo / ".github/workflows/agent-implement.yml"
    caller.write_text(
        caller.read_text().replace(
            "      ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}\n", ""
        )
    )
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    assert "agent-implement.yml:implement: ANTHROPIC_API_KEY" in (
        failed["implement callers forward every routed secret"].detail
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda job, step: step.update({"continue-on-error": True}),
        lambda job, step: step.update({"if": "github.event_name == 'push'"}),
        lambda job, step: job.update({"continue-on-error": True}),
        lambda job, step: job.update({"if": "false"}),
        lambda job, step: step.update({"run": step["run"] + " || true"}),
        lambda job, step: step.update({"run": "set +e\n" + step["run"] + "\necho done"}),
        lambda job, step: step.update({"run": "if " + step["run"] + "; then echo ok; fi"}),
    ],
)
def test_doctor_rejects_a_ci_gate_whose_failure_is_ignored(tmp_path, mutate):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    ci = repo / ".github/workflows/ci.yml"
    doc = yaml.safe_load(ci.read_text())
    job = doc["jobs"]["test"]
    mutate(job, next(s for s in job["steps"] if s.get("name") == "Test"))
    ci.write_text(yaml.safe_dump(doc, sort_keys=False))
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False)
    assert "ci.yml runs the policy gates as 'test'" in _failed(report)


def test_doctor_requires_the_managed_files_on_the_remote_default_branch(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _healthy_gh()
    gh.answers[CONSUMER_CONTENTS] = ""  # nothing pushed yet
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))
    assert set(failed) == {"managed files are on the default branch"}
    detail = failed["managed files are on the default branch"].detail
    assert ".github/workflows/ci.yml (missing)" in detail and "push these files first" in detail
    gh = _healthy_gh()
    plan = repo / ".github/workflows/agent-plan.yml"
    gh.answers[CONSUMER_CONTENTS + ".github/workflows/agent-plan.yml"] = json.dumps(
        {"encoding": "base64", "content": base64.b64encode(plan.read_bytes()).decode()}
    )
    plan.write_text(plan.read_text() + "# edited, not pushed\n")
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))
    assert set(failed) == {"managed files are on the default branch"}
    assert "agent-plan.yml (differs)" in failed["managed files are on the default branch"].detail
    assert any(
        c[1].endswith("contents/.github/workflows/agent-plan.yml?ref=main") for c, _ in gh.calls
    )


@pytest.mark.parametrize(
    ("caller", "job"),
    [("agent-plan.yml", "preflight"), ("agent-auto-implement.yml", "notify_failure")],
)
def test_doctor_validates_every_caller_runner_target(tmp_path, caller, job):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    path = repo / ".github/workflows" / caller
    doc = yaml.safe_load(path.read_text())
    doc["jobs"][job]["runs-on"] = "ubuntu-lates"
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", _healthy_gh()))
    # a lone custom label names no OS: not provably Linux either (Codex 4205367730)
    assert set(failed) == {"self-hosted runner online for this repo", LINUX_CHECK}
    assert f"{caller}:{job}" in failed["self-hosted runner online for this repo"].detail
    assert f"{caller}:{job}" in failed[LINUX_CHECK].detail


def test_doctor_validates_the_runner_a_caller_hands_its_reusable_workflow(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    path = repo / ".github/workflows/agent-plan.yml"
    doc = yaml.safe_load(path.read_text())
    doc["jobs"]["plan"]["with"]["runs_on"] = '["self-hosted","linux","arm64"]'
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", _healthy_gh()))
    assert "agent-plan.yml:plan (runs_on)" in (
        failed["self-hosted runner online for this repo"].detail
    )


# ---------------------------------------------------------------- review regressions (PR 139, 4)


def test_public_repository_ci_defaults_to_a_github_hosted_runner():
    files = render_onboarding(spec(public=True))
    ci = yaml.safe_load(files[".github/workflows/ci.yml"])
    assert ci["jobs"]["test"]["runs-on"] == ["ubuntu-latest"]
    # issue-triggered agent workflows keep the self-hosted target: they run no fork code
    plan = yaml.safe_load(files[".github/workflows/agent-plan.yml"])
    assert plan["jobs"]["preflight"]["runs-on"] == ["self-hosted", "linux", "x64"]
    assert spec(public=True, ci_runs_on=("macos-15",)).ci_labels == ("macos-15",)
    with pytest.raises(OnboardError, match="public repository"):
        spec(public=True, ci_runs_on=("self-hosted", "linux", "x64"))
    # a private repository keeps CI wherever --runs-on says
    assert yaml.safe_load(render_onboarding(spec())[".github/workflows/ci.yml"])["jobs"]["test"][
        "runs-on"
    ] == ["self-hosted", "linux", "x64"]


@pytest.mark.parametrize("visibility", [{"visibility": "public"}, {"private": False}])
def test_doctor_fails_a_public_repository_running_pull_requests_on_self_hosted(
    tmp_path, visibility
):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())  # CI on self-hosted, as a private repo would have it
    extra = repo / ".github/workflows/lint.yml"
    extra.write_text(
        "on: pull_request_target\njobs:\n  lint:\n    runs-on: [self-hosted]\n"
        "    steps:\n      - run: make lint\n"
    )
    gh = _healthy_gh()
    gh.answers["api repos/owner/comic"] = json.dumps({"default_branch": "main", **visibility})
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))
    check = failed["public repository runs pull requests on GitHub-hosted runners"]
    assert not check.manual
    assert "ci.yml:test" in check.detail and "lint.yml:lint" in check.detail
    # issue-triggered callers are not pull-request code
    assert "agent-plan.yml" not in check.detail


def test_doctor_accepts_a_public_repository_onboarded_with_hosted_ci(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec(public=True))
    gh = _healthy_gh()
    gh.answers["api repos/owner/comic"] = json.dumps(
        {"default_branch": "main", "visibility": "public", "private": False}
    )
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    assert report.ok, report.render()
    assert any(
        c.name == "public repository runs pull requests on GitHub-hosted runners"
        for c in report.checks
    )


def test_cli_onboard_reads_visibility_and_refuses_self_hosted_ci_for_public_repos(
    tmp_path, monkeypatch
):
    from agentic_sdlc import cli

    monkeypatch.setattr(cli, "repository_is_public", lambda project: True)
    repo = _repo(tmp_path)
    base = ["onboard", "--destination", str(repo), "--project-id", "owner/comic"]
    base += ["--platform-repository", "owner/agentic-sdlc", "--platform-ref", SHA]
    base += ["--test", "pytest -q", "--output", str(tmp_path / "o.json")]
    assert cli.main([*base, "--ci-runs-on", "self-hosted,linux,x64"]) != 0
    assert not (repo / "agentic-sdlc.toml").exists()
    assert cli.main(base) == 0
    ci = yaml.safe_load((repo / ".github/workflows/ci.yml").read_text())
    assert ci["jobs"]["test"]["runs-on"] == ["ubuntu-latest"]

    def unreadable(project):
        raise OnboardError("gh failed")

    monkeypatch.setattr(cli, "repository_is_public", unreadable)
    assert cli.main([*base, "--force"]) != 0  # visibility unknown: ask, never guess


def test_repository_is_public_reads_visibility():
    from agentic_sdlc.onboard import repository_is_public

    assert repository_is_public("o/r", lambda args, input=None: '{"visibility": "public"}')
    assert not repository_is_public("o/r", lambda args, input=None: '{"private": true}')
    with pytest.raises(OnboardError):
        repository_is_public("o/r", lambda args, input=None: "{}")


def test_runner_labels_render_as_yaml_strings():
    labels = ("true", "null", "on", "123")
    agent_labels = ("linux", *labels)  # implementation is Linux-only
    files = render_onboarding(spec(runs_on=agent_labels, ci_runs_on=labels))
    seen = 0
    for name, content in files.items():
        if not name.endswith(".yml"):
            continue
        want = list(labels) if name.endswith("/ci.yml") else list(agent_labels)
        for job in (yaml.safe_load(content).get("jobs") or {}).values():
            if "runs-on" in job:
                assert job["runs-on"] == want, name
                seen += 1
            if "runs_on" in (job.get("with") or {}):
                assert json.loads(job["with"]["runs_on"]) == want, name
                seen += 1
    assert seen >= 8


def test_doctor_reads_reusable_calls_from_jobs_not_comments(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    plan = repo / ".github/workflows/agent-plan.yml"
    doc = yaml.safe_load(plan.read_text())
    target = doc["jobs"]["plan"]["uses"]
    doc["jobs"]["plan"] = {"runs-on": "ubuntu-latest", "steps": [{"run": "true"}]}
    plan.write_text(f"# uses: {target}\n" + yaml.safe_dump(doc, sort_keys=False))
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    assert "agent-plan.yml calls no reusable workflow" in (
        failed["workflows pin the platform to a commit SHA"].detail
    )


def test_cli_doctor_infers_the_platform_from_the_plan_job_not_a_comment(tmp_path, capsys):
    from agentic_sdlc.cli import main

    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    plan = repo / ".github/workflows/agent-plan.yml"
    plan.write_text(
        "# uses: evil/fork/.github/workflows/reusable-plan.yml@main\n" + plan.read_text()
    )
    main(["doctor", "--destination", str(repo), "--local"])
    out = capsys.readouterr().out
    assert "[PASS] workflows pin the platform to a commit SHA" in out


def test_doctor_routes_the_implementation_mission_the_workflow_routes(tmp_path):
    """Codex's example: a registry whose only permitted executor is DeepSeek Flash (quality
    0.76) passes a low-risk floor but never the medium-risk implementation-worker route."""
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    path = repo / ".forge/executors.json"
    registry = json.loads(path.read_text())
    for entry in registry["executors"][1:]:
        entry["permittedRepositories"] = ["owner/other"]
    path.write_text(json.dumps(registry))
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    detail = failed["routing files valid and permit this repo"].detail
    assert "implementation-worker" in detail and "quality floor not met" in detail


def test_doctor_route_request_matches_the_implement_workflow():
    """Pin doctor's request to the `route-executor` call in reusable-implement.yml."""
    from agentic_sdlc.onboard import (
        ROUTE_DEFAULT_BUDGET_USD,
        ROUTE_MIN_CONTEXT_WINDOW,
        ROUTE_MISSION_ID,
        ROUTE_TASK_CLASS,
        ROUTE_TOOL_CAPABILITIES,
    )

    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/reusable-implement.yml").read_text())
    steps = [s for job in workflow["jobs"].values() for s in job.get("steps") or []]
    route = next(str(s["run"]) for s in steps if "route-executor" in str(s.get("run", "")))
    assert re.findall(r"--mission-id (\S+)", route) == [ROUTE_MISSION_ID]
    assert re.findall(r"--task-class (\S+)", route) == [ROUTE_TASK_CLASS.value]
    assert tuple(re.findall(r"--required-tool-capability (\S+)", route)) == (
        ROUTE_TOOL_CAPABILITIES
    )
    assert re.findall(r"--min-context-window (\d+)", route) == [str(ROUTE_MIN_CONTEXT_WINDOW)]
    assert "--missions" not in route  # platform missions only, as doctor loads them
    assert re.findall(r'--budget-usd "\$(\w+)"', route) == ["ROUTE_BUDGET_USD"]
    triggers = workflow.get("on", workflow.get(True))  # YAML 1.1: a bare `on` is True
    budget = triggers["workflow_call"]["inputs"]["route_budget_usd"]["default"]
    assert float(budget) == ROUTE_DEFAULT_BUDGET_USD


def test_doctor_routable_set_is_the_route_executor_cli_decision(tmp_path):
    from agentic_sdlc.cli import main
    from agentic_sdlc.onboard import (
        ROUTE_DEFAULT_BUDGET_USD,
        ROUTE_MIN_CONTEXT_WINDOW,
        ROUTE_MISSION_ID,
        ROUTE_TASK_CLASS,
        ROUTE_TOOL_CAPABILITIES,
        implementation_route_request,
        route_candidates,
    )

    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    out = tmp_path / "route.json"
    args = ["route-executor", "--config", str(repo / "agentic-sdlc.toml")]
    args += ["--mission-id", ROUTE_MISSION_ID, "--executors", str(repo / ".forge/executors.json")]
    args += ["--routing-policy", str(repo / ".forge/routing-policy.json")]
    args += ["--repository", "owner/comic", "--task-class", ROUTE_TASK_CLASS.value]
    for tool in ROUTE_TOOL_CAPABILITIES:
        args += ["--required-tool-capability", tool]
    args += ["--budget-usd", str(ROUTE_DEFAULT_BUDGET_USD)]
    args += ["--min-context-window", str(ROUTE_MIN_CONTEXT_WINDOW), "--output", str(out)]
    main(args)
    candidates = json.loads(out.read_text())["candidates"]
    cli_eligible = {c["executorId"] for c in candidates if c["eligible"]}
    request = implementation_route_request(load_policy(repo / "agentic-sdlc.toml"), "owner/comic")
    verdict = route_candidates(
        load_executors(json.loads((repo / ".forge/executors.json").read_text())),
        load_routing_policy(json.loads((repo / ".forge/routing-policy.json").read_text())),
        request,
    )
    assert {eid for eid, reasons in verdict.items() if not reasons} == cli_eligible
    assert "deepseek-v4-flash" not in cli_eligible  # 0.76 < the medium floor 0.82


def test_doctor_honours_a_caller_route_budget(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    for name in ("agent-implement.yml", "agent-auto-implement.yml"):
        path = repo / ".github/workflows" / name
        doc = yaml.safe_load(path.read_text())
        doc["jobs"]["implement"]["with"]["route_budget_usd"] = "0.1"
        path.write_text(yaml.safe_dump(doc, sort_keys=False))
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    assert "budget exceeded" in failed["routing files valid and permit this repo"].detail


def test_onboarding_doc_lists_every_default_credential(tmp_path):
    """Every secret and variable doctor demands of a freshly onboarded repository, in every
    implementer mode, is named in docs/onboarding.md."""
    doc = (Path(__file__).resolve().parents[1] / "docs/onboarding.md").read_text()
    gh = _healthy_gh()
    gh.answers["api repos/owner/comic/actions/secrets"] = _secret_pages()
    gh.answers["api repos/owner/comic/actions/variables"] = _var_pages()
    gh.answers["api repos/owner/agentic-sdlc --jq .private"] = "true"
    for index, implementer in enumerate(("route", "claude", "codex", "cloud-routine")):
        repo = _repo(tmp_path / str(index))
        write_onboarding(repo, spec(implementer=implementer))
        failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))
        names = set()
        for check in ("repo secrets set (by name)", "repo variables set (non-empty)"):
            if check in failed:
                detail = failed[check].detail.split("→")[0]
                names |= set(re.findall(r"\b[A-Z][A-Z0-9_]{3,}\b", detail))
        assert names, implementer
        missing = sorted(n for n in names if f"`{n}`" not in doc)
        assert not missing, (implementer, missing)


def test_commit_guard_consumes_the_values_of_wrapper_options(tmp_path):
    guard = _guard_module(tmp_path, "forge_commit_guard")

    def code(command):
        payload = {"tool_name": "Bash", "session_id": "s1", "tool_input": {"command": command}}
        return guard.decide(payload, "forge/issue-7", lambda n: None, lambda b: False)[0]

    for blocked in (
        "env -u HOME git commit -m x",
        "env --unset HOME git commit -m x",
        "env --unset=HOME git commit -m x",
        "env -i -u HOME A=1 git commit -m x",
        "env -C /tmp git commit -m x",
        "env -S 'git commit -m x'",
        "sudo -u bob git commit -m x",
        "sudo -Eu bob git commit -m x",
        "sudo -g staff -u bob -- git commit -m x",
        "sudo --user=bob git commit -m x",
        "nice -n 5 git commit -m x",
        "nice -n5 git commit -m x",
        "timeout 10 git commit -m x",
        "timeout -s KILL -k 5 10 git commit -m x",
        "stdbuf -o L git commit -m x",
        "xargs -n 1 git commit -m",
        "doas -u bob git commit -m x",
        "nohup nice -n 5 env -u HOME git commit -m x",
    ):
        assert code(blocked) == 2, blocked
    for allowed in ("env -u HOME git status", "timeout 10 git log", "sudo -u bob ls"):
        assert code(allowed) == 0, allowed


def _git(cwd, *args):
    import subprocess

    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def test_commit_guard_checks_the_repository_git_c_selects(tmp_path):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    main_repo, issue_repo = tmp_path / "main", tmp_path / "wt"
    for path, branch in ((main_repo, "main"), (issue_repo, "forge/issue-7")):
        path.mkdir()
        _git(path, "init", "-q", "-b", branch)
        _git(path, "commit", "-q", "--allow-empty", "-m", "init")
    payload = {"tool_name": "Bash", "session_id": "s1"}

    def code(command, cwd=main_repo):
        branches = guard.commit_branches(command, str(cwd))
        return guard.decide(
            {**payload, "tool_input": {"command": command}},
            branches,
            lambda n: None,
            lambda b: False,
        )[0]

    assert code("git commit -m x") == 0  # main carries no issue
    for blocked in (
        "git -C ../wt commit -m x",
        f"git -C {issue_repo} commit -m x",
        "git -C .. -C wt commit -m x",
        "git --git-dir=../wt/.git --work-tree=../wt commit -m x",
        "GIT_DIR=../wt/.git git commit -m x",
        "cd ../wt && git commit -m x",
        "env -C ../wt git commit -m x",
        "bash -c 'git -C ../wt commit -m x'",
    ):
        assert code(blocked) == 2, blocked
    # -C elsewhere commits there, not to the issue branch the hook runs on
    assert code("git -C ../main commit -m x", cwd=issue_repo) == 0
    assert code("git commit -m x", cwd=issue_repo) == 2
    assert code("git -C ../wt status") == 0


# ---------------------------------------------------------------- review regressions (PR 139, 5)


def test_ruleset_update_merges_forge_rules_without_weakening_existing_ones():
    from agentic_sdlc.onboard import merged_ruleset

    strong = {
        "id": 5,
        "name": RULESET_NAME,
        "source_type": "Repository",
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": [{"actor_id": 1, "actor_type": "RepositoryRole"}],
        "conditions": {"ref_name": {"include": ["refs/heads/release"], "exclude": []}},
        "rules": [
            {"type": "deletion"},
            {"type": "required_signatures"},
            {"type": "required_linear_history"},
            {
                "type": "pull_request",
                "parameters": {
                    "required_approving_review_count": 2,
                    "dismiss_stale_reviews_on_push": False,
                    "require_code_owner_review": True,
                    "require_last_push_approval": True,
                    "required_review_thread_resolution": False,
                    "allowed_merge_methods": ["squash"],
                },
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": False,
                    "required_status_checks": [
                        {"context": "lint"},
                        {"context": "test", "integration_id": 15368},
                        {"context": "e2e"},
                    ],
                },
            },
        ],
    }
    assert ruleset_mismatches(strong, spec())  # it lacks Forge's requirements
    gh = FakeGh(
        {
            "api repos/owner/comic/rulesets/5": json.dumps(strong),
            "api repos/owner/comic/rulesets": json.dumps([strong]),
        }
    )
    apply_repo_settings(spec(), gh)
    body = json.loads(next(i for c, i in gh.calls if c[:3] == ("api", "-X", "PUT")))
    assert body == merged_ruleset(strong, spec())
    assert "id" not in body and "source_type" not in body
    assert ruleset_mismatches(body, spec()) == []
    rules = {r["type"]: r.get("parameters") for r in body["rules"]}
    # every existing rule survives, and Forge's missing one is added
    assert {"required_signatures", "required_linear_history", "non_fast_forward"} <= set(rules)
    pr = rules["pull_request"]
    assert pr["required_approving_review_count"] == 2  # never lowered to Forge's 0
    assert pr["require_code_owner_review"] is True and pr["require_last_push_approval"] is True
    assert pr["required_review_thread_resolution"] is True
    assert pr["dismiss_stale_reviews_on_push"] is True
    assert pr["allowed_merge_methods"] == ["squash"]
    checks = rules["required_status_checks"]
    assert checks["strict_required_status_checks_policy"] is True
    assert checks["required_status_checks"] == [
        {"context": "lint"},
        {"context": "test", "integration_id": 15368},
        {"context": "e2e"},
    ]
    include = body["conditions"]["ref_name"]["include"]
    assert include == ["refs/heads/release", "~DEFAULT_BRANCH"]
    assert body["bypass_actors"] == []
    # a stricter Forge-free count on a fresh merge never drops below the existing one
    assert merged_ruleset(body, spec()) == body


def test_ruleset_rejects_and_unbinds_a_foreign_integration_on_the_test_check():
    """A `test` context pinned to another app's integration_id can never be satisfied by ci.yml
    (Codex review 4204434963): doctor reports it, and the merge drops the binding."""
    payload = ruleset_payload(spec())
    foreign = json.loads(json.dumps(payload))
    checks = foreign["rules"][3]["parameters"]["required_status_checks"]
    checks[:] = [
        {"context": "test", "integration_id": 99999},
        {"context": "test"},
        {"context": "lint", "integration_id": 4242},
    ]
    problems = ruleset_mismatches(foreign, spec())
    assert any("integration 99999" in p for p in problems), problems
    merged = merged_ruleset(foreign, spec())
    kept = next(r for r in merged["rules"] if r["type"] == "required_status_checks")
    assert kept["parameters"]["required_status_checks"] == [
        {"context": "test"},
        {"context": "lint", "integration_id": 4242},  # not Forge's context: untouched
    ]
    assert ruleset_mismatches(merged, spec()) == []
    actions = json.loads(json.dumps(payload))
    actions["rules"][3]["parameters"]["required_status_checks"] = [
        {"context": "test", "integration_id": 15368}
    ]
    assert ruleset_mismatches(actions, spec()) == []


def _wf(repo: Path, name: str, text: str) -> None:
    (repo / ".github/workflows" / name).write_text(text)


REUSE_SELF_HOSTED = (
    "on:\n  workflow_call:\njobs:\n  build:\n    runs-on: [self-hosted, linux]\n"
    "    steps:\n      - run: make test\n"
)
REUSE_INPUT = (
    "on:\n  workflow_call:\n    inputs:\n      runner:\n        type: string\n"
    "        default: RUNNER_DEFAULT\njobs:\n  build:\n    runs-on: ${{ inputs.runner }}\n"
    "    steps:\n      - run: make test\n"
)


CALL_ONLY = "on:\n  workflow_call:\njobs:\n  go:\n    uses: ./.github/workflows/{}.yml\n"


def _pr_caller(uses: str, with_: str = "") -> str:
    return f"on: pull_request\njobs:\n  call:\n    uses: {uses}\n{with_}"


@pytest.mark.parametrize(
    ("files", "exposed"),
    [
        # the Codex case: no with.runs_on, the called file's job is self-hosted
        (
            {"reuse.yml": REUSE_SELF_HOSTED, "pr.yml": _pr_caller("./.github/workflows/reuse.yml")},
            True,
        ),
        # inputs substituted from `with:`
        (
            {
                "reuse.yml": REUSE_INPUT.replace("RUNNER_DEFAULT", "self-hosted"),
                "pr.yml": _pr_caller(
                    "./.github/workflows/reuse.yml", "    with:\n      runner: ubuntu-latest\n"
                ),
            },
            False,
        ),
        (
            {
                "reuse.yml": REUSE_INPUT.replace("RUNNER_DEFAULT", "ubuntu-latest"),
                "pr.yml": _pr_caller(
                    "./.github/workflows/reuse.yml", "    with:\n      runner: self-hosted\n"
                ),
            },
            True,
        ),
        # the input's default applies when the caller passes none
        (
            {
                "reuse.yml": REUSE_INPUT.replace("RUNNER_DEFAULT", "self-hosted"),
                "pr.yml": _pr_caller("./.github/workflows/reuse.yml"),
            },
            True,
        ),
        (
            {
                "reuse.yml": REUSE_INPUT.replace("RUNNER_DEFAULT", "ubuntu-latest"),
                "pr.yml": _pr_caller("./.github/workflows/reuse.yml"),
            },
            False,
        ),
        # an expression that is not a resolvable input fails closed
        (
            {
                "reuse.yml": REUSE_INPUT.replace("RUNNER_DEFAULT", "ubuntu-latest"),
                "pr.yml": _pr_caller(
                    "./.github/workflows/reuse.yml", "    with:\n      runner: ${{ vars.RUNNER }}\n"
                ),
            },
            True,
        ),
        # nested local calls are followed
        (
            {
                "inner.yml": REUSE_SELF_HOSTED,
                "outer.yml": "on:\n  workflow_call:\njobs:\n  go:\n"
                "    uses: ./.github/workflows/inner.yml\n",
                "pr.yml": _pr_caller("./.github/workflows/outer.yml"),
            },
            True,
        ),
        # a cycle cannot be proven and must not recurse forever
        (
            {
                "a.yml": CALL_ONLY.format("b"),
                "b.yml": CALL_ONLY.format("a"),
                "pr.yml": _pr_caller("./.github/workflows/a.yml"),
            },
            True,
        ),
        ({"pr.yml": _pr_caller("./.github/workflows/missing.yml")}, True),
        # a third-party reusable workflow's runners are unknowable from here
        ({"pr.yml": _pr_caller(f"someone/else/.github/workflows/ci.yml@{SHA}")}, True),
        # the Forge platform's reusable workflows run where with.runs_on says
        (
            {
                "pr.yml": _pr_caller(
                    f"owner/agentic-sdlc/.github/workflows/reusable-review.yml@{SHA}",
                    "    with:\n      runs_on: '[\"ubuntu-latest\"]'\n",
                )
            },
            False,
        ),
        (
            {
                "pr.yml": _pr_caller(
                    f"owner/agentic-sdlc/.github/workflows/reusable-review.yml@{SHA}",
                    "    with:\n      runs_on: '[\"self-hosted\"]'\n",
                )
            },
            True,
        ),
        (
            {
                "pr.yml": _pr_caller(
                    f"owner/agentic-sdlc/.github/workflows/reusable-review.yml@{SHA}"
                )
            },
            True,
        ),
        (
            {
                "pr.yml": _pr_caller(
                    "owner/agentic-sdlc/.github/workflows/reusable-review.yml@main",
                    "    with:\n      runs_on: '[\"ubuntu-latest\"]'\n",
                )
            },
            True,
        ),
    ],
)
def test_public_runner_safety_follows_reusable_workflows(tmp_path, files, exposed):
    from agentic_sdlc.onboard import _pull_request_off_hosted

    repo = _repo(tmp_path)
    write_onboarding(repo, spec(public=True))
    for name, text in files.items():
        _wf(repo, name, text)
    found = _pull_request_off_hosted(repo, "owner/agentic-sdlc")
    assert bool(found) is exposed, found
    assert not any(f.startswith("ci.yml:") for f in found)  # the hosted CI stays clean


def test_doctor_fails_a_public_repo_whose_pr_workflow_calls_a_self_hosted_reusable(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec(public=True))
    _wf(repo, "reuse.yml", REUSE_SELF_HOSTED)
    _wf(repo, "pr.yml", _pr_caller("./.github/workflows/reuse.yml"))
    gh = _healthy_gh()
    gh.answers["api repos/owner/comic"] = json.dumps(
        {"default_branch": "main", "visibility": "public"}
    )
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))
    check = failed["public repository runs pull requests on GitHub-hosted runners"]
    assert "pr.yml:call > reuse.yml:build" in check.detail


def test_platform_runs_on_workflows_run_every_job_on_the_runs_on_input():
    from agentic_sdlc.onboard import PLATFORM_RUNS_ON_WORKFLOWS

    workflows = Path(__file__).resolve().parents[1] / ".github/workflows"
    pinned = set()
    for path in workflows.glob("reusable-*.yml"):
        jobs = yaml.safe_load(path.read_text())["jobs"].values()
        if all(job.get("runs-on") == "${{ fromJSON(inputs.runs_on) }}" for job in jobs):
            pinned.add(path.name)
    assert pinned >= PLATFORM_RUNS_ON_WORKFLOWS


@pytest.mark.parametrize(
    "suffix",
    [
        "--help",
        "-h",
        "--version",
        "--collect-only",
        "--co",
        "--setup-plan",
        "-k nothing",
        "--ignore tests",
        "--exit-zero",
    ],
)
def test_doctor_rejects_a_ci_gate_whose_extra_arguments_change_what_runs(tmp_path, suffix):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    _ci_test_step(repo, f"pytest tests -q -m 'not e2e' {suffix}")
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False)
    assert "ci.yml runs the policy gates as 'test'" in _failed(report)


@pytest.mark.parametrize("suffix", ["", "-x", "--maxfail=3 -vv", "--tb=short -rA --durations=5"])
def test_doctor_accepts_a_ci_gate_with_only_output_or_fail_fast_extras(tmp_path, suffix):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    _ci_test_step(repo, f"pytest tests -q -m 'not e2e' {suffix}".strip())
    if suffix:
        _assert_only_template_drift(repo)
    else:
        report = doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False)
        assert "ci.yml runs the policy gates as 'test'" not in _failed(report)


@pytest.mark.parametrize(
    "overrides",
    [
        {"runs_on": ("windows-2025",)},
        {"runs_on": ("windows-latest",)},
        {"runs_on": ("self-hosted", "windows", "x64")},
        {"ci_runs_on": ("windows-11-arm",)},
        {"public": True, "ci_runs_on": ("windows-latest",)},
    ],
)
def test_onboard_rejects_windows_runner_targets(overrides):
    with pytest.raises(OnboardError, match="Windows runners are not supported"):
        spec(**overrides)


def test_doctor_rejects_windows_runner_targets(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    ci = repo / ".github/workflows/ci.yml"
    doc = yaml.safe_load(ci.read_text())
    doc["jobs"]["test"]["runs-on"] = "windows-2025"
    ci.write_text(yaml.safe_dump(doc, sort_keys=False))
    plan = repo / ".github/workflows/agent-plan.yml"
    doc = yaml.safe_load(plan.read_text())
    doc["jobs"]["preflight"]["runs-on"] = "windows-latest"
    plan.write_text(yaml.safe_dump(doc, sort_keys=False))
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", _healthy_gh()))
    assert set(failed) == {
        "ci.yml test job runs on an available runner",
        "workflows target Unix runners",
    }
    assert all("Windows" in c.detail and not c.manual for c in failed.values())
    assert "agent-plan.yml:preflight" in failed["workflows target Unix runners"].detail


def test_hosted_labels_are_the_documented_finite_set():
    from agentic_sdlc.onboard import GITHUB_HOSTED_LABELS, _github_hosted

    for label in ("ubuntu-latest", "ubuntu-22.04-arm", "macos-15-intel", "macos-14-large"):
        assert _github_hosted({label}), label
    for label in ("ubuntu-99.04", "windows-2099", "macos-99", "macos-15-huge", "ubuntu-20.10"):
        assert not _github_hosted({label}), label
    assert {f for f in GITHUB_HOSTED_LABELS.values()} == {"linux", "macos", "windows"}


HOOK_START = ".claude/hooks/forge_session_start.py"


def _settings(repo: Path, data) -> None:
    (repo / ".claude/settings.json").write_text(data if isinstance(data, str) else json.dumps(data))


def _hook(script: str, command: str | None = None) -> dict:
    return {"type": "command", "command": command or f"python3 .claude/hooks/{script}"}


@pytest.mark.parametrize(
    "settings",
    [
        # both names merely mentioned
        {"note": "forge_session_start.py and forge_commit_guard.py run as hooks"},
        "not json at all forge_session_start.py forge_commit_guard.py",
        # hooks disabled outright
        {
            "disableAllHooks": True,
            "hooks": {
                "SessionStart": [{"hooks": [_hook("forge_session_start.py")]}],
                "PreToolUse": [{"matcher": "Bash", "hooks": [_hook("forge_commit_guard.py")]}],
            },
        },
        # guard under a matcher that never sees Bash
        {
            "hooks": {
                "SessionStart": [{"hooks": [_hook("forge_session_start.py")]}],
                "PreToolUse": [{"matcher": "Edit", "hooks": [_hook("forge_commit_guard.py")]}],
            }
        },
        # scripts swapped between events
        {
            "hooks": {
                "SessionStart": [{"hooks": [_hook("forge_commit_guard.py")]}],
                "PreToolUse": [{"matcher": "Bash", "hooks": [_hook("forge_session_start.py")]}],
            }
        },
        # a command that only echoes the path
        {
            "hooks": {
                "SessionStart": [
                    {"hooks": [_hook("x", "echo .claude/hooks/forge_session_start.py")]}
                ],
                "PreToolUse": [{"matcher": "Bash", "hooks": [_hook("forge_commit_guard.py")]}],
            }
        },
        # SessionStart only on resume
        {
            "hooks": {
                "SessionStart": [{"matcher": "resume", "hooks": [_hook("forge_session_start.py")]}],
                "PreToolUse": [{"matcher": "Bash", "hooks": [_hook("forge_commit_guard.py")]}],
            }
        },
    ],
)
def test_doctor_rejects_hooks_claude_code_would_not_run(tmp_path, settings):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    _settings(repo, settings)
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False)
    assert "required files present" in _failed(report)


def test_doctor_accepts_equivalent_hook_configurations(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    _settings(
        repo,
        {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "startup|resume",
                        "hooks": [_hook("x", 'python3 "$CLAUDE_PROJECT_DIR"/' + HOOK_START)],
                    }
                ],
                "PreToolUse": [
                    {"matcher": "Edit", "hooks": [_hook("other.py")]},
                    {"matcher": "Bash|Edit", "hooks": [_hook("forge_commit_guard.py")]},
                ],
            }
        },
    )
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False)
    assert "required files present" not in _failed(report)


def test_onboarding_records_the_implementation_mode_in_the_policy():
    import tomllib

    for implementer, mode in (("route", "actions"), ("cloud-routine", "cloud-routine")):
        files = render_onboarding(spec(implementer=implementer, runs_on=("ubuntu-latest",)))
        agents = tomllib.loads(files["agentic-sdlc.toml"])["agents"]
        assert agents["implementation_mode"] == mode


def test_doctor_does_not_infer_cloud_mode_from_a_stale_routine_doc(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    for name in ("agent-implement.yml", "agent-auto-implement.yml"):
        (repo / ".github/workflows" / name).unlink()
    (repo / "docs/forge").mkdir(parents=True)
    (repo / "docs/forge/cloud-implementer.md").write_text("# stale\n")
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    assert "agent-implement.yml" in failed["required files present"].detail
    assert "implementation profile is unambiguous" in failed


def test_doctor_fails_a_legacy_policy_without_mode_or_callers(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec(implementer="cloud-routine", runs_on=("ubuntu-latest",)))
    toml = repo / "agentic-sdlc.toml"
    legacy = re.sub(r"(?m)^implementation_mode = .*\n", "", toml.read_text())
    toml.write_text(legacy)
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    assert "implementation_mode" in failed["implementation mode is configured"].detail
    # a legacy Actions repo keeps working: its callers are the mode
    repo2 = _repo(tmp_path / "two")
    write_onboarding(repo2, spec())
    toml2 = repo2 / "agentic-sdlc.toml"
    toml2.write_text(re.sub(r"(?m)^implementation_mode = .*\n", "", toml2.read_text()))
    report = doctor(repo2, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False)
    assert "implementation mode is configured" not in _failed(report)
    # an unknown value is not guessed at
    toml2.write_text(
        toml2.read_text().replace(
            'reviewer = "codex"', 'reviewer = "codex"\nimplementation_mode = "cloud"'
        )
    )
    failed = _failed(doctor(repo2, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    assert "'cloud'" in failed["implementation mode is configured"].detail


@pytest.mark.parametrize(
    "permissions",
    [
        {"contents": "write", "issues": "write", "pull_requests": "write"},
        {**PUBLISHER_PERMS, "workflows": "write"},
        {**PUBLISHER_PERMS, "administration": "read"},
        {**PUBLISHER_PERMS, "metadata": "write"},
        {"issues": "write", "pull_requests": "write"},
    ],
)
def test_doctor_rejects_a_publisher_app_outside_the_exact_permission_set(tmp_path, permissions):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _healthy_gh()
    gh.answers["api /user/installations"] = _installs(7, permissions=permissions)
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))
    assert set(failed) == {"Publisher GitHub App installed on this repo"}


def test_doctor_accepts_the_publisher_app_with_its_mandatory_metadata_permission(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _healthy_gh()
    gh.answers["api /user/installations"] = _installs(
        7, permissions={**PUBLISHER_PERMS, "metadata": "read"}
    )
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    assert report.ok, report.render()


# ---------------------------------------------------------------- review regressions (PR 139, 6)


@pytest.mark.parametrize(
    "command",
    [
        "if true; then git commit -m x; fi",
        "while true; do git commit -m x; done",
        "until false; do git commit; done",
        "! git commit -m x",
        "{ git commit -m x; }",
        "( git commit -m x )",
        "time git commit -m x",
        "if git commit -m x; then :; fi",
        "for i in 1; do git -C . commit -m x; done",
        "case a in a) git commit;; esac",
        "f() { git commit -m x; }; f",
        "eval git commit -m x",
        "unknownwrapper --flag git commit",
    ],
)
def test_commit_guard_sees_commits_under_shell_control_flow(tmp_path, command):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    assert guard.is_git_commit(command)


@pytest.mark.parametrize(
    "command",
    ["git log --grep commit", "git status && echo commit", "echo hi", "if true; then :; fi"],
)
def test_commit_guard_keeps_classified_non_commits(tmp_path, command):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    assert not guard.is_git_commit(command)


@pytest.mark.parametrize(
    "command",
    [
        "bash -lc 'git commit -m x'",
        "sh -ec 'git commit -m x'",
        "zsh -ic 'git -C . commit'",
        "dash -xec 'git commit'",
        "bash -o pipefail -c 'git commit -m x'",
        "bash -c -e 'git commit -m x'",
        "bash --norc -lc 'git commit'",
        "/bin/sh -euc 'cd sub && git commit -am x'",
    ],
)
def test_commit_guard_reads_clustered_shell_c_options(tmp_path, command):
    """`bash -lc CMD`: the shell's options cluster, and any cluster holding `c` makes the first
    operand the command string (Codex review 4204434919)."""
    guard = _guard_module(tmp_path, "forge_commit_guard")
    assert guard.is_git_commit(command)


@pytest.mark.parametrize(
    "command",
    ["bash -lc 'git status'", "sh -e script.sh", "bash -x run-commit.sh", "bash -l"],
)
def test_commit_guard_clustered_shell_options_without_a_commit(tmp_path, command):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    assert not guard.is_git_commit(command)


@pytest.mark.parametrize("name", ["agent-auto-plan.yml", "agent-auto-implement.yml"])
def test_doctor_requires_the_automatic_callers_to_trigger_on_labeled_issues(tmp_path, name):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    path = repo / ".github/workflows" / name
    path.write_text(
        path.read_text().replace("  issues:\n    types: [labeled]\n", "  workflow_dispatch:\n")
    )
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    assert name in failed["automatic callers trigger on issue labels"].detail
    path.write_text(path.read_text().replace("  workflow_dispatch:\n", "  issues:\n"))
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    assert "automatic callers trigger on issue labels" not in failed  # all types include labeled


def _mutate_ci_test_step(repo, mutate):
    ci = repo / ".github/workflows/ci.yml"
    doc = yaml.safe_load(ci.read_text())
    job = doc["jobs"]["test"]
    mutate(doc, job, next(s for s in job["steps"] if s.get("name") == "Test"))
    ci.write_text(yaml.safe_dump(doc, sort_keys=False))


@pytest.mark.parametrize(
    "mutate",
    [
        # an uncalled function only DEFINES the gate
        lambda doc, job, step: step.update(
            {"run": "run_tests() {\n  " + step["run"] + "\n}\necho done"}
        ),
        lambda doc, job, step: step.update({"run": "function t {\n" + step["run"] + "\n}"}),
        lambda doc, job, step: step.update({"run": "t() { " + step["run"] + "; }"}),
        # flags injected through the environment
        lambda doc, job, step: step.update({"env": {"PYTEST_ADDOPTS": "--collect-only"}}),
        lambda doc, job, step: job.update({"env": {"PYTEST_ADDOPTS": "--co"}}),
        lambda doc, job, step: doc.update({"env": {"PYTEST_ADDOPTS": "--co"}}),
        lambda doc, job, step: step.update({"run": "PYTEST_ADDOPTS=--co " + step["run"]}),
        lambda doc, job, step: step.update({"run": "export PYTEST_ADDOPTS=--co\n" + step["run"]}),
        lambda doc, job, step: step.update({"env": {"BASH_ENV": "./evil.sh"}}),
        lambda doc, job, step: job["steps"].insert(
            0, {"run": 'echo "PYTEST_ADDOPTS=--co" >> "$GITHUB_ENV"'}
        ),
        lambda doc, job, step: step.update({"run": "pytest() { :; }\n" + step["run"]}),
        lambda doc, job, step: step.update({"run": "source ./env.sh\n" + step["run"]}),
        lambda doc, job, step: job["steps"].insert(0, {"uses": "someone/export-env@v1"}),
    ],
)
def test_doctor_rejects_a_gate_whose_meaning_is_changed(tmp_path, mutate):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    _mutate_ci_test_step(repo, mutate)
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    assert "ci.yml runs the policy gates as 'test'" in failed


@pytest.mark.parametrize(
    "mutate",
    [
        lambda doc, job, step: step.update({"env": {"PYTHONUNBUFFERED": "1", "CI": "true"}}),
        lambda doc, job, step: step.update({"run": "source .venv/bin/activate\n" + step["run"]}),
        lambda doc, job, step: job["steps"].insert(1, {"uses": "actions/cache@v4"}),
        lambda doc, job, step: step.update({"run": "helper() { echo hi; }\n" + step["run"]}),
    ],
)
def test_doctor_accepts_a_gate_with_benign_surroundings(tmp_path, mutate):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    _mutate_ci_test_step(repo, mutate)
    _assert_only_template_drift(repo)


@pytest.mark.parametrize("budget", ["bogus", "${{ vars.ROUTE_BUDGET }}", True, "nan", "-1"])
def test_doctor_fails_an_unreadable_route_budget(tmp_path, budget):
    repo = _routed_repo(tmp_path)
    path = repo / ".github/workflows/agent-implement.yml"
    doc = yaml.safe_load(path.read_text())
    doc["jobs"]["implement"]["with"]["route_budget_usd"] = budget
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    assert "agent-implement.yml:implement" in (
        failed["implement callers pass a readable route_budget_usd"].detail
    )


def test_doctor_judges_secret_forwarding_on_the_implement_call_alone(tmp_path):
    api = _extra_executor(executorId="claude-api", authMode="api-key", executionType="direct-api")
    repo = _routed_repo(tmp_path, api)
    path = repo / ".github/workflows/agent-implement.yml"
    doc = yaml.safe_load(path.read_text())
    del doc["jobs"]["implement"]["secrets"]["ANTHROPIC_API_KEY"]
    # an unrelated reusable call forwarding everything reaches nothing in the implement call
    doc["jobs"]["other"] = {"uses": "owner/elsewhere/.github/workflows/x.yml@main"}
    doc["jobs"]["other"]["secrets"] = "inherit"
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    assert "agent-implement.yml:implement: ANTHROPIC_API_KEY" in (
        failed["implement callers forward every routed secret"].detail
    )
    # `secrets: inherit` on the implement call itself forwards them all
    doc["jobs"]["implement"]["secrets"] = "inherit"
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    assert "implement callers forward every routed secret" not in failed


def test_doctor_excludes_a_matching_runner_whose_os_is_windows(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _healthy_gh()
    gh.answers["api repos/owner/comic/actions/runners"] = _pages(
        "runners",
        [
            {
                "status": "online",
                "os": "Windows",
                "labels": [{"name": n} for n in ("self-hosted", "linux", "x64")],
            }
        ],
    )
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))
    assert "self-hosted runner online for this repo" in failed
    assert "ci.yml test job runs on an available runner" in failed


def test_doctor_fails_closed_on_a_runner_group(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    path = repo / ".github/workflows/agent-plan.yml"
    doc = yaml.safe_load(path.read_text())
    doc["jobs"]["preflight"]["runs-on"] = {
        "group": "builders",
        "labels": ["self-hosted", "linux", "x64"],
    }
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", _healthy_gh()))
    detail = failed["workflow runner targets are verifiable"].detail
    assert "agent-plan.yml:preflight" in detail and "runner group 'builders'" in detail


def test_doctor_follows_local_reusable_workflows_for_runner_availability(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    (repo / ".github/workflows/helper.yml").write_text(
        "on:\n  workflow_call:\njobs:\n  gpu:\n    runs-on: [self-hosted, gpu]\n"
        "    steps:\n      - run: echo hi\n"
    )
    path = repo / ".github/workflows/agent-plan.yml"
    doc = yaml.safe_load(path.read_text())
    doc["jobs"]["extra"] = {"uses": "./.github/workflows/helper.yml"}
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", _healthy_gh()))
    assert "agent-plan.yml:extra > helper.yml:gpu" in (
        failed["self-hosted runner online for this repo"].detail
    )


@pytest.mark.parametrize(
    "condition",
    [
        lambda c: c + " || true",
        lambda c: "true || " + c,
        lambda c: c.replace("'human-review-required'", "'other'"),
    ],
)
def test_doctor_requires_the_exact_label_conditions(tmp_path, condition):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    for name, job in (("agent-auto-plan.yml", "plan"), ("agent-auto-implement.yml", "preflight")):
        path = repo / ".github/workflows" / name
        doc = yaml.safe_load(path.read_text())
        original = doc["jobs"][job]["if"]
        doc["jobs"][job]["if"] = condition(original)
        assert doc["jobs"][job]["if"] != original
        path.write_text(yaml.safe_dump(doc, sort_keys=False))
        failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
        assert name in failed["workflow label conditions match the policy labels"].detail
        doc["jobs"][job]["if"] = original
        path.write_text(yaml.safe_dump(doc, sort_keys=False))


def test_doctor_reads_the_parsed_agent_of_the_implement_call(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec(implementer="codex"))
    gh = _healthy_gh()
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))
    assert "OPENAI_API_KEY" in failed["repo secrets set (by name)"].detail
    path = repo / ".github/workflows/agent-implement.yml"
    doc = yaml.safe_load(path.read_text())
    doc["jobs"]["implement"]["with"]["agent"] = "${{ vars.AGENT }}"
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    assert "agent-implement.yml:implement" in failed["implement callers name a known agent"].detail


def test_route_mode_comes_from_the_parsed_policy_not_its_text(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec(implementer="claude"))
    toml = repo / "agentic-sdlc.toml"
    toml.write_text(toml.read_text() + "\n# [routing] is documented elsewhere\n")
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False)
    assert "routing files valid and permit this repo" not in {c.name for c in report.checks}


def test_reusable_implement_agent_default_matches_doctor():
    root = Path(__file__).parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/reusable-implement.yml").read_text())
    inputs = workflow[True]["workflow_call"]["inputs"]
    assert inputs["agent"]["default"] == REUSABLE_IMPLEMENT_DEFAULT_AGENT
    text = (root / ".github/workflows/reusable-implement.yml").read_text()
    assert "codex|route) ;;" in text and "claude)" in text
    assert {"codex", "route", "claude"} == REUSABLE_IMPLEMENT_AGENTS


@pytest.mark.parametrize("value", ["true", "null", "on", "off", "yes", "123", "0x1F", "1e3", "007"])
def test_every_template_placeholder_round_trips_as_a_string(value):
    rendered = render_onboarding(
        spec(
            default_branch=value,
            runs_on=("self-hosted", "linux", value),
            ci_runs_on=(value,) if value != "true" else ("ubuntu-latest",),
            ready_label=f"{value}'s",
            setup_command=value,
            quality_command=value,
            test_command=value,
        )
    )
    workflows = {k: v for k, v in rendered.items() if k.startswith(".github/workflows/")}
    assert workflows
    placeholders = re.compile(
        r"\b(PLATFORM_REPOSITORY|PLATFORM_COMMIT_SHA|REUSABLE_\w+_USES|RUNS_ON_\w+|AGENT|"
        r"EXECUTOR_REGISTRY_PATH|ROUTING_POLICY_PATH|DEFAULT_BRANCH|\w+_COMMAND|AUTO_\w+_IF|"
        r"ISSUE_NUMBER_EXPR|READY_LABEL|IMPLEMENTATION_LABEL|IMPLEMENT_JOBS|PREFLIGHT_EXTRA)\b"
    )
    for name, text in workflows.items():
        assert not placeholders.search(text), name
        doc = yaml.safe_load(text)
        for job in doc["jobs"].values():
            for key in ("runs-on",):
                if key in job:
                    assert all(isinstance(x, str) for x in job[key]), (name, job[key])
            passed = job.get("with") or {}
            for key in ("runs_on", "platform_ref", "platform_repository", "agent"):
                if key in passed:
                    assert isinstance(passed[key], str), (name, key)
            if "runs_on" in passed:
                assert json.loads(passed["runs_on"]) == ["self-hosted", "linux", value]
            for step in job.get("steps") or []:
                if "run" in step:
                    assert isinstance(step["run"], str)
    ci = yaml.safe_load(workflows[".github/workflows/ci.yml"])
    assert ci[True]["push"]["branches"] == [value]
    steps = {s.get("name"): s for s in ci["jobs"]["test"]["steps"]}
    assert steps["Test"]["run"] == value
    auto = yaml.safe_load(workflows[".github/workflows/agent-auto-implement.yml"])
    assert f"'{value}''s'" in auto["jobs"]["preflight"]["if"]
    hooks = rendered[".claude/hooks/forge_session_start.py"]
    assert f"DEFAULT_BRANCH = {json.dumps(value)}" in hooks


# ---------------------------------------------------------------- managed workflow structure
# One structural comparison against the rendered template replaces a check per field: Codex kept
# finding one more field of the generated callers doctor did not read.

DRIFT = "managed workflows match the generated templates"


def _edit_workflow(repo: Path, name: str, change) -> None:
    path = repo / ".github/workflows" / name
    doc = yaml.safe_load(path.read_text())
    change(doc)
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


def _drift(repo: Path) -> str | None:
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    return failed[DRIFT].detail if DRIFT in failed else None


def test_doctor_accepts_the_generated_workflows_unchanged(tmp_path):
    for implementer in ("route", "claude", "codex", "cloud-routine"):
        repo = _repo(tmp_path / implementer)
        write_onboarding(repo, spec(implementer=implementer, runs_on=("ubuntu-latest",)))
        assert _drift(repo) is None, implementer


def test_doctor_tolerates_only_the_tunable_knobs(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())

    def runners(doc):
        for job in doc["jobs"].values():
            if "runs-on" in job:
                job["runs-on"] = "ubuntu-24.04"
            if "runs_on" in (job.get("with") or {}):
                job["with"]["runs_on"] = '["ubuntu-24.04"]'

    for name in (
        "agent-plan.yml",
        "agent-auto-plan.yml",
        "agent-implement.yml",
        "agent-auto-implement.yml",
        "ci.yml",
    ):
        _edit_workflow(repo, name, runners)
    _edit_workflow(
        repo,
        "agent-auto-implement.yml",
        lambda d: d["jobs"]["implement"]["with"].update(route_budget_usd="5"),
    )
    assert _drift(repo) is None


@pytest.mark.parametrize(
    ("name", "change", "where"),
    [
        # Codex 4204434930: a disabled or detached automatic implementation call
        (
            "agent-auto-implement.yml",
            lambda d: d["jobs"]["implement"].update({"if": False}),
            "jobs.implement.if",
        ),
        (
            "agent-auto-implement.yml",
            lambda d: d["jobs"]["implement"].pop("needs"),
            "jobs.implement.needs",
        ),
        # Codex 4204434942: the automatic callers must forward the event's issue
        (
            "agent-auto-implement.yml",
            lambda d: d["jobs"]["implement"]["with"].update(issue_number="1"),
            "jobs.implement.with.issue_number",
        ),
        (
            "agent-auto-plan.yml",
            lambda d: d["jobs"]["plan"]["with"].update(issue_number="${{ github.run_number }}"),
            "jobs.plan.with.issue_number",
        ),
        (
            "agent-auto-plan.yml",
            lambda d: d["jobs"]["plan"]["with"].update(config_path="other.toml"),
            "jobs.plan.with.config_path",
        ),
        (
            "agent-implement.yml",
            lambda d: d["jobs"]["implement"]["with"].update(trigger_actor="bot"),
            "jobs.implement.with.trigger_actor",
        ),
        (
            "agent-implement.yml",
            lambda d: d["jobs"]["implement"].update(secrets="inherit"),
            "jobs.implement.secrets",
        ),
        (
            "agent-plan.yml",
            lambda d: d["jobs"]["plan"]["secrets"].pop("PLATFORM_READ_TOKEN"),
            "jobs.plan.secrets.PLATFORM_READ_TOKEN",
        ),
        (
            "agent-auto-plan.yml",
            lambda d: d[True]["issues"].update(types=["labeled", "opened"]),
            "on.issues.types",
        ),
        (
            "agent-plan.yml",
            lambda d: d["jobs"]["notify_failure"].update(needs=["plan"]),
            "jobs.notify_failure.needs",
        ),
        (
            "ci.yml",
            lambda d: d[True].update(pull_request={"paths": ["src/**"]}),
            "on.pull_request",
        ),
        (
            "ci.yml",
            lambda d: d["jobs"]["test"].update({"continue-on-error": True}),
            "jobs.test.continue-on-error",
        ),
        (
            "agent-implement.yml",
            lambda d: d["jobs"].update(extra={"runs-on": "ubuntu-latest", "steps": []}),
            "jobs.extra",
        ),
    ],
)
def test_doctor_fails_any_drift_outside_the_tunable_knobs(tmp_path, name, change, where):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    _edit_workflow(repo, name, change)
    detail = _drift(repo)
    assert detail is not None and f"managed workflow {name} differs" in detail, detail
    assert f"at {where}" in detail and "re-run" in detail, detail


def test_doctor_rebuilds_the_template_from_the_policy_implementer(tmp_path):
    """An agent other than the policy's is drift, not a tunable."""
    repo = _repo(tmp_path)
    write_onboarding(repo, spec(implementer="codex", runs_on=("ubuntu-latest",)))
    assert _drift(repo) is None
    _edit_workflow(
        repo,
        "agent-implement.yml",
        lambda d: d["jobs"]["implement"]["with"].update(agent="claude"),
    )
    assert "jobs.implement.with.agent" in (_drift(repo) or "")


@pytest.mark.parametrize(
    ("trigger", "problem"),
    [
        ({"paths": ["src/**"]}, "paths filter"),
        ({"paths-ignore": ["docs/**"]}, "paths-ignore filter"),
        ({"branches-ignore": ["main"]}, "branches-ignore filter"),
        ({"branches": ["release/*"]}, "does not name the default branch"),
        ({"branches": ["ma*"]}, "does not name the default branch"),
        ({"types": ["opened"]}, "types omit synchronize, reopened"),
    ],
)
def test_ci_check_requires_every_pull_request_to_reach_the_test_job(tmp_path, trigger, problem):
    """Codex 4204434926: a filtered pull_request trigger leaves some PRs without the required
    check; the gate check itself reports it (the structural check does too)."""
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    _edit_workflow(repo, "ci.yml", lambda d: d[True].update(pull_request=trigger))
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    assert problem in failed["ci.yml runs the policy gates as 'test'"].detail
    assert DRIFT in failed


def test_ci_check_accepts_an_exact_default_branch_filter(tmp_path):
    from agentic_sdlc.onboard import _ci_problems

    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    for trigger in (
        {"branches": ["main"]},
        {"branches": "main"},
        {"types": ["opened", "synchronize", "reopened", "labeled"]},
    ):
        _edit_workflow(repo, "ci.yml", lambda d, t=trigger: d[True].update(pull_request=t))
        assert _ci_problems(repo) == [], trigger


LINUX_CHECK = "Forge workflows run on Linux runners"


@pytest.mark.parametrize("implementer", ["route", "claude", "codex", "cloud-routine"])
@pytest.mark.parametrize("runs_on", [("macos-15",), ("self-hosted", "macOS"), ("self-hosted",)])
def test_onboard_requires_linux_for_every_forge_runner(implementer, runs_on):
    """Codex 4204434954: reusable-implement.yml installs bubblewrap with apt-get. Codex
    4205367730: reusable-plan.yml runs GNU sha256sum, so a cloud routine's plan callers (and every
    other non-CI Forge runner) need Linux too."""
    with pytest.raises(OnboardError, match="Linux only"):
        spec(implementer=implementer, runs_on=runs_on)
    # macOS stays fine for ci.yml only
    spec(implementer=implementer, runs_on=("ubuntu-latest",), ci_runs_on=runs_on)
    spec(implementer=implementer, runs_on=("self-hosted", "Linux", "ARM64"))


def test_doctor_fails_implementation_on_a_macos_runner(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec(runs_on=("ubuntu-latest",)))
    _edit_workflow(
        repo,
        "agent-auto-implement.yml",
        lambda d: d["jobs"]["implement"]["with"].update(runs_on='["macos-15"]'),
    )
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", _healthy_gh()))
    detail = failed[LINUX_CHECK].detail
    assert "agent-auto-implement.yml:implement (runs_on)" in detail and "macos-15" in detail
    assert DRIFT not in failed  # the runner is a tunable knob, checked here instead


# ---------------------------------------------------------------- review regressions (PR 139, 8)
# The CI test job is fully managed: its steps must equal what onboard renders from the policy.

SETUP_PYTHON = "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1"
GATE_CHECK = "ci.yml runs the policy gates as 'test'"


def test_ci_template_pins_setup_python_and_checks_out_the_pull_request_itself():
    from agentic_sdlc.onboard import TUNABLE_KNOBS

    files = render_onboarding(spec(python_version="3.11"))
    steps = yaml.safe_load(files[".github/workflows/ci.yml"])["jobs"]["test"]["steps"]
    assert steps[0]["uses"].startswith("actions/checkout@")
    assert steps[0]["with"] == {"persist-credentials": False}  # no ref/repository
    assert steps[1] == {"uses": SETUP_PYTHON, "with": {"python-version": "3.11"}}
    assert [s.get("name") for s in steps[2:]] == ["Setup", "Quality", "Test"]
    assert "GITHUB_PATH" not in files[".github/workflows/ci.yml"]
    policy = tomllib.loads(files["agentic-sdlc.toml"])
    assert policy["ci"] == {"python_version": "3.11"}
    assert not any("steps" in knob for knob in TUNABLE_KNOBS)
    with pytest.raises(OnboardError, match="python version"):
        spec(python_version="3.12; rm -rf /")


@pytest.mark.parametrize(
    "change",
    [
        # Codex 4204654774: a shim directory on $GITHUB_PATH replaces pytest for later steps
        lambda steps: steps.insert(
            2, {"run": 'mkdir -p shim && echo "$PWD/shim" >> "$GITHUB_PATH"'}
        ),
        # Codex 4204654809: the default branch tested instead of the pull request
        lambda steps: steps[0]["with"].update(ref="main"),
        lambda steps: steps[0]["with"].update(repository="someone/else"),
        # Codex 4204654813: the gate after an unconditional exit never runs
        lambda steps: steps[-1].update(run="exit 0; " + steps[-1]["run"]),
        lambda steps: steps[-1].update(run="exec true; " + steps[-1]["run"]),
        # a harmless-looking hand edit is drift too
        lambda steps: steps.insert(2, {"uses": "actions/cache@v4"}),
        lambda steps: steps[1]["with"].update({"python-version": "3.9"}),
    ],
)
def test_doctor_requires_the_ci_test_steps_to_equal_the_template(tmp_path, change):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    _edit_workflow(repo, "ci.yml", lambda d: change(d["jobs"]["test"]["steps"]))
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    assert STEPS_DIFFER in failed[GATE_CHECK].detail
    assert "at jobs.test.steps" in failed[DRIFT].detail


def test_ci_gate_analysis_flags_github_path_checkout_inputs_and_terminators(tmp_path):
    """Defense in depth behind template equality: each trick is also caught by the analysis."""
    cases = {
        "mentions $GITHUB_PATH": lambda st: st.insert(
            2, {"run": 'echo "$PWD/shim" >> "$GITHUB_PATH"'}
        ),
        "checks out with ref": lambda st: st[0]["with"].update(ref="main"),
        "checks out with repository": lambda st: st[0]["with"].update(repository="a/b"),
        "does not run the test command": lambda st: st[-1].update(run="exit 0; " + st[-1]["run"]),
    }
    for index, (expected, change) in enumerate(cases.items()):
        repo = _repo(tmp_path / str(index))
        write_onboarding(repo, spec())
        _edit_workflow(repo, "ci.yml", lambda d, c=change: c(d["jobs"]["test"]["steps"]))
        found = _analysis_only(repo)
        assert any(expected in p for p in found), (expected, found)


@pytest.mark.parametrize(
    "script",
    [
        "exit 0; pytest",
        "exit; pytest",
        "exec true; pytest",
        "return 0; pytest",
        "false || exit 0; pytest",
        "if true; then exit 0; fi; pytest",
        "f() { exit 0; }; f; pytest",
        "builtin exit 0; pytest",
        "exit 0\npytest",
    ],
)
def test_shell_parse_treats_exit_exec_return_as_terminators(script):
    from agentic_sdlc.onboard import _shell_parse

    commands = _shell_parse(script).commands
    assert ("pytest",) in [argv for argv, _ in commands]
    assert not any(enforced for argv, enforced in commands if argv == ("pytest",)), commands
    assert _shell_parse("pytest; exit 0").commands[0] == (("pytest",), True)


@pytest.mark.parametrize("bad", ["true\npytest", "pytest\r", "pytest ${{ github.ref }}", "a\x00b"])
def test_gate_commands_must_be_single_line_and_expression_free(tmp_path, bad):
    """Codex 4204654802: `true\\npytest` used to render as `true pytest` (always passing)."""
    from agentic_sdlc.onboard import _yaml_run

    for field in ("test_command", "setup_command", "quality_command"):
        with pytest.raises(OnboardError):
            spec(**{field: bad})
    with pytest.raises(OnboardError):
        _yaml_run(bad)
    # A policy edited by hand to hold one is refused by doctor too, never normalized.
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    toml = repo / "agentic-sdlc.toml"
    toml.write_text(
        toml.read_text().replace(
            "test = \"pytest tests -q -m 'not e2e'\"", f"test = {json.dumps(bad)}"
        )
    )
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    assert "policy [commands] test must" in failed[GATE_CHECK].detail


def test_gate_commands_render_byte_for_byte():
    command = "pytest -k 'a and not b'   --tb=short"
    files = render_onboarding(spec(test_command=command))
    steps = yaml.safe_load(files[".github/workflows/ci.yml"])["jobs"]["test"]["steps"]
    assert steps[-1]["run"] == command


# Codex 4204654822: values interpolated into TOML / YAML / Markdown must round-trip.

HOSTILE_LABEL = "-ready: \"q\" #x 'y' \\ ü `z`"
HOSTILE_COMMANDS = {
    "setup_command": "python -m pip install -e '.[dev]' # \"deps\" ü",
    "quality_command": "ruff check . --select 'E9' \\ `x` ``y``",
    "test_command": '``` pytest -k "a: b" -q',
}


def test_every_template_round_trips_hostile_values(tmp_path):
    import tomllib

    hostile = spec(ready_label=HOSTILE_LABEL, **HOSTILE_COMMANDS)
    commands = [HOSTILE_COMMANDS[k] for k in ("setup_command", "quality_command", "test_command")]
    for implementer in ("route", "cloud-routine"):
        files = render_onboarding(replace(hostile, implementer=implementer))
        policy = tomllib.loads(files["agentic-sdlc.toml"])
        assert policy["automation"]["ready_label"] == HOSTILE_LABEL
        assert [policy["commands"][g] for g in ("setup", "quality", "test")] == commands
        assert load_policy_text(files["agentic-sdlc.toml"]).ready_label == HOSTILE_LABEL
        front = files[".github/ISSUE_TEMPLATE/agent-work-request.md"].split("---")[1]
        assert yaml.safe_load(front)["labels"] == [HOSTILE_LABEL, "human-review-required"]
        for name, content in files.items():
            if name.endswith(".yml"):
                assert isinstance(yaml.safe_load(content), dict), name
            if name.endswith(".json"):
                json.loads(content)
        steps = yaml.safe_load(files[".github/workflows/ci.yml"])["jobs"]["test"]["steps"]
        assert [s["run"] for s in steps[2:]] == commands
        # Markdown: the fenced block holds the commands verbatim and its fence is not closed
        # early; the inline code span reproduces the command.
        agents = files["AGENTS.md"]
        fence = re.search(r"^(`{3,})bash\n(.*?)^\1$", agents, re.MULTILINE | re.DOTALL)
        assert fence and fence.group(2).splitlines() == commands
        claude = files["CLAUDE.md"]
        span = re.search(r"Run (`+) ?(.*?) ?\1 before", claude)
        assert span and span.group(2) == HOSTILE_COMMANDS["test_command"]
        if implementer == "cloud-routine":
            routine = files["docs/forge/cloud-implementer.md"]
            block = re.search(r"^(`{3,})\n(.*?)^\1$", routine, re.MULTILINE | re.DOTALL)
            assert block and all(c in block.group(2) for c in commands)
    repo = _repo(tmp_path)
    write_onboarding(repo, hostile)
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    for name in (GATE_CHECK, DRIFT, "agentic-sdlc.toml loads"):
        assert name not in failed, failed.get(name)
    assert "workflow label conditions match the policy labels" not in failed


def load_policy_text(text: str):
    from agentic_sdlc.policy import load_policy_bytes

    return load_policy_bytes(text.encode())


# Codex 4204654830: every parameter of Forge's ruleset is compared, not a hand-picked few.


def test_ruleset_requires_stale_review_dismissal():
    from agentic_sdlc.onboard import merged_ruleset

    weak = json.loads(json.dumps(ruleset_payload(spec())))
    pr_rule = next(r for r in weak["rules"] if r["type"] == "pull_request")
    pr_rule["parameters"]["dismiss_stale_reviews_on_push"] = False
    problems = ruleset_mismatches(weak, spec())
    assert any("stale approvals are not dismissed" in p for p in problems), problems
    merged = merged_ruleset(weak, spec())
    merged_pr = next(r for r in merged["rules"] if r["type"] == "pull_request")
    assert merged_pr["parameters"]["dismiss_stale_reviews_on_push"] is True
    assert ruleset_mismatches(merged, spec()) == []


def test_ruleset_mismatches_cover_every_payload_parameter():
    """Flip each parameter Forge sets to its weakest value: every flip must be reported."""
    payload = ruleset_payload(spec())
    for index, rule in enumerate(payload["rules"]):
        for key, value in (rule.get("parameters") or {}).items():
            if value is False:
                continue  # Forge does not require it; any value is at least as strict
            weak = json.loads(json.dumps(payload))
            params = weak["rules"][index]["parameters"]
            if value is True:
                params[key] = False
            elif isinstance(value, int):
                if value == 0:
                    params[key] = -1
                else:
                    params[key] = value - 1
            elif isinstance(value, list):
                params[key] = []
            assert ruleset_mismatches(weak, spec()), (rule["type"], key)
            params.pop(key)
            assert ruleset_mismatches(weak, spec()), (rule["type"], key, "absent")


# Codex 4204654786: git aliases must not hide a commit from the guard.


@pytest.mark.parametrize(
    "command",
    [
        "git -c alias.ci=commit ci -m x",
        "git -c alias.CI=commit ci -m x",
        "git -c alias.c1=c2 -c alias.c2=commit c1 -m x",
        "git -c 'alias.sc=!git commit' sc",
        "git --config-env=alias.ci=SOME_ENV ci",
        "git --config-env alias.ci=SOME_ENV ci",
        "git -c alias.ci='-c user.name=x commit' ci",
        "git -c alias.loop=loop loop",
    ],
)
def test_commit_guard_resolves_inline_git_aliases(tmp_path, command):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    assert guard.is_git_commit(command, cwd=str(tmp_path))


@pytest.mark.parametrize(
    "command",
    ["git -c alias.st=status st", "git -c alias.ci=commit status", "git lfs push origin main"],
)
def test_commit_guard_keeps_non_committing_aliases(tmp_path, command):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    assert not guard.is_git_commit(command, cwd=str(tmp_path))


def test_commit_guard_resolves_repository_and_global_aliases(tmp_path, monkeypatch):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[alias]\n\tgci = commit\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo = tmp_path / "wt"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "forge/issue-7")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    _git(repo, "config", "alias.ci", "commit")
    _git(repo, "config", "alias.sh", "!git commit -m x")
    _git(repo, "config", "alias.st", "status")
    payload = {"tool_name": "Bash", "session_id": "s1", "cwd": str(repo)}

    def code(command, cwd=repo):
        branches = guard.commit_branches(command, str(cwd))
        return guard.decide(
            {**payload, "cwd": str(cwd), "tool_input": {"command": command}},
            branches,
            lambda n: None,
            lambda b: False,
        )[0]

    for blocked in ("git ci -m x", "git sh", "git gci -m x", "cd wt && git ci -m x"):
        cwd = tmp_path if blocked.startswith("cd ") else repo
        assert code(blocked, cwd) == 2, blocked
    assert code("git st") == 0
    assert not guard.is_git_commit("git ci -m x", cwd=str(tmp_path))  # no such alias there
    # GIT_CONFIG_* on the command line is read the way git reads it
    assert guard.is_git_commit(
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.yy GIT_CONFIG_VALUE_0=commit git yy",
        cwd=str(tmp_path),
    )


def test_cli_onboard_passes_the_python_version_into_policy_and_ci(tmp_path):
    from agentic_sdlc import cli

    repo = _repo(tmp_path)
    args = ["onboard", "--visibility", "private", "--destination", str(repo)]
    args += ["--project-id", "owner/comic", "--platform-repository", "owner/agentic-sdlc"]
    args += ["--platform-ref", SHA, "--test", "pytest -q", "--python-version", "3.11"]
    assert cli.main([*args, "--output", str(tmp_path / "o.json")]) == 0
    policy = tomllib.loads((repo / "agentic-sdlc.toml").read_text())
    assert policy["ci"]["python_version"] == "3.11"
    ci = yaml.safe_load((repo / ".github/workflows/ci.yml").read_text())
    assert ci["jobs"]["test"]["steps"][1]["with"]["python-version"] == "3.11"
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    assert GATE_CHECK not in failed and DRIFT not in failed


# ---------------------------------------------------------------- remote workflows / managed files

PUBLIC_CHECK = "public repository runs pull requests on GitHub-hosted runners"
FILES_CHECK = "managed files match the generated files"
PUSHED_CHECK = "managed files are on the default branch"
SELF_HOSTED_PR = (
    "on: pull_request\njobs:\n  lint:\n    runs-on: [self-hosted]\n"
    "    steps:\n      - run: make lint\n"
)


def _public_gh() -> FakeGh:
    gh = _healthy_gh()
    gh.answers["api repos/owner/comic"] = json.dumps(
        {"default_branch": "main", "visibility": "public"}
    )
    return gh


def _remote_workflows(gh: FakeGh, files: dict[str, str]) -> None:
    """Serve `files` as the default branch's .github/workflows (instead of the checkout's)."""
    listing = [{"name": n, "path": f".github/workflows/{n}", "type": "file"} for n in sorted(files)]
    gh.answers[CONSUMER_CONTENTS + ".github/workflows?ref=main"] = json.dumps(listing)
    for name, text in files.items():
        gh.answers[CONSUMER_CONTENTS + f".github/workflows/{name}?ref=main"] = json.dumps(
            {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}
        )


def test_public_runner_safety_reads_the_default_branchs_workflows(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec(public=True))
    local = {p.name: p.read_text() for p in (repo / ".github/workflows").glob("*.yml")}
    gh = _public_gh()
    # The checkout is stale: the default branch also runs a self-hosted pull_request workflow.
    _remote_workflows(gh, {**local, "lint.yml": SELF_HOSTED_PR})
    check = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))[PUBLIC_CHECK]
    assert "lint.yml:lint" in check.detail and "local checkout" not in check.detail
    # ... and a reusable workflow present only remotely is followed from the remote set.
    gh = _public_gh()
    _remote_workflows(
        gh,
        {
            **local,
            "reuse.yml": REUSE_SELF_HOSTED,
            "pr.yml": _pr_caller("./.github/workflows/reuse.yml"),
        },
    )
    check = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))[PUBLIC_CHECK]
    assert "pr.yml:call > reuse.yml:build" in check.detail


def test_public_runner_safety_fails_closed_without_the_remote_workflow_set(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec(public=True))
    gh = _public_gh()
    gh.answers[CONSUMER_CONTENTS + ".github/workflows?ref=main"] = "not json"
    check = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))[PUBLIC_CHECK]
    assert "unverifiable" in check.detail and "fails closed" in check.detail
    # One unreadable file is an unreadable set, not a smaller one.
    gh = _public_gh()
    gh.answers[CONSUMER_CONTENTS + ".github/workflows/ci.yml?ref=main"] = json.dumps(
        {"encoding": "none", "content": ""}
    )
    check = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))[PUBLIC_CHECK]
    assert "could not read .github/workflows/ci.yml" in check.detail


def test_public_runner_safety_still_checks_local_workflows_as_an_addition(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec(public=True))
    remote = {p.name: p.read_text() for p in (repo / ".github/workflows").glob("*.yml")}
    _wf(repo, "lint.yml", SELF_HOSTED_PR)  # not pushed yet
    gh = _public_gh()
    _remote_workflows(gh, remote)
    check = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))[PUBLIC_CHECK]
    assert "local checkout: lint.yml:lint" in check.detail


def test_doctor_compares_hook_scripts_with_the_rendered_hooks(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    guard = repo / ".claude/hooks/forge_commit_guard.py"
    guard.write_text("import sys\nsys.exit(0)\n")
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", _healthy_gh()))
    assert ".claude/hooks/forge_commit_guard.py differs" in failed[FILES_CHECK].detail
    # the pushed copy is the same disabled script: compared with the template, not only local
    assert "forge_commit_guard.py (differs from the generated file" in failed[PUSHED_CHECK].detail
    local = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    assert FILES_CHECK in local


def test_doctor_flags_a_remote_only_disabled_session_start_hook(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _healthy_gh()
    gh.answers[CONSUMER_CONTENTS + ".claude/hooks/forge_session_start.py"] = json.dumps(
        {"encoding": "base64", "content": base64.b64encode(b"pass\n").decode()}
    )
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))
    assert FILES_CHECK not in failed
    assert "forge_session_start.py (differs)" in failed[PUSHED_CHECK].detail


@pytest.mark.parametrize(
    ("relative", "edit", "fails"),
    [
        (".github/ISSUE_TEMPLATE/agent-work-request.md", lambda t: t + "\nextra\n", True),
        ("AGENTS.md", lambda t: t + "\n## Repository notes\n\nlocal detail\n", False),
        ("AGENTS.md", lambda t: t.replace("Never merge", "Feel free to merge"), True),
        ("CLAUDE.md", lambda t: t.replace("Tests first", "Tests later"), True),
    ],
)
def test_doctor_compares_every_managed_file_with_its_rendered_form(tmp_path, relative, edit, fails):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    path = repo / relative
    path.write_text(edit(path.read_text()))
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", _healthy_gh()))
    assert (FILES_CHECK in failed) is fails, failed.get(FILES_CHECK)
    if fails:
        assert f"{relative} differs" in failed[FILES_CHECK].detail


def test_doctor_compares_the_cloud_routine_doc_apart_from_its_ci_runner(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec(implementer="cloud-routine"))
    doc = repo / "docs/forge/cloud-implementer.md"
    text = doc.read_text()
    doc.write_text(re.sub(r"runs on\s+`[^`]*`", "runs on\n`ubuntu-latest`", text))
    local = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    assert FILES_CHECK not in local
    doc.write_text(text.replace("Never merge", "Merge when green"))
    local = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    assert "docs/forge/cloud-implementer.md differs" in local[FILES_CHECK].detail


def test_every_rendered_file_is_compared_by_some_doctor_check():
    """No Forge-managed file may be checked for existence only."""
    from agentic_sdlc.onboard import (
        CONTAINED_MANAGED_FILES,
        EXACT_MANAGED_FILES,
        MANAGED_IMPLEMENT_WORKFLOWS,
        MANAGED_WORKFLOWS,
        ROUTINE_DOC,
    )

    compared = {
        *EXACT_MANAGED_FILES,
        *CONTAINED_MANAGED_FILES,
        ROUTINE_DOC,
        *(f".github/workflows/{n}" for n in (*MANAGED_WORKFLOWS, *MANAGED_IMPLEMENT_WORKFLOWS)),
        ".claude/settings.json",  # hook_problems: the parsed hooks structure
        "agentic-sdlc.toml",  # the policy itself: load_policy + the policy checks
        ".forge/executors.json",  # the routing check validates and routes on both registries
        ".forge/routing-policy.json",
    }
    for implementer in ("route", "claude", "cloud-routine"):
        assert set(render_onboarding(spec(implementer=implementer))) <= compared


def test_settings_json_stays_tunable_while_its_hooks_are_present(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    settings = repo / ".claude/settings.json"
    doc = json.loads(settings.read_text())
    doc["permissions"] = {"allow": ["Bash(pytest:*)"]}
    settings.write_text(json.dumps(doc))
    local = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    assert FILES_CHECK not in local and "required files present" not in local


# Codex 4205123774: commits inside substitutions, eval and here-strings. The guard fails closed:
# text naming `git` and `commit` is a commit unless every simple command is proven otherwise.


@pytest.mark.parametrize(
    "command",
    [
        'output="$(git commit -m x)"',
        'echo "`git commit -m x`"',
        'echo "$(echo "$(git commit -m x)")"',
        'eval "git commit"',
        'bash <<< "git commit"',
        "x=commit; git $x -m y",
        'cd "$(git commit -m x)"',
        "cd `git commit -m x` && ls",
        "c=git; $c commit -m y",
        "bash -c 'echo $(git commit -m x)'",
        "echo '$(git commit)'",  # a literal, but quoting is gone after shlex: over-blocked
        # Conservative: any expansion in a line naming both words is unproven, so blocked.
        'out="$(git rev-parse HEAD)"; echo commit',
    ],
)
def test_commit_guard_fails_closed_on_substitutions_and_eval(tmp_path, command):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    assert guard.is_git_commit(command, cwd=str(tmp_path))


@pytest.mark.parametrize(
    "command",
    [
        "git log --grep commit",
        # Proven: `echo commit` has no git word, `git status` no commit word -> allowed.
        "echo commit && git status",
        "bash -c 'git log --grep commit'",
        "git status",
        "cd sub && git log --grep commit",
    ],
)
def test_commit_guard_fail_closed_still_proves_non_commits(tmp_path, command):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    assert not guard.is_git_commit(command, cwd=str(tmp_path))


def test_commit_guard_substitution_targets_its_own_repository(tmp_path):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    targets = guard.commit_targets('out="$(git -C ../wt commit -m x)"', cwd=str(tmp_path))
    assert ((), ("-C", "../wt"), ()) in targets


def test_commit_guard_blocks_substituted_commit_on_unleased_branch(tmp_path):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    payload = {
        "tool_name": "Bash",
        "session_id": "s1",
        "tool_input": {"command": 'output="$(git commit -m x)"'},
    }
    assert guard.decide(payload, "forge/issue-7", lambda n: None, lambda b: False)[0] == 2
    held = {"session": "s1", "agent": "a"}
    assert guard.decide(payload, "forge/issue-7", lambda n: held, lambda b: False)[0] == 0


# Codex 4205123787: files a mode does not own must be absent on the default branch too.

POLICY_FIELDS_CHECK = "policy fields match what the generated automation requires"


def test_doctor_flags_a_stale_remote_implement_workflow_after_switching_to_cloud(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    stale = (repo / ".github/workflows/agent-auto-implement.yml").read_bytes()
    write_onboarding(repo, spec(implementer="cloud-routine"), force=True)
    assert not (repo / ".github/workflows/agent-auto-implement.yml").exists()
    gh = _healthy_gh()
    # the deletion was never pushed: the default branch still launches the Actions implementer
    gh.answers[CONSUMER_CONTENTS + ".github/workflows/agent-auto-implement.yml?ref=main"] = (
        json.dumps({"encoding": "base64", "content": base64.b64encode(stale).decode()})
    )
    detail = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))[PUSHED_CHECK].detail
    assert ".github/workflows/agent-auto-implement.yml (present" in detail
    assert "push the onboard --force changes first" in detail
    assert "agent-implement.yml (present" not in detail  # 404 remotely: absent, as required
    # once the deletion is pushed (404 again) the check no longer fails on it
    del gh.answers[CONSUMER_CONTENTS + ".github/workflows/agent-auto-implement.yml?ref=main"]
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))
    assert "agent-auto-implement" not in getattr(failed.get(PUSHED_CHECK), "detail", "")


def test_doctor_fails_closed_when_a_non_owned_files_absence_is_unverifiable(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _healthy_gh()
    gh.answers[CONSUMER_CONTENTS + "docs/forge/cloud-implementer.md?ref=main"] = "not json"
    detail = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))[PUSHED_CHECK].detail
    assert "docs/forge/cloud-implementer.md (unverifiable" in detail


def test_doctor_flags_a_stale_remote_routine_doc_after_switching_to_actions(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec(implementer="cloud-routine"))
    doc = (repo / "docs/forge/cloud-implementer.md").read_bytes()
    write_onboarding(repo, spec(), force=True)
    gh = _healthy_gh()
    gh.answers[CONSUMER_CONTENTS + "docs/forge/cloud-implementer.md?ref=main"] = json.dumps(
        {"encoding": "base64", "content": base64.b64encode(doc).decode()}
    )
    detail = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))[PUSHED_CHECK].detail
    assert "docs/forge/cloud-implementer.md (present, but the actions mode" in detail


# Codex 4205123815: the policy fields the generated automation depends on.


def test_doctor_requires_the_github_provider(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    toml = repo / "agentic-sdlc.toml"
    toml.write_text(toml.read_text().replace('provider = "github"', 'provider = "gitlab"'))
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    assert "prepare-request --provider github" in failed[POLICY_FIELDS_CHECK].detail
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", _healthy_gh())
    assert not report.ok and POLICY_FIELDS_CHECK in _failed(report)


@pytest.mark.parametrize(
    ("old", "new", "needle"),
    [
        (
            'human_review_label = "human-review-required"',
            'human_review_label = "needs-human"',
            "human_review_label",
        ),
        (
            'implementation_label = "implementation-approved"',
            'implementation_label = "go"',
            "implementation_label",
        ),
        ('implementer = "router"', 'implementer = "gpt"', "implementer"),
        (
            'executors = ".forge/executors.json"',
            'executors = "elsewhere.json"',
            "[routing] executors",
        ),
    ],
)
def test_doctor_compares_each_policy_field_the_automation_depends_on(tmp_path, old, new, needle):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    toml = repo / "agentic-sdlc.toml"
    assert old in toml.read_text()
    toml.write_text(toml.read_text().replace(old, new))
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    assert needle in failed[POLICY_FIELDS_CHECK].detail


def test_doctor_policy_fields_pass_for_every_generated_mode(tmp_path):
    for index, implementer in enumerate(("route", "claude", "codex", "cloud-routine")):
        repo = _repo(tmp_path / str(index))
        write_onboarding(repo, spec(implementer=implementer))
        report = doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False)
        assert POLICY_FIELDS_CHECK not in _failed(report), implementer
        assert any(c.name == POLICY_FIELDS_CHECK for c in report.checks)


# ---------------------------------------------------------------- review regressions (PR 139, 12)

ONBOARD_EDGE_CASES = {
    "setup_command": ["", " ", "\t", "a\nb", "x\x7f", "x y", "x\ud800", "${{ x }}", "  pip  "],
    "quality_command": ["", "  ", "ruff check . # c", "echo '`'", "a\tb", "x\x85y", "ünï"],
    "test_command": ["", " ", "pytest\r", "pytest -k 'a and not b'", "x "],
    "ready_label": [
        "",
        " ",
        " x",
        "x ",
        "a,b",
        "x" * 50,
        "x" * 51,
        "human-review-required",
        "implementation-approved",
        "in-progress",
        "Implementation-Approved",
        "Claude-Ready",
        "a'b",
        'a"b',
        "a #b: c",
        "-x",
        "x\x7f",
        "x ",
        "ünï",
    ],
    "default_branch": [
        "",
        "main",
        "feature/x",
        "a b",
        "release-1.2",
        "x\n",
        "foo..bar",
        "main.lock",
    ],
    "python_version": ["", "3", "3.12", "3.12.4", "3.12 ", "3.12.4.1", 3.12],
    "forbidden_paths": [(), ("",), ("a\nb",), ("ok/**", "*.png"), ("x\x7f",), ("ü/**",)],
    "protected_paths": [(), ("",), ("src/app.py",), ("a ",)],
    "max_changed_files": [0, 1, 10_000, 10_001, -1, True, "5"],
    "max_diff_lines": [0, 1, 10_000_000, 10_000_001],
    "project_id": ["a/b", "a/b/c", "a", "a b/c", "o.k/n_a-me"],
    "runs_on": [
        ("macos-15",),
        ("self-hosted",),
        ("ubuntu-latest",),
        ("self-hosted", "linux"),
        ("self-hosted", "macOS"),
        ("windows-latest",),
    ],
    "ci_runs_on": [("macos-15",), ("windows-latest",), ("self-hosted", "macOS")],
    "implementer": ["route", "claude", "codex", "cloud-routine", "gemini"],
}


@pytest.mark.parametrize(
    ("field", "value"),
    [(f, v) for f, values in ONBOARD_EDGE_CASES.items() for v in values],
    ids=lambda x: repr(x)[:40],
)
@pytest.mark.parametrize("implementer", ["route", "cloud-routine"])
def test_onboard_rejects_or_doctor_is_green(tmp_path, field, value, implementer):
    """Codex 4205367720 (`--setup ""` accepted by onboard, rejected by doctor), as a property:
    for every edge case, onboard either refuses it or writes a repository `doctor --local` calls
    READY. And the shared POLICY_FIELDS validator gives the same verdict on both sides."""
    from agentic_sdlc.onboard import POLICY_FIELDS, policy_field_problem

    overrides = {"implementer": implementer, "runs_on": ("ubuntu-latest",), field: value}
    try:
        built = spec(**overrides)
    except OnboardError:
        built = None
    policy_field = {attr: name for name, (attr, *_) in POLICY_FIELDS.items()}.get(field)
    if policy_field is not None:  # doctor's validator says the same about the written value
        assert bool(policy_field_problem(policy_field, value)) == (built is None), value
    if built is None:
        return
    repo = _repo(tmp_path)
    write_onboarding(repo, built)
    report = doctor(repo, built.project_id, "owner/agentic-sdlc", FakeGh(), remote=False)
    assert report.ok, report.render()


@pytest.mark.parametrize(
    ("table", "key", "value"),
    [
        ("commands", "setup", ""),
        ("commands", "quality", "   "),
        ("automation", "ready_label", " padded"),
        ("policy", "max_changed_files", 0),
        ("ci", "python_version", "3"),
    ],
)
def test_doctor_rejects_a_hand_edited_policy_value_onboard_would_refuse(
    tmp_path, table, key, value
):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    _set_policy_value(repo, table, key, value)
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    assert f"policy [{table}] {key} must" in failed[POLICY_VALUES_CHECK].detail


POLICY_VALUES_CHECK = "policy values are ones onboard accepts"


def _set_policy_value(repo: Path, table: str, key: str, value) -> None:
    toml = repo / "agentic-sdlc.toml"
    pattern = re.compile(rf"(?ms)^(\[{table}\]\n(?:(?!^\[).)*?^){key} = [^\n]*$")
    text, count = pattern.subn(
        lambda m: m.group(1) + f"{key} = {json.dumps(value)}", toml.read_text()
    )
    assert count == 1, (table, key)
    toml.write_text(text)


GUARD_HOOK = "forge_commit_guard.py"


@pytest.mark.parametrize("event", ["PreToolUse", "SessionStart"])
@pytest.mark.parametrize(
    "extra",
    [{"async": True}, {"async": False}, {"timeout": 1}, {"once": True}, {"statusMessage": "x"}],
)
def test_doctor_requires_each_forge_hook_to_be_exactly_the_generated_handler(
    tmp_path, event, extra
):
    """Codex 4205367704: an `"async": true` commit guard runs in the background and cannot block,
    yet doctor called it ready. Any field onboard does not write fails, on either Forge hook."""
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    path = repo / ".claude/settings.json"
    settings = json.loads(path.read_text())
    settings["hooks"][event][0]["hooks"][0].update(extra)
    _settings(repo, settings)
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    detail = failed["required files present"].detail
    assert f"{event} hook running python3" in detail and next(iter(extra)) in detail


def test_doctor_rejects_an_async_guard_even_beside_a_synchronous_one(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    settings = json.loads((repo / ".claude/settings.json").read_text())
    settings["hooks"]["PreToolUse"].append(
        {"matcher": "Bash", "hooks": [{**_hook(GUARD_HOOK), "async": True}]}
    )
    _settings(repo, settings)
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    assert "async" in failed["required files present"].detail
    # an extra field on the Forge hook's GROUP is a deviation too
    settings["hooks"]["PreToolUse"] = [{"matcher": "Bash", "hooks": [_hook(GUARD_HOOK)], "x": 1}]
    _settings(repo, settings)
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    assert "group x" in failed["required files present"].detail


def test_doctor_allows_unrelated_hooks_with_any_fields(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    settings = json.loads((repo / ".claude/settings.json").read_text())
    settings["hooks"]["PreToolUse"].append(
        {"matcher": "Bash", "hooks": [{**_hook("lint.py"), "async": True, "timeout": 5}]}
    )
    settings["hooks"]["PostToolUse"] = [{"hooks": [{**_hook("fmt.py"), "async": True}]}]
    _settings(repo, settings)
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False)
    assert report.ok, report.render()


def test_rendered_hooks_are_the_handlers_doctor_compares_against():
    from agentic_sdlc.onboard import HOOK_SCRIPTS, forge_hook_entry

    settings = json.loads(render_onboarding(spec())[".claude/settings.json"])
    for event, script in HOOK_SCRIPTS.items():
        assert settings["hooks"][event][0]["hooks"] == [forge_hook_entry(script)]


def test_doctor_fails_a_cloud_routine_plan_caller_on_macos(tmp_path):
    """Codex 4205367730: reusable-plan.yml runs GNU sha256sum; a cloud routine still plans."""
    repo = _repo(tmp_path)
    write_onboarding(repo, spec(implementer="cloud-routine", runs_on=("ubuntu-latest",)))
    _edit_workflow(
        repo, "agent-plan.yml", lambda d: d["jobs"]["plan"]["with"].update(runs_on='["macos-15"]')
    )
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", _healthy_gh()))
    assert "agent-plan.yml:plan (runs_on)" in failed[LINUX_CHECK].detail
    # ci.yml alone may run on macOS
    repo = _repo(tmp_path / "ci")
    write_onboarding(
        repo,
        spec(implementer="cloud-routine", runs_on=("ubuntu-latest",), ci_runs_on=("macos-15",)),
    )
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", _healthy_gh()))
    assert LINUX_CHECK not in failed


# Codex 4205654928: the shell can ASSEMBLE `git` or `commit` from quote fragments, ANSI-C
# quoting, escapes, braces and substitutions. Proof needs plain literal program/subcommand words.


@pytest.mark.parametrize(
    "command",
    [
        "g$'it' com$'mit' -m x",
        '"g"it commit',
        "\\git commit",
        "gi\\t com\\mit",
        "git co{m,}mit",
        "$(echo git) commit",
        "git $'commit' -m x",
        "env g$'it' x",
        "eval g${x}it com${x}mit",
        "eval \"g\\$'it' com\\$'mit'\"",
        "echo $(g$'it' status)",
        "bash -c \"\\$'g'it commit\"",
    ],
)
def test_commit_guard_treats_assembled_programs_as_unproven(tmp_path, command):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    assert guard.is_git_commit(command, cwd=str(tmp_path))
    payload = {"tool_name": "Bash", "session_id": "s1", "tool_input": {"command": command}}
    assert guard.decide(payload, "forge/issue-7", lambda n: None, lambda b: False)[0] == 2


@pytest.mark.parametrize(
    "command",
    [
        "git log --grep commit",
        "ls",
        "ls -la && git status",
        'git -C "$dir" status',
        "# a note\nls",
        "if [ -f x ]; then echo 'it''s'; fi",
        "sh -e script.sh",
    ],
)
def test_commit_guard_raw_proof_keeps_plain_non_commits(tmp_path, command):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    assert not guard.is_git_commit(command, cwd=str(tmp_path))


def test_commit_guard_checks_an_assembled_commit_after_cd(tmp_path):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    targets = guard.commit_targets('cd ../wt && g"it" commit', cwd=str(tmp_path))
    assert (("../wt",), (), ()) in targets and ((), (), ()) in targets


# Codex 4205654958 / 4205654967: the shared POLICY_FIELDS validator (onboard and doctor).


@pytest.mark.parametrize(
    "label",
    [
        "Implementation-Approved",
        "IMPLEMENTATION-APPROVED",
        "Human-Review-Required",
        "In-Progress",
        "Agentic-SDLC",
        "claude-BLOCKED",
    ],
)
def test_ready_label_may_not_collide_case_insensitively_with_a_fixed_label(label):
    from agentic_sdlc.onboard import policy_field_problem

    assert "case-insensitively" in policy_field_problem("ready label", label)
    with pytest.raises(OnboardError):
        spec(ready_label=label)


@pytest.mark.parametrize("label", ["claude-ready", "Claude-Ready", "forge-ready"])
def test_ready_label_accepts_distinct_names(label):
    from agentic_sdlc.onboard import policy_field_problem

    assert policy_field_problem("ready label", label) == ""


@pytest.mark.parametrize(
    "branch",
    ["foo..bar", "/main", "main/", "main.lock", "a//b", ".hidden", "a/.b", "a/b.lock/c"]
    + [
        "main.",
        "-main",
        "@",
        "HEAD",
        "a@{1}",
        "a b",
        "a~1",
        "a^",
        "a:b",
        "a?",
        "a*",
        "a[b",
        "a\\b",
    ],
)
def test_default_branch_must_be_a_valid_git_branch_name(branch, monkeypatch):
    from agentic_sdlc import onboard

    monkeypatch.setattr(onboard, "_git_rejects_branch", lambda name: False)  # the rules alone
    assert onboard.policy_field_problem("default branch", branch)
    with pytest.raises(OnboardError):
        spec(default_branch=branch)


@pytest.mark.parametrize("branch", ["main", "feature/x", "release-1.2", "a.b/c_d", "v1.0-rc"])
def test_default_branch_accepts_valid_git_branch_names(branch):
    from agentic_sdlc.onboard import policy_field_problem

    assert policy_field_problem("default branch", branch) == ""


def test_default_branch_also_requires_the_installed_git_to_agree(monkeypatch):
    from agentic_sdlc import onboard

    monkeypatch.setattr(onboard, "_git_rejects_branch", lambda name: name == "trunk")
    assert "check-ref-format" in onboard.policy_field_problem("default branch", "trunk")
    assert onboard.policy_field_problem("default branch", "main") == ""


# Codex 4205654942: a private platform must admit the consumer as a reusable-workflow caller.

PLATFORM_ACCESS = "private platform lets this repository call its workflows"


def _private_platform_gh(access: str | None):
    gh = _healthy_gh()
    gh.answers = {"api repos/owner/agentic-sdlc --jq": "true\n", **gh.answers}
    secrets = ("PUBLISHER_APP_PRIVATE_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "DEEPSEEK_API_KEY")
    gh.answers["api repos/owner/comic/actions/secrets"] = _secret_pages(
        *secrets, "ZAI_API_KEY", "KIMI_API_KEY", "PLATFORM_READ_TOKEN"
    )
    key = "api repos/owner/agentic-sdlc/actions/permissions/access"
    if access is None:
        gh.fail = {*gh.fail, key}  # 403/404: the reviewer is not an admin of the platform
    else:
        gh.answers[key] = json.dumps({"access_level": access})
    return gh


@pytest.mark.parametrize("access", ["user", "organization"])
def test_doctor_accepts_a_private_platform_shared_with_its_owner(tmp_path, access):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", _private_platform_gh(access))
    assert report.ok, report.render()
    assert next(c for c in report.checks if c.name == PLATFORM_ACCESS).ok


@pytest.mark.parametrize("access", [None, "none"])
def test_doctor_reports_an_unshared_or_unreadable_private_platform_as_manual(tmp_path, access):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", _private_platform_gh(access))
    assert not report.ok
    check = _failed(report)[PLATFORM_ACCESS]
    assert check.manual
    assert "Settings → Actions → General → Access" in check.detail
    assert "access_level=organization" in check.detail
    assert ("could not read" in check.detail) == (access is None)


def test_doctor_cannot_verify_cross_owner_enterprise_sharing(tmp_path):
    from agentic_sdlc.onboard import platform_access_check

    gh = FakeGh(
        {
            "api repos/plat/forge/actions/permissions/access": json.dumps(
                {"access_level": "enterprise"}
            )
        }
    )
    check = platform_access_check(gh, "owner/comic", "plat/forge")
    assert not check.ok and check.manual and "same enterprise" in check.detail
    gh.answers["api repos/plat/forge/actions/permissions/access"] = json.dumps(
        {"access_level": "organization"}
    )
    assert not platform_access_check(gh, "owner/comic", "plat/forge").ok


def test_doctor_skips_the_access_check_for_a_public_platform(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", _healthy_gh())
    assert all(c.name != PLATFORM_ACCESS for c in report.checks)
