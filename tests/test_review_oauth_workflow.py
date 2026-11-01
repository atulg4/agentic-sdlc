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
    # only the disposable derivation tree (and the sandbox-private home
    # beside it) writable.
    assert "--ro-bind / /" in review
    assert '--tmpfs "$hidden"' in review
    assert '"${HOME:-}" "${GITHUB_WORKSPACE:-}" "${RUNNER_TEMP:-}"' in review
    assert '--bind "$PWD" "$PWD"' in review
    assert '--chdir "$PWD"' in review
    assert "/usr/local/bin/forge-gate-sandbox /bin/true" in review


def test_gate_sandbox_persists_setup_installs_across_gate_invocations() -> None:
    review = _review_job()
    # run_gates launches setup, quality and test as SEPARATE launcher
    # invocations: a per-invocation tmpfs HOME would throw away what the
    # setup gate installed (Codex 4434015126). HOME lives beside the
    # derivation tree, is bind-mounted writable, and its .local/bin joins
    # PATH so user-installed entry points resolve.
    assert 'home_dir="$(dirname "$PWD")/.agentic-sandbox-home"' in review
    assert 'mkdir -p "$home_dir/.local/bin"' in review
    assert '--bind "$home_dir" "$home_dir"' in review
    assert '--setenv HOME "$home_dir"' in review
    assert '--setenv PATH "$home_dir/.local/bin:' in review
    assert "--setenv HOME /tmp" not in review


def test_trusted_python_steps_cannot_import_candidate_code() -> None:
    document = WORKFLOW.read_text(encoding="utf-8")
    # Trusted snippets execute from the PR-head checkout, where stdin and -m
    # execution would otherwise put the candidate tree first on sys.path: a
    # committed root-level agentic_sdlc/ (or pip/) package would shadow the
    # pinned engine (Codex 4434015133). -P (safe path) keeps the checkout
    # off the import path while the user-site engine install stays visible.
    assert "python3 - <<'PY'" not in document
    assert "python3 -m pip" not in document
    assert document.count("python3 -P - <<'PY'") == 7
    assert "python3 -P -m pip install" in document


def test_trusted_inputs_are_sealed_before_review_and_verified_after() -> None:
    review = _review_job()
    # The reviewers hold a Write tool (Codex 4434056699): a prompt-injected
    # reviewer could rewrite packet.json, roles, schema, checklists, or the
    # installed engine before enforcement reads them. The combined digest is
    # recorded as a step output BEFORE the reviewers run and re-verified
    # FIRST after them, so tampering fails closed.
    seal = "Seal trusted review inputs"
    invoke = "Independently review the exact diff as code-reviewer"
    verify = "Verify review scope and repository integrity"
    assert seal in review
    assert review.index("sdlcctl context-packet") < review.index(seal) < review.index(invoke)
    assert review.index(invoke) < review.index(verify)
    # The digest covers every control file except the verdicts the
    # reviewers legitimately write, plus the installed engine package —
    # located without importing it, so a tampered engine never runs first.
    assert review.count("find .agentic-review -type f ! -name 'review-*.json' -print0") == 2
    assert review.count('find "$engine_dir" -type f -print0') == 2
    assert 'importlib.util.find_spec("agentic_sdlc")' in review
    assert "import agentic_sdlc, os" not in review
    # Recorded as a step output (runner-held state no later file write can
    # alter) and compared before anything else in the verification step.
    assert "EXPECTED_SEAL_SHA256: ${{ steps.seal.outputs.sha256 }}" in review
    seal_check = 'test "$digest" = "$EXPECTED_SEAL_SHA256"'
    assert seal_check in review
    assert review.index(seal_check) < review.index("sha256sum -c -")


def test_role_selection_sees_the_full_published_series_of_paths() -> None:
    # Role selection reads the packet's derived verification paths —
    # collect_commit_diff's union of the endpoint diff (both rename sides,
    # Codex 4434056701) and every per-commit path (Codex 4434134599: a
    # protected file changed and restored mid-series is still published by
    # the merge) — never a workflow-side endpoint-only git listing. The
    # paths travel NUL-delimited so git quoting cannot mis-spell a
    # non-ASCII protected path (Codex 4434104201).
    review = _review_job()
    assert "git diff --name-only --no-renames" not in review
    assert "> .agentic-review/changed-paths.txt" not in review
    assert 'packet["verification"]["diffPaths"]' in review
    assert 'path.encode("utf-8") + b"\\0"' in review
    assert "--changed-paths-file0 .agentic-review/changed-paths.txt" in review
    # The packet must exist before the selection that reads it.
    assert review.index("sdlcctl context-packet") < review.index("sdlcctl review-roles")


def test_the_code_verdict_is_sealed_before_the_security_reviewer_runs() -> None:
    review = _review_job()
    # The security reviewer runs in the same checkout with a Write tool
    # (Codex 4434104192): the completed code verdict is hashed into a step
    # output before it starts and re-verified afterward, so one compromised
    # reviewer can never supply both approvals.
    seal = "Seal the code-reviewer verdict"
    code = "Independently review the exact diff as code-reviewer"
    security = "Independently review the exact diff as security-reviewer"
    assert review.index(code) < review.index(seal) < review.index(security)
    assert "EXPECTED_CODE_VERDICT_SHA256: ${{ steps.seal-code-verdict.outputs.sha256 }}" in review
    check = (
        'echo "${EXPECTED_CODE_VERDICT_SHA256}  '
        '.agentic-review/review-code-reviewer.json" | sha256sum -c -'
    )
    assert check in review


def test_the_anchor_accepts_every_branch_the_policy_loader_accepts() -> None:
    review = _review_job()
    # Names like release+next and feature@two are supported by the policy's
    # branch-name contract; a narrower inline alphabet would fail every
    # review for such a project (Codex 4434104198).
    assert '[[ "$default_branch" =~ ' not in review
    assert "from agentic_sdlc.policy import branch_name_problem" in review


def test_roles_are_selected_deterministically_and_recorded() -> None:
    review = _review_job()
    assert "sdlcctl review-roles" in review
    assert "--changed-paths-file0 .agentic-review/changed-paths.txt" in review
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
