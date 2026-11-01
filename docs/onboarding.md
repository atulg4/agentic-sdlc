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

### Runners, required approvals and the executable trust mode

- `--runs-on` defaults to `ubuntu-latest` (Codex 4243193108). The jobs it targets run
  model-produced patches (the reusable implement/repair `verify` job applies the patch and runs
  the setup, quality and test commands), and `ci.yml` runs every pull request's code; a
  persistent self-hosted runner would let that code persist into later jobs that receive model
  credentials and Publisher App secrets. Self-hosted labels (`--runs-on` or `--ci-runs-on`) are
  refused unless `--ephemeral-runners` records the owner's acknowledgement
  (`[automation] ephemeral_runners = true`) that every such runner is ephemeral (one job, then
  discarded) or otherwise contained; `doctor` shows "self-hosted runners are acknowledged
  ephemeral" as WARN for any self-hosted label without it.
- `--required-approvals N` (0-10, `[policy] required_approvals`) is the ruleset's
  `required_approving_review_count` (Codex 4243193100), and `doctor` holds the ruleset to at least
  that. Unset, it depends on who authors the pull requests (Codex 4243386518). In the Actions
  modes (`route`, `claude`, `codex`) the Publisher App opens them, so the owner is not the author
  and can approve: the default is 1, with or without `--apply`. In `cloud-routine` mode the
  routine opens them as the owner: with `--apply` it is 1 when
  `GET repos/{repo}/collaborators?permission=push` (every page; bots, the owner and the
  authenticated user excluded) finds another writer, else 0 -- an author can never approve their
  own PR, so 1 for a solo owner would block every merge; offline, or when the collaborators
  cannot be read, 0. At 0, `onboard` prints a WARNING and `doctor` shows "ruleset requires a
  human approval" as WARN -- loudly in `cloud-routine` mode, where the routine acts with the
  owner's identity, so the server-side human-merge boundary is not enforced; in the Actions modes
  with the `--required-approvals 1` fix, since the owner can review the App's PR. The ruleset
  has no bypass, so at 1 a pull request the owner opens by hand needs another reviewer too.
- `--trust-mode strict|local` (`[automation] trust_mode`, default strict; `--force` keeps the
  installed policy's) decides which git, gh and hook interpreter Forge will run. Strict: only
  files root alone can replace (the file and every directory above it), no PATH search as root,
  override variables refused as root. Local, for a developer's own machine: the first match on
  PATH (then the system locations), the override honoured, and the file owned by root or the
  current user with no group/other write bit -- environments stay allowlisted (no `LD_*`/`DYLD_*`,
  a fresh gh config directory, the pinned host) and the hook interpreter stays an absolute path
  chosen at onboard time and probed as CPython 3.11+ (with local, onboard also looks at the
  first `python3` on PATH, so a Homebrew Python works). The mode is rendered into the commit
  guard (and so the session-start hook). At run time GitHub Actions (`GITHUB_ACTIONS=true`) and
  Claude Code cloud sessions (`CLAUDE_CODE_REMOTE=true`) are always strict; elsewhere the CLI uses
  the mode of the policy it reads (`--config`, else `agentic-sdlc.toml`), strict without one --
  except the publication-decision commands (`check-diff`, `check-request`, `authorize-labeler`,
  `claim`, `renew`, `release`, `confirm-cloud-routine`: the gates the cloud routine and the
  Actions workflows run), which are ALWAYS strict and never consult the `AGENTIC_SDLC_GIT` /
  `AGENTIC_SDLC_GH` overrides (a set one is reported on stderr and ignored). Neither a policy
  file (the working tree is the agent's to edit) nor the environment (an agent's shell can unset
  `CLAUDE_CODE_REMOTE`) may loosen how they pick git and gh; `local` only affects the
  developer-facing commands (`onboard`, `doctor`, `inspect-diff`, `run-gates`, `claims`, ...)
  and the hooks.
  `doctor` reports the mode ("executable trust mode"; WARN in local, since strict is what
  protects agent contexts).

**Migration:** re-run `sdlcctl onboard --force` (plus `--apply` for the ruleset). Repositories on
self-hosted runners add `--runs-on <labels> --ephemeral-runners` or move to hosted runners; pass
`--required-approvals 1` (Actions modes: now the default) once a reviewer other than the PR's
author exists. A policy written before these fields
reads as `trust_mode = "strict"`, `ephemeral_runners = false`, `required_approvals = 0`.

### Credentials `doctor` requires

Secrets are set with `gh secret set NAME --repo owner/repo`; variables with
`gh variable set NAME --repo owner/repo --body VALUE` (or `onboard --apply --var NAME=VALUE`, or
`--copy-vars-from owner/onboarded-repo`). `doctor` derives the list from the installed workflows
and the routed registry, and `tests/test_onboard.py` checks that every name it can demand appears
here.
In an organization-owned repository the organization's secrets and variables shared with it
count too (`repos/{repo}/actions/organization-secrets` / `organization-variables`; a repository
variable wins over an organization one of the same name). When those inherited inventories cannot
be read, a name missing at repository scope is a WARN to confirm, not a failure.

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
the router falls back across them after a recoverable failure. Trimming executors from the
registry, or providers from `.forge/routing-policy.json`, drops their rows: `doctor` derives this
row from the router run over the installed registry and policy, and the generated `route`
preflight checks only the Publisher App, never a provider's credentials (it cannot run the
router, so a hard-coded provider would fail every run of a registry without it). An executor
with `authMode: "api-key"` on the `anthropic` provider needs `ANTHROPIC_API_KEY` instead of the
OAuth token.

### Fork-exposed repositories and self-hosted runners

GitHub runs every fork pull request's code in `ci.yml`, so a persistent self-hosted runner on a
repository that fork pull requests can reach would execute untrusted code with whatever the host
holds. That is every public repository, and also a private or internal one whose repository,
organization or enterprise policy allows forking AND runs workflows from fork pull requests: an
enterprise member or read collaborator can then fork it and run PR code on the runner.

A repository counts as **fork-safe** only when GitHub proves one of the two is off: the repository
is private or internal and `GET repos/{owner}/{repo}` reports `allow_forking: false`, or `GET
repos/{owner}/{repo}/actions/permissions/fork-pr-workflows-private-repos` reports
`run_workflows_from_fork_pull_requests: false`. Anything unread (a 403/404 on that endpoint, a
missing field, an unknown visibility) is not proof. Everything else is **fork-exposed**.

