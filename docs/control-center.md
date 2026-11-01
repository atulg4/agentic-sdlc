# Forge Control Center

The Control Center answers one question for every project Forge manages:
**what is happening right now, and what is stopping it?**

It is a *view*. It re-derives nothing from a provider API, holds no credential,
and mutates nothing. One schema-versioned snapshot document is built by
`sdlcctl control-center`, and any surface — a web console, a phone screen, a
terminal — renders that document.

```
project registry ─┐
lifecycle ledger ─┼─► sdlcctl control-center ─► snapshot.json ─► any console
backlog document ─┤
incident document ┘
```

## What the snapshot contains

| Section | Answers |
|---|---|
| `projects[].counts` | how much work exists, by factory state and stage |
| `projects[].active` | the current task and the current activity, per work unit |
| `projects[].attention` | which units stopped, and whether a human gate stopped them |
| `projects[].backlog` | the epic → story → task tree, each node with its state |
| `projects[].incidents` | what is failing, with declared remediations attached |
| `projects[].health` | one word, derived only from the above |
| `warnings` | data that did not fit the model, never silently dropped |

`snapshotSha256` digests the content and excludes the run metadata —
`generatedAt` and each incident's `observedAt` — so two snapshots of an
unchanged factory compare equal even when the exporter stamped its incidents
with the time it happened to run.

### Health is derived, never declared

`health` is computed in a fixed order: `unregistered` → `blocked` (a blocker
incident) → `needs-attention` (a unit nobody is moving) → `degraded` (a
non-blocking incident) → `running` → `waiting` (no active work, but units
held in a dependency wait — the project sits still for a reason, not for
lack of work) → `idle` → `no-data`. Nothing writes it.

### Unknown is never fabricated

Each backlog node reports a `state` *and* a `stateBasis`:

- `state projected from the lifecycle ledger` — the node names a work unit the
  ledger has events for. This always wins.
- `state declared by the backlog source` — no ledger events, but the tracker
  declared a state.
- `state unknown: no ledger events and no declared state` — the console shows
  *unknown*. It does not show `intake`.

### Nothing is dropped

Data arriving for a project the registry does not declare still appears, with
`registered: false` and a warning. An operator cannot remediate what the
console hides.

## Incidents and remediations

An `Incident` is one observed execution problem with a `kind` from a closed
set. Each kind maps to declared `RemediationAction` descriptors in
`REMEDIATION_CATALOGUE`:

| Kind | Typical cause |
|---|---|
| `dispatch-blocked` | approved work whose dispatch preconditions are unmet |
| `runner-capacity` | queued work with no worker to run it |
| `quota-exhausted` | the executor's plan window is spent |
| `deterministic-ci-failed` | the deterministic gate rejected the change |
| `review-gate-failed` | the independent reviewer withheld approval |
| `merge-conflict` | the change request conflicts with its base |
| `branch-behind` | the change request head is out of date with its base |
| `dependency-wait` | an upstream unit has not finished |
| `awaiting-dispatch-evidence` | every label is present; confirm the trigger actually fired |
| `awaiting-spec-review` | a drafted specification waits for its human release |
| `awaiting-human-approval` | the plan needs a person |
| `triage-needed` | no lifecycle label owns the item yet |
| `awaiting-human-merge` | merge authority is human, by design |
| `infrastructure-failure` | the failure is the runner, not the change |
| `unclassified` | no declared remediation; classify it first |

Every action carries an `authority`:

- `operator` — a command an operator runs.
- `agent` — work an agent may take on.
- `human-only` — a step no agent may take, and which **must** name a `handoff`.

`awaiting-human-merge` is human-only in every row, so no console can present
merging as automatable. A kind with no catalogue entry falls back to
"classify the incident" rather than to a guess.

## Building a snapshot

```bash
sdlcctl control-center \
  --registry examples/control-center/project-registry.json \
  --ledger event-ledger.json \
  --backlog examples/control-center/backlog.json \
  --incidents examples/control-center/incidents.json \
  --output snapshot.json
```

Every input is optional. With none, the snapshot is empty rather than wrong.
`--project-id` may be repeated to narrow the snapshot, and naming a project the
inputs never mention is an error, not an empty page.

## The GitHub adapter

