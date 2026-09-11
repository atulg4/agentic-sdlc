# Project registry and lifecycle event ledger

Forge is a general-purpose software factory, so nothing in its core may assume
one consumer repository. Two modules make that concrete:

- `src/agentic_sdlc/project_registry.py` declares **which projects exist**.
- `src/agentic_sdlc/event_ledger.py` records **what happened to them**, as one
  append-only telemetry stream shared by every project.

Dashboards, cost accounting and effectiveness analysis read this canonical
data model instead of inferring state independently from each repository.

## Project registry

A registry is a versioned JSON document. Every project has a stable ID, a
display name, one or more repositories on any supported SCM provider, optional
environments, a default branch, the lifecycle capabilities it has enabled, and
an optional grouping:

```json
{
  "schemaVersion": 1,
  "projects": [
    {
      "projectId": "marketmaestro",
      "displayName": "MarketMaestro",
      "group": "maestro",
      "defaultBranch": "main",
      "repositories": [
        {"provider": "github", "identifier": "example/marketmaestro", "role": "primary"},
        {"provider": "gitlab", "identifier": "example/group/assets", "role": "secondary"}
      ],
      "environments": [
        {"name": "production", "kind": "production", "deployTarget": "cloud-run",
         "requiresHumanApproval": true}
      ],
      "capabilities": ["planning", "implementation", "independent-review", "protected-merge"]
    }
  ]
}
```

- **Optional.** A Forge deployment that manages a single repository never needs
  a registry. `read_optional_project_registry(None)` — and a declared path that
  does not exist — returns the empty registry, so no existing command changes
  behavior when the file is absent.
- **Fail-closed when present.** `load_project_registry` rejects an unsupported
  `schemaVersion`, an unknown key at any level, an unknown capability,
  environment kind or SCM provider, a malformed project or repository
  identifier, a project without exactly one primary repository, a duplicate
  project, environment or repository, and any key whose name suggests it
  carries a credential.
- **No credentials.** The registry names repositories, environments and deploy
  targets. It never holds a token, key or connection string; deployment
  authority stays with the operator.

`ProjectRegistry` answers the cross-project questions the control plane needs:
`get`, `project_ids`, `groups`, `in_group`, and `for_repository`, which resolves
which project owns a repository without hard-coding a consumer name.

## Lifecycle event ledger

One immutable `LifecycleEvent` is appended per meaningful lifecycle moment.
Current state is never stored twice: it is *projected* from the history.

Every event records:

| Field | Purpose |
|---|---|
| `eventId`, `idempotencyKey` | identity, and the replay key |
| `workUnit` | `projectId`, `workUnitId`, repository, issue/PR, workflow and run refs, `supersededBy` |
| `stage` | the canonical factory stage (below) |
| `state` | the durable factory state |
| `activity` | the ephemeral "right now" note |
| `occurredAt`, `recordedAt` | when it happened and when Forge saw it |
| `actor` | name, kind (`human`/`agent`/`system`), agent ID, provider, model, model alias, worker/runner |
| `provenance` | source, mission ID/version, adapter/version, prompt and envelope digests, context-pack digest, commit SHA, policy version, evidence refs |
| `parentEventId`, `dependsOn` | optional parent and dependency references |

### Stages

`intake` (backlog/intake), `specification`, `planning`, `dependency-wait`,
`dispatch`, `implementation`, `deterministic-verification`,
`independent-review`, `repair`, `re-review`, `merge`, `deploy`,
`deployment-verification`, `live`, `rollback`, `blocked`, `failed`,
`cancelled`, `superseded`.

`dependency-wait` is deliberately a stage, not a state: work blocked behind an
unmerged dependency is still durably `approved`, and only its stage and
activity say what it is waiting for.

### Durable state versus ephemeral activity

