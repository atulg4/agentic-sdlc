"""Create and verify immutable metadata for generated patch artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


class ArtifactError(ValueError):
    """Raised when patch metadata is malformed or does not match its artifact."""


_SHA = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def create_manifest(
    patch: str | Path,
    *,
    base_sha: str,
    repository: str,
    request_number: int,
) -> dict[str, Any]:
    patch_path = Path(patch)
    if not patch_path.is_file():
        raise ArtifactError("patch artifact does not exist")
    if not _SHA.fullmatch(base_sha):
        raise ArtifactError("base SHA must be 40 lowercase hexadecimal characters")
    if not _REPOSITORY.fullmatch(repository):
        raise ArtifactError("repository must use owner/name format")
    if request_number < 1:
        raise ArtifactError("request number must be positive")
    size = patch_path.stat().st_size
    if size < 1:
        raise ArtifactError("patch artifact is empty")
    return {
        "schemaVersion": 1,
        "baseSha": base_sha,
        "repository": repository,
        "requestNumber": request_number,
        "patchBytes": size,
        "patchSha256": _digest(patch_path),
    }


def write_manifest(document: dict[str, Any], output: str | Path) -> None:
    Path(output).write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def verify_manifest(
    patch: str | Path,
    manifest: str | Path,
    *,
    expected_base_sha: str | None = None,
    expected_repository: str | None = None,
    expected_request_number: int | None = None,
) -> dict[str, Any]:
    try:
        document = json.loads(Path(manifest).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ArtifactError("patch manifest is unreadable") from error
    if not isinstance(document, dict) or document.get("schemaVersion") != 1:
        raise ArtifactError("unsupported patch manifest")

    patch_path = Path(patch)
    if not patch_path.is_file():
        raise ArtifactError("patch artifact does not exist")
    checks = {
        "patchBytes": patch_path.stat().st_size,
        "patchSha256": _digest(patch_path),
    }
    if expected_base_sha is not None:
        checks["baseSha"] = expected_base_sha
    if expected_repository is not None:
        checks["repository"] = expected_repository
    if expected_request_number is not None:
        checks["requestNumber"] = expected_request_number

    mismatches = [name for name, value in checks.items() if document.get(name) != value]
    if mismatches:
        raise ArtifactError("patch manifest mismatch: " + ", ".join(sorted(mismatches)))
    return document
