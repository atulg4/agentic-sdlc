"""Issue leases: one agent/session per ticket, visible to every kind of agent.

A lease is GitHub-native so Actions jobs, cloud routines and interactive sessions all see it:
the issue carries the `in-progress` label, an assignee when one is known, and a machine-readable
comment ``<!-- forge-claim agent=… session=… branch=… expires=… -->``. A later
``<!-- forge-release session=… -->`` comment ends it. Leases expire, so a crashed agent cannot
block a ticket forever; an expired lease may be taken over (and the takeover is commented).

A claim with an assignee records it in the marker (``assignee=<login>``), and
``owns_assignee=1`` when the lease itself put that assignee on the issue (it was not assigned
already, or it was inherited from the expired lease this one took over). Release -- and a
takeover -- remove only an assignee a lease owned, and only when the authoritative live lease
does not want the same login; a pre-existing assignee is never touched. A release that finds
no lease reconciles the owned assignee of the last released one, so retrying a release whose
cleanup failed after its marker was posted completes that cleanup.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

GhRunner = Callable[..., str]

IN_PROGRESS_LABEL = "in-progress"
DEFAULT_TTL_MINUTES = 240
MAX_TTL_MINUTES = 7 * 24 * 60  # policy.lease_ttl_minutes maximum
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
CLAIM_MARKER = "forge-claim"
RELEASE_MARKER = "forge-release"
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@+-]{0,120}$")
_CLAIM = re.compile(r"<!--\s*forge-claim\s+(?P<fields>[^>]*?)\s*-->")
_RELEASE = re.compile(r"<!--\s*forge-release\s+(?P<fields>[^>]*?)\s*-->")
_FIELD = re.compile(r"(\w+)=(\S+)")
_PR_REF = re.compile(
    r"(?:\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b\s*:?\s*)"
    r"(?:(?P<repo>[\w.-]+/[\w.-]+)#|(?<![\w/#-])#|https?://github\.com/(?P<url_repo>[\w.-]+/[\w.-]+)/issues/)"
    r"(?P<num>\d+)\b",
    re.IGNORECASE,
)


class LeaseError(ValueError):
    """Raised when a lease operation is refused."""


@dataclass(frozen=True)
class Lease:
    issue: int
    agent: str
    session: str
    branch: str
    expires: datetime
    #: The login this lease asked to be assigned ('' = none).
    assignee: str = ""
    #: The lease added `assignee` to the issue (it was not already assigned), so release removes it.
    owns_assignee: bool = False

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
    optional = (lease.assignee,) if lease.assignee else ()
    for value in (lease.agent, lease.session, lease.branch, *optional):
        if not _TOKEN.fullmatch(value):
            raise LeaseError(f"lease field contains unsupported characters: {value!r}")
    owner = ""
    if lease.assignee:
        owner = f" assignee={lease.assignee}" + (" owns_assignee=1" if lease.owns_assignee else "")
    return (
        f"<!-- {CLAIM_MARKER} agent={lease.agent} session={lease.session} "
        f"branch={lease.branch} expires={_iso(lease.expires)}{owner} -->"
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
        assignee=fields.get("assignee", ""),
        owns_assignee=bool(fields.get("assignee")) and fields.get("owns_assignee") == "1",
    )


def _release_session(body: str) -> str | None:
    match = _RELEASE.search(body or "")
    if not match:
        return None
    return dict(_FIELD.findall(match.group("fields"))).get("session")


# ---------------------------------------------------------------- GitHub reads


def _paged(path: str, gh: GhRunner) -> list[dict]:
    """Every item of a paginated list endpoint (--slurp wraps the pages in one outer array)."""
    data = json.loads(gh(["api", path, "--paginate", "--slurp"]) or "[]")
    if not isinstance(data, list):
        return []
    items = [item for page in data for item in (page if isinstance(page, list) else [page])]
    return [item for item in items if isinstance(item, dict)]


def _comments(project: str, issue: int, gh: GhRunner) -> list[dict]:
    return _paged(f"repos/{project}/issues/{issue}/comments?per_page=100", gh)


def _trusted(comment: dict) -> bool:
    """Only repository members/collaborators and GitHub Apps (installed by an admin) may post
    lease markers; anyone else commenting on a public issue must not block or free a ticket."""
    if comment.get("author_association") in TRUSTED_ASSOCIATIONS:
        return True
    return (comment.get("user") or {}).get("type") == "Bot"


def current_lease(
    project: str, issue: int, gh: GhRunner, now: datetime | None = None
) -> Lease | None:
    """The authoritative lease: the live claim whose session claimed earliest.

    Comments are chronological. A session's newest marker supersedes its older ones (renewals);
    a trusted release marker voids that session's claims; expiry is capped at post time +
    MAX_TTL_MINUTES. Ordering by *first* claim time is what makes acquisition safe: two agents
    that both post a claim re-read the thread and only the earlier poster keeps it. A marker
    posted after the session's previous lease had already lapsed is a fresh claim, not a
    renewal: it queues behind whoever claimed in the meantime, so a stale session cannot
    reclaim seniority over a takeover. When no claim is live the most recent expired one is
    returned so callers can report a takeover.
    """
    now = now or datetime.now(UTC)
    first_seen: dict[str, int] = {}
    latest: dict[str, Lease] = {}
    for position, comment in enumerate(_comments(project, issue, gh)):
        if not _trusted(comment):
            continue
        body = comment.get("body") or ""
        parsed = parse_marker(body, issue)
        if parsed is not None:
            posted = _parse_iso(comment.get("created_at") or "")
            if posted is None:
                continue
            cap = posted + timedelta(minutes=MAX_TTL_MINUTES)
            lease = parsed if parsed.expires <= cap else replace(parsed, expires=cap)
            previous = latest.get(lease.session)
            if previous is not None and not previous.live(posted):
                first_seen[lease.session] = position  # lapsed: seniority is forfeited
            first_seen.setdefault(lease.session, position)
            latest[lease.session] = lease
            continue
        released = _release_session(body)
        if released in latest:
            del latest[released]
            del first_seen[released]
    ordered = [latest[sess] for sess in sorted(latest, key=first_seen.__getitem__)]
    live = [lease for lease in ordered if lease.live(now)]
    if live:
        return live[0]
    return ordered[-1] if ordered else None


def last_released_lease(project: str, issue: int, gh: GhRunner) -> Lease | None:
    """The lease the most recent trusted release (or retraction) marker ended: the claim it
    voided, with the assignee bookkeeping that claim recorded. None when no release has ended
    a claim. Lets a retried release finish the cleanup a failed one began after its marker."""
    latest: dict[str, Lease] = {}
    ended: Lease | None = None
    for comment in _comments(project, issue, gh):
        if not _trusted(comment):
            continue
        body = comment.get("body") or ""
        parsed = parse_marker(body, issue)
        if parsed is not None:
            latest[parsed.session] = parsed
            continue
        released = _release_session(body)
        if released in latest:
            ended = latest.pop(released)
    return ended


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
        refs = set()
        for m in _PR_REF.finditer(text):
            repo = m.group("repo") or m.group("url_repo")
            if repo and repo.lower() != project.lower():
                continue  # a closing reference into another repository
            refs.add(int(m.group("num")))
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


def _ensure_label(project: str, gh: GhRunner) -> None:
    """Repos onboarded before the lease feature lack the label; create it idempotently."""
    gh(
        [
            "label",
            "create",
            IN_PROGRESS_LABEL,
            "--repo",
            project,
            "--color",
            "fbca04",
            "--description",
            "Leased: an agent/session is actively implementing this",
            "--force",
        ]
    )


def _edit(project: str, issue: int, gh: GhRunner, *flags: str) -> None:
    gh(["issue", "edit", str(issue), "--repo", project, *flags])


# ---------------------------------------------------------------- operations


def _check_ttl(ttl_minutes: int) -> None:
    if not 1 <= ttl_minutes <= MAX_TTL_MINUTES:
        raise LeaseError(f"ttl_minutes must be between 1 and {MAX_TTL_MINUTES}")


def _post_and_arbitrate(
    project: str, issue: int, session: str, body: str, gh: GhRunner, now: datetime
) -> Lease | None:
    """Post a claim/renewal marker, then re-read the thread and keep it only if it is authoritative.

    GitHub has no compare-and-set, so every lease acquisition -- fresh claim, takeover or
    renewal -- appends its marker and re-reads; only the session `current_lease()` names keeps
    it, everyone else retracts. Returns the winner (``None`` when nothing is live).
    """
    _comment(project, issue, body, gh)
    winner = current_lease(project, issue, gh, now)
    if winner is not None and not winner.live(now):
        winner = None
    if winner is None or winner.session != session:
        _comment(
            project,
            issue,
            format_release_marker(session)
            + f"\nLost the claim race to session `{getattr(winner, 'session', '?')}`; retracting.",
            gh,
        )
    return winner


def _sync_label(project: str, issue: int, gh: GhRunner, now: datetime | None = None) -> None:
    """Make the `in-progress` label match the authoritative lease.

    Removes it only when no lease is live, then re-reads and restores it if a claimant arrived
    while we were removing it: label edits are not arbitrated, so the cleanup reconciles after.
    """
    now = now or datetime.now(UTC)
    held = current_lease(project, issue, gh, now)
    if held is None or not held.live(now):
        _edit(project, issue, gh, "--remove-label", IN_PROGRESS_LABEL)
        held = current_lease(project, issue, gh, now)
    if held is not None and held.live(now):
        _ensure_label(project, gh)
        _edit(project, issue, gh, "--add-label", IN_PROGRESS_LABEL)


def _sync_assignee(
    project: str, issue: int, login: str, gh: GhRunner, now: datetime | None = None
) -> None:
    """Remove `login` -- an assignee a dead lease added -- unless the authoritative live lease
    wants it, then re-read and restore it if a claimant that wants it arrived meanwhile: the same
    remove-then-reconcile as `_sync_label`, since assignee edits are not arbitrated either."""
    now = now or datetime.now(UTC)

    def wanted(held: Lease | None) -> bool:
        return held is not None and held.live(now) and held.assignee == login

    if wanted(current_lease(project, issue, gh, now)):
        return
    _edit(project, issue, gh, "--remove-assignee", login)
    if wanted(current_lease(project, issue, gh, now)):
        _edit(project, issue, gh, "--add-assignee", login)


def _drop_owned_assignee(
    project: str, issue: int, ended: Lease | None, gh: GhRunner, now: datetime | None
) -> None:
    """Undo the assignment an ended lease made (nothing when it did not make one)."""
    if ended is not None and ended.owns_assignee and ended.assignee:
        _sync_assignee(project, issue, ended.assignee, gh, now)


def _reconcile_released_assignee(
    project: str, issue: int, gh: GhRunner, now: datetime | None
) -> None:
    """Drop the assignee the last released lease owned if it is still on the issue and no live
    lease wants it (`_sync_assignee` re-checks that)."""
    ended = last_released_lease(project, issue, gh)
    if ended is None or not ended.owns_assignee or not ended.assignee:
        return
    target = json.loads(gh(["api", f"repos/{project}/issues/{issue}"]) or "{}")
    if ended.assignee in _assignee_logins(target if isinstance(target, dict) else {}):
        _sync_assignee(project, issue, ended.assignee, gh, now)


def _assignee_logins(target: dict) -> set[str]:
    return {
        str(a.get("login"))
        for a in target.get("assignees") or []
        if isinstance(a, dict) and a.get("login")
    }


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
    _check_ttl(ttl_minutes)
    lease = Lease(
        issue, agent, session, branch, now + timedelta(minutes=ttl_minutes), assignee or ""
    )
    format_claim_marker(lease)  # validate before reading or writing anything
    target = json.loads(gh(["api", f"repos/{project}/issues/{issue}"]) or "{}")
    if target.get("pull_request"):
        return ClaimResult(False, None, reason=f"#{issue} is a pull request, not an issue")
    if target.get("state") != "open":
        return ClaimResult(
            False, None, reason=f"issue #{issue} is {target.get('state', 'unknown')}"
        )
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
        # A renewal keeps the assignment bookkeeping of the lease it extends.
        lease = replace(lease, assignee=existing.assignee, owns_assignee=existing.owns_assignee)
        winner = _post_and_arbitrate(
            project,
            issue,
            session,
            format_claim_marker(lease) + f"\nLease renewed until {_iso(lease.expires)}.",
            gh,
            now,
        )
        if winner is None or winner.session != session:
            return ClaimResult(
                False,
                winner,
                reason=f"issue #{issue}: renewal lost to session={getattr(winner, 'session', '?')}",
            )
        return ClaimResult(True, lease, renewed=True)
    if existing is not None:
        took_over = existing.session
    prs = open_prs_for_issue(project, issue, gh)
    if prs:
        urls = ", ".join(p.get("url", "") for p in prs)
        return ClaimResult(
            False, None, reason=f"issue #{issue} already has an open pull request: {urls}"
        )
    note = (
        f"Claimed by `{agent}` (session `{session}`) on branch `{branch}`; "
        f"lease expires {_iso(lease.expires)}."
    )
    if took_over:
        note = f"Took over an expired lease from session `{took_over}`. " + note
    if assignee:
        # The lease owns the assignment it makes: one not already on the issue, or one the
        # expired lease it takes over owned (that lease will never release it now).
        inherited = (
            existing is not None and existing.owns_assignee and existing.assignee == assignee
        )
        lease = replace(lease, owns_assignee=assignee not in _assignee_logins(target) or inherited)
    marker = format_claim_marker(lease)
    winner = _post_and_arbitrate(project, issue, session, marker + "\n" + note, gh, now)
    if winner is None or winner.session != session:
        return ClaimResult(
            False,
            winner,
            reason=(
                f"issue #{issue}: lost the claim race to session={getattr(winner, 'session', '?')}"
            ),
        )
    flags = ["--add-label", IN_PROGRESS_LABEL]
    if assignee:
        flags += ["--add-assignee", assignee]
    try:
        _ensure_label(project, gh)
        _edit(project, issue, gh, *flags)
    except Exception:
        # The marker already made the lease authoritative, but the caller is about to fail and
        # nothing downstream will release it: retract so the issue is not blocked for the TTL.
        try:
            _comment(
                project,
                issue,
                format_release_marker(session)
                + "\nLabel/assignee bookkeeping failed; retracting the claim.",
                gh,
            )
            _sync_label(project, issue, gh, now)
            _drop_owned_assignee(project, issue, lease, gh, now)
        except Exception:  # noqa: BLE001 -- best effort; the original failure is what matters
            pass
        raise
    # A takeover ends the expired lease: an assignee it added (and this lease does not want)
    # would otherwise stay on the issue forever, accumulating one per takeover.
    if existing is not None and existing.assignee != lease.assignee:
        _drop_owned_assignee(project, issue, existing, gh, now)
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
    _check_ttl(ttl_minutes)
    existing = current_lease(project, issue, gh, now)
    if existing is None or existing.session != session:
        raise LeaseError(f"issue #{issue} is not leased by session {session}")
    if not existing.live(now):
        # An expired lease is up for takeover; renewing it could race a new claimant. Claim again.
        raise LeaseError(f"issue #{issue}: the lease of session {session} has expired; claim again")
    lease = replace(existing, expires=now + timedelta(minutes=ttl_minutes))
    body = format_claim_marker(lease) + f"\nLease renewed until {_iso(lease.expires)}."
    winner = _post_and_arbitrate(project, issue, session, body, gh, now)
    if winner is None or winner.session != session:
        raise LeaseError(
            f"issue #{issue}: renewal lost to session={getattr(winner, 'session', '?')}"
        )
    return lease


def release(
    project: str,
    issue: int,
    *,
    session: str,
    gh: GhRunner,
    force: bool = False,
    note: str = "",
    now: datetime | None = None,
) -> None:
    existing = current_lease(project, issue, gh, now)
    if existing is None:
        # Nothing to release -- possibly because an earlier release posted its marker and then
        # failed: finish that release's cleanup (idempotent, so a retry completes it).
        _sync_label(project, issue, gh, now)
        _reconcile_released_assignee(project, issue, gh, now)
        return
    if existing.session != session and not force:
        raise LeaseError(f"issue #{issue} is leased by session {existing.session}, not {session}")
    who = (
        session
        if existing.session == session
        else f"{session} (forced; holder was {existing.session})"
    )
    # The marker is authoritative; post it first so a failed label cleanup cannot leave the
    # lease live (and the issue unclaimable) until its TTL runs out.
    _comment(
        project,
        issue,
        format_release_marker(existing.session) + f"\nLease released by `{who}`. {note}".rstrip(),
        gh,
    )
    # A new claimant may have taken the lease (and added the label) since we read it.
    _sync_label(project, issue, gh, now)
    # The assignee this lease added goes too, reconciled the same way.
    _drop_owned_assignee(project, issue, existing, gh, now)


def list_claims(project: str, gh: GhRunner, now: datetime | None = None) -> list[ClaimRow]:
    now = now or datetime.now(UTC)
    issues = _paged(
        f"repos/{project}/issues?labels={IN_PROGRESS_LABEL}&state=open&per_page=100", gh
    )
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
