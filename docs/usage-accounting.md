# Usage, cost and capacity accounting

`src/agentic_sdlc/usage_ledger.py` accounts for every AI or agent activity
Forge dispatches: which project, work unit, issue and change request it served,
at which lifecycle stage, in which run and attempt, under which mission, by
which agent, model, provider and worker, and what it was expected to consume
versus what it actually consumed. It builds on the multi-project registry and
the lifecycle event ledger (`docs/project-registry.md`): the same `WorkUnitRef`
and `EventActor` vocabulary, the same project scoping, the same fail-closed
loaders, and an optional `lifecycleEventId` that ties a usage record to the
event it accounts for.

Three rules keep the numbers honest.

- **Unknown is unknown.** A provider or runner that does not expose a token
  count, a runtime or a price yields `null` with an explicit status. Aggregates
  sum known values only and say how many records were unknown. Nothing is ever
  imputed.
- **Subscriptions are not pay-as-you-go.** Claude Max/Code and Codex
  subscription usage is recorded against the plan's capacity unit. A
  pay-as-you-go *equivalent* is computed only when reference rates exist, and it
  is labeled `subscription`, never `billed`.
- **No credentials, no prompts.** No field can hold a secret or a prompt, every
  document key that looks like one is rejected, and free text is capped at 256
  characters so a transcript cannot be smuggled into an accounting record.

## The usage record

One `UsageRecord` per run attempt, identified by a stable `usageId`
(`usage_id_for(project, work_unit, run_id, attempt)`):

| Section | What it holds |
|---|---|
| identity | `workUnit` (project, work unit, repository, issue, change request), `stage`, `taskClass`, `runId`, `attempt`, `missionId`/`missionVersion`, `actor` (agent, provider, model, alias, worker), `lifecycleEventId`, `complexityClass`, `reviewRound`, `repairCycle`, `recordedAt` |
| `estimate` | pre-dispatch `tokens` (input, output, cacheRead, cacheWrite), `runtimeSeconds`, `monetary`, the `estimatorVersion`, a `basis` sentence and the `calibration` coefficients that were applied |
| `actual` | observed `tokens`, `runtimeSeconds`, `waitSeconds`, `monetary`, `result` (completed, failed, cancelled, abandoned, inconclusive, unknown), the `source` of the observation and `queuedAt`/`startedAt`/`finishedAt` |
| `infrastructure` | `runnerClass`, `runnerSeconds`, `ciMinutes`, `costUsd`, `storageBytes`, `networkBytes` — reported separately from AI usage and never combined with it |

Each section is optional and **immutable once written**. Appending a record
whose `usageId` already exists is a no-op when the payload is identical, fills a
section that was still absent (an actual arriving after the estimate), and
fails closed on any other difference. A replayed workflow or duplicated CLI call
therefore cannot double-count, and history is never rewritten.

Runtime and wait are derived from the timestamps when they were not observed
directly (`finishedAt - startedAt`, `startedAt - queuedAt`); an observed value
is never overwritten, and timestamps that run backwards are rejected.

### Token counts

Every component is `int | null`. `status` is `known` when all four are known,
`partial` when some are, `unknown` when none is; `total` exists only for a
`known` count. A provider that does not cache reports `0`, not `null`.

### Pricing snapshots and monetary equivalents

A `PricingSnapshot` is the versioned price list a monetary figure was computed
from. It is embedded in the record, so every figure is reproducible without
looking anything up later.

```json
{
  "schemaVersion": 1,
  "pricing": [
    {"pricingId": "anthropic-payg-2026-09", "provider": "anthropic",
     "model": "claude-opus-5", "billingMode": "payg", "version": "2026-09-01",
     "usdPerMillion": {"input": 15, "output": 75, "cacheRead": 1.5, "cacheWrite": 18.75}},
    {"pricingId": "claude-max-20x", "provider": "anthropic",
     "model": "claude-opus-5", "billingMode": "subscription", "version": "2026-09",
     "usdPerMillion": {"input": 15, "output": 75},
     "plan": {"name": "Claude Max 20x", "capacityUnit": "five-hour-window-share",
              "monthlyUsd": 200}},
    {"pricingId": "codex-plan", "provider": "codex", "model": "gpt-5-codex",
     "billingMode": "subscription", "version": "2026-09",
     "plan": {"name": "ChatGPT Pro", "capacityUnit": "requests",
              "capacityUnitsPerMillionTokens": 10}}
  ]
}
```

`monetary_equivalent(tokens, pricing, plan_capacity_units=...)` yields a
`MonetaryEquivalent` whose `status` says how to read it:

