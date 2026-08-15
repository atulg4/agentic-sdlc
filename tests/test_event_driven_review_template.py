from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / "src" / "agentic_sdlc" / "templates" / "github" / "agent-review.yml"


def _document() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_review_waits_for_successful_ci_workflow_run_instead_of_pr_polling() -> None:
    document = _document()

    assert "workflow_run:" in document
    assert "workflows: [CI]" in document
    assert "types: [completed]" in document
    assert "github.event.workflow_run.conclusion == 'success'" in document
    assert "github.event.workflow_run.event == 'pull_request'" in document
    assert "\n  pull_request:\n" not in document


def test_ci_completion_is_bound_to_exact_open_same_repository_pr_head() -> None:
    document = _document()

    assert "RUN_HEAD_SHA: ${{ github.event.workflow_run.head_sha }}" in document
    assert 'test "$state" = open' in document
    assert 'test "$draft" = false' in document
    assert 'test "$head_repo" = "$GITHUB_REPOSITORY"' in document
    assert 'test "$head_sha" = "$RUN_HEAD_SHA"' in document
    assert '[[ "$base_sha" =~ ^[0-9a-f]{40}$ ]]' in document


def test_deterministic_pre_review_must_pass_before_claude_review() -> None:
    document = _document()
    pre_review = document.index("\n  deterministic_pre_review:\n")
    review = document.index("\n  review:\n", pre_review)

    assert pre_review < review
    assert "reusable-pre-review.yml@PLATFORM_COMMIT_SHA" in document
    assert "platform_ref: PLATFORM_COMMIT_SHA" in document
    assert "platform_repository: PLATFORM_REPOSITORY" in document
    assert "needs: [review_eligibility, deterministic_pre_review]" in document


def test_event_driven_review_is_claude_subscription_oauth_only() -> None:
    document = _document()

    assert "CLAUDE_CODE_OAUTH_TOKEN" in document
    assert "OPENAI_API_KEY" not in document
    assert "ANTHROPIC_API_KEY" not in document
    assert "openai/codex-action" not in document
    assert "anthropic_api_key:" not in document


def test_event_dispatcher_has_no_repository_write_or_pr_code_checkout() -> None:
    document = _document()
    eligibility = document[
        document.index("\n  review_eligibility:\n") : document.index(
            "\n  deterministic_pre_review:\n"
        )
    ]

    assert "pull-requests: read" in eligibility
    assert "contents: write" not in eligibility
    assert "pull-requests: write" not in eligibility
    assert "actions/checkout" not in eligibility
    assert "gh api --method GET" in eligibility


def test_review_concurrency_collapses_obsolete_ci_completions_by_head() -> None:
    document = _document()

    assert "group: independent-review-${{ github.event.workflow_run.head_sha }}" in document
    assert "cancel-in-progress: true" in document
