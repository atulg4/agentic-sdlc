# Harness manifest foundation

This is the first implementation increment of the
[Harness Plane proposal](design/forge-harness-plane.md), **phase 1a**. It provides
a packaged strict schema, RFC 8785 hashing, immutable manifest values, selected
reference checks and a read-only inspection command. It does not complete phase
1 or authorize a pilot. The design decision is recorded for review in
[ADR 0002](adr/0002-harness-manifest-foundation.md).

## Inspect a manifest

After installing the package (including its schema/canonicalization dependencies),
run the self-contained offline example. Existing source-only CLI commands do not
import those dependencies:

```sh
sdlcctl validate-harness \
  --manifest examples/harness/manifest.json \
  --config examples/harness/agentic-sdlc.toml \
  --executors examples/harness/executors.json \
  --effective-risk medium
```

The example uses a fictional repository/model, hypothetical costs and unresolved
references represented by repeated digest characters. It is useful for exercising
the implemented checks only; it is not an approved execution recipe. No model is
called, no source is edited and no credentials are read. `--missions` optionally
adds an existing consumer mission registry; `--output` writes the JSON report.

Exit 0 means the reported checks passed. Exit 2 means parsing or validation
failed. A successful report **always** contains `dispatchAuthorized: false` and
a `notVerified` list. Neither the exit code nor a manifest digest grants approval
or replaces the existing dispatch/verification/publication gates. The CLI is not
called by any existing workflow or orchestrator in this increment.

`--effective-risk` is the caller's asserted expected risk for inspection. It is
not a path-risk classifier or authority source. The library guard rejects a
critical value even when the static mission is medium, rejects mismatches and
lowered mission floors, and requires the additional review gate for high risk.
It must be supplied a fresh **trusted** assessment when live dispatch integration
is implemented; accepting a worker's flag would defeat that boundary.

## What is checked

- All manifest fields from the proposed v1 contract are required; unknown keys,
  invalid enums, duplicate JSON names, duplicate set entries, duplicate skill IDs,
  non-finite/unsafe numbers and excessive depth/size fail closed.
- Candidate tree and patch identity appear together; a published head requires
  both. Work references identify an issue in the declared repository. Paths are
  syntactically normalized repository-relative patterns. This does not prove
  actual filesystem scope or symlink safety.
- Work and lineage IDs preserve Forge's opaque colon/slash identities; registry
  names retain stricter slug rules. Direct `HarnessManifest` construction also
  validates and normalizes input, and rejects mutable byte buffers.
- The schema labels every array as ordered or set-valued. Set arrays are sorted
  by each element's JCS bytes; ordered skill application is preserved. The digest
  is SHA-256 of the normalized manifest's JCS bytes. Existing envelope, patch and
  context-pack hashing formats are unchanged.
- Raw project policy and executor registry bytes must match their digest fields.
  The loaded mission registry and mission contract must match their new reference
  digests. Those references use JCS over existing `as_dict()` documents without
  changing the legacy documents themselves.
- The full executor profile is materialized through the existing loader. Its
  snapshot is `{"schemaVersion": 1, "profile": <all resolved profile fields>}`;
  `taskClasses`, `capabilities`, `toolCapabilities` and `permittedRepositories`
  are normalized as sets. The snapshot digest and duplicated adapter/auth/model
  identity must agree. Repository scope, required capabilities, risk ceiling and
  context-window bounds are checked against that snapshot.
- Effective risk, declared mission capabilities/independence and mission budget
  ceilings are checked. Foundation manifests always retain deterministic CI,
  independent review and human merge gates, even when a supplied policy's legacy
  `human_merge_required` flag is false. This foundation does not enable the
  separate protected-merge path.

The JSON Schema is shipped at
[`schemas/harness-manifest-v1.json`](../src/agentic_sdlc/schemas/harness-manifest-v1.json).
Schema validation is followed by semantic checks in
[`harness.py`](../src/agentic_sdlc/harness.py). Hashing uses the
[RFC 8785 specification](https://www.rfc-editor.org/rfc/rfc8785) through the
[rfc8785 library](https://pypi.org/project/rfc8785/); schema validation uses
[jsonschema](https://python-jsonschema.readthedocs.io/en/stable/validate/).
Pinned input bytes prove consistency, not their approval or provenance.

## Remaining work before dispatch or a pilot

The following are **not implemented** by this increment and remain required:

1. Approved recipe/skill registries, revocation and protection of their control
   paths; deterministic skill selection and context construction.
2. Trusted path/task effective-risk computation and enforcement before every
   dispatch/retry/repair/resume/fallback, including worker clearance, high-risk
   context freshness, gate derivation and independent reviewer identity.
3. Routing-policy replay, selected-tool/sandbox permissions, all remaining
   artifact references, approved source provenance, fresh capacity validation
   and atomic capacity/budget reservations. A historically available executor
   is not thereby eligible to run now.
4. Durable pre-dispatch/post-run usage hooks, invocation/parent attribution and
   interruption reconciliation; existing usage-ledger construction still does
   not establish complete dispatch coverage.
5. Versioned envelope/run-record integration and binding through verification,
   clean attestation, publication and exact-head independent review.

The inspector's report lists these limits so callers cannot confuse a valid
document with an authorized or executable work unit. No new runtime or
control-plane enablement should consume it until those boundaries have separate
implementation, adversarial tests and owner review.
