from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError

import pytest

from agentic_sdlc import claude_cloud as cloud
from agentic_sdlc.cli import main

TASK = cloud.CloudTask("atulg4/agentic-sdlc", "pull", 155, "a" * 40, "review")
RESPONSE = {
    "type": "routine_fire",
    "claude_code_session_id": "session_example",
    "claude_code_session_url": "https://claude.ai/code/session_example",
}


@pytest.fixture
def configured(monkeypatch, tmp_path):
    monkeypatch.setenv("FORGE_CLAUDE_REVIEW_ROUTINE_ID", "trig_example")
    monkeypatch.setenv("FORGE_CLAUDE_REVIEW_ROUTINE_TOKEN", "sk-ant-oat01-test-secret")
    return cloud.DispatchJournal(tmp_path / "dispatch.sqlite")


def test_success_persists_without_token_and_deduplicates_across_process_restarts(
    configured, monkeypatch
):
    calls = []

    def post(routine, token, payload):
        calls.append(payload)
        assert routine == "trig_example" and token == "sk-ant-oat01-test-secret"
        assert configured.get(TASK.request_id)["state"] == "dispatching"
        return RESPONSE

    monkeypatch.setattr(cloud, "_post", post)
    result = cloud.dispatch(TASK, configured)
    assert (
        result["state"] == "started"
        and result["session_url"] == RESPONSE["claude_code_session_url"]
    )
    restarted = cloud.DispatchJournal(configured.path)
    assert cloud.dispatch(TASK, restarted)["deduplicated"] is True
    assert len(calls) == 1 and calls[0]["request_id"] == TASK.request_id
    assert b"test-secret" not in configured.path.read_bytes()


def test_concurrent_dispatchers_make_one_request(configured, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def post(*_args):
        started.set()
        assert release.wait(5)
        return RESPONSE

    monkeypatch.setattr(cloud, "_post", post)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(cloud.dispatch, TASK, configured)
        assert started.wait(5)
        try:
            second = cloud.dispatch(TASK, cloud.DispatchJournal(configured.path))
            assert second["state"] == "dispatching" and second["deduplicated"]
        finally:
            release.set()
        assert first.result()["state"] == "started"


@pytest.mark.parametrize(
    "failure",
    [
        URLError("secret"),
        TimeoutError("secret"),
        IncompleteRead(b"secret", 100),
        HTTPError("https://secret", 503, "secret", {}, None),
        HTTPError("https://secret", 302, "secret", {}, None),
    ],
)
def test_ambiguous_failure_is_not_retried_even_with_flag(configured, monkeypatch, failure):
    calls = []

    def post(*_args):
        calls.append(1)
        raise failure

    monkeypatch.setattr(cloud, "_post", post)
    first = cloud.dispatch(TASK, configured)
    assert first["state"] == "unknown" and "secret" not in json.dumps(first)
    assert cloud.dispatch(TASK, configured, retry_rejected=True)["deduplicated"]
    assert len(calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {**RESPONSE, "type": "error"},
        {**RESPONSE, "claude_code_session_url": "https://evil.test/session_example"},
        {**RESPONSE, "claude_code_session_id": "session_../../elsewhere"},
    ],
)
def test_invalid_response_is_unknown(configured, monkeypatch, response):
    monkeypatch.setattr(cloud, "_post", lambda *_: response)
    assert cloud.dispatch(TASK, configured)["state"] == "unknown"


def test_process_crash_after_reservation_never_sends_twice(configured, monkeypatch):
    configured.reserve(TASK, "trig_example", False)
    monkeypatch.setattr(cloud, "_post", lambda *_: pytest.fail("duplicate POST"))
    assert cloud.dispatch(TASK, configured, retry_rejected=True)["state"] == "dispatching"


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_known_rejection_needs_explicit_retry(configured, monkeypatch, status):
    def reject(*_args):
        raise HTTPError("https://api.anthropic.com", status, "", {}, None)

    monkeypatch.setattr(cloud, "_post", reject)
    assert cloud.dispatch(TASK, configured)["state"] == "rejected"
    monkeypatch.setattr(cloud, "_post", lambda *_: RESPONSE)
    assert cloud.dispatch(TASK, configured)["state"] == "rejected"
    result = cloud.dispatch(TASK, configured, retry_rejected=True)
    assert result["state"] == "started" and result["attempt"] == 2


