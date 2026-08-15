from __future__ import annotations

import pytest

from agentic_sdlc.efficacy import (
    EfficacyError,
    OutcomeLedger,
    PolicyVersionStore,
    QualityFinding,
    RunOutcome,
    build_report,
    compare_cohorts,
    compute_metrics,
    create_experiment,
    evaluate_experiment,
    promote_experiment,
    propose_corrections,
    rollback,
)


def _outcome(work_ref: str, **overrides) -> RunOutcome:
    values = {
        "work_ref": work_ref,
        "mission_id": "implementation-worker",
        "mission_version": "1.0.0",
        "agent_id": "codex-1",
        "model": "gpt-5",
        "task_class": "bug",
        "cohort": "example/project",
        "result": "completed",
        "verification_passed": True,
        "review_findings": 0,
        "merged": True,
        "tokens_used": 1000,
        "cycle_time_seconds": 600,
        "finished_at": "2026-08-06T12:00:00Z",
    }
    values.update(overrides)
    return RunOutcome(**values)


def _cohort(prefix: str, count: int, **overrides) -> list[RunOutcome]:
    return [_outcome(f"{prefix}-{index}", **overrides) for index in range(count)]


def test_ledger_is_append_only_and_immutable() -> None:
    ledger = OutcomeLedger()
    outcome = _outcome("w1")
    outcome_id = ledger.record(outcome)
    # Idempotent re-record of the identical outcome is fine.
    assert ledger.record(outcome) == outcome_id
    # Rewriting history is not.
    tampered = _outcome("w1", merged=False)
    assert tampered.outcome_id == outcome.outcome_id
    with pytest.raises(EfficacyError, match="immutable"):
        ledger.record(tampered)
    assert len(ledger) == 1


def test_failed_and_abandoned_runs_remain_in_denominators() -> None:
    outcomes = [
        _outcome("w1"),
        _outcome("w2", result="failed", verification_passed=False, merged=False),
        _outcome("w3", result="abandoned", verification_passed=None, merged=False),
        _outcome("w4", result="cancelled", verification_passed=None, merged=False),
    ]
    metrics = compute_metrics(outcomes)
    completion = metrics["task_completion_rate"]
    assert completion.denominator == 4
    assert completion.value == 0.25
    assert "stay in the denominator" in completion.formula


def test_missing_data_is_never_interpreted_as_success() -> None:
    inconclusive = _outcome(
        "w1",
        result="inconclusive",
        verification_passed=None,
        review_findings=None,
        merged=False,
    )
    metrics = compute_metrics([inconclusive])
    assert metrics["task_completion_rate"].value == 0.0
    assert metrics["first_pass_verification_rate"].value == 0.0
    # No recorded review means the outcome is absent from the review-rate
    # numerator AND denominator — not silently counted as accepted.
    assert metrics["first_pass_review_acceptance_rate"].denominator == 0
    assert metrics["first_pass_review_acceptance_rate"].value == 0.0


def test_human_overrides_require_classification() -> None:
    with pytest.raises(EfficacyError, match="classification"):
        OutcomeLedger().record(_outcome("w1", human_action="overridden", human_override_class=""))
    ledger = OutcomeLedger()
    ledger.record(_outcome("w1", human_action="overridden", human_override_class="unclassified"))


def test_report_separates_dimensions_and_gates_the_composite() -> None:
    ledger = OutcomeLedger()
    for outcome in _cohort("w", 5):
        ledger.record(outcome)
    report = build_report(ledger, min_sample=20)
    assert set(report["dimensions"]) == {
        "quality",
        "safety",
        "autonomy",
        "speed",
        "cost",
        "completion",
    }
    assert report["composite"] is None
    assert "below minimum" in report["compositeSuppressedReason"]

    for outcome in _cohort("x", 20):
        ledger.record(outcome)
    full = build_report(ledger, min_sample=20)
    assert full["composite"] is not None
    assert set(full["composite"]["components"]) == {"quality", "completion", "safety"}
    assert full["composite"]["limitations"]
    quality_metric = full["dimensions"]["quality"][0]
    assert quality_metric["confidenceInterval"] is not None  # n >= 10


def test_small_or_incomparable_samples_block_superiority_claims() -> None:
    with pytest.raises(EfficacyError, match="insufficient samples"):
        compare_cohorts(_cohort("a", 5), _cohort("b", 30))
    with pytest.raises(EfficacyError, match="not comparable"):
        compare_cohorts(
            _cohort("a", 20, task_class="bug"),
            _cohort("b", 20, task_class="feature"),
        )
    comparison = compare_cohorts(_cohort("a", 20), _cohort("b", 20))
    assert comparison["baseline"]["task_completion_rate"]["sampleCount"] == 20


