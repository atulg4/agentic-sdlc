"""Versioned, project-neutral registry of the repositories Forge operates on.

Forge is a general-purpose software factory: no core behavior may assume one
consumer repository. The registry is the single declaration of which projects
exist, which repositories and environments belong to them, and which lifecycle
capabilities each project has enabled.

The registry is intentionally optional. Single-project deployments never need
one, so every command keeps working when the file is absent — the empty
registry simply asserts nothing about projects. When a file *is* declared it is
loaded fail-closed: unknown keys, unknown capabilities, duplicate identifiers,
and anything that looks like a credential are rejected rather than ignored.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "EMPTY_PROJECT_REGISTRY",
    "ENVIRONMENT_KINDS",
    "PROJECT_CAPABILITIES",
    "REGISTRY_SCHEMA_VERSION",
    "SCM_PROVIDERS",
    "ProjectEnvironment",
    "ProjectProfile",
    "ProjectRegistry",
    "ProjectRegistryError",
    "ProjectRepository",
    "load_project_registry",
    "read_optional_project_registry",
    "read_project_registry",
]

REGISTRY_SCHEMA_VERSION = 1

SCM_PROVIDERS = frozenset({"github", "gitlab"})

REPOSITORY_ROLES = frozenset({"primary", "secondary"})

ENVIRONMENT_KINDS = frozenset({"development", "preview", "staging", "production"})

PROJECT_CAPABILITIES = frozenset(
    {
        "intake",
        "specification",
        "planning",
        "dependency-resolution",
        "dispatch",
        "implementation",
        "deterministic-verification",
        "independent-review",
        "bounded-repair",
        "protected-merge",
        "deploy",
        "deployment-verification",
        "rollback",
    }
)

_PROJECT_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_REPOSITORY_ID = re.compile(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+")
_ENVIRONMENT_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_GROUP = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_SECRET_KEY = re.compile(r"credential|secret|token|password|passphrase|apikey|api_key", re.I)

_REGISTRY_KEYS = frozenset({"schemaVersion", "projects"})
_PROJECT_KEYS = frozenset(
    {
        "projectId",
        "displayName",
        "group",
        "defaultBranch",
        "repositories",
        "environments",
        "capabilities",
    }
)
_REPOSITORY_KEYS = frozenset({"provider", "identifier", "role", "defaultBranch"})
_ENVIRONMENT_KEYS = frozenset({"name", "kind", "deployTarget", "requiresHumanApproval"})


class ProjectRegistryError(ValueError):
    """Raised when a project registry document cannot be trusted."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ProjectRegistryError(message)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    _require(isinstance(value, Mapping), f"{label} must be an object")
    assert isinstance(value, Mapping)
    for key in value:
        _require(isinstance(key, str), f"{label} keys must be strings")
        _require(
            _SECRET_KEY.search(key) is None,
            f"{label} must not carry credentials or secrets: {key}",
        )
    return value


def _known_keys(entry: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(entry) - allowed)
    if unknown:
        raise ProjectRegistryError(f"{label} declares unknown keys: " + ", ".join(unknown))


def _text(entry: Mapping[str, Any], key: str, label: str, *, required: bool = True) -> str:
    value = entry.get(key, "")
    _require(isinstance(value, str), f"{label}: {key} must be a string")
    assert isinstance(value, str)
    value = value.strip()
    _require(not required or bool(value), f"{label}: {key} is required")
    return value


def _pattern(value: str, pattern: re.Pattern[str], label: str) -> str:
    _require(bool(pattern.fullmatch(value)), f"{label} must match {pattern.pattern}: {value}")
    return value


def _flag(entry: Mapping[str, Any], key: str, label: str, default: bool) -> bool:
    value = entry.get(key, default)
    _require(isinstance(value, bool), f"{label}: {key} must be a boolean")
    assert isinstance(value, bool)
    return value


@dataclass(frozen=True)
class ProjectRepository:
    """One repository a project owns, on any supported SCM provider."""

    provider: str
    identifier: str
    role: str = "primary"
    default_branch: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "identifier": self.identifier,
            "role": self.role,
            "defaultBranch": self.default_branch,
        }


@dataclass(frozen=True)
class ProjectEnvironment:
    """A deployable environment. Forge observes it; it never holds its credentials."""

    name: str
    kind: str
    deploy_target: str = ""
    requires_human_approval: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "deployTarget": self.deploy_target,
            "requiresHumanApproval": self.requires_human_approval,
        }


