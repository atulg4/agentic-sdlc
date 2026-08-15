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


def test_sandbox_dependencies_are_installed_before_isolated_claude_review() -> None:
    review = _review_job()
    install = "Install Claude subprocess isolation dependencies"
    invoke = "Independently review the exact diff with Claude Max OAuth"

    assert install in review
    assert "sudo apt-get install -y -q bubblewrap socat" in review
    assert "command -v bwrap >/dev/null" in review
    assert "command -v socat >/dev/null" in review
    assert 'CLAUDE_CODE_SUBPROCESS_ENV_SCRUB: "1"' in review
    assert review.index(install) < review.index(invoke)


def test_ai_review_job_remains_read_only_to_repository() -> None:
    review = _review_job()

    assert "contents: read" in review
    assert "contents: write" not in review
    assert "pull-requests: write" not in review
    assert "issues: write" not in review
