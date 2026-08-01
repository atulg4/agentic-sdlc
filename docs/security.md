# Security Model

## Non-negotiable boundaries

Agents cannot:

- push to or merge the protected default branch;
- approve their own pull requests or merge requests;
- deploy to production;
- access broker, customer, production, or personal credentials;
- change workflow definitions, repository policy, CODEOWNERS, or branch rules;
- weaken, delete, or skip tests to obtain a passing result;
- trigger live trades, paid bulk jobs, unrestricted scans, or model training;
- decide that their own output is correct.

## Credential separation

The pipeline uses separate jobs and fresh checkouts:

1. A no-AI preparation job may receive a framework-only read token when the
   platform repository is private.
2. The planner has consumer-repository read access and an AI credential, but no
   write token.
3. The patch generator has consumer-repository read access and an AI
   credential, but no write token.
4. The verifier receives only the patch and trusted base-branch configuration.
   It receives neither AI nor repository write credentials. Project commands
   run with a sanitized environment and cannot inherit CI control-file paths or
   protected credential variable names.
5. A fresh attestation job runs only trusted policy code. It waits for the
   verifier, then reconstructs the original patch without executing changed
   consumer code. Only this job may create the publishable patch artifact.
6. The publisher receives a verified patch but no AI credential. It uses its
   native short-lived token only for branch contents, then a separate
   current-repository GitHub App token for the pull request and issue comment.
7. The reviewer has read access and an independent AI credential context.
   Its fixed publisher has no checkout or AI credential and receives only
   pull-request write permission to post the validated review comment.

GitHub publishing uses the native `GITHUB_TOKEN` for the branch push so that
push-triggered workflows do not run on the unreviewed branch. It then requires
the dedicated App token because a pull request created with `GITHUB_TOKEN` does
not reliably start downstream checks without another approval. The App has
Contents read, plus Issues and Pull requests write. It cannot change Actions,
workflows, rules, deployments, secrets, or administration. For cloud access,
prefer OIDC-issued short-lived credentials. Do not store cloud provider keys in
repository secrets when OIDC is available.

## Prompt injection

Issue bodies, comments, commit messages, source files, logs, and external data
are untrusted. They are placed inside explicit untrusted-data boundaries. This
is defense in depth, not the primary control. Primary controls are sandboxing,
job permissions, path denial, fresh-job verification, and branch protection.

## Patch policy

Default forbidden paths include:

- `.github/**`
- `.gitlab-ci.yml` and `.gitlab/**`
- `agentic-sdlc.toml`
- `CODEOWNERS`, `REVIEW.md`, and nested agent instruction files
- root or nested `.env*`, private keys, and production data directories

Changes to authentication, authorization, financial calculations, migrations,
trading code, or public endpoints are high risk. They require an architect and
security review in addition to deterministic CI and human merge approval.

## Workflow event safety

- Start with manual dispatch.
- Refuse plan or implementation runs whose checkout is not the protected
  default-branch commit.
- Accept event-driven runs only from actors with repository write permission.
- Use separate labels for plan eligibility and implementation approval.
- Never run implementation from a public fork with secrets.
- Do not use `pull_request_target` to execute untrusted checked-out code.
- Pin third-party actions and CI components to reviewed immutable commits.
- Keep action and component updates behind their own review process.
- Enforce file-count, text-line, and raw patch-byte limits before verification.

## Logs and artifacts

- Redact token formats and known secret values before publishing output.
- Retain plans, policy decisions, test reports, reviews, and patch hashes.
- Do not retain raw AI authentication material.
- Set short artifact retention for source patches.
- Treat artifacts as untrusted until their producing run and hash are verified.
- Never create a publishable artifact in a job that executed changed consumer
  code; tests can modify any file available to their own job.

## Merge policy

Version 0.1 never auto-merges. A future low-risk auto-merge mode requires a
separate policy change and threat review. It must remain unavailable for
security, authentication, data, financial, workflow, dependency, or deployment
changes.
