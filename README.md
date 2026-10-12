# Agentic SDLC

Agentic SDLC is a reusable control plane for planning, implementing, verifying,
reviewing, and maintaining software through GitHub Actions or GitLab CI.

It is designed for a simple operating model:

- the owner describes work in natural language;
- Codex acts as the primary architect, planner, and independent reviewer;
- Claude Code or Codex can act as an implementation worker;
- deterministic CI decides whether code is technically acceptable;
- repository policy decides what an agent may touch;
- a human remains the merge authority until a separately approved low-risk
  auto-merge policy is introduced.

MarketMaestro is the first consumer and proving ground. Project-specific rules
remain in MarketMaestro; reusable orchestration lives here.

## What exists in v0.1

- A provider-neutral work-request parser.
- A spec stage that drafts missing work-request sections for owner review
  and holds `spec-drafted` issues out of planning and implementation until approved.
- Deterministic task and diff policy checks.
- Configuration-driven provider/model executor routing with recoverable
  quota, capacity, and auth fallback.
- GitHub and GitLab webhook normalization.
- A command-line interface for CI: `sdlcctl`.
- A reusable GitHub policy action.
- GitHub plan, implementation, and review workflow templates.
- A GitLab CI component template and webhook contract.
- A MarketMaestro policy profile.
- A provider-neutral usage ledger with estimate-versus-actual accounting for
  tokens, time, model, worker and cost, subscription-aware and never
  fabricating unknown figures.
- Parallelism, queueing, runner and subscription-capacity measurement with
  explainable bottlenecks, reported against a caller-supplied window.
- Security, operations, onboarding, and phone-control documentation.
- Offline unit tests for the core policy behavior.

## Trust model

No AI worker can merge, approve its own work, deploy, or retrieve production
credentials. AI execution and repository publishing occur in different jobs.
The patch-generation job receives the AI credential but no repository write
token. Changed code runs only in the verifier. A later clean attestation job
reconstructs the publishable patch without executing consumer code. The
publisher receives a repository write token but no AI credential. On GitHub,
the publisher job uses its native short-lived token only to push the attested
branch. A dedicated App token opens the draft pull request and comments on the
issue, while every AI and verification job remains read-only.

See [Security](docs/security.md) and [Architecture](docs/architecture.md).

## Local verification

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
```

## CLI examples

```bash
sdlcctl validate-task \
  --config examples/marketmaestro/agentic-sdlc.toml \
  --task issue.md \
  --title "Implement secure preview-share field controls" \
  --label agent-ready \
  --label human-review-required

sdlcctl evaluate-diff \
  --config examples/marketmaestro/agentic-sdlc.toml \
  --paths-file changed-paths.txt \
  --added 120 \
  --deleted 14
```

## Onboard another repository

### Fast path: `sdlcctl onboard` (production profile)

`onboard` installs the shape the real consumer repos run — GitHub-hosted runners by
default (self-hosted only with `--ephemeral-runners`), preflight + failure-notify jobs,
`claude-ready` labels, optional provider routing — with the platform pinned to the current
`main` SHA, and (with `--apply`) configures the GitHub repo itself: labels, the "Protect main"
ruleset (review-thread resolution + required `test` check + `--required-approvals` approving
reviews) and repo variables. `doctor` then reports what
is still missing and which steps only the owner can do (secrets, GitHub App, runner).

```bash
# implementer = Claude Code cloud routine, CI on GitHub-hosted runners (no runner to register)
sdlcctl onboard \
  --destination /path/to/consumer-repository \
  --project-id owner/consumer-repository \
  --test "pytest tests -q" \
  --implementer cloud-routine --runs-on ubuntu-latest \
  --forbidden "data/**" --protected "app/server.py" \
  --apply

# implementer = GitHub Actions with provider routing on self-hosted runners (MusicMaestro shape);
# self-hosted labels need the owner's acknowledgement that the runners are ephemeral/contained
sdlcctl onboard --destination ... --project-id owner/repo --test "pytest -q" \
  --implementer route --runs-on self-hosted,linux,x64 --ephemeral-runners \
  --apply --copy-vars-from atulg4/MusicMaestro

