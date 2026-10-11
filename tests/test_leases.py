from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from agentic_sdlc.leases import (
    IN_PROGRESS_LABEL,
    MAX_TTL_MINUTES,
    Lease,
    LeaseError,
    claim,
    current_lease,
    format_claim_marker,
    list_claims,
    parse_marker,
    release,
    renew,
)
from agentic_sdlc.onboard import GhCommandError, OnboardError

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
PROJECT = "owner/repo"
PAGE = 30


def _comment_row(cid, body, created_at, login="atulg4", kind="User", association="OWNER"):
    return {
        "id": cid,
        "body": body,
        "created_at": created_at,
        "user": {"login": login, "type": kind},
        "author_association": association,
    }


def _graphql_fields(args) -> dict:
    return dict(a.split("=", 1) for a in args[2:] if "=" in a and not a.startswith("query="))


def _open_prs_page(prs, args, size=100):
    """One page of the open-PR GraphQL query over `prs` (rows in `gh pr list --json` shape;
    a list `closingIssuesReferences` is wrapped the way GraphQL pages it)."""
    start = int(_graphql_fields(args).get("after") or 0)
    nodes = []
    for pr in prs[start : start + size]:
        refs = pr.get("closingIssuesReferences")
        if isinstance(refs, list):
            pr = {
                **pr,
                "closingIssuesReferences": {"pageInfo": {"hasNextPage": False}, "nodes": refs},
            }
        nodes.append(pr)
    more = start + size < len(prs)
    info = {"hasNextPage": more, "endCursor": str(start + size) if more else None}
    return {"data": {"repository": {"pullRequests": {"pageInfo": info, "nodes": nodes}}}}


def _timeline_page(timelines: dict, path: str, size: int = 100) -> list:
    """One page (`?per_page=&page=`) of an issue timeline from {issue: [events]}."""
    n = int(path.split("/")[4])
    query = dict(kv.split("=", 1) for kv in path.split("?", 1)[1].split("&"))
    assert int(query["per_page"]) == size
    start = (int(query["page"]) - 1) * size
    return timelines.get(n, [])[start : start + size]


class FakeGh:
    """Minimal GitHub: issues with labels/assignees/comments, open PRs."""

    def __init__(self):
        self.issues: dict[int, dict] = {}
        self.prs: list[dict] = []
        self.calls: list[tuple[str, ...]] = []
        self._next_comment = 100
        self.poster: dict = {}  # who posts comments made through this fake (default: OWNER)
        self.before_post = None  # one-shot hook: simulate a comment arriving before ours
        self.posted_at = NOW  # created_at stamped on comments made through this fake
        # repository permission per login (the collaborator permission API); others: 404
        self.permissions: dict[str, str] = {"atulg4": "admin"}
        self.events: dict[int, list[dict]] = {}  # issue events (assigned, ...)
        self.timeline: dict[int, list[dict]] = {}  # issue timeline (connected, cross-referenced)
        self.link_subjects: dict[str, dict | None] = {}  # connected/disconnected node -> subject
        # (owner/name, number) -> another repository's PR answer (dict, raw text or exception);
        # unknown ones answer NOT_FOUND
        self.external_prs: dict[tuple[str, int], object] = {}
        self.repo_comments_error: Exception | None = None  # the repo-wide comment list fails
        self.repo_comment_pages: list[int] = []  # pages of it read
        # the policy committed on the default branch (Codex 4242925440): text, an exception
        # the contents read raises, or None (no committed policy: 404)
        self.committed_policy: str | Exception | None = None

    def issue(self, number, labels=(), assignees=(), comments=()):
        self.issues[number] = {
            "number": number,
            "labels": [{"name": n} for n in labels],
            "assignees": [{"login": a} for a in assignees],
            "comments": [
                _comment_row(1 + i, b, "2026-09-30T00:00:00Z") for i, b in enumerate(comments)
            ],
        }
        return self

    @staticmethod
    def _pages(items, args):
        """`gh api --paginate`: one JSON document per page, wrapped only with --slurp."""
        pages = [items[i : i + PAGE] for i in range(0, len(items), PAGE)] or [[]]
        if "--slurp" in args:
            return json.dumps(pages)
        return "".join(json.dumps(page) for page in pages)

    def __call__(self, args, input=None):
        args = tuple(args)
        self.calls.append(args)
        if args[0] == "api" and args[1].startswith(f"repos/{PROJECT}/issues?labels="):
            labelled = [
                i
                for i in self.issues.values()
                if any(lbl["name"] == IN_PROGRESS_LABEL for lbl in i["labels"])
            ]
            return self._pages(labelled, args)
        if args[:2] == ("api", f"repos/{PROJECT}"):
            return json.dumps({"full_name": PROJECT, "default_branch": "main"})
        if args[0] == "api" and args[1].startswith(f"repos/{PROJECT}/contents/"):
            assert args[1] == f"repos/{PROJECT}/contents/agentic-sdlc.toml?ref=main"
            if isinstance(self.committed_policy, Exception):
                raise self.committed_policy
            if self.committed_policy is None:  # what run_gh raises for GitHub's 404 answer
                raise GhCommandError("gh: Not Found (HTTP 404)", http_status=404)
            content = base64.b64encode(self.committed_policy.encode()).decode()
            return json.dumps({"type": "file", "encoding": "base64", "content": content})
        if args[0] == "api" and args[1].startswith(f"repos/{PROJECT}/collaborators/"):
            login = args[1].split("/")[4]
            known = {k.casefold(): v for k, v in self.permissions.items()}  # GitHub: any case
            if login.casefold() not in known:
                raise RuntimeError("gh: Not Found (HTTP 404)")
            level = known[login.casefold()]
            legacy = {"maintain": "write", "triage": "read"}.get(level, level)
            return json.dumps({"permission": legacy, "role_name": level})
        if args[0] == "api" and args[1].split("?")[0].endswith("/events"):
            n = int(args[1].split("/")[4])
            return self._pages(self.events.get(n, []), args)
        if args[0] == "api" and args[1].split("?")[0].endswith("/timeline"):
            return json.dumps(_timeline_page(self.timeline, args[1]))
        if args[0] == "api" and args[1].startswith(f"repos/{PROJECT}/issues/comments?"):
            # the repository's issue comments updated since `since`, one `page` at a time
            if self.repo_comments_error is not None:
                raise self.repo_comments_error
            query = dict(kv.split("=", 1) for kv in args[1].split("?", 1)[1].split("&"))
            since = datetime.fromisoformat(query["since"].replace("Z", "+00:00"))
            rows = [
                {**c, "issue_url": f"https://api.github.com/repos/{PROJECT}/issues/{n}"}
                for n, issue in sorted(self.issues.items())
                for c in issue["comments"]
                if datetime.fromisoformat(c["created_at"].replace("Z", "+00:00")) >= since
            ]
            size, page = int(query["per_page"]), int(query["page"])
            self.repo_comment_pages.append(page)
            return json.dumps(rows[(page - 1) * size : page * size])
        if args[0] == "api" and args[1].split("?")[0].endswith("/comments") and "-X" not in args:
            n = int(args[1].split("/")[4])
            return self._pages(self.issues[n]["comments"], args)
        if args[:3] == ("api", "-X", "POST") and args[3].endswith("/comments"):
            n = int(args[3].split("/")[4])
            body = json.loads(input)["body"]
            if self.before_post is not None:
                hook, self.before_post = self.before_post, None
                hook(n)  # a rival's comment lands first (concurrent claimer)
            self._next_comment += 1
            self.issues[n]["comments"].append(
                _comment_row(self._next_comment, body, self.posted_at.isoformat(), **self.poster)
            )
            return json.dumps({"id": self._next_comment})
        if args[:2] == ("issue", "edit"):
            n = int(args[2])
            if "--add-label" in args:
                self.issues[n]["labels"].append({"name": args[args.index("--add-label") + 1]})
            if "--remove-label" in args:
                name = args[args.index("--remove-label") + 1]
                self.issues[n]["labels"] = [
                    lbl for lbl in self.issues[n]["labels"] if lbl["name"] != name
                ]
            if "--add-assignee" in args:
                login = args[args.index("--add-assignee") + 1]
                held = {a["login"].casefold() for a in self.issues[n]["assignees"]}
                if login.casefold() not in held:  # GitHub logins are case-insensitive
                    self.issues[n]["assignees"].append({"login": login})
                    self.events.setdefault(n, []).append(
                        {
                            "event": "assigned",
                            "assignee": {"login": login},
                            "actor": {"login": self.poster.get("login", "atulg4")},
                            "created_at": self.posted_at.isoformat(),
                        }
                    )
            if "--remove-assignee" in args:
                login = args[args.index("--remove-assignee") + 1]
                self.issues[n]["assignees"] = [
                    a
                    for a in self.issues[n]["assignees"]
                    if a["login"].casefold() != login.casefold()
                ]
            return ""
        if (
            args[0] == "api"
            and args[1].startswith(f"repos/{PROJECT}/issues/")
            and (args[1].count("/") == 4)
        ):
            n = int(args[1].split("/")[4])
            issue = self.issues[n]
            return json.dumps(
                {
                    "number": n,
                    "state": issue.get("state", "open"),
                    "assignees": issue["assignees"],
                    **issue.get("extra", {}),
                }
            )
        if args[:2] == ("api", "graphql") and "nodes(ids:" in args[3]:
            ids = [a.split("=", 1)[1] for a in args if a.startswith("ids[]=")]
            nodes = [{"subject": self.link_subjects.get(i)} for i in ids]
            return json.dumps({"data": {"nodes": nodes}})
        if args[:2] == ("api", "graphql") and "pullRequest(number:" in args[3]:
            values = dict(a.split("=", 1) for a in args[4:] if "=" in a)
            key = (f"{values['owner']}/{values['name']}", int(values["number"]))
            answer = self.external_prs.get(key)
            if isinstance(answer, Exception):
                raise answer
            if isinstance(answer, str):
                return answer
            if answer is None:
                return json.dumps({"data": {"repository": None}, "errors": [{"type": "NOT_FOUND"}]})
            return json.dumps(answer)
        if args[:2] == ("api", "graphql"):
            return json.dumps(_open_prs_page(self.prs, args))
        if args[:2] == ("label", "create"):
            return ""
        raise AssertionError(f"unexpected gh call: {' '.join(args)}")


def test_marker_round_trips():
    lease = Lease(
        issue=7,
        agent="cloud-routine",
        session="s1",
        branch="forge/issue-7",
        expires=NOW + timedelta(hours=4),
    )
    text = format_claim_marker(lease)
    assert text.startswith("<!-- forge-claim ") and text.endswith("-->")
    assert parse_marker(f"{text}\nmore human text", issue=7) == lease
    # a marker is read only from the first line, where the tool writes it
    assert parse_marker(f"some human text\n{text}\nmore", issue=7) is None


def test_parse_ignores_unrelated_and_malformed_comments():
    assert parse_marker("just chatting about <!-- forge-claim --> nothing", issue=1) is None
    assert (
        parse_marker("<!-- forge-claim agent=x session=y branch=b expires=not-a-date -->", issue=1)
        is None
    )


def test_current_lease_prefers_the_session_that_claimed_first():
    """Two live claims (a race both contenders lost track of): the earlier poster owns it."""
    gh = FakeGh().issue(
        7,
        labels=[IN_PROGRESS_LABEL],
        comments=[
            format_claim_marker(Lease(7, "a", "s1", "b1", NOW + timedelta(hours=1))),
            format_claim_marker(Lease(7, "b", "s2", "b2", NOW + timedelta(hours=2))),
        ],
    )
    assert current_lease(PROJECT, 7, gh, now=NOW).session == "s1"
    gh.issues[7]["comments"].append(
        _comment_row(9, "<!-- forge-release session=s1 -->", "2026-09-30T01:00:00Z")
    )
    assert current_lease(PROJECT, 7, gh, now=NOW).session == "s2"


def test_current_lease_takes_the_latest_claim_unless_released():
    gh = FakeGh().issue(
        7,
        labels=[IN_PROGRESS_LABEL],
        comments=[
            format_claim_marker(Lease(7, "a", "s1", "b1", NOW + timedelta(hours=1))),
            format_claim_marker(Lease(7, "b", "s2", "b2", NOW + timedelta(hours=2))),
        ],
    )
    assert current_lease(PROJECT, 7, gh, now=NOW).session == "s1"
    gh.issues[7]["comments"].append(
        _comment_row(9, "<!-- forge-release session=s1 -->", "2026-09-30T01:00:00Z")
    )
    assert current_lease(PROJECT, 7, gh, now=NOW).session == "s2"
    gh.issues[7]["comments"].append(
        _comment_row(10, "<!-- forge-release session=s2 -->", "2026-09-30T01:00:00Z")
    )
    assert current_lease(PROJECT, 7, gh) is None


def test_claim_succeeds_on_free_issue_and_writes_label_assignee_marker():
    gh = FakeGh().issue(7)
    result = claim(
        PROJECT,
        7,
        agent="cloud-routine",
        session="s1",
        branch="forge/issue-7",
        ttl_minutes=240,
        gh=gh,
        now=NOW,
        assignee="atulg4",
    )
    assert result.ok and result.lease.expires == NOW + timedelta(minutes=240)
    labels = {lbl["name"] for lbl in gh.issues[7]["labels"]}
    assert IN_PROGRESS_LABEL in labels
    assert gh.issues[7]["assignees"] == [{"login": "atulg4"}]
    assert any("forge-claim" in c["body"] for c in gh.issues[7]["comments"])


def test_claim_refuses_when_another_live_lease_exists_and_changes_nothing():
    other = format_claim_marker(
        Lease(7, "actions", "run-1", "forge/issue-7", NOW + timedelta(hours=1))
    )
    gh = FakeGh().issue(7, labels=[IN_PROGRESS_LABEL], comments=[other])
    result = claim(PROJECT, 7, agent="me", session="s9", branch="x", ttl_minutes=60, gh=gh, now=NOW)
    assert not result.ok and "run-1" in result.reason
    assert len(gh.issues[7]["comments"]) == 1
    assert not any(c[:2] == ("issue", "edit") for c in gh.calls)


def test_claim_by_the_same_session_renews_instead_of_refusing():
    mine = format_claim_marker(Lease(7, "me", "s1", "b", NOW + timedelta(minutes=5)))
    gh = FakeGh().issue(7, labels=[IN_PROGRESS_LABEL], comments=[mine])
    result = claim(PROJECT, 7, agent="me", session="s1", branch="b", ttl_minutes=60, gh=gh, now=NOW)
    assert result.ok and result.renewed and result.lease.expires == NOW + timedelta(minutes=60)


def test_expired_lease_can_be_taken_over_with_a_note():
    stale = format_claim_marker(Lease(7, "actions", "run-1", "b", NOW - timedelta(minutes=1)))
    gh = FakeGh().issue(7, labels=[IN_PROGRESS_LABEL], comments=[stale])
    result = claim(
        PROJECT, 7, agent="me", session="s2", branch="b2", ttl_minutes=60, gh=gh, now=NOW
    )
    assert result.ok and result.took_over_from == "run-1"
    assert "took over" in gh.issues[7]["comments"][-1]["body"].lower()


def test_claim_refuses_when_an_open_pr_references_the_issue():
    gh = FakeGh().issue(7)
    gh.prs = [
        {
            "number": 20,
            "url": "https://x/pr/20",
            "body": "Closes #7",
            "title": "t",
            "headRefName": "forge/issue-7",
        }
    ]
    result = claim(PROJECT, 7, agent="me", session="s1", branch="b", ttl_minutes=60, gh=gh, now=NOW)
    assert not result.ok and "pr/20" in result.reason


def test_pr_reference_must_match_the_issue_number_exactly():
    gh = FakeGh().issue(7)
    gh.prs = [
        {
            "number": 21,
            "url": "https://x/pr/21",
            "body": "Closes #70",
            "title": "t",
            "headRefName": "feat/x",
        }
    ]
    assert claim(
        PROJECT, 7, agent="me", session="s1", branch="b", ttl_minutes=60, gh=gh, now=NOW
    ).ok


def test_renew_requires_the_holding_session():
    mine = format_claim_marker(Lease(7, "me", "s1", "b", NOW + timedelta(minutes=5)))
    gh = FakeGh().issue(7, labels=[IN_PROGRESS_LABEL], comments=[mine])
    with pytest.raises(LeaseError):
        renew(PROJECT, 7, session="someone-else", ttl_minutes=60, gh=gh, now=NOW)
    lease = renew(PROJECT, 7, session="s1", ttl_minutes=60, gh=gh, now=NOW)
    assert lease.expires == NOW + timedelta(minutes=60)


def test_release_removes_label_and_marks_release():
    mine = format_claim_marker(Lease(7, "me", "s1", "b", NOW + timedelta(minutes=5)))
    gh = FakeGh().issue(7, labels=[IN_PROGRESS_LABEL], comments=[mine])
    release(PROJECT, 7, session="s1", gh=gh)
    assert not any(lbl["name"] == IN_PROGRESS_LABEL for lbl in gh.issues[7]["labels"])
    assert current_lease(PROJECT, 7, gh) is None


def test_release_by_non_holder_is_refused_unless_forced():
    mine = format_claim_marker(Lease(7, "me", "s1", "b", NOW + timedelta(minutes=5)))
    gh = FakeGh().issue(7, labels=[IN_PROGRESS_LABEL], comments=[mine])
    with pytest.raises(LeaseError):  # a LIVE holder (pinned clock: the lease is live at NOW)
        release(PROJECT, 7, session="s2", gh=gh, now=NOW)
    release(PROJECT, 7, session="s2", gh=gh, force=True, now=NOW)
    assert current_lease(PROJECT, 7, gh, now=NOW) is None


def test_list_claims_reports_live_and_expired():
    gh = FakeGh()
    gh.issue(
        1,
        labels=[IN_PROGRESS_LABEL],
        comments=[format_claim_marker(Lease(1, "a", "s1", "b", NOW + timedelta(hours=1)))],
    )
    gh.issue(
        2,
        labels=[IN_PROGRESS_LABEL],
        comments=[format_claim_marker(Lease(2, "b", "s2", "b", NOW - timedelta(hours=1)))],
    )
    gh.issue(3, labels=["bug"])
    rows = list_claims(PROJECT, gh, now=NOW)
    assert [(r.lease.issue, r.expired) for r in rows] == [(1, False), (2, True)]


# ---------------------------------------------------------------- review follow-ups


def test_lease_history_spanning_several_api_pages_is_read():
    chatter = [f"comment {i}" for i in range(PAGE + 5)]
    mine = format_claim_marker(Lease(7, "me", "s1", "b", NOW + timedelta(hours=1)))
    gh = FakeGh().issue(7, labels=[IN_PROGRESS_LABEL], comments=[*chatter, mine])
    assert current_lease(PROJECT, 7, gh).session == "s1"
    assert [(r.lease.issue, r.expired) for r in list_claims(PROJECT, gh, now=NOW)] == [(7, False)]


def test_markers_from_untrusted_commenters_are_ignored():
    live = format_claim_marker(Lease(7, "me", "s1", "b", NOW + timedelta(hours=1)))
    gh = FakeGh().issue(7, labels=[IN_PROGRESS_LABEL], comments=[live])
    forged_release = _comment_row(
        50, "<!-- forge-release session=s1 -->", "2026-09-30T01:00:00Z", "rando", "User", "NONE"
    )
    gh.issues[7]["comments"].append(forged_release)
    assert current_lease(PROJECT, 7, gh).session == "s1"  # forged release does not end it

    forged_claim = format_claim_marker(Lease(8, "x", "evil", "b", NOW + timedelta(days=3650)))
    gh.issue(8)
    gh.issues[8]["comments"].append(
        _comment_row(51, forged_claim, "2026-09-30T01:00:00Z", "rando", "User", "CONTRIBUTOR")
    )
    assert current_lease(PROJECT, 8, gh) is None
    assert claim(PROJECT, 8, agent="me", session="s2", branch="b", gh=gh, now=NOW).ok


def test_markers_from_the_configured_publisher_bot_are_trusted(monkeypatch):
    """Codex 4242753917: the Publisher bot counts when named exactly -- by the policy's
    `publisher_bot_login` (`use_policy_bot_logins`) or FORGE_LEASE_BOT_LOGINS -- and an App
    bot whose slug merely looks like Forge's never does."""
    from agentic_sdlc.leases import LEASE_BOT_LOGINS_ENV, use_policy_bot_logins

    monkeypatch.delenv(LEASE_BOT_LOGINS_ENV, raising=False)
    bot_claim = format_claim_marker(
        Lease(7, "forge-actions", "run-1", "b", NOW + timedelta(hours=1))
    )
    gh = FakeGh().issue(7)
    gh.issues[7]["comments"].append(
        _comment_row(
            60, bot_claim, "2026-09-30T01:00:00Z", "agentic-sdlc-publisher[bot]", "Bot", "NONE"
        )
    )
    try:
        use_policy_bot_logins("")
        assert current_lease(PROJECT, 7, gh, now=NOW) is None  # unset: fail closed
        use_policy_bot_logins("agentic-sdlc-evil[bot]")
        assert current_lease(PROJECT, 7, gh, now=NOW) is None  # a look-alike slug
        use_policy_bot_logins("agentic-sdlc-publisher[bot]")
        assert current_lease(PROJECT, 7, gh, now=NOW).session == "run-1"
        monkeypatch.setenv(LEASE_BOT_LOGINS_ENV, "other[bot]")  # the variable wins
        assert current_lease(PROJECT, 7, gh, now=NOW) is None
    finally:
        use_policy_bot_logins("")


def test_accepted_expiry_is_bounded_by_the_maximum_ttl():
    far = format_claim_marker(Lease(7, "me", "s1", "b", NOW + timedelta(days=3650)))
    gh = FakeGh().issue(7, labels=[IN_PROGRESS_LABEL], comments=[far])
    lease = current_lease(PROJECT, 7, gh)
    posted = datetime(2026, 9, 30, tzinfo=UTC)
    assert lease.expires == posted + timedelta(minutes=MAX_TTL_MINUTES)
    with pytest.raises(LeaseError):
        claim(
            PROJECT,
            9,
            agent="me",
            session="s",
            branch="b",
            gh=gh,
            now=NOW,
            ttl_minutes=MAX_TTL_MINUTES + 1,
        )


