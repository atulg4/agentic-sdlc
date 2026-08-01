# MarketMaestro Rollout

## Current Checkpoint

As of 2026-08-01, MarketMaestro issue #2 is fully specified and carries the
existing `claude-ready` and `human-review-required` labels. It depends on issue
#10. Pull request #11 is the open draft for that CI repair, so issue #2 must not
start implementation until #11 is reviewed, merged, and #10 is closed.

The latest pull-request run confirms that the new job graph works:
`changed-file-quality` and `quality-baseline` passed, and `test` ran
independently. The test job is currently red with 34 failures and 22 setup
errors. Most are missing SQLite/PostgreSQL tables; four are stale logging or
metadata assertions. Do not suppress those failures. Repair the test database
lifecycle and reconcile the four assertions before treating native CI as a
required implementation gate for issue #2.

The MarketMaestro example profile intentionally recognizes `claude-ready` so
the existing backlog does not need a label migration. New projects use the
provider-neutral `agent-ready` label.

## Activation Sequence

1. Create repository `atulg4/agentic-sdlc` and push this project. Public is the
   simplest distribution choice because the framework contains no project
   secrets. If private, create `PLATFORM_READ_TOKEN` with Contents: Read for
   only this repository.
2. Review its workflow permissions and security-contract tests.
3. Create the first release and record its exact commit SHA.
4. Permit MarketMaestro to call reusable workflows from the private platform
   repository.
5. Add `OPENAI_API_KEY` to MarketMaestro Actions secrets. Add
   `ANTHROPIC_API_KEY` only if the Claude adapter will be used.
6. Create the dedicated publisher GitHub App, grant Contents read-only plus
   Issues and Pull requests read/write, install it only on MarketMaestro, and
   configure `PUBLISHER_APP_CLIENT_ID` and `PUBLISHER_APP_PRIVATE_KEY` as
   documented in [GitHub Publisher App](github-publisher-app.md).
7. Create label `implementation-approved` in MarketMaestro.
8. Scaffold or copy only the policy, agent guide, work-request template, and
   manual plan workflow. Replace every placeholder with the immutable SHA.
9. Triage the now-visible unit-test baseline in a focused follow-up change; do
   not add broad ignores or make the `test` job non-blocking.
10. Finish the normal review and merge of MarketMaestro pull request #11, then
    close issue #10 when its agreed acceptance criteria are met.
11. Make the native `test` check green before issue #2 implementation.
12. Run issue #2 through the new plan workflow. Do not add
   `implementation-approved` yet.
13. Review five plan-only runs and adjust specifications and policy.
14. Add the implementation and independent-review callers at automation level
    2, configure verified project commands, and protect the resulting checks.
15. Add `implementation-approved` only after a plan is accepted. The pipeline
    may then open a draft PR; the owner still merges it.
16. After the manual trial is stable, install the MarketMaestro-specific
    `agent-auto-plan.yml` and `agent-auto-implement.yml` examples. They preserve
    the existing `claude-ready` label while requiring the separate
    implementation approval label.

MarketMaestro's portable pre-publication profile runs setup and changed-file
quality. Its existing pull-request CI remains responsible for unit and
integration tests because that workflow provisions PostgreSQL. Both native CI
checks and independent review must pass before merge.

## Coexistence With the Existing Claude Worker

Keep the existing worker in manual plan-only mode during rollout. Do not allow
both systems to implement the same issue. After five successful plan runs and
one verified draft-PR trial, either retire the old worker or reduce it to a
secondary implementation adapter behind the same policy and verification
contract.