sdlcctl doctor --destination /path/to/consumer-repository   # exit 2 until READY
```

Three owner choices are recorded in `agentic-sdlc.toml` and checked by `doctor`:

- **Runners** (`--runs-on`, default `ubuntu-latest`): the reusable implement/repair jobs apply a
  model-produced patch and run the repository's setup/quality/test commands on it, and `ci.yml`
  runs every pull request's code. On a persistent self-hosted runner that code could persist into
  later jobs holding model credentials and Publisher App secrets, so self-hosted labels are
  refused unless `--ephemeral-runners` acknowledges that each runner is ephemeral (one job, then
  discarded) or otherwise contained (`[automation] ephemeral_runners = true`); `doctor` warns
  about a self-hosted label without it. Hosted minutes cost money and a hosted VM cannot reach a
  private network: that trade-off is the owner's.
- **Required approvals** (`--required-approvals N`, `[policy] required_approvals`): the "Protect
  main" ruleset's approving-review count. Default in the Actions modes (`route`, `claude`,
  `codex`): 1 -- the Publisher App authors the PR, so the owner can approve it. In
  `cloud-routine` mode, where the routine authors it as the owner, with `--apply`: 1 when the
  repository has a collaborator with write/maintain/admin access other than the owner and the
  authenticated user, else 0 (GitHub never lets an author approve their own PR, so 1 would leave
  a solo owner unable to merge anything); offline, 0. At 0 the human-merge boundary is not enforced server-side --
  in `cloud-routine` mode the routine acts as the owner and could merge its own PR -- and both
  `onboard` and `doctor` say so loudly.
- **Executable trust mode** (`--trust-mode strict|local`, `[automation] trust_mode`, default
  strict, kept on `--force`): strict runs only a git, gh and hook interpreter that root alone can
  replace; local (a developer's own machine, e.g. Homebrew's Python 3.11+, gh and git on macOS)
  takes the first on PATH owned by root or the user and not group/other-writable, and keeps
  everything else (allowlisted environments, a fresh gh config, an absolute probed hook
  interpreter). GitHub Actions (`GITHUB_ACTIONS=true`) and Claude Code cloud sessions
  (`CLAUDE_CODE_REMOTE=true`) are strict whatever the policy says; `doctor` reports the mode.
  The publication-decision commands -- `check-diff`, `check-request`, `authorize-labeler`,
  `claim`, `renew`, `release` and `confirm-cloud-routine`, what the cloud routine and the
  Actions workflows run as gates -- are ALWAYS strict and ignore `AGENTIC_SDLC_GIT` /
  `AGENTIC_SDLC_GH`, whatever the policy or the environment says (the agent can edit a
  working-tree policy and unset `CLAUDE_CODE_REMOTE`). `local` only shapes the developer-facing
  commands (`onboard`, `doctor`, `inspect-diff`, `run-gates`, `claims`, ...) and the hooks.

**Migrating an onboarded repository:** re-run `sdlcctl onboard --force` (with `--apply` to update
the ruleset). A repository on self-hosted runners must add `--runs-on <labels>
--ephemeral-runners` (or move to hosted runners), and should pass `--required-approvals 1` when a
second reviewer exists. A policy written before these fields reads as strict, unacknowledged and
0 approvals.

`--implementer` is one of `route` (multi-provider, needs `.forge/` + DeepSeek secrets),
`claude`, `codex`, or `cloud-routine` (no Actions implementer; see the generated
`docs/forge/cloud-implementer.md` for the routine prompt, which authorizes the approval labeler
with `sdlcctl authorize-labeler` -- whoever applied each approval label last must have write
access -- before claiming, gates on `sdlcctl check-request` (valid request, dependencies
closed), renews its lease between stages (a renewal fails once the issue is closed) and
runs `sdlcctl check-diff --project` (the diff policy committed on the default branch -- whose
head must be the one GitHub reports -- over every commit it pushes, run with a trusted git),
and, LAST before pushing, ONE final gate -- `check-request --expect-digest <digest>
--authorize-labels` -- that judges a single snapshot of the issue (request unedited, still open,
dependencies still closed, each approval label last applied by a writer after the last edit)
and re-reads the issue, its history and every dependency to require them unchanged. A fresh
cloud environment has no `sdlcctl`: the document's "Environment setup script" installs it from
the platform repository at the workflows' pinned commit, and step 0 of the prompt stops the run
unless `sdlcctl --version` prints that commit. Every `gh` and git those gates run gets an
allowlisted environment (no `LD_*`/`DYLD_*`, a fixed PATH, a fresh empty `GH_CONFIG_DIR`, no
`SSL_CERT_FILE`/`SSL_CERT_DIR`; the token comes from `GH_TOKEN`/`GITHUB_TOKEN` or `gh auth
token`): an egress proxy set in `HTTPS_PROXY`/`HTTP_PROXY`/`NO_PROXY` is still used, but its CA
must be in the system trust store, not in an environment variable; once the routine is
scheduled on claude.ai,
record it with `sdlcctl confirm-cloud-routine` -- `doctor` stays NOT READY until then, under
`--local` too). Planning always runs Claude Code: every generated plan caller invokes the
platform's `reusable-plan.yml` (`anthropics/claude-code-action`), so the generated policy records
`[agents] planner = "claude"` and `doctor` fails a planner that does not match that workflow
(checked against the fetched `reusable-plan.yml` at the pin when online). Work requests must follow the
generated `.github/ISSUE_TEMPLATE/agent-work-request.md`: `## Summary`, `## Acceptance
Criteria`, `## Required Tests`, `## Non-Goals`, `## Dependencies`, the last two as lists.

