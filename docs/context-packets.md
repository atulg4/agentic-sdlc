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
  rendered with its citation collapsed to one line INSIDE the
  `<untrusted-evidence>` boundary — locator and revision are
  retrieval-controlled text, so metadata never holds a privileged prompt
  position.
- **Reviewer verification** (`VerificationEvidence`): the candidate's exact
  40- or 64-hex head SHA, the changed paths and line counts, the patch
  digest, the complete `run_gates` report (exactly the keys `run_gates`
  emits, top level and per entry — a nonempty gates array whose entries each
  carry name, command, exitCode, durationSeconds and a boolean verdict, with
  an agreeing aggregate), and prior findings, which fail closed when they
  carry secret-like content. Changed paths render inside an untrusted
  boundary — filenames are patch data the patch author controls. Builders
  and testers must not receive it; reviews start from fresh context.

## Budgeting and ordering

Mandatory content — the task sections, policy limits, reviewer verification,
and **every record from a `required` knowledge source** — is never truncated:
when it cannot fit `budget_bytes` (default 256 KiB), the build blocks with a
reason instead. Optional evidence fills the remaining budget in deterministic
relevance order (keyword overlap with the task, ties broken by evidence ID);
records that do not fit are recorded as `omitted by budget` in the packet's
selection notes. The pack's qualification notices count against the budget
too: `renderedBytes` reports the real rendered prompt size, and a packet
whose notices push it past the budget blocks rather than under-reporting.
The knowledge layer's record cap is a second selection budget: mandatory
records past it block the build, and excess optional records are recorded
as `omitted by record limit`. For high- and critical-risk missions,
staleness is judged over everything admitted — before budget selection —
so a stale record cannot slip out of sight by being omitted for size.

## Fail-closed exclusions and dispatch blocking

Evidence never enters a packet when its repository path matches the policy's
or the mission's forbidden paths or falls outside the mission's `read_paths` (matched case-sensitively — casefolding an allowlist would widen it, while the deny patterns stay casefolded),
or when its rendered block — the payload and its citation metadata (locator,
revision) alike — carries a secret-like value (key blocks, provider token
shapes, quoted credential assignments). A record whose `sourceType` disagrees
with its declared source is rejected outright; identical duplicate records
are counted once, matching the knowledge layer, while records that share an
evidence ID but differ in other fields fail closed, so the prompt never
depends on input order. Each exclusion is recorded on the packet, and the
pack's own qualifications (stale retentions, detected contradictions) are
rendered into the prompt with the evidence they qualify — each notice
single-lined, secret-scanned (a credential-bearing retrieval-failure detail
is withheld), and wrapped inside an untrusted boundary, since failure details
quote retrieval-controlled text. The secret scan walks every RAW serialized
record field — the packet artifact carries the whole record, author, scope,
anchors and link targets included — alongside the rendered block, so JSON
escaping cannot defeat the patterns. A
secret-like retrieval-failure detail is
redacted before the pack handoff — keeping its prefix only when it names a
declared source — so neither the serialized packet nor a blocking error can
echo it. A repository record whose path is not the
canonical relative form Git stores (traversal segments, absolute prefixes,
backslashes) fails closed before any pattern is matched. Retrieval failures naming a source the mission may not read
are filtered from the knowledge-pack handoff for the same reason the source
itself is.

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

A required source the mission may not read — its `permitted_missions` or the
mission's explicit `knowledge_sources` exclude it — is not demanded of that
mission, since supplying it would be rejected by the same access policy.

High- and critical-risk missions additionally keep the knowledge layer's
stricter rule: any stale or missing evidence, required or not, refuses the
pack outright.

## Digest and the agent run record

`ContextPacket.packet_sha256` is deterministic over content-stable fields
(retrieval times never change it), embeds the underlying knowledge pack's
`packSha256`, and binds the SHA-256 of the fully rendered prompt, so two
packets never share a digest while sending the model different text (a
changed citation locator changes the digest). `dispatch_envelope_for_packet` pins it into the dispatch
envelope as `contextPackDigest` and adds `sha256:<packSha256>` to
`inputRefs`, so the orchestrator's agent run record carries the exact packet
content every run received. The envelope also verifies the full mission
contract — role, risk level, write/read/forbidden paths, token and runtime
limits —
against the packet's bound limits, so a mission edited without a version
bump cannot dispatch a prompt that still encodes the old policy.

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
