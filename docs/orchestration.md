# Autonomous orchestration state machine

`src/agentic_sdlc/orchestration.py` provides the provider-neutral engine that
owns the full autonomous lifecycle of a work unit:

```
intake → triaged → specified → planned → approval-pending → approved
      → dispatched → implementing → verifying → reviewing
      → repair-needed → repairing → re-reviewing
      → ready-for-human-merge → merged | blocked | failed
```

with `cancelled` and `superseded` reachable from every non-terminal state.
`merged`, `failed`, `cancelled`, and `superseded` are terminal and immutable.

## Guarantees

- **Explicit, validated transitions.** Only the versioned transition table is
  legal; everything else fails closed (`OrchestrationError`).
- **Idempotent events.** Every mutation carries an `event_key` (for webhooks,
  the delivery GUID). Replays are no-ops: duplicate events never duplicate
  units, runs, or PRs.
- **Serialized concurrency groups.** One work unit — or several units sharing
  a `concurrency_group` — can never hold two active implementation/repair
  runs at once.
- **Bounded repair with escalation.** Review findings route to
  `repair-needed`; each repair increments a counter capped by
  `max_repair_cycles`. Findings after the last permitted cycle transition to
  `blocked` with an `escalated:` reason instead of looping.
- **Fresh independent review after every repair.** A review run is stamped
  with the repair cycle it observed; approving from a pre-repair review is
  rejected. Reviewer selection goes through the mission registry, so an
  implementer can never review its own work, even as a fallback.
- **Human merge is mandatory.** `merged` requires a human actor; the
  constructor refuses `human_merge_required=False`; manual overrides cannot
  target `merged`. There are no deployment states at all.
- **Dependency blocking, cancellation, supersession, manual override.**
  Dispatch is refused while a dependency is unmerged; superseded/cancelled
  units can never publish; human overrides are recorded with a mandatory
  reason.
- **Pinned, reproducible run records.** Every agent run stores mission
  ID/version, agent/adapter/model identity, prompt and envelope digests,
  context-pack digest, input refs, commit SHA, result, and timestamps.
  `as_dict`/`from_dict` round-trip the entire engine, so every transition is
  reproducible from durable artifacts rather than hidden agent memory.
- **One updateable status document.** `status_document()` renders a single
  marked (`<!-- agentic-sdlc:status -->`) body that callers upsert instead of
  posting noisy repeated comments.

## CLI

`sdlcctl orchestrate` drives the engine from CI against a durable JSON state
file:

```sh
sdlcctl orchestrate --action create --state state.json --unit 42 \
  --event-key "$DELIVERY_GUID" --timestamp "$NOW"
sdlcctl orchestrate --action transition --state state.json --unit 42 \
  --to triaged --actor "$ACTOR" --actor-kind human \
  --event-key "$DELIVERY_GUID" --timestamp "$NOW" --status-output status.md
```

## Consumer integration

`.github/workflows/reusable-orchestrate.yml` is the thin platform driver: it
restores the durable state artifact, applies exactly one action via
`sdlcctl orchestrate`, republishes the state, and upserts the single status
comment. It holds no AI credentials, no `contents: write`, and no merge
authority. Follow-on events that must trigger workflows use the
least-privilege publisher GitHub App token (see
`docs/github-publisher-app.md`), never recursive `GITHUB_TOKEN` behavior.

`examples/marketmaestro/.github/workflows/agent-orchestrate.yml` shows
MarketMaestro consuming the capability through a SHA-pinned call with zero
copied orchestration logic.