### Issue leases: one agent per ticket

Actions jobs, cloud routines and interactive Claude Code sessions all coordinate through a
GitHub-native lease on the issue (`in-progress` label + assignee + a machine-readable
`<!-- forge-claim … expires=… -->` comment; default TTL 4 h via `[automation] lease_ttl_minutes`).
The marker records an `--assignee` and whether the lease added it (`owns_assignee=1`); release,
and a takeover of an expired lease, remove only an assignee the lease added, never one that was
already on the issue, and only when the live lease does not want the same login. A completed
cleanup is recorded (`<!-- forge-cleanup … -->`), and a retried release also skips a login that
was assigned again after the release, so a retry never undoes a later human assignment.
A marker counts only as the whole first line of a comment, alone; free text the tool adds
(`release --note`) has `<!--`/`-->` escaped. A session whose claim is queued behind a live
holder (a lost race whose retraction failed) can `release` it without touching the holder.

```bash
sdlcctl claim   --project owner/repo --issue 12 --agent claude-code --session "$SESSION" --branch forge/issue-12
sdlcctl renew   --project owner/repo --issue 12 --session "$SESSION"
sdlcctl release --project owner/repo --issue 12 --session "$SESSION"
sdlcctl claims  --project owner/repo          # live + expired leases
```

Each prints its result as one JSON document on stdout (or writes it to `--output`); the human
status line (`claimed #12 …`, the `claims` table, a refusal) goes to stderr, so
`sdlcctl claim … | jq .ok` always parses. GitHub logins compare case-insensitively everywhere
(assignees, marker authors, bot logins): `--assignee alice` on an issue assigned to `Alice` is
the pre-existing assignee, never one the lease owns.