@dataclass(frozen=True)
class ProjectProfile:
    """A registered project: stable identity plus the surfaces Forge may act on."""

    project_id: str
    display_name: str
    default_branch: str
    repositories: tuple[ProjectRepository, ...]
    environments: tuple[ProjectEnvironment, ...] = ()
    capabilities: tuple[str, ...] = ()
    group: str = ""

    @property
    def primary_repository(self) -> ProjectRepository:
        return self.repositories[0]

    def has_capability(self, capability: str) -> bool:
        return capability in self.capabilities

    def environment(self, name: str) -> ProjectEnvironment:
        for item in self.environments:
            if item.name == name:
                return item
        raise ProjectRegistryError(f"project {self.project_id}: unknown environment: {name}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "projectId": self.project_id,
            "displayName": self.display_name,
            "group": self.group,
            "defaultBranch": self.default_branch,
            "repositories": [item.as_dict() for item in self.repositories],
            "environments": [item.as_dict() for item in self.environments],
            "capabilities": list(self.capabilities),
        }


@dataclass(frozen=True)
class ProjectRegistry:
    """An immutable, versioned set of registered projects."""

    projects: tuple[ProjectProfile, ...] = ()
    schema_version: int = REGISTRY_SCHEMA_VERSION

    def __contains__(self, project_id: object) -> bool:
        return any(project.project_id == project_id for project in self.projects)

    def __len__(self) -> int:
        return len(self.projects)

    @property
    def is_empty(self) -> bool:
        return not self.projects

    def project_ids(self) -> tuple[str, ...]:
        return tuple(project.project_id for project in self.projects)

    def get(self, project_id: str) -> ProjectProfile:
        for project in self.projects:
            if project.project_id == project_id:
                return project
        raise ProjectRegistryError(f"unknown project: {project_id}")

    def in_group(self, group: str) -> tuple[ProjectProfile, ...]:
        """Projects in one optional grouping, in registry order."""
        return tuple(project for project in self.projects if project.group == group)

    def groups(self) -> dict[str, tuple[str, ...]]:
        """Group name -> project IDs. Ungrouped projects are omitted."""
        grouped: dict[str, list[str]] = {}
        for project in self.projects:
            if project.group:
                grouped.setdefault(project.group, []).append(project.project_id)
        return {name: tuple(ids) for name, ids in sorted(grouped.items())}

    def for_repository(self, provider: str, identifier: str) -> ProjectProfile:
        """Resolve which project owns one repository, without assuming a consumer."""
        for project in self.projects:
            for repository in project.repositories:
                if repository.provider == provider and repository.identifier == identifier:
                    return project
        raise ProjectRegistryError(f"no registered project owns {provider}:{identifier}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "projects": [project.as_dict() for project in self.projects],
        }


EMPTY_PROJECT_REGISTRY = ProjectRegistry()


def _load_repository(raw: Any, label: str) -> ProjectRepository:
    entry = _mapping(raw, label)
    _known_keys(entry, _REPOSITORY_KEYS, label)
    provider = _text(entry, "provider", label)
    _require(provider in SCM_PROVIDERS, f"{label}: unknown provider: {provider}")
    identifier = _pattern(_text(entry, "identifier", label), _REPOSITORY_ID, f"{label}: identifier")
    role = _text(entry, "role", label, required=False) or "primary"
    _require(role in REPOSITORY_ROLES, f"{label}: unknown role: {role}")
    return ProjectRepository(
        provider=provider,
        identifier=identifier,
        role=role,
        default_branch=_text(entry, "defaultBranch", label, required=False),
    )


def _load_environment(raw: Any, label: str) -> ProjectEnvironment:
    entry = _mapping(raw, label)
    _known_keys(entry, _ENVIRONMENT_KEYS, label)
    name = _pattern(_text(entry, "name", label), _ENVIRONMENT_NAME, f"{label}: name")
    kind = _text(entry, "kind", label)
    _require(kind in ENVIRONMENT_KINDS, f"{label}: unknown kind: {kind}")
    return ProjectEnvironment(
        name=name,
        kind=kind,
        deploy_target=_text(entry, "deployTarget", label, required=False),
        requires_human_approval=_flag(entry, "requiresHumanApproval", label, True),
    )


def _load_capabilities(raw: Any, label: str) -> tuple[str, ...]:
    _require(isinstance(raw, list), f"{label}: capabilities must be an array")
    assert isinstance(raw, list)
    values: list[str] = []
    for item in raw:
        _require(isinstance(item, str), f"{label}: capabilities must be strings")
        assert isinstance(item, str)
        values.append(item.strip())
    unknown = sorted(set(values) - PROJECT_CAPABILITIES)
    if unknown:
        raise ProjectRegistryError(f"{label}: unknown capabilities: " + ", ".join(unknown))
    return tuple(sorted(set(values)))


