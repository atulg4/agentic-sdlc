# Quality evidence and budget-aware assurance

Agentic SDLC never redefines quality downward merely because a project has a small budget. It reports absolute evidence-backed quality and separately reports whether the result is budget-optimal.

## Quality dimensions

Verified quality is composed from independent dimensions rather than a single reviewer opinion:

- requirements and acceptance-criteria coverage
- deterministic test evidence
- hidden/independent test evidence when available
- regression evidence
- static/type/lint/security analysis
- independent review findings and severity-weighted recall/precision
- maintainability/change-risk indicators
- domain-specific validation
- realized post-merge outcomes

Weights are versioned by task class and risk class. Safety-critical dimensions are hard constraints and cannot be compensated for by speed or cost.

## Budget modes

### Hard budget

Maximize expected quality subject to cost <= budget.

If the best feasible result is below the project's required quality floor, surface that explicitly.

### Quality-floor mode

Minimize expected cost subject to conservative quality lower bound >= required quality.

This is the default for high-risk/security/financial work.

## Conservative estimates

A model with three successful runs is not treated as 100% reliable. Quality estimates include uncertainty and a conservative lower bound. New or sparsely observed models are restricted to low-risk exploration until sufficient evidence accumulates.

## Judge independence

The implementing executor cannot be the sole judge. Merge-time evidence must include deterministic verification and, when policy requires, an independent reviewer or specialist evaluator. Review-provider diversity may be rewarded for high-risk work to reduce correlated failure modes.

## Example

For a $5 hard budget, the router may produce:

- verified quality estimate: 82/100
- required project floor: 95/100
- budget efficiency: best currently feasible under $5
- status: below required assurance; do not auto-merge high-risk work

For a $200 budget it may obtain 98/100 with stronger planning, redundant independent review and higher-assurance verification. The scale remains the same in both cases.