`onboard` reads this (`--visibility auto`, the default; pass `public`/`private` when offline --
`private` asserts fork-safe, and `doctor` verifies it) and, for a fork-exposed repository, renders
`ci.yml` on `ubuntu-latest` while the issue-triggered agent workflows keep `--runs-on`.
`--ci-runs-on` overrides the CI runner, but only with GitHub-hosted labels on a fork-exposed
repository: there is no opt-in for self-hosted pull-request CI. To keep `ci.yml` self-hosted on a
private repository, disable forking (or fork pull-request workflows) for it first.

`doctor` checks "fork pull requests cannot reach self-hosted runners". A fork-safe repository
passes. Otherwise it fails when any `pull_request*`- or `merge_group`-triggered job (a merge
group's SHA contains the queued PRs' code, fork PRs included; in any workflow
file on the default branch, enumerated through the Git Trees API -- the set GitHub runs, not this
checkout's, which is checked in addition; the contents API stops listing a directory at 1,000
entries without saying so, and a tree answer that is truncated, at the API's entry cap or
unreadable fails closed, as does any unreadable file) targets anything
but a single GitHub-hosted label, following the jobs of every local
reusable workflow it calls (`./.github/workflows/x.yml`, with `with:` inputs substituted). A call
to a third-party reusable workflow, a runner expression that is not a resolvable input, or a
Forge platform reusable workflow called without an explicit `runs_on` cannot be proven hosted and
fails. When the fork policy is merely unknown, that failure is an owner TODO (never `READY`):
disable forking or fork pull-request workflows, or move those jobs to GitHub-hosted runners. A
private repository that disables only fork pull-request WORKFLOWS (forking still allowed) still
merges fork PRs through its merge queue, so its `merge_group` jobs must be GitHub-hosted too;
forking disabled outright needs nothing more.

### Runner labels

The generated workflows are bash with Unix paths, so `--runs-on`/`--ci-runs-on` accept Linux and
macOS targets only: a GitHub-hosted `ubuntu-*`/`macos-*` label from GitHub's documented list
(`GITHUB_HOSTED_LABELS` in `onboard.py`; a version-shaped label GitHub does not provide is
rejected), or a self-hosted runner's labels. Windows labels are refused by `onboard` and failed by
`doctor`.

Every Forge plan and implementation workflow is Linux-only, whatever the implementer and in both
implementation modes: `reusable-implement.yml` installs bubblewrap for Claude Code with `apt-get`,
and `reusable-plan.yml` hashes the checkout with GNU `sha256sum`, neither of which macOS has. A
`cloud-routine` profile still runs the plan callers, so it is no exception. `--runs-on` must
therefore be a GitHub-hosted `ubuntu-*` label or a self-hosted label set that includes `linux` (the
OS label every self-hosted Linux runner carries); `onboard` refuses anything else, and `doctor`
fails ("Forge workflows run on Linux runners") any job of a plan or implement caller, or any
`runs_on` it hands a platform workflow, that is not Linux. macOS remains fine for `ci.yml` only
(`--ci-runs-on macos-15`).

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
| every job's `runs-on` (except `agent-auto-plan.yml:preflight` and `:dedupe`, `agent-auto-implement.yml:authorize` and both files' `authorization_failed`, below), and a reusable call's `with.runs_on` | the runner checks (hosted label or an online runner; Linux for implementation) |
| `with.route_budget_usd` on a reusable call (may be added) | "implement callers pass a readable route_budget_usd" |
| the `if:` of `agent-auto-plan.yml:preflight` and `agent-auto-implement.yml:authorize` | must be exactly the generated condition for the POLICY's labels |
| the platform pin (`uses: ...@<sha>`, `with.platform_ref`) | the template is rendered at the caller's own pin; "workflows pin the platform to a commit SHA" requires one SHA on the platform repository |

Everything else in those files is the template. To change it, change the policy and re-run
`onboard --force`.

`onboard --force` replaces the workflows and the other fully managed files wholesale, but merges
the tunable ones: in `AGENTS.md` and `CLAUDE.md` only the generated guide text is replaced (notes
around it are kept; a file with no guide gets it appended; a guide edited in place is refused
rather than guessed at), and in `.claude/settings.json` the Forge hook handlers are written once
beside every other key and hook (unparseable settings are refused, never overwritten).

`agent-auto-plan.yml` authorizes before it schedules anything on your runners. Anyone who can open
an issue can use the work-request template, and the template applies the planning labels itself;
so its `preflight` job runs on `ubuntu-latest` (GitHub-hosted, NOT tunable, `permissions:
contents: read, issues: read` -- the label events need `issues: read` in a private repository), asks
`GET repos/{owner}/{repo}/collaborators/{login}/permission` with `GITHUB_TOKEN` whether the
labeler (`github.event.sender`), `github.actor` AND whoever applied each of the ready and
human-review labels most recently (read from the issue's label events, every page) have write,
maintain or admin, and the reusable
`plan` call runs only `if: needs.preflight.outputs.authorized == 'true'` (and `dedupe`, below,
found no plan for the current approval). An outsider's issue therefore never occupies a
self-hosted runner; a maintainer who re-applies either planning label starts planning (Codex
4241196642: EITHER planning label's `labeled` event triggers, once both are present, so
re-adding a `human-review-required` whose removal cancelled planning restarts it). Duplicates
are suppressed by the `dedupe` job (`ubuntu-latest`, NOT tunable, `permissions: issues: read`):
it skips planning when a `## Agentic SDLC plan` comment by `github-actions[bot]` is newer than
the latest application of EVERY planning label (`preflight` outputs the newest of those times as
`approved_latest_at`; Codex 4241327876: re-applying just one label after a plan therefore plans
again -- `approved_at`, the earliest, is kept for the content-staleness check, since an edit must
precede every label), so an issue opened with both labels -- two `labeled` runs, serialized by the
shared concurrency group -- is planned once; comments it cannot read fail it, and
`notify_failure` reports that. (`agent-plan.yml` is `workflow_dispatch` only, which already
requires write access.)

`agent-auto-implement.yml` does the same, with the same step (one shared template fragment): a
user with the Triage role can apply and remove the approval labels without write access, so its
first job, `authorize` (`ubuntu-latest`, carrying the label condition, NOT tunable), runs the
identical collaborator-permission check, and every job that can reach your runners --
`preflight`, the reusable `implement` call and `notify_failure` -- lists `authorize` in `needs`
and runs only when `needs.authorize.outputs.authorized == 'true'`. Toggling a label without write
access therefore never schedules anything on a self-hosted runner. The approver is read from the
label history, not taken from the event: the run can be triggered by a writer adding the last
prerequisite label after a Triage user applied `implementation-approved` (or the other way
round), so whoever applied EACH label of the approval set -- `implementation-approved`, the ready
label and `human-review-required` -- most recently must have write, maintain or admin too: the
rule `sdlcctl authorize-labeler` applies for a cloud routine. A non-writer's label (for example
the planning labels the work-request template applied for a Triage user) therefore needs a writer
to re-apply it before implementation starts. The approver (who applied `implementation-approved`
last), not the event's sender, is what the reusable call receives as `trigger_actor` (the
platform re-checks its permission). Label events that cannot be read, or that never show a label
of the set applied, fail `authorize`; a label removed since it was last applied is
"unauthorized". (`agent-implement.yml` is
`workflow_dispatch` only and has no `authorize` job.)

