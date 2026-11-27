from __future__ import annotations

from pathlib import Path

import pytest

from agentic_sdlc.scaffold import ScaffoldError, scaffold_project


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "consumer"
    repository.mkdir()
    (repository / ".git").mkdir()
    return repository


def test_github_scaffold_defaults_to_plan_only(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    created = scaffold_project(
        repository,
        provider="github",
        project_id="owner/consumer",
        platform_repository="owner/agentic-sdlc",
        platform_ref="a" * 40,
    )

    assert repository / ".github/workflows/agent-plan.yml" in created
    assert not (repository / ".github/workflows/agent-implement.yml").exists()
    plan = (repository / ".github/workflows/agent-plan.yml").read_text(encoding="utf-8")
    assert "owner/agentic-sdlc" in plan
    assert "a" * 40 in plan
    assert "PLATFORM_COMMIT_SHA" not in plan
    assert "PLATFORM_REPOSITORY" not in plan


def test_level_two_adds_implementation_and_review(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    scaffold_project(
        repository,
        provider="github",
        project_id="owner/consumer",
        platform_repository="owner/agentic-sdlc",
        platform_ref="b" * 40,
        automation_level=2,
        publisher_bot_login="consumer-publisher[bot]",
    )

    implementation = repository / ".github/workflows/agent-implement.yml"
    assert implementation.is_file()
    assert (repository / ".github/workflows/agent-review.yml").is_file()
    content = implementation.read_text(encoding="utf-8")
    assert "publisher_app_client_id: ${{ vars.PUBLISHER_APP_CLIENT_ID }}" in content
    assert "PUBLISHER_APP_PRIVATE_KEY" in content
    assert "contents: write" in content
    assert "issues: write" not in content
    assert "pull-requests: write" not in content


def test_level_three_adds_label_driven_automation(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    created = scaffold_project(
        repository,
        provider="github",
        project_id="owner/consumer",
        platform_repository="owner/agentic-sdlc",
        platform_ref="e" * 40,
        automation_level=3,
        publisher_bot_login="consumer-publisher[bot]",
    )

    auto_plan = repository / ".github/workflows/agent-auto-plan.yml"
    auto_implement = repository / ".github/workflows/agent-auto-implement.yml"
    assert auto_plan in created
    assert auto_implement in created
    assert "github.event.label.name == 'agent-ready'" in auto_plan.read_text(encoding="utf-8")
    assert "implementation-approved" in auto_implement.read_text(encoding="utf-8")


def test_scaffold_refuses_to_overwrite_existing_policy(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository / "agentic-sdlc.toml").write_text("owner data\n", encoding="utf-8")

    with pytest.raises(ScaffoldError, match="refusing to overwrite"):
        scaffold_project(
            repository,
            provider="github",
            project_id="owner/consumer",
            platform_repository="owner/agentic-sdlc",
            platform_ref="c" * 40,
        )
    assert (repository / "agentic-sdlc.toml").read_text(encoding="utf-8") == "owner data\n"


def test_gitlab_scaffold_separates_component_host_from_project_path(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    scaffold_project(
        repository,
        provider="gitlab",
        project_id="group/consumer",
        platform_repository="gitlab.example.com/group/agentic-sdlc",
        platform_ref="d" * 40,
    )
    include = (repository / ".gitlab-ci.agentic-sdlc.yml").read_text(encoding="utf-8")
    assert "gitlab.example.com/group/agentic-sdlc/agentic-sdlc@" in include
    assert "platform_project_path: group/agentic-sdlc" in include


# Codex 4243386512: the implementation workflow's `claim` posts its lease marker as the
# Publisher App's bot and counts only a bot the policy names, so a level 2/3 GitHub scaffold
# without `[automation] publisher_bot_login` failed every implementation at prepare.
@pytest.mark.parametrize("level", [2, 3])
def test_github_implementation_levels_write_the_publisher_bot(tmp_path: Path, level: int) -> None:
    from agentic_sdlc.policy import load_policy

    repository = _repository(tmp_path)
    scaffold_project(
        repository,
        provider="github",
        project_id="owner/consumer",
        platform_repository="owner/agentic-sdlc",
        platform_ref="b" * 40,
        automation_level=level,
        publisher_bot_login="consumer-publisher[bot]",
    )
    policy = load_policy(repository / "agentic-sdlc.toml")
    assert policy.publisher_bot_login == "consumer-publisher[bot]"


@pytest.mark.parametrize("level", [2, 3])
def test_github_implementation_levels_refuse_without_the_publisher_bot(
    tmp_path: Path, level: int
) -> None:
    repository = _repository(tmp_path)
    with pytest.raises(ScaffoldError, match="--publisher-bot-login"):
        scaffold_project(
            repository,
            provider="github",
            project_id="owner/consumer",
            platform_repository="owner/agentic-sdlc",
            platform_ref="b" * 40,
            automation_level=level,
        )
    assert not any(repository.rglob("*.toml")) and not (repository / ".github").exists()


@pytest.mark.parametrize("login", ["consumer-publisher", "Consumer[bot]", "a b[bot]", "x[bot]\n"])
def test_scaffold_refuses_a_malformed_publisher_bot(tmp_path: Path, login: str) -> None:
    repository = _repository(tmp_path)
    with pytest.raises(ScaffoldError, match="publisher bot login"):
        scaffold_project(
            repository,
            provider="github",
            project_id="owner/consumer",
            platform_repository="owner/agentic-sdlc",
            platform_ref="b" * 40,
            automation_level=2,
            publisher_bot_login=login,
        )
    assert not (repository / "agentic-sdlc.toml").exists()


def test_plan_only_scaffold_needs_no_publisher_bot(tmp_path: Path) -> None:
    from agentic_sdlc.policy import load_policy

    repository = _repository(tmp_path)
    scaffold_project(
        repository,
        provider="github",
        project_id="owner/consumer",
        platform_repository="owner/agentic-sdlc",
        platform_ref="b" * 40,
    )
    policy = load_policy(repository / "agentic-sdlc.toml")
    assert policy.publisher_bot_login == ""


def test_scaffold_cli_takes_the_publisher_bot(tmp_path: Path, capsys) -> None:
    from agentic_sdlc import cli
    from agentic_sdlc.policy import load_policy

    repository = _repository(tmp_path)
    base = [
        "scaffold",
        "--destination",
        str(repository),
        "--provider",
        "github",
        "--project-id",
        "owner/consumer",
        "--platform-repository",
        "owner/agentic-sdlc",
        "--platform-ref",
        "b" * 40,
        "--automation-level",
        "2",
    ]
    assert cli.main(base) == 2
    assert "--publisher-bot-login" in capsys.readouterr().err
    assert cli.main([*base, "--publisher-bot-login", "consumer-publisher[bot]"]) == 0
    policy = load_policy(repository / "agentic-sdlc.toml")
    assert policy.publisher_bot_login == "consumer-publisher[bot]"


def test_the_claim_step_trusts_the_scaffolded_publisher_bot(tmp_path: Path, monkeypatch) -> None:
    """The reusable workflow's claim runs `--config agentic-sdlc.toml`: the scaffolded policy
    alone makes the Publisher bot's lease marker count."""
    import argparse

    from agentic_sdlc import cli, leases

    monkeypatch.delenv(leases.LEASE_BOT_LOGINS_ENV, raising=False)
    repository = _repository(tmp_path)
    scaffold_project(
        repository,
        provider="github",
        project_id="owner/consumer",
        platform_repository="owner/agentic-sdlc",
        platform_ref="b" * 40,
        automation_level=2,
        publisher_bot_login="consumer-publisher[bot]",
    )
    args = argparse.Namespace(
        ttl_minutes=None, config=str(repository / "agentic-sdlc.toml"), project="owner/consumer"
    )
    try:
        cli._lease_ttl(args)
        assert leases.configured_bot_logins() == "consumer-publisher[bot]"
        assert leases.trusted_bot("consumer-publisher[bot]")
    finally:
        leases.use_policy_bot_logins("")


@pytest.mark.parametrize("branch", ["a..b", ".topic", "foo.lock", "team/.hidden", "x/", "a b"])
def test_scaffold_refuses_a_branch_the_policy_loader_rejects(tmp_path: Path, branch: str) -> None:
    """Codex 4243550478: scaffold applies `policy.branch_name_problem`, so it cannot write a
    policy every later load refuses, and refuses before writing anything."""
    repository = _repository(tmp_path)
    with pytest.raises(ScaffoldError, match="default branch"):
        scaffold_project(
            repository,
            provider="github",
            project_id="owner/consumer",
            platform_repository="owner/agentic-sdlc",
            platform_ref="b" * 40,
            default_branch=branch,
        )
    assert not (repository / "agentic-sdlc.toml").exists()
    assert not (repository / ".github").exists()


@pytest.mark.parametrize("branch", ["main", "release+next", "feature@two", "release/2026.10"])
def test_scaffold_accepts_every_branch_the_policy_loader_accepts(
    tmp_path: Path, branch: str
) -> None:
    from agentic_sdlc.policy import load_policy

    repository = _repository(tmp_path)
    scaffold_project(
        repository,
        provider="github",
        project_id="owner/consumer",
        platform_repository="owner/agentic-sdlc",
        platform_ref="b" * 40,
        default_branch=branch,
    )
    policy = load_policy(repository / "agentic-sdlc.toml")
    assert policy.default_branch == branch
