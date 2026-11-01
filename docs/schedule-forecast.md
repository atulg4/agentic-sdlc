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
2. **`pooled-history`** — the project's stage-wide pool, once *that* clears
   the minimum.
3. **`pooled-history`** again, this time the project's full cross-stage
   pool, once tier 2 still falls short but this broader one clears the
   minimum.
4. **`declared-limit`** — no real evidence at all, at any tier: the unit's
   declared `timeoutSeconds`, scaled to 50/80/95% so the range still widens
   instead of repeating one number three times.

Each tier is tried in order and skipped entirely — not used to produce a
falsely narrow range — until one clears `minSampleSize`; a unit's own 1-2
samples never get reported as `history` just because pooling further
up would hit the pooled-history label anyway.

A unit's duration forecasts only its *current* stage (or, if it has not
started, the implementation stage as a proxy). Composing a full multi-stage
duration across implementation → verification → review → repair is a known
limitation, not attempted here.

## Critical path (`earliestStart`, `etaRange`, `onCriticalPath`)

A forward pass over the graph's topological order computes, for each
quantile (`p50`, `p80`, `p95`) independently, the start and finish offset
from `now` for every unit:

- **Done** units contribute zero remaining duration — still the structural
  predecessor on the path, just a free one.
- **Active** units (a run already dispatched, no outcome recorded yet)
  start at their real, already-past observed `startedAt`, and finish at
  that start plus the *full* duration forecast — which, credited against
  time already spent, is the same as saying their remaining duration is
  `max(0, duration - elapsed)`. A refresh an hour later does not restart
  the clock at "now" and add a fresh full duration.
- Every other unit starts the moment its slowest dependency finishes
  (`0` if it has none) and contributes its own forecast duration in full.

Ties — equal finish offsets — break on the **smaller unit id**, so the
reported path is reproducible rather than depending on iteration order. The
critical path is the longest path to the latest-finishing unit, walked back
through each unit's chosen predecessor.

`earliestStart` and `etaRange` are populated only when this module can
actually compute them: for a runnable unit, or one that is only
`dependency_blocked` with every ancestor's own resolution time *also*
computable. A unit blocked for any other reason has an unknown resolution
time, and that unknown propagates forward through every dependent — a unit
only `dependency_blocked` on an ancestor stuck on, say, human approval does
not get a concrete ETA computed as though the approval lands immediately.
The project's own `etaRange` is likewise `null` for any quantile whose
critical path runs through an indeterminate unit.

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
   unit's own usage records, comparing *parsed instants* (not raw
   `recordedAt` text — differing RFC 3339 offsets would otherwise sort out
   of true chronological order): its latest attempt for its current stage
   failed and no retry remains. One initial attempt plus `maxRetries`
   retries are allowed — exhaustion is `attempt > maxRetries`, matching
   `MissionLedger`'s own "`attempts >= max_retries + 1`" rule in
   `missions.py`. A verification or review stage reports
   `test_or_verification_failed`; any other stage reports `unknown` — this
   module has evidence the unit is stuck, but not *why* in taxonomy terms.
3. **Declared** (`declaredBlocks`) — everything this module cannot itself
   observe: human approval, policy, provider/subscription/runner capacity,
   budget, quality floor, environment, security/compliance, and real
   fan-in/composition conflicts each live in a different subsystem (the
   event ledger, executors, gates, policy, the composition step itself). A
   caller that has that evidence declares it per-project,
   `{"<projectId>": {"<unitId>": "<reason>"}}` — the same "declare what the
   records cannot carry" pattern `CapacityInputs` uses in `capacity_metrics`.
   What is not declared is never guessed.
4. **`exclusive_resource_locked`** — derived from `heldResources`
   (`{"<projectId>": {"<resource>": "<holdingUnitId>"}}`): a unit's declared
   `ownedPaths` or `sharedResources` held by a different unit — the same
   resources `work_graph.GraphExecutor.start` actually leases.

An explicit, shared composition strategy over overlapping write scope is
**not** treated as mutual exclusion: `GraphExecutor.start` leases only
`ownedPaths` and `sharedResources`, never `writePaths`, so two units a
validated graph lets share a strategy are free to run concurrently and
compose afterward. This module invents no serialization a composition
strategy does not itself impose — a real fan-in conflict is reported only
through `declaredBlocks`.

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