The approval is bound to the issue content it saw (Codex 4241196649): the title (its `renamed`
events) and the body (GraphQL `issue.lastEditedAt` and the newest `userContentEdits` entry,
read with `GITHUB_TOKEN`) must have been last edited BEFORE the most recent application of EACH
label of the set -- planning and implementation alike, and `sdlcctl authorize-labeler` applies
the same rule. An issue edited at or after that is "unauthorized" (the run's notice says to
review it and re-apply -- remove and add -- each label); an edit time that cannot be read FAILS
the authorization job, which `authorization_failed` reports. Both automatic callers also trigger
on `edited`, which joins the issue's concurrency group with `cancel-in-progress` true (below),
so an edit cancels an authorized run that is queued or running; the `edited` run itself starts
nothing.

A permission check that cannot get a definitive answer (rate limit, 5xx, network) after its
retries FAILS the authorization job rather than reading as "unauthorized". Every job after it is
then skipped, so both automatic callers carry one more job, `authorization_failed`
(`ubuntu-latest`, NOT tunable, `permissions: issues: write` only): it runs only when
`preflight`/`authorize` finished as `failure` or `cancelled`, and comments on the issue that the
labeler's authority could not be verified, with the run link, so the label event is not consumed
in silence. A definitive "unauthorized" (the job succeeds with `authorized=false`) posts nothing.
`notify_failure` keeps its `authorized == 'true'` gate: it runs on your runners, which an
unverified labeler must never reach.

Authorization is read when a run starts, so REMOVING an approval label stops a run that is
already queued or running (Codex 4240628982): both automatic callers trigger on `issues:
[labeled, unlabeled, edited, closed]`, and the
workflow-level `concurrency` puts every `labeled` event of a triggering label, every `unlabeled`
event of a required label and every `edited` event in one group per issue
(`forge-auto-implement-<n>` / `forge-auto-plan-<n>`), with `cancel-in-progress` true only for the
`unlabeled` and `edited` runs. Closing the issue cancels a queued or running implementation or
planning run the same way (Codex 4242753923, 4242925431): the `closed` run joins the issue's
group with `cancel-in-progress` and starts nothing itself (`authorize` / `preflight` require
`labeled`, although a closed issue keeps its labels). The reusable workflow also renews its lease after generation, after verification and
immediately before pushing, and `renew` refuses once the issue is closed, so a close that lands
before that last renewal stops the run without publishing. A Triage user who removes a writer's label and re-applies it while the
authorized run waits for a runner therefore cancels that run; the `unlabeled` run itself starts
nothing (every label condition requires `github.event.action == 'labeled'`), and the re-applied
label starts a run that re-authorizes from the label history -- and finds the Triage user.
Every other label event, the lease's own `in-progress` label included, gets a group of its own
and cancels nothing; for planning, applying the second planning label queues behind the run the
first one started (a `labeled` run never cancels), and `dedupe` keeps it from planning twice. A cancelled run reports like any other: `authorization_failed` (its
message says that removing an approval label cancels a run) or `notify_failure`, and the
reusable workflow's `release_lease_on_failure` releases the lease of any run whose claim may
have been posted, a cancelled or failed `prepare` included.
**Residual risk:** cancellation is asynchronous, so steps already executing finish their current
work first. The pinned reusable workflow re-checks only `trigger_actor`'s permission, not the
label history; a re-check of the label set inside it is left to the platform's maintainer.
The cloud routine re-authorizes the labels in its LAST gate, immediately before pushing and
after check-diff and the lease check: step 4d of its prompt runs ONE command, `sdlcctl
check-request --expect-digest <digest> --authorize-labels` (Codex 4241839918, 4242264478), which
judges a single snapshot of the issue by every request and labeler rule and then re-reads the
issue and its history and requires them unchanged; its residual window is the seconds between
that command's second read and the push.

### Other managed files

Every other file `onboard` writes is compared with what it renders for the repository's policy,
both in the checkout and on the default branch:

| File | Compared |
|---|---|
| `.claude/hooks/forge_commit_guard.py`, `.claude/hooks/forge_session_start.py` | byte for byte |
| `.github/ISSUE_TEMPLATE/agent-work-request.md` | byte for byte |
| `docs/forge/cloud-implementer.md` (cloud-routine mode) | byte for byte apart from the CI runner it names |
| `AGENTS.md`, `CLAUDE.md` | must contain the generated text verbatim (notes may be added around it) |
| `.claude/settings.json` | structurally: the Forge hooks must be configured (other settings are yours) |
| `.forge/executors.json`, `.forge/routing-policy.json` | semantically: loaded and routed on |
| `agentic-sdlc.toml` | the policy itself: loaded and checked against the repository |

Every one of them must be a regular file reached without a link: doctor applies the same
`lstat` rule `onboard` writes under, so the path and every parent component must not be a
symlink -- not even one to an identical file elsewhere in the checkout, whose target is an
unmanaged path a later patch could change -- or "required files present" (and, for the routing
registries, the routing check) fails.

