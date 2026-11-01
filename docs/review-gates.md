# Role-aware acceptance review gates

Independent review in Forge is role-aware and acceptance-criteria-aware
(issue #119): every candidate change gets a correctness review, security-
sensitive changes also get an independent security review, and the gate
passes only when every selected role approves the exact head with every
acceptance criterion verified met.

```
issue + labels + changed paths ─► sdlcctl review-roles ─► roles.json (+ reasons)
#118 reviewer context packet  ─► context.md (criteria, tests, non-goals,
                                             diff summary, gate report)
one review per selected role  ─► review-<role>.json (schema v2)
pinned engine                 ─► enforce_reviews ─► gate.json ─► pass/block
```

## Role selection is deterministic and recorded

`review_roles.select_review_roles(task_text, labels, changed_paths)` always
selects `code-reviewer`. It adds `security-reviewer` when any of these
documented triggers fire, and records the exact reason for each role with
the review run (`roles.json` in the review artifact):

- the task text or a label contains a security keyword, matched on word
  boundaries (`SECURITY_TEXT_PATTERNS`): auth, authentication,
  authorization, credential(s), oauth, password, permission(s), policy,
  privacy, secret(s), security, token(s);
- a changed path matches a security path pattern
  (`SECURITY_PATH_PATTERNS`): `.github/workflows/*`, or any path containing
  auth, credential, oauth, permission, policy, privacy, secret, or token;
- a changed path matches the project policy's `protected_paths` — protected
  paths always escalate.

Selection uses the policy committed at the PR **base**, never the candidate
head's copy.

## The reviewer prompt comes from the context packet

The review workflow builds the #118 **reviewer context packet** with the
pinned platform engine (`sdlcctl context-packet … --prompt-output`): the
acceptance criteria, required tests, non-goals, policy limits, diff summary
and trusted gate report all reach the reviewer as the packet renders them —
inside untrusted-data boundaries — never by the workflow re-parsing the
issue. The packet's gate report runs candidate-controlled gate commands, so
the workflow confines them in a bubblewrap sandbox (a root-owned
`forge-gate-sandbox` launcher): the host is read-only, the runner home (the
installed engine), the checkout and the runner temp are hidden, and only
the disposable derivation tree is writable — candidate gate code cannot
rewrite the engine or the review control files later steps enforce with. Each selected role additionally gets its own checklist
(`ROLE_CHECKLISTS`), which is trusted pipeline text. Reviewers get no write
instructions; builders get no reviewer checklist and no verdict schema.

## The review schema is versioned

The canonical schema lives at `src/agentic_sdlc/schemas/review-v2.json` and
the workflow materializes it from the engine, so the contract the reviewer
is held to is always the one enforcement validates. Version 2 adds, and
requires:

- per finding: `blocking` (boolean), `gateOutcome` (`block`/`advise`), and
  `repairRecommendation`;
- per review: `role`, `headSha`, and `acceptanceCriteriaVerdict` — one
  `met` / `not_met` / `not_verifiable` entry per acceptance criterion.

A review without `schemaVersion: 2` — every legacy document included —
fails closed.

## Enforcement is fail-closed

`review_gate.enforce_reviews(selection, documents, head_sha)` passes only
when, for **every** selected role, a structurally valid review exists for
exactly `head_sha` and that review satisfies all three:

1. `verdict` is `approve`;
2. no finding is blocking — `blocking: true`, `gateOutcome: block`, or
   severity `critical`/`high` (a medium advisory finding alone does not
   block);
3. the criteria verdict covers **exactly** the packet's acceptance criteria
   (compared whitespace-insensitively): an omitted, duplicated, or
   fabricated criterion entry blocks by name, so a reviewer can neither
   skip the criterion it could not verify nor invent one marked met;
4. every acceptance criterion is marked `met` — `not_verifiable` blocks,
   because unverified is not verified.

Each refusal names the finding or criterion. When `security-reviewer` was
selected, a passing code review alone never passes the gate. The decision
is computed in the review job by the pinned engine and recorded as
`gate.json`; the publisher job stays checkout-free and only applies it.

## Repair receives only what blocks

`sdlcctl repair-findings` filters the role reviews down to the blocking
findings (with each finding's `repairRecommendation`), and the repair
prompt is the #118 **builder context packet** — pinned at the trusted base
— plus those findings inside an untrusted boundary. Low-severity and
advisory findings never spend the bounded repair budget.

## What this deliberately does not change

- Deterministic CI and `run_pre_review` still run first; this gate follows
  them, it does not replace them.
- An implementer can never approve its own work, and merge authority stays
  human (`decide_review_continuation` and the protected merge executor are
  unchanged).
- Stale pre-repair approvals still cannot pass: a review binds its exact
  `headSha`, and a verdict for any other head is not a verdict at all.
