# Security Policy

Do not report vulnerabilities through a public issue. Contact the repository
owner privately and include the affected workflow, trust boundary, and a safe
reproduction that contains no live credentials.

## Supported Version

Only the latest reviewed release is supported. Consumer repositories must pin
the platform and every third-party action to immutable commit SHAs.

## High-Impact Areas

- AI credentials reachable from repository-controlled code;
- repository-write tokens reachable from an AI process;
- publisher acceptance of an unverified or mismatched patch;
- policy loaded from a modified working tree instead of the protected base;
- workflow events that expose secrets to fork-controlled code;
- path-policy bypasses involving deletion, rename, symlink, or unusual names;
- any route that lets an agent approve, merge, deploy, or access production.

Do not include API keys, tokens, private repository contents, brokerage data,
or personal information in a report.
