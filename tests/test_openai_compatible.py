from __future__ import annotations

import io
import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from agentic_sdlc import openai_compatible
from agentic_sdlc.openai_compatible import (
    BYTES_PER_TOKEN_ESTIMATE,
    MAX_TASK_BYTES,
    ROUTED_BUNDLE_BYTES,
    ROUTED_MIN_CONTEXT_WINDOW,
    AdapterError,
    ProviderExhaustedError,
    RouteCandidate,
    _safe_text_path,
    build_repository_context,
    chat_completion,
    classify_provider_error,
    extract_patch,
    extract_response_patch,
    extract_text,
    is_readable_tracked_file,
    resolve_model,
    route_candidates,
)


def test_configured_model_identifier_resolves_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_MODEL_PRO", "deepseek-provider-model")

    assert resolve_model("configured-by-DEEPSEEK_MODEL_PRO") == "deepseek-provider-model"
    assert resolve_model("already-explicit") == "already-explicit"


def test_missing_configured_model_identifier_fails_closed(monkeypatch) -> None:
    monkeypatch.delenv("DEEPSEEK_MODEL_PRO", raising=False)

    with pytest.raises(AdapterError, match="missing model environment variable"):
        resolve_model("configured-by-DEEPSEEK_MODEL_PRO")


def test_provider_error_classification_marks_exhaustion_recoverable() -> None:
    assert classify_provider_error(401, "invalid api key") == "auth-exhausted"
    assert classify_provider_error(429, "quota exceeded") == "quota-exhausted"
    assert classify_provider_error(503, "capacity overloaded") == "capacity-exhausted"
    assert classify_provider_error(500, "server error") == "unavailable"


def test_extract_text_and_patch_from_mocked_provider_response() -> None:
    response = {
        "choices": [
            {
                "message": {
                    "content": (
                        "```diff\n"
                        "diff --git a/example.txt b/example.txt\n"
                        "new file mode 100644\n"
                        "index 0000000..ce01362\n"
                        "--- /dev/null\n"
                        "+++ b/example.txt\n"
                        "@@ -0,0 +1 @@\n"
                        "+hello\n"
                        "```\n"
                    )
                }
            }
        ]
    }

    patch = extract_patch(extract_text(json.loads(json.dumps(response))))

    assert patch.startswith("diff --git a/example.txt b/example.txt")
    assert "+hello" in patch


def test_extract_response_patch_accepts_json_patch_field() -> None:
    patch_text = (
        "diff --git a/example.txt b/example.txt\n"
        "new file mode 100644\n"
        "index 0000000..ce01362\n"
        "--- /dev/null\n"
        "+++ b/example.txt\n"
        "@@ -0,0 +1 @@\n"
        "+hello\n"
    )
    response = {"choices": [{"message": {"content": json.dumps({"patch": patch_text})}}]}

    assert extract_response_patch(response) == patch_text


def test_extract_patch_requires_git_patch() -> None:
    with pytest.raises(AdapterError, match="unified git patch"):
        extract_patch("I changed the file.")


