"""Issue leases: one agent/session per ticket, visible to every kind of agent.

A lease is GitHub-native so Actions jobs, cloud routines and interactive sessions all see it:
the issue carries the `in-progress` label, an assignee when one is known, and a machine-readable
comment ``<!-- forge-claim agent=… session=… branch=… expires=… -->``. A later
``<!-- forge-release session=… -->`` comment ends it. Leases expire, so a crashed agent cannot
block a ticket forever; an expired lease may be taken over (and the takeover is commented).

A claim with an assignee records it in the marker (``assignee=<login>``), and
``owns_assignee=1`` when the lease itself put that assignee on the issue (it was not assigned
already, or it was inherited from the expired lease this one took over). A fresh assignment is
owned only when PROVEN: a fresh read right before the add finds the login unassigned and the
issue events then show a new `assigned` event for it by this process's own actor; the claim
marker carries no ownership until a follow-up marker records that proof. Release -- and a
takeover -- remove only an assignee a lease owned, and only when the authoritative live lease
does not want the same login; a pre-existing assignee is never touched. Every claim and release
reconciles the owned assignees of ALL ended leases (released, or expired and taken over) whose
cleanup is not recorded, so a cleanup that failed after its marker was posted -- a release's, or
a takeover's, which is best effort and never fails the claim -- is completed by a later one. A
completed cleanup is recorded (``<!-- forge-cleanup session=… assignee=… -->``) and a retry also
skips a login that was assigned again after the lease ended, so it can never undo a later human
assignment. When a live lease wants a login an ended lease owned, the login stays and the
ended lease's cleanup is recorded only after its ownership is handed to that live lease (its
marker re-posted with ``owns_assignee=1``, sticky for the rest of its run), so the live lease's
release removes it; a hand-over that fails leaves the cleanup pending. Every removal path
(release, takeover, retry) also reads the issue's events first: a login a maintainer removed and
re-assigned WHILE the lease was active is the maintainer's now, so the lease's ownership ends and
it stays; events that cannot be read keep it too (``reassigned_during``). A takeover inherits
the expired lease's ownership only when that check proves it was not superseded, and a live
lease found holding superseded ownership is disowned (``owns_assignee=0``, sticky for its run).

A marker is the whole first line of a comment, alone, exactly where this module writes it; a
comment with marker syntax anywhere else, or with more than one marker, carries none. Free text
in a marker comment (a release ``note``) has its ``<!--``/``-->`` escaped (``sanitize_text``).

Markers count only from authors who can write to the repository (``trusted_marker_author``): a
user whose repository permission is write/maintain/admin, `github-actions[bot]`, or the Forge
Publisher App's bot. ``author_association`` is not authority (a read-only organization member
comments as MEMBER). The commit-guard hook mirrors the same rule.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

GhRunner = Callable[..., str]
_log = logging.getLogger(__name__)

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
#: A marker is the WHOLE first line of a comment, exactly where this module writes it; marker
#: syntax anywhere else (a human note, a quoted marker) is never read.
_MARKER_LINE = re.compile(
    r"^<!--\s*(?P<kind>forge-claim|forge-release|forge-cleanup)\s+(?P<fields>[^>]*?)\s*-->$"
)
#: Any marker opening anywhere in a body: a comment holding more than one is invalid.
_ANY_MARKER = re.compile(r"<!--\s*forge-(?:claim|release|cleanup)\b")
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
    #: When this lease's run of claim markers began (the first marker of the session's current
    #: claim, server time; renewals keep it). Read from the thread, never written to a marker;
    #: None for a lease not read from the thread.
    claimed_at: datetime | None = field(default=None, compare=False)
    #: The marker revokes `owns_assignee` (``owns_assignee=0``): the ownership this run held was
    #: found superseded by a maintainer's re-assignment. Sticky for the rest of the run.
    disowned: bool = field(default=False, compare=False)

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
            else {
                **{
                    k: v
                    for k, v in self.lease.__dict__.items()
                    if k not in ("claimed_at", "disowned")
                },
                "expires": self.lease.expires.isoformat(),
            },
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
        owns = (
            " owns_assignee=0"
            if lease.disowned
            else (" owns_assignee=1" if lease.owns_assignee else "")
        )
        owner = f" assignee={lease.assignee}" + owns
    return (
        f"<!-- {CLAIM_MARKER} agent={lease.agent} session={lease.session} "
        f"branch={lease.branch} expires={_iso(lease.expires)}{owner} -->"
    )


def format_release_marker(session: str) -> str:
    return f"<!-- {RELEASE_MARKER} session={session} -->"


def format_cleanup_marker(session: str, assignee: str) -> str:
    """Records that the released lease of `session` no longer holds `assignee` on the issue."""
    return f"<!-- {CLEANUP_MARKER} session={session} assignee={assignee} -->"


def sanitize_text(text: str) -> str:
    """Free text placed in a marker comment, with HTML comment delimiters escaped: a note that
    quotes `<!-- forge-claim ... -->` must never read as a marker (`_marker`)."""
    return str(text or "").replace("<!--", "&lt;!--").replace("-->", "--&gt;")


def _marker_comment(marker: str, text: str = "") -> str:
    """A marker comment as this module posts it: the marker alone on the first line, then the
    (sanitized) human text."""
    text = sanitize_text(text).strip()
    return f"{marker}\n{text}" if text else marker


def _marker(body: str) -> tuple[str, dict[str, str]] | None:
    """The marker a comment carries: its kind and fields, read ONLY from the comment's first line
    (where every marker is written). A comment with marker syntax anywhere else, or with more
    than one marker, carries none."""
    body = body or ""
    if len(_ANY_MARKER.findall(body)) != 1:
        return None
    lines = body.splitlines()
    match = _MARKER_LINE.match(lines[0].strip()) if lines else None
    if match is None:
        return None
    return match.group("kind"), dict(_FIELD.findall(match.group("fields")))


def parse_marker(body: str, issue: int) -> Lease | None:
    """Return the lease encoded in a comment body, or None when absent/malformed."""
    marker = _marker(body)
    if marker is None or marker[0] != CLAIM_MARKER:
        return None
    fields = marker[1]
    expires = _parse_iso(fields.get("expires", ""))
    if expires is None or not all(k in fields for k in ("agent", "session", "branch")):
        return None
    owns = fields.get("owns_assignee")
    return Lease(
        issue=issue,
        agent=fields["agent"],
        session=fields["session"],
        branch=fields["branch"],
        expires=expires,
        assignee=fields.get("assignee", ""),
        owns_assignee=bool(fields.get("assignee")) and owns == "1",
        disowned=bool(fields.get("assignee")) and owns == "0",
    )


def _release_session(body: str) -> str | None:
    marker = _marker(body)
    if marker is None or marker[0] != RELEASE_MARKER:
        return None
    return marker[1].get("session")


def _cleanup_fields(body: str) -> dict[str, str] | None:
    marker = _marker(body)
    if marker is None or marker[0] != CLEANUP_MARKER:
        return None
    return marker[1]


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


def login_key(login: object) -> str:
    """A GitHub login as compared. Logins are case-insensitive (`Alice` and `alice` are one
    account), so EVERY login comparison and cache key -- assignees, marker authors, bot logins,
    the permission cache -- goes through here; the original spelling is kept for display and
    API calls. The commit-guard hook carries the same function."""
    return str(login or "").strip().casefold()


def same_login(a: object, b: object) -> bool:
    return login_key(a) == login_key(b)


def trusted_bot(login: str, configured: str | None = None) -> bool:
    """`github-actions[bot]`, or the Publisher App's bot: the logins in FORGE_LEASE_BOT_LOGINS
    when set, else a `<slug>[bot]` whose slug carries PUBLISHER_APP_SLUG_HINT. No other bot."""
    login = login_key(login)
    if login == login_key(GITHUB_ACTIONS_BOT):
        return True
    if configured is None:
        configured = os.environ.get(LEASE_BOT_LOGINS_ENV, "")
    named = {login_key(part) for part in configured.split(",") if part.strip()}
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
    if user.get("type") == "Bot" or login_key(login).endswith("[bot]"):
        return user.get("type") == "Bot" and trusted_bot(login, configured)
    if not _LOGIN.fullmatch(login):
        return False
    granted = permission(login)
    return granted is not None and login_key(granted) in WRITE_PERMISSIONS


class PermissionCache:
    """Repository permission per login, read once per run (`GET .../collaborators/{u}/permission`).
    A failed read is None (untrusted) and is not retried within the run. Keyed by `login_key`:
    `Alice` and `alice` are one read."""

    def __init__(self, project: str, gh: GhRunner):
        self.project = project
        self.gh = gh
        self._known: dict[str, str | None] = {}

    def __call__(self, login: str) -> str | None:
        key = login_key(login)
        if key not in self._known:
            self._known[key] = self._read(login)
        return self._known[key]

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
            value = login_key(data.get(key))
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
    ordered = _open_leases(project, issue, gh)
    live = [lease for lease in ordered if lease.live(now)]
    if live:
        return live[0]
    return ordered[-1] if ordered else None


def _open_leases(project: str, issue: int, gh: GhRunner) -> list[Lease]:
    """Every session's unreleased lease (live or expired), in order of first claim."""
    first_seen: dict[str, int] = {}
    started: dict[str, datetime] = {}
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
                started[lease.session] = posted
            else:
                lease = _keep_ownership(previous, lease)
            first_seen.setdefault(lease.session, position)
            started.setdefault(lease.session, posted)
            latest[lease.session] = replace(lease, claimed_at=started[lease.session])
            continue
        released = _release_session(body)
        if released in latest:
            del latest[released]
            del first_seen[released]
            del started[released]
    return [latest[sess] for sess in sorted(latest, key=first_seen.__getitem__)]