`claim` exits 2, changing nothing, when another live lease or an open PR for the issue exists
(a PR of this repository that closes the issue or that comes from a same-repository branch
named `issue-N`; a fork's identically named branch does not count), or when the issue's timeline
links an open same-repository PR to it -- linked by hand in the Development sidebar (`connected`
and not `disconnected` since) or cross-referencing it -- or shows an open PR of ANOTHER
repository cross-referencing it whose `closingIssuesReferences` name this issue (`Fixes
owner/repo#N`; one that only mentions the issue does not block, and a Development-sidebar link
from another repository is not counted). Every open PR and every timeline page is
read to the last page; a page that cannot be read, a manual link whose PR cannot be
identified, or a cross-referencing PR of another repository that cannot be read (inaccessible,
deleted, API failure, more than 100 closing issues) refuses the claim rather than reading as
"no PR" -- the refusal names that PR; there is no override, so check it by hand and make it
readable to the `gh` login (or let the outage pass) before retrying.
Those PR signals are read again right after the claim marker wins arbitration (Codex
4241196645): a PR opened while the claim was being made -- or a re-read that fails -- retracts
the marker (a release marker) and the claim fails naming the PR. The lease's expiry is computed
from the clock immediately before its marker is posted, and arbitration re-reads the clock
(Codex 4241196647): a marker that has already expired by then (slow reads, a short
`lease_ttl_minutes`) is retracted and the claim fails, so a reported claim is always live.
A fresh claim that FAILS after posting its marker -- the post's answer lost, the arbitration
re-read, the PR re-check or the label/assignee bookkeeping failing -- retracts the marker before
the error is raised (Codex 4241327872), retrying the retraction up to 3 times; only a GitHub that
refuses all three leaves the marker live until its TTL (logged). A failed renewal is not
retracted: the session held the lease before it.
Expired leases can be taken over (the takeover is commented). Markers count only when posted by
a user whose repository permission (read per run from the collaborator permission API) is
write, maintain or admin, by `github-actions[bot]`, or by the Forge Publisher App's bot named
EXACTLY -- the policy's `[automation] publisher_bot_login` (`sdlcctl onboard
--publisher-bot-login <app-slug>[bot]`, which with `--apply` also sets the
`FORGE_LEASE_BOT_LOGINS` repository variable), or `FORGE_LEASE_BOT_LOGINS` in the environment,
which takes precedence. A lease command run with no policy file at hand (no `--config`, no
`./agentic-sdlc.toml`) and no variable reads the policy committed on the default branch through
the trusted `gh`; `claim` and `renew` exit 2 when it cannot be read, and `release` still
releases its own session's claims whoever posted them, so the failure-cleanup job's release in an
empty workspace frees the issue. With neither, no App bot's markers count (fail closed; in Actions mode
the Publisher App's own claims are then ignored, and doctor fails until the login is set);
`author_association` is not trusted (a read-only member comments as
MEMBER) and an unreadable permission is untrusted. An expiry is capped at post time + 7 days, so
an outside or read-only commenter cannot forge or free a lease; the commit guard applies the same
rule. `reusable-implement.yml` claims as the last
step of `prepare` (with an App-minted token: issues write, pull requests read) and releases after
the draft PR exists or on failure. `sdlcctl onboard` installs two Claude Code hooks in the consumer repo: a SessionStart
hook that fetches origin and lists PRs and leased issues (every open `in-progress` issue, read
page by page up to 5,000; an unreadable page or the cap is printed as an INCOMPLETE inventory,
never as the whole list), and a PreToolUse guard that blocks every git command except literal
read-only ones (deny by default) on `*/issue-N` branches unless the session holds the lease. The
guard also blocks when its hook payload cannot be read (not JSON, truncated, not an object, no
tool name, a Bash call without a string command); only a payload shown to be another tool passes
unread.

The hooks run a Python interpreter `onboard` vetted (`/usr/bin/python3`, else `/bin/python3`, or
`--hook-python`; in trust mode local also the first `python3` on PATH owned by you: a file only
root can replace -- in local mode, one owned by root or you, not group/other-writable -- that, run with `-I -c`, answers as CPython 3.11 or
newer -- a root-owned `/bin/true` would exit 0 without guarding) by absolute path, isolated (`-I`), under a fixed
PATH -- never `python3` looked up on the session's PATH, where a planted copy could stand in for
the guard -- and each handler pins its script's SHA-256, refusing (exit 2) to run a modified copy.
**Trust boundary:** these hooks are a guardrail against mistakes and casual misuse, not a
sandbox. Claude Code runs them as the same user as the agent, from the agent-writable checkout:
an agent that can write files can equally rewrite `.claude/settings.json` (handler and hash
together) or the hook script, and no local, same-user hook can prevent that. What enforces the
workflow is server-side: the `Protect main` ruleset, required review, and Publisher-only pushes.
Do not treat a local hook as the control that stops a determined agent.

### Generic profile: `sdlcctl scaffold`

The scaffold command refuses to overwrite existing files and requires an
immutable platform commit SHA. Level 1 installs manual plan-only automation.
Level 2 adds manually approved draft-PR generation and automatic independent
review. Level 3 adds label-driven plan and implementation triggers.

```bash
sdlcctl scaffold \
  --destination /path/to/consumer-repository \
  --provider github \
  --project-id owner/consumer-repository \
  --platform-repository atulg4/agentic-sdlc \
  --platform-ref 0123456789abcdef0123456789abcdef01234567 \
  --automation-level 1
```

On GitHub, levels 2 and 3 also need `--publisher-bot-login <app-slug>[bot]`: the
login of the Publisher App whose `PUBLISHER_APP_CLIENT_ID` / `PUBLISHER_APP_PRIVATE_KEY`
the implementation workflow uses. It is written to `[automation] publisher_bot_login`,
the only App bot whose issue-lease markers count; without it the scaffold refuses,
since every implementation run would otherwise fail at its claim.

The generated policy contains no guessed build or test commands. Draft-PR
automation therefore fails closed until the project owner adds and reviews its
deterministic verification commands.

Level 3 remains approval-driven: `agent-ready` starts planning and the separate
`implementation-approved` label starts implementation. The trigger actor must
have repository write authority, and every resulting pull request is a draft.

## Provider status

| Capability | GitHub | GitLab |
|---|---|---|
| Work-request validation | Ready | Ready |
| Read-only plan | Ready | Prompt artifact ready |
| Isolated patch generation | Ready | Planned |
| Fresh verification | Ready | Planned |
| Draft change request publisher | Ready | Planned |
| Independent review gate | Ready | Planned |

See [GitLab Adapter](docs/gitlab.md) for the deliberately limited v0.1
boundary.

See [Provider and model routing](docs/routing.md) for configurable executor
aliases, fallback semantics, and credential boundaries.

## Rollout

1. Publish this repository as `atulg4/agentic-sdlc`. Public is simplest; a
   private repository needs a dedicated framework-only read token.
2. Tag and pin the first reviewed release.
3. Install only the plan workflow in MarketMaestro.
4. Complete five successful plan runs.
5. Create and install the least-privilege publisher GitHub App.
6. Enable patch generation, still draft-PR only.
7. Repair MarketMaestro's deterministic test baseline.
8. Require CI and independent review before every merge.
9. Consider low-risk auto-merge only after a separate threat review and a
   substantial history of clean runs.

The phone-first operating model is documented in
[Phone Control](docs/phone-control.md).

The publisher App setup is documented in
[GitHub Publisher App](docs/github-publisher-app.md).

The exact MarketMaestro activation sequence is documented in
[MarketMaestro Rollout](docs/marketmaestro-rollout.md).
