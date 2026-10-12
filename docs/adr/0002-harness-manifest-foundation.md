# ADR 0002: Read-only harness manifest foundation

- Status: Proposed; initial implementation available for review.
- Date: 2026-09-25.
- Owner approval: the owner requested implementation to start; full Harness
  Plane acceptance and live enablement have not been recorded.
- Related: [Harness Plane proposal](../design/forge-harness-plane.md),
  [ADR 0001](0001-separated-agentic-control-plane.md).

## Context

The Harness Plane needs strict, repeatable run identities before assembly can
be integrated with live execution. Review of the proposal identified ambiguous
JSON encoding, incomplete executor binding, static versus effective risk, and
missing usage-recording integration. Shipping a manifest alone cannot resolve
all of these runtime boundaries.

## Proposed decision

Implement a read-only foundation as phase 1a: a closed Draft 2020-12 JSON Schema,
RFC 8785 canonical encoding, immutable normalized manifest objects, full executor
registry/profile binding and an explicit effective-risk validation function.
Expose `sdlcctl validate-harness` for offline contract inspection, with a report
that always says `dispatchAuthorized: false` and enumerates unverified boundaries.

Use maintained `jsonschema` and `rfc8785` libraries rather than approximating
their standards. Bound the JSON domain and reject ambiguous input before schema
validation. Declare array ordering explicitly. Preserve all legacy hash formats
and existing workflow behavior. The inspector retains human merge gates and
cannot activate the separate protected-merge executor.

## Alternatives

- Handwritten canonicalization would reduce dependencies but risk subtly
  different numbers, Unicode ordering and escaping between runtimes.
- Wiring the first validator directly into generation would expose incomplete
  provenance, permission, budget and context checks to live work.
- Implementing the whole Harness Plane in one change would make independent
  review and rollback substantially harder.

## Consequences and security impact

This is an executable contract and fixture baseline, not an execution engine.
The CLI reads local artifacts and emits a report; no provider, git publication,
merge, deployment or changed-code execution is added. A forged collection of
mutually consistent input artifacts can pass some checks, because provenance
and approval are not yet implemented. The output explicitly does not authorize
dispatch. Callers must not substitute it for existing policy decisions.

The new dependencies add a supply-chain surface and must remain under normal
dependency review. The schema is bundled locally, so validation never fetches
a user-supplied or remote schema. Optional future skill content is not loaded
or executed by the inspector.

## Migration and rollback

No legacy records, checks or workflow contracts migrate in this increment.
New manifests have their own schema and canonicalization contract; legacy
envelopes and context digests retain their exact formats. Rollback removes the
new inspection command and package/schema support without altering existing
run records or consumer workflows.

Live dispatch remains deferred until the prerequisites in
[Harness manifest foundation](../harness-manifests.md) are implemented and
independently reviewed. This ADR's proposed status does not grant that approval.