def _keep_ownership(previous: Lease | None, lease: Lease) -> Lease:
    """Assignee ownership is sticky within one run of a session's markers: once a marker of the
    run records `owns_assignee` for a login, a later marker of that run for the same login keeps
    it. A renewal that read the thread before an ownership transfer (`_transfer_assignee`) was
    posted would otherwise drop the transferred ownership again. A revocation
    (``owns_assignee=0``, `_disown_superseded`) is sticky the same way and wins over ownership:
    a marker posted later in the run from a stale read cannot re-own a maintainer's login."""
    if lease.assignee and lease.disowned:
        return replace(lease, owns_assignee=False)
    if (
        previous is not None
        and previous.disowned
        and lease.assignee
        and same_login(previous.assignee, lease.assignee)
    ):
        return replace(lease, owns_assignee=False, disowned=True)
    if (
        previous is not None
        and previous.owns_assignee
        and not lease.owns_assignee
        and lease.assignee
        and same_login(previous.assignee, lease.assignee)
    ):
        return replace(lease, owns_assignee=True)
    return lease


@dataclass(frozen=True)
class ReleasedLease:
    """A lease that ended -- by a trusted release (or retraction) marker, or by a takeover after
    it expired -- when it ended, and whether its assignee cleanup is recorded as done."""

    lease: Lease
    released_at: datetime | None
    cleaned: bool


