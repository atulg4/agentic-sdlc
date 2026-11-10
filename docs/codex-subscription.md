# Codex subscription workers

Forge can run the official Codex CLI on a trusted remote machine using that
machine's saved ChatGPT login. This operator-driven adapter is the first
execution increment for #39. The existing scheduler, issue leases, verification
and publication gates remain the authority for assigning and accepting work.

## Execution surfaces

| Registry execution type | Reported execution surface | Dispatch |
| --- | --- | --- |
| `subscription-runner` | `authenticated-remote-client` | Operator invokes `codex-subscription-run` on a signed-in host |
| `subscription-cloud` | `provider-cloud` | Delegation through a supported Codex cloud surface |
| `direct-api` or `cloud-platform` | `api` | Existing provider adapter |
| `self-hosted` | `authenticated-remote-client` | Existing local adapter |

A Codex subscription profile requires `provider: codex`, `authMode: oauth`
and an explicit account-supported model ID. The model alias can remain `codex`.
Credentials belong to Codex's authentication store, outside the executor
registry and run reports.

Route candidates and the selected route report `executionSurface`. Existing
profile serialization stays stable so stored profile digests remain usable.
`allowApiBillingFallback` defaults to `false`. When a Codex subscription is
registered for the task and repository, routing rejects direct-API OpenAI and
Codex candidates unless this flag is explicitly enabled. Quality and capability
requirements apply to every candidate, including fallbacks.

The existing Actions Codex step uses API authentication. Consequently
`route-executor` rejects a selected Codex subscription by default. A trusted
operator using the remote client path supplies `--codex-subscription-client`.
This flag selects the caller contract and does not grant issue or publication
authority. Subscription cloud profiles remain delegated capacity.

## Prepare the host

Install a pinned Forge revision and the official Codex CLI. Authenticate with
`codex login`, or use `codex login --device-auth` on a headless machine when that
flow is enabled for the account. Check `codex login status` on the host.
Complete authentication directly with OpenAI.

Use a private, trusted development host with isolated working directories. Keep
repository authentication, account authentication and publication permissions
under the existing Forge separation. For public or untrusted CI jobs, use the
provider's recommended API authentication workflow instead of copying a
personal account's login into the job.

## Run an approved task

Fetch the repository and validate the current work request, dependencies,
permissions and issue lease through the existing Forge commands first. Select
the executor using the project's routing policy. The runner is a low-level
operator adapter and does not independently acquire leases, schedule work,
enforce the full Harness Manifest proposal or publish a pull request.

Preview the invocation using an existing validated registry and a unique run
directory outside the source checkout:

```sh
sdlcctl codex-subscription-run \
  --executors /private/forge/executors.json \
  --executor-id codex-personal \
  --worktree /private/forge/worktrees/task \
  --prompt-file /private/forge/tasks/task.txt \
  --run-dir /private/forge/runs/task-attempt-1 \
  --timeout-seconds 900
```

The default is a dry run. Add `--execute` to start the account-authenticated
client. The default sandbox is `read-only`; an approved implementation task
uses `--sandbox workspace-write`. The selected registry profile supplies the
model. Each attempt uses a new run directory, so an uncertain prior run cannot
be silently replayed.

The runner performs an account-login preflight, supplies the prompt over stdin,
and consumes the CLI's JSON events. A successful CLI process exit must also
have a completed turn. Failed turns, missing completion evidence and timeouts
produce failed results. The CLI's process group is cleaned up after each turn,
including timeouts and lingering child processes.

## Status and accounting

The run directory holds `status.json`. It reports the requested model,
observed model when exposed, process heartbeat, outcome, failure category and
observed usage. Raw prompts and provider error bodies are excluded. A heartbeat
records a live local process, which may be waiting on the provider. An old
heartbeat must appear stale in any consumer. A completed agent turn still
requires the repository's tests and independent review before acceptance.

A successful final agent message is saved separately in private `result.txt`
(mode `0600`); `resultAvailable` indicates its presence. That file can contain
source or task content and must not be copied into public telemetry. Input
usage separates uncached tokens from cache reads when both are known;
`providerInputTokens` preserves the provider's inclusive input total.

The CLI uses the subscription account. Remaining allowance and billed dollars
stay unknown unless the provider supplies authoritative observations. The
existing usage ledger can receive the run evidence through its normal
`record-usage` contract. Automatic ledger collection, a persistent supervisor,
shared-account concurrency reservations and dashboard delivery require a
separate integration step.

For Claude, the existing cloud-helper work in PR #179 supplies an
operator-triggered dispatch journal. A returned Claude session URL establishes
dispatch. Its live state needs a supported session observation before it can
be presented as running or completed. Map each routine to its repository and
role before sharing a scheduler across projects.

## Provider references

- [Codex authentication](https://learn.chatgpt.com/docs/auth)
- [Codex non-interactive execution](https://learn.chatgpt.com/docs/non-interactive-mode)
- [Codex usage and pricing](https://learn.chatgpt.com/docs/pricing)

These references describe provider behavior. Executor availability and model
access must be checked on the actual host and account.
