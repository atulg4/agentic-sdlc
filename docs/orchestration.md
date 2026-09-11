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
- **Approval and unblocking are never agent-granted.** Once a unit is
  `approval-pending`, only a human actor can move it to `approved`; the
  auto-approval path (`planned → approved`) is open to the system policy
  engine but never to an agent actor; and resuming `blocked` (escalated)
  work requires a human decision.
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

## Transient infrastructure retry

A failed check is not evidence about the change until the platform that ran it
is ruled out. `sdlcctl classify-failure` sorts terminal evidence into
`transient_infrastructure`, `deterministic_code_or_test`,
`review_changes_requested`, `policy_or_security_block`, or `unknown`. Only
bounded platform signatures — GitHub 5xx and service-unavailable responses,
runner provisioning and startup failures, temporary API availability errors —
classify as transient. A pytest, Ruff, policy, or review failure never does,
and the first two route to bounded exact-head repair instead.

`sdlcctl decide-infra-retry` turns one classification into one idempotent
action. For a transient failure on an unchanged head it asks GitHub to re-run
only the failed jobs of the same run, so already-green evidence and the exact
head SHA both survive. Attempts use bounded exponential backoff with
deterministic per-target jitter and a budget that defaults to 3
(`FORGE_MAX_TRANSIENT_RETRIES`).

Retry state is durable and keyed by repository, pull request, run, and exact
head SHA. It lives in `<!-- forge-transient-retry … -->` markers on trusted
`github-actions[bot]` pull-request comments, the same evidence channel bounded
repair uses, so it survives workflow interruption without a second state
service. Each decision carries the completion event key (run id plus run
attempt): a duplicate delivery or an hourly watchdog sweep replaying the same
event spends no budget, while a genuinely new failed attempt does. A new head
SHA is a different key, so old-head evidence is never reused and never
inherited.

When the budget is exhausted, the decision emits a durable blocker record —
blocker class `external_infrastructure`, `userActionRequired` false, exact head
SHA, attempts against maximum, last error summary, and next action.
`build_infrastructure_blocker_panel` in `dashboard_efficiency.py` renders it
for a Control Center as `Blocked: GitHub infrastructure`, `Auto-retry: 2/3`,
plus the next retry time when one is known.

Nothing in this path can bypass a required check, weaken branch protection,
convert a red result to green, or merge. The failing run stays failing; the
only authority exercised is asking GitHub to try the same jobs again.

### Consumer integration

`.github/workflows/reusable-transient-retry.yml` is the platform driver. It
holds no AI credential at all: the classifier job reads run evidence and
trusted comments, a separate job records durable evidence before any retry, and
only the last job holds `actions: write` — using the repository's native
short-lived token — to call
`POST /repos/{owner}/{repo}/actions/runs/{run_id}/rerun-failed-jobs`. It
re-verifies the pull request head immediately before the rerun and fails closed
if a new commit arrived. See `docs/security.md` for the credential-separation
rules it follows.

`src/agentic_sdlc/templates/github/agent-transient-retry.yml` is the consumer
template. It resolves a candidate from a failed `workflow_run` or
`check_suite` completion, falls back to an hourly watchdog sweep that recovers
at most one stranded head per pass, and calls the reusable workflow through a
SHA-pinned reference with no copied decision logic.

## Lifecycle event ledger

The state machine above owns one work unit inside one repository. The
multi-project ledger in `src/agentic_sdlc/event_ledger.py` records the same
lifecycle as an append-only event stream across every registered project, and
projects it back to current state deterministically.

`FactoryState` is a superset of `WorkUnitState` — same values, plus the
post-merge release states (`deploying`, `deployment-verifying`, `live`,
`rolling-back`, `rolled-back`) that this engine deliberately does not own — and
`FACTORY_TRANSITIONS` is derived from `TRANSITIONS` above, so the two tables
cannot drift. A projected transition serializes to exactly the
`TransitionRecord` shape this module writes, and
`events_from_orchestrator_document()` migrates an existing
`sdlcctl orchestrate` state file into ledger events without losing a
transition.

Authority stays here: the ledger records what happened and never re-decides who
may approve, merge, or unblock. See [docs/project-registry.md](project-registry.md).