@pytest.mark.parametrize(
    "retry_after,seconds", [("120", 120), ("nonsense", 60), ("Thu, 01 Jan 1970 00:20:00 GMT", 200)]
)
def test_rate_limit_backoff(configured, monkeypatch, retry_after, seconds):
    monkeypatch.setattr(cloud.time, "time", lambda: 1000)

    def reject(*_args):
        raise HTTPError("https://api.anthropic.com", 429, "", {"Retry-After": retry_after}, None)

    monkeypatch.setattr(cloud, "_post", reject)
    result = cloud.dispatch(TASK, configured)
    assert result["retry_at"] == 1000 + seconds
    with pytest.raises(cloud.CloudDispatchError, match="rate limited"):
        cloud.dispatch(TASK, configured, retry_rejected=True)
    monkeypatch.setattr(cloud.time, "time", lambda: 1000 + seconds)
    monkeypatch.setattr(cloud, "_post", lambda *_: RESPONSE)
    assert cloud.dispatch(TASK, configured, retry_rejected=True)["state"] == "started"


def test_no_api_key_fallback(configured, monkeypatch):
    monkeypatch.delenv("FORGE_CLAUDE_REVIEW_ROUTINE_TOKEN")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")
    monkeypatch.setattr(cloud, "_post", lambda *_: pytest.fail("must not send"))
    with pytest.raises(cloud.CloudDispatchError, match="per-routine"):
        cloud.dispatch(TASK, configured)
    assert configured.get(TASK.request_id) is None


def test_routine_identifier_cannot_redirect_credentials(configured, monkeypatch):
    monkeypatch.setenv("FORGE_CLAUDE_REVIEW_ROUTINE_ID", "trig_x/../../other?url=evil")
    monkeypatch.setattr(cloud, "_post", lambda *_: pytest.fail("must not send"))
    with pytest.raises(cloud.CloudDispatchError, match="trig_"):
        cloud.dispatch(TASK, configured)


def test_transport_pins_endpoint_headers_and_payload(monkeypatch):
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, count):
            assert count == 65537
            return json.dumps(RESPONSE).encode()

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == f"{cloud.API_ROOT}/trig_example/fire"
            assert request.get_method() == "POST" and timeout == 30
            assert request.get_header("Authorization") == "Bearer test-token"
            assert request.get_header("Anthropic-version") == cloud.API_VERSION
            assert json.loads(json.loads(request.data)["text"]) == TASK.payload()
            return Response()

    def opener(handler):
        assert isinstance(handler, cloud._NoRedirect)
        assert handler.redirect_request(None, None, 302, "", {}, "https://evil.test") is None
        return Opener()

    monkeypatch.setattr(cloud, "build_opener", opener)
    assert cloud._post("trig_example", "test-token", TASK.payload()) == RESPONSE


@pytest.mark.parametrize(
    "field,value",
    [
        ("repository", "https://evil.test/repo"),
        ("repository", "a/.."),
        ("head_sha", "main"),
        ("target_number", True),
        ("role", "implement"),
        ("target_type", "issue"),
    ],
)
def test_invalid_task(field, value):
    args = cloud.asdict(TASK)
    args[field] = value
    with pytest.raises(cloud.CloudDispatchError):
        cloud.CloudTask(**args)


def test_cli_preview_is_offline_and_status_returns_dispatch_not_completion(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(cloud, "_post", lambda *_: pytest.fail("dry run must not dispatch"))
    argv = [
        "claude-cloud-dispatch",
        "--project",
        "atulg4/agentic-sdlc",
        "--target-type",
        "pull",
        "--number",
        "155",
        "--head-sha",
        "A" * 40,
        "--role",
        "review",
    ]
    assert main(argv) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["dry_run"] and preview["request_id"] == TASK.request_id
    assert main([*argv, "--execute"]) == 2
    assert "requires --journal" in capsys.readouterr().err
    journal = cloud.DispatchJournal(tmp_path / "journal.sqlite")
    journal.reserve(TASK, "trig_example", False)
    assert (
        main(
            ["claude-cloud-status", "--journal", str(journal.path), "--request-id", TASK.request_id]
        )
        == 3
    )
    assert json.loads(capsys.readouterr().out)["state"] == "dispatching"


def test_corrupt_journal_fails_without_dispatch(tmp_path, monkeypatch, capsys):
    journal = tmp_path / "bad.sqlite"
    journal.write_text("not a database")
    monkeypatch.setattr(cloud, "_post", lambda *_: pytest.fail("must not send"))
    assert main(["claude-cloud-status", "--journal", str(journal), "--request-id", "x"]) == 2
    assert "journal could not be read or committed" in capsys.readouterr().err
