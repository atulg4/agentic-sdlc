"""Read-only harness manifest contracts; never an execution authorization.

The foundation validates structure, immutable identity, and selected reference
bindings. It does not load skills, call providers, reserve budgets or dispatch.
Approval provenance and the remaining evidence chain must be implemented before
any caller can use a manifest in live execution.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from importlib.resources import files
from typing import Any

import rfc8785
from jsonschema import Draft202012Validator

from .executors import ExecutorProfile, load_executors
from .missions import MissionSpec
from .models import RiskLevel

MAX_DOCUMENT_BYTES = 1_000_000
MAX_DEPTH = 32
MAX_SAFE_INTEGER = 2**53 - 1
_RISKS = tuple(RiskLevel(item) for item in ("low", "medium", "high", "critical"))
_BASE_GATES = frozenset({"deterministic-ci", "independent-agent-review", "human-merge-approval"})
_PROFILE_SETS = (
    "taskClasses",
    "capabilities",
    "toolCapabilities",
    "permittedRepositories",
)


class HarnessError(ValueError):
    """Malformed or inconsistent harness evidence."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise HarnessError(message)


def _json_domain(value: Any, depth: int = 0) -> None:
    _require(depth <= MAX_DEPTH, "JSON nesting limit exceeded")
    if type(value) is dict:
        for key, item in value.items():
            _require(type(key) is str, "JSON object keys must be strings")
            _json_domain(key, depth + 1)
            _json_domain(item, depth + 1)
    elif type(value) is list:
        for item in value:
            _json_domain(item, depth + 1)
    elif type(value) is str:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise HarnessError("invalid Unicode in JSON") from error
    elif type(value) is int:
        _require(abs(value) <= MAX_SAFE_INTEGER, "integer exceeds interoperable JSON range")
    elif type(value) is float:
        _require(math.isfinite(value), "JSON numbers must be finite")
        if value.is_integer():
            _require(abs(value) <= MAX_SAFE_INTEGER, "integer exceeds interoperable JSON range")
    else:
        _require(value is None or type(value) is bool, "unsupported JSON value")


def canonical_json(document: Any) -> bytes:
    """RFC 8785 bytes with the foundation's bounded JSON domain."""
    _json_domain(document)
    try:
        encoded = rfc8785.dumps(document)
    except rfc8785.CanonicalizationError as error:
        raise HarnessError("JSON cannot be canonicalized") from error
    _require(len(encoded) <= MAX_DOCUMENT_BYTES, "JSON document exceeds size limit")
    return encoded


def document_digest(document: Any) -> str:
    return hashlib.sha256(canonical_json(document)).hexdigest()


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def load_json(raw: bytes) -> Any:
    """Bounded JSON parsing with duplicate and non-finite rejection."""
    _require(len(raw) <= MAX_DOCUMENT_BYTES, "JSON document exceeds size limit")
    try:
        document = json.loads(raw.decode("utf-8"), object_pairs_hook=_object)
        canonical_json(document)
        return document
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise HarnessError("invalid JSON document") from error


def manifest_schema() -> dict[str, Any]:
    """Return a fresh packaged schema; caller mutation cannot alter validation."""
    return json.loads(
        files("agentic_sdlc").joinpath("schemas/harness-manifest-v1.json").read_text("utf-8")
    )


def _normalize(value: Any, schema: dict[str, Any], root: dict[str, Any]) -> Any:
    if "$ref" in schema:
        return _normalize(value, root["$defs"][schema["$ref"].split("/")[-1]], root)
    if isinstance(value, dict):
        return {
            key: _normalize(item, schema["properties"][key], root) for key, item in value.items()
        }
    if isinstance(value, list):
        normalized = [_normalize(item, schema["items"], root) for item in value]
        if schema["x-order"] == "set":
            normalized.sort(key=canonical_json)
        return normalized
    return value


def _path(pattern: str) -> None:
    _require(
        not pattern.startswith("/")
        and "\\" not in pattern
        and ":" not in pattern
        and all(part not in {"", ".", ".."} for part in pattern.split("/")),
        "manifest paths must be normalized repository-relative patterns",
    )


def _manifest_strings(value: Any) -> None:
    if isinstance(value, dict):
        for item in value.values():
            _manifest_strings(item)
    elif isinstance(value, list):
        for item in value:
            _manifest_strings(item)
    elif isinstance(value, str):
        _require(
            not any(ord(char) < 32 or ord(char) == 127 for char in value),
            "manifest strings must not contain control characters",
        )


@dataclass(frozen=True)
class HarnessManifest:
    """Validated immutable payload, including when constructed from raw bytes."""

    canonical_bytes: bytes

    def __post_init__(self) -> None:
        _require(type(self.canonical_bytes) is bytes, "manifest input must be immutable bytes")
        object.__setattr__(
            self, "canonical_bytes", _validated_manifest_bytes(load_json(self.canonical_bytes))
        )

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return json.loads(self.canonical_bytes)


