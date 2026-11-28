# Outcome effectiveness, learning ledger and value scoring

`src/agentic_sdlc/effectiveness.py` answers the question the rest of the
factory instruments cannot: did a real engineering outcome arrive, and what did
it take to get there?

The layers below it supply the facts:

| Layer | Module | Supplies |
|---|---|---|
| Lifecycle | `event_ledger.py` | project, work unit, issue/PR, stage, state |
| Usage | `usage_ledger.py` | tokens, runtime, model, worker, cost, estimate-vs-actual |
| Capacity | `capacity_metrics.py` | concurrency, queueing, utilization |
| **Effectiveness** | **`effectiveness.py`** | **delivered outcomes, learnings, value per unit of effort** |

See [docs/project-registry.md](project-registry.md),
[docs/usage-accounting.md](usage-accounting.md) and
[docs/capacity-metrics.md](capacity-metrics.md) for the layers it reads.

## The four honesty rules

- **Value is never inferred from effort.** The numerator of every value metric
  is a count of *accepted outcomes*. Tokens, runtime, spend, lines of code,
  commits and change-request counts appear only as denominators or context, so
  no amount of consumption can raise the measured value of the factory. A test
  asserts this structurally: every `per…` metric's `numerator` equals the
  accepted-outcome count.
- **Unknown is unknown.** A rate whose denominator is zero, or whose inputs
  were not observed on *every* record in the sample, reads as
  `status: "unknown"` with the reason. A partial denominator would divide the
  same outcomes by less effort and overstate the factory — the one direction an
  honest metric must never fail in.
- **Subscriptions are not dollars.** Subscription usage is scored as a labeled
  pay-as-you-go *equivalent*, separately from billed spend, exactly as the
  usage ledger records it.
- **Learnings advise, they never act.** Every learning carries
  `advisory: true`, and the loader refuses a record claiming otherwise.
  Adopting a recommendation is a separate, human-approved policy change.

## Outcome records

One `OutcomeRecord` links what a work unit was *meant* to deliver to what it
actually delivered:

```json
{
  "schemaVersion": 1,
  "outcomeId": "outcome-9f2c1d0b4a7e5c83",
  "workUnit": {
    "projectId": "marketmaestro",
    "workUnitId": "github:example/marketmaestro:issue:42",
    "issueRef": "example/marketmaestro#42",
    "changeRequestRef": "example/marketmaestro!118",
    "runRef": "34619405233"
  },
  "intendedOutcome": "Portfolio holders can export a tax-year summary",
  "acceptanceCriteria": [
    {"text": "export matches the broker statement", "met": true,
     "evidenceRef": "gate-report.json"}
  ],
  "deliveredCapability": "GET /portfolio/export?year=",
  "result": "delivered",
  "deploymentStatus": "verified",
  "taskClass": "implementation",
  "firstPassVerification": true,
  "firstPassReviewAccepted": false,
  "repairRounds": 1,
  "regressionEscaped": false,
  "reverted": false,
  "rolledBack": false,
  "defectFollowups": 0,
  "intakeAt": "2026-10-01T09:00:00Z",
  "mergedAt": "2026-10-01T17:00:00Z",
  "liveAt": "2026-10-03T09:00:00Z",
  "observations": [
    {"observationId": "obs-1", "kind": "user-visible-capability-verified",
     "observedAt": "2026-10-03T12:00:00Z", "observer": "atulg4",
     "observerKind": "human", "healthy": true, "environment": "production"}
  ],
  "recordedAt": "2026-10-03T12:00:00Z",
  "provenance": {"source": "forge-ci", "releaseRef": "v1.4.0",
                 "commitSha": "…", "evidenceRefs": ["gate-report.json"]}
}
```

An **accepted** outcome is one recorded as `delivered` and neither reverted nor
rolled back. Partial delivery appears in the result breakdown but is never
scored as value: "some of it works" is not a delivered capability.

### Post-merge and post-deploy observations

An outcome is not finished when CI goes green. `OutcomeObservation` records what
happened afterwards, with the kinds the factory can actually distinguish:
`feature-exercised`, `tests-healthy`, `telemetry-healthy`,
`user-visible-capability-verified`, `no-observable-adoption-yet`,
`regression-observed` and `defect-followup`. "Nobody has used it yet" is a
first-class answer, not a gap.

### Completing an outcome without rewriting it

An outcome is learned in stages: intake, then the merge, then the deployment,
then an observation days later. `EffectivenessLedger.append` fills what was
unobserved and adds what is new, but a value already recorded can never be
quietly replaced — a contradicting append fails closed. Replaying the same
record is a no-op.

Each kind of field is merged on its own terms:

- **Scalars** (`result`, timings, counts, flags) fill once and then freeze.
- **References and provenance** fill field by field, because at intake there is
  no change request, commit, release or deployment evidence to record yet. A
  later append supplies them; evidence references union; a conflicting value for
  something already recorded fails closed. The provenance `source` stays the one
  that opened the outcome.
- **Acceptance criteria** merge per criterion: a verdict or evidence reference
  filled later is kept, and replaying the unassessed intake record cannot erase
  it. The criteria text must still match, and a contradicting verdict fails
  closed.