def test_invalid_claim_fields_are_refused_before_any_github_edit():
    gh = FakeGh().issue(7)
    with pytest.raises(LeaseError):
        claim(PROJECT, 7, agent="Claude Code", session="s1", branch="b", gh=gh, now=NOW)
    assert not any(c[:2] == ("issue", "edit") for c in gh.calls)
    assert gh.issues[7]["labels"] == [] and gh.issues[7]["comments"] == []


def test_cli_rejects_an_explicit_zero_ttl(monkeypatch):
    import argparse

    from agentic_sdlc import cli

    args = argparse.Namespace(ttl_minutes=0, config=None)
    assert cli._lease_ttl(args) == 0
    gh = FakeGh().issue(7)
    monkeypatch.setattr(cli, "run_gh", gh)
    assert (
        cli.main(
            [
                "claim",
                "--project",
                PROJECT,
                "--issue",
                "7",
                "--agent",
                "me",
                "--session",
                "s1",
                "--branch",
                "b",
                "--ttl-minutes",
                "0",
            ]
        )
        != 0
    )
    assert gh.issues[7]["comments"] == [] and gh.issues[7]["labels"] == []


def test_release_marker_is_posted_before_the_label_is_removed():
    mine = format_claim_marker(Lease(7, "me", "s1", "b", NOW + timedelta(minutes=5)))
    gh = FakeGh().issue(7, labels=[IN_PROGRESS_LABEL], comments=[mine])
    real = gh.__call__

    def failing_label(args, input=None):
        if tuple(args[:2]) == ("issue", "edit") and "--remove-label" in args:
            raise RuntimeError("label edit failed")
        return real(args, input=input)

    with pytest.raises(RuntimeError):
        release(PROJECT, 7, session="s1", gh=failing_label)
    assert current_lease(PROJECT, 7, gh) is None  # the lease is ended even though cleanup failed


def test_claim_refuses_closed_issues_and_pull_requests_without_writing():
    gh = FakeGh().issue(7).issue(8)
    gh.issues[7]["state"] = "closed"
    gh.issues[8]["extra"] = {"pull_request": {"url": "https://x/pulls/8"}}
    for number, word in ((7, "closed"), (8, "pull request")):
        result = claim(PROJECT, number, agent="me", session="s1", branch="b", gh=gh, now=NOW)
        assert not result.ok and word in result.reason
    assert not any(c[:2] == ("issue", "edit") for c in gh.calls)
    assert gh.issues[7]["comments"] == [] and gh.issues[8]["comments"] == []


def test_cli_renew_and_release_honor_output(tmp_path, monkeypatch):
    from agentic_sdlc import cli

    wall = datetime.now(UTC)  # the CLI runs on the wall clock, and renew refuses expired leases
    mine = format_claim_marker(Lease(7, "me", "s1", "b", wall + timedelta(hours=1)))
    gh = FakeGh().issue(7, labels=[IN_PROGRESS_LABEL])
    gh.posted_at = wall
    gh.issues[7]["comments"].append(_comment_row(1, mine, wall.isoformat()))
    monkeypatch.setattr(cli, "run_gh", gh)
    base = ["--project", PROJECT, "--issue", "7", "--session", "s1"]
    out = tmp_path / "renew.json"
    assert cli.main(["renew", *base, "--ttl-minutes", "60", "--output", str(out)]) == 0
    assert json.loads(out.read_text())["lease"]["session"] == "s1"
    out = tmp_path / "release.json"
    assert cli.main(["release", *base, "--output", str(out)]) == 0
    assert json.loads(out.read_text()) == {"issue": 7, "released": True, "session": "s1"}


def test_claim_loses_the_race_when_a_rival_marker_lands_first_and_retracts():
    gh = FakeGh().issue(7)

    def rival_first(n):
        gh._next_comment += 1
        gh.issues[n]["comments"].append(
            _comment_row(
                gh._next_comment,
                format_claim_marker(
                    Lease(7, "cloud-routine", "r1", "forge/issue-7", NOW + timedelta(hours=1))
                ),
                NOW.isoformat(),
            )
        )

    gh.before_post = rival_first
    result = claim(PROJECT, 7, agent="me", session="s1", branch="b", ttl_minutes=60, gh=gh, now=NOW)
    assert not result.ok and "r1" in result.reason
    bodies = [c["body"] for c in gh.issues[7]["comments"]]
    assert any("forge-release session=s1" in b for b in bodies)  # we retracted
    assert not any(c[:2] == ("issue", "edit") for c in gh.calls)  # never touched label/assignee
    assert current_lease(PROJECT, 7, gh, now=NOW).session == "r1"


def test_claim_wins_the_race_when_it_posted_first():
    gh = FakeGh().issue(7)
    result = claim(PROJECT, 7, agent="me", session="s1", branch="b", ttl_minutes=60, gh=gh, now=NOW)
    assert result.ok
    assert (
        "label",
        "create",
        IN_PROGRESS_LABEL,
        "--repo",
        PROJECT,
        "--color",
        "fbca04",
        "--description",
        "Leased: an agent/session is actively implementing this",
        "--force",
    ) in gh.calls
    assert any(lbl["name"] == IN_PROGRESS_LABEL for lbl in gh.issues[7]["labels"])


def test_open_pr_detection_needs_a_closing_keyword_and_accepts_qualified_forms():
    from agentic_sdlc.leases import open_prs_for_issue

    gh = FakeGh().issue(7)
    gh.prs = [
        {"number": 1, "url": "u1", "title": "t", "body": "Depends on #7", "headRefName": "feat/x"},
        {
            "number": 2,
            "url": "u2",
            "title": "t",
            "body": "Closes owner/repo#7",
            "headRefName": "feat/y",
        },
        {
            "number": 3,
            "url": "u3",
            "title": "t",
            "body": "Fixes https://github.com/owner/repo/issues/7",
            "headRefName": "z",
        },
        {
            "number": 4,
            "url": "u4",
            "title": "t",
            "body": "Resolves other/repo#7",
            "headRefName": "q",
        },
        {
            "number": 5,
            "url": "u5",
            "title": "t",
            "body": "",
            "headRefName": "forge/issue-7",
            **_SAME_REPO_HEAD,
        },
        {"number": 6, "url": "u6", "title": "t", "body": "closes #70", "headRefName": "w"},
    ]
    assert [p["number"] for p in open_prs_for_issue(PROJECT, 7, gh)] == [2, 3, 5]


_SAME_REPO_HEAD = {
    "isCrossRepository": False,
    "headRepository": {"name": "repo"},
    "headRepositoryOwner": {"login": "owner"},
}


def _fork_pr(number, head="forge/issue-7", body="", **fields):
    row = {
        "number": number,
        "url": f"https://x/pr/{number}",
        "title": "t",
        "body": body,
        "headRefName": head,
        "isCrossRepository": True,
        "headRepository": {"name": "repo"},
        "headRepositoryOwner": {"login": "mallory"},
    }
    row.update(fields)
    return row


def test_a_fork_pr_named_after_the_issue_does_not_block_claims():
    """Codex 4239667540: a fork PR from `forge/issue-7` is not durable work for issue 7."""
    gh = FakeGh().issue(7)
    gh.prs = [_fork_pr(30)]
    result = claim(PROJECT, 7, agent="me", session="s1", branch="b", ttl_minutes=60, gh=gh, now=NOW)
    assert result.ok, result.reason


def test_pr_list_requests_the_head_repository_fields():
    from agentic_sdlc.leases import open_prs_for_issue

    gh = FakeGh().issue(7)
    open_prs_for_issue(PROJECT, 7, gh)
    pr_calls = [c for c in gh.calls if c[:2] == ("api", "graphql")]
    assert pr_calls
    query = next(a for a in pr_calls[-1] if a.startswith("query="))
    for field in ("headRepository", "headRepositoryOwner", "isCrossRepository"):
        assert field in query


@pytest.mark.parametrize(
    "fields",
    [
        {},  # the fork's own repository
        {"isCrossRepository": None},
        {"isCrossRepository": "false"},
        {"isCrossRepository": False, "headRepositoryOwner": {"login": "mallory"}},
        {"isCrossRepository": False, "headRepository": {"name": "other"}},
        {"isCrossRepository": False, "headRepository": None},
        {"isCrossRepository": False, "headRepositoryOwner": "owner"},
        {"isCrossRepository": False, "headRepository": {"name": ""}},
        {
            "isCrossRepository": False,
            "headRefName": None,
            "headRepositoryOwner": {"login": "owner"},
        },
    ],
)
def test_branch_name_match_needs_a_provably_same_repository_head(fields):
    from agentic_sdlc.leases import open_prs_for_issue

    gh = FakeGh().issue(7)
    gh.prs = [_fork_pr(31, **fields)]
    assert open_prs_for_issue(PROJECT, 7, gh) == []


def test_same_repository_branch_match_is_case_insensitive():
    from agentic_sdlc.leases import open_prs_for_issue

    gh = FakeGh().issue(7)
    gh.prs = [
        _fork_pr(
            32,
            isCrossRepository=False,
            headRepository={"name": "REPO"},
            headRepositoryOwner={"login": "Owner"},
        )
    ]
    assert [p["number"] for p in open_prs_for_issue(PROJECT, 7, gh)] == [32]
    result = claim(PROJECT, 7, agent="me", session="s1", branch="b", ttl_minutes=60, gh=gh, now=NOW)
    assert not result.ok and "pr/32" in result.reason


def test_a_fork_pr_that_explicitly_closes_the_issue_still_blocks_claims():
    gh = FakeGh().issue(7)
    gh.prs = [_fork_pr(33, head="patch-1", body="Closes #7")]
    result = claim(PROJECT, 7, agent="me", session="s1", branch="b", ttl_minutes=60, gh=gh, now=NOW)
    assert not result.ok and "pr/33" in result.reason


# ------------------------------------------------------- every lease mutation is arbitrated


def _rival_claim(gh, session="r1", expires=NOW + timedelta(hours=1), label=False):
    def land(n):
        gh._next_comment += 1
        gh.issues[n]["comments"].append(
            _comment_row(
                gh._next_comment,
                format_claim_marker(Lease(n, "cloud-routine", session, "forge/x", expires)),
                gh.posted_at.isoformat(),
            )
        )
        if label:
            gh.issues[n]["labels"].append({"name": IN_PROGRESS_LABEL})

    return land


def test_renew_refuses_an_expired_lease_without_posting():
    stale = format_claim_marker(Lease(7, "me", "s1", "b", NOW - timedelta(minutes=1)))
    gh = FakeGh().issue(7, labels=[IN_PROGRESS_LABEL], comments=[stale])
    with pytest.raises(LeaseError, match="expired"):
        renew(PROJECT, 7, session="s1", ttl_minutes=60, gh=gh, now=NOW)
    assert len(gh.issues[7]["comments"]) == 1


def test_a_lapsed_session_cannot_regain_seniority_over_a_takeover():
    """Old session's lease lapses, a new claimant takes over, then the old marker lands late."""
    gh = FakeGh().issue(7)
    gh.issues[7]["comments"] += [
        _comment_row(
            1,
            format_claim_marker(Lease(7, "a", "old", "b", NOW - timedelta(minutes=30))),
            "2026-09-30T08:00:00Z",
        ),
        _comment_row(
            2,
            format_claim_marker(Lease(7, "b", "new", "b", NOW + timedelta(hours=1))),
            "2026-09-30T11:45:00Z",
        ),
        _comment_row(
            3,
            format_claim_marker(Lease(7, "a", "old", "b", NOW + timedelta(hours=2))),
            "2026-09-30T11:50:00Z",
        ),
    ]
    assert current_lease(PROJECT, 7, gh, now=NOW).session == "new"


def test_renewal_that_lands_after_expiry_loses_to_the_takeover_and_retracts():
    read_at = NOW - timedelta(minutes=2)  # the lease is still live when renew() reads it ...
    mine = format_claim_marker(Lease(7, "me", "old", "b", NOW - timedelta(minutes=1)))
    gh = FakeGh().issue(7, labels=[IN_PROGRESS_LABEL], comments=[mine])
    gh.before_post = _rival_claim(gh, "new")  # ... but a takeover lands before the renewal
    with pytest.raises(LeaseError, match="new"):
        renew(PROJECT, 7, session="old", ttl_minutes=60, gh=gh, now=read_at)
    assert "forge-release session=old" in gh.issues[7]["comments"][-1]["body"]
    assert current_lease(PROJECT, 7, gh, now=read_at).session == "new"


def test_claim_retracts_its_marker_when_label_bookkeeping_fails():
    gh = FakeGh().issue(7)
    real = gh.__call__

    def failing_assignee(args, input=None):
        if tuple(args[:2]) == ("issue", "edit") and "--add-assignee" in args:
            raise RuntimeError("invalid assignee")
        return real(args, input=input)

    with pytest.raises(RuntimeError):
        claim(
            PROJECT,
            7,
            agent="me",
            session="s1",
            branch="b",
            gh=failing_assignee,
            now=NOW,
            assignee="nobody",
        )
    bodies = [c["body"] for c in gh.issues[7]["comments"]]
    # the add never happened (no `assigned` event by us): nothing is owned, nothing to clean up
    assert "forge-release session=s1" in bodies[-1]
    assert not any("forge-cleanup" in body for body in bodies)
    assert _logins(gh) == []
    assert current_lease(PROJECT, 7, gh, now=NOW) is None  # not blocked for the TTL


def test_release_keeps_the_label_of_a_claimant_that_arrived_before_cleanup():
    mine = format_claim_marker(Lease(7, "me", "s1", "b", NOW + timedelta(minutes=5)))
    gh = FakeGh().issue(7, labels=[IN_PROGRESS_LABEL], comments=[mine])
    gh.before_post = _rival_claim(gh, "r1", label=True)
    release(PROJECT, 7, session="s1", gh=gh, now=NOW)
    assert current_lease(PROJECT, 7, gh, now=NOW).session == "r1"
    assert any(lbl["name"] == IN_PROGRESS_LABEL for lbl in gh.issues[7]["labels"])


def test_release_restores_the_label_when_a_claimant_lands_during_removal():
    mine = format_claim_marker(Lease(7, "me", "s1", "b", NOW + timedelta(minutes=5)))
    gh = FakeGh().issue(7, labels=[IN_PROGRESS_LABEL], comments=[mine])
    real = gh.__call__
    land = _rival_claim(gh, "r1", label=True)
    fired = []

    def interleaved(args, input=None):
        if tuple(args[:2]) == ("issue", "edit") and "--remove-label" in args and not fired:
            fired.append(True)
            land(7)  # the rival claims and labels just before our stale removal executes
        return real(args, input=input)

    release(PROJECT, 7, session="s1", gh=interleaved, now=NOW)
    assert current_lease(PROJECT, 7, gh, now=NOW).session == "r1"
    assert any(lbl["name"] == IN_PROGRESS_LABEL for lbl in gh.issues[7]["labels"])


# ---------------------------------------------------------------- lease-owned assignees
# Codex review 4204434971: release removed the label but left the assignee the claim added.


def _logins(gh, n=7):
    return [a["login"] for a in gh.issues[n]["assignees"]]


def test_release_removes_the_assignee_the_lease_added():
    gh = FakeGh().issue(7)
    assert claim(
        PROJECT, 7, agent="a", session="s1", branch="b", gh=gh, now=NOW, assignee="alice"
    ).ok
    assert "assignee=alice owns_assignee=1" in gh.issues[7]["comments"][-1]["body"]
    assert _logins(gh) == ["alice"]
    release(PROJECT, 7, session="s1", gh=gh, now=NOW)
    assert _logins(gh) == []
    assert not any(lbl["name"] == IN_PROGRESS_LABEL for lbl in gh.issues[7]["labels"])


def test_release_keeps_a_pre_existing_assignee():
    gh = FakeGh().issue(7, assignees=["alice", "bob"])
    assert claim(
        PROJECT, 7, agent="a", session="s1", branch="b", gh=gh, now=NOW, assignee="alice"
    ).ok
    assert "owns_assignee" not in gh.issues[7]["comments"][-1]["body"]
    release(PROJECT, 7, session="s1", gh=gh, now=NOW)
    assert _logins(gh) == ["alice", "bob"]
    assert not any("--remove-assignee" in c for c in gh.calls)


def test_renewal_keeps_assignee_ownership_until_release():
    gh = FakeGh().issue(7)
    claim(PROJECT, 7, agent="a", session="s1", branch="b", gh=gh, now=NOW, assignee="alice")
    renew(PROJECT, 7, session="s1", ttl_minutes=60, gh=gh, now=NOW)
    assert "assignee=alice owns_assignee=1" in gh.issues[7]["comments"][-1]["body"]
    claim(PROJECT, 7, agent="a", session="s1", branch="b", gh=gh, now=NOW, assignee="alice")
    assert current_lease(PROJECT, 7, gh, now=NOW).owns_assignee
    release(PROJECT, 7, session="s1", gh=gh, now=NOW)
    assert _logins(gh) == []


def test_takeover_drops_the_expired_leases_assignee_and_inherits_a_shared_one():
    stale = Lease(7, "a", "old", "b", NOW - timedelta(minutes=1), "alice", True)
    gh = FakeGh().issue(7, assignees=["alice"], comments=[format_claim_marker(stale)])
    assert claim(
        PROJECT, 7, agent="a", session="new", branch="b", gh=gh, now=NOW, assignee="bob"
    ).ok
    assert _logins(gh) == ["bob"]  # no accumulation across takeovers

    gh = FakeGh().issue(7, assignees=["alice"], comments=[format_claim_marker(stale)])
    assert claim(
        PROJECT, 7, agent="a", session="new", branch="b", gh=gh, now=NOW, assignee="alice"
    ).ok
    assert current_lease(PROJECT, 7, gh, now=NOW).owns_assignee  # inherited, not pre-existing
    release(PROJECT, 7, session="new", gh=gh, now=NOW)
    assert _logins(gh) == []


def test_release_keeps_an_assignee_a_new_live_lease_wants():
    mine = Lease(7, "me", "s1", "b", NOW + timedelta(minutes=5), "alice", True)
    gh = FakeGh().issue(
        7, labels=[IN_PROGRESS_LABEL], assignees=["alice"], comments=[format_claim_marker(mine)]
    )
    real = gh.__call__
    fired = []

    def interleaved(args, input=None):
        if tuple(args[:2]) == ("issue", "edit") and "--remove-assignee" in args and not fired:
            fired.append(True)
            gh._next_comment += 1
            rival = Lease(7, "x", "r1", "forge/x", NOW + timedelta(hours=1), "alice", False)
            gh.issues[7]["comments"].append(
                _comment_row(gh._next_comment, format_claim_marker(rival), NOW.isoformat())
            )
        return real(args, input=input)

    release(PROJECT, 7, session="s1", gh=interleaved, now=NOW)
    assert current_lease(PROJECT, 7, gh, now=NOW).session == "r1"
    assert _logins(gh) == ["alice"]  # restored for the claimant that landed during removal


def test_marker_round_trips_assignee_ownership():
    lease = Lease(7, "a", "s", "b", NOW + timedelta(hours=1), "alice", True)
    assert parse_marker(format_claim_marker(lease), issue=7) == lease
    plain = Lease(7, "a", "s", "b", NOW + timedelta(hours=1))
    assert "assignee" not in format_claim_marker(plain)
    with pytest.raises(LeaseError):
        format_claim_marker(replace(lease, assignee="bad login"))


# Codex review 4204654794: the assignee cleanup must be retryable after the release marker.


def test_retried_release_drops_the_owned_assignee_after_a_failed_cleanup():
    gh = FakeGh().issue(7)
    assert claim(
        PROJECT, 7, agent="a", session="s1", branch="b", gh=gh, now=NOW, assignee="alice"
    ).ok
    real = gh.__call__

    def flaky(args, input=None):
        if tuple(args[:2]) == ("issue", "edit") and "--remove-assignee" in args:
            raise RuntimeError("transient GitHub failure")
        return real(args, input=input)

    with pytest.raises(RuntimeError):
        release(PROJECT, 7, session="s1", gh=flaky, now=NOW)
    assert current_lease(PROJECT, 7, gh, now=NOW) is None  # the marker landed
    assert _logins(gh) == ["alice"]
    release(PROJECT, 7, session="s1", gh=gh, now=NOW)  # the retry finishes the cleanup
    assert _logins(gh) == []
    removals = sum("--remove-assignee" in c for c in gh.calls)
    release(PROJECT, 7, session="s1", gh=gh, now=NOW)  # idempotent: nothing left to remove
    assert sum("--remove-assignee" in c for c in gh.calls) == removals


def test_retried_release_keeps_a_pre_existing_or_newly_wanted_assignee():
    gh = FakeGh().issue(7, assignees=["alice"])
    claim(PROJECT, 7, agent="a", session="s1", branch="b", gh=gh, now=NOW, assignee="alice")
    release(PROJECT, 7, session="s1", gh=gh, now=NOW)
    release(PROJECT, 7, session="s1", gh=gh, now=NOW)
    assert _logins(gh) == ["alice"]  # never owned: untouched however often release runs

    gh = FakeGh().issue(7)
    claim(PROJECT, 7, agent="a", session="s1", branch="b", gh=gh, now=NOW, assignee="alice")
    gh.issues[7]["comments"].append(
        _comment_row(50, "<!-- forge-release session=s1 -->", NOW.isoformat())
    )
    later = Lease(7, "x", "s2", "b", NOW + timedelta(hours=1), "alice", False)
    gh.issues[7]["comments"].append(_comment_row(51, format_claim_marker(later), NOW.isoformat()))
    release(PROJECT, 7, session="s2", gh=gh, now=NOW)
    # s2 never owned alice, but s1 did and never recorded its cleanup: once no live lease wants
    # alice, the reconciliation of every ended lease (Codex 4205123792) finishes s1's.
    assert _logins(gh) == []


# ---------------------------------------------------------------- marker authority


