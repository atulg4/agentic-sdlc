from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from agentic_sdlc.executors import load_executors, load_routing_policy
from agentic_sdlc.onboard import (
    FORK_RUNNER_CHECK,
    GITHUB_ACTIONS_INTEGRATION_ID,
    IMPLEMENTATION_LABEL,
    REUSABLE_IMPLEMENT_AGENTS,
    REUSABLE_IMPLEMENT_DEFAULT_AGENT,
    RULESET_NAME,
    GhCommandError,
    OnboardError,
    OnboardSpec,
    ProducedContexts,
    apply_repo_settings,
    copy_variables,
    doctor,
    merged_ruleset,
    render_hooks,
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
        publisher_bot_login="agentic-sdlc-publisher[bot]",
        # The suite was written for the self-hosted MusicMaestro shape; GitHub-hosted is the
        # default now (Codex 4243193108) and self-hosted needs the acknowledgement:
        # tests/test_owner_defaults.py covers both.
        runs_on=("self-hosted", "linux", "x64"),
        ephemeral_runners=True,
    )
    base.update(overrides)
    return OnboardSpec(**base)


_CONSUMER: dict[str, Path] = {}  # the consumer checkout the current test created
CONSUMER_CONTENTS = "api repos/owner/comic/contents/"
# Codex 4240170623: the workflow directory is enumerated through the Git Trees API (a commit's
# tree, then `.github`, then `workflows`). By default the fake serves each tree from the SAME
# answer the contents API would give for that directory, so a test that answers the contents
# listing (or fails it) answers the tree listing too.
CONSUMER_COMMITS = "api repos/owner/comic/commits/"
CONSUMER_TREES = "api repos/owner/comic/git/trees/"
PLATFORM_CONTENTS = "api repos/owner/agentic-sdlc/contents/"
PLATFORM_WORKFLOWS = Path(__file__).resolve().parents[1] / ".github/workflows"


def _writable(gh: FakeGh) -> None:
    """Answer `GET repos/owner/comic` as a writable repository (`--apply` reads it first, Codex
    4240170627). Added LAST: answers match by prefix in insertion order."""
    gh.answers.pop("api repos/owner/comic", None)
    gh.answers["api repos/owner/comic"] = json.dumps(
        {"full_name": "owner/comic", "archived": False, "disabled": False}
    )


def _blob(data: bytes) -> str:
    """The contents API's answer for a regular file."""
    return json.dumps(
        {"type": "file", "encoding": "base64", "content": base64.b64encode(data).decode()}
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "consumer"
    (repo / ".git").mkdir(parents=True)
    _CONSUMER["path"] = repo
    return repo


CONSUMER_WORKFLOW_STATES = "api repos/owner/comic/actions/workflows"


class FakeGh:
    """Records gh invocations and answers from a canned table keyed by the first few args.

    Unless the table answers it, the consumer's contents API serves the files of the test's
    checkout: by default everything doctor checks locally has also been pushed. The platform's
    contents API serves this repository's own reusable workflows (the real contract) unless the
    table answers or `fail` fails the path."""

    def __init__(self, answers: dict[str, str] | None = None, fail: set[str] | None = None):
        self.calls: list[tuple[tuple[str, ...], str | None]] = []
        self.answers = answers or {}
        self.fail = fail or set()

    def __call__(self, args, input=None):
        self.calls.append((tuple(args), input))
        key = " ".join(args)
        if key.startswith((CONSUMER_COMMITS, CONSUMER_TREES)):
            for prefix, value in self.answers.items():
                if prefix.startswith((CONSUMER_COMMITS, CONSUMER_TREES)) and key.startswith(prefix):
                    if isinstance(value, Exception):
                        raise value
                    return value
            if any(
                prefix.startswith((CONSUMER_COMMITS, CONSUMER_TREES)) and key.startswith(prefix)
                for prefix in self.fail
            ):
                raise OnboardError("boom")
            return self._git_object(key)
        return self._answer(key)

    def _git_object(self, key: str) -> str:
        """A commit (`commits/{ref}`) or a tree (`git/trees/{sha}`) of the consumer, built from
        the contents API's answer for the same directory at the same ref."""
        from urllib.parse import quote, unquote

        trees = self.__dict__.setdefault("trees", {})

        def tree_sha(ref: str, rel: str) -> str:
            sha = hashlib.sha1(f"{ref}\0{rel}".encode()).hexdigest()
            trees[sha] = (ref, rel)
            return sha

        if key.startswith(CONSUMER_COMMITS):
            ref = unquote(key[len(CONSUMER_COMMITS) :])
            return json.dumps({"sha": "c" * 40, "commit": {"tree": {"sha": tree_sha(ref, "")}}})
        sha = unquote(key[len(CONSUMER_TREES) :])
        if sha not in trees:
            raise GhCommandError("gh: Not Found (HTTP 404)", http_status=404)
        ref, rel = trees[sha]
        raw = self._answer(CONSUMER_CONTENTS + f"{rel}?ref={quote(ref, safe='')}")
        try:
            listing = json.loads(raw)
        except json.JSONDecodeError:
            return raw
        if not isinstance(listing, list):
            return raw
        kinds = {"file": ("blob", "100644"), "dir": ("tree", "040000")}
        kinds |= {"symlink": ("blob", "120000"), "submodule": ("commit", "160000")}
        entries = []
        for entry in listing:
            name = str(entry.get("name"))
            kind, mode = kinds.get(entry.get("type"), (str(entry.get("type")), "100644"))
            child = f"{rel}/{name}" if rel else name
            sha_of = tree_sha(ref, child) if kind == "tree" else hashlib.sha1(child.encode())
            entries.append(
                {
                    "path": name,
                    "mode": mode,
                    "type": kind,
                    "sha": sha_of if isinstance(sha_of, str) else sha_of.hexdigest(),
                }
            )
        return json.dumps({"sha": sha, "tree": entries, "truncated": False})

    def _answer(self, key: str) -> str:
        if key.startswith(CONSUMER_CONTENTS):
            for prefix, value in self.answers.items():
                if prefix.startswith(CONSUMER_CONTENTS) and key.startswith(prefix):
                    if isinstance(value, Exception):
                        raise value
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
                raise GhCommandError("gh: Not Found (HTTP 404)", http_status=404)
            return _blob(path.read_bytes())
        if key.startswith(PLATFORM_CONTENTS):
            for prefix, value in self.answers.items():
                if prefix.startswith(PLATFORM_CONTENTS) and key.startswith(prefix):
                    if isinstance(value, Exception):
                        raise value
                    return value
            if any(key.startswith(prefix) for prefix in self.fail):
                raise GhCommandError("gh: Not Found (HTTP 404)", http_status=404)
            name = key[len(PLATFORM_CONTENTS) :].split("?")[0].removeprefix(".github/workflows/")
            path = PLATFORM_WORKFLOWS / name
            if "/" in name or not path.is_file():
                raise GhCommandError("gh: Not Found (HTTP 404)", http_status=404)
            return _blob(path.read_bytes())
        if key == "api repos/owner/agentic-sdlc --jq .private" and not any(
            key.startswith(prefix) for prefix in self.answers
        ):
            return "false\n"  # a public platform unless the test answers otherwise
        if key.startswith(CONSUMER_WORKFLOW_STATES):
            for prefix, value in self.answers.items():
                if prefix.startswith(CONSUMER_WORKFLOW_STATES) and key.startswith(prefix):
                    if isinstance(value, Exception):
                        raise value
                    return value
            # Every workflow file of the checkout and of an answered remote listing, active.
            names = {p.name for p in (_CONSUMER["path"] / ".github/workflows").glob("*.y*ml")}
            for prefix, value in self.answers.items():
                if prefix.startswith(CONSUMER_CONTENTS + ".github/workflows?"):
                    try:
                        listing = json.loads(value) if isinstance(value, str) else []
                    except json.JSONDecodeError:
                        listing = []
                    names |= {
                        str(e.get("name"))
                        for e in (listing if isinstance(listing, list) else [])
                        if isinstance(e, dict)
                    }
            rows = [{"path": f".github/workflows/{n}", "state": "active"} for n in sorted(names)]
            return json.dumps({"total_count": len(rows), "workflows": rows})
        for prefix, value in self.answers.items():
            if key.startswith(prefix):
                if isinstance(value, Exception):
                    raise value
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
            # the workflow placeholders; the guard names GIT_SSH_COMMAND etc. on purpose
            "SETUP_COMMAND",
            "QUALITY_COMMAND",
            "TEST_COMMAND",
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
    cond = doc["jobs"]["authorize"]["if"]
    for label in ("claude-ready", "human-review-required", IMPLEMENTATION_LABEL):
        assert f"contains(github.event.issue.labels.*.name, '{label}')" in cond
    assert (
        doc["jobs"]["implement"]["with"]["issue_number"]
        == "${{ format('{0}', github.event.issue.number) }}"
    )
    assert doc["jobs"]["implement"]["with"]["agent"] == "route"
    assert doc["jobs"]["implement"]["with"]["executor_registry_path"] == ".forge/executors.json"
    assert doc["jobs"]["implement"]["needs"] == ["authorize", "preflight"]


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


def test_routed_preflight_checks_no_provider_credential():
    """Codex 4246082570: which providers the router can select depends on the INSTALLED registry
    and routing policy, which the owner may trim; the caller's preflight cannot run the router,
    so it checks only the Publisher App -- never a hard-coded provider."""
    for name in ("agent-implement.yml", "agent-auto-implement.yml"):
        doc = yaml.safe_load(render_onboarding(spec())[f".github/workflows/{name}"])
        run = doc["jobs"]["preflight"]["steps"][0]["run"]
        checked = re.findall(r'test -n "\$([A-Z0-9_]+)"', run)
        assert checked == ["PUBLISHER_APP_CLIENT_ID", "PUBLISHER_APP_PRIVATE_KEY"], name


def test_doctor_requires_only_the_credentials_a_trimmed_registry_can_select(tmp_path):
    """Codex 4246082570: with DeepSeek trimmed from the registry and the routing policy, doctor
    (which reads the preflight's `test -n` lines as requirements, and the router's selectable
    executors) demands no DeepSeek credential -- and still demands the remaining providers'."""
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    registry_path = repo / ".forge/executors.json"
    registry = json.loads(registry_path.read_text())
    registry["executors"] = [e for e in registry["executors"] if e["provider"] != "deepseek"]
    registry_path.write_text(json.dumps(registry, indent=2) + "\n")
    policy_path = repo / ".forge/routing-policy.json"
    policy = json.loads(policy_path.read_text())
    policy["allowedProviders"] = [p for p in policy["allowedProviders"] if p != "deepseek"]
    policy["preferredModelAliases"] = {
        key: [alias for alias in aliases if not alias.startswith("deepseek")]
        for key, aliases in policy["preferredModelAliases"].items()
    }
    policy_path.write_text(json.dumps(policy, indent=2) + "\n")
    gh = _healthy_gh()
    gh.answers["api repos/owner/comic/actions/variables"] = _var_pages(
        "PUBLISHER_APP_CLIENT_ID", "ZAI_MODEL_GLM", "KIMI_MODEL_K3"
    )
    gh.answers["api repos/owner/comic/actions/secrets"] = _secret_pages(
        "PUBLISHER_APP_PRIVATE_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ZAI_API_KEY", "KIMI_API_KEY"
    )
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    assert report.ok, report.render()
    rendered = report.render()
    assert "DEEPSEEK" not in rendered
    # the providers the trimmed registry still routes to remain required
    gh.answers["api repos/owner/comic/actions/secrets"] = _secret_pages(
        "PUBLISHER_APP_PRIVATE_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ZAI_API_KEY"
    )
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    secrets = next(c for c in report.checks if c.name == "repo secrets set (by name)")
    assert not secrets.ok and "KIMI_API_KEY" in secrets.detail


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
    _writable(gh)
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
        {"context": "test", "integration_id": GITHUB_ACTIONS_INTEGRATION_ID}
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
            "api repos/owner/comic/rulesets": json.dumps([[{"id": 5, "name": RULESET_NAME}]]),
        }
    )
    _writable(gh2)
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


def _effective_rules(*extra: dict) -> str:
    """`gh api repos/.../rules/branches/main --paginate --slurp`: Forge's own rules, as the
    repository's ruleset applies them, plus `extra` (inherited) rules."""
    own = [
        {**rule, "ruleset_source_type": "Repository", "ruleset_source": "owner/comic"}
        for rule in ruleset_payload(spec())["rules"]
    ]
    return json.dumps([[*own, *extra]])


AGENT_BRANCH_RULES = "api repos/owner/comic/rules/branches/agent%2F"
CLOUD_BRANCH_RULES = "api repos/owner/comic/rules/branches/forge%2F"
CLASSIC_RULES = "api graphql"


def _classic_rules(*nodes: dict, more: bool = False) -> str:
    """The GraphQL answer listing the repository's classic branch-protection rules."""
    rules = {"pageInfo": {"hasNextPage": more}, "nodes": list(nodes)}
    return json.dumps({"data": {"repository": {"branchProtectionRules": rules}}})


def _healthy_gh() -> FakeGh:
    return FakeGh(
        {
            # before "api repos/owner/comic": answers match by prefix, in this order
            "api repos/owner/comic/branches/main/protection": GhCommandError(
                "gh: Branch not protected (HTTP 404)", http_status=404
            ),
            "api repos/owner/comic/rules/branches/main": _effective_rules(),
            # the publisher's agent/... branches: no ruleset targets them
            AGENT_BRANCH_RULES: json.dumps([[]]),
            # nor the cloud routine's forge/issue-<N> branches (cloud-routine mode)
            CLOUD_BRANCH_RULES: json.dumps([[]]),
            # ... and no classic branch-protection pattern either
            CLASSIC_RULES: _classic_rules(),
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
                [[{"id": 11, "name": RULESET_NAME, "enforcement": "active"}]]
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
            "api repos/owner/comic/actions/permissions": json.dumps(
                {"enabled": True, "allowed_actions": "all"}
            ),
            "api repos/owner/comic": json.dumps(
                {
                    "full_name": "owner/comic",
                    "default_branch": "main",
                    "visibility": "private",
                    "allow_forking": False,
                    "has_issues": True,
                    "archived": False,
                    "disabled": False,
                }
            ),
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
    # Codex 4239868034: nothing here can see the claude.ai routine -- an owner TODO until the
    # owner records it, never READY on a document alone
    failed = _failed(report)
    assert set(failed) == {"cloud implementer routine is scheduled"}, report.render()
    assert failed["cloud implementer routine is scheduled"].manual
    assert "sdlcctl confirm-cloud-routine" in report.render() and not report.ok
    from agentic_sdlc.onboard import confirm_cloud_routine

    confirm_cloud_routine(repo)
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
    # Codex 4240306498: NOT READY under --local until the owner confirms the routine
    capsys.readouterr()
    assert main(["doctor", "--destination", str(repo), "--local"]) == 2
    assert "sdlcctl confirm-cloud-routine" in capsys.readouterr().out
    assert main(["confirm-cloud-routine", "--destination", str(repo)]) == 0
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
    _trust_fake_gh(guard)
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


_NO_SESSION = [
    pytest.param({}, id="missing"),
    pytest.param({"session_id": None}, id="null"),
    pytest.param({"session_id": ""}, id="empty"),
    pytest.param({"session_id": "   "}, id="blank"),
    pytest.param({"session_id": 123}, id="int"),
    pytest.param({"session_id": ["s1"]}, id="list"),
    pytest.param({"session_id": {"id": "s1"}}, id="dict"),
]


@pytest.mark.parametrize("session_field", _NO_SESSION)
@pytest.mark.parametrize("lease_session", ["s1", "", "123"])
def test_commit_guard_refuses_a_live_lease_without_a_usable_session_id(
    tmp_path, session_field, lease_session
):
    """Codex 4240101138: a payload without a non-empty string session_id must not have any
    live lease accepted as its own -- the guard fails closed on issue branches."""
    from datetime import UTC, datetime, timedelta

    guard = _guard_module(tmp_path, "forge_commit_guard")
    lease = {
        "agent": "claude-code",
        "session": lease_session,
        "expires": datetime.now(UTC) + timedelta(hours=1),
    }
    payload = {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}, **session_field}
    code, msg = guard.decide(payload, "forge/issue-7", lambda n: lease, lambda b: False)
    assert code == 2 and "no usable session_id" in msg
    # not an issue branch: still unguarded
    assert guard.decide(payload, "main", lambda n: lease, lambda b: False) == (0, "")


def test_commit_guard_matches_a_lease_only_on_the_exact_session_id(tmp_path):
    from datetime import UTC, datetime, timedelta

    guard = _guard_module(tmp_path, "forge_commit_guard")
    lease = {
        "agent": "claude-code",
        "session": "s1",
        "expires": datetime.now(UTC) + timedelta(hours=1),
    }
    commit = {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}}
    assert guard.decide({**commit, "session_id": "s1"}, "forge/issue-7", lambda n: lease)[0] == 0
    for other in ("s2", "S1", "s1 ", " s1"):
        code, msg = guard.decide({**commit, "session_id": other}, "forge/issue-7", lambda n: lease)
        assert code == 2 and "is leased by agent=claude-code session=s1" in msg
    assert guard.payload_session({"session_id": "s1"}) == "s1"
    assert guard.payload_session({"session_id": 1}) == ""


_BAD_HOOK_PAYLOADS = [
    pytest.param("", id="empty"),
    pytest.param('{"tool_name": "Bash", "tool_input": {"command": "git com', id="truncated"),
    pytest.param("not json", id="not-json"),
    pytest.param("[]", id="list"),
    pytest.param('"Bash"', id="string"),
    pytest.param("null", id="null"),
    pytest.param("{}", id="no-tool"),
    pytest.param('{"tool_name": 7}', id="tool-not-string"),
    pytest.param('{"tool_name": "Bash"}', id="bash-no-input"),
    pytest.param('{"tool_name": "Bash", "tool_input": "git commit"}', id="input-not-object"),
    pytest.param('{"tool_name": "Bash", "tool_input": {}}', id="bash-no-command"),
    pytest.param('{"tool_name": "Bash", "tool_input": {"command": ["git"]}}', id="command-list"),
    pytest.param(
        '{"tool_name": "Bash", "tool_input": {"command": "git commit"}, "cwd": 3}', id="cwd-int"
    ),
]


@pytest.mark.parametrize("stdin", _BAD_HOOK_PAYLOADS)
def test_commit_guard_blocks_a_payload_it_cannot_read(tmp_path, monkeypatch, capsys, stdin):
    """Codex 4240306495: a malformed hook payload used to exit 0 (allow) before any analysis,
    so an unguarded git command could run on an issue branch. It now blocks with a reason."""
    import io

    guard = _guard_module(tmp_path, "forge_commit_guard")
    monkeypatch.setattr(guard, "lease_for", lambda n: None)
    monkeypatch.setattr(guard.sys, "stdin", io.StringIO(stdin))
    assert guard.main() == 2
    err = capsys.readouterr().err
    assert "Forge guard: the PreToolUse hook payload" in err and "fail closed" in err


def test_commit_guard_lets_another_tool_through_unread(tmp_path, monkeypatch, capsys):
    import io

    guard = _guard_module(tmp_path, "forge_commit_guard")
    for payload in (
        {"tool_name": "Edit", "tool_input": {"file_path": "x"}},
        {"tool_name": "Read", "tool_input": "anything"},
        {"tool_name": "Bash", "session_id": "s1", "tool_input": {"command": "ls"}},
    ):
        monkeypatch.setattr(guard.sys, "stdin", io.StringIO(json.dumps(payload)))
        assert guard.main() == 0
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("session_field", _NO_SESSION)
def test_session_start_claims_no_lease_without_a_usable_session_id(
    tmp_path, monkeypatch, session_field
):
    """Codex 4240101138 (SessionStart side): no usable session id means no lease is reported
    as this session's -- every live one is listed as another agent's."""
    import io

    start = _guard_module(tmp_path, "forge_session_start")
    issues = [{"number": 5, "title": "held", "assignees": []}]
    for lease_session in ("", "123", "None"):
        leases = {5: {"session": lease_session}}
        session = session_field.get("session_id")
        live, stale, mine = start.classify_leased(issues, leases.get, session)
        assert mine == [] and [x.split()[0] for x in live] == ["#5"]

    leases = {5: {"session": "123"}}

    def run(cmd):
        return json.dumps(issues) if _is_issue_page(cmd) else ""

    monkeypatch.setattr(start, "run", run)
    monkeypatch.setattr(start, "_lease_lookup", lambda: leases.get)
    monkeypatch.setattr(start.sys, "stdin", io.StringIO(json.dumps(session_field)))
    out = io.StringIO()
    monkeypatch.setattr(start.sys, "stdout", out)
    assert start.main() == 0
    text = out.getvalue()
    assert "Your current lease" not in text and "#5 held" in text


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
            "api repos/owner/comic/rulesets": json.dumps([[stale]]),
        }
    )
    _writable(gh)
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
    gh2.fail = {PLATFORM_CONTENTS + ".github/workflows/reusable-implement.yml"}
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
    gh.answers["api repos/owner/comic"] = json.dumps(
        {
            "full_name": "owner/comic",
            "default_branch": "master",
            "visibility": "private",
            "allow_forking": False,
            "has_issues": True,
            "archived": False,
            "disabled": False,
        }
    )
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
    _trust_fake_gh(guard)
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
    assert notify["needs"] == ["dedupe", "plan"]
    assert "always()" in notify["if"] and "needs.plan.result == 'failure'" in notify["if"]
    assert "needs.dedupe.result == 'failure'" in notify["if"]  # unreadable comments: reported
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
    _trust_fake_gh(guard)

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
        # deny by default: `-c` can set core.pager / diff.external, so even `log` needs the lease
        "git -c commit.gpgsign=false log",
    ):
        assert code(blocked) == 2, blocked
    for allowed in ("git -C . status", "git --no-pager log", "git log --grep commit"):
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


def _is_issue_page(cmd) -> bool:
    """A session-start read of one page of the open in-progress issues (REST)."""
    return cmd[:2] == ["gh", "api"] and "/issues?state=open&labels=in-progress" in cmd[2]


def _trust_fake_gh(module):
    """Point a loaded guard's gh calls at a bare `gh` with no token lookup, so a test's fake
    `subprocess.run` answers them (Codex 4242095556: the guard itself only ever runs a vetted
    absolute gh -- `test_commit_guard_runs_only_a_trusted_gh` covers that)."""
    if hasattr(module, "_TRUSTED"):
        module._TRUSTED["gh"] = "gh"
        module._GH_STATE["token"] = ""
    return module


def _guard_module(tmp_path, name):
    import importlib.util

    path = tmp_path / f"{name}.py"
    path.write_text(render_onboarding(spec())[f".claude/hooks/{name}.py"])
    module_spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return _trust_fake_gh(module)


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
            "api repos/owner/comic/rulesets": json.dumps([[org_owned]]),
        }
    )
    _writable(gh)
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
        # the inherited one is read too (does it target only some agent/** branches?)
        "api repos/owner/comic/rulesets/99": json.dumps({**org_owned, **ruleset_payload(spec())}),
        **healthy.answers,
    }
    healthy.answers["api repos/owner/comic/rulesets"] = json.dumps([[org_owned]])
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
        return json.dumps(issues) if _is_issue_page(cmd) else ""

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
    monkeypatch.setattr(start, "run", lambda cmd: calls.append(cmd) or "[]")
    monkeypatch.setattr(start.sys, "stdin", io.StringIO("{}"))
    assert start.main() == 0
    listing = next(c for c in calls if _is_issue_page(c))
    assert "per_page=100" in listing[2] and "page=1" in listing[2]


def _issue_rows(first: int, count: int) -> list:
    return [{"number": n, "title": f"t{n}", "assignees": []} for n in range(first, first + count)]


def _is_comment_page(cmd) -> bool:
    """A session-start read of one page of the repository's recent issue comments (REST)."""
    return cmd[:2] == ["gh", "api"] and "/issues/comments?since=" in cmd[2]


def _run_session_start(start, monkeypatch, pages, leases=None, comments=None, details=None):
    """Run main() with the in-progress listing served from `pages` (page number -> JSON text or
    None), the recent comments from `comments` (likewise; default: none) and single issues from
    `details` (number -> row); returns (stdout, the page numbers requested)."""
    import io

    asked = []

    def run(cmd):
        if _is_issue_page(cmd):
            page = int(cmd[2].rsplit("page=", 1)[1])
            asked.append(page)
            return pages(page)
        if _is_comment_page(cmd):
            return (comments or (lambda page: "[]"))(int(cmd[2].rsplit("page=", 1)[1]))
        number = cmd[2].rsplit("/issues/", 1)[-1] if cmd[:2] == ["gh", "api"] else ""
        if number.isdigit():
            row = (details or {}).get(int(number))
            return json.dumps(row) if row is not None else None
        return ""

    monkeypatch.setattr(start, "run", run)
    monkeypatch.setattr(start, "_lease_lookup", lambda: (leases or {}).get)
    monkeypatch.setattr(start.sys, "stdin", io.StringIO(json.dumps({"session_id": "me"})))
    out = io.StringIO()
    monkeypatch.setattr(start.sys, "stdout", out)
    assert start.main() == 0
    return out.getvalue(), asked


def test_session_start_paginates_past_a_thousand_leases(tmp_path, monkeypatch):
    """Codex 4240306503: `gh issue list --limit 1000` silently dropped every lease past the
    1000th. The REST listing is read page by page until a short page; PRs are skipped."""
    start = _guard_module(tmp_path, "forge_session_start")
    rows = {p: _issue_rows(1 + (p - 1) * 100, 100) for p in range(1, 12)}
    rows[12] = _issue_rows(1101, 7) + [{"number": 9999, "title": "pr", "pull_request": {}}]
    leases = {n: {"session": "other"} for n in range(1, 1108)}
    text, asked = _run_session_start(
        start, monkeypatch, lambda p: json.dumps(rows.get(p, [])), leases
    )
    assert asked == list(range(1, 13))
    others = text.split("other agents (do NOT work on these):\n")[1].split("[Forge]")[0]
    assert "#1107 t1107" in others and "#1001 t1001" in others and "#9999" not in others
    assert "INCOMPLETE" not in text and "lease inventory unavailable" not in text


@pytest.mark.parametrize("failure", ["unreadable", "cap"])
def test_session_start_says_the_lease_inventory_is_incomplete(tmp_path, monkeypatch, failure):
    """Codex 4240306503: a later page that cannot be read, or the page cap reached with pages
    still full, is reported as an INCOMPLETE inventory -- never as the whole list."""
    start = _guard_module(tmp_path, "forge_session_start")
    if failure == "unreadable":

        def pages(p):
            return json.dumps(_issue_rows(1 + (p - 1) * 100, 100)) if p == 1 else None

    else:

        def pages(p):
            return json.dumps(_issue_rows(1 + (p - 1) * 100, 100))

    leases = {n: {"session": "other"} for n in range(1, 100 * start.ISSUE_PAGE_CAP + 1)}
    text, asked = _run_session_start(start, monkeypatch, pages, leases)
    every_page = list(range(1, start.ISSUE_PAGE_CAP + 1))
    assert asked == ([1, 2] if failure == "unreadable" else every_page)
    assert "lease inventory INCOMPLETE" in text and "sdlcctl claims" in text
    others = text.split("other agents (do NOT work on these):\n")[1].split("[Forge]")[0]
    assert "#1 t1" in others and "INCOMPLETE" in others and "  none" not in others


def _marker_comment_row(number: int, session: str = "other") -> dict:
    body = f"<!-- forge-claim agent=a session={session} branch=b expires=2099-01-01T00:00:00Z -->"
    return {
        "body": body + "\nclaimed",
        "issue_url": f"https://api.github.com/repos/owner/comic/issues/{number}",
    }


def test_session_start_lists_a_live_lease_whose_label_was_removed(tmp_path, monkeypatch):
    """Codex 4240839923: the in-progress label is only a hint. An issue whose label was removed
    but whose claim marker is live is still listed as held; a PR or a closed issue with a marker
    is not, and a marker issue with no live lease is not reported as a stale label."""
    start = _guard_module(tmp_path, "forge_session_start")
    comments = [
        _marker_comment_row(40),
        _marker_comment_row(41),
        _marker_comment_row(42),
        _marker_comment_row(43),
        {
            "body": "just chatting",
            "issue_url": "https://api.github.com/repos/owner/comic/issues/44",
        },
    ]
    details = {
        40: {"number": 40, "title": "unlabelled", "state": "open", "assignees": []},
        41: {"number": 41, "title": "a pr", "state": "open", "pull_request": {}},
        42: {"number": 42, "title": "closed", "state": "closed", "assignees": []},
        43: {"number": 43, "title": "released", "state": "open", "assignees": []},
    }
    leases = {40: {"session": "other"}, 41: {"session": "other"}, 42: {"session": "other"}}
    text, _ = _run_session_start(
        start,
        monkeypatch,
        lambda p: "[]",
        leases,
        comments=lambda p: json.dumps(comments if p == 1 else []),
        details=details,
    )
    others = text.split("other agents (do NOT work on these):\n")[1].split("[Forge]")[0]
    assert "#40 unlabelled" in others and "in-progress label missing" in others
    assert "#41" not in text and "#42" not in text and "#43" not in text and "#44" not in text
    assert "INCOMPLETE" not in text and "lease inventory unavailable" not in text


@pytest.mark.parametrize("failure", ["unreadable", "cap"])
def test_session_start_is_incomplete_when_marker_comments_cannot_be_read(
    tmp_path, monkeypatch, failure
):
    """Codex 4240839923: the recent comments are part of the inventory; an unreadable page or
    the page cap reached makes it INCOMPLETE, never the whole list."""
    start = _guard_module(tmp_path, "forge_session_start")
    if failure == "unreadable":

        def comments(page):
            return None

    else:

        def comments(page):
            return json.dumps([{"body": "chat", "issue_url": "x/issues/1"}] * 100)

    text, _ = _run_session_start(start, monkeypatch, lambda p: "[]", comments=comments)
    assert "lease inventory INCOMPLETE" in text
    if failure == "unreadable":
        assert "the recent lease-marker comments could not be read" in text
    others = text.split("other agents (do NOT work on these):\n")[1].split("[Forge]")[0]
    assert "INCOMPLETE" in others and "  none" not in others


@pytest.mark.parametrize("stdin", ["", "not json", "[1, 2]", '"text"', "null", '{"session_id"'])
def test_session_start_reports_an_unreadable_payload_without_failing(tmp_path, monkeypatch, stdin):
    """A SessionStart payload that is not a JSON object never blocks or crashes the session; it
    is reported, and no lease is taken as this session's."""
    import io

    start = _guard_module(tmp_path, "forge_session_start")
    monkeypatch.setattr(
        start,
        "run",
        lambda cmd: json.dumps(_issue_rows(5, 1)) if _is_issue_page(cmd) else "",
    )
    monkeypatch.setattr(start, "_lease_lookup", lambda: {5: {"session": ""}}.get)
    monkeypatch.setattr(start.sys, "stdin", io.StringIO(stdin))
    out = io.StringIO()
    monkeypatch.setattr(start.sys, "stdout", out)
    assert start.main() == 0
    text = out.getvalue()
    assert "could not read the SessionStart hook payload" in text
    assert "Your current lease" not in text and "#5 t5" in text


@pytest.mark.parametrize("fetch_ok", [True, False])
def test_session_start_warns_when_origin_cannot_be_fetched(tmp_path, monkeypatch, fetch_ok):
    """Codex 4206492773: a failed fetch must not present the cached origin ref as current."""
    import io

    start = _guard_module(tmp_path, "forge_session_start")
    calls = []

    def run(cmd):
        calls.append(cmd)
        if cmd[:2] == ["git", "fetch"]:
            return "" if fetch_ok else None
        if cmd[:2] == ["git", "rev-list"]:
            return "3"
        if cmd[:2] == ["git", "log"]:
            return "abc1234 cached commit"
        return "[]" if _is_issue_page(cmd) else ""

    monkeypatch.setattr(start, "run", run)
    monkeypatch.setattr(start, "fetch_blocker", lambda: None)
    monkeypatch.setattr(start, "log_blocker", lambda: None)
    monkeypatch.setattr(start, "_lease_lookup", lambda: {}.get)
    monkeypatch.setattr(start.sys, "stdin", io.StringIO("{}"))
    out = io.StringIO()
    monkeypatch.setattr(start.sys, "stdout", out)
    assert start.main() == 0
    text = out.getvalue()
    assert [c for c in calls if c[:2] == ["git", "fetch"]] == [start.fetch_command()]
    if fetch_ok:
        assert "HEAD is 3 commit(s) behind origin/main" in text
        assert "could not fetch origin" not in text
    else:
        assert "WARNING: could not fetch origin" in text and "may be stale" in text
        assert "3 commit(s) behind" not in text  # no stale count presented as current
        assert not any(c[:2] == ["git", "rev-list"] for c in calls)
        assert "CACHED -- possibly stale" in text and "abc1234 cached commit" in text


def test_cli_onboard_validates_var_before_writing_any_file(tmp_path, monkeypatch):
    from agentic_sdlc import cli

    monkeypatch.setattr(cli, "apply_repo_settings", lambda *a, **k: pytest.fail("applied"))
    monkeypatch.setattr(cli, "canonical_project_id", lambda project: project)
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
    monkeypatch.setattr(cli, "canonical_project_id", lambda project: project)
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
    "runs_on", ["ubuntu-latest", "ubuntu-24.04-arm", "ubuntu-26.04", "ubuntu-26.04-arm", "macos-15"]
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


def test_auto_plan_triggers_on_either_planning_label():
    """Codex 4241196642: either planning label triggers (re-adding a removed one restarts the
    planning its removal cancelled); both are required. Duplicates: the `dedupe` job."""
    doc = yaml.safe_load(render_onboarding(spec())[".github/workflows/agent-auto-plan.yml"])
    cond = doc["jobs"]["preflight"]["if"]
    assert "github.event.label.name == 'claude-ready'" in cond
    assert "github.event.label.name == 'human-review-required'" in cond
    assert "contains(github.event.issue.labels.*.name, 'human-review-required')" in cond
    assert "contains(github.event.issue.labels.*.name, 'claude-ready')" in cond


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
    gh.answers[CONSUMER_CONTENTS] = GhCommandError(
        "gh: Not Found (HTTP 404)", http_status=404
    )  # nothing pushed
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))
    # with no workflows on the default branch, its required `test` context is unverifiable too
    rules = "effective default-branch rules (org/enterprise rulesets too) fit Forge"
    assert set(failed) == {"managed files are on the default branch", rules}
    assert failed[rules].manual and "cannot be verified" in failed[rules].detail
    detail = failed["managed files are on the default branch"].detail
    assert ".github/workflows/ci.yml (missing)" in detail and "push these files first" in detail
    gh = _healthy_gh()
    plan = repo / ".github/workflows/agent-plan.yml"
    gh.answers[CONSUMER_CONTENTS + ".github/workflows/agent-plan.yml"] = json.dumps(
        {
            "type": "file",
            "encoding": "base64",
            "content": base64.b64encode(plan.read_bytes()).decode(),
        }
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
    assert "agent-plan.yml:plan > reusable-plan.yml:plan" in (
        failed["self-hosted runner online for this repo"].detail
    )


# ---------------------------------------------------------------- review regressions (PR 139, 4)


def test_public_repository_ci_defaults_to_a_github_hosted_runner():
    files = render_onboarding(spec(fork_exposed=True))
    ci = yaml.safe_load(files[".github/workflows/ci.yml"])
    assert ci["jobs"]["test"]["runs-on"] == ["ubuntu-latest"]
    # issue-triggered agent workflows keep the self-hosted target: they run no fork code
    plan = yaml.safe_load(files[".github/workflows/agent-plan.yml"])
    assert plan["jobs"]["preflight"]["runs-on"] == ["self-hosted", "linux", "x64"]
    assert spec(fork_exposed=True, ci_runs_on=("macos-15",)).ci_labels == ("macos-15",)
    with pytest.raises(OnboardError, match="fork-exposed"):
        spec(fork_exposed=True, ci_runs_on=("self-hosted", "linux", "x64"))
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
    check = failed[FORK_RUNNER_CHECK]
    assert not check.manual
    assert "ci.yml:test" in check.detail and "lint.yml:lint" in check.detail
    # issue-triggered callers are not pull-request code
    assert "agent-plan.yml" not in check.detail


def test_doctor_accepts_a_public_repository_onboarded_with_hosted_ci(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec(fork_exposed=True))
    gh = _healthy_gh()
    gh.answers["api repos/owner/comic"] = json.dumps(
        {
            "full_name": "owner/comic",
            "default_branch": "main",
            "visibility": "public",
            "private": False,
            "has_issues": True,
            "archived": False,
            "disabled": False,
        }
    )
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    assert report.ok, report.render()
    assert any(c.name == FORK_RUNNER_CHECK for c in report.checks)


def test_cli_onboard_reads_visibility_and_refuses_self_hosted_ci_for_public_repos(
    tmp_path, monkeypatch
):
    from agentic_sdlc import cli

    monkeypatch.setattr(cli, "repository_fork_exposed", lambda project: True)
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

    monkeypatch.setattr(cli, "repository_fork_exposed", unreadable)
    assert cli.main([*base, "--force"]) != 0  # visibility unknown: ask, never guess


FORK_POLICY = "api repos/o/r/actions/permissions/fork-pr-workflows-private-repos"


def _repo_gh(repo: dict, fork_policy: str | None = None):
    """`repos/o/r` answers `repo`; the fork-PR policy answers `fork_policy`, or 404s (None)."""

    def gh(args, input=None):
        key = " ".join(args)
        if key == FORK_POLICY:
            if fork_policy is None:
                raise GhCommandError("gh: Not Found (HTTP 404)", http_status=404)
            return fork_policy
        assert key == "api repos/o/r", key
        return json.dumps(repo)

    return gh


@pytest.mark.parametrize(
    ("repo", "fork_policy", "state"),
    [
        ({"visibility": "public"}, None, "exposed"),
        ({"private": False}, None, "exposed"),
        # private, forkable, and the fork-PR workflow policy runs them: exposed
        (
            {"visibility": "private", "allow_forking": True},
            '{"run_workflows_from_fork_pull_requests": true}',
            "exposed",
        ),
        # private, forkable, policy unreadable (403/404): unknown, never safe
        ({"visibility": "private", "allow_forking": True}, None, "unknown"),
        # private with forking disabled: safe without consulting the policy
        ({"visibility": "private", "allow_forking": False}, None, "safe"),
        # forkable, but fork-PR workflows proven off: safe
        (
            {"visibility": "private", "allow_forking": True},
            '{"run_workflows_from_fork_pull_requests": false}',
            "safe",
        ),
        # internal, forking unknown, policy unreadable: unknown
        ({"visibility": "internal"}, None, "unknown"),
        ({"default_branch": "main"}, None, "unknown"),
    ],
)
def test_fork_pull_request_exposure(repo, fork_policy, state):
    from agentic_sdlc.onboard import fork_pull_request_exposure

    gh = _repo_gh(repo, fork_policy)
    assert fork_pull_request_exposure(repo, gh, "o/r")[0] == state


def test_repository_fork_exposed_is_false_only_when_forks_provably_cannot_run_workflows():
    from agentic_sdlc.onboard import repository_fork_exposed

    assert repository_fork_exposed("o/r", _repo_gh({"visibility": "public"}))
    assert repository_fork_exposed("o/r", _repo_gh({"private": True, "allow_forking": True}))
    assert repository_fork_exposed("o/r", _repo_gh({"visibility": "internal"}))
    assert not repository_fork_exposed("o/r", _repo_gh({"private": True, "allow_forking": False}))
    with pytest.raises(OnboardError):
        repository_fork_exposed("o/r", lambda args, input=None: "{}")


@pytest.mark.parametrize(
    ("repo_info", "fork_policy", "manual"),
    [
        # private + allow_forking: true, fork-PR workflows enabled -> FAIL
        (
            {"visibility": "private", "allow_forking": True},
            '{"run_workflows_from_fork_pull_requests": true}',
            False,
        ),
        # internal, fork policy unreadable -> TODO, never READY
        ({"visibility": "internal"}, None, True),
        # visibility unreadable -> TODO
        ({}, None, True),
    ],
)
def test_doctor_fails_closed_on_a_forkable_private_repository_with_self_hosted_pr_jobs(
    tmp_path, repo_info, fork_policy, manual
):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())  # ci.yml on self-hosted
    gh = _healthy_gh()
    key = "api repos/owner/comic/actions/permissions/fork-pr-workflows-private-repos"
    gh.answers = {key: fork_policy or "", **gh.answers}
    gh.answers["api repos/owner/comic"] = json.dumps({"default_branch": "main", **repo_info})
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    assert not report.ok and "NOT READY" in report.render()
    check = _failed(report)[FORK_RUNNER_CHECK]
    assert check.manual is manual
    assert "ci.yml:test" in check.detail


def test_doctor_accepts_self_hosted_pr_jobs_when_fork_workflows_are_proven_off(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _healthy_gh()
    key = "api repos/owner/comic/actions/permissions/fork-pr-workflows-private-repos"
    gh.answers = {key: '{"run_workflows_from_fork_pull_requests": false}', **gh.answers}
    gh.answers["api repos/owner/comic"] = json.dumps(
        {
            "full_name": "owner/comic",
            "default_branch": "main",
            "visibility": "internal",
            "allow_forking": True,
            "has_issues": True,
            "archived": False,
            "disabled": False,
        }
    )
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    assert report.ok, report.render()
    # forking disabled outright (the healthy fixture) passes without reading the fork policy
    gh = _healthy_gh()
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    assert report.ok, report.render()
    assert not any("fork-pr-workflows" in " ".join(args) for args, _ in gh.calls)


def test_runner_labels_render_as_yaml_strings():
    labels = ("true", "null", "on", "123")
    agent_labels = ("linux", *labels)  # implementation is Linux-only
    files = render_onboarding(spec(runs_on=agent_labels, ci_runs_on=labels))
    seen = 0
    for name, content in files.items():
        if not name.endswith(".yml"):
            continue
        want = list(labels) if name.endswith("/ci.yml") else list(agent_labels)
        for job_name, job in (yaml.safe_load(content).get("jobs") or {}).items():
            if (
                (name.endswith("/agent-auto-plan.yml") and job_name in ("preflight", "dedupe"))
                or (name.endswith("/agent-auto-implement.yml") and job_name == "authorize")
                or job_name == "authorization_failed"
            ):
                assert job["runs-on"] == "ubuntu-latest"  # authorizes before RUNS_ON is used
                continue
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
            "api repos/owner/comic/rulesets": json.dumps([[strong]]),
        }
    )
    _writable(gh)
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


def test_ruleset_update_keeps_repeated_rules_of_one_type():
    """GitHub permits repeated restrictions of one type (Codex 4246082551): two
    `commit_message_pattern` rules are two patterns, and folding them by type kept the first
    operator/pattern and silently deleted the second. Every existing rule survives as its own
    entry, and a Forge-owned type that repeats gets Forge's floor merged into each one."""
    from agentic_sdlc.onboard import merged_ruleset

    pattern_a = {"operator": "starts_with", "pattern": "feat", "negate": False}
    pattern_b = {"operator": "regex", "pattern": "JIRA-[0-9]+", "negate": False}
    actual = json.loads(json.dumps(ruleset_payload(spec())))
    actual["rules"] = [
        {"type": "commit_message_pattern", "parameters": dict(pattern_a)},
        {"type": "deletion"},
        {"type": "commit_message_pattern", "parameters": dict(pattern_b)},
        {"type": "file_path_restriction", "parameters": {"restricted_file_paths": ["a/**"]}},
        {"type": "file_path_restriction", "parameters": {"restricted_file_paths": ["b/**"]}},
        {"type": "pull_request", "parameters": {"required_approving_review_count": 2}},
        {"type": "pull_request", "parameters": {"require_code_owner_review": True}},
    ]
    merged = merged_ruleset(actual, spec())
    rules = merged["rules"]
    assert [r["parameters"] for r in rules if r["type"] == "commit_message_pattern"] == [
        pattern_a,
        pattern_b,
    ]
    assert [
        r["parameters"]["restricted_file_paths"]
        for r in rules
        if r["type"] == "file_path_restriction"
    ] == [["a/**"], ["b/**"]]
    prs = [r["parameters"] for r in rules if r["type"] == "pull_request"]
    assert len(prs) == 2
    assert prs[0]["required_approving_review_count"] == 2
    assert prs[1]["require_code_owner_review"] is True
    for pr in prs:  # Forge's floor reaches every rule of a type it owns
        assert pr["required_review_thread_resolution"] is True
        assert pr["dismiss_stale_reviews_on_push"] is True
    # existing order kept, Forge's missing types appended once
    assert [r["type"] for r in rules[:7]] == [r["type"] for r in actual["rules"]]
    assert [r["type"] for r in rules[7:]] == ["non_fast_forward", "required_status_checks"]
    assert ruleset_mismatches(merged, spec()) == []
    assert merged_ruleset(merged, spec()) == merged  # idempotent: no growth on re-apply


def test_ruleset_rejects_and_rebinds_a_foreign_integration_on_the_test_check():
    """A `test` context pinned to another app's integration_id can never be satisfied by ci.yml
    (Codex review 4204434963): doctor reports it, and the merge rebinds it to GitHub Actions --
    never to an unbound check any integration could satisfy (Codex review 4239510554)."""
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
        {"context": "test", "integration_id": GITHUB_ACTIONS_INTEGRATION_ID},
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
        # no with.runs_on: the pinned callee's own default ('"ubuntu-latest"') applies
        (
            {
                "pr.yml": _pr_caller(
                    f"owner/agentic-sdlc/.github/workflows/reusable-review.yml@{SHA}"
                )
            },
            False,
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
    write_onboarding(repo, spec(fork_exposed=True))
    for name, text in files.items():
        _wf(repo, name, text)
    found = _pull_request_off_hosted(repo, "owner/agentic-sdlc", _platform_loader())
    assert bool(found) is exposed, found
    assert not any(f.startswith("ci.yml:") for f in found)  # the hosted CI stays clean
    if any("agentic-sdlc/" in text for text in files.values()):
        # without the pinned callee nothing about a platform call is provable: fails closed
        assert _pull_request_off_hosted(repo, "owner/agentic-sdlc")


def _platform_loader(**overrides):
    """A CalleeLoader serving this repository's own reusable workflows (the real contract) at
    any pin, with `overrides` (file name -> parsed doc, None = unreadable) in their place."""

    def load(name, sha):
        if name in overrides:
            return overrides[name]
        path = PLATFORM_WORKFLOWS / name
        return yaml.safe_load(path.read_text()) if path.is_file() else None

    return load


def test_doctor_fails_a_public_repo_whose_pr_workflow_calls_a_self_hosted_reusable(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec(fork_exposed=True))
    _wf(repo, "reuse.yml", REUSE_SELF_HOSTED)
    _wf(repo, "pr.yml", _pr_caller("./.github/workflows/reuse.yml"))
    gh = _healthy_gh()
    gh.answers["api repos/owner/comic"] = json.dumps(
        {"default_branch": "main", "visibility": "public"}
    )
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))
    check = failed[FORK_RUNNER_CHECK]
    assert "pr.yml:call > reuse.yml:build" in check.detail


# Codex 4238739193: runner diagnosis used to trust a list of platform file names whose jobs
# "all run on inputs.runs_on". It now reads the callee AS FETCHED at the pin: a pinned workflow
# that hard-codes another runner is reported wherever the caller's runs_on points.
def test_runner_diagnosis_reads_the_pinned_callee_not_its_file_name(tmp_path):
    from agentic_sdlc.onboard import _job_runner_targets

    repo = _repo(tmp_path)
    write_onboarding(repo, spec(runs_on=("ubuntu-latest",)))
    plan = repo / ".github/workflows/agent-plan.yml"
    honest = _job_runner_targets(repo, plan, "owner/agentic-sdlc", load_callee=_platform_loader())
    assert all(isinstance(t, set) for t in honest.values()), honest
    assert {k for k in honest if k.startswith("agent-plan.yml:plan > reusable-plan.yml:")}

    callee = yaml.safe_load((PLATFORM_WORKFLOWS / "reusable-plan.yml").read_text())
    first = next(iter(callee["jobs"]))
    callee["jobs"][first]["runs-on"] = "macos-15"
    targets = _job_runner_targets(
        repo,
        plan,
        "owner/agentic-sdlc",
        load_callee=_platform_loader(**{"reusable-plan.yml": callee}),
    )
    assert targets[f"agent-plan.yml:plan > reusable-plan.yml:{first}"] == {"macos-15"}

    unreadable = _job_runner_targets(
        repo,
        plan,
        "owner/agentic-sdlc",
        load_callee=_platform_loader(**{"reusable-plan.yml": None}),
    )
    assert "could not be read at that pin" in str(unreadable["agent-plan.yml:plan"])

    # End to end: the pinned callee (served by the platform contents API) hard-codes macOS and a
    # self-hosted runner nobody registered; the caller's ubuntu runs_on does not hide either.
    callee["jobs"][first]["runs-on"] = ["self-hosted", "linux", "gpu"]
    gh = _healthy_gh()
    gh.answers[PLATFORM_CONTENTS + ".github/workflows/reusable-plan.yml"] = _blob(
        yaml.safe_dump(callee).encode()
    )
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))
    detail = failed["self-hosted runner online for this repo"].detail
    assert f"agent-plan.yml:plan > reusable-plan.yml:{first} ['gpu', 'linux', 'self-hosted']" in (
        detail
    )


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
        {"fork_exposed": True, "ci_runs_on": ("windows-latest",)},
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
HOOK_GUARD = ".claude/hooks/forge_commit_guard.py"


def _settings(repo: Path, data) -> None:
    (repo / ".claude/settings.json").write_text(data if isinstance(data, str) else json.dumps(data))


def _forge_entry(event: str, **spec_overrides) -> dict:
    """The handler onboard writes for `event` in a repository onboarded with `spec()`: its
    script's SHA-256 pinned, the vetted interpreter (Codex 4242753911, 4242753904)."""
    from agentic_sdlc.onboard import HOOK_SCRIPTS, forge_hook_entry, hook_script_digest

    script = render_hooks(spec(**spec_overrides))[HOOK_SCRIPTS[event]]
    return forge_hook_entry(event, hook_script_digest(script))


def _hook(script: str, command: str | None = None) -> dict:
    """A handler running `script`; a Forge script's is exactly the generated one (fail-closed
    fields, pinned interpreter and hash), so only what a test changes differs from it."""
    if command is None and script == "forge_commit_guard.py":
        return _forge_entry("PreToolUse")
    if command is None and script == "forge_session_start.py":
        return _forge_entry("SessionStart")
    return {
        "type": "command",
        "command": command or f'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/{script}"',
    }


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
                        "hooks": [
                            _hook(
                                "x",
                                _forge_entry("SessionStart")["command"].replace(
                                    '"${CLAUDE_PROJECT_DIR}/' + HOOK_START + '"',
                                    '"$CLAUDE_PROJECT_DIR"/' + HOOK_START,
                                ),
                            )
                        ],
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
    ["bash -lc 'git status'", "sh -e script.sh", "bash -x run-commit.sh"],
)
def test_commit_guard_clustered_shell_options_without_a_commit(tmp_path, command):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    _harmless_scripts(tmp_path)  # Codex 4240526381: a script file is read, not passed blind
    assert not guard.is_git_commit(command, cwd=str(tmp_path))


def _harmless_scripts(root: Path) -> None:
    """The script files the "allowed" shell commands above run, each with no git in it."""
    for name in ("script.sh", "run-commit.sh", "scripts/test.sh"):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("#!/bin/sh\n# runs the tests, doesn't commit\necho ok\n")


@pytest.mark.parametrize("name", ["agent-auto-plan.yml", "agent-auto-implement.yml"])
def test_doctor_requires_the_automatic_callers_to_trigger_on_labeled_issues(tmp_path, name):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    path = repo / ".github/workflows" / name
    text = path.read_text()
    path.write_text(re.sub(r"  issues:\n    types: \[[^\]]*\]\n", "  workflow_dispatch:\n", text))
    assert path.read_text() != text
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
    for name, job in (
        ("agent-auto-plan.yml", "preflight"),
        ("agent-auto-implement.yml", "authorize"),
    ):
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
    assert f"'{value}''s'" in auto["jobs"]["authorize"]["if"]
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
        # not the labeler authorizations nor their failure reporters: GitHub-hosted
        for job_name, job in doc["jobs"].items():
            hosted = {"authorized", "fresh"} & set(job.get("outputs") or {})
            if "runs-on" in job and not hosted and job_name != "authorization_failed":
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
    assert (
        "agent-auto-implement.yml:implement > reusable-implement.yml:" in detail
        and "macos-15" in detail
    )
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
def test_commit_guard_denies_aliases_and_external_subcommands_by_default(tmp_path, command):
    """Deny by default (Codex 4206173686): an alias or `git-<name>` program is not a literal
    allowlisted subcommand, and `-c` makes even `status` unprovable."""
    guard = _guard_module(tmp_path, "forge_commit_guard")
    assert guard.is_git_commit(command, cwd=str(tmp_path))


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
    assert code("git st") == 2  # aliases are not followed: not a literal read-only subcommand
    assert code("git status") == 0
    assert guard.is_git_commit("git ci -m x", cwd=str(tmp_path))  # unknown subcommand
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

PUBLIC_CHECK = FORK_RUNNER_CHECK
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
            {
                "type": "file",
                "encoding": "base64",
                "content": base64.b64encode(text.encode()).decode(),
            }
        )


def test_public_runner_safety_reads_the_default_branchs_workflows(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec(fork_exposed=True))
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
    write_onboarding(repo, spec(fork_exposed=True))
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
    write_onboarding(repo, spec(fork_exposed=True))
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
        {"type": "file", "encoding": "base64", "content": base64.b64encode(b"pass\n").decode()}
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
        # the substitution is proven read-only; `echo commit` names no git
        'out="$(git rev-parse HEAD)"; echo commit',
    ],
)
def test_commit_guard_fail_closed_still_proves_non_commits(tmp_path, command):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    (tmp_path / "sub").mkdir()  # a directory whose configuration can be read (Codex 4239667542)
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
        json.dumps(
            {"type": "file", "encoding": "base64", "content": base64.b64encode(stale).decode()}
        )
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
        {"type": "file", "encoding": "base64", "content": base64.b64encode(doc).decode()}
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
    if built.implementer == "cloud-routine":
        # Codex 4240306498: an unconfirmed routine is NOT READY under --local too
        from agentic_sdlc.onboard import confirm_cloud_routine

        report = doctor(repo, built.project_id, "owner/agentic-sdlc", FakeGh(), remote=False)
        assert set(_failed(report)) == {"cloud implementer routine is scheduled"}
        confirm_cloud_routine(repo)
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
    assert f"{event} hook running" in detail and next(iter(extra)) in detail


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
    from agentic_sdlc.onboard import HOOK_SCRIPTS

    settings = json.loads(render_onboarding(spec())[".claude/settings.json"])
    for event in HOOK_SCRIPTS:
        assert settings["hooks"][event][0]["hooks"] == [_forge_entry(event)]


def test_rendered_hooks_are_anchored_at_the_project_and_the_guard_fails_closed():
    """Codex 4242373510: the script is named under ${CLAUDE_PROJECT_DIR}, so a `cd` in the
    session cannot break it. Codex 4242373515: the guard blocks when it cannot run --
    `onFailure: "block"`, an explicit timeout above its own deadline, and `|| exit 2`."""
    from agentic_sdlc.onboard import HOOK_LOADER, hook_script_digest

    files = render_onboarding(spec())
    settings = json.loads(files[".claude/settings.json"])
    start = settings["hooks"]["SessionStart"][0]["hooks"][0]
    guard = settings["hooks"]["PreToolUse"][0]["hooks"][0]
    run = f"FORGE_SESSION_PATH=\"$PATH\" PATH=/usr/bin:/bin /usr/bin/python3 -I -c '{HOOK_LOADER}'"
    assert start == {
        "type": "command",
        "command": f'{run} "${{CLAUDE_PROJECT_DIR}}/{HOOK_START}" '
        + hook_script_digest(files[HOOK_START]),
    }
    assert guard == {
        "type": "command",
        "command": f'{run} "${{CLAUDE_PROJECT_DIR}}/{HOOK_GUARD}" '
        + hook_script_digest(files[HOOK_GUARD])
        + " || exit 2",
        "timeout": 300,
        "onFailure": "block",
    }
    guard_source = files[".claude/hooks/forge_commit_guard.py"]
    deadline = int(re.search(r"(?m)^GUARD_DEADLINE = (\d+)$", guard_source).group(1))
    assert deadline < guard["timeout"]


def test_the_guard_fails_closed_on_its_own_crash_or_deadline(tmp_path, monkeypatch, capsys):
    """Codex 4242373515: an exception escaping the guard would exit 1, a NON-blocking error in
    Claude Code; `run` turns any failure, the deadline included, into exit 2."""
    guard = _guard_module(tmp_path, "forge_commit_guard")

    def crash():
        raise RuntimeError("boom")

    monkeypatch.setattr(guard, "main", crash)
    assert guard.run() == 2
    assert "RuntimeError: boom" in capsys.readouterr().err

    def stall():
        raise guard.GuardDeadline("no verdict within 240s")

    monkeypatch.setattr(guard, "main", stall)
    assert guard.run() == 2
    monkeypatch.setattr(guard, "main", lambda: 0)
    assert guard.run() == 0


@pytest.mark.parametrize(
    "change, why",
    [
        ({"command": "python3 .claude/hooks/forge_commit_guard.py || exit 2"}, "relative"),
        (
            {"command": 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/forge_commit_guard.py"'},
            "exit 2",
        ),
        ({"onFailure": "continue"}, "onFailure"),
        ({"onFailure": None}, "onFailure"),
        ({"timeout": 600}, "timeout"),
        ({"timeout": None}, "timeout"),
    ],
)
def test_doctor_rejects_a_guard_hook_that_fails_open(tmp_path, change, why):
    from agentic_sdlc.onboard import hook_problems

    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    path = repo / ".claude/settings.json"
    entry = {**_forge_entry("PreToolUse"), **change}
    entry = {k: v for k, v in entry.items() if v is not None}
    settings = json.loads(path.read_text())
    settings["hooks"]["PreToolUse"] = [{"matcher": "Bash", "hooks": [entry]}]
    path.write_text(json.dumps(settings))
    problems = hook_problems(path)
    assert problems and why in " ".join(problems)


def test_forced_onboard_replaces_a_relative_hook_from_an_older_onboard(tmp_path):
    """A settings file written before Codex 4242373510 (relative paths, no fail-closed guard)
    is flagged by doctor and fixed by `onboard --force`, leaving one copy of each hook."""
    from agentic_sdlc.onboard import hook_problems

    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    path = repo / ".claude/settings.json"
    old = {
        "hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": "python3 " + HOOK_START}]}],
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "python3 " + HOOK_GUARD}],
                }
            ],
        }
    }
    path.write_text(json.dumps(old))
    problems = " ".join(hook_problems(path))
    assert "a path relative to the working directory" in problems
    assert "no PreToolUse (matcher Bash) hook" in problems
    write_onboarding(repo, spec(), force=True)
    assert hook_problems(path) == []
    merged = json.loads(path.read_text())["hooks"]
    assert [len(merged[e]) for e in ("SessionStart", "PreToolUse")] == [1, 1]


def test_doctor_fails_a_cloud_routine_plan_caller_on_macos(tmp_path):
    """Codex 4205367730: reusable-plan.yml runs GNU sha256sum; a cloud routine still plans."""
    repo = _repo(tmp_path)
    write_onboarding(repo, spec(implementer="cloud-routine", runs_on=("ubuntu-latest",)))
    _edit_workflow(
        repo, "agent-plan.yml", lambda d: d["jobs"]["plan"]["with"].update(runs_on='["macos-15"]')
    )
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", _healthy_gh()))
    assert "agent-plan.yml:plan > reusable-plan.yml:" in failed[LINUX_CHECK].detail
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
        "# a note\nls",
        "if [ -f x ]; then echo 'it''s'; fi",
        "sh -e script.sh",
    ],
)
def test_commit_guard_raw_proof_keeps_plain_non_commits(tmp_path, command):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    _harmless_scripts(tmp_path)
    assert not guard.is_git_commit(command, cwd=str(tmp_path))


def test_commit_guard_checks_an_assembled_commit_after_cd(tmp_path):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    targets = guard.commit_targets('cd ../wt && g"it" commit', cwd=str(tmp_path))
    # the cooked parse still checks ../wt; the assembled program itself is unresolved (Codex
    # 4240170620), never the hook's own checkout
    assert (("../wt",), (), ()) in targets and guard._unresolved() in targets
    assert ((), (), ()) not in targets
    assert guard.UNRESOLVED_REPO in guard.commit_branches('cd ../wt && g"it" commit', None)


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
def test_default_branch_must_be_a_valid_git_branch_name(branch):
    from agentic_sdlc import onboard

    assert onboard.policy_field_problem("default branch", branch)
    with pytest.raises(OnboardError):
        spec(default_branch=branch)


@pytest.mark.parametrize("branch", ["main", "feature/x", "release-1.2", "a.b/c_d", "v1.0-rc"])
def test_default_branch_accepts_valid_git_branch_names(branch):
    from agentic_sdlc.onboard import policy_field_problem

    assert policy_field_problem("default branch", branch) == ""


def _git_check_ref_format_branch_rejects(names):
    """What the real `git check-ref-format --branch` says about each name (True = refuses), run
    outside any repository so `@{-N}` cannot expand. Skips when no git is installed."""
    import shutil
    import subprocess

    git = shutil.which("git")
    if git is None:
        pytest.skip("git is not installed")
    env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1"}
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CEILING_DIRECTORIES"] = "/"
    return {
        name: subprocess.run(
            [git, "check-ref-format", "--branch", name],
            capture_output=True,
            env=env,
            cwd="/",
            check=False,
        ).returncode
        != 0
        for name in names
    }


def _branch_corpus():
    import random

    pieces = list("ab./-@{}~^:?*[\\ +!&;'\"$`\u00e9\x7f\x01\t") + [
        ".lock",
        "..",
        "@{",
        "//",
        "HEAD",
        "refs/heads/",
        "-",
        "@",
        "/.",
        ".lock/",
    ]
    rng = random.Random(4243659475)
    corpus = {"".join(rng.choice(pieces) for _ in range(rng.randint(1, 6))) for _ in range(1500)}
    corpus.update(GIT_VALID_BRANCHES)
    corpus.update(["main", "HEAD", "@", "@{-1}", "a.lock", "a/.b", "a/b.", "-x", "a..b", "/a"])
    return sorted(corpus)


def test_branch_rules_agree_with_the_real_git_check_ref_format():
    """Codex 4243659475: onboard no longer runs `git check-ref-format` (a bare `git` with the
    caller's whole environment), so the in-process rules must BE git's: every name in a generated
    corpus gets the same verdict from `policy.git_branch_format_problem` as from the real git."""
    from agentic_sdlc.policy import git_branch_format_problem

    corpus = _branch_corpus()
    git = _git_check_ref_format_branch_rejects(corpus)
    disagree = [n for n in corpus if bool(git_branch_format_problem(n)) != git[n]]
    assert disagree == []
    # and the corpus exercises both verdicts
    assert any(git.values()) and not all(git.values())


def test_branch_validation_runs_no_program(monkeypatch):
    """Codex 4243659475: validating a default branch starts no subprocess at all."""
    import subprocess

    from agentic_sdlc import onboard

    def refuse(*args, **kwargs):
        raise AssertionError(f"subprocess started: {args!r}")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setenv("GH_TOKEN", "secret-token")
    assert onboard.policy_field_problem("default branch", "release+next") == ""
    assert onboard.policy_field_problem("default branch", "a..b")
    assert not hasattr(onboard, "_git_rejects_branch")


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


# ---------------------------------------------------------------- Codex 4205853966


def test_session_start_reports_an_unreadable_lease_inventory_not_none(tmp_path, monkeypatch):
    import io

    start = _guard_module(tmp_path, "forge_session_start")
    for listing in (None, "not json", '{"message": "Bad credentials"}'):
        monkeypatch.setattr(start, "run", lambda cmd, out=listing: out)  # every call fails
        monkeypatch.setattr(start, "_lease_lookup", lambda: lambda n: None)
        monkeypatch.setattr(start.sys, "stdin", io.StringIO("{}"))
        out = io.StringIO()
        monkeypatch.setattr(start.sys, "stdout", out)
        assert start.main() == 0
        text = out.getvalue()
        assert "lease inventory unavailable -- do not start issue work until it can be checked" in (
            text
        )
        others = text.split("other agents (do NOT work on these):\n")[1].split("[Forge]")[0]
        assert "UNAVAILABLE" in others and "  none" not in others
        if listing is None:
            assert "Open PRs:\n  (unavailable" in text


def test_session_start_lists_an_unreadable_lease_as_held(tmp_path, monkeypatch):
    import io

    start = _guard_module(tmp_path, "forge_session_start")
    issues = [{"number": 4, "title": "t", "assignees": []}]
    live, stale, mine = start.classify_leased(issues, lambda n: {"unavailable": True})
    assert [x.split()[0] for x in live] == ["#4"] and "treat as held" in live[0]
    assert stale == [] and mine == []
    monkeypatch.setattr(start, "run", lambda cmd: json.dumps(issues) if _is_issue_page(cmd) else "")
    monkeypatch.setattr(start, "_lease_lookup", lambda: lambda n: {"unavailable": True})
    monkeypatch.setattr(start.sys, "stdin", io.StringIO("{}"))
    out = io.StringIO()
    monkeypatch.setattr(start.sys, "stdout", out)
    start.main()
    assert "lease inventory unavailable" in out.getvalue()


def test_commit_guard_treats_an_unreadable_lease_as_unavailable_not_free(tmp_path, monkeypatch):
    import subprocess

    guard = _guard_module(tmp_path, "forge_commit_guard")

    def down(args, **kwargs):
        raise subprocess.CalledProcessError(1, args, stderr="gh: Bad Gateway (HTTP 502)")

    monkeypatch.setattr(guard.subprocess, "run", down)
    lease = guard.lease_for(7)
    assert lease is not None and guard.lease_unavailable(lease)
    commit = {"tool_name": "Bash", "session_id": "s1", "tool_input": {"command": "git commit"}}
    code, message = guard.decide(commit, "forge/issue-7", guard.lease_for, lambda b: False)
    assert code == 2 and "Lease inventory unavailable" in message
    # The open-PR lookup failing is not "no PR" either (and blocks, naming why).
    code, message = guard.decide(commit, "forge/issue-7", lambda n: None, guard.open_pr_for)
    assert code == 2 and "could not be read" in message


def test_commit_guard_permission_failure_is_unavailable_and_404_is_untrusted(tmp_path, monkeypatch):
    import subprocess
    from datetime import UTC, datetime, timedelta

    guard = _guard_module(tmp_path, "forge_commit_guard")
    expires = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    claim = f"<!-- forge-claim agent=a session=s1 branch=b expires={expires} -->"
    pages = [[{"body": claim, "user": {"login": "owner", "type": "User"}}]]
    permission = {"stderr": "gh: Bad Gateway (HTTP 502)", "code": 1}

    def fake_run(args, **kwargs):
        if args[2].endswith("/permission"):
            return subprocess.CompletedProcess(
                args, permission["code"], stdout="", stderr=permission["stderr"]
            )
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(pages), stderr="")

    monkeypatch.setattr(guard.subprocess, "run", fake_run)
    monkeypatch.setattr(guard, "_PERMISSIONS", {})
    assert guard.lease_unavailable(guard.lease_for(7))
    permission.update(stderr="gh: Not Found (HTTP 404)")
    monkeypatch.setattr(guard, "_PERMISSIONS", {})
    assert guard.lease_for(7) is None  # no permission at all: the marker is not trusted


# ---------------------------------------------------------------- Codex 4205853961


def test_doctor_fails_a_private_platform_for_a_public_consumer(tmp_path):
    from agentic_sdlc.onboard import platform_access_check

    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _private_platform_gh("organization")
    gh.answers["api repos/owner/comic"] = json.dumps(
        {"default_branch": "main", "visibility": "public"}
    )
    check = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))[PLATFORM_ACCESS]
    assert not check.manual and "public" in check.detail and "PUBLIC reusable" in check.detail
    access = {"api repos/plat/forge/actions/permissions/access": '{"access_level": "user"}'}
    unknown = platform_access_check(FakeGh(dict(access)), "plat/comic", "plat/forge")
    assert not unknown.ok and unknown.manual and "visibility" in unknown.detail
    private = FakeGh({**access, "api repos/plat/comic": '{"private": true}'})
    assert platform_access_check(private, "plat/comic", "plat/forge").ok


# ---------------------------------------------------------------- Codex 4205853956

ACTIONS_POLICY = "repository Actions policy allows the generated workflows"


def _actions_policy_gh(perms: object, selected: object = None) -> FakeGh:
    gh = _healthy_gh()
    del gh.answers["api repos/owner/comic/actions/permissions"]
    head = {  # unanswered (""): unreadable, not the permissions prefix's answer
        "api repos/owner/comic/actions/permissions/selected-actions": json.dumps(selected)
        if selected is not None
        else ""
    }
    if perms is not None:
        head["api repos/owner/comic/actions/permissions"] = json.dumps(perms)
    gh.answers = {**head, **gh.answers}
    return gh


def _actions_check(tmp_path, perms, selected=None):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", _actions_policy_gh(perms, selected))
    return next(c for c in report.checks if c.name == ACTIONS_POLICY), report


SELECTED = {"enabled": True, "allowed_actions": "selected"}
ALLOWLIST = {
    "github_owned_allowed": True,
    "verified_allowed": False,
    "patterns_allowed": ["anthropics/claude-code-action@*", "openai/*"],
}


def test_doctor_accepts_all_actions_and_a_complete_selected_allowlist(tmp_path):
    check, report = _actions_check(tmp_path / "a", {"enabled": True, "allowed_actions": "all"})
    assert check.ok and report.ok
    check, report = _actions_check(tmp_path / "s", SELECTED, ALLOWLIST)
    assert check.ok and report.ok, report.render()


@pytest.mark.parametrize(
    ("perms", "selected", "manual", "needle"),
    [
        ({"enabled": False}, None, False, "disabled"),
        ({"enabled": True, "allowed_actions": "local_only"}, None, False, "local_only"),
        (None, None, True, "could not read the Actions permissions"),
        (SELECTED, None, True, "allowlist of owner/comic could not be read"),
        (
            SELECTED,
            {**ALLOWLIST, "patterns_allowed": ["openai/*"]},
            False,
            "anthropics/claude-code-action@",
        ),
        (SELECTED, {**ALLOWLIST, "github_owned_allowed": False}, False, "actions/checkout@"),
        (
            SELECTED,
            {**ALLOWLIST, "patterns_allowed": [], "verified_allowed": True},
            True,
            "Marketplace-verified",
        ),
    ],
)
def test_doctor_fails_an_actions_policy_the_generated_workflows_cannot_run_under(
    tmp_path, perms, selected, manual, needle
):
    check, report = _actions_check(tmp_path, perms, selected)
    assert not check.ok and not report.ok and check.manual == manual
    assert needle in check.detail


def test_selected_actions_patterns_match_like_github():
    from agentic_sdlc.onboard import _action_pattern_matches, actions_permissions_check

    sha = "a" * 40
    action = f"Anthropics/claude-code-action@{sha}"
    assert _action_pattern_matches("anthropics/*", action)
    assert _action_pattern_matches("anthropics/claude-code-action@*", action)
    assert _action_pattern_matches("anthropics/claude-code-action", action)
    assert _action_pattern_matches(f"anthropics/claude-code-action@{sha}", action)
    assert not _action_pattern_matches("anthropics/claude-code-action@v1", action)
    assert not _action_pattern_matches("anthropics/claude-*-x@*", action)
    assert not _action_pattern_matches("anthropic/*", action)
    reusable = f"plat/forge/.github/workflows/reusable-plan.yml@{sha}"
    assert _action_pattern_matches("plat/forge/.github/workflows/*", reusable)
    gh = FakeGh(
        {
            "api repos/owner/comic/actions/permissions/selected-actions": json.dumps(
                {"github_owned_allowed": True, "patterns_allowed": ["plat/other@*"]}
            ),
            "api repos/owner/comic/actions/permissions": json.dumps(SELECTED),
        }
    )
    check = actions_permissions_check(gh, "owner/comic", {reusable, f"actions/checkout@{sha}"})
    assert not check.ok and reusable in check.detail and "actions/checkout" not in check.detail
    own = actions_permissions_check(gh, "plat/comic", {reusable})  # the owner's own workflows
    assert not own.ok  # unreadable for plat/comic in this fake: never READY


def test_selected_actions_negative_patterns_block_a_subset_of_an_allowance(tmp_path):
    from agentic_sdlc.onboard import _action_allowed

    sha = "a" * 40
    claude = f"anthropics/claude-code-action@{sha}"
    other = f"anthropics/other-action@{sha}"
    # Codex 4206492757: GitHub blocks the Claude action here, whatever the entry order.
    for patterns in (
        ["anthropics/*", "!anthropics/claude-code-action@*"],
        ["!anthropics/claude-code-action@*", "anthropics/*"],
    ):
        selected = {"patterns_allowed": patterns, "verified_allowed": True}
        assert _action_allowed(claude, selected, "owner") is False
        assert _action_allowed(other, selected, "owner") is True
    # an exclusion overrides the GitHub-owned allowance too
    blocked = {"github_owned_allowed": True, "patterns_allowed": ["!actions/checkout@*"]}
    assert _action_allowed(f"actions/checkout@{sha}", blocked, "owner") is False
    assert _action_allowed(f"actions/setup-python@{sha}", blocked, "owner") is True
    # end to end: doctor reports the blocked action and is NOT READY
    check, report = _actions_check(
        tmp_path,
        SELECTED,
        {**ALLOWLIST, "patterns_allowed": ["anthropics/*", "openai/*", "!anthropics/claude-*@*"]},
    )
    assert not check.ok and not report.ok and not check.manual
    assert "anthropics/claude-code-action@" in check.detail


# ---------------------------------------------------------------- Codex 4206492761


@pytest.mark.parametrize("force", [False, True])
def test_write_onboarding_refuses_a_dangling_managed_symlink(tmp_path, force):
    repo = _repo(tmp_path / "repo")
    outside = tmp_path / "outside.txt"  # does not exist yet: the link dangles
    (repo / "agentic-sdlc.toml").symlink_to(outside)
    with pytest.raises(OnboardError, match=r"agentic-sdlc.toml is a symlink"):
        write_onboarding(repo, spec(), force=force)
    assert not outside.exists()
    assert not (repo / ".github").exists()  # validated before anything is written


def test_write_onboarding_refuses_a_symlink_to_a_file_outside_the_repo(tmp_path):
    repo = _repo(tmp_path / "repo")
    outside = tmp_path / "victim.yml"
    outside.write_text("keep me\n")
    (repo / ".github/workflows").mkdir(parents=True)
    (repo / ".github/workflows/ci.yml").symlink_to(outside)
    with pytest.raises(OnboardError, match=r"ci.yml is a symlink"):
        write_onboarding(repo, spec(), force=True)
    assert outside.read_text() == "keep me\n"


def test_write_onboarding_refuses_a_symlinked_parent_directory(tmp_path):
    repo = _repo(tmp_path / "repo")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (repo / ".github").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(OnboardError, match=r"\.github is a symlink"):
        write_onboarding(repo, spec(), force=True)
    assert list(elsewhere.iterdir()) == []


def test_write_onboarding_replaces_regular_files_atomically_keeping_their_mode(tmp_path):
    import os
    import stat

    repo = _repo(tmp_path / "repo")
    write_onboarding(repo, spec())
    guard = repo / ".claude/hooks/forge_commit_guard.py"
    guard.write_text("stale\n")
    guard.chmod(0o755)
    inode = guard.stat().st_ino
    write_onboarding(repo, spec(), force=True)
    assert guard.read_text() != "stale\n"
    assert stat.S_IMODE(guard.stat().st_mode) == 0o755
    assert guard.stat().st_ino != inode  # a new file swapped in, not written in place
    assert not [p for p in repo.rglob("*.tmp")]
    assert os.path.isfile(guard) and not guard.is_symlink()


# ---------------------------------------------------------------- Codex 4205853975


def test_doctor_requires_required_managed_paths_to_be_regular_files(tmp_path):
    import shutil

    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    guard = repo / ".claude/hooks/forge_commit_guard.py"
    guard.unlink()
    guard.mkdir()
    (guard / "x.py").write_text("pass\n")
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", _healthy_gh()))
    assert "forge_commit_guard.py (not a regular file)" in failed["required files present"].detail
    assert "forge_commit_guard.py (a directory, not a file)" in failed[PUSHED_CHECK].detail
    # A symlink leaving the repository is not the managed file either.
    shutil.rmtree(guard)
    outside = tmp_path / "elsewhere.py"
    outside.write_text(render_onboarding(spec())[".claude/hooks/forge_commit_guard.py"])
    guard.symlink_to(outside)
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", _healthy_gh()))
    assert "forge_commit_guard.py is a symlink" in failed["required files present"].detail


def test_doctor_requires_required_managed_paths_to_be_files_on_the_default_branch(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _healthy_gh()
    gh.answers[CONSUMER_CONTENTS + ".claude/hooks/forge_session_start.py"] = json.dumps(
        [{"name": "x.py", "path": ".claude/hooks/forge_session_start.py/x.py", "type": "file"}]
    )
    gh.answers[CONSUMER_CONTENTS + ".claude/hooks/forge_commit_guard.py"] = json.dumps(
        {"type": "symlink", "target": "../../outside.py", "encoding": "base64", "content": ""}
    )
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))
    assert "required files present" not in failed
    detail = failed[PUSHED_CHECK].detail
    assert "forge_session_start.py (a directory, not a file)" in detail
    assert "forge_commit_guard.py (not a regular file: type 'symlink')" in detail


# ---------------------------------------------------------------- Codex 4205853985

CONTRACT = "pinned reusable workflows accept the generated calls"


def _older_plan_workflow() -> str:
    """reusable-plan.yml as an older platform revision had it: no runs_on input, and the
    OpenAI key required instead of the Claude OAuth token."""
    doc = yaml.safe_load((PLATFORM_WORKFLOWS / "reusable-plan.yml").read_text())
    call = doc.get("on", doc.get(True))["workflow_call"]
    del call["inputs"]["runs_on"]
    call["secrets"] = {"OPENAI_API_KEY": {"required": True}}
    return yaml.safe_dump(doc)


def test_doctor_validates_the_pinned_reusable_workflow_contract(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _healthy_gh()
    gh.answers[PLATFORM_CONTENTS + ".github/workflows/reusable-plan.yml"] = _blob(
        _older_plan_workflow().encode()
    )
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    failed = _failed(report)
    assert "platform ref reachable (reusable workflows exist at pin)" not in failed
    detail = failed[CONTRACT].detail
    assert "agent-plan.yml:plan: passes input 'runs_on', which reusable-plan.yml does not" in (
        detail
    )
    assert "does not pass secret 'OPENAI_API_KEY', which reusable-plan.yml requires" in detail
    assert "passes secret 'CLAUDE_CODE_OAUTH_TOKEN'" in detail
    assert "reusable-implement.yml" not in detail  # that callee still matches


def test_reusable_call_contract_rules():
    from agentic_sdlc.onboard import reusable_call_mismatches

    callee = {
        "on": {
            "workflow_call": {
                "inputs": {
                    "a": {"type": "string", "required": True},
                    "b": {"type": "string", "required": False},
                },
                "secrets": {"S": {"required": True}, "T": {}},
            }
        }
    }
    ok = {"with": {"a": "1"}, "secrets": {"S": "${{ secrets.S }}"}}
    assert reusable_call_mismatches("c:j", ok, "x.yml", callee) == []
    inherit = {"with": {"a": "${{ inputs.a }}"}, "secrets": "inherit"}
    assert reusable_call_mismatches("c:j", inherit, "x.yml", callee) == []
    gaps = reusable_call_mismatches("c:j", {"with": {"b": "2", "z": 3}}, "x.yml", callee)
    assert gaps == [
        "c:j: passes input 'z', which x.yml does not declare",
        "c:j: does not pass input 'a', which x.yml requires",
        "c:j: does not pass secret 'S', which x.yml requires",
    ]
    assert "no on.workflow_call" in reusable_call_mismatches("c:j", {}, "x.yml", {"on": "push"})[0]


# Codex 4207036775: names and requiredness matching is not enough. GitHub rejects a call whose
# value does not fit the callee input's declared `type` (and a callee input with no type at all)
# before any job starts, so doctor checks each passed value against the declaration and treats
# an expression it cannot type as unverified for a number/boolean input.
def test_reusable_call_contract_checks_input_types():
    from agentic_sdlc.onboard import reusable_call_mismatches

    def callee(**types):
        inputs = {k: ({"type": t} if t else {}) for k, t in types.items()}
        return {"on": {"workflow_call": {"inputs": inputs}}}

    def gaps(types, passed):
        return reusable_call_mismatches("c:j", {"with": passed}, "x.yml", callee(**types))

    # Compatible: literals of the declared type, fromJSON for any type, numeric contexts,
    # and any expression (or interpolated text) into a string.
    assert gaps({"n": "number", "b": "boolean", "s": "string"}, {"n": 3, "b": True, "s": "x"}) == []
    assert gaps({"n": "number", "b": "boolean"}, {"n": 1.5, "b": False}) == []
    assert gaps({"n": "number"}, {"n": "${{ fromJSON(inputs.n) }}"}) == []
    assert gaps({"b": "boolean"}, {"b": "${{ fromJSON(inputs.b) }}"}) == []
    assert gaps({"n": "number"}, {"n": "${{ github.event.issue.number }}"}) == []
    assert gaps({"s": "string"}, {"s": "${{ needs.a.outputs.n }}"}) == []
    assert gaps({"s": "string"}, {"s": "pr-${{ github.event.issue.number }}"}) == []

    # A string where the pin now declares a number or a boolean: rejected by GitHub.
    for declared in ("number", "boolean"):
        found = gaps({"i": declared}, {"i": "42"})
        assert found == [
            f"c:j: passes input 'i' as '42' (string), but x.yml declares it {declared}"
        ]
        found = gaps({"i": declared}, {"i": "pr-${{ inputs.i }}"})
        assert found and f"declares it {declared}" in found[0]
    # ...and the reverse.
    assert "(number), but x.yml declares it string" in gaps({"s": "string"}, {"s": 7})[0]
    assert "(boolean), but x.yml declares it number" in gaps({"n": "number"}, {"n": True})[0]
    assert (
        "(number), but x.yml declares it string"
        in (gaps({"s": "string"}, {"s": "${{ github.event.issue.number }}"})[0])
    )
    assert "but x.yml declares it number" in gaps({"n": "number"}, {"n": None})[0]

    # An expression doctor cannot type, into a number/boolean: unverified, never compatible.
    for declared in ("number", "boolean"):
        found = gaps({"i": declared}, {"i": "${{ inputs.i }}"})
        assert found == [
            "c:j: passes input 'i' as '${{ inputs.i }}', whose type doctor cannot determine, "
            f"but x.yml declares it {declared}"
        ]
    assert gaps({"n": "number"}, {"n": "${{ fromJSON(a) || fromJSON(b) }}"})

    # A callee input with no type, or one workflow_call does not support: malformed.
    assert gaps({"i": None}, {}) == [
        "c:j: x.yml declares input 'i' with no type (workflow_call inputs need string, number "
        "or boolean)"
    ]
    assert gaps({"i": "choice"}, {"i": "a"}) == [
        "c:j: x.yml declares input 'i' with type 'choice' (workflow_call inputs need string, "
        "number or boolean)"
    ]


# Codex 4207036789: a job named `${{ matrix.name }}` has an empty literal prefix, and every
# context starts with ''. Doctor now evaluates matrix names over a finite literal matrix and
# otherwise counts an expression-named job as reporting nothing (fail closed).
def test_expression_job_names_do_not_satisfy_every_required_check(tmp_path):
    from agentic_sdlc.onboard import effective_rule_problems, workflow_check_contexts

    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "on: pull_request\n"
        "jobs:\n"
        "  lint:\n"
        "    name: ${{ matrix.name }}\n"
        "    strategy:\n"
        "      matrix:\n"
        "        name: [ruff, mypy]\n"
        "        exclude:\n"
        "          - name: mypy\n"
        "        include:\n"
        "          - name: pyright\n"
        "    runs-on: ubuntu-latest\n"
        "    steps: [{run: 'true'}]\n"
        "  test:\n"
        "    name: test-${{ matrix.py }}-${{ matrix.fast }}\n"
        "    strategy: {matrix: {py: ['3.11', 3.12], fast: [true]}}\n"
        "    runs-on: ubuntu-latest\n"
        "    steps: [{run: 'true'}]\n"
        "  dyn:\n"
        "    name: ${{ matrix.x }}\n"
        "    strategy: {matrix: {x: '${{ fromJSON(inputs.xs) }}'}}\n"
        "    runs-on: ubuntu-latest\n"
        "    steps: [{run: 'true'}]\n"
        "  other:\n"
        "    name: ${{ inputs.label }}\n"
        "    runs-on: ubuntu-latest\n"
        "    steps: [{run: 'true'}]\n"
    )
    produced = workflow_check_contexts(tmp_path)
    exact = produced.pull_request
    assert {"ruff", "pyright", "test-3.11-true", "test-3.12-true"} <= exact
    assert "mypy" not in exact
    assert not [c for c in exact if not c.strip()]

    def required(*contexts):
        checks = [{"context": c, "integration_id": 15368} for c in contexts]
        return [
            {"type": "required_status_checks", "parameters": {"required_status_checks": checks}}
        ]

    assert effective_rule_problems(required("ruff", "pyright", "test-3.12-true"), produced) == []
    problems = effective_rule_problems(required("mypy", "deploy", "anything"), produced)
    assert len(problems) == 3 and all("no installed workflow job reports" in p for p in problems)
    # Nothing proves a context but the exact name.
    assert effective_rule_problems(
        required("anything"), ProducedContexts(frozenset({"", " ", "any"}))
    )


# Codex 4206173667 / 4206173686: the guard is deny-by-default for git. On an issue branch every
# git invocation needs the lease except literal read-only subcommands; anything that runs git or
# a shell indirectly (xargs, find -exec, parallel, `| sh`) needs it too.


@pytest.mark.parametrize(
    "command",
    [
        "printf '%s\\n' commit -m x | xargs git",
        "xargs -a args.txt git",
        "xargs -n 1 -a f git",
        "find . -exec git commit -m x {} +",
        "find . -name '*.sh' -exec sh -c 'x' {} +",
        "ls | xargs bash",
        "parallel git ::: commit",
        "printf 'git commit -m x' | sh",
        "cat cmds.txt | bash",
        "bash -s",
        "sh < cmds.txt",
        'bash <<< "echo hi"',
        "git cherry-pick abc123",
        "git revert abc123",
        "git merge feature",
        "git rebase main",
        "git pull",
        "git am < patch",
        "git reset --hard HEAD~1",
        "git checkout other",
        "git switch other",
        "git stash",
        "git stash pop",
        "git notes add -m x",
        "git tag v1",
        "git tag -a v1 -m x",
        "git update-ref refs/heads/forge/issue-7 HEAD~1",
        "git worktree add ../x",
        "git push",
        "git-merge feature",
        "git branch newbranch",
        "git branch -D old",
        "git branch -f forge/issue-7 HEAD~1",
        "git config user.name x",
        "git stash drop",
        "git reflog expire --all",
        "git fetch -u origin main:main",
        "git fetch origin main:forge/issue-7",
        "git fetch --upload-pack='sh -c x' origin",
        "git fetch -{u,}",
        "git grep -O'sh -c x' foo",
        "git -c core.pager=x log",
        "GIT_EXTERNAL_DIFF=x git diff",
        "export GIT_EXTERNAL_DIFF=x; git diff",
        "git log --output=.git/refs/heads/forge/issue-7",
        "git diff $REV",
        "python3 -c \"import subprocess; subprocess.run(['git', 'merge', 'x'])\"",
        "some-new-subcommand-wrapper git status",
    ],
)
def test_commit_guard_denies_git_by_default(tmp_path, command):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    assert guard.is_git_commit(command, cwd=str(tmp_path))
    payload = {"tool_name": "Bash", "session_id": "s1", "tool_input": {"command": command}}
    assert guard.decide(payload, "forge/issue-7", lambda n: None, lambda b: False)[0] == 2
    held = {"session": "s1", "agent": "a"}
    assert guard.decide(payload, "forge/issue-7", lambda n: held, lambda b: False)[0] == 0
    assert guard.decide(payload, "main", lambda n: None, lambda b: False)[0] == 0


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git log --grep commit",
        "git log --grep merge --oneline",
        "git diff",
        "git diff HEAD~1 -- src",
        "git show HEAD:README.md",
        "git branch --show-current",
        "git branch",
        "git branch -a -vv",
        "git branch --contains abc123",
        "git remote -v",
        "git remote show origin",
        "git config --get user.name",
        "git config --list",
        "git fetch origin",
        "git stash list",
        "git stash show -p",
        "git tag -l 'v*'",
        "git reflog",
        "git reflog show",
        "git help commit",
        "git --version",
        "git grep -n foo",
        "git cat-file -p HEAD",
        "git for-each-ref refs/heads",
        "git merge-base main HEAD",
        "git rev-parse --show-toplevel",
        "LC_ALL=C git log",
        "git status | head",
        "git log --oneline | xargs echo",
        "ls | xargs grep foo",
        "ls .git && cat .git/HEAD",
        "bash scripts/test.sh",
        "pytest -q",
    ],
)
def test_commit_guard_allows_literal_read_only_git(tmp_path, command):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    _harmless_scripts(tmp_path)
    assert not guard.is_git_commit(command, cwd=str(tmp_path))
    payload = {
        "tool_name": "Bash",
        "session_id": "s1",
        "cwd": str(tmp_path),
        "tool_input": {"command": command},
    }
    assert guard.decide(payload, "forge/issue-7", lambda n: None, lambda b: False)[0] == 0


# Codex 4206173701: doctor diagnoses the EFFECTIVE rules of the default branch, inherited
# organization/enterprise rulesets included; `--apply` still manages only the repository's own.


def _org_rule(kind: str, **parameters) -> dict:
    return {
        "type": kind,
        "parameters": parameters,
        "ruleset_source_type": "Organization",
        "ruleset_source": "owner",
        "ruleset_id": 99,
    }


def _effective_check(gh, tmp_path):
    from agentic_sdlc.onboard import EFFECTIVE_RULES_CHECK

    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    return report, next(c for c in report.checks if c.name == EFFECTIVE_RULES_CHECK)


def test_doctor_flags_an_inherited_required_check_no_workflow_reports(tmp_path):
    gh = _healthy_gh()
    gh.answers["api repos/owner/comic/rules/branches/main"] = _effective_rules(
        _org_rule("required_status_checks", required_status_checks=[{"context": "sec/scan"}])
    )
    report, check = _effective_check(gh, tmp_path)
    assert not report.ok and "NOT READY" in report.render()
    assert not check.ok and check.manual
    assert "Organization owner" in check.detail and "'sec/scan'" in check.detail


def test_doctor_accepts_an_inherited_required_check_an_installed_job_reports(tmp_path):
    gh = _healthy_gh()
    gh.answers["api repos/owner/comic/rules/branches/main"] = _effective_rules(
        _org_rule(
            "required_status_checks",
            required_status_checks=[
                {"context": "test", "integration_id": GITHUB_ACTIONS_INTEGRATION_ID}
            ],
        ),
        _org_rule("pull_request", required_approving_review_count=2),
    )
    report, check = _effective_check(gh, tmp_path)
    assert report.ok, report.render()
    assert check.ok and "inherited from Organization owner" in check.detail


def test_doctor_rejects_an_inherited_test_check_not_bound_to_github_actions(tmp_path):
    """Forge's `test` gate, required with no integration, accepts a `test` status from ANY app
    or credential with commit-status access (Codex review 4239510554): never READY."""
    gh = _healthy_gh()
    gh.answers["api repos/owner/comic/rules/branches/main"] = _effective_rules(
        _org_rule("required_status_checks", required_status_checks=[{"context": "test"}]),
    )
    report, check = _effective_check(gh, tmp_path)
    assert not report.ok and not check.ok and check.manual
    assert "'test' is not bound to GitHub Actions" in check.detail
    assert "Organization owner" in check.detail


def test_doctor_rejects_an_inherited_check_bound_to_a_foreign_integration(tmp_path):
    from agentic_sdlc.onboard import GITHUB_ACTIONS_INTEGRATION_ID

    gh = _healthy_gh()
    gh.answers["api repos/owner/comic/rules/branches/main"] = _effective_rules(
        _org_rule(
            "required_status_checks",
            required_status_checks=[{"context": "test", "integration_id": 424242}],
        )
    )
    report, check = _effective_check(gh, tmp_path)
    assert not report.ok and not check.ok and check.manual
    assert "'test'" in check.detail and "424242" in check.detail
    assert "Organization owner" in check.detail
    # bound to GitHub Actions itself, the same inherited check is fine
    gh.answers["api repos/owner/comic/rules/branches/main"] = _effective_rules(
        _org_rule(
            "required_status_checks",
            required_status_checks=[
                {"context": "test", "integration_id": GITHUB_ACTIONS_INTEGRATION_ID}
            ],
        )
    )
    (tmp_path / "actions").mkdir()
    report, check = _effective_check(gh, tmp_path / "actions")
    assert report.ok and check.ok, report.render()


@pytest.mark.parametrize(
    ("rule", "words"),
    [
        (_org_rule("required_deployments", required_deployment_environments=["prod"]), "prod"),
        (_org_rule("workflows", workflows=[{"path": ".github/workflows/sec.yml"}]), "sec.yml"),
        (_org_rule("merge_queue", merge_method="SQUASH"), "merge_group"),
        (_org_rule("required_signatures"), "signed commits"),
        (_org_rule("update"), "restricts updates"),
        (_org_rule("code_scanning", code_scanning_tools=[{"tool": "CodeQL"}]), "CodeQL"),
    ],
)
def test_doctor_surfaces_inherited_requirements_forge_cannot_meet(tmp_path, rule, words):
    gh = _healthy_gh()
    gh.answers["api repos/owner/comic/rules/branches/main"] = _effective_rules(rule)
    report, check = _effective_check(gh, tmp_path)
    assert not report.ok and not check.ok and check.manual
    assert words in check.detail and "Organization owner" in check.detail


def test_doctor_reports_unreadable_effective_rules_as_a_manual_todo(tmp_path):
    gh = _healthy_gh()
    gh.answers["api repos/owner/comic/rules/branches/main"] = "not json"
    report, check = _effective_check(gh, tmp_path)
    assert not report.ok and not check.ok and check.manual
    assert "by hand" in check.detail and "[TODO]" in report.render()


def test_apply_still_manages_only_the_repository_rulesets():
    gh = FakeGh({"api repos/owner/comic/rulesets": json.dumps([])})
    _writable(gh)
    apply_repo_settings(spec(), gh)
    called = [" ".join(c) for c, _ in gh.calls]
    assert any("rulesets?includes_parents=false" in c for c in called)
    assert not any("includes_parents=true" in c or "/rules/branches/" in c for c in called)


# ---------------------------------------------------------------- Codex 4206780284


def _any_case(gh):
    """GitHub answers a repository path in any case (logins and names are case-insensitive)."""
    return lambda args, input=None: gh(
        [a.replace("Owner/Comic", "owner/comic") for a in args], input=input
    )


def test_doctor_requires_the_canonical_project_id_spelling(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec(project_id="Owner/Comic"))
    # doctor run from the policy's own (mis-cased) id: the API still answers every path...
    report = doctor(repo, "Owner/Comic", "owner/agentic-sdlc", _any_case(_healthy_gh()))
    failed = _failed(report)
    # ...but every run passes the canonical $GITHUB_REPOSITORY, compared exactly
    assert "policy project id matches the repository" in failed
    assert "use owner/comic" in failed["policy project id matches the repository"].detail
    assert not report.ok


def test_canonical_project_id_takes_githubs_spelling_of_the_same_repository():
    from agentic_sdlc.onboard import canonical_project_id

    writable = {"archived": False, "disabled": False}  # Codex 4240170627: checked here too
    gh = FakeGh({"api repos/OWNER/Comic": json.dumps({"full_name": "owner/comic", **writable})})
    assert canonical_project_id("OWNER/Comic", gh) == "owner/comic"
    # Codex 4239868019: a rename/transfer redirect answers with the CURRENT name, which is what
    # every run passes as $GITHUB_REPOSITORY: that is the canonical id, not the stale slug
    renamed = FakeGh({"api repos/owner/old": json.dumps({"full_name": "owner/new", **writable})})
    assert canonical_project_id("owner/old", renamed) == "owner/new"
    moved = FakeGh({"api repos/owner/comic": json.dumps({"full_name": "neworg/Comic", **writable})})
    assert canonical_project_id("owner/comic", moved) == "neworg/Comic"
    # Unreadable is never a guess: --apply would write the id into the policy and configure by it.
    for answer in ("not json", "", OnboardError("gh: HTTP 502"), "[]", "{}", '{"full_name": 7}'):
        with pytest.raises(OnboardError, match="could not read owner/comic"):
            canonical_project_id("owner/comic", FakeGh({"api repos/owner/comic": answer}))


def test_cli_onboard_apply_writes_the_canonical_project_id(tmp_path, monkeypatch):
    from agentic_sdlc import cli
    from agentic_sdlc.onboard import DoctorReport

    seen: list[str] = []
    monkeypatch.setattr(cli, "canonical_project_id", lambda project: "owner/comic")
    monkeypatch.setattr(cli, "apply_repo_settings", lambda spec, **k: seen.append(spec.project_id))
    monkeypatch.setattr(
        cli, "doctor", lambda root, project, *a, **k: seen.append(project) or DoctorReport()
    )
    repo = _repo(tmp_path)
    args = ["onboard", "--visibility", "private", "--destination", str(repo)]
    args += ["--project-id", "OWNER/Comic"]
    args += ["--platform-repository", "owner/agentic-sdlc", "--platform-ref", SHA]
    args += ["--test", "pytest -q", "--default-branch", "main", "--apply"]
    assert cli.main([*args, "--output", str(tmp_path / "o.json")]) == 0
    assert 'id = "owner/comic"' in (repo / "agentic-sdlc.toml").read_text()
    assert seen == ["owner/comic", "owner/comic"]


# ---------------------------------------------------------------- Codex 4238739183
# An INTERNAL caller can use only internal (or public) reusable workflows: a private platform
# rejects it whatever the access setting; an unknown platform visibility is a manual TODO.
def test_platform_access_rejects_an_internal_caller_of_a_private_platform(tmp_path):
    from agentic_sdlc.onboard import platform_access_check

    access = {"api repos/plat/forge/actions/permissions/access": '{"access_level": "organization"}'}
    internal = {**access, "api repos/plat/comic": '{"visibility": "internal", "private": true}'}
    private = {**access, "api repos/plat/comic": '{"visibility": "private", "private": true}'}

    rejected = platform_access_check(
        FakeGh(dict(internal)), "plat/comic", "plat/forge", None, "private"
    )
    assert not rejected.ok and not rejected.manual and "INTERNAL or public" in rejected.detail
    unknown = platform_access_check(FakeGh(dict(internal)), "plat/comic", "plat/forge", None, None)
    assert not unknown.ok and unknown.manual and "internal" in unknown.detail
    assert platform_access_check(
        FakeGh(dict(internal)), "plat/comic", "plat/forge", None, "internal"
    ).ok
    for platform in ("private", "internal", None):
        assert platform_access_check(
            FakeGh(dict(private)), "plat/comic", "plat/forge", None, platform
        ).ok
    # cross-owner enterprise sharing stays unverifiable for an internal pairing
    cross = platform_access_check(
        FakeGh(
            {
                "api repos/plat/forge/actions/permissions/access": '{"access_level": "enterprise"}',
                "api repos/owner/comic": '{"visibility": "internal"}',
            }
        ),
        "owner/comic",
        "plat/forge",
        None,
        "internal",
    )
    assert not cross.ok and cross.manual and "same enterprise" in cross.detail

    # end to end: doctor reads the platform's visibility
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _private_platform_gh("organization")
    gh.answers = {
        "api repos/owner/agentic-sdlc --jq {visibility}": '{"visibility": "private"}',
        **gh.answers,
    }
    gh.answers["api repos/owner/comic"] = json.dumps(
        {"default_branch": "main", "visibility": "internal", "private": True}
    )
    check = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))[PLATFORM_ACCESS]
    assert not check.manual and "is internal" in check.detail
    gh.answers["api repos/owner/agentic-sdlc --jq {visibility}"] = '{"visibility": "internal"}'
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    assert next(c for c in report.checks if c.name == PLATFORM_ACCESS).ok


# ---------------------------------------------------------------- Codex 4238739188
# Only workflows that run on EVERY pull request into the protected branch produce required
# contexts: schedule/dispatch-only and path- or branch-filtered workflows do not count.
@pytest.mark.parametrize(
    ("on", "counts"),
    [
        ("pull_request", True),
        ("[pull_request, schedule]", True),
        ("{pull_request_target: {branches: [main]}}", False),
        ("pull_request_target", False),
        ("[pull_request_target, push]", False),
        ("{pull_request: {branches: ['ma*']}}", True),
        ("{pull_request: {branches: ['**']}}", True),
        ("{schedule: [{cron: '0 0 * * *'}]}", False),
        ("workflow_dispatch", False),
        ("{workflow_call: {}}", False),
        ("push", False),
        ("{pull_request: {paths: ['src/**']}}", False),
        ("{pull_request: {paths-ignore: ['docs/**']}}", False),
        ("{pull_request: {branches: [develop]}}", False),
        ("{pull_request: {branches-ignore: [develop]}}", False),
        ("{pull_request: {branches: ['**', '!main']}}", False),
        ("{pull_request: {types: [opened]}}", False),
    ],
)
def test_required_contexts_come_only_from_workflows_running_on_protected_prs(tmp_path, on, counts):
    from agentic_sdlc.onboard import effective_rule_problems, workflow_check_contexts

    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    (workflows / "scan.yml").write_text(
        f"on: {on}\njobs:\n  security-scan:\n    runs-on: ubuntu-latest\n"
        "    steps: [{run: 'true'}]\n"
    )
    produced = workflow_check_contexts(tmp_path, "main")
    assert ("security-scan" in produced.pull_request) is counts
    rules = [
        {
            "type": "required_status_checks",
            "parameters": {"required_status_checks": [{"context": "security-scan"}]},
        }
    ]
    assert (effective_rule_problems(rules, produced) == []) is counts


# ---------------------------------------------------------------- Codex 4238739196
# Classic branch protection is enforced alongside rulesets but is not in rules/branches: doctor
# reads it too (404 = none; unreadable = manual TODO) and applies the same incompatibility rules.
LEGACY = "classic branch protection on the default branch fits Forge"


def test_legacy_protection_problems():
    from agentic_sdlc.onboard import legacy_protection_problems

    produced = ProducedContexts(frozenset({"test", "lint", "call / plan"}))
    assert legacy_protection_problems({}, produced) == []
    fine = {
        "required_status_checks": {
            "contexts": ["test", "call / plan"],
            "checks": [{"context": "test", "app_id": 15368}, {"context": "lint", "app_id": None}],
        },
        "required_linear_history": {"enabled": True},
        "required_signatures": {"enabled": False},
        "lock_branch": {"enabled": False},
        "required_pull_request_reviews": {"required_approving_review_count": 1},
    }
    assert legacy_protection_problems(fine, produced) == []
    bad = {
        "required_status_checks": {
            "contexts": ["security-scan", "lint"],
            "checks": [{"context": "lint", "app_id": 999}],
        },
        "required_signatures": {"enabled": True},
        "restrictions": {"users": [], "teams": [], "apps": []},
        "lock_branch": {"enabled": True},
    }
    problems = legacy_protection_problems(bad, produced)
    assert len(problems) == 5
    assert any("'security-scan'" in p and "no installed" in p for p in problems)
    assert any("'lint' from app 999" in p for p in problems)
    assert any("signed commits" in p for p in problems)
    assert any("restricts who can push" in p for p in problems)
    assert any("locks the branch" in p for p in problems)


@pytest.mark.parametrize(
    "required",
    [
        {"contexts": ["test"]},  # a bare context: its binding cannot be read
        {"contexts": ["test"], "checks": [{"context": "test", "app_id": None}]},  # any source
        {"contexts": ["test"], "checks": [{"context": "test", "app_id": -1}]},  # any source
        {
            "contexts": ["test"],
            "checks": [
                {"context": "test", "app_id": GITHUB_ACTIONS_INTEGRATION_ID},
                {"context": "test", "app_id": None},
            ],
        },
    ],
)
def test_classic_test_check_must_be_bound_to_github_actions(required):
    """Codex review 4239510554: a classic `test` requirement any app may satisfy (no app, or an
    unreadable binding) lets a `test` status from another integration pass Forge's gate."""
    from agentic_sdlc.onboard import legacy_protection_problems

    produced = ProducedContexts(frozenset({"test"}))
    problems = legacy_protection_problems({"required_status_checks": required}, produced)
    assert len(problems) == 1 and "'test' is not bound to GitHub Actions" in problems[0]
    bound = {
        "contexts": ["test"],
        "checks": [{"context": "test", "app_id": GITHUB_ACTIONS_INTEGRATION_ID}],
    }
    assert legacy_protection_problems({"required_status_checks": bound}, produced) == []


def test_ruleset_requires_the_test_check_bound_to_github_actions():
    """Codex review 4239510554: an unbound `test` in `Protect main` accepts the status from any
    integration. The payload binds it; doctor rejects an unbound one; the merge binds it while
    keeping every other rule and check."""
    payload = ruleset_payload(spec())
    gate = next(r for r in payload["rules"] if r["type"] == "required_status_checks")
    assert gate["parameters"]["required_status_checks"] == [
        {"context": "test", "integration_id": GITHUB_ACTIONS_INTEGRATION_ID}
    ]
    assert ruleset_mismatches(payload, spec()) == []
    unbound = json.loads(json.dumps(payload))
    unbound["rules"].append({"type": "required_linear_history"})
    checks = next(r for r in unbound["rules"] if r["type"] == "required_status_checks")
    checks["parameters"]["required_status_checks"] = [
        {"context": "test"},
        {"context": "lint"},
    ]
    problems = ruleset_mismatches(unbound, spec())
    assert any("'test' is not bound to GitHub Actions" in p for p in problems), problems
    merged = merged_ruleset(unbound, spec())
    assert ruleset_mismatches(merged, spec()) == []
    assert {"type": "required_linear_history"} in merged["rules"]
    gate = next(r for r in merged["rules"] if r["type"] == "required_status_checks")
    assert gate["parameters"]["required_status_checks"] == [
        {"context": "test", "integration_id": GITHUB_ACTIONS_INTEGRATION_ID},
        {"context": "lint"},  # not Forge's context: left as the owner set it
    ]


def test_doctor_checks_classic_branch_protection(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    key = "api repos/owner/comic/branches/main/protection"
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", _healthy_gh())
    assert report.ok, report.render()
    assert next(c for c in report.checks if c.name == LEGACY).ok

    gh = _healthy_gh()
    gh.answers[key] = json.dumps(
        {
            "required_status_checks": {"contexts": ["security-scan"], "checks": []},
            "required_signatures": {"enabled": True},
        }
    )
    check = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))[LEGACY]
    assert "'security-scan'" in check.detail and "signed commits" in check.detail

    gh.answers[key] = OnboardError("gh: Must have admin rights to Repository. (HTTP 403)")
    check = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))[LEGACY]
    assert check.manual and "cannot read branch protection" in check.detail


# ---------------------------------------------------------------- review regressions (PR 139, 6)
# Codex 4238832970: a branch change earlier in one command re-targets the git after it.


def _branch_flow_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    _git(repo, "branch", "forge/issue-7")
    (tmp_path / "wt").mkdir()
    _git(tmp_path / "wt", "init", "-q", "-b", "forge/issue-9")
    _git(tmp_path / "wt", "commit", "-q", "--allow-empty", "-m", "init")
    return repo


def _flow_code(guard, command, cwd, leased=()):
    branches = guard.commit_branches(command, str(cwd))
    payload = {"tool_name": "Bash", "session_id": "s1", "cwd": str(cwd)}
    lease = {"session": "s1", "agent": "claude-code"}
    return guard.decide(
        {**payload, "tool_input": {"command": command}},
        branches,
        lambda n: dict(lease) if n in leased else None,
        lambda b: False,
    )


@pytest.mark.parametrize(
    "command",
    [
        "git switch forge/issue-7 && git commit -m x",
        "git checkout forge/issue-7 && git commit -m x",
        "git checkout -b forge/issue-7 && git commit -m x",
        "git switch -c forge/issue-7 && git commit -m x",
        "git checkout -q forge/issue-7; git commit -m x",
        "git switch forge/issue-7 && git add . && git commit -m x",
        "bash -c 'git switch forge/issue-7 && git commit -m x'",
        "git switch forge/issue-7 && echo ok && git merge main",
    ],
)
def test_commit_guard_follows_a_static_branch_change_in_the_same_command(tmp_path, command):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    repo = _branch_flow_repo(tmp_path)
    code, message = _flow_code(guard, command, repo)
    assert code == 2 and "issue #7" in message, command
    assert _flow_code(guard, command, repo, leased=(7,))[0] == 0, command


def test_commit_guard_a_switch_off_an_issue_branch_is_checked_against_its_target(tmp_path):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    repo = _branch_flow_repo(tmp_path)
    _git(repo, "switch", "-q", "forge/issue-7")
    # a pure switch to an existing branch rewrites nothing; the commit lands on main
    assert _flow_code(guard, "git switch main && git commit -m x", repo)[0] == 0
    assert _flow_code(guard, "git switch main", repo)[0] == 0
    # `;` does not prove the switch happened: the commit may still land on issue 7
    assert _flow_code(guard, "git switch main; git commit -m x", repo)[0] == 2
    assert _flow_code(guard, "git commit -m x", repo)[0] == 2


@pytest.mark.parametrize(
    "command",
    [
        'git checkout "$B" && git commit -m x',
        "git checkout - && git commit -m x",
        "git switch @{-1} && git commit -m x",
        "git bisect start && git commit -m x",
        "git co forge/issue-7 && git commit -m x",
        "source switch.sh && git commit -m x",
        'eval "$SWITCH" && git commit -m x',
        "pushd ../wt && git commit -m x",
        'cd "$WT" && git commit -m x',
        "export GIT_DIR=../wt/.git; git commit -m x",
        "echo forge/issue-7 | xargs git switch && git commit -m x",
        'git commit -m "$(git switch forge/issue-7)"',
    ],
)
def test_commit_guard_blocks_an_unknowable_branch_change_before_git(tmp_path, command):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    repo = _branch_flow_repo(tmp_path)
    code, message = _flow_code(guard, command, repo, leased=(7, 9))
    assert code == 2 and "SEPARATE commands" in message, command


def test_commit_guard_branch_flow_keeps_existing_cd_and_plain_commands(tmp_path):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    repo = _branch_flow_repo(tmp_path)
    assert _flow_code(guard, "cd ../wt && git commit -m x", repo)[0] == 2  # issue 9, no lease
    assert _flow_code(guard, "cd ../wt && git commit -m x", repo, leased=(9,))[0] == 0
    for allowed in (
        "git commit -m x",
        "ruff check . && pytest -q && git commit -m x",
        "git add . && git commit -m x && git push",
        "git checkout -- README.md && git commit -m x",
        "git stash && git switch main && git stash pop",
        'gh pr create --title "git: tidy" && git push',
        "git checkout -b feature && git commit -m x",
    ):
        assert _flow_code(guard, allowed, repo)[0] == 0, allowed


# Codex 4238832967: a reusable call reports `caller / callee-job` for the callee's REAL jobs only.
def test_reusable_call_contexts_come_from_the_callees_jobs(tmp_path):
    from agentic_sdlc.onboard import effective_rule_problems, workflow_check_contexts

    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    pin = "a" * 40
    (workflows / "pr.yml").write_text(
        "on: pull_request\n"
        "jobs:\n"
        "  plan:\n"
        "    uses: ./.github/workflows/reuse.yml\n"
        "  remote:\n"
        "    name: Remote\n"
        f"    uses: owner/agentic-sdlc/.github/workflows/reusable-plan.yml@{pin}\n"
        "  third:\n"
        "    uses: someone/else/.github/workflows/x.yml@v1\n"
    )
    (workflows / "reuse.yml").write_text(
        "on: workflow_call\n"
        "jobs:\n"
        "  build:\n"
        "    name: Build it\n"
        "    runs-on: ubuntu-latest\n"
        "    steps: [{run: 'true'}]\n"
        "  lint:\n"
        "    strategy: {matrix: {py: ['3.11']}}\n"
        "    runs-on: ubuntu-latest\n"
        "    steps: [{run: 'true'}]\n"
        "  label:\n"
        "    name: ${{ inputs.label }}\n"
        "    runs-on: ubuntu-latest\n"
        "    steps: [{run: 'true'}]\n"
        "  nested:\n"
        "    uses: ./.github/workflows/leaf.yml\n"
    )
    (workflows / "leaf.yml").write_text(
        "on: workflow_call\njobs:\n  leaf:\n    runs-on: ubuntu-latest\n"
        "    steps: [{run: 'true'}]\n"
    )
    callee = {"on": {"workflow_call": {}}, "jobs": {"prepare": {"runs-on": "x"}}}

    def load(name, sha):
        return callee if (name, sha) == ("reusable-plan.yml", pin) else None

    produced = workflow_check_contexts(tmp_path, "main", "owner/agentic-sdlc", load)
    exact = produced.pull_request
    assert {"plan / Build it", "plan / nested / leaf", "Remote / prepare"} <= exact
    assert "plan / lint (3.11)" in exact and not any(c.endswith(" / ") for c in exact)

    def required(*contexts):
        checks = [{"context": c} for c in contexts]
        return [
            {"type": "required_status_checks", "parameters": {"required_status_checks": checks}}
        ]

    fine = ("plan / Build it", "plan / lint (3.11)", "plan / nested / leaf", "Remote / prepare")
    assert effective_rule_problems(required(*fine), produced) == []
    for bogus in (
        "plan / nonexistent",
        "plan / lint (3.11) / x",
        "plan / ",
        "Remote / nonexistent",
        "third / anything",
        "plan / build",  # the job id: GitHub reports its `name:`
        "plan / lint (3.12)",  # not a combination of the matrix
        "plan / lint",  # a matrix job reports only its combinations
    ):
        assert effective_rule_problems(required(bogus), produced), bogus
    # without the pinned callee readable, the remote call reports nothing
    assert "Remote / prepare" not in workflow_check_contexts(tmp_path, "main").pull_request


# Codex 4238832973: required contexts are judged on the DEFAULT BRANCH's workflows.
def test_doctor_judges_required_contexts_on_the_default_branchs_workflows(tmp_path):
    from agentic_sdlc.onboard import EFFECTIVE_RULES_CHECK

    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    pushed = {p.name: p.read_text() for p in (repo / ".github/workflows").glob("*.yml")}
    scan = "on: pull_request\njobs:\n  security-scan:\n    runs-on: ubuntu-latest\n"
    scan += "    steps: [{run: 'true'}]\n"
    rule = _org_rule(
        "required_status_checks", required_status_checks=[{"context": "security-scan"}]
    )

    # only in the checkout (not pushed): not produced, and reported as drift
    _wf(repo, "scan.yml", scan)
    gh = _healthy_gh()
    gh.answers["api repos/owner/comic/rules/branches/main"] = _effective_rules(rule)
    _remote_workflows(gh, pushed)
    check = next(
        c
        for c in doctor(repo, "owner/comic", "owner/agentic-sdlc", gh).checks
        if c.name == EFFECTIVE_RULES_CHECK
    )
    assert not check.ok and "'security-scan', which no installed" in check.detail
    assert "local checkout: 'security-scan'" in check.detail

    # on the default branch (even if not in the checkout): produced
    (repo / ".github/workflows/scan.yml").unlink()
    gh = _healthy_gh()
    gh.answers["api repos/owner/comic/rules/branches/main"] = _effective_rules(rule)
    _remote_workflows(gh, {**pushed, "scan.yml": scan})
    check = next(
        c
        for c in doctor(repo, "owner/comic", "owner/agentic-sdlc", gh).checks
        if c.name == EFFECTIVE_RULES_CHECK
    )
    assert check.ok, check.detail

    # the default branch's set unreadable: a manual TODO, never the checkout's answer
    _wf(repo, "scan.yml", scan)
    gh = _healthy_gh()
    gh.answers["api repos/owner/comic/rules/branches/main"] = _effective_rules(rule)
    gh.answers[CONSUMER_CONTENTS + ".github/workflows?ref=main"] = "not json"
    check = next(
        c
        for c in doctor(repo, "owner/comic", "owner/agentic-sdlc", gh).checks
        if c.name == EFFECTIVE_RULES_CHECK
    )
    assert not check.ok and check.manual and "cannot be verified" in check.detail


# Codex 4238832976: anyone can open the work-request template, which applies the planning labels;
# the labeler is authorized on a GitHub-hosted runner before anything is scheduled on RUNS_ON.
def test_auto_plan_authorizes_the_labeler_on_a_hosted_runner_first():
    doc = yaml.safe_load(
        render_onboarding(spec(runs_on=("self-hosted", "linux")))[
            ".github/workflows/agent-auto-plan.yml"
        ]
    )
    preflight, plan = doc["jobs"]["preflight"], doc["jobs"]["plan"]
    assert preflight["runs-on"] == "ubuntu-latest"
    assert "github.event.label.name == 'claude-ready'" in preflight["if"]
    assert preflight["outputs"]["authorized"] == "${{ steps.authorize.outputs.authorized }}"
    step = preflight["steps"][0]
    assert step["env"]["SENDER"] == "${{ github.event.sender.login }}"
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "collaborators/${encoded}/permission" in step["run"]
    assert "admin|write) ;;" in step["run"]
    assert plan["needs"] == ["preflight", "dedupe"]
    assert plan["if"] == (
        "needs.preflight.outputs.authorized == 'true' && needs.dedupe.outputs.fresh == 'true'"
    )
    assert plan["uses"].startswith("owner/agentic-sdlc/.github/workflows/reusable-plan.yml@")
    # every job that can reach a self-hosted runner waits on the authorization
    for name, job in doc["jobs"].items():
        if name != "preflight" and job.get("runs-on") != "ubuntu-latest":
            needs = job.get("needs")
            needs = [needs] if isinstance(needs, str) else needs or []
            assert "preflight" in needs or "plan" in needs, name


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["jobs"]["preflight"].update({"runs-on": ["self-hosted", "linux"]}),
        lambda d: d["jobs"]["plan"].pop("if"),
        lambda d: d["jobs"]["plan"].pop("needs"),
        lambda d: d["jobs"].pop("preflight"),
    ],
)
def test_doctor_requires_the_auto_plan_authorization_preflight(tmp_path, change):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    assert _drift(repo) is None
    _edit_workflow(repo, "agent-auto-plan.yml", change)
    drift = _drift(repo)
    assert drift is not None and "agent-auto-plan.yml" in drift


# Codex 4238935445: a Triage user can apply the approval labels without write access; the labeler
# is authorized on a GitHub-hosted runner before any job is scheduled on the configured runner.
@pytest.mark.parametrize("implementer", ["route", "claude", "codex"])
def test_auto_implement_authorizes_the_labeler_on_a_hosted_runner_first(implementer):
    files = render_onboarding(spec(implementer=implementer, runs_on=("self-hosted", "linux")))
    doc = yaml.safe_load(files[".github/workflows/agent-auto-implement.yml"])
    plan = yaml.safe_load(files[".github/workflows/agent-auto-plan.yml"])
    authorize = doc["jobs"]["authorize"]
    assert authorize["runs-on"] == "ubuntu-latest"
    assert "github.event.label.name == 'implementation-approved'" in authorize["if"]
    assert authorize["outputs"]["authorized"] == "${{ steps.authorize.outputs.authorized }}"
    # the same authorization as automatic planning, step for step but the notice wording
    step, plan_step = authorize["steps"][0], plan["jobs"]["preflight"]["steps"][0]
    # (but the label whose latest applier it authorizes: Codex 4239965189)
    assert step["env"]["APPROVAL_LABEL"] == "implementation-approved"
    assert plan_step["env"]["APPROVAL_LABEL"] == "claude-ready"
    # ... and the label set whose latest appliers it authorizes (Codex 4240421144)
    assert json.loads(step["env"]["REQUIRED_LABELS"]) == [
        "implementation-approved",
        "claude-ready",
        "human-review-required",
    ]
    assert json.loads(plan_step["env"]["REQUIRED_LABELS"]) == [
        "claude-ready",
        "human-review-required",
    ]
    masked = {"APPROVAL_LABEL": "x", "REQUIRED_LABELS": "x"}
    assert {**step["env"], **masked} == {**plan_step["env"], **masked}
    assert step["run"].replace("Implementation", "Planning") == plan_step["run"]
    assert "admin|write) ;;" in step["run"]
    gate = "needs.authorize.outputs.authorized == 'true'"
    for name, job in doc["jobs"].items():
        if name in ("authorize", "authorization_failed"):
            continue  # the hosted failure reporter runs exactly when authorization did not
        needs = job.get("needs")
        needs = [needs] if isinstance(needs, str) else needs or []
        assert "authorize" in needs, name
        assert gate in job["if"], name
    assert doc["jobs"]["preflight"]["if"] == gate
    assert doc["jobs"]["implement"]["if"] == gate
    assert doc["jobs"]["notify_failure"]["if"].startswith(f"${{{{ always() && {gate} &&")
    # workflow_dispatch already requires write access: no authorization job there
    manual = yaml.safe_load(files[".github/workflows/agent-implement.yml"])
    assert "authorize" not in manual["jobs"]
    assert manual["jobs"]["implement"]["needs"] == "preflight"
    assert "if" not in manual["jobs"]["preflight"]


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["jobs"]["authorize"].update({"runs-on": ["self-hosted", "linux"]}),
        lambda d: d["jobs"]["preflight"].pop("if"),
        lambda d: d["jobs"]["preflight"].pop("needs"),
        lambda d: d["jobs"]["implement"].pop("if"),
        lambda d: d["jobs"]["notify_failure"].update({"needs": ["preflight", "implement"]}),
        lambda d: d["jobs"].pop("authorize"),
    ],
)
def test_doctor_requires_the_auto_implement_authorization(tmp_path, change):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    assert _drift(repo) is None
    _edit_workflow(repo, "agent-auto-implement.yml", change)
    drift = _drift(repo)
    assert drift is not None and "agent-auto-implement.yml" in drift


# Codex 4240028941: naming any job permission sets the others to none, and the authorization
# step lists the issue's label events, which needs `issues: read` in a private repository.
@pytest.mark.parametrize(
    ("name", "job"),
    [("agent-auto-implement.yml", "authorize"), ("agent-auto-plan.yml", "preflight")],
)
def test_labeler_authorization_job_can_read_the_issue_events(name, job):
    for implementer in ("route", "claude", "codex"):
        doc = yaml.safe_load(
            render_onboarding(spec(implementer=implementer))[f".github/workflows/{name}"]
        )
        authorize = doc["jobs"][job]
        assert "/issues/${ISSUE_NUMBER}/events" in authorize["steps"][0]["run"]
        # least privilege: exactly the scopes the step's API calls need
        assert authorize["permissions"] == {"contents": "read", "issues": "read"}


@pytest.mark.parametrize(
    ("name", "job"),
    [("agent-auto-implement.yml", "authorize"), ("agent-auto-plan.yml", "preflight")],
)
def test_doctor_requires_issue_read_on_the_authorization_job(tmp_path, name, job):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    assert _drift(repo) is None
    _edit_workflow(repo, name, lambda d: d["jobs"][job]["permissions"].pop("issues"))
    drift = _drift(repo)
    assert drift is not None and name in drift and "permissions" in drift


_ISSUE_READ_CALLS = ("/issues/${ISSUE_NUMBER}/events",)
_ISSUE_WRITE_CALLS = ("issues.createComment", "issues.addLabels", "issues.removeLabel")
_LEVELS = {"none": 0, "read": 1, "write": 2}


def test_every_rendered_forge_job_has_the_permissions_its_api_calls_need():
    """Audit: a job that names `permissions` has no scope it does not name, so every REST call
    a step makes with the job token must be covered by what the job grants."""
    for implementer in ("route", "claude", "codex", "cloud-routine"):
        files = render_onboarding(spec(implementer=implementer))
        for path, text in files.items():
            if not path.startswith(".github/workflows/"):
                continue
            doc = yaml.safe_load(text)
            for job_name, job in (doc.get("jobs") or {}).items():
                steps = job.get("steps") or []
                body = "\n".join(
                    str(step.get("run", "")) + str((step.get("with") or {}).get("script", ""))
                    for step in steps
                )
                granted = job.get("permissions", doc.get("permissions"))
                granted = granted if isinstance(granted, dict) else {}
                issues = _LEVELS[granted.get("issues", "none")]
                where = f"{path}:{job_name}"
                if any(call in body for call in _ISSUE_READ_CALLS):
                    assert issues >= _LEVELS["read"], where
                if any(call in body for call in _ISSUE_WRITE_CALLS):
                    assert issues >= _LEVELS["write"], where
                if "actions/checkout@" in str(steps):
                    assert _LEVELS[granted.get("contents", "none")] >= 1, where


# Codex 4238935465: GitHub retired the macOS 13 images on 2025-12-04; their labels never start.
@pytest.mark.parametrize("label", ["macos-13", "macos-13-large", "macos-13-xlarge"])
def test_retired_macos_13_labels_are_not_github_hosted(tmp_path, label):
    from agentic_sdlc.onboard import GITHUB_HOSTED_LABELS, _github_hosted

    assert label not in GITHUB_HOSTED_LABELS
    assert not _github_hosted({label})
    with pytest.raises(OnboardError, match="fork-exposed"):
        spec(fork_exposed=True, ci_runs_on=(label,))
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    ci = repo / ".github/workflows/ci.yml"
    doc = yaml.safe_load(ci.read_text())
    doc["jobs"]["test"]["runs-on"] = label
    ci.write_text(yaml.safe_dump(doc, sort_keys=False))
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", _healthy_gh()))
    assert "ci.yml test job runs on an available runner" in failed


# Codex 4238935452: a matrix job reports one context per concrete combination, `name (v1, ...)`;
# no prefix is accepted, so a combination the matrix never produces is never "reported".
def _contexts_of(tmp_path, workflow: str) -> frozenset[str]:
    from agentic_sdlc.onboard import workflow_check_contexts

    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    (workflows / "ci.yml").write_text(workflow)
    return workflow_check_contexts(tmp_path, "main").pull_request


def test_matrix_jobs_report_only_their_concrete_combinations(tmp_path):
    produced = _contexts_of(
        tmp_path,
        "on: pull_request\n"
        "jobs:\n"
        "  test:\n"
        "    strategy: {matrix: {os: [ubuntu-latest]}}\n"
        "    runs-on: ${{ matrix.os }}\n"
        "    steps: [{run: 'true'}]\n",
    )
    assert produced == {"test (ubuntu-latest)"}
    assert "test (windows-latest)" not in produced and "test" not in produced
    from agentic_sdlc.onboard import effective_rule_problems

    rules = _queue_rules("test (windows-latest)", "test")[1:]
    problems = effective_rule_problems(rules, ProducedContexts(produced))
    assert len(problems) == 2 and all("no installed workflow job reports" in p for p in problems)


def test_matrix_contexts_follow_axes_exclude_and_include(tmp_path):
    produced = _contexts_of(
        tmp_path,
        "on: pull_request\n"
        "jobs:\n"
        "  test:\n"
        "    name: Unit\n"
        "    strategy:\n"
        "      matrix:\n"
        "        os: [ubuntu-latest, macos-15]\n"
        "        py: ['3.11', '3.12']\n"
        "        exclude: [{os: macos-15, py: '3.11'}]\n"
        "        include:\n"
        "          - {os: ubuntu-latest, py: '3.12', experimental: true}\n"
        "          - {os: ubuntu-24.04-arm, py: '3.13'}\n"
        "    runs-on: ${{ matrix.os }}\n"
        "    steps: [{run: 'true'}]\n"
        "  lint:\n"
        "    strategy: {matrix: {tool: [ruff]}}\n"
        "    runs-on: ubuntu-latest\n"
        "    steps: [{run: 'true'}]\n"
        "  named:\n"
        "    name: Check ${{ matrix.tool }}\n"
        "    strategy: {matrix: {tool: [ruff, mypy]}}\n"
        "    runs-on: ubuntu-latest\n"
        "    steps: [{run: 'true'}]\n",
    )
    assert produced == {
        # a static name gets the values appended, axes in order, include's extra key last
        "Unit (ubuntu-latest, 3.11)",
        "Unit (ubuntu-latest, 3.12, true)",
        "Unit (macos-15, 3.12)",
        "Unit (ubuntu-24.04-arm, 3.13)",
        # an unnamed job: its id, values appended
        "lint (ruff)",
        # a name holding a matrix expression: exactly the expanded name, nothing appended
        "Check ruff",
        "Check mypy",
    }


@pytest.mark.parametrize(
    "matrix",
    [
        "'${{ fromJSON(inputs.oses) }}'",
        "{os: '${{ fromJSON(inputs.oses) }}'}",
        "{os: [{name: ubuntu}]}",
        "{os: [ubuntu-latest], exclude: [{os: ubuntu-latest}]}",
    ],
)
def test_unresolvable_or_empty_matrices_report_nothing(tmp_path, matrix):
    produced = _contexts_of(
        tmp_path,
        "on: pull_request\n"
        "jobs:\n"
        "  test:\n"
        f"    strategy: {{matrix: {matrix}}}\n"
        "    runs-on: ubuntu-latest\n"
        "    steps: [{run: 'true'}]\n",
    )
    assert not produced


# Codex 4238935454: with a merge queue, every required context must ALSO be reported in the merge
# group: a workflow that declares `merge_group` but does not produce `test` does not serve it.
def _queue_rules(*contexts):
    return [
        {"type": "merge_queue", "parameters": {}},
        {
            "type": "required_status_checks",
            "parameters": {
                "required_status_checks": [
                    {"context": c, "integration_id": GITHUB_ACTIONS_INTEGRATION_ID}
                    for c in contexts
                ]
            },
        },
    ]


@pytest.mark.parametrize(
    ("ci_on", "noop_on", "ok"),
    [
        ("[pull_request, merge_group]", None, True),
        ("{pull_request: {}, merge_group: {types: [checks_requested]}}", None, True),
        ("{pull_request: {}, merge_group: {branches: [main]}}", None, True),
        ("pull_request", "merge_group", False),  # an unrelated no-op serves the queue only
        ("{pull_request: {}, merge_group: {branches: [develop]}}", None, False),
        ("{pull_request: {}, merge_group: {branches-ignore: [develop]}}", None, False),
    ],
)
def test_merge_queue_requires_each_context_in_the_merge_group(tmp_path, ci_on, noop_on, ok):
    from agentic_sdlc.onboard import effective_rule_problems, workflow_check_contexts

    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        f"on: {ci_on}\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps: [{{run: 'true'}}]\n"
    )
    if noop_on:
        (workflows / "noop.yml").write_text(
            f"on: {noop_on}\njobs:\n  noop:\n    runs-on: ubuntu-latest\n"
            "    steps: [{run: 'true'}]\n"
        )
    produced = workflow_check_contexts(tmp_path, "main")
    assert "test" in produced.pull_request and produced.merge_group_declared
    problems = effective_rule_problems(_queue_rules("test"), produced)
    if ok:
        assert problems == []
    else:
        assert len(problems) == 1 and "'test'" in problems[0] and "merge_group" in problems[0]
    # without a merge queue the PR contexts alone decide
    assert effective_rule_problems(_queue_rules("test")[1:], produced) == []


# Codex 4238935460: a disabled workflow does not respond to its triggers. Managed workflows must
# be `active`; a non-active workflow is no evidence for a required context; unreadable = TODO.
def _states(gh: FakeGh, states: dict[str, str]) -> None:
    rows = [{"path": f".github/workflows/{n}", "state": s} for n, s in sorted(states.items())]
    gh.answers[CONSUMER_WORKFLOW_STATES] = json.dumps({"total_count": len(rows), "workflows": rows})


def _doctor_checks(repo, gh) -> dict:
    return {c.name: c for c in doctor(repo, "owner/comic", "owner/agentic-sdlc", gh).checks}


def _all_active(repo) -> dict[str, str]:
    return {p.name: "active" for p in (repo / ".github/workflows").glob("*.yml")}


def test_doctor_requires_managed_workflows_to_be_active(tmp_path):
    from agentic_sdlc.onboard import EFFECTIVE_RULES_CHECK, WORKFLOWS_ACTIVE_CHECK

    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _healthy_gh()
    _states(gh, _all_active(repo))
    checks = _doctor_checks(repo, gh)
    assert checks[WORKFLOWS_ACTIVE_CHECK].ok
    # ci.yml disabled: its `test` (required by Forge's own ruleset) is no longer produced
    gh = _healthy_gh()
    _states(gh, {**_all_active(repo), "ci.yml": "disabled_manually"})
    checks = _doctor_checks(repo, gh)
    active = checks[WORKFLOWS_ACTIVE_CHECK]
    assert not active.ok and active.manual and "ci.yml (disabled_manually)" in active.detail
    rules = checks[EFFECTIVE_RULES_CHECK]
    assert not rules.ok and "'test'" in rules.detail and "not active" in rules.detail
    # a managed workflow GitHub has not registered at all is not proven to run either
    gh = _healthy_gh()
    _states(gh, {n: s for n, s in _all_active(repo).items() if n != "agent-auto-implement.yml"})
    active = _doctor_checks(repo, gh)[WORKFLOWS_ACTIVE_CHECK]
    assert not active.ok and "agent-auto-implement.yml (not registered" in active.detail


def test_disabled_context_producing_workflow_is_no_evidence(tmp_path):
    from agentic_sdlc.onboard import EFFECTIVE_RULES_CHECK, WORKFLOWS_ACTIVE_CHECK

    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    _wf(repo, "scan.yml", "on: pull_request\njobs:\n  scan:\n    runs-on: ubuntu-latest\n")
    rule = _org_rule("required_status_checks", required_status_checks=[{"context": "scan"}])
    gh = _healthy_gh()
    gh.answers["api repos/owner/comic/rules/branches/main"] = _effective_rules(rule)
    assert _doctor_checks(repo, gh)[EFFECTIVE_RULES_CHECK].ok
    gh = _healthy_gh()
    gh.answers["api repos/owner/comic/rules/branches/main"] = _effective_rules(rule)
    _states(gh, {**_all_active(repo), "scan.yml": "disabled_inactivity"})
    checks = _doctor_checks(repo, gh)
    assert checks[WORKFLOWS_ACTIVE_CHECK].ok  # not a managed workflow
    rules = checks[EFFECTIVE_RULES_CHECK]
    assert not rules.ok
    assert "'scan', which no installed workflow job reports" in rules.detail
    assert "not active in GitHub Actions (scan.yml) → enable it" in rules.detail
    # a disabled workflow is no merge-group evidence either
    rules = [
        _org_rule("merge_queue"),
        _org_rule(
            "required_status_checks",
            required_status_checks=[
                {"context": "test", "integration_id": GITHUB_ACTIONS_INTEGRATION_ID}
            ],
        ),
    ]
    ci = repo / ".github/workflows/ci.yml"
    _wf(repo, "queue.yml", ci.read_text().replace("pull_request:", "merge_group:", 1))
    gh = _healthy_gh()
    gh.answers["api repos/owner/comic/rules/branches/main"] = _effective_rules(*rules)
    assert _doctor_checks(repo, gh)[EFFECTIVE_RULES_CHECK].ok  # queue.yml serves the queue
    gh = _healthy_gh()
    gh.answers["api repos/owner/comic/rules/branches/main"] = _effective_rules(*rules)
    _states(gh, {**_all_active(repo), "queue.yml": "disabled_manually"})
    detail = _doctor_checks(repo, gh)[EFFECTIVE_RULES_CHECK].detail
    assert "no installed workflow runs on merge_group" in detail


def test_unreadable_workflow_states_are_a_manual_todo(tmp_path):
    from agentic_sdlc.onboard import EFFECTIVE_RULES_CHECK, WORKFLOWS_ACTIVE_CHECK

    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _healthy_gh()
    gh.answers[CONSUMER_WORKFLOW_STATES] = OnboardError("gh: Forbidden (HTTP 403)")
    checks = _doctor_checks(repo, gh)
    active = checks[WORKFLOWS_ACTIVE_CHECK]
    assert not active.ok and active.manual and "could not read" in active.detail
    rules = checks[EFFECTIVE_RULES_CHECK]
    assert not rules.ok and rules.manual and "workflow states unreadable" in rules.detail


def test_remote_workflow_states_reads_every_page():
    from agentic_sdlc.onboard import remote_workflow_states

    rows = [{"path": f".github/workflows/w{i}.yml", "state": "active"} for i in range(150)]
    rows[120]["state"] = "disabled_manually"
    rows.append({"path": "dynamic/github-code-scanning/codeql", "state": "active"})
    calls = []

    def gh(args, input=None):
        calls.append(args[1])
        page = int(args[1].rsplit("page=", 1)[1])
        assert "per_page=100" in args[1]
        chunk = rows[(page - 1) * 100 : page * 100]
        return json.dumps({"total_count": len(rows), "workflows": chunk})

    states, why = remote_workflow_states(gh, "owner/comic")
    assert why == "" and len(calls) == 2
    assert len(states) == 150 and states["w120.yml"] == "disabled_manually"
    assert states["w0.yml"] == "active" and "codeql" not in states

    def broken(args, input=None):
        if args[1].endswith("page=2"):
            raise OnboardError("gh: HTTP 502")
        return json.dumps({"total_count": len(rows), "workflows": rows[:100]})

    assert remote_workflow_states(broken, "owner/comic")[0] is None


# ---------------------------------------------------------------- unreadable is never empty
# Codex 4239080218 / 4239080211 (root cause): an API answer that cannot be read -- a failed call,
# an empty or non-JSON body, a malformed page -- was read as "empty", "absent" or "public".
# Now it is `Unreadable`: doctor never reports READY on it, and --apply aborts before mutating.
_UNREADABLE_ANSWERS = [
    pytest.param(OnboardError("gh: HTTP 502 Bad Gateway"), id="failed"),
    pytest.param("not json", id="not-json"),
    pytest.param("", id="empty"),
]

# (the gh answer key doctor reads at that call site, the check that must not pass)
_ACTIONS_POLICY = "repository Actions policy allows the generated workflows"
_PLATFORM_REF = "platform ref reachable (reusable workflows exist at pin)"
_EFFECTIVE = "effective default-branch rules (org/enterprise rulesets too) fit Forge"
_CLASSIC = "classic branch protection on the default branch fits Forge"
_DOCTOR_READ_SITES = [
    ("api repos/owner/comic", "policy project id matches the repository"),
    ("api repos/owner/agentic-sdlc --jq .private", PLATFORM_ACCESS),
    ("api repos/owner/comic/actions/permissions", _ACTIONS_POLICY),
    (PLATFORM_CONTENTS + ".github/workflows/reusable-implement.yml", _PLATFORM_REF),
    (CONSUMER_CONTENTS + "agentic-sdlc.toml", "managed files are on the default branch"),
    ("api repos/owner/comic/labels", "Forge labels exist"),
    ("api repos/owner/comic/rulesets/11", f"ruleset '{RULESET_NAME}' active on default branch"),
    ("api repos/owner/comic/rulesets", f"ruleset '{RULESET_NAME}' active on default branch"),
    (CONSUMER_WORKFLOW_STATES, "Forge workflows are active in GitHub Actions"),
    ("api repos/owner/comic/rules/branches/main", _EFFECTIVE),
    ("api repos/owner/comic/branches/main/protection", _CLASSIC),
    ("api repos/owner/comic/actions/variables", "repo variables set (non-empty)"),
    ("api repos/owner/comic/actions/secrets", "repo secrets set (by name)"),
    ("api repos/owner/comic/actions/runners", "self-hosted runner online for this repo"),
    ("api /user/installations/", "Publisher GitHub App installed on this repo"),
    ("api /user/installations", "Publisher GitHub App installed on this repo"),
    ("api apps/agentic-sdlc-publisher", "Publisher GitHub App installed on this repo"),
]


@pytest.mark.parametrize("answer", _UNREADABLE_ANSWERS)
@pytest.mark.parametrize(("key", "check"), _DOCTOR_READ_SITES)
def test_an_unreadable_answer_never_yields_ready(tmp_path, key, check, answer):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _healthy_gh()  # READY as is (test_doctor_is_green_for_a_fully_configured_repo)
    gh.answers[key] = answer  # replaces in place: prefix order is kept
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    assert not report.ok and "NOT READY" in report.render()
    assert check in _failed(report), report.render()


@pytest.mark.parametrize(
    "answer",
    [
        *_UNREADABLE_ANSWERS,
        pytest.param(json.dumps({"id": 5}), id="not-a-list"),
        pytest.param(json.dumps([{"id": 5, "name": RULESET_NAME}]), id="page-not-a-list"),
        pytest.param(json.dumps([["junk"]]), id="item-not-an-object"),
    ],
)
def test_apply_aborts_before_any_mutation_when_the_ruleset_inventory_is_unreadable(answer):
    gh = FakeGh({"api repos/owner/comic/rulesets": answer})
    with pytest.raises(OnboardError, match="could not read the rulesets.*nothing was changed"):
        _writable(gh)
        apply_repo_settings(spec(), gh, variables={"PUBLISHER_APP_CLIENT_ID": "Iv1"})
    # only reads: no label, no ruleset POST/PUT (a duplicate 'Protect main'), no variable
    assert all(c[0] == "api" and "-X" not in c for c, _ in gh.calls), gh.calls


@pytest.mark.parametrize(
    "answer", [*_UNREADABLE_ANSWERS, pytest.param(json.dumps({"id": 5}), id="no-rules")]
)
def test_apply_aborts_before_any_mutation_when_the_existing_ruleset_is_unreadable(answer):
    gh = FakeGh(
        {
            "api repos/owner/comic/rulesets/5": answer,
            "api repos/owner/comic/rulesets": json.dumps([[{"id": 5, "name": RULESET_NAME}]]),
        }
    )
    with pytest.raises(OnboardError, match=f"could not read ruleset '{RULESET_NAME}'"):
        _writable(gh)
        apply_repo_settings(spec(), gh)
    assert all(c[0] == "api" and "-X" not in c for c, _ in gh.calls), gh.calls


def test_apply_reads_an_explicit_empty_inventory_as_empty():
    gh = FakeGh({"api repos/owner/comic/rulesets": json.dumps([[]])})
    _writable(gh)
    assert any("created" in line for line in apply_repo_settings(spec(), gh))
    assert sum(c[:3] == ("api", "-X", "POST") for c, _ in gh.calls) == 1


def test_paged_items_distinguishes_unreadable_from_empty():
    from agentic_sdlc.onboard import Unreadable, _paged_items

    def answering(text):
        return FakeGh({"api x": text})

    assert _paged_items(answering("[[]]"), "x") == []
    assert _paged_items(answering("[]"), "x") == []
    assert _paged_items(answering(_pages("runners", [])), "x", "runners") == []
    for text in ("", "nope", "{}", '[{"a": 1}]', '[["a"]]', '[{"total_count": 1}]'):
        key = "runners" if text.startswith('[{"total') else None
        found = _paged_items(answering(text), "x", key)
        assert isinstance(found, Unreadable), text
        with pytest.raises(TypeError):
            bool(found)  # an unhandled Unreadable fails loudly, never reads as empty
    not_found = _paged_items(
        FakeGh({"api x": GhCommandError("gh: Not Found (HTTP 404)", http_status=404)}), "x"
    )
    assert isinstance(not_found, Unreadable) and not_found.not_found


@pytest.mark.parametrize("answer", [*_UNREADABLE_ANSWERS, pytest.param('"yes"', id="not-bool")])
def test_doctor_fails_closed_on_an_unreadable_platform_visibility(tmp_path, answer):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _healthy_gh()
    gh.answers["api repos/owner/agentic-sdlc --jq .private"] = answer
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))
    access = failed[PLATFORM_ACCESS]
    assert access.manual and "could not read whether owner/agentic-sdlc is public" in access.detail
    # an unknown platform may be private: its checkout token stays required
    assert "PLATFORM_READ_TOKEN" in failed["repo secrets set (by name)"].detail
    # only a proven-public platform skips both
    gh.answers["api repos/owner/agentic-sdlc --jq .private"] = "false\n"
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    assert report.ok and PLATFORM_ACCESS not in {c.name for c in report.checks}


# Codex 4239080209: with a merge queue, a CLASSIC required context must reach the merge group too.
def test_classic_required_contexts_must_also_report_in_the_merge_group():
    from agentic_sdlc.onboard import ProducedContexts, legacy_protection_problems

    produced = ProducedContexts(
        frozenset({"test", "security-scan"}), frozenset({"test"}), merge_group_declared=True
    )
    protection = {
        "required_status_checks": {
            "contexts": ["test", "security-scan"],
            "checks": [{"context": "test", "app_id": GITHUB_ACTIONS_INTEGRATION_ID}],
        }
    }
    assert legacy_protection_problems(protection, produced) == []
    assert legacy_protection_problems(protection, produced, merge_queue=False) == []
    queued = legacy_protection_problems(protection, produced, merge_queue=True)
    assert len(queued) == 1 and "'security-scan'" in queued[0] and "merge_group" in queued[0]
    unknown = legacy_protection_problems(protection, produced, merge_queue=None)
    assert (
        len(unknown) == 1 and "'security-scan'" in unknown[0] and "cannot be verified" in unknown[0]
    )
    both = ProducedContexts(frozenset({"test", "security-scan"}), produced.pull_request, True)
    assert legacy_protection_problems(protection, both, merge_queue=True) == []


def test_doctor_passes_the_effective_merge_queue_to_the_classic_check(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    legacy = "classic branch protection on the default branch fits Forge"
    protection = json.dumps(
        {
            "required_status_checks": {
                "contexts": ["test"],
                "checks": [{"context": "test", "app_id": GITHUB_ACTIONS_INTEGRATION_ID}],
            }
        }
    )
    gh = _healthy_gh()
    gh.answers["api repos/owner/comic/branches/main/protection"] = protection
    assert doctor(repo, "owner/comic", "owner/agentic-sdlc", gh).ok  # no queue: PR runs suffice
    gh.answers["api repos/owner/comic/rules/branches/main"] = _effective_rules(
        {"type": "merge_queue", "parameters": {}, "ruleset_source_type": "Organization"}
    )
    check = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))[legacy]
    assert "merge queue" in check.detail and "merge_group" in check.detail


# Codex 4239080214: commit-metadata and branch-name pattern rules are judged against what the
# pinned publisher pushes; a rule type doctor does not know is never silently compatible.
def _publisher():
    from agentic_sdlc.onboard import publisher_commit

    doc = yaml.safe_load((PLATFORM_WORKFLOWS / "reusable-implement.yml").read_text())
    return publisher_commit(doc)


def test_publisher_commit_is_read_from_the_pinned_reusable_implement():
    from agentic_sdlc.onboard import PublisherCommit, publisher_commit

    found = _publisher()
    assert isinstance(found, PublisherCommit)
    assert found.message.template == "Implement issue #${ISSUE_NUMBER}"
    assert "Implement issue #42" in found.message.samples
    assert found.author_email == found.committer_email
    assert found.author_email.constant
    assert found.author_email.example == "agentic-sdlc[bot]@users.noreply.github.com"
    assert found.branch.template == "agent/issue-${ISSUE_NUMBER}-${GITHUB_RUN_ID}"
    assert found.branch.example == "agent/issue-1-1"
    assert "agent/issue-42-9876543210" in found.branch.samples
    # anything doctor cannot read exactly is a reason, never a guess
    doc = yaml.safe_load((PLATFORM_WORKFLOWS / "reusable-implement.yml").read_text())
    pushing = next(
        step
        for job in doc["jobs"].values()
        for step in job.get("steps") or []
        if "git push" in str(step.get("run"))
    )
    run = pushing["run"]
    for broken in (
        run.replace('git commit -m "Implement', 'git commit -F msg.txt -m "Implement'),
        run.replace("Implement issue #", "Implement $(date) #"),
        run.replace("git config user.email", "git config user.mail"),
        run.replace("${ISSUE_NUMBER}-${GITHUB_RUN_ID}", "${ISSUE_NUMBER}-${UNKNOWN}"),
    ):
        pushing["run"] = broken
        assert isinstance(publisher_commit(doc), str), broken
    pushing["run"] = run
    pushing["env"] = {**pushing.get("env", {}), "GIT_AUTHOR_EMAIL": "x@y"}
    assert isinstance(publisher_commit(doc), str)
    assert isinstance(publisher_commit({}), str)


def _pattern_rule(kind: str, operator: str, pattern: str, negate: bool = False) -> dict:
    return {
        "type": kind,
        "parameters": {"operator": operator, "pattern": pattern, "negate": negate},
        "ruleset_source_type": "Organization",
        "ruleset_source": "owner",
    }


@pytest.mark.parametrize(
    ("kind", "operator", "pattern", "negate", "verdict"),
    [
        ("commit_message_pattern", "starts_with", "Implement issue #", False, "ok"),
        ("commit_message_pattern", "starts_with", "JIRA-", False, "rejects"),
        ("commit_message_pattern", "contains", "WIP", True, "ok"),
        ("commit_message_pattern", "contains", "issue", True, "rejects"),
        # a regex over a numbered message is never proven for every number (Codex 4239269925)
        ("commit_message_pattern", "regex", r"^Implement issue #\d+$", False, "todo"),
        # issue #2 is a real value outside it: a proven rejection
        ("commit_message_pattern", "regex", r"^Implement issue #(1|42|12345)$", False, "rejects"),
        # passes every sample, rejects issue #3: never PASS on samples alone
        ("commit_message_pattern", "regex", r"^Implement issue #(1|2|42|12345)$", False, "todo"),
        ("commit_message_pattern", "starts_with", "Implement issue #1", False, "rejects"),
        ("commit_message_pattern", "starts_with", "Implement issue #1", True, "rejects"),
        ("commit_message_pattern", "starts_with", "Implement issue #x", False, "rejects"),
        ("commit_message_pattern", "contains", "#4", False, "rejects"),  # issue #1 lacks it
        ("commit_message_pattern", "contains", "#", False, "ok"),
        ("commit_message_pattern", "contains", "ssue #", False, "ok"),
        ("commit_message_pattern", "contains", "Implement issue #4", True, "rejects"),
        ("commit_message_pattern", "contains", "#1", True, "rejects"),
        ("commit_message_pattern", "contains", "JIRA", True, "ok"),
        ("commit_message_pattern", "ends_with", "#", False, "rejects"),
        ("commit_message_pattern", "ends_with", "#1", True, "rejects"),
        ("commit_message_pattern", "ends_with", "x", True, "ok"),
        ("commit_message_pattern", "contains", "9", True, "todo"),  # no sample has a 9
        ("commit_message_pattern", "regex", r"^[A-Z]+-\d+: ", False, "rejects"),
        ("commit_message_pattern", "regex", r"(?i)^implement", False, "todo"),
        ("commit_message_pattern", "regex", r"^(?=Impl)", False, "todo"),
        ("commit_message_pattern", "regex", r"\bissue\b", False, "todo"),
        ("commit_message_pattern", "glob", "Implement*", False, "todo"),
        ("commit_author_email_pattern", "ends_with", "@users.noreply.github.com", False, "ok"),
        ("commit_author_email_pattern", "ends_with", "@example.com", False, "rejects"),
        ("committer_email_pattern", "regex", r"@corp\.example$", False, "rejects"),
        ("committer_email_pattern", "regex", r"^agentic-sdlc\[bot\]@", False, "ok"),
        ("branch_name_pattern", "starts_with", "agent/", False, "ok"),
        ("branch_name_pattern", "regex", r"^feature/", False, "rejects"),
        ("branch_name_pattern", "regex", r"^agent/issue-\d{1,3}-", False, "rejects"),
        ("tag_name_pattern", "starts_with", "v", False, "ok"),  # the publisher pushes no tags
    ],
)
def test_effective_metadata_pattern_rules_are_evaluated(kind, operator, pattern, negate, verdict):
    from agentic_sdlc.onboard import ProducedContexts, effective_rule_problems

    rules = [_pattern_rule(kind, operator, pattern, negate)]
    problems = effective_rule_problems(rules, ProducedContexts(), _publisher())
    if verdict == "ok":
        assert problems == []
    elif verdict == "rejects":
        assert len(problems) == 1 and "does not" in problems[0], problems
    else:
        assert len(problems) == 1 and "cannot evaluate" in problems[0], problems
    if kind != "tag_name_pattern":  # an unread publisher leaves every such rule a TODO
        unread = effective_rule_problems(rules, ProducedContexts(), "reusable-implement unread")
        assert len(unread) == 1 and "could not be read" in unread[0]


def test_unknown_effective_rule_types_are_never_silently_compatible():
    from agentic_sdlc.onboard import ProducedContexts, effective_rule_problems

    compatible = [
        {"type": t} for t in ("creation", "deletion", "non_fast_forward", "required_linear_history")
    ] + [{"type": "pull_request", "parameters": {"required_approving_review_count": 1}}]
    assert effective_rule_problems(compatible, ProducedContexts()) == []
    for kind in ("file_path_restriction", "max_file_size", "copilot_code_review", "", "brand_new"):
        problems = effective_rule_problems([{"type": kind}], ProducedContexts())
        assert len(problems) == 1 and "not one doctor understands" in problems[0], kind
    # a merge queue a workflow serves is understood (not reported as an unknown type)
    served = ProducedContexts(merge_group_declared=True)
    assert effective_rule_problems([{"type": "merge_queue", "parameters": {}}], served) == []


@pytest.mark.parametrize(
    ("rule", "ok"),
    [
        (_pattern_rule("commit_message_pattern", "starts_with", "Implement issue"), True),
        (_pattern_rule("commit_message_pattern", "starts_with", "JIRA-"), False),
        (_pattern_rule("commit_author_email_pattern", "ends_with", "@corp.example"), False),
        ({"type": "file_path_restriction", "parameters": {"restricted_file_paths": ["x"]}}, False),
    ],
)
def test_doctor_judges_effective_metadata_rules_against_the_pinned_publisher(tmp_path, rule, ok):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _healthy_gh()
    gh.answers["api repos/owner/comic/rules/branches/main"] = _effective_rules(rule)
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    assert report.ok is ok, report.render()
    if not ok:
        assert set(_failed(report)) == {
            "effective default-branch rules (org/enterprise rulesets too) fit Forge"
        }


# ---------------------------------------------------------------- Codex review 5480794212


def test_platform_refs_are_encoded_as_one_path_component():
    """Codex 4239194843: a ref with `/` or `%` must not split the REST path."""
    from agentic_sdlc.onboard import remote_workflow_files

    gh = FakeGh({"api repos/owner/agentic-sdlc/commits/feature%2Fonboarding": SHA + "\n"})
    assert resolve_platform_ref("owner/agentic-sdlc", "feature/onboarding", gh) == SHA
    assert gh.calls[0][0][1] == "repos/owner/agentic-sdlc/commits/feature%2Fonboarding"
    gh = FakeGh({"api repos/owner/agentic-sdlc/commits/50%25off": SHA + "\n"})
    assert resolve_platform_ref("owner/agentic-sdlc", "50%off", gh) == SHA
    # a branch resolved to its tree (Codex 4240170623) is one path component too
    gh = FakeGh({"api repos/owner/comic/contents/.github/workflows?ref=release%2F1.0": "[]"})
    assert remote_workflow_files(gh, "owner/comic", "release/1.0") == ({}, "")
    assert gh.calls[0][0][1] == "repos/owner/comic/commits/release%2F1.0"


@pytest.mark.parametrize(
    ("pattern", "branch", "expected"),
    [
        ("ma?n", "main", False),  # `?` is zero or one of the PRECEDING character
        ("ma?n", "man", True),
        ("ma?n", "mn", True),
        ("mai?n", "main", True),
        ("mai+n", "maiiin", True),
        ("mai+n", "man", False),
        ("[mx]ain", "main", True),
        ("[a-z]ain", "main", True),
        ("[0-9]ain", "main", False),
        ("*", "main", True),
        ("*", "a/b", False),
        ("**", "a/b", True),
        ("releases/**", "releases/1/2", True),
        ("releases/*", "releases/1/2", False),
        ("ma\\*n", "ma*n", True),
        ("ma\\*n", "main", False),
        ("feature!", "feature!", True),  # `!` is special only first
        # unsupported or ambiguous: fail closed (None)
        ("!main", "main", None),
        ("?main", "main", None),
        ("*?", "main", None),
        ("[mx]?ain", "main", None),
        ("[!m]ain", "main", None),
        ("[a-Z]ain", "main", None),
        ("[ab", "main", None),
        ("ma]n", "main", None),
        ("***", "main", None),
        ("ma\\n", "main", None),
    ],
)
def test_branch_filters_follow_githubs_cheat_sheet(pattern, branch, expected):
    """Codex 4239194847: GitHub's filter-pattern semantics, or None (not provably matching)."""
    from agentic_sdlc.onboard import _branch_glob_matches

    assert _branch_glob_matches(pattern, branch) is expected


def test_a_question_mark_filter_does_not_prove_a_workflow_reports_on_main():
    from agentic_sdlc.onboard import _reports_on_protected_pull_requests

    def doc(branches):
        return {"on": {"pull_request": {"branches": branches}}, "jobs": {}}

    assert not _reports_on_protected_pull_requests(doc(["ma?n"]), "main")
    assert not _reports_on_protected_pull_requests(doc(["[!x]ain"]), "main")
    assert _reports_on_protected_pull_requests(doc(["mai?n"]), "main")
    assert _reports_on_protected_pull_requests(doc(["[!x]ain", "ma*"]), "main")


NESTED_SHA = "f" * 40


def _plan_calling_nested(nested: str, *, with_: dict | None = None) -> str:
    """reusable-plan.yml with one more job that calls the platform's `nested.yml` pinned."""
    doc = yaml.safe_load((PLATFORM_WORKFLOWS / "reusable-plan.yml").read_text())
    job = {"uses": f"owner/agentic-sdlc/.github/workflows/{nested}@{NESTED_SHA}"}
    if with_:
        job["with"] = with_
    doc["jobs"]["nested"] = job
    return yaml.safe_dump(doc)


NESTED_CALLEE = (
    "on:\n  workflow_call:\n{inputs}jobs:\n  inner:\n    runs-on: ubuntu-latest\n"
    "    steps:\n      - uses: evil/action@" + "a" * 40 + "\n"
)


def _nested_gh(gh: FakeGh, nested: str | Exception, **kwargs) -> FakeGh:
    gh.answers[PLATFORM_CONTENTS + ".github/workflows/reusable-plan.yml"] = _blob(
        _plan_calling_nested("nested.yml", **kwargs).encode()
    )
    gh.answers[PLATFORM_CONTENTS + ".github/workflows/nested.yml"] = (
        nested if isinstance(nested, Exception) else _blob(nested.encode())
    )
    return gh


def test_actions_allowlist_covers_nested_platform_callees(tmp_path):
    """Codex 4239194850: an action used only by a nested pinned platform workflow must be
    admitted too; a nested callee that cannot be read is a manual TODO, never READY."""
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _nested_gh(_actions_policy_gh(SELECTED, ALLOWLIST), NESTED_CALLEE.format(inputs=""))
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    check = next(c for c in report.checks if c.name == ACTIONS_POLICY)
    assert not check.ok and not check.manual and "evil/action@" in check.detail
    assert not report.ok
    # allowed: READY again (the nested call itself is the consumer owner's own workflow)
    allow = {**ALLOWLIST, "patterns_allowed": [*ALLOWLIST["patterns_allowed"], "evil/*"]}
    gh = _nested_gh(_actions_policy_gh(SELECTED, allow), NESTED_CALLEE.format(inputs=""))
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    assert report.ok, report.render()
    # unreadable nested callee: fail closed
    gh = _nested_gh(_actions_policy_gh(SELECTED, allow), OnboardError("gh: boom (HTTP 500)"))
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    check = next(c for c in report.checks if c.name == ACTIONS_POLICY)
    assert not check.ok and check.manual and "nested.yml could not be read" in check.detail
    assert not report.ok


def test_callee_graph_is_cycle_safe_bounded_and_fails_closed():
    from agentic_sdlc.onboard import platform_callee_graph

    def calls(target: str) -> dict:
        return {"on": {"workflow_call": None}, "jobs": {"j": {"uses": target}}}

    plat = "owner/agentic-sdlc/.github/workflows"
    docs = {
        ("a.yml", NESTED_SHA): calls(f"{plat}/b.yml@{NESTED_SHA}"),
        ("b.yml", NESTED_SHA): calls(f"{plat}/a.yml@{NESTED_SHA}"),
    }
    loads: list[tuple[str, str]] = []

    def load(name: str, sha: str) -> object:
        loads.append((name, sha))
        return docs.get((name, sha))

    roots = {"root.yml": calls(f"{plat}/a.yml@{NESTED_SHA}")}
    graph = platform_callee_graph(roots, SHA, "owner/agentic-sdlc", load)
    assert set(graph.docs) == {("root.yml", SHA), *docs}
    assert any("cycle through a.yml" in why for why in graph.unfollowed)
    assert loads == [("a.yml", NESTED_SHA), ("b.yml", NESTED_SHA)]  # each read once
    # depth bound
    graph = platform_callee_graph(roots, SHA, "owner/agentic-sdlc", load, max_depth=2)
    assert any("deeper than 2" in why for why in graph.unfollowed)
    # a branch ref, another repository, a `./` call: not followed, fail closed
    for target in (f"{plat}/a.yml@main", "other/repo/.github/workflows/x.yml@" + SHA, "./x.yml"):
        graph = platform_callee_graph({"r.yml": calls(target)}, SHA, "owner/agentic-sdlc", load)
        assert graph.unfollowed and graph.uninspectable, target


def test_callee_graph_depth_counts_the_generated_caller():
    """Codex 4239269932: GitHub's ten-level nesting limit includes the generated top-level
    caller, so nine called workflows (root + 8 nested) fit and a tenth is reported."""
    from agentic_sdlc.onboard import platform_callee_graph

    plat = "owner/agentic-sdlc/.github/workflows"

    def chain(called: int) -> tuple[dict, dict]:
        # root.yml -> w1.yml -> ... -> w{called-1}.yml: `called` workflows below the caller
        def doc(nxt: str | None) -> dict:
            jobs = {"j": {"uses": f"{plat}/{nxt}@{NESTED_SHA}"}} if nxt else {"j": {}}
            return {"on": {"workflow_call": None}, "jobs": jobs}

        names = ["root.yml", *[f"w{i}.yml" for i in range(1, called)]]
        docs = {
            (n, NESTED_SHA): doc(names[i + 1] if i + 1 < len(names) else None)
            for i, n in enumerate(names)
            if i
        }
        root = doc(names[1] if len(names) > 1 else None)
        return {"root.yml": root}, docs

    roots, docs = chain(9)
    graph = platform_callee_graph(roots, SHA, "owner/agentic-sdlc", lambda n, s: docs.get((n, s)))
    assert not graph.unfollowed and not graph.uninspectable
    assert len(graph.docs) == 9
    roots, docs = chain(10)
    graph = platform_callee_graph(roots, SHA, "owner/agentic-sdlc", lambda n, s: docs.get((n, s)))
    assert any("deeper than 10" in why for why in graph.unfollowed)


def test_nested_reusable_calls_must_match_their_callee_contract(tmp_path):
    """Codex 4239194854: a nested pinned call that omits a required input is rejected by
    GitHub before its jobs start; doctor must not report READY."""
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    required = "    inputs:\n      mode:\n        type: string\n        required: true\n"
    gh = _nested_gh(_healthy_gh(), NESTED_CALLEE.format(inputs=required))
    gh.answers["api repos/owner/comic/actions/permissions"] = json.dumps(
        {"enabled": True, "allowed_actions": "all"}
    )
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))
    detail = failed[CONTRACT].detail
    assert "reusable-plan.yml@" in detail and "nested.yml" in detail and "'mode'" in detail
    # passing it satisfies the nested contract
    gh = _nested_gh(_healthy_gh(), NESTED_CALLEE.format(inputs=required), with_={"mode": "x"})
    gh.answers["api repos/owner/comic/actions/permissions"] = json.dumps(
        {"enabled": True, "allowed_actions": "all"}
    )
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    assert report.ok, report.render()


@pytest.mark.parametrize(
    ("rule", "needle"),
    [
        (_org_rule("creation"), "restricts creating agent/issue-1-1"),
        (_org_rule("update"), "restricts updates to agent/issue-1-1"),
        (_org_rule("required_signatures"), "'required_signatures' applies to agent/issue-1-1"),
        (
            _org_rule("required_status_checks", required_status_checks=[{"context": "x"}]),
            "'required_status_checks' applies",
        ),
        (
            _pattern_rule("commit_message_pattern", "starts_with", "JIRA-"),
            "the publisher's commit message",
        ),
    ],
)
def test_doctor_reads_the_rules_on_the_publishers_agent_branches(tmp_path, rule, needle):
    """Codex 4239194838: a ruleset targeting only agent/** is invisible in the default branch's
    rules but rejects the publisher's push."""
    from agentic_sdlc.onboard import PUBLISHER_BRANCH_RULES_CHECK

    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _healthy_gh()
    gh.answers[AGENT_BRANCH_RULES] = json.dumps([[rule]])
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    failed = _failed(report)
    assert set(failed) == {PUBLISHER_BRANCH_RULES_CHECK}, report.render()
    assert needle in failed[PUBLISHER_BRANCH_RULES_CHECK].detail
    paths = [c[1] for c, _ in gh.calls if "/rules/branches/agent" in c[1]]
    assert "repos/owner/comic/rules/branches/agent%2Fissue-1-1" in paths  # one encoded component


def test_publisher_branch_rules_compatible_or_unreadable(tmp_path):
    from agentic_sdlc.onboard import PUBLISHER_BRANCH_RULES_CHECK

    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _healthy_gh()
    compatible = [
        _org_rule("deletion"),
        _org_rule("non_fast_forward"),
        _org_rule(
            "required_status_checks",
            required_status_checks=[{"context": "x"}],
            do_not_enforce_on_create=True,
        ),
        _pattern_rule("branch_name_pattern", "starts_with", "agent/"),
    ]
    gh.answers[AGENT_BRANCH_RULES] = json.dumps([compatible])
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    assert report.ok, report.render()
    gh = _healthy_gh()
    gh.answers[AGENT_BRANCH_RULES] = OnboardError("gh: Forbidden (HTTP 403)")
    check = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))[
        PUBLISHER_BRANCH_RULES_CHECK
    ]
    assert check.manual and "could not read the rules that apply to agent/issue-1-1" in (
        check.detail
    )


# ---------------------------------------------------------------- Codex 4239269925
# The publisher's branch is agent/issue-<N>-<RUN> for EVERY issue and run: the effective rules of
# one sample branch speak for all of them only when no ruleset targets some and not others.
@pytest.mark.parametrize(
    ("pattern", "scope"),
    [
        ("~ALL", True),
        ("~DEFAULT_BRANCH", False),
        ("refs/heads/agent/**", True),
        ("refs/heads/**", None),  # Ruby FNM_PATHNAME reads a bare `**` as `*`: unprovable
        ("refs/heads/agent/*", True),
        ("refs/heads/agent/issue-*", True),
        ("refs/heads/agent/issue-*-*", True),
        ("refs/heads/**/*", True),
        ("refs/heads/*", False),
        ("refs/heads/main", False),
        ("refs/heads/release-1.*", False),
        ("refs/heads/feature/**/*", False),
        ("refs/heads/agent/issue-1*", None),
        ("refs/heads/agent/issue-2-*", None),
        ("refs/heads/agent/issue-*-1", None),
        ("refs/heads/agent/issue-42-9876543210", None),
        ("refs/heads/agent/issue-x*", False),
        ("refs/heads/agent/issue-?-*", None),
        ("refs/heads/agent/issue-[0-9]*", None),
        ("refs/heads/Agent/*", None),  # case sensitivity unknown
        ("agent/*", None),  # not a full ref: do not guess
    ],
)
def test_ruleset_ref_patterns_are_judged_for_every_agent_branch(pattern, scope):
    from agentic_sdlc.onboard import PublisherValue, _ref_pattern_scope

    ref = PublisherValue(("refs/heads/agent/issue-", "-", ""), ("ISSUE_NUMBER", "GITHUB_RUN_ID"))
    assert _ref_pattern_scope(pattern, ref, "main") is scope


def _scoped_ruleset(include: list[str], exclude: list[str] | None = None, **extra) -> dict:
    return {
        "id": 77,
        "name": "org agents",
        "source_type": "Organization",
        "source": "owner",
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": include, "exclude": exclude or []}},
        **extra,
    }


@pytest.mark.parametrize(
    ("ruleset", "ok"),
    [
        (_scoped_ruleset(["refs/heads/agent/issue-2*"]), False),  # issue-1-1 misses it
        (_scoped_ruleset(["~ALL"], ["refs/heads/agent/issue-7-*"]), False),
        (_scoped_ruleset(["refs/heads/agent/issue-*"]), True),  # every agent branch alike
        (_scoped_ruleset(["refs/heads/release/*"]), True),  # none of them
        (_scoped_ruleset(["~ALL"], ["refs/heads/agent/*"]), True),
        (_scoped_ruleset(["refs/heads/agent/issue-2*"], enforcement="disabled"), True),
        (_scoped_ruleset(["refs/heads/agent/issue-2*"], target="tag"), True),
    ],
)
def test_doctor_requires_rulesets_to_treat_every_agent_branch_alike(tmp_path, ruleset, ok):
    from agentic_sdlc.onboard import PUBLISHER_BRANCH_RULES_CHECK

    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _healthy_gh()
    summary = {k: ruleset[k] for k in ("id", "name", "source_type", "source", "target")}
    summary["enforcement"] = ruleset["enforcement"]
    gh.answers = {
        "api repos/owner/comic/rulesets/77": json.dumps(ruleset),
        "api repos/owner/comic/rulesets?includes_parents=true": json.dumps(
            [[{"id": 11, "name": RULESET_NAME, "enforcement": "active"}, summary]]
        ),
        **gh.answers,
    }
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    if ok:
        assert report.ok, report.render()
    else:
        check = _failed(report)[PUBLISHER_BRANCH_RULES_CHECK]
        assert check.manual and "'org agents'" in check.detail
        assert "agent/issue-${ISSUE_NUMBER}-${GITHUB_RUN_ID}" in check.detail
        assert set(_failed(report)) == {PUBLISHER_BRANCH_RULES_CHECK}


def test_doctor_fails_closed_when_agent_branch_rulesets_are_unreadable(tmp_path):
    from agentic_sdlc.onboard import PUBLISHER_BRANCH_RULES_CHECK

    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _healthy_gh()
    gh.answers = {
        "api repos/owner/comic/rulesets?includes_parents=true": OnboardError("gh: boom (HTTP 500)"),
        **gh.answers,
    }
    check = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))[
        PUBLISHER_BRANCH_RULES_CHECK
    ]
    assert check.manual and "could not list the rulesets" in check.detail
    gh = _healthy_gh()
    gh.answers = {
        "api repos/owner/comic/rulesets/77": OnboardError("gh: boom (HTTP 500)"),
        "api repos/owner/comic/rulesets?includes_parents=true": json.dumps(
            [[{"id": 11, "name": RULESET_NAME, "enforcement": "active"}, {"id": 77, "name": "x"}]]
        ),
        **gh.answers,
    }
    check = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))[
        PUBLISHER_BRANCH_RULES_CHECK
    ]
    assert check.manual and "could not be read" in check.detail


# ---------------------------------------------------------------- Codex 4239269950
# Only a definitive answer (200 with a permission, or 404) may make the labeler unauthorized; an
# API failure is retried and then FAILS the job instead of silently consuming the label event.
#: The GraphQL answer for an issue whose body was never edited (Codex 4241196649).
NEVER_EDITED = json.dumps(
    {"data": {"repository": {"issue": {"lastEditedAt": None, "userContentEdits": {"nodes": []}}}}}
)
#: The time a label event of a fixture without one carries (the issue is never edited after it).
EVENT_AT = "2026-10-01T00:00:00Z"


def _epoch(stamp: str) -> int:
    from datetime import datetime

    return int(datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp())


def _gh_events_prelude(events_script: str, edits_script: str | None = None) -> str:
    """A fake `gh` prelude answering the label-events read with `events_script`'s pages, each
    filtered through the step's own `--jq` program as `gh api --paginate --jq` does (an event
    without a `created_at` gets EVENT_AT), and the GraphQL edit-time read with `edits_script`
    (default: never edited)."""
    edits = edits_script or f"printf '%s' '{NEVER_EDITED}'"
    stamp = (
        'if type == "array" then map(if type == "object" and (has("created_at") | not) '
        f'then . + {{created_at: "{EVENT_AT}"}} else . end) else . end'
    )
    return (
        'jqexpr=""; prev=""\n'
        'for a in "$@"; do [ "$prev" = "--jq" ] && jqexpr="$a"; prev="$a"; done\n'
        'if [ "${1:-}" = api ] && [ "${2:-}" = graphql ]; then\n'
        f"  {edits}\n"
        "  exit $?\n"
        "fi\n"
        'for a in "$@"; do case "$a" in */events*)\n'
        f'  out="$( {events_script} )" || exit $?\n'
        f"  out=\"$(printf '%s\\n' \"$out\" | jq -c '{stamp}')\" || exit $?\n"
        '  [ -z "$jqexpr" ] && { printf \'%s\\n\' "$out"; exit 0; }\n'
        '  printf \'%s\\n\' "$out" | jq -c "$jqexpr"; exit $? ;;\n'
        "esac; done\n"
    )


def _run_authorize(tmp_path: Path, script: str, workflow: str = "agent-auto-plan.yml"):
    import shutil
    import subprocess

    if not (shutil.which("bash") and shutil.which("jq")):
        pytest.skip("bash and jq are needed to run the generated authorization step")
    doc = yaml.safe_load(render_onboarding(spec())[f".github/workflows/{workflow}"])
    step = next(
        s
        for job in doc["jobs"].values()
        for s in job.get("steps") or []
        if s.get("id") == "authorize"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    label = step["env"]["APPROVAL_LABEL"]
    required = step["env"]["REQUIRED_LABELS"]
    applied = json.dumps(
        [
            {"event": "labeled", "label": {"name": name}, "actor": {"login": "alice"}}
            for name in json.loads(required)
        ]
    )
    prelude = _gh_events_prelude(f"printf '%s' '{applied}'")
    (bin_dir / "gh").write_text("#!/usr/bin/env bash\n" + prelude + script)
    (bin_dir / "sleep").write_text("#!/usr/bin/env bash\nexit 0\n")  # no real backoff in tests
    for tool in ("gh", "sleep"):
        (bin_dir / tool).chmod(0o755)
    output = tmp_path / "output"
    output.write_text("")
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "SENDER": "alice",
        "ACTOR": "alice",
        "ISSUE_NUMBER": "7",
        "APPROVAL_LABEL": label,
        "REQUIRED_LABELS": required,
        "GITHUB_REPOSITORY": "owner/comic",
        "GITHUB_OUTPUT": str(output),
        "GH_COUNT": str(tmp_path / "count"),
    }
    done = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return done, output.read_text()


COUNT_CALLS = 'n=$(( $(cat "$GH_COUNT" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$GH_COUNT"\n'


@pytest.mark.parametrize("workflow", ["agent-auto-plan.yml", "agent-auto-implement.yml"])
@pytest.mark.parametrize(
    ("script", "code", "authorized"),
    [
        ('echo \'{"permission": "write"}\'\n', 0, "true"),
        ('echo \'{"permission": "admin"}\'\n', 0, "true"),
        ('echo \'{"permission": "read"}\'\n', 0, "false"),
        ('echo \'{"permission": "none"}\'\n', 0, "false"),
        ('echo "gh: Not Found (HTTP 404)" >&2; exit 1\n', 0, "false"),
        # transient failures and malformed answers: never "unauthorized"
        ('echo "gh: API rate limit exceeded (HTTP 403)" >&2; exit 1\n', 1, None),
        ('echo "gh: Server Error (HTTP 502)" >&2; exit 1\n', 1, None),
        ('echo "connection reset" >&2; exit 1\n', 1, None),
        ("echo '{}'\n", 1, None),
        ("echo 'not json'\n", 1, None),
        # a transient failure that clears on retry
        (
            COUNT_CALLS
            + 'if [ "$n" -lt 3 ]; then echo "gh: (HTTP 500)" >&2; exit 1; fi\n'
            + 'echo \'{"permission": "write"}\'\n',
            0,
            "true",
        ),
    ],
)
def test_labeler_authorization_fails_on_unreadable_permissions(
    tmp_path, workflow, script, code, authorized
):
    done, output = _run_authorize(tmp_path, script, workflow)
    assert done.returncode == code, (done.stdout, done.stderr)
    if authorized is None:
        assert "authorized=" not in output  # nothing is decided: the job fails
        assert "::error::" in done.stdout
    else:
        at = _epoch(EVENT_AT)
        expected = (
            f"authorized={authorized}\napprover=alice\napproved_at={at}\napproved_latest_at={at}"
        )
        assert output.strip() == expected


# ---------------------------------------------------------------- Codex 4239269954
# Work requests, labels and leases are all issues: a repository with Issues disabled is never
# READY; an answer that does not say they are enabled is a manual TODO.
@pytest.mark.parametrize(
    ("has_issues", "manual"),
    [(False, False), (None, True), ("yes", True)],
)
def test_doctor_requires_github_issues_enabled(tmp_path, has_issues, manual):
    from agentic_sdlc.onboard import ISSUES_ENABLED_CHECK

    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _healthy_gh()
    info = json.loads(gh.answers["api repos/owner/comic"])
    info.pop("has_issues")
    if has_issues is not None:
        info["has_issues"] = has_issues
    gh.answers["api repos/owner/comic"] = json.dumps(info)
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    failed = _failed(report)
    assert set(failed) == {ISSUES_ENABLED_CHECK}, report.render()
    assert failed[ISSUES_ENABLED_CHECK].manual is manual
    assert "Issues" in failed[ISSUES_ENABLED_CHECK].detail
    assert not report.ok


# ---------------------------------------------------------------- review regressions (PR 139, 7)
# Codex review 5481000688.


# Codex 4239397967: a git command is checked against every ref it NAMES as a destination, not
# only the checked-out branch.
@pytest.mark.parametrize(
    "command",
    [
        "git push origin HEAD:forge/issue-7",
        "git push origin :forge/issue-7",
        "git push origin forge/issue-7",
        "git push --delete origin forge/issue-7",
        "git push -d origin forge/issue-7",
        "git push origin +main:refs/heads/forge/issue-7",
        "git push -f origin main:forge/issue-7",
        "git push -o ci.skip origin HEAD:forge/issue-7",
        "git push --force-with-lease=forge/issue-7 origin HEAD:forge/issue-7",
        "git branch -f forge/issue-7 HEAD",
        "git branch --force forge/issue-7 main",
        "git branch -D forge/issue-7",
        "git branch --delete forge/issue-7",
        "git branch -m forge/issue-7 renamed",
        "git branch -c main forge/issue-7",
        "git branch forge/issue-7",
        "git branch -u origin/main forge/issue-7",
        "git update-ref refs/heads/forge/issue-7 HEAD",
        "git update-ref -d refs/heads/forge/issue-7",
        "git update-ref -m msg refs/heads/forge/issue-7 HEAD",
        "git symbolic-ref refs/heads/forge/issue-7 refs/heads/main",
        "git fetch origin main:forge/issue-7",
        "git pull origin main:forge/issue-7",
        "git worktree add -b forge/issue-7 ../wt7",
        "git worktree add ../issue-7",
        "git stash branch forge/issue-7",
        "git push-wip",  # a git alias: followed
        "python3 -c \"import subprocess as s; s.run(['git','push','o','HEAD:forge/issue-7'])\"",
        "git commit -m x && git push origin HEAD:forge/issue-7",
    ],
)
def test_commit_guard_checks_the_refs_a_command_names(tmp_path, command):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    repo = _branch_flow_repo(tmp_path)
    _git(repo, "config", "alias.push-wip", "push origin HEAD:forge/issue-7")
    code, message = _flow_code(guard, command, repo)
    assert code == 2 and "issue #7" in message, command
    assert _flow_code(guard, command, repo, leased=(7,))[0] == 0, command


@pytest.mark.parametrize(
    ("command", "config"),
    [
        ("git push --all", ()),
        ("git push --all origin", ()),
        ("git push --branches origin", ()),
        ("git push --mirror origin", ()),
        ("git push --prune origin main", ()),
        ("git push origin 'refs/heads/*:refs/heads/*'", ()),
        ("git push origin :", ()),
        ("git push origin main:HEAD", ()),
        ('git push origin "$B"', ()),
        ("git push origin HEAD:forge/issue-$N", ()),
        ("git push --weird-option origin main", ()),
        ("git push", (("remote.origin.push", "refs/heads/main:refs/heads/forge/issue-7"),)),
        ("git push origin", (("remote.origin.push", "HEAD:refs/heads/forge/issue-7"),)),
        ("git push", (("push.default", "upstream"),)),
        ("git push", (("push.default", "matching"),)),
        ("git -c push.default=matching push", ()),
        ("git rebase --update-refs main", ()),
        ("git rebase main", (("rebase.updateRefs", "true"),)),
        ("git rebase -x 'git push origin HEAD:forge/issue-7' main", ()),
        ("git update-ref --stdin < refs.txt", ()),
        ("git shellpush", (("alias.shellpush", "!git push origin HEAD:forge/issue-7"),)),
        ("git no-such-subcommand", ()),
        ("git-push origin --all", ()),
        ("xargs git push origin < refs.txt", ()),
        ("echo HEAD:x | xargs git push origin", ()),
        ("eval git push origin HEAD:x", ()),
        ("git filter-branch --all", ()),
        ("git fetch --refmap='+refs/heads/*:refs/heads/*' origin main", ()),
        ("git branch -D 'forge/*'", ()),
        ("git bisect run make", ()),
        ("git submodule foreach git push", ()),
    ],
)
def test_commit_guard_blocks_unknowable_destinations_on_any_branch(tmp_path, command, config):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    repo = _branch_flow_repo(tmp_path)
    for key, value in config:
        _git(repo, "config", key, value)
    code, message = _flow_code(guard, command, repo, leased=(7, 9))  # every lease held
    assert code == 2 and "cannot be known before it runs" in message, command


def test_commit_guard_named_refs_leave_ordinary_commands_alone(tmp_path):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    repo = _branch_flow_repo(tmp_path)
    _git(repo, "config", "alias.pm", "push origin main")
    for allowed in (
        "git push",
        "git push origin",
        "git push origin main",
        "git push -u origin HEAD",
        "git push origin HEAD",
        "git push --tags origin",
        "git push origin --force-with-lease main",
        "git branch feature",
        "git branch -d feature",
        "git branch -a",
        "git branch --list 'forge/*'",
        "git fetch origin main:main",
        "git rebase main",
        "git rebase --no-update-refs main",
        "git pm",
        "git commit -m x && git push",
        "xargs git log",
        "find . -name '*.py' -exec git log -- {} ;",
        "git update-ref HEAD main",
        "git worktree add --detach ../wt-x",
    ):
        assert _flow_code(guard, allowed, repo)[0] == 0, allowed
    _git(repo, "config", "push.default", "current")
    assert _flow_code(guard, "git push", repo)[0] == 0


def test_commit_guard_named_refs_checked_from_an_issue_branch_too(tmp_path):
    """On a leased issue branch, a push into ANOTHER issue's branch needs that issue's lease."""
    guard = _guard_module(tmp_path, "forge_commit_guard")
    repo = _branch_flow_repo(tmp_path)
    wt = tmp_path / "wt"  # on forge/issue-9
    assert _flow_code(guard, "git push origin HEAD:forge/issue-7", wt, leased=(9,))[0] == 2
    assert _flow_code(guard, "git push origin HEAD:forge/issue-7", wt, leased=(7, 9))[0] == 0
    assert _flow_code(guard, "git push origin HEAD", wt, leased=(9,))[0] == 0
    assert repo.is_dir()


# Codex 4239397963 / 4239397966 / 4239397961: hosted runner labels.
@pytest.mark.parametrize("label", ["ubuntu-26.04", "ubuntu-26.04-arm"])
def test_ubuntu_26_04_labels_are_standard_hosted_linux(label):
    from agentic_sdlc.onboard import _linux_labels, _runner_available, _standard_hosted

    assert _linux_labels({label}) and _standard_hosted({label})
    assert _runner_available({label}, [])
    assert spec(runs_on=(label,)).runs_on == (label,)


@pytest.mark.parametrize(
    "overrides",
    [
        {"runs_on": ("ubuntu-slim",)},
        {"runs_on": ("ubuntu-latest",), "ci_runs_on": ("ubuntu-slim",)},
    ],
)
def test_onboard_rejects_ubuntu_slim_for_forge_workloads(overrides):
    with pytest.raises(OnboardError, match="15 minutes"):
        spec(**overrides)


def test_doctor_fails_ubuntu_slim_workloads(tmp_path):
    from agentic_sdlc.onboard import _runner_available

    assert not _runner_available({"ubuntu-slim"}, [])
    repo = _repo(tmp_path)
    write_onboarding(repo, spec(runs_on=("ubuntu-latest",)))
    ci = repo / ".github/workflows/ci.yml"
    doc = yaml.safe_load(ci.read_text())
    doc["jobs"]["test"]["runs-on"] = "ubuntu-slim"
    ci.write_text(yaml.safe_dump(doc, sort_keys=False))
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", _healthy_gh()))
    check = failed["ci.yml test job runs on an available runner"]
    assert not check.manual and "15 minutes" in check.detail
    # the plan/implement callers, too
    repo = _repo(tmp_path / "second")
    write_onboarding(repo, spec(runs_on=("ubuntu-latest",)))
    plan = repo / ".github/workflows/agent-plan.yml"
    plan.write_text(plan.read_text().replace('["ubuntu-latest"]', '["ubuntu-slim"]'))
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", _healthy_gh()))
    assert "15 minutes" in failed["runners allow the workflows' job timeouts"].detail


@pytest.mark.parametrize("label", ["macos-15-xlarge", "macos-latest-large", "macos-14-large"])
def test_doctor_never_auto_passes_a_larger_macos_runner(tmp_path, label):
    from agentic_sdlc.onboard import _github_hosted, _runner_available, _standard_hosted

    assert _github_hosted({label}) and not _standard_hosted({label})
    assert not _runner_available({label}, [])
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    ci = repo / ".github/workflows/ci.yml"
    doc = yaml.safe_load(ci.read_text())
    doc["jobs"]["test"]["runs-on"] = label
    ci.write_text(yaml.safe_dump(doc, sort_keys=False))
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", _healthy_gh())
    check = _failed(report)["ci.yml test job runs on an available runner"]
    assert check.manual and "larger GitHub-hosted runner" in check.detail
    assert not report.ok
    # proven only by a runner of the repository that carries the label
    gh = _healthy_gh()
    gh.answers["api repos/owner/comic/actions/runners"] = _pages(
        "runners",
        [
            {"status": "online", "labels": [{"name": n} for n in ("self-hosted", "linux", "x64")]},
            {"status": "online", "os": "macOS", "labels": [{"name": label}]},
        ],
    )
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    assert report.ok, report.render()


# Codex 4239397981: the default policy forbids the media AGENTS.md says it rejects.
def test_default_policy_rejects_media_case_insensitively(tmp_path):
    from agentic_sdlc.policy import evaluate_diff

    files = render_onboarding(spec(forbidden_paths=()))
    path = tmp_path / "agentic-sdlc.toml"
    path.write_text(files["agentic-sdlc.toml"])
    policy = load_policy(path)
    for media in (
        "logo.png",
        "assets/img/photo.JPG",
        "docs/shot.jpeg",
        "a/b/anim.gif",
        "x.webp",
        "favicon.ico",
        "clip.mp4",
        "media/Movie.MOV",
        "song.mp3",
        "voice.wav",
        "track.flac",
        "sound.ogg",
        "rec.m4a",
    ):
        decision = evaluate_diff((media,), 1, 0, policy)
        assert any("forbidden paths changed" in r for r in decision.reasons), media
    for allowed in ("icons/logo.svg", "src/app.py", "README.md", "src/png.py"):
        decision = evaluate_diff((allowed,), 1, 0, policy)
        assert not any("forbidden" in r for r in decision.reasons), allowed
    agents = files["AGENTS.md"]
    assert "image, video and audio files" in agents and "SVG is source" in agents


# Codex 4239397976: a shared callee reached shallow first is walked again along a deeper path.
@pytest.mark.parametrize("direct_first", [True, False])
def test_callee_graph_checks_every_path_of_a_shared_callee(direct_first):
    from agentic_sdlc.onboard import platform_callee_graph

    plat = "owner/agentic-sdlc/.github/workflows"

    def doc(*targets: str) -> dict:
        jobs = {f"j{i}": {"uses": f"{plat}/{t}@{NESTED_SHA}"} for i, t in enumerate(targets)}
        return {"on": {"workflow_call": None}, "jobs": jobs or {"leaf": {"runs-on": "x"}}}

    # caller (1) -> root (2) -> w1 (3) ... w7 (9) -> x (10) -> y (11): one edge too deep;
    # root also calls x directly (x at 3, y at 4: fine)
    chain = [f"w{i}.yml" for i in range(1, 8)]
    docs = {(n, NESTED_SHA): doc(nxt) for n, nxt in zip(chain, [*chain[1:], "x.yml"], strict=True)}
    docs[("x.yml", NESTED_SHA)] = doc("y.yml")
    docs[("y.yml", NESTED_SHA)] = doc()
    loads: list[tuple[str, str]] = []

    def load(name: str, sha: str) -> object:
        loads.append((name, sha))
        return docs.get((name, sha))

    order = ("x.yml", "w1.yml") if direct_first else ("w1.yml", "x.yml")
    graph = platform_callee_graph({"root.yml": doc(*order)}, SHA, "owner/agentic-sdlc", load)
    assert any("x.yml" in why and "deeper than 10" in why for why in graph.unfollowed), (
        graph.unfollowed
    )
    assert len(loads) == len(set(loads))  # each document loaded once
    # nine levels along every path: nothing reported
    docs[("w7.yml", NESTED_SHA)] = doc("y.yml")
    loads.clear()
    graph = platform_callee_graph({"root.yml": doc(*order)}, SHA, "owner/agentic-sdlc", load)
    assert not graph.unfollowed and not graph.uninspectable


# Codex 4239397986: at most 50 unique reusable workflows per top-level caller, nested included.
def test_reusable_workflow_count_is_limited_per_top_level_caller():
    from agentic_sdlc.onboard import platform_callee_graph, reusable_workflow_limit_problems

    plat = "owner/agentic-sdlc/.github/workflows"

    def doc(*targets: str) -> dict:
        jobs = {f"j{i}": {"uses": f"{plat}/{t}@{NESTED_SHA}"} for i, t in enumerate(targets)}
        return {"on": {"workflow_call": None}, "jobs": jobs or {"leaf": {"runs-on": "x"}}}

    def problems(width: int, roots: dict) -> list[str]:
        leaves = [f"c{i}.yml" for i in range(width)]
        docs = {(n, NESTED_SHA): doc() for n in leaves}
        callee_docs = {name: doc(*leaves) if name == "a.yml" else doc() for name in roots}
        graph = platform_callee_graph(
            callee_docs, SHA, "owner/agentic-sdlc", lambda n, s: docs.get((n, s))
        )
        caller_docs, calls = [], []
        for caller, callees in roots.items():
            jobs = {f"k{i}": {"uses": f"{plat}/{c}@{SHA}"} for i, c in enumerate(callees)}
            caller_docs.append((caller, {"jobs": jobs}))
            calls += [(f"{caller}:k{i}", jobs[f"k{i}"], c) for i, c in enumerate(callees)]
        return reusable_workflow_limit_problems(caller_docs, calls, graph, SHA)

    # a.yml + 49 nested = 50: allowed; + 50 nested = 51: rejected
    assert problems(49, {"a.yml": ["a.yml"]}) == []
    found = problems(50, {"a.yml": ["a.yml"]})
    assert len(found) == 1 and "51 unique reusable workflows" in found[0]
    # counted per caller: one caller reaching a.yml's tree, another only b.yml
    found = problems(50, {"a.yml": ["a.yml"], "b.yml": ["b.yml"]})
    assert len(found) == 1 and found[0].startswith("a.yml ")


# Codex 4239397979: classic branch-protection patterns over the publisher's agent branches.
def _bpr(pattern: str, **flags) -> dict:
    return {"pattern": pattern, **flags}


@pytest.mark.parametrize(
    ("rule", "needle"),
    [
        (
            _bpr("agent/**", restrictsPushes=True, pushAllowances={"nodes": []}),
            "allowlist without the pushing actor github-actions[bot]",
        ),
        (_bpr("agent/**", blocksCreations=True, restrictsPushes=True), "could not be read"),
        (_bpr("agent/*", requiresCommitSignatures=True), "signed commits"),
        (_bpr("agent/issue-*", requiresStatusChecks=True), "status checks"),
        (_bpr("**/issue-*", requiresApprovingReviews=True), "pull-request reviews"),
        (_bpr("agent/**", lockBranch=True), "locked branch"),
        (_bpr("agent/issue-1*", restrictsPushes=True), "matches some of the publisher"),
        (_bpr("agent/issue-[0-9]*", requiresCommitSignatures=True), "cannot be evaluated"),
    ],
)
def test_doctor_reads_classic_protection_patterns_on_agent_branches(tmp_path, rule, needle):
    from agentic_sdlc.onboard import PUBLISHER_BRANCH_RULES_CHECK

    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _healthy_gh()
    gh.answers[CLASSIC_RULES] = _classic_rules(rule)
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    failed = _failed(report)
    assert set(failed) == {PUBLISHER_BRANCH_RULES_CHECK}, report.render()
    assert needle in failed[PUBLISHER_BRANCH_RULES_CHECK].detail
    assert "classic branch protection" in failed[PUBLISHER_BRANCH_RULES_CHECK].detail


def test_classic_protection_patterns_that_spare_agent_branches_pass(tmp_path):
    from agentic_sdlc.onboard import PUBLISHER_BRANCH_RULES_CHECK

    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    pusher = {"actor": {"__typename": "App", "slug": "github-actions"}}
    for rules in (
        [_bpr("main", requiresStatusChecks=True, restrictsPushes=True)],
        [_bpr("*", requiresCommitSignatures=True)],  # `*` does not cross `/`
        [_bpr("release/**", lockBranch=True)],
        [_bpr("agent/**", restrictsPushes=True, pushAllowances={"nodes": [pusher]})],
        [_bpr("agent/**", allowsForcePushes=False, requiresLinearHistory=True)],
    ):
        gh = _healthy_gh()
        gh.answers[CLASSIC_RULES] = _classic_rules(*rules)
        report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
        assert report.ok, (rules, report.render())
    for answer, needle in (
        (OnboardError("gh: GraphQL: Resource not accessible (HTTP 403)"), "could not read"),
        (json.dumps({"errors": [{"message": "nope"}]}), "could not read"),
        (_classic_rules(more=True), "more than 100"),
    ):
        gh = _healthy_gh()
        gh.answers[CLASSIC_RULES] = answer
        check = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))[
            PUBLISHER_BRANCH_RULES_CHECK
        ]
        assert check.manual and needle in check.detail


@pytest.mark.parametrize("junk", ["a rule", None, 7, ["nested"]])
def test_effective_rules_with_a_malformed_item_are_unreadable_not_partial(tmp_path, junk):
    """--slurp pages of rules must hold only rule objects: an item of another shape makes the
    rules unreadable (a manual TODO), never a shorter list read as complete (Codex review
    4239510547, applied to every --slurp consumer)."""
    from agentic_sdlc.onboard import EFFECTIVE_RULES_CHECK, PUBLISHER_BRANCH_RULES_CHECK

    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _healthy_gh()
    own = json.loads(_effective_rules())
    gh.answers["api repos/owner/comic/rules/branches/main"] = json.dumps([[*own[0], junk]])
    check = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))[EFFECTIVE_RULES_CHECK]
    assert check.manual and "could not read the rules that apply to main" in check.detail
    gh = _healthy_gh()
    gh.answers[AGENT_BRANCH_RULES] = json.dumps([[junk]])
    check = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))[
        PUBLISHER_BRANCH_RULES_CHECK
    ]
    assert check.manual and "could not read the rules that apply to agent/issue-1-1" in (
        check.detail
    )


# ---------------------------------------------------------------- Codex 4239510543
# The pinned publisher checks out with github.token (persisted by actions/checkout) and pushes
# with it: the push is made by the GitHub Actions app, not the Publisher App, whose token only
# opens the pull request. A push allowlist must admit the actor that actually pushes.
def _implement_doc() -> dict:
    return yaml.safe_load((PLATFORM_WORKFLOWS / "reusable-implement.yml").read_text())


def _publishing_steps(doc: dict) -> list:
    return next(
        job["steps"]
        for job in doc["jobs"].values()
        if any("git push" in str(step.get("run")) for step in job.get("steps") or [])
    )


def _checkout(steps: list) -> dict:
    return next(s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@"))


def test_the_pinned_publisher_pushes_as_github_actions():
    from agentic_sdlc.onboard import GITHUB_ACTIONS_PUSHER, publisher_push_actor

    assert publisher_push_actor(_implement_doc()) == GITHUB_ACTIONS_PUSHER
    # the token spelled as the secret, or the checkout's default token, is the same actor
    doc = _implement_doc()
    _checkout(_publishing_steps(doc))["with"]["token"] = "${{ secrets.GITHUB_TOKEN }}"
    assert publisher_push_actor(doc) == GITHUB_ACTIONS_PUSHER
    doc = _implement_doc()
    del _checkout(_publishing_steps(doc))["with"]["token"]
    assert publisher_push_actor(doc) == GITHUB_ACTIONS_PUSHER


def _mutated(change) -> object:
    from agentic_sdlc.onboard import publisher_push_actor

    doc = _implement_doc()
    change(doc, _publishing_steps(doc))
    return publisher_push_actor(doc)


def _push_step(steps: list) -> dict:
    return next(s for s in steps if "git push" in str(s.get("run")))


@pytest.mark.parametrize(
    ("change", "needle"),
    [
        (
            lambda d, s: _checkout(s)["with"].update(token="${{ steps.app.outputs.token }}"),
            "not github.token",
        ),
        (
            lambda d, s: _checkout(s)["with"].update({"persist-credentials": False}),
            "does not persist",
        ),
        (lambda d, s: _checkout(s)["with"].update({"ssh-key": "x"}), "SSH key"),
        (
            lambda d, s: s.insert(
                s.index(_push_step(s)), {"name": "auth", "uses": "some/creds-action@v1"}
            ),
            "may change git's credentials",
        ),
        (
            lambda d, s: _push_step(s).update(
                run="git config http.extraheader x\n" + _push_step(s)["run"]
            ),
            "git config http.extraheader",
        ),
        (
            lambda d, s: _push_step(s).update(
                run="git remote set-url origin x\n" + _push_step(s)["run"]
            ),
            "git remote",
        ),
        (lambda d, s: _push_step(s).update(env={"GIT_ASKPASS": "/x"}), "GIT_ASKPASS"),
        (lambda d, s: s.insert(0, {"uses": "actions/checkout@v4"}), "found 2"),
    ],
)
def test_an_unrecognised_push_credential_leaves_the_pusher_unknown(change, needle):
    found = _mutated(change)
    assert isinstance(found, str) and needle in found, found


def test_a_classic_allowlist_must_admit_the_actor_that_actually_pushes(tmp_path):
    """Allowing only the Publisher App does not let `github-actions[bot]` push agent/**: doctor
    must not report READY for it (it did, judging the Publisher App)."""
    from agentic_sdlc.onboard import PUBLISHER_BRANCH_RULES_CHECK

    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    app = {"actor": {"__typename": "App", "slug": "agentic-sdlc-publisher"}}
    gh = _healthy_gh()
    gh.answers[CLASSIC_RULES] = _classic_rules(
        _bpr("agent/**", restrictsPushes=True, pushAllowances={"nodes": [app]})
    )
    check = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))[
        PUBLISHER_BRANCH_RULES_CHECK
    ]
    assert check.manual and "without the pushing actor github-actions[bot]" in check.detail
    # an unknown pusher never passes an allowlist, whoever is on it
    from agentic_sdlc.onboard import (
        GITHUB_ACTIONS_PUSHER,
        PublisherValue,
        classic_publisher_branch_problems,
    )

    branch = PublisherValue(("agent/issue-", "-", ""), ("ISSUE_NUMBER", "GITHUB_RUN_ID"))
    actions = {"actor": {"__typename": "App", "slug": "github-actions"}}
    answer = json.loads(
        _classic_rules(
            _bpr("agent/**", restrictsPushes=True, pushAllowances={"nodes": [actions, app]})
        )
    )
    assert classic_publisher_branch_problems(answer, branch, GITHUB_ACTIONS_PUSHER) == []
    unknown = classic_publisher_branch_problems(answer, branch, "the checkout persists X")
    assert len(unknown) == 1 and "could not be determined (the checkout persists X)" in unknown[0]


def test_commit_guard_pr_fallback_accepts_only_a_same_repository_head(tmp_path, monkeypatch):
    """A fork PR from an identically named branch is not this branch's claim, and a PR whose head
    repository cannot be read is not one either."""
    guard = _guard_module(tmp_path, "forge_commit_guard")
    seen: list[list[str]] = []
    answer: dict[str, object] = {}

    def fake_run(args, **kwargs):
        seen.append(list(args))
        assert kwargs.get("timeout")

        class Done:
            stdout = json.dumps(answer["pages"])

        return Done()

    monkeypatch.setattr(guard.subprocess, "run", fake_run)
    own = {
        "number": 3,
        "state": "open",
        "head": {"ref": "forge/issue-7", "repo": {"full_name": "Owner/Comic"}},
    }
    fork = {**own, "head": {"ref": "forge/issue-7", "repo": {"full_name": "mallory/comic"}}}
    answer["pages"] = [[fork]]
    assert guard.open_pr_for("forge/issue-7") is False
    for broken in (
        {k: v for k, v in own.items() if k != "head"},
        {**own, "state": "closed"},
        {**own, "head": {"ref": "forge/issue-7", "repo": None}},  # deleted head repository
        {**own, "head": {"ref": "forge/issue-8", "repo": {"full_name": "owner/comic"}}},
        {**own, "head": {"ref": "forge/issue-7", "repo": {"full_name": ""}}},
        {**own, "head": "owner:forge/issue-7"},
    ):
        answer["pages"] = [[broken]]
        assert guard.open_pr_for("forge/issue-7") is False, broken
    for unreadable in ({"number": 3}, [{"number": 3}], None):
        answer["pages"] = unreadable
        assert guard.open_pr_for("forge/issue-7") is None, unreadable
    answer["pages"] = [[fork], [own]]
    assert guard.open_pr_for("forge/issue-7") is True
    commit = {"tool_name": "Bash", "session_id": "s1", "tool_input": {"command": "git commit"}}
    answer["pages"] = [[fork]]
    code, message = guard.decide(commit, "forge/issue-7", lambda n: None, guard.open_pr_for)
    assert code == 2 and "no live lease exists" in message
    answer["pages"] = [[own]]
    assert guard.decide(commit, "forge/issue-7", lambda n: None, guard.open_pr_for) == (0, "")


# Codex 4240421140: `gh pr list --head` fetched only 30 PRs and filtered by branch NAME, so more
# than 30 fork PRs from an identically named branch hid the same-repository one. The lookup is the
# REST list filtered to `OWNER:BRANCH`, every page read.
def test_commit_guard_pr_lookup_is_exhaustive_and_owner_qualified(tmp_path, monkeypatch):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    seen: list[list[str]] = []
    forks = [
        {
            "number": n,
            "state": "open",
            "head": {"ref": "forge/issue-7", "repo": {"full_name": f"fork{n}/comic"}},
        }
        for n in range(40)
    ]
    own = {
        "number": 99,
        "state": "open",
        "head": {"ref": "forge/issue-7", "repo": {"full_name": "owner/comic"}},
    }

    def fake_run(args, **kwargs):
        seen.append(list(args))
        assert 0 < kwargs["timeout"] <= guard.GH_TIMEOUT

        class Done:
            stdout = json.dumps([forks[:30], [*forks[30:], own]])

        return Done()

    monkeypatch.setattr(guard.subprocess, "run", fake_run)
    assert guard.open_pr_for("forge/issue-7") is True
    args = seen[-1]
    assert args[:2] == ["gh", "api"] and "--paginate" in args and "--slurp" in args
    assert "pr" not in args and "--limit" not in args
    assert args[2] == "repos/owner/comic/pulls?state=open&head=owner%3Aforge%2Fissue-7&per_page=100"


@pytest.mark.parametrize(
    "overrides",
    [
        {"test_command": "echo TEST_COMMAND"},
        {"setup_command": "make SETUP_COMMAND=1 PLATFORM_REPOSITORY"},
        {"quality_command": "echo AGENT IMPLEMENT_JOBS LABELER_AUTHORIZATION"},
        {"project_id": "owner/AGENT"},
        {"platform_repository": "owner/PLATFORM_REPOSITORY"},
        {"ready_label": "AUTHORIZED_WORK"},
    ],
)
def test_user_values_containing_placeholder_words_render_verbatim(overrides):
    files = render_onboarding(spec(**overrides))
    for key, value in overrides.items():
        if key.endswith("_command"):
            ci = yaml.safe_load(files[".github/workflows/ci.yml"])
            runs = [step.get("run") for job in ci["jobs"].values() for step in job["steps"]]
            assert value in runs
            assert tomllib.loads(files["agentic-sdlc.toml"])["commands"][key[:-8]] == value
        elif key == "project_id":
            assert f'PROJECT = "{value}"' in files[".claude/hooks/forge_commit_guard.py"]
            assert tomllib.loads(files["agentic-sdlc.toml"])["project"]["id"] == value
        elif key == "ready_label":
            assert value in files[".github/ISSUE_TEMPLATE/agent-work-request.md"]
        else:
            assert value in files[".github/workflows/agent-plan.yml"]


def test_hook_placeholders_are_substituted_in_one_pass():
    """A project id equal to the branch placeholder is not substituted a second time."""
    files = render_onboarding(spec(project_id="DEFAULT_BRANCH_NAME/x", default_branch="trunk"))
    start = files[".claude/hooks/forge_session_start.py"]
    assert 'PROJECT = "DEFAULT_BRANCH_NAME/x"' in start and 'DEFAULT_BRANCH = "trunk"' in start


def test_template_completeness_is_checked_on_the_source_template(monkeypatch):
    import agentic_sdlc.onboard as onboard

    real = onboard._resource

    def broken(relative):
        text = real(relative)
        if relative == "github/forge/agent-plan.yml":
            text += "# leftover: PREFLIGHT_EXTRA here\n"
        return text

    monkeypatch.setattr(onboard, "_resource", broken)
    with pytest.raises(OnboardError, match="unrendered placeholder PREFLIGHT_EXTRA in template"):
        render_onboarding(spec())

    def no_project(relative):
        text = real(relative)
        if relative == "hooks/forge_commit_guard.py":
            text = text.replace('"PROJECT_ID"', '"x"')
        return text

    monkeypatch.setattr(onboard, "_resource", no_project)
    with pytest.raises(OnboardError, match="lacks its placeholder PROJECT_ID"):
        render_onboarding(spec())

    def branch_in_guard(relative):
        text = real(relative)
        if relative == "hooks/forge_commit_guard.py":
            text += 'X = "DEFAULT_BRANCH_NAME"\n'
        return text

    monkeypatch.setattr(onboard, "_resource", branch_in_guard)
    with pytest.raises(OnboardError, match="unrendered placeholder DEFAULT_BRANCH_NAME"):
        render_onboarding(spec())


# ---------------------------------------------------------------- Codex 4239598425
# Active `target: push` rulesets reject pushes by their FILES, for every actor; doctor proves
# a restriction harmless only when the Forge policy's forbidden_paths provably excludes it.
def _push_ruleset(*rules: dict, enforcement: str = "active", source: str = "Organization"):
    return {
        "id": 88,
        "name": "org push",
        "source_type": source,
        "source": "owner",
        "target": "push",
        "enforcement": enforcement,
        "rules": list(rules),
    }


def _push_gh(detail: object, summary: dict | None = None) -> FakeGh:
    gh = _healthy_gh()
    ruleset = detail if isinstance(detail, dict) else _push_ruleset()
    summary = summary or {
        k: ruleset[k] for k in ("id", "name", "source_type", "source", "target", "enforcement")
    }
    gh.answers = {
        "api repos/owner/comic/rulesets/88": (
            json.dumps(detail) if isinstance(detail, dict) else detail
        ),
        "api repos/owner/comic/rulesets?includes_parents=true": json.dumps(
            [[{"id": 11, "name": RULESET_NAME, "enforcement": "active"}, summary]]
        ),
        **gh.answers,
    }
    return gh


@pytest.mark.parametrize(
    ("ruleset", "ok", "needle"),
    [
        # covered by forbidden_paths (spec's "*.png", the baseline's "*.pem"/".env*")
        (
            _push_ruleset(
                {
                    "type": "file_path_restriction",
                    "parameters": {"restricted_file_paths": ["*.png", ".env*"]},
                }
            ),
            True,
            "",
        ),
        (
            _push_ruleset(
                {
                    "type": "file_extension_restriction",
                    "parameters": {"restricted_file_extensions": ["*.pem", ".key"]},
                }
            ),
            True,
            "",
        ),
        (
            _push_ruleset(
                {
                    "type": "file_path_restriction",
                    "parameters": {"restricted_file_paths": ["src/**"]},
                }
            ),
            False,
            "restricts file paths 'src/**'",
        ),
        (
            _push_ruleset(
                {
                    "type": "file_path_restriction",
                    "parameters": {"restricted_file_paths": ["*.{png,exe}"]},
                }
            ),
            False,
            "restricts file paths",
        ),
        (
            _push_ruleset(
                {
                    "type": "file_extension_restriction",
                    "parameters": {"restricted_file_extensions": ["*.exe"]},
                }
            ),
            False,
            "restricts file extensions '*.exe'",
        ),
        (
            _push_ruleset({"type": "max_file_size", "parameters": {"max_file_size": 100}}),
            False,
            "limits files to 100 MB",
        ),
        (
            _push_ruleset(
                {"type": "max_file_path_length", "parameters": {"max_file_path_length": 255}}
            ),
            False,
            "limits file paths to 255",
        ),
        (_push_ruleset({"type": "something_new"}), False, "'something_new'"),
        (
            _push_ruleset(
                {"type": "max_file_size", "parameters": {"max_file_size": 1}}, source="Repository"
            ),
            False,
            "MB",
        ),
        (_push_ruleset({"type": "max_file_size"}, enforcement="evaluate"), True, ""),
        (_push_ruleset({"type": "max_file_size"}, enforcement="disabled"), True, ""),
    ],
)
def test_doctor_inspects_active_push_rulesets(tmp_path, ruleset, ok, needle):
    from agentic_sdlc.onboard import PUSH_RULESETS_CHECK

    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", _push_gh(ruleset))
    if ok:
        assert report.ok, report.render()
    else:
        check = _failed(report)[PUSH_RULESETS_CHECK]
        assert check.manual and needle in check.detail and "'org push'" in check.detail
        assert set(_failed(report)) == {PUSH_RULESETS_CHECK}


def test_doctor_fails_closed_on_unreadable_push_rulesets(tmp_path):
    from agentic_sdlc.onboard import PUSH_RULESETS_CHECK

    repo = _repo(tmp_path)
    write_onboarding(repo, spec(implementer="cloud-routine"))  # every mode is checked
    gh = _push_gh(
        OnboardError("gh: boom (HTTP 500)"),
        {"id": 88, "name": "org push", "target": "push", "enforcement": "active"},
    )
    check = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))[PUSH_RULESETS_CHECK]
    assert check.manual and "could not be read" in check.detail
    gh = _push_gh(_push_ruleset())
    gh.answers["api repos/owner/comic/rulesets?includes_parents=true"] = OnboardError(
        "gh: boom (HTTP 500)"
    )
    check = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))[PUSH_RULESETS_CHECK]
    assert check.manual and "could not list the rulesets" in check.detail
    gh = _push_gh({**_push_ruleset(), "rules": "nope"})
    check = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))[PUSH_RULESETS_CHECK]
    assert "push rules could not be read" in check.detail


def test_push_ruleset_restrictions_are_unprovable_without_a_policy():
    from agentic_sdlc.onboard import push_ruleset_problems

    ruleset = _push_ruleset(
        {"type": "file_path_restriction", "parameters": {"restricted_file_paths": ["*.png"]}}
    )
    assert push_ruleset_problems([(ruleset, ruleset)], ("*.png",)) == []
    assert push_ruleset_problems([(ruleset, ruleset)], None)


# ---------------------------------------------------------------- Codex 4239598427
# A reusable-workflow chain can only keep or lose GITHUB_TOKEN permissions: every callee job is
# compared with the ceiling its caller passes on, transitively.
def _plan_nested_with_permissions(job_permissions: object = None) -> str:
    doc = yaml.safe_load(_plan_calling_nested("nested.yml"))
    if job_permissions is not None:
        doc["jobs"]["nested"]["permissions"] = job_permissions
    return yaml.safe_dump(doc)


NESTED_PERMISSIONS_CALLEE = (
    "on:\n  workflow_call:\njobs:\n  inner:\n    runs-on: ubuntu-latest\n"
    "    permissions:\n      issues: write\n    steps:\n      - run: echo hi\n"
)


@pytest.mark.parametrize(
    ("nested_job_permissions", "callee", "needle"),
    [
        # the nested call inherits reusable-plan.yml's `permissions: {}`: nothing to pass on
        (None, NESTED_PERMISSIONS_CALLEE, "requests issues: write but"),
        # granted by the nested call, which the generated caller's `issues: write` covers
        ({"issues": "write"}, NESTED_PERMISSIONS_CALLEE, None),
        # the nested call itself asks for more than the generated caller grants
        ({"issues": "write", "pull-requests": "write"}, NESTED_PERMISSIONS_CALLEE, "pull-requests"),
        # the callee's workflow-level default applies to a job without its own
        (
            {"issues": "read"},
            NESTED_PERMISSIONS_CALLEE.replace(
                "    permissions:\n      issues: write\n", ""
            ).replace("jobs:\n", "permissions: write-all\njobs:\n"),
            "nested.yml@ffffffffffff:inner requests actions: write",
        ),
    ],
)
def test_doctor_compares_token_permissions_through_reusable_chains(
    tmp_path, nested_job_permissions, callee, needle
):
    from agentic_sdlc.onboard import PLATFORM_CONTRACT_CHECK

    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _nested_gh(_healthy_gh(), callee)
    gh.answers[PLATFORM_CONTENTS + ".github/workflows/reusable-plan.yml"] = _blob(
        _plan_nested_with_permissions(nested_job_permissions).encode()
    )
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    if needle is None:
        assert report.ok, report.render()
    else:
        check = _failed(report)[PLATFORM_CONTRACT_CHECK]
        assert not check.manual and needle in check.detail, check.detail
        assert "reusable-plan.yml@" in check.detail or "nested.yml@" in check.detail


def _permission_graph(callee_doc: dict):
    from agentic_sdlc.onboard import platform_callee_graph

    return platform_callee_graph({"r.yml": callee_doc}, SHA, "owner/agentic-sdlc", lambda *a: None)


def test_token_ceiling_falls_back_to_the_repository_default_and_fails_closed():
    from agentic_sdlc.onboard import (
        Unreadable,
        default_token_permissions,
        token_permission_problems,
    )

    callee = {
        "on": {"workflow_call": None},
        "jobs": {"j": {"runs-on": "x", "permissions": {"contents": "write"}, "steps": []}},
    }
    graph = _permission_graph(callee)
    caller_job = {"uses": f"owner/agentic-sdlc/.github/workflows/r.yml@{SHA}"}
    calls = [("caller.yml:call", caller_job, "r.yml")]

    def check(caller_doc, default):
        return token_permission_problems(
            [("caller.yml", caller_doc)], calls, graph, SHA, "owner/agentic-sdlc", lambda: default
        )

    read = default_token_permissions({"default_workflow_permissions": "read"})
    write = default_token_permissions({"default_workflow_permissions": "write"})
    over, unknown = check({"jobs": {"call": caller_job}}, read)
    assert (
        unknown == []
        and "requests contents: write but caller.yml:call grants at most contents: read" in over[0]
    )
    assert check({"jobs": {"call": caller_job}}, write) == ([], [])
    # the caller's workflow-level permissions are its jobs' default: the repo default is unused
    assert check({"permissions": {"contents": "write"}}, "never read") == ([], [])
    over, unknown = check({"permissions": {"contents": "read"}}, "never read")
    assert over and not unknown
    # unreadable default, unreadable permissions, unreadable callee: unknown, never fine
    unreadable = default_token_permissions(Unreadable("boom"))
    assert isinstance(unreadable, str)
    over, unknown = check({}, unreadable)
    assert not over and "cannot be determined" in unknown[0]
    assert isinstance(default_token_permissions({"default_workflow_permissions": "x"}), str)
    # an invalid level is not unknown but a mismatch: GitHub rejects the workflow (4239780832)
    over, unknown = check({"permissions": {"contents": "admin"}}, "never read")
    assert over and "invalid permission contents: 'admin'" in over[0] and not unknown
    over, unknown = token_permission_problems(
        [("caller.yml", {"permissions": "write-all"})],
        [("caller.yml:call", caller_job, "gone.yml")],
        graph,
        SHA,
        "owner/agentic-sdlc",
        lambda: "never read",
    )
    assert not over and "could not be read" in unknown[0]


def test_doctor_reports_an_undeterminable_token_ceiling_as_manual(tmp_path):
    from agentic_sdlc.onboard import PLATFORM_CONTRACT_CHECK, TOKEN_PERMISSIONS_CHECK

    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    plan = repo / ".github/workflows/agent-plan.yml"
    doc = yaml.safe_load(plan.read_text())
    gh = _healthy_gh()
    # an installed caller with no permissions on the calling job or the workflow inherits the
    # repository default
    doc.pop("permissions")
    doc["jobs"]["plan"].pop("permissions")
    plan.write_text(yaml.safe_dump(doc))
    # before the healthy table's `actions/permissions` prefix
    gh.answers = {
        "api repos/owner/comic/actions/permissions/workflow": OnboardError("gh: boom (HTTP 500)"),
        **gh.answers,
    }
    check = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)).get(
        TOKEN_PERMISSIONS_CHECK
    )
    assert check is not None and check.manual and "cannot be determined" in check.detail
    assert "agent-plan.yml:plan" in check.detail and "boom" in check.detail
    # readable and restricted: the callee's `issues: write` is then over the ceiling
    gh = _healthy_gh()
    gh.answers = {
        "api repos/owner/comic/actions/permissions/workflow": json.dumps(
            {"default_workflow_permissions": "read"}
        ),
        **gh.answers,
    }
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))
    assert TOKEN_PERMISSIONS_CHECK not in failed
    assert "grants at most issues: none" in failed[PLATFORM_CONTRACT_CHECK].detail


# ---------------------------------------------------------------- Codex review 5481273396


@pytest.fixture
def helper_repo(tmp_path, monkeypatch):
    """A git repository on `forge/issue-7` whose configuration is ONLY what a test writes: no
    global/system configuration, no GIT_CONFIG_* or helper variables from the environment."""
    import subprocess

    for name in list(os.environ):
        if name.startswith("GIT_CONFIG") or name in (
            "GIT_EXTERNAL_DIFF", "GIT_EXEC_PATH", "GIT_PAGER", "PAGER", "GIT_SSH",
            "GIT_SSH_COMMAND", "GIT_ASKPASS", "SSH_ASKPASS", "GIT_PROXY_COMMAND", "GIT_DIR",
            "GIT_WORK_TREE",
        ):  # fmt: skip
            monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    git("init", "-q", "-b", "forge/issue-7")
    git("config", "user.name", "t")
    git("config", "user.email", "t@example.com")
    git("config", "commit.gpgsign", "false")
    (repo / "a.txt").write_text("a\n")
    git("add", "a.txt")
    git("commit", "-q", "-m", "init")
    git("remote", "add", "origin", "https://github.com/owner/comic.git")
    guard = _guard_module(tmp_path, "forge_commit_guard")
    return guard, repo, git


def _guarded_decision(guard, repo, command, lease=None):
    payload = {
        "tool_name": "Bash",
        "session_id": "s1",
        "cwd": str(repo),
        "tool_input": {"command": command},
    }
    branches = guard.commit_branches(command, str(repo))
    return guard.decide(payload, branches, lambda n: lease, lambda b: False)


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git diff",
        "git log -p",
        "git show HEAD",
        "git blame a.txt",
        "git grep -n a",
        "git fetch origin",
        "git remote show origin",
        "git help commit",
        "git log --show-signature",
    ],
)
def test_commit_guard_read_only_git_without_helpers_still_passes(helper_repo, command):
    guard, repo, git = helper_repo
    git("config", "core.pager", "less -R")  # plain pagers and booleans run nothing
    git("config", "pager.log", "false")
    git("config", "core.fsmonitor", "false")
    git("config", "filter.lfs.clean", "git-lfs clean -- %f")
    git("config", "filter.lfs.smudge", "git-lfs smudge --skip -- %f")
    git("config", "filter.lfs.process", "git-lfs filter-process")
    git("config", "credential.helper", "store")
    git("config", "gpg.ssh.program", "/opt/sign")  # only verifying commands run it
    git("config", "url.https://github.com/.insteadOf", "git@github.com:")
    if command.endswith("--show-signature"):
        git("config", "--unset", "gpg.ssh.program")
    assert not guard.is_git_commit(command, cwd=str(repo)), command
    assert _guarded_decision(guard, repo, command) == (0, "")


@pytest.mark.parametrize(
    ("key", "value", "command"),
    [
        ("diff.external", "/tmp/evil", "git diff"),
        ("diff.foo.textconv", "/tmp/evil", "git log -p"),
        ("diff.foo.command", "/tmp/evil", "git show HEAD"),
        ("filter.foo.clean", "/tmp/evil", "git status"),
        ("filter.foo.process", "/tmp/evil", "git diff"),
        ("filter.lfs.clean", "git-lfs clean -- %f; /tmp/evil", "git status"),
        ("core.pager", "sh -c /tmp/evil", "git log"),
        ("pager.log", "/tmp/evil", "git log"),
        ("core.fsmonitor", "/tmp/evil", "git status"),
        ("submodule.lib.url", "https://github.com/o/lib", "git status"),
        ("gpg.program", "/tmp/evil", "git log --show-signature"),
        ("gpg.ssh.program", "/tmp/evil", "git log --format=%G?"),
        ("core.sshCommand", "/tmp/evil", "git fetch origin"),
        ("core.askPass", "/tmp/evil", "git fetch origin"),
        ("credential.helper", "!/tmp/evil", "git fetch origin"),
        ("credential.https://github.com.helper", "/tmp/evil", "git remote show origin"),
        ("remote.origin.uploadpack", "/tmp/evil", "git fetch origin"),
        ("remote.origin.vcs", "evil", "git fetch origin"),
        ("remote.origin.url", "ext::sh -c /tmp/evil", "git fetch origin"),
        ("remote.origin.url", "evil://host/repo", "git fetch origin"),
        ("remote.origin.url", "fd::3", "git remote show origin"),
        ("url.ext::sh -c x.insteadOf", "https://", "git fetch origin"),
        ("core.gitProxy", "/tmp/evil", "git fetch origin"),
        ("man.viewer", "evil", "git help commit"),
    ],
)
def test_commit_guard_read_only_git_with_a_configured_helper_needs_the_lease(
    helper_repo, key, value, command
):
    """Codex 4239667542: configuration can make a "read-only" subcommand run a program (that
    could commit or push), so it is not read-only any more: it needs the lease."""
    guard, repo, git = helper_repo
    git("config", key, value)
    assert guard.is_git_commit(command, cwd=str(repo)), (key, command)
    code, message = _guarded_decision(guard, repo, command)
    assert code == 2 and "no live lease exists" in message, message
    assert "configured outside the command" in message and key.split(".")[0] in message.lower()
    # with the lease it is allowed, and off an issue branch nothing changes
    lease = {"session": "s1", "agent": "a", "expires": None}
    assert _guarded_decision(guard, repo, command, lease) == (0, "")
    git("switch", "-q", "-c", "main")
    assert _guarded_decision(guard, repo, command) == (0, "")


def test_commit_guard_network_helpers_only_matter_to_network_subcommands(helper_repo):
    guard, repo, git = helper_repo
    git("config", "credential.helper", "!/tmp/evil")
    git("config", "core.sshCommand", "/tmp/evil")
    assert not guard.is_git_commit("git diff", cwd=str(repo))
    assert not guard.is_git_commit("git remote -v", cwd=str(repo))
    assert not guard.is_git_commit("git remote show -n origin", cwd=str(repo))
    assert guard.is_git_commit("git fetch origin", cwd=str(repo))
    assert guard.is_git_commit("git remote show origin", cwd=str(repo))


def test_commit_guard_follows_includes_git_resolves(helper_repo):
    guard, repo, git = helper_repo
    extra = repo.parent / "extra.gitconfig"
    extra.write_text("[diff]\n\texternal = /tmp/evil\n")
    git("config", "include.path", str(extra))
    assert guard.is_git_commit("git diff", cwd=str(repo))
    extra.write_text("")
    git("config", f"includeIf.gitdir:{repo}/.path", str(extra))
    extra.write_text("[core]\n\tpager = /tmp/evil\n")
    git("config", "--unset", "include.path")
    assert guard.is_git_commit("git log", cwd=str(repo))


def test_commit_guard_global_configuration_counts_too(helper_repo):
    guard, repo, _ = helper_repo
    (Path(os.environ["GIT_CONFIG_GLOBAL"])).write_text("[diff]\n\texternal = /tmp/evil\n")
    assert guard.is_git_commit("git diff", cwd=str(repo))


@pytest.mark.parametrize(
    ("name", "value", "command"),
    [
        ("GIT_EXTERNAL_DIFF", "/tmp/evil", "git diff"),
        ("GIT_EXEC_PATH", "/tmp/evil", "git status"),
        ("GIT_PAGER", "/tmp/evil", "git log"),
        ("PAGER", "sh -c /tmp/evil", "git log"),
        ("GIT_SSH_COMMAND", "/tmp/evil", "git fetch origin"),
        ("GIT_SSH", "/tmp/evil", "git fetch origin"),
        ("GIT_ASKPASS", "/tmp/evil", "git fetch origin"),
        ("GIT_PROXY_COMMAND", "/tmp/evil", "git fetch origin"),
        ("GIT_CONFIG_PARAMETERS", "'diff.external'='/tmp/evil'", "git diff"),
    ],
)
def test_commit_guard_environment_helpers_need_the_lease(
    helper_repo, monkeypatch, name, value, command
):
    guard, repo, _ = helper_repo
    monkeypatch.setenv(name, value)
    assert guard.is_git_commit(command, cwd=str(repo)), name


def test_commit_guard_harmless_environment_pagers_pass(helper_repo, monkeypatch):
    guard, repo, _ = helper_repo
    monkeypatch.setenv("PAGER", "less")
    monkeypatch.setenv("GIT_PAGER", "cat")
    monkeypatch.setenv("GIT_ASKPASS", "")  # empty: unset
    monkeypatch.setenv("GIT_SSH_COMMAND", "/tmp/evil")  # only network subcommands use it
    assert not guard.is_git_commit("git log", cwd=str(repo))
    assert not guard.is_git_commit("git diff", cwd=str(repo))


@pytest.mark.parametrize(
    ("hook", "command"),
    [
        ("post-index-change", "git status"),
        ("reference-transaction", "git fetch origin"),
        ("pre-auto-gc", "git fetch origin"),
    ],
)
def test_commit_guard_hooks_a_read_only_command_runs_need_the_lease(helper_repo, hook, command):
    guard, repo, git = helper_repo
    assert not guard.is_git_commit(command, cwd=str(repo))
    hooks = repo / ".git/hooks"
    (hooks / hook).write_text("#!/bin/sh\n")
    assert guard.is_git_commit(command, cwd=str(repo))
    # found from a subdirectory and through `-C` as well (git answers relative paths)
    (repo / "sub").mkdir()
    assert guard.is_git_commit(command, cwd=str(repo / "sub"))
    assert guard.is_git_commit(command.replace("git ", "git -C repo ", 1), cwd=str(repo.parent))
    (hooks / hook).unlink()
    assert not guard.is_git_commit(command.replace("git ", "git -C repo ", 1), cwd=str(repo.parent))
    elsewhere = repo.parent / "hooks"
    elsewhere.mkdir()
    (elsewhere / hook).write_text("#!/bin/sh\n")
    git("config", "core.hooksPath", str(elsewhere))
    assert guard.is_git_commit(command, cwd=str(repo))


def test_commit_guard_submodules_need_the_lease(helper_repo):
    guard, repo, _ = helper_repo
    (repo / ".gitmodules").write_text('[submodule "lib"]\n\tpath = lib\n')
    assert guard.is_git_commit("git status", cwd=str(repo))


@pytest.mark.parametrize(
    "command",
    [
        "git diff --ext-diff",
        "git log -p --ext-diff",
        "git show --textconv HEAD",
        "git grep --textconv a",
        "git cat-file --textconv HEAD:a.txt",
        "git cat-file --filters HEAD:a.txt",
    ],
)
def test_commit_guard_helper_options_need_the_lease(helper_repo, command):
    guard, repo, _ = helper_repo
    assert guard.is_git_commit(command, cwd=str(repo))
    assert _guarded_decision(guard, repo, command)[0] == 2


@pytest.mark.parametrize("command", ["git -c diff.external=/tmp/evil diff", "git -c x=y status"])
def test_commit_guard_config_overrides_on_read_only_git_need_the_lease(helper_repo, command):
    guard, repo, _ = helper_repo
    assert guard.is_git_commit(command, cwd=str(repo))


def test_commit_guard_unreadable_configuration_fails_closed(helper_repo, monkeypatch):
    import subprocess

    guard, repo, _ = helper_repo
    config = repo / ".git/config"
    good = config.read_text()
    config.write_text(good + "[broken\n")
    assert guard.is_git_commit("git status", cwd=str(repo))
    config.write_text(good)
    assert not guard.is_git_commit("git status", cwd=str(repo))
    real = subprocess.run

    def slow_config(args, **kwargs):
        if "config" in args:
            raise subprocess.TimeoutExpired(args, kwargs.get("timeout"))
        return real(args, **kwargs)

    monkeypatch.setattr(guard.subprocess, "run", slow_config)
    assert guard.is_git_commit("git status", cwd=str(repo))
    # a location whose configuration cannot be known is not proven read-only either
    monkeypatch.setattr(guard.subprocess, "run", real)
    assert guard.is_git_commit('git -C "$dir" status', cwd=str(repo))
    assert guard.is_git_commit('cd "$(git rev-parse --show-toplevel)" && git status', cwd=str(repo))


def test_commit_guard_bounds_every_subprocess_call():
    """Codex 4239667544: every subprocess.run in the guard carries a timeout."""
    import ast

    source = render_onboarding(spec())[".claude/hooks/forge_commit_guard.py"]
    calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
    ]
    assert len(calls) >= 3  # the trusted git probe, the token read and the trusted gh call
    for call in calls:
        timeouts = [k for k in call.keywords if k.arg == "timeout"]
        assert timeouts, f"subprocess.run at line {call.lineno} has no timeout"


def test_commit_guard_github_lookups_time_out_as_unavailable(tmp_path, monkeypatch):
    import subprocess

    guard = _guard_module(tmp_path, "forge_commit_guard")
    seen: list[float | None] = []

    def stalled(args, **kwargs):
        seen.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(args, kwargs.get("timeout"))

    monkeypatch.setattr(guard.subprocess, "run", stalled)
    monkeypatch.setattr(guard, "_PERMISSIONS", {})
    assert guard.lease_unavailable(guard.lease_for(7))
    assert guard.open_pr_for("forge/issue-7") is None
    with pytest.raises(guard.LeaseUnavailable, match="timed out"):
        guard.repo_permission("someone")
    assert seen == [guard.GH_TIMEOUT] * 3 and 0 < guard.GH_TIMEOUT <= 60
    commit = {"tool_name": "Bash", "session_id": "s1", "tool_input": {"command": "git commit"}}
    code, message = guard.decide(commit, "forge/issue-7", guard.lease_for, guard.open_pr_for)
    assert code == 2 and "Lease inventory unavailable" in message
    code, message = guard.decide(commit, "forge/issue-7", lambda n: None, guard.open_pr_for)
    assert code == 2 and "could not be read" in message
    # a marker whose author's permission read stalls makes the lease unreadable, not free
    expires = "2999-01-01T00:00:00Z"
    claim = f"<!-- forge-claim agent=a session=s9 branch=b expires={expires} -->"
    comments = [{"body": claim, "user": {"login": "owner", "type": "User"}}]
    with pytest.raises(guard.LeaseUnavailable):
        guard.lease_from_comments(comments)


def test_commit_guard_local_git_timeouts_refuse(tmp_path, monkeypatch):
    import subprocess

    guard = _guard_module(tmp_path, "forge_commit_guard")

    def stalled(args, **kwargs):
        assert kwargs.get("timeout") == guard.GIT_TIMEOUT
        raise subprocess.TimeoutExpired(args, kwargs.get("timeout"))

    monkeypatch.setattr(guard.subprocess, "run", stalled)
    assert guard.current_branch(str(tmp_path)) == guard.BRANCH_TIMEOUT
    assert guard._git_config(str(tmp_path), ((), (), ()), "--get", "x") == (-1, "")
    assert not guard._branch_exists("main", str(tmp_path), ((), (), ()))
    commit = {"tool_name": "Bash", "session_id": "s1", "tool_input": {"command": "git commit"}}
    branches = guard.commit_branches("git commit", str(tmp_path))
    assert branches == [guard.BRANCH_TIMEOUT]
    code, message = guard.decide(commit, branches, lambda n: None, lambda b: False)
    assert code == 2 and "did not report the checked-out branch" in message


def test_guard_and_leases_agree_on_same_repository_prs(tmp_path, monkeypatch):
    """Codex 4239667540: `claim()` and the commit guard apply ONE same-repository rule -- the
    guard over the REST `pulls` shape (Codex 4240421140), `claim()` over GraphQL rows: the same
    pull request reads the same way in both."""
    from agentic_sdlc import leases

    guard = _guard_module(tmp_path, "forge_commit_guard")
    monkeypatch.setattr(guard, "PROJECT", "Owner/Comic")
    own = {
        "isCrossRepository": False,
        "headRepository": {"name": "comic"},
        "headRepositoryOwner": {"login": "owner"},
    }

    def rest(full_name):
        repo = {"full_name": full_name} if full_name is not None else None
        return {"state": "open", "head": {"ref": "forge/issue-7", "repo": repo}}

    pairs = [
        (own, rest("owner/comic")),
        (
            {**own, "isCrossRepository": True, "headRepositoryOwner": {"login": "mallory"}},
            rest("mallory/comic"),
        ),
        ({**own, "headRepository": {"name": "other"}}, rest("owner/other")),
        ({**own, "headRepository": None}, rest(None)),  # the head repository was deleted
        ({**own, "headRepositoryOwner": {"login": ""}}, rest("")),
        ("not a dict", "not a dict"),
    ]
    for row, rest_row in pairs:
        assert guard._same_repository_pr(rest_row, "forge/issue-7") == leases._same_repository_pr(
            row, "Owner/Comic"
        ), row


def test_auto_workflows_report_a_failed_authorization_from_a_hosted_runner():
    """Codex 4239667541: a FAILED authorization job (not a definitive `authorized=false`) is
    reported on the issue by a GitHub-hosted job, so the label event is not silently consumed."""
    files = render_onboarding(spec(runs_on=("self-hosted", "linux")))
    for name, job_name, work in (
        ("agent-auto-plan.yml", "preflight", "Planning"),
        ("agent-auto-implement.yml", "authorize", "Implementation"),
    ):
        doc = yaml.safe_load(files[f".github/workflows/{name}"])
        report = doc["jobs"]["authorization_failed"]
        assert report["needs"] == job_name
        assert report["runs-on"] == "ubuntu-latest"
        assert report["permissions"] == {"issues": "write"}
        condition = report["if"]
        assert "always()" in condition
        assert f"needs.{job_name}.result == 'failure'" in condition
        assert f"needs.{job_name}.result == 'cancelled'" in condition
        assert "authorized" not in condition  # a definitive "no" is not a failure
        step = report["steps"][0]
        assert step["uses"].startswith("actions/github-script@")
        assert step["env"]["ISSUE_NUMBER"] == "${{ github.event.issue.number }}"
        assert "createComment" in step["with"]["script"]
        assert f"{work} was not started" in step["with"]["script"]
        assert "could not be verified" in step["with"]["script"]
        # the existing notifiers still never reach RUNS_ON for an unauthorized labeler
        notify = doc["jobs"]["notify_failure"]
        assert notify["runs-on"] == ["self-hosted", "linux"]


@pytest.mark.parametrize("name", ["agent-auto-plan.yml", "agent-auto-implement.yml"])
@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["jobs"]["authorization_failed"].update({"runs-on": ["self-hosted", "linux"]}),
        lambda d: d["jobs"]["authorization_failed"].update({"permissions": {"issues": "read"}}),
        lambda d: d["jobs"]["authorization_failed"].update({"if": "false"}),
        lambda d: d["jobs"].pop("authorization_failed"),
    ],
)
def test_doctor_requires_the_authorization_failure_reporter(tmp_path, name, change):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    assert _drift(repo) is None
    _edit_workflow(repo, name, change)
    drift = _drift(repo)
    assert drift is not None and name in drift


# ---------------------------------------------------------------- Codex review 5481401488


@pytest.mark.parametrize(
    ("permissions", "needle"),
    [
        ({"issuse": "none"}, "invalid permission scope 'issuse'"),
        ({"metadata": "read"}, "invalid permission scope 'metadata'"),
        ({"id-token": "read"}, "invalid permission id-token: 'read'"),
        ({"models": "write"}, "invalid permission models: 'write'"),
        ({"contents": True}, "invalid permission contents: True"),
        ("read", "invalid permissions 'read'"),
        (None, "invalid permissions None"),
    ],
)
def test_invalid_token_permission_syntax_is_a_mismatch(permissions, needle):
    """Codex 4239780832: GitHub's `permissions` syntax is finite. An unknown scope or a level the
    scope does not accept makes GitHub reject the workflow: a mismatch, never READY."""
    from agentic_sdlc.onboard import token_permission_problems, token_permissions

    assert isinstance(token_permissions(permissions), str)
    callee = {
        "on": {"workflow_call": None},
        "jobs": {"j": {"runs-on": "x", "permissions": permissions, "steps": []}},
    }
    caller_job = {"uses": f"owner/agentic-sdlc/.github/workflows/r.yml@{SHA}"}
    calls = [("caller.yml:call", caller_job, "r.yml")]
    graph = _permission_graph(callee)
    over, unknown = token_permission_problems(
        [("caller.yml", {"permissions": "write-all"})],
        calls,
        graph,
        SHA,
        "owner/agentic-sdlc",
        lambda: "never read",
    )
    assert not unknown and any(needle in o for o in over), over
    # the same entry on the caller (the ceiling) is a mismatch too
    over, unknown = token_permission_problems(
        [("caller.yml", {"permissions": permissions})],
        calls,
        _permission_graph({**callee, "jobs": {"j": {"runs-on": "x", "steps": []}}}),
        SHA,
        "owner/agentic-sdlc",
        lambda: "never read",
    )
    assert not unknown and any(needle in o for o in over), over


def test_valid_token_permission_syntax_reads():
    from agentic_sdlc.onboard import TOKEN_SCOPES, token_permissions

    assert token_permissions({"id-token": "write", "models": "read", "issues": "none"}) == {
        "id-token": 2,
        "models": 1,
        "issues": 0,
    }
    assert token_permissions({"id-token": "none", "models": "none"}) == {
        "id-token": 0,
        "models": 0,
    }
    assert token_permissions({}) == {}
    assert set(token_permissions("read-all")) == set(TOKEN_SCOPES)
    for scope in TOKEN_SCOPES:
        assert isinstance(token_permissions({scope: "none"}), dict), scope


def test_forced_onboard_keeps_notes_around_the_guides(tmp_path):
    """Codex 4239780830: doctor accepts repository notes around the generated guide text, so a
    forced re-onboard replaces only that text and is idempotent."""
    from agentic_sdlc.onboard import managed_file_problem, render_agents_md, render_claude_md

    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    for name in ("AGENTS.md", "CLAUDE.md"):
        guide = repo / name
        guide.write_text(f"# Team notes for {name}\n\n" + guide.read_text() + "\nKeep me too.\n")
    write_onboarding(repo, spec(), force=True)
    agents = (repo / "AGENTS.md").read_text()
    assert agents.startswith("# Team notes for AGENTS.md\n\n") and agents.endswith("Keep me too.\n")
    first = {n: (repo / n).read_text() for n in ("AGENTS.md", "CLAUDE.md")}
    write_onboarding(repo, spec(), force=True)
    assert {n: (repo / n).read_text() for n in first} == first  # idempotent

    # a new policy replaces the previous managed text in place, notes kept
    newer = spec(test_command="pytest -q")
    write_onboarding(repo, newer, force=True)
    for name, render in (("AGENTS.md", render_agents_md), ("CLAUDE.md", render_claude_md)):
        text = (repo / name).read_text()
        assert text.startswith(f"# Team notes for {name}\n\n") and text.endswith("Keep me too.\n")
        assert render(newer) in text and render(spec()) not in text
        assert text.count(render(newer)) == 1
        assert managed_file_problem(name, text.encode(), {name: render(newer)}) is None


def test_forced_onboard_appends_the_guide_to_an_owners_file(tmp_path):
    from agentic_sdlc.onboard import render_agents_md

    repo = _repo(tmp_path)
    (repo / "AGENTS.md").write_text("owner content\n")
    write_onboarding(repo, spec(), force=True)
    assert (repo / "AGENTS.md").read_text() == "owner content\n\n" + render_agents_md(spec())


def test_forced_onboard_refuses_an_edited_guide_rather_than_delete_notes(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    guide = repo / "AGENTS.md"
    edited = "my notes\n" + guide.read_text().replace("Plan before implementation.", "Plan.")
    guide.write_text(edited)
    workflow = repo / ".github/workflows/ci.yml"
    workflow.write_text("stale\n")
    with pytest.raises(OnboardError, match="AGENTS.md holds an edited Forge guide"):
        write_onboarding(repo, spec(), force=True)
    assert guide.read_text() == edited
    assert workflow.read_text() == "stale\n"  # refused before anything was written


def test_forced_onboard_merges_claude_settings(tmp_path):
    """Codex 4239780830: permissions and unrelated hooks in `.claude/settings.json` survive a
    forced re-onboard; Forge's own handlers are written once, exactly."""
    from agentic_sdlc.onboard import HOOK_SCRIPTS, hook_problems

    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    path = repo / ".claude/settings.json"
    settings = json.loads(path.read_text())
    settings["permissions"] = {"allow": ["Bash(make test)"]}
    settings["hooks"]["PreToolUse"].append(
        {"matcher": "Edit", "hooks": [{"type": "command", "command": "./fmt.sh"}]}
    )
    settings["hooks"]["Stop"] = [{"hooks": [{"type": "command", "command": "./notify.sh"}]}]
    # a stale async copy of the guard, beside a user hook in the same group
    settings["hooks"]["PreToolUse"].append(
        {
            "matcher": "Bash",
            "hooks": [
                {**_forge_entry("PreToolUse"), "async": True},
                {"type": "command", "command": "./audit.sh"},
            ],
        }
    )
    path.write_text(json.dumps(settings))
    write_onboarding(repo, spec(), force=True)
    merged = json.loads(path.read_text())
    assert merged["permissions"] == {"allow": ["Bash(make test)"]}
    assert merged["hooks"]["Stop"] == settings["hooks"]["Stop"]
    commands = [
        h["command"] for g in merged["hooks"]["PreToolUse"] for h in g["hooks"] if "command" in h
    ]
    assert "./fmt.sh" in commands and "./audit.sh" in commands
    assert sum(HOOK_SCRIPTS["PreToolUse"] in c for c in commands) == 1
    assert hook_problems(path) == []
    first = path.read_text()
    write_onboarding(repo, spec(), force=True)
    assert path.read_text() == first  # idempotent


@pytest.mark.parametrize("content", ["{not json", "[]", '{"hooks": []}', '{"hooks": {"Stop": 1}}'])
def test_forced_onboard_refuses_unmergeable_settings(tmp_path, content):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    path = repo / ".claude/settings.json"
    path.write_text(content)
    if content == '{"hooks": {"Stop": 1}}':  # an unrelated event: kept as is
        write_onboarding(repo, spec(), force=True)
        assert json.loads(path.read_text())["hooks"]["Stop"] == 1
        return
    with pytest.raises(OnboardError, match="settings.json"):
        write_onboarding(repo, spec(), force=True)
    assert path.read_text() == content


# Codex 4239868022: a repository selector the shell expands cannot be probed; the probe failed
# and the failure read as "no branch", so the command reached an unleased issue checkout.
@pytest.mark.parametrize(
    "command",
    [
        'DIR=../wt; git -C "$DIR" commit -m x',
        'git -C "$DIR" commit -m x',
        "git -C $DIR commit -m x",
        'git --git-dir="$X/.git" commit -m x',
        "git --git-dir=$X commit -m x",
        'git --git-dir "$X" commit -m x',
        'git --work-tree="$W" commit -m x',
        'GIT_DIR="$DIR/.git" git commit -m x',
        "GIT_WORK_TREE=$W git commit -m x",
        'env GIT_DIR="$DIR/.git" git commit -m x',
        'git -C "$(pwd)/../wt" commit -m x',
        "git -C `pwd`/../wt commit -m x",
        "git -C ../w* commit -m x",
        "git -C ../w{t,x} commit -m x",
        "git --git-dir=~/wt/.git commit -m x",
        'git -C "~/wt" commit -m x',
        'env -C "$DIR" git commit -m x',
        'git -C "$DIR" push origin HEAD',
        'git -C "$DIR" fetch origin main:main',
    ],
)
def test_commit_guard_refuses_repository_selectors_it_cannot_resolve(
    tmp_path, monkeypatch, command
):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    repo = _branch_flow_repo(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    code, message = _flow_code(guard, command, repo, leased=(7, 9))  # every lease held
    assert code == 2 and "selects its repository through text the shell" in message, command


# Codex 4240170620: an ASSEMBLED program word reached a fallback that mapped the command to the
# hook's own checkout and dropped its `-C`/`--git-dir`/GIT_DIR selectors, so `G=git; "$G" -C
# ../wt commit` run from main committed in an unleased issue checkout.
@pytest.mark.parametrize(
    "command",
    [
        'G=git; "$G" -C WT commit -m x',
        "G=git; $G -C WT commit -m x",
        '"$G" --git-dir WT/.git --work-tree WT commit -m x',
        '"$(which git)" -C WT commit -m x',
        "$(which git) -C WT commit -m x",
        "`which git` -C WT commit -m x",
        "${GIT:-git} -C WT commit -m x",
        "$HOME/../wt/../repo/../../usr/bin/git -C WT commit -m x",
        "\\git -C WT commit -m x",
        '"git" -C WT commit -m x',
        "'git' -C WT commit -m x",
        "g'i't -C WT commit -m x",
        'env "$G" -C WT commit -m x',
        'command "$G" -C WT commit -m x',
        'GIT_DIR=WT/.git "$G" commit -m x',
        'cd WT && "$G" commit -m x',
        "xargs git -C WT < args.txt",
        "printf '%s\\n' -C WT commit | xargs git",
        "xargs -I{} git -C {} commit -m x",
        "xargs -i git -C {} commit -m x",
        "find WT -maxdepth 0 -exec git -C {} commit -m x ;",
        "find WT -name x -execdir git commit -m x ;",
        "parallel git -C {} commit ::: WT",
        "parallel git ::: -C WT commit",
        "eval git -C WT commit -m x",
        'echo "git -C WT commit -m x" | sh',
        "bash <<< 'git -C WT commit -m x'",
        "python3 -c \"import subprocess; subprocess.run(['git', '-C', 'WT', 'commit'])\"",
        "python3 -c \"import os, subprocess; os.chdir('WT'); subprocess.run(['git', 'commit'])\"",
        "node -e \"require('child_process').execSync('git commit', {cwd: 'WT'})\"",
    ],
)
def test_commit_guard_refuses_assembled_or_indirect_git_with_unknown_repository(
    tmp_path, monkeypatch, command
):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    repo = _branch_flow_repo(tmp_path)  # repo is on main, tmp/wt on forge/issue-9
    monkeypatch.setenv("HOME", str(tmp_path / "repo"))
    command = command.replace("WT", str(tmp_path / "wt"))
    branches = guard.commit_branches(command, str(repo))
    assert guard.UNRESOLVED_REPO in branches, (command, branches)
    code, message = _flow_code(guard, command, repo, leased=(7, 9))  # every lease held
    assert code == 2, command
    assert "cannot be checked" in message or "cannot be known" in message, command


@pytest.mark.parametrize(
    "command",
    [
        "git -C WT commit -m x",
        "/usr/bin/git -C WT commit -m x",
        "command git -C WT commit -m x",
        "command -p git -C WT commit -m x",
        "env git -C WT commit -m x",
        "env -i A=1 git -C WT commit -m x",
        "env GIT_DIR=WT/.git GIT_WORK_TREE=WT git commit -m x",
        "exec git -C WT commit -m x",
        "nice -n 5 git -C WT commit -m x",
        "time git -C WT commit -m x",
        "nohup git --git-dir=WT/.git --work-tree=WT commit -m x",
        "timeout 60 git -C WT commit -m x",
        "sh -c 'git -C WT commit -m x'",
        "xargs git -C WT commit -m x < files.txt",
        "find . -name x -exec git -C WT commit -m x {} +",
        "export GIT_EDITOR=true; git -C WT commit -m x",
        "export GIT_PAGER=cat; git -C WT log -1",
    ],
)
def test_commit_guard_keeps_selectors_of_literal_and_wrapped_git(tmp_path, command):
    """Plain `git`, a literal path to git and the parsed wrappers keep their selectors: checked
    against the issue checkout they name, passing with its lease."""
    guard = _guard_module(tmp_path, "forge_commit_guard")
    repo = _branch_flow_repo(tmp_path)
    command = command.replace("WT", str(tmp_path / "wt"))
    branches = guard.commit_branches(command, str(repo))
    assert "forge/issue-9" in branches and guard.UNRESOLVED_REPO not in branches, branches
    code, message = _flow_code(guard, command, repo)
    assert code == 2 and "issue #9" in message, command
    assert _flow_code(guard, command, repo, leased=(9,))[0] == 0, command


def test_commit_guard_unparseable_command_is_unresolved(tmp_path):
    """Text the parser cannot split (bash may: `$'...'` with an escaped quote) selects nothing
    known: unresolved, never the hook's own checkout."""
    guard = _guard_module(tmp_path, "forge_commit_guard")
    repo = _branch_flow_repo(tmp_path)
    command = "git -C $'../w\\'t' commit -m x"
    assert guard.is_git_commit(command, cwd=str(repo))
    assert guard.commit_branches(command, str(repo)) == [guard.UNRESOLVED_REPO]
    assert _flow_code(guard, command, repo, leased=(7, 9))[0] == 2


def test_commit_guard_inert_programs_naming_git_keep_working(tmp_path):
    """Printing or searching for `git` runs nothing: off an issue branch it passes as before."""
    guard = _guard_module(tmp_path, "forge_commit_guard")
    repo = _branch_flow_repo(tmp_path)
    for command in (
        "grep -rn -C 3 git src",
        "echo 'git -C ../wt commit'",
        "which git",
        "python3 -c \"print('git')\"",
        "xargs git log",
        "find . -name '*.py' -exec git log -- {} ;",
    ):
        assert guard.UNRESOLVED_REPO not in guard.commit_branches(command, str(repo)), command
        assert _flow_code(guard, command, repo)[0] == 0, command


def test_commit_guard_cd_to_an_expansion_still_refuses(tmp_path):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    repo = _branch_flow_repo(tmp_path)
    for command in ('cd "$DIR" && git commit -m x', "cd $(dirname $F) && git commit -m x"):
        assert _flow_code(guard, command, repo, leased=(7, 9))[0] == 2, command


def test_commit_guard_resolves_literal_repository_selectors(tmp_path, monkeypatch):
    """Literal selectors keep working -- a leading `~` the shell expands is resolved as the shell
    would -- and lead to the issue checkout's lease."""
    guard = _guard_module(tmp_path, "forge_commit_guard")
    repo = _branch_flow_repo(tmp_path)  # tmp/wt is on forge/issue-9
    monkeypatch.setenv("HOME", str(tmp_path))
    for command in (
        "git -C ../wt commit -m x",
        f"git -C {tmp_path / 'wt'} commit -m x",
        "git -C ~/wt commit -m x",
        "git --git-dir ~/wt/.git --work-tree ~/wt commit -m x",
        "GIT_DIR=~/wt/.git git commit -m x",
        "cd ../wt && git commit -m x",
        "cd ~/wt && git commit -m x",
    ):
        code, message = _flow_code(guard, command, repo)
        assert code == 2 and "issue #9" in message, command
        assert _flow_code(guard, command, repo, leased=(9,))[0] == 0, command
    assert _flow_code(guard, "git commit -m x", repo)[0] == 0  # plain commit on main


def test_commit_guard_branch_lookup_failures_refuse(tmp_path, monkeypatch):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    repo = _branch_flow_repo(tmp_path)
    # a directory that does not exist (yet): unknown, not "no branch"
    code, message = _flow_code(guard, "git -C ../nope commit -m x", repo, leased=(7, 9))
    assert code == 2 and "could not report the branch" in message
    assert guard.current_branch(str(tmp_path / "nope")) == guard.BRANCH_UNKNOWN
    # a repository with no commits yet still reports its (unborn) branch
    unborn = tmp_path / "unborn"
    unborn.mkdir()
    _git(unborn, "init", "-q", "-b", "forge/issue-7")
    assert guard.current_branch(str(unborn)) == "forge/issue-7"
    assert _flow_code(guard, "git commit -m x", unborn)[0] == 2
    assert _flow_code(guard, "git commit -m x", unborn, leased=(7,))[0] == 0
    # no repository at all: nothing to protect, `git clone` / `git init` keep working
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    assert guard.current_branch(str(plain)) == ""
    assert _flow_code(guard, "git clone https://example.com/x.git", plain)[0] == 0
    assert _flow_code(guard, "git init -q", plain)[0] == 0

    # any other failure (an untrusted repository, a corrupt HEAD, ...) is unknown
    import subprocess

    def failing(args, **kwargs):
        return subprocess.CompletedProcess(args, 128, stdout="", stderr="fatal: dubious ownership")

    monkeypatch.setattr(guard.subprocess, "run", failing)
    assert guard.current_branch(str(repo)) == guard.BRANCH_UNKNOWN

    def missing(args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(guard.subprocess, "run", missing)
    assert guard.current_branch(str(repo)) == guard.BRANCH_UNKNOWN


# Codex 4239868024: `git fetch` without command-line refspecs fetches what `remote.*.fetch` says,
# and that may write a local issue branch.
@pytest.mark.parametrize(
    "command",
    [
        "git fetch",
        "git fetch origin",
        "git fetch --all",
        "git fetch --prune origin",
        "git fetch --multiple origin other",
        "git fetch origin main",
        "git pull",
        "git pull origin main",
    ],
)
def test_commit_guard_fetch_checks_configured_destinations(tmp_path, command):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    repo = _branch_flow_repo(tmp_path)
    _git(repo, "remote", "add", "origin", str(tmp_path / "wt"))
    _git(
        repo, "config", "--add", "remote.origin.fetch", "+refs/heads/main:refs/heads/forge/issue-7"
    )
    code, message = _flow_code(guard, command, repo)
    assert code == 2 and "issue #7" in message, command
    assert _flow_code(guard, command, repo, leased=(7,))[0] == 0, command


@pytest.mark.parametrize(
    "spec",
    [
        "+refs/heads/*:refs/heads/*",
        "+refs/*:refs/*",
        "+refs/heads/*:forge/*",
        "refs/heads/main:HEAD",
    ],
)
def test_commit_guard_fetch_with_unknowable_configured_destinations_refuses(tmp_path, spec):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    repo = _branch_flow_repo(tmp_path)
    _git(repo, "remote", "add", "other", str(tmp_path / "wt"))
    _git(repo, "config", "--replace-all", "remote.other.fetch", spec)
    for command in ("git fetch", "git fetch other", "git fetch --all --prune"):
        code, message = _flow_code(guard, command, repo, leased=(7, 9))
        assert code == 2 and "cannot be known before it runs" in message, (spec, command)


def test_commit_guard_fetch_with_standard_refspecs_stays_read_only(tmp_path, monkeypatch):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    repo = _branch_flow_repo(tmp_path)
    _git(repo, "remote", "add", "origin", str(tmp_path / "wt"))
    _git(repo, "config", "--add", "remote.origin.fetch", "+refs/tags/*:refs/tags/*")
    _git(repo, "config", "--add", "remote.origin.fetch", "^refs/heads/forge/issue-7")
    _git(repo, "config", "--add", "remote.origin.fetch", "refs/heads/main")
    wt = tmp_path / "wt"  # on forge/issue-9: a read-only fetch needs no lease there either
    _git(wt, "remote", "add", "origin", str(repo))
    for command in ("git fetch", "git fetch origin", "git fetch --all --prune", "git fetch -p"):
        assert _flow_code(guard, command, repo)[0] == 0, command
        assert _flow_code(guard, command, wt)[0] == 0, command
    # unreadable configuration is unknown, never "no destinations"
    assert guard._configured_fetch_refs(str(repo), ((), (), ()), False) == []
    monkeypatch.setattr(guard, "_read_config", lambda where, target: None)
    assert guard._configured_fetch_refs(str(repo), ((), (), ()), False) == [guard.UNKNOWN_REF]
    assert guard._configured_fetch_refs(str(repo), ((), (), ()), True) == [guard.UNKNOWN_REF]
    assert _flow_code(guard, "git fetch origin", repo)[0] == 2


# Codex 4239868019: GET repos/{id} follows a rename/transfer and answers the NEW full_name, which
# is what every run passes as $GITHUB_REPOSITORY.
def test_doctor_fails_a_policy_id_that_github_answers_under_a_new_name(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec(project_id="owner/old"))
    gh = _healthy_gh()
    info = json.loads(gh.answers["api repos/owner/comic"])
    gh.answers = {
        f"api repos/owner/old{key[len('api repos/owner/comic') :]}"
        if key.startswith("api repos/owner/comic")
        else key: value
        for key, value in gh.answers.items()
    }
    gh.answers["api repos/owner/old"] = json.dumps({**info, "full_name": "owner/new"})
    report = doctor(repo, "owner/old", "owner/agentic-sdlc", gh)
    check = next(c for c in report.checks if c.name == "policy project id matches the repository")
    assert not check.ok and not check.manual and not report.ok
    assert "renamed or transferred" in check.detail and '[project].id = "owner/new"' in check.detail
    # unread, or an answer without a full_name: unverified, never a pass
    gh.answers["api repos/owner/old"] = json.dumps(
        {k: v for k, v in info.items() if k != "full_name"}
    )
    report = doctor(repo, "owner/old", "owner/agentic-sdlc", gh)
    check = next(c for c in report.checks if c.name == "policy project id matches the repository")
    assert not check.ok and check.manual and "unverified" in check.detail


def test_cli_onboard_apply_writes_and_reports_the_new_name_of_a_renamed_repository(
    tmp_path, monkeypatch, capsys
):
    from agentic_sdlc import cli
    from agentic_sdlc.onboard import DoctorReport

    monkeypatch.setattr(cli, "canonical_project_id", lambda project: "owner/new")
    monkeypatch.setattr(cli, "apply_repo_settings", lambda spec, **k: {})
    monkeypatch.setattr(cli, "doctor", lambda *a, **k: DoctorReport())
    repo = _repo(tmp_path)
    args = ["onboard", "--visibility", "private", "--destination", str(repo)]
    args += ["--project-id", "owner/old"]
    args += ["--platform-repository", "owner/agentic-sdlc", "--platform-ref", SHA]
    args += ["--test", "pytest -q", "--default-branch", "main", "--apply"]
    assert cli.main([*args, "--output", str(tmp_path / "o.json")]) == 0
    assert 'id = "owner/new"' in (repo / "agentic-sdlc.toml").read_text()
    assert json.loads((tmp_path / "o.json").read_text())["project_id"] == "owner/new"
    assert "owner/old as owner/new (renamed or transferred)" in capsys.readouterr().err


# Codex 4239868034: a cloud-routine repository is READY only once the owner records the routine.
def test_confirm_cloud_routine_records_an_explicit_owner_confirmation(tmp_path, capsys):
    import tomllib

    from agentic_sdlc import cli
    from agentic_sdlc.onboard import cloud_routine_check, confirm_cloud_routine

    repo = _repo(tmp_path)
    write_onboarding(repo, spec(implementer="cloud-routine", runs_on=("ubuntu-latest",)))
    policy = repo / "agentic-sdlc.toml"
    before = policy.read_text()
    assert "cloud_routine_confirmed" not in before  # onboarding never confirms on its own
    assert not cloud_routine_check(tomllib.loads(before)).ok
    assert cloud_routine_check(tomllib.loads(before)).manual
    doc = (repo / "docs/forge/cloud-implementer.md").read_text()
    assert "sdlcctl confirm-cloud-routine" in doc
    assert cli.main(["confirm-cloud-routine", "--destination", str(repo)]) == 0
    after = policy.read_text()
    assert tomllib.loads(after)["automation"]["cloud_routine_confirmed"] is True
    assert after.replace("cloud_routine_confirmed = true\n", "", 1) == before  # nothing else
    assert cloud_routine_check(tomllib.loads(after)).ok
    confirm_cloud_routine(repo)  # idempotent
    assert policy.read_text() == after
    # only `true` itself confirms
    for value in ("false", '"yes"', "1"):
        policy.write_text(
            after.replace("cloud_routine_confirmed = true", f"cloud_routine_confirmed = {value}")
        )
        assert not cloud_routine_check(tomllib.loads(policy.read_text())).ok, value
        confirm_cloud_routine(repo)
        assert policy.read_text() == after, value
    # an Actions-mode policy has nothing to confirm
    actions = _repo(tmp_path / "actions")
    write_onboarding(actions, spec())
    with pytest.raises(OnboardError, match="not 'cloud-routine'"):
        confirm_cloud_routine(actions)
    assert cli.main(["confirm-cloud-routine", "--destination", str(actions)]) == 2
    assert "cloud_routine_confirmed" not in (actions / "agentic-sdlc.toml").read_text()


# Codex 4239868029: the cloud routine authorizes the approval labeler before it claims.
def test_cloud_routine_prompt_authorizes_the_labeler_before_claiming():
    files = render_onboarding(spec(implementer="cloud-routine", runs_on=("ubuntu-latest",)))
    doc = files["docs/forge/cloud-implementer.md"]
    authorize = doc.index("sdlcctl authorize-labeler --project owner/comic --issue <n>")
    assert authorize < doc.index("sdlcctl claim --project owner/comic")
    assert "--label implementation-approved" in doc
    assert "Proceed ONLY on exit 0" in doc and "do not claim" in doc


# Codex 4242373521: a fresh cloud environment has no `sdlcctl`. The routine doc carries the
# setup script that installs it at the workflows' pin, and step 0 refuses any other install.
def test_cloud_routine_doc_installs_the_pinned_sdlcctl_and_checks_it_first():
    files = render_onboarding(spec(implementer="cloud-routine", runs_on=("ubuntu-latest",)))
    doc = files["docs/forge/cloud-implementer.md"]
    install = (
        "python3 -m pip install --user --disable-pip-version-check "
        f"'agentic-sdlc @ git+https://github.com/owner/agentic-sdlc@{SHA}'"
    )
    assert "## Environment setup script" in doc and install in doc
    step0 = doc.index(f"0. Run `sdlcctl --version`. It must print `commit {SHA}`")
    assert step0 < doc.index("1. List open issues")
    assert "STOP: claim nothing" in doc
    assert doc.index("## Environment setup script") < doc.index("## Routine prompt")
    # the install source is the commit every generated workflow pins
    assert (
        f"owner/agentic-sdlc/.github/workflows/reusable-plan.yml@{SHA}"
        in (files[".github/workflows/agent-plan.yml"])
    )


def test_doctor_compares_the_routine_doc_at_the_installed_pin(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec(implementer="cloud-routine", runs_on=("ubuntu-latest",)))
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False)
    assert "managed files match the generated files" not in _failed(report)
    routine = repo / "docs/forge/cloud-implementer.md"
    routine.write_text(routine.read_text().replace(SHA, "1" * 40))  # a stale pin
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False)
    drift = _failed(report)["managed files match the generated files"].detail
    assert "cloud-implementer.md" in drift
    assert "setup script" in _failed(report)["cloud implementer routine is scheduled"].detail


def test_sdlcctl_version_names_the_installed_commit(monkeypatch, capsys):
    from importlib import metadata

    from agentic_sdlc import cli

    class Dist:
        version = "0.1.0"

        def read_text(self, name):
            assert name == "direct_url.json"
            return json.dumps({"url": "https://github.com/o/p", "vcs_info": {"commit_id": SHA}})

    monkeypatch.setattr(metadata, "distribution", lambda name: Dist())
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out == f"sdlcctl 0.1.0 commit {SHA}\n"
    Dist.read_text = lambda self, name: None  # an editable or sdist install records no commit
    assert cli.installed_version() == "sdlcctl 0.1.0 commit unknown"


# ---------------------------------------------------------------- review regressions (PR 139, 11)
# Codex review 5481591166.


# Codex 4239965192: `--root [BRANCH]` has ONE operand, the branch; every rebase form's branch
# operand is a destination needing its lease, and operands that cannot be told apart block.
@pytest.mark.parametrize(
    "command",
    [
        "git rebase --onto main --root forge/issue-7",
        "git rebase --root forge/issue-7",
        "git rebase --root --onto=main forge/issue-7",
        "git rebase -i --root forge/issue-7",
        "git rebase main forge/issue-7",
        "git rebase --onto main main forge/issue-7",
        "git rebase --keep-base main forge/issue-7",
        "git rebase --fork-point main forge/issue-7",
        "git rebase -i main forge/issue-7",
        "git rebase -S main forge/issue-7",  # -S takes an ATTACHED value only
        "git rebase --gpg-sign main forge/issue-7",
        "git rebase -r main forge/issue-7",
        "git rebase -Sabc -s ort -X ours main forge/issue-7",
        "git rebase -sort main forge/issue-7",
        "git rebase -C 3 main forge/issue-7",
        "git rebase main refs/heads/forge/issue-7",
        "git rebase -- main forge/issue-7",
    ],
)
def test_commit_guard_checks_every_rebase_branch_operand(tmp_path, command):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    repo = _branch_flow_repo(tmp_path)
    code, message = _flow_code(guard, command, repo)
    assert code == 2 and "issue #7" in message, (command, message)
    assert _flow_code(guard, command, repo, leased=(7,))[0] == 0, command


@pytest.mark.parametrize(
    "command",
    [
        "git rebase --ont main forge/issue-7",  # an abbreviation git expands: unknowable
        "git rebase --no-such-option main forge/issue-7",
        "git rebase main other forge/issue-7",  # too many operands: never guess
        "git rebase --root main forge/issue-7",
        "git rebase -Z main forge/issue-7",
        "git rebase --onto",
        "git rebase --continue forge/issue-7",
    ],
)
def test_commit_guard_blocks_unknowable_rebase_operands(tmp_path, command):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    repo = _branch_flow_repo(tmp_path)
    code, message = _flow_code(guard, command, repo, leased=(7, 9))  # every lease held
    assert code == 2 and "cannot be known before it runs" in message, (command, message)


def test_commit_guard_plain_rebases_stay_allowed(tmp_path):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    repo = _branch_flow_repo(tmp_path)
    _git(repo, "branch", "feature")
    for allowed in (
        "git rebase main",
        "git rebase --root",
        "git rebase -i --autosquash main",
        "git rebase --onto main main feature",
        "git rebase --root feature",
        "git rebase -S main feature",
        "git rebase --keep-base --fork-point main",
        "git rebase --abort",
    ):
        assert _flow_code(guard, allowed, repo)[0] == 0, allowed
    assert guard._rebase_branch(["--onto", "main", "--root", "forge/issue-7"]) == "forge/issue-7"
    assert guard._rebase_branch(["--root"]) is None
    assert guard._rebase_branch(["-S", "main", "x"]) == "x"
    assert guard._rebase_branch(["main"]) is None


# Codex 4239965189: the authorization must cover whoever applied the approval label LAST, not
# only the sender of the event that completed the label set.
def _events_page(*events):
    return json.dumps(
        [
            {"event": kind, "label": {"name": name}, "actor": {"login": login}}
            for kind, name, login in events
        ]
    )


def _run_approval(
    tmp_path, workflow, events_script, permissions, sender="writer", edits_script=None
):
    """Run the rendered authorization step with a fake gh answering the label events with
    `events_script`, each collaborator permission from `permissions` (login -> answer) and the
    issue's edit times with `edits_script` (default: never edited)."""
    perm = "".join(
        f'  *"/collaborators/{login}/permission"*) {answer} ;;\n'
        for login, answer in permissions.items()
    )
    script = _gh_events_prelude(events_script, edits_script) + (
        'for a in "$@"; do case "$a" in\n'
        f"{perm}"
        "  *) continue ;;\n"
        "esac; done\n"
        'echo unexpected "$*" >&2; exit 1\n'
    )
    import shutil
    import subprocess

    if not (shutil.which("bash") and shutil.which("jq")):
        pytest.skip("bash and jq are needed to run the generated authorization step")
    doc = yaml.safe_load(render_onboarding(spec())[f".github/workflows/{workflow}"])
    step = next(
        s
        for job in doc["jobs"].values()
        for s in job.get("steps") or []
        if s.get("id") == "authorize"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text("#!/usr/bin/env bash\n" + script)
    (bin_dir / "sleep").write_text("#!/usr/bin/env bash\nexit 0\n")
    for tool in ("gh", "sleep"):
        (bin_dir / tool).chmod(0o755)
    output = tmp_path / "output"
    output.write_text("")
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "SENDER": sender,
        "ACTOR": sender,
        "ISSUE_NUMBER": "7",
        "APPROVAL_LABEL": step["env"]["APPROVAL_LABEL"],
        "REQUIRED_LABELS": step["env"]["REQUIRED_LABELS"],
        "GITHUB_REPOSITORY": "owner/comic",
        "GITHUB_OUTPUT": str(output),
    }
    done = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return done, dict(line.split("=", 1) for line in output.read_text().splitlines() if "=" in line)


APPROVED = "implementation-approved"


def _print_events(*events) -> str:
    return f"printf '%s' '{json.dumps(list(events))}'; exit 0"


WRITE = 'echo \'{"permission": "write"}\'; exit 0'
TRIAGE = 'echo \'{"permission": "read", "role_name": "triage"}\'; exit 0'


def test_auto_implement_authorizes_the_approval_labeler_not_the_last_labeler(tmp_path):
    events = _events_page(
        ("labeled", "implementation-approved", "triager"),
        ("labeled", "claude-ready", "writer"),
        ("labeled", "human-review-required", "writer"),
    )
    done, out = _run_approval(
        tmp_path,
        "agent-auto-implement.yml",
        f"printf '%s' '{events}'; exit 0",
        {"writer": WRITE, "triager": TRIAGE},
    )
    assert done.returncode == 0, done.stderr
    assert out["authorized"] == "false" and out["approver"] == "triager"


@pytest.mark.parametrize(
    ("events", "approver"),
    [
        # the latest application counts, across pages and label-name case
        (
            [
                ("labeled", "implementation-approved", "triager"),
                ("unlabeled", "implementation-approved", "writer"),
                ("labeled", "Implementation-Approved", "maintainer"),
            ],
            "maintainer",
        ),
        ([("labeled", "implementation-approved", "writer")], "writer"),
    ],
)
def test_auto_implement_passes_the_verified_approver(tmp_path, events, approver):
    events = [
        ("labeled", "claude-ready", "writer"),
        ("labeled", "human-review-required", "writer"),
        *events,
    ]
    pages = " ".join(f"'{_events_page(e)}'" for e in events)  # one event per page
    done, out = _run_approval(
        tmp_path,
        "agent-auto-implement.yml",
        f"printf '%s\\n' {pages}; exit 0",
        {"writer": WRITE, "maintainer": WRITE, "triager": TRIAGE},
    )
    assert done.returncode == 0, done.stderr
    at = str(_epoch(EVENT_AT))
    assert out == {
        "authorized": "true",
        "approver": approver,
        "approved_at": at,
        "approved_latest_at": at,
    }


@pytest.mark.parametrize(
    "events_script",
    [
        "echo 'gh: Server Error (HTTP 502)' >&2; exit 1",  # unreadable history
        "printf '%s' '[]'; exit 0",  # the label never applied
        "printf '%s' '{\"message\": \"x\"}'; exit 0",
        _print_events({"event": "labeled", "label": None, "actor": {"login": "w"}}),
        _print_events({"event": "labeled", "label": {"name": APPROVED}, "actor": None}),
        _print_events({"event": "labeled", "label": {"name": APPROVED}, "actor": {"login": "a b"}}),
    ],
)
def test_auto_implement_fails_when_the_approver_cannot_be_verified(tmp_path, events_script):
    done, out = _run_approval(
        tmp_path, "agent-auto-implement.yml", events_script, {"writer": WRITE}
    )
    assert done.returncode == 1, (done.stdout, done.stderr)
    assert "authorized" not in out  # nothing decided: authorization_failed reports it
    assert "::error::" in done.stdout


def test_a_removed_approval_label_is_unauthorized(tmp_path):
    events = _events_page(
        ("labeled", "implementation-approved", "writer"),
        ("unlabeled", "implementation-approved", "writer"),
    )
    done, out = _run_approval(
        tmp_path,
        "agent-auto-implement.yml",
        f"printf '%s' '{events}'; exit 0",
        {"writer": WRITE},
    )
    assert done.returncode == 0, done.stderr
    assert out["authorized"] == "false"


def test_auto_plan_authorizes_the_ready_labeler(tmp_path):
    events = _events_page(
        ("labeled", "human-review-required", "writer"), ("labeled", "claude-ready", "outsider")
    )
    done, out = _run_approval(
        tmp_path,
        "agent-auto-plan.yml",
        f"printf '%s' '{events}'; exit 0",
        {"writer": WRITE, "outsider": "echo 'gh: Not Found (HTTP 404)' >&2; exit 1"},
    )
    assert done.returncode == 0, done.stderr
    assert out["authorized"] == "false" and out["approver"] == "outsider"


def test_reusable_calls_receive_the_verified_approver_as_trigger_actor():
    files = render_onboarding(spec(ready_label="go-plan"))
    impl = yaml.safe_load(files[".github/workflows/agent-auto-implement.yml"])
    plan = yaml.safe_load(files[".github/workflows/agent-auto-plan.yml"])
    manual = yaml.safe_load(files[".github/workflows/agent-implement.yml"])
    auth = impl["jobs"]["authorize"]
    assert auth["outputs"]["approver"] == "${{ steps.authorize.outputs.approver }}"
    assert auth["steps"][0]["env"]["APPROVAL_LABEL"] == "implementation-approved"
    assert plan["jobs"]["preflight"]["steps"][0]["env"]["APPROVAL_LABEL"] == "go-plan"
    assert impl["jobs"]["implement"]["with"]["trigger_actor"] == (
        "${{ needs.authorize.outputs.approver }}"
    )
    assert plan["jobs"]["plan"]["with"]["trigger_actor"] == (
        "${{ needs.preflight.outputs.approver }}"
    )
    # workflow_dispatch: the dispatcher is the actor, as before
    assert manual["jobs"]["implement"]["with"]["trigger_actor"] == "${{ github.actor }}"


def test_doctor_requires_the_generated_approval_label(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    assert _drift(repo) is None
    _edit_workflow(
        repo,
        "agent-auto-implement.yml",
        lambda d: d["jobs"]["authorize"]["steps"][0]["env"].update({"APPROVAL_LABEL": "x"}),
    )
    assert "agent-auto-implement.yml" in (_drift(repo) or "")


# ---------------------------------------------------------------- review regressions (PR 139, 8)
# Codex review 5481796241.


# Codex 4240170627: an archived repository is read-only -- claims, plan comments, publisher pushes
# and draft PRs all fail -- so it is never READY, and `--apply` refuses it before any mutation.
@pytest.mark.parametrize(
    ("fields", "manual"),
    [
        ({"archived": True}, False),
        ({"disabled": True}, False),
        ({"archived": True, "disabled": True}, False),
        ({"archived": None}, True),
        ({"disabled": None}, True),
        ({"archived": "false"}, True),
        ({"disabled": 0}, True),
    ],
)
def test_doctor_requires_a_writable_repository(tmp_path, fields, manual):
    from agentic_sdlc.onboard import REPO_WRITABLE_CHECK

    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _healthy_gh()
    info = json.loads(gh.answers["api repos/owner/comic"])
    for key, value in fields.items():
        if value is None:
            info.pop(key)
        else:
            info[key] = value
    gh.answers["api repos/owner/comic"] = json.dumps(info)
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    failed = _failed(report)
    assert set(failed) == {REPO_WRITABLE_CHECK}, report.render()
    assert failed[REPO_WRITABLE_CHECK].manual is manual
    assert not report.ok
    if "archived" in fields and fields["archived"] is True:
        assert "archived" in failed[REPO_WRITABLE_CHECK].detail
        assert "Unarchive" in failed[REPO_WRITABLE_CHECK].detail


def test_doctor_passes_the_writable_repository_check_only_on_explicit_false(tmp_path):
    from agentic_sdlc.onboard import REPO_WRITABLE_CHECK

    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", _healthy_gh())
    check = next(c for c in report.checks if c.name == REPO_WRITABLE_CHECK)
    assert check.ok and report.ok, report.render()


@pytest.mark.parametrize(
    "answer",
    [
        json.dumps({"full_name": "owner/comic", "archived": True, "disabled": False}),
        json.dumps({"full_name": "owner/comic", "archived": False, "disabled": True}),
        json.dumps({"full_name": "owner/comic"}),
        "not json",
    ],
)
def test_apply_refuses_an_archived_or_unconfirmed_repository_before_any_mutation(answer):
    gh = FakeGh({"api repos/owner/comic/rulesets": "[]", "api repos/owner/comic": answer})
    with pytest.raises(OnboardError, match="nothing was changed"):
        apply_repo_settings(spec(), gh, variables={"PUBLISHER_APP_CLIENT_ID": "Iv1"})
    assert all(c[0] == "api" and "-X" not in c for c, _ in gh.calls), gh.calls


def test_onboard_apply_refuses_an_archived_repository_before_writing_files():
    from agentic_sdlc.onboard import canonical_project_id

    archived = {"full_name": "owner/comic", "archived": True, "disabled": False}
    gh = FakeGh({"api repos/owner/comic": json.dumps(archived)})
    with pytest.raises(OnboardError, match="archived.*nothing was changed"):
        canonical_project_id("owner/comic", gh)
    archived["archived"] = False
    gh = FakeGh({"api repos/owner/comic": json.dumps(archived)})
    assert canonical_project_id("owner/comic", gh) == "owner/comic"


# Codex 4240170623: the contents API lists at most 1,000 entries of a directory, silently; the
# workflow set is enumerated through the Git Trees API, which lists a tree in full or says
# `truncated: true` -- and anything short of a complete, explicit listing fails closed.
_T_ROOT, _T_GITHUB, _T_WORKFLOWS = "a" * 40, "b" * 40, "c" * 40


def _tree_gh(workflow_entries: list[dict], *, truncated: object = False) -> FakeGh:
    gh = _public_gh()
    gh.answers[CONSUMER_COMMITS + "main"] = json.dumps(
        {"sha": "d" * 40, "commit": {"tree": {"sha": _T_ROOT}}}
    )
    gh.answers[CONSUMER_TREES + _T_ROOT] = json.dumps(
        {
            "tree": [{"path": ".github", "mode": "040000", "type": "tree", "sha": _T_GITHUB}],
            "truncated": False,
        }
    )
    gh.answers[CONSUMER_TREES + _T_GITHUB] = json.dumps(
        {
            "tree": [{"path": "workflows", "mode": "040000", "type": "tree", "sha": _T_WORKFLOWS}],
            "truncated": False,
        }
    )
    listing: dict = {"tree": workflow_entries}
    if truncated is not None:
        listing["truncated"] = truncated
    gh.answers[CONSUMER_TREES + _T_WORKFLOWS] = json.dumps(listing)
    return gh


def _wf_entry(name: str, mode: str = "100644") -> dict:
    return {"path": name, "mode": mode, "type": "blob", "sha": "e" * 40}


def test_remote_workflow_set_reads_past_the_contents_api_limit(tmp_path):
    from agentic_sdlc.onboard import fork_runner_exposure, remote_workflow_files

    repo = _repo(tmp_path)
    write_onboarding(repo, spec(fork_exposed=True))
    names = [f"w{index:04d}.yml" for index in range(1200)]
    files = {name: "on: push\njobs: {}\n" for name in names}
    files["w1150.yml"] = SELF_HOSTED_PR  # past the first 1,000 entries
    gh = _tree_gh([_wf_entry(n) for n in names])
    _remote_workflows(gh, files)
    # the contents API's listing stops at 1,000 entries without saying so
    gh.answers[CONSUMER_CONTENTS + ".github/workflows?ref=main"] = json.dumps(
        [{"name": n, "path": f".github/workflows/{n}", "type": "file"} for n in names[:1000]]
    )
    remote, why = remote_workflow_files(gh, "owner/comic", "main")
    assert remote is not None and len(remote) == 1200, why
    exposed = fork_runner_exposure(repo, gh, "owner/comic", "owner/agentic-sdlc", "main")
    assert any("w1150.yml:lint" in item for item in exposed), exposed


@pytest.mark.parametrize("truncated", [True, None, "false", 0])
def test_remote_workflow_set_fails_closed_on_a_truncated_tree(tmp_path, truncated):
    from agentic_sdlc.onboard import fork_runner_exposure, remote_workflow_files

    repo = _repo(tmp_path)
    write_onboarding(repo, spec(fork_exposed=True))
    gh = _tree_gh([_wf_entry("ci.yml")], truncated=truncated)
    remote, why = remote_workflow_files(gh, "owner/comic", "main")
    assert remote is None and "not listed in full" in why
    exposed = fork_runner_exposure(repo, gh, "owner/comic", "owner/agentic-sdlc", "main")
    assert any("unverifiable" in item and "fails closed" in item for item in exposed)
    check = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))[PUBLIC_CHECK]
    assert "unverifiable" in check.detail


def test_remote_workflow_set_fails_closed_at_the_tree_api_limit(monkeypatch):
    from agentic_sdlc import onboard

    monkeypatch.setattr(onboard, "_TREE_ENTRY_LIMIT", 3)
    gh = _tree_gh([_wf_entry(f"w{i}.yml") for i in range(3)])
    remote, why = onboard.remote_workflow_files(gh, "owner/comic", "main")
    assert remote is None and "API limit" in why


@pytest.mark.parametrize(
    ("key", "answer"),
    [
        (CONSUMER_COMMITS + "main", "not json"),
        (CONSUMER_COMMITS + "main", json.dumps({"commit": {"tree": {}}})),
        (CONSUMER_TREES + "a" * 40, "not json"),
        (CONSUMER_TREES + "a" * 40, json.dumps({"tree": [], "truncated": False})),
        (CONSUMER_TREES + "b" * 40, json.dumps({"tree": "x", "truncated": False})),
        (CONSUMER_TREES + "c" * 40, json.dumps({"tree": ["x"], "truncated": False})),
    ],
)
def test_remote_workflow_set_fails_closed_on_an_unreadable_tree(key, answer):
    from agentic_sdlc.onboard import remote_workflow_files

    gh = _tree_gh([_wf_entry("ci.yml")])
    gh.answers[key] = answer
    remote, _ = remote_workflow_files(gh, "owner/comic", "main")
    assert remote is None


def test_remote_workflow_set_skips_symlinks_and_directories():
    from agentic_sdlc.onboard import remote_workflow_files

    entries = [
        _wf_entry("ci.yml"),
        _wf_entry("link.yml", "120000"),
        {"path": "sub.yml", "mode": "040000", "type": "tree", "sha": "f" * 40},
    ]
    gh = _tree_gh(entries)
    _remote_workflows(gh, {"ci.yml": "on: push\njobs: {}\n"})
    remote, why = remote_workflow_files(gh, "owner/comic", "main")
    assert remote is not None and set(remote) == {"ci.yml"}, why


# ---------------------------------------------------------------- Codex 4240306498


def test_doctor_local_reports_an_unconfirmed_cloud_routine_not_ready(tmp_path):
    """Codex 4240306498: `doctor --local` returned before the routine-confirmation check, so a
    fresh cloud-routine repo with no confirmed worker read READY. The confirmation is local
    policy state: it is checked in local mode too."""
    from agentic_sdlc.onboard import confirm_cloud_routine

    repo = _repo(tmp_path)
    write_onboarding(repo, spec(implementer="cloud-routine", runs_on=("ubuntu-latest",)))
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False)
    failed = _failed(report)
    assert set(failed) == {"cloud implementer routine is scheduled"}, report.render()
    assert failed["cloud implementer routine is scheduled"].manual and not report.ok
    confirm_cloud_routine(repo)
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False)
    assert report.ok, report.render()
    # an Actions-mode repo has no routine to confirm
    other = _repo(tmp_path / "actions")
    write_onboarding(other, spec())
    local = doctor(other, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False)
    assert all(c.name != "cloud implementer routine is scheduled" for c in local.checks)


# ---------------------------------------------------------------- Codex 4240306502

PLANNER_CHECK_NAME = "policy planner matches the plan workflow"


@pytest.mark.parametrize("implementer", ["route", "claude", "codex", "cloud-routine"])
def test_generated_policy_and_guides_name_claude_as_the_planner(implementer):
    """Codex 4240306502: every generated plan caller runs the platform's reusable-plan.yml, whose
    plan job runs Claude Code -- the policy and the agent guides must say so, not Codex."""
    files = render_onboarding(spec(implementer=implementer))
    policy = tomllib.loads(files["agentic-sdlc.toml"])
    assert policy["agents"]["planner"] == "claude"
    assert policy["agents"]["reviewer"] == "codex"
    guide = files["AGENTS.md"]
    assert "Claude Code is the planner" in guide and "Codex is the independent reviewer" in guide
    assert "Codex is the primary architect, planner" not in guide
    plan = yaml.safe_load((PLATFORM_WORKFLOWS / "reusable-plan.yml").read_text())
    from agentic_sdlc.onboard import plan_workflow_agents

    assert plan_workflow_agents(plan) == {"claude"}


def test_doctor_fails_a_planner_that_does_not_match_the_plan_workflow(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False)
    assert report.checks and PLANNER_CHECK_NAME not in _failed(report)
    assert any(c.name == PLANNER_CHECK_NAME for c in report.checks)
    toml = repo / "agentic-sdlc.toml"
    toml.write_text(toml.read_text().replace('planner = "claude"', 'planner = "codex"'))
    for remote in (False, True):
        gh = _healthy_gh() if remote else FakeGh()
        report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh, remote=remote)
        failed = _failed(report)
        assert PLANNER_CHECK_NAME in failed and not report.ok, report.render()
        assert "planner = 'codex'" in failed[PLANNER_CHECK_NAME].detail
        assert "'claude'" in failed[PLANNER_CHECK_NAME].detail


def test_doctor_reads_the_planner_from_the_plan_workflow_at_the_pin(tmp_path):
    """Online, the planner is compared with the reusable-plan.yml actually fetched at the pin."""
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    text = (PLATFORM_WORKFLOWS / "reusable-plan.yml").read_text()
    swapped = re.sub(r"anthropics/claude-code-action@\S+", "openai/codex-action@" + SHA, text)
    assert swapped != text
    gh = _healthy_gh()
    gh.answers[PLATFORM_CONTENTS + ".github/workflows/reusable-plan.yml"] = _blob(swapped.encode())
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))
    assert PLANNER_CHECK_NAME in failed
    assert "runs 'codex'" in failed[PLANNER_CHECK_NAME].detail
    assert "at the pinned ref" in failed[PLANNER_CHECK_NAME].detail


# ---------------------------------------------------------------- Codex 4240306501


def _break_workflow(repo: Path, name: str, mutate) -> None:
    path = repo / ".github/workflows" / name
    doc = yaml.safe_load(path.read_text())
    mutate(doc)
    path.write_text(yaml.safe_dump(doc))


def _first_job(doc: dict) -> str:
    return next(iter(doc["jobs"]))


def _set_job(value):
    def mutate(doc):
        doc["jobs"][_first_job(doc)] = value

    return mutate


def _set_job_field(field, value):
    def mutate(doc):
        for job in doc["jobs"].values():
            job[field] = value

    return mutate


def _set_steps_items(value):
    def mutate(doc):
        for job in doc["jobs"].values():
            if isinstance(job.get("steps"), list) and job["steps"]:
                job["steps"][0] = value

    return mutate


_MALFORMED = [
    pytest.param(_set_job(None), id="job-null"),
    pytest.param(_set_job("x"), id="job-string"),
    pytest.param(_set_job(7), id="job-int"),
    pytest.param(_set_job(["a"]), id="job-list"),
    pytest.param(_set_job_field("steps", "x"), id="steps-string"),
    pytest.param(_set_job_field("steps", 7), id="steps-int"),
    pytest.param(_set_job_field("steps", None), id="steps-null"),
    pytest.param(_set_steps_items(None), id="step-null"),
    pytest.param(_set_steps_items("x"), id="step-string"),
    pytest.param(_set_job_field("with", "x"), id="with-string"),
    pytest.param(_set_job_field("secrets", 7), id="secrets-int"),
]


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("name", ["agent-plan.yml", "agent-implement.yml", "ci.yml"])
@pytest.mark.parametrize("mutate", _MALFORMED)
def test_doctor_completes_not_ready_on_scalar_or_null_jobs_and_steps(
    tmp_path, remote, name, mutate
):
    """Codex 4240306501: a scalar/null job crashed the workflow pin extraction with an
    AttributeError before doctor could report anything. Every malformed job, step list, step,
    `with:` or `secrets:` is now a FAIL: doctor completes and the repo is NOT READY."""
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    _break_workflow(repo, name, mutate)
    gh = _healthy_gh() if remote else FakeGh()
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh, remote=remote)
    assert not report.ok, report.render()
    report.render()  # renders without a traceback too


def test_caller_pin_skips_non_mapping_jobs():
    from agentic_sdlc.onboard import _caller_pin

    call = {"uses": f"owner/agentic-sdlc/.github/workflows/reusable-plan.yml@{SHA}"}
    doc = {"jobs": {"a": None, "b": "x", "c": 7, "d": ["a"], "e": call}}
    assert _caller_pin(doc, "owner/agentic-sdlc") == SHA
    assert _caller_pin({"jobs": {"a": "x"}}, "owner/agentic-sdlc") is None
    assert _caller_pin({"jobs": "x"}, "owner/agentic-sdlc") is None


# ---------------------------------------------------------------- review regressions (PR 139, 13)
# Codex review 5482060549.


# Codex 4240421142: git runs the default SSH transport (`ssh`) -- and the shell runs git itself --
# from PATH, so a command that changes PATH anywhere can run a program of its own choosing through
# an allowlisted git command. On an unleased issue branch it needs the lease.
@pytest.mark.parametrize(
    "command",
    [
        "export PATH=/tmp/bin:$PATH; git fetch origin",
        "export PATH=/tmp/bin:$PATH && git remote show origin",
        "PATH=/tmp/bin:$PATH; git fetch origin",
        "PATH+=:/tmp/bin; git fetch origin",
        'export "PATH=/tmp/bin:$PATH"; git fetch origin',
        "declare -x PATH=/tmp/bin; git fetch origin",
        "read -r PATH <<< /tmp/bin; git fetch origin",
        "for PATH in /tmp/bin; do git fetch origin; done",
        "declare -n p=PATH; p=/tmp/bin; git fetch origin",
        "path=(/tmp/bin $path); git fetch origin",
        "path+=(/tmp/bin); git fetch origin",
        "hash -p /tmp/bin/git git; git status",
        "export PATH=/tmp/bin:$PATH; git status",
        "bash -c 'export PATH=/tmp/bin:$PATH; git fetch origin'",
        "export SSH_ASKPASS=/tmp/x SSH_ASKPASS_REQUIRE=force; git fetch origin",
        "export LD_PRELOAD=/tmp/x.so; git status",
        "export GIT_SSH_COMMAND+=/tmp/x; git fetch origin",
        "env PATH=/tmp/bin git fetch origin",
        "PATH=/tmp/bin git fetch origin",
    ],
)
def test_commit_guard_path_changes_guard_allowlisted_git(helper_repo, command):
    guard, repo, _ = helper_repo
    assert guard.is_git_commit(command, cwd=str(repo)), command
    code, message = _guarded_decision(guard, repo, command)
    assert code == 2, message  # some forms are refused outright (an unknown ref or repository)


@pytest.mark.parametrize(
    "command",
    [
        "export PATH=/tmp/bin:$PATH; git fetch origin",
        "export PATH=/tmp/bin:$PATH; git status",
        "declare -x PATH=/tmp/bin; git remote show origin",
    ],
)
def test_commit_guard_path_changes_need_the_lease_only(helper_repo, command):
    guard, repo, git = helper_repo
    code, message = _guarded_decision(guard, repo, command)
    assert code == 2 and "no live lease exists" in message, message
    lease = {"session": "s1", "agent": "a", "expires": None}
    assert _guarded_decision(guard, repo, command, lease) == (0, "")
    git("switch", "-q", "-c", "main")
    assert _guarded_decision(guard, repo, command) == (0, "")


def test_commit_guard_unchanged_path_keeps_fetch_allowed(helper_repo):
    guard, repo, _ = helper_repo
    for command in ("git fetch origin", "git remote show origin", "git log --oneline -- paths"):
        assert not guard.is_git_commit(command, cwd=str(repo)), command
        assert _guarded_decision(guard, repo, command) == (0, "")


@pytest.mark.parametrize("entry", [".", "", "bin", "./tools"])
def test_commit_guard_relative_path_entries_guard_allowlisted_git(helper_repo, monkeypatch, entry):
    """A relative PATH entry resolves against the directory the command runs in (a `cd`, or a
    file the agent wrote there, picks the `ssh` git runs): every allowlisted command is guarded."""
    guard, repo, _ = helper_repo
    (repo / "sub").mkdir()
    monkeypatch.setenv("PATH", f"{entry}{os.pathsep}{os.environ['PATH']}")
    for command in ("git fetch origin", "cd sub && git fetch origin", "git status"):
        assert guard.is_git_commit(command, cwd=str(repo)), (entry, command)
        code, message = _guarded_decision(guard, repo, command)
        assert code == 2 and "is relative" in message, message


# Codex 4240421149: a merge group's SHA contains the queued PRs' code, fork PRs included, so a
# `merge_group` workflow is fork-exposed exactly like a `pull_request*` one.
SELF_HOSTED_MERGE_GROUP = (
    "on: merge_group\njobs:\n  queue-check:\n    runs-on: [self-hosted]\n"
    "    steps:\n      - run: make check\n"
)


def test_public_runner_safety_covers_merge_group_workflows(tmp_path):
    from agentic_sdlc.onboard import _pull_request_off_hosted, fork_runner_exposure

    repo = _repo(tmp_path)
    write_onboarding(repo, spec(fork_exposed=True))
    _wf(repo, "queue.yml", SELF_HOSTED_MERGE_GROUP)
    found = _pull_request_off_hosted(repo, "owner/agentic-sdlc", _platform_loader())
    assert any(f.startswith("queue.yml:queue-check") for f in found), found
    gh = _public_gh()
    exposed = fork_runner_exposure(
        repo, gh, "owner/comic", "owner/agentic-sdlc", "main", _platform_loader()
    )
    assert any("queue.yml:queue-check" in item for item in exposed), exposed
    check = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))[PUBLIC_CHECK]
    assert "queue.yml:queue-check" in check.detail
    # a hosted merge-group job is fine
    _wf(repo, "queue.yml", SELF_HOSTED_MERGE_GROUP.replace("[self-hosted]", "ubuntu-latest"))
    assert not _pull_request_off_hosted(repo, "owner/agentic-sdlc", _platform_loader())


def test_merge_group_runs_fork_code_even_with_fork_pr_workflows_disabled(tmp_path):
    """A private repository that only disables fork pull-request WORKFLOWS still merges fork PRs
    through its merge queue: its `merge_group` jobs must be GitHub-hosted too. Forking disabled
    outright leaves no fork code to run."""
    from agentic_sdlc.onboard import FORK_PR_WORKFLOWS_PRIVATE

    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    _wf(repo, "queue.yml", SELF_HOSTED_MERGE_GROUP)
    gh = _healthy_gh()
    info = json.loads(gh.answers["api repos/owner/comic"])
    info.update({"visibility": "private", "allow_forking": True})
    gh.answers["api repos/owner/comic"] = json.dumps(info)
    gh.answers[f"api repos/owner/comic/{FORK_PR_WORKFLOWS_PRIVATE}"] = json.dumps(
        {"run_workflows_from_fork_pull_requests": False}
    )
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    check = next(c for c in report.checks if c.name == PUBLIC_CHECK)
    assert not check.ok and "queue.yml:queue-check" in check.detail, check.detail
    info["allow_forking"] = False
    gh.answers["api repos/owner/comic"] = json.dumps(info)
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    assert next(c for c in report.checks if c.name == PUBLIC_CHECK).ok


# Codex 4240421144: whoever applied EACH label of the approval set most recently must hold write
# authority -- on the Actions path exactly as for `sdlcctl authorize-labeler` -- so a Triage user
# can neither approve nor complete the set, in either order.
@pytest.mark.parametrize(
    ("events", "authorized"),
    [
        # a writer approved FIRST, a Triage user completed the set later
        (
            [
                ("labeled", "implementation-approved", "writer"),
                ("labeled", "claude-ready", "writer"),
                ("labeled", "human-review-required", "triager"),
            ],
            "false",
        ),
        # a Triage user's prerequisite, then a writer's approval (the event's sender)
        (
            [
                ("labeled", "claude-ready", "triager"),
                ("labeled", "human-review-required", "writer"),
                ("labeled", "implementation-approved", "writer"),
            ],
            "false",
        ),
        # a writer re-applied the Triage user's label: its LATEST applier is the writer
        (
            [
                ("labeled", "claude-ready", "triager"),
                ("unlabeled", "claude-ready", "writer"),
                ("labeled", "Claude-Ready", "writer"),
                ("labeled", "human-review-required", "writer"),
                ("labeled", "implementation-approved", "writer"),
            ],
            "true",
        ),
        # a prerequisite removed since it was last applied
        (
            [
                ("labeled", "claude-ready", "writer"),
                ("labeled", "human-review-required", "writer"),
                ("labeled", "implementation-approved", "writer"),
                ("unlabeled", "human-review-required", "writer"),
            ],
            "false",
        ),
    ],
)
def test_auto_implement_authorizes_every_label_of_the_set(tmp_path, events, authorized):
    done, out = _run_approval(
        tmp_path,
        "agent-auto-implement.yml",
        f"printf '%s' '{_events_page(*events)}'; exit 0",
        {"writer": WRITE, "triager": TRIAGE},
    )
    assert done.returncode == 0, (done.stdout, done.stderr)
    assert out["authorized"] == authorized, (done.stdout, out)
    if authorized == "true":
        assert out["approver"] == "writer"


@pytest.mark.parametrize(
    "events",
    [
        # a prerequisite with no labeled event at all: unverifiable, the job fails
        [("labeled", "implementation-approved", "writer"), ("labeled", "claude-ready", "writer")],
        # a prerequisite's latest event names no valid actor
        [
            ("labeled", "implementation-approved", "writer"),
            ("labeled", "claude-ready", "writer"),
            ("labeled", "human-review-required", "a b"),
        ],
    ],
)
def test_auto_implement_fails_when_a_prerequisite_labeler_cannot_be_verified(tmp_path, events):
    done, out = _run_approval(
        tmp_path,
        "agent-auto-implement.yml",
        f"printf '%s' '{_events_page(*events)}'; exit 0",
        {"writer": WRITE},
    )
    assert done.returncode == 1, (done.stdout, done.stderr)
    assert "authorized" not in out and "::error::" in done.stdout


# Codex 4240421134: every `gh` call claim/renew/release/authorize-labeler/onboard/doctor make is
# bounded; a stalled `gh` fails closed (OnboardError -> exit 2) instead of hanging the session.
def test_run_gh_times_out_and_fails_closed(tmp_path, monkeypatch):
    import time

    from agentic_sdlc import onboard

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text("#!/bin/sh\nexec sleep 30\n")
    (bin_dir / "gh").chmod(0o755)
    # the resolver is what picks the executable (Codex 4241745403): hand it the stalling one
    monkeypatch.setattr(onboard, "trusted_gh", lambda: str(bin_dir / "gh"))
    monkeypatch.setenv(onboard.GH_TIMEOUT_ENV, "1")
    started = time.monotonic()
    with pytest.raises(OnboardError, match="did not answer within 1s"):
        onboard.run_gh(["api", "repos/owner/comic"])
    assert time.monotonic() - started < 15


@pytest.mark.parametrize(
    ("value", "expected"),
    [("", 60), ("5", 5), ("0", 60), ("-1", 60), ("nope", 60), ("99999", 60), ("inf", 60)],
)
def test_gh_timeout_is_always_finite(monkeypatch, value, expected):
    from agentic_sdlc import onboard

    monkeypatch.setenv(onboard.GH_TIMEOUT_ENV, value)
    assert onboard.gh_timeout() == expected
    seen = {}

    def fake_run(args, **kwargs):
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    import subprocess

    monkeypatch.setattr(onboard.subprocess, "run", fake_run)
    monkeypatch.setattr(onboard, "trusted_gh", lambda: "/trusted/gh")
    with pytest.raises(OnboardError, match="did not answer"):
        onboard.run_gh(["api", "x"])
    assert seen["timeout"] == expected


def test_lease_commands_exit_2_when_gh_stalls(monkeypatch):
    import subprocess

    from agentic_sdlc import cli, onboard

    def stalled(args, **kwargs):
        assert kwargs.get("timeout")
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(onboard.subprocess, "run", stalled)
    base = ["--project", "owner/comic", "--issue", "7"]
    session = ["--session", "s1"]
    for argv in (
        ["claim", *base, *session, "--agent", "a", "--branch", "forge/issue-7"],
        ["renew", *base, *session],
        ["release", *base, *session],
        ["authorize-labeler", *base, "--label", "implementation-approved"],
    ):
        assert cli.main(argv) == 2, argv


def test_every_subprocess_run_is_bounded():
    """No subprocess call in the package (hook templates included) may wait forever."""
    import ast

    root = Path(__file__).resolve().parents[1] / "src" / "agentic_sdlc"
    unbounded: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            owner = node.func.value
            if not (isinstance(owner, ast.Name) and owner.id == "subprocess"):
                continue
            where = f"{path.relative_to(root)}:{node.lineno}"
            if node.func.attr != "run":
                unbounded.append(f"{where}: subprocess.{node.func.attr} (use subprocess.run)")
            elif not any(k.arg == "timeout" for k in node.keywords):
                unbounded.append(f"{where}: subprocess.run without timeout=")
    assert not unbounded, unbounded


# ---- Codex 4240526381: git hidden in a shell script FILE is guarded like inline git.


def _script_guard_repos(tmp_path):
    """(guard, main checkout, issue-7 checkout, decide(command, cwd, lease) -> exit code)."""
    guard = _guard_module(tmp_path, "forge_commit_guard")
    main_repo, issue_repo = tmp_path / "main", tmp_path / "forge" / "issue-7"
    for path, branch in ((main_repo, "main"), (issue_repo, "forge/issue-7")):
        path.mkdir(parents=True)
        _git(path, "init", "-q", "-b", branch)
        _git(path, "commit", "-q", "--allow-empty", "-m", "init")
    from datetime import UTC, datetime, timedelta

    mine = {"agent": "claude-code", "session": "s1", "expires": datetime.now(UTC) + timedelta(1)}

    def decide(command, cwd, leased=False):
        branches = guard.commit_branches(command, str(cwd))
        payload = {
            "tool_name": "Bash",
            "session_id": "s1",
            "cwd": str(cwd),
            "tool_input": {"command": command},
        }
        lease = (lambda n: mine) if leased else (lambda n: None)
        return guard.decide(payload, branches, lease, lambda b: False)[0]

    return guard, main_repo, issue_repo, decide


def _script(path: Path, text: str, mode: int = 0o755) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


@pytest.mark.parametrize(
    "form",
    [
        "bash {s}",
        "sh {s}",
        "zsh {s}",
        "dash -e {s}",
        "ksh {s}",
        "bash -o pipefail {s}",
        "source {s}",
        ". {s}",
        "{s}",
        "bash < {s}",
        "env FOO=1 bash {s}",
    ],
)
def test_commit_guard_reads_a_script_file_that_commits_on_an_issue_branch(tmp_path, form):
    _, _, issue_repo, decide = _script_guard_repos(tmp_path)
    script = _script(
        tmp_path / "bin" / "do-commit.sh",
        "#!/bin/bash\n# don't forget the message\ngit add -A && \\\n  git commit -m x\n",
    )
    command = form.format(s=script)
    assert decide(command, issue_repo) == 2, command  # no lease: refused
    assert decide(command, issue_repo, leased=True) == 0, command  # this session's lease


def test_commit_guard_follows_git_c_inside_a_script_run_from_main(tmp_path):
    _, main_repo, issue_repo, decide = _script_guard_repos(tmp_path)
    script = _script(tmp_path / "c.sh", f"echo committing\ngit -C {issue_repo} commit -m x\n")
    _script(main_repo / "rel.sh", "git -C ../forge/issue-7 commit -m x\n")
    for command in (f"bash {script}", f"/bin/sh {script}", "bash rel.sh", "./rel.sh"):
        assert decide(command, main_repo) == 2, command  # issue 7's lease is needed
        assert decide(command, main_repo, leased=True) == 0, command
    # the same script committing in main itself needs no lease
    local = _script(tmp_path / "m.sh", f"git -C {main_repo} commit -m x\n")
    assert decide(f"bash {local}", issue_repo) == 0


def test_commit_guard_passes_a_script_without_git_and_its_shebang_twin(tmp_path):
    guard, main_repo, issue_repo, decide = _script_guard_repos(tmp_path)
    harmless = _script(tmp_path / "echo.sh", "#!/bin/sh\necho 'hello' # it's fine\nls -la\n")
    shebang = _script(tmp_path / "tool", "#!/usr/bin/env bash\ngit commit -m y\n")
    other = _script(tmp_path / "py-tool", "#!/usr/bin/env python3\nprint('git commit')\n")
    for cwd in (main_repo, issue_repo):
        assert decide(f"bash {harmless}", cwd) == 0
        assert decide(f"{harmless} && ls", cwd) == 0
    assert not guard.is_git_commit(f"bash {harmless}", cwd=str(issue_repo))
    # no .sh suffix: the `#!` line names a shell, so it is read too
    assert decide(str(shebang), issue_repo) == 2
    # a script that changes no branch does not make the git after it unknowable
    assert decide(f"bash {harmless} && git commit -m x", main_repo) == 0
    # not a shell script: out of scope (documented), like any other program
    assert decide(str(other), issue_repo) == 0


@pytest.mark.parametrize("kind", ["missing", "binary", "too-large", "directory", "fifo", "quote"])
def test_commit_guard_refuses_a_script_it_cannot_read_on_any_branch(tmp_path, kind):
    import os

    guard, main_repo, issue_repo, decide = _script_guard_repos(tmp_path)
    path = tmp_path / "s.sh"
    if kind == "binary":
        path.write_bytes(b"\x7fELF\x00\x00git commit")
    elif kind == "too-large":
        path.write_text("echo x\n" * (guard.MAX_SCRIPT_BYTES // 7 + 1))
    elif kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)  # never opened: reading one could hang the hook
    elif kind == "quote":
        path.write_text("echo 'unclosed\n")
    for command in (f"bash {path}", f"source {path}", f"{path}"):
        if kind == "fifo" and command == str(path):
            continue  # not a .sh/.bash path read as a script only by its `#!` line
        for cwd in (main_repo, issue_repo):
            assert guard.commit_branches(command, str(cwd)) == [guard.UNRESOLVED_REPO], command
            assert decide(command, cwd, leased=True) == 2, command
    msg = guard._decide_branch({"session_id": "s1"}, guard.UNRESOLVED_REPO, None, None)[1]
    assert "shell script file the guard could not read" in msg


def test_commit_guard_refuses_a_script_named_by_an_expansion(tmp_path):
    _, main_repo, _, decide = _script_guard_repos(tmp_path)
    _script(main_repo / "x.sh", "echo ok\n")
    for command in ('bash "$S"', "bash ~/x.sh", "source $(mktemp)", "bash x{,}.sh"):
        assert decide(command, main_repo, leased=True) == 2, command
    assert decide("bash x.sh", main_repo) == 0


def test_commit_guard_analyses_nested_scripts_up_to_the_depth_limit(tmp_path):
    guard, main_repo, issue_repo, decide = _script_guard_repos(tmp_path)
    inner = _script(tmp_path / "inner.sh", f"git -C {issue_repo} commit -m x\n")
    middle = _script(tmp_path / "middle.sh", f"echo start\nsh {inner}\n")
    outer = _script(tmp_path / "outer.sh", f"source {middle}\n")
    assert decide(f"bash {outer}", main_repo) == 2  # found two scripts down
    assert decide(f"bash {outer}", main_repo, leased=True) == 0
    # a harmless chain within the limit passes; one level deeper is refused unread
    chain = [_script(tmp_path / "d0.sh", "echo leaf\n")]
    for level in range(1, guard.MAX_SUBST_DEPTH + 2):
        chain.append(_script(tmp_path / f"d{level}.sh", f"bash {chain[-1]}\n"))
    within = chain[guard.MAX_SUBST_DEPTH - 1]
    assert decide(f"bash {within}", main_repo) == 0
    deeper = chain[guard.MAX_SUBST_DEPTH]
    assert guard.commit_branches(f"bash {deeper}", str(main_repo)) == [guard.UNRESOLVED_REPO]
    assert decide(f"bash {deeper}", main_repo, leased=True) == 2


def test_commit_guard_applies_command_line_rules_inside_a_script(tmp_path):
    guard, main_repo, issue_repo, decide = _script_guard_repos(tmp_path)
    cases = {
        "assembled.sh": 'G=git\n"$G" -C /elsewhere commit -m x\n',  # raw proof: unresolved
        "path.sh": "export PATH=/tmp/evil:$PATH\ngit fetch origin\n",  # taint: fetch guarded
        "dollar-args.sh": 'git "$@"\n',
    }
    for name, text in cases.items():
        script = _script(tmp_path / name, text)
        assert decide(f"bash {script}", issue_repo) == 2, name
    # a script that switches branch makes the git after it unknowable, as inline
    _script(tmp_path / "switch.sh", "git switch forge/issue-7\n")
    assert decide(f"bash {tmp_path / 'switch.sh'} && git commit -m x", main_repo) == 2
    # `find -exec ./x.sh` hands work to a script per file: unresolved
    assert guard.UNRESOLVED_REPO in guard.commit_branches(
        "find . -name '*.py' -exec ./fmt.sh {} ;", str(main_repo)
    )


# ---- Codex 4240526383: the SessionStart fetch writes remote-tracking refs only.


def test_session_start_fetch_never_updates_a_local_issue_branch(tmp_path, monkeypatch):
    import subprocess

    start = _guard_module(tmp_path, "forge_session_start")
    _guard_module(tmp_path, "forge_commit_guard")  # the sibling whose helper check it reuses
    upstream, local = tmp_path / "up", tmp_path / "local"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", "main")
    _git(upstream, "commit", "-q", "--allow-empty", "-m", "one")
    _git(tmp_path, "clone", "-q", str(upstream), str(local))
    _git(local, "branch", "forge/issue-7")
    _git(upstream, "commit", "-q", "--allow-empty", "-m", "two")
    # a hostile mapping: a plain `git fetch origin` would force-update the issue branch
    _git(
        local, "config", "--add", "remote.origin.fetch", "+refs/heads/main:refs/heads/forge/issue-7"
    )

    def rev(ref, cwd=local):
        return subprocess.run(
            ["git", "rev-parse", ref], cwd=cwd, capture_output=True, text=True, check=True
        ).stdout.strip()

    before, new_main = rev("refs/heads/forge/issue-7"), rev("refs/heads/main", upstream)
    monkeypatch.chdir(local)
    assert start.fetch_blocker() is None
    assert start.run(start.fetch_command()) is not None
    assert rev("refs/heads/forge/issue-7") == before  # untouched
    assert rev("refs/remotes/origin/main") == new_main  # the remote-tracking ref did move
    # the control: git's own default fetch would have moved it
    _git(local, "fetch", "-q", "origin")
    assert rev("refs/heads/forge/issue-7") == new_main


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("core.sshCommand", "/tmp/evil-ssh"),
        ("remote.origin.url", "ext::sh -c touch% /tmp/pwned"),
        ("credential.helper", "!/tmp/evil-helper"),
    ],
)
def test_session_start_skips_the_fetch_when_git_would_run_a_configured_program(
    tmp_path, monkeypatch, key, value
):
    import io

    start = _guard_module(tmp_path, "forge_session_start")
    _guard_module(tmp_path, "forge_commit_guard")
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "remote", "add", "origin", "https://example.invalid/x.git")
    _git(repo, "config", key, value)
    monkeypatch.chdir(repo)
    reason = start.fetch_blocker()
    assert reason and key.lower().split(".")[-1] in reason.lower()
    calls = []

    def run(cmd):
        calls.append(cmd)
        return "[]" if _is_issue_page(cmd) else ""

    monkeypatch.setattr(start, "run", run)
    monkeypatch.setattr(start, "_lease_lookup", lambda: {}.get)
    monkeypatch.setattr(start.sys, "stdin", io.StringIO("{}"))
    out = io.StringIO()
    monkeypatch.setattr(start.sys, "stdout", out)
    assert start.main() == 0  # a session start is never blocked
    assert not any(c[:2] == ["git", "fetch"] for c in calls)
    text = out.getvalue()
    assert "[Forge] Did not fetch origin:" in text and "could not fetch origin" in text


def test_session_start_fetch_command_is_explicit_and_validates_the_remote(tmp_path):
    start = _guard_module(tmp_path, "forge_session_start")
    cmd = start.fetch_command()
    assert cmd[-2:] == ["origin", "+refs/heads/*:refs/remotes/origin/*"]
    assert "--refmap=" in cmd and "--no-tags" in cmd and "--no-recurse-submodules" in cmd
    for bad in ("--upload-pack=x", "../up", "a b", "ext::x", "", "-o"):
        assert "not a plain name" in start.fetch_blocker(bad), bad


# ---- Codex 4240628982: removing an approval label cancels a queued or running automatic run.


def _actions_expr(text, action, label, labels, run_id=99):
    """Evaluate one generated `${{ }}`/`if:` expression for an `issues` event (the subset the
    generated callers use: ==, &&, ||, !, contains, format, null)."""
    expr = " ".join(str(text).split())
    if expr.startswith("${{"):
        expr = expr[3:-2]
    for old, new in (
        ("github.event.issue.labels.*.name", "LABELS"),
        ("github.event.issue.pull_request", "PR"),
        ("github.event.issue.number", "NUM"),
        ("github.event.label.name", "LABEL"),
        ("github.event.action", "ACTION"),
        ("github.run_id", "RUN"),
        ("&&", " and "),
        ("||", " or "),
        ("!contains", "not contains"),
        ("null", "None"),
    ):
        expr = expr.replace(old, new)
    scope = {
        "contains": lambda items, item: item in items,
        "format": lambda pattern, *args: pattern.format(*args),
        "LABELS": labels,
        "LABEL": label,
        "ACTION": action,
        "PR": None,
        "NUM": 7,
        "RUN": run_id,
    }
    return eval(expr, scope)  # noqa: S307 - test-only evaluation of generated literals


APPROVAL_SET = [IMPLEMENTATION_LABEL, "claude-ready", "human-review-required"]


def test_auto_implement_label_removal_cancels_the_authorized_run():
    rendered = render_onboarding(spec())[".github/workflows/agent-auto-implement.yml"]
    doc = yaml.safe_load(rendered)
    assert doc[True]["issues"]["types"] == ["labeled", "unlabeled", "edited", "closed"]
    group, cancel = doc["concurrency"]["group"], doc["concurrency"]["cancel-in-progress"]
    condition = doc["jobs"]["authorize"]["if"]
    shared = "forge-auto-implement-7"
    for label in APPROVAL_SET:
        # applying an approval label joins the issue's group and queues (cancels nothing)
        assert _actions_expr(group, "labeled", label, APPROVAL_SET) == shared
        assert _actions_expr(cancel, "labeled", label, APPROVAL_SET) is False
        assert _actions_expr(condition, "labeled", label, APPROVAL_SET) is True
        # removing one cancels what is queued or running there, and starts nothing itself
        rest = [other for other in APPROVAL_SET if other != label]
        assert _actions_expr(group, "unlabeled", label, rest) == shared
        assert _actions_expr(cancel, "unlabeled", label, rest) is True
        assert _actions_expr(condition, "unlabeled", label, rest) is False
        # even a payload that still lists it never starts work on `unlabeled`
        assert _actions_expr(condition, "unlabeled", label, APPROVAL_SET) is False
    # Codex 4241196649: editing the title or body cancels too, and starts nothing itself
    assert _actions_expr(group, "edited", None, APPROVAL_SET) == shared
    assert _actions_expr(cancel, "edited", None, APPROVAL_SET) is True
    assert _actions_expr(condition, "edited", None, APPROVAL_SET) is False
    # Codex 4242753923: closing the issue cancels the implementation, and the `closed` run
    # starts nothing even though a closed issue keeps its approval labels
    assert _actions_expr(group, "closed", None, APPROVAL_SET) == shared
    assert _actions_expr(cancel, "closed", None, APPROVAL_SET) is True
    assert _actions_expr(condition, "closed", None, APPROVAL_SET) is False
    # the lease's own `in-progress` label (and any other) never cancels the work
    for action in ("labeled", "unlabeled"):
        for label in ("in-progress", "bug"):
            assert _actions_expr(group, action, label, APPROVAL_SET) == f"{shared}-run-99"
            assert _actions_expr(condition, action, label, APPROVAL_SET) is False


def test_auto_plan_label_removal_cancels_planning_and_restoring_it_restarts_planning():
    """Codex 4241196642: removing either planning label cancels planning, and re-adding it (the
    other still present) starts it again; the shared group serializes the two `labeled` runs of
    an issue opened with both labels (neither cancels the other: `cancel-in-progress` is false
    for `labeled`), and `dedupe` keeps the later one from planning twice."""
    rendered = render_onboarding(spec())[".github/workflows/agent-auto-plan.yml"]
    doc = yaml.safe_load(rendered)
    assert doc[True]["issues"]["types"] == ["labeled", "unlabeled", "edited", "closed"]
    group, cancel = doc["concurrency"]["group"], doc["concurrency"]["cancel-in-progress"]
    condition = doc["jobs"]["preflight"]["if"]
    labels = ["claude-ready", "human-review-required"]
    shared = "forge-auto-plan-7"
    for label in labels:
        assert _actions_expr(group, "labeled", label, labels) == shared
        assert _actions_expr(cancel, "labeled", label, labels) is False
        assert _actions_expr(condition, "labeled", label, labels) is True  # restored: plans
        rest = [other for other in labels if other != label]
        assert _actions_expr(condition, "labeled", label, [label]) is False  # set incomplete
        assert _actions_expr(group, "unlabeled", label, rest) == shared
        assert _actions_expr(cancel, "unlabeled", label, rest) is True
        assert _actions_expr(condition, "unlabeled", label, rest) is False
    # Codex 4241196649: an edit cancels planning too
    assert _actions_expr(group, "edited", None, labels) == shared
    assert _actions_expr(cancel, "edited", None, labels) is True
    assert _actions_expr(condition, "edited", None, labels) is False
    # Codex 4242925431: closing the issue cancels planning, and the `closed` run starts
    # nothing even though a closed issue keeps its planning labels
    assert _actions_expr(group, "closed", None, labels) == shared
    assert _actions_expr(cancel, "closed", None, labels) is True
    assert _actions_expr(condition, "closed", None, labels) is False
    # the jobs that do work need `preflight`, which the `closed` run skips
    for job_name in ("dedupe", "plan"):
        needs = doc["jobs"][job_name]["needs"]
        assert "preflight" in ([needs] if isinstance(needs, str) else needs), job_name
    # implementation approval stops planning; other labels are in groups of their own
    assert _actions_expr(condition, "labeled", "claude-ready", [*labels, IMPLEMENTATION_LABEL]) is (
        False
    )
    assert _actions_expr(group, "unlabeled", "in-progress", labels) != shared
    assert _actions_expr(group, "labeled", "bug", labels) != shared


@pytest.mark.parametrize("name", ["agent-auto-plan.yml", "agent-auto-implement.yml"])
def test_doctor_requires_the_label_removal_cancellation(tmp_path, name):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    path = repo / ".github/workflows" / name
    text = path.read_text()
    original = re.search(r"cancel-in-progress: .*?\}\}", text, re.DOTALL).group(0)
    weaker = [
        "cancel-in-progress: false",
        # Codex 4241196649: an edit must cancel too
        "cancel-in-progress: ${{ github.event.action == 'unlabeled' }}",
    ]
    # and closing the issue (implementation: Codex 4242753923; planning: Codex 4242925431)
    weaker.append(
        "cancel-in-progress: "
        "${{ github.event.action == 'unlabeled' || github.event.action == 'edited' }}"
    )
    for weak in weaker:
        weakened = text.replace(original, weak)
        assert weakened != text
        path.write_text(weakened)
        failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
        detail = failed["managed workflows match the generated templates"].detail
        assert name in detail and "concurrency" in detail
    # a `closed` event that does not join the issue's group cancels nothing (Codex 4242925431)
    joined = "github.event.action == 'edited' ||\n    github.event.action == 'closed') &&"
    assert joined in text
    path.write_text(text.replace(joined, "github.event.action == 'edited') &&"))
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    assert name in failed["managed workflows match the generated templates"].detail
    types_line = re.search(r"types: \[[^\]]*\]", text).group(0)
    weaker_types = [
        "types: [labeled]",
        "types: [labeled, unlabeled]",
        "types: [labeled, unlabeled, edited]",
    ]
    for types in weaker_types:
        path.write_text(text.replace(types_line, types))
        failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
        assert name in failed["managed workflows match the generated templates"].detail


def test_cloud_routine_reauthorizes_right_before_publishing():
    doc = render_onboarding(spec(implementer="cloud-routine", runs_on=("ubuntu-latest",)))[
        "docs/forge/cloud-implementer.md"
    ]
    claim = doc.index("1b. Claim it")
    # Codex 4241839918 / 4242264478: the LAST gate, after check-diff and the lease check, is one
    # command that re-checks the request AND re-authorizes the labels
    others = [doc.index(f"4{step}. ") for step in "abc"]
    final = doc.index("4d. The final gate, LAST, immediately before pushing")
    publish = doc.index("5. Push the checked commit at once")
    assert claim < max(others) < final < publish
    assert "4e. " not in doc  # no second check after the final gate
    step = _step(doc, "4d. The final gate", "5. Push the checked commit")
    # ONE command decides (the other is the release on failure)
    assert step.count("`sdlcctl check-request") == 1 and "authorize-labeler" not in step
    assert "--authorize-labels" in step
    assert "Proceed to step 5 ONLY on exit 0" in step
    assert "do NOT push or open a PR" in step
    assert "run nothing between this check and the push" in step
    assert "ONE command" in " ".join(doc.split()).split("Suggested routine prompt")[0]


def test_authorization_failure_report_names_a_removed_label():
    rendered = render_onboarding(spec())[".github/workflows/agent-auto-implement.yml"]
    assert "a run is cancelled when an approval label is removed" in rendered


# ---- Codex 4240628980: a bare command PATH resolves to a shell script is read like `./x.sh`.


def _path_bin(tmp_path, monkeypatch, name="bin"):
    directory = tmp_path / name
    directory.mkdir(exist_ok=True)
    monkeypatch.setenv("PATH", f"{directory}{os.pathsep}{os.environ['PATH']}")
    return directory


@pytest.mark.parametrize(
    "shebang", ["#!/bin/sh", "#!/bin/bash", "#!/usr/bin/env bash", "#!/usr/bin/env -S bash -e"]
)
def test_commit_guard_reads_a_bare_command_that_is_a_shell_script_on_path(
    tmp_path, monkeypatch, shebang
):
    guard, main_repo, issue_repo, decide = _script_guard_repos(tmp_path)
    bindir = _path_bin(tmp_path, monkeypatch)
    _script(bindir / "do-commit", f"{shebang}\ngit add -A && git commit -m x\n")
    _script(bindir / "ship", f"{shebang}\ngit -C {issue_repo} commit -m x\n")
    for command in ("do-commit", "do-commit && ls", "env FOO=1 do-commit", "ls | do-commit"):
        assert decide(command, issue_repo) == 2, command  # no lease: refused
        assert decide(command, issue_repo, leased=True) == 0, command
    assert decide("ship", main_repo) == 2  # its `-C` names issue 7
    assert decide("ship", main_repo, leased=True) == 0


def test_commit_guard_passes_bare_commands_out_of_scope_or_without_git(tmp_path, monkeypatch):
    guard, main_repo, issue_repo, decide = _script_guard_repos(tmp_path)
    bindir = _path_bin(tmp_path, monkeypatch)
    _script(bindir / "pytool", "#!/usr/bin/env python3\nimport os\nos.system('git commit')\n")
    _script(bindir / "harmless", "#!/bin/sh\n# runs the tests\necho 'it''s fine'\nls -la\n")
    _script(bindir / "unreadable-ok", "#!/bin/sh\necho 'unclosed\n")  # no git anywhere
    _script(bindir / "not-exec", "#!/bin/sh\ngit commit -m x\n", mode=0o644)
    for command in ("pytool", "harmless", "harmless && ls", "unreadable-ok", "not-exec"):
        assert decide(command, issue_repo) == 0, command
        assert not guard.is_git_commit(command, cwd=str(issue_repo)), command
    # the first match on PATH is what the shell runs: a later git script is never reached
    later = _path_bin(tmp_path, monkeypatch, "later")
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{later}{os.pathsep}/usr/bin:/bin")
    _script(later / "harmless", "#!/bin/sh\ngit commit -m x\n")
    assert decide("harmless", issue_repo) == 0
    # builtins and keywords are never looked up on PATH
    _script(bindir / "cd", "#!/bin/sh\ngit commit -m x\n")
    assert decide("cd .", issue_repo) == 0


def test_commit_guard_follows_a_git_free_wrapper_into_the_script_it_runs(tmp_path, monkeypatch):
    guard, _, issue_repo, decide = _script_guard_repos(tmp_path)
    bindir = _path_bin(tmp_path, monkeypatch)
    _script(bindir / "inner-tool", "#!/bin/sh\ngit commit -m x\n")
    _script(bindir / "outer", '#!/bin/sh\nset -e\ninner-tool "$@"\n')
    _script(tmp_path / "lib" / "helpers", "do_it() { git commit -m x; }\n", mode=0o644)
    _script(bindir / "sourcing", f"#!/bin/sh\n. {tmp_path / 'lib' / 'helpers'}\ndo_it\n")
    for command in ("outer", "sourcing"):
        assert decide(command, issue_repo) == 2, command
        assert decide(command, issue_repo, leased=True) == 0, command


def test_commit_guard_refuses_an_unparseable_bare_script_that_names_git(tmp_path, monkeypatch):
    guard, main_repo, issue_repo, decide = _script_guard_repos(tmp_path)
    bindir = _path_bin(tmp_path, monkeypatch)
    _script(bindir / "broken", "#!/bin/sh\ngit status\necho 'unclosed\n")
    for cwd in (main_repo, issue_repo):
        assert guard.commit_branches("broken", str(cwd)) == [guard.UNRESOLVED_REPO]
        assert decide("broken", cwd, leased=True) == 2


def test_commit_guard_resolves_bare_commands_under_the_path_the_command_sets(tmp_path, monkeypatch):
    guard, main_repo, issue_repo, decide = _script_guard_repos(tmp_path)
    hidden = tmp_path / "hidden"
    _script(hidden / "deploy", "#!/bin/sh\ngit commit -m x\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    for command in (
        f"PATH={hidden}:$PATH deploy",
        f'export PATH="{hidden}:$PATH"; deploy',
        'export PATH="$HOME/hidden:$PATH" && deploy',
        "PATH=~/hidden:$PATH deploy",
        f"declare -x PATH={hidden}; deploy",
    ):
        assert decide(command, issue_repo) == 2, command
        assert decide(command, issue_repo, leased=True) == 0, command
    # a script that sets PATH and runs the bare name is followed the same way
    wrapper = _script(tmp_path / "w.sh", f"PATH={hidden}:$PATH\ndeploy\n")
    assert decide(f"bash {wrapper}", issue_repo) == 2
    # a PATH this guard cannot compute leaves every bare program unresolved, on any branch
    for command in (
        "export PATH=$(mktemp -d):$PATH; deploy",
        "read PATH; ls",
        "hash -p /tmp/x ls; ls",
        "path[1]=/tmp/x; ls",
        f"PATH+=:{hidden}; deploy",  # `NAME+=` is not an assignment to the parser: unproven
    ):
        assert guard.UNRESOLVED_REPO in guard.commit_branches(command, str(main_repo)), command
    # reading PATH, or printing/searching the word, changes nothing
    for command in ('echo "PATH is $PATH"; ls', "grep -rn PATH . ; ls", "printenv PATH"):
        assert decide(command, main_repo) == 0, command


def test_commit_guard_resolves_each_bare_command_once_per_check(tmp_path, monkeypatch):
    guard, _, issue_repo, _ = _script_guard_repos(tmp_path)
    bindir = _path_bin(tmp_path, monkeypatch)
    _script(bindir / "harmless", "#!/bin/sh\necho ok\n")
    calls = []
    real = guard._shell_shebang
    monkeypatch.setattr(guard, "_shell_shebang", lambda p: calls.append(p) or real(p))
    assert not guard.is_git_commit("harmless; harmless && harmless | harmless", str(issue_repo))
    assert calls.count(str(bindir / "harmless")) == 1


# ---- Codex 4240628986: confirm-cloud-routine never writes through a link.


def test_confirm_cloud_routine_refuses_a_symlinked_or_irregular_policy(tmp_path):
    from agentic_sdlc.onboard import confirm_cloud_routine

    repo = _repo(tmp_path)
    write_onboarding(repo, spec(implementer="cloud-routine", runs_on=("ubuntu-latest",)))
    policy = repo / "agentic-sdlc.toml"
    outside = tmp_path / "outside.toml"
    outside.write_text(policy.read_text())  # a VALID cloud-routine policy, outside the repo
    before = outside.read_bytes()
    policy.unlink()
    policy.symlink_to(outside)
    with pytest.raises(OnboardError, match="symlink"):
        confirm_cloud_routine(repo)
    assert outside.read_bytes() == before and policy.is_symlink()
    policy.unlink()
    policy.mkdir()
    with pytest.raises(OnboardError, match="not a regular file"):
        confirm_cloud_routine(repo)
    policy.rmdir()
    with pytest.raises(OnboardError, match="absent"):
        confirm_cloud_routine(repo)
    # a regular policy is replaced atomically (a new inode), its mode kept
    policy.write_bytes(before)
    policy.chmod(0o640)
    inode = policy.stat().st_ino
    assert confirm_cloud_routine(repo) == policy
    assert "cloud_routine_confirmed = true" in policy.read_text()
    assert policy.stat().st_ino != inode and (policy.stat().st_mode & 0o777) == 0o640
    assert outside.read_bytes() == before
    assert not [p for p in repo.iterdir() if p.name.endswith(".tmp")]


# ---- Codex 4240839925: a required context reported by more than one job is ambiguous.


def _required_rules(*contexts):
    checks = [{"context": c, "integration_id": 15368} for c in contexts]
    return [{"type": "required_status_checks", "parameters": {"required_status_checks": checks}}]


_TEST_JOB = "jobs:\n  test:\n    runs-on: ubuntu-latest\n    steps: [{run: 'true'}]\n"


def test_a_second_pull_request_workflow_with_a_test_job_makes_the_required_check_ambiguous(
    tmp_path,
):
    from agentic_sdlc.onboard import (
        ambiguous_context,
        effective_rule_problems,
        legacy_protection_problems,
        workflow_check_contexts,
    )

    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    workflows = repo / ".github/workflows"
    unique = workflow_check_contexts(repo, "main")
    assert "test" in unique.pull_request and ambiguous_context("test", unique) is None
    assert effective_rule_problems(_required_rules("test"), unique) == []

    (workflows / "other.yml").write_text("on: pull_request\n" + _TEST_JOB)
    produced = workflow_check_contexts(repo, "main")
    why = ambiguous_context("test", produced)
    # the managed ci.yml counts as one of the producers
    assert why and "2 jobs on pull_request (ci.yml, other.yml)" in why
    problems = effective_rule_problems(_required_rules("test"), produced)
    assert len(problems) == 1 and "reported by 2 jobs on pull_request" in problems[0]
    legacy = legacy_protection_problems(
        {"required_status_checks": {"checks": [{"context": "test", "app_id": 15368}]}}, produced
    )
    assert any("reported by 2 jobs on pull_request" in p for p in legacy)
    # a context only one job reports stays fine next to the ambiguous one
    assert ambiguous_context("lint-only", produced) is None


def test_duplicate_required_contexts_are_judged_per_triggering_event(tmp_path):
    from agentic_sdlc.onboard import ambiguous_context, workflow_check_contexts

    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    (workflows / "pr.yml").write_text("on: pull_request\n" + _TEST_JOB)
    (workflows / "queue.yml").write_text("on: merge_group\n" + _TEST_JOB)
    split = workflow_check_contexts(tmp_path, "main")
    assert "test" in split.pull_request and "test" in split.merge_group
    assert ambiguous_context("test", split) is None  # one producer per event

    (workflows / "queue2.yml").write_text("on: merge_group\n" + _TEST_JOB)
    queued = workflow_check_contexts(tmp_path, "main")
    why = ambiguous_context("test", queued)
    assert why and "2 jobs on merge_group (queue.yml, queue2.yml)" in why
    assert "on pull_request" not in why


def test_two_jobs_of_one_workflow_with_the_same_name_are_two_producers(tmp_path):
    from agentic_sdlc.onboard import ambiguous_context, workflow_check_contexts

    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "on: pull_request\njobs:\n"
        "  unit:\n    name: test\n    runs-on: ubuntu-latest\n    steps: [{run: 'true'}]\n"
        "  e2e:\n    name: test\n    runs-on: ubuntu-latest\n    steps: [{run: 'true'}]\n"
    )
    produced = workflow_check_contexts(tmp_path, "main")
    assert "2 jobs on pull_request (ci.yml, ci.yml)" in (ambiguous_context("test", produced) or "")
    # a disabled workflow reports nothing, so it produces nothing either
    (workflows / "dup.yml").write_text("on: pull_request\n" + _TEST_JOB)
    inactive = workflow_check_contexts(tmp_path, "main", inactive=frozenset({"dup.yml"}))
    assert inactive.pull_request_producers["test"] == ("ci.yml", "ci.yml")


# ---- Codex 4240952099: a matrix name that omits an axis renders the SAME name for several
# combinations; each combination is its own job, so each is a producer of that context.


def test_matrix_combinations_sharing_a_rendered_name_are_separate_producers(tmp_path):
    from agentic_sdlc.onboard import ambiguous_context, workflow_check_contexts

    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text(
        "on: pull_request\njobs:\n"
        "  test:\n    name: test-${{ matrix.os }}\n    runs-on: ubuntu-latest\n"
        "    strategy:\n      matrix:\n        os: [ubuntu]\n        python: ['3.11', '3.12']\n"
        "    steps: [{run: 'true'}]\n"
    )
    produced = workflow_check_contexts(tmp_path, "main")
    assert "test-ubuntu" in produced.pull_request
    assert produced.pull_request_producers["test-ubuntu"] == ("ci.yml", "ci.yml")
    why = ambiguous_context("test-ubuntu", produced) or ""
    assert "2 jobs on pull_request (ci.yml, ci.yml)" in why

    # every axis in the name: one producer per distinct context, nothing ambiguous
    (workflows / "ci.yml").write_text(
        "on: pull_request\njobs:\n"
        "  test:\n    name: test-${{ matrix.os }}-${{ matrix.python }}\n"
        "    runs-on: ubuntu-latest\n"
        "    strategy:\n      matrix:\n        os: [ubuntu]\n        python: ['3.11', '3.12']\n"
        "    steps: [{run: 'true'}]\n"
    )
    distinct = workflow_check_contexts(tmp_path, "main")
    assert ambiguous_context("test-ubuntu-3.11", distinct) is None
    assert distinct.pull_request_producers["test-ubuntu-3.12"] == ("ci.yml",)


# ---- Codex 4240952121: doctor never follows a managed path through a link, even to an
# identical file inside the checkout (the target is an unmanaged path a patch may change).


@pytest.mark.parametrize(
    "managed",
    [
        ".claude/hooks/forge_commit_guard.py",
        "agentic-sdlc.toml",
        "AGENTS.md",
        ".github/workflows/ci.yml",
    ],
)
def test_doctor_rejects_in_repository_symlinks_for_managed_files(tmp_path, managed):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    assert doctor(repo, "owner/comic", "owner/agentic-sdlc", _healthy_gh(), remote=False).ok
    path = repo / managed
    target = repo / "tools" / path.name
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(path.read_bytes())  # identical content, unprotected location
    path.unlink()
    path.symlink_to(target)
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", _healthy_gh(), remote=False)
    assert not report.ok and "NOT READY" in report.render()
    detail = _failed(report)["required files present"].detail
    assert f"{managed} is a symlink" in detail


def test_doctor_rejects_a_symlinked_parent_of_managed_files(tmp_path):
    import shutil

    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    hooks = repo / ".claude/hooks"
    shutil.copytree(hooks, repo / "tools/hooks")
    shutil.rmtree(hooks)
    hooks.symlink_to(repo / "tools/hooks")
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", _healthy_gh(), remote=False)
    assert not report.ok
    assert ".claude/hooks is a symlink" in _failed(report)["required files present"].detail


# ---- Codex 4240952107: a WRITABLE absolute PATH entry searched before the program git runs
# lets a command drop its own `ssh` (or pager, or git) there without ever mentioning PATH.


def _system_dir(tmp_path, monkeypatch, names=("ssh", "less", "man")):
    """A PATH directory holding the real git and stub `names`, which the test makes read-only
    (the suite's `os.access` reports every other directory under tmp_path as writable)."""
    import shutil

    system = tmp_path / "system"
    system.mkdir()
    (system / "git").symlink_to(shutil.which("git"))
    for name in names:
        stub = system / name
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
    read_only = {str(system), *(str(system / n) for n in names)}
    access = os.access

    def fake(path, mode, *args, **kwargs):
        if mode & os.W_OK and not isinstance(path, int) and os.fspath(path) in read_only:
            return False
        return access(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "access", fake)
    return system


def test_commit_guard_writable_path_entry_before_ssh_guards_fetch(helper_repo, monkeypatch):
    guard, repo, _ = helper_repo
    system = _system_dir(repo.parent, monkeypatch)
    drop = repo.parent / "drop"
    drop.mkdir()
    # Every program resolves in the read-only directory before the writable one: still allowed.
    monkeypatch.setenv("PATH", f"{system}{os.pathsep}{drop}")
    for command in ("git fetch origin", "git remote show origin", "git status", "git log"):
        assert not guard.is_git_commit(command, cwd=str(repo)), command
        assert _guarded_decision(guard, repo, command) == (0, "")
    # The writable directory first: `cp evil drop/ssh && git fetch origin` would run that ssh.
    monkeypatch.setenv("PATH", f"{drop}{os.pathsep}{system}")
    for command in ("git fetch origin", "cp evil drop/ssh && git fetch origin", "git status"):
        assert guard.is_git_commit(command, cwd=str(repo)), command
        code, message = _guarded_decision(guard, repo, command)
        assert code == 2 and "no live lease exists" in message, message
        assert f"writable PATH entry {drop} precedes" in message, message
    lease = {"session": "s1", "agent": "a", "expires": None}
    assert _guarded_decision(guard, repo, "git fetch origin", lease) == (0, "")


def test_commit_guard_writable_entry_matters_only_for_programs_it_precedes(
    helper_repo, monkeypatch
):
    guard, repo, _ = helper_repo
    system = _system_dir(repo.parent, monkeypatch, names=("less", "man"))  # no ssh installed
    drop = repo.parent / "drop"
    drop.mkdir()
    monkeypatch.setenv("PATH", f"{system}{os.pathsep}{drop}")
    # git and the pager resolve before `drop`: a read-only local command stays allowed ...
    assert _guarded_decision(guard, repo, "git log --oneline") == (0, "")
    # ... but `ssh` resolves nowhere, so a file created in `drop` would be the one git runs.
    code, message = _guarded_decision(guard, repo, "git fetch origin")
    assert code == 2 and f"writable PATH entry {drop} precedes ssh" in message, message


def test_commit_guard_missing_path_entry_under_a_writable_parent_guards(helper_repo, monkeypatch):
    guard, repo, _ = helper_repo
    system = _system_dir(repo.parent, monkeypatch)
    missing = repo.parent / "later" / "bin"  # `mkdir -p later/bin && cp evil later/bin/ssh`
    monkeypatch.setenv("PATH", f"{missing}{os.pathsep}{system}")
    code, message = _guarded_decision(guard, repo, "git fetch origin")
    assert code == 2 and f"writable PATH entry {missing} precedes" in message, message


def test_commit_guard_writable_resolved_program_guards(helper_repo, monkeypatch):
    guard, repo, _ = helper_repo
    system = _system_dir(repo.parent, monkeypatch, names=("less", "man"))
    ssh = system / "ssh"  # in the read-only directory, but the file itself is writable
    ssh.write_text("#!/bin/sh\nexit 0\n")
    ssh.chmod(0o755)
    monkeypatch.setenv("PATH", str(system))
    assert _guarded_decision(guard, repo, "git status") == (0, "")
    code, message = _guarded_decision(guard, repo, "git fetch origin")
    assert code == 2 and f"ssh resolves to the writable file {ssh}" in message, message


def test_commit_guard_writable_git_exec_path_guards(helper_repo, monkeypatch):
    """Git searches its exec path before PATH for every program it runs."""
    guard, repo, _ = helper_repo
    system = _system_dir(repo.parent, monkeypatch)
    monkeypatch.setenv("PATH", str(system))
    assert _guarded_decision(guard, repo, "git fetch origin") == (0, "")
    exec_dir = repo.parent / "git-core"
    exec_dir.mkdir()
    real = guard._git_in

    def git_in(where, target, *args):
        if args == ("--exec-path",):
            return 0, f"{exec_dir}\n"
        return real(where, target, *args)

    monkeypatch.setattr(guard, "_git_in", git_in)
    code, message = _guarded_decision(guard, repo, "git fetch origin")
    assert code == 2 and f"writable git exec path {exec_dir} precedes" in message, message
    unreadable = {("--exec-path",): (-1, "")}
    monkeypatch.setattr(guard, "_git_in", lambda w, t, *a: unreadable.get(a) or real(w, t, *a))
    code, message = _guarded_decision(guard, repo, "git status")
    assert code == 2 and "exec path could not be read" in message, message


def test_commit_guard_configured_pager_found_after_a_writable_entry_guards(
    helper_repo, monkeypatch
):
    guard, repo, git = helper_repo
    system = _system_dir(repo.parent, monkeypatch)
    drop = repo.parent / "drop"
    drop.mkdir()
    monkeypatch.setenv("PATH", f"{system}{os.pathsep}{drop}")
    assert _guarded_decision(guard, repo, "git log") == (0, "")
    git("config", "core.pager", "more")  # a plain pager, but `more` resolves only past `drop`
    code, message = _guarded_decision(guard, repo, "git log")
    assert code == 2 and f"writable PATH entry {drop} precedes more" in message, message


# ---- Codex 4241050279: a PATH-QUALIFIED git program (`/tmp/git status`, `./git`, `~/bin/git`)
# is not found by a PATH search, so it was never vetted: a `git` script dropped anywhere ran as
# an allowlisted read and committed on an issue branch another session leases.


def _fake_git(directory):
    """A `git` script that commits through the real git, whatever subcommand it is given."""
    import shutil

    directory.mkdir(parents=True, exist_ok=True)
    fake = directory / "git"
    fake.write_text(f"#!/bin/sh\nexec {shutil.which('git')} commit --allow-empty -qm evil\n")
    fake.chmod(0o755)
    return fake


@pytest.mark.parametrize(
    "command",
    [
        "FAKE status",
        "FAKE log --oneline",
        "./git status",
        "bin/git status",
        "./bin/../git status",
        "~/bin/git status",
        "env FAKE status",
        "command FAKE diff",
        "xargs FAKE log < args.txt",
    ],
)
def test_commit_guard_refuses_a_path_qualified_git_that_is_not_the_system_git(
    helper_repo, monkeypatch, command
):
    guard, repo, _ = helper_repo
    system = _system_dir(repo.parent, monkeypatch)
    monkeypatch.setenv("PATH", str(system))
    fake = _fake_git(repo.parent / "tmpbin")
    _fake_git(repo)
    _fake_git(repo / "bin")
    _fake_git(Path(os.environ["HOME"]) / "bin")
    command = command.replace("FAKE", str(fake))
    assert guard.is_git_commit(command, cwd=str(repo)), command
    branches = guard.commit_branches(command, str(repo))
    assert guard.UNRESOLVED_REPO in branches, (command, branches)
    # Another session's live lease does not matter, and neither does holding the lease.
    for lease in ({"session": "other", "agent": "a", "expires": None}, {"session": "s1"}):
        code, message = _guarded_decision(guard, repo, command, lease)
        assert code == 2, command
        assert "path-qualified git that is not the system's own git" in message, message


def _read_only_chain(monkeypatch, *paths):
    """Make `paths` and every directory above them read-only to the suite's `os.access`."""
    read_only = set()
    for path in paths:
        current = os.path.abspath(os.fspath(path))
        while True:
            read_only.add(current)
            if os.path.dirname(current) == current:
                break
            current = os.path.dirname(current)
    access = os.access

    def fake(path, mode, *args, **kwargs):
        if mode & os.W_OK and not isinstance(path, int) and os.fspath(path) in read_only:
            return False
        return access(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "access", fake)


def test_commit_guard_path_qualified_git_must_be_the_same_unwritable_file(helper_repo, monkeypatch):
    import shutil

    guard, repo, _ = helper_repo
    system = _system_dir(repo.parent, monkeypatch)
    real = os.path.realpath(shutil.which("git"))
    monkeypatch.setenv("PATH", os.path.dirname(real))
    lease = {"session": "other", "agent": "a", "expires": None}
    # The system git by its own path: allowed, as before -- also when only the PATH lookup (no
    # known system location) vouches for it.
    for known in (guard.SYSTEM_GIT_PATHS, ()):
        monkeypatch.setattr(guard, "SYSTEM_GIT_PATHS", known)
        for command in (f"{real} status", f"{real} log --grep commit"):
            assert not guard.is_git_commit(command, cwd=str(repo)), command
            assert _guarded_decision(guard, repo, command, lease) == (0, ""), command
    # A link to it from a directory whose chain is read-only is the same file: allowed.
    _read_only_chain(monkeypatch, system / "git")
    assert _guarded_decision(guard, repo, f"{system}/git diff", lease) == (0, "")
    # The same real git, linked from a directory this user can write: refused (the link or
    # the directory can be swapped for another file before the shell runs it).
    writable = repo.parent / "linkdir"
    writable.mkdir()
    (writable / "git").symlink_to(real)
    code, message = _guarded_decision(guard, repo, f"{writable}/git status", {"session": "s1"})
    assert code == 2 and f"writable directory {writable}" in message, message
    # A read-only directory chain holding a DIFFERENT program named git: refused.
    other = repo.parent / "other"
    other.mkdir()
    impostor = other / "git"
    impostor.write_text("#!/bin/sh\nexit 0\n")
    impostor.chmod(0o755)
    _read_only_chain(monkeypatch, impostor)
    code, message = _guarded_decision(guard, repo, f"{impostor} status", {"session": "s1"})
    assert code == 2 and "not the git on a non-writable PATH" in message, message
    # A fake git that would switch branches is unknown to the branch-change tracker too.
    assert guard._changes_branch(f"{impostor} status", 0, str(repo))
    assert not guard._changes_branch(f"{real} status", 0, str(repo))


def test_commit_guard_path_qualified_fake_git_cannot_commit_under_another_lease(
    helper_repo, monkeypatch
):
    """The reported case end to end: `/tmp/git status` whose script commits."""
    import subprocess

    guard, repo, _ = helper_repo
    system = _system_dir(repo.parent, monkeypatch)
    monkeypatch.setenv("PATH", str(system))
    fake = _fake_git(repo.parent / "tmp")
    command = f"{fake} status"
    lease = {"session": "other", "agent": "a", "expires": None}
    code, _ = _guarded_decision(guard, repo, command, lease)
    assert code == 2
    # (What the hook prevented: the script really commits.)
    before = subprocess.run(["git", "rev-list", "--count", "--all"], cwd=repo, text=True,
                            capture_output=True).stdout  # fmt: skip
    subprocess.run([str(fake), "status"], cwd=repo, check=True)
    after = subprocess.run(["git", "rev-list", "--count", "--all"], cwd=repo, text=True,
                           capture_output=True).stdout  # fmt: skip
    assert before != after


# ---- Codex 4241050285: doctor checked only that forbidden_paths was a renderable list, so a
# policy that dropped `.forge/**` and `.claude/**` was READY while evaluate_diff let patches
# change the executor registry and the commit-guard hook.


def test_doctor_requires_the_generated_forbidden_paths_to_stay_enforced(tmp_path):
    from agentic_sdlc.onboard import BASE_FORBIDDEN, FORBIDDEN_BASELINE_CHECK
    from agentic_sdlc.policy import evaluate_diff, load_policy

    repo = _repo(tmp_path)
    write_onboarding(repo, spec(forbidden_paths=("vendor/**",)))
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False)
    assert report.ok, report.render()
    baseline = next(c for c in report.checks if c.name == FORBIDDEN_BASELINE_CHECK)
    assert baseline.ok and str(len(BASE_FORBIDDEN)) in baseline.detail
    toml = repo / "agentic-sdlc.toml"
    toml.write_text(
        toml.read_text().replace('  ".forge/**",\n', "").replace('  ".claude/**",\n', "")
    )
    policy = load_policy(toml)
    assert ".forge/**" not in policy.forbidden_paths  # what the reviewer saw enforced
    assert evaluate_diff((".forge/executors.json",), 1, 0, policy).allowed  # the hole
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False)
    assert not report.ok and "NOT READY" in report.render()
    failed = _failed(report)
    assert set(failed) == {FORBIDDEN_BASELINE_CHECK}, report.render()
    detail = failed[FORBIDDEN_BASELINE_CHECK].detail
    assert ".forge/**" in detail and ".claude/**" in detail and "vendor/**" not in detail
    # Restored by onboard: READY again. A project-specific entry may still be removed: only the
    # generated baseline is mandatory.
    write_onboarding(repo, spec(), force=True)
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False)
    assert report.ok, report.render()


# ---- Codex 4241050282: `doctor --local` returned before any runner-target validation, so a
# caller whose `runs-on` and reusable `runs_on` name a runner GitHub does not host was READY.


def _retarget_plan(repo, labels_json):
    """agent-plan.yml's job runs-on AND the runs_on it hands reusable-plan.yml, rewritten."""
    plan = repo / ".github/workflows/agent-plan.yml"
    text = plan.read_text()
    text = text.replace('runs-on: ["self-hosted", "linux", "x64"]', f"runs-on: {labels_json}")
    escaped = json.dumps(json.dumps(json.loads(labels_json), separators=(",", ":")))
    text = text.replace(
        'runs_on: "[\\"self-hosted\\",\\"linux\\",\\"x64\\"]"', f"runs_on: {escaped}"
    )
    plan.write_text(text)


def test_local_doctor_fails_a_runner_github_does_not_host(tmp_path):
    from agentic_sdlc.onboard import LOCAL_RUNNER_CHECK

    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False)
    assert report.ok, report.render()
    assert next(c for c in report.checks if c.name == LOCAL_RUNNER_CHECK).ok
    _retarget_plan(repo, '"ubuntu-99.04"')  # the reported case
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False)
    assert not report.ok and "NOT READY" in report.render()
    failed = _failed(report)
    assert not failed[LOCAL_RUNNER_CHECK].manual
    assert "ubuntu-99.04" in failed[LOCAL_RUNNER_CHECK].detail
    assert "agent-plan.yml:plan > reusable-plan.yml" in failed[LOCAL_RUNNER_CHECK].detail
    assert "Forge workflows run on Linux runners" in failed


@pytest.mark.parametrize(
    ("labels", "check", "manual"),
    [
        ('"ubuntu-slim"', "runners allow the workflows' job timeouts", False),
        ('"macos-latest"', "Forge workflows run on Linux runners", False),
        ('"windows-latest"', "workflows target Unix runners", False),
        (
            '["gpu-box", "linux"]',
            "runner labels are recognised (availability is checked remotely)",
            True,
        ),
        ('["self-hosted", "linux", "ubuntu-99.04"]', None, None),
        ('"ubuntu-24.04"', None, None),
    ],
)
def test_local_doctor_applies_the_non_api_runner_rules(tmp_path, labels, check, manual):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    _retarget_plan(repo, labels)
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False)
    if check is None:
        assert report.ok, report.render()
        return
    failed = _failed(report)
    assert check in failed, report.render()
    assert failed[check].manual is manual


def test_local_doctor_checks_ci_and_unresolvable_runner_targets(tmp_path):
    from agentic_sdlc.onboard import LOCAL_RUNNER_CHECK

    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    ci = repo / ".github/workflows/ci.yml"
    original = ci.read_text()
    ci.write_text(original.replace('runs-on: ["self-hosted", "linux", "x64"]', "runs-on: macos-99"))
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    assert "macos-99" in failed[LOCAL_RUNNER_CHECK].detail
    ci.write_text(
        original.replace('runs-on: ["self-hosted", "linux", "x64"]', "runs-on: ubuntu-slim")
    )
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    assert "ubuntu-slim" in failed["ci.yml test job runs on an available runner"].detail
    ci.write_text(original.replace('runs-on: ["self-hosted", "linux", "x64"]', "runs-on: macos-15"))
    assert doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False).ok
    ci.write_text(original)
    _retarget_plan(repo, '"ubuntu-latest"')
    plan = repo / ".github/workflows/agent-plan.yml"
    plan.write_text(
        plan.read_text().replace('runs_on: "\\"ubuntu-latest\\""', "runs_on: ${{ vars.X }}")
    )
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    assert "workflow runner targets are verifiable" in failed


# ---------------------------------------------------------------- Codex review 5482912640
# Codex 4241196649: on the Actions path too, an approval covers the title and body it saw. The
# title (`renamed` events) and the body (GraphQL lastEditedAt / userContentEdits) must have been
# last edited before the latest application of EACH label of the set; otherwise the run is
# unauthorized, and an edit time that cannot be read FAILS the job.
def _stamped_events(*events) -> str:
    """Label (and `renamed`) events at minute 1, 2, 3, ... of 2026-10-01."""
    rows = []
    for minute, (kind, name, login) in enumerate(events, start=1):
        row = {"event": kind, "actor": {"login": login}, "created_at": _minute(minute)}
        if kind != "renamed":
            row["label"] = {"name": name}
        rows.append(row)
    return json.dumps(rows)


def _minute(minute: int) -> str:
    return f"2026-10-01T00:{minute:02d}:00Z"


def _edits_raw(answer: dict) -> str:
    return "printf '%s' '" + json.dumps(answer) + "'"


def _edits_answer(last_edited=None, edits=()) -> str:
    issue = {"lastEditedAt": last_edited, "userContentEdits": {"nodes": list(edits)}}
    return "printf '%s' '" + json.dumps({"data": {"repository": {"issue": issue}}}) + "'"


IMPLEMENT_SET_APPLIED = (
    ("labeled", "claude-ready", "writer"),
    ("labeled", "human-review-required", "writer"),
    ("labeled", "implementation-approved", "writer"),
)


@pytest.mark.parametrize("workflow", ["agent-auto-implement.yml", "agent-auto-plan.yml"])
@pytest.mark.parametrize(
    ("extra", "edits", "authorized"),
    [
        ((), _edits_answer(), "true"),  # never edited
        ((), _edits_answer(_minute(0)), "true"),  # edited before every label was applied
        ((), _edits_answer(_minute(9)), "false"),  # body edited after the approval
        ((), _edits_answer(_minute(2)), "false"),  # after a prerequisite was applied
        ((), _edits_answer(None, [{"editedAt": _minute(9)}]), "false"),  # newest edit entry
        ((("renamed", "", "author"),), _edits_answer(), "false"),  # title edited after
    ],
)
def test_actions_authorization_binds_the_approval_to_the_issue_content(
    tmp_path, workflow, extra, edits, authorized
):
    done, out = _run_approval(
        tmp_path,
        workflow,
        f"printf '%s' '{_stamped_events(*IMPLEMENT_SET_APPLIED, *extra)}'; exit 0",
        {"writer": WRITE},
        edits_script=edits,
    )
    assert done.returncode == 0, (done.stdout, done.stderr)
    assert out["authorized"] == authorized, (done.stdout, out)
    if authorized == "false":
        assert "was edited after its approval labels were applied" in done.stdout
        assert "re-apply" in done.stdout


@pytest.mark.parametrize(
    "edits",
    [
        'echo "gh: Server Error (HTTP 502)" >&2; exit 1',
        "printf '%s' 'not json'",
        'printf \'%s\' \'{"errors": [{"message": "boom"}]}\'',
        _edits_raw({"data": {"repository": {"issue": {"userContentEdits": {"nodes": []}}}}}),
        _edits_raw({"data": {"repository": {"issue": {"lastEditedAt": None}}}}),
        _edits_answer("yesterday"),
        _edits_answer(None, [{"editedAt": None}]),
    ],
)
def test_actions_authorization_fails_when_the_edit_time_is_unreadable(tmp_path, edits):
    done, out = _run_approval(
        tmp_path,
        "agent-auto-implement.yml",
        f"printf '%s' '{_stamped_events(*IMPLEMENT_SET_APPLIED)}'; exit 0",
        {"writer": WRITE},
        edits_script=edits,
    )
    assert done.returncode == 1, (done.stdout, done.stderr)
    assert "authorized" not in out and "::error::" in done.stdout


def test_actions_authorization_fails_when_a_label_time_is_unreadable(tmp_path):
    rows = json.loads(_stamped_events(*IMPLEMENT_SET_APPLIED))
    rows[-1]["created_at"] = None
    done, out = _run_approval(
        tmp_path,
        "agent-auto-implement.yml",
        f"printf '%s' '{json.dumps(rows)}'; exit 0",
        {"writer": WRITE},
    )
    assert done.returncode == 1, (done.stdout, done.stderr)
    assert "authorized" not in out and "::error::" in done.stdout


# Codex 4241196642: duplicate planning is suppressed by the `dedupe` job: a plan comment posted by
# github-actions[bot] after the approval was last applied means the approval is already planned.
def _run_dedupe(tmp_path, comments_script, approved_at):
    import shutil
    import subprocess

    if not (shutil.which("bash") and shutil.which("jq")):
        pytest.skip("bash and jq are needed to run the generated dedupe step")
    doc = yaml.safe_load(render_onboarding(spec())[".github/workflows/agent-auto-plan.yml"])
    job = doc["jobs"]["dedupe"]
    assert job["runs-on"] == "ubuntu-latest" and job["permissions"] == {"issues": "read"}
    assert job["needs"] == "preflight"
    assert job["if"] == "needs.preflight.outputs.authorized == 'true'"
    step = job["steps"][0]
    # Codex 4241327876: dedupe reads the NEWEST latest label application, not the earliest.
    assert step["env"]["APPROVED_AT"] == "${{ needs.preflight.outputs.approved_latest_at }}"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(
        "#!/usr/bin/env bash\n"
        'jqexpr=""; prev=""\n'
        'for a in "$@"; do [ "$prev" = "--jq" ] && jqexpr="$a"; prev="$a"; done\n'
        'case "$*" in *"/issues/7/comments"*) ;; *) echo "unexpected $*" >&2; exit 1 ;; esac\n'
        f'out="$( {comments_script} )" || exit $?\n'
        'printf \'%s\\n\' "$out" | jq -r "$jqexpr"\n'
    )
    (bin_dir / "sleep").write_text("#!/usr/bin/env bash\nexit 0\n")
    for tool in ("gh", "sleep"):
        (bin_dir / tool).chmod(0o755)
    output = tmp_path / "output"
    output.write_text("")
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "ISSUE_NUMBER": "7",
        "APPROVED_AT": str(approved_at),
        "GITHUB_REPOSITORY": "owner/comic",
        "GITHUB_OUTPUT": str(output),
    }
    done = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return done, output.read_text()


def _comment(login, body, minute):
    return {"user": {"login": login}, "body": body, "created_at": _minute(minute)}


PLAN_BODY = "## Agentic SDLC plan\n\n1. do it"


@pytest.mark.parametrize(
    ("comments", "fresh"),
    [
        ([], "true"),
        ([_comment("github-actions[bot]", PLAN_BODY, 2)], "true"),  # a plan of an older approval
        ([_comment("github-actions[bot]", PLAN_BODY, 9)], "false"),  # this approval is planned
        ([_comment("mallory", PLAN_BODY, 9)], "true"),  # only the platform's own plan counts
        ([_comment("github-actions[bot]", "Agent planning did not complete", 9)], "true"),
        ([_comment("github-actions[bot]", None, 9)], "true"),
    ],
)
def test_auto_plan_dedupe_skips_an_already_planned_approval(tmp_path, comments, fresh):
    script = f"printf '%s' '{json.dumps(comments)}'"
    done, output = _run_dedupe(tmp_path, script, _epoch(_minute(5)))
    assert done.returncode == 0, (done.stdout, done.stderr)
    assert output.strip() == f"fresh={fresh}"


@pytest.mark.parametrize(
    ("script", "approved_at"),
    [
        ('echo "gh: Server Error (HTTP 502)" >&2; exit 1', _epoch(_minute(5))),
        ("printf '%s' '[]'", ""),  # no approval time from the preflight
    ],
)
def test_auto_plan_dedupe_fails_closed(tmp_path, script, approved_at):
    done, output = _run_dedupe(tmp_path, script, approved_at)
    assert done.returncode == 1, (done.stdout, done.stderr)
    assert "fresh=" not in output and "::error::" in done.stdout


# Codex 4241327876: the preflight outputs the EARLIEST latest label application (`approved_at`,
# for the content-staleness check: an edit must precede every label) AND the NEWEST
# (`approved_latest_at`, for dedupe: a plan covers the approval only if it follows every current
# label application). Re-applying ONE planning label after a plan must therefore plan again.
def test_auto_plan_reapplying_one_label_after_a_plan_plans_again(tmp_path):
    doc = yaml.safe_load(render_onboarding(spec())[".github/workflows/agent-auto-plan.yml"])
    assert doc["jobs"]["preflight"]["outputs"]["approved_latest_at"] == (
        "${{ steps.authorize.outputs.approved_latest_at }}"
    )
    step = next(s for s in doc["jobs"]["preflight"]["steps"] if s.get("id") == "authorize")
    labels = json.loads(step["env"]["REQUIRED_LABELS"])
    assert len(labels) >= 2
    for sub in ("authorize", "dedupe", "dedupe-min"):
        (tmp_path / sub).mkdir()
    rows = [
        {
            "event": "labeled",
            "label": {"name": name},
            "actor": {"login": "writer"},
            "created_at": _minute(minute),
        }
        for minute, name in enumerate(labels, start=1)
    ]
    plan_minute = len(labels) + 1  # the plan of the first approval
    rows += [
        {
            "event": "unlabeled",
            "label": {"name": labels[0]},
            "actor": {"login": "writer"},
            "created_at": _minute(plan_minute + 1),
        },
        {
            "event": "labeled",
            "label": {"name": labels[0]},
            "actor": {"login": "writer"},
            "created_at": _minute(plan_minute + 2),
        },
    ]
    done, out = _run_approval(
        tmp_path / "authorize",
        "agent-auto-plan.yml",
        f"printf '%s' '{json.dumps(rows)}'; exit 0",
        {"writer": WRITE},
    )
    assert done.returncode == 0, (done.stdout, done.stderr)
    assert out["authorized"] == "true"
    # the other label's older application stays the earliest; the re-applied one is the newest
    assert out["approved_at"] == str(_epoch(_minute(2)))
    assert out["approved_latest_at"] == str(_epoch(_minute(plan_minute + 2)))
    plan = f"printf '%s' '{json.dumps([_comment('github-actions[bot]', PLAN_BODY, plan_minute)])}'"
    done, output = _run_dedupe(tmp_path / "dedupe", plan, out["approved_latest_at"])
    assert done.returncode == 0, (done.stdout, done.stderr)
    assert output.strip() == "fresh=true"
    # the earliest time alone would have read the old plan as covering the new approval
    done, output = _run_dedupe(tmp_path / "dedupe-min", plan, out["approved_at"])
    assert output.strip() == "fresh=false"


# Codex 4241196636: an issue with an invalid body must not starve the cloud routine's queue: step
# 1 never selects a `claude-blocked` issue, and step 2 blocks it AND releases its lease.
def test_cloud_routine_retires_an_invalid_issue_instead_of_starving_the_queue():
    import shlex

    files = render_onboarding(
        spec(implementer="cloud-routine", runs_on=("ubuntu-latest",), ready_label="it's ready")
    )
    doc = files["docs/forge/cloud-implementer.md"]
    step1 = doc[doc.index("1. List open issues") : doc.index("1a. Authorize")]
    command = re.search(r"`(gh issue list [^`]*)`", step1).group(1)
    argv = shlex.split(command)
    assert argv[:6] == ["gh", "issue", "list", "--repo", "owner/comic", "--state"]
    # Codex 4241327879: not the default 30 -- skipped issues stay in the search
    assert argv[argv.index("--limit") + 1] == "1000"
    terms = shlex.split(argv[argv.index("--search") + 1])
    assert terms == [
        "label:it's ready",
        "label:human-review-required",
        "label:implementation-approved",
        "-label:claude-blocked",
        "sort:created-asc",
    ]
    step2 = doc[doc.index("2. Check the request") : doc.index("3. `git fetch origin")]
    assert "add label `claude-blocked`" in step2
    release = step2.index("sdlcctl release --project owner/comic --issue <n>")
    assert release > step2.index("claude-blocked") and "--session <run id>" in step2
    assert "go back to step 1" in step2 and "stop." not in step2


# Codex 4241327879: an issue that is leased elsewhere or fails authorization stays in the search
# (only `claude-blocked` leaves it), so the routine lists well past the default 30 and works
# through the whole list in order instead of stopping at the first skip.
def test_cloud_routine_works_through_every_listed_candidate():
    import shlex

    from agentic_sdlc.onboard import ROUTINE_CANDIDATE_LIMIT

    files = render_onboarding(spec(implementer="cloud-routine", runs_on=("ubuntu-latest",)))
    doc = files["docs/forge/cloud-implementer.md"]
    step1 = doc[doc.index("1. List open issues") : doc.index("1a. Authorize")]
    argv = shlex.split(re.search(r"`(gh issue list [^`]*)`", step1).group(1))
    assert ROUTINE_CANDIDATE_LIMIT == 1000
    assert argv.count("--limit") == 1 and argv[argv.index("--limit") + 1] == "1000"
    flat = " ".join(step1.split())
    assert "Work through it IN ORDER" in flat and "do not list again" in flat
    assert "never ends the run while candidates remain" in flat
    assert "Take the oldest one; if none, stop." not in flat
    # every skip in steps 1a/1b moves on to the next candidate rather than stopping
    skips = doc[doc.index("1a. Authorize") : doc.index("2. Check the request")]
    assert " ".join(skips.split()).count("take the next issue") == 2
    assert "skip that issue and take the next one" in " ".join(skips.split())
    step2 = doc[doc.index("2. Check the request") : doc.index("3. `git fetch origin")]
    assert "next candidate of the step 1 list" in " ".join(step2.split())


# Codex 4241196640: in cloud-routine mode the routine pushes `forge/issue-<N>` with the owner's
# credentials; the branch-domain ruleset and classic-protection checks run against that branch
# and actor (a user: an allowlist or a bypass cannot be matched, so a push restriction is an
# owner TODO), and anything unreadable is a manual TODO.
def _cloud_doctor(tmp_path, **answers):
    from agentic_sdlc.onboard import confirm_cloud_routine

    repo = _repo(tmp_path)
    write_onboarding(repo, spec(implementer="cloud-routine", runs_on=("ubuntu-latest",)))
    confirm_cloud_routine(repo)
    gh = _healthy_gh()
    gh.answers["api repos/owner/comic/actions/secrets"] = _secret_pages("CLAUDE_CODE_OAUTH_TOKEN")
    gh.answers["api repos/owner/comic/actions/variables"] = _var_pages()
    gh.answers["api repos/owner/comic/actions/runners"] = _pages("runners", [])
    gh.answers["api /user/installations"] = _installs()
    # FakeGh answers by prefix, first match first: the test's answers go in front
    gh.answers = {**answers, **{k: v for k, v in gh.answers.items() if k not in answers}}
    return doctor(repo, "owner/comic", "owner/agentic-sdlc", gh), gh


def test_doctor_checks_the_cloud_routine_branch_rules(tmp_path):
    from agentic_sdlc.onboard import CLOUD_BRANCH_RULES_CHECK

    report, gh = _cloud_doctor(tmp_path)
    assert report.ok, report.render()
    check = next(c for c in report.checks if c.name == CLOUD_BRANCH_RULES_CHECK)
    assert check.ok and "forge/issue-${ISSUE_NUMBER}" in check.detail
    paths = [c[1] for c, _ in gh.calls if "/rules/branches/" in c[1]]
    assert "repos/owner/comic/rules/branches/forge%2Fissue-1-1" in paths
    assert not any("/rules/branches/agent" in p for p in paths)  # no Actions publisher here


@pytest.mark.parametrize(
    ("rule", "needle"),
    [
        (_org_rule("creation"), "restricts creating forge/issue-1"),
        (_org_rule("update"), "restricts updates to forge/issue-1"),
        (_org_rule("required_signatures"), "requires signed commits"),
        (_org_rule("pull_request"), "rule 'pull_request' applies to forge/issue-1"),
        (
            _org_rule("required_status_checks", required_status_checks=[{"context": "x"}]),
            "rule 'required_status_checks' applies to forge/issue-1",
        ),
        (
            _pattern_rule("branch_name_pattern", "starts_with", "agent/"),
            "'forge/issue-1-1' does not",
        ),
        (_pattern_rule("commit_message_pattern", "starts_with", "JIRA-"), "commit message"),
        (_pattern_rule("committer_email_pattern", "ends_with", "@corp"), "committer email"),
    ],
)
def test_doctor_reports_rules_that_can_reject_the_cloud_routine_push(tmp_path, rule, needle):
    from agentic_sdlc.onboard import CLOUD_BRANCH_RULES_CHECK

    report, _ = _cloud_doctor(tmp_path, **{CLOUD_BRANCH_RULES: json.dumps([[rule]])})
    failed = _failed(report)
    assert set(failed) == {CLOUD_BRANCH_RULES_CHECK}, report.render()
    check = failed[CLOUD_BRANCH_RULES_CHECK]
    assert check.manual and not report.ok
    assert needle in check.detail, check.detail
    if rule["type"] in ("creation", "update"):
        assert "owner" in check.detail and "forge/**" in check.detail


def test_doctor_accepts_rules_compatible_with_the_cloud_routine_push(tmp_path):
    compatible = [
        _org_rule("deletion"),
        _org_rule("non_fast_forward"),
        _org_rule(
            "required_status_checks",
            required_status_checks=[{"context": "x"}],
            do_not_enforce_on_create=True,
        ),
        _pattern_rule("branch_name_pattern", "starts_with", "forge/issue-"),
    ]
    report, _ = _cloud_doctor(tmp_path, **{CLOUD_BRANCH_RULES: json.dumps([compatible])})
    assert report.ok, report.render()


@pytest.mark.parametrize(
    ("rule", "needle"),
    [
        (
            _bpr("forge/**", restrictsPushes=True, pushAllowances={"nodes": []}),
            "which actor the cloud routine pushes as could not be determined",
        ),
        (_bpr("forge/*", requiresCommitSignatures=True), "signed commits"),
        (_bpr("forge/issue-1*", restrictsPushes=True), "matches some of the cloud routine's"),
        (_bpr("**", lockBranch=True), "locked branch"),
    ],
)
def test_doctor_reads_classic_protection_on_cloud_routine_branches(tmp_path, rule, needle):
    from agentic_sdlc.onboard import CLOUD_BRANCH_RULES_CHECK

    report, _ = _cloud_doctor(tmp_path, **{CLASSIC_RULES: _classic_rules(rule)})
    failed = _failed(report)
    assert CLOUD_BRANCH_RULES_CHECK in failed, report.render()
    assert needle in failed[CLOUD_BRANCH_RULES_CHECK].detail
    assert failed[CLOUD_BRANCH_RULES_CHECK].manual


@pytest.mark.parametrize(
    "answers",
    [
        {CLOUD_BRANCH_RULES: OnboardError("gh: Forbidden (HTTP 403)")},
        {CLASSIC_RULES: OnboardError("gh: boom (HTTP 500)")},
        {"api repos/owner/comic/rulesets?includes_parents=true": OnboardError("gh: (HTTP 500)")},
    ],
)
def test_doctor_reports_unreadable_cloud_routine_branch_rules_as_todo(tmp_path, answers):
    from agentic_sdlc.onboard import CLOUD_BRANCH_RULES_CHECK

    report, _ = _cloud_doctor(tmp_path, **answers)
    check = _failed(report)[CLOUD_BRANCH_RULES_CHECK]
    assert check.manual and "could not" in check.detail and not report.ok


@pytest.mark.parametrize(
    ("include", "exclude", "ok"),
    [
        (["refs/heads/forge/issue-2*"], [], False),  # some issues only
        (["refs/heads/forge/**"], [], True),  # all alike: judged by the effective rules
        (["refs/heads/agent/**"], [], True),  # none of them
    ],
)
def test_doctor_requires_rulesets_to_treat_every_cloud_branch_alike(tmp_path, include, exclude, ok):
    from agentic_sdlc.onboard import CLOUD_BRANCH_RULES_CHECK

    ruleset = _scoped_ruleset(include, exclude)
    summary = {k: ruleset[k] for k in ("id", "name", "source_type", "source", "target")}
    summary["enforcement"] = ruleset["enforcement"]
    report, _ = _cloud_doctor(
        tmp_path,
        **{
            "api repos/owner/comic/rulesets/77": json.dumps(ruleset),
            "api repos/owner/comic/rulesets?includes_parents=true": json.dumps(
                [[{"id": 11, "name": RULESET_NAME, "enforcement": "active"}, summary]]
            ),
        },
    )
    if ok:
        assert report.ok, report.render()
    else:
        check = _failed(report)[CLOUD_BRANCH_RULES_CHECK]
        assert check.manual and "'org agents'" in check.detail
        assert "forge/issue-${ISSUE_NUMBER}" in check.detail


# ---------------------------------------------------------------- review regressions (PR 139, 12)
# Codex review 5483165195.


def _signed_origin_repo(tmp_path):
    """A repository whose `origin/main` is a signed commit, with `log.showSignature` on and a
    `gpg.program` that leaves a marker file when git runs it; returns (repo, marker)."""
    import subprocess

    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "one")
    tree = subprocess.run(
        ["git", "rev-parse", "HEAD^{tree}"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    signed = (
        f"tree {tree}\nauthor t <t@example.com> 1700000000 +0000\n"
        "committer t <t@example.com> 1700000000 +0000\n"
        "gpgsig -----BEGIN PGP SIGNATURE-----\n \n iQEzBAABCAAdFiEE\n -----END PGP SIGNATURE-----\n"
        "\nsigned tip\n"
    )
    commit = subprocess.run(
        ["git", "hash-object", "-t", "commit", "-w", "--stdin"],
        cwd=repo,
        input=signed,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    _git(repo, "update-ref", "refs/remotes/origin/main", commit)
    marker = tmp_path / "gpg-ran"
    program = tmp_path / "evil-gpg"
    program.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
    program.chmod(0o755)
    _git(repo, "config", "log.showSignature", "true")
    _git(repo, "config", "gpg.program", str(program))
    return repo, marker


def _session_start_text(start, monkeypatch, repo):
    """main() in `repo` with every git command run for real and GitHub answering nothing."""
    import io

    real = start.run
    calls = []

    def run(cmd):
        calls.append(cmd)
        if cmd[:1] == ["git"]:
            return real(cmd)
        return "[]" if _is_issue_page(cmd) or _is_comment_page(cmd) else ""

    monkeypatch.chdir(repo)
    monkeypatch.setattr(start, "run", run)
    monkeypatch.setattr(start, "_lease_lookup", lambda: {}.get)
    monkeypatch.setattr(start.sys, "stdin", io.StringIO("{}"))
    out = io.StringIO()
    monkeypatch.setattr(start.sys, "stdout", out)
    assert start.main() == 0
    return out.getvalue(), calls


# Codex 4241434513: the fetch was vetted but the recent-commits `git log` ran unconditionally, so
# `log.showSignature` made it verify the signed tip with whatever `gpg.program` names.
def test_session_start_log_never_runs_the_signature_program(tmp_path, monkeypatch):
    import subprocess

    start = _guard_module(tmp_path, "forge_session_start")
    _guard_module(tmp_path, "forge_commit_guard")
    repo, marker = _signed_origin_repo(tmp_path)
    # the control: a plain `git log` (the old command) runs the program
    subprocess.run(["git", "log", "--oneline", "-8", "origin/main"], cwd=repo, capture_output=True)
    assert marker.exists()
    marker.unlink()
    text, calls = _session_start_text(start, monkeypatch, repo)
    assert not marker.exists()
    assert "[Forge] Did not fetch origin: gpg." in text  # the fetch still verifies signatures
    assert "signed tip" in text  # the listing itself ran, without verifying
    logs = [c for c in calls if c[:2] == ["git", "log"]]
    assert logs == [start.log_command()] and "--no-show-signature" in logs[0]


def test_session_start_skips_the_log_when_git_would_still_run_a_program(tmp_path, monkeypatch):
    start = _guard_module(tmp_path, "forge_session_start")
    _guard_module(tmp_path, "forge_commit_guard")
    repo, marker = _signed_origin_repo(tmp_path)
    _git(repo, "config", "core.pager", str(tmp_path / "evil-pager"))
    text, calls = _session_start_text(start, monkeypatch, repo)
    assert not any(c[:2] == ["git", "log"] for c in calls)
    assert "[Forge] Did not list recent commits: core.pager=" in text
    assert not marker.exists()


def test_session_start_log_is_skipped_without_the_commit_guard(tmp_path, monkeypatch):
    start = _guard_module(tmp_path, "forge_session_start")  # no forge_commit_guard.py beside it
    monkeypatch.setitem(sys.modules, "forge_commit_guard", None)  # nor one imported earlier
    assert "commit guard" in start.log_blocker()


def test_session_start_git_env_forces_its_overrides_after_existing_ones(tmp_path):
    start = _guard_module(tmp_path, "forge_session_start")
    env = start.git_env(
        {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "a.b", "GIT_CONFIG_VALUE_0": "c"}
    )
    assert env["GIT_CONFIG_KEY_0"] == "a.b"  # the caller's own entry is kept
    pairs = {
        env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(1, int(env["GIT_CONFIG_COUNT"]))
    }
    assert pairs == {"core.fsmonitor": "false", "core.hooksPath": os.devnull}
    assert env["GIT_PAGER"] == "cat" and env["GIT_NO_LAZY_FETCH"] == "1"


def test_session_start_runs_git_with_a_fsmonitor_hook_disabled(tmp_path, monkeypatch):
    """A configured fsmonitor program never starts from the hook's own git (here `git status`,
    which consults it; the hook's commands are run the same way)."""
    start = _guard_module(tmp_path, "forge_session_start")
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    marker = tmp_path / "fsmonitor-ran"
    program = tmp_path / "evil-fsmonitor"
    program.write_text(f"#!/bin/sh\ntouch {marker}\n")
    program.chmod(0o755)
    _git(repo, "config", "core.fsmonitor", str(program))
    monkeypatch.chdir(repo)
    assert start.run(["git", "status", "--porcelain"]) is not None
    assert not marker.exists()


@pytest.mark.parametrize(
    ("args", "wanted"),
    [
        (["--no-show-signature", "--oneline"], False),
        (["--show-signature", "--no-show-signature"], False),
        (["--no-show-signature", "--show-signature"], True),
        (["--no-show-signature", "--format=%G?"], True),  # a %G format always verifies
        (["--oneline", "--", "--no-show-signature"], True),  # a path, not an option
        (["--oneline"], True),  # log.showSignature decides
    ],
)
def test_commit_guard_signature_check_honours_no_show_signature(tmp_path, args, wanted):
    guard = _guard_module(tmp_path, "forge_commit_guard")
    config = {"log.showsignature": ["true"], "gpg.program": ["/tmp/x"]}
    assert guard._signature_wanted(args, config) is wanted
    reason = guard._config_reason("log", args, config)
    assert (reason == "gpg.program=/tmp/x") is wanted


def test_commit_guard_internal_git_never_pages(tmp_path, monkeypatch):
    """The guard's own git calls pass `--no-pager` AFTER the vetted command's global options, so
    `git -p ...` cannot start a pager from inside the guard."""
    guard = _guard_module(tmp_path, "forge_commit_guard")
    seen = []

    class Done:
        returncode = 0
        stdout = "x\n"
        stderr = ""

    def fake(cmd, **kwargs):
        seen.append(cmd)
        return Done()

    monkeypatch.setattr(guard.subprocess, "run", fake)
    guard._git_in(str(tmp_path), ((), ("-p", "-C", "."), ()), "rev-parse", "--show-toplevel")
    guard._branch_exists("b", str(tmp_path), ((), ("-p",), ()))
    guard.current_branch(str(tmp_path), ("-p",))
    assert seen and all(c[: c.index("--no-pager")].count("-p") == 1 for c in seen)
    assert seen[0] == [
        guard._trusted_program("git"),
        *["-p", "-C", ".", "--no-pager", "rev-parse", "--show-toplevel"],
    ]


def _routine_prompt(**overrides):
    doc = render_onboarding(
        spec(implementer="cloud-routine", runs_on=("ubuntu-latest",), **overrides)
    )["docs/forge/cloud-implementer.md"]
    return doc[doc.index("Repository: ") : doc.index("```", doc.index("Repository: "))]


def _step(prompt, label, following):
    return " ".join(prompt[prompt.index(label) : prompt.index(following)].split())


# Codex 4241434509: the routine runs the deterministic diff policy on exactly what it pushes and
# pushes nothing unless it passes.
def test_cloud_routine_checks_the_diff_policy_right_before_pushing():
    import shlex

    prompt = _routine_prompt(default_branch="trunk")
    step = _step(prompt, "4b. Check the diff policy", "4c. Verify the lease")
    command = re.search(r"`(sdlcctl check-diff [^`]*)`", step).group(1)
    assert shlex.split(command) == [
        "sdlcctl",
        "check-diff",
        "--project",
        "owner/comic",
        "--config",
        "agentic-sdlc.toml",
        "--base",
        "origin/trunk",
    ]
    assert "`git fetch origin refs/heads/trunk`" in step and "Commit everything first" in step
    assert "On any non-zero exit do NOT push or open a PR" in step
    assert "Make no commit after this check" in step
    assert "every commit of the series" in step  # Codex 4241839922
    assert "that branch's head" in step  # Codex 4241839926
    order = [
        prompt.index("4. Run each"),
        prompt.index("4b. Check the diff policy"),
        prompt.index("sdlcctl check-diff"),
        prompt.index("5. Push the checked commit"),
        prompt.index("git push origin HEAD:refs/heads/<branch>"),
    ]
    assert order == sorted(order)
    # the command the prompt names exists and gates on its exit code
    from agentic_sdlc.cli import build_parser

    args = build_parser().parse_args(shlex.split(command)[1:])
    assert args.command == "check-diff" and args.head == "HEAD"
    assert args.project == "owner/comic" and args.base == "origin/trunk"


# Codex 4241434506: the routine renews its lease between the long stages and verifies it holds
# it immediately before pushing; `renew` fails unless this session holds a live lease
# (test_leases.test_cli_renew_fails_unless_this_session_holds_a_live_lease).
def test_cloud_routine_renews_its_lease_and_verifies_it_before_pushing():
    prompt = _routine_prompt()
    renew = (
        "`sdlcctl renew --project owner/comic --issue <n> --session <run id> "
        "--config agentic-sdlc.toml`"
    )
    flat = " ".join(prompt.split())
    assert flat.count(renew) == 1  # step 3a; 4a and 4c repeat "the step 3a renew command"
    implement = prompt.index("3. `git fetch origin refs/heads/main`")
    renew_1 = prompt.index("3a. Renew the lease")
    verify = prompt.index("4. Run each")
    renew_2 = prompt.index("4a. Renew the lease again, exactly as in step 3a")
    diff = prompt.index("4b. Check the diff policy")
    renew_3 = prompt.index("4c. Verify the lease")
    final_gate = prompt.index("4d. The final gate")
    push = prompt.index("5. Push the checked commit")
    assert implement < renew_1 < verify < renew_2 < diff < renew_3 < final_gate < push
    step3a = _step(prompt, "3a. Renew the lease", "4. Run each")
    assert renew in step3a
    assert "On any non-zero exit STOP: do NOT push or open a PR" in step3a
    step4c = _step(prompt, "4c. Verify the lease", "4d. The final gate")
    assert "run the step 3a renew command once more" in step4c
    assert "do NOT push or open a PR" in step4c
    # Codex 4241614587: a renewal is also a state check
    assert "fails once the issue has been closed" in step3a
    assert "fails if the issue was closed meanwhile" in step4c


# Codex 4241434511: the routine parses the request with the Actions path's validator and
# requires every dependency closed before implementing.
def test_cloud_routine_checks_the_request_and_its_dependencies_before_implementing():
    import shlex

    prompt = _routine_prompt()
    step = _step(prompt, "2. Check the request", "3. `git fetch origin")
    command = re.search(r"`(sdlcctl check-request [^`]*)`", step).group(1)
    assert shlex.split(command) == [
        "sdlcctl",
        "check-request",
        "--project",
        "owner/comic",
        "--issue",
        "<n>",
        "--config",
        "agentic-sdlc.toml",
        "--base",
        "origin/main",
    ]
    assert "`git fetch origin refs/heads/main`, then" in step
    assert "keep that value as <digest>" in step
    assert "Exit 0: continue" in step
    assert "Exit 3: not a valid work request" in step and "add label `claude-blocked`" in step
    assert "Exit 4: a dependency issue is still open" in step and "do NOT block it" in step
    assert "Any other exit: it could not be verified" in step
    assert prompt.index("1b. Claim it") < prompt.index("sdlcctl check-request")
    assert prompt.index("sdlcctl check-request") < prompt.index("3. `git fetch origin")
    from agentic_sdlc.cli import DEPENDENCY_OPEN, REQUEST_INVALID

    assert (REQUEST_INVALID, DEPENDENCY_OPEN) == (3, 4)  # the exit codes the prompt names


def test_cloud_routine_pushes_a_run_specific_branch():
    """Codex 4241532615: a fixed `forge/issue-<n>` left behind by a run whose PR could not be
    opened would make every retry's non-forced push fail. Each run names its own branch once,
    claims with it, creates it and pushes to it; doctor's branch rules cover that namespace."""
    from agentic_sdlc.onboard import CLOUD_ROUTINE_BRANCH

    files = render_onboarding(spec(implementer="cloud-routine", runs_on=("ubuntu-latest",)))
    routine = files["docs/forge/cloud-implementer.md"]
    assert "forge/issue-<n>-<stamp>" in routine and "date -u +%Y%m%d%H%M%S" in routine
    assert "--branch <branch>" in routine
    assert "create branch <branch> from" in routine
    assert "git push origin HEAD:refs/heads/<branch>" in routine
    assert (
        "refs/heads/forge/issue-<n>`" not in routine and "--branch forge/issue-<n>`" not in routine
    )
    assert CLOUD_ROUTINE_BRANCH.template == "forge/issue-${ISSUE_NUMBER}-${RUN_STAMP}"
    for sample in CLOUD_ROUTINE_BRANCH.samples:
        assert re.fullmatch(r"forge/issue-\d+-\d+", sample)
        # the commit guard and the lease PR heuristic both still read the issue number from it
        assert re.search(r"(?:^|/)issue-(\d+)(?:$|[^0-9])", sample)


# Codex 4241614596 / 4241614582 / 4242264478: the final gate, right before the push, is ONE
# command: the request re-checked (dependencies still closed, issue still open, title and body
# the ones step 2 checked) and the labels re-authorized, on one snapshot that a re-read must find
# unchanged -- two separate commands left a gap an edit plus a label re-apply could land in.
def test_cloud_routine_final_gate_is_one_command_right_before_pushing():
    import shlex

    prompt = _routine_prompt(default_branch="trunk")
    step = _step(prompt, "4d. The final gate", "5. Push the checked commit")
    commands = re.findall(r"`(sdlcctl [^`]*)`", step)
    assert len(commands) == 2 and commands[1].startswith("sdlcctl release")
    command = commands[0]
    assert shlex.split(command) == [
        "sdlcctl",
        "check-request",
        "--project",
        "owner/comic",
        "--issue",
        "<n>",
        "--config",
        "agentic-sdlc.toml",
        "--base",
        "origin/trunk",
        "--expect-digest",
        "<digest>",
        "--authorize-labels",
        "--label",
        "implementation-approved",
        "--label",
        "claude-ready",
        "--label",
        "human-review-required",
    ]
    first = re.search(r"`(sdlcctl check-request [^`]*)`", prompt).group(1)
    assert command.startswith(first + " --expect-digest <digest> --authorize-labels")
    # the same approval set step 1a authorizes before claiming
    authorize = re.search(r"`(sdlcctl authorize-labeler [^`]*)`", prompt).group(1)
    assert shlex.split(authorize)[-6:] == shlex.split(command)[-6:]
    assert "Proceed to step 5 ONLY on exit 0" in step
    assert "run nothing between this check and the push" in step
    assert "On any other exit do NOT push or open a PR" in step and "sdlcctl release" in step
    assert "Exit 5 means the title or body changed since step 2" in step
    assert "Exit 7" in step and "Exit 8" in step
    order = [
        prompt.index("2. Check the request"),
        prompt.index("4b. Check the diff policy"),
        prompt.index("4c. Verify the lease"),
        prompt.index("4d. The final gate"),
        prompt.index("5. Push the checked commit"),
        prompt.index("git push origin HEAD:refs/heads/<branch>"),
    ]
    assert order == sorted(order)
    assert "4e. " not in prompt
    from agentic_sdlc.cli import (
        ISSUE_CLOSED,
        LABELS_NOT_AUTHORIZED,
        REQUEST_CHANGED,
        REQUEST_UNSTABLE,
        build_parser,
    )

    # the exit codes the prompt names
    assert (REQUEST_CHANGED, ISSUE_CLOSED, LABELS_NOT_AUTHORIZED, REQUEST_UNSTABLE) == (5, 6, 7, 8)
    argv = ["7" if word == "<n>" else word for word in shlex.split(command)[1:]]
    args = build_parser().parse_args(argv)
    assert args.command == "check-request" and args.expect_digest == "<digest>"
    assert args.authorize_labels and args.label == [
        "implementation-approved",
        "claude-ready",
        "human-review-required",
    ]
    assert args.base == "origin/trunk"


# ---- Codex 4242095555 / 4242095556: the guard's own git and gh are vetted absolute programs.


def _raw_guard_module(tmp_path, name="forge_commit_guard"):
    """A freshly loaded hook WITHOUT `_trust_fake_gh`: its trust resolution is the real one."""
    import importlib.util

    path = tmp_path / f"{name}.py"
    path.write_text(render_onboarding(spec())[f".claude/hooks/{name}.py"])
    module_spec = importlib.util.spec_from_file_location(f"raw_{name}", path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def _shim_dir(tmp_path, marker, **outputs):
    """A directory of executables named after `outputs`' keys; each records that it ran in
    `marker` and prints its value -- a planted `git` answering `main`, a `gh` forging a lease."""
    shims = tmp_path / "shims"
    shims.mkdir(exist_ok=True)
    for name, output in outputs.items():
        shim = shims / name
        shim.write_text(f"#!/bin/sh\necho {name} >> {marker}\nprintf '%s\\n' '{output}'\n")
        shim.chmod(0o755)
    return shims


def _issue_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@e",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "x",
    )
    _git(repo, "switch", "-q", "-c", "forge/issue-7")
    return repo


def test_commit_guard_branch_probe_ignores_a_planted_git(tmp_path, monkeypatch):
    """A `git` shim ahead of the system git on PATH that reports `main` must not decide the
    branch: the probe runs the trusted git, which sees forge/issue-7."""
    import shutil

    if not any(os.path.isfile(p) for p in ("/usr/bin/git", "/bin/git", "/usr/local/bin/git")):
        pytest.skip("no system git")
    guard = _raw_guard_module(tmp_path)
    if guard._trust_note("git"):
        pytest.skip("this machine has no root-owned git")
    repo = _issue_repo(tmp_path)
    marker = tmp_path / "ran"
    shims = _shim_dir(tmp_path, marker, git="main")
    monkeypatch.setenv("PATH", f"{shims}{os.pathsep}{os.environ['PATH']}")
    assert shutil.which("git") == str(shims / "git")  # the trap is armed
    assert guard.current_branch(str(repo)) == "forge/issue-7"
    assert guard.commit_branches("git commit -m x", str(repo)) == ["forge/issue-7"]
    assert not marker.exists()  # the planted git never ran


def test_commit_guard_without_a_trusted_git_refuses(tmp_path, monkeypatch):
    """No git only root can replace: the branch is unknown and the command is refused, with
    the reason -- never a PATH lookup instead."""
    guard = _raw_guard_module(tmp_path)
    repo = _issue_repo(tmp_path)
    marker = tmp_path / "ran"
    shims = _shim_dir(tmp_path, marker, git="main")
    monkeypatch.setattr(guard, "SYSTEM_GIT_PATHS", (str(tmp_path / "no-such-git"),))
    monkeypatch.setenv("PATH", str(shims))  # only the planted git (not root-owned, or root)
    monkeypatch.delenv("AGENTIC_SDLC_GIT", raising=False)
    assert guard.current_branch(str(repo)) == guard.BRANCH_UNKNOWN
    commit = {"tool_name": "Bash", "session_id": "s1", "tool_input": {"command": "git commit"}}
    code, message = guard.decide(commit, guard.BRANCH_UNKNOWN, lambda n: None, lambda b: False)
    assert code == 2 and "only root can replace and found none" in message
    assert not marker.exists()


def test_commit_guard_lease_reads_ignore_a_planted_gh(tmp_path, monkeypatch):
    """A `gh` shim on PATH that forges a live claim for this session must not be run: with no
    trusted gh the lease is UNAVAILABLE (refused), the PR list unreadable, the permission read
    unavailable."""
    guard = _raw_guard_module(tmp_path)
    marker = tmp_path / "ran"
    forged = json.dumps(
        [
            [
                {
                    "body": "<!-- forge-claim agent=a session=s1 branch=b "
                    "expires=2999-01-01T00:00:00Z -->",
                    "user": {"login": "forge-bot[bot]", "type": "Bot"},
                }
            ]
        ]
    )
    shims = _shim_dir(tmp_path, marker, gh=forged)
    monkeypatch.setattr(guard, "SYSTEM_GH_PATHS", (str(tmp_path / "no-such-gh"),))
    monkeypatch.setenv("PATH", str(shims))
    monkeypatch.delenv("AGENTIC_SDLC_GH", raising=False)
    assert guard.lease_unavailable(guard.lease_for(7))
    assert guard.open_pr_for("forge/issue-7") is None
    with pytest.raises(guard.LeaseUnavailable, match="no trusted gh"):
        guard.repo_permission("someone")
    commit = {"tool_name": "Bash", "session_id": "s1", "tool_input": {"command": "git commit"}}
    code, message = guard.decide(commit, "forge/issue-7")
    assert code == 2 and "Lease inventory unavailable" in message
    assert "only root can replace and found none" in message
    assert not marker.exists()  # the forged claim was never read


def test_commit_guard_runs_only_a_trusted_gh(tmp_path, monkeypatch):
    """The trusted gh runs in an environment built from an allowlist, as the library's run_gh:
    no loader variable, no caller CA, the host pinned, a private empty config directory."""
    import subprocess

    guard = _raw_guard_module(tmp_path)
    monkeypatch.setitem(guard._TRUSTED, "gh", "/vetted/gh")
    for key, value in {
        "LD_PRELOAD": "/tmp/evil.so",
        "DYLD_INSERT_LIBRARIES": "/tmp/evil.dylib",
        "SSL_CERT_FILE": "/tmp/ca.pem",
        "GH_HOST": "evil.example",
        "GH_CONFIG_DIR": "/tmp/evil-config",
        "GH_TOKEN": "tok",
        "HTTPS_PROXY": "http://proxy:3128",
        "PATH": "/tmp/evil-bin:/usr/bin",
    }.items():
        monkeypatch.setenv(key, value)
    seen = []

    def fake(args, **kwargs):
        seen.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="[[]]", stderr="")

    monkeypatch.setattr(guard.subprocess, "run", fake)
    assert guard.lease_for(7) is None
    ((args, kwargs),) = seen
    env = kwargs["env"]
    assert args[0] == "/vetted/gh" and args[1] == "api"
    assert not any(key.startswith(("LD_", "DYLD_")) for key in env)
    assert "SSL_CERT_FILE" not in env and env["GH_HOST"] == "github.com"
    assert env["GH_TOKEN"] == "tok" and env["HTTPS_PROXY"] == "http://proxy:3128"
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["GH_CONFIG_DIR"] == env["HOME"] == kwargs["cwd"] != "/tmp/evil-config"


def test_commit_guard_git_probe_environment_is_allowlisted(tmp_path, monkeypatch):
    """The probe keeps what selects the command's repository and configuration and drops the
    loader's variables and the caller's PATH."""
    guard = _raw_guard_module(tmp_path)
    monkeypatch.setitem(guard._TRUSTED, "git", "/vetted/git")
    monkeypatch.setenv("LD_PRELOAD", "/tmp/evil.so")
    monkeypatch.setenv("GIT_TRACE", "/tmp/trace")
    monkeypatch.setenv("PATH", "/tmp/evil-bin:/usr/bin")
    monkeypatch.setenv("GIT_DIR", "/elsewhere/.git")
    seen = []

    class Done:
        returncode = 0
        stdout = "forge/issue-7\n"
        stderr = ""

    monkeypatch.setattr(
        guard.subprocess, "run", lambda args, **kw: seen.append((args, kw)) or Done()
    )
    branch = guard.current_branch(str(tmp_path), env={"GIT_WORK_TREE": "/wt", "LD_AUDIT": "x"})
    assert branch == "forge/issue-7"
    ((args, kwargs),) = seen
    env = kwargs["env"]
    assert args[0] == "/vetted/git"
    assert env["GIT_DIR"] == "/elsewhere/.git" and env["GIT_WORK_TREE"] == "/wt"
    assert env["PATH"] == "/usr/bin:/bin" and env["LC_ALL"] == "C"
    assert not any(key.startswith(("LD_", "DYLD_", "GIT_TRACE")) for key in env)


def test_commit_guard_trusts_only_root_owned_programs(tmp_path):
    guard = _raw_guard_module(tmp_path)
    planted = tmp_path / "git"
    planted.write_text("#!/bin/sh\necho main\n")
    planted.chmod(0o755)
    # as root, under a world-writable /tmp; as anyone else, not owned by root
    assert guard._trust_candidate_reason(str(planted)) is not None
    assert guard._trust_candidate_reason("relative/git") is not None


def test_commit_guard_vets_a_git_other_than_its_own(tmp_path, monkeypatch):
    """An allowlisted command run by a git that is not the guard's own: the exec path the guard
    reads is not the one that git searches, so the command is guarded."""
    guard = _raw_guard_module(tmp_path)
    repo = _issue_repo(tmp_path)
    if guard._trust_note("git"):
        pytest.skip("this machine has no root-owned git")
    other = tmp_path / "other-git"
    other.write_text("#!/bin/sh\nexit 0\n")
    other.chmod(0o755)
    found = guard._other_git_reason(str(other), str(repo))
    assert found and "is not the git the guard reads with" in found
    assert guard._other_git_reason(guard._trusted_program("git"), str(repo)) is None


def test_session_start_runs_only_trusted_programs(tmp_path, monkeypatch):
    """The SessionStart hook runs git and gh through the guard's vetted programs: a planted
    `git` cannot answer, and with no trusted gh the inventory read fails (None), unread."""
    start = _guard_module(tmp_path, "forge_session_start")
    _guard_module(tmp_path, "forge_commit_guard")
    guard = start._guard()
    if guard._trust_note("git"):
        pytest.skip("this machine has no root-owned git")
    repo = _issue_repo(tmp_path)
    marker = tmp_path / "ran"
    shims = _shim_dir(tmp_path, marker, git="main", gh="[]")
    monkeypatch.setattr(guard, "_TRUSTED", {})
    monkeypatch.setattr(guard, "SYSTEM_GH_PATHS", (str(tmp_path / "no-such-gh"),))
    monkeypatch.setenv("PATH", f"{shims}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("AGENTIC_SDLC_GH", raising=False)
    monkeypatch.chdir(repo)
    assert start.run(["git", "rev-parse", "--abbrev-ref", "HEAD"]) == "forge/issue-7"
    if os.geteuid() == 0 or guard._trust_note("gh"):
        assert start.run(["gh", "api", "repos/x/y/issues"]) is None
    assert not marker.exists()


# Codex 4242545209: every --var name is validated before any file is written or anything
# remote is read or changed; `gh variable set ''` used to fail only after labels and rulesets.


@pytest.mark.parametrize(
    "item",
    ["=value", "1ABC=x", "A-B=x", "A B=x", "GITHUB_TOKEN=x", "github_sha=x", "Ä=x"],
)
def test_cli_onboard_rejects_an_invalid_var_name_before_anything(item, tmp_path, monkeypatch):
    from agentic_sdlc import cli

    for name in (
        "apply_repo_settings",
        "canonical_project_id",
        "resolve_platform_ref",
        "repository_default_branch",
        "repository_fork_exposed",
        "write_onboarding",
    ):
        monkeypatch.setattr(cli, name, lambda *a, _n=name, **k: pytest.fail(f"{_n} called"))
    repo = _repo(tmp_path)
    with pytest.raises(OnboardError, match="variable name") as caught:
        cli._onboard(
            cli.build_parser().parse_args(
                [
                    "onboard",
                    "--visibility",
                    "auto",
                    "--destination",
                    str(repo),
                    "--project-id",
                    "owner/comic",
                    "--platform-repository",
                    "owner/agentic-sdlc",
                    "--platform-ref",
                    "main",
                    "--test",
                    "pytest -q",
                    "--apply",
                    "--var",
                    "GOOD_NAME=1",
                    "--var",
                    item,
                ]
            )
        )
    assert repr(item) in str(caught.value)
    assert not (repo / "agentic-sdlc.toml").exists()


@pytest.mark.parametrize("name", ["A", "_x", "model_1", "DEEPSEEK_MODEL_PRO", "GITHUBX"])
def test_variable_name_problem_accepts_github_variable_names(name):
    from agentic_sdlc.onboard import variable_name_problem

    assert variable_name_problem(name) == ""


@pytest.mark.parametrize("name", ["", "1A", "A-B", "A.B", "GITHUB_X", "Github_x"])
def test_variable_name_problem_rejects_what_github_refuses(name):
    from agentic_sdlc.onboard import variable_name_problem

    assert variable_name_problem(name)


def test_apply_repo_settings_refuses_a_bad_variable_name_before_any_gh_call():
    from agentic_sdlc.onboard import apply_repo_settings

    gh = FakeGh()
    with pytest.raises(OnboardError, match="nothing was changed"):
        apply_repo_settings(spec(), gh, variables={"OK": "1", "": "x"})
    assert gh.calls == []


# Codex 4242545214: any name `git check-ref-format --branch` accepts is a valid default branch,
# except the characters the platform's double-quoted shell sink cannot carry; every sink Forge
# renders itself quotes or escapes it.

GIT_VALID_BRANCHES = ["release+next", "feature@two", "a{b}!c", "x&y;(z)", "it's", "@"]


@pytest.mark.parametrize("branch", GIT_VALID_BRANCHES)
def test_git_valid_default_branches_round_trip_through_onboarding(branch, tmp_path):
    import shlex
    import subprocess

    from agentic_sdlc import onboard
    from agentic_sdlc.policy import load_policy

    git = subprocess.run(["git", "check-ref-format", "--branch", branch], capture_output=True)
    assert git.returncode == 0, branch
    assert onboard.policy_field_problem("default branch", branch) == ""
    repo = _repo(tmp_path)
    write_onboarding(repo, spec(default_branch=branch))
    assert load_policy(repo / "agentic-sdlc.toml").default_branch == branch
    # ci.yml: the push filter is a glob that matches exactly this branch
    ci = yaml.safe_load((repo / ".github/workflows/ci.yml").read_text())
    (pattern,) = ci[True]["push"]["branches"]
    assert onboard._branch_glob_matches(pattern, branch) is True
    assert onboard._branch_glob_matches(pattern, branch + "x") is False
    if "+" in branch:
        assert onboard._branch_glob_matches(pattern, branch.replace("+", "")) is False
    # the session-start hook carries it as a Python string literal
    hook = (repo / ".claude/hooks/forge_session_start.py").read_text()
    assert f"DEFAULT_BRANCH = {json.dumps(branch)}" in hook
    # the ruleset's ~DEFAULT_BRANCH is that one branch, matched literally (never a glob)
    agent = onboard.PublisherValue(
        ("refs/heads/forge/issue-", "-", ""), ("ISSUE_NUMBER", "RUN_STAMP")
    )
    assert onboard._ref_pattern_scope("~DEFAULT_BRANCH", agent, branch) is False
    itself = onboard.PublisherValue(("refs/heads/" + branch,), ())
    assert onboard._ref_pattern_scope("~DEFAULT_BRANCH", itself, branch) is True
    # the cloud routine prompt passes it as one shell word
    prompt = _routine_prompt(default_branch=branch)
    fetch = re.search(r"`(git fetch origin [^`]*)`", prompt).group(1)
    assert shlex.split(fetch) == ["git", "fetch", "origin", f"refs/heads/{branch}"]
    check = re.search(r"`(sdlcctl check-diff [^`]*)`", prompt).group(1)
    assert shlex.split(check)[-2:] == ["--base", f"origin/{branch}"]
    # doctor, offline, finds nothing about it that it would not find for `main`
    plain = _repo(tmp_path / "plain")
    write_onboarding(plain, spec())

    def failures(path):
        report = doctor(path, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False)
        return {c.name for c in report.checks if not c.ok}

    assert failures(repo) == failures(plain)


# Codex 4242643519: a branch literally named `@` is valid, but revision syntax reads a bare `@`
# as HEAD (`git fetch origin @` fetches the remote's HEAD and leaves origin/@ stale). The
# routine's commands name it fully qualified, so they act on the branch itself.
def test_a_default_branch_named_at_is_fetched_and_resolved_as_the_branch(tmp_path):
    import shlex
    import subprocess

    def git(cwd, *args):
        return subprocess.run(
            ["git", "-c", "init.defaultBranch=other", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
        ).stdout.strip()

    ident = ("-c", "user.name=t", "-c", "user.email=t@example.com")
    remote, clone = tmp_path / "remote", tmp_path / "clone"
    remote.mkdir()
    git(remote, "init", "-q")
    git(remote, *ident, "commit", "-q", "--allow-empty", "-m", "base")
    git(remote, "branch", "@")
    git(remote, *ident, "commit", "-q", "--allow-empty", "-m", "other moves on")
    git(tmp_path, "clone", "-q", str(remote), str(clone))
    # the remote's `@` branch moves; its HEAD (`other`) is a different commit
    git(remote, "checkout", "-q", "@")
    git(remote, *ident, "commit", "-q", "--allow-empty", "-m", "on @")
    git(remote, "checkout", "-q", "other")
    at_head = git(remote, "rev-parse", "refs/heads/@")
    assert git(remote, "rev-parse", "@") != at_head  # bare `@` is HEAD, not the branch

    prompt = _routine_prompt(default_branch="@")
    fetches = set(re.findall(r"`(git fetch origin [^`]*)`", prompt))
    assert fetches == {"git fetch origin refs/heads/@"}
    git(clone, *shlex.split(fetches.pop())[1:])
    base = shlex.split(re.search(r"`(sdlcctl check-diff [^`]*)`", prompt).group(1))[-1]
    assert base == "origin/@"
    assert git(clone, "rev-parse", "--verify", f"{base}^{{commit}}") == at_head
    git(clone, "checkout", "-q", "-b", "forge/issue-1-1", base)
    assert git(clone, "rev-parse", "HEAD") == at_head


@pytest.mark.parametrize("branch", ['a"b', "a$b", "a${{x}}", "a`b"])
def test_default_branch_rejects_only_what_the_platform_shell_sink_cannot_carry(branch, tmp_path):
    from agentic_sdlc import onboard
    from agentic_sdlc.policy import load_policy

    assert "reusable-implement.yml" in onboard.policy_field_problem("default branch", branch)
    with pytest.raises(OnboardError):
        spec(default_branch=branch)
    policy = tmp_path / "agentic-sdlc.toml"
    policy.write_text(
        f'version = 1\n[project]\nid = "owner/comic"\ndefault_branch = {json.dumps(branch)}\n'
    )
    with pytest.raises(ValueError, match="reusable-implement.yml"):
        load_policy(policy)


# ---- Codex 4242753911 / 4242753904: the hooks run a vetted interpreter on pinned bytes

SYSTEM_PYTHON = "/usr/bin/python3"
needs_system_python = pytest.mark.skipif(
    not os.path.exists(SYSTEM_PYTHON) or sys.platform == "win32",
    reason="the hook handlers run /usr/bin/python3",
)


def _run_hook(repo: Path, command: str, stdin: str = "{}", path: str | None = None):
    import subprocess

    env = {
        "CLAUDE_PROJECT_DIR": str(repo),
        "PATH": path if path is not None else os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", "/"),
    }
    return subprocess.run(
        ["/bin/sh", "-c", command],
        input=stdin,
        text=True,
        capture_output=True,
        env=env,
        cwd=repo,
        timeout=120,
    )


@needs_system_python
def test_the_hook_loader_runs_the_pinned_script_isolated_with_the_session_path(tmp_path):
    """The handler runs the vetted interpreter in isolated mode on exactly the bytes whose
    SHA-256 it pins, as `__main__`, and hands the script the session's own PATH."""
    from agentic_sdlc.onboard import forge_hook_entry, hook_script_digest

    repo = _repo(tmp_path)
    script = repo / HOOK_GUARD
    script.parent.mkdir(parents=True)
    script.write_text(
        "import json, os, sys\n"
        "print(json.dumps({'name': __name__, 'file': __file__, 'argv': sys.argv,\n"
        "  'isolated': sys.flags.isolated, 'path': os.environ['PATH'],\n"
        "  'leak': 'FORGE_SESSION_PATH' in os.environ, 'stdin': sys.stdin.read()}))\n"
        "sys.exit(0)\n"
    )
    entry = forge_hook_entry("PreToolUse", hook_script_digest(script.read_bytes()))
    done = _run_hook(repo, entry["command"], stdin="payload", path="/session/bin:/usr/bin")
    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout)
    assert seen == {
        "name": "__main__",
        "file": str(script),
        "argv": [str(script)],
        "isolated": 1,
        "path": "/session/bin:/usr/bin",
        "leak": False,
        "stdin": "payload",
    }


@needs_system_python
def test_a_tampered_hook_script_or_a_planted_python3_cannot_pass_the_guard(tmp_path):
    """Codex 4242753904: a guard whose bytes are not the pinned ones is refused (exit 2, which
    blocks) before any of it runs. Codex 4242753911: a `python3` planted in a writable PATH
    entry is never the interpreter."""
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    command = json.loads((repo / ".claude/settings.json").read_text())["hooks"]["PreToolUse"][0][
        "hooks"
    ][0]["command"]
    planted = tmp_path / "bin"
    planted.mkdir()
    (planted / "python3").write_text("#!/bin/sh\nexit 0\n")
    (planted / "python3").chmod(0o755)
    guard = repo / HOOK_GUARD
    guard.write_text("import sys\nsys.exit(0)\n")
    done = _run_hook(repo, command, path=f"{planted}:/usr/bin:/bin")
    assert done.returncode == 2
    assert "is not the copy onboard installed" in done.stderr
    guard.unlink()
    assert _run_hook(repo, command, path=f"{planted}:/usr/bin:/bin").returncode == 2


@needs_system_python
def test_the_installed_guard_runs_through_the_loader_and_allows_a_harmless_command(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    command = json.loads((repo / ".claude/settings.json").read_text())["hooks"]["PreToolUse"][0][
        "hooks"
    ][0]["command"]
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": str(repo)})
    done = _run_hook(repo, command, stdin=payload)
    assert done.returncode == 0, done.stderr


def test_doctor_rejects_an_unpinned_a_stale_or_an_untrusted_hook_handler(tmp_path):
    from agentic_sdlc.onboard import forge_hook_entry, hook_problems

    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    path = repo / ".claude/settings.json"
    good = path.read_text()
    assert hook_problems(path) == []
    guard = _forge_entry("PreToolUse")
    fake = tmp_path / "python3"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    cases = {
        # the pre-4242753911 handler: python3 resolved through the session's PATH
        "found on PATH": {
            **guard,
            "command": f'python3 "${{CLAUDE_PROJECT_DIR}}/{HOOK_GUARD}" || exit 2',
        },
        "pinned SHA-256": forge_hook_entry("PreToolUse", "0" * 64),
        "untrusted interpreter": forge_hook_entry(
            "PreToolUse", guard["command"].split()[-4], str(fake)
        ),
    }
    for why, entry in cases.items():
        settings = json.loads(good)
        settings["hooks"]["PreToolUse"] = [{"matcher": "Bash", "hooks": [entry]}]
        path.write_text(json.dumps(settings))
        problems = " ".join(hook_problems(path))
        assert why in problems, (why, problems)
    # the script changed under an intact handler: the hash no longer matches
    path.write_text(good)
    (repo / HOOK_GUARD).write_text("import sys\nsys.exit(0)\n")
    assert "pinned SHA-256" in " ".join(hook_problems(path))


def test_onboard_refuses_an_untrusted_hook_interpreter(tmp_path, monkeypatch):
    from agentic_sdlc import onboard as module

    fake = tmp_path / "python3"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    with pytest.raises(OnboardError, match="--hook-python"):
        module.trusted_hook_python(str(fake))
    with pytest.raises(OnboardError, match="absolute path"):
        module.trusted_hook_python("python3")
    monkeypatch.setattr(module, "HOOK_PYTHON_CANDIDATES", (str(tmp_path / "none"), str(fake)))
    with pytest.raises(OnboardError, match="no trusted interpreter for the Claude Code hooks"):
        module.trusted_hook_python()
    repo = _repo(tmp_path)
    with pytest.raises(OnboardError, match="hook interpreter"):
        write_onboarding(repo, spec(hook_python=str(fake)))
    assert not (repo / ".claude").exists()  # refused before anything was written


@needs_system_python
def test_the_default_hook_interpreter_is_the_vetted_system_python():
    from agentic_sdlc.onboard import hook_python_problem, trusted_hook_python

    if hook_python_problem(SYSTEM_PYTHON):
        pytest.skip(f"{SYSTEM_PYTHON} is not root-owned here")
    assert trusted_hook_python() == SYSTEM_PYTHON
    assert trusted_hook_python(SYSTEM_PYTHON) == SYSTEM_PYTHON


# ---- Codex 4242925452: a root-owned executable is not enough; it must BE a recent CPython


def _fake_interpreter(tmp_path, name, answer):
    """A program that answers the probe the way `answer` says (`{nonce}` is the nonce)."""
    fake = tmp_path / name
    body = answer.replace("{nonce}", '"$4"')  # argv: -I -c <code> <nonce>
    fake.write_text(f"#!/bin/sh\necho {body}\n")
    fake.chmod(0o755)
    return str(fake)


def test_the_hook_interpreter_is_probed_not_just_owned(tmp_path, monkeypatch):
    from agentic_sdlc import onboard as module
    from agentic_sdlc import trusted_exec

    # /bin/true is root-owned and exits 0 without running anything: refused
    true = "/bin/true" if os.path.exists("/bin/true") else "/usr/bin/true"
    assert "not a Python interpreter" in module.hook_python_problem(true)
    with pytest.raises(OnboardError, match="not a Python interpreter"):
        module.trusted_hook_python(true)
    # pretend the fakes are root-owned, so only the probe decides
    monkeypatch.setattr(trusted_exec, "candidate_reason", lambda _path: None)
    old = _fake_interpreter(tmp_path, "python3.9", "{nonce} cpython 3 9")
    assert "3.9; the hooks need 3.11 or newer" in module.hook_python_problem(old)
    other = _fake_interpreter(tmp_path, "pypy3", "{nonce} pypy 3 11")
    assert "is pypy, not cpython" in module.hook_python_problem(other)
    static = _fake_interpreter(tmp_path, "static", "fixed cpython 3 12")  # ignores the nonce
    assert "not a Python interpreter" in module.hook_python_problem(static)
    good = _fake_interpreter(tmp_path, "python3", "{nonce} cpython 3 12")
    assert module.hook_python_problem(good) == ""
    failing = tmp_path / "failing"
    failing.write_text("#!/bin/sh\nexit 3\n")
    failing.chmod(0o755)
    assert "exit 3" in module.hook_python_problem(str(failing))
    monkeypatch.setattr(module, "HOOK_PYTHON_CANDIDATES", (true, old))
    with pytest.raises(OnboardError, match="no trusted interpreter"):
        module.trusted_hook_python()
    repo = _repo(tmp_path)
    with pytest.raises(OnboardError, match="hook interpreter"):
        write_onboarding(repo, spec(hook_python=old))
    assert not (repo / ".claude").exists()


def test_doctor_rejects_a_hook_handler_running_a_root_owned_non_python(tmp_path):
    from agentic_sdlc.onboard import forge_hook_entry, hook_problems

    true = "/bin/true" if os.path.exists("/bin/true") else "/usr/bin/true"
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    path = repo / ".claude/settings.json"
    settings = json.loads(path.read_text())
    digest = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"].split()[-4]
    settings["hooks"]["PreToolUse"][0]["hooks"] = [forge_hook_entry("PreToolUse", digest, true)]
    path.write_text(json.dumps(settings))
    problems = " ".join(hook_problems(path))
    assert "untrusted interpreter" in problems and "not a Python interpreter" in problems


def test_the_hook_python_minimum_is_the_packages_requires_python():
    from agentic_sdlc.onboard import HOOK_PYTHON_MIN_VERSION

    pyproject = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    floor = pyproject["project"]["requires-python"]
    assert floor == ">=" + ".".join(map(str, HOOK_PYTHON_MIN_VERSION))


# ---- Codex 4242753917: lease markers trust the exact Publisher bot


def test_onboard_records_the_publisher_bot_in_the_policy_and_the_guard():
    files = render_onboarding(spec())
    policy = tomllib.loads(files["agentic-sdlc.toml"])
    assert policy["automation"]["publisher_bot_login"] == "agentic-sdlc-publisher[bot]"
    assert _policy_bot_of(files["agentic-sdlc.toml"]) == "agentic-sdlc-publisher[bot]"
    guard = files[HOOK_GUARD]
    assert 'PUBLISHER_BOT_LOGINS = "agentic-sdlc-publisher[bot]"' in guard
    none = render_onboarding(spec(publisher_bot_login=""))
    assert 'PUBLISHER_BOT_LOGINS = ""' in none[HOOK_GUARD]
    for bad in ("agentic-sdlc", "Agentic[bot]", "x[bot] ", "a b[bot]"):
        with pytest.raises(OnboardError, match="publisher_bot_login"):
            spec(publisher_bot_login=bad)


def _policy_bot_of(text: str) -> str:
    from agentic_sdlc.policy import load_policy_bytes

    return load_policy_bytes(text.encode()).publisher_bot_login


def test_doctor_requires_the_publisher_bot_in_actions_mode_only(tmp_path):
    from agentic_sdlc.onboard import LEASE_BOT_CHECK

    repo = _repo(tmp_path)
    write_onboarding(repo, spec(publisher_bot_login=""))
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False))
    assert "publisher_bot_login is empty" in failed[LEASE_BOT_CHECK].detail
    write_onboarding(repo, spec(), force=True)
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False)
    assert LEASE_BOT_CHECK not in _failed(report)
    cloud = _repo(tmp_path / "cloud")
    write_onboarding(
        cloud,
        spec(implementer="cloud-routine", runs_on=("ubuntu-latest",), publisher_bot_login=""),
    )
    report = doctor(cloud, "owner/comic", "owner/agentic-sdlc", FakeGh(), remote=False)
    assert LEASE_BOT_CHECK not in _failed(report)


def test_a_forced_reonboard_keeps_the_installed_publisher_bot(tmp_path):
    from agentic_sdlc.onboard import installed_bot_login

    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    assert installed_bot_login(repo) == "agentic-sdlc-publisher[bot]"
    assert installed_bot_login(tmp_path / "nowhere") == ""


def test_remote_doctor_requires_the_bot_to_be_the_publisher_apps(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec(publisher_bot_login="other-app[bot]"))
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", _healthy_gh()))
    detail = failed["lease bot login is the Publisher App's"].detail
    assert "other-app[bot]" in detail and "agentic-sdlc-publisher[bot]" in detail
    write_onboarding(repo, spec(), force=True)
    gh = _healthy_gh()
    gh.answers["api repos/owner/comic/actions/variables"] = json.dumps(
        [
            {
                "total_count": 2,
                "variables": [
                    {"name": "PUBLISHER_APP_CLIENT_ID", "value": "publisher_app_client_id-value"},
                    {"name": "FORGE_LEASE_BOT_LOGINS", "value": "someone-else[bot]"},
                ],
            }
        ]
    )
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))
    assert (
        "FORGE_LEASE_BOT_LOGINS variable" in failed["lease bot login is the Publisher App's"].detail
    )
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", _healthy_gh())
    assert report.ok, report.render()


def test_onboard_cli_validates_and_records_the_publisher_bot(tmp_path):
    from agentic_sdlc import cli

    repo = _repo(tmp_path)
    base = ["onboard", "--destination", str(repo), "--project-id", "owner/comic"]
    base += ["--platform-repository", "owner/agentic-sdlc", "--platform-ref", SHA]
    base += ["--test", "pytest -q", "--visibility", "private", "--output", str(tmp_path / "o")]
    assert cli.main([*base, "--publisher-bot-login", "agentic-sdlc"]) == 2
    assert not (repo / "agentic-sdlc.toml").exists()
    assert cli.main([*base, "--publisher-bot-login", "my-forge[bot]"]) == 0
    assert load_policy(repo / "agentic-sdlc.toml").publisher_bot_login == "my-forge[bot]"
    assert cli.main([*base, "--force"]) == 0  # kept without the flag
    assert load_policy(repo / "agentic-sdlc.toml").publisher_bot_login == "my-forge[bot]"


# Codex 4243193105: a failed claim (any exit) may have left its marker queued when retracting it
# failed; the routine releases the same session, best-effort, before moving on.
def test_cloud_routine_releases_after_a_failed_claim():
    files = render_onboarding(spec(implementer="cloud-routine", runs_on=("ubuntu-latest",)))
    doc = files["docs/forge/cloud-implementer.md"]
    step = " ".join(doc[doc.index("1b. Claim it") : doc.index("2. Check the request")].split())
    assert "If the claim is refused (exit 2)" not in step
    fails = step[step.index("If the claim fails (ANY non-zero exit)") :]
    release = fails.index("sdlcctl release --project owner/comic --issue <n> --session <run id>")
    assert release < fails.index("then skip that issue and take the next one")
    assert "same <run id>" in fails and "best-effort" in fails
    assert "sdlcctl release` with the same `--session`" in " ".join(files["AGENTS.md"].split())


# Codex 4243193102: renewals and the final gate refuse once the issue has an open PR.
def test_cloud_routine_stops_when_a_pr_appears_after_the_claim():
    files = render_onboarding(spec(implementer="cloud-routine", runs_on=("ubuntu-latest",)))
    doc = " ".join(files["docs/forge/cloud-implementer.md"].split())
    step3a = doc[doc.index("3a. Renew the lease") : doc.index("4. Run each")]
    assert "once the issue has an open pull request" in step3a
    step4d = doc[doc.index("4d. The final gate") : doc.index("5. Push the checked commit")]
    assert "Exit 9: a pull request for the issue was opened or linked meanwhile" in step4d


# ---------------------------------------------------------------- Codex 4243386524
# An organization repository's workflows also see the organization secrets and variables shared
# with it: doctor unions the inherited inventories in, and one it cannot read leaves what is
# missing at repository scope unverifiable (a warning), never "missing".
ORG_SECRETS = "api repos/owner/comic/actions/organization-secrets"
ORG_VARIABLES = "api repos/owner/comic/actions/organization-variables"


def _org_gh(owner_type: str = "Organization", **first) -> FakeGh:
    gh = _healthy_gh()
    repo = json.loads(gh.answers["api repos/owner/comic"])
    gh.answers["api repos/owner/comic"] = json.dumps(
        {**repo, "owner": {"login": "owner", "type": owner_type}}
    )
    # FakeGh answers by prefix in order: the inherited endpoints before `api repos/owner/comic`
    gh.answers = {**first, **gh.answers}
    gh.answers["api repos/owner/comic/actions/secrets"] = _secret_pages(
        "DEEPSEEK_API_KEY", "ZAI_API_KEY", "KIMI_API_KEY"
    )
    gh.answers["api repos/owner/comic/actions/variables"] = _var_pages(
        "DEEPSEEK_MODEL_FLASH", "DEEPSEEK_MODEL_PRO", "ZAI_MODEL_GLM", "KIMI_MODEL_K3"
    )
    return gh


def test_doctor_counts_inherited_organization_secrets_and_variables(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _org_gh(
        **{
            ORG_SECRETS: _secret_pages("PUBLISHER_APP_PRIVATE_KEY", "CLAUDE_CODE_OAUTH_TOKEN"),
            ORG_VARIABLES: _var_pages("PUBLISHER_APP_CLIENT_ID"),
        }
    )
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    assert report.ok, report.render()
    by_name = {c.name: c for c in report.checks}
    assert not by_name["repo secrets set (by name)"].warning
    assert not by_name["repo variables set (non-empty)"].warning
    called = {" ".join(c[:2]) for c, _ in gh.calls}
    assert ORG_SECRETS in called and ORG_VARIABLES in called


def test_doctor_still_fails_a_secret_set_nowhere_in_an_organization(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _org_gh(
        **{
            ORG_SECRETS: _secret_pages("CLAUDE_CODE_OAUTH_TOKEN"),
            ORG_VARIABLES: _var_pages("PUBLISHER_APP_CLIENT_ID"),
        }
    )
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))
    assert "PUBLISHER_APP_PRIVATE_KEY" in failed["repo secrets set (by name)"].detail


def test_doctor_warns_when_the_inherited_inventory_is_unreadable(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    denied = OnboardError("gh: Resource not accessible by integration (HTTP 403)")
    gh = _org_gh(**{ORG_SECRETS: denied, ORG_VARIABLES: denied})
    report = doctor(repo, "owner/comic", "owner/agentic-sdlc", gh)
    by_name = {c.name: c for c in report.checks}
    for name, missing in (
        ("repo secrets set (by name)", "PUBLISHER_APP_PRIVATE_KEY"),
        ("repo variables set (non-empty)", "PUBLISHER_APP_CLIENT_ID"),
    ):
        check = by_name[name]
        assert check.ok and check.warning, check
        assert missing in check.detail and "could not be read" in check.detail


def test_doctor_reads_no_organization_inventory_for_a_user_repository(tmp_path):
    repo = _repo(tmp_path)
    write_onboarding(repo, spec())
    gh = _org_gh(owner_type="User")
    failed = _failed(doctor(repo, "owner/comic", "owner/agentic-sdlc", gh))
    assert "PUBLISHER_APP_PRIVATE_KEY" in failed["repo secrets set (by name)"].detail
    assert not any("organization-" in c[1] for c, _ in gh.calls if len(c) > 1)


# Codex 4244027557: "absent" is GitHub's 404 ANSWER (`gh_http_status`), never "404" in the
# message -- a repository or branch named `404` echoes it in every failed request.
_FAILURES_ON_A_404_PATH = [
    GhCommandError(
        "gh api repos/o/404/contents/x?ref=404 failed: gh: Server Error (HTTP 500)",
        http_status=500,
    ),
    GhCommandError(
        "gh api repos/o/404/contents/x?ref=404 failed: gh: Forbidden (HTTP 403)", http_status=403
    ),
    GhCommandError("gh api repos/o/404/contents/x?ref=404 failed: Not Found", http_status=None),
    OnboardError("gh api repos/o/404/contents/x?ref=404 did not answer within 60s; not concluding"),
    # an OnboardError that is not run_gh's: no parsed status, whatever the text says
    OnboardError("gh: Not Found (HTTP 404)"),
]


def _raising(failure):
    def gh(args, input=None):
        raise failure

    return gh


@pytest.mark.parametrize(
    "failure", [f for f in _FAILURES_ON_A_404_PATH if isinstance(f, OnboardError)]
)
def test_remote_file_state_is_unknown_unless_github_answered_404(failure):
    from agentic_sdlc.onboard import _remote_file_state

    args = ["api", "repos/o/404/contents/x?ref=404"]
    assert _remote_file_state(_raising(failure), args) == "unknown"
    genuine = GhCommandError("gh: Not Found (HTTP 404)", http_status=404)
    assert _remote_file_state(_raising(genuine), args) == "absent"


@pytest.mark.parametrize(
    "failure", [f for f in _FAILURES_ON_A_404_PATH if isinstance(f, OnboardError)]
)
def test_safe_json_not_found_only_on_a_404_answer(failure):
    from agentic_sdlc.onboard import Unreadable, _safe_json

    args = ["api", "repos/o/404/branches/404"]
    answer = _safe_json(_raising(failure), args)
    assert isinstance(answer, Unreadable) and not answer.not_found
    genuine = _safe_json(
        _raising(GhCommandError("gh: Not Found (HTTP 404)", http_status=404)), args
    )
    assert isinstance(genuine, Unreadable) and genuine.not_found


def test_hook_python_is_strict_in_an_agent_context(monkeypatch, tmp_path) -> None:
    # Codex 4244145211: --trust-mode local / a local policy must not let an agent context pin a
    # user-writable interpreter; the effective mode there is strict.
    from agentic_sdlc import onboard

    fake = tmp_path / "python3"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(0o755)
    seen: list[str] = []
    monkeypatch.setattr(onboard, "_hook_python_probe_problem", lambda path: "")
    import agentic_sdlc.trusted_exec as te

    monkeypatch.setattr(te, "local_candidate_reason", lambda path: seen.append("local") or "")
    monkeypatch.setattr(te, "candidate_reason", lambda path: seen.append("strict") or "untrusted")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_REMOTE", raising=False)
    assert onboard.hook_python_problem(str(fake), onboard.LOCAL) == ""
    assert seen == ["local"]
    seen.clear()
    monkeypatch.setenv("CLAUDE_CODE_REMOTE", "true")
    assert "only root" in onboard.hook_python_problem(str(fake), onboard.LOCAL)
    assert seen == ["strict"]
    seen.clear()
    monkeypatch.delenv("CLAUDE_CODE_REMOTE")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert "only root" in onboard.hook_python_problem(str(fake), onboard.LOCAL)
    assert seen == ["strict"]
