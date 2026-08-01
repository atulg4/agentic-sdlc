# MarketMaestro Agent Guide

## Authority

The repository owner defines product intent and is the final merge authority.
Codex is the primary architect, planner, and independent reviewer. Claude Code
or Codex may implement an approved, bounded work request.

## Required workflow

1. Read the relevant code and `REVIEW.md` before proposing changes.
2. Require complete Acceptance Criteria, Required Tests, Non-Goals, and
   Dependencies sections.
3. Plan before implementation.
4. Add a failing test for changed behavior and prove the failure is relevant.
5. Make the smallest scoped implementation.
6. Run changed-file quality checks and the affected deterministic tests.
7. Open a draft PR only. Never merge, approve, deploy, or trade.

## MarketMaestro invariants

- Never invent financial formulas or expected returns.
- Never access or alter live brokerage credentials or production position data.
- Treat authentication, preview sharing, trading, migrations, and confidence
  calculations as high-risk areas.
- Preserve point-in-time behavior and avoid look-ahead in research/backtests.
- Missing data must remain visible and must never be fabricated.
- All public endpoints use explicit response allowlists and generic errors.

The controlling machine-readable policy is `agentic-sdlc.toml`.