def ended_leases(project: str, issue: int, gh: GhRunner) -> list[ReleasedLease]:
    """Every lease that has ended, oldest first, with the assignee bookkeeping it recorded. A
    lease ends at a trusted release marker for its session, or when another session claims after
    it expired (a takeover: it can never release now). A trusted `forge-cleanup` marker for a
    session and assignee marks every earlier ended lease of that pair cleaned."""
    latest: dict[str, Lease] = {}
    ended: list[ReleasedLease] = []
    for comment in _trusted_comments(project, issue, gh):
        body = comment.get("body") or ""
        posted = _parse_iso(comment.get("created_at") or "")
        parsed = parse_marker(body, issue)
        if parsed is not None:
            if posted is not None:
                cap = posted + timedelta(minutes=MAX_TTL_MINUTES)
                parsed = parsed if parsed.expires <= cap else replace(parsed, expires=cap)
                # Every lease that had expired when this claim was posted has ended -- taken
                # over by another session, or reclaimed by its OWN session (a lapsed marker is a
                # fresh claim, `current_lease`): either way it can never release now, and
                # replacing it below without recording it would lose its owned-assignee cleanup.
                for other in [o for o in latest if not latest[o].live(posted)]:
                    ended.append(ReleasedLease(latest.pop(other), posted, cleaned=False))
            previous = latest.get(parsed.session)
            began = previous.claimed_at if previous is not None else posted
            latest[parsed.session] = replace(_keep_ownership(previous, parsed), claimed_at=began)
            continue
        released = _release_session(body)
        if released in latest:
            ended.append(ReleasedLease(latest.pop(released), posted, cleaned=False))
            continue
        fields = _cleanup_fields(body)
        if fields is not None:
            ended = [
                replace(e, cleaned=True)
                if e.lease.session == fields.get("session")
                and same_login(e.lease.assignee, fields.get("assignee"))
                else e
                for e in ended
            ]
    return ended


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
        if same_login(who, login) and (at is None or at > since):
            return True
    return False


