# Dependency-aware parallel work graph

`src/agentic_sdlc/work_graph.py` decomposes an approved parent work request
into a validated DAG of bounded work units and executes independent missions
in parallel without conflicting edits, duplicated effort, or unsafe artifact
composition.

## Work units

Each `WorkUnitSpec` declares its mission ID/version, dependencies, owned
paths, read/write sets, shared semantic resources (API contracts, schemas,
migrations, model configs), expected output artifact, verification gates,
concurrency group, composition strategy, provider, and timeout/retry/token
budgets. Units with an empty write set are read-only and may always run
concurrently.

## Validation (`validate_graph`)

Fails closed on: duplicate or malformed unit IDs, missing dependencies,
self-dependencies, cycles, ambiguous path ownership, and any write scope
touching policy-forbidden paths (globally enforced regardless of the unit's
apparent risk). Two units with overlapping write sets must either be ordered
by dependencies or declare the same explicit composition strategy. Units
sharing a declared semantic resource conflict even when their file paths are
disjoint — they must serialize unless both are read-only.

## Execution (`GraphExecutor`)

- **Isolated workspaces.** Every unit gets `workspaces/<unit_id>`; parallel
  agents never share a writable checkout.
- **Leases.** Owned paths and shared resources are exclusively leased for the
  unit's timeout, with expiration, reclamation, and a full audit log
  (`LeaseManager`).
- **Bounds.** `max_parallel`, per-provider concurrency limits, and a total
  token budget are enforced at `start()`; a unit whose budget cap would
  overrun the remaining budget is refused.
- **Fan-out/fan-in.** `ready()` releases a unit only when every dependency is
  *attested* — completed, verified, with a recorded immutable artifact
  digest. Downstream work receives digests, never another agent's branch.
- **Invalidation.** A failed unit blocks its dependents (unrelated evidence
  is preserved); an upstream artifact change resets dependents to pending,
  discards their stale artifacts, and clears the composed-tree verification.

## Composition

`compose()` refuses fan-in while any unit is failed or blocked (naming the
attested evidence it preserves), admits only attested artifacts, and requires
`record_final_verification(passed=True)` — verification of the recomposed
tree against the pinned `base_sha`, not just individual branches. The report
lists every unit with mission, agent, workspace, artifact digest,
verification result, composition/conflict decision, token spend,
invalidations, and the lease audit trail. Composition never bypasses consumer
CI or independent final review — the report is input to the #16 lifecycle,
which still ends in human merge.
