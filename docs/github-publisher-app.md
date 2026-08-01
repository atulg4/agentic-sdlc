# GitHub Publisher App

The publisher App is the only identity allowed to open an automated draft pull
request and post its issue comment. It is separate from Codex, Claude, the
user's personal login, and the normal Actions token.

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

The implementation workflow creates an installation token only in the final
publisher job. No owner or repository list is supplied, so the token is scoped
to the current repository. It requests only the three permissions above,
expires after about one hour, and is revoked when the job ends. The native job
token pushes the already attested branch; the App token opens the pull request
and posts its issue comment. The AI and verification jobs never receive the App
key or installation token.

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