def test_markers_need_write_permission_not_an_association_label():
    live = format_claim_marker(Lease(7, "me", "s1", "b", NOW + timedelta(hours=1)))
    gh = FakeGh().issue(7, labels=[IN_PROGRESS_LABEL], comments=[live])
    gh.permissions.update({"reader": "read", "triager": "triage"})
    # a read-only organization member / collaborator carries MEMBER / COLLABORATOR
    for cid, (login, association) in enumerate(
        [("reader", "MEMBER"), ("triager", "COLLABORATOR"), ("outsider", "MEMBER")], start=70
    ):
        gh.issues[7]["comments"].append(
            _comment_row(
                cid,
                "<!-- forge-release session=s1 -->",
                "2026-09-30T01:00:00Z",
                login,
                "User",
                association,
            )
        )
    assert current_lease(PROJECT, 7, gh, now=NOW).session == "s1"
    blocker = format_claim_marker(Lease(8, "x", "squat", "b", NOW + timedelta(days=7)))
    gh.issue(8)
    gh.issues[8]["comments"].append(
        _comment_row(80, blocker, "2026-09-30T01:00:00Z", "reader", "User", "MEMBER")
    )
    assert current_lease(PROJECT, 8, gh, now=NOW) is None
    assert claim(PROJECT, 8, agent="me", session="s2", branch="b", gh=gh, now=NOW).ok


def test_maintainers_and_writers_markers_count_and_permissions_are_read_once():
    gh = FakeGh().issue(7)
    gh.permissions.update({"maint": "maintain", "writer": "write"})
    claim_ = format_claim_marker(Lease(7, "me", "s1", "b", NOW + timedelta(hours=1)))
    gh.issues[7]["comments"] += [
        _comment_row(90, claim_, "2026-09-30T01:00:00Z", "maint", "User", "NONE"),
        _comment_row(91, "<!-- forge-release session=s1 -->", "2026-09-30T01:01:00Z", "writer"),
    ]
    assert current_lease(PROJECT, 7, gh, now=NOW) is None  # the writer's release counts
    gh.issues[7]["comments"].pop()
    assert current_lease(PROJECT, 7, gh, now=NOW).session == "s1"
    reads = [c for c in gh.calls if "collaborators/maint/permission" in c[1]]
    assert len(reads) == 1  # cached for the run


def test_an_unreadable_permission_refuses_the_lease_read():
    """Codex 4239397972: a failed permission read of a marker author is not "no marker". It used
    to drop the holder's claim, so a rival whose own read worked (or the Actions bot, which needs
    none) posted a marker and won while the holder kept working: two agents on one issue."""
    gh = FakeGh().issue(7)
    real = gh.__call__

    def broken(args, input=None):
        if "/collaborators/" in args[1]:
            raise RuntimeError("HTTP 502")
        return real(args, input=input)

    marker = format_claim_marker(Lease(7, "me", "s1", "b", NOW + timedelta(hours=1)))
    gh.issues[7]["comments"].append(_comment_row(95, marker, "2026-09-30T01:00:00Z"))
    before = len(gh.issues[7]["comments"])
    with pytest.raises(LeaseError, match="cannot read the repository permission"):
        current_lease(PROJECT, 7, broken, now=NOW)
    with pytest.raises(LeaseError):
        claim(PROJECT, 7, agent="me", session="s9", branch="b", gh=broken, now=NOW)
    assert len(gh.issues[7]["comments"]) == before  # refused before posting any marker

    # A bot rival needs no permission read, but the holder's marker still has to be classified.
    bot = format_claim_marker(Lease(7, "bot", "s2", "b", NOW + timedelta(hours=1)))
    gh.issues[7]["comments"].append(
        _comment_row(96, bot, "2026-09-30T01:05:00Z", "github-actions[bot]", "Bot", "NONE")
    )
    with pytest.raises(LeaseError):
        current_lease(PROJECT, 7, broken, now=NOW)

    # Only a definitive answer classifies: a 404 is "no access" (untrusted, marker ignored) ...
    gone = FakeGh().issue(7)
    gone.issues[7]["comments"].append(
        _comment_row(95, marker, "2026-09-30T01:00:00Z", "stranger", "User", "NONE")
    )
    assert current_lease(PROJECT, 7, gone, now=NOW) is None
    # ... and a 200 that carries no permission is as unreadable as a failure.
    blank = FakeGh().issue(7)
    blank.issues[7]["comments"].append(_comment_row(95, marker, "2026-09-30T01:00:00Z"))
    blank_real = blank.__call__

    def shapeless(args, input=None):
        if "/collaborators/" in args[1]:
            return "{}"
        return blank_real(args, input=input)

    with pytest.raises(LeaseError, match="no permission"):
        current_lease(PROJECT, 7, shapeless, now=NOW)


@pytest.mark.parametrize(
    ("login", "kind", "configured", "trusted"),
    [
        ("github-actions[bot]", "Bot", "", True),
        ("dependabot[bot]", "Bot", "", False),
        ("claude[bot]", "Bot", "", False),
        ("agentic-sdlc-publisher[bot]", "Bot", "my-forge[bot]", False),
        # Codex 4242753917: nothing configured trusts no App bot, however Forge-like its slug
        ("agentic-sdlc-publisher[bot]", "Bot", "", False),
        ("agentic-sdlc[bot]", "Bot", "", False),
        ("my-forge[bot]", "Bot", "my-forge[bot]", True),
        ("github-actions[bot]", "Bot", "my-forge[bot]", True),
        ("agentic-sdlc-publisher[bot]", "User", "", False),  # not actually an App
    ],
)
def test_only_the_actions_and_publisher_bots_may_post_markers(
    login, kind, configured, trusted, monkeypatch
):
    from agentic_sdlc.leases import LEASE_BOT_LOGINS_ENV

    monkeypatch.setenv(LEASE_BOT_LOGINS_ENV, configured)
    marker = format_claim_marker(Lease(7, "forge-actions", "run-1", "b", NOW + timedelta(hours=1)))
    gh = FakeGh().issue(7)
    gh.issues[7]["comments"].append(
        _comment_row(60, marker, "2026-09-30T01:00:00Z", login, kind, "NONE")
    )
    found = current_lease(PROJECT, 7, gh, now=NOW)
    assert (found is not None) is trusted


def _guard():
    import importlib.util
    import tempfile
    from pathlib import Path

    from agentic_sdlc.onboard import OnboardSpec, render_hooks

    text = render_hooks(
        OnboardSpec(
            project_id=PROJECT,
            platform_repository="owner/agentic-sdlc",
            platform_ref="e" * 40,
            test_command="pytest",
        )
    )[".claude/hooks/forge_commit_guard.py"]
    path = Path(tempfile.mkdtemp()) / "guard_parity.py"
    path.write_text(text)
    spec = importlib.util.spec_from_file_location("guard_parity", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._TRUSTED["gh"] = "gh"  # the test's fake subprocess.run answers gh (Codex 4242095556)
    module._GH_STATE["token"] = ""
    return module


def test_the_commit_guard_and_leases_make_identical_trust_and_lease_decisions(monkeypatch):
    from agentic_sdlc import leases

    guard = _guard()
    for name in (
        "WRITE_PERMISSIONS",
        "GITHUB_ACTIONS_BOT",
        "LEASE_BOT_LOGINS_ENV",
        "MAX_TTL_MINUTES",
    ):
        assert getattr(guard, name) == getattr(leases, name), name
    permissions = {"admin1": "admin", "maint": "maintain", "w": "write", "r": "read", "t": "triage"}
    later = (NOW + timedelta(hours=2)).isoformat()
    authors = [
        ("admin1", "User", "OWNER"),
        ("maint", "User", "MEMBER"),
        ("w", "User", "COLLABORATOR"),
        ("r", "User", "MEMBER"),
        ("t", "User", "COLLABORATOR"),
        ("unknown", "User", "MEMBER"),  # permission unreadable
        ("bad/login", "User", "OWNER"),
        ("github-actions[bot]", "Bot", "NONE"),
        ("agentic-sdlc-publisher[bot]", "Bot", "NONE"),
        ("dependabot[bot]", "Bot", "NONE"),
        ("fake[bot]", "User", "NONE"),
        ("", "User", "OWNER"),
    ]
    for configured in ("", "agentic-sdlc-publisher[bot]", "other[bot]"):
        monkeypatch.setenv(leases.LEASE_BOT_LOGINS_ENV, configured)
        for login, kind, association in authors:
            row = _comment_row(1, "", later, login, kind, association)
            assert leases.trusted_marker_author(
                row, permissions.get
            ) == guard.trusted_marker_author(row, permissions.get), (login, configured)
        # and the lease each side derives from the same thread
        for claimant, releaser in [(a[0], b[0]) for a in authors for b in authors[:4]]:
            gh = FakeGh().issue(7)
            gh.permissions = dict(permissions)
            claim_ = format_claim_marker(Lease(7, "a", "s1", "b", NOW + timedelta(days=1)))
            rows = [
                _comment_row(1, claim_, NOW.isoformat(), claimant, *_kind(authors, claimant)),
                _comment_row(
                    2,
                    "<!-- forge-release session=s1 -->",
                    NOW.isoformat(),
                    releaser,
                    *_kind(authors, releaser),
                ),
            ]
            gh.issues[7]["comments"] = rows
            mine = leases.current_lease(PROJECT, 7, gh, now=NOW)
            theirs = guard.lease_from_comments(rows, permissions.get, now=NOW)
            assert (mine.session if mine and mine.live(NOW) else None) == (
                theirs["session"] if theirs else None
            ), (claimant, releaser, configured)


def _kind(authors, login):
    return next((kind, association) for name, kind, association in authors if name == login)


# ---------------------------------------------------------------- idempotent assignee cleanup


def test_a_retried_release_never_removes_a_later_human_reassignment():
    gh = FakeGh().issue(7)
    assert claim(
        PROJECT, 7, agent="a", session="s1", branch="b", gh=gh, now=NOW, assignee="alice"
    ).ok
    release(PROJECT, 7, session="s1", gh=gh, now=NOW)
    assert _logins(gh) == []
    assert gh.issues[7]["comments"][-1]["body"] == (
        "<!-- forge-cleanup session=s1 assignee=alice -->"
    )
    gh.posted_at = NOW + timedelta(minutes=5)
    gh(["issue", "edit", "7", "--repo", PROJECT, "--add-assignee", "alice"])  # a maintainer
    release(PROJECT, 7, session="s1", gh=gh, now=NOW + timedelta(minutes=6))  # retry
    assert _logins(gh) == ["alice"]


def test_a_retry_after_a_failed_cleanup_skips_a_login_reassigned_after_the_release():
    gh = FakeGh().issue(7)
    assert claim(
        PROJECT, 7, agent="a", session="s1", branch="b", gh=gh, now=NOW, assignee="alice"
    ).ok
    real = gh.__call__

    def flaky(args, input=None):
        if tuple(args[:2]) == ("issue", "edit") and "--remove-assignee" in args:
            raise RuntimeError("transient GitHub failure")
        return real(args, input=input)

    with pytest.raises(RuntimeError):
        release(PROJECT, 7, session="s1", gh=flaky, now=NOW)
    # a maintainer assigns alice again (still listed, since the removal failed) after the release
    gh.events[7].append(
        {
            "event": "assigned",
            "assignee": {"login": "alice"},
            "created_at": (NOW + timedelta(minutes=3)).isoformat(),
        }
    )
    before = len(gh.calls)
    release(PROJECT, 7, session="s1", gh=gh, now=NOW + timedelta(minutes=4))
    assert _logins(gh) == ["alice"]
    assert not any("--remove-assignee" in c for c in gh.calls[before:])


def test_a_retry_records_the_cleanup_it_completes():
    gh = FakeGh().issue(7)
    assert claim(
        PROJECT, 7, agent="a", session="s1", branch="b", gh=gh, now=NOW, assignee="alice"
    ).ok
    real = gh.__call__

    def flaky(args, input=None):
        if tuple(args[:2]) == ("issue", "edit") and "--remove-assignee" in args:
            raise RuntimeError("transient GitHub failure")
        return real(args, input=input)

    with pytest.raises(RuntimeError):
        release(PROJECT, 7, session="s1", gh=flaky, now=NOW)
    release(PROJECT, 7, session="s1", gh=gh, now=NOW)
    assert _logins(gh) == []
    assert gh.issues[7]["comments"][-1]["body"].startswith("<!-- forge-cleanup session=s1")
    comments = len(gh.issues[7]["comments"])
    before = len(gh.calls)
    release(PROJECT, 7, session="s1", gh=gh, now=NOW)  # nothing left to do
    assert not any("--remove-assignee" in c for c in gh.calls[before:])
    assert len(gh.issues[7]["comments"]) == comments


# Codex 4205123792 / 4205123802: every ended lease is reconciled, and a takeover's cleanup of the
# expired lease's assignee is best effort, never failing the authoritative claim.


def _failing_removal(gh):
    real = gh.__call__

    def flaky(args, input=None):
        if tuple(args[:2]) == ("issue", "edit") and "--remove-assignee" in args:
            raise RuntimeError("transient GitHub failure")
        return real(args, input=input)

    return flaky


def test_a_retry_cleans_an_earlier_release_after_another_session_claimed_and_released():
    gh = FakeGh().issue(7)
    assert claim(
        PROJECT, 7, agent="a", session="A", branch="b", gh=gh, now=NOW, assignee="alice"
    ).ok
    with pytest.raises(RuntimeError):
        release(PROJECT, 7, session="A", gh=_failing_removal(gh), now=NOW)
    assert _logins(gh) == ["alice"]
    flaky = _failing_removal(gh)  # B's whole run sees the outage too, so A's cleanup stays undone
    assert claim(
        PROJECT, 7, agent="b", session="B", branch="b", gh=flaky, now=NOW, assignee="bob"
    ).ok
    with pytest.raises(RuntimeError):
        release(PROJECT, 7, session="B", gh=flaky, now=NOW)
    assert sorted(_logins(gh)) == ["alice", "bob"]
    release(PROJECT, 7, session="A", gh=gh, now=NOW)  # A's retry: the latest release is B's
    assert _logins(gh) == []
    cleaned = [c["body"] for c in gh.issues[7]["comments"] if "forge-cleanup" in c["body"]]
    assert "<!-- forge-cleanup session=A assignee=alice -->" in cleaned
    assert "<!-- forge-cleanup session=B assignee=bob -->" in cleaned


def test_reconciling_every_release_still_skips_a_login_reassigned_after_its_release():
    gh = FakeGh().issue(7)
    claim(PROJECT, 7, agent="a", session="A", branch="b", gh=gh, now=NOW, assignee="alice")
    with pytest.raises(RuntimeError):
        release(PROJECT, 7, session="A", gh=_failing_removal(gh), now=NOW)
    gh.events[7].append(
        {
            "event": "assigned",
            "assignee": {"login": "alice"},
            "created_at": (NOW + timedelta(minutes=3)).isoformat(),
        }
    )
    gh.posted_at = NOW + timedelta(minutes=4)
    claim(PROJECT, 7, agent="b", session="B", branch="b", gh=gh, now=gh.posted_at)
    release(PROJECT, 7, session="B", gh=gh, now=gh.posted_at)
    assert _logins(gh) == ["alice"]  # a maintainer's later assignment is never undone


def test_a_takeover_succeeds_when_the_expired_assignee_cleanup_fails_and_a_later_op_finishes():
    stale = Lease(7, "a", "old", "b", NOW - timedelta(minutes=1), "alice", True)
    gh = FakeGh().issue(7, assignees=["alice"], comments=[format_claim_marker(stale)])
    result = claim(
        PROJECT,
        7,
        agent="a",
        session="new",
        branch="b",
        gh=_failing_removal(gh),
        now=NOW,
        assignee="bob",
    )
    assert result.ok and result.took_over_from == "old"
    assert current_lease(PROJECT, 7, gh, now=NOW).session == "new"  # never retracted
    assert not any("forge-release session=new" in c["body"] for c in gh.issues[7]["comments"])
    assert sorted(_logins(gh)) == ["alice", "bob"]
    renewed = claim(  # any later claim/release retries the expired lease's cleanup
        PROJECT, 7, agent="a", session="new", branch="b", gh=gh, now=NOW, assignee="bob"
    )
    assert renewed.ok and renewed.renewed
    assert _logins(gh) == ["bob"]
    release(PROJECT, 7, session="new", gh=gh, now=NOW)
    assert _logins(gh) == []
    bodies = [c["body"] for c in gh.issues[7]["comments"]]
    assert "<!-- forge-cleanup session=old assignee=alice -->" in bodies


def test_a_successful_takeover_records_the_expired_leases_cleanup():
    stale = Lease(7, "a", "old", "b", NOW - timedelta(minutes=1), "alice", True)
    gh = FakeGh().issue(7, assignees=["alice"], comments=[format_claim_marker(stale)])
    assert claim(
        PROJECT, 7, agent="a", session="new", branch="b", gh=gh, now=NOW, assignee="bob"
    ).ok
    assert _logins(gh) == ["bob"]
    bodies = [c["body"] for c in gh.issues[7]["comments"]]
    assert bodies.count("<!-- forge-cleanup session=old assignee=alice -->") == 1


# ---------------------------------------------------------------- review regressions (PR 139, 12)


def test_a_same_session_reclaim_records_the_expired_lease_as_ended():
    """Codex 4205367682: a session reclaiming its OWN expired lease overwrote it before it was
    recorded as ended, so a failed cleanup of its owned assignee was never retried."""
    from agentic_sdlc.leases import ended_leases

    stale = Lease(7, "a", "s1", "b", NOW - timedelta(minutes=1), "alice", True)
    gh = FakeGh().issue(7, assignees=["alice"], comments=[format_claim_marker(stale)])
    result = claim(
        PROJECT,
        7,
        agent="a",
        session="s1",
        branch="b",
        gh=_failing_removal(gh),
        now=NOW,
        assignee="bob",
    )
    assert result.ok and result.took_over_from == "s1"
    assert sorted(_logins(gh)) == ["alice", "bob"]  # the cleanup failed
    ended = ended_leases(PROJECT, 7, gh)
    assert [(e.lease.session, e.lease.assignee, e.cleaned) for e in ended] == [
        ("s1", "alice", False)
    ]
    release(PROJECT, 7, session="s1", gh=gh, now=NOW)  # the next operation finishes it
    assert _logins(gh) == []
    bodies = [c["body"] for c in gh.issues[7]["comments"]]
    assert "<!-- forge-cleanup session=s1 assignee=alice -->" in bodies


def test_a_live_same_session_marker_is_a_renewal_not_an_end():
    from agentic_sdlc.leases import ended_leases

    live = Lease(7, "a", "s1", "b", NOW + timedelta(hours=1), "alice", True)
    gh = FakeGh().issue(7, comments=[format_claim_marker(live)])
    assert claim(PROJECT, 7, agent="a", session="s1", branch="b", gh=gh, now=NOW).renewed
    assert ended_leases(PROJECT, 7, gh) == []


def test_a_differently_cased_pre_existing_assignee_is_never_lease_owned():
    """Codex 4205367690: `--assignee alice` on an issue GitHub reports as assigned to `Alice`
    is the same account; the lease must not own it, so release keeps it."""
    gh = FakeGh().issue(7, assignees=["Alice"])
    result = claim(
        PROJECT, 7, agent="a", session="s1", branch="b", gh=gh, now=NOW, assignee="alice"
    )
    assert result.ok and not result.lease.owns_assignee
    release(PROJECT, 7, session="s1", gh=gh, now=NOW)
    assert _logins(gh) == ["Alice"]


def test_a_takeover_inherits_an_owned_assignee_whatever_its_case():
    stale = Lease(7, "a", "old", "b", NOW - timedelta(minutes=1), "Alice", True)
    gh = FakeGh().issue(7, assignees=["Alice"], comments=[format_claim_marker(stale)])
    result = claim(
        PROJECT, 7, agent="a", session="new", branch="b", gh=gh, now=NOW, assignee="ALICE"
    )
    assert result.ok and result.lease.owns_assignee  # inherited, not dropped and re-added
    assert _logins(gh) == ["Alice"]
    release(PROJECT, 7, session="new", gh=gh, now=NOW)
    assert _logins(gh) == []


def test_cleanup_markers_and_live_leases_match_logins_case_insensitively():
    from agentic_sdlc.leases import ended_leases, format_cleanup_marker, format_release_marker

    ended = Lease(7, "a", "s1", "b", NOW + timedelta(hours=1), "alice", True)
    gh = FakeGh().issue(
        7,
        comments=[
            format_claim_marker(ended),
            format_release_marker("s1"),
            format_cleanup_marker("s1", "ALICE"),
        ],
    )
    assert [e.cleaned for e in ended_leases(PROJECT, 7, gh)] == [True]
    # a live lease that wants `Alice` keeps the login an ended lease owned as `alice`
    gh = FakeGh().issue(7, assignees=["alice"])
    gh.issues[7]["comments"] = [
        _comment_row(1, format_claim_marker(ended), "2026-09-30T00:00:00Z"),
        _comment_row(2, format_release_marker("s1"), "2026-09-30T00:01:00Z"),
        _comment_row(
            3,
            format_claim_marker(Lease(7, "a", "s2", "b", NOW + timedelta(hours=1), "Alice", False)),
            "2026-09-30T00:02:00Z",
        ),
    ]
    release(PROJECT, 7, session="s2", gh=gh, now=NOW)  # s2 owns nothing; s1's cleanup retried
    bodies = [c["body"] for c in gh.issues[7]["comments"]]
    assert "<!-- forge-cleanup session=s1 assignee=alice -->" in bodies


def test_marker_authors_bots_and_the_permission_cache_ignore_login_case(monkeypatch):
    from agentic_sdlc.leases import LEASE_BOT_LOGINS_ENV, PermissionCache, trusted_marker_author

    guard = _guard()
    gh = FakeGh().issue(7)
    marker = format_claim_marker(Lease(7, "me", "s1", "b", NOW + timedelta(hours=1)))
    gh.issues[7]["comments"] += [
        _comment_row(90, marker, "2026-09-30T01:00:00Z", "ATULG4"),
        _comment_row(91, "<!-- forge-release session=s1 -->", "2026-09-30T01:01:00Z", "AtulG4"),
    ]
    assert current_lease(PROJECT, 7, gh, now=NOW) is None  # both spellings are the admin
    reads = [c for c in gh.calls if "/collaborators/" in c[1]]
    assert len(reads) == 1  # one cache entry per account, read with the spelling it was given
    cache = PermissionCache(PROJECT, gh)
    assert cache("Atulg4") == cache("atulg4") == "admin"
    for configured, login in (
        ("", "GitHub-Actions[bot]"),
        ("agentic-sdlc-publisher[bot]", "Agentic-SDLC-Publisher[BOT]"),
        ("My-Forge[bot]", "my-forge[BOT]"),
    ):
        monkeypatch.setenv(LEASE_BOT_LOGINS_ENV, configured)
        row = _comment_row(1, "", NOW.isoformat(), login, "Bot", "NONE")
        assert trusted_marker_author(row, lambda _: None), (configured, login)
        assert guard.trusted_marker_author(row, lambda _: None), (configured, login)
    row = _comment_row(1, "", NOW.isoformat(), "Writer", "User", "NONE")
    assert trusted_marker_author(row, lambda _: "Write")  # a permission's case is not authority
    assert guard.trusted_marker_author(row, lambda _: "Write")


def test_the_commit_guard_caches_permissions_per_account(monkeypatch):
    import subprocess

    guard = _guard()
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout='{"role_name": "write"}', stderr="")

    monkeypatch.setattr(guard.subprocess, "run", fake_run)
    monkeypatch.setattr(guard, "_PERMISSIONS", {})
    assert guard.repo_permission("Alice") == guard.repo_permission("alice") == "write"
    assert len(calls) == 1 and calls[0][-1].endswith("/collaborators/Alice/permission")


