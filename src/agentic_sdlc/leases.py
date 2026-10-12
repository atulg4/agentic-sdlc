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
Publisher App's bot, named exactly (FORGE_LEASE_BOT_LOGINS or the policy's
``[automation] publisher_bot_login``; with neither, no other App bot).
``author_association`` is not authority (a read-only organization member comments as MEMBER).
The commit-guard hook mirrors the same rule.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
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
#: The only GitHub App bots whose markers count: Actions' own token, and the Forge Publisher
#: App's bot, named EXACTLY (Codex 4242753917): FORGE_LEASE_BOT_LOGINS (comma-separated) when
#: set, else the policy's `[automation] publisher_bot_login` (`use_policy_bot_logins`). With
#: neither, no other App bot is trusted -- never one whose slug merely looks like Forge's.
GITHUB_ACTIONS_BOT = "github-actions[bot]"
LEASE_BOT_LOGINS_ENV = "FORGE_LEASE_BOT_LOGINS"
#: A GitHub App's bot login: `<app-slug>[bot]` (App slugs are lowercase).
PUBLISHER_BOT_LOGIN = re.compile(r"^[a-z0-9][a-z0-9-]*\[bot\]$")
_POLICY_BOT_LOGINS: list[str] = [""]  # the policy's publisher_bot_login for this process
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
    """Every item of a paginated list endpoint (--slurp wraps the pages in one outer array).
    An answer that is not that shape raises: an unreadable list (comments, events, claims) is
    never read as an empty one -- no lease, no history -- which would let a claim proceed."""
    data = json.loads(gh(["api", path, "--paginate", "--slurp"]) or "null")
    if not isinstance(data, list):
        raise LeaseError(f"unreadable answer from {path}: not a list of pages")
    if not all(isinstance(page, list) for page in data):
        raise LeaseError(f"unreadable answer from {path}: a page is not a list")
    items = [item for page in data for item in page]
    if not all(isinstance(item, dict) for item in items):
        raise LeaseError(f"unreadable answer from {path}: an item is not an object")
    return items


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


def publisher_bot_login_problem(value: object) -> str:
    """Why `value` is not a Publisher App bot login (`<app-slug>[bot]`; '' = it is)."""
    if not isinstance(value, str) or not PUBLISHER_BOT_LOGIN.fullmatch(value):
        return f"must be the Publisher App's bot login, `<app-slug>[bot]` (got {value!r})"
    return ""


def use_policy_bot_logins(login: str) -> None:
    """Record the policy's `[automation] publisher_bot_login` for this process: the Publisher
    bot `trusted_bot` names when FORGE_LEASE_BOT_LOGINS is unset ('' = none)."""
    _POLICY_BOT_LOGINS[0] = login or ""


def configured_bot_logins() -> str:
    """The exact Publisher bot logins in force: FORGE_LEASE_BOT_LOGINS, else the policy's."""
    return os.environ.get(LEASE_BOT_LOGINS_ENV, "") or _POLICY_BOT_LOGINS[0]


def trusted_bot(login: str, configured: str | None = None) -> bool:
    """`github-actions[bot]`, or a Publisher App bot named exactly by `configured` (default:
    `configured_bot_logins()`). Nothing configured: no other bot (fail closed, Codex
    4242753917) -- an App bot's markers are then ignored, never trusted on a name hint."""
    login = login_key(login)
    if login == login_key(GITHUB_ACTIONS_BOT):
        return True
    if configured is None:
        configured = configured_bot_logins()
    named = {login_key(part) for part in configured.split(",") if part.strip()}
    return login.endswith("[bot]") and login in named


