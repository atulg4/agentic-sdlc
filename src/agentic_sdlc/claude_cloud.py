"""Operator-triggered Claude subscription workers with durable duplicate suppression.

The routines API has no idempotency key. Reserve locally BEFORE sending; an
ambiguous response must never be retried automatically. This records dispatch,
not worker completion, acceptance, or authority to publish a change.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROLES = ("review", "tests", "research")
API_ROOT = "https://api.anthropic.com/v1/claude_code/routines"
API_VERSION = "2026-10-21"


class CloudDispatchError(ValueError):
    """Invalid configuration or a dispatch that cannot safely be attempted."""


@dataclass(frozen=True)
class CloudTask:
    repository: str
    target_type: str
    target_number: int
    head_sha: str
    role: str

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", self.repository):
            raise CloudDispatchError("repository must be a GitHub owner/repository")
        if self.repository.split("/")[1] in {".", ".."}:
            raise CloudDispatchError("invalid repository name")
        if self.target_type not in {"issue", "pull"}:
            raise CloudDispatchError("target type must be issue or pull")
        if type(self.target_number) is not int or self.target_number <= 0:
            raise CloudDispatchError("target number must be a positive integer")
        if not re.fullmatch(r"[0-9a-fA-F]{40}", self.head_sha):
            raise CloudDispatchError("head SHA must be a full 40-character commit SHA")
        if self.role not in ROLES:
            raise CloudDispatchError("role must be review, tests, or research")
        if self.role == "review" and self.target_type != "pull":
            raise CloudDispatchError("review workers require a pull request")
        object.__setattr__(self, "repository", self.repository.lower())
        object.__setattr__(self, "head_sha", self.head_sha.lower())

    def payload(self) -> dict:
        return {"schema_version": 1, **asdict(self)}

    @property
    def request_id(self) -> str:
        return hashlib.sha256(_json(self.payload()).encode()).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the routine credential to another URL, including a login page.
        return None


def _post(routine: str, token: str, payload: dict) -> dict:
    request = Request(
        f"{API_ROOT}/{routine}/fire",
        data=_json({"text": _json(payload)}).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-version": API_VERSION,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with build_opener(_NoRedirect()).open(request, timeout=30) as response:
        if response.status != 200:
            raise CloudDispatchError("unexpected response status")
        body = response.read(65537)
        if len(body) > 65536:
            raise CloudDispatchError("response exceeds size limit")
        return json.loads(body)


def _session(response: object) -> dict:
    if not isinstance(response, dict) or response.get("type") != "routine_fire":
        raise CloudDispatchError("invalid routine response")
    identifier = response.get("claude_code_session_id")
    if not isinstance(identifier, str) or not re.fullmatch(r"session_[A-Za-z0-9_-]+", identifier):
        raise CloudDispatchError("invalid session identifier")
    url = f"https://claude.ai/code/{identifier}"
    if response.get("claude_code_session_url") != url:
        raise CloudDispatchError("invalid session URL")
    return {"session_id": identifier, "session_url": url}


def _retry_at(headers, now: float) -> float:
    value = headers.get("Retry-After", "") if headers else ""
    try:
        seconds = int(value)
        return now + max(60, seconds)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return max(now + 60, parsed.timestamp())
        except (TypeError, ValueError, OverflowError):
            return now + 60


class DispatchJournal:
    """One persistent SQLite file shared by every dispatcher for this account.

    Keep it outside disposable worktrees/CI runners. Losing it loses local
    duplicate suppression. The journal never stores credentials or remote errors.
    """

    def __init__(self, path: Path, *, create: bool = True):
        self.path = path
        self.create = create
        if not create:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS cloud_dispatches "
                "(request_id TEXT PRIMARY KEY, record TEXT NOT NULL)"
            )
            db.commit()

    def _connect(self, *, read_only: bool = False):
        mode = "ro" if read_only else "rwc" if self.create else "rw"
        return sqlite3.connect(f"{self.path.resolve().as_uri()}?mode={mode}", uri=True, timeout=10)

    def get(self, request_id: str) -> dict | None:
        with closing(self._connect(read_only=True)) as db:
            row = db.execute(
                "SELECT record FROM cloud_dispatches WHERE request_id = ?", (request_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def reserve(self, task: CloudTask, routine: str, retry_rejected: bool) -> tuple[dict, bool]:
        now = time.time()
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT record FROM cloud_dispatches WHERE request_id = ?", (task.request_id,)
            ).fetchone()
            previous = json.loads(row[0]) if row else None
            if previous:
                if not retry_rejected or previous["state"] != "rejected":
                    return previous, False
                if now < previous.get("retry_at", 0):
                    raise CloudDispatchError("rate limited; wait until the journal's retry_at")
            record = {
                "request_id": task.request_id,
                "task": task.payload(),
                "routine_id": routine,
                "state": "dispatching",
                "attempt": previous["attempt"] + 1 if previous else 1,
                "updated_at": now,
            }
            db.execute(
                "INSERT OR REPLACE INTO cloud_dispatches VALUES (?, ?)",
                (task.request_id, _json(record)),
            )
        return record, True

    def finish(self, record: dict) -> dict:
        record = {**record, "updated_at": time.time()}
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT record FROM cloud_dispatches WHERE request_id = ?", (record["request_id"],)
            ).fetchone()
            if row:
                previous = json.loads(row[0])
                if previous.get("reconciliation_source") == "operator":
                    # An in-flight request can fail after the operator observed
                    # its session. Never overwrite that evidence with uncertainty.
                    return previous
            db.execute(
                "UPDATE cloud_dispatches SET record = ? WHERE request_id = ?",
                (_json(record), record["request_id"]),
            )
        return record

    def reconcile(self, request_id: str, session_url: str, observed_head_sha: str) -> dict:
        """Record an operator-observed launch; this never asserts review completion."""
        match = re.fullmatch(r"https://claude\.ai/code/(session_[A-Za-z0-9_-]+)", session_url)
        if not match:
            raise CloudDispatchError("provide a canonical Claude Code session URL without a query")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT record FROM cloud_dispatches WHERE request_id = ?", (request_id,)
            ).fetchone()
            if not row:
                raise CloudDispatchError("request is not present in this journal")
            record = json.loads(row[0])
            if record["task"]["head_sha"] != observed_head_sha.lower():
                raise CloudDispatchError("observed SHA does not match the recorded task")
            if record["state"] == "started":
                if record.get("session_url") != session_url:
                    raise CloudDispatchError("request already has a different session")
                return record
            if record["state"] not in {"dispatching", "unknown"}:
                raise CloudDispatchError("only an ambiguous dispatch can be reconciled")
            record.update(
                state="started",
                session_id=match[1],
                session_url=session_url,
                reconciliation_source="operator",
                updated_at=time.time(),
            )
            db.execute(
                "UPDATE cloud_dispatches SET record = ? WHERE request_id = ?",
                (_json(record), request_id),
            )
        return record


def dispatch(task: CloudTask, journal: DispatchJournal, *, retry_rejected: bool = False) -> dict:
    prefix = f"FORGE_CLAUDE_{task.role.upper()}"
    routine = os.environ.get(f"{prefix}_ROUTINE_ID", "")
    token = os.environ.get(f"{prefix}_ROUTINE_TOKEN", "")
    if not re.fullmatch(r"trig_[A-Za-z0-9_-]+", routine):
        raise CloudDispatchError(f"set {prefix}_ROUTINE_ID to the routine's trig_ identifier")
    if not re.fullmatch(r"sk-ant-oat01-[A-Za-z0-9_-]+", token):
        raise CloudDispatchError(f"set {prefix}_ROUTINE_TOKEN to a per-routine bearer token")
    record, reserved = journal.reserve(task, routine, retry_rejected)
    if not reserved:
        return {**record, "deduplicated": True}
    try:
        response = _post(routine, token, {**task.payload(), "request_id": task.request_id})
        record.update(state="started", **_session(response))
    except HTTPError as error:
        # Documented rejections cannot have created a session. Everything else is
        # ambiguous, including 5xx and redirects; there is no automatic POST retry.
        state = "rejected" if error.code in {400, 401, 403, 404, 429} else "unknown"
        record.update(state=state, http_status=error.code)
        if error.code == 429:
            record["retry_at"] = _retry_at(error.headers, time.time())
        error.close()
    except (URLError, OSError, ValueError, HTTPException) as error:
        # Do not include exception messages: URLs/headers/bodies may contain secrets.
        record.update(
            state="unknown",
            failure_kind=(
                "invalid_response"
                if isinstance(error, ValueError)
                else "timeout"
                if isinstance(error, TimeoutError)
                else "transport"
            ),
        )
    try:
        return {**journal.finish(record), "deduplicated": False}
    except sqlite3.Error:
        # Reservation still blocks a duplicate POST. Preserve known session data
        # so the operator can reconcile after repairing the journal.
        return {**record, "deduplicated": False, "journal_committed": False}


def _command(args) -> int:
    if args.command == "claude-cloud-status":
        result = DispatchJournal(Path(args.journal), create=False).get(args.request_id)
        if result is None:
            raise CloudDispatchError("request is not present in this journal")
    elif args.command == "claude-cloud-reconcile":
        result = DispatchJournal(Path(args.journal), create=False).reconcile(
            args.request_id, args.session_url, args.observed_head_sha
        )
    else:
        task = CloudTask(args.project, args.target_type, args.number, args.head_sha, args.role)
        if not args.execute:
            print(_json({"request_id": task.request_id, "task": task.payload(), "dry_run": True}))
            return 0
        if not args.journal:
            raise CloudDispatchError("--execute requires --journal on persistent storage")
        result = dispatch(
            task, DispatchJournal(Path(args.journal)), retry_rejected=args.retry_rejected
        )
    print(_json(result))
    return 0 if result["state"] == "started" and result.get("journal_committed", True) else 3


def command(args) -> int:
    try:
        return _command(args)
    except sqlite3.Error:
        raise CloudDispatchError("dispatch journal could not be read or committed") from None


def add_commands(commands) -> None:
    fire = commands.add_parser("claude-cloud-dispatch", help="submit a bounded Claude routine task")
    fire.add_argument("--project", required=True)
    fire.add_argument("--target-type", choices=("issue", "pull"), required=True)
    fire.add_argument("--number", type=int, required=True)
    fire.add_argument("--head-sha", required=True)
    fire.add_argument("--role", choices=ROLES, required=True)
    fire.add_argument("--journal", help="persistent SQLite journal shared by all dispatchers")
    fire.add_argument("--execute", action="store_true", help="send the task (default is dry-run)")
    fire.add_argument("--retry-rejected", action="store_true", help="retry a known HTTP rejection")
    fire.set_defaults(handler=command)
    status = commands.add_parser("claude-cloud-status", help="read a local dispatch record")
    status.add_argument("--journal", required=True)
    status.add_argument("--request-id", required=True)
    status.set_defaults(handler=command)
    reconcile = commands.add_parser(
        "claude-cloud-reconcile", help="record an observed session without launching another"
    )
    reconcile.add_argument("--journal", required=True)
    reconcile.add_argument("--request-id", required=True)
    reconcile.add_argument("--session-url", required=True)
    reconcile.add_argument("--observed-head-sha", required=True)
    reconcile.set_defaults(handler=command)
