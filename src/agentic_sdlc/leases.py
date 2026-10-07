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
cleanup failed after its marker was posted completes that cleanup. A completed cleanup is
recorded (``<!-- forge-cleanup session=… assignee=… -->``) and a retry also skips a login that was
assigned again after the release, so it can never undo a later human assignment.

Markers count only from authors who can write to the repository (``trusted_marker_author``): a
user whose repository permission is write/maintain/admin, `github-actions[bot]`, or the Forge
Publisher App's bot. ``author_association`` is not authority (a read-only organization member
comments as MEMBER). The commit-guard hook mirrors the same rule.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

GhRunner = Callable[..., str]

IN_PROGRESS_LABEL = "in-progress"
DEFAULT_TTL_MINUTES = 240
MAX_TTL_MINUTES = 7 * 24 * 60  # policy.lease_ttl_minutes maximum
#: Repository permissions (`permission` or `role_name` of the collaborator permission API) whose
#: holders may post lease markers.
WRITE_PERMISSIONS = frozenset({"admin", "maintain", "write"})
#: The only GitHub App bots whose markers count: Actions' own token, and the Forge Publisher App
#: (its slug carries this hint, the same rule `sdlcctl doctor` identifies the App by). Name the
#: Publisher bot exactly with FORGE_LEASE_BOT_LOGINS (comma-separated) to replace the hint rule.
GITHUB_ACTIONS_BOT = "github-actions[bot]"
PUBLISHER_APP_SLUG_HINT = "agentic-sdlc"
LEASE_BOT_LOGINS_ENV = "FORGE_LEASE_BOT_LOGINS"
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
CLAIM_MARKER = "forge-claim"
RELEASE_MARKER = "forge-release"
CLEANUP_MARKER = "forge-cleanup"
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@+-]{0,120}$")
_CLAIM = re.compile(r"<!--\s*forge-claim\s+(?P<fields>[^>]*?)\s*-->")
_RELEASE = re.compile(r"<!--\s*forge-release\s+(?P<fields>[^>]*?)\s*-->")
_CLEANUP = re.compile(r"<!--\s*forge-cleanup\s+(?P<fields>[^>]*?)\s*-->")
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


def format_cleanup_marker(session: str, assignee: str) -> str:
    """Records that the released lease of `session` no longer holds `assignee` on the issue."""
    return f"<!-- {CLEANUP_MARKER} session={session} assignee={assignee} -->"


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


def trusted_bot(login: str, configured: str | None = None) -> bool:
    """`github-actions[bot]`, or the Publisher App's bot: the logins in FORGE_LEASE_BOT_LOGINS
    when set, else a `<slug>[bot]` whose slug carries PUBLISHER_APP_SLUG_HINT. No other bot."""
    login = login.lower()
    if login == GITHUB_ACTIONS_BOT:
        return True
    if configured is None:
        configured = os.environ.get(LEASE_BOT_LOGINS_ENV, "")
    named = {part.strip().lower() for part in configured.split(",") if part.strip()}
    if named:
        return login in named
    return login.endswith("[bot]") and PUBLISHER_APP_SLUG_HINT in login[: -len("[bot]")]


def trusted_marker_author(
    comment: dict, permission: Callable[[str], str | None], configured: str | None = None
) -> bool:
    """May this comment's author post lease markers? A bot by `trusted_bot`; a user only with
    write/maintain/admin on the repository (`permission(login)` -> the permission API's
    `permission`/`role_name`, None when it could not be read: untrusted). Never by
    `author_association`, which a read-only member or collaborator also carries."""
    user = comment.get("user") or {}
    login = str(user.get("login") or "")
    if not login:
        return False
    if user.get("type") == "Bot" or login.endswith("[bot]"):
        return user.get("type") == "Bot" and trusted_bot(login, configured)
    if not _LOGIN.fullmatch(login):
        return False
    granted = permission(login)
    return granted is not None and granted in WRITE_PERMISSIONS


class PermissionCache:
    """Repository permission per login, read once per run (`GET .../collaborators/{u}/permission`).
    A failed read is None (untrusted) and is not retried within the run."""

    def __init__(self, project: str, gh: GhRunner):
        self.project = project
        self.gh = gh
        self._known: dict[str, str | None] = {}

    def __call__(self, login: str) -> str | None:
        if login not in self._known:
            self._known[login] = self._read(login)
        return self._known[login]

    def _read(self, login: str) -> str | None:
        try:
            data = json.loads(
                self.gh(["api", f"repos/{self.project}/collaborators/{login}/permission"]) or "{}"
            )
        except Exception:  # noqa: BLE001 -- unreadable means untrusted (fail closed)
            return None
        if not isinstance(data, dict):
            return None
        # role_name distinguishes maintain (permission reads "write") and custom roles.
        for key in ("role_name", "permission"):
            value = str(data.get(key) or "").lower()
            if value in WRITE_PERMISSIONS:
                return value
        return str(data.get("permission") or "") or None


_RUN_TRUST: dict[tuple[str, int], PermissionCache] = {}


