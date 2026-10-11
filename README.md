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

`onboard` installs the shape the real consumer repos run — self-hosted or hosted
runners, preflight + failure-notify jobs, `claude-ready` labels, optional provider
routing — with the platform pinned to the current `main` SHA, and (with `--apply`)
configures the GitHub repo itself: labels, the "Protect main" ruleset (review-thread
resolution + required `test` check) and repo variables. `doctor` then reports what
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

# implementer = GitHub Actions with provider routing on self-hosted runners (MusicMaestro shape)
sdlcctl onboard --destination ... --project-id owner/repo --test "pytest -q" \
  --implementer route --apply --copy-vars-from atulg4/MusicMaestro

sdlcctl doctor --destination /path/to/consumer-repository   # exit 2 until READY
```

`--implementer` is one of `route` (multi-provider, needs `.forge/` + DeepSeek secrets),
`claude`, `codex`, or `cloud-routine` (no Actions implementer; see the generated
`docs/forge/cloud-implementer.md` for the routine prompt, which authorizes the approval labeler
with `sdlcctl authorize-labeler` -- whoever applied each approval label last must have write
access -- before claiming; once the routine is scheduled on claude.ai,
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
Expired leases can be taken over (the takeover is commented). Markers count only when posted by
a user whose repository permission (read per run from the collaborator permission API) is
write, maintain or admin, by `github-actions[bot]`, or by the Forge Publisher App's bot (a
`<slug>[bot]` whose slug contains `agentic-sdlc`, or exactly the logins in
`FORGE_LEASE_BOT_LOGINS`); `author_association` is not trusted (a read-only member comments as
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
