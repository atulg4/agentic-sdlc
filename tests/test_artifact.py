from __future__ import annotations

from pathlib import Path

import pytest

from agentic_sdlc.artifact import (
    ArtifactError,
    create_manifest,
    verify_manifest,
    write_manifest,
)


def test_manifest_binds_patch_to_base_repository_and_request(tmp_path: Path) -> None:
    patch = tmp_path / "change.patch"
    manifest = tmp_path / "manifest.json"
    patch.write_bytes(b"diff --git a/a b/a\n")
    document = create_manifest(
        patch,
        base_sha="a" * 40,
        repository="owner/repository",
        request_number=17,
    )
    write_manifest(document, manifest)

    verified = verify_manifest(
        patch,
        manifest,
        expected_base_sha="a" * 40,
        expected_repository="owner/repository",
        expected_request_number=17,
    )
    assert verified["patchSha256"] == document["patchSha256"]


def test_manifest_rejects_changed_patch(tmp_path: Path) -> None:
    patch = tmp_path / "change.patch"
    manifest = tmp_path / "manifest.json"
    patch.write_bytes(b"original")
    write_manifest(
        create_manifest(
            patch,
            base_sha="b" * 40,
            repository="owner/repository",
            request_number=2,
        ),
        manifest,
    )
    patch.write_bytes(b"tampered")

    with pytest.raises(ArtifactError, match="patch manifest mismatch"):
        verify_manifest(patch, manifest)