Every policy field `onboard` writes from its options -- `[project] id`/`default_branch`,
`[automation] ready_label`, `[commands]` `setup`/`quality`/`test`, `[ci] python_version`,
`[policy] forbidden_paths`/`protected_paths`/`max_changed_files`/`max_diff_lines` -- is checked by
ONE validator (`POLICY_FIELDS` in `onboard.py`) on both sides: `onboard` refuses a value it
rejects, and `doctor` fails a policy holding one ("policy values are ones onboard accepts"). So
`--setup ""` is refused up front instead of writing a profile doctor then calls not ready.
The generated `forbidden_paths` baseline (`BASE_FORBIDDEN`: workflows, the policy, `.forge/**`,
`.claude/**`, agent docs, CODEOWNERS, secrets, databases and media) must also survive in the
EFFECTIVE policy (`load_policy`): a hand edit or merge that drops any entry fails "policy keeps
every generated forbidden path", naming it (Codex 4241050285). Project-specific entries may be
removed; `protected_paths` has no generated baseline.

A difference fails "managed files match the generated files" (or, on the default branch,
"managed files are on the default branch"); re-run `sdlcctl onboard --force`.

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
something that runs differently. The Actions implement and repair paths verify a patch with
`sdlcctl run-gates`, which runs each gate the way ci.yml's default `run:` shell does -- one
`bash -e -o pipefail -c '<command>'` (`sh -e -c` without bash) from the repository root -- so
`cd sub && make test`, `VAR=x pytest` and `a && b` mean the same there as in CI (Codex
4243193109); only its environment differs (an allowlist, never the runner's credentials or
control files), and pipefail makes it stricter than ci.yml's plain `bash -e` in one case: a
piped gate (`pytest | tee out`) fails when any part of the pipe fails (Codex 4246344191).

### What `doctor` verifies exactly

- **Implementation mode** is `[agents] implementation_mode` in `agentic-sdlc.toml` (`actions` or
  `cloud-routine`), never inferred from which files exist. A policy written before the field
  existed still passes while its Actions implement callers are installed; without them, add
  `implementation_mode = "cloud-routine"` (or re-run `onboard --force`).