@pytest.mark.parametrize("subcommand", ["claim", "renew", "release", "claims"])
@pytest.mark.parametrize("to_file", [False, True])
def test_lease_subcommands_keep_stdout_pure_json(
    subcommand, to_file, tmp_path, monkeypatch, capsys
):
    """Codex 4205367711: `claim` without --output wrote its JSON and then a status line to
    stdout. Every lease subcommand: stdout is exactly one JSON document (or empty with --output),
    status lines on stderr."""
    from agentic_sdlc import cli

    wall = datetime.now(UTC)
    gh = FakeGh().issue(7)
    gh.posted_at = wall
    if subcommand != "claim":
        mine = format_claim_marker(Lease(7, "me", "s1", "b", wall + timedelta(hours=1)))
        gh.issues[7]["labels"].append({"name": IN_PROGRESS_LABEL})
        gh.issues[7]["comments"].append(_comment_row(1, mine, wall.isoformat()))
    monkeypatch.setattr(cli, "run_gh", gh)
    argv = {
        "claim": ["claim", "--agent", "me", "--branch", "b"],
        "renew": ["renew"],
        "release": ["release"],
        "claims": ["claims"],
    }[subcommand] + ["--project", PROJECT]
    if subcommand != "claims":
        argv += ["--issue", "7", "--session", "s1"]
    out = tmp_path / "out.json"
    if to_file:
        argv += ["--output", str(out)]
    assert cli.main(argv) == 0
    captured = capsys.readouterr()
    assert captured.err.strip()  # the human status line
    if to_file:
        assert captured.out == ""
        document = json.loads(out.read_text())
    else:
        document = json.loads(captured.out)
    assert document is not None


def test_a_refused_claim_still_prints_only_json_on_stdout(monkeypatch, capsys):
    from agentic_sdlc import cli

    wall = datetime.now(UTC)
    gh = FakeGh().issue(7)
    theirs = format_claim_marker(Lease(7, "x", "other", "b", wall + timedelta(hours=1)))
    gh.issues[7]["comments"].append(_comment_row(1, theirs, wall.isoformat()))
    monkeypatch.setattr(cli, "run_gh", gh)
    argv = ["claim", "--project", PROJECT, "--issue", "7", "--session", "s1"]
    assert cli.main([*argv, "--agent", "me", "--branch", "b"]) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out)["ok"] is False
    assert "REFUSED" in captured.err


# ---------------------------------------------------------------- Codex 4205654923


def _readd(gh, login, at, n=7):
    """A maintainer removes and re-adds `login` at `at` (both events), leaving it assigned."""
    gh.events.setdefault(n, []).extend(
        [
            {"event": "unassigned", "assignee": {"login": login}, "created_at": at.isoformat()},
            {
                "event": "assigned",
                "assignee": {"login": login.upper()},
                "created_at": (at + timedelta(seconds=5)).isoformat(),
            },
        ]
    )


def test_release_keeps_an_owned_assignee_a_maintainer_re_added_during_the_lease():
    gh = FakeGh().issue(7)
    assert claim(
        PROJECT, 7, agent="a", session="s1", branch="b", gh=gh, now=NOW, assignee="alice"
    ).ok
    _readd(gh, "alice", NOW + timedelta(minutes=1))
    release(PROJECT, 7, session="s1", gh=gh, now=NOW + timedelta(minutes=2))
    assert _logins(gh) == ["alice"]
    assert not any("--remove-assignee" in c for c in gh.calls)
    # ownership ended: recorded, so no later retry removes it either
    assert gh.issues[7]["comments"][-1]["body"] == (
        "<!-- forge-cleanup session=s1 assignee=alice -->"
    )


def test_takeover_keeps_an_expired_leases_assignee_re_added_while_it_was_active():
    stale = Lease(7, "a", "old", "b", NOW - timedelta(minutes=1), "alice", True)
    gh = FakeGh().issue(7, assignees=["alice"], comments=[format_claim_marker(stale)])
    gh.events[7] = [
        {
            "event": "assigned",
            "assignee": {"login": "alice"},
            "created_at": "2026-09-30T00:00:01Z",
        }
    ]
    _readd(gh, "alice", datetime(2026, 9, 30, 1, 0, tzinfo=UTC))
    assert claim(
        PROJECT, 7, agent="a", session="new", branch="b", gh=gh, now=NOW, assignee="bob"
    ).ok
    assert sorted(_logins(gh)) == ["alice", "bob"]
    assert not any("--remove-assignee" in c for c in gh.calls)


def test_unreadable_events_keep_the_owned_assignee_and_leave_the_cleanup_pending():
    gh = FakeGh().issue(7)
    assert claim(
        PROJECT, 7, agent="a", session="s1", branch="b", gh=gh, now=NOW, assignee="alice"
    ).ok
    real = gh.__call__

    def no_events(args, input=None):
        if args[0] == "api" and args[1].split("?")[0].endswith("/events"):
            raise RuntimeError("gh: Forbidden (HTTP 403)")
        return real(args, input=input)

    release(PROJECT, 7, session="s1", gh=no_events, now=NOW)
    assert _logins(gh) == ["alice"]
    assert "forge-cleanup" not in gh.issues[7]["comments"][-1]["body"]
    # Readable again and never re-added: a later release finishes the cleanup.
    release(PROJECT, 7, session="s1", gh=gh, now=NOW + timedelta(minutes=1))
    assert _logins(gh) == []
    assert gh.issues[7]["comments"][-1]["body"] == (
        "<!-- forge-cleanup session=s1 assignee=alice -->"
    )


# ---------------------------------------------------------------- Codex 4205853970


def test_cleanup_restores_an_assignment_a_maintainer_made_between_check_and_removal():
    """The history check and the removal are not atomic: an `assigned` event that appears after
    the pre-removal snapshot is a maintainer's, and the login is given back."""
    gh = FakeGh().issue(7)
    assert claim(
        PROJECT, 7, agent="a", session="s1", branch="b", gh=gh, now=NOW, assignee="alice"
    ).ok
    real = gh.__call__

    def racing(args, input=None):
        if tuple(args[:2]) == ("issue", "edit") and "--remove-assignee" in args:
            gh.events[7].append(  # the maintainer assigns alice just before our edit lands
                {
                    "id": 9001,
                    "event": "assigned",
                    "assignee": {"login": "Alice"},
                    "created_at": (NOW + timedelta(minutes=2)).isoformat(),
                }
            )
        return real(args, input=input)

    release(PROJECT, 7, session="s1", gh=racing, now=NOW + timedelta(minutes=2))
    assert _logins(gh) == ["alice"]
    edits = [c for c in gh.calls if c[:2] == ("issue", "edit") and "alice" in c]
    assert "--remove-assignee" in edits[-2] and "--add-assignee" in edits[-1]
    # ownership ended: recorded, so no retry removes the maintainer's assignment again
    assert gh.issues[7]["comments"][-1]["body"] == (
        "<!-- forge-cleanup session=s1 assignee=alice -->"
    )


def test_cleanup_without_a_racing_assignment_stays_removed():
    gh = FakeGh().issue(7)
    assert claim(
        PROJECT, 7, agent="a", session="s1", branch="b", gh=gh, now=NOW, assignee="alice"
    ).ok
    release(PROJECT, 7, session="s1", gh=gh, now=NOW + timedelta(minutes=2))
    assert _logins(gh) == []
    assert not any(c[:2] == ("issue", "edit") and "--add-assignee" in c for c in gh.calls[-6:])


# ---------------------------------------------------------------- Codex 4205853990


def test_release_retry_reconciles_despite_another_sessions_expired_marker():
    """Session B holds an unreleased EXPIRED claim; A took over, released, and its assignee
    cleanup failed. B is no live holder: retrying A's release must finish the cleanup."""
    stale = Lease(7, "b", "sB", "b", NOW - timedelta(minutes=1))
    gh = FakeGh().issue(7, comments=[format_claim_marker(stale)])
    assert claim(
        PROJECT, 7, agent="a", session="sA", branch="b", gh=gh, now=NOW, assignee="alice"
    ).ok
    real = gh.__call__

    def failing(args, input=None):
        if tuple(args[:2]) == ("issue", "edit") and "--remove-assignee" in args:
            raise RuntimeError("gh: Bad Gateway (HTTP 502)")
        return real(args, input=input)

    later = NOW + timedelta(minutes=5)
    with pytest.raises(RuntimeError):
        release(PROJECT, 7, session="sA", gh=failing, now=later)
    remaining = current_lease(PROJECT, 7, gh, now=later)
    assert remaining is not None and remaining.session == "sB" and not remaining.live(later)
    release(PROJECT, 7, session="sA", gh=gh, now=later)  # the retry: no LeaseError
    assert _logins(gh) == []
    assert gh.issues[7]["comments"][-1]["body"] == (
        "<!-- forge-cleanup session=sA assignee=alice -->"
    )
    # A LIVE lease of another session still refuses a non-forced release.
    gh2 = FakeGh().issue(7, comments=[format_claim_marker(replace(stale, expires=later))])
    with pytest.raises(LeaseError):
        release(PROJECT, 7, session="sA", gh=gh2, now=NOW)


# Codex 4206173677: ownership of the assignee is proven around the add, not taken from the
# claim's first (stale) read of the issue.


def _maintainer_assigns(gh, login, n=7):
    gh.issues[n]["assignees"].append({"login": login})
    gh.events.setdefault(n, []).append(
        {
            "event": "assigned",
            "assignee": {"login": login},
            "actor": {"login": "maintainer"},
            "created_at": NOW.isoformat(),
        }
    )


@pytest.mark.parametrize("moment", ["before_fresh_read", "between_fresh_read_and_add"])
def test_claim_does_not_own_an_assignee_a_maintainer_added_during_the_claim(moment):
    gh = FakeGh().issue(7)
    real = gh.__call__
    fired = []

    def racing(args, input=None):
        args = tuple(args)
        issue_read = args[0] == "api" and args[1] == f"repos/{PROJECT}/issues/7"
        add = args[:2] == ("issue", "edit") and "--add-assignee" in args
        reads = sum(1 for c in gh.calls if c[0] == "api" and c[1] == f"repos/{PROJECT}/issues/7")
        if not fired and (
            (moment == "before_fresh_read" and issue_read and reads >= 1)
            or (moment == "between_fresh_read_and_add" and add)
        ):
            fired.append(True)
            _maintainer_assigns(gh, "alice")  # after the claim's first read of the issue
        return real(args, input=input)

    result = claim(
        PROJECT, 7, agent="a", session="s1", branch="b", gh=racing, now=NOW, assignee="alice"
    )
    assert fired and result.ok and not result.lease.owns_assignee
    assert not any("owns_assignee=1" in c["body"] for c in gh.issues[7]["comments"])
    release(PROJECT, 7, session="s1", gh=gh, now=NOW)
    assert _logins(gh) == ["alice"]  # the maintainer's assignment survives the release


def test_claim_owns_only_an_assignment_its_own_actor_made():
    gh = FakeGh().issue(7)
    result = claim(
        PROJECT, 7, agent="a", session="s1", branch="b", gh=gh, now=NOW, assignee="alice"
    )
    assert result.ok and result.lease.owns_assignee
    assert current_lease(PROJECT, 7, gh, now=NOW).owns_assignee
    # the same add, but the event GitHub reports names someone else as the actor
    gh2 = FakeGh().issue(7)
    real = gh2.__call__

    def other_actor(args, input=None):
        out = real(args, input=input)
        for event in gh2.events.get(7, []):
            event["actor"] = {"login": "maintainer"}
        return out

    result = claim(
        PROJECT, 7, agent="a", session="s1", branch="b", gh=other_actor, now=NOW, assignee="alice"
    )
    assert result.ok and not result.lease.owns_assignee


def test_claim_does_not_own_an_assignee_when_events_cannot_be_read():
    gh = FakeGh().issue(7)
    real = gh.__call__

    def no_events(args, input=None):
        if args[0] == "api" and args[1].split("?")[0].endswith("/events"):
            raise RuntimeError("HTTP 502")
        return real(args, input=input)

    result = claim(
        PROJECT, 7, agent="a", session="s1", branch="b", gh=no_events, now=NOW, assignee="alice"
    )
    assert result.ok and not result.lease.owns_assignee and _logins(gh) == ["alice"]


# ---------------------------------------------------------------- Codex 4206492751


def test_a_pending_cleanup_hands_its_assignee_to_the_live_lease_that_wants_it():
    gh = FakeGh().issue(7)
    assert claim(
        PROJECT, 7, agent="a", session="A", branch="b", gh=gh, now=NOW, assignee="alice"
    ).ok
    with pytest.raises(RuntimeError):
        release(PROJECT, 7, session="A", gh=_failing_removal(gh), now=NOW)
    assert _logins(gh) == ["alice"]  # A's cleanup is pending
    # B wants alice too: it finds her assigned already, so its own claim does not own her...
    assert claim(
        PROJECT, 7, agent="b", session="B", branch="b", gh=gh, now=NOW, assignee="alice"
    ).ok
    assert _logins(gh) == ["alice"]
    # ...so A's cleanup is recorded only together with an explicit hand-over to B.
    held = current_lease(PROJECT, 7, gh, now=NOW)
    assert held.session == "B" and held.owns_assignee
    bodies = [c["body"] for c in gh.issues[7]["comments"]]
    assert "<!-- forge-cleanup session=A assignee=alice -->" in bodies
    handed = next(i for i, b in enumerate(bodies) if "handed over" in b)
    assert handed < bodies.index("<!-- forge-cleanup session=A assignee=alice -->")
    # A renewal that read the thread before the hand-over cannot drop it again.
    stale = Lease(7, "b", "B", "b", NOW + timedelta(hours=2), "alice", False)
    gh.issues[7]["comments"].append(_comment_row(900, format_claim_marker(stale), NOW.isoformat()))
    assert current_lease(PROJECT, 7, gh, now=NOW).owns_assignee
    release(PROJECT, 7, session="B", gh=gh, now=NOW)
    assert _logins(gh) == []  # B's release removes the login it was handed


def test_a_failed_hand_over_leaves_the_ended_leases_cleanup_pending():
    gh = FakeGh().issue(7)
    assert claim(
        PROJECT, 7, agent="a", session="A", branch="b", gh=gh, now=NOW, assignee="alice"
    ).ok
    with pytest.raises(RuntimeError):
        release(PROJECT, 7, session="A", gh=_failing_removal(gh), now=NOW)
    real = gh.__call__
    posts = []

    def no_handover(args, input=None):
        if tuple(args[:3]) == ("api", "-X", "POST") and input and "handed over" in input:
            posts.append(True)
            raise RuntimeError("comment failed")
        return real(args, input=input)

    # the claim itself succeeds (the reconciliation is best effort) ...
    assert claim(
        PROJECT, 7, agent="b", session="B", branch="b", gh=no_handover, now=NOW, assignee="alice"
    ).ok
    assert posts
    bodies = [c["body"] for c in gh.issues[7]["comments"]]
    # ... and A's cleanup is NOT recorded: the login is nobody's to remove yet
    assert not any("forge-cleanup session=A" in b for b in bodies)
    assert not current_lease(PROJECT, 7, gh, now=NOW).owns_assignee
    # a later operation (B's own release) finishes A's cleanup once nobody wants alice
    release(PROJECT, 7, session="B", gh=gh, now=NOW)
    assert _logins(gh) == []


# ---------------------------------------------------------------- Codex 4206780262


def _superseded_thread(*comments):
    """`old` (expired, owning alice, claimed 00:00) whose alice a maintainer removed and re-added
    at 01:00 -- while `old` was active."""
    stale = Lease(7, "a", "old", "b", NOW - timedelta(minutes=1), "alice", True)
    gh = FakeGh().issue(7, assignees=["alice"], comments=[format_claim_marker(stale), *comments])
    gh.events[7] = [
        {"event": "assigned", "assignee": {"login": "alice"}, "created_at": "2026-09-30T00:00:01Z"}
    ]
    _readd(gh, "alice", datetime(2026, 9, 30, 1, 0, tzinfo=UTC))
    return gh


def test_a_takeover_does_not_inherit_ownership_a_maintainer_superseded():
    gh = _superseded_thread()
    result = claim(
        PROJECT, 7, agent="a", session="new", branch="b", gh=gh, now=NOW, assignee="alice"
    )
    assert result.ok and result.took_over_from == "old"
    assert not result.lease.owns_assignee
    assert not current_lease(PROJECT, 7, gh, now=NOW).owns_assignee
    release(PROJECT, 7, session="new", gh=gh, now=NOW + timedelta(minutes=1))
    assert _logins(gh) == ["alice"]  # the maintainer's assignment survives the new lease
    assert not any("--remove-assignee" in c for c in gh.calls)


def test_a_takeover_inherits_nothing_when_the_expired_leases_history_is_unreadable():
    stale = Lease(7, "a", "old", "b", NOW - timedelta(minutes=1), "alice", True)
    gh = FakeGh().issue(7, assignees=["alice"], comments=[format_claim_marker(stale)])
    real = gh.__call__

    def no_events(args, input=None):
        if args[0] == "api" and args[1].split("?")[0].endswith("/events"):
            raise RuntimeError("gh: Forbidden (HTTP 403)")
        return real(args, input=input)

    result = claim(
        PROJECT, 7, agent="a", session="new", branch="b", gh=no_events, now=NOW, assignee="alice"
    )
    assert result.ok and not result.lease.owns_assignee
    assert _logins(gh) == ["alice"]


def test_reconciliation_disowns_a_live_lease_that_inherited_superseded_ownership():
    # A live lease that inherited `old`'s ownership before the takeover check existed.
    inherited = Lease(7, "a", "new", "b", NOW + timedelta(hours=1), "alice", True)
    gh = _superseded_thread()
    gh.issues[7]["comments"].append(
        _comment_row(50, format_claim_marker(inherited), (NOW - timedelta(seconds=30)).isoformat())
    )
    assert current_lease(PROJECT, 7, gh, now=NOW).owns_assignee
    # any reconciling operation (here the holder's own renewal) finds old's ownership superseded
    assert claim(
        PROJECT, 7, agent="a", session="new", branch="b", gh=gh, now=NOW, assignee="alice"
    ).renewed
    held = current_lease(PROJECT, 7, gh, now=NOW)
    assert held.session == "new" and not held.owns_assignee
    bodies = [c["body"] for c in gh.issues[7]["comments"]]
    assert any("assignee=alice owns_assignee=0" in b for b in bodies)
    assert "<!-- forge-cleanup session=old assignee=alice -->" in bodies
    # a renewal from a stale read cannot re-own it within the run
    gh.issues[7]["comments"].append(
        _comment_row(901, format_claim_marker(inherited), NOW.isoformat())
    )
    assert not current_lease(PROJECT, 7, gh, now=NOW).owns_assignee
    release(PROJECT, 7, session="new", gh=gh, now=NOW + timedelta(minutes=1))
    assert _logins(gh) == ["alice"]


# ---------------------------------------------------------------- Codex 4206780275


def test_a_release_note_with_marker_syntax_cannot_keep_the_lease_live():
    gh = FakeGh().issue(7)
    assert claim(PROJECT, 7, agent="a", session="s1", branch="b", gh=gh, now=NOW).ok
    forged = format_claim_marker(Lease(7, "x", "evil", "b", NOW + timedelta(hours=3)))
    release(PROJECT, 7, session="s1", gh=gh, now=NOW, note=f"copied: {forged}")
    body = gh.issues[7]["comments"][-1]["body"]
    assert body.startswith("<!-- forge-release session=s1 -->\n")
    assert "&lt;!-- forge-claim" in body and "--&gt;" in body
    assert body.count("<!--") == 1
    assert current_lease(PROJECT, 7, gh, now=NOW) is None


