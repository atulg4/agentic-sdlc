"""Normalize GitHub and GitLab webhook payloads."""

from __future__ import annotations

from typing import Any

from .models import WorkEvent, WorkKind


class EventError(ValueError):
    """Raised when a provider payload cannot be normalized."""


def _github_labels(item: dict[str, Any]) -> tuple[str, ...]:
    values = []
    for label in item.get("labels", []):
        values.append(str(label.get("name", "")) if isinstance(label, dict) else str(label))
    return tuple(sorted(value for value in values if value))


def normalize_github(payload: dict[str, Any]) -> WorkEvent:
    repository = str(payload.get("repository", {}).get("full_name", ""))
    actor = str(payload.get("sender", {}).get("login", ""))
    action = str(payload.get("action", "unknown"))
    if "issue" in payload:
        item = payload["issue"]
        kind = WorkKind.ISSUE
    elif "pull_request" in payload:
        item = payload["pull_request"]
        kind = WorkKind.CHANGE_REQUEST
    else:
        raise EventError("GitHub payload contains neither issue nor pull_request")
    return WorkEvent(
        provider="github",
        kind=kind,
        action=action,
        repository=repository,
        number=int(item["number"]) if item.get("number") is not None else None,
        title=str(item.get("title", "")),
        body=str(item.get("body") or ""),
        labels=_github_labels(item),
        actor=actor,
        raw=payload,
    )


def normalize_gitlab(payload: dict[str, Any]) -> WorkEvent:
    object_kind = str(payload.get("object_kind", ""))
    attributes = payload.get("object_attributes", {})
    if object_kind == "issue":
        kind = WorkKind.ISSUE
    elif object_kind == "merge_request":
        kind = WorkKind.CHANGE_REQUEST
    elif object_kind == "pipeline":
        kind = WorkKind.PIPELINE
    else:
        raise EventError(f"unsupported GitLab object_kind: {object_kind or 'missing'}")
    labels = tuple(
        sorted(
            str(label.get("title", "")) for label in payload.get("labels", []) if label.get("title")
        )
    )
    project = payload.get("project", {})
    user = payload.get("user", {})
    return WorkEvent(
        provider="gitlab",
        kind=kind,
        action=str(attributes.get("action") or attributes.get("state") or "unknown"),
        repository=str(project.get("path_with_namespace", "")),
        number=int(attributes["iid"]) if attributes.get("iid") is not None else None,
        title=str(attributes.get("title", "")),
        body=str(attributes.get("description") or ""),
        labels=labels,
        actor=str(user.get("username") or user.get("name") or ""),
        raw=payload,
    )


def normalize_event(provider: str, payload: dict[str, Any]) -> WorkEvent:
    if provider == "github":
        return normalize_github(payload)
    if provider == "gitlab":
        return normalize_gitlab(payload)
    raise EventError(f"unsupported provider: {provider}")
