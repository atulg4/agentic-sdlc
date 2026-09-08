"""Minimal OpenAI-compatible patch generator for routed Forge executors."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


class AdapterError(RuntimeError):
    """Raised when a provider cannot produce an applicable patch."""


@dataclass(frozen=True)
class ProviderConfig:
    api_key_env: str
    base_url_env: str
    default_base_url: str


PROVIDERS = {
    "deepseek": ProviderConfig(
        api_key_env="DEEPSEEK_API_KEY",
        base_url_env="DEEPSEEK_BASE_URL",
        default_base_url="https://api.deepseek.com/chat/completions",
    ),
    "zai": ProviderConfig(
        api_key_env="ZAI_API_KEY",
        base_url_env="ZAI_BASE_URL",
        default_base_url="https://api.z.ai/api/paas/v4/chat/completions",
    ),
    "kimi": ProviderConfig(
        api_key_env="KIMI_API_KEY",
        base_url_env="KIMI_BASE_URL",
        default_base_url="https://api.moonshot.ai/v1/chat/completions",
    ),
}

MAX_CONTEXT_BYTES = 160_000
MAX_FILE_BYTES = 24_000
MAX_CONTEXT_FILES = 24
TEXT_SUFFIXES = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yml",
    ".yaml",
}
SENSITIVE_PARTS = {
    ".env",
    ".git",
    ".venv",
    "dist",
    "node_modules",
    "private-key",
    "secret",
    "secrets",
    "site-packages",
}


def resolve_model(model: str) -> str:
    prefix = "configured-by-"
    if model.startswith(prefix):
        env_name = model[len(prefix) :]
        value = os.environ.get(env_name, "").strip()
        if not value:
            raise AdapterError(f"missing model environment variable: {env_name}")
        return value
    return model


def _tokenize(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-zA-Z][a-zA-Z0-9_-]{2,}", text.lower())}


def _safe_text_path(path: str) -> bool:
    parts = {part.lower() for part in Path(path).parts}
    if parts & SENSITIVE_PARTS:
        return False
    lowered = path.lower()
    if any(marker in lowered for marker in ("secret", "private-key", ".pem", ".key")):
        return False
    return Path(path).suffix.lower() in TEXT_SUFFIXES


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if _safe_text_path(line)]


def _score_path(path: str, prompt_tokens: set[str]) -> int:
    lowered = path.lower()
    path_tokens = _tokenize(lowered.replace("/", " "))
    score = len(path_tokens & prompt_tokens) * 5
    anchors = {
        "admin": 2,
        "agent": 3,
        "analytics": 2,
        "api": 3,
        "maestro": 8,
        "playback": 2,
        "queue": 2,
        "request": 2,
        "server": 5,
        "song": 2,
        "static": 3,
        "test": 4,
        "web": 3,
    }
    for anchor, weight in anchors.items():
        if anchor in lowered and anchor in prompt_tokens:
            score += weight
    if Path(path).name in {"AGENTS.md", "README.md", "TESTING.md", "pyproject.toml"}:
        score += 2
    if "/tests/" in lowered or lowered.startswith("tests/") or "/test" in lowered:
        score += 2
    return score


def build_repository_context(prompt: str) -> str:
    """Build a bounded, secret-filtered source context bundle for routed providers."""
    files = _tracked_files()
    prompt_tokens = _tokenize(prompt)
    ranked = sorted(files, key=lambda path: (-_score_path(path, prompt_tokens), path))
    selected = ranked[:MAX_CONTEXT_FILES]
    chunks = ["Repository files:\n" + "\n".join(files[:1500])]
    used = sum(len(chunk.encode("utf-8")) for chunk in chunks)
    for path in selected:
        file_path = Path(path)
        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if not content.strip():
            continue
        content = content[:MAX_FILE_BYTES]
        chunk = f"\n\n--- FILE: {path} ---\n{content}"
        chunk_bytes = len(chunk.encode("utf-8"))
        if used + chunk_bytes > MAX_CONTEXT_BYTES:
            break
        chunks.append(chunk)
        used += chunk_bytes
    return "\n".join(chunks)


def classify_provider_error(status: int, body: str) -> str:
    normalized = body.lower()
    if status in {401, 403} or "auth" in normalized or "api key" in normalized:
        return "auth-exhausted"
    if status == 429 or "quota" in normalized or "rate limit" in normalized:
        return "quota-exhausted"
    if status in {503, 529} or "capacity" in normalized or "overload" in normalized:
        return "capacity-exhausted"
    return "unavailable"


def extract_text(response: dict[str, object]) -> str:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AdapterError("provider response did not include choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise AdapterError("provider response choice is malformed")
    message = first.get("message")
    if not isinstance(message, dict):
        raise AdapterError("provider response message is malformed")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise AdapterError("provider response content is empty")
    return content.strip()


def extract_response_patch(response: dict[str, object]) -> str:
    text = extract_text(response)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return extract_patch(text)
    patch = payload.get("patch")
    if not isinstance(patch, str) or not patch.strip():
        raise AdapterError("provider JSON response did not contain a patch string")
    return extract_patch(patch)


def extract_patch(text: str) -> str:
    marker = "```"
    if marker in text:
        blocks = text.split(marker)
        for block in blocks:
            candidate = block.removeprefix("diff").removeprefix("patch").strip()
            if candidate.startswith("diff --git "):
                return candidate + "\n"
    start = text.find("diff --git ")
    if start == -1:
        raise AdapterError("provider response did not contain a unified git patch")
    return text[start:].strip() + "\n"


def _excerpt(text: str, limit: int = 500) -> str:
    scrubbed = re.sub(
        r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*\S+",
        r"\1=<redacted>",
        text,
    )
    return scrubbed.replace("\n", "\\n")[:limit]


def chat_completion(
    *,
    provider: str,
    model: str,
    prompt: str,
    timeout_seconds: int = 600,
) -> dict[str, object]:
    config = PROVIDERS.get(provider)
    if config is None:
        raise AdapterError(f"unsupported OpenAI-compatible provider: {provider}")
    api_key = os.environ.get(config.api_key_env, "").strip()
    if not api_key:
        raise AdapterError(f"missing API key environment variable: {config.api_key_env}")
    base_url = os.environ.get(config.base_url_env, "").strip() or config.default_base_url
    body = {
        "model": resolve_model(model),
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a bounded software patch generator. You receive a task plus "
                    "a secret-filtered repository context bundle. Return one JSON object "
                    'with exactly one key named "patch". The patch value must be a unified '
                    "git patch beginning with diff --git. The patch must modify tracked "
                    "source or test files only. Do not include prose, markdown, commands, "
                    "deployment actions, or secrets."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        base_url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        error_body = error.read().decode("utf-8", errors="replace")
        status = classify_provider_error(error.code, error_body)
        raise AdapterError(f"{status}: provider request failed with HTTP {error.code}") from error
    return payload


def request_patch(
    *,
    provider: str,
    model: str,
    prompt: str,
) -> str:
    response = chat_completion(provider=provider, model=model, prompt=prompt)
    try:
        return extract_response_patch(response)
    except AdapterError as first_error:
        first_text = extract_text(response)
        retry_response = chat_completion(
            provider=provider,
            model=model,
            prompt=(
                f"{prompt}\n\n"
                "Your previous response was rejected because it did not contain an "
                "applicable unified git patch. Return exactly one JSON object with a "
                'single "patch" string. The string must start with diff --git. '
                "No prose, no markdown fences, no explanation."
            ),
        )
        try:
            return extract_response_patch(retry_response)
        except AdapterError as retry_error:
            retry_text = extract_text(retry_response)
            raise AdapterError(
                "provider response did not contain a unified git patch; "
                f"first_error={first_error}; "
                f"first_excerpt={_excerpt(first_text)!r}; "
                f"retry_error={retry_error}; "
                f"retry_excerpt={_excerpt(retry_text)!r}"
            ) from retry_error


def generate_and_apply_patch(
    *,
    provider: str,
    model: str,
    prompt_path: Path,
    output_patch: Path,
    output_message: Path,
) -> None:
    prompt = prompt_path.read_text(encoding="utf-8")
    context = build_repository_context(prompt)
    patch = request_patch(
        provider=provider,
        model=model,
        prompt=(
            f"{prompt}\n\n"
            "Use this bounded repository context to produce the patch. "
            "If more files would be useful, make the smallest correct change with the "
            "files provided instead of returning prose.\n\n"
            f"{context}"
        ),
    )
    output_patch.write_text(patch, encoding="utf-8")
    subprocess.run(["git", "apply", "--check", str(output_patch)], check=True)
    subprocess.run(["git", "apply", str(output_patch)], check=True)
    output_message.write_text(
        f"Generated patch with routed OpenAI-compatible provider {provider}.\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    args = list(argv or sys.argv[1:])
    if len(args) != 5:
        print(
            "usage: openai_compatible <provider> <model> <prompt> <patch-output> <message-output>",
            file=sys.stderr,
        )
        return 2
    provider, model, prompt, patch_output, message_output = args
    try:
        generate_and_apply_patch(
            provider=provider,
            model=model,
            prompt_path=Path(prompt),
            output_patch=Path(patch_output),
            output_message=Path(message_output),
        )
    except (AdapterError, OSError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