`control_center` knows nothing about any tracker. `github_backlog` is one
adapter that turns GitHub payloads into the backlog and incident documents:

```bash
# fail closed: any failed request aborts before a partial snapshot is built
set -euo pipefail

# paginate: one page caps issues at 100 and pulls at 30
gh api --paginate 'repos/OWNER/REPO/issues?state=all&per_page=100' \
  --jq '.[]' | jq -s '.' > issues.json

# the pulls LIST response omits mergeable_state: fetch each pull's detail
gh api --paginate 'repos/OWNER/REPO/pulls?state=open&per_page=100' --jq '.[].number' |
  while read n; do gh api "repos/OWNER/REPO/pulls/$n" || exit 1; done |
  jq -s '.' > pulls.json

gh api --paginate 'repos/OWNER/REPO/issues/109/sub_issues?per_page=100' \
  --jq '.[]' | jq -s '.' > sub-109.json

sdlcctl github-backlog \
  --issues issues.json --pulls pulls.json --sub-issues 109=sub-109.json \
  --failing 148=test \
  --config agentic-sdlc.toml \
  --project-id forge --repository OWNER/REPO \
  --backlog-output backlog.json --incidents-output incidents.json
```

`--config` points at the project's `agentic-sdlc.toml`, whose `ready_label`,
`human_review_label` and `implementation_label` rename the automation labels
the generated workflows fire on; the adapter then evaluates those names.
Without it, the platform defaults (`agent-ready`, `human-review-required`,
`implementation-approved`) apply. The spec labels are platform-fixed.

A computed incident always needs a destination: the export refuses without
`--incidents-output` whenever `--pulls`/`--failing` request change-request
classification OR the issues payload itself yields incidents (an untriaged
open issue, an approved issue missing its dispatch labels) — silently
dropping the generated incident document would leave those blockers absent
from the next snapshot. Rejected URL values (a malformed `repository_url`,
`html_url` or `parent_issue_url`) are withheld from error messages when they
carry userinfo or token-shaped text, since those messages reach stderr and
CI logs.

A change request without a mergeability verdict in its payload is never
reported as "ready and waiting": unknown is not a state, so feed the command
detail payloads, not the list response. `--sub-issues` prefers the
`PARENT=path` form, which does not depend on each child carrying its own
parent reference; a bare path still works when the payload carries
`parent_issue_url`, and a child whose reference disagrees with the declared
parent is rejected rather than silently relinked. A supplied listing must
be COMPLETE: a parent whose declared `sub_issues_summary.total` exceeds the
children the listing maps — an explicitly supplied empty listing included —
fails the export closed, so an unpaginated fetch cannot silently drop the
hierarchy.

Fetching stays outside the library: the operator's own client supplies the
credential, and the library only ever sees already-fetched JSON.

Three published tables drive every inference, so none of it is a guess:

- `LABEL_STATES` — the label set that declares each factory state.
- `CLOSED_STATES` — GitHub's close reason mapped to a terminal state. *Closed
  as completed* means completed; *closed as not planned* means cancelled.
- `DISPATCH_RULES` — the label preconditions the bundled `agent-auto-plan` and
  `agent-auto-implement` triggers enforce. Each unmet row becomes an incident,
  which is how "approved, but nothing is running it" becomes visible.

Parent links come from the declared `PARENT=path` pairing or from GitHub's
`parent_issue_url` on each sub-issue — never from a title or a checklist. An
epic is recognised from the title convention or from owning sub-issues.

## The console: mobile-first, chart-first

`examples/control-center/control-center.html` is a single file with no
framework and no build step. Below 1024px it renders a **phone shell** built
from its own components; at and above it, a **dense analytical view**. These
are two component trees, not one layout at two widths: a backlog that reads
well under a thumb and a table that reads well on a monitor are different
designs, and shrinking either into the other produces neither.

### Navigation

Phone widths get a bottom bar with five destinations — Projects, Backlog,
Agents, Cost, Effectiveness — and every touch target is at least 44×44 CSS px.
The views that are not destinations (Blockers, Add project, Snapshot) open as
an overlay, reached from buttons on the Projects destination: their desktop
tabs are hidden at this width, so without those the console would lose project
registration and the whole blocker remediation workflow on a phone. The
desktop tab bar keeps every view it had, plus **All units** for the dense
table.

