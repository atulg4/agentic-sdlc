"""The idle control plane is bounded and workers are never kept warm."""

from pathlib import Path

import yaml

ARC = Path(__file__).parents[1] / "infra/arc"


def test_workers_use_pinned_image_and_sandbox_without_cluster_credentials():
    worker = yaml.safe_load((ARC / "worker.yaml").read_text())
    spec = worker["template"]["spec"]
    assert spec["runtimeClassName"] == "gvisor"
    assert spec["automountServiceAccountToken"] is False
    container = spec["containers"][0]
    assert container["name"] == "runner"
    assert "@sha256:" in container["image"]
    assert container["command"] == ["/home/runner/run.sh"]
    assert "Unconfined" not in str(spec)
    assert "privileged" not in str(spec)


def test_every_forge_repository_has_a_bounded_pool():
    expected = {
        "marketmaestro": 4,
        "MusicMaestro": 2,
        "MarketingMaestro": 1,
        "openmandate": 1,
        "constellation": 1,
    }
    for repo, maximum in expected.items():
        pool = yaml.safe_load((ARC / "pools" / f"{repo.lower()}.yaml").read_text())
        assert pool["githubConfigUrl"] == f"https://github.com/atulg4/{repo}"
        assert pool["githubConfigSecret"] == "github-pat"
        assert pool["runnerScaleSetName"] == f"{repo.lower()}-runners"
        assert pool["minRunners"] == 0
        assert pool["maxRunners"] == maximum


def test_controller_and_listener_requests_are_explicit_and_small():
    controller = yaml.safe_load((ARC / "controller.yaml").read_text())
    assert controller["resources"]["requests"] == {
        "cpu": "100m",
        "memory": "128Mi",
        "ephemeral-storage": "100Mi",
    }
    listeners = yaml.safe_load((ARC / "listener.yaml").read_text())
    containers = listeners["listenerTemplate"]["spec"]["containers"]
    assert len(containers) == 1
    assert containers[0]["name"] == "listener"
    assert containers[0]["resources"]["requests"] == {
        "cpu": "50m",
        "memory": "64Mi",
        "ephemeral-storage": "100Mi",
    }
    assert listeners["minRunners"] == 0
