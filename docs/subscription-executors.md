# Subscription-backed executors

## Claude Code Pro/Max

Anthropic's Claude Code GitHub Action supports `claude_code_oauth_token` as an alternative to `anthropic_api_key`. Pro/Max users may generate a long-lived OAuth token with the provider-supported `claude setup-token` flow and store it as a GitHub Actions secret named `CLAUDE_CODE_OAUTH_TOKEN`.

Agentic SDLC must:

- prefer OAuth subscription execution when configured and permitted
- never print, commit, or copy the OAuth token into artifacts
- keep provider action full-SHA pinned
- keep generated patches untrusted until independent verification
- treat quota/auth expiration as infrastructure failure
- support fallback to another qualified executor without silently switching to pay-as-you-go API billing

## ChatGPT-plan Codex

Codex is available through ChatGPT plans and supports authenticated clients and cloud-delegated tasks connected to GitHub. Agentic SDLC should model this as a subscription-cloud executor and integrate only through supported OpenAI authentication/delegation mechanisms.

A ChatGPT-plan credential is never represented as an `OPENAI_API_KEY`. If an unattended workflow cannot officially invoke subscription Codex, the router must mark that execution mode unavailable for event-driven work and select another qualified provider.

## Common contract

Subscription executors expose:

- provider and plan class
- supported task classes
- current/estimated capacity where observable
- reset horizon where observable
- model availability
- execution locality (provider cloud, GitHub runner, authenticated remote client)
- authentication health without exposing credentials
- observed success/failure/cost/latency history
- explicit `may_fallback_to_paid_api` policy (default false)

No subscription executor may automatically incur pay-as-you-go API cost unless project policy explicitly allows it.
