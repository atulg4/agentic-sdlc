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
    resolve_platform_ref,
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

    gh2 = FakeGh({"api repos/owner/comic/rulesets": json.dumps([{"name": RULESET_NAME}])})
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
    assert copy_variables("owner/music", ["DEEPSEEK_MODEL_FLASH", "DEEPSEEK_MODEL_PRO"], gh) == {
        "DEEPSEEK_MODEL_FLASH": "deepseek-v4-flash"
    }


# ---------------------------------------------------------------- doctor


def _healthy_gh() -> FakeGh:
    return FakeGh(
        {
            "api repos/owner/agentic-sdlc/contents": '{"path": "reusable-implement.yml"}',
            "label list": json.dumps(
                [
                    {"name": n}
                    for n in ("claude-ready", "human-review-required", IMPLEMENTATION_LABEL)
                ]
            ),
            "api repos/owner/comic/rulesets": json.dumps(
                [{"name": RULESET_NAME, "enforcement": "active"}]
            ),
            "api repos/owner/comic/actions/variables": json.dumps(
                [
                    {"name": n}
                    for n in (
                        "PUBLISHER_APP_CLIENT_ID",
                        "DEEPSEEK_MODEL_FLASH",
                        "DEEPSEEK_MODEL_PRO",
                    )
                ]
            ),
            "api repos/owner/comic/actions/secrets": json.dumps(
                [
                    {"name": n}
                    for n in (
                        "PUBLISHER_APP_PRIVATE_KEY",
                        "CLAUDE_CODE_OAUTH_TOKEN",
                        "DEEPSEEK_API_KEY",
                    )
                ]
            ),
            "api repos/owner/comic/actions/runners": json.dumps(
                [{"status": "online", "labels": []}]
            ),
            "api /user/installations/": json.dumps(["owner/comic", "owner/music"]),
            "api /user/installations": json.dumps(
                [{"id": 7, "app_slug": "agentic-sdlc-publisher"}]
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
    gh.answers["api repos/owner/comic/actions/secrets"] = "[]"
    gh.answers["api repos/owner/comic/actions/runners"] = "[]"
    gh.answers["api /user/installations/"] = json.dumps(["owner/music"])
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    assert not report.ok
    failed = {c.name: c for c in report.checks if not c.ok}
    assert set(failed) == {
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
    gh.answers["api repos/owner/comic/actions/secrets"] = json.dumps(
        [{"name": "CLAUDE_CODE_OAUTH_TOKEN"}]
    )
    gh.answers["api repos/owner/comic/actions/variables"] = "[]"
    gh.answers["api repos/owner/comic/actions/runners"] = "[]"
    gh.answers["api /user/installations"] = "[]"
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
