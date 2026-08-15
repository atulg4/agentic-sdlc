from __future__ import annotations

from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "marketmaestro"
WORKFLOWS = EXAMPLE / ".github" / "workflows"
AI_WORKFLOWS = (
    "agent-plan.yml",
    "agent-auto-plan.yml",
    "agent-implement.yml",
    "agent-auto-implement.yml",
    "agent-review.yml",
)


def _documents() -> str:
    return "\n".join((WORKFLOWS / name).read_text(encoding="utf-8") for name in AI_WORKFLOWS)


def test_marketmaestro_example_is_claude_subscription_oauth_only() -> None:
    documents = _documents()

    assert "CLAUDE_CODE_OAUTH_TOKEN" in documents
    assert "OPENAI_API_KEY" not in documents
    assert "ANTHROPIC_API_KEY" not in documents
    assert "openai/codex-action" not in documents
    assert "anthropic_api_key:" not in documents
    assert "agent: codex" not in documents
    assert "agent: claude" in documents
    assert "claude_auth_mode: subscription_oauth" in documents


def test_marketmaestro_example_fails_closed_before_ai_execution() -> None:
    documents = _documents()

    assert documents.count("permissions: {}") >= len(AI_WORKFLOWS)
    assert 'test -n "$CLAUDE_CODE_OAUTH_TOKEN"' in documents
    assert "review_eligibility:" in documents
    assert 'test "$SAME_REPOSITORY" = "true"' in documents
    assert 'test "$IS_DRAFT" = "false"' in documents
    assert "cancel-in-progress: true" in documents


def test_marketmaestro_example_policy_preserves_human_merge_and_governance_guards() -> None:
    policy = tomllib.loads((EXAMPLE / "agentic-sdlc.toml").read_text(encoding="utf-8"))

    assert policy["agents"] == {
        "planner": "claude",
        "implementer": "claude",
        "reviewer": "claude",
    }
    assert policy["policy"]["human_merge_required"] is True
    assert "docs/engineering/**" in policy["policy"]["protected_paths"]
    assert policy["policy"]["low_risk_paths"] == ["docs/forge-generated/**"]
    assert policy["verification"]["gates"] == ["setup", "quality", "test"]
    assert "requirements-web.txt" in policy["commands"]["setup"]
