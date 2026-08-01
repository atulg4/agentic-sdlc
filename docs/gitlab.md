# GitLab Adapter

The GitLab v0.1 adapter validates an issue and emits the same bounded prompt and
policy decision used by GitHub. It is intentionally plan-preparation only until
a protected GitLab runner and publisher token have been configured.

## Setup

1. Mirror this repository into the target GitLab instance.
2. Tag a reviewed release and pin the component and `platform_ref` to its exact
   40-character commit SHA.
3. Allow the consumer project job token to clone the private component project,
   or provide a read-only deploy credential through the runner.
4. Add a masked, protected project token named
   `AGENTIC_GITLAB_READ_TOKEN` with `read_api` only.
5. Include the `agentic-sdlc` CI/CD component from `templates/agentic-sdlc`.
6. Trigger the pipeline with `AGENTIC_SDLC_RUN=true` and a numeric
   `AGENTIC_ISSUE_IID`.

## Automatic Intake

GitLab issue webhooks should call a small authenticated intake gateway. The
gateway validates the webhook secret, confirms the actor and labels, and starts
a pipeline with only the issue IID. It does not execute code or hold a
repository-write credential.

Implementation and merge-request publication should use the same four-stage
contract as GitHub: isolated generation, fresh verification, independent
review, and a publisher with no AI credential. That adapter is a v0.2
deliverable; do not attach a write token to the v0.1 preparation job.