def test_repeated_findings_generate_one_deduplicated_proposal() -> None:
    finding = QualityFinding(
        work_ref="w1",
        mission_id="implementation-worker",
        category="missing-tests",
        severity="medium",
        description="Patch shipped without regression tests for the changed module.",
    )
    repeats = [
        QualityFinding(
            work_ref=f"w{index}",
            mission_id=finding.mission_id,
            category=finding.category,
            severity="medium",
            description=finding.description,
        )
        for index in range(1, 5)
    ]
    rare = QualityFinding(
        work_ref="w9",
        mission_id="implementation-worker",
        category="naming",
        severity="low",
        description="One-off style nit.",
    )
    proposals = propose_corrections([*repeats, rare], min_occurrences=3)
    assert len(proposals) == 1
    proposal = proposals[0].as_dict()
    assert proposal["occurrences"] == 4
    assert proposal["evidence"] == ["w1", "w2", "w3", "w4"]
    # Running again over the same findings yields the same single proposal.
    assert propose_corrections([*repeats, rare], min_occurrences=3) == proposals


def _experiment(hypothesis_outcomes, **overrides):
    values = {
        "hypothesis": "Tighter context packs reduce review findings.",
        "hypothesis_evidence": ("cp-report-1", "review-report-7"),
        "hypothesis_outcomes": hypothesis_outcomes,
        "variable": "context-pack-composition",
        "baseline_version": "pv-base",
        "challenger_version": "pv-chal",
        "mode": "shadow",
        "proposed_by": "efficacy-manager",
    }
    values.update(overrides)
    return create_experiment(**values)


def test_challenger_cannot_be_promoted_from_in_sample_performance() -> None:
    history = _cohort("h", 20)
    experiment = _experiment(history)
    with pytest.raises(EfficacyError, match="in-sample"):
        evaluate_experiment(
            experiment,
            _cohort("base", 20),
            history,  # same observations that produced the hypothesis
            evaluated_by="claude-1",
        )


def test_cost_improvements_cannot_hide_degraded_safety_or_quality() -> None:
    experiment = _experiment(_cohort("h", 5))
    baseline = _cohort("base", 20, tokens_used=2000)
    cheap_but_unsafe = _cohort("chal", 20, tokens_used=100, policy_violations=1, review_findings=2)
    evaluated = evaluate_experiment(experiment, baseline, cheap_but_unsafe, evaluated_by="claude-1")
    assert evaluated.status == "rejected"
    assert "cannot compensate" in evaluated.decision_reason
    store = PolicyVersionStore()
    with pytest.raises(EfficacyError, match="only evaluated experiments"):
        promote_experiment(evaluated, store, approved_by="atulg4")


def test_promotion_requires_human_and_independent_review_for_high_risk() -> None:
    baseline = _cohort("base", 20, tokens_used=2000)
    better = _cohort("chal", 20, tokens_used=800)
    store = PolicyVersionStore()
    challenger_version = store.register("challenger-policy-doc")

    experiment = _experiment(
        _cohort("h", 5),
        challenger_version=challenger_version,
        high_risk=True,
        proposed_by="efficacy-manager",
    )
    evaluated = evaluate_experiment(experiment, baseline, better, evaluated_by="efficacy-manager")
    assert evaluated.status == "evaluated"
    with pytest.raises(EfficacyError, match="independent review"):
        promote_experiment(evaluated, store, approved_by="atulg4")

    independent = evaluate_experiment(
        _experiment(
            _cohort("h", 5),
            challenger_version=challenger_version,
            high_risk=True,
            hypothesis="Tighter context packs reduce review findings again.",
        ),
        baseline,
        better,
        evaluated_by="claude-reviewer",
    )
    with pytest.raises(EfficacyError, match="named human approver"):
        promote_experiment(independent, store, approved_by="  ")
    # The proposer can never approve its own high-risk change, even when an
    # independent party evaluated it.
    with pytest.raises(EfficacyError, match="independent approval"):
        promote_experiment(independent, store, approved_by="efficacy-manager")
    promoted = promote_experiment(independent, store, approved_by="atulg4")
    assert promoted.status == "promoted"
    assert store.is_approved(challenger_version)


def test_rollback_restores_only_approved_content_addressed_versions() -> None:
    store = PolicyVersionStore()
    approved = store.register("safe-policy-v1", approved_by="atulg4")
    unapproved = store.register("experimental-policy")
    assert rollback(store, target_version=approved, reason="guardrail breach") == ("safe-policy-v1")
    with pytest.raises(EfficacyError, match="previously approved"):
        rollback(store, target_version=unapproved, reason="guardrail breach")
    with pytest.raises(EfficacyError, match="reason"):
        rollback(store, target_version=approved, reason=" ")


def test_experiments_change_one_bounded_variable_with_evidence() -> None:
    with pytest.raises(EfficacyError, match="bounded variable"):
        _experiment(_cohort("h", 2), variable="rewrite-everything")
    with pytest.raises(EfficacyError, match="evidence-backed"):
        _experiment(_cohort("h", 2), hypothesis_evidence=())
    experiment = _experiment(_cohort("h", 2))
    document = experiment.as_dict()
    assert document["variable"] == "context-pack-composition"
    assert document["status"] == "proposed"


def test_outcomes_link_full_attribution() -> None:
    outcome = _outcome(
        "w1",
        context_pack_digest="deadbeef",
        commit_sha="a" * 40,
    )
    document = outcome.as_dict()
    assert document["missionVersion"] == "1.0.0"
    assert document["contextPackDigest"] == "deadbeef"
    assert document["commitSha"] == "a" * 40
    assert document["humanAction"] == "none"
