# Project Onboarding

## Required repository state

- protected default branch;
- pull request or merge request required;
- force pushes and branch deletion blocked;
- required deterministic CI checks identified;
- conversation resolution required;
- stale approvals dismissed after new commits;
- no bot or app bypass permission;
- production secrets absent from agent-accessible CI;
- repository-specific `AGENTS.md` and review checklist committed.

## GitHub

1. For a public platform repository, no platform credential is needed. For a
   private platform, allow the consumer to call its reusable workflows and
   create a fine-grained token that has Contents: Read for only the platform
   repository. Store it as `PLATFORM_READ_TOKEN`; never use a broad PAT.
2. Copy the thin caller workflows from `examples/marketmaestro`.
3. Replace release tags with reviewed immutable commit SHAs.
4. Add `OPENAI_API_KEY` only for the Codex action. Prefer workload identity or
   short-lived provider authentication where supported.
5. Add an Anthropic credential only when enabling the Claude implementation
   adapter. Prefer workload identity federation over a long-lived API key.
6. Before enabling implementation, create the dedicated publisher GitHub App
   described in [GitHub Publisher App](github-publisher-app.md). Install it only
   on approved consumer repositories.
7. Store its Client ID as the Actions variable `PUBLISHER_APP_CLIENT_ID` and
   its private key as the Actions secret `PUBLISHER_APP_PRIVATE_KEY`.
8. Start with manual `workflow_dispatch` plan runs.

Automation levels are deliberately staged:

| Level | Behavior |
|---|---|
| 1 | Manual, read-only planning |
| 2 | Manual implementation plus independent PR review |
| 3 | Labels automatically start planning and approved implementation |

At level 3, add `human-review-required` before `agent-ready` to start one plan
run. Add `implementation-approved` only after accepting that plan. The pipeline
checks that the label or manual trigger came from an actor with repository write
authority.

The platform read token is used only to retrieve the pinned policy engine in a
no-AI preparation job. It is not a consumer-repository write credential and is
not passed to the AI subprocess or publisher.

Do not put the user's personal GitHub token in this pipeline. Implementation
publishing fails closed when the publisher App variable or secret is missing.

## GitLab

1. Mirror or publish this project as a GitLab CI/CD component project.
2. Include the component at a reviewed version or commit SHA.
3. Configure a protected runner for implementation jobs.
4. Use a project access token with only the API and repository permissions the
   publisher needs, or a short-lived job token where supported.
5. Route issue webhooks through the optional intake gateway to a pipeline
   trigger. Validate the GitLab webhook secret and event UUID.
6. Start with manual plan pipelines.

## Baseline audit

Before enabling implementation, record:

- repository languages and package managers;
- exact setup, lint, test, build, and security commands;
- current failing checks and technical debt;
- secret locations by name only;
- protected and forbidden paths;
- deployment mechanisms;
- data or financial correctness invariants;
- rollback procedures.
