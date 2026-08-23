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
