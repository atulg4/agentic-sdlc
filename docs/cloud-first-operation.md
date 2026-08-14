# Cloud-first operation

Agentic SDLC is designed so a developer laptop is not required for routine planning, implementation, verification, review, or approval.

## Preferred execution order

1. Provider-supported subscription-backed cloud/runner capacity when available and policy-compatible.
2. Cheapest empirically qualified API/cloud provider that meets the required quality floor.
3. Frontier-model escalation for high-risk work, repeated failure, or reviewer disagreement.
4. Human intervention only when policy, unresolved risk, or exhausted automated recovery requires it.

## Claude Max / Pro

Claude Code GitHub Actions supports a `CLAUDE_CODE_OAUTH_TOKEN` as an alternative to an Anthropic API key. A user authorizes once with the provider-supported `claude setup-token` flow and stores the resulting token as a GitHub Actions secret. The token is never committed or logged.

Agentic SDLC treats this as a subscription-backed runner with bounded capacity, not as a direct API account. Quota exhaustion is an infrastructure/capacity event and may route to another qualified executor.

## ChatGPT Plus / Codex

Codex included with ChatGPT plans supports authenticated Codex clients and delegated cloud tasks connected to GitHub. Agentic SDLC must integrate only through provider-supported delegation/authentication surfaces. A ChatGPT subscription is not an OpenAI API key and must not be represented as one.

Until OpenAI exposes a supported unattended credential/delegation mechanism suitable for the same GitHub-event path, ChatGPT-plan Codex is modeled as a subscription-cloud executor that may require provider-side delegation rather than a generic GitHub Actions secret.

## APIs and cloud model platforms

Direct OpenAI/Anthropic APIs, Vertex AI, Bedrock, Foundry, DeepSeek, Kimi and other adapters remain valuable for event-driven fallback, cross-model review, benchmark challengers, enterprise identity, and capacity overflow.

## GitHub Actions role

GitHub Actions is primarily the remote automation and independent verification fabric:

- event-driven intake/orchestration
- deterministic unit/integration/end-to-end tests
- lint/type/security checks
- policy and protected-path gates
- artifact verification/attestation
- bounded provider runners where supported
- deployment gates when policy permits

AI implementation should not make deterministic CI optional. An implementation agent may run tests during development, but a protected remote verification run judges the resulting artifact independently.

## Best use of a local computer

A local computer is optional for normal operation and should be reserved for:

- one-time provider authentication/bootstrap that requires an interactive login
- emergency diagnosis when cloud runners are unavailable
- local hardware/device integration
- very large local datasets that should not be uploaded
- fast exploratory pair-programming where immediate human feedback is valuable
- credential rotation/recovery where provider policy requires local interaction

A phone or browser should be sufficient for normal mission creation, prioritization, status, discussion, and approvals.