- **Cloud routine** (`cloud-routine` mode): `onboard` only writes `docs/forge/cloud-implementer.md`
  with a suggested prompt; the scheduled routine lives on claude.ai, where `doctor` cannot see
  it. "cloud implementer routine is scheduled" is therefore a manual TODO -- and the repository
  NOT READY -- until the owner creates the routine and records it with `sdlcctl
  confirm-cloud-routine` (sets `[automation] cloud_routine_confirmed = true`; only the boolean
  `true` counts; the policy must be a regular file in the repository -- a symlinked or
non-regular `agentic-sdlc.toml` is refused, and the file is replaced atomically, never written
through a link). `onboard --force` rewrites the policy and drops the confirmation: re-check the
  routine against the regenerated document, then confirm again. The document's "Environment
  setup script" section (`python3 -m pip install --user "agentic-sdlc @
  git+https://github.com/<platform>@<pinned sha>"`) must be the routine environment's setup
  script: a fresh cloud environment has no `sdlcctl`, and step 0 of the prompt stops the run
  unless `sdlcctl --version` prints `commit <pinned sha>`. Doctor compares the document at the
  pin the installed workflows carry. The routine prompt runs `sdlcctl
  authorize-labeler --project owner/repo --issue N --label implementation-approved --label
  <ready> --label human-review-required` before claiming, the same check the Actions path runs:
  the most recent application of EACH of those labels (from the issue's label events) must be by
  a user with write/maintain/admin -- a 200 carrying that permission; a 404 or a lower role, or a
  label removed since, or a title or body edited at or after a label of the set was last
  applied, exits 3 (not authorized), and anything unreadable -- the edit time included -- exits
  2 (not verified);
  only exit 0 claims, and the prompt runs the same command again right before pushing (exit
  0 only; otherwise it releases the lease and stops). Without `--label` it reads the three labels from `--config` (default
  `agentic-sdlc.toml`); a policy it cannot read exits 2. Step 1 of the prompt selects issues
  with a `gh issue list --search` that excludes `claude-blocked`, and an issue whose body is not
  a valid work request is commented on, labelled `claude-blocked` and its lease released before
  the routine moves on (Codex 4241196636), so one malformed request cannot starve the queue.
  The list asks for `--limit 1000` (`gh issue list` returns 30 by default; 1000 is also the
  most one GitHub issue search returns) and the routine works through it in order, oldest
  first, until it claims one: an issue skipped because it is unauthorized, unverifiable or
  leased elsewhere stays in the search, so it moves on to the next candidate instead of ending
  the run (Codex 4241327879). More than 1000 approved candidates is out of scope.
  The Actions path's other deterministic gates run as commands in the prompt, each gating on its
  exit code (Codex review 5483165195): `sdlcctl check-request --project owner/repo --issue N`
  parses the issue with the same validator and policy as `prepare-request` (exit 3: invalid --
  the issue is retired as above) and requires every dependency issue to be closed (exit 4: the
  lease is released and the issue stays queued; anything unreadable exits 2); `sdlcctl renew`
  runs after implementation, after verification and immediately before pushing, and fails
  unless this run still holds a live lease (it never pushes over another agent's takeover --
  keep `lease_ttl_minutes` longer than the longest stage); and `sdlcctl check-diff --project
  owner/repo --base origin/<default>` applies `inspect-diff`'s diff policy (forbidden paths,
  file/line/byte caps) to the COMMITTED change from its merge base to `HEAD` -- exactly what
  the push publishes, never the working tree -- and exits 0 only when it passes and is
  non-empty. The forbidden-path check covers every path ANY commit of the series touches, not
  only the end state, since the push publishes the history (Codex 4241839922: a file added in
  one commit and deleted in the next is still refused); a merge commit in the series exits 2;
  the caps apply to the end-state diff. `--base` is a local ref the agent could re-point, so
  check-diff (and `check-request --base`) first reads the default branch and its head from
  GitHub through the trusted gh and exits 2 unless `--base` resolves to exactly that commit;
  policy and diff are then read from that commit (Codex 4241839926), and the committed
  policy's project must be `--project`. check-diff reads
  history with replacement disabled (`--no-replace-objects`, `GIT_NO_REPLACE_OBJECTS=1`, grafts
  off): a `refs/replace/*` object or graft could otherwise make it judge a benign tree while the
  push publishes the original commit (Codex 4241532610). It judges the change under the
  policy COMMITTED on `--base` (`git cat-file blob <base>:agentic-sdlc.toml`; `--config` is
  that file's path in the repository), never the working-tree copy, which the agent could
  loosen without committing (Codex 4241614590); a policy missing or unparseable there exits 2,
  and the baseline forbidden paths apply whatever it lists. Every git it runs (check-diff and
  `inspect-diff` alike) is a trusted executable, never a bare PATH lookup (Codex 4241614592): a
  git only root can replace -- the file and every directory on the way to it owned by root,
  with no group/other write bit and not writable by this user -- taken from the system
  locations (`/usr/bin/git` first) and, for a non-root user, from absolute PATH entries that
  pass the same test; running as root only the system locations count. `AGENTIC_SDLC_GIT` may
  name another git for `inspect-diff`, vetted the same way (refused as root); check-diff, a
  publication decision, ignores it (Codex 4244027559). No trusted git exits 2. Git runs
  with an environment built from an allowlist (fixed `PATH=/usr/bin:/bin`, `LC_ALL=C`, the
  replacement/config switches above) -- nothing of the caller's, so `LD_PRELOAD`,
  `LD_LIBRARY_PATH`, `LD_AUDIT` or `DYLD_*` cannot load code into it (Codex 4241839908).
  `sdlcctl renew` also re-reads the issue and fails once it is closed or unreadable (Codex
  4241614587), so every renewal is a state check -- and re-reads the issue's open pull requests
  (the inventory `claim` reads, linked and cross-referenced PRs included) and fails once one
  exists or cannot be read (Codex 4243193102). A claim that fails (any non-zero exit) is
  followed by a best-effort `sdlcctl release` of the same session before the routine moves on
  (Codex 4243193105): a claim that lost the race and could not retract its marker would
  otherwise leave it queued, to become authoritative when the holder releases. Step 2 runs `check-request --base
  origin/<default>` (the committed policy) and keeps the `content digest: sha256:...` it
  prints; step 4d, the final gate run immediately before the push with nothing in between,
  runs it again with `--expect-digest <digest> --authorize-labels` and the three approval
  labels: ONE command that reads the issue and its event history once, judges that snapshot by
  every rule -- exit 5 when the title or body changed since step 2 (even if a maintainer then
  re-applied the labels, which re-authorizes: Codex 4241614582), 4 when a dependency was
  reopened (Codex 4241614596), 6 when the issue is no longer open, 7 when an approval label was
  not last applied by a writer after the last edit (the `authorize-labeler` rule) -- then reads
  the issue and its history again and requires the update time, state, content digest, labels
  and events unchanged. Both reads also read the issue's open pull requests: any open PR in
  either read exits 9 (Codex 4243193102: a PR opened or linked after the claim already
  represents the work). A changed title or body fails at once (5); any other change is judged
  afresh from a new snapshot, at most 3 times, then exit 8. Separate digest and labeler checks
  let an edit plus a label re-apply, or a reopened dependency, land between them and pass both
  (Codex 4242264478). On anything but 0 the routine releases its lease and stops without
  pushing. Each run pushes, never forced, to its
  own branch `forge/issue-<N>-<stamp>` (`<stamp>` = `date -u +%Y%m%d%H%M%S`, chosen once before
  the claim and passed to it as `--branch`), so a branch left behind by a run whose PR could not
  be opened never blocks a retry (Codex 4241532615); an abandoned one can be deleted by hand.
  A routine created from an earlier version of the document must be re-pasted.
- **Cloud routine branch rules** (`cloud-routine` mode, Codex 4241196640): the routine pushes
  `forge/issue-<N>-<stamp>` with the owner's credentials, so "effective rules on the cloud
  routine's forge/issue-<N>-<stamp> branches allow its push" runs the publisher's branch-domain
  checks against that namespace: every ruleset's branch targeting is judged over all issue
  numbers and stamps (one that targets some and not others is a manual TODO), classic
  protection patterns matching it are reported for each requirement (a push/creation allowlist is always a TODO: the pusher is a user, which doctor
  cannot match), and the effective rules of `forge/issue-1-1` pass only for rule types that cannot
  reject a new branch (deletion, non-fast-forward, linear history, tag names, required checks
  not enforced on creation, a branch-name pattern proven for every issue number and stamp).
  Creation and update restrictions, signatures, commit-metadata patterns (the owner's own message and
  identity) and anything unknown or unreadable are owner TODOs.
- **Project id**: `[project].id` must equal, exactly, the `full_name` GitHub answers for `GET
  repos/{id}` -- its spelling, and after a rename or transfer (which the API follows as a
  redirect) the NEW name, since every run passes that as `$GITHUB_REPOSITORY`. A mismatch fails
  with the fix; an unreadable answer is a manual TODO. `onboard --apply` writes the `full_name`
  and says so when it differs from `--project-id`.
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
  through the repository API. `doctor --local` runs every runner rule that needs no API too
  (Codex 4241050282): resolvable targets, Unix/Linux, the `ubuntu-slim` job-time limit, and
  hosted-label recognition ("runner labels are recognised"), reading each Forge platform call
  by its `runs_on` input (default `ubuntu-latest`) since the callee is not fetched. Locally a
  standard GitHub-hosted label or a set naming `self-hosted` passes (whether a runner is
  registered and online is checked only remotely); a hosted-runner-shaped label GitHub does not
  host (`ubuntu-99.04`, `macos-99`) without `self-hosted` fails; a larger hosted runner or a
  custom label set without `self-hosted` is a TODO only remote `doctor` can clear.
- **Claude Code hooks** are read from the parsed `.claude/settings.json`: a `SessionStart` group
  firing on startup that runs the session-start script, and a `PreToolUse` group whose matcher
  covers `Bash` running the commit guard followed by `|| exit 2`, with `"timeout": 300` and
  `"onFailure": "block"`. Each handler is
  `FORGE_SESSION_PATH="$PATH" PATH=/usr/bin:/bin <python> -I -c '<loader>'
  "${CLAUDE_PROJECT_DIR}/.claude/hooks/<script>.py" <sha256>` (Codex 4242753911, 4242753904):
  `<python>` is an absolute interpreter `onboard` vetted -- `/usr/bin/python3`, else
  `/bin/python3`, or `--hook-python PATH` -- that only root can replace (the file and every
  directory above it root-owned and not group/other-writable) and that IS a CPython of at least
  the package's `requires-python` (3.11): onboard and doctor RUN it as the handler does
  (`-I -c`, PATH=/usr/bin:/bin) and it must echo a fresh nonce with its implementation and
  version (Codex 4242925452: a root-owned `/bin/true` exits 0 without running the loader, so
  ownership alone would let the guard be skipped); onboarding FAILS when there is
  none, and doctor fails a handler whose interpreter is not trusted on the machine it runs on,
  or one that runs a bare `python3` (a PATH lookup: the cloud setup puts the user-writable
  `~/.local/bin` on PATH, where a planted `python3` could exit 0 instead of guarding). `-I`
  ignores `PYTHON*` variables and the user site. The fixed inline loader reads the script once,
  refuses (exit 2, which blocks) unless its SHA-256 is the one recorded in the handler, restores
  the session's PATH (the guard models what a command's bare program names resolve to from it)
  and runs exactly those bytes as `__main__`. Doctor requires the recorded hash to be the
  installed script's, and the installed script to be the rendered template (byte for byte), so
  the hash in the handler is the shipped template's. On a machine without a root-owned Python
  (macOS with only a Homebrew python: `/opt/homebrew` and `/usr/local` are user/admin-writable)
  onboarding refuses; there `/usr/bin/python3` (Apple's, root-owned) is the one to use. The
  script is named under the project root, so a
  `cd` in the session cannot break the hook (a relative path fails doctor); the guard fails
  CLOSED -- a crash, a missing interpreter or file, or a timeout blocks the Bash call
  (`onFailure: "block"` is Claude Code's documented handler field for that, v2.1.295 or later;
  `|| exit 2` covers older clients, and the guard bounds its own run at 240s, below the
  timeout). Each Forge handler must be exactly the one `onboard` writes (only the command's
  spelling may vary: the vetted interpreter's path, `$CLAUDE_PROJECT_DIR` with or without
  braces); a handler with any
  other field -- `"async": true` above all, which runs it in the background where its exit 2 can
  no longer block the commit -- or a missing or different `timeout`/`onFailure` fails, wherever
  it sits. Settings from an older onboard fail until `sdlcctl onboard --force` rewrites them.
  **Trust boundary:** the hooks are a guardrail against mistakes and casual misuse. They run as
  the agent's own user from the agent-writable checkout, so an agent with write access can edit
  `.claude/settings.json` (handler and hash together) as easily as the script; no local,
  same-user hook can be tamper-proof. Enforcement is server-side -- the `Protect main` ruleset,
  required review and Publisher-only pushes. Unrelated
  hooks may sit beside them. The commit guard is deny-by-default for git: on an issue branch without the
  session's lease, every git invocation is blocked except literal read-only subcommands (status,
  log, diff, show, blame, grep, ls-files, ls-tree, rev-parse, describe, shortlog, `reflog show`,
  listing forms of `branch`/`tag`/`remote`/`config`/`stash`, `fetch` without a refspec or
  `--update-head-ok`, help, version, cat-file, for-each-ref, name-rev, merge-base, check-ignore,
  check-attr; `fetch` also only while no configured `remote.*.fetch` refspec writes a local
  branch -- an issue branch there needs its lease, a glob into `refs/heads` or unreadable
  configuration blocks). Aliases, `git-<name>` programs, `-c`/`--config-env`, and anything that runs git or
  a shell indirectly (`xargs git`, `find -exec git`, `... | sh`) need the lease too. So does a
  read-only subcommand whenever git could run a program configured outside the command --
  `diff.external`, a textconv/diff/filter driver (git-lfs's canonical filters excepted), a pager
  other than plain `less`/`more`/`cat`, a `core.fsmonitor` hook, a signature program for
  `--show-signature`, an initialized submodule or `.gitmodules`, a reference-transaction /
  post-index-change / pre-auto-gc hook, and for `fetch`/`remote` an ssh/askpass/credential/proxy
  helper or a remote URL that needs a remote helper -- in any config scope or include, or in
  `GIT_EXTERNAL_DIFF`/`GIT_EXEC_PATH`/`GIT_PAGER`/`PAGER`/`GIT_SSH*`/`*ASKPASS`; when that
  configuration cannot be read; and with `--ext-diff`, `--textconv` or `--filters`. git runs
  the default SSH transport (`ssh`) -- and the shell runs git itself -- from PATH, so every git
  in a command that mentions PATH anywhere (`export PATH=/tmp/bin:$PATH; git fetch origin`,
  `read PATH`, zsh's `path=(...)`, `hash -p`) or sets an `LD_*`/`DYLD_*`/`SSH_ASKPASS*` variable
  needs the lease, and so does every read-only subcommand while PATH holds a relative entry
  (`.`, an empty entry) -- or a WRITABLE absolute entry searched before (or at) a program the
  command may launch: `git` itself when named bare, the pager (`less`, or the configured one),
  `man` for `git help`, and for `fetch`/`remote show` `ssh` and the remote/credential helpers git
  runs by name (git's exec path is searched first and must not be writable either; a missing
  entry counts as writable when its nearest existing ancestor is). A command can drop an `ssh`
  there without mentioning PATH. On a machine where such a directory is writable -- running as
  root, a user-owned `~/.local/bin` or Homebrew prefix ahead of the system git -- every
  allowlisted git on an unleased issue branch therefore needs the lease. Every
  GitHub lookup is bounded at 30s and every local git call at 15s: a lookup that times out
  refuses instead of hanging the session. A branch
  change earlier in the same command re-targets the git after it: `git switch forge/issue-7 &&
  git commit` is checked against issue 7, `git switch main && git commit` against main (a plain
  switch to an existing branch needs no lease); a change whose destination is not literal
  (`git checkout "$B"`, `git checkout -`, an alias, bisect, `source`, `cd "$D"`) blocks any
  later guarded git in that command -- run the two as separate commands. The repository a
  guarded command selects must be resolvable before it runs: `git -C "$DIR"`, `--git-dir=$X`,
  `--work-tree`, `GIT_DIR=`/`GIT_WORK_TREE=` set to an expansion, `$(...)`, backticks, a glob or
  an ambiguous `~` are refused (use a literal path; an unquoted leading `~` is resolved), and a
  branch lookup that fails for any reason other than "not a git repository" (a directory that
  does not exist yet, an untrusted repository) refuses instead of reading as "no branch".
- **Publisher App** permissions must be exactly Contents read, Issues write, Pull requests write
  (plus GitHub's mandatory Metadata read); any other grant fails.
- **Rulesets**: every parameter Forge's ruleset sets is compared (iterated from the payload, not
  hand-listed): a boolean Forge sets true -- `dismiss_stale_reviews_on_push`,
  `required_review_thread_resolution`, `strict_required_status_checks_policy` -- must be true, a
  count at least Forge's, a list a superset of Forge's.
- **Effective rules**: `doctor` also reads the rules that actually apply to the default branch
  (`GET repos/{owner}/{repo}/rules/branches/{branch}`), so organization and enterprise rulesets
  count. A required status context no installed workflow job reports, required deployments,
  required workflows, a merge queue no workflow runs on `merge_group` for -- or, with a merge
  queue, a required context no workflow running on `merge_group` reports (the queue waits for
  every required check in the merge group, not on the PR) -- required signatures, update
  restrictions and code-scanning gates are reported as a manual TODO, as is an unreadable
  answer. The contexts a PR (and, separately, a merge group) can receive are computed from the
  default branch's workflow set as GitHub serves it (the checkout only adds a drift note for a
  workflow not yet pushed; an unreadable set is a manual TODO), always as EXACT names: job
  names; for a matrix job one context per literal combination -- `name (v1, v2, ...)`, the axes'
  values in axis order then the values an `include` adds, minus `exclude`d ones -- or exactly
  the expanded name when the name itself holds `matrix.*` expressions, once PER COMBINATION (a
  name that omits an axis, `test-${{ matrix.os }}` over two Python versions, is two jobs
  reporting `test-ubuntu`: two producers, so a required `test-ubuntu` is ambiguous) (a matrix
  doctor cannot enumerate reports nothing; no `name (` prefix is ever accepted); and for a reusable call `caller / callee-job` for each job of the callee as fetched (a local callee, or
  a platform callee at its pinned SHA, recursively); an unresolvable callee reports nothing, so
  `plan / anything` is never assumed. `onboard --apply` still changes only the repository's own
  ruleset.
- **Workflow state**: a workflow disabled in GitHub Actions (`disabled_manually`,
  `disabled_inactivity`, ...) does not respond to its triggers. `doctor` reads every page of
  `GET repos/{owner}/{repo}/actions/workflows` and requires each Forge-managed workflow
  (`agent-*.yml`, `ci.yml`) to be `active` ("Forge workflows are active in GitHub Actions"); a
  non-active workflow is no evidence for any required context (a context only it would report
  is flagged "enable it", including on `merge_group`). An unreadable state list is a manual
  TODO, and leaves the required contexts unverifiable.
- **`onboard --apply`** merges Forge's rules into an existing `Protect main` ruleset: existing
  rules and stricter parameters (more approvals, extra status checks, signatures) are kept.
  The one exception is Forge's own `test` check, which is always bound to GitHub Actions
  (`integration_id` 15368): unbound, any app or credential with commit-status access could
  report `test` and satisfy the gate without `ci.yml` running; bound to another app, `ci.yml`
  could never satisfy it. The merge rebinds it to GitHub Actions, and `doctor` fails a `test`
  requirement that is unbound or bound elsewhere -- in `Protect main`, in inherited
  org/enterprise rulesets and in classic branch protection (`checks[].app_id`) alike.
- A push allowlist (classic protection) or ruleset over `agent/**` must admit the actor that
  actually pushes the publisher's branch. The pinned `reusable-implement.yml` pushes with the
  checkout's persisted `github.token`, i.e. as `github-actions[bot]` (the GitHub Actions app);
  the Publisher App's token only opens the pull request. `doctor` reads the pusher from the
  pinned workflow and reports a manual TODO when it cannot.
- **Push rulesets** (`target: push`, repository, organization or enterprise) reject a push by
  the files it carries, for every actor. `doctor` reads each active one (disabled and
  evaluate-mode ones never block): a restricted path or extension passes only when the policy's
  `forbidden_paths` provably excludes it (the same glob, or `*.<ext>`, is listed there); a
  path-length or file-size limit, an unknown rule and an unreadable ruleset are a manual TODO.
- **GITHUB_TOKEN across reusable calls**: a called workflow can only keep or lose
  permissions. The calling job's `permissions` (else its workflow's, else the repository
  default from `GET repos/{owner}/{repo}/actions/permissions/workflow`) is the ceiling for every
  job of the callee and everything it calls in turn; a callee job asking for more fails the
  contract check, and a ceiling doctor cannot determine is a manual TODO.
