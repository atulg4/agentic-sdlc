# Deterministic schedule forecast

`src/agentic_sdlc/schedule_forecast.py` turns a work graph and usage history
into an ETA, a critical path, and a typed blocking reason for every work
unit — read-only and deterministic. It never dispatches, reorders, or
changes concurrency, leases, routing, or policy; it only reports what the
graph and the ledger already imply.

A work graph is one parent work request's DAG (see `work_graph.py`), so a
multi-project forecast is one graph per project (`graphs: Mapping[str,
WorkGraph]`). Each project is forecast from only its own graph and usage
records — one project's history never influences another's quantiles or
critical path.

## Duration quantiles (`quantile`, `DurationForecast`)

`quantile(samples, q)` is the **nearest-rank** method: for `n` sorted
samples, the result is `samples[ceil(q * n) - 1]`. Dependency-free,
deterministic, and never interpolates a value that was not actually
observed.

A unit's `expectedDuration` is `{p50, p80, p95, sampleSize, basis}`, picked
from the narrowest basis that has enough real evidence:

1. **`history`** — the unit's own (project, current-stage) finished usage
   records, once there are at least `minSampleSize` (default 5) of them.
2. **`pooled-history`** — the project's stage-wide, then project-wide pool,
   once *that* clears the minimum.
3. **`declared-limit`** — no real evidence at all: the unit's declared
   `timeoutSeconds`, scaled to 50/80/95% so the range still widens instead
   of repeating one number three times.

Below the minimum, a basis is **skipped entirely** rather than used to
produce a falsely narrow range — a class with one or two samples never
reports a confident, zero-width forecast.

A unit's duration forecasts only its *current* stage (or, if it has not
started, the implementation stage as a proxy). Composing a full multi-stage
duration across implementation → verification → review → repair is a known
limitation, not attempted here.

## Critical path (`earliestStart`, `etaRange`, `onCriticalPath`)

A forward pass over the graph's topological order computes, for each
quantile (`p50`, `p80`, `p95`) independently, the earliest finish offset
from `now`: a unit starts the moment its slowest dependency finishes, and
contributes its own duration unless it is **done**, in which case it
contributes zero remaining duration (it is still the structural predecessor
on the path, just a free one). Ties — equal finish offsets — break on the
**smaller unit id**, so the reported path is reproducible rather than
depending on iteration order. The critical path is the longest path to the
latest-finishing unit, walked back through each unit's chosen predecessor.

`earliestStart` and `etaRange` are populated only when this module can
actually compute them: for a runnable unit, or one that is only
`dependency_blocked` (its ancestors' finish time is itself computable).
Every other blocking reason resolves at a time outside this module's
evidence, so those fields stay `null` rather than implying a known clock.

Nothing here is cached: every call re-reads the graph and usage records it
is given, so a unit failing, retrying, or completing between two calls is
reflected immediately in the next forecast.

## Blocking reasons (`BlockingReason`)

```
dependency_blocked | policy_blocked | human_approval_required
provider_capacity_exhausted | subscription_quota_exhausted
runner_capacity_exhausted | budget_blocked | quality_floor_unmet
conflicting_change_scope | exclusive_resource_locked
environment_unavailable | test_or_verification_failed
security_or_compliance_blocked | unknown
```

Checked in order, each rule firmer than the next — a later rule never
overrides an earlier one:

1. **`dependency_blocked`** — derived from the graph: any ancestor not yet
   done.
2. **`test_or_verification_failed`** / **`unknown`** — derived from the
   unit's own usage records: its latest attempt for its current stage failed
   and no retry remains (`attempt >= maxRetries`). A verification or review
   stage reports `test_or_verification_failed`; any other stage reports
   `unknown` — this module has evidence the unit is stuck, but not *why* in
   taxonomy terms.
3. **Declared** (`declaredBlocks`) — everything this module cannot itself
   observe: human approval, policy, provider/subscription/runner capacity,
   budget, quality floor, environment, and security/compliance each live in
   a different subsystem (the event ledger, executors, gates, policy). A
   caller that has that evidence declares it per-project,
   `{"<projectId>": {"<unitId>": "<reason>"}}` — the same "declare what the
   records cannot carry" pattern `CapacityInputs` uses in `capacity_metrics`.
   What is not declared is never guessed.
4. **`exclusive_resource_locked`** — derived from `heldResources`
   (`{"<projectId>": {"<resource>": "<holdingUnitId>"}}`): a unit's declared
   `sharedResources` held by a different unit.
5. **`conflicting_change_scope`** — derived from the graph: two units with
   an explicit, shared composition strategy and overlapping write scope or
   shared resources, with no dependency edge ordering them. A validated
   graph already orders or strategizes every such pair at dispatch time;
   this reports the still-pending case at forecast time. Deterministic
   tie-break: the lexicographically smaller unit id proceeds, the other
   waits.

`unknown` is never counted as active or queued — a unit this module cannot
explain the state of is excluded from both counts, not guessed into either.

## The report (`build_schedule_forecast`)

```
build_schedule_forecast(
    graphs,            # Mapping[projectId, WorkGraph]
    usage_records,
    now=...,           # caller-supplied clock; never read internally
    project_ids=None,  # restrict to a subset; default is every graph
    min_sample_size=5,
    declared_blocks=None,
    held_resources=None,
)
```

Per project: `units` (every field above, keyed by unit id), `etaRange`
(the project's own longest-path finish per quantile), `criticalPath`
(`{p50, p80, p95}`, each a tuple of unit ids), `activeCount`, `queuedCount`,
and `blockersByReason` (unit ids grouped by reason, `unknown` included).

Two calls over the same graphs, usage records, and `now` always produce
byte-identical JSON.

## CLI

```
sdlcctl schedule-forecast --graph <graph.json> --usage <usage.json> \
    --now <ts> [--registry <registry.json>] [--project-id <id> ...] \
    [--output <path>]
```

`graph.json` is `{"schemaVersion": 1, "projects": {"<projectId>": {"units":
{...}}}}` — one `WorkUnitSpec`-shaped object per unit, the same field names
`work_graph.WorkUnitSpec.as_dict()` produces. Only the DAG's shape is
re-checked here (cycles, missing dependencies) — not the path-policy and
ownership rules `work_graph.validate_graph` enforces at dispatch time, which
belong to the pipeline that actually schedules writes. `--usage` is a usage
ledger file in the same format `record-usage`/`capacity-report` read.

The CLI wires up only the graph and the usage ledger; it does not yet have
inputs for `declaredBlocks` or `heldResources`, so a dashboard built on it
today sees every non-derivable blocking reason as `unknown` until a richer
input is added. Fails closed on an invalid graph, a missing usage ledger, or
an unparseable `--now`.

Extracted from #46's forecasting scope; depends on #103 (project registry
and event ledger) and #104 (usage accounting) for the richer telemetry a
future iteration can read, and feeds the Control Center views in #107/#108.
