"""Fail-closed continuation decisions after an independent review completes.

This module is deliberately transport-free: trusted workflow/event adapters normalize
review and gate evidence, then a credential-isolated publisher executes the returned
action. AI jobs never receive merge authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .autonomy import AutonomyGateEvidence, AutonomyPhase, evaluate_autonomy

__all__ = ["ContinuationAction", "ContinuationDecision", "decide_review_continuation"]


class ContinuationAction(StrEnum):
    REPAIR = "repair"
    MERGE = "merge"
    BLOCK = "block"
    NOOP = "noop"


@dataclass(frozen=True)
class ContinuationDecision:
    action: ContinuationAction
    reason: str
    repair_cycle: int


def decide_review_continuation(
    *,
    verdict: str,
    reviewed_head_sha: str,
    current_head_sha: str,
    repair_cycle: int,
    max_repair_cycles: int,
    merge_evidence: AutonomyGateEvidence,
) -> ContinuationDecision:
    """Choose the next exact-head action after review, idempotently and fail-closed.

    A stale review can never trigger repair or merge. Changes-requested starts another
    repair only while the configured retry budget remains. Approval requests merge
    only when every trusted autonomous-merge gate passes; the actual merge remains in
    the separate protected merge executor.
    """
    normalized = verdict.strip().lower().replace("-", "_")
    if not reviewed_head_sha or reviewed_head_sha != current_head_sha:
        return ContinuationDecision(ContinuationAction.NOOP, "review is not for the current exact head", repair_cycle)
    if repair_cycle < 0 or max_repair_cycles < 0:
        return ContinuationDecision(ContinuationAction.BLOCK, "repair counters must be non-negative", repair_cycle)
    if normalized in {"changes_requested", "request_changes"}:
        if repair_cycle >= max_repair_cycles:
            return ContinuationDecision(ContinuationAction.BLOCK, "bounded repair budget exhausted", repair_cycle)
        return ContinuationDecision(ContinuationAction.REPAIR, "independent review requested changes", repair_cycle + 1)
    if normalized in {"approved", "approve"}:
        decision = evaluate_autonomy(AutonomyPhase.MERGE, merge_evidence)
        if not decision.allowed:
            return ContinuationDecision(ContinuationAction.BLOCK, "; ".join(decision.reasons), repair_cycle)
        return ContinuationDecision(ContinuationAction.MERGE, "exact-head review and merge gates passed", repair_cycle)
    return ContinuationDecision(ContinuationAction.BLOCK, f"unsupported or non-terminal review verdict: {verdict}", repair_cycle)
