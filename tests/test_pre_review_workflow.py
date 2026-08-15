from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "reusable-pre-review.yml"


def test_pre_review_is_credential_free_and_read_only() -> None:
    document = WORKFLOW.read_text(encoding="utf-8")

    assert "permissions: {}" in document
    assert "contents: read" in document
    assert "contents: write" not in document
    assert "pull-requests: write" not in document
    assert "issues: write" not in document
    assert "actions: write" not in document
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in document
    assert "OPENAI_API_KEY" not in document
    assert "ANTHROPIC_API_KEY" not in document
    assert "PUBLISHER_APP_PRIVATE_KEY" not in document
    assert "claude-code-action" not in document
    assert "codex-action" not in document


def test_pre_review_requires_exact_pr_and_platform_identity() -> None:
    document = WORKFLOW.read_text(encoding="utf-8")

    assert '[[ "$PR_NUMBER" =~ ^[1-9][0-9]{0,9}$ ]]' in document
    assert '[[ "$BASE_SHA" =~ ^[0-9a-f]{40}$ ]]' in document
    assert '[[ "$HEAD_SHA" =~ ^[0-9a-f]{40}$ ]]' in document
    assert '[[ "$PLATFORM_REF" =~ ^[0-9a-f]{40}$ ]]' in document
    assert "repository: ${{ inputs.platform_repository }}" in document
    assert "ref: ${{ inputs.platform_ref }}" in document
    assert 'test "$(git rev-parse HEAD)" = "$HEAD_SHA"' in document


def test_pre_review_runs_before_any_ai_review_and_executes_no_consumer_commands() -> None:
    document = WORKFLOW.read_text(encoding="utf-8")

    assert "from agentic_sdlc.pre_review import run_pre_review" in document
    assert "run-gates" not in document
    assert "pytest" not in document
    assert "npm test" not in document
    assert "git apply" not in document
    assert "pull_request_target" not in document


def test_pre_review_evidence_is_always_retained() -> None:
    document = WORKFLOW.read_text(encoding="utf-8")

    assert "if: always()" in document
    assert "deterministic-pre-review-${{ github.run_id }}" in document
    assert "if-no-files-found: error" in document
