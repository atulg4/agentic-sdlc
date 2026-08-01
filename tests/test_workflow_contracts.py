from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _job(document: str, name: str, next_name: str | None = None) -> str:
    start = document.index(f"\n  {name}:\n")
    if next_name is None:
        return document[start:]
    end = document.index(f"\n  {next_name}:\n", start + 1)
    return document[start:end]


def test_platform_workflows_pin_external_actions_to_full_sha() -> None:
    pattern = re.compile(r"^[ \t]*(?:-[ \t]+)?uses:[ \t]+[^\s]+@([^\s#]+)", re.MULTILINE)
    for workflow in WORKFLOWS.glob("*.yml"):
        references = pattern.findall(workflow.read_text(encoding="utf-8"))
        assert references, f"{workflow.name} contains no action references"
        assert all(re.fullmatch(r"[0-9a-f]{40}", ref) for ref in references), workflow.name


def test_implementation_separates_ai_verifier_and_publisher_authority() -> None:
    document = (WORKFLOWS / "reusable-implement.yml").read_text(encoding="utf-8")
    generator = _job(document, "generate_patch", "verify")
    verifier = _job(document, "verify", "attest_patch")
    attestor = _job(document, "attest_patch", "publish_draft")
    publisher = _job(document, "publish_draft")

    assert "OPENAI_API_KEY" in generator
    assert "ANTHROPIC_API_KEY" in generator
    assert "PUBLISHER_APP_PRIVATE_KEY" not in generator
    assert "Refuse a duplicate open pull request" in document
    assert "contents: write" not in generator
    assert "API_KEY" not in verifier
    assert "PUBLISHER_APP_PRIVATE_KEY" not in verifier
    assert "contents: write" not in verifier
    assert "run-gates" in verifier
    assert "verified-patch-${{ github.run_id }}" not in verifier
    assert "API_KEY" not in attestor
    assert "PUBLISHER_APP_PRIVATE_KEY" not in attestor
    assert "contents: write" not in attestor
    assert "run-gates" not in attestor
    assert "generated-patch-${{ github.run_id }}" in attestor
    assert "verified-patch-${{ github.run_id }}" in attestor
    assert "actions/create-github-app-token@" in publisher
    assert "permission-contents: read" in publisher
    assert "permission-issues: write" in publisher
    assert "permission-pull-requests: write" in publisher
    assert "token: ${{ github.token }}" in publisher
    assert "GH_TOKEN: ${{ steps.publisher-token.outputs.token }}" in publisher
    assert "GH_TOKEN: ${{ github.token }}" not in publisher
    assert "API_KEY" not in publisher
    assert "PUBLISHER_APP_PRIVATE_KEY" in publisher
    assert "codex-action" not in publisher
    assert "claude-code-action" not in publisher
    assert "verified-patch-${{ github.run_id }}" in publisher
    assert "needs: [prepare, attest_patch]" in publisher


def test_implementation_caller_grants_only_branch_write_to_native_token() -> None:
    document = (ROOT / "src/agentic_sdlc/templates/github/agent-implement.yml").read_text(
        encoding="utf-8"
    )

    assert "contents: write" in document
    assert "issues: write" not in document
    assert "pull-requests: write" not in document
    assert "publisher_app_client_id: ${{ vars.PUBLISHER_APP_CLIENT_ID }}" in document
    assert "PUBLISHER_APP_PRIVATE_KEY" in document


def test_plan_and_implementation_require_write_authority_from_trigger_actor() -> None:
    for name in ("reusable-plan.yml", "reusable-implement.yml"):
        document = (WORKFLOWS / name).read_text(encoding="utf-8")
        assert "trigger_actor:" in document
        assert "collaborators/${encoded_actor}/permission" in document
        assert "admin|write" in document
        assert "refs/remotes/origin/${DEFAULT_BRANCH}^{commit}" in document
        assert "must start from the protected default branch" in document


def test_label_driven_templates_use_distinct_exact_trigger_labels() -> None:
    template_root = ROOT / "src/agentic_sdlc/templates/github"
    plan = (template_root / "agent-auto-plan.yml").read_text(encoding="utf-8")
    implementation = (template_root / "agent-auto-implement.yml").read_text(encoding="utf-8")

    assert "github.event.label.name == 'agent-ready'" in plan
    assert "github.event.label.name == 'implementation-approved'" in implementation
    assert "trigger_actor: ${{ github.actor }}" in plan
    assert "trigger_actor: ${{ github.actor }}" in implementation


