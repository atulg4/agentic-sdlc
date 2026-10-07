# Project Onboarding

## Fast path

```bash
pip install -e /path/to/agentic-sdlc          # provides `sdlcctl`
sdlcctl onboard --destination . --project-id owner/repo --test "pytest -q" \
  --implementer cloud-routine --runs-on ubuntu-latest --apply
sdlcctl doctor --destination .
```

`onboard --apply` writes the policy, guides, issue template, CI and Forge workflows, creates the
labels/ruleset/variables, and runs `doctor`. `doctor` lists the owner-only steps that remain:
setting the credentials below, installing the Publisher GitHub App, and registering a self-hosted
runner when `--runs-on` is `self-hosted,...`. Everything below is the manual procedure `onboard`
automates.

### Credentials `doctor` requires

Secrets are set with `gh secret set NAME --repo owner/repo`; variables with
`gh variable set NAME --repo owner/repo --body VALUE` (or `onboard --apply --var NAME=VALUE`, or
`--copy-vars-from owner/onboarded-repo`). `doctor` derives the list from the installed workflows
and the routed registry, and `tests/test_onboard.py` checks that every name it can demand appears
here.

| `--implementer` | Secrets | Variables |
|---|---|---|
| every mode | `CLAUDE_CODE_OAUTH_TOKEN` (planning) | |
| `route`, `claude`, `codex` (Actions implementers) | `PUBLISHER_APP_PRIVATE_KEY` | `PUBLISHER_APP_CLIENT_ID` |
| `route` (default) | `DEEPSEEK_API_KEY`, `ZAI_API_KEY`, `KIMI_API_KEY` | `DEEPSEEK_MODEL_FLASH`, `DEEPSEEK_MODEL_PRO`, `ZAI_MODEL_GLM`, `KIMI_MODEL_K3` |
| `codex` | `OPENAI_API_KEY` | |
| `cloud-routine` | nothing beyond the planning token | |
| any, with a private platform repository | `PLATFORM_READ_TOKEN` | |

The `route` row is the generated `.forge/executors.json`: every executor the router can select for
an implementation run needs its provider key and its `configured-by-NAME` model variable, because
the router falls back across them after a recoverable failure (the preflight also insists on both
DeepSeek model variables). Trimming executors from the registry, or providers from
`.forge/routing-policy.json`, drops their rows; an executor with `authMode: "api-key"` on the
`anthropic` provider needs `ANTHROPIC_API_KEY` instead of the OAuth token.

### Public repositories and self-hosted runners

GitHub runs every fork pull request's code in `ci.yml`, so a persistent self-hosted runner on a
public repository would execute untrusted code with whatever the host holds. `onboard` therefore
reads the repository's visibility (`--visibility auto`, the default; pass `public`/`private` when
offline) and, for a public repository, renders `ci.yml` on `ubuntu-latest` while the
issue-triggered agent workflows keep `--runs-on`. `--ci-runs-on` overrides the CI runner, but only
with GitHub-hosted labels on a public repository: there is no opt-in for self-hosted pull-request
CI. `doctor` fails a public repository in which any `pull_request*`-triggered job (in any workflow
file) targets anything but a single GitHub-hosted label.

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

### Spec stage for filed issues

Issues that are missing intake sections (Summary, Acceptance Criteria,
Required Tests, Non-Goals, Dependencies) no longer sit in the backlog. Install
`templates/github/auto-spec.yml` as `.github/workflows/agent-auto-spec.yml`.
Replace `PLATFORM_REPOSITORY` and `PLATFORM_COMMIT_SHA` with the same pinned
values as your other callers. Level 3 scaffolding installs this file
automatically. The workflow needs `CLAUDE_CODE_OAUTH_TOKEN`, or pass
`agent: codex` with `OPENAI_API_KEY`, or pass `agent: route` with an
`executor_registry_path` to use executor routing. It requests `contents: read`,
`issues: write` and `id-token: write`.

When a maintainer opens, edits or labels an incomplete issue, Forge does four
things:

- it drafts only the missing sections;
- it appends them under **"Drafted by Forge spec stage — owner review
  required"** and leaves your text unchanged;
- it adds the `spec-drafted` label;
- it comments to say which sections it drafted; open questions sit in the drafted block.

Review the draft and edit it if needed. Then remove `spec-drafted` or add
`spec-approved`. Until you do, planning and implementation are refused even if
`agent-ready` or `implementation-approved` is present. The spec stage never adds `agent-ready`
or `implementation-approved`. See [Intake: spec stage](intake.md#spec-stage).

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
