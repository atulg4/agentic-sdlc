# Mission registry and capability-based dispatch

Missions replace hardcoded planner/implementer/reviewer role names with
explicit, versioned contracts. The orchestration layer assigns work by matching
a mission's required capabilities, risk class, scope, and independence rules
against a declared agent roster — a model name is never treated as proof of
capability.

## Mission contract

Every mission declares (see `MissionSpec` in `src/agentic_sdlc/missions.py`):

- a stable mission ID and `MAJOR.MINOR.PATCH` version;
- purpose and success criteria;
- input and output artifact names (outputs are validated with
  `validate_output` before downstream consumption);
- required capabilities from a closed, platform-defined universe;
- repository read/write scope plus additional forbidden paths;
- deterministic commands and required evidence;
- risk class and whether human approval is required;
- runtime, token-budget, retry, and concurrency limits;
- whether the mission may run in parallel;
- independence constraints (`independent_of`) and an escalation target.

## Platform templates and consumer extensions

The platform ships generic missions (specification planner, implementation
worker, test author, deterministic verifier, code/security/architecture/
data-quality/quant reviewers, documentation agent, repair agent, release
validator, context builder). A consumer repository may add domain-specific
missions in a `missions.toml` file:

```toml
version = 1

[registry]
# Optional: narrow the capability universe for this repository.
allowed_capabilities = ["specify", "plan", "edit-code", "author-tests",
                        "run-commands", "review-code", "review-quantitative"]

[[mission]]
id = "strategy-quant-reviewer"
version = "1.0.0"
purpose = "Review trading-strategy math"
success_criteria = ["Math reviewed with citations"]
capabilities = ["review-quantitative"]
output_artifacts = ["review-report"]
independent_of = ["implementation-worker", "repair-agent"]
```

Consumer missions extend the platform set; they can never redefine a platform
mission, request unknown or policy-denied capabilities, write to forbidden
paths, or combine review and write capabilities in one mission. `missions.toml`
is part of the baseline forbidden paths, so ordinary autonomous agents cannot
modify mission configuration — changes arrive only through human-reviewed
commits.

## Dispatch

`MissionRegistry.select_agent` picks the first declared agent that satisfies
the entire contract; later roster entries act as fallbacks under exactly the
same rules. Selection enforces:

- capability coverage (`mission.capabilities ⊆ agent.capabilities`);
- risk clearance (`agent.max_risk ≥ mission.risk`);
- declared independence (`independent_of`);
- structural self-approval prohibition: a review mission never selects an
  agent that produced writes for the same work unit, even if the mission
  forgot to declare independence;
- availability and, when a `MissionLedger` is supplied, concurrency and
  token-budget limits.

`create_dispatch_envelope` pins each run to the mission version, adapter
version, provider/model, work reference, sorted input references, and a
SHA-256 prompt digest. Identical inputs always produce an identical envelope
digest, making dispatch reproducible and auditable.

## CLI

```sh
sdlcctl validate-missions --config agentic-sdlc.toml --missions missions.toml
sdlcctl dispatch-mission --config agentic-sdlc.toml --missions missions.toml \
  --mission-id security-reviewer --agents agents.json --history history.json \
  --work-ref owner/repo#7 --input-ref patch --prompt prompt.md \
  --output envelope.json
```

Both commands emit machine-readable JSON for dashboards and audit trails and
fail closed (exit code 2) on any contract violation.
