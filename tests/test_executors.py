from __future__ import annotations

from dataclasses import replace

import pytest

from agentic_sdlc.executors import (
    AuthMode,
    ExecutorError,
    RouteRequest,
    RouteStatus,
    RoutingPolicy,
    TaskClass,
    load_executors,
    load_routing_policy,
    route_executor,
)
from agentic_sdlc.models import RiskLevel


def _executor(executor_id: str, **overrides):
    values = {
        "executorId": executor_id,
        "provider": "openai",
        "adapter": "openai-direct",
        "adapterVersion": "1.0.0",
        "executionType": "direct-api",
        "authMode": "api-key",
        "model": "gpt-5",
        "modelFamily": "gpt-5",
        "taskClasses": ["implementation", "review"],
        "capabilities": ["edit-code", "author-tests", "run-commands", "review-code"],
        "toolCapabilities": ["structured-output", "function-calling"],
        "contextWindow": 200000,
        "maxConcurrency": 4,
        "activeRuns": 0,
        "maxRisk": "high",
        "qualityLowerBound": 0.9,
        "directCostUsd": 4.0,
        "shadowCostUsd": 0.0,
        "dataResidency": "us",
    }
    values.update(overrides)
    return values


def _request(**overrides) -> RouteRequest:
    values = {
        "repository": "atulg4/agentic-sdlc",
        "task_class": TaskClass.IMPLEMENTATION,
        "risk": RiskLevel.MEDIUM,
        "required_capabilities": ("edit-code", "author-tests", "run-commands"),
        "required_tool_capabilities": ("structured-output",),
        "min_context_window": 100000,
        "budget_usd": 10.0,
    }
    values.update(overrides)
    return RouteRequest(**values)


def _codex_subscription(**overrides):
    values = {
        "provider": "codex",
        "adapter": "codex-cli",
        "executionType": "subscription-runner",
        "authMode": "oauth",
        "model": "account-supported-model",
        "modelAlias": "codex",
        "directCostUsd": 0,
        "shadowCostUsd": 1,
        "subscriptionMonthlyUsd": 200,
        "subscriptionCapacityRemaining": 10,
    }
    values.update(overrides)
    return _executor("codex-subscription", **values)


@pytest.mark.parametrize("execution_type", ["subscription-cloud", "subscription-runner"])
def test_codex_subscription_requires_oauth(execution_type) -> None:
    executor = load_executors([_codex_subscription(executionType=execution_type)])[0]
    assert executor.auth_mode is AuthMode.OAUTH

    with pytest.raises(ExecutorError, match="Codex subscription executors require oauth"):
        load_executors([_codex_subscription(executionType=execution_type, authMode="api-key")])
    with pytest.raises(ExecutorError, match="credentials or secrets"):
        load_executors([_codex_subscription(credentialReference="OPENAI_API_KEY")])


def test_directly_constructed_codex_subscription_cannot_route_with_api_auth() -> None:
    executor = load_executors([_codex_subscription()])[0]
    invalid = replace(executor, auth_mode=AuthMode.API_KEY)

    with pytest.raises(ExecutorError, match="Codex subscription executors require oauth"):
        route_executor(_request(), [invalid])


@pytest.mark.parametrize(
    ("execution_type", "auth_mode", "expected"),
    [
        ("subscription-cloud", "oauth", "provider-cloud"),
        ("subscription-runner", "oauth", "authenticated-remote-client"),
        ("direct-api", "api-key", "api"),
        ("cloud-platform", "oidc", "api"),
        ("self-hosted", "local-trusted-runner", "authenticated-remote-client"),
    ],
)
def test_routes_report_the_derived_execution_surface(execution_type, auth_mode, expected) -> None:
    executor = load_executors(
        [_codex_subscription(executionType=execution_type, authMode=auth_mode)]
    )[0]
    decision = route_executor(_request(), [executor])

    assert executor.execution_surface == expected
    assert decision.as_dict()["executionSurface"] == expected
    assert decision.as_dict()["candidates"][0]["executionSurface"] == expected