def trusted_marker_author(
    comment: dict, permission: Callable[[str], str | None], configured: str | None = None
) -> bool:
    """May this comment's author post lease markers? A bot by `trusted_bot`; a user only with
    write/maintain/admin on the repository (`permission(login)` -> the permission API's
    `permission`/`role_name`, None when the account has none; a permission that cannot be read
    raises LeaseError out of `permission` and refuses the whole read). Never by
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


def _answered_404(exc: BaseException) -> bool:
    """True only when GitHub ANSWERED 404 -- the status `run_gh` parsed from the response
    (`gh_http_status`), never a "404" anywhere in the message (Codex 4244027557): a timeout or
    5xx on a path that echoes `404` must not read as "no access"."""
    from .onboard import gh_http_status  # onboard imports this module

    return gh_http_status(exc) == 404


class PermissionCache:
    """Repository permission per login, read once per run (`GET .../collaborators/{u}/permission`).
    Keyed by `login_key`: `Alice` and `alice` are one read.

    Only a definitive answer classifies an author: a 200 carrying the permission, or a 404 (the
    account has no access: None, untrusted). Any other failure -- network, auth, 5xx, an answer
    that is not the documented shape -- raises LeaseError, every time it is asked within the
    run (Codex 4239397972): dropping that author's markers would read an existing lease as
    absent and let a rival claim the issue. The commit-guard hook refuses the same way."""

    def __init__(self, project: str, gh: GhRunner):
        self.project = project
        self.gh = gh
        self._known: dict[str, str | None | LeaseError] = {}

    def __call__(self, login: str) -> str | None:
        key = login_key(login)
        if key not in self._known:
            try:
                self._known[key] = self._read(login)
            except LeaseError as exc:
                self._known[key] = exc
        known = self._known[key]
        if isinstance(known, LeaseError):
            raise LeaseError(str(known))
        return known

    def _read(self, login: str) -> str | None:
        path = f"repos/{self.project}/collaborators/{login}/permission"
        try:
            raw = self.gh(["api", path])
        except Exception as exc:  # noqa: BLE001 -- only a 404 is an answer
            if _answered_404(exc):
                return None  # GitHub: the account has no access to the repository
            raise LeaseError(
                f"cannot read the repository permission of marker author {login!r} "
                f"({str(exc).strip()[:200] or type(exc).__name__}); lease state unknown, "
                "refusing the lease operation -- retry once GitHub answers"
            ) from exc
        try:
            data = json.loads(raw or "null")
        except ValueError as exc:
            raise LeaseError(f"unreadable answer from {path}: not JSON") from exc
        if not isinstance(data, dict) or not (data.get("permission") or data.get("role_name")):
            raise LeaseError(f"unreadable answer from {path}: no permission in it")
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
    """The issue's marker comments whose authors may post lease markers, in order.

    Only a comment carrying a syntactically valid claim/release/cleanup marker (`_marker`, the
    commit guard's `comment_marker`) can change lease state, so only its author's permission is
    resolved (Codex 4239780833): ordinary discussion costs no permission read, and a failed read
    for a chatter's account cannot refuse the lease operation. A marker author whose permission
    cannot be read still refuses it (`PermissionCache`)."""
    permission = _trust(project, gh)
    return [
        c
        for c in _comments(project, issue, gh)
        if _marker(str(c.get("body") or "")) is not None and trusted_marker_author(c, permission)
    ]


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


#: Every open pull request of the repository, one cursor page at a time (Codex 4239965191:
#: `gh pr list --limit N` TRUNCATES, so an issue's PR beyond the first N went unseen and a claim
#: started duplicate work). The fields are those `gh pr list --json` returned -- GitHub's GraphQL
#: names -- so every signal below reads them unchanged; `closingIssuesReferences` carries the
#: issues each PR will close (Codex 4239780827). A manual Development-sidebar link that does not
#: close the issue is read from the issue's timeline instead (`_timeline_prs`, Codex 4240028945).
_OPEN_PRS_QUERY = """\
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number url title body headRefName isCrossRepository
        headRepository { name }
        headRepositoryOwner { login }
        closingIssuesReferences(first: 100) {
          pageInfo { hasNextPage }
          nodes { number repository { name owner { login } } }
        }
      }
    }
  }
}"""
#: A runaway cursor (an answer that never ends) stops here and refuses, never "no PR".
_MAX_PR_PAGES = 1000


def _open_pr_page(project: str, after: str | None, gh: GhRunner) -> tuple[list, str | None]:
    """One page of open PRs and the cursor of the next (None: this was the last). Anything but
    a well-formed page raises LeaseError: an unreadable inventory is never "no open PR"."""
    owner, _, name = project.partition("/")
    args = ["api", "graphql", "-f", f"query={_OPEN_PRS_QUERY}"]
    args += ["-f", f"owner={owner}", "-f", f"name={name}"]
    if after is not None:
        args += ["-f", f"after={after}"]
    what = "unreadable answer listing the open pull requests"
    try:
        answer = json.loads(gh(args) or "null")
    except ValueError as exc:
        raise LeaseError(f"{what}: not JSON") from exc
    if not isinstance(answer, dict) or answer.get("errors"):
        raise LeaseError(
            f"{what}: {str(answer.get('errors') if isinstance(answer, dict) else answer)[:200]}"
        )
    data = answer.get("data")
    repo = data.get("repository") if isinstance(data, dict) else None
    prs = repo.get("pullRequests") if isinstance(repo, dict) else None
    info = prs.get("pageInfo") if isinstance(prs, dict) else None
    nodes = prs.get("nodes") if isinstance(prs, dict) else None
    if not isinstance(info, dict) or not isinstance(nodes, list):
        raise LeaseError(f"{what}: no page of pull requests in it")
    if not all(isinstance(pr, dict) for pr in nodes):
        raise LeaseError(f"{what}: not a list of pull requests")
    more = info.get("hasNextPage")
    if more is False:
        return nodes, None
    cursor = info.get("endCursor")
    if more is not True or not isinstance(cursor, str) or not cursor or cursor == after:
        raise LeaseError(f"{what}: the page does not say where the next one starts")
    return nodes, cursor


def _flatten_links(pr: dict, project: str) -> dict:
    """`pr` with `closingIssuesReferences` as the plain list `_links_issue` reads. A PR with
    more linked issues than one page holds raises: the unread ones could be this issue."""
    refs = pr.get("closingIssuesReferences")
    if isinstance(refs, dict):
        info = refs.get("pageInfo")
        if not isinstance(info, dict) or info.get("hasNextPage") is not False:
            raise LeaseError(
                f"{project} pull request #{pr.get('number')}: its linked issues could not all "
                "be read; not concluding that no open PR covers the issue"
            )
        pr = {**pr, "closingIssuesReferences": refs.get("nodes")}
    return pr


def open_pull_requests(project: str, gh: GhRunner) -> list[dict]:
    """EVERY open pull request of `project` (cursor-paginated to the end; see above)."""
    prs: list[dict] = []
    after: str | None = None
    for _ in range(_MAX_PR_PAGES):
        nodes, after = _open_pr_page(project, after, gh)
        prs.extend(_flatten_links(pr, project) for pr in nodes)
        if after is None:
            return prs
    raise LeaseError(f"listing the open pull requests of {project} did not end; not concluding")


def open_prs_for_issue(project: str, issue: int, gh: GhRunner) -> list[dict]:
    prs = open_pull_requests(project, gh)
    hits = []
    for pr in prs:
        text = f"{pr.get('title', '')}\n{pr.get('body', '')}"
        refs = set()
        for m in _PR_REF.finditer(text):
            repo = m.group("repo") or m.group("url_repo")
            if repo and repo.lower() != project.lower():
                continue  # a closing reference into another repository
            refs.add(int(m.group("num")))
        # Codex 4239667540: the branch-name heuristic only for a PR whose head lives in THIS
        # repository -- a fork can open a PR from `forge/issue-7` and must not block every
        # claimant of issue 7. An explicit closing reference counts from any PR, as before.
        head = pr.get("headRefName")
        by_branch = (
            isinstance(head, str)
            and _same_repository_pr(pr, project)
            and re.search(rf"(^|[/-])issue-{issue}($|[^0-9])", head) is not None
        )
        if issue in refs or by_branch or _links_issue(pr, project, issue):
            hits.append(pr)
    # Codex 4240028945: `closingIssuesReferences` holds what the PR will CLOSE; a PR linked by
    # hand in the Development sidebar does not close the issue and is not in it. The issue's
    # timeline is: `connected` (minus a later `disconnected`) and same-repository open PRs that
    # cross-reference the issue are durable work too, and so is an open PR of another repository
    # that cross-references the issue AND will close it (Codex 4240306505).
    by_number = {pr.get("number"): pr for pr in prs}
    seen = {pr.get("number") for pr in hits}
    for number, row in _timeline_prs(project, issue, gh):
        if number in seen:
            continue
        seen.add(number)
        hits.append(by_number.get(number) or row)
    return hits


#: Pages of an issue timeline read before refusing (a runaway answer is never "no PR").
_MAX_TIMELINE_PAGES = 1000
_TIMELINE_PAGE = 100

#: The PR a `connected`/`disconnected` timeline event links, by the event's global node id:
#: the REST timeline names no PR for these events, so they are resolved through GraphQL.
_LINK_EVENTS_QUERY = """\
query($ids: [ID!]!) {
  nodes(ids: $ids) {
    __typename
    ... on ConnectedEvent { subject { ...linked } }
    ... on DisconnectedEvent { subject { ...linked } }
  }
}
fragment linked on ReferencedSubject {
  __typename
  ... on PullRequest { number url title state repository { nameWithOwner } }
  ... on Issue { number }
}"""


def _issue_timeline(project: str, issue: int, gh: GhRunner) -> list[dict]:
    """Every event of the issue's timeline, page by page until a short page. Anything but a
    list of objects raises LeaseError: an unreadable timeline is never "no linked PR"."""
    events: list[dict] = []
    for page in range(1, _MAX_TIMELINE_PAGES + 1):
        path = f"repos/{project}/issues/{issue}/timeline?per_page={_TIMELINE_PAGE}&page={page}"
        try:
            items = json.loads(gh(["api", path]) or "null")
        except ValueError as exc:
            raise LeaseError(f"unreadable answer from {path}: not JSON") from exc
        if not isinstance(items, list):
            raise LeaseError(f"unreadable answer from {path}: not a list of events")
        if not all(isinstance(item, dict) for item in items):
            raise LeaseError(f"unreadable answer from {path}: an event is not an object")
        events.extend(items)
        if len(items) < _TIMELINE_PAGE:
            return events
    raise LeaseError(f"the timeline of {project} issue #{issue} did not end; not concluding")


def _repo_of(issue_row: dict) -> str | None:
    """`owner/name` of a timeline `source.issue`: its `repository.full_name`, else the API URL."""
    repo = issue_row.get("repository")
    if isinstance(repo, dict) and isinstance(repo.get("full_name"), str):
        return repo["full_name"]
    url = issue_row.get("repository_url")
    if isinstance(url, str):
        found = re.search(r"/repos/([^/]+/[^/]+)$", url)
        if found:
            return found.group(1)
    return None


def _number(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _link_subjects(project: str, issue: int, ids: list[str], gh: GhRunner) -> dict[str, dict]:
    """The `subject` of each connected/disconnected event (by node id). Any id whose subject
    cannot be read raises LeaseError: an unidentified manual link could be this issue's PR."""
    what = f"{project} issue #{issue}: a Development-sidebar link could not be identified"
    subjects: dict[str, dict] = {}
    for start in range(0, len(ids), 100):
        chunk = ids[start : start + 100]
        args = ["api", "graphql", "-f", f"query={_LINK_EVENTS_QUERY}"]
        for node_id in chunk:
            args += ["-f", f"ids[]={node_id}"]
        try:
            answer = json.loads(gh(args) or "null")
        except ValueError as exc:
            raise LeaseError(f"{what}: not JSON") from exc
        if not isinstance(answer, dict) or answer.get("errors"):
            raise LeaseError(f"{what}: {str(answer)[:200]}")
        data = answer.get("data")
        nodes = data.get("nodes") if isinstance(data, dict) else None
        if not isinstance(nodes, list) or len(nodes) != len(chunk):
            raise LeaseError(f"{what}: no answer for every link event")
        for node_id, node in zip(chunk, nodes, strict=True):
            subject = node.get("subject") if isinstance(node, dict) else None
            if not isinstance(subject, dict) or not isinstance(subject.get("__typename"), str):
                raise LeaseError(f"{what}: event {node_id} names no subject")
            subjects[node_id] = subject
    return subjects


