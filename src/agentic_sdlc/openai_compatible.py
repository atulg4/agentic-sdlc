"""Minimal OpenAI-compatible patch generator for routed Forge executors."""

from __future__ import annotations

import json
import os
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


def resolve_model(model: str) -> str:
    prefix = "configured-by-"
    if model.startswith(prefix):
        env_name = model[len(prefix) :]
        value = os.environ.get(env_name, "").strip()
        if not value:
            raise AdapterError(f"missing model environment variable: {env_name}")
        return value
    return model


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


def chat_completion(
    *,
    provider: str,
    model: str,
    prompt: str,
    timeout_seconds: int = 600,
) -> str:
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
                    "You are a bounded software patch generator. Return only a unified "
                    "git patch beginning with diff --git. Do not include secrets, "
                    "deployment actions, prose, markdown outside the patch, or commands."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
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
    return extract_text(payload)


def generate_and_apply_patch(
    *,
    provider: str,
    model: str,
    prompt_path: Path,
    output_patch: Path,
    output_message: Path,
) -> None:
    prompt = prompt_path.read_text(encoding="utf-8")
    text = chat_completion(provider=provider, model=model, prompt=prompt)
    patch = extract_patch(text)
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
            "usage: openai_compatible <provider> <model> <prompt> <patch-output> "
            "<message-output>",
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
