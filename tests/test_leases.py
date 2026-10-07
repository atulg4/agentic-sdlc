from __future__ import annotations

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
        if args[:2] == ("pr", "list"):
            return json.dumps(self.prs)
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
    parsed = parse_marker(f"some human text\n{text}\nmore", issue=7)
    assert parsed == lease


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


def test_markers_from_a_github_app_bot_are_trusted():
    bot_claim = format_claim_marker(
        Lease(7, "forge-actions", "run-1", "b", NOW + timedelta(hours=1))
    )
    gh = FakeGh().issue(7)
    gh.issues[7]["comments"].append(
        _comment_row(
            60, bot_claim, "2026-09-30T01:00:00Z", "agentic-sdlc-publisher[bot]", "Bot", "NONE"
        )
    )
    assert current_lease(PROJECT, 7, gh).session == "run-1"


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
        {"number": 5, "url": "u5", "title": "t", "body": "", "headRefName": "forge/issue-7"},
        {"number": 6, "url": "u6", "title": "t", "body": "closes #70", "headRefName": "w"},
    ]
    assert [p["number"] for p in open_prs_for_issue(PROJECT, 7, gh)] == [2, 3, 5]


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


def test_an_unreadable_permission_makes_the_marker_untrusted():
    gh = FakeGh().issue(7)
    real = gh.__call__

    def broken(args, input=None):
        if "/collaborators/" in args[1]:
            raise RuntimeError("HTTP 502")
        return real(args, input=input)

    marker = format_claim_marker(Lease(7, "me", "s1", "b", NOW + timedelta(hours=1)))
    gh.issues[7]["comments"].append(_comment_row(95, marker, "2026-09-30T01:00:00Z"))
    assert current_lease(PROJECT, 7, broken, now=NOW) is None
    # Our own claim cannot be proven authoritative either: the claim fails closed.
    result = claim(PROJECT, 7, agent="me", session="s9", branch="b", gh=broken, now=NOW)
    assert not result.ok


@pytest.mark.parametrize(
    ("login", "kind", "configured", "trusted"),
    [
        ("github-actions[bot]", "Bot", "", True),
        ("agentic-sdlc-publisher[bot]", "Bot", "", True),
        ("dependabot[bot]", "Bot", "", False),
        ("claude[bot]", "Bot", "", False),
        ("agentic-sdlc-publisher[bot]", "Bot", "my-forge[bot]", False),
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
    return module


def test_the_commit_guard_and_leases_make_identical_trust_and_lease_decisions(monkeypatch):
    from agentic_sdlc import leases

    guard = _guard()
    for name in (
        "WRITE_PERMISSIONS",
        "GITHUB_ACTIONS_BOT",
        "PUBLISHER_APP_SLUG_HINT",
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
        ("", "Agentic-SDLC-Publisher[BOT]"),
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
