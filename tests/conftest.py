from __future__ import annotations

import os
from pathlib import Path

import pytest

_REAL_ACCESS = os.access


@pytest.fixture(autouse=True)
def _system_dirs_read_only(monkeypatch, tmp_path_factory):
    """Model an ordinary user whose system directories are not writable, wherever the suite runs.

    The commit guard needs the lease when a directory PATH searches before a program git runs is
    writable (Codex 4240952107). Run as root -- or on a CI runner whose tool-cache `bin` precedes
    /usr/bin -- every PATH entry is writable and every read-only git command would be guarded.
    Writability is therefore real only under pytest's own temporary directories, where tests
    build the writable entries they mean; everything else answers "not writable"."""
    base = os.path.realpath(tmp_path_factory.getbasetemp())

    def access(path, mode, *args, **kwargs):
        if mode & os.W_OK and not isinstance(path, int):
            real = os.path.realpath(os.fspath(path))
            if real != base and not real.startswith(base + os.sep):
                return False
        return _REAL_ACCESS(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "access", access)


@pytest.fixture
def policy_file(tmp_path: Path) -> Path:
    path = tmp_path / "agentic-sdlc.toml"
    path.write_text(
        """
version = 1

[project]
id = "example/project"
provider = "github"
default_branch = "main"

[automation]
ready_label = "agent-ready"
human_review_label = "human-review-required"

[policy]
human_merge_required = true
max_changed_files = 3
max_diff_lines = 100
max_patch_bytes = 1000
forbidden_paths = [".github/workflows/**", ".env*"]
protected_paths = ["src/auth/**", "migrations/**"]
low_risk_paths = ["docs/**", "**/*.md"]
forbidden_task_patterns = ['\\bplace\\b.{0,30}\\blive trade\\b']
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def valid_body() -> str:
    return """
## Summary
Add a bounded, observable behavior.

## Acceptance Criteria
- [ ] The behavior is visible to the user.
- [ ] Failure returns a generic error.

## Required Tests
- Unit test proves the successful path.
- Unit test proves the failure path.

## Non-Goals
- Production deployment.

## Dependencies
None
""".strip()
