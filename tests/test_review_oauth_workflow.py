from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "reusable-review.yml"


def _review_job() -> str:
    document = WORKFLOW.read_text(encoding="utf-8")
    start = document.index("\n  review:\n")
    end = document.index("\n  enforce_review:\n", start)
    return document[start:end]


def test_review_uses_subscription_oauth_without_paid_api_fallbacks() -> None:
    document = WORKFLOW.read_text(encoding="utf-8")
    review = _review_job()

    assert "CLAUDE_CODE_OAUTH_TOKEN" in document
    assert "claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}" in review
    assert "OPENAI_API_KEY" not in document
    assert "ANTHROPIC_API_KEY" not in document
    assert "openai/codex-action" not in document
    assert "anthropic_api_key:" not in document


def test_trusted_workflow_materializes_and_hashes_exact_review_diff() -> None:
    review = _review_job()
    materialize = "Materialize exact immutable review diff"
    invoke = "Independently review the exact diff as code-reviewer"

    assert materialize in review
    assert 'git diff --find-renames "${BASE_SHA}...${HEAD_SHA}"' in review
    assert "test -s .agentic-review/change.diff" in review
    assert "sha256sum .agentic-review/change.diff" in review
    assert "steps.review-diff.outputs.sha256" in review
    assert review.index(materialize) < review.index(invoke)


def test_sandbox_is_prepared_and_verified_before_isolated_claude_review() -> None:
    review = _review_job()
    install = "Install and verify Claude subprocess isolation"
    invoke = "Independently review the exact diff as code-reviewer"

    assert install in review
    assert "sudo apt-get -o DPkg::Lock::Timeout=300 install -y -q bubblewrap socat" in review
    assert "command -v bwrap >/dev/null" in review
    assert "command -v socat >/dev/null" in review
    assert "apparmor_restrict_unprivileged_userns" in review
    assert "sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0" in review
    assert "bwrap --unshare-user --uid 0 --gid 0 --ro-bind / / /bin/true" in review
    assert 'CLAUDE_CODE_SUBPROCESS_ENV_SCRUB: "1"' in review
    assert 'CLAUDE_CODE_SUBPROCESS_ENV_SCRUB: "0"' not in review
    assert review.index(install) < review.index(invoke)


def test_sandbox_verification_survives_an_apt_lock_collision() -> None:
    # bubblewrap and socat are already on the runner images, so the install is a
    # no-op that still contends for the apt/dpkg locks with sibling runners and
    # unattended-upgrades. A lock collision must not be reported as a sandbox
    # failure: skip apt when the binaries are present, wait for the lock when an
    # install is genuinely needed, and verify the sandbox either way.
    review = _review_job()
    guard = review.index("if command -v bwrap >/dev/null")
    update = review.index("sudo apt-get -o DPkg::Lock::Timeout=300 update -q")
    install = review.index("sudo apt-get -o DPkg::Lock::Timeout=300 install -y -q bubblewrap socat")
    smoke = review.index("bwrap --unshare-user --uid 0 --gid 0 --ro-bind / / /bin/true")

    assert guard < update < install < smoke
    assert "skipping apt" in review[guard:update]
    # Verification runs outside the conditional, so a skipped install still
    # fails closed when a sandbox binary is missing.
    assert install < review.rindex("command -v bwrap >/dev/null") < smoke
    assert install < review.rindex("command -v socat >/dev/null") < smoke


def test_claude_reviews_materialized_diff_without_shell_access() -> None:
    review = _review_job()

    assert "Read .agentic-review/change.diff as the authoritative review scope" in review
    assert "Do not invoke git or a shell to reconstruct the diff" in review
    assert '--allowedTools "Read,Write,Glob,Grep"' in review
    assert "Read,Write,Glob,Grep,Bash" not in review
    assert 'CLAUDE_CODE_SUBPROCESS_ENV_SCRUB: "1"' in review


def test_review_scope_and_repository_mutation_are_deterministically_verified() -> None:
    review = _review_job()

    assert "Verify review scope and repository integrity" in review
    assert 'echo "${EXPECTED_DIFF_SHA256}  .agentic-review/change.diff" | sha256sum -c -' in review
    assert 'changed=$(git diff --name-only "${HEAD_SHA}" -- .)' in review
    assert 'git diff --quiet "${BASE_SHA}" -- "$path"' in review
    assert "unexpected tracked repository mutation during review" in review
    assert "git ls-files --others --exclude-standard" in review
    assert "actual == expected" in review
    assert "expected.add(f'review-{role}.json')" in review


def test_expected_claude_action_hardening_is_narrow_and_base_pinned() -> None:
    review = _review_job()

    for protected in (
        "CLAUDE.md",
        "CLAUDE.local.md",
        ".mcp.json",
        ".claude.json",
        ".gitmodules",
        ".ripgreprc",
        ".claude/*",
        ".husky/*",
    ):
        assert protected in review

    assert "BASE_SHA: ${{ inputs.base_sha }}" in review
    assert "HEAD_SHA: ${{ inputs.head_sha }}" in review
    assert 'git diff --quiet "${BASE_SHA}" -- "$path"' in review
    assert "trusted agent-control path does not match fixed base" in review
    assert "origin/main" not in review


