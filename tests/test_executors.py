from __future__ import annotations

import pytest

from agentic_sdlc.executors import (
    ExecutorError,
    RouteRequest,
    RouteStatus,
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


def test_unknown_provider_model_or_auth_mode_fails_closed() -> None:
    with pytest.raises(ExecutorError, match="unknown provider"):
        load_executors([_executor("bad", provider="mystery")])
    with pytest.raises(ExecutorError, match="unknown model"):
        load_executors([_executor("bad", model="gpt-unknown")])
    with pytest.raises(ExecutorError, match="authMode is unknown"):
        load_executors([_executor("bad", authMode="browser-cookie")])


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
