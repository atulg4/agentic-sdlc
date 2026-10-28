from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


_JOB_HEADER = re.compile(r"\n  [A-Za-z_][\w-]*:\n")


def _job(document: str, name: str, next_name: str | None = None) -> str:
    """The text of job `name`: up to `next_name`'s header when given (which must follow it),
    and never past the next job's header, whichever job that is."""
    start = document.index(f"\n  {name}:\n")
    end = len(document)
    if next_name is not None:
        end = document.index(f"\n  {next_name}:\n", start + 1)
    following = _JOB_HEADER.search(document, start + 1)
    if following is not None:
        end = min(end, following.start())
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
    assert "CLAUDE_CODE_OAUTH_TOKEN" in generator
    assert "PUBLISHER_APP_PRIVATE_KEY" not in generator
    assert "Refuse a duplicate open pull request" in document
    assert "contents: write" not in generator
    assert "API_KEY" not in verifier
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in verifier
    assert "PUBLISHER_APP_PRIVATE_KEY" not in verifier
    assert "contents: write" not in verifier
    assert "run-gates" in verifier
    assert "verified-patch-${{ github.run_id }}" not in verifier
    assert "API_KEY" not in attestor
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in attestor
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
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in publisher
    assert "PUBLISHER_APP_PRIVATE_KEY" in publisher
    assert "codex-action" not in publisher
    assert "claude-code-action" not in publisher
    assert "verified-patch-${{ github.run_id }}" in publisher
    assert "needs: [prepare, attest_patch]" in publisher


def test_publishing_draft_state_is_a_caller_choice_defaulting_to_draft() -> None:
    document = (WORKFLOWS / "reusable-implement.yml").read_text(encoding="utf-8")
    publisher = _job(document, "publish_draft")

    # The flag must be a declared input, so a caller whose independent review
    # gates on `draft == false` can publish ready for review.
    assert "open_as_draft:" in document
    assert re.search(r"open_as_draft:\n(?:.*\n)*?\s+default: true\n", document), (
        "open_as_draft must default to true and keep the existing behaviour"
    )

    # The publisher must branch on the input rather than hardcoding --draft,
    # and must read it through env like every other value in that job.
    assert "OPEN_AS_DRAFT: ${{ inputs.open_as_draft }}" in publisher
    assert '[ "$OPEN_AS_DRAFT" = "true" ]' in publisher
    assert "\n            --draft \\\n" not in publisher, "--draft must not be unconditional"

    # The issue comment must not call a ready pull request a draft.
    assert "Verified ${state}: ${pr_url}" in publisher
    assert "Verified draft pull request: ${pr_url}" not in publisher


def test_claude_subscription_oauth_is_explicit_and_never_silently_falls_back() -> None:
    document = (WORKFLOWS / "reusable-implement.yml").read_text(encoding="utf-8")
    preparer = _job(document, "prepare", "generate_patch")
    generator = _job(document, "generate_patch", "verify")

    assert "claude_auth_mode:" in document
    assert "default: subscription_oauth" in document
    assert "subscription_oauth|direct_api" in document
    # The legacy input is translated into the routed executor auth mode in prepare,
    # and patch generation dispatches only on that recorded auth mode.
    assert "CLAUDE_AUTH_MODE: ${{ inputs.claude_auth_mode }}" in preparer
    assert "inputs.claude_auth_mode" not in generator
    assert "needs.prepare.outputs.selected_auth_mode == 'oauth'" in generator
    assert "claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}" in generator
    assert "needs.prepare.outputs.selected_auth_mode == 'api-key'" in generator
    assert "anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}" in generator

    caller = (ROOT / "src/agentic_sdlc/templates/github/agent-implement.yml").read_text(
        encoding="utf-8"
    )
    assert "claude_auth_mode: ${{ inputs.claude_auth_mode }}" in caller
    assert "CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}" in caller


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
    review_output = "needs.review_eligibility.outputs.pull_request_number"
    review_number = f"pull_request_number: ${{{{ {review_output} }}}}"
    assert issue_cast in auto_plan
    assert issue_cast in auto_implement
    assert review_number in review


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
    assert "pull-requests: write" in review_publisher
    assert "issues: write" not in review_publisher
    assert "actions/checkout" not in review_publisher


