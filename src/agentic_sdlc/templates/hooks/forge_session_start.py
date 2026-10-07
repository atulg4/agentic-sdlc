#!/usr/bin/env python3
"""Claude Code SessionStart hook: start every session from the real state of the repository.

Installed by `sdlcctl onboard`. Fetches origin, reports how far the default branch moved, lists
open PRs, this session's own live lease (a resume, clear or compaction after a claim) and the
issues currently leased by other agents, and reminds the session to claim before implementing.
Output goes into the session's context.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PROJECT = "PROJECT_ID"
DEFAULT_BRANCH = "DEFAULT_BRANCH_NAME"


def run(cmd: list[str]) -> str:
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, check=True, timeout=60
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ""


def _lease_lookup():
    """The commit guard's lease reader (same trust/expiry rules), from the sibling hook file."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from forge_commit_guard import lease_for
    except ImportError:  # guard missing: treat every labelled issue as held (fail safe)
        return lambda number: {}
    return lease_for


def classify_leased(
    issues: list, lease_lookup, session: str = ""
) -> tuple[list[str], list[str], list[str]]:
    """Split `in-progress` issues into live leases held by OTHER sessions, expired/released ones
    (label left over), and this `session`'s own live leases (never "another agent's")."""
    live: list[str] = []
    stale: list[str] = []
    mine: list[str] = []
    for issue in issues:
        who = ",".join(a.get("login", "") for a in issue.get("assignees") or [])
        line = f"#{issue.get('number')} {issue.get('title', '')} (assignee: {who})"
        lease = lease_lookup(int(issue.get("number")))
        if lease is None:
            stale.append(line)
        elif session and isinstance(lease, dict) and str(lease.get("session") or "") == session:
            mine.append(line)
        else:
            live.append(line)
    return live, stale, mine


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        payload = {}
    session = str(payload.get("session_id") or "")
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
    raw = run(
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
            "--limit",
            "1000",  # gh defaults to 30; every lease must be in the do-not-touch inventory
            "--json",
            "number,title,assignees",
        ]
    )
    try:
        issues = json.loads(raw or "[]")
    except json.JSONDecodeError:
        issues = []
    live, stale, mine = classify_leased(issues, _lease_lookup(), session)
    print(
        f"[Forge] {PROJECT}: HEAD is {behind} commit(s) behind origin/{DEFAULT_BRANCH}. "
        "Recent on main:"
    )
    print(recent or "  (unavailable)")
    print("[Forge] Open PRs:\n" + (prs or "  none"))
    for line in mine:
        print(f"[Forge] Your current lease (this session, {session}): {line} -- keep working on it")
    print(
        "[Forge] Issues leased by other agents (do NOT work on these):\n"
        + ("\n".join(live) or "  none")
    )
    if stale:
        print(
            "[Forge] Labelled in-progress but no live lease (expired, released, or unreadable; "
            "an expired lease may be taken over with sdlcctl claim):\n" + "\n".join(stale)
        )
    print(
        f"[Forge] Rule: every change maps to an issue. Before implementing one, claim it: "
        f"sdlcctl claim --project {PROJECT} --issue <N> --agent claude-code "
        f"--session {session or '<session_id>'} "
        f"--branch forge/issue-<N>. Your session id is {session or 'unknown'}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