def reassigned_during(
    project: str, issue: int, login: str, claimed_at: datetime | None, gh: GhRunner
) -> bool | None:
    """Was `login` assigned to the issue again -- by someone other than the lease's own claim --
    after the lease claimed at `claimed_at`? From the issue's `assigned`/`unassigned` events since
    the claim: the lease's own assignment is at most ONE `assigned` event, so a second one, or an
    `assigned` after an `unassigned` (a maintainer removed and re-added the login), supersedes the
    lease's ownership. None when it cannot be known (no claim time, unreadable events or times):
    the caller then keeps the assignee."""
    if claimed_at is None:
        return None
    try:
        events = _issue_events(project, issue, gh)
    except Exception:  # noqa: BLE001 -- unknown history: do not remove anyone
        return None
    return _reassigned_in(events, login, claimed_at)


def _issue_events(project: str, issue: int, gh: GhRunner) -> list[dict]:
    return _paged(f"repos/{project}/issues/{issue}/events?per_page=100", gh)


def _event_key(event: dict) -> str:
    """An issue event's identity: its id, else its whole content (fakes and old payloads)."""
    if event.get("id") is not None:
        return f"id:{event['id']}"
    return json.dumps(event, sort_keys=True, default=str)


def _assigned_after(before: list[dict], after: list[dict], login: str) -> bool:
    """Does `after` hold an `assigned` event for `login` that the `before` snapshot lacked?"""
    seen: dict[str, int] = {}
    for event in before:
        seen[_event_key(event)] = seen.get(_event_key(event), 0) + 1
    for event in after:
        key = _event_key(event)
        if seen.get(key):
            seen[key] -= 1
            continue
        if event.get("event") == "assigned" and same_login(
            (event.get("assignee") or {}).get("login"), login
        ):
            return True
    return False


def _reassigned_in(events: list[dict], login: str, claimed_at: datetime) -> bool | None:
    """`reassigned_during` over an already-read event list."""
    assignments, removed = 0, False
    for event in events:
        kind = event.get("event")
        if kind not in ("assigned", "unassigned"):
            continue
        if not same_login((event.get("assignee") or {}).get("login"), login):
            continue
        at = _parse_iso(str(event.get("created_at") or ""))
        if at is None:
            return None
        if at < claimed_at:
            continue
        if kind == "unassigned":
            removed = True
        elif removed or assignments:
            return True
        else:
            assignments += 1
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
            _marker_comment(
                format_release_marker(session),
                f"Lost the claim race to session `{getattr(winner, 'session', '?')}`; retracting.",
            ),
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
) -> bool:
    """Remove `login` -- an assignee a dead lease added -- unless the authoritative live lease
    wants it, then re-read and restore it if a claimant that wants it arrived meanwhile: the same
    remove-then-reconcile as `_sync_label`, since assignee edits are not arbitrated either. True
    when the login was removed and stays removed."""
    now = now or datetime.now(UTC)

    def wanted(held: Lease | None) -> bool:
        return held is not None and held.live(now) and same_login(held.assignee, login)

    if wanted(current_lease(project, issue, gh, now)):
        return False
    _edit(project, issue, gh, "--remove-assignee", login)
    if wanted(current_lease(project, issue, gh, now)):
        _edit(project, issue, gh, "--add-assignee", login)
        return False
    return True