def test_review_caller_grants_only_required_comment_authority() -> None:
    document = (ROOT / "src/agentic_sdlc/templates/github/agent-review.yml").read_text(
        encoding="utf-8"
    )

    assert "contents: read" in document
    assert "pull-requests: write" in document
    assert "issues: write" not in document


def test_secret_bearing_workflows_never_use_pull_request_target() -> None:
    for workflow in WORKFLOWS.glob("*.yml"):
        assert "pull_request_target" not in workflow.read_text(encoding="utf-8")


def test_orchestrate_workflow_holds_no_ai_credentials_or_merge_authority() -> None:
    document = (WORKFLOWS / "reusable-orchestrate.yml").read_text(encoding="utf-8")

    assert "API_KEY" not in document
    assert "PUBLISHER_APP_PRIVATE_KEY" not in document
    assert "contents: write" not in document
    assert "merge" not in document.lower().replace("human merge", "").replace("merge authority", "")
    assert "orchestrate" in document
    assert "agentic-orchestration-state" in document
    assert "agentic-sdlc:status" in document


def test_orchestrate_consumer_fixture_is_thin_and_sha_pinned() -> None:
    document = (ROOT / "examples/marketmaestro/.github/workflows/agent-orchestrate.yml").read_text(
        encoding="utf-8"
    )

    assert (
        "uses: atulg4/agentic-sdlc/.github/workflows/reusable-orchestrate.yml"
        "@PLATFORM_COMMIT_SHA" in document
    )
    assert "platform_ref: PLATFORM_COMMIT_SHA" in document
    assert "python" not in document
    assert "sdlcctl" not in document
    assert "repair" not in document


def test_implementation_leases_the_issue_before_generating_and_releases_after() -> None:
    document = (WORKFLOWS / "reusable-implement.yml").read_text(encoding="utf-8")
    preparer = _job(document, "prepare", "generate_patch")
    publisher = _job(document, "publish_draft", "release_lease_on_failure")
    failure = _job(document, "release_lease_on_failure")

    assert (
        "      issues: read" in preparer
    )  # native token stays read-only; the App token writes the lease
    assert "permission-issues: write" in preparer and "actions/create-github-app-token@" in preparer
    assert "GH_TOKEN: ${{ steps.lease-token.outputs.token }}" in preparer
    assert "python3 -m agentic_sdlc claim" in preparer
    assert preparer.index("Refuse a duplicate open pull request") < preparer.index(
        "agentic_sdlc claim"
    )
    # Claim is the LAST step of prepare: a prepare that fails after claiming would skip
    # release_lease_on_failure (gated on prepare success) and strand the lease until it expires.
    assert preparer.index("Store validated implementation request") < preparer.index(
        "agentic_sdlc claim"
    )
    assert preparer.index("Store pinned policy engine") < preparer.index("agentic_sdlc claim")
    assert "\n      - " not in preparer[preparer.index("agentic_sdlc claim") :]
    # claim() lists open PRs, which an installation token may only do with pull-requests: read.
    lease_token = preparer[preparer.index("id: lease-token") :]
    lease_token = lease_token[: lease_token.index("\n      - ")]
    assert "permission-pull-requests: read" in lease_token
    assert '--session "run-${GITHUB_RUN_ID}"' in preparer
    assert "python3 -m agentic_sdlc release" in publisher
    assert publisher.index("gh pr create") < publisher.index("agentic_sdlc release")
    assert "python3 -m agentic_sdlc release" in failure
    assert "permission-issues: write" in failure and "contents: write" not in failure
    assert "GH_TOKEN: ${{ github.token }}" not in failure


