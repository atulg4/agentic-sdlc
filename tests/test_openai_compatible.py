from __future__ import annotations

import json

import pytest

from agentic_sdlc.openai_compatible import (
    AdapterError,
    classify_provider_error,
    extract_patch,
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


def test_extract_patch_requires_git_patch() -> None:
    with pytest.raises(AdapterError, match="unified git patch"):
        extract_patch("I changed the file.")
