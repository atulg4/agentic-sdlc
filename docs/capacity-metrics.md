# Parallelism, queueing and capacity metrics

`src/agentic_sdlc/capacity_metrics.py` reads the usage ledger
(`docs/usage-accounting.md`) as a factory instrument. The ledger already
records, per run attempt, when the work was queued, started and finished, and
on which worker, model and provider. This module turns that history into the
questions an operator actually asks: how much ran at once, where time went
while nothing ran, which runners and models were saturated, and how much of
the busy time was useful rather than rework.

Three rules carry over from the ledger it reads.

- **Unknown is never a percentage.** A utilization figure appears only when the
  capacity it divides by was declared or observed. Plan capacity a provider
  does not expose is reported as unknown beside the usage proxies that *are*
  observable, never as an invented fraction.
- **Every number shows its working.** Each metric carries the seconds and
  counts it came from, so a reader can recompute it by hand.
- **Findings explain, they never act.** Bottlenecks and recommendations are
  evidence with an explanation attached. Nothing here changes routing,
  concurrency, policy or spend.

## The observation window

The window is supplied by the caller, never inferred from the records. A quiet
hour is then visible as idle capacity instead of vanishing from the
denominator. Runs are clipped to the window, runs entirely outside it are
excluded, and both are counted in `coverage` so nothing disappears silently.

A run with no recorded start and finish is counted as unobserved rather than
assumed to have run. A run that started the instant it was queued waited zero
seconds, which is an observation and is counted as one.

## Concurrency and queueing

`concurrency_timeline` sweeps the busy intervals into contiguous segments of
constant concurrency covering the whole window, idle stretches included, so the
seconds add back up to the window. A run ending exactly where another begins is
a handover, not a peak.

| Figure | Meaning |
|---|---|
| `peak` | the most runs active at any instant |
| `mean` | busy seconds divided by window seconds |
| `busySeconds` | total run time, counting overlap once per run |
| `occupiedSeconds` | seconds with at least one run active |
| `idleSeconds` | window seconds with nothing running |

The same shape is reported for the queue, built from the interval between
queued and started, giving peak and mean queue depth.

## Why work waited

A usage record says a run waited; it cannot say what it waited for. Causes are
resolved in this order, and anything unresolved stays unclassified rather than
being attributed to the nearest plausible cause:

1. **Dependency**, when the caller supplies explicit evidence. The ledger's
   `dependency-wait` stage names the blocking unit, so that mapping is the
   input, keyed by usage id.
2. **Resource**, derived rather than declared: if the pool that eventually ran
   the work was at all its slots for at least half the wait, the wait was for a
   slot.
3. **Unclassified**, with its seconds reported plainly.

`wait.handoff` measures a different loss: the gap between one stage finishing
and the next being queued for the same work unit. That time belongs to no run
at all, and is usually a trigger or polling delay rather than a shortage.

## Runners, models and plan capacity

Pools are declared, because slots are what make utilization computable:

```json
{
  "schemaVersion": 1,
  "pools": [
    {"poolId": "hosted", "slots": 4, "workers": ["gha-1", "gha-2"], "kind": "github-hosted"},
    {"poolId": "mm", "slots": 1, "workers": ["mm-runner-1"], "kind": "self-hosted"}
  ],
  "planCapacity": [
    {"provider": "anthropic", "plan": "Claude Max", "capacityUnit": "five-hour-window-share",
     "observedAt": "2026-09-22T00:30:00Z", "unitsTotal": 100, "unitsUsed": 25}
  ],
  "dependencyBlocked": {"usage-abc": "alpha:unit-0"},
  "concurrencyGroups": {"usage-abc": "migrations"},
  "providerLimits": {"anthropic": 4}
}
```

A pool reports busy seconds against `slots × window`, plus the seconds it spent
with every slot full. A declaration that contradicts the evidence fails closed:
if more runs were active at once than the pool declares slots, the report is
refused rather than showing an impossible utilization. Workers belonging to no
declared pool are listed, and their pool utilization reads `unknown`.

Models and providers get the same concurrency treatment plus a stage mix, so a
model saturated by reviews looks different from one saturated by builds.

Plan capacity is reported in one of three states, and only the first is a
percentage:

| State | When | `usedFraction` |
|---|---|---|
| `observed` | the provider reported both total and used | the real fraction |
| `partial` | only one of the two was reported | `null` |
| `unknown` | no observation was supplied | `null` |

Every state carries `usageProxies`: peak concurrent runs, busy seconds, run
count, the plan capacity units the usage ledger actually recorded, and how many
runs reported none. Those are observations, not estimates of the plan.

## Useful work versus busy time

Busy time is not the same as useful time. The report gives both, with the
formula exposed:

```
usefulWorkRatio = busy seconds of runs that completed on their first attempt
                  / all busy seconds
```

Failed, cancelled, abandoned and inconclusive runs stay in the denominator, and
so do retries. A retry that succeeded is still rework, and is reported that way,
alongside a full breakdown by result and by first attempt versus retry.

## Bottlenecks and recommendations

Detectors name what the numbers show and attach the seconds that show it. Each
finding carries a kind, a scope, the time it cost, an explanation and its
evidence; findings are ordered by cost.

- **runner-shortage**: a pool at all slots while its own work sat queued.
- **serialized-concurrency-group**: a group that never exceeded one run while
  its own work waited.
- **model-capacity-throttling**: a provider at its declared concurrent-run
  ceiling with work queued, or a plan reported fully consumed.
- **dependency-fan-in**: three or more runs waiting on the same blocker.
- **repeated-review-cycles**: repairs and re-reviews, and what they cost.
- **handoff-delay**: time between stages rather than inside one.

Each finding produces one advisory recommendation carrying the same evidence.
They are flagged `advisory` and change nothing: this module holds no routing,
concurrency, merge or spending authority.

## Relationship to the other metric modules

`factory_metrics.py` scores throughput and first-pass rates from the efficacy
ledger's `RunOutcome` records. This module answers a different question, from
the usage ledger's per-run timing, and neither replaces the other.
`dashboard_efficiency.py` remains the freshness wrapper a Control Center uses to
mark any of these figures stale or unknown.

## CLI

```sh
sdlcctl capacity-report --usage usage.json \
  --window-start 2026-09-22T00:00:00Z --window-end 2026-09-22T01:00:00Z \
  --inputs capacity-inputs.json --output capacity.json

# Scope the same window to one or more projects.
sdlcctl capacity-report --usage usage.json \
  --window-start 2026-09-22T00:00:00Z --window-end 2026-09-22T01:00:00Z \
  --project-id marketmaestro
```

`--inputs` and `--registry` are optional; without declarations the report still
measures concurrency, queueing, waits and useful work, and reports every
utilization it cannot ground as unknown. Every rejection exits 2 with the reason
on stderr, like the other `sdlcctl` subcommands.
