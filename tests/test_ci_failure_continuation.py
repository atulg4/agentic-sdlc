from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
REUSABLE = ROOT / ".github" / "workflows" / "reusable-ci-repair.yml"
CI_TEMPLATE = ROOT / "src" / "agentic_sdlc" / "templates" / "github" / "agent-ci-continuation.yml"
REVIEW_TEMPLATE = (
    ROOT / "src" / "agentic_sdlc" / "templates" / "github" / "agent-review-continuation.yml"
)


def _yaml(path: Path) -> dict:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_ci_failure_is_event_driven_and_never_blindly_rerun() -> None:
    text = CI_TEMPLATE.read_text(encoding="utf-8")
    assert "workflow_run:" in text
    assert "workflows: [CI]" in text
    assert "types: [completed]" in text
    assert "github.event.workflow_run.conclusion == 'failure'" in text
    assert "rerun" not in text.lower()
    assert "re-run" not in text.lower()


def test_ci_continuation_binds_same_repository_exact_head_and_shared_budget() -> None:
    text = CI_TEMPLATE.read_text(encoding="utf-8")
    assert "pr.data.head.repo.full_name" in text
    assert "pr.data.head.sha === sha" in text
    assert "forge-repair head=" in text
    assert "github-actions[bot]" in text
    assert "FORGE_MAX_REPAIR_CYCLES" in text
    assert "forge-repair-continuation-${{ github.event.workflow_run.head_sha }}" in text


def test_review_and_ci_continuations_serialize_on_same_exact_head() -> None:
    ci = _yaml(CI_TEMPLATE)
    review = _yaml(REVIEW_TEMPLATE)
    assert ci["concurrency"] == review["concurrency"]
    assert ci["concurrency"]["cancel-in-progress"] is False
    assert ci["concurrency"]["group"] == (
        "forge-repair-continuation-${{ github.event.workflow_run.head_sha }}"
    )


def test_ci_repair_validates_trusted_failed_run_before_claude() -> None:
    text = REUSABLE.read_text(encoding="utf-8")
    assert "repos/${GITHUB_REPOSITORY}/actions/runs/${CI_RUN_ID}" in text
    assert "'.head_sha' <<< \"$run\"" in text
    assert "'.conclusion' <<< \"$run\"" in text
    assert "test \"$path\" = '.github/workflows/ci.yml'" in text
    assert "gh run view \"$CI_RUN_ID\" --log-failed" in text
    assert "truncated to final 200000 bytes" in text


def test_ci_repair_preserves_ai_publisher_and_verifier_separation() -> None:
    document = _yaml(REUSABLE)
    assert document["permissions"] == {}
    generate = document["jobs"]["generate_patch"]
    verify = document["jobs"]["verify"]
    publish = document["jobs"]["publish"]

    assert generate["permissions"] == {"contents": "read", "id-token": "write"}
    assert verify["permissions"] == {"contents": "read"}
    assert publish["permissions"] == {"contents": "read"}

    generate_text = yaml.safe_dump(generate)
    verify_text = yaml.safe_dump(verify)
    publish_text = yaml.safe_dump(publish)
    assert "claude_code_oauth_token" in generate_text
    assert "PUBLISHER_APP_PRIVATE_KEY" not in generate_text
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in verify_text
    assert "PUBLISHER_APP_PRIVATE_KEY" not in verify_text
    assert "actions/create-github-app-token" in publish_text
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in publish_text


def test_ci_repair_forbids_direct_provider_keys_and_deployment_authority() -> None:
    text = REUSABLE.read_text(encoding="utf-8") + CI_TEMPLATE.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY" not in text
    assert "ANTHROPIC_API_KEY" not in text
    assert "anthropic_api_key" not in text
    assert "CLAUDE_CODE_OAUTH_TOKEN" in text
    assert "git push origin" in REUSABLE.read_text(encoding="utf-8")
    assert "deploy" in REUSABLE.read_text(encoding="utf-8").lower()  # only explicit prohibition
    assert "Do not push, merge, deploy" in REUSABLE.read_text(encoding="utf-8")