def _load_project(raw: Any, index: int) -> ProjectProfile:
    entry = _mapping(raw, f"project[{index}]")
    project_id = _pattern(
        _text(entry, "projectId", f"project[{index}]"), _PROJECT_ID, f"project[{index}]: projectId"
    )
    label = f"project {project_id}"
    _known_keys(entry, _PROJECT_KEYS, label)
    group = _text(entry, "group", label, required=False)
    if group:
        _pattern(group, _GROUP, f"{label}: group")

    repositories_raw = entry.get("repositories", [])
    _require(isinstance(repositories_raw, list), f"{label}: repositories must be an array")
    assert isinstance(repositories_raw, list)
    _require(bool(repositories_raw), f"{label}: at least one repository is required")
    repositories = tuple(
        _load_repository(item, f"{label}: repository[{position}]")
        for position, item in enumerate(repositories_raw)
    )
    primary = [item for item in repositories if item.role == "primary"]
    _require(len(primary) == 1, f"{label}: exactly one primary repository is required")
    seen_repositories: set[tuple[str, str]] = set()
    for repository in repositories:
        key = (repository.provider, repository.identifier)
        _require(key not in seen_repositories, f"{label}: duplicate repository: {key[1]}")
        seen_repositories.add(key)
    # The primary repository is always first so callers never re-scan for it.
    repositories = primary[0], *(item for item in repositories if item.role != "primary")

    environments_raw = entry.get("environments", [])
    _require(isinstance(environments_raw, list), f"{label}: environments must be an array")
    assert isinstance(environments_raw, list)
    environments = tuple(
        _load_environment(item, f"{label}: environment[{position}]")
        for position, item in enumerate(environments_raw)
    )
    seen_environments: set[str] = set()
    for environment in environments:
        _require(
            environment.name not in seen_environments,
            f"{label}: duplicate environment: {environment.name}",
        )
        seen_environments.add(environment.name)

    return ProjectProfile(
        project_id=project_id,
        display_name=_text(entry, "displayName", label),
        default_branch=_text(entry, "defaultBranch", label, required=False) or "main",
        repositories=repositories,
        environments=environments,
        capabilities=_load_capabilities(entry.get("capabilities", []), label),
        group=group,
    )


def load_project_registry(document: Any) -> ProjectRegistry:
    """Parse a registry document fail-closed.

    Every rejection is explicit: an unsupported schema version, an unknown key,
    an unknown capability, a duplicate project or repository, or a key whose
    name suggests it carries a credential.
    """
    entry = _mapping(document, "project registry")
    _known_keys(entry, _REGISTRY_KEYS, "project registry")
    version = entry.get("schemaVersion")
    _require(
        version == REGISTRY_SCHEMA_VERSION,
        f"project registry schemaVersion must be {REGISTRY_SCHEMA_VERSION}, not {version!r}",
    )
    projects_raw = entry.get("projects", [])
    _require(isinstance(projects_raw, list), "project registry projects must be an array")
    assert isinstance(projects_raw, list)

    projects: list[ProjectProfile] = []
    seen: set[str] = set()
    owned: dict[tuple[str, str], str] = {}
    for index, item in enumerate(projects_raw):
        project = _load_project(item, index)
        _require(project.project_id not in seen, f"duplicate project: {project.project_id}")
        seen.add(project.project_id)
        for repository in project.repositories:
            key = (repository.provider, repository.identifier)
            owner = owned.get(key)
            _require(
                owner is None,
                f"repository {repository.identifier} is claimed by both "
                f"{owner} and {project.project_id}",
            )
            owned[key] = project.project_id
        projects.append(project)
    return ProjectRegistry(projects=tuple(projects))


def read_project_registry(path: str | Path) -> ProjectRegistry:
    """Read and validate a registry file. A missing file is an error here."""
    location = Path(path)
    try:
        raw = location.read_text(encoding="utf-8")
    except OSError as error:
        raise ProjectRegistryError(f"unable to read project registry: {error}") from error
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ProjectRegistryError(f"project registry is not valid JSON: {error}") from error
    return load_project_registry(document)


def read_optional_project_registry(path: str | Path | None) -> ProjectRegistry:
    """Read a registry that may not exist.

    Forge deployments that manage a single repository never need a registry, so
    an undeclared or absent file yields the empty registry instead of failing.
    A declared file that exists is still validated fail-closed.
    """
    if path is None or not str(path).strip():
        return EMPTY_PROJECT_REGISTRY
    location = Path(path)
    if not location.is_file():
        return EMPTY_PROJECT_REGISTRY
    return read_project_registry(location)
