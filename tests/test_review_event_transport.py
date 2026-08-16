from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
CONTINUATION = (
    ROOT / "src" / "agentic_sdlc" / "templates" / "github" / "agent-review-continuation.yml"
)
PROTECTED_MERGE = ROOT / ".github" / "workflows" / "reusable-protected-merge.yml"


def _yaml(path: Path) -> dict:
    loaded = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(loaded, dict)
    return loaded


def test_review_completion_is_the_primary_continuation_trigger() -> None:
    document = _yaml(CONTINUATION)
    assert document["on"]["workflow_run"]["workflows"] == ["Independent agent review"]
    assert document["on"]["workflow_run"]["types"] == ["completed"]
    assert document["permissions"] == {}
    classify = document["jobs"]["classify"]
    assert classify["permissions"] == {
        "actions": "read",
        "issues": "read",
        "pull-requests": "read",
    }


def test_continuation_is_exact_head_fail_closed_and_idempotent() -> None:
    text = CONTINUATION.read_text(encoding="utf-8")
    assert "pr.data.head.repo.full_name" in text
    assert "pr.data.head.sha !== sha" in text
    assert "core.setOutput('action', 'noop')" in text
    assert "independent-review-${{ github.event.workflow_run.id }}" in text
    assert "FORGE_MAX_REPAIR_CYCLES" in text
    assert "forge-repair head=${escaped} cycle=([0-9]+)" in text
    assert "previous >= max" in text
    assert "core.setOutput('action', 'block')" in text


def test_changes_requested_dispatches_accepted_bounded_repair_contract() -> None:
    document = _yaml(CONTINUATION)
    repair = document["jobs"]["repair"]
    inputs = repair["with"]
    assert inputs["pull_request_number"]
    assert inputs["base_sha"]
    assert inputs["head_sha"]
    assert inputs["review_run_id"]
    assert inputs["repair_cycle"]
    assert inputs["max_repair_cycles"]
    assert inputs["config_path"] == "agentic-sdlc.toml"
    text = CONTINUATION.read_text(encoding="utf-8")
    assert "reviewed_head_sha:" not in text
    assert "CLAUDE_CODE_OAUTH_TOKEN" in text
    assert "PUBLISHER_APP_PRIVATE_KEY" in text
    assert "OPENAI_API_KEY" not in text
    assert "ANTHROPIC_API_KEY" not in text


def test_green_review_can_only_reach_protected_merge_when_explicitly_enabled() -> None:
    document = _yaml(CONTINUATION)
    merge = document["jobs"]["merge"]
    assert "FORGE_AUTONOMOUS_MERGE_ENABLED" in merge["if"]
    inputs = merge["with"]
    assert inputs["expected_head_sha"]
    assert inputs["deterministic_checks_json"]
    assert inputs["quality_checks_json"]
    assert inputs["independent_review_checks_json"]
    assert inputs["secret_scan_checks_json"]


def test_protected_merge_is_non_ai_exact_head_and_normal_branch_protected() -> None:
    document = _yaml(PROTECTED_MERGE)
    assert document["permissions"] == {}
    merge = document["jobs"]["merge"]
    assert merge["permissions"] == {
        "checks": "read",
        "contents": "write",
        "pull-requests": "write",
    }
    text = PROTECTED_MERGE.read_text(encoding="utf-8")
    assert "EXPECTED_HEAD_SHA" in text
    assert "GitHubMergeEvidenceCollector" in text
    assert "TrustedProtectedMergeService" in text
    assert "GitHubMergeGateway" in text
    assert "deployment_requested='deployment-requested' in labels" in text
    assert "broker_access_requested='broker-access-requested' in labels" in text
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in text
    assert "OPENAI_API_KEY" not in text
    assert "ANTHROPIC_API_KEY" not in text
    assert "--force" not in text
    assert "bypass" not in text.lower()
