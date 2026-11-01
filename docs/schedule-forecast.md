# Schedule forecast

`sdlcctl schedule-forecast` estimates when the remaining work in a work graph
will finish, which units sit on its critical path, and why each unit that
cannot run is stuck. It complements the capacity report
([`capacity-metrics.md`](capacity-metrics.md)): capacity metrics explain where
time *went*; the forecast estimates where it is *going*.

The forecast is observational only. It never dispatches, reorders or leases
work, and never changes concurrency, routing or policy. It never reads the
wall clock: `--now` is an input, so identical inputs give byte-identical
output.

```text
sdlcctl schedule-forecast --graph forecast.json --usage usage.json \
  --policy agentic-sdlc.toml --now 2026-11-01T12:00:00Z \
  [--min-samples 5] [--registry projects.json] [--project-id alpha] [--output out.json]
```

## Inputs

**Forecast document** (`--graph`). One work graph per project. Each unit
carries the `WorkUnitSpec` fields (`unitId`, `missionId`, `missionVersion`,
`outputArtifact`, `dependsOn`, `writePaths`, `timeoutSeconds`, …) plus what is
observed about it now:

```json
{
  "schemaVersion": 1,
  "graphs": [
    {
      "projectId": "alpha",
      "units": [
        {"unitId": "api", "missionId": "implementation-worker", "missionVersion": "1.0.0",
         "outputArtifact": "patch", "writePaths": ["src/api/**"], "status": "running",
         "startedAt": "2026-11-01T11:40:00Z"},
        {"unitId": "docs", "missionId": "docs-worker", "missionVersion": "1.0.0",
         "outputArtifact": "patch", "dependsOn": ["api"], "writePaths": ["docs/**"],
         "signals": [{"source": "orchestration-state", "value": "approval-pending"}]}
      ]
    }
  ]
}
```

- `status` is one of `pending` (default), `running`, `completed`, `failed` or
  `blocked`, the `GraphExecutor` vocabulary.
- `startedAt` places a running unit in time. Without it, the unit is treated
  as starting at `--now`.
- `signals` are raw observations from the modules that produce them. See
  [Blocking reasons](#blocking-reasons).

Every graph is checked with `validate_graph` against `--policy` before it is
forecast, so the command never forecasts a graph the orchestrator would
refuse. Unknown keys, bad statuses, malformed timestamps, cycles, missing
dependencies, forbidden write paths and unregistered projects (when
`--registry` is given) all exit with code 2.

**Usage ledger** (`--usage`). Finished run attempts supply observed runtimes.
An attempt counts when it has an `actual` with a known `runtimeSeconds` and a
`finishedAt`. Its result does not matter, because failed attempts took time
too.

## Durations

A unit's duration is estimated from the runtimes of its **(project, mission
id)** class. The mission id is the one key a work-graph unit and a usage
record share.

Quantiles use the **nearest-rank** method: the qth quantile of n sorted
samples is the ⌈q·n⌉-th smallest (1-based). The forecast reports P50, P80 and
P95.

Low-sample classes widen rather than narrow. With fewer than `--min-samples`
(default 5) observations, the estimate falls back, and `basis` says which
source was used:

| `basis` | Used when | Source |
|---|---|---|
| `history` | the mission has at least `--min-samples` runtimes | that mission's runtimes in the project |
| `pooled-history` | the mission is thin, but the project is not | every runtime in the project |
| `declared-limit` | the project is thin too | the unit's `timeoutSeconds`, for P50, P80 and P95 alike |

`sampleSize` is the number of samples behind the basis (0 for
`declared-limit`). History never crosses projects.

## Schedule and critical path

Each quantile is scheduled separately:

- a `completed` unit contributes zero remaining duration;
- a `running` unit finishes at `startedAt + duration`, but never before `--now`;
- any other unit starts at the later of `--now` and the latest finish among its
  dependencies, and finishes one duration later.

A `failed` unit must run again, so it keeps its full duration.

A unit's `etaRange` holds its P50, P80 and P95 finish times; a project's
`etaRange` is the latest of each. Path quantiles are sums of per-unit
quantiles, not a convolution of distributions. A path's P80 is therefore a
conservative upper estimate, not a true 80th percentile.

The critical path is the chain of dependencies that sets the latest P50
finish (`criticalPath`). The same chain is also computed for P80
(`criticalPathP80`), which can differ when a variable mission dominates the
tail. Ties break by unit id: the lowest id wins, both for which dependency set
a start and for which unit finishes last. Every call re-reads the inputs, so
completions, failures, retries and new history change the path at once.

## Blocking reasons

`blockingReason` is the highest-precedence reason, and `blockingReasons` lists
all of them. Precedence is the declaration order below. A unit is `runnable`
only when it is `pending` and has no reason.

| Reason | Comes from |
|---|---|
| `dependency_blocked` | a dependency not yet `completed`; status `blocked`; signal `orchestration-error` containing "is dependency-blocked by"; signal `capacity-wait` = `dependency` |
| `policy_blocked` | (no producer yet) |
| `human_approval_required` | signal `orchestration-state` = `approval-pending` |
| `provider_capacity_exhausted` | signal `executor-runtime` = `capacity-exhausted` |
| `subscription_quota_exhausted` | signal `executor-runtime` = `quota-exhausted` |
| `runner_capacity_exhausted` | signal `capacity-wait` = `resource` (capacity metrics assign it only when the pool was saturated) |
| `budget_blocked` | (no producer yet) |
| `quality_floor_unmet` | (no producer yet) |
| `conflicting_change_scope` | signal `work-graph-error` containing "overlapping write scopes" |
| `exclusive_resource_locked` | signal `lease-error` containing "exclusively leased" |
| `environment_unavailable` | (no producer yet) |
| `test_or_verification_failed` | signal `verification` = `failed` |
| `security_or_compliance_blocked` | (no producer yet) |
| `unknown` | any unrecognized signal, and a `failed` unit with no classified signal |

`unknown` is never treated as runnable. Each project also reports its
`blockers` grouped by reason, along with `active` (running), `queued` (neither
running nor completed) and `completed` counts.
