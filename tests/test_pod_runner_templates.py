"""A consumer's ARC target must reach every job, including reusable calls."""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]
TEMPLATES = [
    *sorted((ROOT / "src/agentic_sdlc/templates/github").glob("*.yml")),
    *sorted((ROOT / "templates/github").glob("*.yml")),
    *sorted((ROOT / "examples/marketmaestro/.github/workflows").glob("*.yml")),
]
DIRECT = "${{ vars.FORGE_RUNNER || 'ubuntu-latest' }}"
REUSABLE = "${{ toJSON(vars.FORGE_RUNNER || 'ubuntu-latest') }}"


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_template_job_honors_the_consumer_runner(path):
    jobs = yaml.safe_load(path.read_text())["jobs"]
    assert jobs
    for name, job in jobs.items():
        if "uses" in job:
            assert job.get("with", {}).get("runs_on") == REUSABLE, (path.name, name)
        else:
            assert job["runs-on"] == DIRECT, (path.name, name)
