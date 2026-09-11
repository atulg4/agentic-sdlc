# Provider and model routing

Forge routes implementation and repair work through executor profiles. An
executor profile describes the adapter, provider, configured provider model ID,
stable model alias, capabilities, risk ceiling, cost estimate, and current
runtime availability. It must not contain credentials, tokens, or secret names.

Provider model IDs are intentionally configuration. Policy should use
`modelAlias` values instead of hard-coding provider API identifiers because
those identifiers can differ by gateway, account, region, or provider version.

Supported policy aliases include:

| Alias | Intended role |
|---|---|
| `deepseek-v4-flash` | Cheapest routine/default implementation worker |
| `deepseek-v4-pro` | Normal serious implementation and first repair worker |
| `glm-5.3` | Difficult or architectural implementation |
| `kimi-k3` | Escalation and repeated-failure work |
| `claude`, `claude-opus`, `claude-sonnet` | Claude adapters when quota is available |
| `codex`, `gpt-5`, `gpt-5-mini` | Codex/OpenAI adapters |

The default routing policy prefers:

| Work | First choices |
|---|---|
| Low-risk implementation | `deepseek-v4-flash`, then `deepseek-v4-pro` |
| Medium-risk implementation | `deepseek-v4-pro`, then `deepseek-v4-flash` |
| High-risk implementation | `glm-5.3`, then `deepseek-v4-pro` |
| Critical implementation | `kimi-k3`, then `glm-5.3` |
| Repair | `deepseek-v4-pro`, then `glm-5.3`, then `kimi-k3` |

Consumers can override this with `preferredModelAliases` in a routing policy
JSON file. Keys are either a task class, such as `implementation`, or a
task-and-risk pair, such as `implementation:high`.

Runtime exhaustion is recoverable. Executor status values
`quota-exhausted`, `capacity-exhausted`, and `auth-exhausted` reject that
executor for the current route while marking the candidate as recoverable in
the route decision. Deterministic verification, review, and merge gates do not
change when fallback occurs.

Secrets remain outside routing files. Adapter jobs should read credentials from
environment variables populated by the CI secret store, for example
`DEEPSEEK_API_KEY`, `ZAI_API_KEY`, `KIMI_API_KEY`,
`CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`, or `OPENAI_API_KEY`.

Every route decision records the selected provider, configured model ID,
stable alias, policy version, candidate rejection reasons, and recoverable
fallback reasons. Orchestration run records can persist `modelAlias`,
`routingPolicyVersion`, `routeReason`, and `fallbackReason` in durable
telemetry.
