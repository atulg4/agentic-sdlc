from __future__ import annotations

import subprocess
from pathlib import Path

from agentic_sdlc.pre_review import run_pre_review


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init(repository: Path, *, command: str = "python -m pytest -q") -> str:
    _git(repository, "init", "-q", "-b", "main")
    _git(repository, "config", "user.email", "test@example.com")
    _git(repository, "config", "user.name", "Test")
    (repository / "agentic-sdlc.toml").write_text(
        "[commands]\n"
        f'test = "{command}"\n\n'
        "[verification]\n"
        'gates = ["test"]\n',
        encoding="utf-8",
    )
    (repository / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repository, "add", "--", ".")
    _git(repository, "commit", "-qm", "base")
    return _git(repository, "rev-parse", "HEAD")


def _codes(report: dict[str, object]) -> set[str]:
    findings = report["findings"]
    assert isinstance(findings, list)
    return {str(item["code"]) for item in findings if isinstance(item, dict)}


def test_clean_non_workflow_change_passes(tmp_path: Path) -> None:
    base = _init(tmp_path)
    (tmp_path / "app.py").write_text("VALUE = 2\n", encoding="utf-8")

    report = run_pre_review(tmp_path / "agentic-sdlc.toml", tmp_path, base)

    assert report["passed"] is True
    assert report["findings"] == []


def test_gate_command_environment_prefix_fails_before_review(tmp_path: Path) -> None:
    base = _init(tmp_path)
    (tmp_path / "agentic-sdlc.toml").write_text(
        "[commands]\n"
        'test = "DATABASE_URL=sqlite:///test.db pytest -q"\n\n'
        "[verification]\n"
        'gates = ["test"]\n',
        encoding="utf-8",
    )

    report = run_pre_review(tmp_path / "agentic-sdlc.toml", tmp_path, base)

    assert report["passed"] is False
    assert "gate-command-env-prefix" in _codes(report)


def test_gate_command_shell_metacharacters_fail_before_review(tmp_path: Path) -> None:
    base = _init(tmp_path)
    (tmp_path / "agentic-sdlc.toml").write_text(
        "[commands]\n"
        'test = "python -m pytest -q && python -m ruff check ."\n\n'
        "[verification]\n"
        'gates = ["test"]\n',
        encoding="utf-8",
    )

    report = run_pre_review(tmp_path / "agentic-sdlc.toml", tmp_path, base)

    assert "gate-command-shell-syntax" in _codes(report)


def test_mutable_external_workflow_reference_fails(tmp_path: Path) -> None:
    base = _init(tmp_path)
    workflow = tmp_path / ".github/workflows/agent-review.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "name: review\n"
        "on: pull_request\n"
        "permissions: {}\n"
        "jobs:\n"
        "  review:\n"
        "    permissions:\n"
        "      contents: read\n"
        "    steps:\n"
        "      - uses: anthropics/claude-code-action@main\n"
        "        with:\n"
        "          claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}\n",
        encoding="utf-8",
    )

    report = run_pre_review(tmp_path / "agentic-sdlc.toml", tmp_path, base)

    assert "workflow-mutable-reference" in _codes(report)


def test_direct_claude_job_cannot_receive_repository_write(tmp_path: Path) -> None:
    base = _init(tmp_path)
    workflow = tmp_path / ".github/workflows/agent-review.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "name: review\n"
        "on: pull_request\n"
        "permissions: {}\n"
        "jobs:\n"
        "  review:\n"
        "    permissions:\n"
        "      contents: write\n"
        "    steps:\n"
        "      - uses: anthropics/claude-code-action@"
        "be7b93b1907a4abad570368f3c74b6fe3807510b\n"
        "        with:\n"
        "          claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}\n",
        encoding="utf-8",
    )

    report = run_pre_review(tmp_path / "agentic-sdlc.toml", tmp_path, base)

    assert "claude-job-contents-permission" in _codes(report)


def test_agent_workflow_requires_top_level_default_deny(tmp_path: Path) -> None:
    base = _init(tmp_path)
    workflow = tmp_path / ".github/workflows/agent-review.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "name: review\n"
        "on: pull_request\n"
        "jobs:\n"
        "  review:\n"
        "    permissions:\n"
        "      contents: read\n"
        "      id-token: write\n"
        "    steps:\n"
        "      - uses: anthropics/claude-code-action@"
        "be7b93b1907a4abad570368f3c74b6fe3807510b\n"
        "        with:\n"
        "          claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}\n",
        encoding="utf-8",
    )

    report = run_pre_review(tmp_path / "agentic-sdlc.toml", tmp_path, base)

    assert "workflow-default-permissions" in _codes(report)


def test_provider_api_key_addition_fails_but_preexisting_text_is_not_reflagged(
    tmp_path: Path,
) -> None:
    base = _init(tmp_path)
    target = tmp_path / "docs.md"
    target.write_text("No provider credentials here.\n", encoding="utf-8")
    _git(tmp_path, "add", "--", ".")
    _git(tmp_path, "commit", "-qm", "docs base")
    base = _git(tmp_path, "rev-parse", "HEAD")
    target.write_text("No provider credentials here.\nOPENAI_API_KEY must not return.\n", encoding="utf-8")

    report = run_pre_review(tmp_path / "agentic-sdlc.toml", tmp_path, base)

    assert "forbidden-provider-addition" in _codes(report)


def test_configurable_required_policy_phrase_fails_closed(tmp_path: Path) -> None:
    base = _init(tmp_path)
    (tmp_path / "AGENTS.md").write_text("Agents may open pull requests.\n", encoding="utf-8")
    (tmp_path / "agentic-sdlc.toml").write_text(
        "[commands]\n"
        'test = "python -m pytest -q"\n\n'
        "[verification]\n"
        'gates = ["test"]\n\n'
        "[pre_review.required_phrases]\n"
        '"AGENTS.md" = ["agents may never approve pull requests"]\n',
        encoding="utf-8",
    )

    report = run_pre_review(tmp_path / "agentic-sdlc.toml", tmp_path, base)

    assert "required-policy-phrase-missing" in _codes(report)