def _drop_owned_assignee(
    project: str,
    issue: int,
    ended: Lease | None,
    gh: GhRunner,
    now: datetime | None,
    *,
    check_history: bool = True,
) -> bool:
    """Undo the assignment an ended lease made (nothing when it did not make one). True when the
    cleanup is finished, so the caller records it: the login was removed, or a maintainer
    assigned it again while the lease was active (`reassigned_during`), which ends the lease's
    ownership -- that newer assignment is the maintainer's and stays. Unknowable history keeps
    the assignee and leaves the cleanup pending (False) for a later reconciliation."""
    if ended is None or not ended.owns_assignee or not ended.assignee:
        return False
    try:
        before: list[dict] | None = _issue_events(project, issue, gh)
    except Exception:  # noqa: BLE001 -- unknown history
        before = None
    if check_history:
        if before is None or ended.claimed_at is None:
            return False
        superseded = _reassigned_in(before, ended.assignee, ended.claimed_at)
        if superseded is None:
            return False
        if superseded:
            # The login is the maintainer's: no live lease may own it through this one either.
            return _disown_superseded(project, issue, ended, gh, now)
    if not _sync_assignee(project, issue, ended.assignee, gh, now):
        # A live lease wants the login: it stays. The ended lease's cleanup is finished only
        # once that lease OWNS it (so its release removes it); otherwise it stays pending.
        return _transfer_assignee(project, issue, ended, gh, now)
    if before is None:
        return True
    # The history check and the removal are not atomic: a maintainer may have assigned the
    # login in between, and the removal just undid that. Re-read and give it back.
    try:
        after = _issue_events(project, issue, gh)
    except Exception:  # noqa: BLE001 -- cannot tell: restore, keep the cleanup pending
        _edit(project, issue, gh, "--add-assignee", ended.assignee)
        return False
    if _assigned_after(before, after, ended.assignee):
        _edit(project, issue, gh, "--add-assignee", ended.assignee)  # the maintainer's now
    return True


def _disown_superseded(
    project: str, issue: int, ended: Lease, gh: GhRunner, now: datetime | None
) -> bool:
    """An ended lease's ownership of its assignee was superseded by a maintainer's
    re-assignment. A live lease that inherited that ownership (a takeover, a hand-over) holds
    it on the same stale grounds, so its release would remove the maintainer's assignment:
    post its marker again with ``owns_assignee=0`` (sticky for the rest of its run). True when
    no live lease owns the login any more."""
    now = now or datetime.now(UTC)
    holder = current_lease(project, issue, gh, now)
    if (
        holder is None
        or not holder.live(now)
        or holder.session == ended.session
        or not holder.owns_assignee
        or not same_login(holder.assignee, ended.assignee)
    ):
        return True
    _comment(
        project,
        issue,
        _marker_comment(
            format_claim_marker(replace(holder, owns_assignee=False, disowned=True)),
            f"Assignee `{holder.assignee}` was re-assigned by a maintainer while the lease of "
            f"session `{ended.session}` held it; this lease no longer owns it.",
        ),
        gh,
    )
    holder = current_lease(project, issue, gh, now)
    return not (
        holder is not None
        and holder.live(now)
        and holder.owns_assignee
        and same_login(holder.assignee, ended.assignee)
    )


def _transfer_assignee(
    project: str, issue: int, ended: Lease, gh: GhRunner, now: datetime | None
) -> bool:
    """Hand the assignee an ended lease owned to the live lease that wants the same login. That
    lease found the login already on the issue, so it does not own it, and its release would
    leave it forever once the ended lease's cleanup is recorded. Posts the holder's marker again
    with ``owns_assignee=1`` (sticky for the rest of its run, `_keep_ownership`) and re-reads:
    True only when the authoritative lease now owns the login; anything else leaves the ended
    lease's cleanup pending for a later reconciliation."""
    now = now or datetime.now(UTC)
    holder = current_lease(project, issue, gh, now)
    if holder is None or not holder.live(now) or not same_login(holder.assignee, ended.assignee):
        return False
    if not holder.owns_assignee:
        owned = replace(holder, owns_assignee=True)
        _comment(
            project,
            issue,
            _marker_comment(
                format_claim_marker(owned),
                f"Assignee `{holder.assignee}` handed over from the ended lease of session "
                f"`{ended.session}`; it is removed when this lease is released.",
            ),
            gh,
        )
        holder = current_lease(project, issue, gh, now)
    return (
        holder is not None
        and holder.live(now)
        and holder.owns_assignee
        and same_login(holder.assignee, ended.assignee)
    )


