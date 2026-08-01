# Agentic SDLC

Agentic SDLC is a reusable control plane for planning, implementing, verifying,
reviewing, and maintaining software through GitHub Actions or GitLab CI.

It is designed for a simple operating model:

- the owner describes work in natural language;
- Codex acts as the primary architect, planner, and independent reviewer;
- Claude Code or Codex can act as an implementation worker;
- deterministic CI decides whether code is technically acceptable;
- repository policy decides what an agent may touch;
- a human remains the merge authority until a separately approved low-risk
  auto-merge policy is introduced.

MarketMaestro is the first consumer and proving ground. Project-specific rules
remain in MarketMaestro; reusable orchestration lives here.

## What exists in v0.1

- A provider-neutral work-request parser.
- Deterministic task and diff policy checks.
- GitHub and GitLab webhook normalization.
- A command-line interface for CI: `sdlcctl`.
- A reusable GitHub policy action.
- GitHub plan, implementation, and review workflow templates.
- A GitLab CI component template and webhook contract.
- A MarketMaestro policy profile.
- Security, operations, onboarding, and phone-control documentation.
- Offline unit tests for the core policy behavior.

## Trust model

No AI worker can merge, approve its own work, deploy, or retrieve production
credentials. AI execution and repository publishing occur in different jobs.
The patch-generation job receives the AI credential but no repository write
token. Changed code runs only in the verifier. A later clean attestation job
reconstructs the publishable patch without executing consumer code. The
publisher receives a repository write token but no AI credential. On GitHub,
the publisher job uses its native short-lived token only to push the attested
branch. A dedicated App token opens the draft pull request and comments on the
issue, while every AI and verification job remains read-only.

See [Security](docs/security.md) and [Architecture](docs/architecture.md).

## Local verification

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
```

## CLI examples

```bash
sdlcctl validate-task \
  --config examples/marketmaestro/agentic-sdlc.toml \
  --task issue.md \
  --title "Implement secure preview-share field controls" \
  --label agent-ready \
  --label human-review-required

sdlcctl evaluate-diff \
  --config examples/marketmaestro/agentic-sdlc.toml \
  --paths-file changed-paths.txt \
  --added 120 \
  --deleted 14
```

## Onboard another repository

The scaffold command refuses to overwrite existing files and requires an
immutable platform commit SHA. Level 1 installs manual plan-only automation.
Level 2 adds manually approved draft-PR generation and automatic independent
review. Level 3 adds label-driven plan and implementation triggers.

```bash
sdlcctl scaffold \
  --destination /path/to/consumer-repository \
  --provider github \
  --project-id owner/consumer-repository \
  --platform-repository atulg4/agentic-sdlc \
  --platform-ref 0123456789abcdef0123456789abcdef01234567 \
  --automation-level 1
```

The generated policy contains no guessed build or test commands. Draft-PR
automation therefore fails closed until the project owner adds and reviews its
deterministic verification commands.

Level 3 remains approval-driven: `agent-ready` starts planning and the separate
`implementation-approved` label starts implementation. The trigger actor must
have repository write authority, and every resulting pull request is a draft.

## Provider status

| Capability | GitHub | GitLab |
|---|---|---|
| Work-request validation | Ready | Ready |
| Read-only plan | Ready | Prompt artifact ready |
| Isolated patch generation | Ready | Planned |
| Fresh verification | Ready | Planned |
| Draft change request publisher | Ready | Planned |
| Independent review gate | Ready | Planned |

See [GitLab Adapter](docs/gitlab.md) for the deliberately limited v0.1
boundary.

## Rollout

1. Publish this repository as `atulg4/agentic-sdlc`. Public is simplest; a
   private repository needs a dedicated framework-only read token.
2. Tag and pin the first reviewed release.
3. Install only the plan workflow in MarketMaestro.
4. Complete five successful plan runs.
5. Create and install the least-privilege publisher GitHub App.
6. Enable patch generation, still draft-PR only.
7. Repair MarketMaestro's deterministic test baseline.
8. Require CI and independent review before every merge.
9. Consider low-risk auto-merge only after a separate threat review and a
   substantial history of clean runs.

The phone-first operating model is documented in
[Phone Control](docs/phone-control.md).

The publisher App setup is documented in
[GitHub Publisher App](docs/github-publisher-app.md).

The exact MarketMaestro activation sequence is documented in
[MarketMaestro Rollout](docs/marketmaestro-rollout.md).
