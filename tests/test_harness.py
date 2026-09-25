from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from agentic_sdlc.cli import main
from agentic_sdlc.dispatcher import AutonomousIntakeDispatcher
from agentic_sdlc.executors import load_executors
from agentic_sdlc.harness import (
    HarnessError,
    HarnessManifest,
    canonical_json,
    document_digest,
    executor_profile_snapshot,
    load_json,
    load_manifest,
    manifest_schema,
    validate_effective_risk,
    validate_executor_binding,
    validate_manifest,
)
from agentic_sdlc.missions import load_registry
from agentic_sdlc.models import RiskLevel, WorkEvent, WorkKind
from agentic_sdlc.orchestration import Orchestrator
from agentic_sdlc.policy import load_policy

EXAMPLE = Path(__file__).resolve().parents[1] / "examples/harness"


@pytest.fixture
def manifest() -> dict:
    return json.loads((EXAMPLE / "manifest.json").read_bytes())


def _mission():
    return load_registry(None, load_policy(EXAMPLE / "agentic-sdlc.toml")).get(
        "implementation-worker"
    )


def test_schema_is_valid_and_every_array_declares_order() -> None:
    schema = manifest_schema()
    Draft202012Validator.check_schema(schema)

    def check(value):
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert value["additionalProperties"] is False
                assert set(value["required"]) == set(value["properties"])
            if value.get("type") == "array":
                assert value["x-order"] in {"ordered", "set"}
            for item in value.values():
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)

    check(schema)


def test_jcs_numbers_strings_and_utf16_property_order() -> None:
    # RFC 8785 numbers/strings, and supplementary characters before U+FB33
    # under UTF-16 ordering (the reverse of Python's Unicode code-point order).
    assert canonical_json({"z": 2.0, "a": 0.002, "small": 1e-27}) == (
        b'{"a":0.002,"small":1e-27,"z":2}'
    )
    assert canonical_json({"\ufb33": 1, "\U0001f600": 2}) == (
        '{"\U0001f600":2,"\ufb33":1}'.encode()
    )
    assert canonical_json(load_json(b'{"text":"\\u00e9"}')) == canonical_json({"text": "é"})
    assert canonical_json({"text": "é"}) != canonical_json({"text": "e\u0301"})


@pytest.mark.parametrize(
    "raw",
    [
        b'{"a":1,"a":2}',
        b'{"a":{"b":1,"b":2}}',
        b'{"n":NaN}',
        b'{"n":Infinity}',
        b'{"n":1e999}',
        b'{"n":9007199254740992}',
        b'{"s":"\\ud800"}',
        b"\xff",
        b"{broken",
        b"[" * 40 + b"0" + b"]" * 40,
    ],
)
def test_rejects_non_interoperable_or_ambiguous_json(raw: bytes) -> None:
    with pytest.raises(HarnessError):
        load_json(raw)


def test_rejects_large_document() -> None:
    with pytest.raises(HarnessError, match="size"):
        load_json(b'"' + b"x" * 1_000_000 + b'"')


def test_canonical_manifest_ignores_set_order_but_preserves_skill_order(manifest: dict) -> None:
    manifest["skills"].append({"id": "second", "version": "1.0.0", "contentDigest": "e" * 64})
    first = validate_manifest(manifest)
    manifest["classification"]["domains"].reverse()
    manifest["mission"]["requiredCapabilities"].reverse()
    manifest["budget"]["maxCostUsd"] = 2
    assert validate_manifest(manifest).digest == first.digest
    manifest["skills"].reverse()
    assert validate_manifest(manifest).digest != first.digest
    document = first.as_dict()
    document["budget"]["maxCostUsd"] = 100
    assert first.as_dict()["budget"]["maxCostUsd"] == 2


def test_direct_construction_validates_and_normalizes(manifest: dict) -> None:
    manifest["verification"]["requiredGateIds"].reverse()
    value = HarnessManifest(json.dumps(manifest, indent=2).encode())
    assert value == validate_manifest(manifest)
    manifest["verification"]["requiredGateIds"] = []
    with pytest.raises(HarnessError):
        HarnessManifest(canonical_json(manifest))


def test_direct_construction_rejects_mutable_buffers(manifest: dict) -> None:
    with pytest.raises(HarnessError, match="immutable bytes"):
        HarnessManifest(bytearray(canonical_json(manifest)))