#: An open pull request of ANOTHER repository that cross-references the issue: whether it will
#: close it (`Fixes owner/repo#N`, which GitHub honours across repositories) is read from its own
#: `closingIssuesReferences` (Codex 4240306505).
_EXTERNAL_PR_QUERY = """\
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      state url title
      closingIssuesReferences(first: 100) {
        pageInfo { hasNextPage }
        nodes { number repository { nameWithOwner } }
      }
    }
  }
}"""


def _external_pr_closes(
    project: str, issue: int, source: str, number: int, gh: GhRunner
) -> dict | None:
    """The open pull request `source`#`number` (another repository) as a row when it will close
    `project` issue `issue`, else None (closed since, or it only mentions the issue). A PR that
    cannot be read -- an inaccessible or deleted repository, an API failure, a malformed answer
    or more linked issues than one page holds -- raises LeaseError naming it: it could be the
    work this claim would duplicate (fail closed)."""
    pr_name = f"{source}#{number}"
    blocked = (
        f"{project} issue #{issue} is cross-referenced by the open pull request {pr_name} in "
        "another repository, which may close it, and "
    )
    remedy = (
        f"; not claiming. Check {pr_name} by hand: if it closes the issue the work is taken; if "
        "it does not (or is gone), make it readable to this `gh` login (or wait for the outage) "
        "and retry -- `claim` has no override for an unverifiable closing pull request"
    )
    owner, _, name = source.partition("/")
    args = ["api", "graphql", "-f", f"query={_EXTERNAL_PR_QUERY}"]
    args += ["-f", f"owner={owner}", "-f", f"name={name}", "-F", f"number={number}"]
    try:
        answer = json.loads(gh(args) or "null")
    except Exception as exc:  # noqa: BLE001 - any failure to read it is "unverifiable"
        raise LeaseError(f"{blocked}it could not be read ({str(exc)[:160]}){remedy}") from exc
    data = answer.get("data") if isinstance(answer, dict) else None
    repo = data.get("repository") if isinstance(data, dict) else None
    pr = repo.get("pullRequest") if isinstance(repo, dict) else None
    if not isinstance(answer, dict) or answer.get("errors") or not isinstance(pr, dict):
        why = answer.get("errors") if isinstance(answer, dict) else answer
        raise LeaseError(f"{blocked}it could not be read ({str(why)[:160]}){remedy}")
    refs = pr.get("closingIssuesReferences")
    info = refs.get("pageInfo") if isinstance(refs, dict) else None
    nodes = refs.get("nodes") if isinstance(refs, dict) else None
    state = pr.get("state")
    if not isinstance(state, str) or not isinstance(info, dict) or not isinstance(nodes, list):
        raise LeaseError(f"{blocked}its state or closing issues could not be read{remedy}")
    if state != "OPEN":
        return None  # closed or merged since the timeline was read: no work in progress
    if info.get("hasNextPage") is not False:
        raise LeaseError(f"{blocked}its closing issues could not all be read{remedy}")
    for node in nodes:
        if not isinstance(node, dict):
            raise LeaseError(f"{blocked}one of its closing issues could not be read{remedy}")
        target = node.get("repository")
        target_name = target.get("nameWithOwner") if isinstance(target, dict) else None
        if _number(node.get("number")) is None or not isinstance(target_name, str):
            raise LeaseError(f"{blocked}one of its closing issues could not be read{remedy}")
        if node["number"] == issue and target_name.lower() == project.lower():
            url = pr.get("url") if isinstance(pr.get("url"), str) else ""
            title = pr.get("title") if isinstance(pr.get("title"), str) else ""
            return {"number": number, "url": url, "title": title, "repository": source}
    return None


