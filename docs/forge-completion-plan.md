# Forge completion plan

Repository audit snapshot: November 8, 2026, main
`379e5ab50c8d03e249c73f96c962e14d784c2b3d`. Open PRs are proposed work, not delivered
capabilities. Recheck their heads, reviews, and checks before integrating.

Progress update, November 8: #155 is merged. The helper integration in #179 now
includes main `8e584d00f3031e6dd68e50320df648f067a24080`, and its Forge-only Claude
review routine has completed real API-triggered reviews. The audit snapshot above
remains the original audit baseline, not the integration's current base.

## Ownership and budget

Codex leads coding, integration, verification, and coordination. Claude Max
provides scoped independent reviews, regression tests, and design research.
The [cloud helper adapter](claude-cloud-workers.md) implements explicit dispatch.
Routine setup and live review are verified; durable credential storage, the
test-development role, and automatic queue wiring remain rollout work.

Keep the existing owner merge policy, branch protections, leases, publication
gates, and CI. Keep subscription-only execution: no paid-provider fallback and
no extra-usage enablement. In particular, the existing `codex` Actions executor
uses `OPENAI_API_KEY`; choosing that executor does not make it use a ChatGPT
subscription. [Issue #39](https://github.com/atulg4/agentic-sdlc/issues/39) remains
a separate executor integration requirement.

## Integration order and definition of done

| Priority | Work | Completion evidence |
|---|---|---|
| 1 | [#155](https://github.com/atulg4/agentic-sdlc/pull/155): CI shell/pipefail detection | Current-main integration passes CI; review of current head is clear; merge through existing protections. |
| 1 | Claude helper dispatch | Unit tests prove duplicate suppression, failure handling, and no API-key fallback; a real review session returns useful findings on the exact submitted SHA. |
| 2 | [#165](https://github.com/atulg4/agentic-sdlc/pull/165): effectiveness ledger; [#167](https://github.com/atulg4/agentic-sdlc/pull/167): heartbeat/queue | Recorded completed outcomes and a live dispatcher tick populate a queryable status; outages and missing evidence remain explicit. |
| 2 | [#169](https://github.com/atulg4/agentic-sdlc/pull/169): live control-center contract; [#176](https://github.com/atulg4/agentic-sdlc/pull/176): dashboard | Integrate data contracts before UI. Exercise real data plus empty/stale/failed states. Run browser/mobile tests with their dependencies installed rather than relying on skips. |
| 2 | [#173](https://github.com/atulg4/agentic-sdlc/pull/173): role-aware acceptance reviews | A candidate change is independently evaluated against acceptance criteria; stale-head or missing evidence cannot pass. |
| 3 | [#151](https://github.com/atulg4/agentic-sdlc/pull/151): Forge caller workflows | Update immutable platform pins after prerequisites merge. Run a bounded test issue end to end before enabling backlog dispatch. |
| 3 | [#120](https://github.com/atulg4/agentic-sdlc/issues/120): factory runtime wiring | One approved request becomes a dependency graph, context packet, implementation, fresh verification, independent review, and draft PR. Crashes resume without duplicate work. |
| 3 | Usage accounting and [#121](https://github.com/atulg4/agentic-sdlc/issues/121) learning loop | Real dispatch/completion paths produce usage and outcome records automatically; unavailable subscription usage stays unknown rather than reported as zero. |
| 4 | [#42](https://github.com/atulg4/agentic-sdlc/issues/42): benchmarks | Repeatable held-out tasks measure acceptance, regressions, rework, elapsed time, and observed subscription usage. Route based on evidence. |
| Separate infra gate | [#142](https://github.com/atulg4/agentic-sdlc/pull/142): ARC/GKE runners | Resolve sandbox and network-isolation concerns before provisioning or migrating. Demonstrate isolation, cleanup, and cost bounds. |

## What is already present

- Context packets merged through #154; they should be reused by factory wiring.
- Schedule forecasting merged through #172; #168's issue state alone is not
  evidence that the implementation is missing.
- The project-directory hook paths use `${CLAUDE_PROJECT_DIR}` on the audited
  main. Reproduce #175 against that version before treating it as an open defect.
- Claude OAuth workflows, issue leases, diff/request gates, usage-ledger
  primitives, and control-center primitives exist. The main gaps are connecting
  these into a verified operational path and integrating the pending PRs.

## First operational milestone

Start with one Forge PR review in Claude, using the current head SHA and the
saved review prompt. Codex checks the findings, fixes confirmed defects, and
runs fresh CI. Then try one isolated test-development task. Only after both
return useful results should the dispatcher be connected to a queue. A launched
session is not a completed task, and a proposed patch is not an accepted change.
