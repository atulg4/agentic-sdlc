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
task-and-risk pair, such as `implementation:high`. Overrides are merged per key
onto the defaults, so overriding `implementation:medium` leaves the documented
escalation order of every other task and risk tier intact. The most specific
entry wins: an explicit `task:risk` override, then an explicit task-class
override, then the default `task:risk` preference, then the default task-class
preference.

## Routing policy validation

A routing policy must declare only known top-level keys: `policyVersion`,
`qualityFloors`, `allowedProviders`, `deniedProviders`,
`preferredModelAliases`, `requireNoTrainingStorage`, and
`allowedDataResidency`. Any other key fails validation, so a misspelled
security field such as `deniedProvider` or `requireNoTrainingStore` can never be
silently ignored.

`allowedProviders` distinguishes a missing value from an explicitly empty one.
Omitting the key allows every known provider. Setting `allowedProviders: []` is
a deny-all decision for incident response or maintenance: no executor is
eligible and the route fails closed instead of falling back to every provider.

Budgets must be finite. A `--budget-usd` value of `nan` or `inf` is rejected by
both the CLI and the router rather than silently disabling the maximum-spend
constraint.

An executor whose `qualityLowerBound` is zero has no defined expected cost per
successful mission, so it is rejected with a recorded reason even under a
quality floor of zero.

Runtime exhaustion is recoverable. Executor status values
`quota-exhausted`, `capacity-exhausted`, and `auth-exhausted` reject that
executor for the current route while marking the candidate as recoverable in
the route decision. Concurrency and subscription-capacity exhaustion are
recoverable the same way, so ordinary capacity-driven fallbacks keep their
reason in `fallbackReason` telemetry. Deterministic verification, review, and
merge gates do not change when fallback occurs.

When a provider reports exhaustion during the request itself - HTTP 401/403,
429, or 503/529 - the routed adapter continues to the next eligible
OpenAI-compatible candidate from the same route decision, in the order routing
ranked them, for a bounded number of attempts. The fallback reason is recorded
in the agent message. If every candidate is exhausted the run fails closed.

## Dispatching the routed executor

Supplying `executor_registry_path` requires `agent: route`. The implementation
workflow refuses the run otherwise, because the legacy `agent` input would
otherwise override the recorded provider, cost, and policy decision. Patch
generation dispatches solely from the selected executor: its provider chooses
the adapter, and for Anthropic executors its `authMode` chooses the credential
and its `model` is passed to Claude Code. Legacy `agent: codex` and
`agent: claude` runs are expressed as a single-candidate route so the same
dispatch path applies.

The route requires a context window large enough for the bundle the
OpenAI-compatible adapter actually sends - the task prompt plus the bounded
repository context. The adapter bounds that bundle itself and rejects a task
prompt larger than its budget rather than truncating requirements.

Repository context never leaves the checkout. A tracked path that is a symlink,
or that traverses a symlinked directory, is rejected before it is read, so
runner files outside the checkout cannot reach a third-party provider.

Secrets remain outside routing files. Adapter jobs should read credentials from
environment variables populated by the CI secret store, for example
`DEEPSEEK_API_KEY`, `ZAI_API_KEY`, `KIMI_API_KEY`,
`CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_API_KEY`, or `OPENAI_API_KEY`.

Every route decision records the selected provider, configured model ID,
stable alias, policy version, candidate rejection reasons, and recoverable
fallback reasons. Orchestration run records can persist `modelAlias`,
`routingPolicyVersion`, `routeReason`, and `fallbackReason` in durable
telemetry.
