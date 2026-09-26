# Autonomous work intake

`src/agentic_sdlc/intake.py` lets the platform discover, normalize,
deduplicate, prioritize, and route work from generic systems instead of
requiring every task to begin as a manually labeled GitHub issue.

## Sources

Intake origins are declared in `intake.toml` (a baseline forbidden path, so
only human-reviewed commits can change it):

```toml
version = 1

[[source]]
id = "gh-issues"
type = "github-issues"
provider = "github"

[[source]]
id = "gh-ci"
type = "github-ci"
provider = "github"
credential_ref = "CI_READ_TOKEN"
```

A source's `permitted_actions` are drawn from a closed set
(`create-work`, `update-work`); anything resembling write authority over
external systems (`close-ticket`, …) fails closed. Closing or updating an
external ticket is a separate mission with its own policy — never an intake
permission. Credentials are named by reference and are adapter-specific.

## Normalization

Adapters turn raw payloads into versioned `WorkRequest` records: source
identity, owning project, work type and severity, title/problem statement,
evidence citations, affected components and candidate paths, linked work,
confidence, timestamps, a deterministic `WorkFingerprint`, lifecycle state,
and the human-approval flag.

Implemented adapters: GitHub issues, GitHub PR review findings, and GitHub CI
failures — a CI failure fingerprints on workflow + branch, so retried and
repeated failures converge onto one work request, and the triggering PR or
commit lands in `linked_work` instead of spawning an unrelated issue.
`INTAKE_CONTRACTS` documents the authenticated, read-only contracts for Jira,
GitLab, generic webhooks, scanners, and REST submissions, and
`check_intake_conformance` proves any new adapter is deterministic, validated,
and free of ticket-write operations.

## Deduplication and evidence

`WorkQueue.submit` converges any event with a matching fingerprint into the
existing request (evidence and links merge, version increments) — duplicate
webhook deliveries, polling overlaps, and mirrored tracker items produce one
work unit. `add_evidence(..., contradictory=True)` returns work to triage
when new evidence conflicts with the current understanding.

## Prioritization

`prioritize()` produces a `WorkPriority` whose component scores (urgency,
impact, confidence, effort, risk), weights, formula version, and reasons are
all exposed — no opaque LLM-only ordering. High-severity security and
data-quality signals are marked preemptive by platform rule; consumers can
override business impact, but the preemption floor cannot be removed. Effort
and urgency are advisory and calibrated over time.

## Routing

`route()` applies consumer `RoutingRules`: ignore lists, a minimum-confidence
gate (low-confidence inferred issues stay investigations), approval-required
work types (evidence preserved while a human decides), and auto-plan
eligibility for low-risk types. The default is human approval. Routing feeds
the #16 orchestration lifecycle; it never implements anything by itself.

`WorkQueue.report()` returns the machine-readable queue snapshot (totals,
lifecycle buckets, full requests, and an audit log) for dashboards.

## Spec stage

Every issue must satisfy the intake contract in `task_spec.parse_task`:
**Summary**, **Acceptance Criteria**, **Required Tests**, **Non-Goals** and
**Dependencies**. The last three list sections each need at least one list item,
and Dependencies must hold `#N` references or say `None`. Before the spec stage,
an issue that failed this check just sat in the backlog with the "spec"
disposition. The spec stage (`src/agentic_sdlc/spec_stage.py`,
`.github/workflows/reusable-spec.yml`) drafts the missing sections itself and
then holds the issue for owner review.

1. **Diagnose.** `sdlcctl spec-check` (`check_task_spec`) reports every
   deficient section as JSON, not just the first one. Each finding names the
   section and one of four problems: `missing`, `empty`, `no-list-items` or
   `invalid-dependencies`. Request-level problems such as an empty title, a
   NUL byte or an oversized body go in `blockers` and make the request
   non-draftable. `ready` is defined by `parse_task`, so the diagnosis can
   never disagree with intake. The command exits 0 when the issue is ready, 1
   when sections are deficient and 2 on error. `validate-task` is unchanged.

   ```bash
   sdlcctl spec-check --task issue.md --title "Export CSV" --output check.json
   ```

2. **Draft.** `render_prompt(task, "spec")` asks the routed
   `specification-planner` executor (task class `planning`) to write only the
   deficient sections, under their exact required headings. The executor must
   ground each section in the repository's code and docs and must leave the
   existing text alone. The issue body is wrapped in the same
   `<untrusted-work-request>` boundary as the other modes, and embedded
   boundary markers are removed until none remain. The executor must not
   invent dependencies. If it cannot ground one, it writes `None` and adds an
   `## Open Questions` item. If the issue looks superseded or duplicated, it
   returns only a `## Not Drafted` section with its evidence, and Forge posts
   that as a comment instead of editing the issue.

3. **Merge.** `sdlcctl merge-spec` (`merge_spec`) appends the accepted
   sections beneath the author's text. They go inside a Forge-owned block
   titled **"Drafted by Forge spec stage — owner review required"** and
   bounded by `<!-- forge-spec-stage:begin/end -->`. The author's text is kept
   byte for byte, apart from trailing whitespace. Drafts for sections the
   author already satisfied are dropped, because a later heading would
   override the author's version. The merge refuses the draft if any of
   these are true:
   - it misses a deficient section;
   - it contains a heading or a Forge marker;
   - it references the issue itself;
   - the result would still fail `parse_task`.

   Re-running the merge replaces only the Forge block, so the merge is
   idempotent. A drafted `None` dependency always gets an open question
   asking the owner to confirm it. The workflow also refuses the draft if the
   issue body changed after drafting started (checked by SHA-256), or if a
   drafted `#N` dependency does not exist in the repository.

4. **Hold for review.** The credential-free publish job adds the
   `spec-drafted` label and removes any stale `spec-approved` label. Only
   after that does it write the body. If the body write fails, the issue is
   still held and never left with an unreviewed spec that passes intake. The
   spec stage never adds `agent-ready`, `human-review-required` or
   `implementation-approved`.

### Safety gate

`spec_review_block(labels)` is the deterministic gate. An issue labelled
`spec-drafted` stays ineligible until a human either removes `spec-drafted`
or adds `spec-approved`. The gate is enforced in two places:

- `policy.evaluate_task(..., mode="implement")`, which `prepare-request` runs
  before any implementation executor, so `reusable-implement.yml` fails
  closed even when `implementation-approved` is present;
- `AutonomousIntakeDispatcher.dispatch`, which records the event as `blocked`
  instead of admitting it to orchestration.

Planning is read-only and stays available, so an owner can still request a
plan while reviewing a drafted spec.

### Triggers

The consumer workflow (`templates/github/auto-spec.yml`, installed as
`agent-auto-spec.yml` by `sdlcctl scaffold --automation-level 3`) runs when an
open issue is opened, edited or labelled and does not already carry
`spec-drafted`. It drafts only when all of these hold:

- the event came from an actor with repository write authority (issues from
  outside collaborators wait until a maintainer edits or labels them);
- the issue is open, is not a pull request and is not already held;
- `spec-check` finds draftable gaps.

Label and body writes made with the workflow token do not start new
workflow runs. Once merged, the body passes `spec-check`, so the stage does
not loop.
