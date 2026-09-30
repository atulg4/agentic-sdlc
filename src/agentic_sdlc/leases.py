"""Issue leases: one agent/session per ticket, visible to every kind of agent.

A lease is GitHub-native so Actions jobs, cloud routines and interactive sessions all see it:
the issue carries the `in-progress` label, an assignee when one is known, and a machine-readable
comment ``<!-- forge-claim agent=… session=… branch=… expires=… -->``. A later
``<!-- forge-release session=… -->`` comment ends it. Leases expire, so a crashed agent cannot
block a ticket forever; an expired lease may be taken over (and the takeover is commented).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

GhRunner = Callable[..., str]

IN_PROGRESS_LABEL = "in-progress"
DEFAULT_TTL_MINUTES = 240
CLAIM_MARKER = "forge-claim"
RELEASE_MARKER = "forge-release"
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@+-]{0,120}$")
_CLAIM = re.compile(r"<!--\s*forge-claim\s+(?P<fields>[^>]*?)\s*-->")
_RELEASE = re.compile(r"<!--\s*forge-release\s+(?P<fields>[^>]*?)\s*-->")
_FIELD = re.compile(r"(\w+)=(\S+)")
_PR_REF = re.compile(r"(?<![\w/#-])#(\d+)\b")


class LeaseError(ValueError):
    """Raised when a lease operation is refused."""


@dataclass(frozen=True)
class Lease:
    issue: int
    agent: str
    session: str
    branch: str
    expires: datetime

    def live(self, now: datetime) -> bool:
        return self.expires > now


@dataclass(frozen=True)
class ClaimResult:
    ok: bool
    lease: Lease | None = None
    reason: str = ""
    renewed: bool = False
    took_over_from: str | None = None

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "renewed": self.renewed,
            "took_over_from": self.took_over_from,
            "lease": None
            if self.lease is None
            else {**self.lease.__dict__, "expires": self.lease.expires.isoformat()},
        }


@dataclass(frozen=True)
class ClaimRow:
    lease: Lease
    expired: bool


# ---------------------------------------------------------------- markers


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(text: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def format_claim_marker(lease: Lease) -> str:
    for value in (lease.agent, lease.session, lease.branch):
        if not _TOKEN.fullmatch(value):
            raise LeaseError(f"lease field contains unsupported characters: {value!r}")
    return (
        f"<!-- {CLAIM_MARKER} agent={lease.agent} session={lease.session} "
        f"branch={lease.branch} expires={_iso(lease.expires)} -->"
    )


def format_release_marker(session: str) -> str:
    return f"<!-- {RELEASE_MARKER} session={session} -->"


def parse_marker(body: str, issue: int) -> Lease | None:
    """Return the lease encoded in a comment body, or None when absent/malformed."""
    matches = list(_CLAIM.finditer(body or ""))
    match = matches[-1] if matches else None
    if match is None:
        return None
    fields = dict(_FIELD.findall(match.group("fields")))
    expires = _parse_iso(fields.get("expires", ""))
    if expires is None or not all(k in fields for k in ("agent", "session", "branch")):
        return None
    return Lease(
        issue=issue,
        agent=fields["agent"],
        session=fields["session"],
        branch=fields["branch"],
        expires=expires,
    )


def _release_session(body: str) -> str | None:
    match = _RELEASE.search(body or "")
    if not match:
        return None
    return dict(_FIELD.findall(match.group("fields"))).get("session")


# ---------------------------------------------------------------- GitHub reads


def _comments(project: str, issue: int, gh: GhRunner) -> list[dict]:
    raw = gh(["api", f"repos/{project}/issues/{issue}/comments", "--paginate"]) or "[]"
    data = json.loads(raw)
    if (
        isinstance(data, list) and data and isinstance(data[0], list)
    ):  # --paginate may concatenate pages
        data = [item for page in data for item in page]
    return data if isinstance(data, list) else []


def current_lease(project: str, issue: int, gh: GhRunner) -> Lease | None:
    """Latest claim not followed by a release from the same session (comments are chronological)."""
    lease: Lease | None = None
    for comment in _comments(project, issue, gh):
        body = comment.get("body") or ""
        parsed = parse_marker(body, issue)
        if parsed is not None:
            lease = parsed
            continue
        released = _release_session(body)
        if released and lease is not None and released == lease.session:
            lease = None
    return lease


def open_prs_for_issue(project: str, issue: int, gh: GhRunner) -> list[dict]:
    raw = (
        gh(
            [
                "pr",
                "list",
                "--repo",
                project,
                "--state",
                "open",
                "--limit",
                "1000",
                "--json",
                "number,url,body,title,headRefName",
            ]
        )
        or "[]"
    )
    hits = []
    for pr in json.loads(raw):
        text = f"{pr.get('title', '')}\n{pr.get('body', '')}"
        refs = {int(n) for n in _PR_REF.findall(text)}
        head = pr.get("headRefName") or ""
        if issue in refs or re.search(rf"(^|[/-])issue-{issue}($|[^0-9])", head):
            hits.append(pr)
    return hits


# ---------------------------------------------------------------- GitHub writes


def _comment(project: str, issue: int, body: str, gh: GhRunner) -> None:
    gh(
        ["api", "-X", "POST", f"repos/{project}/issues/{issue}/comments", "--input", "-"],
        input=json.dumps({"body": body}),
    )


def _edit(project: str, issue: int, gh: GhRunner, *flags: str) -> None:
    gh(["issue", "edit", str(issue), "--repo", project, *flags])


# ---------------------------------------------------------------- operations


def claim(
    project: str,
    issue: int,
    *,
    agent: str,
    session: str,
    branch: str,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
    gh: GhRunner,
    now: datetime | None = None,
    assignee: str | None = None,
) -> ClaimResult:
    """Take the lease if free (or expired, or already ours). Never mutates on refusal."""
    now = now or datetime.now(UTC)
    if ttl_minutes < 1:
        raise LeaseError("ttl_minutes must be positive")
    existing = current_lease(project, issue, gh)
    took_over = None
    if existing is not None and existing.live(now):
        if existing.session != session:
            return ClaimResult(
                False,
                existing,
                reason=(
                    f"issue #{issue} is leased by agent={existing.agent} "
                    f"session={existing.session} "
                    f"on {existing.branch} until {_iso(existing.expires)}"
                ),
            )
        renewed = Lease(issue, agent, session, branch, now + timedelta(minutes=ttl_minutes))
        _comment(
            project,
            issue,
            format_claim_marker(renewed) + f"\nLease renewed until {_iso(renewed.expires)}.",
            gh,
        )
        return ClaimResult(True, renewed, renewed=True)
    if existing is not None:
        took_over = existing.session
    prs = open_prs_for_issue(project, issue, gh)
    if prs:
        urls = ", ".join(p.get("url", "") for p in prs)
        return ClaimResult(
            False, None, reason=f"issue #{issue} already has an open pull request: {urls}"
        )
    lease = Lease(issue, agent, session, branch, now + timedelta(minutes=ttl_minutes))
    flags = ["--add-label", IN_PROGRESS_LABEL]
    if assignee:
        flags += ["--add-assignee", assignee]
    _edit(project, issue, gh, *flags)
    note = (
        f"Claimed by `{agent}` (session `{session}`) on branch `{branch}`; "
        f"lease expires {_iso(lease.expires)}."
    )
    if took_over:
        note = f"Took over an expired lease from session `{took_over}`. " + note
    _comment(project, issue, format_claim_marker(lease) + "\n" + note, gh)
    return ClaimResult(True, lease, took_over_from=took_over)


def renew(
    project: str,
    issue: int,
    *,
    session: str,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
    gh: GhRunner,
    now: datetime | None = None,
) -> Lease:
    now = now or datetime.now(UTC)
    existing = current_lease(project, issue, gh)
    if existing is None or existing.session != session:
        raise LeaseError(f"issue #{issue} is not leased by session {session}")
    lease = Lease(
        issue, existing.agent, session, existing.branch, now + timedelta(minutes=ttl_minutes)
    )
    _comment(
        project,
        issue,
        format_claim_marker(lease) + f"\nLease renewed until {_iso(lease.expires)}.",
        gh,
    )
    return lease


def release(
    project: str, issue: int, *, session: str, gh: GhRunner, force: bool = False, note: str = ""
) -> None:
    existing = current_lease(project, issue, gh)
    if existing is None:
        _edit(project, issue, gh, "--remove-label", IN_PROGRESS_LABEL)
        return
    if existing.session != session and not force:
        raise LeaseError(f"issue #{issue} is leased by session {existing.session}, not {session}")
    _edit(project, issue, gh, "--remove-label", IN_PROGRESS_LABEL)
    who = (
        session
        if existing.session == session
        else f"{session} (forced; holder was {existing.session})"
    )
    _comment(
        project,
        issue,
        format_release_marker(existing.session) + f"\nLease released by `{who}`. {note}".rstrip(),
        gh,
    )


def list_claims(project: str, gh: GhRunner, now: datetime | None = None) -> list[ClaimRow]:
    now = now or datetime.now(UTC)
    raw = (
        gh(
            [
                "api",
                f"repos/{project}/issues?labels={IN_PROGRESS_LABEL}&state=open&per_page=100",
                "--paginate",
            ]
        )
        or "[]"
    )
    issues = json.loads(raw)
    if issues and isinstance(issues[0], list):
        issues = [i for page in issues for i in page]
    rows = []
    for item in issues:
        if item.get("pull_request"):
            continue
        lease = current_lease(project, int(item["number"]), gh)
        if lease is not None:
            rows.append(ClaimRow(lease, expired=not lease.live(now)))
    return sorted(rows, key=lambda r: r.lease.issue)


def render_claims(rows: Sequence[ClaimRow]) -> str:
    if not rows:
        return "no active leases"
    lines = [f"{'issue':>6}  {'state':7}  {'agent':22}  {'session':20}  {'branch':28}  expires"]
    for r in rows:
        state = "EXPIRED" if r.expired else "live"
        lines.append(
            f"#{r.lease.issue:<5}  {state:7}  {r.lease.agent[:22]:22}  {r.lease.session[:20]:20}  "
            f"{r.lease.branch[:28]:28}  {_iso(r.lease.expires)}"
        )
    return "\n".join(lines)