def _record_cleanup(project: str, issue: int, ended: Lease, gh: GhRunner) -> None:
    """Mark the released lease's assignee cleanup done, so a later retry never repeats it."""
    _comment(
        project, issue, _marker_comment(format_cleanup_marker(ended.session, ended.assignee)), gh
    )


def _reconcile_released_assignees(
    project: str, issue: int, gh: GhRunner, now: datetime | None
) -> None:
    """Finish the assignee cleanup of EVERY ended lease (`ended_leases`) not recorded as done --
    not only the latest, since another session may claim and release before a failed cleanup is
    retried. Each owned login is removed only when it is still on the issue, nobody (re)assigned
    it after that lease ended, and no live lease wants it (`_sync_assignee` re-checks that).
    Idempotent: each completion is recorded."""
    pending: dict[tuple[str, str], ReleasedLease] = {}
    for ended in ended_leases(project, issue, gh):
        lease = ended.lease
        if not ended.cleaned and lease.owns_assignee and lease.assignee:
            pending[(lease.session, login_key(lease.assignee))] = ended  # latest end of the pair
    for ended in pending.values():
        lease = ended.lease
        target = json.loads(gh(["api", f"repos/{project}/issues/{issue}"]) or "{}")
        if login_key(lease.assignee) not in _assignee_logins(
            target if isinstance(target, dict) else {}
        ):
            _record_cleanup(project, issue, lease, gh)
            continue
        if ended.released_at is None or assigned_since(
            project, issue, lease.assignee, ended.released_at, gh
        ):
            continue  # assigned again after the lease ended (or unknowable): a later owner's
        if _drop_owned_assignee(project, issue, lease, gh, now):  # re-added while it was active?
            _record_cleanup(project, issue, lease, gh)


def _reconcile_best_effort(project: str, issue: int, gh: GhRunner, now: datetime | None) -> None:
    """`_reconcile_released_assignees` where its failure must not fail the caller (a claim or a
    release whose own work is done): logged, and retried by the next claim or release."""
    try:
        _reconcile_released_assignees(project, issue, gh, now)
    except Exception as exc:  # noqa: BLE001 -- retryable bookkeeping, never the operation
        _log.warning("issue #%s: assignee cleanup of ended leases deferred: %s", issue, exc)


#: How far GitHub's event clock may lag the local one when an assignment is tied to the add.
ASSIGN_CLOCK_SKEW = timedelta(minutes=2)


def _assignment_snapshot(
    project: str, issue: int, login: str, gh: GhRunner
) -> tuple[bool, list[dict]] | None:
    """Immediately before a claim adds `login`: is it on the issue already (a FRESH read, not
    the claim's first one), and the issue's events so far. None when either cannot be read."""
    try:
        target = json.loads(gh(["api", f"repos/{project}/issues/{issue}"]) or "{}")
        events = _issue_events(project, issue, gh)
    except Exception:  # noqa: BLE001 -- unknown: the lease will not own the assignee
        return None
    assigned = login_key(login) in _assignee_logins(target if isinstance(target, dict) else {})
    return assigned, events


def _own_actor(project: str, issue: int, session: str, gh: GhRunner) -> str:
    """The account this process acts as: the author of this session's newest claim marker (the
    same token posted it and makes the add; `/user` is unreadable for App tokens)."""
    for comment in reversed(_comments(project, issue, gh)):
        parsed = parse_marker(comment.get("body") or "", issue)
        if parsed is not None and parsed.session == session:
            return str((comment.get("user") or {}).get("login") or "")
    return ""


