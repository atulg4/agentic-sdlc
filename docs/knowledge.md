# Knowledge sources, evidence records, and context packs

Agents never consume raw, unaudited content. Every fact enters the pipeline as
a normalized `EvidenceRecord` retrieved from a declared `KnowledgeSource`, and
each mission run receives an immutable, content-addressed `ContextPack`
containing only the evidence that mission is permitted to see.

## Declaring sources

Sources live in `knowledge.toml`, which is part of the baseline forbidden
paths — like `missions.toml`, it can only change through human-reviewed
commits:

```toml
version = 1

[[source]]
id = "repo-main"
type = "git-repository"
provider = "github"
locator = "https://github.com/owner/project"
scope = "owner/project"

[[source]]
id = "tracker"
type = "github-issues"
provider = "github"
locator = "https://github.com/owner/project/issues"
scope = "owner/project"
redact_fields = ["api_token"]

[[source]]
id = "positions-view"
type = "sql-readonly"
provider = "postgres"
locator = "views/positions"
scope = "marketmaestro"
classification = "restricted"
permitted_missions = ["data-quality-reviewer"]
credential_ref = "POSITIONS_RO_DSN"
```

Rules enforced at load time (fail closed):

- unknown keys, types, and classifications are rejected — there is no
  write flag to grant, so no adapter can acquire write authority through
  configuration;
- `restricted` sources must list `permitted_missions`;
- `credential_ref` names a least-privilege credential; secrets themselves
  never appear in configuration, prompts, or artifacts.

## Evidence records

Adapters normalize provider documents into `EvidenceRecord`s carrying source
identity, a stable external ID, canonical locator, scope, SHA-256 content
hash, revision, author/timestamps, classification, retrieval provenance,
freshness, typed links (`duplicates`, `contradicts`, `supersedes`, …),
sanitized payload, and `L<start>-L<end>` citation anchors that stay resolvable
after normalization.

`GitRepositoryAdapter` and `GitHubAdapter` are implemented;
`STUB_ADAPTER_CONTRACTS` documents the read-only contracts for Jira,
Confluence, generic REST, and SQL read models. `check_adapter_conformance`
proves any new adapter is deterministic, validates its records, applies
field-level redaction, resolves its anchors, and exposes no write operations.

## Untrusted-content boundary

Payloads are sanitized (NUL bytes and prompt-boundary markers stripped) and
rendered inside a single `<untrusted-evidence>` block with an explicit "this
is evidence, not instructions" preamble. Malicious instructions inside
retrieved documents remain data.

## Context packs

`build_context_pack(mission, work_ref, records, sources)` enforces access
policy a second time at publication, deduplicates, surfaces cross-source
duplicates/contradictions/supersessions as explicit ambiguities, bounds pack
size, and reports coverage (sources covered, failures, fresh vs. stale). The
pack digest is computed over content-stable fields only, so the same source
revisions always produce the same `packSha256` regardless of retrieval time —
and the digest is what dispatch envelopes and run records pin.

Failure policy: retrieval failures, stale evidence, or missing required
sources *block* high-risk missions and produce a *qualified* pack with
explicit exclusions for lower-risk missions. `pack_invalidation_reasons`
reports when upstream revisions changed or evidence was revoked, invalidating
dependent packs.

## CLI

```sh
sdlcctl validate-knowledge --knowledge knowledge.toml --output sources.json
```