def test_cross_repository_identifiers_use_string_contracts() -> None:
    reusable_inputs = {
        "reusable-plan.yml": "issue_number",
        "reusable-implement.yml": "issue_number",
        "reusable-review.yml": "pull_request_number",
    }
    for filename, input_name in reusable_inputs.items():
        document = (WORKFLOWS / filename).read_text(encoding="utf-8")
        definition = re.search(
            rf"^      {input_name}:\n(?P<body>(?:        .*\n)+)",
            document,
            re.MULTILINE,
        )
        assert definition is not None, filename
        assert "        type: string\n" in definition.group("body"), filename

    template_root = ROOT / "src/agentic_sdlc/templates/github"
    for filename in ("agent-plan.yml", "agent-implement.yml"):
        document = (template_root / filename).read_text(encoding="utf-8")
        definition = re.search(
            r"^      issue_number:\n(?P<body>(?:        .*\n)+)",
            document,
            re.MULTILINE,
        )
        assert definition is not None, filename
        assert "        type: string\n" in definition.group("body"), filename

    auto_plan = (template_root / "agent-auto-plan.yml").read_text(encoding="utf-8")
    auto_implement = (template_root / "agent-auto-implement.yml").read_text(encoding="utf-8")
    review = (template_root / "agent-review.yml").read_text(encoding="utf-8")
    issue_cast = "issue_number: ${{ format('{0}', github.event.issue.number) }}"
    assert issue_cast in auto_plan
    assert issue_cast in auto_implement
    assert "pull_request_number: ${{ format('{0}', github.event.pull_request.number) }}" in review


def test_reusable_identifiers_are_allowlisted_before_api_use() -> None:
    issue_allowlist = '[[ "$ISSUE_NUMBER" =~ ^[1-9][0-9]{0,9}$ ]]'
    for name in ("reusable-plan.yml", "reusable-implement.yml"):
        document = (WORKFLOWS / name).read_text(encoding="utf-8")
        assert issue_allowlist in document
        assert document.index(issue_allowlist) < document.index(
            "Read issue without shell interpolation"
        )

    review = (WORKFLOWS / "reusable-review.yml").read_text(encoding="utf-8")
    pr_allowlist = '[[ "$PR_NUMBER" =~ ^[1-9][0-9]{0,9}$ ]]'
    assert pr_allowlist in review
    assert review.index(pr_allowlist) < review.index("Validate and publish review")


def test_legacy_and_example_workflows_match_identifier_contracts() -> None:
    issue_cast = "issue_number: ${{ format('{0}', github.event.issue.number) }}"
    for path in (
        ROOT / "templates/github/auto-plan.yml",
        ROOT / "templates/github/auto-implement.yml",
        ROOT / "examples/marketmaestro/.github/workflows/agent-auto-plan.yml",
        ROOT / "examples/marketmaestro/.github/workflows/agent-auto-implement.yml",
    ):
        assert issue_cast in path.read_text(encoding="utf-8"), path

    example_root = ROOT / "examples/marketmaestro/.github/workflows"
    for name in ("agent-plan.yml", "agent-implement.yml"):
        document = (example_root / name).read_text(encoding="utf-8")
        definition = re.search(
            r"^      issue_number:\n(?P<body>(?:        .*\n)+)",
            document,
            re.MULTILINE,
        )
        assert definition is not None, name
        assert "        type: string\n" in definition.group("body"), name

    review = (example_root / "agent-review.yml").read_text(encoding="utf-8")
    assert "pull_request_number: ${{ format('{0}', github.event.pull_request.number) }}" in review


def test_plan_and_review_publishers_receive_no_ai_credentials() -> None:
    plan = (WORKFLOWS / "reusable-plan.yml").read_text(encoding="utf-8")
    review = (WORKFLOWS / "reusable-review.yml").read_text(encoding="utf-8")

    assert "API_KEY" not in _job(plan, "publish")
    review_publisher = _job(review, "enforce_review")
    assert "API_KEY" not in review_publisher
    assert "pull-requests: write" not in review_publisher


def test_secret_bearing_workflows_never_use_pull_request_target() -> None:
    for workflow in WORKFLOWS.glob("*.yml"):
        assert "pull_request_target" not in workflow.read_text(encoding="utf-8")
