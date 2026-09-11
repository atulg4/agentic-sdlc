from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agentic_sdlc.openai_compatible import (
    AdapterError,
    build_repository_context,
    classify_provider_error,
    extract_patch,
    extract_response_patch,
    extract_text,
    resolve_model,
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
