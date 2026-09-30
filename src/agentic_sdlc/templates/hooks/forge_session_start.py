#!/usr/bin/env python3
"""Claude Code SessionStart hook: start every session from the real state of the repository.

Installed by `sdlcctl onboard`. Fetches origin, reports how far the default branch moved, lists
open PRs and issues currently leased by other agents, and reminds the session to claim before
implementing. Output goes into the session's context.
"""

from __future__ import annotations

import json
import subprocess
import sys

PROJECT = "PROJECT_ID"
DEFAULT_BRANCH = "DEFAULT_BRANCH_NAME"


def run(cmd: list[str]) -> str:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, check=True, timeout=60
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ""


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}
    session = payload.get("session_id", "")
    run(["git", "fetch", "-q", "origin"])
    behind = run(["git", "rev-list", "--count", f"HEAD..origin/{DEFAULT_BRANCH}"]) or "?"
    recent = run(["git", "log", "--oneline", "-8", f"origin/{DEFAULT_BRANCH}"])
    prs = run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            PROJECT,
            "--state",
            "open",
            "--limit",
            "20",
            "--json",
            "number,title,headRefName",
            "--jq",
            '.[] | "#\\(.number) \\(.title) [\\(.headRefName)]"',
        ]
    )
    leased = run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            PROJECT,
            "--label",
            "in-progress",
            "--state",
            "open",
            "--json",
            "number,title,assignees",
            "--jq",
            '.[] | "#\\(.number) \\(.title) (assignee: \\(.assignees | map(.login) | join(",")))"',
        ]
    )
    print(
        f"[Forge] {PROJECT}: HEAD is {behind} commit(s) behind origin/{DEFAULT_BRANCH}. "
        "Recent on main:"
    )
    print(recent or "  (unavailable)")
    print("[Forge] Open PRs:\n" + (prs or "  none"))
    print("[Forge] Issues leased by other agents (do NOT work on these):\n" + (leased or "  none"))
    print(
        f"[Forge] Rule: every change maps to an issue. Before implementing one, claim it: "
        f"sdlcctl claim --project {PROJECT} --issue <N> --agent claude-code "
        f"--session {session or '<session_id>'} "
        f"--branch forge/issue-<N>. Your session id is {session or 'unknown'}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
