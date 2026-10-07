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
file) targets anything but a single GitHub-hosted label, following the jobs of every local
reusable workflow it calls (`./.github/workflows/x.yml`, with `with:` inputs substituted). A call
to a third-party reusable workflow, a runner expression that is not a resolvable input, or a
Forge platform reusable workflow called without an explicit `runs_on` cannot be proven hosted and
fails.

### Runner labels

The generated workflows are bash with Unix paths, so `--runs-on`/`--ci-runs-on` accept Linux and
macOS targets only: a GitHub-hosted `ubuntu-*`/`macos-*` label from GitHub's documented list
(`GITHUB_HOSTED_LABELS` in `onboard.py`; a version-shaped label GitHub does not provide is
rejected), or a self-hosted runner's labels. Windows labels are refused by `onboard` and failed by
`doctor`.

Implementation is Linux-only, whatever the implementer: `reusable-implement.yml` installs
bubblewrap for Claude Code with `apt-get`, which macOS has neither of, and a routed or Claude
implementation reaches that step. With an Actions implementer, `--runs-on` must therefore be a
GitHub-hosted `ubuntu-*` label or a self-hosted label set that includes `linux` (the OS label
every self-hosted Linux runner carries); `doctor` fails an implement call whose `runs_on` is not.
macOS remains fine for `ci.yml` (`--ci-runs-on macos-15`) and for a `cloud-routine` profile.

### Managed workflows