def test_review_fails_closed_instead_of_disabling_subprocess_isolation() -> None:
    review = _review_job()

    assert "bwrap --unshare-user --uid 0 --gid 0 --ro-bind / / /bin/true" in review
    assert "continue-on-error: true" not in review
    assert 'CLAUDE_CODE_SUBPROCESS_ENV_SCRUB: "0"' not in review


def test_ai_review_job_remains_read_only_to_repository() -> None:
    review = _review_job()

    assert "contents: read" in review
    assert "contents: write" not in review
    assert "pull-requests: write" not in review
    assert "issues: write" not in review


# Issue #119: role-aware, acceptance-criteria-aware review gates.


def test_review_prompt_content_comes_from_the_context_packet() -> None:
    review = _review_job()
    # The task sections, policy limits, diff summary and trusted gate report
    # reach the reviewer through the #118 packet contract, built by the
    # pinned engine against the authenticated review target — the workflow
    # never re-parses the issue into the prompt.
    assert "sdlcctl context-packet" in review
    assert "--review-target-repository" in review
    assert "--prompt-output .agentic-review/context.md" in review
    assert "Read .agentic-review/context.md" in review
    # The issue text is bound as DATA for the packet request; nothing splices
    # it into prompt text.
    assert "request.json" in review
    assert "${{ inputs.head_sha }}" in review


def test_derived_gates_run_sandboxed_never_on_the_trusted_host() -> None:
    review = _review_job()
    # Derived gates execute candidate-controlled commands (Codex 4433905461):
    # trusted-host would run them directly where they could rewrite the
    # installed engine or the review control files later steps enforce with.
    assert "--gate-sandbox trusted-host" not in review
    assert "--gate-sandbox forge-gate-sandbox" in review
    launcher = "Install the derived-gate sandbox launcher"
    derive = "Derive the reviewer context packet from the exact candidate"
    install = "Install and verify Claude subprocess isolation"
    assert launcher in review
    # bubblewrap exists and is verified BEFORE the launcher and the first
    # gate execution, and the launcher is installed before the derivation.
    assert review.index(install) < review.index(launcher) < review.index(derive)
    # The launcher is root-owned in a root-only directory, so the engine's
    # strict launcher trust accepts it and a candidate cannot rewrite it.
    assert (
        "sudo install -o root -g root -m 0755"
        ' "${RUNNER_TEMP}/forge-gate-sandbox" /usr/local/bin/forge-gate-sandbox' in review
    )
    # The confinement itself: read-only host, hidden home/checkout/temp,
    # only the disposable derivation tree writable.
    assert "--ro-bind / /" in review
    assert '--tmpfs "$hidden"' in review
    assert '"${HOME:-}" "${GITHUB_WORKSPACE:-}" "${RUNNER_TEMP:-}"' in review
    assert '--bind "$PWD" "$PWD" --chdir "$PWD"' in review
    assert "/usr/local/bin/forge-gate-sandbox /bin/true" in review


def test_roles_are_selected_deterministically_and_recorded() -> None:
    review = _review_job()
    assert "sdlcctl review-roles" in review
    assert "--changed-paths-file .agentic-review/changed-paths.txt" in review
    # The policy whose protected paths escalate to the security reviewer is
    # the BASE-committed one, never the candidate head's.
    assert 'git show "${BASE_SHA}:${CONFIG_PATH}"' in review
    assert "--config .agentic-review/trusted-policy.toml" in review
    # The selection travels with the review run.
    assert ".agentic-review/roles.json" in review


def test_the_security_reviewer_runs_conditionally_with_its_own_checklist() -> None:
    review = _review_job()
    assert "Independently review the exact diff as security-reviewer" in review
    assert "contains(steps.roles.outputs.roles, 'security-reviewer')" in review
    assert "checklist-code-reviewer.md" in review
    assert "checklist-security-reviewer.md" in review
    # Reviewers get no write instructions: both prompts forbid modification.
    assert review.count("Do not modify repository files.") == 2


def test_the_schema_is_the_versioned_platform_schema() -> None:
    review = _review_job()
    assert "from agentic_sdlc.review_gate import load_review_schema" in review
    assert "schemaVersion 2" in review
    assert "acceptanceCriteriaVerdict" in review


def test_the_gate_is_computed_by_the_pinned_engine_and_applied_without_checkout() -> None:
    document = WORKFLOW.read_text(encoding="utf-8")
    review = _review_job()
    enforce = document[document.index("\n  enforce_review:\n") :]
    assert "from agentic_sdlc.review_gate import enforce_reviews" in review
    assert ".agentic-review/gate.json" in review
    # Coverage is total: the packet's own acceptance criteria — never the
    # reviewer's self-chosen list — define what the verdicts must cover.
    assert "packet['task']['acceptanceCriteria']" in review
    assert "expected_criteria=expected_criteria" in review
    # The publisher job stays checkout-free: it applies the recorded
    # decision, it never recomputes it beside candidate code.
    assert "actions/checkout" not in enforce
    assert "jq -e '.allowed == true'" in enforce
    assert "jq -e --arg head \"$HEAD_SHA\" '.headSha == $head'" in enforce


def test_every_selected_role_must_produce_a_structurally_valid_review() -> None:
    review = _review_job()
    assert "Verify every selected role produced a structurally valid review" in review
    assert "_structural_errors(document)" in review
    assert "document['role'] == role" in review
    assert "document['headSha'] == os.environ['HEAD_SHA']" in review