def test_preserves_dispatcher_work_and_attempt_identities(manifest: dict) -> None:
    event = WorkEvent(
        provider="github",
        kind=WorkKind.ISSUE,
        action="opened",
        repository="example/project",
        number=42,
        title="Example task",
        body="",
        labels=("forge-managed",),
        actor="example-owner",
        raw={},
    )
    decision = AutonomousIntakeDispatcher(Orchestrator()).dispatch(
        event, timestamp="2026-09-25T00:00:00Z"
    )
    assert decision.accepted
    manifest["work"]["unitId"] = decision.unit_id
    manifest["lineage"]["attemptId"] = f"{decision.unit_id}:run-001"
    manifest["lineage"]["budgetLedgerId"] = decision.unit_id
    assert validate_manifest(manifest).as_dict()["work"]["unitId"] == decision.unit_id


@pytest.mark.parametrize("field", ["schemaVersion", "policy", "risk", "executor", "permissions"])
def test_rejects_missing_required_fields(manifest: dict, field: str) -> None:
    del manifest[field]
    with pytest.raises(HarnessError):
        validate_manifest(manifest)


@pytest.mark.parametrize("field", [None, "executor", "permissions", "policy", "budget"])
def test_rejects_unknown_keys_at_every_boundary(manifest: dict, field: str | None) -> None:
    target = manifest if field is None else manifest[field]
    target["allowMerge"] = True
    with pytest.raises(HarnessError, match="additionalProperties"):
        validate_manifest(manifest)


@pytest.mark.parametrize(
    "path", ["../outside", "/root", "src/../secret", "src//x", "C:/x", "src\\x", "./src"]
)
def test_rejects_escaping_or_ambiguous_paths(manifest: dict, path: str) -> None:
    manifest["permissions"]["writePaths"] = [path]
    with pytest.raises(HarnessError, match="relative"):
        validate_manifest(manifest)


@pytest.mark.parametrize(
    "change",
    [
        ("schemaVersion", None, 2),
        ("budget", "maxCostUsd", float("nan")),
        ("budget", "maxTotalTokens", True),
        ("budget", "maxRuntimeSeconds", 0),
        ("risk", "effective", "unrestricted"),
        ("policy", "platformCommit", "a" * 40 + "\n"),
        ("work", "workRef", "other/project#42"),
        ("work", "publishedHeadCommit", "a" * 40),
        ("work", "candidateTree", "a" * 40),
        ("permissions", "denyCapabilities", ["merge"]),
    ],
)
def test_rejects_invalid_semantics(manifest: dict, change: tuple) -> None:
    field, child, value = change
    if child is None:
        manifest[field] = value
    else:
        manifest[field][child] = value
    with pytest.raises(HarnessError):
        validate_manifest(manifest)


def test_rejects_duplicate_skill_identity_and_missing_merge_gate(manifest: dict) -> None:
    other = copy.deepcopy(manifest)
    other["skills"].append({**other["skills"][0], "version": "2.0.0"})
    with pytest.raises(HarnessError, match="skill ID"):
        validate_manifest(other)
    manifest["verification"]["requiredGateIds"].remove("human-merge-approval")
    with pytest.raises(HarnessError, match="human merge"):
        validate_manifest(manifest)


def test_medium_mission_cannot_hide_critical_effective_risk(manifest: dict) -> None:
    mission = _mission()
    assert mission.risk is RiskLevel.MEDIUM
    manifest["risk"]["effective"] = "critical"
    with pytest.raises(HarnessError, match="critical effective risk"):
        validate_effective_risk(
            validate_manifest(manifest), mission, expected_risk=RiskLevel.CRITICAL
        )
    manifest["risk"]["effective"] = "medium"
    with pytest.raises(HarnessError, match="trusted assessment"):
        validate_effective_risk(
            validate_manifest(manifest), mission, expected_risk=RiskLevel.CRITICAL
        )


def test_high_effective_risk_requires_additional_gate(manifest: dict) -> None:
    manifest["risk"]["effective"] = "high"
    with pytest.raises(HarnessError, match="architect/security"):
        validate_effective_risk(
            validate_manifest(manifest), _mission(), expected_risk=RiskLevel.HIGH
        )
    manifest["verification"]["requiredGateIds"].append("architect-security-review")
    validate_effective_risk(validate_manifest(manifest), _mission(), expected_risk=RiskLevel.HIGH)