def test_markers_count_only_alone_on_the_first_line():
    from agentic_sdlc import leases

    guard = _guard()
    claim_ = format_claim_marker(Lease(7, "a", "s1", "b", NOW + timedelta(hours=1)))
    release_ = "<!-- forge-release session=s1 -->"
    cases = [
        (claim_ + "\nnote", "s1"),  # the tool's own shape
        ("quoting a claim:\n" + claim_, None),  # not on the first line
        ("text " + claim_, None),  # not alone on the first line
        (claim_ + "\n" + release_, None),  # two markers: invalid, neither counts
        (release_ + "\n" + claim_, None),
    ]
    for body, expected in cases:
        gh = FakeGh().issue(7)
        gh.issues[7]["comments"] = [_comment_row(1, body, NOW.isoformat())]
        mine = current_lease(PROJECT, 7, gh, now=NOW)
        theirs = guard.lease_from_comments(gh.issues[7]["comments"], {"atulg4": "admin"}.get, NOW)
        assert (mine.session if mine else None) == expected, body
        assert (theirs["session"] if theirs else None) == expected, body
    # a multi-marker release does not void the live claim before it
    gh = FakeGh().issue(7, comments=[claim_])
    gh.issues[7]["comments"].append(_comment_row(2, release_ + "\n" + claim_, NOW.isoformat()))
    assert current_lease(PROJECT, 7, gh, now=NOW).session == "s1"
    assert leases.sanitize_text("a <!-- b --> c") == "a &lt;!-- b --&gt; c"


# ---------------------------------------------------------------- Codex 4206780290


def test_a_session_retracts_its_own_queued_claim_without_touching_the_live_holder():
    gh = FakeGh().issue(7)
    assert claim(
        PROJECT, 7, agent="a", session="A", branch="b", gh=gh, now=NOW, assignee="alice"
    ).ok
    # B lost the race behind A, and its retraction POST failed: its marker stays queued.
    queued = Lease(7, "b", "B", "b2", NOW + timedelta(hours=2), "bob")
    gh.issues[7]["comments"].append(_comment_row(800, format_claim_marker(queued), NOW.isoformat()))
    edits_before = [c for c in gh.calls if c[:2] == ("issue", "edit")]
    release(PROJECT, 7, session="B", gh=gh, now=NOW)
    assert [c for c in gh.calls if c[:2] == ("issue", "edit")] == edits_before
    assert gh.issues[7]["comments"][-1]["body"].startswith("<!-- forge-release session=B -->")
    held = current_lease(PROJECT, 7, gh, now=NOW)
    assert held.session == "A" and held.owns_assignee
    assert any(lbl["name"] == IN_PROGRESS_LABEL for lbl in gh.issues[7]["labels"])
    assert _logins(gh) == ["alice"]
    # when A releases, B does not become authoritative
    release(PROJECT, 7, session="A", gh=gh, now=NOW)
    assert current_lease(PROJECT, 7, gh, now=NOW) is None
    # a session with no open marker of its own still cannot release a live holder
    assert claim(PROJECT, 7, agent="a", session="A2", branch="b", gh=gh, now=NOW).ok
    with pytest.raises(LeaseError):
        release(PROJECT, 7, session="C", gh=gh, now=NOW)


@pytest.mark.parametrize(
    "slurped",
    [
        [None],  # a page that is not a list
        [{"message": "unexpected response"}],  # an error object where a page should be
        [[], {"message": "unexpected response"}],  # one good page, one malformed
        [["not an object"]],  # a page whose item is not a comment object
        [[None]],
        {"message": "unexpected response"},  # not even a list of pages
    ],
)
def test_a_malformed_paginated_comment_answer_is_unreadable_not_empty(slurped):
    """A successful but malformed --slurp answer is an unreadable lease history: reading it as
    an empty one would let a rival claim over a live lease it merely could not see."""
    fake = FakeGh().issue(7)

    def gh(args, input=None):
        args = tuple(args)
        if args[0] == "api" and args[1].split("?")[0].endswith("/comments") and "-X" not in args:
            return json.dumps(slurped)
        return fake(args, input)

    with pytest.raises(LeaseError, match="unreadable"):
        current_lease(PROJECT, 7, gh, now=NOW)
    with pytest.raises(LeaseError):
        claim(PROJECT, 7, agent="me", session="s1", branch="b", ttl_minutes=60, gh=gh, now=NOW)
    assert not any(c[:3] == ("api", "-X", "POST") for c in fake.calls)  # no marker posted


@pytest.mark.parametrize(
    "slurped", [[None], [{"message": "x"}], [[], {"message": "x"}], [["s"]], [[None]], {}, None]
)
def test_the_commit_guard_reads_a_malformed_comment_answer_as_unavailable(
    slurped, monkeypatch, tmp_path
):
    import subprocess

    guard = _guard()
    out = "" if slurped is None else json.dumps(slurped)

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")

    monkeypatch.setattr(guard.subprocess, "run", fake_run)
    assert guard.lease_unavailable(guard.lease_for(7))


# ---------------------------------------------------------------- Codex review 5481401488


def test_chatter_authors_permissions_are_never_read(monkeypatch):
    """Codex 4239780833: only a comment carrying a valid lease marker needs its author's
    permission. Ordinary discussion costs no read, and an unreadable permission of a chatter
    cannot refuse the lease operation; a marker author's still does."""
    gh = FakeGh().issue(7)
    marker = format_claim_marker(Lease(7, "me", "s1", "b", NOW + timedelta(hours=1)))
    gh.issues[7]["comments"] += [
        _comment_row(80, "looks good to me", "2026-09-30T00:10:00Z", "chatty", "User", "NONE"),
        _comment_row(
            81, "see <!-- forge-claim --> above", "2026-09-30T00:11:00Z", "quoter", "User", "NONE"
        ),
        _comment_row(82, marker, "2026-09-30T01:00:00Z"),
    ]
    real = gh.__call__

    def flaky(args, input=None):
        if "/collaborators/" in args[1] and "/collaborators/atulg4/" not in args[1]:
            raise RuntimeError("HTTP 502")
        return real(args, input=input)

    lease = current_lease(PROJECT, 7, flaky, now=NOW)
    assert lease is not None and lease.session == "s1"
    reads = [c for c in gh.calls if "/collaborators/" in c[1]]
    assert reads and all("/collaborators/atulg4/" in c[1] for c in reads)
    # the marker author's unreadable permission still refuses
    gh.issues[7]["comments"].append(_comment_row(83, marker, "2026-09-30T01:05:00Z", "chatty"))
    with pytest.raises(LeaseError, match="cannot read the repository permission"):
        current_lease(PROJECT, 7, flaky, now=NOW)

    # the commit guard filters the same way
    guard = _guard()
    asked = []

    def permission(login):
        asked.append(login)
        if login != "atulg4":
            raise guard.LeaseUnavailable("HTTP 502")
        return "admin"

    rows = gh.issues[7]["comments"][:-1]
    assert guard.lease_from_comments(rows, permission, now=NOW)["session"] == "s1"
    assert set(asked) == {"atulg4"}


def _linked(number, owner="owner", name="repo"):
    return {"number": number, "repository": {"name": name, "owner": {"login": owner}}}


def test_a_pr_linked_through_the_development_sidebar_blocks_claims():
    """Codex 4239780827: a manual Development-sidebar link shows only in
    `closingIssuesReferences`; it marks the work like a closing keyword does."""
    from agentic_sdlc.leases import open_prs_for_issue

    gh = FakeGh().issue(7)
    gh.prs = [
        _fork_pr(40, head="unrelated", closingIssuesReferences=[_linked(7)]),
        _fork_pr(41, head="x", closingIssuesReferences=[_linked(7, "OWNER", "Repo")]),
        _fork_pr(42, head="x", closingIssuesReferences=[_linked(7, "other", "repo")]),
        _fork_pr(43, head="x", closingIssuesReferences=[_linked(70)]),
        _fork_pr(44, head="x", closingIssuesReferences=[_linked(True)]),
        _fork_pr(45, head="x", closingIssuesReferences=[{"number": 7}]),
        _fork_pr(46, head="x", closingIssuesReferences=[{"number": 7, "repository": "repo"}]),
        _fork_pr(47, head="x", closingIssuesReferences="#7"),
        _fork_pr(48, head="x", closingIssuesReferences=[None, "7"]),
        _fork_pr(49, head="x", closingIssuesReferences=[_linked(7, name="")]),
    ]
    assert [p["number"] for p in open_prs_for_issue(PROJECT, 7, gh)] == [40, 41]
    call = [c for c in gh.calls if c[:2] == ("api", "graphql")][-1]
    assert "closingIssuesReferences" in next(a for a in call if a.startswith("query="))
    result = claim(PROJECT, 7, agent="me", session="s1", branch="b", ttl_minutes=60, gh=gh, now=NOW)
    assert not result.ok and "pr/40" in result.reason


def _page(nodes, more=False, cursor=None):
    info = {"hasNextPage": more, "endCursor": cursor}
    return json.dumps(
        {"data": {"repository": {"pullRequests": {"pageInfo": info, "nodes": nodes}}}}
    )


@pytest.mark.parametrize(
    "answer",
    [
        '{"number": 1}',
        "[1, 2]",
        "not json",
        '[{"n": 1}, null]',
        "null",
        '{"errors": [{"message": "rate limited"}], "data": null}',
        '{"data": {"repository": null}}',
        '{"data": {"repository": {"pullRequests": {"nodes": []}}}}',
        _page([None]),
        _page([], more=None),
        _page([], more=True),  # "there is more" without a cursor
        _page([], more=True, cursor=""),
        # a PR whose linked issues fill more than one page: the unread ones could be #7
        _page([{"number": 9, "closingIssuesReferences": {"pageInfo": {"hasNextPage": True}}}]),
        _page([{"number": 9, "closingIssuesReferences": {"nodes": []}}]),
    ],
)
def test_an_unreadable_open_pr_inventory_is_never_no_pr(answer):
    from agentic_sdlc.leases import open_prs_for_issue

    gh = FakeGh().issue(7)
    real = gh.__call__

    def broken(args, input=None):
        if tuple(args[:2]) == ("api", "graphql"):
            return answer
        return real(args, input=input)

    with pytest.raises(LeaseError, match="pull request"):
        open_prs_for_issue(PROJECT, 7, broken)
    with pytest.raises(LeaseError):
        claim(PROJECT, 7, agent="me", session="s1", branch="b", gh=broken, now=NOW)


# Codex 4239965191: `gh pr list --limit 1000` truncated the inventory; every page is read now.
@pytest.mark.parametrize("position", [0, 150, 1000, 2499])
def test_open_pr_detection_reads_every_page(position):
    from agentic_sdlc.leases import open_prs_for_issue

    gh = FakeGh().issue(7)
    gh.prs = [_fork_pr(1000 + i, head=f"feat/{i}") for i in range(2500)]
    gh.prs[position] = _fork_pr(1000 + position, head="patch", body="Closes #7")
    assert [p["number"] for p in open_prs_for_issue(PROJECT, 7, gh)] == [1000 + position]
    pages = [c for c in gh.calls if c[:2] == ("api", "graphql")]
    assert len(pages) == 25
    assert not any(a.startswith("after=") for a in pages[0])
    assert tuple(pages[1][-2:]) == ("-f", "after=100")
    result = claim(PROJECT, 7, agent="me", session="s1", branch="b", ttl_minutes=60, gh=gh, now=NOW)
    assert not result.ok and f"pr/{1000 + position}" in result.reason


def test_an_open_pr_page_that_fails_mid_inventory_refuses_the_claim():
    gh = FakeGh().issue(7)
    gh.prs = [_fork_pr(1000 + i, head=f"feat/{i}") for i in range(250)]
    real = gh.__call__

    def flaky(args, input=None):
        if tuple(args[:2]) == ("api", "graphql") and "after=200" in args:
            raise RuntimeError("gh: HTTP 502")
        return real(args, input=input)

    with pytest.raises(Exception, match="502"):
        claim(PROJECT, 7, agent="me", session="s1", branch="b", gh=flaky, now=NOW)
    assert not gh.issues[7]["comments"]


def test_a_cursor_that_never_advances_refuses():
    from agentic_sdlc.leases import open_prs_for_issue

    def stuck(args, input=None):
        return _page([], more=True, cursor="same")

    with pytest.raises(LeaseError, match="next one starts"):
        open_prs_for_issue(PROJECT, 7, stuck)


# Codex 4240028945: a PR linked by hand in the Development sidebar does not close the issue, so
# it is not in `closingIssuesReferences`; the issue timeline's `connected`/`cross-referenced`
# events are read too.
def _link_event(kind, node_id):
    return {"event": kind, "node_id": node_id, "id": hash(node_id) & 0xFFFF}


def _pr_subject(number, state="OPEN", repo="owner/repo"):
    return {
        "__typename": "PullRequest",
        "number": number,
        "url": f"https://x/pr/{number}",
        "title": "t",
        "state": state,
        "repository": {"nameWithOwner": repo},
    }


def _xref(number, state="open", repo="owner/repo", pull=True):
    row = {
        "number": number,
        "state": state,
        "title": "t",
        "html_url": f"https://x/pr/{number}",
        "repository": {"full_name": repo},
    }
    if pull:
        row["pull_request"] = {"url": f"https://api/pulls/{number}"}
    return {"event": "cross-referenced", "source": {"type": "issue", "issue": row}}


def _claim(gh):
    return claim(PROJECT, 7, agent="me", session="s1", branch="b", ttl_minutes=60, gh=gh, now=NOW)


def test_a_manually_linked_pr_blocks_claims():
    gh = FakeGh().issue(7)
    gh.prs = [
        {"number": 50, "url": "https://x/pr/50", "title": "t", "body": "", "headRefName": "x"}
    ]
    gh.timeline[7] = [_link_event("connected", "CE_1")]
    gh.link_subjects["CE_1"] = _pr_subject(50)
    result = _claim(gh)
    assert not result.ok and "pr/50" in result.reason
    assert not any(c[:3] == ("api", "-X", "POST") for c in gh.calls)


def test_a_manual_link_to_an_open_pr_outside_the_inventory_still_blocks():
    gh = FakeGh().issue(7)
    gh.timeline[7] = [_link_event("connected", "CE_1")]
    gh.link_subjects["CE_1"] = _pr_subject(51)
    result = _claim(gh)
    assert not result.ok and "pr/51" in result.reason


@pytest.mark.parametrize(
    ("events", "subjects"),
    [
        (  # linked, then unlinked
            [_link_event("connected", "CE_1"), _link_event("disconnected", "DE_1")],
            {"CE_1": _pr_subject(52), "DE_1": _pr_subject(52)},
        ),
        ([_link_event("connected", "CE_1")], {"CE_1": _pr_subject(52, state="CLOSED")}),
        ([_link_event("connected", "CE_1")], {"CE_1": _pr_subject(52, state="MERGED")}),
        ([_link_event("connected", "CE_1")], {"CE_1": _pr_subject(52, repo="other/repo")}),
        ([_link_event("connected", "CE_1")], {"CE_1": {"__typename": "Issue", "number": 9}}),
    ],
)
def test_a_link_that_is_gone_closed_or_elsewhere_does_not_block(events, subjects):
    gh = FakeGh().issue(7)
    gh.timeline[7] = events
    gh.link_subjects.update(subjects)
    result = _claim(gh)
    assert result.ok, result.reason


def test_a_relinked_pr_blocks_again():
    gh = FakeGh().issue(7)
    gh.timeline[7] = [
        _link_event("connected", "CE_1"),
        _link_event("disconnected", "DE_1"),
        _link_event("connected", "CE_2"),
    ]
    gh.link_subjects.update(
        {"CE_1": _pr_subject(53), "DE_1": _pr_subject(53), "CE_2": _pr_subject(53)}
    )
    assert not _claim(gh).ok


def test_the_link_owner_repository_compares_case_insensitively():
    gh = FakeGh().issue(7)
    gh.timeline[7] = [_link_event("connected", "CE_1")]
    gh.link_subjects["CE_1"] = _pr_subject(54, repo="Owner/REPO")
    assert not _claim(gh).ok


@pytest.mark.parametrize(
    ("event", "subject"),
    [
        ({"event": "connected"}, None),  # no node id
        ({"event": "connected", "node_id": ""}, None),
        (_link_event("connected", "CE_1"), None),  # the subject could not be read
        (_link_event("connected", "CE_1"), {"number": 55}),  # no type
        (_link_event("connected", "CE_1"), {**_pr_subject(55), "number": None}),
        (_link_event("connected", "CE_1"), {**_pr_subject(55), "repository": None}),
        (_link_event("connected", "CE_1"), {**_pr_subject(55), "state": None}),
        (_link_event("disconnected", "DE_1"), None),
    ],
)
def test_an_unidentified_manual_link_fails_closed(event, subject):
    gh = FakeGh().issue(7)
    gh.timeline[7] = [event]
    if subject is not None:
        gh.link_subjects[event["node_id"]] = subject
    with pytest.raises(LeaseError):
        _claim(gh)
    assert not any(c[:3] == ("api", "-X", "POST") for c in gh.calls)


@pytest.mark.parametrize(
    "answer",
    [
        {"errors": [{"message": "x"}]},
        {"data": None},
        {"data": {"nodes": []}},  # fewer nodes than ids
        "not json",
    ],
)
def test_an_unreadable_link_lookup_fails_closed(answer):
    fake = FakeGh().issue(7)
    fake.timeline[7] = [_link_event("connected", "CE_1")]

    def gh(args, input=None):
        args = tuple(args)
        if args[:2] == ("api", "graphql") and "nodes(ids:" in args[3]:
            return answer if isinstance(answer, str) else json.dumps(answer)
        return fake(args, input)

    with pytest.raises(LeaseError):
        _claim(gh)


def test_an_open_same_repository_pr_cross_referencing_the_issue_blocks():
    gh = FakeGh().issue(7)
    gh.timeline[7] = [_xref(60)]
    result = _claim(gh)
    assert not result.ok and "pr/60" in result.reason


def test_a_cross_reference_falls_back_to_the_repository_url():
    gh = FakeGh().issue(7)
    event = _xref(61)
    del event["source"]["issue"]["repository"]
    event["source"]["issue"]["repository_url"] = "https://api.github.com/repos/OWNER/repo"
    gh.timeline[7] = [event]
    assert not _claim(gh).ok


@pytest.mark.parametrize(
    "event",
    [
        _xref(62, state="closed"),
        _xref(62, repo="other/repo", state="closed"),  # closed: never even read
        _xref(62, pull=False),  # mentioned from an issue
        {"event": "commented", "body": "see #7"},
    ],
)
def test_a_cross_reference_that_is_not_open_same_repository_work_does_not_block(event):
    gh = FakeGh().issue(7)
    gh.timeline[7] = [event]
    result = _claim(gh)
    assert result.ok, result.reason


@pytest.mark.parametrize(
    "event",
    [
        {"event": "cross-referenced"},
        {"event": "cross-referenced", "source": {"issue": None}},
        {"event": "cross-referenced", "source": {"issue": {"pull_request": {}, "state": "open"}}},
        {
            "event": "cross-referenced",
            "source": {"issue": {"pull_request": {}, "number": 1, "repository": {}}},
        },
    ],
)
def test_a_malformed_cross_reference_fails_closed(event):
    gh = FakeGh().issue(7)
    gh.timeline[7] = [event]
    with pytest.raises(LeaseError):
        _claim(gh)


@pytest.mark.parametrize(
    "page",
    ["not json", json.dumps({"message": "x"}), json.dumps([None]), json.dumps(["s"]), "null"],
)
def test_an_unreadable_timeline_fails_closed(page):
    fake = FakeGh().issue(7)

    def gh(args, input=None):
        args = tuple(args)
        if args[0] == "api" and args[1].split("?")[0].endswith("/timeline"):
            return page
        return fake(args, input)

    with pytest.raises(LeaseError):
        _claim(gh)
    assert not any(c[:3] == ("api", "-X", "POST") for c in fake.calls)


def test_the_timeline_is_read_to_its_last_page():
    gh = FakeGh().issue(7)
    filler = [{"event": "commented", "id": i} for i in range(100)]
    gh.timeline[7] = filler + [_xref(63)]
    result = _claim(gh)
    assert not result.ok and "pr/63" in result.reason
    pages = [c[1] for c in gh.calls if c[0] == "api" and "/timeline" in c[1]]
    assert [p.rsplit("page=", 1)[1] for p in pages] == ["1", "2"]


def test_the_existing_signals_still_decide_without_timeline_links():
    from agentic_sdlc.leases import open_prs_for_issue

    gh = FakeGh().issue(7)
    gh.prs = [_fork_pr(64, head="patch-1", body="Closes #7")]
    gh.timeline[7] = [_xref(64)]  # the same PR found twice is listed once
    assert [p["number"] for p in open_prs_for_issue(PROJECT, 7, gh)] == [64]


# Codex 4239868029: a cloud routine claims only work whose approval label a writer applied.
#: The GraphQL answer for an issue whose body was never edited (Codex 4241196649).
NEVER_EDITED = json.dumps(
    {"data": {"repository": {"issue": {"lastEditedAt": None, "userContentEdits": {"nodes": []}}}}}
)


def _labeler_gh(events_pages, permissions, calls=None, edits=NEVER_EDITED):
    def gh(args, input=None):
        if calls is not None:
            calls.append(list(args))
        path = args[1]
        if path == "graphql":
            assert "lastEditedAt" in args[3]
            if isinstance(edits, Exception):
                raise edits
            return edits
        if "/events" in path:
            assert "--paginate" in args and "--slurp" in args
            if isinstance(events_pages, Exception):
                raise events_pages
            return json.dumps(events_pages)
        login = path.split("/collaborators/")[1].split("/")[0]
        answer = permissions[login]
        if isinstance(answer, Exception):
            raise answer
        return answer

    return gh


def _at(minute: int) -> str:
    return (NOW + timedelta(minutes=minute)).isoformat().replace("+00:00", "Z")


def _label_event(kind, label, login, event_id):
    return {
        "id": event_id,
        "event": kind,
        "label": {"name": label},
        "actor": {"login": login},
        "created_at": _at(event_id),
    }


APPROVAL = "implementation-approved"


