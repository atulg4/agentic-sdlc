# Phone-First Control

## Recommended first interface

Use Remote in the ChatGPT mobile app as the primary control plane. It can start,
steer, review, and organize Codex work running on a connected development host.
The code remains on the host; the phone is used for decisions, prompts,
approvals, attachments, and review.

Official guide:
https://developers.openai.com/blog/mastering-codex-remote-for-engineering

This avoids building and securing a custom voice gateway before the SDLC engine
is proven.

## Daily phone workflow

1. Open the MarketMaestro project in ChatGPT mobile Remote.
2. Speak the request, including the outcome and priority.
3. Ask Codex to draft or update the GitHub issue, not to implement immediately.
4. Review the generated acceptance criteria and non-goals.
5. Approve a plan-only run.
6. Receive GitHub mobile notifications for the plan, CI, and review.
7. Approve implementation only when the plan is correct.
8. Merge from GitHub Mobile only after required checks and review are green.

GitHub Mobile or GitLab Mobile voice dictation is the fallback when the
development host is offline. A correctly formatted issue is enough to queue
work for the next pipeline run.

## Future dedicated voice gateway

A custom gateway can be added after the pipeline is stable:

```text
Phone or SIP call
  -> authenticated voice session
  -> speech-to-text and intent confirmation
  -> work-request generator
  -> owner reads back and confirms
  -> GitHub App or GitLab OAuth creates an issue
  -> pipeline handles all code work
```

The gateway must never hold repository admin credentials or execute code. It
may create and update issues through a narrowly scoped app installation. Every
state-changing voice command requires a read-back confirmation. The OpenAI
Realtime API supports low-latency voice sessions and SIP if a dedicated phone
number is later required.