### What a backlog card shows

Stage, state, blocker, model, worker, time in the current state, the change
request, and a progress indicator that is a *position on the lifecycle rail* —
"stage 6 of 14 · implementation". A unit in an off-rail state (blocked,
failed, rolled back, cancelled, superseded) says so instead of being given a
fraction. Every missing value reads `unknown`; none reads `0`.

Tapping a card opens a drill-down: the lifecycle timeline, time in the current
state, worker and model, token and cost estimate versus actual, dependencies,
and artifacts.

The timeline marks **only the current stage**. A stage earlier on the rail is
labelled as exactly that, never as *passed*: the forward path has legal
shortcuts — independent review straight to ready-for-human-merge, with no
repair and no re-review; dependency-wait skipped entirely — and the snapshot
carries the current stage rather than the transition history, so a position on
a display rail cannot establish that a stage happened. Per-stage durations are
reported **unavailable** for the same reason. `sdlcctl project-state` reads
that history from the event ledger.

### Charts

Inline SVG, drawn before the prose that explains them, and each one is a
single accessible image: `role="img"` with an `aria-label` that states the
finding in words. Every segment is focusable, opens its tooltip on focus as
well as hover, and carries its figure as text, so nothing is conveyed by
colour or position alone.

| View | Chart | Source |
|---|---|---|
| Projects, Backlog | lifecycle rail / funnel | the snapshot's stage census |
| Agents | active agents by model, by worker | the snapshot's **active** collection |
| Agents | held work by why it is held, repair loops | the snapshot's work units |
| Agents | queue and parallelism, model usage | `sdlcctl capacity-report` |
| Cost | tokens and pay-as-you-go equivalent by model | `sdlcctl usage-report --group-by model` |
| Cost | daily cost and runtime trend | `sdlcctl usage-report --group-by day` |
| Effectiveness | outcome quality, accepted outcomes | `sdlcctl effectiveness-report` |

A chart whose source document was not loaded reads **unavailable** and names
the command that produces it. A figure the producer reported as unobserved
(`value: null`) stays `unknown` everywhere it is written — the bar, the visible
label and the accessible summary — and is left out of the chart's scale, so an
unmeasured cost is never announced as zero dollars.

**Active agents means executing.** Those two charts read the snapshot's
`active` collection only. A blocked, failed, human-gated or dependency-waiting
unit still names the model and worker that last touched it, and counting those
would report execution that is not happening; held work gets its own chart
instead, grouped by why it is held.

A daily trend never bridges a day nobody observed. The usage report emits a
bucket only for a day that carried records, so a quiet day is *absent* rather
than zero — plotting the survivors side by side would hide exactly the quiet
day a reader is looking for. Every calendar day between the first and last
observation therefore gets a place on the axis, absent ones marked `?`, and the
line breaks across them. A range longer than 400 days reads unavailable and
says to narrow it with `--since`/`--until`.

### The telemetry bundle

The console computes no telemetry. The Cost, Agents and Effectiveness views
render documents the CLI already emits, pasted together as one optional
bundle:

```json
{ "schemaVersion": 1,
  "byModel":    "<sdlcctl usage-report --group-by model>",
  "byDay":      "<sdlcctl usage-report --group-by day>",
  "byWorkUnit": "<sdlcctl usage-report --group-by project --group-by workUnit>",
  "capacity":   "<sdlcctl capacity-report ...>",
  "effectiveness": "<sdlcctl effectiveness-report ...>" }
```

Every section is optional. The bundle is validated fail-closed on the way in —
an unsupported version, an unknown section, an over-budget collection, a
credential-shaped value or a secret-shaped key is refused rather than rendered
— and it is never written to the shared store: it is a per-viewer analysis
aid, and it can be far larger than one snapshot. `tokens`, `totalTokens` and
`cacheReadTokens` are the usage ledger's own figures and are *not* treated as
credentials; `token`, `apiToken` and their kin are.

### Live updates

Deliveries patch the DOM rather than replacing the view. Each section carries
a key and a signature of the data it renders; a section whose signature is
unchanged is not rebuilt, and a work-unit card is not even *constructed*. So
the scroll position, the focused control and an open drill-down all survive an
update.

