#!/usr/bin/env python3
"""Claude Code PreToolUse hook: block `git commit` on an issue branch without this session's lease.

Installed by `sdlcctl onboard`. Reads the hook payload on stdin; exit 2 blocks the tool call and
shows the message to the agent. Only Bash commands that commit on `*/issue-N*` branches are
examined; everything else passes.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from datetime import UTC, datetime, timedelta

PROJECT = "PROJECT_ID"
ISSUE_BRANCH = re.compile(r"(?:^|/)issue-(\d+)(?:$|[^0-9])")
CLAIM = re.compile(r"<!--\s*forge-claim\s+([^>]*?)\s*-->")
RELEASE = re.compile(r"<!--\s*forge-release\s+([^>]*?)\s*-->")
# `git [global options] commit`: -C/-c/--git-dir/... take the NEXT word as their value, so
# `git -C . commit` and `git -c k=v commit` are commits too. `commit.gpgsign=false` is not.
GIT_VALUE_OPTS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"}
)
# Words that run the rest of the command line as a new command (`env X=1 git commit`, ...).
WRAPPERS = frozenset({"env", "command", "exec", "nohup", "time", "builtin", "sudo", "xargs"})
SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
OPERATORS = frozenset({";", "&", "&&", "|", "||", "(", ")", ";;", "|&"})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Fallback for text shlex cannot tokenize (unbalanced quotes): any `.../git ... commit` word pair.
_GIT_COMMIT_LOOSE = re.compile(
    r"(?:^|[\s;&|(])(?:\S*/)?git(?:\.exe)?\s(?:.*\s)?commit(?=$|[\s;&|)])"
)
MAX_TTL_MINUTES = 7 * 24 * 60  # leases.MAX_TTL_MINUTES
TRUSTED = {"OWNER", "MEMBER", "COLLABORATOR"}  # plus GitHub Apps (user.type == "Bot")


def _segments(command: str) -> list[list[str]]:
    """Shell words of each simple command, split on `;`, `&&`, `||`, `|`, `&`, parentheses."""
    lexer = shlex.shlex(command.replace("\n", ";"), posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    lexer.commenters = ""
    segments: list[list[str]] = [[]]
    for token in lexer:
        if token in OPERATORS:
            segments.append([])
        else:
            segments[-1].append(token)
    return [seg for seg in segments if seg]


def _invokes_git_commit(argv: list[str], depth: int = 0) -> bool:
    words = list(argv)
    while words and (_ASSIGNMENT.match(words[0]) or os.path.basename(words[0]) in WRAPPERS):
        words.pop(0)
        while words and words[0].startswith("-"):  # wrapper flags: `env -i`, `sudo -u x`
            words.pop(0)
    if not words:
        return False
    program = os.path.basename(words[0])
    if program in SHELLS and "-c" in words[1:-1] and depth < 3:
        return is_git_commit(words[words.index("-c") + 1], depth + 1)
    if program not in ("git", "git.exe"):
        return False
    rest = words[1:]
    while rest:
        word = rest.pop(0)
        if word in GIT_VALUE_OPTS:
            if rest:
                rest.pop(0)  # the option's value, e.g. the path after -C
            continue
        if word.startswith("-"):
            continue  # --no-pager, --exec-path=..., -C. forms with an attached value
        return word == "commit"
    return False


def is_git_commit(command: str, depth: int = 0) -> bool:
    """True when any simple command in `command` runs `git ... commit`, however git is spelled
    (`git`, `/usr/bin/git`, `./git`) and whatever global options precede the subcommand."""
    try:
        segments = _segments(command)
    except (
        ValueError
    ):  # unbalanced quotes: be conservative, block-check anything that looks like one
        return bool(_GIT_COMMIT_LOOSE.search(command))
    return any(_invokes_git_commit(seg, depth) for seg in segments)


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


def _parse_iso(text: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)  # naive means UTC


def lease_for(issue: int) -> dict | None:
    """The live lease: the earliest-claiming live session (None if none/unreachable)."""
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
    # Same ordering as leases.current_lease(): a session's newest claim supersedes its older ones,
    # a release voids that session, and the live session that claimed FIRST holds the lease, so a
    # losing racer's retraction cannot clear the winner.
    first_seen: dict[str, int] = {}
    latest: dict[str, dict] = {}
    for position, c in enumerate(comments):
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
            exp = _parse_iso(fields.get("expires", ""))
            if exp is None or not all(k in fields for k in ("agent", "session", "branch")):
                continue
            posted = _parse_iso(c.get("created_at") or "")
            if posted is not None:  # expiry is capped at post time + MAX_TTL, as in leases.py
                exp = min(exp, posted + timedelta(minutes=MAX_TTL_MINUTES))
            previous = latest.get(fields["session"])
            if previous is not None and posted is not None and previous["expires"] <= posted:
                first_seen[fields["session"]] = position  # lapsed: a fresh claim, as in leases.py
            first_seen.setdefault(fields["session"], position)
            latest[fields["session"]] = {**fields, "expires": exp}
            continue
        r = RELEASE.search(body)
        released = dict(re.findall(r"(\w+)=(\S+)", r.group(1))).get("session") if r else None
        if released in latest:
            del latest[released]
            del first_seen[released]
    now = datetime.now(UTC)
    for session in sorted(latest, key=first_seen.__getitem__):
        if latest[session]["expires"] > now:
            return latest[session]
    return None


def open_pr_for(branch: str) -> bool:
    """An open PR from this branch is the durable claim once the lease is released."""
    try:
        out = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                PROJECT,
                "--head",
                branch,
                "--state",
                "open",
                "--json",
                "number",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return bool(json.loads(out or "[]"))
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
        return False


def decide(
    payload: dict, branch: str, lease_lookup=lease_for, pr_lookup=open_pr_for
) -> tuple[int, str]:
    if payload.get("tool_name") != "Bash":
        return 0, ""
    command = str((payload.get("tool_input") or {}).get("command", ""))
    if not is_git_commit(command):
        return 0, ""
    m = ISSUE_BRANCH.search(branch or "")
    if not m:
        return 0, ""
    issue = int(m.group(1))
    session = str(payload.get("session_id") or "")
    lease = lease_lookup(issue)
    if lease is None and pr_lookup(branch):
        return 0, ""  # follow-up commits to an open PR need no lease
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
