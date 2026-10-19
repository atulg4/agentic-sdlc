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
  40- or 64-hex head SHA and the trusted review base SHA it was diffed
  against (required and distinct — the head alone does not identify a
  diff), the changed paths and line counts, the patch digest, the complete `run_gates` report (exactly the keys `run_gates`
  emits, top level and per entry — a nonempty gates array whose entries each
  carry name, command, exitCode, durationSeconds and a boolean verdict, with
  an agreeing aggregate), and prior findings, which fail closed when they
  carry secret-like content — as does a gate report whose command or name
  quotes a credential, since the report renders beneath the trusted-pipeline
  heading, and a changed path whose filename is itself token-shaped. The
  validated gate report is detached from the caller's object and stored
  recursively immutable, so it cannot be edited in place after validation;
  the packet's policy limits and the verification's path and finding
  sequences are frozen the same way. For
  reviewer packets the pinned `repository_revision` must equal the verified
  candidate head, so the reviewer never reads source from one tree while
  judging the exact diff of another. Changed paths render inside an untrusted
  boundary — filenames are patch data the patch author controls — with
  angle brackets escaped in their JSON form, so a filename spelling a
  boundary marker reaches the reviewer exactly instead of being stripped.
  Builders and testers must not receive it; reviews start from fresh
  context.

## Budgeting and ordering

Mandatory content — the task sections, policy limits, reviewer verification,
and **every record from a `required` knowledge source** — is never truncated:
when it cannot fit `budget_bytes` (default 256 KiB), the build blocks with a
reason instead. The prompt is also held under the mission's own token
budget: counted at the provably conservative one-token-per-UTF-8-byte
ceiling (a BPE token encodes at least one byte, for any tokenizer) with a
256-token output reserve, mandatory content past `max_tokens` blocks, and
optional selection stops at whichever budget binds first, so the envelope
never advertises a limit the packet already exceeds. Optional evidence fills the remaining budget in deterministic
relevance order (keyword overlap with the task, ties broken by evidence ID);
records that do not fit are recorded as `omitted by budget` in the packet's
selection notes. The pack's qualification notices count against the budget
too: `renderedBytes` reports the real rendered prompt size, and when the
notices push the rendered packet past a budget the least-relevant selected
optional records are evicted (recorded in the selection notes) until it
fits — the build blocks only when no optional selection remains to give
back.
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
shapes, quoted credential assignments) — scanned both raw and after
boundary-marker removal, so an embedded marker cannot split a credential
past the scan only for sanitization to reassemble it. A record whose `sourceType`, `scope`, or
`classification` disagrees with its declared source is rejected outright —
adapter-controlled fields must agree with the source whose permissions the
record rides; identical duplicate records
are counted once, matching the knowledge layer, while records that share an
evidence ID but differ in other fields fail closed, so the prompt never
depends on input order. Each exclusion is recorded on the packet, and the
pack's own qualifications (stale retentions, detected contradictions) are
rendered into the prompt with the evidence they qualify — each notice
single-lined, secret-scanned (a credential-bearing retrieval-failure detail
is withheld, and a non-printable one travels JSON-escaped on one line, so
request-controlled failure text cannot inject log lines into the pack
qualifications or the blocking errors printed to stderr), and wrapped
inside an untrusted boundary, since failure details
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
  stage first) or carries a secret-like value in any of its fields — task
  text reaches the prompt and the serialized artifact exactly like evidence
  payloads, so it fails closed on the same token shapes,
- repository evidence is packed without a pinned `repository_revision`, or a
  repository record was retrieved at a different revision — all repository
  evidence must come from one trusted pinned tree, never a mix of commits,
- `work_ref` does not identify a request within the configured project
  (`projectId#N`, the same binding the harness manifest enforces),
- the policy limits themselves carry a secret-like value (a
  credential-shaped path pattern in the protected configuration) — they
  render outside the untrusted boundary, so they fail closed like every
  other packet string.

Evidence documents reconstructed by `record_from_dict` must be UTF-8
encodable in every string field: a surrogate-escaped value (a non-UTF-8
Git filename, say) fails closed with a remedy instead of raising an
uncaught encoding error when the packet is rendered.