Deliveries are **coalesced**: one verification runs at a time and the newest
pending document wins. Verifying a document means digesting all of it, and a
burst of deliveries is a burst of documents that supersede each other.
Whatever is finally adopted is verified in full — structural validation and
the `snapshotSha256` digest.

Each delivery claims its adoption generation **when it arrives**, not when the
drain reaches it. Stamping it at processing time would make a document that had
been waiting *newer* than a manual load that adopted while it waited, and the
older document would win.

A view's signature covers everything it renders, including the contents of the
store-backed note and draft collections. A note reworded or a draft replaced
leaves the collection the same size, so a signature built from counts alone
would keep the stale text on screen for good.

**"Live" means a feed is established** — a store is present *and* has delivered.
A document pasted into a tab with no store is labelled a session-local document
that is not receiving updates, however fresh its own `generatedAt` reads.

When the feed stops or goes quiet for 90 seconds, the console **re-reads a
full snapshot by itself**. It cannot know what it missed, so it does not try
to catch up from deltas. Until the replacement verifies, the last-good view
stays on screen labelled with the instant it was good, so a degraded console
never passes for a live one.

A unit list is bounded: 50 cards on a phone, 200 rows on the desktop, ordered
attention-first, with a control that says how many of how many are showing. A
phone that lays out five hundred cards spends its frame budget on rows nobody
scrolled to, and every later delivery pays that cost again.

### Motion

Only `opacity` and `transform` are animated — the two properties a compositor
can run off the main thread — and no state change is conveyed by motion alone.
`@media (prefers-reduced-motion: reduce)` disables every transition and
animation in the page.

### Performance budgets

Measured by the browser suite against the example fixtures, and enforced by it:

| Budget | Limit |
|---|---|
| Initial render of a 500-unit snapshot (validated, digest-verified, rendered) | **1000** ms |
| Longest single task on the main thread | **400** ms |
| A burst of 100 live deliveries, applied | **6000** ms |
| Mean latency of one delivery in that burst | **60** ms |
| DOM elements one single-unit change may insert | **40** |

The suite also reports each delivery's phases — validate, verify, adopt,
render — so a regression says *which* phase got slower rather than only that
something did.

### Testing the console

`examples/control-center/e2e/console.e2e.mjs` drives the real page in bundled
Chromium against the fixtures in `examples/control-center/`, through a fake
store injected before the console's own module runs. It covers the phone
viewports (360×780, 390×844, 412×915), the five destinations, touch-target
sizes, an accessibility audit, the drill-down, duplicate and single-unit
deliveries, feed recovery, reduced motion, credential refusal, the performance
budgets, and the dense desktop view.

```sh
node examples/control-center/e2e/console.e2e.mjs
```

`tests/test_control_center_console.py` runs it, and **skips** when Node or
Playwright is absent: this project ships no JavaScript dependency and its CI
image carries no browser, so a browser check cannot be a required one. The
same file also pins the console's structural contracts — the rail matching the
producer's own lifecycle stages, the budgets matching this document, the
credential rules — and those run everywhere.

## What the Control Center deliberately cannot do

The read model is read-only by construction, and a console built on it inherits
that. It ships no credential to a browser, and it offers no action that writes
to a repository, a tracker or a runner. Backlog URLs are refused at load when
they embed a credential — URL userinfo, or a token-shaped value (`ghp_…`,
`github_pat_…`, `glpat-…`) anywhere in the URL, query string and fragment
included, percent-encoded or not — and ledger references (`issueRef`,
`changeRequestRef`, `runRef`) carrying the same shapes are redacted, while
an incident `evidenceRef` or `backlogItemId` carrying them is refused at
load, so the snapshot never republishes a secret as a clickable link or a
rendered chip; the console re-checks the same rules on every document it
renders, peeling percent-escapes individually so one malformed escape never
hides a valid one beside it. A ledger project identifier carrying NUL — the
console's identity-key delimiter — fails the build closed, since the
bundled viewer would refuse the otherwise valid snapshot. A remediation descriptor *describes* the
step and names who may take it; taking it stays with that authority. Mutating
the control plane is a separately reviewed change, not a side effect of
rendering a page.
