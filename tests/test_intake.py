from __future__ import annotations

from pathlib import Path

import pytest

from agentic_sdlc.intake import (
    INTAKE_CONTRACTS,
    GitHubCIIntake,
    GitHubIssueIntake,
    GitHubPullRequestIntake,
    IntakeError,
    IntakeSource,
    RoutingRules,
    WorkQueue,
    check_intake_conformance,
    load_intake_sources,
    prioritize,
    route,
)
from agentic_sdlc.policy import evaluate_diff, load_policy


def _source(source_id: str = "gh-issues", source_type: str = "github-issues") -> IntakeSource:
    return IntakeSource(source_id=source_id, source_type=source_type, provider="github")


def _issue_payload(number: int = 7, labels: tuple[str, ...] = ("bug",), body: str = "It broke."):
    return {
        "repository": {"full_name": "example/project"},
        "issue": {
            "number": number,
            "title": f"Something is wrong #{number}",
            "body": body,
            "labels": [{"name": label} for label in labels],
            "html_url": f"https://github.com/example/project/issues/{number}",
            "updated_at": "2026-08-06T10:00:00Z",
        },
    }


def _ci_payload(run_id: int = 100, sha: str = "a" * 40, pr: int | None = 12):
    return {
        "repository": {"full_name": "example/project"},
        "workflow_run": {
            "id": run_id,
            "name": "ci",
            "conclusion": "failure",
            "head_branch": "feature/x",
            "head_sha": sha,
            "html_url": f"https://github.com/example/project/actions/runs/{run_id}",
            "pull_requests": [{"number": pr}] if pr is not None else [],
            "updated_at": "2026-08-06T11:00:00Z",
        },
    }


