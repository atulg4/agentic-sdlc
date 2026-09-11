from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
REUSABLE = ROOT / ".github" / "workflows" / "reusable-transient-retry.yml"
TEMPLATE = ROOT / "src" / "agentic_sdlc" / "templates" / "github" / "agent-transient-retry.yml"

PINNED_ACTIONS = {
    "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
    "actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
    "actions/github-script@3a2844b7e9c422d3c10d287c895573f7108da1b3",
}


def _yaml(path: Path) -> dict:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _uses(document: dict) -> list[str]:
    found: list[str] = []
    for job in document["jobs"].values():
        if isinstance(job.get("uses"), str):
            found.append(job["uses"])
        for step in job.get("steps", []) or []:
            if isinstance(step.get("uses"), str):
                found.append(step["uses"])
    return found


def test_every_third_party_action_is_pinned_to_a_reviewed_commit() -> None:
    for document in (_yaml(REUSABLE), _yaml(TEMPLATE)):
        for uses in _uses(document):
            if uses.startswith("PLATFORM_REPOSITORY/"):
                assert uses.endswith("@PLATFORM_COMMIT_SHA")
                continue
            action, _, ref = uses.partition("@")
            assert len(ref) == 40 and set(ref) <= set("0123456789abcdef"), uses
            assert f"{action}@{ref}" in PINNED_ACTIONS, uses


def test_retry_workflow_never_receives_an_ai_or_publisher_credential() -> None:
    text = REUSABLE.read_text(encoding="utf-8")
    for forbidden in (
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "PUBLISHER_APP_PRIVATE_KEY",
        "claude-code-action",
        "codex-action",
    ):
        assert forbidden not in text
    document = _yaml(REUSABLE)
    assert set(document[True]["workflow_call"].get("secrets", {})) == {"PLATFORM_READ_TOKEN"}


def test_workflow_is_least_privilege_and_only_the_rerun_job_writes_actions() -> None:
    document = _yaml(REUSABLE)
    assert document["permissions"] == {}
    jobs = document["jobs"]
    assert jobs["classify"]["permissions"] == {
        "actions": "read",
        "checks": "read",
        "contents": "read",
        "issues": "read",
        "pull-requests": "read",
    }
    assert jobs["record"]["permissions"] == {"issues": "write"}
    assert jobs["rerun"]["permissions"] == {"actions": "write", "pull-requests": "read"}
    # The job that can restart jobs holds no comment authority, and the job that
    # writes durable evidence cannot restart anything.
    assert "issues" not in jobs["rerun"]["permissions"]
    assert "actions" not in jobs["record"]["permissions"]


def test_retry_uses_the_native_token_and_reruns_only_failed_jobs() -> None:
    text = REUSABLE.read_text(encoding="utf-8")
    assert "GH_TOKEN: ${{ github.token }}" in text
    assert "actions/runs/${RUN_ID}/rerun-failed-jobs" in text
    assert "rerun-workflow" not in text
    assert "/rerun\n" not in text
    assert "--method POST" in text
    assert text.count("--method POST") == 1


def test_retry_is_bound_to_the_unchanged_exact_head_and_fails_closed() -> None:
    text = REUSABLE.read_text(encoding="utf-8")
    assert 'test "$(jq -r \'.head.sha\' <<< "$pr")" = "$HEAD_SHA"' in text
    assert 'test "$(jq -r \'.head_sha\' <<< "$run")" = "$HEAD_SHA"' in text
    assert 'test "$(jq -r \'.head.repo.full_name\' <<< "$pr")" = "$GITHUB_REPOSITORY"' in text
    assert 'test "$(jq -r \'.status\' <<< "$run")" = completed' in text
    # Nothing here may approve, merge, deploy, or relax a required check.
    lowered = text.lower()
    for forbidden in ("merge", "deploy", "approve", "branch-protection", "required_status_checks"):
        assert forbidden not in lowered


def test_durable_evidence_is_recorded_before_the_rerun_can_run() -> None:
    document = _yaml(REUSABLE)
    jobs = document["jobs"]
    assert jobs["record"]["needs"] == "classify"
    assert jobs["rerun"]["needs"] == ["classify", "record"]
    assert jobs["rerun"]["if"] == "needs.classify.outputs.action == 'retry_failed_jobs'"
    assert document["concurrency"]["cancel-in-progress"] is False
    assert document["concurrency"]["group"] == (
        "forge-transient-retry-${{ github.repository }}-${{ inputs.head_sha }}"
    )


def test_classifier_and_budget_come_from_the_pinned_platform_engine() -> None:
    text = REUSABLE.read_text(encoding="utf-8")
    assert "python3 -m agentic_sdlc classify-failure" in text
    assert "python3 -m agentic_sdlc decide-infra-retry" in text
    assert '--event-key "run-${RUN_ID}-attempt-${RUN_ATTEMPT}"' in text
    assert "--comments .agentic-retry/comments.json" in text
    assert "persist-credentials: false" in text


def test_consumer_template_covers_events_and_the_hourly_watchdog() -> None:
    document = _yaml(TEMPLATE)
    triggers = document[True]
    assert triggers["workflow_run"]["types"] == ["completed"]
    assert "check_suite" in triggers
    assert triggers["schedule"] == [{"cron": "23 * * * *"}]
    assert document["permissions"] == {}
    assert document["jobs"]["discover"]["permissions"] == {
        "actions": "read",
        "pull-requests": "read",
    }
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "FORGE_MAX_TRANSIENT_RETRIES || '3'" in text
    assert "Transient infrastructure retry" in text
    assert "pr.head.repo?.full_name !== `${owner}/${repo}`" in text


def test_consumer_template_never_receives_provider_api_keys_or_merge_authority() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    lowered = text.lower()
    for forbidden in (
        "CLAUDE_CODE_OAUTH_TOKEN",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "PUBLISHER_APP_PRIVATE_KEY",
    ):
        assert forbidden not in text
    for forbidden in ("merge", "deploy", "broker", "direct_api"):
        assert forbidden not in lowered
    document = _yaml(TEMPLATE)
    assert set(document["jobs"]["retry"]["secrets"]) == {"PLATFORM_READ_TOKEN"}
