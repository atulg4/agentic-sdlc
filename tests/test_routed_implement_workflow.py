"""Contract tests for routed executor dispatch in the implementation workflow."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from agentic_sdlc.openai_compatible import ROUTED_MIN_CONTEXT_WINDOW

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "reusable-implement.yml"


def _document() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _steps(job: str) -> list[dict[str, object]]:
    parsed = yaml.safe_load(_document())
    return parsed["jobs"][job]["steps"]


def _step(job: str, name_fragment: str) -> dict[str, object]:
    matches = [step for step in _steps(job) if name_fragment in str(step.get("name", ""))]
    assert len(matches) == 1, f"{name_fragment} matched {len(matches)} steps in {job}"
    return matches[0]


def test_registry_routing_requires_route_mode_and_fails_closed() -> None:
    guard = _step("prepare", "Require route mode when an executor registry is supplied")

    assert guard["if"] == "inputs.executor_registry_path != '' && inputs.agent != 'route'"
    assert "exit 2" in str(guard["run"])


def test_patch_generation_dispatches_only_from_the_selected_executor() -> None:
    for step in _steps("generate_patch"):
        condition = str(step.get("if", ""))
        assert "inputs.agent" not in condition, step.get("name")
        assert "inputs.claude_auth_mode" not in condition, step.get("name")

    assert _step("generate_patch", "Codex")["if"] == (
        "needs.prepare.outputs.selected_provider == 'codex'"
    )
    routed = _step("generate_patch", "routed OpenAI-compatible provider")
    assert routed["if"] == (
        'contains(fromJSON(\'["deepseek","zai","kimi"]\'), needs.prepare.outputs.selected_provider)'
    )
    unsupported = _step("generate_patch", "Stop when selected adapter is not installed")
    assert "inputs.agent" not in str(unsupported["if"])
    assert "exit 2" in str(unsupported["run"])


def test_legacy_agent_input_is_translated_into_a_single_candidate_route() -> None:
    route = str(_step("prepare", "Route implementation executor")["run"])

    assert "provider=anthropic" in route
    assert "provider=codex" in route
    assert "auth_mode=oauth" in route
    assert "auth_mode=api-key" in route
    assert "requires executor_registry_path" in route


def test_routed_anthropic_execution_installs_bubblewrap() -> None:
    install = _step("generate_patch", "Install bubblewrap")

    assert install["if"] == "needs.prepare.outputs.selected_provider == 'anthropic'"
    assert "bubblewrap" in str(install["run"])


def test_routed_anthropic_execution_honors_the_selected_executor_identity() -> None:
    oauth = _step("generate_patch", "Max/Pro OAuth")
    api = _step("generate_patch", "direct API billing")

    assert oauth["if"] == (
        "needs.prepare.outputs.selected_provider == 'anthropic' && "
        "needs.prepare.outputs.selected_auth_mode == 'oauth'"
    )
    assert api["if"] == (
        "needs.prepare.outputs.selected_provider == 'anthropic' && "
        "needs.prepare.outputs.selected_auth_mode == 'api-key'"
    )
    for step in (oauth, api):
        claude_args = str(step["with"]["claude_args"])
        assert "--model ${{ needs.prepare.outputs.selected_model }}" in claude_args
        assert "claude-opus-5" not in claude_args

    unsupported = _step("generate_patch", "routed Anthropic auth mode")
    assert "exit 2" in str(unsupported["run"])


def test_route_requires_a_context_window_matching_the_generated_bundle() -> None:
    route = str(_step("prepare", "Route implementation executor")["run"])
    match = re.search(r"--min-context-window (\d+)", route)

    assert match is not None
    assert int(match.group(1)) == ROUTED_MIN_CONTEXT_WINDOW


def test_routed_adapter_receives_the_route_decision_for_in_process_fallback() -> None:
    routed = str(_step("generate_patch", "routed OpenAI-compatible provider")["run"])

    assert ".agentic-input/route.json" in routed
