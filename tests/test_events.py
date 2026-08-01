from __future__ import annotations

import pytest

from agentic_sdlc.events import EventError, normalize_event
from agentic_sdlc.models import WorkKind


def test_normalize_github_issue_event() -> None:
    event = normalize_event(
        "github",
        {
            "action": "labeled",
            "repository": {"full_name": "atulg4/project"},
            "sender": {"login": "atulg4"},
            "issue": {
                "number": 12,
                "title": "Build feature",
                "body": "spec",
                "labels": [{"name": "agent-ready"}],
            },
        },
    )
    assert event.kind == WorkKind.ISSUE
    assert event.repository == "atulg4/project"
    assert event.labels == ("agent-ready",)


def test_normalize_gitlab_merge_request_event() -> None:
    event = normalize_event(
        "gitlab",
        {
            "object_kind": "merge_request",
            "project": {"path_with_namespace": "atulg4/project"},
            "user": {"username": "atulg4"},
            "labels": [{"title": "agent-review"}],
            "object_attributes": {
                "iid": 9,
                "title": "Change",
                "description": "body",
                "action": "update",
            },
        },
    )
    assert event.kind == WorkKind.CHANGE_REQUEST
    assert event.number == 9
    assert event.labels == ("agent-review",)


def test_unknown_event_is_rejected() -> None:
    with pytest.raises(EventError):
        normalize_event("gitlab", {"object_kind": "wiki_page"})
