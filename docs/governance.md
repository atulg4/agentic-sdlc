# Governance

## Durable Authority

Codex is the primary architect, planner, and review coordinator. That role is
made durable through repository artifacts: work requests, architecture decision
records, policy, plans, review findings, and test evidence. No individual chat
session is treated as hidden project memory or as an approval authority.

The project owner retains product priority, security exceptions, production
access, and final merge authority. Automated agent runs are delegates with
bounded permissions, not additional repository owners.

## Decision Classes

| Decision | Proposer | Required approval |
|---|---|---|
| Product scope and priority | Owner or architect | Owner |
| Routine implementation plan | Architect | Owner for ambiguity |
| Low or medium-risk patch | Implementation agent | CI, independent review, owner merge |
| Security, auth, financial, or data design | Architect | Explicit architect/security and owner review |
| Workflow, policy, or deployment change | Human-controlled branch | Owner review; never agent-generated |
| Production deployment | Separate release process | Environment approval |

## Architecture Records

Material architecture changes use an ADR under `docs/adr/`. An ADR includes
context, decision, alternatives, consequences, security impact, migration,
rollback, and owner approval. An agent may draft an ADR but cannot approve it.

## Emergency Stop

Disable the consumer caller workflows or revoke their Actions access to this
platform repository. Suspend or uninstall the publisher GitHub App before
rotating the affected AI credential and App private key. Preserve the run
artifacts, and review every branch and draft change request created by the
affected runs.