Exclusion notes are serialized by `as_dict` and echoed in blocking errors,
so a forbidden or out-of-scope filename that is itself token-shaped is
withheld from the exclusion text rather than quoted, and one carrying a
newline, carriage return, or other non-printable character travels
JSON-escaped on a single line — a candidate-controlled filename must not
inject extra log lines or terminal control sequences into the stderr and CI
output that echo these errors.

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
contract — role, risk level, write/read/forbidden paths, the knowledge-source
allowlist, token and runtime limits —
against the packet's bound limits, so a mission edited without a version
bump cannot dispatch a prompt that still encodes the old policy or reuse
evidence the mission may no longer read. The bound limits carry the FULL
execution contract — purpose, success criteria, output artifacts, and the
command allowlist render into the prompt's policy section, and the complete
canonical mission document (retries, approval, capabilities, inputs,
evidence, concurrency, independence included) travels in the digest and is
compared at dispatch — so the agent is told its mission-specific contract
(not generic role text) and a same-version edit to any execution-relevant
field refuses dispatch of the stale packet.

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
serializes them), `repositoryRevision` (the trusted checkout revision —
required whenever `records` carry repository evidence), optional
`verification`, optional `budgetBytes`, and optional `retrievalFailures`.
A blocked dispatch exits `2` and prints the actionable reason on stderr.

Dispatching goes through the packet contract, never around it:

```sh
sdlcctl dispatch-packet \
  --config agentic-sdlc.toml \
  --knowledge knowledge.toml \
  --request request.json \
  --agents agents.json \
  --packet packet.json \
  --output envelope.json
```