def _confirm_own_assignment(
    project: str,
    issue: int,
    session: str,
    login: str,
    snapshot: tuple[bool, list[dict]] | None,
    since: datetime,
    gh: GhRunner,
) -> bool:
    """Did THIS claim's add put `login` on the issue? Only when the fresh read right before the
    add did not have it AND the events now hold an `assigned` event for it that the pre-add
    snapshot lacked, made by our own actor, at or after the claim began (less clock skew).
    Anything unreadable or ambiguous is False: the lease then never removes the login."""
    if snapshot is None or snapshot[0]:
        return False
    try:
        actor = _own_actor(project, issue, session, gh)
        after = _issue_events(project, issue, gh)
    except Exception:  # noqa: BLE001 -- unknown: not ours
        return False
    if not actor:
        return False
    seen: dict[str, int] = {}
    for event in snapshot[1]:
        seen[_event_key(event)] = seen.get(_event_key(event), 0) + 1
    for event in after:
        key = _event_key(event)
        if seen.get(key):
            seen[key] -= 1
            continue
        if event.get("event") != "assigned":
            continue
        if not same_login((event.get("assignee") or {}).get("login"), login):
            continue
        at = _parse_iso(str(event.get("created_at") or ""))
        by = (event.get("actor") or {}).get("login")
        if at is not None and at >= since - ASSIGN_CLOCK_SKEW and same_login(by, actor):
            return True
    return False


