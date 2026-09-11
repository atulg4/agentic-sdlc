"""Minimal OpenAI-compatible patch generator for routed Forge executors."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path


class AdapterError(RuntimeError):
    """Raised when a provider cannot produce an applicable patch."""


class ProviderExhaustedError(AdapterError):
    """Raised when a provider is out of quota, capacity, or valid credentials."""

    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


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
MAX_TASK_BYTES = 65_536
MAX_FILE_BYTES = 24_000
MAX_CONTEXT_FILES = 24

# The adapter never sends more than the task plus the context bundle, so the route
# must require an executor whose declared context window can hold that much.
ROUTED_BUNDLE_BYTES = MAX_TASK_BYTES + MAX_CONTEXT_BYTES
BYTES_PER_TOKEN_ESTIMATE = 4
ROUTED_MIN_CONTEXT_WINDOW = ROUTED_BUNDLE_BYTES // BYTES_PER_TOKEN_ESTIMATE

RECOVERABLE_PROVIDER_STATUSES = frozenset(
    {"auth-exhausted", "quota-exhausted", "capacity-exhausted"}
)

# Bounded number of routed candidates tried in one adapter invocation.
MAX_ROUTE_ATTEMPTS = 3

TEXT_SUFFIXES = {
    ".bash",
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".css",
    ".cxx",
    ".go",
    ".h",
    ".hh",
    ".hpp",
    ".hxx",
    ".html",
    ".java",
    ".js",
    ".json",
    ".kt",
    ".kts",
    ".md",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yml",
    ".yaml",
    ".zsh",
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


def is_readable_tracked_file(path: str, root: Path | None = None) -> bool:
    """Return True only for a regular file that stays inside the checkout root.

    A tracked path that is a symlink, or that traverses a symlinked directory, is
    rejected before any read so a consumer repository cannot pull runner files
    outside the checkout into the prompt sent to a third-party provider.
    """
    checkout_root = (root or Path.cwd()).resolve()
    candidate = checkout_root / path
    current = checkout_root
    for part in Path(path).parts:
        current = current / part
        if current.is_symlink():
            return False
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return False
    if not resolved.is_relative_to(checkout_root):
        return False
    return resolved.is_file()


def build_repository_context(prompt: str, *, max_context_bytes: int = MAX_CONTEXT_BYTES) -> str:
    """Build a bounded, secret-filtered source context bundle for routed providers."""
    files = _tracked_files()
    prompt_tokens = _tokenize(prompt)
    ranked = sorted(files, key=lambda path: (-_score_path(path, prompt_tokens), path))
    selected = ranked[:MAX_CONTEXT_FILES]
    chunks = ["Repository files:\n" + "\n".join(files[:1500])]
    used = sum(len(chunk.encode("utf-8")) for chunk in chunks)
    checkout_root = Path.cwd().resolve()
    for path in selected:
        if not is_readable_tracked_file(path, checkout_root):
            continue
        try:
            content = (checkout_root / path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if not content.strip():
            continue
        content = content[:MAX_FILE_BYTES]
        chunk = f"\n\n--- FILE: {path} ---\n{content}"
        chunk_bytes = len(chunk.encode("utf-8"))
        if used + chunk_bytes > max_context_bytes:
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
        message = f"{status}: provider request failed with HTTP {error.code}"
        if status in RECOVERABLE_PROVIDER_STATUSES:
            raise ProviderExhaustedError(status, message) from error
        raise AdapterError(message) from error
    return payload


def request_patch(
    *,
    provider: str,
    model: str,
    prompt: str,
    validate: Callable[[str], None] | None = None,
) -> str:
    """Request one patch, with a single corrective retry.

    ``validate`` is applied to an extracted patch and must raise ``AdapterError``
    when the patch is unusable. A patch that extracts but does not apply therefore
    gets the same bounded corrective retry as a response with no patch at all.
    """

    def _patch_from(response: dict[str, object]) -> str:
        patch = extract_response_patch(response)
        if validate is not None:
            validate(patch)
        return patch

    response = chat_completion(provider=provider, model=model, prompt=prompt)
    try:
        return _patch_from(response)
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
                "Every hunk must apply cleanly to the files in the context bundle with "
                "git apply. No prose, no markdown fences, no explanation.\n"
                f"Rejection detail: {_excerpt(str(first_error), 400)}"
            ),
        )
        try:
            return _patch_from(retry_response)
        except AdapterError as retry_error:
            retry_text = extract_text(retry_response)
            raise AdapterError(
                "provider response did not contain an applicable unified git patch; "
                f"first_error={first_error}; "
                f"first_excerpt={_excerpt(first_text)!r}; "
                f"retry_error={retry_error}; "
                f"retry_excerpt={_excerpt(retry_text)!r}"
            ) from retry_error


@dataclass(frozen=True)
class RouteCandidate:
    executor_id: str
    provider: str
    model: str


def route_candidates(document: object) -> tuple[RouteCandidate, ...]:
    """Return the eligible OpenAI-compatible candidates of a route decision.

    Candidates are ordered exactly as routing ranks them - preference rank, then
    expected cost per successful mission, then executor id - so an in-process
    re-route after live provider exhaustion stays deterministic. The selected
    executor is always tried first.
    """
    if not isinstance(document, dict):
        return ()
    selected_id = str(document.get("selectedExecutorId", ""))
    ranked: list[tuple[int, float, str, RouteCandidate]] = []
    for entry in document.get("candidates") or ():
        if not isinstance(entry, dict) or not entry.get("eligible"):
            continue
        provider = str(entry.get("provider", ""))
        model = str(entry.get("model", ""))
        executor_id = str(entry.get("executorId", ""))
        if provider not in PROVIDERS or not model:
            continue
        rank = entry.get("preferenceRank")
        ecps = entry.get("ecps")
        ranked.append(
            (
                int(rank) if isinstance(rank, int) else 1_000_000,
                float(ecps) if isinstance(ecps, int | float) else float("inf"),
                executor_id,
                RouteCandidate(executor_id=executor_id, provider=provider, model=model),
            )
        )
    ordered = [candidate for *_, candidate in sorted(ranked, key=lambda item: item[:3])]
    selected = [item for item in ordered if item.executor_id == selected_id]
    return tuple(selected + [item for item in ordered if item.executor_id != selected_id])


def _attempt_order(
    provider: str,
    model: str,
    candidates: Sequence[RouteCandidate],
) -> tuple[RouteCandidate, ...]:
    # Keep the selected executor's id on the first attempt so fallback telemetry
    # names the executor rather than only its provider.
    first = next(
        (item for item in candidates if (item.provider, item.model) == (provider, model)),
        RouteCandidate(executor_id="", provider=provider, model=model),
    )
    attempts = [first]
    for candidate in candidates:
        if (candidate.provider, candidate.model) == (provider, model):
            continue
        attempts.append(candidate)
    return tuple(attempts[:MAX_ROUTE_ATTEMPTS])


def _bundle_prompt(prompt: str) -> str:
    task_bytes = len(prompt.encode("utf-8"))
    if task_bytes > MAX_TASK_BYTES:
        raise AdapterError(
            f"task prompt is {task_bytes} bytes, above the routed bundle task budget "
            f"of {MAX_TASK_BYTES} bytes"
        )
    context = build_repository_context(
        prompt,
        max_context_bytes=min(MAX_CONTEXT_BYTES, ROUTED_BUNDLE_BYTES - task_bytes),
    )
    return (
        f"{prompt}\n\n"
        "Use this bounded repository context to produce the patch. "
        "If more files would be useful, make the smallest correct change with the "
        f"files provided instead of returning prose.\n\n{context}"
    )


def generate_and_apply_patch(
    *,
    provider: str,
    model: str,
    prompt_path: Path,
    output_patch: Path,
    output_message: Path,
    candidates: Sequence[RouteCandidate] = (),
) -> None:
    bundle = _bundle_prompt(prompt_path.read_text(encoding="utf-8"))

    def _check_applies(patch: str) -> None:
        output_patch.write_text(patch, encoding="utf-8")
        result = subprocess.run(
            ["git", "apply", "--check", str(output_patch)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise AdapterError(
                "patch does not apply to the checkout: " + _excerpt(result.stderr.strip())
            )

    attempts = _attempt_order(provider, model, candidates)
    fallbacks: list[str] = []
    for index, attempt in enumerate(attempts):
        try:
            patch = request_patch(
                provider=attempt.provider,
                model=attempt.model,
                prompt=bundle,
                validate=_check_applies,
            )
        except ProviderExhaustedError as error:
            label = attempt.executor_id or attempt.provider
            fallbacks.append(f"{label} {error.status}")
            if index + 1 >= len(attempts):
                raise AdapterError(
                    "every routed candidate was exhausted: " + "; ".join(fallbacks)
                ) from error
            print(f"Routing fallback: {label} {error.status}", file=sys.stderr)
            continue
        output_patch.write_text(patch, encoding="utf-8")
        subprocess.run(["git", "apply", str(output_patch)], check=True)
        message = f"Generated patch with routed OpenAI-compatible provider {attempt.provider}.\n"
        if fallbacks:
            message += "Routing fallback: " + "; ".join(fallbacks) + "\n"
        output_message.write_text(message, encoding="utf-8")
        return


def main(argv: list[str] | None = None) -> int:
    args = list(argv or sys.argv[1:])
    if len(args) not in {5, 6}:
        print(
            "usage: openai_compatible <provider> <model> <prompt> <patch-output> "
            "<message-output> [route-json]",
            file=sys.stderr,
        )
        return 2
    provider, model, prompt, patch_output, message_output = args[:5]
    candidates: tuple[RouteCandidate, ...] = ()
    if len(args) == 6 and args[5]:
        candidates = route_candidates(json.loads(Path(args[5]).read_text(encoding="utf-8")))
    try:
        generate_and_apply_patch(
            provider=provider,
            model=model,
            prompt_path=Path(prompt),
            output_patch=Path(patch_output),
            output_message=Path(message_output),
            candidates=candidates,
        )
    except (AdapterError, OSError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