def test_authorize_labeler_requires_write_authority_from_the_latest_approver():
    from agentic_sdlc.leases import authorize_labeler
    from agentic_sdlc.onboard import OnboardError

    write = json.dumps({"permission": "write", "role_name": "write"})
    maintain = json.dumps({"permission": "write", "role_name": "maintain"})
    admin = json.dumps({"permission": "admin", "role_name": "admin"})
    triage = json.dumps({"permission": "read", "role_name": "triage"})
    perms = {
        "writer": write,
        "keeper": maintain,
        "boss": admin,
        "triager": triage,
        "stranger": OnboardError("gh: Not Found (HTTP 404)"),
    }
    other = _label_event("labeled", "agent-ready", "writer", 1)
    for login, ok in (
        ("writer", True),
        ("keeper", True),
        ("boss", True),
        ("triager", False),
        ("stranger", False),
    ):
        pages = [[other, _label_event("labeled", APPROVAL, login, 2)]]
        verdict = authorize_labeler(PROJECT, 7, APPROVAL, _labeler_gh(pages, perms))
        assert verdict.authorized is ok and verdict.labeler == login, login
    # the MOST RECENT application decides, across pages, whoever applied it before
    pages = [
        [_label_event("labeled", APPROVAL, "boss", 1)],
        [
            _label_event("unlabeled", APPROVAL, "boss", 2),
            _label_event("labeled", APPROVAL, "triager", 3),
        ],
    ]
    verdict = authorize_labeler(PROJECT, 7, APPROVAL, _labeler_gh(pages, perms))
    assert not verdict.authorized and verdict.labeler == "triager"
    pages = [
        [_label_event("labeled", APPROVAL, "triager", 1)],
        [_label_event("labeled", APPROVAL, "writer", 2)],
    ]
    assert authorize_labeler(PROJECT, 7, APPROVAL, _labeler_gh(pages, perms)).authorized
    # label names are case-insensitive; a label removed after it was applied authorizes nothing
    pages = [[_label_event("labeled", "Implementation-Approved", "writer", 1)]]
    assert authorize_labeler(PROJECT, 7, APPROVAL, _labeler_gh(pages, perms)).authorized
    pages = [
        [
            _label_event("labeled", APPROVAL, "writer", 1),
            _label_event("unlabeled", APPROVAL, "x", 2),
        ]
    ]
    assert not authorize_labeler(PROJECT, 7, APPROVAL, _labeler_gh(pages, perms)).authorized


@pytest.mark.parametrize(
    ("pages", "permission"),
    [
        (OSError("network down"), None),  # events unreadable
        ("not pages", None),
        ([{"event": "labeled"}], None),  # a page that is not a list
        ([[]], None),  # no labeled event at all
        ([[{"event": "labeled", "label": {"name": APPROVAL}}]], None),  # no actor
        ([[{"event": "labeled", "label": None, "actor": {"login": "x"}}]], None),
        ([[_label_event("labeled", APPROVAL, "writer", 1)]], "HTTP 502"),
        ([[_label_event("labeled", APPROVAL, "writer", 1)]], "{}"),
        ([[_label_event("labeled", APPROVAL, "writer", 1)]], "not json"),
    ],
)
def test_authorize_labeler_fails_closed_when_unverifiable(pages, permission):
    from agentic_sdlc.leases import authorize_labeler
    from agentic_sdlc.onboard import OnboardError

    answer = (
        OnboardError(f"gh: Server Error ({permission})") if permission == "HTTP 502" else permission
    )
    perms = {"writer": answer if permission else json.dumps({"permission": "write"})}
    if pages == "not pages":
        gh = lambda args, input=None: "{}" if "/events" in args[1] else perms["writer"]  # noqa: E731
    else:
        gh = _labeler_gh(pages, perms)
    with pytest.raises(LeaseError, match="not authorizing|unreadable"):
        authorize_labeler(PROJECT, 7, APPROVAL, gh)


def test_cli_authorize_labeler_exit_codes(tmp_path, monkeypatch):
    from agentic_sdlc import cli

    calls: list[list[str]] = []
    perms = {
        "writer": json.dumps({"permission": "write"}),
        "triager": json.dumps({"permission": "read", "role_name": "triage"}),
        "bot%5Bbot%5D": json.dumps({"permission": "read"}),
    }
    base = ["authorize-labeler", "--project", PROJECT, "--issue", "7", "--label", APPROVAL]
    for login, code in (("writer", 0), ("triager", 3), ("bot[bot]", 3)):
        pages = [[_label_event("labeled", APPROVAL, login, 1)]]
        monkeypatch.setattr(cli, "run_gh", _labeler_gh(pages, perms, calls))
        out = tmp_path / f"{code}.json"
        assert cli.main([*base, "--output", str(out)]) == code, login
        assert json.loads(out.read_text())["authorized"] is (code == 0)
    assert ["api", f"repos/{PROJECT}/collaborators/bot%5Bbot%5D/permission"] in calls
    monkeypatch.setattr(cli, "run_gh", _labeler_gh(OSError("down"), perms))
    assert cli.main(base) == 2  # unverifiable: an error, never "authorized"


# Codex 4240421144: the cloud routine authorized only the latest applier of the approval label, so
# a Triage user could COMPLETE the set by adding a prerequisite after a writer approved. Whoever
# applied EACH label of the set most recently must hold write authority.
APPROVAL_SET = [APPROVAL, "agent-ready", "human-review-required"]
WRITE_PERMS = {
    "writer": json.dumps({"permission": "write"}),
    "keeper": json.dumps({"permission": "write", "role_name": "maintain"}),
    "triager": json.dumps({"permission": "read", "role_name": "triage"}),
}


@pytest.mark.parametrize(
    ("events", "authorized"),
    [
        # the reported ordering: a writer approved first, a Triage user completed the set
        (
            [
                ("labeled", "agent-ready", "writer"),
                ("labeled", APPROVAL, "writer"),
                ("labeled", "human-review-required", "triager"),
            ],
            False,
        ),
        # a Triage user's prerequisite before a writer's approval
        (
            [
                ("labeled", "agent-ready", "triager"),
                ("labeled", "human-review-required", "writer"),
                ("labeled", APPROVAL, "writer"),
            ],
            False,
        ),
        # re-applied by a writer: the LATEST application of each label counts (any case)
        (
            [
                ("labeled", "agent-ready", "triager"),
                ("unlabeled", "agent-ready", "keeper"),
                ("labeled", "Agent-Ready", "keeper"),
                ("labeled", "human-review-required", "writer"),
                ("labeled", APPROVAL, "writer"),
            ],
            True,
        ),
        # a prerequisite removed since it was last applied
        (
            [
                ("labeled", "agent-ready", "writer"),
                ("labeled", "human-review-required", "writer"),
                ("labeled", APPROVAL, "writer"),
                ("unlabeled", "agent-ready", "writer"),
            ],
            False,
        ),
    ],
)
def test_authorize_labeler_requires_write_authority_for_every_label_of_the_set(events, authorized):
    from agentic_sdlc.leases import authorize_labeler

    pages = [[_label_event(kind, name, login, n) for n, (kind, name, login) in enumerate(events)]]
    verdict = authorize_labeler(PROJECT, 7, APPROVAL_SET, _labeler_gh(pages, WRITE_PERMS))
    assert verdict.authorized is authorized, verdict.reason
    if authorized:
        assert verdict.labeler == "writer" and verdict.label == APPROVAL
        assert dict(verdict.labelers)["agent-ready"] == "keeper"


@pytest.mark.parametrize(
    "events",
    [
        [("labeled", APPROVAL, "writer"), ("labeled", "agent-ready", "writer")],  # one never
        [  # a prerequisite's latest event names no actor
            ("labeled", APPROVAL, "writer"),
            ("labeled", "agent-ready", "writer"),
            ("labeled", "human-review-required", ""),
        ],
    ],
)
def test_authorize_labeler_fails_closed_on_an_unverifiable_prerequisite(events):
    from agentic_sdlc.leases import authorize_labeler

    pages = [[_label_event(kind, name, login, n) for n, (kind, name, login) in enumerate(events)]]
    with pytest.raises(LeaseError, match="not authorizing"):
        authorize_labeler(PROJECT, 7, APPROVAL_SET, _labeler_gh(pages, WRITE_PERMS))
    with pytest.raises(LeaseError, match="not authorizing"):
        authorize_labeler(PROJECT, 7, [], _labeler_gh(pages, WRITE_PERMS))


def test_cli_authorize_labeler_checks_the_whole_approval_set(tmp_path, monkeypatch):
    from agentic_sdlc import cli

    pages = [
        [
            _label_event("labeled", "agent-ready", "writer", 1),
            _label_event("labeled", APPROVAL, "writer", 2),
            _label_event("labeled", "human-review-required", "triager", 3),
        ]
    ]
    monkeypatch.setattr(cli, "run_gh", _labeler_gh(pages, WRITE_PERMS))
    base = ["authorize-labeler", "--project", PROJECT, "--issue", "7"]
    labels = [arg for name in APPROVAL_SET for arg in ("--label", name)]
    assert cli.main([*base, *labels]) == 3  # the Triage user completed the set
    assert cli.main([*base, "--label", APPROVAL]) == 0  # (the approval label alone: a writer)
    # the default: the policy's whole approval set
    policy = tmp_path / "agentic-sdlc.toml"
    policy.write_text(
        'version = 1\n[project]\nid = "owner/repo"\n[automation]\nready_label = "agent-ready"\n'
    )
    out = tmp_path / "verdict.json"
    assert cli.main([*base, "--config", str(policy), "--output", str(out)]) == 3
    assert set(json.loads(out.read_text())["labelers"]) == set(APPROVAL_SET)
    monkeypatch.chdir(tmp_path)
    assert cli.main(base) == 3  # agentic-sdlc.toml in the working directory
    # no readable policy and no --label: an error, never a smaller set
    assert cli.main([*base, "--config", str(tmp_path / "missing.toml")]) == 2


# ---------------------------------------------------------------- Codex 4240306505
# GitHub closes an issue from a PR in ANOTHER repository (`Fixes owner/repo#7`). That PR is not in
# this repository's open-PR inventory, so its timeline cross-reference is resolved: an open PR
# whose closingIssuesReferences name this issue is durable work; an unreadable one fails closed.


def _external(state="OPEN", closes=(("owner/repo", 7),), more=False, number=80):
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "state": state,
                    "url": f"https://x/other/pr/{number}",
                    "title": "external",
                    "closingIssuesReferences": {
                        "pageInfo": {"hasNextPage": more},
                        "nodes": [
                            {"number": n, "repository": {"nameWithOwner": repo}}
                            for repo, n in closes
                        ],
                    },
                }
            }
        }
    }


@pytest.mark.parametrize("target", ["owner/repo", "OWNER/Repo"])
def test_an_open_pr_of_another_repository_that_closes_the_issue_blocks(target):
    gh = FakeGh().issue(7)
    gh.timeline[7] = [_xref(80, repo="other/repo"), _xref(80, repo="Other/Repo")]
    gh.external_prs[("other/repo", 80)] = _external(closes=(("x/y", 7), (target, 7)))
    result = _claim(gh)
    assert not result.ok and "other/pr/80" in result.reason
    assert not any(c[:3] == ("api", "-X", "POST") for c in gh.calls)
    reads = [c for c in gh.calls if c[:2] == ("api", "graphql") and "pullRequest(number:" in c[3]]
    assert len(reads) == 1 and "-F" in reads[0] and "number=80" in reads[0]


def test_an_external_pr_and_a_same_numbered_local_pr_are_both_reported():
    from agentic_sdlc.leases import open_prs_for_issue

    gh = FakeGh().issue(7)
    gh.timeline[7] = [_xref(80), _xref(80, repo="other/repo")]
    gh.external_prs[("other/repo", 80)] = _external()
    hits = open_prs_for_issue(PROJECT, 7, gh)
    assert sorted(h["url"] for h in hits) == ["https://x/other/pr/80", "https://x/pr/80"]


@pytest.mark.parametrize(
    "answer",
    [
        _external(closes=(("owner/repo", 8),)),  # another issue of this repository
        _external(closes=(("other/repo", 7),)),  # issue 7 of its own repository
        _external(closes=()),  # only mentions the issue
        _external(state="MERGED"),  # merged since the timeline was read
        _external(state="CLOSED"),
    ],
)
def test_an_external_pr_that_does_not_close_the_issue_does_not_block(answer):
    gh = FakeGh().issue(7)
    gh.timeline[7] = [_xref(81, repo="other/repo")]
    gh.external_prs[("other/repo", 81)] = answer
    result = _claim(gh)
    assert result.ok, result.reason


@pytest.mark.parametrize(
    "answer",
    [
        None,  # NOT_FOUND: an inaccessible or deleted repository
        RuntimeError("gh: HTTP 502"),
        "not json",
        {"errors": [{"message": "x"}]},
        {"data": {"repository": {"pullRequest": None}}},
        _external(more=True),  # more closing issues than one page
        {"data": {"repository": {"pullRequest": {"state": "OPEN"}}}},
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "state": "OPEN",
                        "closingIssuesReferences": {
                            "pageInfo": {"hasNextPage": False},
                            "nodes": [{"number": 7}],
                        },
                    }
                }
            }
        },
    ],
)
def test_an_unreadable_external_cross_reference_fails_closed_naming_the_pr(answer):
    gh = FakeGh().issue(7)
    gh.timeline[7] = [_xref(82, repo="other/repo")]
    gh.external_prs[("other/repo", 82)] = answer
    with pytest.raises(LeaseError) as raised:
        _claim(gh)
    message = str(raised.value)
    assert "other/repo#82" in message and "no override" in message
    assert not any(c[:3] == ("api", "-X", "POST") for c in gh.calls)


# ---- Codex 4240839923: the inventory follows the authoritative markers, not the mutable label.


def _unlabelled_live_lease() -> FakeGh:
    """Issue 7 holds a live lease whose `in-progress` label a Triage user removed."""
    live = format_claim_marker(Lease(7, "a", "s1", "b", NOW + timedelta(hours=1)))
    return FakeGh().issue(7, labels=["bug"], comments=[live])


def test_claims_lists_a_live_lease_whose_label_was_removed():
    gh = _unlabelled_live_lease()
    gh.issue(8, labels=["bug"], comments=["just chatting"])
    rows = list_claims(PROJECT, gh, now=NOW)
    assert [(r.lease.issue, r.lease.session, r.expired) for r in rows] == [(7, "s1", False)]
    # the comments are read only as far back as the longest lease a marker can carry
    since = next(c[1] for c in gh.calls if c[1].startswith(f"repos/{PROJECT}/issues/comments?"))
    assert "since=2026-09-23T12:00:00Z" in since


def test_claims_skips_marker_comments_on_pull_requests_and_closed_issues():
    gh = _unlabelled_live_lease()
    gh.issues[7]["extra"] = {"pull_request": {"url": "x"}}
    live = format_claim_marker(Lease(9, "a", "s9", "b", NOW + timedelta(hours=1)))
    gh.issue(9, comments=[live])
    gh.issues[9]["state"] = "closed"
    assert list_claims(PROJECT, gh, now=NOW) == []


def test_claims_inventory_is_incomplete_when_recent_comments_are_unreadable(monkeypatch, capsys):
    from agentic_sdlc import cli
    from agentic_sdlc.leases import claim_inventory

    gh = _unlabelled_live_lease()
    gh.repo_comments_error = RuntimeError("gh: HTTP 502")
    inventory = claim_inventory(PROJECT, gh, now=NOW)
    assert not inventory.complete and "page 1 unreadable" in inventory.gaps[0]
    with pytest.raises(LeaseError, match="INCOMPLETE"):
        list_claims(PROJECT, gh, now=NOW)
    monkeypatch.setattr(cli, "run_gh", gh)
    assert cli.main(["claims", "--project", PROJECT]) == 3
    captured = capsys.readouterr()
    assert json.loads(captured.out) == [] and "INCOMPLETE" in captured.err


def test_claims_inventory_is_incomplete_at_the_comment_page_cap():
    from agentic_sdlc.leases import CLAIMS_COMMENT_PAGE_SIZE, claim_inventory

    gh = _unlabelled_live_lease()
    gh.issue(8, comments=[f"chatter {i}" for i in range(2 * CLAIMS_COMMENT_PAGE_SIZE)])
    inventory = claim_inventory(PROJECT, gh, now=NOW, page_cap=2)
    assert gh.repo_comment_pages == [1, 2]
    assert not inventory.complete and "page cap reached" in inventory.gaps[0]
    full = claim_inventory(PROJECT, gh, now=NOW, page_cap=3)
    assert full.complete and [r.lease.issue for r in full.rows] == [7]


# ---------------------------------------------------------------- Codex review 5482912640
# Codex 4241196649: an approval is bound to the issue content it saw. A title or body edited at or
# after the latest application of ANY label of the set leaves the issue unauthorized until the
# labels are re-applied; an edit time that cannot be read refuses.
def _edits(last_edited=None, edits=()):
    issue = {"lastEditedAt": last_edited, "userContentEdits": {"nodes": list(edits)}}
    return json.dumps({"data": {"repository": {"issue": issue}}})


def _approved_set_pages(*extra):
    return [
        [
            _label_event("labeled", "agent-ready", "writer", 1),
            _label_event("labeled", "human-review-required", "writer", 2),
            _label_event("labeled", APPROVAL, "writer", 3),
            *extra,
        ]
    ]


@pytest.mark.parametrize(
    ("extra", "edits", "authorized"),
    [
        ([], _edits(), True),  # never edited
        ([], _edits(_at(0)), True),  # body edited before every label was applied
        ([], _edits(_at(2)), False),  # body edited after a prerequisite, before the approval
        ([], _edits(_at(5)), False),  # body edited after the whole set was applied
        ([], _edits(_at(3)), False),  # the same instant as the approval: not proven before it
        ([], _edits(None, [{"editedAt": _at(9)}]), False),  # the newest edit entry counts too
        ([{"event": "renamed", "created_at": _at(4), "actor": {"login": "x"}}], _edits(), False),
        ([{"event": "renamed", "created_at": _at(0), "actor": {"login": "x"}}], _edits(), True),
    ],
)
def test_authorize_labeler_binds_the_approval_to_the_issue_content(extra, edits, authorized):
    from agentic_sdlc.leases import authorize_labeler

    pages = _approved_set_pages(*extra)
    verdict = authorize_labeler(
        PROJECT, 7, APPROVAL_SET, _labeler_gh(pages, WRITE_PERMS, None, edits)
    )
    assert verdict.authorized is authorized, verdict.reason
    if not authorized:
        assert "edited" in verdict.reason and "re-apply" in verdict.reason


def test_authorize_labeler_reapplied_labels_approve_the_edited_content():
    from agentic_sdlc.leases import authorize_labeler

    pages = _approved_set_pages(
        *(
            _label_event(kind, name, "writer", n)
            for n, (kind, name) in enumerate(
                [(k, name) for name in APPROVAL_SET for k in ("unlabeled", "labeled")], start=10
            )
        )
    )
    verdict = authorize_labeler(
        PROJECT, 7, APPROVAL_SET, _labeler_gh(pages, WRITE_PERMS, None, _edits(_at(5)))
    )
    assert verdict.authorized, verdict.reason


@pytest.mark.parametrize(
    "edits",
    [
        OSError("network down"),
        "not json",
        json.dumps({"errors": [{"message": "boom"}]}),
        json.dumps({"data": {"repository": {"issue": {"userContentEdits": {"nodes": []}}}}}),
        json.dumps({"data": {"repository": {"issue": {"lastEditedAt": None}}}}),
        _edits("yesterday"),
        _edits(None, [{"editedAt": None}]),
    ],
)
def test_authorize_labeler_fails_closed_when_the_edit_time_is_unreadable(edits):
    from agentic_sdlc.leases import authorize_labeler

    gh = _labeler_gh(_approved_set_pages(), WRITE_PERMS, None, edits)
    with pytest.raises(LeaseError, match="not authorizing"):
        authorize_labeler(PROJECT, 7, APPROVAL_SET, gh)
    # a label event without a time cannot be ordered against an edit either
    pages = [[{**_label_event("labeled", APPROVAL, "writer", 1), "created_at": None}]]
    with pytest.raises(LeaseError, match="not authorizing"):
        authorize_labeler(PROJECT, 7, APPROVAL, _labeler_gh(pages, WRITE_PERMS))
    pages = _approved_set_pages({"event": "renamed", "created_at": "", "actor": {"login": "x"}})
    with pytest.raises(LeaseError, match="not authorizing"):
        authorize_labeler(PROJECT, 7, APPROVAL_SET, _labeler_gh(pages, WRITE_PERMS))


def test_cli_authorize_labeler_refuses_content_edited_after_approval(monkeypatch):
    from agentic_sdlc import cli

    base = ["authorize-labeler", "--project", PROJECT, "--issue", "7"]
    labels = [arg for name in APPROVAL_SET for arg in ("--label", name)]
    gh = _labeler_gh(_approved_set_pages(), WRITE_PERMS, None, _edits(_at(5)))
    monkeypatch.setattr(cli, "run_gh", gh)
    assert cli.main([*base, *labels]) == 3
    monkeypatch.setattr(
        cli, "run_gh", _labeler_gh(_approved_set_pages(), WRITE_PERMS, None, OSError("x"))
    )
    assert cli.main([*base, *labels]) == 2


# Codex 4241196645: a pull request opened between the open-PR inventory and the claim marker is
# found by a second read after winning, and the claim is retracted.
def _late_pr():
    return {
        "number": 70,
        "url": "https://x/pr/70",
        "title": "Fix it",
        "body": "Closes #7",
        "headRefName": "patch",
    }


def test_a_pull_request_opened_while_claiming_retracts_the_claim():
    gh = FakeGh().issue(7)
    gh.before_post = lambda n: gh.prs.append(_late_pr())
    result = _claim(gh)
    assert not result.ok and "pr/70" in result.reason and "while" in result.reason
    assert current_lease(PROJECT, 7, gh, NOW) is None  # the marker was retracted
    assert IN_PROGRESS_LABEL not in [lbl["name"] for lbl in gh.issues[7]["labels"]]
    assert "<!-- forge-release" in gh.issues[7]["comments"][-1]["body"]


def test_an_unreadable_recheck_of_open_pull_requests_retracts_the_claim():
    gh = FakeGh().issue(7)
    posted = {"done": False}

    def flaky(args, input=None):
        if tuple(args[:3]) == ("api", "-X", "POST"):
            posted["done"] = True
        if posted["done"] and args[:2] == ["api", "graphql"] and "pullRequests(" in args[3]:
            raise RuntimeError("gh: Server Error (HTTP 502)")
        return gh(args, input)

    with pytest.raises(RuntimeError, match="HTTP 502"):
        claim(PROJECT, 7, agent="me", session="s1", branch="b", ttl_minutes=60, gh=flaky, now=NOW)
    assert current_lease(PROJECT, 7, gh, NOW) is None
    assert IN_PROGRESS_LABEL not in [lbl["name"] for lbl in gh.issues[7]["labels"]]


