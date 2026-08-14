from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "reusable-plan.yml"


def _document() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _job(document: str, name: str, next_name: str | None = None) -> str:
    start = document.index(f"\n  {name}:\n")
    if next_name is None:
        return document[start:]
    end = document.index(f"\n  {next_name}:\n", start + 1)
    return document[start:end]


def test_planning_uses_subscription_oauth_without_openai_key_dependency() -> None:
    document = _document()
    plan = _job(document, "plan", "publish")

    assert "CLAUDE_CODE_OAUTH_TOKEN" in document
    assert "claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}" in plan
    assert "OPENAI_API_KEY" not in document
    assert "openai/codex-action" not in document
    assert "claude-opus-5" in plan


def test_planner_has_no_shell_tool_and_scrubs_subprocess_environment() -> None:
    plan = _job(_document(), "plan", "publish")

    assert 'CLAUDE_CODE_SUBPROCESS_ENV_SCRUB: "1"' in plan
    assert '--allowedTools "Read,Write,Glob,Grep"' in plan
    assert '--allowedTools "Read,Write,Glob,Grep,Bash"' not in plan
    assert "Bash" not in plan.split("claude_args:", 1)[1].split(
        "Prove planning did not mutate repository source", 1
    )[0]


def test_planner_records_and_verifies_source_integrity_around_ai_step() -> None:
    plan = _job(_document(), "plan", "publish")

    snapshot = "Snapshot tracked source before planning"
    invoke = "Produce read-only architecture plan with Claude Max OAuth"
    verify = "Prove planning did not mutate repository source"
    artifact = "Verify plan artifact exists"

    assert snapshot in plan
    assert "git ls-files -z | xargs -0 sha256sum" in plan
    assert invoke in plan
    assert verify in plan
    assert 'sha256sum --check --strict "$RUNNER_TEMP/agent-plan-source.sha256"' in plan
    assert "git diff --quiet --" in plan
    assert "git diff --cached --quiet --" in plan
    assert 'git status --porcelain --untracked-files=all' in plan
    assert plan.index(snapshot) < plan.index(invoke) < plan.index(verify) < plan.index(artifact)


def test_ai_planning_job_cannot_publish_or_write_repository_contents() -> None:
    plan = _job(_document(), "plan", "publish")

    assert "contents: read" in plan
    assert "contents: write" not in plan
    assert "issues: write" not in plan
    assert "pull-requests: write" not in plan


def test_publisher_is_separated_from_ai_credential() -> None:
    publish = _job(_document(), "publish")

    assert "issues: write" in publish
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in publish
    assert "claude-code-action" not in publish
    assert "openai/codex-action" not in publish