def _workflow() -> dict:
    import yaml

    return yaml.safe_load((WORKFLOWS / "reusable-implement.yml").read_text(encoding="utf-8"))


def _named(steps: list[dict], fragment: str) -> int:
    found = [i for i, step in enumerate(steps) if fragment in str(step.get("name", ""))]
    assert len(found) == 1, f"{fragment!r} matched {len(found)} steps"
    return found[0]


def test_prepare_reports_how_far_its_claim_got() -> None:
    """release_lease_on_failure keys on prepare's `claim` output, not on prepare's result: a
    prepare that fails, is cancelled, or fails in a post step after the claim posted still
    has its lease released (Codex 4239510550, 4242643527, 4244145209, 4244276656)."""
    prepare = _workflow()["jobs"]["prepare"]
    steps = prepare["steps"]
    expression = prepare["outputs"]["claim"]
    assert expression == (
        "${{ steps.claim.outputs.state || steps.claim-start.outputs.state "
        "|| steps.claim-init.outputs.state }}"
    )
    assert steps[0]["id"] == "claim-init" and "state=not-started" in steps[0]["run"]
    start = next(i for i, s in enumerate(steps) if s.get("id") == "claim-start")
    claim = next(i for i, s in enumerate(steps) if s.get("id") == "claim")
    assert claim == len(steps) - 1 and start == claim - 1
    assert "state=started" in steps[start]["run"]
    run = steps[claim]["run"]
    assert "state=claimed" in run and "state=refused" in run
    assert '"$(jq -r \'.ok\' "${RUNNER_TEMP}/claim.json" 2>/dev/null)" = "false"' in run
    assert run.rstrip().endswith('exit "$status"')
    # the lease jobs' material is uploaded before anything is claimed
    for artifact in ("policy-engine-", "lease-policy-"):
        upload = next(
            i for i, s in enumerate(steps) if artifact in str((s.get("with") or {}).get("name"))
        )
        assert upload < start
    stage = steps[_named(steps, "Stage the protected-base policy")]["run"]
    assert 'cp -- "$CONFIG_PATH" .agentic-lease/agentic-sdlc.toml' in stage


_RENEWALS = {
    # renewal job -> (the job it follows, the job that waits for it)
    "renew_lease_before_verify": ("generate_patch", "verify"),
    "renew_lease_before_attest": ("verify", "attest_patch"),
}


def _renew_run(steps: list[dict]) -> str:
    runs = [s["run"] for s in steps if "agentic_sdlc renew" in str(s.get("run"))]
    assert len(runs) == 1
    return runs[0]


def test_the_lease_is_renewed_at_every_stage_boundary() -> None:
    """Codex 4203866655 et al.: the lease is renewed after generation, after verification and
    immediately before publishing, each time failing the run unless it still holds the lease
    of an open issue with no open PR. The renewals are jobs of their own: the AI and gate jobs
    never hold an installation token."""
    jobs = _workflow()["jobs"]
    for name, (after, before) in _RENEWALS.items():
        job = jobs[name]
        assert after in job["needs"] and "prepare" in job["needs"]
        assert name in jobs[before]["needs"]
        assert job["runs-on"] == "${{ fromJSON(inputs.runs_on) }}"
        assert job["permissions"] == {}
        token = next(s for s in job["steps"] if s.get("id") == "lease-token")
        assert token["uses"].startswith("actions/create-github-app-token@")
        assert token["with"]["permission-issues"] == "write"
        assert token["with"]["permission-pull-requests"] == "read"
        run = _renew_run(job["steps"])
        assert 'cd "${RUNNER_TEMP}/lease/policy"' in run
        assert "--config agentic-sdlc.toml" in run
        assert "|| true" not in run and "continue-on-error" not in job["steps"][-1]
        text = _job((WORKFLOWS / "reusable-implement.yml").read_text(encoding="utf-8"), name)
        assert "API_KEY" not in text and "CLAUDE_CODE_OAUTH_TOKEN" not in text
    for name in ("generate_patch", "verify", "attest_patch"):
        text = _job((WORKFLOWS / "reusable-implement.yml").read_text(encoding="utf-8"), name)
        assert "PUBLISHER_APP_PRIVATE_KEY" not in text and "lease-token" not in text