def _timeline_prs(project: str, issue: int, gh: GhRunner) -> list[tuple[int | str, dict]]:
    """(key, row) of each OPEN pull request the issue's timeline links as work on it: a PR of
    `project` connected through the Development sidebar and not disconnected since, or one that
    cross-references the issue, and an open PR of ANOTHER repository that cross-references the
    issue and will close it (Codex 4240306505). The key is the number for a PR of `project`,
    `owner/name#N` for another repository's. A malformed event, or a link whose PR cannot be
    identified or read, raises LeaseError (fail closed: it could be the work this claim would
    duplicate)."""
    events = _issue_timeline(project, issue, gh)
    links = [e for e in events if e.get("event") in ("connected", "disconnected")]
    unresolved = []
    for event in links:
        if not isinstance(event.get("node_id"), str) or not event["node_id"]:
            raise LeaseError(
                f"{project} issue #{issue}: a {event.get('event')} event has no id; "
                "not concluding that no linked PR covers the issue"
            )
        unresolved.append(event["node_id"])
    subjects = _link_subjects(project, issue, unresolved, gh) if unresolved else {}
    connected: dict[int, dict] = {}
    for event in links:
        subject = subjects[event["node_id"]]
        if subject["__typename"] != "PullRequest":
            continue  # an issue-to-issue link is not work in progress
        number = _number(subject.get("number"))
        repo = subject.get("repository")
        name = repo.get("nameWithOwner") if isinstance(repo, dict) else None
        if number is None or not isinstance(name, str) or not isinstance(subject.get("state"), str):
            raise LeaseError(
                f"{project} issue #{issue}: a linked pull request could not be identified"
            )
        if name.lower() != project.lower():
            continue
        if event["event"] == "connected":
            connected[number] = subject
        else:
            connected.pop(number, None)
    found: list[tuple[int | str, dict]] = [
        (number, {"number": number, "url": row.get("url", ""), "title": row.get("title", "")})
        for number, row in connected.items()
        if row["state"] == "OPEN"
    ]
    external: set[str] = set()
    for event in events:
        if event.get("event") != "cross-referenced":
            continue
        source = event.get("source")
        row = source.get("issue") if isinstance(source, dict) else None
        if not isinstance(row, dict):
            raise LeaseError(f"{project} issue #{issue}: a cross-reference names no source")
        if "pull_request" not in row:
            continue  # referenced from an issue, not a pull request
        number, repo, state = _number(row.get("number")), _repo_of(row), row.get("state")
        if number is None or repo is None or not isinstance(state, str):
            raise LeaseError(
                f"{project} issue #{issue}: a cross-referencing pull request could not be read"
            )
        if state != "open":
            continue
        if repo.lower() == project.lower():
            url = row.get("html_url") or ""
            found.append((number, {"number": number, "url": url, "title": row.get("title", "")}))
            continue
        key = f"{repo.lower()}#{number}"
        if key in external:
            continue  # one PR can cross-reference the issue many times; read it once
        external.add(key)
        closing = _external_pr_closes(project, issue, repo, number, gh)
        if closing is not None:
            found.append((key, closing))
    return found


def _links_issue(pr: dict, project: str, issue: int) -> bool:
    """Does the PR's `closingIssuesReferences` -- the issues it will close, by keyword or by a
    closing link (Codex 4239780827); manual links come from the timeline -- name issue `issue` of
    `project` itself? Owner/name compare case-insensitively; a malformed field or entry proves
    nothing (the textual and branch signals still decide)."""
    refs = pr.get("closingIssuesReferences")
    if not isinstance(refs, list):
        return False
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        number = ref.get("number")
        if isinstance(number, bool) or not isinstance(number, int) or number != issue:
            continue
        repo = ref.get("repository")
        if not isinstance(repo, dict):
            continue
        owner = repo.get("owner")
        name = repo.get("name")
        login = owner.get("login") if isinstance(owner, dict) else None
        if not isinstance(name, str) or not isinstance(login, str) or not name or not login:
            continue
        if f"{login}/{name}".lower() == project.lower():
            return True
    return False


def _same_repository_pr(pr: object, project: str) -> bool:
    """True only for a PR whose head branch lives in `project` itself (the commit guard's
    `_same_repository_pr` applies the same rule to the REST shape): `isCrossRepository` exactly
    false and the head repository's
    owner/name equal to `project`, case-insensitively. Missing or malformed fields prove nothing."""
    if not isinstance(pr, dict) or pr.get("isCrossRepository") is not False:
        return False
    repo, owner = pr.get("headRepository"), pr.get("headRepositoryOwner")
    if not isinstance(repo, dict) or not isinstance(owner, dict):
        return False
    name, login = repo.get("name"), owner.get("login")
    if not isinstance(name, str) or not isinstance(login, str) or not name or not login:
        return False
    return f"{login}/{name}".lower() == project.lower()


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


Clock = Callable[[], datetime]


def _clock(now: datetime | None, clock: Clock | None) -> Clock:
    """The clock an operation reads: `clock` when given, else a fixed `now` (tests), else the
    wall clock. Codex 4241196647: a lease's expiry and its arbitration read the clock when they
    happen, not once at the start of an operation whose reads may take a while."""
    if clock is not None:
        return clock
    if now is not None:
        return lambda: now
    return lambda: datetime.now(UTC)


#: How many times a claim tries to post the retraction of its own marker, and the pause before
#: each retry (Codex 4241327872). A claim that fails after posting must not leave a live lease
#: behind: the caller is about to fail and nothing downstream releases it, so the issue would
#: stay blocked for the whole TTL. Bounded: a GitHub that refuses every write is reported, not
#: retried forever.
RETRACT_ATTEMPTS = 3
RETRACT_PAUSES = (1.0, 2.0)
_sleep = time.sleep  # replaced in tests


def _retract(project: str, issue: int, session: str, why: str, gh: GhRunner) -> None:
    """Post `session`'s release marker: the claim it just posted is withdrawn. Tried up to
    RETRACT_ATTEMPTS times; the last failure is raised."""
    body = _marker_comment(format_release_marker(session), why)
    for attempt in range(1, RETRACT_ATTEMPTS + 1):
        try:
            _comment(project, issue, body, gh)
            return
        except Exception as exc:
            if attempt == RETRACT_ATTEMPTS:
                raise
            _log.warning(
                "issue #%s: retraction of session %s failed (attempt %s/%s): %s",
                issue,
                session,
                attempt,
                RETRACT_ATTEMPTS,
                exc,
            )
            _sleep(RETRACT_PAUSES[min(attempt, len(RETRACT_PAUSES)) - 1])


def _retract_on_failure(project: str, issue: int, session: str, why: str, gh: GhRunner) -> bool:
    """`_retract` on the way out of a failing claim: the claim's own failure is what the caller
    must see, so a retraction that fails every attempt is logged, never raised. True when the
    retraction was posted."""
    try:
        _retract(project, issue, session, why, gh)
    except Exception as exc:  # noqa: BLE001 -- the original failure is what matters
        _log.error(
            "issue #%s: could not retract the claim of session %s after %s attempts; it stays "
            "live until it expires or is released: %s",
            issue,
            session,
            RETRACT_ATTEMPTS,
            exc,
        )
        return False
    return True