def _assignee_logins(target: dict) -> set[str]:
    """The issue's assignees, as `login_key`s."""
    return {
        login_key(a.get("login"))
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
        lease = replace(
            lease,
            assignee=existing.assignee,
            owns_assignee=existing.owns_assignee,
            disowned=existing.disowned,
        )
        winner = _post_and_arbitrate(
            project,
            issue,
            session,
            _marker_comment(
                format_claim_marker(lease), f"Lease renewed until {_iso(lease.expires)}."
            ),
            gh,
            now,
        )
        if winner is None or winner.session != session:
            return ClaimResult(
                False,
                winner,
                reason=f"issue #{issue}: renewal lost to session={getattr(winner, 'session', '?')}",
            )
        _reconcile_best_effort(project, issue, gh, now)
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
    inherited = False
    if assignee:
        # The lease owns an assignment the expired lease it takes over owned (that lease will
        # never release it now). One it makes itself is owned only once PROVEN around the add
        # (`_confirm_own_assignment`): the issue read above is stale by then, and a maintainer
        # who assigns the login in between makes the add a no-op (Codex 4206173677). Until
        # proven, the marker claims no ownership -- a crash leaves an assignee, never removes one.
        # The expired lease's ownership is inherited only when it was still its own: a
        # maintainer who removed and re-added the login while that lease was active made the
        # assignment theirs (`reassigned_during`; unknowable history inherits nothing either),
        # and inheriting it anyway would let this lease's release remove it (Codex 4206780262).
        inherited = (
            existing is not None
            and existing.owns_assignee
            and same_login(existing.assignee, assignee)
            and reassigned_during(project, issue, existing.assignee, existing.claimed_at, gh)
            is False
        )
        lease = replace(lease, owns_assignee=inherited)
    marker = format_claim_marker(lease)
    winner = _post_and_arbitrate(project, issue, session, _marker_comment(marker, note), gh, now)
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
    snapshot: tuple[bool, list[dict]] | None = None
    try:
        _ensure_label(project, gh)
        if assignee and not inherited:
            snapshot = _assignment_snapshot(project, issue, assignee, gh)
        _edit(project, issue, gh, *flags)
    except Exception:
        if assignee and not inherited:
            lease = replace(
                lease,
                owns_assignee=_confirm_own_assignment(
                    project, issue, session, assignee, snapshot, now, gh
                ),
            )
        # The marker already made the lease authoritative, but the caller is about to fail and
        # nothing downstream will release it: retract so the issue is not blocked for the TTL.
        try:
            _comment(
                project,
                issue,
                _marker_comment(
                    format_release_marker(session),
                    "Label/assignee bookkeeping failed; retracting the claim.",
                ),
                gh,
            )
            _sync_label(project, issue, gh, now)
            # This claim's own assignment, seconds old: nobody can have re-added it yet.
            if _drop_owned_assignee(project, issue, lease, gh, now, check_history=False):
                _record_cleanup(project, issue, lease, gh)
        except Exception:  # noqa: BLE001 -- best effort; the original failure is what matters
            pass
        raise
    if (
        assignee
        and not inherited
        and _confirm_own_assignment(project, issue, session, assignee, snapshot, now, gh)
    ):
        owned = replace(lease, owns_assignee=True)
        try:
            _comment(
                project,
                issue,
                _marker_comment(
                    format_claim_marker(owned),
                    f"Assigned `{assignee}` for this lease; it is removed on release.",
                ),
                gh,
            )
            lease = owned
        except Exception as exc:  # noqa: BLE001 -- unrecorded ownership only leaves the login
            _log.warning("issue #%s: assignee ownership not recorded: %s", issue, exc)
    # A takeover ends the expired lease: an assignee it added (and this lease does not want)
    # would otherwise stay on the issue forever, accumulating one per takeover. The new claim is
    # already authoritative, so this cleanup is best effort: a failure is logged and left to the
    # reconciliation of ended leases, which every later claim and release retries.
    if existing is not None and not same_login(existing.assignee, lease.assignee):
        try:
            if _drop_owned_assignee(project, issue, existing, gh, now):
                _record_cleanup(project, issue, existing, gh)
        except Exception as exc:  # noqa: BLE001 -- retryable; never fails the claim
            _log.warning(
                "issue #%s: assignee cleanup of the expired lease of session %s deferred: %s",
                issue,
                existing.session,
                exc,
            )
    _reconcile_best_effort(project, issue, gh, now)
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
    body = _marker_comment(
        format_claim_marker(lease), f"Lease renewed until {_iso(lease.expires)}."
    )
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
    now = now or datetime.now(UTC)
    existing = current_lease(project, issue, gh, now)
    if (
        existing is not None
        and existing.session != session
        and not existing.live(now)
        and not force
    ):
        # Another session's EXPIRED lease is never a live conflicting holder: release this
        # session's own unreleased lease if it has one, else just finish the cleanup (a retry
        # after this session's release posted its marker and its assignee cleanup failed).
        existing = next((x for x in _open_leases(project, issue, gh) if x.session == session), None)
    if existing is None:
        # Nothing to release -- possibly because an earlier release posted its marker and then
        # failed: finish that release's cleanup (idempotent, so a retry completes it).
        _sync_label(project, issue, gh, now)
        _reconcile_released_assignees(project, issue, gh, now)
        return
    if existing.session != session and not force:
        queued = next(
            (x for x in _open_leases(project, issue, gh) if x.session == session),
            None,
        )
        if queued is None:
            raise LeaseError(
                f"issue #{issue} is leased by session {existing.session}, not {session}"
            )
        # This session's own claim is queued behind the live holder -- a race it lost whose
        # retraction failed (Codex 4206780290). Left open it would become authoritative when
        # the holder releases. Retract it; the label and the assignee are the holder's.
        _comment(
            project,
            issue,
            _marker_comment(
                format_release_marker(session),
                f"Queued claim retracted by `{session}`; the lease stays with session "
                f"`{existing.session}`. {note}",
            ),
            gh,
        )
        return
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
        _marker_comment(
            format_release_marker(existing.session), f"Lease released by `{who}`. {note}"
        ),
        gh,
    )
    # A new claimant may have taken the lease (and added the label) since we read it.
    _sync_label(project, issue, gh, now)
    # The assignee this lease added goes too, reconciled the same way; completion is recorded
    # so a retried release cannot remove a login a maintainer assigns again afterwards.
    if _drop_owned_assignee(project, issue, existing, gh, now):
        _record_cleanup(project, issue, existing, gh)
    # Earlier ended leases whose cleanup failed (a release's, a takeover's) are finished too.
    _reconcile_best_effort(project, issue, gh, now)


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