def test_publish_renews_immediately_before_pushing_and_never_hides_its_release() -> None:
    """Codex 4241839931, 4243659479: no push and no PR unless the renewal succeeds. Codex
    4204203796 et al.: the release is not `|| true`; a failure leaves `lease_released` unset
    and release_lease_on_failure retries it. Codex 4240839922: its note says whether a PR was
    opened."""
    publish = _workflow()["jobs"]["publish_draft"]
    steps = publish["steps"]
    renew = next(i for i, s in enumerate(steps) if "agentic_sdlc renew" in str(s.get("run")))
    push = next(i for i, s in enumerate(steps) if "git push" in str(s.get("run")))
    opened = next(i for i, s in enumerate(steps) if "gh pr create" in str(s.get("run")))
    verified = _named(steps, "Verify artifact without executing consumer code")
    assert verified < renew and renew + 1 == push < opened
    assert "if" not in steps[renew] and "continue-on-error" not in steps[renew]
    assert steps[renew]["env"]["GH_TOKEN"] == "${{ steps.publisher-token.outputs.token }}"
    assert 'printf \'pr_url=%s\\n\' "$pr_url" >> "$GITHUB_OUTPUT"' in steps[opened]["run"]
    assert steps[opened]["run"].index("pr_url=") < steps[opened]["run"].index("gh issue comment")
    release = steps[-1]
    assert release["id"] == "release" and "agentic_sdlc release" in release["run"]
    assert release["if"] == "always()" and release["continue-on-error"] is True
    assert "|| true" not in release["run"]
    assert release["run"].rstrip().endswith("printf 'released=true\\n' >> \"$GITHUB_OUTPUT\"")
    assert release["env"]["PR_URL"] == "${{ steps.open-pr.outputs.pr_url }}"
    assert 'if [ -n "$PR_URL" ]; then' in release["run"]
    assert "This run did not open a draft pull request" in release["run"]
    assert publish["outputs"] == {
        "pr_url": "${{ steps.open-pr.outputs.pr_url }}",
        "lease_released": "${{ steps.release.outputs.released }}",
    }
    # the lease commands run outside the checkout the patch lands in
    for i in (renew, len(steps) - 1):
        assert 'cd "${RUNNER_TEMP}/lease/policy"' in steps[i]["run"]
        assert 'PYTHONPATH="${RUNNER_TEMP}/lease/engine"' in steps[i]["run"]
    assert "GITHUB_WORKSPACE" not in str(steps[renew]) + str(release)


def test_failure_cleanup_covers_every_claimed_run_on_an_independent_runner() -> None:
    workflow = _workflow()
    cleanup = workflow["jobs"]["release_lease_on_failure"]
    jobs = [name for name in workflow["jobs"] if name != "release_lease_on_failure"]
    assert cleanup["needs"] == jobs
    condition = " ".join(cleanup["if"].split())
    assert condition.startswith("${{ always() && needs.prepare.result != 'skipped' && ")
    assert "needs.prepare.result == 'success'" not in condition
    assert "needs.prepare.outputs.claim != 'not-started'" in condition
    assert "needs.prepare.outputs.claim != 'refused'" in condition
    for name in jobs:
        assert f"needs.{name}.result != 'success'" in condition
    assert "needs.publish_draft.outputs.lease_released != 'true'" in condition
    # Codex 4203698218: API-only, so not on the runner whose loss it recovers from
    assert cleanup["runs-on"] == "${{ fromJSON(inputs.lease_cleanup_runs_on) }}"
    lease_input = workflow[True]["workflow_call"]["inputs"]["lease_cleanup_runs_on"]
    assert lease_input["default"] == '"ubuntu-latest"' and lease_input["required"] is False
    steps = cleanup["steps"]
    restores = [s for s in steps if str(s.get("uses", "")).startswith("actions/download-artifact")]
    assert len(restores) == 4 and all(s["continue-on-error"] is True for s in restores)
    token = next(s for s in steps if s.get("id") == "lease-token")
    release = next(s for s in steps if "agentic_sdlc release" in str(s.get("run")))
    # the token and the release run unless an earlier step failed the job: no condition of
    # their own that could turn a failed restore into a silent skip
    assert "if" not in token and "if" not in release
    assert release["env"]["PR_URL"] == "${{ needs.publish_draft.outputs.pr_url }}"
    assert "|| true" not in release["run"] and "continue-on-error" not in release


