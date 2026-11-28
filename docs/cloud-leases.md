# Issue leases when cloud workers cannot use GraphQL

Some Claude Code cloud environments can use GitHub REST while GraphQL calls
fail. Set `FORGE_LEASE_PR_API=rest` for the lease commands in such an environment.
The default remains GraphQL. Any other value is rejected rather than silently
selecting a different observation path.

```sh
export FORGE_LEASE_PR_API=rest
sdlcctl claim --project owner/repo --issue 12 --agent claude-code \
  --session "$FORGE_SESSION_ID" --branch forge/issue-12
sdlcctl renew --project owner/repo --issue 12 --session "$FORGE_SESSION_ID"
sdlcctl release --project owner/repo --issue 12 --session "$FORGE_SESSION_ID"
```

Use the same setting throughout the run, including other commands that consume
the open-PR inventory. The REST path reads every open-PR page and preserves the
existing branch, body and issue-timeline conflict checks. Lease label and
assignee bookkeeping uses additive REST operations, so unrelated labels and
assignees are retained. Existing lease arbitration and ownership-aware cleanup
remain in force. The setting neither authenticates GitHub nor grants permission
to claim, implement or publish work.

## Evidence that still requires GraphQL

REST does not expose every relationship Forge must check. A manual Development
sidebar connection or disconnection can require a GraphQL lookup to identify
its subject. An open cross-reference from another repository requires that PR's
GraphQL `closingIssuesReferences` to determine whether it closes this issue.

If either lookup is required and unavailable, Forge refuses the claim or
renewal. It does not interpret an unreadable relationship as an empty one. Use
a host with access to the required evidence, or restore access before retrying;
do not work around a refusal by posting a claim marker manually.

This option is limited to lease observations and bookkeeping. It does not make
the entire scheduler, authorization checks or cloud onboarding independent of
GraphQL. All normal authorization and publication gates still apply.

## Bounded smoke check

Run this only on an owner-approved disposable issue with no existing live lease
and no open or linked PR. Use a fresh session ID and the pinned Forge version
containing this support.

1. Confirm ordinary authenticated REST reads work on the target repository.
2. Set `FORGE_LEASE_PR_API=rest`, then claim the disposable issue using a short
   approved lease duration. Optionally include an assignee the operator may
   assign.
3. Verify command success and the normal claim marker and `in-progress` label
   on GitHub. If an assignee was supplied, verify it as well.
4. Renew the same session and verify the new expiry marker.
5. Release that session. Verify the release marker and ownership-aware cleanup;
   pre-existing labels and assignees must remain.

If any command fails, retain the error as evidence and reconcile the session
before retrying. Do not create a PR, spend model credits or change repository
permissions for this smoke check. Offline regression tests cover pagination,
malformed observations, PR conflicts and the normal lease lifecycle.
