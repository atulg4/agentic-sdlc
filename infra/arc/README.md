# Forge ARC runners

This fleet serves `atulg4/marketmaestro`, `MusicMaestro`, `MarketingMaestro`,
`openmandate`, and `constellation` in the `ci-runners` GKE Autopilot cluster
(`marketmaestro-463502`, `us-west1`). Application deployment remains separate.

## Rollout status: blocked on sandbox compatibility

On 2026-10-02, the first image revision passed the toolchain and user-namespace
probe on GKE Sandbox/Balanced, but the network-namespace probe failed:
`bwrap: loopback: Failed RTM_NEWADDR: No child processes`. This reproduces
[upstream gVisor issue 13438](https://github.com/google/gvisor/issues/13438).
The current release cannot run the complete Forge sandbox contract. Do not
apply `worker.yaml` or merge consumer cutovers until the probe passes.

Controller/listener and cert-manager resource reductions were applied and
verified healthy. Worker image/runtime upgrades and the two missing pools
remain unapplied. Both image builds succeeded (the second adds ripgrep and verifies it during
the build); the first revision was tested live. The default Autopilot runtime
also failed the basic user-namespace probe. E2 sandbox nodes encountered
regional capacity shortages, so the final probe used Balanced (N2/N2D).

The final VM audit found both `mm-runner` and `mm-runner-b` running again.
Cloud Audit Logs show a separate local gcloud invocation restarted them at
09:03 PDT; several legacy runners were busy. VM compute charges continue until
the replacement is validated and those jobs are drained. `mm-terminal` remains
the application host.

A supported runtime fix or a separately reviewed GKE Standard runner pool with
an appropriate seccomp profile is required. Do not remove network isolation,
ignore the bubblewrap error, or turn off environment scrubbing as a workaround.
A Standard alternative must specify its idle node cost, scale-to-zero worker
pool, host/profile maintenance, and replacement/retirement of this cluster
before provisioning; running two clusters indefinitely defeats the cost goal.

## Acceptance Criteria

- Every consumer's Forge caller forwards its pool name through `runs_on`.
- All five pools have zero idle workers and bounded concurrency (4/2/1/1/1).
- The controller and listeners have explicit small idle resource requests.
- A credential-free probe proves the toolchain and nested bubblewrap sandbox.
- Two GitHub jobs exchange an artifact on different ephemeral runner pods.

## Required Tests

Run the platform test suite, workflow lint, and the consumer routing/governance
tests. Render Helm charts and perform server dry-runs before applying values.
Run the standalone sandbox probe before enabling agent work, then require each
repository's `Forge pod smoke` workflow to pass. Confirm that idle worker count
returns to zero and that listeners/controller have no new restarts.

## Non-Goals

No PR approvals/merges, production deployments, broker access or live trades.
Do not disable subprocess environment scrubbing, bubblewrap, or host isolation.
Docker service-container/build jobs stay on GitHub-hosted runners; this Autopilot
configuration does not provide a Docker daemon. Do not delete retained VM disks
or the `mm-terminal` application host as part of the CI migration.

## Dependencies

- ARC controller and scale-set Helm charts **0.14.2**; cert-manager **v1.21.2**.
- Existing `github-pat` secret in `arc-runners`, referenced by name only.
- GKE Sandbox support and sufficient disk/IP quota for node replacement.
- Owner-reviewed consumer workflow changes and immutable Forge references.

## Image and sandbox

`Dockerfile` pins the official runner and Node images. It adds GitHub CLI,
Python with a writable virtual environment, jq, bubblewrap, socat and build
tools. The unmodified ARC image lacks tools needed by existing Forge workflows.
The built image is pinned by digest in `worker.yaml`.

Build only this directory; `.dockerignore` excludes everything except the
Dockerfile. Never add provider, GitHub or cloud credentials to the build context.

```sh
gcloud builds submit infra/arc --project marketmaestro-463502 \
  --tag gcr.io/marketmaestro-463502/forge-runner:REVIEWED_VERSION
```

Record the resulting digest in `worker.yaml`. Probe it with the same runtime and
resources before upgrading pools. The default Autopilot runtime rejects the
user namespaces required by bubblewrap. `runtimeClassName: gvisor` keeps the
GKE Sandbox boundary; it must also pass the nested namespace probe. Workers
have no mounted Kubernetes service-account token, privileged container, or host
Docker socket.

## Applying values

Pin the chart version and use explicit context and namespace. Apply
`controller.yaml` to release `arc` in `arc-systems`. For each scale-set release
in `arc-runners`, combine these files in order:

1. `listener.yaml`
2. `worker.yaml`
3. `pools/REPOSITORY.yaml`

Existing releases can retain authentication references with `--reuse-values`.
New releases use the named secret and controller service account in the pool
file. Inspect `helm template`, then run `helm upgrade --install --dry-run=server
--hide-secret` before the real upgrade. Do not print Helm secret resources.

Apply `cert-manager.yaml` with `--reuse-values` to the existing cert-manager
release. It changes resource reservations only.

Set each consumer's `FORGE_RUNNER` Actions variable to its scale-set name and
open the workflow migration as an owner-reviewed draft PR. A pending smoke job
should wake a worker without starting a VM runner. A failed sandbox test blocks
cutover; do not treat a Running pod as evidence that Forge is functional.

## Costs and recovery

With five listeners the ARC control plane requests **350m CPU / 448Mi memory**.
The three cert-manager pods request another **150m / 384Mi**. Workers request
1 CPU / 2Gi memory / 8Gi ephemeral storage only while jobs run. Inspect actual
Autopilot-admitted pod requests after every upgrade; these determine the pod
reservation, not the small amount of CPU shown by `kubectl top`.

Idle workers scale to zero. GKE management, image storage, logs, retained VM
disks and the application VM can still incur charges. This configuration is
not an invoice or a guarantee of zero cost. Regional SSD and in-use IP quotas
must allow temporary node replacement; increasing a quota does not itself
provision resources.

To recover from a failed upgrade, use `helm history` and roll back to the
recorded previous release revision. Restore the previous reviewed consumer
workflow commit separately. The old VM runner registrations are not a fallback
unless an owner explicitly restarts and validates them.