| `status` | `billedUsd` | `paygEquivalentUsd` | `planCapacityUnits` |
|---|---|---|---|
| `payg` | the priced tokens at the snapshot rates | same figure | never |
| `subscription` | always `null` | reference figure when the snapshot carries reference rates and the priced tokens are known, else `null` | observed value, else estimated from `capacityUnitsPerMillionTokens` when the total is known, else `null` |
| `unknown` | `null` | `null` | `null` |

A pay-as-you-go figure requires every *priced* component to be known; a rate
left `null` (for example cache rates on a provider without caching) is simply
not priced. The loader refuses a `subscription` figure with a `billedUsd` and an
`unknown` figure with any monetary value, so a subscription run can never be
rewritten as billed dollars downstream.

### Estimate error

`record.estimate_error` compares each dimension (`input`, `output`,
`cacheRead`, `cacheWrite`, `runtimeSeconds`, plus `totalTokens` and
`paygEquivalentUsd`) only where both the estimate and the actual are known,
reporting the absolute error and the percentage of the estimate. A zero
estimate has no percentage. When nothing is comparable the error is
`unavailable` with the reason.

## Aggregation

`UsageLedger.aggregate(group_by=...)` rolls records up by any combination of
`project`, `workUnit`, `issue`, `changeRequest`, `run`, `stage`, `taskClass`,
`mission`, `agent`, `provider`, `model`, `modelAlias`, `worker`, `reviewRound`,
`repairCycle`, `billingMode`, `result`, `day`, `week` (ISO) and `month`, scoped
to one or more projects and a time window. Every bucket reports:

- `records`, distinct `runs`, `retryAttempts` (attempt > 1) and `byResult`;
- `estimated` and `actual` tokens per component and in total, runtime, wait,
  `paygEquivalentUsd`, `billedUsd`, `planCapacityUnits` per capacity unit,
  `runtimeSecondsByResult` and the `monetaryStatus` mix;
- `estimateError.meanAbsolutePercentage` per dimension with its sample count;
- `infrastructure` cost, runner seconds and CI minutes.

Every sum has the shape `{"value", "knownRecords", "unknownRecords"}`; `value`
is `null` when nothing was known. A cancelled attempt that spent runtime but
exposed no tokens keeps its runtime under `cancelled` and counts as unknown for
tokens, so retries and abandoned work stay visible in the time accounting.
Infrastructure cost is never added to the AI cost equivalent; the report's
`limitations` say so explicitly.

## Estimator calibration

`EstimatorCalibration` learns correction coefficients from records that carry
both an estimate and a completed, failed, abandoned or inconclusive actual
(cancelled and unobserved runs teach nothing). For each dimension it keeps a
running mean of `actual / estimate`, clamped to `[0.05, 20]`, at every key on
the path

```
model;stage;taskClass;complexity → model;stage;taskClass → model;stage → stage → *
```

`estimate(...)` multiplies a caller-supplied baseline by the most specific
coefficient with at least `minSamples` observations, records which key and
sample count corrected each dimension, and falls back to the uncorrected
baseline (`cold-start`) when none qualifies. An unknown baseline stays
unknown; a zero baseline is never calibrated. Observed `usageId`s are
remembered, so re-running calibration over the same ledger changes nothing.

The calibration document is versioned and round-trips through
`as_dict`/`from_dict` fail-closed.

## Non-goals

No automatic purchasing or plan changes; no fabricated per-run dollars for
subscription providers; no user interface. The ledger is evidence for the
Control Center, the effectiveness metrics (`docs/efficacy.md`) and the factory
learning loop, and holds no merge, deploy or credential authority.

## CLI

```sh
# Append or complete one usage record; replaying the same record changes nothing.
sdlcctl record-usage --ledger usage.json --record record.json \
  --registry projects.json --error-output error.json

# Roll usage up by project (default), or by any documented dimensions.
sdlcctl usage-report --ledger usage.json --output by-project.json
sdlcctl usage-report --ledger usage.json --group-by changeRequest --group-by stage \
  --project-id marketmaestro --since 2026-09-01T00:00:00Z --output by-pr.json

# Learn coefficients from every record with an estimate and an actual.
sdlcctl calibrate-usage --ledger usage.json --calibration calibration.json --min-samples 3

# Produce a pre-dispatch estimate to embed in the next record.
sdlcctl estimate-usage --calibration calibration.json \
  --pricing pricing.json --pricing-id claude-max-20x \
  --model claude-opus-5 --stage implementation --task-class implementation \
  --complexity medium --baseline-input-tokens 40000 --baseline-output-tokens 8000 \
  --baseline-cache-read-tokens 0 --baseline-cache-write-tokens 0 \
  --baseline-runtime-seconds 900 --output estimate.json
```

`--registry` is optional everywhere it appears. A missing `--calibration` file
means cold start. Every rejection exits 2 with the reason on stderr, like the
other `sdlcctl` subcommands.
