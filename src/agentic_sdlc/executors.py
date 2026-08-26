"""Provider-neutral executor registry and deterministic routing policy."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .missions import KNOWN_CAPABILITIES, MissionSpec
from .models import RiskLevel

__all__ = [
    "AuthMode",
    "ExecutionType",
    "ExecutorError",
    "ExecutorProfile",
    "RouteDecision",
    "RouteRequest",
    "RouteStatus",
    "RoutingPolicy",
    "TaskClass",
    "load_executors",
    "load_routing_policy",
    "route_executor",
]


class ExecutorError(ValueError):
    """Raised when executor registry or routing evidence is invalid."""


class ExecutionType(StrEnum):
    SUBSCRIPTION_CLOUD = "subscription-cloud"
    SUBSCRIPTION_RUNNER = "subscription-runner"
    DIRECT_API = "direct-api"
    CLOUD_PLATFORM = "cloud-platform"
    SELF_HOSTED = "self-hosted"


class AuthMode(StrEnum):
    OAUTH = "oauth"
    API_KEY = "api-key"
    OIDC = "oidc"
    WORKLOAD_IDENTITY = "workload-identity"
    LOCAL_TRUSTED_RUNNER = "local-trusted-runner"


class TaskClass(StrEnum):
    TRIAGE = "triage"
    PLANNING = "planning"
    IMPLEMENTATION = "implementation"
    REVIEW = "review"
    REPAIR = "repair"
    SPECIALIST_VALIDATION = "specialist-domain-validation"


class RouteStatus(StrEnum):
    SELECTED = "selected"
    INSUFFICIENT_BUDGET_OR_ASSURANCE = "insufficient-budget-or-assurance"


KNOWN_PROVIDERS = frozenset(
    {
        "anthropic",
        "aws-bedrock",
        "azure-foundry",
        "codex",
        "deepseek",
        "google-vertex",
        "kimi",
        "moonshot",
        "openai",
        "self-hosted",
    }
)

KNOWN_MODELS: Mapping[str, frozenset[str]] = {
    "anthropic": frozenset({"claude-opus-5", "claude-sonnet-5"}),
    "aws-bedrock": frozenset({"anthropic.claude-opus-5", "meta.llama-4", "amazon.nova-pro"}),
    "azure-foundry": frozenset({"gpt-5", "gpt-5-mini", "phi-4"}),
    "codex": frozenset({"gpt-5", "gpt-5-codex"}),
    "deepseek": frozenset({"deepseek-chat", "deepseek-reasoner"}),
    "google-vertex": frozenset({"gemini-2.5-pro", "gemini-2.5-flash"}),
    "kimi": frozenset({"kimi-k2", "kimi-k2-thinking"}),
    "moonshot": frozenset({"kimi-k2", "moonshot-v1-128k"}),
    "openai": frozenset({"gpt-5", "gpt-5-mini", "gpt-5-nano"}),
    "self-hosted": frozenset({"llama-4", "qwen3-coder", "deepseek-r1"}),
}

TOOL_CAPABILITIES = frozenset(
    {
        "code-edit",
        "shell",
        "structured-output",
        "function-calling",
        "long-context",
        "vision",
        "web-search",
    }
)

DATA_RESIDENCY = frozenset({"unspecified", "us", "eu", "customer-controlled", "local-only"})


@dataclass(frozen=True)
class ExecutorProfile:
    executor_id: str
    provider: str
    adapter: str
    adapter_version: str
    execution_type: ExecutionType
    auth_mode: AuthMode
    model: str
    model_family: str
    task_classes: tuple[TaskClass, ...]
    capabilities: tuple[str, ...]
    tool_capabilities: tuple[str, ...] = ()
    context_window: int = 0
    supports_cloud: bool = True
    available: bool = True
    max_concurrency: int = 1
    active_runs: int = 0
    max_risk: RiskLevel = RiskLevel.MEDIUM
    quality_lower_bound: float = 0.0
    direct_cost_usd: float = 0.0
    shadow_cost_usd: float = 0.0
    subscription_monthly_usd: float = 0.0
    subscription_capacity_remaining: int = 0
    average_latency_seconds: int = 0
    recent_failure_rate: float = 0.0
    data_residency: str = "unspecified"
    stores_training_data: bool = False
    permitted_repositories: tuple[str, ...] = ()

    @property
    def expected_mission_cost(self) -> float:
        return self.direct_cost_usd + self.shadow_cost_usd

    def as_dict(self) -> dict[str, Any]:
        return {
            "executorId": self.executor_id,
            "provider": self.provider,
            "adapter": self.adapter,
            "adapterVersion": self.adapter_version,
            "executionType": self.execution_type.value,
            "authMode": self.auth_mode.value,
            "model": self.model,
            "modelFamily": self.model_family,
            "taskClasses": [item.value for item in self.task_classes],
            "capabilities": list(self.capabilities),
            "toolCapabilities": list(self.tool_capabilities),
            "contextWindow": self.context_window,
            "supportsCloud": self.supports_cloud,
            "available": self.available,
            "maxConcurrency": self.max_concurrency,
            "activeRuns": self.active_runs,
            "maxRisk": self.max_risk.value,
            "qualityLowerBound": self.quality_lower_bound,
            "directCostUsd": self.direct_cost_usd,
            "shadowCostUsd": self.shadow_cost_usd,
            "subscriptionMonthlyUsd": self.subscription_monthly_usd,
            "subscriptionCapacityRemaining": self.subscription_capacity_remaining,
            "averageLatencySeconds": self.average_latency_seconds,
            "recentFailureRate": self.recent_failure_rate,
            "dataResidency": self.data_residency,
            "storesTrainingData": self.stores_training_data,
            "permittedRepositories": list(self.permitted_repositories),
        }


@dataclass(frozen=True)
class RoutingPolicy:
    policy_version: str = "1.0.0"
    quality_floors: Mapping[RiskLevel, float] | None = None
    allowed_providers: tuple[str, ...] = tuple(sorted(KNOWN_PROVIDERS))
    denied_providers: tuple[str, ...] = ()
    require_no_training_storage: bool = False
    allowed_data_residency: tuple[str, ...] = tuple(sorted(DATA_RESIDENCY))

    def floor_for(self, risk: RiskLevel) -> float:
        floors = self.quality_floors or {
            RiskLevel.LOW: 0.50,
            RiskLevel.MEDIUM: 0.70,
            RiskLevel.HIGH: 0.85,
            RiskLevel.CRITICAL: 0.95,
        }
        return floors[risk]

    def as_dict(self) -> dict[str, Any]:
        return {
            "policyVersion": self.policy_version,
            "qualityFloors": {
                risk.value: self.floor_for(risk)
                for risk in (
                    RiskLevel.LOW,
                    RiskLevel.MEDIUM,
                    RiskLevel.HIGH,
                    RiskLevel.CRITICAL,
                )
            },
            "allowedProviders": list(self.allowed_providers),
            "deniedProviders": list(self.denied_providers),
            "requireNoTrainingStorage": self.require_no_training_storage,
            "allowedDataResidency": list(self.allowed_data_residency),
        }


@dataclass(frozen=True)
class RouteRequest:
    repository: str
    task_class: TaskClass
    risk: RiskLevel
    required_capabilities: tuple[str, ...]
    required_tool_capabilities: tuple[str, ...] = ()
    min_context_window: int = 0
    budget_usd: float = 0.0


@dataclass(frozen=True)
class RouteDecision:
    status: RouteStatus
    selected_executor_id: str
    policy_version: str
    required_quality_floor: float
    budget_usd: float
    candidates: tuple[dict[str, Any], ...]
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "selectedExecutorId": self.selected_executor_id,
            "policyVersion": self.policy_version,
            "requiredQualityFloor": self.required_quality_floor,
            "budgetUsd": self.budget_usd,
            "candidates": list(self.candidates),
            "reason": self.reason,
        }


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ExecutorError(message)


def _string_tuple(value: object, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ExecutorError(f"{name} must be an array of non-empty strings")
    return tuple(item.strip() for item in value)


def _float(value: object, name: str, minimum: float, maximum: float) -> float:
    if type(value) not in {int, float} or not minimum <= float(value) <= maximum:
        raise ExecutorError(f"{name} must be a number between {minimum} and {maximum}")
    return float(value)


def _int(value: object, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ExecutorError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def _risk(value: object, name: str) -> RiskLevel:
    try:
        return RiskLevel(str(value))
    except ValueError as error:
        raise ExecutorError(f"{name} must be one of low, medium, high, critical") from error


def _task_class(value: object, name: str) -> TaskClass:
    try:
        return TaskClass(str(value))
    except ValueError as error:
        allowed = ", ".join(item.value for item in TaskClass)
        raise ExecutorError(f"{name} must be one of {allowed}") from error


def _execution_type(value: object) -> ExecutionType:
    try:
        return ExecutionType(str(value))
    except ValueError as error:
        raise ExecutorError("executionType is unknown") from error


def _auth_mode(value: object) -> AuthMode:
    try:
        return AuthMode(str(value))
    except ValueError as error:
        raise ExecutorError("authMode is unknown") from error


def _parse_executor(entry: object) -> ExecutorProfile:
    if not isinstance(entry, dict):
        raise ExecutorError("each executor must be an object")
    allowed = {
        "executorId",
        "provider",
        "adapter",
        "adapterVersion",
        "executionType",
        "authMode",
        "model",
        "modelFamily",
        "taskClasses",
        "capabilities",
        "toolCapabilities",
        "contextWindow",
        "supportsCloud",
        "available",
        "maxConcurrency",
        "activeRuns",
        "maxRisk",
        "qualityLowerBound",
        "directCostUsd",
        "shadowCostUsd",
        "subscriptionMonthlyUsd",
        "subscriptionCapacityRemaining",
        "averageLatencySeconds",
        "recentFailureRate",
        "dataResidency",
        "storesTrainingData",
        "permittedRepositories",
    }
    if any("credential" in key.lower() or "secret" in key.lower() for key in entry):
        raise ExecutorError("executor registry must not contain credentials or secrets")
    unknown = sorted(set(entry) - allowed)
    if unknown:
        raise ExecutorError("executor declares unknown keys: " + ", ".join(unknown))

    executor_id = str(entry.get("executorId", "")).strip()
    _require(bool(executor_id), "executorId is required")
    provider = str(entry.get("provider", "")).strip()
    _require(provider in KNOWN_PROVIDERS, f"unknown provider: {provider}")
    model = str(entry.get("model", "")).strip()
    _require(model in KNOWN_MODELS[provider], f"unknown model for {provider}: {model}")
    model_family = str(entry.get("modelFamily", "")).strip() or model
    adapter = str(entry.get("adapter", "")).strip()
    adapter_version = str(entry.get("adapterVersion", "")).strip()
    _require(bool(adapter), f"executor {executor_id}: adapter is required")
    _require(bool(adapter_version), f"executor {executor_id}: adapterVersion is required")

    task_classes = tuple(
        _task_class(item, f"executor {executor_id}: taskClasses")
        for item in _string_tuple(entry.get("taskClasses"), f"executor {executor_id}: taskClasses")
    )
    _require(bool(task_classes), f"executor {executor_id}: taskClasses must not be empty")
    capabilities = _string_tuple(entry.get("capabilities"), f"executor {executor_id}: capabilities")
    unknown_caps = sorted(set(capabilities) - KNOWN_CAPABILITIES)
    if unknown_caps:
        raise ExecutorError(
            f"executor {executor_id}: unknown capabilities: " + ", ".join(unknown_caps)
        )
    tool_capabilities = _string_tuple(
        entry.get("toolCapabilities"), f"executor {executor_id}: toolCapabilities"
    )
    unknown_tools = sorted(set(tool_capabilities) - TOOL_CAPABILITIES)
    if unknown_tools:
        raise ExecutorError(
            f"executor {executor_id}: unknown tool capabilities: " + ", ".join(unknown_tools)
        )
    data_residency = str(entry.get("dataResidency", "unspecified")).strip()
    _require(
        data_residency in DATA_RESIDENCY,
        f"executor {executor_id}: unknown dataResidency: {data_residency}",
    )
    stores_training_data = entry.get("storesTrainingData", False)
    _require(
        isinstance(stores_training_data, bool),
        f"executor {executor_id}: storesTrainingData must be a boolean",
    )
    supports_cloud = entry.get("supportsCloud", True)
    available = entry.get("available", True)
    _require(isinstance(supports_cloud, bool), f"executor {executor_id}: supportsCloud is invalid")
    _require(isinstance(available, bool), f"executor {executor_id}: available is invalid")

    execution_type = _execution_type(entry.get("executionType"))
    shadow_cost_usd = _float(entry.get("shadowCostUsd", 0), "shadowCostUsd", 0, 1_000_000)
    subscription_monthly_usd = _float(
        entry.get("subscriptionMonthlyUsd", 0), "subscriptionMonthlyUsd", 0, 1_000_000
    )
    subscription_capacity_remaining = _int(
        entry.get("subscriptionCapacityRemaining", 0),
        "subscriptionCapacityRemaining",
        0,
        1_000_000,
    )
    if execution_type in {ExecutionType.SUBSCRIPTION_CLOUD, ExecutionType.SUBSCRIPTION_RUNNER}:
        missing = [
            key
            for key in ("shadowCostUsd", "subscriptionMonthlyUsd", "subscriptionCapacityRemaining")
            if key not in entry
        ]
        if missing:
            raise ExecutorError(
                f"executor {executor_id}: subscription executors must declare " + ", ".join(missing)
            )
        if shadow_cost_usd <= 0:
            raise ExecutorError(
                f"executor {executor_id}: subscription executors require positive shadowCostUsd"
            )

    return ExecutorProfile(
        executor_id=executor_id,
        provider=provider,
        adapter=adapter,
        adapter_version=adapter_version,
        execution_type=execution_type,
        auth_mode=_auth_mode(entry.get("authMode")),
        model=model,
        model_family=model_family,
        task_classes=task_classes,
        capabilities=capabilities,
        tool_capabilities=tool_capabilities,
        context_window=_int(entry.get("contextWindow", 0), "contextWindow", 0, 10_000_000),
        supports_cloud=supports_cloud,
        available=available,
        max_concurrency=_int(entry.get("maxConcurrency", 1), "maxConcurrency", 1, 10_000),
        active_runs=_int(entry.get("activeRuns", 0), "activeRuns", 0, 10_000),
        max_risk=_risk(entry.get("maxRisk", "medium"), "maxRisk"),
        quality_lower_bound=_float(entry.get("qualityLowerBound", 0), "qualityLowerBound", 0, 1),
        direct_cost_usd=_float(entry.get("directCostUsd", 0), "directCostUsd", 0, 1_000_000),
        shadow_cost_usd=shadow_cost_usd,
        subscription_monthly_usd=subscription_monthly_usd,
        subscription_capacity_remaining=subscription_capacity_remaining,
        average_latency_seconds=_int(
            entry.get("averageLatencySeconds", 0), "averageLatencySeconds", 0, 86400
        ),
        recent_failure_rate=_float(entry.get("recentFailureRate", 0), "recentFailureRate", 0, 1),
        data_residency=data_residency,
        stores_training_data=stores_training_data,
        permitted_repositories=_string_tuple(
            entry.get("permittedRepositories"), f"executor {executor_id}: permittedRepositories"
        ),
    )


def load_executors(document: object) -> tuple[ExecutorProfile, ...]:
    if isinstance(document, dict):
        if document.get("schemaVersion") != 1:
            raise ExecutorError("executor registry schemaVersion must be 1")
        raw = document.get("executors")
    else:
        raw = document
    if not isinstance(raw, list) or not raw:
        raise ExecutorError("executors must be a non-empty array")
    executors = tuple(_parse_executor(item) for item in raw)
    seen: set[str] = set()
    for executor in executors:
        if executor.executor_id in seen:
            raise ExecutorError(f"duplicate executorId: {executor.executor_id}")
        seen.add(executor.executor_id)
    return executors


def load_routing_policy(document: object | None) -> RoutingPolicy:
    if document is None:
        return RoutingPolicy()
    if not isinstance(document, dict):
        raise ExecutorError("routing policy must be an object")
    if any("credential" in key.lower() or "secret" in key.lower() for key in document):
        raise ExecutorError("routing policy must not contain credentials or secrets")
    version = str(document.get("policyVersion", "1.0.0")).strip()
    _require(bool(version), "policyVersion is required")
    floors = document.get("qualityFloors", {})
    if not isinstance(floors, dict):
        raise ExecutorError("qualityFloors must be an object")
    quality_floors = {
        risk: _float(floors.get(risk.value, RoutingPolicy().floor_for(risk)), risk.value, 0, 1)
        for risk in (RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL)
    }
    allowed_providers = _string_tuple(document.get("allowedProviders"), "allowedProviders")
    if not allowed_providers:
        allowed_providers = tuple(sorted(KNOWN_PROVIDERS))
    unknown_allowed = sorted(set(allowed_providers) - KNOWN_PROVIDERS)
    if unknown_allowed:
        raise ExecutorError(
            "allowedProviders contains unknown providers: " + ", ".join(unknown_allowed)
        )
    denied_providers = _string_tuple(document.get("deniedProviders"), "deniedProviders")
    unknown_denied = sorted(set(denied_providers) - KNOWN_PROVIDERS)
    if unknown_denied:
        raise ExecutorError(
            "deniedProviders contains unknown providers: " + ", ".join(unknown_denied)
        )
    allowed_data_residency = _string_tuple(
        document.get("allowedDataResidency"), "allowedDataResidency"
    )
    if not allowed_data_residency:
        allowed_data_residency = tuple(sorted(DATA_RESIDENCY))
    unknown_residency = sorted(set(allowed_data_residency) - DATA_RESIDENCY)
    if unknown_residency:
        raise ExecutorError(
            "allowedDataResidency contains unknown values: " + ", ".join(unknown_residency)
        )
    require_no_training_storage = document.get("requireNoTrainingStorage", False)
    if not isinstance(require_no_training_storage, bool):
        raise ExecutorError("requireNoTrainingStorage must be a boolean")
    return RoutingPolicy(
        policy_version=version,
        quality_floors=quality_floors,
        allowed_providers=allowed_providers,
        denied_providers=denied_providers,
        require_no_training_storage=require_no_training_storage,
        allowed_data_residency=allowed_data_residency,
    )


def _risk_order(risk: RiskLevel) -> int:
    return {
        RiskLevel.LOW: 0,
        RiskLevel.MEDIUM: 1,
        RiskLevel.HIGH: 2,
        RiskLevel.CRITICAL: 3,
    }[risk]


def request_from_mission(
    mission: MissionSpec,
    *,
    repository: str,
    task_class: TaskClass,
    budget_usd: float,
    required_tool_capabilities: Sequence[str] = (),
    min_context_window: int = 0,
) -> RouteRequest:
    return RouteRequest(
        repository=repository,
        task_class=task_class,
        risk=mission.risk,
        required_capabilities=mission.capabilities,
        required_tool_capabilities=tuple(required_tool_capabilities),
        min_context_window=min_context_window,
        budget_usd=budget_usd,
    )


def route_executor(
    request: RouteRequest,
    executors: Sequence[ExecutorProfile],
    policy: RoutingPolicy | None = None,
) -> RouteDecision:
    active_policy = policy or RoutingPolicy()
    floor = active_policy.floor_for(request.risk)
    required_capabilities = set(request.required_capabilities)
    unknown_caps = sorted(required_capabilities - KNOWN_CAPABILITIES)
    if unknown_caps:
        raise ExecutorError("request has unknown capabilities: " + ", ".join(unknown_caps))
    required_tools = set(request.required_tool_capabilities)
    unknown_tools = sorted(required_tools - TOOL_CAPABILITIES)
    if unknown_tools:
        raise ExecutorError("request has unknown tool capabilities: " + ", ".join(unknown_tools))
    if request.budget_usd < 0:
        raise ExecutorError("budget_usd cannot be negative")

    considered: list[dict[str, Any]] = []
    eligible: list[tuple[float, str, ExecutorProfile]] = []
    for executor in executors:
        reasons: list[str] = []
        if not executor.available:
            reasons.append("unavailable")
        if executor.provider not in active_policy.allowed_providers:
            reasons.append("provider not allowed")
        if executor.provider in active_policy.denied_providers:
            reasons.append("provider denied")
        if (
            request.repository
            and executor.permitted_repositories
            and request.repository not in executor.permitted_repositories
        ):
            reasons.append("repository not permitted")
        if request.task_class not in executor.task_classes:
            reasons.append(f"does not support task class {request.task_class.value}")
        missing_caps = sorted(required_capabilities - set(executor.capabilities))
        if missing_caps:
            reasons.append("missing capabilities: " + ", ".join(missing_caps))
        missing_tools = sorted(required_tools - set(executor.tool_capabilities))
        if missing_tools:
            reasons.append("missing tool capabilities: " + ", ".join(missing_tools))
        if _risk_order(executor.max_risk) < _risk_order(request.risk):
            reasons.append(f"not cleared for {request.risk.value} risk")
        if executor.quality_lower_bound < floor:
            reasons.append(
                f"quality floor not met: {executor.quality_lower_bound:.3f} < {floor:.3f}"
            )
        if executor.context_window < request.min_context_window:
            reasons.append(
                "context window too small: "
                f"{executor.context_window} < {request.min_context_window}"
            )
        if executor.active_runs >= executor.max_concurrency:
            reasons.append("capacity exhausted")
        if (
            executor.execution_type
            in {ExecutionType.SUBSCRIPTION_CLOUD, ExecutionType.SUBSCRIPTION_RUNNER}
            and executor.subscription_capacity_remaining <= 0
        ):
            reasons.append("subscription capacity exhausted")
        if executor.expected_mission_cost > request.budget_usd:
            reasons.append(
                f"budget exceeded: {executor.expected_mission_cost:.4f} > {request.budget_usd:.4f}"
            )
        if executor.data_residency not in active_policy.allowed_data_residency:
            reasons.append("data residency not allowed")
        if active_policy.require_no_training_storage and executor.stores_training_data:
            reasons.append("training-data storage not allowed")

        ecps = (
            round(executor.expected_mission_cost / executor.quality_lower_bound, 6)
            if executor.quality_lower_bound > 0
            else None
        )
        considered.append(
            {
                "executorId": executor.executor_id,
                "provider": executor.provider,
                "model": executor.model,
                "executionType": executor.execution_type.value,
                "authMode": executor.auth_mode.value,
                "qualityLowerBound": executor.quality_lower_bound,
                "expectedMissionCostUsd": round(executor.expected_mission_cost, 6),
                "ecps": ecps,
                "eligible": not reasons,
                "rejectionReasons": reasons,
            }
        )
        if not reasons:
            assert ecps is not None
            eligible.append((ecps, executor.executor_id, executor))

    if not eligible:
        return RouteDecision(
            RouteStatus.INSUFFICIENT_BUDGET_OR_ASSURANCE,
            "",
            active_policy.policy_version,
            floor,
            request.budget_usd,
            tuple(considered),
            "no eligible executor satisfies quality, capability, capacity, security, "
            "and budget constraints",
        )

    _, _, selected = min(eligible)
    return RouteDecision(
        RouteStatus.SELECTED,
        selected.executor_id,
        active_policy.policy_version,
        floor,
        request.budget_usd,
        tuple(considered),
        "selected lowest expected cost per successful mission among eligible executors",
    )


def load_executors_file(path: str | Path) -> tuple[ExecutorProfile, ...]:
    return load_executors(json.loads(Path(path).read_text(encoding="utf-8")))
