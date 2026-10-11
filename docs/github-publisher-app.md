# GitHub Publisher App

The publisher App is the only identity allowed to open an automated draft pull
request and post its issue comment, and the identity that writes the Actions
path's issue lease (claim and release marker comments and the `in-progress`
label). It is separate from Codex, Claude, the user's personal
login, and the normal Actions token.

## Create the App

1. Open GitHub **Settings > Developer settings > GitHub Apps > New GitHub App**.
2. Name it `Agentic SDLC Publisher`. The homepage can be this framework's
   repository URL. Disable webhooks; this App does not need a callback server.
3. Under repository permissions, grant only:
   - **Contents: Read-only**
   - **Issues: Read and write**
   - **Pull requests: Read and write**
4. Leave Actions, Workflows, Administration, Deployments, Environments,
   Secrets, Members, and every organization permission at **No access**.
5. Limit installation to this account, create the App, and install it on
   **Only select repositories**. Start with MarketMaestro.
6. Generate a private key and treat the downloaded PEM file as a secret.

GitHub documents the App creation process in
[Registering a GitHub App](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app).

## Configure Each Consumer

In the consumer repository, open **Settings > Secrets and variables >
Actions** and create:

| Kind | Name | Value |
|---|---|---|
| Variable | `PUBLISHER_APP_CLIENT_ID` | The App's Client ID |
| Secret | `PUBLISHER_APP_PRIVATE_KEY` | The complete PEM private key |

Do not use the App ID in the Client ID variable. Do not store the key in a
file, `.env`, issue, workflow, or framework repository. A private key can be
shared through an organization-level Actions secret only when repository
access is explicitly limited.

## Runtime Boundary

`reusable-implement.yml` mints a Publisher App installation token
(`actions/create-github-app-token`) in three places, each in its own job and
each requesting only what that job needs. No owner or repository list is
supplied, so every token is scoped to the current repository; each expires
after about one hour and is revoked when its job ends.

| Job | When | Permissions requested | Used for |
|---|---|---|---|
| `prepare` | Every run, as the job's last steps, after the approval, dependency and duplicate-PR checks pass | Issues: write, Pull requests: read | `sdlcctl claim`: reads the issue's lease history and its open/linked pull requests, then posts the claim marker comment and adds the `in-progress` label (the Actions claim assigns no one). A held lease or an open PR for the issue refuses the run. |
| `publish_draft` | After the patch is generated, verified and attested | Contents: read, Issues: write, Pull requests: write | Opens the draft pull request, posts its issue comment, and releases the lease (`sdlcctl release`: release marker and label cleanup). |
| `release_lease_on_failure` | Only when `prepare` succeeded and a later job did not | Issues: write | Releases the lease so another agent can retry. |

The `prepare` and `release_lease_on_failure` tokens never reach the pull
request: they cannot open, edit or merge one, and neither job pushes. The
native job token pushes the already attested branch in `publish_draft`; the App
token never pushes. The AI and verification jobs (`generate_patch`, `verify`,
`attest_patch`) never receive the App key or any installation token.

The lease markers count only because the App's bot is a trusted marker author:
`sdlcctl` and the commit-guard hook accept markers from the Publisher App bot
named EXACTLY -- `[automation] publisher_bot_login` in the policy (set with
`sdlcctl onboard --force --publisher-bot-login <app-slug>[bot]`; `--apply`
also sets the `FORGE_LEASE_BOT_LOGINS` repository variable; the commit guard
carries it as rendered at onboard time), or `FORGE_LEASE_BOT_LOGINS` in the
environment, which wins -- and `github-actions[bot]`, besides users with write
access. With no `--config`, no `./agentic-sdlc.toml` and no variable (the
failure-cleanup job's `release` runs in an empty workspace), the lease commands
read the policy committed on the default branch; `release` additionally
releases its own session's claims whoever posted them, so it never reports
success over a claim it did not trust. No App bot is trusted on its slug alone: with no login configured,
the Publisher App's own claims are ignored and every Actions implementation is
refused at its claim (doctor reports it). Suspending or uninstalling the App therefore also stops the
Actions path from claiming issues.

GitHub limits workflow chaining for events created with `GITHUB_TOKEN`. The
framework uses that behavior intentionally for the branch push, preventing
push-triggered workflows from running unreviewed code, then uses the App token
to let the new pull request start normal CI without a manual workflow approval.
See [GitHub's token event rules](https://docs.github.com/en/actions/concepts/security/github_token)
and the maintained [token action documentation](https://github.com/actions/create-github-app-token).

## Rotation and Removal

- Rotate the private key immediately if it is exposed or copied to an
  unapproved location.
- Remove an old key from the App after the replacement secret is installed.
- Suspend or uninstall the App to stop all automated publishing immediately.
- Review the App's repository installation list whenever a project is added or
  archived.
- Never grant the App Contents write, or permission to merge, edit workflows,
  deploy, or manage repository rules.