@pytest.mark.parametrize("provider", ["openai", "codex"])
@pytest.mark.parametrize("policy_document", [None, {}, {"allowApiBillingFallback": False}])
def test_codex_subscription_does_not_silently_fall_back_to_api(provider, policy_document) -> None:
    executors = load_executors(
        [
            _codex_subscription(runtimeStatus="quota-exhausted"),
            _executor("paid-api", provider=provider, directCostUsd=0.01),
        ]
    )
    decision = route_executor(_request(), executors, load_routing_policy(policy_document))

    assert decision.status is RouteStatus.INSUFFICIENT_BUDGET_OR_ASSURANCE
    assert decision.as_dict()["executionSurface"] is None
    by_id = {item["executorId"]: item for item in decision.candidates}
    assert by_id["codex-subscription"]["recoverable"] is True
    assert "API billing fallback is disabled" in by_id["paid-api"]["rejectionReasons"]


def test_explicit_api_fallback_still_enforces_quality_floor() -> None:
    policy = load_routing_policy({"allowApiBillingFallback": True})
    executors = load_executors(
        [
            _codex_subscription(runtimeStatus="quota-exhausted"),
            _executor("paid-api", qualityLowerBound=0.6),
        ]
    )
    assert policy.as_dict()["allowApiBillingFallback"] is True
    assert route_executor(_request(), executors, policy).status is (
        RouteStatus.INSUFFICIENT_BUDGET_OR_ASSURANCE
    )

    qualified = replace(executors[1], quality_lower_bound=0.9)
    decision = route_executor(_request(), [executors[0], qualified], policy)
    assert decision.selected_executor_id == "paid-api"
    assert decision.as_dict()["executionSurface"] == "api"


def test_exhausted_codex_subscription_can_fall_back_to_qualified_claude_subscription() -> None:
    executors = load_executors(
        [
            _codex_subscription(runtimeStatus="quota-exhausted"),
            _executor(
                "claude-subscription",
                provider="anthropic",
                model="claude-opus-5",
                executionType="subscription-runner",
                authMode="oauth",
                directCostUsd=0,
                shadowCostUsd=1,
                subscriptionMonthlyUsd=200,
                subscriptionCapacityRemaining=10,
            ),
        ]
    )
    assert route_executor(_request(), executors).selected_executor_id == "claude-subscription"
    unqualified = replace(executors[1], quality_lower_bound=0.6)
    assert route_executor(_request(), [executors[0], unqualified]).status is (
        RouteStatus.INSUFFICIENT_BUDGET_OR_ASSURANCE
    )


@pytest.mark.parametrize("value", [1, "true", None, []])
def test_api_billing_fallback_flag_requires_a_boolean(value) -> None:
    with pytest.raises(ExecutorError, match="allowApiBillingFallback must be a boolean"):
        load_routing_policy({"allowApiBillingFallback": value})
    with pytest.raises(ExecutorError, match="allowApiBillingFallback must be a boolean"):
        route_executor(
            _request(),
            load_executors([_executor("paid-api")]),
            RoutingPolicy(allow_api_billing_fallback=value),
        )