def _trust(project: str, gh: GhRunner) -> PermissionCache:
    """The permission cache of this run (one per project and gh runner)."""
    key = (project, id(gh))
    cache = _RUN_TRUST.get(key)
    if cache is None or cache.gh is not gh:
        cache = _RUN_TRUST[key] = PermissionCache(project, gh)
    return cache


def _trusted_comments(project: str, issue: int, gh: GhRunner) -> list[dict]:
    """The issue's comments whose authors may post lease markers, in order."""
    permission = _trust(project, gh)
    return [c for c in _comments(project, issue, gh) if trusted_marker_author(c, permission)]


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
    for position, comment in enumerate(_trusted_comments(project, issue, gh)):
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


@dataclass(frozen=True)
class ReleasedLease:
    """The lease the most recent trusted release ended, when that release was posted, and
    whether its assignee cleanup is recorded as done."""

    lease: Lease
    released_at: datetime | None
    cleaned: bool


def last_release(project: str, issue: int, gh: GhRunner) -> ReleasedLease | None:
    """The claim the most recent trusted release (or retraction) marker voided, with the
    assignee bookkeeping that claim recorded; None when no release has ended a claim. A trusted
    `forge-cleanup` marker for that session and assignee, posted after it, marks it cleaned."""
    latest: dict[str, Lease] = {}
    ended: ReleasedLease | None = None
    for comment in _trusted_comments(project, issue, gh):
        body = comment.get("body") or ""
        parsed = parse_marker(body, issue)
        if parsed is not None:
            latest[parsed.session] = parsed
            continue
        released = _release_session(body)
        if released in latest:
            posted = _parse_iso(comment.get("created_at") or "")
            ended = ReleasedLease(latest.pop(released), posted, cleaned=False)
            continue
        done = _CLEANUP.search(body)
        if done and ended is not None:
            fields = dict(_FIELD.findall(done.group("fields")))
            if (
                fields.get("session") == ended.lease.session
                and fields.get("assignee") == ended.lease.assignee
            ):
                ended = replace(ended, cleaned=True)
    return ended


def last_released_lease(project: str, issue: int, gh: GhRunner) -> Lease | None:
    """The lease the most recent trusted release (or retraction) marker ended (`last_release`).
    Lets a retried release finish the cleanup a failed one began after its marker."""
    ended = last_release(project, issue, gh)
    return ended.lease if ended is not None else None


def assigned_since(project: str, issue: int, login: str, since: datetime, gh: GhRunner) -> bool:
    """Was `login` assigned to the issue after `since` (issue `assigned` events)? Errs to True
    when the events cannot be read: the caller then leaves the assignee alone. (The assignment
    the lease itself made precedes its release marker, so it does not count.)"""
    try:
        events = _paged(f"repos/{project}/issues/{issue}/events?per_page=100", gh)
    except Exception:  # noqa: BLE001 -- unknown history: do not remove anyone
        return True
    for event in events:
        if event.get("event") != "assigned":
            continue
        who = str((event.get("assignee") or {}).get("login") or "")
        at = _parse_iso(str(event.get("created_at") or ""))
        if who.lower() == login.lower() and (at is None or at > since):
            return True
    return False


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
) -> bool:
    """Undo the assignment an ended lease made (nothing when it did not make one). True when the
    lease owned an assignee, so the caller can record the cleanup as done."""
    if ended is not None and ended.owns_assignee and ended.assignee:
        _sync_assignee(project, issue, ended.assignee, gh, now)
        return True
    return False


def _record_cleanup(project: str, issue: int, ended: Lease, gh: GhRunner) -> None:
    """Mark the released lease's assignee cleanup done, so a later retry never repeats it."""
    _comment(project, issue, format_cleanup_marker(ended.session, ended.assignee), gh)


def _reconcile_released_assignee(
    project: str, issue: int, gh: GhRunner, now: datetime | None
) -> None:
    """Finish the assignee cleanup of the last released lease -- only when it is not recorded as
    done, the login is still on the issue, nobody (re)assigned it after the release, and no live
    lease wants it (`_sync_assignee` re-checks that). Idempotent: it records completion."""
    ended = last_release(project, issue, gh)
    if ended is None or ended.cleaned:
        return
    lease = ended.lease
    if not lease.owns_assignee or not lease.assignee:
        return
    target = json.loads(gh(["api", f"repos/{project}/issues/{issue}"]) or "{}")
    if lease.assignee not in _assignee_logins(target if isinstance(target, dict) else {}):
        _record_cleanup(project, issue, lease, gh)
        return
    if ended.released_at is None or assigned_since(
        project, issue, lease.assignee, ended.released_at, gh
    ):
        return  # assigned again after the release (or unknowable): a later owner's assignment
    _sync_assignee(project, issue, lease.assignee, gh, now)
    _record_cleanup(project, issue, lease, gh)


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
            if _drop_owned_assignee(project, issue, lease, gh, now):
                _record_cleanup(project, issue, lease, gh)
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
    # The assignee this lease added goes too, reconciled the same way; completion is recorded
    # so a retried release cannot remove a login a maintainer assigns again afterwards.
    if _drop_owned_assignee(project, issue, existing, gh, now):
        _record_cleanup(project, issue, existing, gh)


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