- The commit guard checks a rebase's branch operand like any other named destination: `git
  rebase UPSTREAM BRANCH` and `git rebase [--onto X] --root BRANCH` (whose only operand is the
  branch) need BRANCH's lease wherever they run; operands it cannot tell from option values (an
  unknown or abbreviated option, too many operands) block the command.
- The commit guard's open-PR fallback counts only a PR whose head branch lives in this
  repository: it lists `repos/{owner}/{repo}/pulls?state=open&head=OWNER:BRANCH`, every page, and
  checks each answer's head repository, so a fork's PR from an identically named branch (however
  many there are) is not this branch's claim and cannot hide this repository's own PR.
- Every `gh` call `sdlcctl` makes (claim, renew, release, authorize-labeler, onboard, doctor) is
  bounded: 60s per call, `AGENTIC_SDLC_GH_TIMEOUT` (1-3600 seconds) overrides it. A call that
  does not answer in time fails closed (exit 2) instead of hanging the session or routine.
- Every such call runs the trusted gh in an environment built from an allowlist (Codex
  4241839914, 4241839908): `PATH=/usr/bin:/bin`, `GH_HOST=github.com`, and `GH_CONFIG_DIR` and
  `HOME` set to a fresh private (0700) empty directory removed afterwards, so no config of the
  caller's (`http_unix_socket`, aliases, a stored host) applies; no `LD_*`/`DYLD_*` variable;
  no `SSL_CERT_FILE`/`SSL_CERT_DIR`, so GitHub's certificate is verified against the system
  trust store. The token is `GH_TOKEN`, else `GITHUB_TOKEN`, else what `gh auth login` stored
  (`gh auth token`, a local read). The proxy variables (`HTTPS_PROXY`, `HTTP_PROXY`,
  `NO_PROXY`, either case) are kept on purpose: a proxy can only relay TLS that the system
  store verifies, so it cannot read or forge an answer -- but an egress proxy that intercepts
  TLS needs its CA installed in the system store (e.g. `/usr/local/share/ca-certificates` +
  `update-ca-certificates`), not supplied through the environment.
- The commit guard matches a live lease to this session only by the hook payload's
  `session_id`, and only when that is a non-empty string: a payload whose id is missing, null,
  empty or not a string owns no lease, so guarded git on a leased issue branch is refused
  (fail closed) and the SessionStart hook reports no lease as this session's.
- The commit guard never maps git it cannot read to the current checkout: an assembled program
  word (`G=git; "$G" -C DIR commit`, `$(which git)`, `${GIT:-git}`, `\git`, `"git"`) or git
  handed its command line by another program (`xargs`/`find -exec`/`parallel` without a literal
  subcommand or with a placeholder before it, `eval`, a script piped to a shell, an interpreter
  whose code also selects a repository) has an unresolved repository and is refused, whatever
  lease the session holds. Plain `git`, a literal path to it (`/usr/bin/git`) and the wrappers
  the guard parses (`env`, `command`, `exec`, `nice`, `time`, `timeout`, `sudo`, ...) keep their
  `-C`/`--git-dir`/`GIT_DIR` selectors and are checked against the repository they name. A
  path-qualified git (`/usr/bin/git`, `./git`, `~/bin/git`, `bin/git`) must be the SAME file as
  the git a non-writable PATH lookup finds, or a known system git (`/usr/bin/git`, `/bin/git`,
  `/usr/local/bin/git`, `/opt/homebrew/bin/git`), with neither the file nor any directory
  traversed to reach it writable by the user (Codex 4241050279); anything else -- a `git`
  script the session wrote, a link from a writable directory -- is unresolved and refused on
  every branch, read-only subcommands included. Where those directories are writable (running
  as root, a user-owned Homebrew prefix) spell it plain `git`.
- The commit guard reads the shell script files a command runs (Codex 4240526381): `bash x.sh`,
  `sh -e x.sh`, `bash < x.sh`, `source x.sh`, `. x.sh`, and `./x.sh` run directly (a `.sh`/`.bash`
  path, or one whose `#!` line names a shell), and a bare command name that PATH resolves to
  such a script (Codex 4240628980: `deploy` where `~/bin/deploy` starts `#!/bin/sh`; the first
  executable match, as the shell picks it, in the hook's PATH and in every PATH the command or a
  script it reads assigns from literal text, `$PATH`, `$HOME`, `$PWD` or a leading `~`). A PATH
  change the guard cannot compute (`read PATH`, `PATH=$(...)`, `for PATH in`, `hash -p`, zsh's
  `path[1]=`) makes every bare program in that command unresolved -- refused on every branch --
  so set PATH to literal directories. A PATH-resolved script that names git nowhere, nor in
  anything it runs or sources by a literal name, is not parsed further: system tools that are
  shell scripts (`which`, `zgrep`, `mvn`, `gradle`, `gcloud`) pass as before. The script (a
  regular UTF-8 file of at most 256 KiB, resolved from the directory the shell runs in, plus PATH for a bare name) is parsed
  like an inline command, so `git commit` inside it on an unleased issue branch, or `git -C
  /path/to/forge/issue-7 commit` inside it run from main, is refused just as it would be inline;
  scripts it runs are read in turn, up to three levels. A script the guard cannot read (missing,
  not a regular file, binary, too large, an unclosed quote, nested deeper) is refused on every
  branch. **Scope:** the guard is a guardrail for the git invocations it can see -- inline
  commands and shell script files -- not a sandbox. A build tool or interpreter that runs git
  internally (`make`, `npm run`, `python x.py`, a compiled program, a non-shell `#!` script) and
  direct writes into `.git/` are out of its sight; the issue lease, branch protection and the
  review gates on the server are the real enforcement.