def _sources_file(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "intake.toml"
    path.write_text(body.strip() + "\n", encoding="utf-8")
    return path


def test_duplicate_webhook_and_polling_events_converge_to_one_request() -> None:
    adapter = GitHubIssueIntake()
    queue = WorkQueue()
    webhook = adapter.normalize(_source(), _issue_payload(), received_at="T1")
    polled = adapter.normalize(_source(), _issue_payload(), received_at="T2")
    first = queue.submit(webhook)
    second = queue.submit(polled)
    assert first.request_id == second.request_id
    report = queue.report()
    assert report["total"] == 1
    assert second.version == 2
    assert second.lifecycle_state == "deduplicated"


def test_ci_failure_updates_existing_related_bug_when_fingerprints_match() -> None:
    adapter = GitHubCIIntake()
    queue = WorkQueue()
    first = queue.submit(adapter.normalize(_source("gh-ci", "github-ci"), _ci_payload(100)))
    # A retried/new failing run of the same workflow+branch converges.
    second = queue.submit(adapter.normalize(_source("gh-ci", "github-ci"), _ci_payload(101)))
    assert second.request_id == first.request_id
    assert second.version == 2
    assert len({item for item in second.evidence}) == 2  # both run URLs retained


def test_ci_failure_correlates_with_its_pull_request() -> None:
    adapter = GitHubCIIntake()
    request = adapter.normalize(_source("gh-ci", "github-ci"), _ci_payload(pr=12))
    assert "example/project#12" in request.linked_work
    no_pr = adapter.normalize(_source("gh-ci", "github-ci"), _ci_payload(run_id=102, pr=None))
    assert no_pr.linked_work == ("example/project@" + "a" * 40,)


def test_malformed_and_unauthenticated_events_fail_closed() -> None:
    adapter = GitHubIssueIntake()
    with pytest.raises(IntakeError, match="requires issue and repository"):
        adapter.normalize(_source(), {"zen": "keep it simple"})
    # A source of the wrong type cannot feed the adapter.
    with pytest.raises(IntakeError, match="cannot feed"):
        adapter.normalize(_source("gh-ci", "github-ci"), _issue_payload())
    # A source without create-work authority cannot create work.
    silenced = IntakeSource(
        source_id="observer",
        source_type="github-issues",
        provider="github",
        permitted_actions=(),
    )
    with pytest.raises(IntakeError, match="not permitted to create work"):
        adapter.normalize(silenced, _issue_payload())
    # Successful CI runs never become work.
    ci = GitHubCIIntake()
    payload = _ci_payload()
    payload["workflow_run"]["conclusion"] = "success"
    with pytest.raises(IntakeError, match="only failed runs"):
        ci.normalize(_source("gh-ci", "github-ci"), payload)


def test_source_adapter_cannot_grant_itself_write_authority(tmp_path: Path) -> None:
    body = """
version = 1

[[source]]
id = "jira-main"
type = "jira"
provider = "atlassian"
permitted_actions = ["create-work", "close-ticket"]
"""
    with pytest.raises(IntakeError, match="cannot hold write authority: close-ticket"):
        load_intake_sources(_sources_file(tmp_path, body))

    class ClosingAdapter(GitHubIssueIntake):
        def close(self, *args, **kwargs):  # pragma: no cover - never called
            raise AssertionError

    with pytest.raises(IntakeError, match="must not expose ticket write operations"):
        check_intake_conformance(ClosingAdapter(), _source(), [{}])


def test_intake_sources_load_and_config_is_protected(tmp_path: Path, policy_file: Path) -> None:
    body = """
version = 1

[[source]]
id = "gh-issues"
type = "github-issues"
provider = "github"

[[source]]
id = "gh-ci"
type = "github-ci"
provider = "github"
credential_ref = "CI_READ_TOKEN"
"""
    sources = load_intake_sources(_sources_file(tmp_path, body))
    assert set(sources) == {"gh-issues", "gh-ci"}
    decision = evaluate_diff(("intake.toml",), 1, 0, load_policy(policy_file))
    assert not decision.allowed


def test_priority_is_deterministic_and_explainable() -> None:
    adapter = GitHubIssueIntake()
    request = adapter.normalize(_source(), _issue_payload(labels=("security",)))
    first = prioritize(request)
    second = prioritize(request)
    assert first == second
    document = first.as_dict()
    assert document["formulaVersion"] == "1.0.0"
    assert set(document["components"]) == {"urgency", "impact", "confidence", "effort", "risk"}
    assert document["reasons"]
    assert document["total"] == first.total
    assert first.preemptive  # high-severity security preempts ordinary work

    # Consumer overrides adjust impact but cannot remove the safety floor.
    overridden = prioritize(request, impact_override=0)
    assert overridden.preemptive
    assert overridden.impact == 0
    with pytest.raises(IntakeError, match="between 0 and 100"):
        prioritize(request, impact_override=500)


def test_low_risk_and_protected_fixtures_route_differently() -> None:
    adapter = GitHubIssueIntake()
    rules = RoutingRules()
    docs = adapter.normalize(_source(), _issue_payload(number=1, labels=("documentation",)))
    security = adapter.normalize(_source(), _issue_payload(number=2, labels=("security",)))
    assert route(docs, rules)["action"] == "auto-plan"
    decision = route(security, rules)
    assert decision["action"] == "approval-required"
    assert "evidence is preserved" in decision["reason"]


def test_low_confidence_work_stays_an_investigation() -> None:
    ci = GitHubCIIntake()
    request = ci.normalize(_source("gh-ci", "github-ci"), _ci_payload())
    assert request.confidence == 70
    decision = route(request, RoutingRules(min_confidence=80))
    assert decision["action"] == "investigate"


def test_ignore_rules_are_honored() -> None:
    ci = GitHubCIIntake()
    request = ci.normalize(_source("gh-ci", "github-ci"), _ci_payload())
    decision = route(request, RoutingRules(ignore_signatures=("ci@feature/x",)))
    assert decision["action"] == "ignore"


def test_contradictory_evidence_returns_work_to_triage() -> None:
    adapter = GitHubIssueIntake()
    queue = WorkQueue()
    request = queue.submit(adapter.normalize(_source(), _issue_payload()))
    queue.set_state(request.request_id, "planned")
    updated = queue.add_evidence(
        request.request_id,
        "https://github.com/example/project/issues/9 reports the opposite behavior",
        contradictory=True,
    )
    assert updated.lifecycle_state == "new"
    assert any("triage" in line for line in queue.report()["log"])


def test_queue_report_shows_lifecycle_buckets() -> None:
    adapter = GitHubIssueIntake()
    queue = WorkQueue()
    a = queue.submit(adapter.normalize(_source(), _issue_payload(number=1)))
    b = queue.submit(adapter.normalize(_source(), _issue_payload(number=2)))
    queue.set_state(b.request_id, "planned")
    report = queue.report()
    assert report["byState"]["new"] == [a.request_id]
    assert report["byState"]["planned"] == [b.request_id]
    assert report["total"] == 2


def test_pr_finding_intake_and_conformance_harness() -> None:
    adapter = GitHubPullRequestIntake()
    source = _source("gh-pulls", "github-pulls")
    payload = {
        "repository": {"full_name": "example/project"},
        "pull_request": {
            "number": 3,
            "html_url": "https://github.com/example/project/pull/3",
            "updated_at": "2026-08-06T10:00:00Z",
        },
        "finding": "The retry loop never backs off.",
        "severity": "high",
    }
    report = check_intake_conformance(adapter, source, [payload])
    assert report["passed"]
    request = adapter.normalize(source, payload)
    assert request.linked_work == ("example/project#3",)
    assert request.severity == "high"


def test_intake_contracts_are_read_only_and_authenticated() -> None:
    for name, contract in INTAKE_CONTRACTS.items():
        assert contract["writeSupport"] is False, name
        assert contract["authentication"], name
        assert contract["requiredFields"], name