def _post_and_arbitrate(
    project: str,
    issue: int,
    session: str,
    body: str,
    gh: GhRunner,
    clock: Clock,
    *,
    retract_on_error: bool = False,
) -> Lease | None:
    """Post a claim/renewal marker, then re-read the thread and keep it only if it is authoritative.

    GitHub has no compare-and-set, so every lease acquisition -- fresh claim, takeover or
    renewal -- appends its marker and re-reads; only the session `current_lease()` names keeps
    it, everyone else retracts. The re-read is judged at a FRESH reading of `clock` (Codex
    4241196647): a marker already expired by then is not a lease, and is retracted. Returns the
    winner (``None`` when nothing is live).

    With `retract_on_error` (a FRESH claim: no lease of this session predates it), a failure
    after the post -- the post's own answer lost, or the arbitration re-read failing -- retracts
    the marker before it is raised (Codex 4241327872): the caller fails, and a marker left behind
    would be a live lease nobody releases. A renewal does not: its session already held the
    lease, and retracting would end that lease too.
    """
    try:
        _comment(project, issue, body, gh)
        at = clock()
        winner = current_lease(project, issue, gh, at)
    except Exception:
        if retract_on_error:
            _retract_on_failure(
                project,
                issue,
                session,
                "The claim could not be confirmed after it was posted; retracting.",
                gh,
            )
        raise
    expired_own = winner is not None and winner.session == session and not winner.live(at)
    if winner is not None and not winner.live(at):
        winner = None
    if winner is None or winner.session != session:
        why = (
            f"The claim had already expired when it was re-read ({_iso(at)}); retracting."
            if expired_own
            else f"Lost the claim race to session `{getattr(winner, 'session', '?')}`; retracting."
        )
        _retract(project, issue, session, why, gh)
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
    clock: Clock | None = None,
) -> ClaimResult:
    """Take the lease if free (or expired, or already ours). Never mutates on refusal.

    The lease's expiry is computed from the clock immediately before its marker is posted, and
    arbitration reads the clock again (Codex 4241196647); after winning, the issue and its
    open-PR inventory are read again and the claim retracted if a PR appeared or the issue was
    closed meanwhile (Codex 4241196645, 4241532620)."""
    clock = _clock(now, clock)
    now = clock()
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
        # A claim that renews this session's live lease is a renewal and checks what `renew`
        # checks (Codex 4243550483): the issue is open (read above) and has no open pull
        # request -- the same inventory, refused the same way, an unreadable one included --
        # before anything is posted.
        try:
            _require_no_open_pr(project, issue, gh)
        except LeaseError as exc:
            return ClaimResult(False, existing, reason=str(exc))
        # A renewal keeps the assignment bookkeeping of the lease it extends.
        lease = replace(
            lease,
            assignee=existing.assignee,
            owns_assignee=existing.owns_assignee,
            disowned=existing.disowned,
            expires=clock() + timedelta(minutes=ttl_minutes),
        )
        winner = _post_and_arbitrate(
            project,
            issue,
            session,
            _marker_comment(
                format_claim_marker(lease), f"Lease renewed until {_iso(lease.expires)}."
            ),
            gh,
            clock,
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
    # Codex 4241196647: the TTL starts now, after the reads above (which may have been slow),
    # not when the operation began.
    lease = replace(lease, expires=clock() + timedelta(minutes=ttl_minutes))
    note = (
        f"Claimed by `{agent}` (session `{session}`) on branch `{branch}`; "
        f"lease expires {_iso(lease.expires)}."
    )
    if took_over:
        note = f"Took over an expired lease from session `{took_over}`. " + note
    marker = format_claim_marker(lease)
    winner = _post_and_arbitrate(
        project, issue, session, _marker_comment(marker, note), gh, clock, retract_on_error=True
    )
    if winner is None:
        return ClaimResult(
            False,
            None,
            reason=(
                f"issue #{issue}: the claim was no longer live when re-read (it expired while "
                "GitHub was being read, or was released); retracted -- retry"
            ),
        )
    if winner.session != session:
        return ClaimResult(
            False,
            winner,
            reason=(
                f"issue #{issue}: lost the claim race to session={getattr(winner, 'session', '?')}"
            ),
        )
    # Codex 4241196645: the open-PR inventory above was read before the marker was posted; a
    # pull request opened in between is durable work this claim would duplicate. Read it again
    # now that the claim is authoritative, and retract on a PR -- or on an unreadable inventory.
    # Codex 4241532620: the issue's state was read before the marker too; an issue closed in
    # between must not be implemented, so it is re-read here as well (unreadable = retract).
    try:
        late_state = json.loads(gh(["api", f"repos/{project}/issues/{issue}"]) or "{}")
        if not isinstance(late_state, dict):
            raise LeaseError(f"issue #{issue} could not be re-read after claiming")
        late = open_prs_for_issue(project, issue, gh)
    except Exception:
        # the read failure is what matters; the retraction is retried (bounded), never raised
        _retract_on_failure(
            project,
            issue,
            session,
            "The issue or its open pull requests could not be re-read after claiming; retracting.",
            gh,
        )
        raise
    if late_state.get("state") != "open":
        state = late_state.get("state", "unknown")
        _retract(
            project,
            issue,
            session,
            f"The issue is {state} now (it changed while claiming); retracting.",
            gh,
        )
        return ClaimResult(
            False, None, reason=f"issue #{issue} became {state} while it was being claimed"
        )
    if late:
        urls = ", ".join(str(p.get("url") or f"#{p.get('number')}") for p in late)
        _retract(
            project,
            issue,
            session,
            f"A pull request for this issue was opened while claiming ({urls}); retracting.",
            gh,
        )
        return ClaimResult(
            False,
            None,
            reason=f"issue #{issue} got an open pull request while it was being claimed: {urls}",
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
        # The marker already made the lease authoritative, but the caller is about to fail and
        # nothing downstream will release it: retract FIRST (bounded retry, Codex 4241327872),
        # so the issue is not blocked for the TTL whatever the cleanup below does.
        retracted = _retract_on_failure(
            project,
            issue,
            session,
            "Label/assignee bookkeeping failed; retracting the claim.",
            gh,
        )
        if assignee and not inherited:
            try:
                owned = _confirm_own_assignment(
                    project, issue, session, assignee, snapshot, now, gh
                )
            except Exception:  # noqa: BLE001 -- unknown: the lease never removes the login
                owned = False
            lease = replace(lease, owns_assignee=owned)
        # Unretracted, the lease is still live: its label and assignee stay with it.
        if retracted:
            try:
                _sync_label(project, issue, gh, now)
                # This claim's own assignment, seconds old: nobody can have re-added it yet.
                if _drop_owned_assignee(project, issue, lease, gh, now, check_history=False):
                    _record_cleanup(project, issue, lease, gh)
            except Exception:  # noqa: BLE001 -- best effort; the original failure matters
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


def _require_open_issue(project: str, issue: int, gh: GhRunner) -> None:
    """Raise LeaseError unless `issue` reads as an open issue (not a pull request). Any read or
    parse failure raises too: an unknown state is never taken for open."""
    try:
        target = json.loads(gh(["api", f"repos/{project}/issues/{issue}"]) or "null")
    except Exception as exc:  # noqa: BLE001 -- any failure means the state is unknown
        raise LeaseError(
            f"issue #{issue}: its state could not be read ({str(exc).strip()[:200]}); not renewing"
        ) from exc
    if not isinstance(target, dict) or target.get("pull_request"):
        raise LeaseError(f"#{issue} is not a readable issue of {project}; not renewing")
    state = target.get("state")
    if state != "open":
        raise LeaseError(f"issue #{issue} is {state or 'in an unknown state'}; not renewing")


def _require_no_open_pr(project: str, issue: int, gh: GhRunner) -> None:
    """Raise LeaseError if `issue` has an open pull request (`open_prs_for_issue`), or if that
    inventory cannot be read."""
    try:
        prs = open_prs_for_issue(project, issue, gh)
    except Exception as exc:  # noqa: BLE001 -- an unreadable inventory is never "no open PR"
        raise LeaseError(
            f"issue #{issue}: its open pull requests could not be read "
            f"({str(exc).strip()[:200]}); not renewing"
        ) from exc
    if prs:
        urls = ", ".join(str(pr.get("url") or f"#{pr.get('number')}") for pr in prs)
        raise LeaseError(f"issue #{issue} already has an open pull request: {urls}; not renewing")


def renew(
    project: str,
    issue: int,
    *,
    session: str,
    ttl_minutes: int = DEFAULT_TTL_MINUTES,
    gh: GhRunner,
    now: datetime | None = None,
    clock: Clock | None = None,
) -> Lease:
    """Extend this session's live lease. Every renewal is also a state check (Codex
    4241614587): the cloud routine renews after implementing, after verifying and immediately
    before pushing, and an issue closed meanwhile must stop it -- so a renewal re-reads the issue
    and refuses unless it is an open issue; an issue that cannot be read refuses too. It also
    re-reads the issue's open pull requests (Codex 4243193102: the same inventory `claim` reads,
    linked and cross-referenced ones included): a PR a human or other automation opened for the
    issue after the claim is durable work this run would duplicate, so the renewal refuses, and an
    unreadable inventory refuses too (LeaseError), never "no PR"."""
    clock = _clock(now, clock)
    now = clock()
    _check_ttl(ttl_minutes)
    _require_open_issue(project, issue, gh)
    _require_no_open_pr(project, issue, gh)
    existing = current_lease(project, issue, gh, now)
    if existing is None or existing.session != session:
        raise LeaseError(f"issue #{issue} is not leased by session {session}")
    if not existing.live(now):
        # An expired lease is up for takeover; renewing it could race a new claimant. Claim again.
        raise LeaseError(f"issue #{issue}: the lease of session {session} has expired; claim again")
    lease = replace(existing, expires=clock() + timedelta(minutes=ttl_minutes))
    body = _marker_comment(
        format_claim_marker(lease), f"Lease renewed until {_iso(lease.expires)}."
    )
    winner = _post_and_arbitrate(project, issue, session, body, gh, clock)
    if winner is None or winner.session != session:
        raise LeaseError(
            f"issue #{issue}: renewal lost to session={getattr(winner, 'session', '?')}"
        )
    return lease


def _release_untrusted_own_claims(
    project: str, issue: int, session: str, note: str, gh: GhRunner
) -> bool:
    """Post a release marker for `session` when the thread holds a claim marker of `session`
    whose author this process does not trust and no release of it has followed (one by that
    same author, or by a trusted one). True when it posted.

    Codex 4242925440: which App bot is trusted depends on configuration
    (FORGE_LEASE_BOT_LOGINS / `publisher_bot_login`). A `release` run without it -- the
    failure-cleanup job's, in an empty workspace -- ignored the Publisher bot's claim, found
    "nothing to release" and exited 0, while every configured reader kept honouring that
    claim until its TTL. Releasing one's OWN session is safe whoever claimed under its name:
    the marker voids only `session`'s claims (a forged claim of it included). Nothing else
    of an untrusted claim is acted on -- its label and assignee are left to the trusted
    reconciliation. Idempotent: a retry finds the release it posted (same author)."""
    permission = _trust(project, gh)
    open_author: str | None = None
    for comment in _comments(project, issue, gh):
        body = str(comment.get("body") or "")
        marker = _marker(body)
        if marker is None or marker[1].get("session") != session:
            continue
        author = login_key((comment.get("user") or {}).get("login"))
        if marker[0] == CLAIM_MARKER and parse_marker(body, issue) is not None:
            if not trusted_marker_author(comment, permission):
                open_author = author
        elif (
            marker[0] == RELEASE_MARKER
            and open_author is not None
            and (author == open_author or trusted_marker_author(comment, permission))
        ):
            open_author = None
    if open_author is None:
        return False
    _comment(
        project,
        issue,
        _marker_comment(
            format_release_marker(session),
            f"Lease released by `{session}` (its claim was posted by `{open_author}`, whose "
            f"markers this run was not configured to trust). {note}",
        ),
        gh,
    )
    return True


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
    # Codex 4242925440: settle this session's claims this process does not trust first -- a
    # release never reports success while its own claim stands unreleased for a reader who
    # trusts its author (the Publisher bot, when this process could not learn it).
    settled = _release_untrusted_own_claims(project, issue, session, note, gh)
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
            if settled:  # this session's only claim was one released above
                return
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


#: The inventory finds leases by their MARKERS, not only by the mutable `in-progress` label (Codex
#: 4240839923: a Triage user removing the label hid a live lease that `claim` still refuses). A
#: live lease's newest claim marker was posted at most MAX_TTL_MINUTES ago (its expiry is capped
#: at post time + MAX_TTL_MINUTES, whatever TTL it asked for), so the repository's comments
#: updated since then hold every live lease's marker. They are read page by page up to the cap;
#: reaching it, or any unreadable answer, makes the inventory INCOMPLETE.
CLAIMS_COMMENT_PAGE_SIZE = 100
CLAIMS_COMMENT_PAGE_CAP = 20
_ISSUE_URL = re.compile(r"/issues/(\d+)$")


@dataclass(frozen=True)
class ClaimInventory:
    """The leases of a repository's open issues, and why the list may be incomplete (`gaps`:
    empty when every source was read in full)."""

    rows: tuple[ClaimRow, ...]
    gaps: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.gaps


def _api_json(path: str, gh: GhRunner) -> object:
    return json.loads(gh(["api", path]) or "null")


def _marker_issue_numbers(
    project: str, gh: GhRunner, since: datetime, page_cap: int
) -> tuple[set[int], list[str]]:
    """The issues (or PRs) with a claim-marker comment updated since `since`, and the gaps."""
    numbers: set[int] = set()
    for page in range(1, page_cap + 1):
        path = (
            f"repos/{project}/issues/comments?since={_iso(since)}"
            f"&per_page={CLAIMS_COMMENT_PAGE_SIZE}&page={page}"
        )
        try:
            rows = _api_json(path, gh)
        except Exception as exc:  # noqa: BLE001 -- an unreadable page is a gap, never "none"
            return numbers, [f"recent comments page {page} unreadable ({type(exc).__name__})"]
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            return numbers, [f"recent comments page {page} unreadable (not a list of objects)"]
        for row in rows:
            body = str(row.get("body") or "")
            marker = _marker(body)
            if marker is None or marker[0] != CLAIM_MARKER:
                continue
            found = _ISSUE_URL.search(str(row.get("issue_url") or ""))
            if found is None:
                return numbers, [f"a claim marker comment ({row.get('id')}) names no issue"]
            numbers.add(int(found.group(1)))
        if len(rows) < CLAIMS_COMMENT_PAGE_SIZE:
            return numbers, []
    return numbers, [
        f"more than {page_cap * CLAIMS_COMMENT_PAGE_SIZE} comments since {_iso(since)} "
        "(page cap reached)"
    ]


def claim_inventory(
    project: str,
    gh: GhRunner,
    now: datetime | None = None,
    *,
    page_cap: int = CLAIMS_COMMENT_PAGE_CAP,
) -> ClaimInventory:
    """Every open issue's authoritative lease (`current_lease`), live or expired. Candidates are
    the open issues labelled `in-progress` (a hint only) AND every issue with a claim-marker
    comment updated within MAX_TTL_MINUTES -- so removing the label never hides a live lease.
    Anything unreadable, or the comment page cap, is recorded in `gaps`."""
    now = now or datetime.now(UTC)
    gaps: list[str] = []
    candidates: set[int] = set()
    try:
        labelled = _paged(
            f"repos/{project}/issues?labels={IN_PROGRESS_LABEL}&state=open&per_page=100", gh
        )
    except Exception as exc:  # noqa: BLE001 -- recorded as a gap
        gaps.append(f"the {IN_PROGRESS_LABEL} issue list is unreadable ({type(exc).__name__})")
        labelled = []
    candidates.update(
        int(item["number"])
        for item in labelled
        if not item.get("pull_request") and isinstance(item.get("number"), int)
    )
    marked, marker_gaps = _marker_issue_numbers(
        project, gh, now - timedelta(minutes=MAX_TTL_MINUTES), page_cap
    )
    gaps.extend(marker_gaps)
    for number in sorted(marked - candidates):
        try:
            issue = _api_json(f"repos/{project}/issues/{number}", gh)
        except Exception as exc:  # noqa: BLE001 -- recorded as a gap
            gaps.append(f"issue #{number} unreadable ({type(exc).__name__})")
            continue
        if not isinstance(issue, dict):
            gaps.append(f"issue #{number} unreadable (not an object)")
        elif not issue.get("pull_request") and issue.get("state") == "open":
            candidates.add(number)
    rows = []
    for number in sorted(candidates):
        try:
            lease = current_lease(project, number, gh)
        except Exception as exc:  # noqa: BLE001 -- recorded as a gap
            gaps.append(f"the lease of #{number} is unreadable ({exc})")
            continue
        if lease is not None:
            rows.append(ClaimRow(lease, expired=not lease.live(now)))
    return ClaimInventory(tuple(sorted(rows, key=lambda r: r.lease.issue)), tuple(gaps))


def list_claims(project: str, gh: GhRunner, now: datetime | None = None) -> list[ClaimRow]:
    """`claim_inventory`'s rows; raises LeaseError when the inventory is incomplete (a partial
    list must never read as the whole one)."""
    inventory = claim_inventory(project, gh, now)
    if not inventory.complete:
        raise LeaseError("lease inventory INCOMPLETE: " + "; ".join(inventory.gaps))
    return list(inventory.rows)


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


# ---- Approval labeler authorization (Codex 4239868029). A user with Triage permission can apply
# labels without repository write authority, so the labels alone never authorize work: the
# Actions path runs `_authorize-labeler.yml` before implementing (the same history rule, in shell:
# Codex 4239965189), and a cloud routine runs `sdlcctl authorize-labeler` before claiming. Both
# require a DEFINITIVE write/maintain/admin answer for whoever applied EACH label of the approval
# set most recently (Codex 4240421144: checking the approval label alone let a Triage user
# COMPLETE the set by adding a prerequisite after a writer approved): a 200 carrying the
# permission, or a 404 (no access: unauthorized). Anything else -- the events or a permission
# unreadable, a label with no `labeled` event, an event without an actor -- is unverifiable and
# refuses (LeaseError), never reads as authorized. A label of the set removed since it was last
# applied is unauthorized.
#
# Codex 4241196649: an approval is bound to the issue content it saw. The title (its `renamed`
# events) and the body (GraphQL `lastEditedAt` and the latest `userContentEdits` entry) must have
# been last edited BEFORE the most recent application of EACH label of the set; an issue edited
# since is unauthorized until the labels are re-applied, and an edit time that cannot be read
# refuses (LeaseError). The Actions path applies the same rule, and an `edited` event cancels a
# queued or running authorized run there.


@dataclass(frozen=True)
class LabelerAuthorization:
    issue: int
    label: str
    labeler: str
    permission: str | None
    authorized: bool
    reason: str
    #: (label, who applied it most recently) for every label of the set, once all were read
    labelers: tuple[tuple[str, str], ...] = ()

    def as_dict(self) -> dict:
        return {
            "issue": self.issue,
            "label": self.label,
            "labeler": self.labeler,
            "permission": self.permission,
            "authorized": self.authorized,
            "reason": self.reason,
            "labelers": dict(self.labelers),
        }


def labeler_permission(project: str, login: str, gh: GhRunner) -> str | None:
    """`login`'s repository permission (role_name when it is a write role, else permission);
    None on a 404 (no access). Any other failure or shape raises LeaseError."""
    from urllib.parse import quote

    path = f"repos/{project}/collaborators/{quote(login, safe='')}/permission"
    try:
        raw = gh(["api", path])
    except Exception as exc:  # noqa: BLE001 -- only a 404 is an answer
        if _answered_404(exc):
            return None
        raise LeaseError(
            f"cannot read the repository permission of {login!r} "
            f"({str(exc).strip()[:200] or type(exc).__name__}); not authorizing"
        ) from exc
    try:
        data = json.loads(raw or "null")
    except ValueError as exc:
        raise LeaseError(f"unreadable answer from {path}: not JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("permission"), str):
        raise LeaseError(f"unreadable answer from {path}: no permission in it")
    for key in ("role_name", "permission"):
        value = login_key(data.get(key))
        if value in WRITE_PERMISSIONS:
            return value
    return str(data.get("permission") or "") or None


#: When the issue's body was last edited (Codex 4241196649). `lastEditedAt` is null for a body
#: never edited; the newest `userContentEdits` entry is read too, and the later of the two counts.
_ISSUE_EDITS_QUERY = """\
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      lastEditedAt
      userContentEdits(last: 1) { nodes { editedAt } }
    }
  }
}"""


def _last_content_edit(
    project: str, issue: int, events: list[dict], gh: GhRunner
) -> tuple[datetime, str] | None:
    """The time and kind ("title" or "body") of the issue's latest title or body edit; None when
    neither was ever edited. Anything unreadable raises LeaseError: an approval whose content
    cannot be dated never authorizes."""
    latest: tuple[datetime, str] | None = None

    def later(at: datetime, what: str) -> None:
        nonlocal latest
        if latest is None or at > latest[0]:
            latest = (at, what)

    for event in events:
        if event.get("event") != "renamed":
            continue
        at = _parse_iso(str(event.get("created_at") or ""))
        if at is None:
            raise LeaseError(
                f"issue #{issue}: a title edit carries no readable time; not authorizing"
            )
        later(at, "title")
    owner, _, name = project.partition("/")
    what = f"issue #{issue}: when its body was last edited could not be read"
    args = ["api", "graphql", "-f", f"query={_ISSUE_EDITS_QUERY}"]
    args += ["-f", f"owner={owner}", "-f", f"name={name}", "-F", f"number={issue}"]
    try:
        answer = json.loads(gh(args) or "null")
    except Exception as exc:  # noqa: BLE001 -- an undatable body never authorizes
        raise LeaseError(f"{what} ({str(exc).strip()[:200]}); not authorizing") from exc
    data = answer.get("data") if isinstance(answer, dict) else None
    repo = data.get("repository") if isinstance(data, dict) else None
    node = repo.get("issue") if isinstance(repo, dict) else None
    edits = node.get("userContentEdits") if isinstance(node, dict) else None
    nodes = edits.get("nodes") if isinstance(edits, dict) else None
    if (
        not isinstance(answer, dict)
        or answer.get("errors")
        or not isinstance(node, dict)
        or "lastEditedAt" not in node
        or not isinstance(nodes, list)
    ):
        raise LeaseError(f"{what} (unreadable answer); not authorizing")
    stamps = [node["lastEditedAt"]] if node["lastEditedAt"] is not None else []
    stamps += [n.get("editedAt") if isinstance(n, dict) else None for n in nodes]
    for stamp in stamps:
        at = _parse_iso(stamp) if isinstance(stamp, str) else None
        if at is None:
            raise LeaseError(f"{what} (an edit carries no readable time); not authorizing")
        later(at, "body")
    return latest


def issue_events(project: str, issue: int, gh: GhRunner) -> list[dict]:
    """Every event of `issue` (labeled, unlabeled, renamed, ...), oldest first; unreadable raises
    LeaseError. `check-request --authorize-labels` reads it once and hands the same list to
    `authorize_labeler`, so the labels are judged on the snapshot it then re-reads."""
    try:
        return _issue_events(project, issue, gh)
    except LeaseError:
        raise
    except Exception as exc:  # noqa: BLE001 -- unreadable history never authorizes
        raise LeaseError(
            f"cannot read the events of issue #{issue} ({str(exc).strip()[:200]}); not authorizing"
        ) from exc


def issue_event_keys(events: list[dict]) -> list[str]:
    """The identities of `events`, in order: two reads of an issue's history are the same
    history only if these are equal."""
    return [_event_key(event) for event in events]


def authorize_labeler(
    project: str,
    issue: int,
    labels: str | Sequence[str],
    gh: GhRunner,
    events: list[dict] | None = None,
) -> LabelerAuthorization:
    """Did someone with write/maintain/admin apply EACH of `labels` (the approval set; a single
    label is a set of one) to `issue` most recently (see above)? The verdict's `label`/`labeler`
    are the FIRST label's (the approval label) and its latest applier. `events`, when given, is
    the issue's history as already read (`issue_events`), judged instead of reading it again.
    Returns the verdict; raises LeaseError when it cannot be verified."""
    wanted = [labels] if isinstance(labels, str) else list(labels)
    if not wanted or not all(isinstance(name, str) and name.strip() for name in wanted):
        raise LeaseError("authorize-labeler needs at least one label name; not authorizing")
    keys = list(dict.fromkeys(name.casefold() for name in wanted))  # case-insensitive on GitHub
    spelled = {name.casefold(): name for name in reversed(wanted)}
    approval = wanted[0]
    if events is None:
        events = issue_events(project, issue, gh)
    latest: dict[str, dict] = {}
    for event in events:
        if event.get("event") not in ("labeled", "unlabeled"):
            continue
        label_obj = event.get("label")
        name = label_obj.get("name") if isinstance(label_obj, dict) else None
        if not isinstance(name, str):
            raise LeaseError(
                f"issue #{issue}: a label event carries no label name; not authorizing"
            )
        if name.casefold() in keys:
            latest[name.casefold()] = event
    missing = [spelled[key] for key in keys if key not in latest]
    if missing:
        raise LeaseError(
            f"issue #{issue}: no `labeled` event for {', '.join(map(repr, missing))}; "
            "not authorizing"
        )
    removed = [spelled[key] for key in keys if latest[key].get("event") != "labeled"]
    if removed:
        return LabelerAuthorization(
            issue,
            approval,
            "",
            None,
            False,
            f"{', '.join(map(repr, removed))} was removed after it was last applied",
        )
    applied_at: dict[str, datetime] = {}
    for key in keys:
        at = _parse_iso(str(latest[key].get("created_at") or ""))
        if at is None:
            raise LeaseError(
                f"issue #{issue}: the {spelled[key]!r} event carries no readable time; "
                "not authorizing"
            )
        applied_at[key] = at
    edited = _last_content_edit(project, issue, events, gh)
    approved_from = min(applied_at.values())
    if edited is not None and edited[0] >= approved_from:
        stale = [spelled[key] for key in keys if applied_at[key] <= edited[0]]
        return LabelerAuthorization(
            issue,
            approval,
            "",
            None,
            False,
            f"the issue's {edited[1]} was edited at {_iso(edited[0])}, after "
            f"{', '.join(map(repr, stale))} was last applied: the approval does not cover the "
            "current content. A maintainer must review it and re-apply (remove and add) "
            + ("that label" if len(stale) == 1 else "those labels"),
        )
    appliers: dict[str, str] = {}
    for key in keys:
        actor = latest[key].get("actor")
        login = actor.get("login") if isinstance(actor, dict) else None
        if not isinstance(login, str) or not login.strip():
            raise LeaseError(
                f"issue #{issue}: the {spelled[key]!r} event names no actor; not authorizing"
            )
        appliers[key] = login
    labelers = tuple((spelled[key], appliers[key]) for key in keys)
    permissions = {
        login: labeler_permission(project, login, gh) for login in dict.fromkeys(appliers.values())
    }
    approver = appliers[keys[0]]
    for key in keys:
        login = appliers[key]
        permission = permissions[login]
        if permission is None or login_key(permission) not in WRITE_PERMISSIONS:
            return LabelerAuthorization(
                issue,
                approval,
                approver,
                permissions[approver],
                False,
                f"{login} applied {spelled[key]!r} without repository write authority "
                f"(permission: {permission or 'none'})",
                labelers,
            )
    who = ", ".join(f"{login} has {permissions[login]}" for login in permissions)
    return LabelerAuthorization(
        issue,
        approval,
        approver,
        permissions[approver],
        True,
        f"{who} permission (latest applier of each approval label)",
        labelers,
    )
