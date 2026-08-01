from __future__ import annotations

from pathlib import Path

import pytest


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
