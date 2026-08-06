"""Versioned agent mission registry and capability-based dispatch.

Missions describe *what* an agent is allowed and expected to do; agents
describe *which* capabilities a concrete adapter/model combination provides.
Dispatch matches the two deterministically so orchestration never depends on
hardcoded role names or treats a model name as proof of capability.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import RiskLevel
from .policy import ProjectPolicy

__all__ = [
    "AgentProfile",
    "MissionError",
    "MissionLedger",
    "MissionRegistry",
    "MissionSpec",
    "PLATFORM_MISSIONS",
    "create_dispatch_envelope",
    "load_agents",
    "load_registry",
    "validate_output",
]


class MissionError(ValueError):
    """Raised when mission configuration or dispatch constraints are violated."""


REVIEW_CAPABILITIES = frozenset(
    {
        "review-code",
        "review-security",
        "review-architecture",
        "review-data-quality",
        "review-quantitative",
    }
)
WRITE_CAPABILITIES = frozenset({"edit-code", "author-tests", "edit-docs", "repair"})
KNOWN_CAPABILITIES = frozenset(
    {
        "specify",
        "plan",
        "run-commands",
        "validate-release",
        "build-context",
    }
    | REVIEW_CAPABILITIES
    | WRITE_CAPABILITIES
)

_MISSION_ID = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
_VERSION = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,4}$")
_AGENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_RISK_ORDER = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}
_BROAD_WRITE_PATTERNS = frozenset({"*", "**", "**/*", "./**", "/**"})
MAX_MISSION_TOKENS = 10_000_000
MAX_MISSION_RUNTIME_SECONDS = 24 * 3600
MAX_MISSION_RETRIES = 10
MAX_MISSION_CONCURRENCY = 64


@dataclass(frozen=True)
class MissionSpec:
    """A versioned, bounded contract for one kind of agent work."""

    mission_id: str
    version: str
    purpose: str
    success_criteria: tuple[str, ...]
    capabilities: tuple[str, ...]
    output_artifacts: tuple[str, ...]
    input_artifacts: tuple[str, ...] = ()
    read_paths: tuple[str, ...] = ("**",)
    write_paths: tuple[str, ...] = ()
    forbidden_paths: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    knowledge_sources: tuple[str, ...] = ()
    risk: RiskLevel = RiskLevel.MEDIUM
    approval_required: bool = False
    max_runtime_seconds: int = 3600
    max_tokens: int = 500_000
    max_retries: int = 2
    max_concurrency: int = 1
    parallelizable: bool = False
    independent_of: tuple[str, ...] = ()
    escalate_to: str = "human"
    origin: str = "platform"

    @property
    def writes(self) -> bool:
        return bool(set(self.capabilities) & WRITE_CAPABILITIES)

    @property
    def reviews(self) -> bool:
        return bool(set(self.capabilities) & REVIEW_CAPABILITIES)

    def as_dict(self) -> dict[str, Any]:
        return {
            "missionId": self.mission_id,
            "version": self.version,
            "purpose": self.purpose,
            "successCriteria": list(self.success_criteria),
            "capabilities": list(self.capabilities),
            "inputArtifacts": list(self.input_artifacts),
            "outputArtifacts": list(self.output_artifacts),
            "readPaths": list(self.read_paths),
            "writePaths": list(self.write_paths),
            "forbiddenPaths": list(self.forbidden_paths),
            "commands": list(self.commands),
            "evidence": list(self.evidence),
            "knowledgeSources": list(self.knowledge_sources),
            "risk": self.risk.value,
            "approvalRequired": self.approval_required,
            "maxRuntimeSeconds": self.max_runtime_seconds,
            "maxTokens": self.max_tokens,
            "maxRetries": self.max_retries,
            "maxConcurrency": self.max_concurrency,
            "parallelizable": self.parallelizable,
            "independentOf": list(self.independent_of),
            "escalateTo": self.escalate_to,
            "origin": self.origin,
        }


@dataclass(frozen=True)
class AgentProfile:
    """A concrete agent (adapter + model) offering a set of capabilities."""

    agent_id: str
    adapter: str
    adapter_version: str
    provider: str
    model: str
    capabilities: tuple[str, ...]
    available: bool = True
    max_risk: RiskLevel = RiskLevel.HIGH

    def as_dict(self) -> dict[str, Any]:
        return {
            "agentId": self.agent_id,
            "adapter": self.adapter,
            "adapterVersion": self.adapter_version,
            "provider": self.provider,
            "model": self.model,
            "capabilities": list(self.capabilities),
            "available": self.available,
            "maxRisk": self.max_risk.value,
        }


def _spec(
    mission_id: str,
    purpose: str,
    capabilities: tuple[str, ...],
    output_artifacts: tuple[str, ...],
    **overrides: Any,
) -> MissionSpec:
    return MissionSpec(
        mission_id=mission_id,
        version="1.0.0",
        purpose=purpose,
        success_criteria=(f"{purpose} with the declared evidence attached.",),
        capabilities=capabilities,
        output_artifacts=output_artifacts,
        **overrides,
    )


PLATFORM_MISSIONS: tuple[MissionSpec, ...] = (
    _spec(
        "specification-planner",
        "Turn an approved work request into a bounded specification and plan",
        ("specify", "plan"),
        ("plan-comment",),
        input_artifacts=("task-spec",),
        evidence=("policy-decision",),
    ),
    _spec(
        "implementation-worker",
        "Implement an approved specification as a reviewable patch",
        ("edit-code", "author-tests", "run-commands"),
        ("patch", "patch-manifest"),
        input_artifacts=("task-spec", "plan-comment"),
        write_paths=("src/**", "tests/**"),
        commands=("test",),
        evidence=("gate-report", "policy-decision"),
        max_retries=1,
    ),
    _spec(
        "test-author",
        "Author tests against a frozen interface contract",
        ("author-tests", "run-commands"),
        ("patch", "patch-manifest"),
        input_artifacts=("task-spec",),
        write_paths=("tests/**",),
        commands=("test",),
        evidence=("gate-report",),
        parallelizable=True,
    ),
    _spec(
        "deterministic-verifier",
        "Run the trusted verification gates against a candidate change",
        ("run-commands",),
        ("gate-report",),
        input_artifacts=("patch", "patch-manifest"),
        risk=RiskLevel.LOW,
        parallelizable=True,
    ),
    _spec(
        "code-reviewer",
        "Independently review a candidate change for correctness and regressions",
        ("review-code",),
        ("review-report",),
        input_artifacts=("patch", "patch-manifest", "gate-report"),
        independent_of=("implementation-worker", "repair-agent", "test-author"),
        parallelizable=True,
    ),
    _spec(
        "security-reviewer",
        "Independently review a candidate change for security regressions",
        ("review-code", "review-security"),
        ("review-report",),
        input_artifacts=("patch", "patch-manifest"),
        independent_of=("implementation-worker", "repair-agent"),
        risk=RiskLevel.HIGH,
        parallelizable=True,
    ),
    _spec(
        "architecture-reviewer",
        "Independently review a candidate change for architectural fit",
        ("review-code", "review-architecture"),
        ("review-report",),
        input_artifacts=("patch", "patch-manifest"),
        independent_of=("implementation-worker", "repair-agent"),
        parallelizable=True,
    ),
    _spec(
        "data-quality-reviewer",
        "Independently review data handling, schemas, and quality controls",
        ("review-data-quality",),
        ("review-report",),
        input_artifacts=("patch", "patch-manifest"),
        independent_of=("implementation-worker", "repair-agent"),
        parallelizable=True,
    ),
    _spec(
        "quant-reviewer",
        "Independently review mathematical and quantitative correctness",
        ("review-quantitative",),
        ("review-report",),
        input_artifacts=("patch", "patch-manifest"),
        independent_of=("implementation-worker", "repair-agent"),
        parallelizable=True,
    ),
    _spec(
        "documentation-agent",
        "Produce or update documentation for a completed change",
        ("edit-docs",),
        ("patch", "patch-manifest"),
        input_artifacts=("task-spec",),
        write_paths=("docs/**", "*.md", "**/*.md"),
        risk=RiskLevel.LOW,
        parallelizable=True,
    ),
    _spec(
        "repair-agent",
        "Address independent review findings with a bounded follow-up patch",
        ("edit-code", "author-tests", "run-commands", "repair"),
        ("patch", "patch-manifest"),
        input_artifacts=("review-report", "patch"),
        write_paths=("src/**", "tests/**"),
        commands=("test",),
        evidence=("gate-report",),
        max_retries=1,
    ),
    _spec(
        "release-validator",
        "Validate a composed candidate against release criteria without deploying",
        ("run-commands", "validate-release"),
        ("gate-report",),
        input_artifacts=("patch-manifest",),
        risk=RiskLevel.HIGH,
        approval_required=True,
    ),
    _spec(
        "context-builder",
        "Assemble the permitted evidence context pack for a downstream mission",
        ("build-context",),
        ("context-pack",),
        risk=RiskLevel.LOW,
        parallelizable=True,
    ),
)

_PLATFORM_IDS = frozenset(mission.mission_id for mission in PLATFORM_MISSIONS)

_TOML_KEYS = {
    "id": "mission_id",
    "version": "version",
    "purpose": "purpose",
    "success_criteria": "success_criteria",
    "capabilities": "capabilities",
    "input_artifacts": "input_artifacts",
    "output_artifacts": "output_artifacts",
    "read_paths": "read_paths",
    "write_paths": "write_paths",
    "forbidden_paths": "forbidden_paths",
    "commands": "commands",
    "evidence": "evidence",
    "knowledge_sources": "knowledge_sources",
    "risk": "risk",
    "approval_required": "approval_required",
    "max_runtime_seconds": "max_runtime_seconds",
    "max_tokens": "max_tokens",
    "max_retries": "max_retries",
    "max_concurrency": "max_concurrency",
    "parallelizable": "parallelizable",
    "independent_of": "independent_of",
    "escalate_to": "escalate_to",
}
_STRING_TUPLE_FIELDS = {
    "success_criteria",
    "capabilities",
    "input_artifacts",
    "output_artifacts",
    "read_paths",
    "write_paths",
    "forbidden_paths",
    "commands",
    "evidence",
    "knowledge_sources",
    "independent_of",
}
_BOOL_FIELDS = {"approval_required", "parallelizable"}
_INT_LIMITS = {
    "max_runtime_seconds": MAX_MISSION_RUNTIME_SECONDS,
    "max_tokens": MAX_MISSION_TOKENS,
    "max_retries": MAX_MISSION_RETRIES,
    "max_concurrency": MAX_MISSION_CONCURRENCY,
}


def _string_tuple(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise MissionError(f"{name} must be an array of non-empty strings")
    return tuple(item.strip() for item in value)


def _parse_mission(entry: object, origin: str) -> MissionSpec:
    if not isinstance(entry, dict):
        raise MissionError("each mission entry must be a TOML table")
    unknown = sorted(set(entry) - set(_TOML_KEYS))
    if unknown:
        raise MissionError("mission declares unknown keys: " + ", ".join(unknown))

    values: dict[str, Any] = {"origin": origin}
    for key, target in _TOML_KEYS.items():
        if key not in entry:
            continue
        value = entry[key]
        if target in _STRING_TUPLE_FIELDS:
            values[target] = _string_tuple(value, key)
        elif target in _BOOL_FIELDS:
            if not isinstance(value, bool):
                raise MissionError(f"{key} must be a boolean")
            values[target] = value
        elif target in _INT_LIMITS:
            maximum = _INT_LIMITS[target]
            if type(value) is not int or value < 0 or value > maximum:
                raise MissionError(f"{key} must be an integer between 0 and {maximum}")
            values[target] = value
        elif target == "risk":
            try:
                values[target] = RiskLevel(str(value))
            except ValueError as error:
                raise MissionError("risk must be one of low, medium, high, critical") from error
        else:
            if not isinstance(value, str) or not value.strip():
                raise MissionError(f"{key} must be a non-empty string")
            values[target] = value.strip()

    for required in ("mission_id", "version", "purpose", "capabilities", "output_artifacts"):
        if required not in values:
            key = "id" if required == "mission_id" else required
            raise MissionError(f"mission is missing required key: {key}")
    if "success_criteria" not in values:
        raise MissionError("mission is missing required key: success_criteria")
    return MissionSpec(**values)


def _validate_mission(
    mission: MissionSpec,
    policy: ProjectPolicy,
    allowed_capabilities: frozenset[str],
) -> None:
    prefix = f"mission {mission.mission_id or '<missing>'}"
    if not _MISSION_ID.fullmatch(mission.mission_id):
        raise MissionError(f"{prefix}: id must match {_MISSION_ID.pattern}")
    if not _VERSION.fullmatch(mission.version):
        raise MissionError(f"{prefix}: version must be MAJOR.MINOR.PATCH")
    if not mission.capabilities:
        raise MissionError(f"{prefix}: capabilities must not be empty")
    if not mission.output_artifacts:
        raise MissionError(f"{prefix}: output_artifacts must not be empty")
    if not mission.success_criteria:
        raise MissionError(f"{prefix}: success_criteria must not be empty")

    unknown = sorted(set(mission.capabilities) - KNOWN_CAPABILITIES)
    if unknown:
        raise MissionError(f"{prefix}: unknown capabilities: " + ", ".join(unknown))
    denied = sorted(set(mission.capabilities) - allowed_capabilities)
    if denied:
        raise MissionError(
            f"{prefix}: capabilities unavailable under repository policy: " + ", ".join(denied)
        )
    if mission.risk is RiskLevel.CRITICAL:
        raise MissionError(f"{prefix}: critical-risk missions cannot be dispatched autonomously")

    if mission.writes and not mission.write_paths:
        raise MissionError(f"{prefix}: write capabilities require an explicit write scope")
    if mission.write_paths and not mission.writes:
        raise MissionError(f"{prefix}: write scope requires a write capability")

    forbidden = tuple(dict.fromkeys((*policy.forbidden_paths, *mission.forbidden_paths)))
    for pattern in mission.write_paths:
        if pattern in _BROAD_WRITE_PATTERNS:
            raise MissionError(f"{prefix}: write scope is too broad: {pattern}")
        normalized = pattern.removeprefix("./")
        if any(fnmatch.fnmatch(normalized, item) for item in forbidden):
            raise MissionError(f"{prefix}: write scope includes a forbidden path: {pattern}")

    if mission.reviews and mission.writes:
        raise MissionError(f"{prefix}: a mission cannot both review and write")
    if mission.escalate_to != "human" and not _MISSION_ID.fullmatch(mission.escalate_to):
        raise MissionError(f"{prefix}: escalate_to must be 'human' or a mission id")


@dataclass(frozen=True)
class MissionRegistry:
    """Validated set of platform and consumer missions plus dispatch rules."""

    missions: Mapping[str, MissionSpec]
    allowed_capabilities: frozenset[str] = KNOWN_CAPABILITIES

    def get(self, mission_id: str) -> MissionSpec:
        mission = self.missions.get(mission_id)
        if mission is None:
            raise MissionError(f"unknown mission: {mission_id}")
        return mission

    def excluded_agents(
        self,
        mission: MissionSpec,
        history: Mapping[str, str],
    ) -> frozenset[str]:
        """Agent IDs that independence rules disqualify for this mission."""
        excluded = {history[other] for other in mission.independent_of if other in history}
        if mission.reviews:
            for other_id, agent_id in history.items():
                other = self.missions.get(other_id)
                if other is not None and other.writes:
                    excluded.add(agent_id)
        return frozenset(excluded)

    def select_agent(
        self,
        mission_id: str,
        agents: Sequence[AgentProfile],
        history: Mapping[str, str] | None = None,
        ledger: MissionLedger | None = None,
    ) -> AgentProfile:
        """Pick the first declared agent satisfying the full mission contract.

        Agent order expresses preference: later entries are fallbacks and must
        satisfy exactly the same capability, risk, and independence rules.
        """
        mission = self.get(mission_id)
        excluded = self.excluded_agents(mission, history or {})
        if ledger is not None:
            ledger.check(mission)
        required = set(mission.capabilities)
        rejected: list[str] = []
        for agent in agents:
            if not agent.available:
                rejected.append(f"{agent.agent_id}: unavailable")
            elif agent.agent_id in excluded:
                rejected.append(f"{agent.agent_id}: independence conflict")
            elif not required <= set(agent.capabilities):
                missing = ", ".join(sorted(required - set(agent.capabilities)))
                rejected.append(f"{agent.agent_id}: missing capabilities ({missing})")
            elif _RISK_ORDER[agent.max_risk] < _RISK_ORDER[mission.risk]:
                rejected.append(f"{agent.agent_id}: not cleared for {mission.risk.value} risk")
            else:
                return agent
        detail = "; ".join(rejected) if rejected else "no agents declared"
        raise MissionError(f"no eligible agent for mission {mission_id}: {detail}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "allowedCapabilities": sorted(self.allowed_capabilities),
            "missions": {
                mission_id: mission.as_dict()
                for mission_id, mission in sorted(self.missions.items())
            },
        }


def load_registry(
    path: str | Path | None,
    policy: ProjectPolicy,
) -> MissionRegistry:
    """Load platform mission templates plus optional consumer extensions.

    Consumer missions may add new missions but can never redefine a platform
    mission or relax platform/policy safety rules.
    """
    document: dict[str, Any] = {}
    if path is not None:
        with Path(path).open("rb") as handle:
            document = tomllib.load(handle)
        if document.get("version") != 1:
            raise MissionError("mission registry version must be 1")

    registry_table = document.get("registry", {})
    if not isinstance(registry_table, dict):
        raise MissionError("registry must be a TOML table")
    allowed = registry_table.get("allowed_capabilities")
    if allowed is None:
        allowed_capabilities = KNOWN_CAPABILITIES
    else:
        requested = frozenset(_string_tuple(allowed, "registry.allowed_capabilities"))
        unknown = sorted(requested - KNOWN_CAPABILITIES)
        if unknown:
            raise MissionError("registry allows unknown capabilities: " + ", ".join(unknown))
        allowed_capabilities = requested

    missions: dict[str, MissionSpec] = {}
    for mission in PLATFORM_MISSIONS:
        if set(mission.capabilities) <= allowed_capabilities:
            missions[mission.mission_id] = mission

    entries = document.get("mission", [])
    if not isinstance(entries, list):
        raise MissionError("mission must be an array of tables")
    for entry in entries:
        mission = _parse_mission(entry, origin="consumer")
        if mission.mission_id in _PLATFORM_IDS:
            raise MissionError(
                f"mission {mission.mission_id}: consumer missions cannot replace platform missions"
            )
        if mission.mission_id in missions:
            raise MissionError(f"mission {mission.mission_id}: duplicate mission id")
        missions[mission.mission_id] = mission

    for mission in missions.values():
        _validate_mission(mission, policy, allowed_capabilities)
    for mission in missions.values():
        unknown = sorted(set(mission.independent_of) - set(missions))
        if unknown and mission.origin == "consumer":
            raise MissionError(
                f"mission {mission.mission_id}: independent_of references unknown "
                "missions: " + ", ".join(unknown)
            )
    return MissionRegistry(missions=missions, allowed_capabilities=allowed_capabilities)


class MissionLedger:
    """Budget, retry, and concurrency accounting across mission runs."""

    def __init__(self, registry: MissionRegistry) -> None:
        self._registry = registry
        self._active: dict[str, int] = {}
        self._tokens: dict[str, int] = {}
        self._attempts: dict[tuple[str, str], int] = {}

    def check(self, mission: MissionSpec, work_ref: str | None = None) -> None:
        active = self._active.get(mission.mission_id, 0)
        if active >= mission.max_concurrency:
            raise MissionError(
                f"mission {mission.mission_id}: concurrency limit reached "
                f"({active}/{mission.max_concurrency})"
            )
        spent = self._tokens.get(mission.mission_id, 0)
        if spent >= mission.max_tokens:
            raise MissionError(
                f"mission {mission.mission_id}: token budget exhausted "
                f"({spent}/{mission.max_tokens})"
            )
        if work_ref is not None:
            attempts = self._attempts.get((mission.mission_id, work_ref), 0)
            if attempts >= mission.max_retries + 1:
                raise MissionError(
                    f"mission {mission.mission_id}: retry limit exceeded for {work_ref} "
                    f"({attempts} attempts, {mission.max_retries} retries allowed)"
                )

    def begin_run(self, mission_id: str, work_ref: str) -> None:
        mission = self._registry.get(mission_id)
        self.check(mission, work_ref)
        key = (mission_id, work_ref)
        self._attempts[key] = self._attempts.get(key, 0) + 1
        self._active[mission_id] = self._active.get(mission_id, 0) + 1

    def finish_run(self, mission_id: str, tokens_used: int = 0) -> None:
        self._registry.get(mission_id)
        if tokens_used < 0:
            raise MissionError("tokens_used cannot be negative")
        active = self._active.get(mission_id, 0)
        if active < 1:
            raise MissionError(f"mission {mission_id}: no active run to finish")
        self._active[mission_id] = active - 1
        self._tokens[mission_id] = self._tokens.get(mission_id, 0) + tokens_used

    def as_dict(self) -> dict[str, Any]:
        return {
            "active": dict(sorted(self._active.items())),
            "tokensSpent": dict(sorted(self._tokens.items())),
            "attempts": {
                f"{mission_id}:{work_ref}": count
                for (mission_id, work_ref), count in sorted(self._attempts.items())
            },
        }


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def create_dispatch_envelope(
    mission: MissionSpec,
    agent: AgentProfile,
    *,
    work_ref: str,
    prompt: str,
    input_refs: Sequence[str] = (),
    context_pack_digest: str | None = None,
) -> dict[str, Any]:
    """Pin one dispatch to mission version, adapter, model, and prompt digest.

    The envelope is deterministic: identical mission, agent, work reference,
    inputs, and prompt always produce the same envelope digest.
    """
    if not work_ref.strip():
        raise MissionError("work_ref must be a non-empty reference")
    body: dict[str, Any] = {
        "schemaVersion": 1,
        "missionId": mission.mission_id,
        "missionVersion": mission.version,
        "risk": mission.risk.value,
        "approvalRequired": mission.approval_required,
        "agentId": agent.agent_id,
        "adapter": agent.adapter,
        "adapterVersion": agent.adapter_version,
        "provider": agent.provider,
        "model": agent.model,
        "workRef": work_ref.strip(),
        "inputRefs": sorted(set(input_refs)),
        "promptSha256": _sha256_text(prompt),
        "contextPackDigest": context_pack_digest,
        "limits": {
            "maxRuntimeSeconds": mission.max_runtime_seconds,
            "maxTokens": mission.max_tokens,
            "maxRetries": mission.max_retries,
        },
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    body["envelopeSha256"] = _sha256_text(canonical)
    return body


def validate_output(mission: MissionSpec, outputs: Mapping[str, str]) -> None:
    """Validate produced artifacts against the mission contract before use."""
    missing = sorted(set(mission.output_artifacts) - set(outputs))
    if missing:
        raise MissionError(
            f"mission {mission.mission_id}: missing declared outputs: " + ", ".join(missing)
        )
    unexpected = sorted(set(outputs) - set(mission.output_artifacts))
    if unexpected:
        raise MissionError(
            f"mission {mission.mission_id}: undeclared outputs: " + ", ".join(unexpected)
        )
    empty = sorted(name for name, value in outputs.items() if not str(value).strip())
    if empty:
        raise MissionError(f"mission {mission.mission_id}: empty outputs: " + ", ".join(empty))


def load_agents(document: object) -> tuple[AgentProfile, ...]:
    """Parse the declared agent roster (order expresses fallback preference)."""
    if not isinstance(document, list) or not document:
        raise MissionError("agents must be a non-empty JSON array")
    agents: list[AgentProfile] = []
    seen: set[str] = set()
    for entry in document:
        if not isinstance(entry, dict):
            raise MissionError("each agent must be a JSON object")
        unknown = sorted(
            set(entry)
            - {
                "agentId",
                "adapter",
                "adapterVersion",
                "provider",
                "model",
                "capabilities",
                "available",
                "maxRisk",
            }
        )
        if unknown:
            raise MissionError("agent declares unknown keys: " + ", ".join(unknown))
        agent_id = entry.get("agentId")
        if not isinstance(agent_id, str) or not _AGENT_ID.fullmatch(agent_id):
            raise MissionError("agentId must match " + _AGENT_ID.pattern)
        if agent_id in seen:
            raise MissionError(f"duplicate agent id: {agent_id}")
        seen.add(agent_id)
        capabilities = entry.get("capabilities")
        if not isinstance(capabilities, list) or not all(
            isinstance(item, str) for item in capabilities
        ):
            raise MissionError(f"agent {agent_id}: capabilities must be an array of strings")
        unknown_caps = sorted(set(capabilities) - KNOWN_CAPABILITIES)
        if unknown_caps:
            raise MissionError(
                f"agent {agent_id}: unknown capabilities: " + ", ".join(unknown_caps)
            )
        available = entry.get("available", True)
        if not isinstance(available, bool):
            raise MissionError(f"agent {agent_id}: available must be a boolean")
        try:
            max_risk = RiskLevel(str(entry.get("maxRisk", "high")))
        except ValueError as error:
            raise MissionError(f"agent {agent_id}: invalid maxRisk") from error
        strings = {}
        for key in ("adapter", "adapterVersion", "provider", "model"):
            value = entry.get(key)
            if not isinstance(value, str) or not value.strip():
                raise MissionError(f"agent {agent_id}: {key} must be a non-empty string")
            strings[key] = value.strip()
        agents.append(
            AgentProfile(
                agent_id=agent_id,
                adapter=strings["adapter"],
                adapter_version=strings["adapterVersion"],
                provider=strings["provider"],
                model=strings["model"],
                capabilities=tuple(capabilities),
                available=available,
                max_risk=max_risk,
            )
        )
    return tuple(agents)