def test_mission_floor_contract_and_budget_cannot_be_weakened(manifest: dict) -> None:
    mission = _mission()
    with pytest.raises(HarnessError, match="mission floor"):
        validate_effective_risk(
            validate_manifest(manifest),
            replace(mission, risk=RiskLevel.HIGH),
            expected_risk=RiskLevel.MEDIUM,
        )
    manifest["mission"]["independentOf"] = ["invented"]
    with pytest.raises(HarnessError, match="independence"):
        validate_effective_risk(
            validate_manifest(manifest), mission, expected_risk=RiskLevel.MEDIUM
        )
    manifest["mission"]["independentOf"] = []
    manifest["budget"]["maxRepairCycles"] = 2
    with pytest.raises(HarnessError, match="maxRepairCycles"):
        validate_effective_risk(
            validate_manifest(manifest), mission, expected_risk=RiskLevel.MEDIUM
        )


def test_executor_binding_pins_raw_registry_bytes(manifest: dict) -> None:
    raw = (EXAMPLE / "executors.json").read_bytes()
    result = validate_executor_binding(validate_manifest(manifest), raw)
    assert result.executor_id == "example-api-worker"
    with pytest.raises(HarnessError, match="registry digest"):
        validate_executor_binding(validate_manifest(manifest), raw + b" ")


@pytest.mark.parametrize(
    "field,value",
    [
        ("authMode", "oauth"),
        ("adapter", "other-adapter"),
        ("storesTrainingData", True),
        ("dataResidency", "eu"),
        ("permittedRepositories", ["other/project"]),
        ("toolCapabilities", ["shell"]),
        ("maxRisk", "low"),
        ("activeRuns", 1),
    ],
)
def test_same_executor_id_cannot_substitute_a_profile(manifest: dict, field: str, value) -> None:
    registry = json.loads((EXAMPLE / "executors.json").read_bytes())
    registry["executors"][0][field] = value
    raw = json.dumps(registry).encode()
    manifest["policy"]["executorRegistryDigest"] = hashlib.sha256(raw).hexdigest()
    with pytest.raises(HarnessError, match="profile digest"):
        validate_executor_binding(validate_manifest(manifest), raw)


def test_full_profile_snapshot_includes_all_resolved_fields() -> None:
    executor = load_executors(json.loads((EXAMPLE / "executors.json").read_bytes()))[0]
    assert set(executor_profile_snapshot(executor)["profile"]) == set(executor.as_dict())


def test_manifest_identity_must_match_pinned_profile(manifest: dict) -> None:
    manifest["executor"]["authMode"] = "oauth"
    with pytest.raises(HarnessError, match="authMode mismatch"):
        validate_executor_binding(
            validate_manifest(manifest), (EXAMPLE / "executors.json").read_bytes()
        )


def _args(manifest_path: Path, output: Path | None = None) -> list[str]:
    args = [
        "validate-harness",
        "--manifest",
        str(manifest_path),
        "--config",
        str(EXAMPLE / "agentic-sdlc.toml"),
        "--executors",
        str(EXAMPLE / "executors.json"),
        "--effective-risk",
        "medium",
    ]
    return args + (["--output", str(output)] if output else [])


def test_cli_example_validates_but_never_authorizes_dispatch(tmp_path: Path) -> None:
    output = tmp_path / "report.json"
    assert main(_args(EXAMPLE / "manifest.json", output)) == 0
    report = json.loads(output.read_bytes())
    assert report["dispatchAuthorized"] is False
    assert "routing-policy-and-live-capacity" in report["notVerified"]
    assert (
        report["manifestDigest"] == load_manifest((EXAMPLE / "manifest.json").read_bytes()).digest
    )
    assert (
        document_digest(report["executorProfileSnapshot"])
        == json.loads((EXAMPLE / "manifest.json").read_bytes())["executor"]["profileDigest"]
    )


@pytest.mark.parametrize("field", ["projectPolicyDigest", "missionRegistryDigest"])
def test_cli_rejects_mismatched_policy_bindings(manifest: dict, tmp_path: Path, field: str) -> None:
    manifest["policy"][field] = "f" * 64
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    assert main(_args(path)) == 2


def test_cli_rejects_duplicate_nested_keys(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text('{"policy":{"risk":"medium","risk":"low"}}')
    assert main(_args(path)) == 2


@pytest.mark.parametrize(
    "arguments",
    [["--help"], ["validate-missions", "--config", str(EXAMPLE / "agentic-sdlc.toml")]],
)
def test_legacy_cli_runs_without_site_dependencies(arguments: list[str]) -> None:
    result = subprocess.run(
        [sys.executable, "-S", "-m", "agentic_sdlc", *arguments],
        env={**os.environ, "PYTHONPATH": str(EXAMPLE.parents[1] / "src")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
