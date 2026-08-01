# ADR 0001: Separated Agentic Control Plane

- Status: Accepted
- Date: 2026-08-01
- Owner: Repository owner

## Context

The owner wants to direct MarketMaestro and future GitHub or GitLab projects
primarily through natural-language conversations, including from a phone. The
system must preserve deterministic engineering checks and repository controls
even when an AI model, issue body, source file, or test process is compromised
or simply incorrect.

A direct webhook that gives an agent a broad repository token would be easy to
operate but would combine planning, code execution, publication, and approval
in one authority. It would also make the orchestration specific to one model or
source-control provider.

## Decision

Create a provider-neutral policy package with thin GitHub and GitLab adapters.
GitHub is the v0.1 proving ground; GitLab gains equivalent execution jobs only
after the GitHub threat model and operating process are proven.

Use repository issues as the durable work queue. A complete work request,
eligibility labels, a separate implementation approval label, closed
dependencies, and a write-authorized trigger actor are required before code
generation.

Separate each authority into a fresh job:

1. Validate the work request and protected policy.
2. Let Codex plan without write access.
3. Let Codex or Claude generate a patch without repository write access.
4. Run changed consumer code in a verifier with no AI or write credential.
5. Reconstruct and attest the original patch in a clean job that executes no
   consumer code.
6. Push only the attested branch using the native GitHub token.
7. Open a draft pull request using a current-repository GitHub App token.
8. Run normal CI and an independent Codex review.
9. Require the owner to merge through protected-branch rules.

Bind patch artifacts to the base commit, repository, work request, byte length,
and SHA-256 digest. Pin the framework and third-party actions to immutable
commits. Version 1 never auto-merges or deploys.

## Alternatives

### Direct agent with a personal access token

Rejected. It exposes broad, long-lived authority to the same process that reads
untrusted prompts and source code.

### One workflow job for generation, tests, and publishing

Rejected. Changed test code can modify every file available in its job,
including a supposedly verified artifact.

### Long-running webhook service as the primary controller

Deferred. It adds public ingress, identity, replay protection, hosting, and
credential custody before the repository pipeline is proven. GitHub or GitLab
events already provide the needed initial callback mechanism.

### Fully automatic merge

Rejected for v0.1. Independent review and branch protection remain useful even
for low-risk changes, and MarketMaestro includes financial and security-sensitive
behavior.

## Consequences

- The owner can control work from ChatGPT mobile Remote or the SCM mobile app.
- Model providers can be exchanged without changing merge authority.
- A few setup steps remain human-controlled: repository creation, App
  installation, secret entry, branch rules, and final merge.
- Jobs duplicate some checkout and policy work to preserve isolation.
- GitLab has policy and planning support in v0.1 but not publishing parity.
- Project CI must not expose production secrets to pull-request code.

## Security Impact

The design reduces credential reachability and prevents an AI or changed test
from directly publishing arbitrary code. Residual risks include model review
errors, malicious dependencies or project test commands, compromised third-party
actions, source exposure to configured model providers, and owner approval of a
bad pull request. Branch protection, pinned actions, least-privilege secrets,
deterministic tests, and human review mitigate but do not eliminate those risks.

## Migration

Start consumers at automation level 1, move to level 2 after plan quality is
stable, and enable level 3 label triggers only after one verified draft-PR
trial. MarketMaestro completes pull request #11, closes issue #10, and restores
a green native test baseline before issue #2 enters implementation.

## Rollback

Disable the consumer caller workflows, suspend the publisher App, revoke model
credentials, and remove automation labels. Existing branches and pull requests
remain auditable and can be closed without changing the protected default
branch.