def test_failure_cleanup_fails_red_when_the_lease_material_cannot_be_restored() -> None:
    """Codex 4246082563: the job only runs when prepare's claim may have been posted (started,
    claimed, or empty from a lost runner), and prepare uploads both artifacts before that, so
    a failed download is not proof that nothing was claimed. It retries each download once,
    then FAILS the job (red, re-runnable) instead of succeeding with the lease held to TTL."""
    cleanup = _workflow()["jobs"]["release_lease_on_failure"]
    steps = cleanup["steps"]
    assert not any("Nothing to release" in str(s.get("name")) for s in steps)
    by_id = {s.get("id"): i for i, s in enumerate(steps) if s.get("id")}
    for first, retry, artifact in (
        ("engine", "engine-retry", "policy-engine-"),
        ("lease-policy", "lease-policy-retry", "lease-policy-"),
    ):
        assert by_id[first] < by_id[retry]
        assert steps[by_id[retry]]["if"] == f"steps.{first}.outcome != 'success'"
        assert steps[by_id[retry]]["with"] == steps[by_id[first]]["with"]
        assert artifact in steps[by_id[retry]]["with"]["name"]
    fail = _named(steps, "Fail if the lease material could not be restored")
    condition = " ".join(steps[fail]["if"].split())
    assert condition == (
        "(steps.engine.outcome != 'success' && steps.engine-retry.outcome != 'success') || "
        "(steps.lease-policy.outcome != 'success' && "
        "steps.lease-policy-retry.outcome != 'success')"
    )
    assert "continue-on-error" not in steps[fail]
    assert steps[fail]["run"].rstrip().endswith("exit 1")
    assert steps[fail]["env"]["CLAIM"] == "${{ needs.prepare.outputs.claim }}"
    assert by_id["lease-policy-retry"] < fail < by_id["lease-token"]
    # a genuinely pre-upload stop never reaches this job: the job condition excludes it
    condition = " ".join(cleanup["if"].split())
    assert "needs.prepare.outputs.claim != 'not-started'" in condition


def test_verify_installs_the_policys_ci_python_before_the_gates() -> None:
    """Codex 4243659485: the gates run under [ci] python_version, installed with the same
    pinned setup-python the generated ci.yml uses; the policy engine keeps the runner's own."""
    steps = _workflow()["jobs"]["verify"]["steps"]
    read = next(i for i, s in enumerate(steps) if s.get("id") == "ci-python")
    install = _named(steps, "Install the policy's CI Python")
    gates = next(i for i, s in enumerate(steps) if "run-gates" in str(s.get("run")))
    assert read + 1 == install and install + 1 == gates
    assert '"${RUNNER_TEMP}/trusted-policy.toml"' in steps[read]["run"]
    assert "python3 -I -" in steps[read]["run"]
    template = (ROOT / "src/agentic_sdlc/templates/github/forge/ci.yml").read_text()
    pinned = re.search(r"uses: (actions/setup-python@[0-9a-f]{40})", template)
    assert pinned and steps[install]["uses"] == pinned.group(1)
    assert steps[install]["if"] == "steps.ci-python.outputs.version != ''"
    assert steps[install]["with"] == {"python-version": "${{ steps.ci-python.outputs.version }}"}
    assert steps[gates]["env"]["ENGINE_PYTHON"] == "${{ steps.ci-python.outputs.engine }}"
    assert '"$ENGINE_PYTHON" -m agentic_sdlc run-gates' in steps[gates]["run"]


