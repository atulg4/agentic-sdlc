#!/usr/bin/env python3
"""Claude Code PreToolUse hook: block `git commit` on an issue branch without this session's lease.

Installed by `sdlcctl onboard`. Reads the hook payload on stdin; exit 2 blocks the tool call and
shows the message to the agent. Only Bash commands that commit on `*/issue-N*` branches are
examined; everything else passes.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import UTC, datetime

PROJECT = "PROJECT_ID"
ISSUE_BRANCH = re.compile(r"(?:^|/)issue-(\d+)(?:$|[^0-9])")
CLAIM = re.compile(r"<!--\s*forge-claim\s+([^>]*?)\s*-->")
RELEASE = re.compile(r"<!--\s*forge-release\s+([^>]*?)\s*-->")
# `git [global options] commit`: -C/-c/--git-dir/... take the NEXT word as their value, so
# `git -C . commit` and `git -c k=v commit` are commits too. `commit.gpgsign=false` is not.
_GIT_VALUE_OPTS = r"(?:-C|-c|--git-dir|--work-tree|--namespace|--super-prefix|--config-env)"
_WORD = r"(?:'[^']*'|\"[^\"]*\"|\S+)"
GIT_COMMIT = re.compile(
    rf"(?:^|[\s;&|(])git(?:\s+(?:{_GIT_VALUE_OPTS}\s+{_WORD}|-\S+))*\s+commit(?=$|[\s;&|)])"
)
TRUSTED = {"OWNER", "MEMBER", "COLLABORATOR"}  # plus GitHub Apps (user.type == "Bot")


def current_branch(cwd: str | None) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def lease_for(issue: int) -> dict | None:
    """Latest live claim marker on the issue (None if none/released/expired/unreachable)."""
    try:
        out = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{PROJECT}/issues/{issue}/comments?per_page=100",
                "--paginate",
                "--slurp",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        pages = json.loads(out or "[]")
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
        return None
    comments = [c for page in pages if isinstance(page, list) for c in page if isinstance(c, dict)]
    lease = None
    for c in comments:
        if (
            c.get("author_association") not in TRUSTED
            and (c.get("user") or {}).get("type") != "Bot"
        ):
            continue  # markers only count from members/collaborators and GitHub Apps
        body = c.get("body") or ""
        found = list(CLAIM.finditer(body))
        m = found[-1] if found else None
        if m:
            fields = dict(re.findall(r"(\w+)=(\S+)", m.group(1)))
            try:
                exp = datetime.fromisoformat(fields.get("expires", "").replace("Z", "+00:00"))
            except ValueError:
                continue
            if exp.tzinfo is None:  # same rule as leases._parse_iso: naive means UTC
                exp = exp.replace(tzinfo=UTC)
            lease = {**fields, "expires": exp}
            continue
        r = RELEASE.search(body)
        if (
            r
            and lease
            and dict(re.findall(r"(\w+)=(\S+)", r.group(1))).get("session") == lease.get("session")
        ):
            lease = None
    if lease and lease["expires"] <= datetime.now(UTC):
        return None
    return lease


def decide(payload: dict, branch: str, lease_lookup=lease_for) -> tuple[int, str]:
    if payload.get("tool_name") != "Bash":
        return 0, ""
    command = str((payload.get("tool_input") or {}).get("command", ""))
    if not GIT_COMMIT.search(command):
        return 0, ""
    m = ISSUE_BRANCH.search(branch or "")
    if not m:
        return 0, ""
    issue = int(m.group(1))
    session = str(payload.get("session_id") or "")
    lease = lease_lookup(issue)
    if lease is None:
        return 2, (
            f"Forge guard: branch '{branch}' targets issue #{issue} but no live lease exists. "
            f"Claim it first: sdlcctl claim --project {PROJECT} --issue {issue} "
            f"--agent claude-code --session {session or '<session_id>'} --branch {branch}"
        )
    if session and lease.get("session") != session:
        return 2, (
            f"Forge guard: issue #{issue} is leased by agent={lease.get('agent')} "
            f"session={lease.get('session')} until {lease['expires'].isoformat()}. "
            "Do not commit to it; pick another issue or wait for the lease to expire."
        )
    return 0, ""


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0
    code, message = decide(payload, current_branch(payload.get("cwd")))
    if message:
        print(message, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
