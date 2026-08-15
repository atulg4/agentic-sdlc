# Efficacy scoring, regression detection, and self-correction

`src/agentic_sdlc/efficacy.py` measures whether the autonomous pipeline is
actually working and improves it only through bounded, human-approved
experiments.

## Immutable outcomes

Every run ends in a `RunOutcome` linking work reference, mission version,
agent/model, context-pack digest, commit SHA, verification and review
results, repair rounds, human action, defect/reopen flags, policy
violations, cost, and timing. The `OutcomeLedger` is append-only: outcomes
cannot be rewritten or dropped, so failed, cancelled, abandoned, expensive,
and inconclusive runs stay in every denominator. Human overrides must carry a
classification (starting at `unclassified`) — they are recorded as outcomes,
never auto-scored as model failure or success.

## Versioned metrics, no opaque score

`compute_metrics` produces versioned `EfficacyMetric`s (completion,
first-pass verification/review, repair success, escaped defects, reopens,
human intervention, policy violations, cost, cycle time), each exposing its
formula, numerator/denominator, sample count, and a Wilson confidence
interval when the sample permits. Missing data is never success: an outcome
without a recorded review is absent from the acceptance-rate numerator *and*
denominator.

`build_report` separates quality, safety, autonomy, speed, cost, and
completion. The optional composite appears only above a minimum sample and
always exposes its components, weights, sample count, and limitations.
`compare_cohorts` refuses superiority claims on small or incomparable
(different task-class) cohorts.

## Correction loop

- Repeated `QualityFinding`s (deduplicated by fingerprint) converge into one
  `CorrectionProposal` each, with the supporting work references as evidence.
- `create_experiment` requires an evidence-backed hypothesis and changes
  exactly one bounded variable (mission prompt, routing rule, context-pack
  composition, agent selection, verification gate, concurrency/retry limit,
  or decomposition strategy), in shadow or cohort mode.
- `evaluate_experiment` scores the challenger only on out-of-sample,
  comparable cohorts — the observations that generated the hypothesis are
  rejected as evaluation data. Cost or speed gains can never compensate for
  degraded safety or quality metrics; such challengers are rejected.
- `promote_experiment` requires a named human approver, and a high-risk
  change cannot be evaluated and promoted by its own proposer.
- `PolicyVersionStore` keeps content-addressed policy versions with an
  explicit approved set; `rollback` restores only a previously approved
  version and records the guardrail reason — the platform never invents a
  new production policy autonomously.

The efficacy manager holds no merge, deploy, permission, or secret
authority; its outputs are reports, proposals, and experiments that feed the
#16 lifecycle and #20 intake queue as ordinary reviewable work.