# Codex 4241532620: the issue's state was read before the marker was posted; an issue closed in
# between is found by a re-read after winning, and the claim is retracted.
def test_an_issue_closed_while_claiming_retracts_the_claim():
    gh = FakeGh().issue(7)
    gh.before_post = lambda n: gh.issues[n].update(state="closed")
    result = _claim(gh)
    assert not result.ok and "closed" in result.reason and "while" in result.reason
    assert current_lease(PROJECT, 7, gh, NOW) is None  # the marker was retracted
    assert IN_PROGRESS_LABEL not in [lbl["name"] for lbl in gh.issues[7]["labels"]]
    last = gh.issues[7]["comments"][-1]["body"]
    assert last.startswith("<!-- forge-release") and "closed now" in last


def test_an_unreadable_recheck_of_the_issue_retracts_the_claim():
    gh = FakeGh().issue(7)
    posted = {"done": False}

    def flaky(args, input=None):
        if tuple(args[:3]) == ("api", "-X", "POST"):
            posted["done"] = True
        if posted["done"] and list(args[:2]) == ["api", f"repos/{PROJECT}/issues/7"]:
            raise RuntimeError("gh: Server Error (HTTP 502)")
        return gh(args, input)

    with pytest.raises(RuntimeError, match="HTTP 502"):
        claim(PROJECT, 7, agent="me", session="s1", branch="b", ttl_minutes=60, gh=flaky, now=NOW)
    assert current_lease(PROJECT, 7, gh, NOW) is None
    assert IN_PROGRESS_LABEL not in [lbl["name"] for lbl in gh.issues[7]["labels"]]


# Codex 4241196647: the TTL starts when the marker is posted, not when the operation began, and
# arbitration reads a fresh clock: a marker already expired by then is retracted.
def _slow(gh, clock, *, on_timeline=timedelta(0), on_first_post=timedelta(0)):
    state = {"posts": 0, "timelines": 0}

    def run(args, input=None):
        args = list(args)
        if args[0] == "api" and "/timeline" in args[1]:
            state["timelines"] += 1
            if state["timelines"] == 1:  # the acquisition's own reads are the slow ones
                clock["now"] += on_timeline
                gh.posted_at = clock["now"]
        out = gh(args, input)
        if args[:3] == ["api", "-X", "POST"]:
            state["posts"] += 1
            if state["posts"] == 1:
                clock["now"] += on_first_post
                gh.posted_at = clock["now"]
        return out

    return run


def test_the_lease_ttl_starts_after_the_acquisition_reads():
    gh = FakeGh().issue(7)
    clock = {"now": NOW}
    slow = _slow(gh, clock, on_timeline=timedelta(minutes=5))
    result = claim(
        PROJECT,
        7,
        agent="me",
        session="s1",
        branch="b",
        ttl_minutes=3,
        gh=slow,
        clock=lambda: clock["now"],
    )
    assert result.ok, result.reason
    assert result.lease.expires == NOW + timedelta(minutes=8)
    held = current_lease(PROJECT, 7, gh, clock["now"])
    assert held is not None and held.session == "s1" and held.live(clock["now"])


def test_a_claim_expired_before_arbitration_is_retracted():
    gh = FakeGh().issue(7)
    clock = {"now": NOW}
    slow = _slow(gh, clock, on_first_post=timedelta(minutes=5))
    result = claim(
        PROJECT,
        7,
        agent="me",
        session="s1",
        branch="b",
        ttl_minutes=3,
        gh=slow,
        clock=lambda: clock["now"],
    )
    assert not result.ok and "no longer live" in result.reason
    bodies = [c["body"] for c in gh.issues[7]["comments"]]
    assert bodies[-1].startswith("<!-- forge-release") and "already expired" in bodies[-1]
    assert IN_PROGRESS_LABEL not in [lbl["name"] for lbl in gh.issues[7]["labels"]]


def test_a_renewal_expiry_is_computed_when_it_is_posted():
    gh = FakeGh().issue(7)
    assert _claim(gh).ok
    clock = {"now": NOW + timedelta(minutes=10)}
    gh.posted_at = clock["now"]
    lease = renew(PROJECT, 7, session="s1", ttl_minutes=30, gh=gh, clock=lambda: clock["now"])
    assert lease.expires == NOW + timedelta(minutes=40)


# Codex 4241327872: a claim that fails after posting its marker must not leave a live lease the
# failing caller never releases. Every failure after the post -- the arbitration re-read, the
# open-PR re-check and the label bookkeeping -- retracts the marker, retried up to
# RETRACT_ATTEMPTS times, and the ORIGINAL failure is what the caller sees.
class _Faults:
    """Wraps a FakeGh: after the first claim marker is posted, the next `reads` thread re-reads
    fail; the first `retract_failures` release-marker POSTs fail (without posting)."""

    def __init__(self, gh, *, reads=0, retract_failures=0, lose_post_answer=False):
        self.gh = gh
        self.reads = reads
        self.retract_failures = retract_failures
        self.lose_post_answer = lose_post_answer
        self.posted = False
        self.retract_attempts = 0

    def __call__(self, args, input=None):
        args = tuple(args)
        posting = args[:3] == ("api", "-X", "POST") and args[3].endswith("/comments")
        body = json.loads(input or "{}").get("body", "") if posting else ""
        if posting and "forge-release" in body:
            self.retract_attempts += 1
            if self.retract_failures:
                self.retract_failures -= 1
                raise RuntimeError("gh: Server Error (HTTP 502) on retraction")
        if posting and "forge-claim" in body and not self.posted:
            self.posted = True
            answer = self.gh(args, input)
            if self.lose_post_answer:
                raise RuntimeError("gh: connection reset after the POST")
            return answer
        reading = args[0] == "api" and args[1].split("?")[0].endswith("/issues/7/comments")
        if self.posted and reading and "-X" not in args and self.reads:
            self.reads -= 1
            raise RuntimeError("gh: Server Error (HTTP 502) on re-read")
        return self.gh(args, input)


@pytest.fixture
def no_retract_pause(monkeypatch):
    import agentic_sdlc.leases as leases

    pauses: list[float] = []
    monkeypatch.setattr(leases, "_sleep", pauses.append)
    return pauses


@pytest.mark.parametrize("retract_failures", [0, 2])
def test_a_claim_whose_arbitration_read_fails_retracts_its_marker(
    no_retract_pause, retract_failures
):
    gh = FakeGh().issue(7)
    faults = _Faults(gh, reads=1, retract_failures=retract_failures)
    with pytest.raises(RuntimeError, match="on re-read"):
        _claim(faults)
    assert faults.retract_attempts == retract_failures + 1
    assert len(no_retract_pause) == retract_failures  # a pause before each retry only
    assert "forge-release session=s1" in gh.issues[7]["comments"][-1]["body"]
    assert current_lease(PROJECT, 7, gh, NOW) is None  # no live lease left behind


def test_a_claim_whose_post_answer_is_lost_retracts_the_marker_that_landed(no_retract_pause):
    gh = FakeGh().issue(7)
    faults = _Faults(gh, lose_post_answer=True)
    with pytest.raises(RuntimeError, match="connection reset"):
        _claim(faults)
    assert current_lease(PROJECT, 7, gh, NOW) is None


def test_a_retraction_that_fails_every_attempt_is_bounded_and_keeps_the_original_error(
    no_retract_pause,
):
    from agentic_sdlc.leases import RETRACT_ATTEMPTS

    gh = FakeGh().issue(7)
    faults = _Faults(gh, reads=1, retract_failures=99)
    with pytest.raises(RuntimeError, match="on re-read"):  # not the retraction's error
        _claim(faults)
    assert faults.retract_attempts == RETRACT_ATTEMPTS == 3
    assert len(no_retract_pause) == RETRACT_ATTEMPTS - 1


def test_an_unreadable_pr_recheck_retries_the_retraction(no_retract_pause):
    gh = FakeGh().issue(7)
    faults = _Faults(gh, retract_failures=1)

    def flaky(args, input=None):
        if faults.posted and args[:2] == ["api", "graphql"] and "pullRequests(" in args[3]:
            raise RuntimeError("gh: Server Error (HTTP 502) on PR re-check")
        return faults(args, input)

    with pytest.raises(RuntimeError, match="PR re-check"):
        claim(PROJECT, 7, agent="me", session="s1", branch="b", ttl_minutes=60, gh=flaky, now=NOW)
    assert faults.retract_attempts == 2
    assert current_lease(PROJECT, 7, gh, NOW) is None
    assert IN_PROGRESS_LABEL not in [lbl["name"] for lbl in gh.issues[7]["labels"]]


def test_failed_label_bookkeeping_retries_the_retraction(no_retract_pause):
    gh = FakeGh().issue(7)
    faults = _Faults(gh, retract_failures=2)

    def failing_label(args, input=None):
        if tuple(args[:2]) == ("issue", "edit") and "--add-label" in args:
            raise RuntimeError("gh: label edit failed")
        return faults(args, input)

    with pytest.raises(RuntimeError, match="label edit failed"):
        claim(
            PROJECT,
            7,
            agent="me",
            session="s1",
            branch="b",
            gh=failing_label,
            now=NOW,
            assignee="nobody",
        )
    assert faults.retract_attempts == 3
    assert current_lease(PROJECT, 7, gh, NOW) is None
    assert _logins(gh) == []


def test_failed_label_bookkeeping_retracts_even_when_the_ownership_check_raises(
    no_retract_pause, monkeypatch
):
    import agentic_sdlc.leases as leases

    def boom(*_args, **_kwargs):
        raise RuntimeError("ownership check exploded")

    monkeypatch.setattr(leases, "_confirm_own_assignment", boom)
    gh = FakeGh().issue(7)
    real = gh.__call__

    def failing_assignee(args, input=None):
        if tuple(args[:2]) == ("issue", "edit") and "--add-assignee" in args:
            raise RuntimeError("invalid assignee")
        return real(args, input=input)

    with pytest.raises(RuntimeError, match="invalid assignee"):
        claim(
            PROJECT,
            7,
            agent="me",
            session="s1",
            branch="b",
            gh=failing_assignee,
            now=NOW,
            assignee="nobody",
        )
    assert current_lease(PROJECT, 7, gh, NOW) is None


def test_a_renewal_whose_arbitration_read_fails_keeps_the_lease(no_retract_pause):
    # the session already held the lease: retracting the renewal would end that lease too
    mine = format_claim_marker(Lease(7, "me", "s1", "b", NOW + timedelta(minutes=5)))
    gh = FakeGh().issue(7, labels=[IN_PROGRESS_LABEL], comments=[mine])
    faults = _Faults(gh, reads=1)
    with pytest.raises(RuntimeError, match="on re-read"):
        renew(PROJECT, 7, session="s1", ttl_minutes=60, gh=faults, now=NOW)
    assert faults.retract_attempts == 0
    assert current_lease(PROJECT, 7, gh, NOW).session == "s1"


# Codex 4241434506: the cloud routine renews between stages and immediately before pushing, and
# relies on `sdlcctl renew` failing unless THIS session still holds a live lease.
@pytest.mark.parametrize("holder", ["other", "expired", "none"])
def test_cli_renew_fails_unless_this_session_holds_a_live_lease(holder, monkeypatch, capsys):
    from agentic_sdlc import cli

    wall = datetime.now(UTC)
    gh = FakeGh().issue(7, labels=[IN_PROGRESS_LABEL])
    gh.posted_at = wall
    if holder == "other":  # another session took the issue over
        lease = Lease(7, "them", "s2", "b", wall + timedelta(hours=1))
        gh.issues[7]["comments"].append(
            _comment_row(1, format_claim_marker(lease), wall.isoformat())
        )
    elif holder == "expired":  # this session's own lease ran out: up for takeover
        lease = Lease(7, "me", "s1", "b", wall - timedelta(minutes=1))
        gh.issues[7]["comments"].append(
            _comment_row(1, format_claim_marker(lease), (wall - timedelta(hours=1)).isoformat())
        )
    monkeypatch.setattr(cli, "run_gh", gh)
    argv = ["renew", "--project", PROJECT, "--issue", "7", "--session", "s1"]
    assert cli.main(argv) == 2
    assert "ERROR" in capsys.readouterr().err
    assert not any(c[:3] == ("api", "-X", "POST") for c in gh.calls)  # nothing posted


# Codex 4241614587: every renewal is also a state check -- an issue closed after the claim (while
# the routine implements or verifies) stops the renewal steps before the push.
def test_renew_refuses_a_closed_issue_without_posting():
    mine = format_claim_marker(Lease(7, "me", "s1", "b", NOW + timedelta(minutes=5)))
    gh = FakeGh().issue(7, labels=[IN_PROGRESS_LABEL], comments=[mine])
    gh.issues[7]["state"] = "closed"
    with pytest.raises(LeaseError, match="is closed; not renewing"):
        renew(PROJECT, 7, session="s1", ttl_minutes=60, gh=gh, now=NOW)
    assert not any(c[:3] == ("api", "-X", "POST") for c in gh.calls)


@pytest.mark.parametrize(
    "answer", [RuntimeError("gh: HTTP 502"), "null", "not json", {"state": None}, {"pr": 1}]
)
def test_renew_refuses_when_the_issue_state_cannot_be_read(answer):
    mine = format_claim_marker(Lease(7, "me", "s1", "b", NOW + timedelta(minutes=5)))
    gh = FakeGh().issue(7, labels=[IN_PROGRESS_LABEL], comments=[mine])
    issue_path = f"repos/{PROJECT}/issues/7"

    def faulty(args, input=None):
        if tuple(args) == ("api", issue_path):
            if isinstance(answer, Exception):
                raise answer
            if answer == {"pr": 1}:
                return json.dumps({"state": "open", "pull_request": {"url": "x"}})
            return answer if isinstance(answer, str) else json.dumps(answer)
        return gh(args, input)

    with pytest.raises(LeaseError):
        renew(PROJECT, 7, session="s1", ttl_minutes=60, gh=faulty, now=NOW)
    assert not any(c[:3] == ("api", "-X", "POST") for c in gh.calls)


def test_cli_renew_fails_once_the_issue_is_closed(monkeypatch, capsys):
    from agentic_sdlc import cli

    wall = datetime.now(UTC)
    gh = FakeGh().issue(7, labels=[IN_PROGRESS_LABEL])
    gh.posted_at = wall
    lease = Lease(7, "me", "s1", "b", wall + timedelta(hours=1))
    gh.issues[7]["comments"].append(_comment_row(1, format_claim_marker(lease), wall.isoformat()))
    monkeypatch.setattr(cli, "run_gh", gh)
    argv = ["renew", "--project", PROJECT, "--issue", "7", "--session", "s1"]
    assert cli.main([*argv, "--ttl-minutes", "60"]) == 0  # open: renews
    gh.issues[7]["state"] = "closed"
    assert cli.main([*argv, "--ttl-minutes", "60"]) == 2
    assert "is closed; not renewing" in capsys.readouterr().err


# ---------------------------------------------------------------- Codex 4242643524
# An explicit --config that is missing or unreadable is an error (exit 2) before any GitHub
# call, never a quiet fall-back to the default lease TTL; only an omitted --config uses defaults.

_POLICY = 'version = 1\n[project]\nid = "owner/repo"\n[automation]\nlease_ttl_minutes = 30\n'


@pytest.mark.parametrize("subcommand", ["claim", "renew", "release", "authorize-labeler"])
@pytest.mark.parametrize("problem", ["missing", "unparseable"])
def test_an_explicit_config_that_cannot_be_read_is_an_error(
    subcommand, problem, tmp_path, monkeypatch, capsys
):
    from agentic_sdlc import cli

    config = tmp_path / "agentic-sdlc.toml"
    if problem == "unparseable":
        config.write_text("version = [\n")
    # a readable policy in the working directory must not stand in for the one named
    (tmp_path / "cwd").mkdir()
    (tmp_path / "cwd" / "agentic-sdlc.toml").write_text(_POLICY)
    monkeypatch.chdir(tmp_path / "cwd")
    gh = FakeGh().issue(7)
    monkeypatch.setattr(cli, "run_gh", gh)
    argv = {
        "claim": ["claim", "--agent", "me", "--branch", "b"],
        "renew": ["renew"],
        "release": ["release"],
        "authorize-labeler": ["authorize-labeler", "--label", APPROVAL],
    }[subcommand] + ["--project", PROJECT, "--issue", "7", "--config", str(config)]
    if subcommand != "authorize-labeler":
        argv += ["--session", "s1"]
    assert cli.main(argv) == 2
    assert str(config) in capsys.readouterr().err
    assert gh.calls == []  # refused before any GitHub call
    if subcommand in ("claim", "renew"):  # an explicit TTL does not excuse it either
        assert cli.main([*argv, "--ttl-minutes", "60"]) == 2
        assert gh.calls == []


def test_the_lease_ttl_comes_from_the_named_config_else_the_default(tmp_path, monkeypatch):
    import argparse

    from agentic_sdlc import cli

    named = tmp_path / "named.toml"
    named.write_text(_POLICY)
    monkeypatch.chdir(tmp_path)
    assert cli._lease_ttl(argparse.Namespace(ttl_minutes=None, config=str(named))) == 30
    assert cli._lease_ttl(argparse.Namespace(ttl_minutes=5, config=str(named))) == 5
    # omitted: ./agentic-sdlc.toml when it exists, else the default
    assert cli._lease_ttl(argparse.Namespace(ttl_minutes=None, config=None)) == 240
    (tmp_path / "agentic-sdlc.toml").write_text(_POLICY.replace("30", "45"))
    assert cli._lease_ttl(argparse.Namespace(ttl_minutes=None, config=None)) == 45


def test_the_guard_trusts_the_publisher_bot_onboard_rendered_into_it(monkeypatch, tmp_path):
    """Codex 4242753917: the cloud session's guard reads no policy at run time; onboard renders
    the policy's `publisher_bot_login` into it, and an unset one trusts no App bot."""
    import importlib.util

    from agentic_sdlc.leases import LEASE_BOT_LOGINS_ENV
    from agentic_sdlc.onboard import OnboardSpec, render_hooks

    monkeypatch.delenv(LEASE_BOT_LOGINS_ENV, raising=False)
    row = _comment_row(1, "", NOW.isoformat(), "my-forge[bot]", "Bot", "NONE")
    for login, trusted in (("my-forge[bot]", True), ("", False), ("other[bot]", False)):
        text = render_hooks(
            OnboardSpec(
                project_id=PROJECT,
                platform_repository="owner/agentic-sdlc",
                platform_ref="e" * 40,
                test_command="pytest",
                publisher_bot_login=login,
            )
        )[".claude/hooks/forge_commit_guard.py"]
        path = tmp_path / f"guard_{len(login)}.py"
        path.write_text(text)
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.trusted_marker_author(row, lambda _: None) is trusted, login


def test_the_lease_commands_take_the_publisher_bot_from_the_policy(tmp_path, monkeypatch):
    import argparse

    from agentic_sdlc import cli, leases

    policy = tmp_path / "agentic-sdlc.toml"
    policy.write_text(
        'version = 1\n[project]\nid = "owner/repo"\n'
        '[automation]\npublisher_bot_login = "my-forge[bot]"\n'
    )
    monkeypatch.delenv(leases.LEASE_BOT_LOGINS_ENV, raising=False)
    try:
        cli._lease_policy(argparse.Namespace(config=str(policy)))
        assert leases.configured_bot_logins() == "my-forge[bot]"
        monkeypatch.chdir(tmp_path)  # omitted --config: ./agentic-sdlc.toml
        leases.use_policy_bot_logins("")
        cli._lease_policy(argparse.Namespace(config=None))
        assert leases.configured_bot_logins() == "my-forge[bot]"
        policy.write_text(policy.read_text().replace("my-forge[bot]", "agentic-sdlc"))
        with pytest.raises(LeaseError):
            cli._lease_policy(argparse.Namespace(config=str(policy)))
    finally:
        leases.use_policy_bot_logins("")


# ---------------------------------------------------------------- Codex 4242925440
# The failure-cleanup job runs `release` in an empty workspace with no --config and no
# FORGE_LEASE_BOT_LOGINS. It must still release the Publisher bot's claim of its session.

_APP = "my-forge[bot]"
_APP_POLICY = (
    f'version = 1\n[project]\nid = "{PROJECT}"\n[automation]\npublisher_bot_login = "{_APP}"\n'
)


def _workflow_release_argv(job: str, env: dict[str, str]) -> list[str]:
    """The `python3 -m agentic_sdlc release ...` arguments of `job` in the platform's
    reusable-implement.yml, exactly as written there, with the shell variables expanded."""
    import re
    import shlex
    from pathlib import Path

    text = (
        Path(__file__).resolve().parents[1] / ".github/workflows/reusable-implement.yml"
    ).read_text()
    section = text.split(f"\n  {job}:\n", 1)[1]
    command, lines = "", iter(section.splitlines())
    for line in lines:
        if "-m agentic_sdlc release" in line:
            command = line
            while command.rstrip().endswith("\\"):
                command = command.rstrip()[:-1] + " " + next(lines).strip()
            break
    assert command, f"no release call in {job}"
    expanded = re.sub(r"\$\{(\w+)\}|\$(\w+)", lambda m: env[m.group(1) or m.group(2)], command)
    words = shlex.split(expanded.split(" || ", 1)[0])
    return words[words.index("agentic_sdlc") + 1 :]


def _app_claimed(wall: datetime, session: str) -> FakeGh:
    gh = FakeGh().issue(7, labels=[IN_PROGRESS_LABEL])
    gh.posted_at = wall
    gh.poster = {"login": _APP, "kind": "Bot", "association": "NONE"}  # the App's token
    claim_ = format_claim_marker(Lease(7, "forge-actions", session, "b", wall + timedelta(hours=1)))
    gh.issues[7]["comments"].append(_comment_row(1, claim_, wall.isoformat(), _APP, "Bot", "NONE"))
    return gh


