# Operating Model

## Normal request

1. The owner describes the desired outcome by voice or text.
2. Codex converts it into one or more complete work requests.
3. The owner confirms priority and any product ambiguity.
4. The plan workflow comments a read-only implementation plan.
5. The owner approves implementation by adding the configured label or
   manually dispatching the implementation workflow.
6. The implementation agent produces a patch artifact.
7. Fresh CI verifies the patch.
8. An independent Codex run reviews the exact verified diff.
9. The owner merges after required checks and conversations are complete.

## Authority levels

| Level | Automation | Default use |
|---|---|---|
| 0 | Read and summarize only | Repository onboarding |
| 1 | Automatically produce plans | Initial rollout |
| 2 | Produce draft PRs after explicit approval | Normal feature work |
| 3 | Retry bounded CI fixes on the same branch | After stable CI |
| 4 | Auto-merge allowlisted low-risk changes | Disabled in v0.1 |
| 5 | Deploy after environment approval | Separate future project |

MarketMaestro starts at Level 1. Level 2 begins only after its CI baseline is
green and at least five plan-only runs have been reviewed.

## Decision records

Architecture decisions belong in `docs/adr/`. Each record states context,
decision, alternatives, consequences, security impact, and rollback. Agents may
propose ADRs but cannot approve their own architectural changes.

## Maintenance loops

Scheduled read-only jobs may:

- triage stale issues;
- summarize dependency alerts;
- identify flaky tests;
- report policy drift;
- propose documentation updates;
- measure agent acceptance, rework, and defect rates.

Scheduled jobs do not implement or merge changes unless the owner separately
enables that task class.

## Success measures

- percentage of plans accepted without major revision;
- percentage of generated patches that pass deterministic CI first time;
- review findings by severity and source;
- escaped defects after merge;
- agent cost and runtime per accepted change;
- blocked runs caused by unclear specifications;
- time from request to reviewed draft PR.