# Sandbox binaries are baked into the self-hosted runner images, so every apt
# invocation in the platform workflows is normally a no-op that still contends
# for the apt/dpkg locks with sibling runners on the same VM and with
# unattended-upgrades. Both pins below keep a lock collision from being reported
# as a sandbox failure.
SANDBOX_INSTALL_WORKFLOWS = (
    "reusable-review.yml",
    "reusable-repair.yml",
    "reusable-ci-repair.yml",
    "reusable-implement.yml",
    "reusable-spec.yml",
)


def test_platform_workflows_give_apt_a_lock_timeout() -> None:
    pattern = re.compile(r"^[ \t]*sudo apt-get .*$", re.MULTILINE)
    invocations = 0
    for workflow in WORKFLOWS.glob("*.yml"):
        for invocation in pattern.findall(workflow.read_text(encoding="utf-8")):
            invocations += 1
            assert "-o DPkg::Lock::Timeout=300" in invocation, f"{workflow.name}: {invocation}"
    assert invocations, "no apt invocations found to pin"


def test_sandbox_install_is_skipped_when_the_binaries_are_already_present() -> None:
    for name in SANDBOX_INSTALL_WORKFLOWS:
        document = (WORKFLOWS / name).read_text(encoding="utf-8")
        install = document.index("sudo apt-get -o DPkg::Lock::Timeout=300 install")
        guard = document.rindex("if command -v bwrap >/dev/null", 0, install)

        assert guard < install, name
        assert "skipping apt" in document[guard:install], name


def test_apt_update_retries_the_lists_lock() -> None:
    # DPkg::Lock::Timeout is honoured for the dpkg locks only; apt-get update
    # acquires /var/lib/apt/lists/lock once and fails at once if it is held,
    # so every update must sit inside a bounded retry loop.
    for name in SANDBOX_INSTALL_WORKFLOWS:
        document = (WORKFLOWS / name).read_text(encoding="utf-8")
        update = document.index("sudo apt-get -o DPkg::Lock::Timeout=300 update -q && break")
        loop = document.rindex("for attempt in ", 0, update)
        assert update - loop < 200, name
        done = document.index("done", update)
        assert "sleep" in document[update:done], name
        assert "exit 1" in document[update:done], name
        assert document.count("update -q") == 1, name


def test_verify_reads_the_ci_python_version_exactly(tmp_path) -> None:
    """The version reader in `verify`, run as written: unset is empty (no setup-python), a
    well-formed version is passed through, anything else fails the job."""
    import subprocess
    import sys

    steps = _workflow()["jobs"]["verify"]["steps"]
    run = next(s for s in steps if s.get("id") == "ci-python")["run"]
    script = run.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    cases = {
        "": (0, ""),
        "[ci]\n": (0, ""),
        '[ci]\npython_version = "3.12"\n': (0, "3.12"),
        '[ci]\npython_version = "3.12.4"\n': (0, "3.12.4"),
        '[ci]\npython_version = "3.12\\nx=1"\n': (1, ""),
        "[ci]\npython_version = 3.12\n": (1, ""),
    }
    for policy, (code, out) in cases.items():
        path = tmp_path / "trusted-policy.toml"
        path.write_text(policy)
        done = subprocess.run(
            [sys.executable, "-I", "-", str(path)],
            input=script,
            capture_output=True,
            text=True,
            check=False,
        )
        assert (done.returncode, done.stdout.strip()) == (code, out), policy