def test_repository_context_filters_sensitive_files_and_ranks_prompt_matches(
    monkeypatch, tmp_path: Path
) -> None:
    files = {
        "web/server.py": "def song_search():\n    return []\n",
        "web/static/index.html": "<button>Ask Maestro</button>\n",
        ".env": "DEEPSEEK_API_KEY=secret\n",
        "docs/private-key-notes.md": "secret\n",
        "node_modules/pkg/index.js": "ignored\n",
        "image.png": "ignored\n",
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init"], check=True, capture_output=True, text=True)
    subprocess.run(
        [
            "git",
            "add",
            "web/server.py",
            "web/static/index.html",
            ".env",
            "docs/private-key-notes.md",
            "node_modules/pkg/index.js",
            "image.png",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    context = build_repository_context("Add Ask Maestro song search support in web server")

    assert "--- FILE: web/server.py ---" in context
    assert "--- FILE: web/static/index.html ---" in context
    assert "DEEPSEEK_API_KEY" not in context
    assert "private-key-notes" not in context
    assert "node_modules" not in context
    assert "image.png" not in context


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )


def _repository_with_patch(tmp_path: Path) -> str:
    """Initialize a repository and return an applicable patch for it."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "--", "app.py")
    _git(tmp_path, "commit", "-qm", "base")
    (tmp_path / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    patch = subprocess.run(
        ["git", "diff"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    _git(tmp_path, "checkout", "--", "app.py")
    (tmp_path / "prompt.md").write_text("Raise VALUE in app.py to 2.\n", encoding="utf-8")
    return patch


STALE_PATCH = (
    "diff --git a/app.py b/app.py\n"
    "--- a/app.py\n"
    "+++ b/app.py\n"
    "@@ -1 +1 @@\n"
    "-VALUE = 9\n"
    "+VALUE = 10\n"
)


def _response(patch: str) -> dict[str, object]:
    return {"choices": [{"message": {"content": json.dumps({"patch": patch})}}]}


def test_repository_context_includes_common_source_languages() -> None:
    for suffix in (
        ".go",
        ".java",
        ".kt",
        ".kts",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".cs",
        ".rb",
        ".rs",
        ".php",
        ".sh",
        ".bash",
        ".zsh",
    ):
        assert _safe_text_path(f"src/main{suffix}"), suffix


def test_repository_context_reads_go_and_java_sources(monkeypatch, tmp_path: Path) -> None:
    files = {
        "cmd/server/main.go": "package main\n\nfunc main() {}\n",
        "src/Server.java": "class Server {}\n",
        "scripts/deploy.sh": "#!/bin/sh\necho server\n",
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", "--", *files)

    context = build_repository_context("Fix the server main entry point")

    assert "--- FILE: cmd/server/main.go ---" in context
    assert "--- FILE: src/Server.java ---" in context
    assert "--- FILE: scripts/deploy.sh ---" in context


def test_repository_context_never_follows_a_symlink_out_of_the_checkout(
    monkeypatch, tmp_path: Path
) -> None:
    outside = tmp_path.parent / f"runner-{tmp_path.name}.txt"
    outside.write_text("RUNNER_ONLY_MATERIAL\n", encoding="utf-8")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "server.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    (checkout / "context.py").symlink_to(outside)
    (checkout / "linked").symlink_to(tmp_path.parent)

    monkeypatch.chdir(checkout)
    _git(checkout, "init", "-q", "-b", "main")
    _git(checkout, "add", "--", "server.py", "context.py", "linked")

    assert is_readable_tracked_file("server.py") is True
    assert is_readable_tracked_file("context.py") is False

    context = build_repository_context("Update the server handler context")

    assert "--- FILE: server.py ---" in context
    assert "RUNNER_ONLY_MATERIAL" not in context
    assert "--- FILE: context.py ---" not in context


def test_live_quota_exhaustion_raises_a_recoverable_provider_error(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    def fake_urlopen(request, timeout=None):  # noqa: ANN001, ARG001
        raise urllib.error.HTTPError(
            url="https://api.deepseek.com/chat/completions",
            code=429,
            msg="Too Many Requests",
            hdrs=None,
            fp=io.BytesIO(b'{"error": "quota exceeded"}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(ProviderExhaustedError) as error:
        chat_completion(provider="deepseek", model="deepseek-reasoner", prompt="task")

    assert error.value.status == "quota-exhausted"


def test_route_candidates_are_deterministic_and_selection_first() -> None:
    route = {
        "selectedExecutorId": "kimi-1",
        "candidates": [
            {
                "executorId": "deepseek-1",
                "provider": "deepseek",
                "model": "deepseek-reasoner",
                "eligible": True,
                "preferenceRank": 1,
                "ecps": 0.4,
            },
            {
                "executorId": "openai-1",
                "provider": "openai",
                "model": "gpt-5",
                "eligible": True,
                "preferenceRank": 0,
                "ecps": 0.2,
            },
            {
                "executorId": "kimi-1",
                "provider": "kimi",
                "model": "kimi-k2-thinking",
                "eligible": True,
                "preferenceRank": 2,
                "ecps": 0.9,
            },
            {
                "executorId": "zai-blocked",
                "provider": "zai",
                "model": "glm-5.3",
                "eligible": False,
                "preferenceRank": 0,
                "ecps": 0.1,
                "rejectionReasons": ["quota-exhausted"],
            },
        ],
    }

    candidates = route_candidates(route)

    # The selected executor runs first; ineligible candidates and providers without
    # an OpenAI-compatible adapter never become fallback attempts.
    assert [item.executor_id for item in candidates] == ["kimi-1", "deepseek-1"]
    assert route_candidates("not a decision") == ()


def test_routed_run_continues_to_the_next_candidate_after_live_exhaustion(
    monkeypatch, tmp_path: Path
) -> None:
    patch = _repository_with_patch(tmp_path)
    monkeypatch.chdir(tmp_path)
    providers: list[str] = []

    def fake_chat_completion(*, provider, model, prompt, timeout_seconds=600):  # noqa: ANN001, ARG001
        providers.append(provider)
        if provider == "deepseek":
            raise ProviderExhaustedError(
                "quota-exhausted", "quota-exhausted: provider request failed with HTTP 429"
            )
        return _response(patch)

    monkeypatch.setattr(openai_compatible, "chat_completion", fake_chat_completion)

    openai_compatible.generate_and_apply_patch(
        provider="deepseek",
        model="deepseek-reasoner",
        prompt_path=tmp_path / "prompt.md",
        output_patch=tmp_path / "routed.patch",
        output_message=tmp_path / "message.md",
        candidates=(
            RouteCandidate(
                executor_id="deepseek-1", provider="deepseek", model="deepseek-reasoner"
            ),
            RouteCandidate(executor_id="kimi-1", provider="kimi", model="kimi-k2-thinking"),
        ),
    )

    assert providers == ["deepseek", "kimi"]
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    message = (tmp_path / "message.md").read_text(encoding="utf-8")
    assert "provider kimi" in message
    assert "Routing fallback: deepseek-1 quota-exhausted" in message


def test_routed_run_fails_closed_when_every_candidate_is_exhausted(
    monkeypatch, tmp_path: Path
) -> None:
    _repository_with_patch(tmp_path)
    monkeypatch.chdir(tmp_path)

    def fake_chat_completion(*, provider, model, prompt, timeout_seconds=600):  # noqa: ANN001, ARG001
        raise ProviderExhaustedError(
            "capacity-exhausted", "capacity-exhausted: provider request failed with HTTP 529"
        )

    monkeypatch.setattr(openai_compatible, "chat_completion", fake_chat_completion)

    with pytest.raises(AdapterError, match="every routed candidate was exhausted"):
        openai_compatible.generate_and_apply_patch(
            provider="deepseek",
            model="deepseek-reasoner",
            prompt_path=tmp_path / "prompt.md",
            output_patch=tmp_path / "routed.patch",
            output_message=tmp_path / "message.md",
            candidates=(
                RouteCandidate(executor_id="kimi-1", provider="kimi", model="kimi-k2-thinking"),
            ),
        )


def test_patch_that_fails_the_applicability_check_gets_one_corrective_retry(
    monkeypatch, tmp_path: Path
) -> None:
    patch = _repository_with_patch(tmp_path)
    monkeypatch.chdir(tmp_path)
    prompts: list[str] = []
    responses = [_response(STALE_PATCH), _response(patch)]

    def fake_chat_completion(*, provider, model, prompt, timeout_seconds=600):  # noqa: ANN001, ARG001
        prompts.append(prompt)
        return responses[len(prompts) - 1]

    monkeypatch.setattr(openai_compatible, "chat_completion", fake_chat_completion)

    openai_compatible.generate_and_apply_patch(
        provider="deepseek",
        model="deepseek-reasoner",
        prompt_path=tmp_path / "prompt.md",
        output_patch=tmp_path / "routed.patch",
        output_message=tmp_path / "message.md",
    )

    assert len(prompts) == 2
    assert "does not apply to the checkout" in prompts[1]
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"


def test_two_non_applicable_patches_fail_closed(monkeypatch, tmp_path: Path) -> None:
    _repository_with_patch(tmp_path)
    monkeypatch.chdir(tmp_path)
    calls: list[str] = []

    def fake_chat_completion(*, provider, model, prompt, timeout_seconds=600):  # noqa: ANN001, ARG001
        calls.append(provider)
        return _response(STALE_PATCH)

    monkeypatch.setattr(openai_compatible, "chat_completion", fake_chat_completion)

    with pytest.raises(AdapterError, match="did not contain an applicable unified git patch"):
        openai_compatible.generate_and_apply_patch(
            provider="deepseek",
            model="deepseek-reasoner",
            prompt_path=tmp_path / "prompt.md",
            output_patch=tmp_path / "routed.patch",
            output_message=tmp_path / "message.md",
        )

    assert len(calls) == 2
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_routed_bundle_stays_within_the_required_context_window(
    monkeypatch, tmp_path: Path
) -> None:
    _repository_with_patch(tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "prompt.md").write_text("x" * (MAX_TASK_BYTES + 1), encoding="utf-8")

    with pytest.raises(AdapterError, match="routed bundle task budget"):
        openai_compatible.generate_and_apply_patch(
            provider="deepseek",
            model="deepseek-reasoner",
            prompt_path=tmp_path / "prompt.md",
            output_patch=tmp_path / "routed.patch",
            output_message=tmp_path / "message.md",
        )

    assert ROUTED_MIN_CONTEXT_WINDOW * BYTES_PER_TOKEN_ESTIMATE >= ROUTED_BUNDLE_BYTES