`dispatch-packet` rebuilds and re-validates the packet from the same request
inputs — evidence authorization, the reviewer-verification requirement, and
the budget checks all run again — selects the agent, and emits the envelope
via `dispatch_envelope_for_packet`
with `contextPackDigest` bound to the packet digest and the rendered packet
as the dispatched prompt. Reviewer verification on the dispatch path is
DERIVED, never declared: with `--candidate-repository`, `--candidate-base`
and `--candidate-head`, the CLI resolves both commits in that repository,
computes the diff summary with `collect_commit_diff`, runs the
gates itself (`run_gates`, with wall-clock durations zeroed so derived
packets are deterministic end to end) inside a disposable PRISTINE CLONE
checked out detached at the candidate head: its brand-new configuration
defines no filter drivers and no hooks (so the smudge filters
repository-local config can define for a committed `.gitattributes` never
run), every committed entry — symlinks included — materializes exactly as a
real checkout would, and the clone carries full Git metadata and origin
refs for repository-aware gates. The caller's own checkout is never
executed, so uncommitted edits, ignored files, status-suppressing
repository config, locally configured filters, and the caches one gate run
leaves behind can neither block a derivation nor reach a gate. The gates
run under the gate configuration AS COMMITTED at the AUTHENTICATED REVIEW
BASE (`--config` must resolve inside the candidate repository and be
committed at the base): the configuration committed at the head is itself
part of the untrusted candidate, so a candidate commit that replaces the
configured gates with `true` never records a passing report under its own
weakened rules — and a head that renames the policy's default branch (which
would re-aim base authentication) refuses derivation outright. The gates
run with `GITHUB_WORKSPACE` repointed at the disposable tree and the standard
revision variables rebound (`GITHUB_SHA` to the candidate head,
`GITHUB_BASE_REF` to the default BRANCH NAME — keeping Actions' semantics
for gates diffing `origin/$GITHUB_BASE_REF...` — with the resolved base SHA
in `AGENTIC_SDLC_BASE_SHA`; `GITHUB_REF`/`GITHUB_HEAD_REF` cleared), so an
Actions-style gate can neither read the caller's checkout nor test the
ambient workflow's revision. Because candidate gates are
candidate-controlled COMMANDS — run bare they execute as the caller's OS
user, and a disposable clone restricts nothing outside itself —
`--gate-sandbox` is required: either an isolation wrapper command prefix
(a container or sandbox launcher, prepended to every gate's argv and
visible in the recorded report — and to the interpreter VERSION PROBE,
which executes whatever `python3`/`python` the gate PATH resolves first,
on a runner with a repository-local virtualenv a candidate-writable
program) or the literal `trusted-host` to explicitly
accept host execution for a candidate the caller already trusts. The clone
also mirrors the
source's remote-tracking and tag refs, since CI checkouts often carry the
candidate only on `origin/*`, and then disconnects its `origin` remote, so
a gate — or candidate code a gate runs — cannot push to or edit the
caller's real repository, and its `origin/<default branch>` ref is PINNED
to the authenticated base rather than imported from the candidate — a
producer that moved its own remote-tracking ref to the head could otherwise
show a repository-aware gate an empty `origin/$GITHUB_BASE_REF...HEAD`
diff. The `--candidate-base` is authenticated OUTSIDE
the candidate repository: `--review-target-repository` names the caller's
separately maintained repository, and the base must equal its current
`origin/<default branch>` — a ref inside the candidate repository
(remote-tracking refs included) is mutable by the producing run itself via
`git update-ref`, so it authenticates nothing, and a caller-chosen later
ancestor cannot hide already-landed changes from the verified diff. The
target must also BE separate, not merely spelled differently: canonical
object-store identities (the realpath of each repository's common git
directory) are compared, so the candidate itself, a symlink alias of it, a
worktree sharing its store, or a repository nested inside the candidate's
tree all refuse — and containment refuses in BOTH directions, since a
candidate checkout nested inside the target's worktree could reach the
parent's `.git` and move its refs. The evidence patch carries the
PER-COMMIT SERIES (`git log --no-show-signature --patch` over `base..head`,
so a candidate-configured `log.showSignature`/`gpg.program` pair can never
execute a verifier before the sandbox): a commit that adds
content a later commit removes leaves the endpoint tree diff empty while a
push of the head still publishes the blob, so the reviewer reads every
intermediate change, the endpoint and whole-series line counters both
travel in the evidence (`historyAddedLines`/`historyDeletedLines`), and
`patchSha256` binds the series bytes. The PACKET configuration — the policy AND the
mission registry and knowledge sources — is read from the same committed
tree on every candidate path (paths judged lexically with relative values
repository-relative, never through symlinks), so an uncommitted caller-side
edit to any of those files shapes nothing — and EVERY dispatch pins it:
`dispatch-packet` requires `--candidate-repository` (builders pin the
full-SHA `repositoryRevision`, reviewers the candidate head) even when the
request carries no records. The derivation also reads every
pinned repository
record's payload back from the candidate tree through the adapter's own
sanitization — a fabricated payload with a self-consistent hash is
rejected, and a token-shaped path is withheld from every readback error.
The verification evidence carries the candidate patch itself, LOSSLESSLY:
UTF-8 patch bytes travel as text, non-UTF-8 bytes travel base64-encoded
(`patchEncoding`), and the carried patch must hash to `patchSha256` — a
replacement-character decode would render two distinct non-UTF-8 changes
identically while the digest described bytes the reviewer never saw. The
patch renders inside an untrusted boundary with
boundary-marker text ESCAPED rather than stripped — code that legitimately
spells a marker reaches the reviewer intact — so the reviewer reads the
change under review rather than only its hash, path names, and line
counts.
Dispatch accepts repository evidence only: every other source type would
dispatch exactly the payload the request chose under a self-consistent
hash, so non-repository records are refused on the dispatch path until an
attested retrieval exists for them. Builder and tester dispatches carrying
repository evidence take
`--candidate-repository` alone (the derivation flags are reviewer-only) and
are refused without it: the request's `repositoryRevision` must be the full
commit SHA of the trusted pinned tree AND that repository's current
checkout (`HEAD`) — a genuine but historical pin would dispatch a
write-capable envelope under stale, possibly weaker configuration and
source — and every pinned record is read back
from it the same way, so a write-capable dispatch can never hand the
implementation agent fabricated source under a self-consistent citation.
The pinned revision is also BOUND into the packet itself — serialized as
`repositoryRevision`, covered by `packetSha256`, and carried on the
envelope as a `revision:<sha>` input reference — even when the request has
no evidence records, so identical tasks at two different commits never
share a digest and an empty-evidence envelope cannot be replayed after the
checkout moves.
Write-capable dispatches also re-run the task policy
(`evaluate_task(..., "implement")`) and require `--authorization`: the
`check-request --authorize-labels` result, which must be ready, authorized
and stable, must name this exact project, work item, and content digest,
must be FRESH (`checkedAt` within `--authorization-max-age-seconds`,
default 900 — a ceiling the flag can only NARROW, never widen, so a caller
holding a stale signed result cannot restore the replay `checkedAt` exists
to prevent — since labels, dependencies, and pull requests all move after
a check), and whose verdict must cover exactly the policy's implementation,
ready, and human-review labels — label names spelled into caller-supplied request
JSON are not approval evidence, and neither is a verdict narrowed to a
caller-selected label. Both trust artifacts are ATTESTED, because structure
proves nothing about provenance — an invoker could author either as plain
JSON: `dispatch-packet` requires `--attestation-key` (a secret key file of
at least 32 bytes) and verifies an HMAC-SHA256 `attestation` field over
each document's canonical JSON. `check-request --attestation-key` signs
its result and `orchestrate --attestation-key` signs the state it writes
(verifying existing state on read, so an out-of-band edit is refused at
the next step); the secret never travels in the artifacts, so reading one
confers no ability to mint one. The attested state also authenticates its
INPUTS before signing: `dispatch-packet --attestation-key` signs the
envelope it emits, `orchestrate start-run` under the key accepts only an
envelope whose mark verifies and whose `envelopeSha256` matches its content
(so run attribution — agent and mission identity — can neither be
hand-authored nor re-labeled on a signed envelope), and the envelope must
be FOR the record: its `workRef` must equal the target unit, its
digested `packetRole` must permit the run kind (builders
implement/repair, testers verify, reviewers review), and its digested
`runKind` (`dispatch-packet --run-kind`, defaulting to the role's
canonical kind) must EQUAL the recorded kind — an implementation envelope
replayed as a repair run would bypass the fresh repair dispatch with its
current authorization, revision, and prompt. An attested `finish-run` with
`--commit-sha` requires
`--completion`: the `attest-completion` record (unit, run, commit, AND the
dispatched envelope's digest, signed under the same key, with the commit
verified to exist in
`--commit-repository` when written) produced by the execution harness at
run completion — mere existence of a commit proves nothing about which run
produced it, and because run IDs restart when state is recreated, the
envelope digest is verified against the finished run's recorded
`envelopeSha256` so a genuine completion from an old history cannot finish
a recreated run. Attested VERIFICATION and REVIEW transitions require
`--outcome` the same way: `--result succeeded` and `--findings 0` are
otherwise raw CLI inputs an invoker could assert over failed gates or an
unread diff, so the `attest-outcome` record (unit, kind, result, findings,
run and the judged envelope's digest, signed by the verifier or reviewer
harness that observed the outcome) must match the transition's arguments
and bind the judged run's recorded `envelopeSha256` — the latest producing
run's for a verification, the named review run's for a review — so a
verdict always attaches to the exact dispatched code it judged. The agent roster is repository-owned configuration exactly
like the policy: `dispatch-packet` reads `--agents` from the committed
candidate tree at the packet's pinned revision — and for REVIEWER
dispatches, whose pin is the untrusted candidate head, from the
AUTHENTICATED BASE instead, so a producer cannot commit a roster aliasing
itself as a fresh reviewer — which means a caller-side roster
cannot relabel a producing agent under a fresh `agentId` or route
authorized work to an undeclared executor. REPAIR dispatches
(`--run-kind repair`) have the same exposure from the other side: the
repair candidate's pinned HEAD is the failed implementation's own commits,
so they require `--review-target-repository` (same separation checks) and
re-pin the WHOLE contract — policy, mission registry, knowledge sources,
and roster — to its `origin/<default branch>`, with the base-committed
policy required to agree on the branch name that located it, so a widened
repair mission or weakened policy committed at the failed head shapes
nothing. And one envelope
digest seeds at most ONE recorded run, with no exception for failed runs:
an attested `start-run` refuses an envelope whose digest the unit already
recorded, so a pre-repair review envelope can never report on the repaired
head and a failed run's envelope cannot seed a second attempt under the
first's attribution — a retry is a fresh dispatch, whose moved revision or
refreshed authorization gives it a fresh digest. And every command that derives
or attests these artifacts (`context-packet`, `dispatch-packet`,
`orchestrate`, `attest-completion`, `attest-outcome`) runs under STRICT executable trust:
working-tree `trust_mode` and the `AGENTIC_SDLC_GIT`/`AGENTIC_SDLC_GH`
overrides are ignored, so a candidate-planted git cannot fabricate the
commits and identities the artifacts are signed over. Reviewer dispatches require `--history`: the
ORCHESTRATOR STATE document, verified against the attestation key and
replayed through the orchestrator's own
validation, whose recorded runs for this work item (mission and agent
identity) define the independence exclusions — EVERY agent that performed a
producing mission is excluded, not only the last assignee, and producing
status comes from each run's persisted `implementation`/`repair` kind, so a
mission later removed or rewritten cannot launder its agents back into
review eligibility. The state must also be the history of THIS candidate:
some producing run must record the candidate head as its `commit_sha`, or
an older genuine state would omit the agent who produced the head. A
caller-authored mission→agent map is not authoritative, and a state
recording no producing run for any mission the review is independent of
refuses dispatch.
Request
JSON can never impersonate pipeline evidence: `dispatch-packet` refuses a
request-supplied `verification` object outright, and `context-packet`
accepts the same flags (request `priorFindings` carries prior review
findings on the derived path). The `--packet` comparison is exact on every
path, digest field included, because derived gate reports carry zeroed
durations and rebuilds are therefore deterministic. When the selected agent's declared window cannot
fit the packet, dispatch falls back through the roster order to the next
eligible agent. Every agent is checked at packet dispatch: the
rendered prompt plus the output reserve must fit its declared
`contextWindowTokens`, and an agent that declares none is refused — an
undeclared window is unknown, not unlimited. With `--packet`, the previously written packet
artifact must equal the rebuilt packet's whole document — never just its
digest field, and compared through canonical JSON encodings so booleans and
numbers never conflate — so a stale or tampered artifact never dispatches. The plain `dispatch-mission` command remains for
non-factory missions whose prompt is not packet-built.
