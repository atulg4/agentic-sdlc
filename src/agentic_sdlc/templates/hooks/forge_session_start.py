#!/usr/bin/env python3
"""Claude Code SessionStart hook: start every session from the real state of the repository.

Installed by `sdlcctl onboard`. Fetches origin (its remote-tracking refs only, see
`fetch_command`), reports how far the default branch moved, lists open PRs, this session's own
live lease (a resume, clear or compaction after a claim) and the issues currently leased by
other agents, and reminds the session to claim before implementing. Output goes into the
session's context.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

PROJECT = "PROJECT_ID"
DEFAULT_BRANCH = "DEFAULT_BRANCH_NAME"
#: the remote fetched at session start; a plain name, never a URL, path or option
REMOTE = "origin"
_PLAIN_REMOTE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)*$")


UNAVAILABLE_WARNING = (
    "[Forge] WARNING: lease inventory unavailable -- do not start issue work until it can be "
    "checked. ({what} could not be read from GitHub: authentication, network or API failure; "
    "fix `gh` and start a new session, or run `sdlcctl claims --project {project}`.)"
)
FETCH_SKIPPED = (
    "[Forge] Did not fetch {remote}: {why}. Fetching could run that program before the session "
    "starts; fetch by hand once it is vetted."
)
FETCH_WARNING = (
    "[Forge] WARNING: could not fetch origin -- the remote branch state below is the last "
    "cached copy and may be stale. Run `git fetch origin` and check before starting work."
)

INCOMPLETE_WARNING = (
    "[Forge] WARNING: lease inventory INCOMPLETE -- only {count} open issue(s) could be checked "
    "(the in-progress issue list or the recent lease-marker comments: a page was unreadable or "
    "the page cap was reached). Issues not listed below may still be leased: run `sdlcctl "
    "claims --project {project}` before starting issue work."
)
PAYLOAD_WARNING = (
    "[Forge] WARNING: could not read the SessionStart hook payload (not a JSON object), so this "
    "session's id is unknown and no lease below is shown as yours."
)
#: open in-progress issues read per page, and the most pages read (100 x 50 = 5000 leases)
ISSUE_PAGE_SIZE = 100
ISSUE_PAGE_CAP = 50
#: Codex 4240839923: the `in-progress` label is a hint, the marker is the lease. A live lease's
#: newest claim marker was posted at most LEASE_WINDOW_MINUTES ago (leases.MAX_TTL_MINUTES: its
#: expiry is capped at post time + that), so the comments updated since then are scanned for
#: claim markers, page by page up to COMMENT_PAGE_CAP; the label's removal never hides a lease.
LEASE_WINDOW_MINUTES = 7 * 24 * 60
COMMENT_PAGE_SIZE = 100
COMMENT_PAGE_CAP = 20
_CLAIM_LINE = re.compile(r"^<!--\s*forge-claim\s[^>]*-->$")
_ISSUE_URL = re.compile(r"/issues/(\d+)$")


LOG_SKIPPED = (
    "[Forge] Did not list recent commits: {why}. `git log` could run that program before the "
    "session starts; look by hand once it is vetted."
)

#: Configuration forced on every git this hook runs, through GIT_CONFIG_COUNT (so the argv stays
#: `git <subcommand> ...`): no fsmonitor hook and no hooks directory (Codex 4241434513 audit).
#: Defence in depth -- each command is still vetted (`fetch_blocker`, `log_blocker`).
GIT_CONFIG_OVERRIDES = (("core.fsmonitor", "false"), ("core.hooksPath", os.devnull))


def git_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment for a git this hook runs: `base` (default: this process's) with no pager
    (`GIT_PAGER=cat`), no lazy fetch of missing objects from a promisor remote
    (`GIT_NO_LAZY_FETCH`, which could contact the network and run its helpers), no terminal
    prompt, and GIT_CONFIG_OVERRIDES APPENDED to any GIT_CONFIG_COUNT entries already set."""
    env = dict(os.environ if base is None else base)
    try:
        count = int(env.get("GIT_CONFIG_COUNT", "0") or "0")
    except ValueError:
        count = 0
    count = max(count, 0)
    for key, value in GIT_CONFIG_OVERRIDES:
        env[f"GIT_CONFIG_KEY_{count}"] = key
        env[f"GIT_CONFIG_VALUE_{count}"] = value
        count += 1
    env["GIT_CONFIG_COUNT"] = str(count)
    env.update(GIT_PAGER="cat", GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0")
    return env


#: Variables that make the dynamic loader run code of the caller's choosing in any program.
_LOADER_ENV_PREFIXES = ("LD_", "DYLD_")


def _guard():
    """The sibling commit guard module, or None when it is missing."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import forge_commit_guard
    except ImportError:
        return None
    return forge_commit_guard


def run(cmd: list[str]) -> str | None:
    """The command's stdout, or None when it failed -- a failure is never an empty answer.

    Never a PATH lookup (Codex 4242095555, 4242095556): `git` and `gh` are the commit guard's
    vetted programs (`_trusted_program`: a file only root can replace), so a `git` or `gh` an
    earlier session dropped into a writable PATH entry can neither run here nor fabricate the
    lease inventory this hook prints. `gh` runs as the guard runs it (`_gh`: allowlisted
    environment, private config directory, pinned host). `git` runs with `git_env()` over this
    process's environment without the loader's variables (`LD_*`, `DYLD_*`); unlike the guard's
    plumbing probes it keeps the caller's PATH, HOME and SSH/credential settings, because the
    fetch contacts the remote through them -- the helpers it may run are vetted first
    (`fetch_blocker`). No trusted program, or no commit guard to vouch for one: None (fail
    closed: the output says the read was unavailable)."""
    guard = _guard()
    if guard is None or cmd[:1] not in (["git"], ["gh"]):
        return None
    try:
        if cmd[0] == "gh":
            return guard._gh(cmd[1:], check=True, timeout=60).stdout.strip()
        base = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(_LOADER_ENV_PREFIXES)
        }
        return subprocess.run(
            [guard._trusted_program("git"), *cmd[1:]],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
            env=git_env(base),
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None


def fetch_command(remote: str = REMOTE) -> list[str]:
    """`git fetch` that can only write `refs/remotes/<remote>/*` (Codex 4240526383). A bare
    `git fetch origin` follows the configured `remote.origin.fetch` refspecs, and one mapping onto
    `refs/heads/forge/issue-7` would force-update a local issue branch before the session even
    started. Here the refspec is explicit and `--refmap=` (empty) makes git ignore the
    configured ones even for opportunistic updates; tags, submodules, pruning, FETCH_HEAD and
    auto-gc are all off."""
    return [
        "git",
        "fetch",
        "--quiet",
        "--no-tags",
        "--no-recurse-submodules",
        "--no-prune",
        "--no-write-fetch-head",
        "--no-auto-gc",
        "--refmap=",
        remote,
        f"+refs/heads/*:refs/remotes/{remote}/*",
    ]


def _vet(sub: str, args: list[str]) -> str | None:
    """Why `git SUB ARGS` may run a program configured outside the command, judged by the commit
    guard's own `_helper_reason`, or None. Never raises: a session start is never blocked."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from forge_commit_guard import _helper_reason
    except ImportError:
        return (
            "the commit guard (forge_commit_guard.py) is missing, so git's configured helpers "
            "cannot be vetted"
        )
    try:
        return _helper_reason(sub, args, os.getcwd(), ((), (), ()))
    except Exception as exc:  # never block the session start
        return f"git's configured helpers could not be vetted ({type(exc).__name__})"


def fetch_blocker(remote: str = REMOTE) -> str | None:
    """Why the session-start fetch is skipped, or None to fetch: `remote` is not a plain name,
    or git would run a program configured outside the command (ssh/askpass/credential/proxy
    helpers, a remote helper URL, a reference-transaction or pre-auto-gc hook, ...)."""
    if not _PLAIN_REMOTE.match(remote):
        return f"the remote name {remote!r} is not a plain name"
    return _vet("fetch", [remote])


def log_command(branch: str = DEFAULT_BRANCH, remote: str = REMOTE) -> list[str]:
    """The recent-commits listing (Codex 4241434513). `log.showSignature` makes a plain `git
    log` verify every signed commit with `gpg.program` -- any program the configuration names --
    so signature verification is switched off explicitly; external diff and textconv drivers
    too, though `--oneline` shows no diff (pager, fsmonitor and hooks: `git_env`)."""
    return [
        "git",
        "log",
        "--no-show-signature",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--oneline",
        "-8",
        f"{remote}/{branch}",
        "--",
    ]


def log_blocker() -> str | None:
    """Why the recent-commits listing is skipped, or None to run it: `log_command()` vetted like
    the fetch."""
    return _vet("log", log_command()[2:])


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


def marker_issues(known: set[int]) -> tuple[list, str]:
    """The open issues (not in `known`, the labelled ones) with a claim-marker comment updated
    within LEASE_WINDOW_MINUTES, each marked `unlabelled`. The state is as for
    `in_progress_issues`; an issue whose details cannot be read is kept (its lease decides)."""
    since = datetime.now(UTC) - timedelta(minutes=LEASE_WINDOW_MINUTES)
    stamp = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    numbers: set[int] = set()
    state = "truncated"
    for page in range(1, COMMENT_PAGE_CAP + 1):
        raw = run(
            [
                "gh",
                "api",
                f"repos/{PROJECT}/issues/comments?since={stamp}"
                f"&per_page={COMMENT_PAGE_SIZE}&page={page}",
            ]
        )
        try:
            rows = json.loads(raw) if raw is not None else None
        except json.JSONDecodeError:
            rows = None
        if not isinstance(rows, list):
            state = "unavailable" if page == 1 else "partial"
            break
        for row in rows:
            body = str(row.get("body") or "") if isinstance(row, dict) else ""
            lines = body.splitlines()
            if not lines or not _CLAIM_LINE.match(lines[0].strip()):
                continue
            found = _ISSUE_URL.search(str(row.get("issue_url") or ""))
            if found is None:
                state = "partial"
                continue
            numbers.add(int(found.group(1)))
        if len(rows) < COMMENT_PAGE_SIZE:
            if state == "truncated":
                state = "complete"
            break
    issues: list = []
    for number in sorted(numbers - known):
        raw = run(["gh", "api", f"repos/{PROJECT}/issues/{number}"])
        try:
            row = json.loads(raw) if raw is not None else None
        except json.JSONDecodeError:
            row = None
        if not isinstance(row, dict):
            row = {"number": number, "title": "(issue details unreadable)", "assignees": []}
        elif "pull_request" in row or row.get("state", "open") != "open":
            continue
        issues.append({**row, "number": number, "unlabelled": True})
    return issues, state


def classify_leased(
    issues: list, lease_lookup, session: str = ""
) -> tuple[list[str], list[str], list[str]]:
    """Split `in-progress` issues into live leases held by OTHER sessions, expired/released ones
    (label left over), and this `session`'s own live leases (never "another agent's"). An issue
    found only by its marker (`unlabelled`) is listed when its lease is live, never as stale."""
    live: list[str] = []
    stale: list[str] = []
    mine: list[str] = []
    for issue in issues:
        who = ",".join(
            str(a.get("login", "")) for a in issue.get("assignees") or [] if isinstance(a, dict)
        )
        line = f"#{issue.get('number')} {issue.get('title', '')} (assignee: {who})"
        if issue.get("unlabelled"):
            line += " [in-progress label missing]"
        lease = lease_lookup(int(issue.get("number")))
        if isinstance(lease, dict) and lease.get("unavailable"):
            live.append(f"{line} -- lease unreadable, treat as held")
        elif lease is None:
            if not issue.get("unlabelled"):
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
    skipped = fetch_blocker()
    fetched = skipped is None and run(fetch_command()) is not None
    behind = (
        run(["git", "rev-list", "--count", f"HEAD..origin/{DEFAULT_BRANCH}"]) if fetched else None
    )
    warnings: list[str] = []
    # rev-list above runs only after the vetted fetch (no signatures, pager, diff or hooks).
    log_skipped = log_blocker()
    recent = run(log_command()) if log_skipped is None else None
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
    issues, label_state = in_progress_issues()
    marked, marker_state = marker_issues({int(i["number"]) for i in issues})
    issues += marked
    if label_state == "unavailable":
        warnings.append("the in-progress issue list")
    if marker_state == "unavailable":
        warnings.append("the recent lease-marker comments")
    states = {label_state, marker_state}
    if states == {"unavailable"}:
        inventory_state = "unavailable"
    elif states == {"complete"}:
        inventory_state = "complete"
    else:
        inventory_state = "truncated" if "truncated" in states else "partial"
    listed = inventory_state != "unavailable"  # only a read list may ever print "none"
    live, stale, mine = classify_leased(issues, _lease_lookup(), session)
    if any(line.endswith("treat as held") for line in live):
        warnings.append("at least one issue's lease")
    if not payload_ok:
        print(PAYLOAD_WARNING)
    for what in warnings:
        print(UNAVAILABLE_WARNING.format(what=what, project=PROJECT))
    if inventory_state in ("truncated", "partial"):
        print(INCOMPLETE_WARNING.format(count=len(issues), project=PROJECT))
    if log_skipped is not None:
        print(LOG_SKIPPED.format(why=log_skipped))
    if fetched:
        print(
            f"[Forge] {PROJECT}: HEAD is {behind or '?'} commit(s) behind "
            f"origin/{DEFAULT_BRANCH}. Recent on main:"
        )
        print(recent or "  (unavailable)")
    else:
        if skipped is not None:
            print(FETCH_SKIPPED.format(remote=REMOTE, why=skipped))
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
