# Adaptive Intelligence Router

## Mission

Agentic SDLC selects the cheapest available execution path that satisfies a task-specific quality and risk floor, using evidence from real project outcomes rather than static model rankings.

The router treats intelligence providers and execution environments as capabilities, including:

- subscription-backed Claude Code (Pro/Max OAuth)
- subscription-backed Codex cloud / Codex clients
- OpenAI API
- Anthropic API
- Google Vertex AI
- AWS Bedrock
- Azure / Foundry
- DeepSeek
- Kimi
- self-hosted/open-weight models
- future providers through a provider adapter contract

## Principles

1. Quality is a constraint first; cost is optimized second.
2. Planning, implementation, review, repair, and triage are separate task classes with separate quality histories.
3. The implementing model never solely judges its own work.
4. Deterministic tests, hidden tests, policy gates, security checks, and domain-specific validation are primary evidence.
5. Subscription capacity has a shadow cost rather than being treated as literally free.
6. New models earn higher-risk work through controlled low-risk exploration.
7. Provider failures, quota exhaustion, and authentication failures are infrastructure failures, not code-quality verdicts.
8. Human approval policy is independent of model choice.

## Optimization objective

For candidate executor A and task context x, the router maintains a posterior estimate of success quality Q(A|x), including a conservative lower confidence bound.

Routing solves:

    minimize ExpectedCostPerSuccessfulMission(A | x)

subject to:

    QualityLowerBound(A | x) >= RequiredQuality(risk)
    capabilities(A) satisfy task requirements
    provider policy permits execution
    budget remaining >= expected mission cost

If no candidate satisfies the quality floor inside the budget, the router reports that the requested assurance cannot be achieved within budget rather than lowering the definition of quality.

### Expected cost per successful mission

    ECPS = (model_cost + compute_cost + retry_cost + expected_human_cost) /
           P(defect-free successful completion)

For subscription-backed executors, model_cost is replaced by a shadow cost derived from subscription price, remaining quota/capacity, reset horizon, and observed successful missions per billing period.

## Three quality measurements

Every mission records:

1. Predicted quality before execution — based on historical evidence for the candidate/task combination.
2. Verified quality at merge time — acceptance criteria, deterministic tests, hidden tests, policy/security gates, independent review, unresolved risk.
3. Realized quality after merge/deployment — regressions, incidents, rework, rollback, user/domain outcomes.

Realized outcomes recalibrate future routing decisions.

## Provider registry

Each provider/executor advertises:

- provider id and adapter version
- execution type: subscription-cloud, subscription-runner, direct-api, cloud-platform, self-hosted
- model / model family
- supported task classes
- tool and structured-output capabilities
- context limits
- cloud execution support
- authentication mode
- current availability
- input/output/cached pricing when applicable
- subscription price and observed quota/capacity when applicable
- latency and failure history
- security/data-residency properties
- concurrency and budget limits

No consumer login cookie or browser session may be scraped or stored. Subscription-backed execution is permitted only through provider-supported authentication or delegation mechanisms.

## Learning store

Every run records at least:

- project/repository
- task class and risk class
- planner, implementer, reviewer, repair executor
- provider/model/execution type
- context-pack digest and task/spec version
- token/usage information when available
- direct and shadow cost
- elapsed time
- attempts/retries
- acceptance-criteria result
- deterministic/hidden test results
- review findings by severity
- human intervention and decision
- post-merge defects/rework/rollback
- domain-specific realized outcome

Historical outcomes remain immutable and failed/abandoned runs stay in denominators.

## Exploration and adaptation

The router uses conservative contextual exploration. A configurable small fraction of eligible low-risk work may be routed to promising challenger executors. Critical/high-risk work is restricted to candidates whose conservative quality lower bound exceeds the policy floor.

Model/provider promotions are evidence-backed and policy-controlled. High-risk routing-policy changes require independent evaluation and human approval.

## Cloud-first execution

Agentic SDLC is the control plane; execution happens remotely whenever possible.

- GitHub Actions: deterministic CI, policy gates, event-driven orchestration, and provider runners where appropriate.
- Claude Max/Pro: supported through provider-supported Claude Code OAuth token for CI/runners.
- Codex via ChatGPT plans: delegated cloud work or authenticated Codex client execution through official OpenAI surfaces; ChatGPT subscription credentials are never treated as API keys.
- Vertex/Bedrock/Foundry: provider-specific cloud identity and model execution.

The local developer computer is optional for normal operation. It is primarily a bootstrap, emergency-debug, local-hardware, or high-bandwidth interactive environment.

## Human control plane

Mobile/web users see mission state, budget, quality evidence, decisions requiring approval, and recommended actions rather than raw runner logs. Policies determine which actions may auto-advance and which always require human approval.

## Initial delivery sequence

1. Provider/executor registry and capability schema.
2. Cost model including subscription shadow cost and hard budgets.
3. Quality evidence schema and conservative quality estimator.
4. Adaptive router with deterministic policy/risk floors.
5. Claude subscription OAuth executor and API/provider fallbacks.
6. Codex subscription-cloud executor contract using official supported delegation/authentication surfaces.
7. Vertex/Bedrock and generic direct-API adapters.
8. Benchmark lab / historical replay and challenger evaluation.
9. Mobile-friendly mission/control API and approval surface.
10. Outcome feedback loop and evidence-backed dynamic re-routing.
