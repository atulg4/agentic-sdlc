"""The implementation prompt must state the deterministic gates the patch must pass."""

from __future__ import annotations

from pathlib import Path

from agentic_sdlc.policy import load_policy
from agentic_sdlc.task_spec import parse_task, render_prompt


def _task(valid_body: str):
    return parse_task("Add a gate", valid_body, ("agent-ready", "human-review-required"))


def test_implement_prompt_states_forbidden_paths_caps_and_formatter(
    policy_file: Path, valid_body: str
) -> None:
    policy = load_policy(policy_file)
    prompt = render_prompt(_task(valid_body), "implement", policy=policy)

    assert "NEVER create, edit, or delete files matching:" in prompt
    for pattern in policy.forbidden_paths:
        assert pattern in prompt
    assert f"at most {policy.max_changed_files} changed files" in prompt
    assert f"{policy.max_diff_lines} changed lines" in prompt
    assert "`ruff check`, `black`, and `isort`" in prompt
    assert "empty working tree is a failure" in prompt
    # Guidance sits above the untrusted body so the body cannot override it.
    assert prompt.index("Repository policy") < prompt.index("<untrusted-work-request>")


def test_plan_and_review_prompts_do_not_carry_implementation_guidance(
    policy_file: Path, valid_body: str
) -> None:
    policy = load_policy(policy_file)
    for mode in ("plan", "review"):
        assert "Repository policy" not in render_prompt(_task(valid_body), mode, policy=policy)


def test_prompt_without_policy_is_unchanged(valid_body: str) -> None:
    assert "Repository policy" not in render_prompt(_task(valid_body), "implement")
