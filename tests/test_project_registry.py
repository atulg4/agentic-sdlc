from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agentic_sdlc.project_registry import (
    EMPTY_PROJECT_REGISTRY,
    REGISTRY_SCHEMA_VERSION,
    ProjectRegistryError,
    load_project_registry,
    read_optional_project_registry,
    read_project_registry,
)


def _project(project_id: str = "marketmaestro", **overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "projectId": project_id,
        "displayName": project_id.title(),
        "defaultBranch": "main",
        "repositories": [
            {"provider": "github", "identifier": f"example/{project_id}", "role": "primary"}
        ],
        "environments": [{"name": "production", "kind": "production", "deployTarget": "cloud-run"}],
        "capabilities": ["planning", "implementation", "independent-review"],
    }
    entry.update(overrides)
    return entry


def _registry(*projects: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemaVersion": REGISTRY_SCHEMA_VERSION,
        "projects": list(projects) or [_project()],
    }


def test_registry_round_trips_through_its_own_document() -> None:
    document = _registry(_project("alpha", group="maestro"), _project("beta", group="maestro"))

    registry = load_project_registry(document)
    reloaded = load_project_registry(registry.as_dict())

    assert registry.as_dict() == reloaded.as_dict()
    assert registry.project_ids() == ("alpha", "beta")
    assert registry.get("alpha").display_name == "Alpha"
    assert registry.groups() == {"maestro": ("alpha", "beta")}
    assert [item.project_id for item in registry.in_group("maestro")] == ["alpha", "beta"]


def test_registry_resolves_a_project_from_any_repository_without_assuming_one() -> None:
    document = _registry(
        _project(
            "comic-maestro",
            repositories=[
                {"provider": "github", "identifier": "example/comic-maestro"},
                {
                    "provider": "gitlab",
                    "identifier": "example/group/comic-assets",
                    "role": "secondary",
                },
            ],
        ),
        _project("sigma-maestro"),
    )

    registry = load_project_registry(document)

    assert registry.for_repository("gitlab", "example/group/comic-assets").project_id == (
        "comic-maestro"
    )
    assert registry.for_repository("github", "example/sigma-maestro").project_id == "sigma-maestro"
    with pytest.raises(ProjectRegistryError, match="no registered project owns"):
        registry.for_repository("github", "example/unregistered")


def test_primary_repository_is_first_and_exactly_one_is_required() -> None:
    registry = load_project_registry(
        _registry(
            _project(
                "alpha",
                repositories=[
                    {"provider": "github", "identifier": "example/docs", "role": "secondary"},
                    {"provider": "github", "identifier": "example/alpha", "role": "primary"},
                ],
            )
        )
    )

    assert registry.get("alpha").primary_repository.identifier == "example/alpha"

    with pytest.raises(ProjectRegistryError, match="exactly one primary repository"):
        load_project_registry(
            _registry(
                _project(
                    "beta",
                    repositories=[
                        {"provider": "github", "identifier": "example/one"},
                        {"provider": "github", "identifier": "example/two"},
                    ],
                )
            )
        )


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"projects": []}, "schemaVersion must be 1"),
        ({"schemaVersion": 2, "projects": []}, "schemaVersion must be 1"),
        ({"schemaVersion": 1, "projects": [], "extra": 1}, "unknown keys"),
    ],
)
def test_registry_document_fails_closed(document: dict[str, Any], message: str) -> None:
    with pytest.raises(ProjectRegistryError, match=message):
        load_project_registry(document)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"capabilities": ["deploy", "teleport"]}, "unknown capabilities"),
        ({"projectId": "Not Valid"}, "projectId must match"),
        ({"displayName": ""}, "displayName is required"),
        ({"repositories": []}, "at least one repository"),
        ({"unexpected": True}, "unknown keys"),
        ({"apiToken": "abc"}, "must not carry credentials or secrets"),
        (
            {"repositories": [{"provider": "svn", "identifier": "example/alpha"}]},
            "unknown provider",
        ),
        (
            {"repositories": [{"provider": "github", "identifier": "not-a-repository"}]},
            "identifier must match",
        ),
        (
            {"environments": [{"name": "prod", "kind": "orbit"}]},
            "unknown kind",
        ),
        (
            {
                "environments": [
                    {"name": "prod", "kind": "production"},
                    {"name": "prod", "kind": "staging"},
                ]
            },
            "duplicate environment",
        ),
    ],
)
def test_project_entry_fails_closed(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ProjectRegistryError, match=message):
        load_project_registry(_registry(_project("alpha", **overrides)))


def test_duplicate_projects_and_shared_repositories_are_rejected() -> None:
    with pytest.raises(ProjectRegistryError, match="duplicate project"):
        load_project_registry(_registry(_project("alpha"), _project("alpha")))

    with pytest.raises(ProjectRegistryError, match="is claimed by both"):
        load_project_registry(
            _registry(
                _project("alpha"),
                _project(
                    "beta",
                    repositories=[{"provider": "github", "identifier": "example/alpha"}],
                ),
            )
        )


def test_registry_serialization_contains_no_credentials() -> None:
    registry = load_project_registry(_registry())
    serialized = json.dumps(registry.as_dict()).lower()

    for forbidden in ("credential", "secret", "token", "password"):
        assert forbidden not in serialized


def test_missing_registry_file_yields_the_empty_registry(tmp_path: Path) -> None:
    assert read_optional_project_registry(None) is EMPTY_PROJECT_REGISTRY
    assert read_optional_project_registry("") is EMPTY_PROJECT_REGISTRY
    assert read_optional_project_registry(tmp_path / "absent.json") is EMPTY_PROJECT_REGISTRY
    assert EMPTY_PROJECT_REGISTRY.is_empty
    assert EMPTY_PROJECT_REGISTRY.project_ids() == ()


def test_declared_registry_file_is_still_validated(tmp_path: Path) -> None:
    path = tmp_path / "projects.json"
    path.write_text(json.dumps(_registry()), encoding="utf-8")

    assert read_optional_project_registry(path).project_ids() == ("marketmaestro",)
    assert read_project_registry(path).project_ids() == ("marketmaestro",)

    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ProjectRegistryError, match="not valid JSON"):
        read_optional_project_registry(path)

    with pytest.raises(ProjectRegistryError, match="unable to read"):
        read_project_registry(tmp_path / "absent.json")


def test_environment_lookup_fails_closed() -> None:
    project = load_project_registry(_registry()).get("marketmaestro")

    assert project.environment("production").requires_human_approval is True
    assert project.has_capability("planning") is True
    assert project.has_capability("deploy") is False
    with pytest.raises(ProjectRegistryError, match="unknown environment"):
        project.environment("staging")
