from __future__ import annotations

import json
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
        if args[0] == "api" and args[1].split("?")[0].endswith("/comments") and "-X" not in args:
            n = int(args[1].split("/")[4])
            return self._pages(self.issues[n]["comments"], args)
        if args[:3] == ("api", "-X", "POST") and args[3].endswith("/comments"):
            n = int(args[3].split("/")[4])
            body = json.loads(input)["body"]
            self._next_comment += 1
            self.issues[n]["comments"].append(
                _comment_row(self._next_comment, body, NOW.isoformat(), **self.poster)
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
                self.issues[n]["assignees"].append(
                    {"login": args[args.index("--add-assignee") + 1]}
                )
            return ""
        if args[:2] == ("pr", "list"):
            return json.dumps(self.prs)
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


def test_current_lease_takes_the_latest_claim_unless_released():
    gh = FakeGh().issue(
        7,
        labels=[IN_PROGRESS_LABEL],
        comments=[
            format_claim_marker(Lease(7, "a", "s1", "b1", NOW + timedelta(hours=1))),
            format_claim_marker(Lease(7, "b", "s2", "b2", NOW + timedelta(hours=2))),
        ],
    )
    assert current_lease(PROJECT, 7, gh).session == "s2"
    gh.issues[7]["comments"].append(
        _comment_row(9, "<!-- forge-release session=s2 -->", "2026-09-30T01:00:00Z")
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
    with pytest.raises(LeaseError):
        release(PROJECT, 7, session="s2", gh=gh)
    release(PROJECT, 7, session="s2", gh=gh, force=True)
    assert current_lease(PROJECT, 7, gh) is None


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
        _comment_row(60, bot_claim, "2026-09-30T01:00:00Z", "publisher[bot]", "Bot", "NONE")
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