def _released(gh: FakeGh, session: str) -> list[dict]:
    return [
        c
        for c in gh.issues[7]["comments"]
        if c["body"].startswith(f"<!-- forge-release session={session} -->")
    ]


@pytest.mark.parametrize("committed", ["policy", "unreadable", "absent"])
def test_the_failure_cleanup_release_call_releases_the_publisher_claim(
    committed, tmp_path, monkeypatch, capsys
):
    """The exact `release_lease_on_failure` invocation (no --config, no env, empty
    workspace): the committed policy names the Publisher bot; if it cannot be read (an
    issues-only token on a private repository) or there is none, `release` still releases
    its own session's claim rather than reporting success over a claim it ignored."""
    from agentic_sdlc import cli, leases

    monkeypatch.delenv(leases.LEASE_BOT_LOGINS_ENV, raising=False)
    monkeypatch.chdir(tmp_path)  # the job checks nothing out
    wall = datetime.now(UTC)
    env = {
        "GITHUB_REPOSITORY": PROJECT,
        "ISSUE_NUMBER": "7",
        "GITHUB_RUN_ID": "4242",
        "GITHUB_SERVER_URL": "https://github.com",
    }
    argv = _workflow_release_argv("release_lease_on_failure", env)
    assert "--config" not in argv and argv[argv.index("--session") + 1] == "run-4242"
    gh = _app_claimed(wall, "run-4242")
    gh.committed_policy = {
        "policy": _APP_POLICY,
        "unreadable": RuntimeError("gh: Resource not accessible by integration (HTTP 403)"),
        "absent": None,
    }[committed]
    monkeypatch.setattr(cli, "run_gh", gh)
    try:
        assert cli.main(argv) == 0
        assert len(_released(gh, "run-4242")) == 1
        # every configured reader (the next claim, the guard) sees the issue free
        leases.use_policy_bot_logins(_APP)
        assert current_lease(PROJECT, 7, gh, wall) is None
        assert IN_PROGRESS_LABEL not in [lbl["name"] for lbl in gh.issues[7]["labels"]]
        if committed == "unreadable":
            assert "WARNING" in capsys.readouterr().err
        # idempotent: a retried cleanup posts nothing more
        leases.use_policy_bot_logins("")
        assert cli.main(argv) == 0
        assert len(_released(gh, "run-4242")) == 1
    finally:
        leases.use_policy_bot_logins("")


def test_the_draft_release_call_works_from_the_checked_out_base(tmp_path, monkeypatch):
    """`publish_draft`'s release runs in the checked-out base: ./agentic-sdlc.toml names the
    bot, so the trusted path releases it (label and all) with no extra read."""
    from agentic_sdlc import cli, leases

    monkeypatch.delenv(leases.LEASE_BOT_LOGINS_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "agentic-sdlc.toml").write_text(_APP_POLICY)
    env = {"GITHUB_REPOSITORY": PROJECT, "ISSUE_NUMBER": "7", "GITHUB_RUN_ID": "77"}
    argv = _workflow_release_argv("publish_draft", env)
    gh = _app_claimed(datetime.now(UTC), "run-77")
    monkeypatch.setattr(cli, "run_gh", gh)
    try:
        assert cli.main(argv) == 0
        assert len(_released(gh, "run-77")) == 1
        assert not any("contents/" in " ".join(c) for c in gh.calls)
    finally:
        leases.use_policy_bot_logins("")


@pytest.mark.parametrize("subcommand", ["claim", "renew", "claims"])
def test_claim_and_renew_fail_closed_when_the_committed_policy_is_unreadable(
    subcommand, tmp_path, monkeypatch, capsys
):
    from agentic_sdlc import cli, leases

    monkeypatch.delenv(leases.LEASE_BOT_LOGINS_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    gh = _app_claimed(datetime.now(UTC), "s1")
    gh.committed_policy = RuntimeError("gh: Resource not accessible by integration (HTTP 403)")
    monkeypatch.setattr(cli, "run_gh", gh)
    argv = {
        "claim": ["claim", "--agent", "me", "--branch", "b"],
        "renew": ["renew"],
        "claims": ["claims"],
    }[subcommand] + ["--project", PROJECT]
    if subcommand != "claims":
        argv += ["--issue", "7", "--session", "s1"]
    before = len(gh.issues[7]["comments"])
    try:
        assert cli.main(argv) == 2
        assert "committed policy" in capsys.readouterr().err
        assert len(gh.issues[7]["comments"]) == before  # nothing posted
    finally:
        leases.use_policy_bot_logins("")


def test_the_committed_policy_supplies_bot_and_ttl(tmp_path, monkeypatch):
    import argparse

    from agentic_sdlc import cli, leases

    monkeypatch.delenv(leases.LEASE_BOT_LOGINS_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    gh = FakeGh()
    gh.committed_policy = _APP_POLICY + "lease_ttl_minutes = 33\n"
    monkeypatch.setattr(cli, "run_gh", gh)
    try:
        args = argparse.Namespace(ttl_minutes=None, config=None, project=PROJECT)
        assert cli._lease_ttl(args) == 33
        assert leases.configured_bot_logins() == _APP
        gh.committed_policy = "version = [\n"  # committed but unparseable: an error
        with pytest.raises(LeaseError, match="committed policy"):
            cli._lease_ttl(args)
        # Codex 4243076065: FORGE_LEASE_BOT_LOGINS overrides only the bot; the committed
        # policy is still read for its TTL
        gh.committed_policy = _APP_POLICY + "lease_ttl_minutes = 33\n"
        monkeypatch.setenv(leases.LEASE_BOT_LOGINS_ENV, "other[bot]")
        assert cli._lease_ttl(args) == 33
        assert leases.configured_bot_logins() == "other[bot]"
        assert any("contents/" in " ".join(c) for c in gh.calls)
        gh.committed_policy = "version = [\n"  # and an unreadable one is still an error
        with pytest.raises(LeaseError, match="committed policy"):
            cli._lease_ttl(args)
    finally:
        leases.use_policy_bot_logins("")


@pytest.mark.parametrize("subcommand", ["claim", "renew", "claims"])
def test_the_env_bot_override_still_fails_closed_on_an_unreadable_committed_policy(
    subcommand, tmp_path, monkeypatch, capsys
):
    """Codex 4243076065: with FORGE_LEASE_BOT_LOGINS set the committed policy is still read,
    and claim/renew/claims still refuse (exit 2) when it cannot be."""
    from agentic_sdlc import cli, leases

    monkeypatch.setenv(leases.LEASE_BOT_LOGINS_ENV, _APP)
    monkeypatch.chdir(tmp_path)
    gh = _app_claimed(datetime.now(UTC), "s1")
    gh.committed_policy = RuntimeError("gh: Resource not accessible by integration (HTTP 403)")
    monkeypatch.setattr(cli, "run_gh", gh)
    argv = {
        "claim": ["claim", "--agent", "me", "--branch", "b"],
        "renew": ["renew"],
        "claims": ["claims"],
    }[subcommand] + ["--project", PROJECT]
    if subcommand != "claims":
        argv += ["--issue", "7", "--session", "s1"]
    before = len(gh.issues[7]["comments"])
    try:
        assert cli.main(argv) == 2
        assert "committed policy" in capsys.readouterr().err
        assert len(gh.issues[7]["comments"]) == before
    finally:
        leases.use_policy_bot_logins("")


def test_release_with_the_env_bot_warns_on_an_unreadable_committed_policy(
    tmp_path, monkeypatch, capsys
):
    from agentic_sdlc import cli, leases

    monkeypatch.setenv(leases.LEASE_BOT_LOGINS_ENV, _APP)
    monkeypatch.chdir(tmp_path)
    gh = _app_claimed(datetime.now(UTC), "s1")
    gh.committed_policy = RuntimeError("gh: Resource not accessible by integration (HTTP 403)")
    monkeypatch.setattr(cli, "run_gh", gh)
    try:
        argv = ["release", "--project", PROJECT, "--issue", "7", "--session", "s1"]
        assert cli.main(argv) == 0
        assert "WARNING" in capsys.readouterr().err
        assert len(_released(gh, "s1")) == 1
    finally:
        leases.use_policy_bot_logins("")


# ---------------------------------------------------------------- Codex 4243076063
# A lease policy that is another project's (or another provider's) is refused before any
# lease call: its TTL and Publisher bot are not this repository's.

_FOREIGN_POLICIES = {
    "other project": (
        'version = 1\n[project]\nid = "owner/other"\n'
        '[automation]\npublisher_bot_login = "evil[bot]"\n'
    ),
    "gitlab": (
        f'version = 1\n[project]\nid = "{PROJECT}"\nprovider = "gitlab"\n'
        '[automation]\npublisher_bot_login = "evil[bot]"\n'
    ),
}


@pytest.mark.parametrize("where", ["explicit", "local"])
@pytest.mark.parametrize("foreign", sorted(_FOREIGN_POLICIES))
@pytest.mark.parametrize("subcommand", ["claim", "renew", "release", "claims"])
def test_a_local_policy_for_another_project_is_refused_before_any_github_call(
    subcommand, foreign, where, tmp_path, monkeypatch, capsys
):
    from agentic_sdlc import cli, leases

    if subcommand == "claims" and where == "explicit":
        pytest.skip("claims takes no --config")
    monkeypatch.delenv(leases.LEASE_BOT_LOGINS_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    policy = tmp_path / ("named.toml" if where == "explicit" else "agentic-sdlc.toml")
    policy.write_text(_FOREIGN_POLICIES[foreign])
    gh = _app_claimed(datetime.now(UTC), "s1")
    monkeypatch.setattr(cli, "run_gh", gh)
    argv = {
        "claim": ["claim", "--agent", "me", "--branch", "b"],
        "renew": ["renew"],
        "release": ["release"],
        "claims": ["claims"],
    }[subcommand] + ["--project", PROJECT]
    if subcommand != "claims":
        argv += ["--issue", "7", "--session", "s1"]
    if where == "explicit":
        argv += ["--config", str(policy)]
    try:
        assert cli.main(argv) == 2
        assert "not leasing with it" in capsys.readouterr().err
        assert gh.calls == []
    finally:
        leases.use_policy_bot_logins("")


def test_a_committed_policy_for_another_project_is_refused_but_release_still_releases(
    tmp_path, monkeypatch, capsys
):
    from agentic_sdlc import cli, leases

    monkeypatch.delenv(leases.LEASE_BOT_LOGINS_ENV, raising=False)
    monkeypatch.chdir(tmp_path)
    gh = _app_claimed(datetime.now(UTC), "s1")
    gh.committed_policy = _FOREIGN_POLICIES["other project"]
    monkeypatch.setattr(cli, "run_gh", gh)
    base = ["--project", PROJECT, "--issue", "7", "--session", "s1"]
    try:
        before = len(gh.issues[7]["comments"])
        assert cli.main(["claim", "--agent", "me", "--branch", "b", *base]) == 2
        assert "not leasing with it" in capsys.readouterr().err
        assert len(gh.issues[7]["comments"]) == before
        assert cli.main(["release", *base]) == 0
        assert "WARNING" in capsys.readouterr().err
        assert leases.configured_bot_logins() == ""  # the foreign bot is not trusted
        assert len(_released(gh, "s1")) == 1
    finally:
        leases.use_policy_bot_logins("")


def test_the_policy_project_compares_case_insensitively(tmp_path, monkeypatch):
    import argparse

    from agentic_sdlc import cli, leases

    monkeypatch.delenv(leases.LEASE_BOT_LOGINS_ENV, raising=False)
    policy = tmp_path / "p.toml"
    policy.write_text(_APP_POLICY.replace(PROJECT, PROJECT.upper()) + "lease_ttl_minutes = 12\n")
    try:
        args = argparse.Namespace(ttl_minutes=None, config=str(policy), project=PROJECT)
        assert cli._lease_ttl(args) == 12
        assert leases.configured_bot_logins() == _APP
    finally:
        leases.use_policy_bot_logins("")


def test_release_settles_its_own_untrusted_claim_but_nobody_elses():
    """Unit: an own-session claim this process does not trust (an unconfigured App bot, or a
    forged one by a read-only user) is released; another session's untrusted claim is not."""
    from agentic_sdlc import leases

    leases.use_policy_bot_logins("")
    gh = FakeGh().issue(7)
    gh.permissions["reader"] = "read"
    mine = format_claim_marker(Lease(7, "x", "s1", "b", NOW + timedelta(hours=1)))
    theirs = format_claim_marker(Lease(7, "x", "s2", "b", NOW + timedelta(hours=1)))
    gh.issues[7]["comments"] += [
        _comment_row(1, mine, NOW.isoformat(), "reader", "User", "MEMBER"),
        _comment_row(2, theirs, NOW.isoformat(), _APP, "Bot", "NONE"),
    ]
    release(PROJECT, 7, session="s1", gh=gh, now=NOW)
    assert len(_released(gh, "s1")) == 1 and _released(gh, "s2") == []
    release(PROJECT, 7, session="s1", gh=gh, now=NOW)  # the owner's release counts: no repeat
    assert len(_released(gh, "s1")) == 1
    release(PROJECT, 7, session="s3", gh=gh, now=NOW)  # no claim of s3 anywhere: nothing
    assert _released(gh, "s3") == []


# Codex 4243193102: a PR a human or other automation opens (or links) for the issue after the
# claim is durable work; every renewal re-reads the same open-PR inventory `claim` reads and
# refuses -- before posting anything -- so the routine stops before pushing a duplicate.
@pytest.mark.parametrize("how", ["closing-reference", "linked", "cross-referenced"])
def test_renew_refuses_once_the_issue_has_an_open_pr(how):
    mine = format_claim_marker(Lease(7, "me", "s1", "b", NOW + timedelta(minutes=5)))
    gh = FakeGh().issue(7, labels=[IN_PROGRESS_LABEL], comments=[mine])
    renew(PROJECT, 7, session="s1", ttl_minutes=60, gh=gh, now=NOW)  # no PR yet: renews
    posted = sum(c[:3] == ("api", "-X", "POST") for c in gh.calls)
    if how == "closing-reference":
        gh.prs = [{"number": 20, "url": "https://x/pr/20", "body": "Fixes #7", "title": "t"}]
    elif how == "linked":
        gh.timeline[7] = [_link_event("connected", "CE_1")]
        gh.link_subjects["CE_1"] = _pr_subject(20, repo=PROJECT)
    else:
        gh.timeline[7] = [_xref(20, repo=PROJECT)]
    with pytest.raises(LeaseError, match="already has an open pull request.*not renewing"):
        renew(PROJECT, 7, session="s1", ttl_minutes=60, gh=gh, now=NOW)
    assert sum(c[:3] == ("api", "-X", "POST") for c in gh.calls) == posted


def test_renew_refuses_when_the_open_pr_inventory_cannot_be_read():
    mine = format_claim_marker(Lease(7, "me", "s1", "b", NOW + timedelta(minutes=5)))
    gh = FakeGh().issue(7, labels=[IN_PROGRESS_LABEL], comments=[mine])

    def faulty(args, input=None):
        if tuple(args[:2]) == ("api", "graphql") and "pullRequests" in " ".join(args):
            raise RuntimeError("gh: HTTP 502")
        return gh(args, input)

    with pytest.raises(LeaseError, match="open pull requests could not be read"):
        renew(PROJECT, 7, session="s1", ttl_minutes=60, gh=faulty, now=NOW)
    assert not any(c[:3] == ("api", "-X", "POST") for c in gh.calls)


# Codex 4243550483: `claim` by the session that holds the live lease renews it, and that path
# runs the same open-PR inventory check `renew` does, refusing the same way before posting.
@pytest.mark.parametrize("how", ["closing-reference", "linked", "cross-referenced"])
def test_claim_renewal_refuses_once_the_issue_has_an_open_pr(how):
    mine = format_claim_marker(Lease(7, "me", "s1", "b", NOW + timedelta(minutes=5)))
    gh = FakeGh().issue(7, labels=[IN_PROGRESS_LABEL], comments=[mine])
    if how == "closing-reference":
        gh.prs = [{"number": 20, "url": "https://x/pr/20", "body": "Fixes #7", "title": "t"}]
    elif how == "linked":
        gh.timeline[7] = [_link_event("connected", "CE_1")]
        gh.link_subjects["CE_1"] = _pr_subject(20, repo=PROJECT)
    else:
        gh.timeline[7] = [_xref(20, repo=PROJECT)]
    result = claim(PROJECT, 7, agent="me", session="s1", branch="b", ttl_minutes=60, gh=gh, now=NOW)
    assert not result.ok and not result.renewed
    assert "already has an open pull request" in result.reason
    assert "not renewing" in result.reason
    with pytest.raises(LeaseError) as caught:
        renew(PROJECT, 7, session="s1", ttl_minutes=60, gh=gh, now=NOW)
    assert str(caught.value) == result.reason
    assert not any(c[:3] == ("api", "-X", "POST") for c in gh.calls)
    assert len(gh.issues[7]["comments"]) == 1


def test_claim_renewal_refuses_when_the_open_pr_inventory_cannot_be_read():
    mine = format_claim_marker(Lease(7, "me", "s1", "b", NOW + timedelta(minutes=5)))
    gh = FakeGh().issue(7, labels=[IN_PROGRESS_LABEL], comments=[mine])

    def faulty(args, input=None):
        if tuple(args[:2]) == ("api", "graphql") and "pullRequests" in " ".join(args):
            raise RuntimeError("gh: HTTP 502")
        return gh(args, input)

    result = claim(
        PROJECT, 7, agent="me", session="s1", branch="b", ttl_minutes=60, gh=faulty, now=NOW
    )
    assert not result.ok and "open pull requests could not be read" in result.reason
    assert not any(c[:3] == ("api", "-X", "POST") for c in gh.calls)
    assert len(gh.issues[7]["comments"]) == 1


# ---------------------------------------------------------------- Codex 4243386515
# "No committed policy" is GitHub's 404 ANSWER, never a "404" in the message: a repository or
# default branch literally named `404` echoes it in every request path, so a 403, 5xx or timeout
# there must still fail closed.


@pytest.mark.parametrize(
    ("stderr", "stdout", "expected"),
    [
        ("gh: Not Found (HTTP 404)", '{"message":"Not Found","status":"404"}', 404),
        ("gh: Not Found (HTTP 404)\n", "", 404),
        ("gh: Forbidden (HTTP 403)", '{"message":"Forbidden","status":"403"}', 403),
        ("gh: Server Error (HTTP 502)", "", 502),
        # the body's own status disagrees: not a clean answer
        ("gh: Not Found (HTTP 404)", '{"message":"x","status":"500"}', None),
        # a 404 that is only in the message, not the trailing status
        ("gh: repos/o/404 (HTTP 404) proxy said no", "", None),
        ("error connecting to api.github.com (repos/o/404)", "", None),
        ("", "", None),
        ("gh: Bad (HTTP 404)\ngh: Server Error (HTTP 500)", "", 500),
    ],
)
def test_gh_error_http_status_reads_only_the_response_status(stderr, stdout, expected):
    from agentic_sdlc.onboard import gh_error_http_status

    assert gh_error_http_status(stderr, stdout) == expected


def test_run_gh_carries_the_response_status(tmp_path, monkeypatch):
    from agentic_sdlc import onboard

    gh = tmp_path / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'printf \'%s\' \'{"message":"Not Found","status":"404"}\'\n'
        "echo 'gh: Not Found (HTTP 404)' >&2\nexit 1\n"
    )
    gh.chmod(0o755)
    monkeypatch.setattr(onboard, "trusted_gh", lambda: str(gh))
    monkeypatch.setenv("GH_TOKEN", "t")
    with pytest.raises(GhCommandError) as caught:
        onboard.run_gh(["api", "repos/o/404/contents/agentic-sdlc.toml?ref=404"])
    assert caught.value.http_status == 404 and onboard.gh_http_status(caught.value) == 404
    gh.write_text("#!/bin/sh\necho 'gh: Forbidden (HTTP 403)' >&2\nexit 1\n")
    with pytest.raises(GhCommandError) as caught:
        onboard.run_gh(["api", "repos/o/404/contents/agentic-sdlc.toml?ref=404"])
    assert caught.value.http_status == 403


@pytest.mark.parametrize(
    "failure",
    [
        GhCommandError(
            "gh api repos/o/404/contents/agentic-sdlc.toml?ref=404 failed: "
            "gh: Forbidden (HTTP 403)",
            http_status=403,
        ),
        GhCommandError(
            "gh api repos/o/404/contents/agentic-sdlc.toml?ref=404 failed: "
            "gh: Server Error (HTTP 502)",
            http_status=502,
        ),
        GhCommandError(
            "gh api repos/o/404/contents/agentic-sdlc.toml?ref=404 failed: Not Found 404",
            http_status=None,
        ),
        # run_gh's timeout: the path carries "404" twice, the answer never came
        OnboardError(
            "gh api repos/o/404/contents/agentic-sdlc.toml?ref=404 did not answer within 60s; "
            "not concluding"
        ),
        RuntimeError("gh: Not Found (HTTP 404)"),  # not run_gh's error: no parsed status
    ],
)
def test_committed_policy_fails_closed_unless_github_answered_404(failure, monkeypatch):
    from agentic_sdlc import cli

    def fake(args, input=None):
        if args == ["api", "repos/o/404"]:
            return json.dumps({"full_name": "o/404", "default_branch": "404"})
        assert args == ["api", "repos/o/404/contents/agentic-sdlc.toml?ref=404"]
        raise failure

    monkeypatch.setattr(cli, "run_gh", fake)
    with pytest.raises(LeaseError, match="cannot read the committed policy"):
        cli._committed_lease_policy("o/404")

    def absent(args, input=None):
        if args == ["api", "repos/o/404"]:
            return json.dumps({"full_name": "o/404", "default_branch": "404"})
        raise GhCommandError("gh: Not Found (HTTP 404)", http_status=404)

    monkeypatch.setattr(cli, "run_gh", absent)
    assert cli._committed_lease_policy("o/404") is None
