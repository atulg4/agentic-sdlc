from __future__ import annotations

from pathlib import Path

import yaml

from agentic_sdlc.scaffold import scaffold_project

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "reusable-spec.yml"
TEMPLATES = (
    ROOT / "templates/github/auto-spec.yml",
    ROOT / "src/agentic_sdlc/templates/github/agent-auto-spec.yml",
)


def _job(name: str, next_name: str | None = None) -> str:
    document = WORKFLOW.read_text(encoding="utf-8")
    start = document.index(f"\n  {name}:\n")
    if next_name is None:
        return document[start:]
    return document[start : document.index(f"\n  {next_name}:\n", start + 1)]


def _code(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_spec_drafting_never_grants_implementation_labels() -> None:
    document = _code(WORKFLOW.read_text(encoding="utf-8"))
    for label in ("agent-ready", "implementation-approved", "human-review-required"):
        assert label not in document
    assert document.count("addLabels(") == 1
    assert "addLabels({ owner, repo, issue_number, labels: ['spec-drafted'] })" in document
    for template in TEMPLATES:
        text = _code(template.read_text(encoding="utf-8"))
        assert "agent-ready" not in text
        assert "implementation-approved" not in text


def test_publisher_holds_issue_before_writing_the_drafted_body() -> None:
    publish = _job("publish")

    hold = publish.index("labels: ['spec-drafted']")
    release_removed = publish.index("name: 'spec-approved'")
    body_write = publish.index("github.rest.issues.update(")
    assert hold < release_removed < body_write
    assert "merge-spec" in publish
    assert '--expected-body-sha256 "$BODY_SHA256"' in publish
    assert "Drafted dependency #%s does not exist" in publish


def test_ai_credentials_and_issue_write_authority_are_separated() -> None:
    prepare = _job("prepare", "draft")
    draft = _job("draft", "publish")
    publish = _job("publish")

    for secret in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        assert secret not in prepare
        assert secret not in publish
    assert "issues: write" not in draft
    assert "contents: write" not in draft
    assert "issues: write" in publish
    assert "claude-code-action" not in publish
    assert "codex-action" not in publish
    assert "actions/checkout" not in publish


def test_drafter_is_read_only_and_proves_source_integrity() -> None:
    draft = _job("draft", "publish")

    assert '--allowedTools "Read,Write,Glob,Grep"' in draft
    assert "Bash" not in draft.split("claude_args:", 1)[1].split("- name:", 1)[0]
    assert "sandbox: read-only" in draft
    assert 'CLAUDE_CODE_SUBPROCESS_ENV_SCRUB: "1"' in draft
    snapshot = draft.index("Snapshot tracked source before drafting")
    verify = draft.index("Prove drafting did not mutate repository source")
    assert snapshot < draft.index("claude-code-action") < verify


def test_prepare_routes_the_specification_planner_through_executor_routing() -> None:
    prepare = _job("prepare", "draft")

    assert "--mission-id specification-planner" in prepare
    assert "--task-class planning" in prepare
    assert "spec-check" in prepare
    assert "admin|write" in prepare
    issue_allowlist = '[[ "$ISSUE_NUMBER" =~ ^[1-9][0-9]{0,9}$ ]]'
    assert prepare.index(issue_allowlist) < prepare.index("Read issue without shell interpolation")
    assert 'index("spec-drafted")' in prepare


def test_consumer_templates_trigger_on_issue_intake_events() -> None:
    for template in TEMPLATES:
        document = yaml.safe_load(template.read_text(encoding="utf-8"))
        assert document[True]["issues"]["types"] == ["opened", "edited", "labeled"]
        job = document["jobs"]["spec"]
        assert "reusable-spec.yml@PLATFORM_COMMIT_SHA" in job["uses"]
        assert "!contains(github.event.issue.labels.*.name, 'spec-drafted')" in job["if"]
        assert job["with"]["issue_number"] == "${{ format('{0}', github.event.issue.number) }}"
        assert job["permissions"] == {"contents": "read", "issues": "write", "id-token": "write"}
    assert TEMPLATES[0].read_text(encoding="utf-8") == TEMPLATES[1].read_text(encoding="utf-8")


def test_level_three_scaffold_installs_the_spec_stage(tmp_path: Path) -> None:
    repository = tmp_path / "consumer"
    (repository / ".git").mkdir(parents=True)
    created = scaffold_project(
        repository,
        provider="github",
        project_id="owner/consumer",
        platform_repository="owner/agentic-sdlc",
        platform_ref="e" * 40,
        automation_level=3,
    )

    spec = repository / ".github/workflows/agent-auto-spec.yml"
    assert spec in created
    assert "owner/agentic-sdlc/.github/workflows/reusable-spec.yml@" + "e" * 40 in spec.read_text(
        encoding="utf-8"
    )