`state` is the state machine's durable answer (`implementing`). `activity` is a
disposable note about this instant ("Claude editing `cli.py` on runner gha-7").
Activity never drives a transition and never survives one: any durable state
change retires the previous activity. `WorkUnitProjection.as_dict()` serializes
the two under separate `durable` and `ephemeral` keys, so a consumer cannot
confuse them by accident.

### Idempotency

Appending an event whose idempotency key was already recorded **for that work
unit** returns the stored event and changes nothing, so a replayed webhook
delivery, a re-run workflow, or a duplicated CLI invocation cannot fork
history. Reusing the same key with a different payload — or the same event ID
under a different key — fails closed rather than silently overwriting.

### Projection

`EventLedger.projection(project_id, work_unit_id)` folds one work unit's events
in a deterministic order (occurrence time, then arrival) into its current
state, stage, references, repair-cycle count, blocking reason, dependencies and
full transition history. The fold is fail-closed: the first event must open at
`intake`, transitions must be legal, and a terminal work unit cannot change.

### Isolation and cross-project queries

Events are scoped by `(projectId, workUnitId)`, so two projects may use the
same work-unit ID without ever seeing each other's history. `query`,
`projections` and `aggregate` take an explicit project scope; `aggregate` rolls
up work units, events, repair cycles and per-state/per-stage counts for one
project or for the whole factory. When a registry is attached, an event for an
unregistered project is refused.

### No credentials in telemetry

No field in the event schema can hold a deployment, merge, broker or model
credential. `secret_bearing_event_fields()` returns the event-schema field
names that could — structurally always empty, and asserted by a test in the
same spirit as the executor registry's credential-free test. The loader also
rejects any document key whose name looks credential-bearing.

## Relationship to `orchestration.py`

The ledger **extends and never contradicts** the orchestration state machine:

- Every `WorkUnitState` is a `FactoryState` with the same value
  (`factory_state()` widens one into the other).
- `FACTORY_TRANSITIONS` is derived from `orchestration.TRANSITIONS`, so the two
  tables cannot drift. The ledger adds only the post-merge release states —
  `deploying`, `deployment-verifying`, `live`, `rolling-back`, `rolled-back` —
  which the orchestrator deliberately does not own, because Forge observes a
  deployment but never holds deployment authority.
- A projected transition serializes to exactly the orchestrator's own
  `TransitionRecord` shape.
- **Authority rules stay in the orchestrator.** The ledger records what
  happened; it does not re-decide who may approve, merge or unblock. That
  authority lives in `orchestration.py` and in branch protection.

## Migration from existing state artifacts

`events_from_orchestrator_document()` turns an `Orchestrator.as_dict()`
document into ledger events: one genesis event per work unit plus one event per
recorded transition, each keeping its original `eventKey` as its idempotency
key. Projecting the result reproduces the orchestrator's final state, repair
count and complete transition history, and replaying the migration is a no-op.

Event documents are versioned independently. Version 0 is the pre-ledger
orchestration vocabulary — a work unit named `unitId` and its key named
`eventKey`, with no `schemaVersion` — and is upgraded on read by
`migrate_event_document()`. A future version fails closed rather than being
read with unknown semantics.

## CLI

```sh
# Validate a registry (fail-closed; writes the normalized document).
sdlcctl validate-registry --registry projects.json --output registry.json

# Append one lifecycle event; replaying the same delivery changes nothing.
sdlcctl record-event --ledger ledger.json --event event.json \
  --registry projects.json --projection-output unit.json

# Project the ledger: whole factory, one project, or one work unit.
sdlcctl project-state --ledger ledger.json --output state.json
sdlcctl project-state --ledger ledger.json --project-id marketmaestro
sdlcctl project-state --ledger ledger.json --project-id marketmaestro \
  --unit "github:example/marketmaestro:issue:42"
```

`--registry` is optional everywhere it appears. Without `--unit`,
`project-state` emits both the projections and the aggregate for the requested
scope; with `--unit` it emits that one projection. Every rejection exits 2 with
the reason on stderr, like the other `sdlcctl` subcommands.
