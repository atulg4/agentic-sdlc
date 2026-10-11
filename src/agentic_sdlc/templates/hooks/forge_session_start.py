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


UNAVAILABLE_WARNING = (
    "[Forge] WARNING: lease inventory unavailable -- do not start issue work until it can be "
    "checked. ({what} could not be read from GitHub: authentication, network or API failure; "
    "fix `gh` and start a new session, or run `sdlcctl claims --project {project}`.)"
)
FETCH_WARNING = (
    "[Forge] WARNING: could not fetch origin -- the remote branch state below is the last "
    "cached copy and may be stale. Run `git fetch origin` and check before starting work."
)

INCOMPLETE_WARNING = (
    "[Forge] WARNING: lease inventory INCOMPLETE -- only the first {count} open in-progress "
    "issue(s) could be read (a page was unreadable or the page cap was reached). Issues not "
    "listed below may still be leased: run `sdlcctl claims --project {project}` before starting "
    "issue work."
)
PAYLOAD_WARNING = (
    "[Forge] WARNING: could not read the SessionStart hook payload (not a JSON object), so this "
    "session's id is unknown and no lease below is shown as yours."
)
#: open in-progress issues read per page, and the most pages read (100 x 50 = 5000 leases)
ISSUE_PAGE_SIZE = 100
ISSUE_PAGE_CAP = 50


def run(cmd: list[str]) -> str | None:
    """The command's stdout, or None when it failed -- a failure is never an empty answer."""
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, check=True, timeout=60
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def _lease_lookup():
    """The commit guard's lease reader (same trust/expiry rules), from the sibling hook file."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from forge_commit_guard import lease_for
    except ImportError:  # guard missing: treat every labelled issue as held (fail safe)
        return lambda number: {}
    return lease_for


def in_progress_issues() -> tuple[list, str]:
    """Every open `in-progress` issue, read page by page through the REST API until a short page
    (pull requests, which that endpoint also returns, are skipped). The state says how far the
    read got: "complete", "unavailable" (the first page could not be read), "partial" (a later
    page could not be read) or "truncated" (ISSUE_PAGE_CAP full pages and still more). Anything
    but "complete" must never be presented as the whole inventory."""
    issues: list = []
    for page in range(1, ISSUE_PAGE_CAP + 1):
        raw = run(
            [
                "gh",
                "api",
                f"repos/{PROJECT}/issues?state=open&labels=in-progress"
                f"&per_page={ISSUE_PAGE_SIZE}&page={page}",
            ]
        )
        try:
            rows = json.loads(raw) if raw is not None else None
        except json.JSONDecodeError:
            rows = None
        if not isinstance(rows, list):
            return issues, "unavailable" if page == 1 else "partial"
        issues.extend(
            row
            for row in rows
            if isinstance(row, dict)
            and "pull_request" not in row
            and isinstance(row.get("number"), int)
            and not isinstance(row.get("number"), bool)
        )
        if len(rows) < ISSUE_PAGE_SIZE:
            return issues, "complete"
    return issues, "truncated"


def classify_leased(
    issues: list, lease_lookup, session: str = ""
) -> tuple[list[str], list[str], list[str]]:
    """Split `in-progress` issues into live leases held by OTHER sessions, expired/released ones
    (label left over), and this `session`'s own live leases (never "another agent's")."""
    live: list[str] = []
    stale: list[str] = []
    mine: list[str] = []
    for issue in issues:
        who = ",".join(
            str(a.get("login", "")) for a in issue.get("assignees") or [] if isinstance(a, dict)
        )
        line = f"#{issue.get('number')} {issue.get('title', '')} (assignee: {who})"
        lease = lease_lookup(int(issue.get("number")))
        if isinstance(lease, dict) and lease.get("unavailable"):
            live.append(f"{line} -- lease unreadable, treat as held")
        elif lease is None:
            stale.append(line)
        elif (
            isinstance(session, str)
            and session.strip()
            and isinstance(lease, dict)
            and lease.get("session") == session
        ):
            mine.append(line)
        else:
            live.append(line)
    return live, stale, mine


def main() -> int:
    payload_ok = True
    try:
        payload = json.loads(sys.stdin.read())
    except (ValueError, OSError):  # not JSON, not text, or stdin unreadable
        payload = None
    if not isinstance(payload, dict):
        # a session is never blocked, but it is told its id is unknown and why
        payload_ok = False
        payload = {}
    session = payload.get("session_id")
    # only a non-empty string identifies this session; anything else matches no lease
    session = session if isinstance(session, str) and session.strip() else ""
    # A failed fetch leaves the cached origin/<default> ref: never present it as current.
    fetched = run(["git", "fetch", "-q", "origin"]) is not None
    behind = (
        run(["git", "rev-list", "--count", f"HEAD..origin/{DEFAULT_BRANCH}"]) if fetched else None
    )
    warnings: list[str] = []
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
    issues, inventory_state = in_progress_issues()
    listed = inventory_state != "unavailable"  # only a read list may ever print "none"
    if inventory_state == "unavailable":
        warnings.append("the in-progress issue list")
    live, stale, mine = classify_leased(issues, _lease_lookup(), session)
    if any(line.endswith("treat as held") for line in live):
        warnings.append("at least one issue's lease")
    if not payload_ok:
        print(PAYLOAD_WARNING)
    for what in warnings:
        print(UNAVAILABLE_WARNING.format(what=what, project=PROJECT))
    if inventory_state in ("truncated", "partial"):
        print(INCOMPLETE_WARNING.format(count=len(issues), project=PROJECT))
    if fetched:
        print(
            f"[Forge] {PROJECT}: HEAD is {behind or '?'} commit(s) behind "
            f"origin/{DEFAULT_BRANCH}. Recent on main:"
        )
        print(recent or "  (unavailable)")
    else:
        print(FETCH_WARNING)
        print(
            f"[Forge] {PROJECT}: how far HEAD is behind origin/{DEFAULT_BRANCH} is UNKNOWN "
            "(origin could not be fetched). Recent on main, CACHED -- possibly stale:"
        )
        print(recent or "  (unavailable)")
    if prs is None:
        print("[Forge] Open PRs:\n  (unavailable -- the PR list could not be read from GitHub)")
    else:
        print("[Forge] Open PRs:\n" + (prs or "  none"))
    for line in mine:
        print(f"[Forge] Your current lease (this session, {session}): {line} -- keep working on it")
    if inventory_state in ("truncated", "partial"):
        inventory = "\n".join(live + ["  INCOMPLETE -- more leased issues may exist; see above"])
    elif listed:
        inventory = "\n".join(live) or "  none"
    else:
        inventory = "  UNAVAILABLE -- could not be read; do not assume none"
    print("[Forge] Issues leased by other agents (do NOT work on these):\n" + inventory)
    if stale:
        print(
            "[Forge] Labelled in-progress but no live lease (expired or released; "
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