`agent-plan.yml`, `agent-auto-plan.yml`, `ci.yml` and, in Actions mode, `agent-implement.yml` and
`agent-auto-implement.yml` are Forge-managed. `doctor` rebuilds each from the repository's own
policy (`[agents] implementer` and `implementation_mode`, `[project] default_branch`, the platform
repository and the caller's pinned SHA) exactly as `onboard` renders it, parses both, and
requires them to be equal. Triggers and their filters, job `if:`, `needs`, every `with:` input
(`issue_number`, `trigger_actor`, `config_path`, `agent`, the registry paths, ...), `secrets:`,
permissions, concurrency, extra or missing jobs: any difference fails with
`managed workflow X differs from the generated template at <path>; re-run sdlcctl onboard --force`.
Comments and formatting do not count; only the parsed document does.

The only tunable knobs, each validated by its own check instead:

| Knob | Validated by |
|---|---|
| every job's `runs-on`, and a reusable call's `with.runs_on` | the runner checks (hosted label or an online runner; Linux for implementation) |
| `with.route_budget_usd` on a reusable call (may be added) | "implement callers pass a readable route_budget_usd" |
| the `if:` of `agent-auto-plan.yml:plan` and `agent-auto-implement.yml:preflight` | must be exactly the generated condition for the POLICY's labels |
| the platform pin (`uses: ...@<sha>`, `with.platform_ref`) | the template is rendered at the caller's own pin; "workflows pin the platform to a commit SHA" requires one SHA on the platform repository |

Everything else in those files is the template. To change it, change the policy and re-run
`onboard --force`.

### The CI test job is fully managed

`ci.yml`'s `test` job is NOT a tunable knob. Its steps are exactly what `onboard` renders from
the policy, and `doctor` requires the installed steps to equal them (parsed, not textually):

1. `actions/checkout` at a pinned SHA with `persist-credentials: false` and no `ref` or
   `repository` input, so the pull request's own revision is what is tested;
2. `actions/setup-python` at a pinned SHA, `python-version` from `[ci] python_version` in
   `agentic-sdlc.toml` (default `3.12`; `onboard --python-version`);
3. `Setup`, `Quality` and `Test`, each running the policy's `[commands]` `setup`, `quality` and
   `test` verbatim as one quoted YAML scalar.

Customize CI only through the policy: edit `[commands]` (or `[ci] python_version`) and re-run
`sdlcctl onboard --force`. Any hand edit to the steps -- an extra action, an extra flag on a
gate, a cache step -- fails "ci.yml runs the policy gates as 'test'" and "managed workflows match
the generated templates". Each gate command must be a single line (join several with `&&`) and
must not contain `${{` (GitHub would evaluate it before the shell runs the command); `onboard`
refuses such a command and `doctor` fails a policy holding one, rather than normalizing it into
something that runs differently.

### What `doctor` verifies exactly

- **Implementation mode** is `[agents] implementation_mode` in `agentic-sdlc.toml` (`actions` or
  `cloud-routine`), never inferred from which files exist. A policy written before the field
  existed still passes while its Actions implement callers are installed; without them, add
  `implementation_mode = "cloud-routine"` (or re-run `onboard --force`).
- **CI gates**: the `test` job's steps must equal the template rendered from `[commands]`
  (above); that equality is the guarantee. The checks that follow are defense in depth behind
  it. Each gate must be invoked exactly as `[commands]` states. Only output and fail-fast extras
  may be appended (`-q`, `-v`, `-x`, `--maxfail=N`, `--tb=…`, `--durations=N`, `-r…`, `--color=…`);
  anything else (`--help`, `--collect-only`, `-k`, `--ignore`, ...) fails the check.
  A gate counts only as a top-level command: commands inside a shell function body never count,
  whether or not the function is called. Nothing in the `test` job may change what a gate does
  without being a visible argument: a `*ADDOPTS*`, `<TOOL>_*` (for the gate tools: `PYTEST_*`,
  `RUFF_*`, `PIP_*`, ...) or interpreter/shell startup variable (`PYTHONPATH`, `BASH_ENV`, ...)
  set by workflow/job/step `env:` or by a script; any mention of `$GITHUB_ENV` or `$GITHUB_PATH`
  (a directory added to the path can shadow a gate tool); an `exit`, `exec` or `return` anywhere
  before a gate (it is then not proven to run); an `actions/checkout` with a `ref` or
  `repository` input; a function or alias
  named like a gate tool; `eval` or a sourced file other than a virtualenv's `bin/activate`; an
  action other than `actions/checkout`, `actions/setup-python`, `actions/cache` or
  `astral-sh/setup-uv`. The setup command itself runs repository code by design; doctor proves
  the workflow, not what that code does. The `pull_request` trigger must reach the `test` job
  for every pull request into the protected branch: no `paths`, `paths-ignore` or
  `branches-ignore` filter, a `branches` filter only when it lists the default branch by its
  exact name, and `types` (if given) including `opened`, `synchronize` and `reopened`.
- **Automatic callers** must trigger on `issues: types: [labeled]`, and their `if:` must be
  exactly the generated label condition (after whitespace normalization) for the policy's labels.
- **Implement calls** are read from the parsed `with:`/`secrets:` of each job calling
  `reusable-implement.yml`: `agent` (default `codex`) must be a literal the workflow accepts,
  `route_budget_usd` a finite non-negative number, and every routed secret must be forwarded by
  that job itself (or `secrets: inherit` on it). Route mode comes from the parsed policy
  (`[routing]`), the registry files, or an implement call's `agent: route`.
- **Runners** are resolved with the same resolver as the public-repository check (local reusable
  workflows followed). A matching self-hosted runner whose `os` is Windows does not count; a
  `runs-on: {group: ...}` target fails, because runner-group membership cannot be verified
  through the repository API.
- **Claude Code hooks** are read from the parsed `.claude/settings.json`: a `SessionStart` group
  firing on startup that runs `python3 .claude/hooks/forge_session_start.py`, and a `PreToolUse`
  group whose matcher covers `Bash` running `python3 .claude/hooks/forge_commit_guard.py`. The
  commit guard resolves git aliases before classifying a subcommand: inline `-c alias.X=...`,
  then `git config --get alias.X` where the command runs (repository and global configuration,
  plus `GIT_CONFIG_*` set on the command line). A `!` shell alias or an alias set through
  `--config-env` cannot be classified and counts as a commit.
- **Publisher App** permissions must be exactly Contents read, Issues write, Pull requests write
  (plus GitHub's mandatory Metadata read); any other grant fails.
- **Rulesets**: every parameter Forge's ruleset sets is compared (iterated from the payload, not
  hand-listed): a boolean Forge sets true -- `dismiss_stale_reviews_on_push`,
  `required_review_thread_resolution`, `strict_required_status_checks_policy` -- must be true, a
  count at least Forge's, a list a superset of Forge's.
- **`onboard --apply`** merges Forge's rules into an existing `Protect main` ruleset: existing
  rules and stricter parameters (more approvals, extra status checks, signatures) are kept.
  The one exception is Forge's own `test` check: a binding (`integration_id`) to an app other
  than GitHub Actions (15368) could never be satisfied by `ci.yml`, so the merge drops it and
  `doctor` fails a ruleset that has one.

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
