from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "reusable-review.yml"


def _review_job() -> str:
    document = WORKFLOW.read_text(encoding="utf-8")
    start = document.index("\n  review:\n")
    end = document.index("\n  enforce_review:\n", start)
    return document[start:end]


def test_review_uses_subscription_oauth_without_paid_api_fallbacks() -> None:
    document = WORKFLOW.read_text(encoding="utf-8")
    review = _review_job()

    assert "CLAUDE_CODE_OAUTH_TOKEN" in document
    assert "claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}" in review
    assert "OPENAI_API_KEY" not in document
    assert "ANTHROPIC_API_KEY" not in document
    assert "openai/codex-action" not in document
    assert "anthropic_api_key:" not in document


def test_trusted_workflow_materializes_and_hashes_exact_review_diff() -> None:
    review = _review_job()
    materialize = "Materialize exact immutable review diff"
    invoke = "Independently review the exact diff with Claude Max OAuth"

    assert materialize in review
    assert 'git diff --find-renames "${BASE_SHA}...${HEAD_SHA}"' in review
    assert "sha256sum .agentic-review/change.diff" in review
    assert "steps.review-diff.outputs.sha256" in review
    assert review.index(materialize) < review.index(invoke)


def test_claude_reviews_materialized_diff_without_shell_access() -> None:
    review = _review_job()

    assert "Read .agentic-review/change.diff as the authoritative review scope" in review
    assert "Do not invoke git or a shell to reconstruct the diff" in review
    assert '--allowedTools "Read,Write,Glob,Grep"' in review
    assert "Read,Write,Glob,Grep,Bash" not in review
    assert "bubblewrap" not in review
    assert "socat" not in review
    assert 'CLAUDE_CODE_SUBPROCESS_ENV_SCRUB: "1"' in review


def test_review_scope_and_repository_mutation_are_deterministically_verified() -> None:
    review = _review_job()

    assert "Verify review scope and repository integrity" in review
    assert 'echo "${EXPECTED_DIFF_SHA256}  .agentic-review/change.diff" | sha256sum -c -' in review
    assert "git diff --exit-code -- ." in review
    assert "git ls-files --others --exclude-standard" in review
    assert "actual == expected" in review
    assert "'schema.json', 'change.diff', 'review.json'" in review


def test_ai_review_job_remains_read_only_to_repository() -> None:
    review = _review_job()

    assert "contents: read" in review
    assert "contents: write" not in review
    assert "pull-requests: write" not in review
    assert "issues: write" not in review
