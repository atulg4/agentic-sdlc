from pathlib import Path

import yaml


ROOT = Path(__file__).parents[1]
REUSABLE = ROOT / ".github" / "workflows" / "reusable-repair.yml"
TEMPLATE = ROOT / "src" / "agentic_sdlc" / "templates" / "github" / "agent-repair.yml"


def _yaml(path: Path) -> dict:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_repair_is_exact_head_bounded_and_uses_review_artifact() -> None:
    text = REUSABLE.read_text(encoding="utf-8")
    assert "independent-review-${{ inputs.review_run_id }}" in text
    assert "repair requires an exact changes_requested review" in text
    assert "test \"$(jq -r '.head.sha' <<< \"$pr\")\" = \"$HEAD_SHA\"" in text
    assert 'test "$REPAIR_CYCLE" -le "$MAX_REPAIR_CYCLES"' in text
    assert "git push origin \"HEAD:refs/heads/${HEAD_REF}\"" in text


def test_ai_repair_job_has_no_repository_write_or_publisher_secret() -> None:
    document = _yaml(REUSABLE)
    generate = document["jobs"]["generate_patch"]
    assert generate["permissions"] == {"contents": "read", "id-token": "write"}
    generate_text = yaml.safe_dump(generate)
    assert "PUBLISHER_APP_PRIVATE_KEY" not in generate_text
    assert "OPENAI_API_KEY" not in generate_text
    assert "ANTHROPIC_API_KEY" not in generate_text
    assert "claude_code_oauth_token" in generate_text
    assert "anthropic_api_key" not in generate_text


def test_verification_executes_without_write_authority_or_ai_secret() -> None:
    document = _yaml(REUSABLE)
    verify = document["jobs"]["verify"]
    assert verify["permissions"] == {"contents": "read"}
    verify_text = yaml.safe_dump(verify)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in verify_text
    assert "PUBLISHER_APP_PRIVATE_KEY" not in verify_text
    assert "persist-credentials: false" in verify_text
    assert "run-gates" in verify_text


def test_publisher_is_separate_and_rebinds_before_fast_forward_push() -> None:
    document = _yaml(REUSABLE)
    publish = document["jobs"]["publish"]
    assert publish["needs"] == ["prepare", "verify"]
    assert publish["permissions"] == {"contents": "read"}
    publish_text = yaml.safe_dump(publish)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in publish_text
    assert "actions/create-github-app-token" in publish_text
    assert "Rebind publisher to exact unchanged PR head" in publish_text
    assert "git ls-remote origin" in publish_text
    assert "git push origin" in publish_text


def test_consumer_template_fails_fast_and_never_receives_provider_api_keys() -> None:
    document = _yaml(TEMPLATE)
    assert document["permissions"] == {}
    preflight = document["jobs"]["auth_preflight"]
    assert preflight["permissions"] == {}
    repair = document["jobs"]["repair"]
    assert repair["needs"] == "auth_preflight"
    assert repair["with"]["max_repair_cycles"] == 3
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "CLAUDE_CODE_OAUTH_TOKEN" in text
    assert "PUBLISHER_APP_PRIVATE_KEY" in text
    assert "OPENAI_API_KEY" not in text
    assert "ANTHROPIC_API_KEY" not in text
    assert "direct_api" not in text
    assert "deploy" not in text.lower()
    assert "broker" not in text.lower()
