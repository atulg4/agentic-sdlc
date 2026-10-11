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

`snapshotSha256` digests the content and excludes `generatedAt`, so two
snapshots of an unchanged factory compare equal.

### Health is derived, never declared

`health` is computed in a fixed order: `unregistered` → `blocked` (a blocker
incident) → `needs-attention` (a unit nobody is moving) → `degraded` (a
non-blocking incident) → `running` → `idle` → `no-data`. Nothing writes it.

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
| `dependency-wait` | an upstream unit has not finished |
| `awaiting-human-approval` | the plan needs a person |
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
gh api 'repos/OWNER/REPO/issues?state=all&per_page=100' > issues.json
gh api 'repos/OWNER/REPO/pulls?state=open' > pulls.json
gh api 'repos/OWNER/REPO/issues/109/sub_issues' > sub-109.json

sdlcctl github-backlog \
  --issues issues.json --pulls pulls.json --sub-issues sub-109.json \
  --failing 148=test \
  --project-id forge --repository OWNER/REPO \
  --backlog-output backlog.json --incidents-output incidents.json
```

Fetching stays outside the library: the operator's own client supplies the
credential, and the library only ever sees already-fetched JSON.

Three published tables drive every inference, so none of it is a guess:

- `LABEL_STATES` — the label set that declares each factory state.
- `CLOSED_STATES` — GitHub's close reason mapped to a terminal state. *Closed
  as completed* means completed; *closed as not planned* means cancelled.
- `DISPATCH_RULES` — the label preconditions the bundled `agent-auto-plan` and
  `agent-auto-implement` triggers enforce. Each unmet row becomes an incident,
  which is how "approved, but nothing is running it" becomes visible.

Parent links come from GitHub's `parent_issue_url` on each sub-issue, never
from a title or a checklist. An epic is recognised from the title convention or
from owning sub-issues.

## What the Control Center deliberately cannot do

The read model is read-only by construction, and a console built on it inherits
that. It ships no credential to a browser, and it offers no action that writes
to a repository, a tracker or a runner. A remediation descriptor *describes* the
step and names who may take it; taking it stays with that authority. Mutating
the control plane is a separately reviewed change, not a side effect of
rendering a page.
