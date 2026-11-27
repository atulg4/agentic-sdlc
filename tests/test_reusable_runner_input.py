"""Every reusable workflow must let the consumer choose the runner.

Consumers that pay for GitHub-hosted minutes can point every job at a
self-hosted runner by passing ``runs_on`` as a JSON string; the default keeps
GitHub-hosted ``ubuntu-latest`` so existing callers are unaffected.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

WORKFLOWS = sorted((Path(__file__).parents[1] / ".github" / "workflows").glob("reusable-*.yml"))


def _load(path: Path) -> dict:
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_runs_on_input_defaults_to_github_hosted(path: Path) -> None:
    inputs = _load(path)[True]["workflow_call"]["inputs"]
    runs_on = inputs["runs_on"]
    assert runs_on["type"] == "string"
    assert runs_on["required"] is False
    assert json.loads(runs_on["default"]) == "ubuntu-latest"


#: Jobs that run on a runner input of their own instead of `runs_on` -- still the consumer's
#: choice. The API-only lease cleanup must not depend on the runner whose loss it recovers from
#: (Codex 4203698218).
_RUNNER_INPUT = {("reusable-implement.yml", "release_lease_on_failure"): "lease_cleanup_runs_on"}


@pytest.mark.parametrize(("workflow", "name"), sorted(_RUNNER_INPUT))
def test_separate_runner_inputs_are_consumer_choices_defaulting_to_hosted(workflow, name):
    path = WORKFLOWS[0].parent / workflow
    declared = _load(path)[True]["workflow_call"]["inputs"][_RUNNER_INPUT[(workflow, name)]]
    assert declared["type"] == "string" and declared["required"] is False
    assert json.loads(declared["default"]) == "ubuntu-latest"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_job_honours_runs_on_input(path: Path) -> None:
    jobs = _load(path)["jobs"]
    for name, job in jobs.items():
        if "uses" in job:
            continue  # nested reusable call; it forwards its own runs_on
        expected = _RUNNER_INPUT.get((path.name, name), "runs_on")
        assert job["runs-on"] == f"${{{{ fromJSON(inputs.{expected}) }}}}", f"{path.name}:{name}"
    assert "ubuntu-latest" not in path.read_text(encoding="utf-8").replace(
        "default: '\"ubuntu-latest\"'", ""
    ).replace("e.g. '\"ubuntu-latest\"'", "")