- The SessionStart hook fetches only remote-tracking refs (Codex 4240526383): `git fetch --no-tags
  --no-recurse-submodules --no-prune --no-write-fetch-head --no-auto-gc --refmap= origin
  '+refs/heads/*:refs/remotes/origin/*'`. A bare `git fetch origin` follows the configured
  `remote.origin.fetch` refspecs, and one mapped onto `refs/heads/forge/issue-7` would
  force-update a local issue branch; the explicit refspec and the empty `--refmap=` make git
  ignore them. When git would run a program configured outside the command for that fetch
  (an ssh/askpass/credential/proxy helper, a remote-helper URL such as `ext::`, a
  reference-transaction or pre-auto-gc hook -- the commit guard's own helper check), the hook
  skips the fetch, says why and warns that the remote state shown is cached; it never blocks
  the session.
- **Repository writable**: `doctor` requires `GET repos/{owner}/{repo}` to answer
  `archived: false` and `disabled: false` explicitly. An archived repository's issues, pull
  requests, comments, branches and labels are read-only (a disabled one accepts nothing), so
  either one being true fails; a missing or non-boolean field is a manual TODO. `onboard --apply`
  refuses such a repository before writing a file or changing a setting.

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
   private platform, allow the consumer to call its reusable workflows (on the
   platform: Settings → Actions → General → Access, "Accessible from
   repositories owned by" its user or organization; `sdlcctl doctor` reads this
   setting and reports it as a manual TODO when it cannot) and
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

### Kubernetes runners

Set the consumer's Actions repository variable `FORGE_RUNNER` to its ARC scale
set name, for example `marketmaestro-runners`. Generated callers use this target
for both their own jobs and the `runs_on` input to reusable Forge workflows.
Without the variable, generated workflows retain the `ubuntu-latest` default.
Passing `runs_on` is essential: a caller's runner choice does not automatically
propagate into a reusable workflow. Older generated files must be updated too.

Use a reviewed immutable platform revision that declares the `runs_on` input.
Validate its required secrets and permissions before updating a consumer pin;
older releases may use a different model authentication interface.

See [the ARC deployment runbook](../infra/arc/README.md) for the pinned runner
image, scale-to-zero pool values, isolation checks and rollout procedure.

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
