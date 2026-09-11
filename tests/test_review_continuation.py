from agentic_sdlc.autonomy import AutonomyGateEvidence
from agentic_sdlc.review_continuation import ContinuationAction, decide_review_continuation


def _green_merge_evidence() -> AutonomyGateEvidence:
    return AutonomyGateEvidence(
        forge_managed=True,
        scope_bounded=True,
        security_clear=True,
        high_risk=False,
        deterministic_verification_passed=True,
        quality_passed=True,
        independent_review_approved=True,
        secret_scan_passed=True,
        branch_protection_allows=True,
        unresolved_conversations=0,
    )


def test_changes_requested_starts_next_bounded_repair_cycle():
    decision = decide_review_continuation(
        verdict="changes_requested",
        reviewed_head_sha="a" * 40,
        current_head_sha="a" * 40,
        repair_cycle=0,
        max_repair_cycles=2,
        merge_evidence=AutonomyGateEvidence(),
    )
    assert decision.action is ContinuationAction.REPAIR
    assert decision.repair_cycle == 1


def test_repair_budget_exhaustion_fails_closed():
    decision = decide_review_continuation(
        verdict="changes_requested",
        reviewed_head_sha="a" * 40,
        current_head_sha="a" * 40,
        repair_cycle=2,
        max_repair_cycles=2,
        merge_evidence=AutonomyGateEvidence(),
    )
    assert decision.action is ContinuationAction.BLOCK


def test_stale_review_is_noop_and_cannot_repair_or_merge():
    decision = decide_review_continuation(
        verdict="approved",
        reviewed_head_sha="a" * 40,
        current_head_sha="b" * 40,
        repair_cycle=0,
        max_repair_cycles=2,
        merge_evidence=_green_merge_evidence(),
    )
    assert decision.action is ContinuationAction.NOOP


def test_approval_with_all_trusted_gates_requests_protected_merge():
    decision = decide_review_continuation(
        verdict="approved",
        reviewed_head_sha="a" * 40,
        current_head_sha="a" * 40,
        repair_cycle=1,
        max_repair_cycles=2,
        merge_evidence=_green_merge_evidence(),
    )
    assert decision.action is ContinuationAction.MERGE


def test_approval_with_missing_gate_blocks_instead_of_merging():
    decision = decide_review_continuation(
        verdict="approved",
        reviewed_head_sha="a" * 40,
        current_head_sha="a" * 40,
        repair_cycle=0,
        max_repair_cycles=2,
        merge_evidence=AutonomyGateEvidence(),
    )
    assert decision.action is ContinuationAction.BLOCK
    assert "forge-managed" in decision.reason
