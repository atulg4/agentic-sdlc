# Context packets

A **context packet** is the complete, budgeted model input for one factory
unit run: the approved task specification, the enforced policy limits, the
selected repository evidence, and — for reviewers — the verification evidence
bound to the exact candidate head. Packets are built by
`agentic_sdlc.context_builder` on top of the knowledge layer's
[context packs](knowledge.md), per the design in
`docs/design/forge-harness-plane.md` §6: the knowledge layer keeps its access
checks, stable record IDs, citations, and freshness behavior; the packet
layer adds roles, relevance ordering, budgeting, and dispatch blocking.

## Roles

A mission's packet role follows its declared capabilities, never its name:

| Role | Capabilities | Packet contents |
| --- | --- | --- |
| `builder` | any write capability beyond tests (`edit-code`, `edit-docs`, `repair`) | task sections, policy limits, repository evidence |
| `tester` | `author-tests` as the only write capability | task sections (frozen acceptance contract), policy limits, repository evidence |
| `reviewer` | any review capability | task acceptance criteria, policy limits, **exact-head verification evidence** (verified diff summary, gate report, prior findings), permitted evidence |

A mission that both writes and reviews has no packet role — one agent must
never review its own writes. Missions with neither capability set
(verifiers, planners, the context builder itself) are refused.

## What a packet contains

- **Task sections**: summary, acceptance criteria, required tests, non-goals,
  and dependencies from the parsed work request, rendered inside an
  `<untrusted-work-request>` boundary whose markers are stripped from the
  content to a fixpoint.
- **Policy limits**: changed-file/diff-line/patch-byte caps, forbidden and
  protected paths, and the mission's write paths and token/runtime limits.
- **Selected repository evidence**: knowledge-layer evidence records, each
  rendered with its citation inside an `<untrusted-evidence>` boundary.
- **Reviewer verification** (`VerificationEvidence`): the candidate's exact
  40-hex head SHA, the changed paths and line counts, the patch digest, the
  trusted gate report, and prior findings. Builders and testers must not
  receive it; reviews start from fresh context.

## Budgeting and ordering

Mandatory content — the task sections, policy limits, reviewer verification,
and **every record from a `required` knowledge source** — is never truncated:
when it cannot fit `budget_bytes` (default 256 KiB), the build blocks with a
reason instead. Optional evidence fills the remaining budget in deterministic
relevance order (keyword overlap with the task, ties broken by evidence ID);
records that do not fit are recorded as `omitted by budget` in the packet's
selection notes.

## Fail-closed exclusions and dispatch blocking

Evidence never enters a packet when its repository path matches the policy's
or the mission's forbidden paths, or when its payload carries a secret-like
value (key blocks, provider token shapes, quoted credential assignments).
Each exclusion is recorded on the packet.

`build_context_packet` raises `ContextBuilderError` with an actionable
reason — naming the source or evidence and the remedy — when:

- a `required` source has no admitted evidence (including when its evidence
  was excluded),
- required evidence is stale,
- retrieval of a required source failed,
- a reviewer packet lacks verification evidence,
- mandatory content cannot fit the budget,
- the task specification is not dispatchable (route it through the spec
  stage first).

High- and critical-risk missions additionally keep the knowledge layer's
stricter rule: any stale or missing evidence, required or not, refuses the
pack outright.

## Digest and the agent run record

`ContextPacket.packet_sha256` is deterministic over content-stable fields
(retrieval times never change it) and embeds the underlying knowledge pack's
`packSha256`. `dispatch_envelope_for_packet` pins it into the dispatch
envelope as `contextPackDigest` and adds `sha256:<packSha256>` to
`inputRefs`, so the orchestrator's agent run record carries the exact packet
content every run received.

## CLI

```sh
sdlcctl context-packet \
  --config agentic-sdlc.toml \
  --knowledge knowledge.toml \
  --request request.json \
  --output packet.json
```

`request.json` holds `missionId`, `workRef`, the `task` (`title`, `body`,
optional `labels`), `records` (evidence documents as the knowledge layer
serializes them), optional `verification`, optional `budgetBytes`, and
optional `retrievalFailures`. A blocked dispatch exits `2` and prints the
actionable reason on stderr.