def _validated_manifest_bytes(document: Any) -> bytes:
    canonical_json(document)
    schema = manifest_schema()
    errors = list(Draft202012Validator(schema).iter_errors(document))
    if errors:
        first = errors[0]
        location = ".".join(map(str, first.absolute_path)) or "manifest"
        # Avoid echoing arbitrary source text or embedded credentials in errors.
        raise HarnessError(f"{location}: violates {first.validator} constraint")
    _manifest_strings(document)
    work = document["work"]
    _require(
        re.fullmatch(re.escape(work["projectId"]) + r"#[1-9][0-9]*", work["workRef"]) is not None,
        "workRef must identify a request within projectId",
    )
    _require(
        (work["candidateTree"] is None) == (work["patchDigest"] is None),
        "candidateTree and patchDigest must be bound together",
    )
    _require(
        work["publishedHeadCommit"] is None or work["candidateTree"] is not None,
        "published head requires candidate tree and patch",
    )
    ids = [skill["id"] for skill in document["skills"]]
    _require(len(ids) == len(set(ids)), "a skill ID cannot resolve to multiple bundles")
    for key in ("readPaths", "writePaths"):
        for pattern in document["permissions"][key]:
            _path(pattern)
    gates = set(document["verification"]["requiredGateIds"])
    _require(
        gates >= _BASE_GATES, "foundation manifests require CI, independent review and human merge"
    )
    _require(
        not gates.intersection(document["verification"]["additionalGateIds"]),
        "required and additional gate lists must not overlap",
    )
    return canonical_json(_normalize(document, schema, schema))


def validate_manifest(document: Any) -> HarnessManifest:
    """Validate and normalize the new schema without changing legacy digests."""
    return HarnessManifest(canonical_json(document))


def load_manifest(raw: bytes) -> HarnessManifest:
    return HarnessManifest(raw)


def validate_effective_risk(
    manifest: HarnessManifest, mission: MissionSpec, *, expected_risk: RiskLevel
) -> None:
    """Reject critical/lowered risk using the trusted caller's assessment.

    This function does not compute path risk and is not wired into dispatch.
    Future dispatch integration must supply freshly evaluated protected-policy
    evidence on every attempt, rather than trusting the manifest's own label.
    """
    _require(type(expected_risk) is RiskLevel, "expected risk must be a RiskLevel")
    data = manifest.as_dict()
    actual = RiskLevel(data["risk"]["effective"])
    _require(actual == expected_risk, "manifest risk differs from the trusted assessment")
    _require(
        actual is not RiskLevel.CRITICAL, "critical effective risk cannot dispatch autonomously"
    )
    _require(
        _RISKS.index(actual) >= _RISKS.index(mission.risk), "effective risk lowers mission floor"
    )
    contract = data["mission"]
    _require(
        (contract["id"], contract["version"]) == (mission.mission_id, mission.version)
        and contract["contractDigest"] == document_digest(mission.as_dict()),
        "mission identity or contract digest mismatch",
    )
    _require(
        set(contract["requiredCapabilities"]) == set(mission.capabilities)
        and set(contract["independentOf"]) == set(mission.independent_of),
        "manifest changes mission capabilities or independence",
    )
    if actual is RiskLevel.HIGH:
        _require(
            "architect-security-review" in data["verification"]["requiredGateIds"],
            "high effective risk requires architect/security review",
        )
    for key, limit in (
        ("maxTotalTokens", mission.max_tokens),
        ("maxRuntimeSeconds", mission.max_runtime_seconds),
        ("maxRepairCycles", mission.max_retries),
        ("maxConcurrentMissions", mission.max_concurrency),
    ):
        _require(data["budget"][key] <= limit, f"{key} exceeds mission limit")


def executor_profile_snapshot(executor: ExecutorProfile) -> dict[str, Any]:
    """Version the complete profile independently of legacy route serialization."""
    profile = executor.as_dict()
    for key in _PROFILE_SETS:
        values = profile[key]
        _require(len(values) == len(set(values)), f"executor {key} contains duplicates")
        profile[key] = sorted(values, key=canonical_json)
    return {"schemaVersion": 1, "profile": profile}


def validate_executor_binding(manifest: HarnessManifest, registry_bytes: bytes) -> ExecutorProfile:
    """Bind exact registry bytes and a full resolved snapshot, not labels alone.

    This proves consistency with supplied bytes, not approval of those bytes or
    current capacity. The caller must obtain artifacts from a trusted source.
    """
    data = manifest.as_dict()
    _require(
        hashlib.sha256(registry_bytes).hexdigest() == data["policy"]["executorRegistryDigest"],
        "executor registry digest mismatch",
    )
    registry = load_json(registry_bytes)
    _require(
        type(registry) is dict and set(registry) == {"schemaVersion", "executors"},
        "executor registry must be a closed versioned object",
    )
    _require(type(registry["schemaVersion"]) is int, "executor registry version must be an integer")
    executors = load_executors(registry)
    selected = data["executor"]
    executor = next((e for e in executors if e.executor_id == selected["executorId"]), None)
    _require(executor is not None, "executor ID is absent from the pinned registry")
    assert executor is not None
    snapshot = executor_profile_snapshot(executor)
    _require(
        document_digest(snapshot) == selected["profileDigest"],
        "resolved executor profile digest mismatch",
    )
    for key in (
        "executorId",
        "adapter",
        "adapterVersion",
        "executionType",
        "authMode",
        "provider",
        "model",
        "modelAlias",
    ):
        _require(selected[key] == snapshot["profile"][key], f"executor {key} mismatch")
    risk = RiskLevel(data["risk"]["effective"])
    _require(
        _RISKS.index(executor.max_risk) >= _RISKS.index(risk), "executor risk ceiling exceeded"
    )
    _require(
        set(data["mission"]["requiredCapabilities"]) <= set(executor.capabilities),
        "executor lacks required mission capabilities",
    )
    _require(
        not executor.permitted_repositories
        or data["work"]["projectId"] in executor.permitted_repositories,
        "executor does not permit this repository",
    )
    context = data["context"]
    _require(
        context["inputTokenLimit"] + context["outputTokenReserve"] <= executor.context_window,
        "rendered input and output reserve exceed executor context window",
    )
    return executor