- **Deployment status** advances but never walks back. A late replay of an
  earlier stage — a `verified` record arriving after a `rolled-back` one — is a
  stale delivery, not news, and is refused rather than allowed to restore
  `verified` and have the rolled-back capability scored as delivered value
  again. `rolled-back` is the end of the line for one outcome; a successful
  redeploy is a new release, and so a new `outcomeId`.
- **Observations** are added by ID; restating one differently fails closed.

### Reading a revert or a rollback

`result: "reverted"` and `deploymentStatus: "rolled-back"` are themselves the
observation, so an outcome that declares either counts as reverted or rolled
back whether or not the redundant boolean flag was also set — otherwise a known
failure would be excluded from `revertRate` and `rollbackRate` and those rates
would read as an observed zero. Only an outcome that says neither reads as
unobserved. `was_reverted` and `was_rolled_back` expose the resolved signal, and
the project comparison reads the same ones.

## Learning records

`LearningRecord` captures a lesson with its provenance: what caused delay or
rework, which model and stage combination performed well or badly, which
deterministic check could replace a recurring AI review finding, and which
estimator or routing adjustment the evidence suggests.

The `LearningLedger` is strictly append-only. Unlike an outcome, a learning is
never completed in place: a revision is a *new* record that supersedes its
predecessor by ID, so the reasoning that was acted on at the time stays
readable afterwards. `current()` returns the learnings nothing supersedes.

## Metrics

`build_effectiveness_report(outcomes, usage_records=…, window=…, learnings=…)`
scores one caller-supplied sample. Nothing is dropped from it: an outcome that
recorded no result is visible as unobserved, so a rate can never be improved by
excluding the runs that would spoil it.

- **Quality** — first-pass verification rate, first-pass review acceptance,
  mean repair and review rounds, regression-escape, reopen, revert, rollback
  and defect-followup rates, acceptance-criteria-met rate, accepted-outcome
  rate, and a time-to-live distribution (min/p50/p90/max, nearest-rank, never
  interpolated) measured only where both the intake and live instants were
  observed.
- **Value** — accepted outcomes per agent-hour, per million tokens, per
  pay-as-you-go dollar, per subscription-equivalent dollar, per infrastructure
  dollar and per wall-clock day. Each carries its numerator, denominator, unit
  and status, so a reader can recompute it by hand.
- **Estimate versus actual** — signed mean percentage error for total tokens,
  runtime and pay-as-you-go cost, reported overall, by task class and by
  project. A positive value means the actual overran the estimate.
- **Observations** — counts by kind, plus how many outcomes have no
  post-merge observation at all.

Wall-clock value requires an explicit `ObservationWindow` (the same type
`capacity_metrics.py` uses), so a quiet period stays in the denominator instead
of vanishing from it.

## Cross-project comparison

A project working mostly on small, low-risk units is not more effective than
one working on hard ones. `compare_projects` reports each project's raw rate
beside one standardized to the **pooled task-class mix** of the compared
projects, so the comparison reflects performance rather than workload.

Where a project has no data for part of that mix, the normalized figure is
marked `partial`, reports the `coveredWeight` it was computed over, and is
never extrapolated across classes the project never worked. Outcomes with no
task class are compared as the `unclassified` class rather than dropped.

Comparable metrics are `acceptedOutcomeRate`, `firstPassVerificationRate`,
`firstPassReviewAcceptanceRate`, `regressionEscapeRate`, `revertRate` and
`rollbackRate`.

## Querying

`EffectivenessLedger.query` scopes by project, work unit, task class, result and
time. Time bounds are compared as **instants**, not as strings: the schema
accepts any RFC 3339 offset and fractional seconds, and those do not sort
chronologically as text — `2026-10-01T01:00:00+02:00` is an hour *before*
`2026-10-01T00:00:00Z`, however the two compare character by character.

## Relationship to `efficacy.py`

[`efficacy.py`](efficacy.md) scores the *pipeline* — per-run outcomes, quality
findings, bounded self-correction experiments and policy promotion. This module
scores the *product* of that pipeline: whether a capability was delivered, what
it took, and what was learned. They are complementary and share no state:
`RunOutcome` is one agent run, `OutcomeRecord` is one delivered outcome across
however many runs produced it.

## No credentials, no transcripts

No field in the outcome or learning schema can hold a deployment, merge, broker
or model credential. `secret_bearing_effectiveness_fields()` returns the
schema field names that could — structurally empty, and asserted by a test, in
the same spirit as the usage ledger and executor registry. Free text is bounded
at 512 characters so a raw prompt or transcript cannot be smuggled into a
record; evidence is referenced, not inlined.

## CLI

```sh
# Record or complete one outcome (replay is a no-op).
sdlcctl record-outcome --outcomes outcomes.json --outcome outcome.json \
  --registry projects.json

# Append one advisory learning.
sdlcctl record-learning --learnings learnings.json --learning learning.json

# Score outcomes against what producing them consumed.
sdlcctl effectiveness-report --outcomes outcomes.json --usage usage.json \
  --learnings learnings.json \
  --window-start 2026-10-01T00:00:00Z --window-end 2026-11-01T00:00:00Z

# Compare projects, normalized for their task-class mix.
sdlcctl compare-projects --outcomes outcomes.json --metric acceptedOutcomeRate
```

`--registry` and `--usage` are optional everywhere they appear; without usage
records the value metrics report `unknown` rather than guessing. `--window-start`
and `--window-end` are required together. Every rejection exits 2 with the
reason on stderr, like the other `sdlcctl` subcommands.