def test_subscription_route_serializes_only_approved_control_fields() -> None:
    executor = load_executors([_codex_subscription()])[0]
    route = route_executor(_request(), [executor]).as_dict()

    assert set(executor.as_dict()) == {
        "executorId",
        "provider",
        "adapter",
        "adapterVersion",
        "executionType",
        "authMode",
        "model",
        "modelAlias",
        "modelFamily",
        "taskClasses",
        "capabilities",
        "toolCapabilities",
        "contextWindow",
        "supportsCloud",
        "available",
        "runtimeStatus",
        "runtimeStatusReason",
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
    assert set(route) == {
        "status",
        "selectedExecutorId",
        "executionSurface",
        "policyVersion",
        "requiredQualityFloor",
        "budgetUsd",
        "candidates",
        "reason",
    }
    assert set(route["candidates"][0]) == {
        "executorId",
        "provider",
        "model",
        "modelAlias",
        "executionType",
        "executionSurface",
        "authMode",
        "qualityLowerBound",
        "expectedMissionCostUsd",
        "ecps",
        "preferenceRank",
        "recoverable",
        "eligible",
        "rejectionReasons",
    }


@pytest.mark.parametrize(
    "subscription_overrides",
    [None, {"permittedRepositories": ["another/repository"]}, {"taskClasses": ["review"]}],
)
def test_api_routing_without_a_matching_codex_subscription_is_preserved(
    subscription_overrides,
) -> None:
    entries = [_executor("paid-api")]
    if subscription_overrides is not None:
        entries.append(_codex_subscription(**subscription_overrides))

    decision = route_executor(_request(), load_executors(entries))
    assert decision.selected_executor_id == "paid-api"


def test_deepseek_can_be_selected_when_it_meets_quality_and_capability_floor() -> None:
    executors = load_executors(
        [
            _executor("openai-expensive", directCostUsd=6.0),
            _executor(
                "deepseek-cheap",
                provider="deepseek",
                adapter="deepseek-direct",
                model="deepseek-reasoner",
                modelFamily="deepseek",
                directCostUsd=0.75,
                qualityLowerBound=0.82,
            ),
        ]
    )

    decision = route_executor(_request(), executors)

    assert decision.status is RouteStatus.SELECTED
    assert decision.selected_executor_id == "deepseek-cheap"
    selected = [item for item in decision.candidates if item["executorId"] == "deepseek-cheap"][0]
    assert selected["ecps"] < 1


def test_initial_provider_families_are_valid_registry_entries() -> None:
    entries = [
        ("anthropic", "claude-code", "subscription-runner", "oauth", "claude-opus-5"),
        ("aws-bedrock", "bedrock", "cloud-platform", "oidc", "amazon.nova-pro"),
        ("azure-foundry", "foundry", "cloud-platform", "workload-identity", "gpt-5-mini"),
        ("codex", "codex-cloud", "subscription-cloud", "oauth", "gpt-5-codex"),
        ("deepseek", "deepseek-direct", "direct-api", "api-key", "deepseek-reasoner"),
        ("google-vertex", "vertex", "cloud-platform", "workload-identity", "gemini-2.5-flash"),
        ("kimi", "kimi-direct", "direct-api", "api-key", "kimi-k2"),
        ("moonshot", "moonshot-direct", "direct-api", "api-key", "moonshot-v1-128k"),
        ("openai", "openai-direct", "direct-api", "api-key", "gpt-5-mini"),
        ("self-hosted", "trusted-runner", "self-hosted", "local-trusted-runner", "qwen3-coder"),
        ("zai", "zai-direct", "direct-api", "api-key", "glm-5.3"),
    ]
    executors = load_executors(
        [
            _executor(
                f"{provider}-executor",
                provider=provider,
                adapter=adapter,
                executionType=execution_type,
                authMode=auth_mode,
                model=model,
                modelFamily=model.split(".", 1)[0],
                shadowCostUsd=0.2 if execution_type.startswith("subscription") else 0.0,
                subscriptionMonthlyUsd=20 if execution_type.startswith("subscription") else 0.0,
                subscriptionCapacityRemaining=5 if execution_type.startswith("subscription") else 0,
                supportsCloud=execution_type != "self-hosted",
                dataResidency="local-only" if execution_type == "self-hosted" else "us",
            )
            for provider, adapter, execution_type, auth_mode, model in entries
        ]
    )

    assert {executor.provider for executor in executors} == {entry[0] for entry in entries}
    serialized = [executor.as_dict() for executor in executors]
    assert {item["authMode"] for item in serialized} == {
        "api-key",
        "local-trusted-runner",
        "oauth",
        "oidc",
        "workload-identity",
    }
    assert any(item["modelAlias"] == "glm-5.3" for item in serialized)


def test_cheapest_executor_never_wins_below_quality_floor() -> None:
    executors = load_executors(
        [
            _executor("openai-qualified", directCostUsd=6.0, qualityLowerBound=0.9),
            _executor(
                "deepseek-too-risky",
                provider="deepseek",
                adapter="deepseek-direct",
                model="deepseek-chat",
                modelFamily="deepseek",
                directCostUsd=0.05,
                qualityLowerBound=0.6,
            ),
        ]
    )

    decision = route_executor(_request(risk=RiskLevel.HIGH), executors)

    assert decision.selected_executor_id == "openai-qualified"
    rejected = [item for item in decision.candidates if item["executorId"] == "deepseek-too-risky"][
        0
    ]
    assert "quality floor not met: 0.600 < 0.850" in rejected["rejectionReasons"]


def test_no_executor_inside_budget_returns_assurance_decision() -> None:
    executors = load_executors([_executor("openai-expensive", directCostUsd=6.0)])

    decision = route_executor(_request(budget_usd=1.0), executors)

    assert decision.status is RouteStatus.INSUFFICIENT_BUDGET_OR_ASSURANCE
    assert decision.selected_executor_id == ""
    assert "no eligible executor" in decision.reason
    assert decision.candidates[0]["rejectionReasons"] == ["budget exceeded: 6.0000 > 1.0000"]


def test_policy_quality_floor_can_reject_all_cheap_models() -> None:
    policy = load_routing_policy({"policyVersion": "strict", "qualityFloors": {"medium": 0.92}})
    executors = load_executors(
        [
            _executor(
                "deepseek-cheap",
                provider="deepseek",
                adapter="deepseek-direct",
                model="deepseek-reasoner",
                modelFamily="deepseek",
                directCostUsd=0.25,
                qualityLowerBound=0.82,
            ),
            _executor(
                "kimi-cheap",
                provider="kimi",
                adapter="kimi-direct",
                model="kimi-k2",
                modelFamily="kimi",
                directCostUsd=0.35,
                qualityLowerBound=0.84,
            ),
        ]
    )

    decision = route_executor(_request(), executors, policy)

    assert decision.status is RouteStatus.INSUFFICIENT_BUDGET_OR_ASSURANCE
    assert decision.required_quality_floor == 0.92
    for candidate in decision.candidates:
        assert candidate["eligible"] is False
        assert any(
            reason.startswith("quality floor not met") for reason in candidate["rejectionReasons"]
        )


def test_capability_tool_context_and_capacity_rejections_are_explainable() -> None:
    executors = load_executors(
        [
            _executor(
                "kimi-missing",
                provider="kimi",
                adapter="kimi-direct",
                model="kimi-k2",
                modelFamily="kimi",
                capabilities=["edit-code"],
                toolCapabilities=[],
                contextWindow=32000,
                activeRuns=1,
                maxConcurrency=1,
                directCostUsd=0.4,
            )
        ]
    )

    decision = route_executor(_request(), executors)

    reasons = decision.candidates[0]["rejectionReasons"]
    assert "missing capabilities: author-tests, run-commands" in reasons
    assert "missing tool capabilities: structured-output" in reasons
    assert "context window too small: 32000 < 100000" in reasons
    assert "capacity exhausted" in reasons


def test_subscription_executor_uses_shadow_cost_not_zero_cost() -> None:
    executors = load_executors(
        [
            _executor(
                "claude-subscription",
                provider="anthropic",
                adapter="claude-code",
                executionType="subscription-runner",
                authMode="oauth",
                model="claude-opus-5",
                modelFamily="claude",
                directCostUsd=0,
                shadowCostUsd=2.5,
                subscriptionMonthlyUsd=200,
                subscriptionCapacityRemaining=20,
            ),
            _executor(
                "deepseek-direct",
                provider="deepseek",
                adapter="deepseek-direct",
                model="deepseek-reasoner",
                modelFamily="deepseek",
                directCostUsd=1.0,
                qualityLowerBound=0.86,
            ),
        ]
    )

    decision = route_executor(_request(risk=RiskLevel.HIGH, budget_usd=5), executors)

    assert decision.selected_executor_id == "deepseek-direct"
    subscription = [
        item for item in decision.candidates if item["executorId"] == "claude-subscription"
    ][0]
    assert subscription["expectedMissionCostUsd"] == 2.5


def test_subscription_executor_requires_shadow_cost_metadata() -> None:
    with pytest.raises(ExecutorError, match="subscription executors must declare"):
        load_executors(
            [
                _executor(
                    "claude-subscription",
                    provider="anthropic",
                    adapter="claude-code",
                    executionType="subscription-runner",
                    authMode="oauth",
                    model="claude-opus-5",
                    modelFamily="claude",
                )
            ]
        )


def test_deterministic_tie_breaking_uses_executor_id() -> None:
    executors = load_executors(
        [
            _executor("z-runner", directCostUsd=1.0, qualityLowerBound=0.8),
            _executor("a-runner", directCostUsd=1.0, qualityLowerBound=0.8),
        ]
    )

    decision = route_executor(_request(), executors)

    assert decision.selected_executor_id == "a-runner"


def test_unknown_provider_model_alias_or_auth_mode_fails_closed() -> None:
    with pytest.raises(ExecutorError, match="unknown provider"):
        load_executors([_executor("bad", provider="mystery")])
    with pytest.raises(ExecutorError, match="unknown modelAlias"):
        load_executors([_executor("bad", model="provider-id", modelAlias="gpt-unknown")])
    with pytest.raises(ExecutorError, match="authMode is unknown"):
        load_executors([_executor("bad", authMode="browser-cookie")])


def test_model_identifiers_are_configurable_behind_documented_aliases() -> None:
    executor = load_executors(
        [
            _executor(
                "deepseek-flash",
                provider="deepseek",
                adapter="openai-compatible",
                model="provider-specific-flash-id",
                modelAlias="deepseek-v4-flash",
                modelFamily="deepseek",
                directCostUsd=0.2,
            )
        ]
    )[0]

    assert executor.model == "provider-specific-flash-id"
    assert executor.model_alias == "deepseek-v4-flash"


def test_default_policy_routes_routine_serious_difficult_and_escalation_tiers() -> None:
    common = {
        "taskClasses": ["implementation", "repair"],
        "capabilities": ["edit-code", "author-tests", "run-commands"],
        "toolCapabilities": ["structured-output"],
        "contextWindow": 256000,
        "maxRisk": "critical",
        "qualityLowerBound": 0.96,
    }
    executors = load_executors(
        [
            _executor(
                "flash",
                **common,
                provider="deepseek",
                adapter="openai-compatible",
                model="configured-flash-id",
                modelAlias="deepseek-v4-flash",
                modelFamily="deepseek",
                directCostUsd=0.1,
            ),
            _executor(
                "pro",
                **common,
                provider="deepseek",
                adapter="openai-compatible",
                model="configured-pro-id",
                modelAlias="deepseek-v4-pro",
                modelFamily="deepseek",
                directCostUsd=0.2,
            ),
            _executor(
                "glm",
                **common,
                provider="zai",
                adapter="openai-compatible",
                model="configured-glm-id",
                modelAlias="glm-5.3",
                modelFamily="glm",
                directCostUsd=0.4,
            ),
            _executor(
                "kimi",
                **common,
                provider="kimi",
                adapter="openai-compatible",
                model="configured-kimi-id",
                modelAlias="kimi-k3",
                modelFamily="kimi",
                directCostUsd=1.0,
            ),
        ]
    )

    assert route_executor(_request(risk=RiskLevel.LOW), executors).selected_executor_id == "flash"
    assert route_executor(_request(risk=RiskLevel.MEDIUM), executors).selected_executor_id == "pro"
    assert route_executor(_request(risk=RiskLevel.HIGH), executors).selected_executor_id == "glm"
    critical = route_executor(_request(risk=RiskLevel.CRITICAL), executors)
    assert critical.selected_executor_id == "kimi"


def test_recoverable_provider_exhaustion_reroutes_and_records_reason() -> None:
    executors = load_executors(
        [
            _executor(
                "pro-exhausted",
                provider="deepseek",
                adapter="openai-compatible",
                model="configured-pro-id",
                modelAlias="deepseek-v4-pro",
                modelFamily="deepseek",
                directCostUsd=0.2,
                runtimeStatus="quota-exhausted",
                runtimeStatusReason="daily paid quota reached",
            ),
            _executor(
                "glm-fallback",
                provider="zai",
                adapter="openai-compatible",
                model="configured-glm-id",
                modelAlias="glm-5.3",
                modelFamily="glm",
                directCostUsd=0.4,
            ),
        ]
    )

    decision = route_executor(_request(risk=RiskLevel.MEDIUM), executors)

    assert decision.selected_executor_id == "glm-fallback"
    rejected = [item for item in decision.candidates if item["executorId"] == "pro-exhausted"][0]
    assert rejected["recoverable"] is True
    assert "quota-exhausted: daily paid quota reached" in rejected["rejectionReasons"]


def test_registry_serialization_contains_no_credentials() -> None:
    with pytest.raises(ExecutorError, match="credentials or secrets"):
        load_executors([_executor("bad", credentialReference="secret-name")])

    executor = load_executors([_executor("safe")])[0]
    document = executor.as_dict()
    serialized_keys = " ".join(document)
    assert "credential" not in serialized_keys.lower()
    assert "secret" not in serialized_keys.lower()


def test_policy_can_deny_training_storage_and_provider_families() -> None:
    policy = load_routing_policy(
        {
            "policyVersion": "test-policy",
            "deniedProviders": ["openai"],
            "requireNoTrainingStorage": True,
            "allowedDataResidency": ["us"],
        }
    )
    executors = load_executors(
        [
            _executor("openai-denied"),
            _executor(
                "deepseek-training",
                provider="deepseek",
                adapter="deepseek-direct",
                model="deepseek-reasoner",
                modelFamily="deepseek",
                directCostUsd=0.7,
                storesTrainingData=True,
            ),
        ]
    )

    decision = route_executor(_request(), executors, policy)

    assert decision.status is RouteStatus.INSUFFICIENT_BUDGET_OR_ASSURANCE
    assert decision.policy_version == "test-policy"
    reasons = {item["executorId"]: item["rejectionReasons"] for item in decision.candidates}
    assert "provider denied" in reasons["openai-denied"]
    assert "training-data storage not allowed" in reasons["deepseek-training"]


def test_explicit_empty_provider_allowlist_denies_every_executor() -> None:
    executors = load_executors([_executor("openai-1")])
    policy = load_routing_policy({"policyVersion": "deny-all", "allowedProviders": []})

    assert policy.allowed_providers == ()

    decision = route_executor(_request(), executors, policy)

    assert decision.status is RouteStatus.INSUFFICIENT_BUDGET_OR_ASSURANCE
    assert decision.selected_executor_id == ""
    assert "provider not allowed" in decision.candidates[0]["rejectionReasons"]


def test_explicit_empty_data_residency_allowlist_denies_every_executor() -> None:
    executors = load_executors([_executor("openai-1")])
    policy = load_routing_policy({"policyVersion": "deny-all", "allowedDataResidency": []})

    assert policy.allowed_data_residency == ()

    decision = route_executor(_request(), executors, policy)

    assert decision.status is RouteStatus.INSUFFICIENT_BUDGET_OR_ASSURANCE
    assert decision.selected_executor_id == ""
    assert "data residency not allowed" in decision.candidates[0]["rejectionReasons"]


def test_missing_data_residency_allowlist_still_defaults_to_every_known_value() -> None:
    policy = load_routing_policy({"policyVersion": "defaults"})

    assert set(policy.allowed_data_residency) == {
        "customer-controlled",
        "eu",
        "local-only",
        "unspecified",
        "us",
    }


def test_missing_provider_allowlist_still_defaults_to_every_known_provider() -> None:
    policy = load_routing_policy({"policyVersion": "defaults"})

    assert "openai" in policy.allowed_providers
    assert "deepseek" in policy.allowed_providers


def test_routing_policy_rejects_unknown_top_level_keys() -> None:
    with pytest.raises(ExecutorError, match="unknown keys: deniedProvider"):
        load_routing_policy({"policyVersion": "typo", "deniedProvider": ["deepseek"]})

    with pytest.raises(ExecutorError, match="unknown keys: requireNoTrainingStore"):
        load_routing_policy({"requireNoTrainingStore": True})


def test_non_finite_route_budget_is_rejected() -> None:
    executors = load_executors([_executor("openai-1")])

    with pytest.raises(ExecutorError, match="budget_usd must be a finite number"):
        route_executor(_request(budget_usd=float("nan")), executors)

    with pytest.raises(ExecutorError, match="budget_usd must be a finite number"):
        route_executor(_request(budget_usd=float("inf")), executors)

    with pytest.raises(ExecutorError, match="budget_usd cannot be negative"):
        route_executor(_request(budget_usd=-1.0), executors)


def test_zero_quality_executor_under_a_zero_floor_is_rejected_without_asserting() -> None:
    executors = load_executors([_executor("free-but-unproven", qualityLowerBound=0.0)])
    policy = load_routing_policy(
        {
            "policyVersion": "zero-floor",
            "qualityFloors": {"low": 0, "medium": 0, "high": 0, "critical": 0},
        }
    )

    decision = route_executor(_request(), executors, policy)

    assert decision.status is RouteStatus.INSUFFICIENT_BUDGET_OR_ASSURANCE
    candidate = decision.candidates[0]
    assert candidate["ecps"] is None
    assert candidate["eligible"] is False
    assert any(
        "qualityLowerBound must be greater than zero" in reason
        for reason in candidate["rejectionReasons"]
    )


def test_capacity_exhaustion_is_reported_as_a_recoverable_fallback_reason() -> None:
    executors = load_executors(
        [
            _executor("openai-busy", maxConcurrency=2, activeRuns=2),
            _executor(
                "claude-subscription-drained",
                provider="anthropic",
                adapter="claude-code",
                model="claude-opus-5",
                modelFamily="claude",
                executionType="subscription-cloud",
                authMode="oauth",
                directCostUsd=0.0,
                shadowCostUsd=1.5,
                subscriptionMonthlyUsd=200.0,
                subscriptionCapacityRemaining=0,
            ),
            _executor("openai-free", directCostUsd=1.0),
        ]
    )

    decision = route_executor(_request(), executors)

    assert decision.selected_executor_id == "openai-free"
    by_id = {item["executorId"]: item for item in decision.candidates}
    assert by_id["openai-busy"]["recoverable"] is True
    assert by_id["claude-subscription-drained"]["recoverable"] is True
    assert by_id["openai-free"]["recoverable"] is False


def test_partial_preference_override_keeps_defaults_for_other_routes() -> None:
    policy = load_routing_policy(
        {
            "policyVersion": "partial",
            "preferredModelAliases": {"implementation:medium": ["kimi-k3"]},
        }
    )

    assert policy.preference_for(TaskClass.IMPLEMENTATION, RiskLevel.MEDIUM) == ("kimi-k3",)
    assert policy.preference_for(TaskClass.IMPLEMENTATION, RiskLevel.CRITICAL) == (
        "kimi-k3",
        "glm-5.3",
        "claude",
    )
    assert policy.preference_for(TaskClass.REPAIR, RiskLevel.HIGH) == (
        "glm-5.3",
        "kimi-k3",
        "claude",
    )


def test_task_class_preference_override_still_wins_over_default_risk_routes() -> None:
    policy = load_routing_policy(
        {
            "policyVersion": "task-level",
            "preferredModelAliases": {"implementation": ["glm-5.3", "claude"]},
        }
    )

    assert policy.preference_for(TaskClass.IMPLEMENTATION, RiskLevel.LOW) == ("glm-5.3", "claude")
    assert policy.preference_for(TaskClass.REPAIR, RiskLevel.LOW) == (
        "deepseek-v4-pro",
        "deepseek-v4-flash",
        "glm-5.3",
        "kimi-k3",
    )


def test_partial_preference_override_does_not_flatten_critical_escalation_order() -> None:
    executors = load_executors(
        [
            _executor(
                "kimi-escalation",
                provider="kimi",
                adapter="kimi-direct",
                model="kimi-k2-thinking",
                modelAlias="kimi-k3",
                modelFamily="kimi",
                maxRisk="critical",
                qualityLowerBound=0.97,
                directCostUsd=4.0,
            ),
            _executor(
                "zai-cheaper",
                provider="zai",
                adapter="zai-direct",
                model="glm-5.3",
                modelAlias="glm-5.3",
                modelFamily="glm",
                maxRisk="critical",
                qualityLowerBound=0.96,
                directCostUsd=1.0,
            ),
        ]
    )
    policy = load_routing_policy(
        {
            "policyVersion": "partial",
            "preferredModelAliases": {"implementation:medium": ["deepseek-v4-pro"]},
        }
    )

    decision = route_executor(_request(risk=RiskLevel.CRITICAL, budget_usd=10.0), executors, policy)

    assert decision.selected_executor_id == "kimi-escalation"
