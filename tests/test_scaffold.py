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
