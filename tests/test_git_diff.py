from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agentic_sdlc.git_diff import collect_git_diff


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )


def test_collect_diff_handles_untracked_and_spaced_paths(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "existing.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "--", ".")
    _git(tmp_path, "commit", "-qm", "base")

    (tmp_path / "existing.py").write_text("VALUE = 2\n", encoding="utf-8")
    (tmp_path / "file with spaces.py").write_text("NEW = True\n", encoding="utf-8")
    snapshot = collect_git_diff(tmp_path)

    assert snapshot.paths == ("existing.py", "file with spaces.py")
    assert snapshot.added_lines == 2
    assert snapshot.deleted_lines == 1
    assert b"file with spaces.py" in snapshot.patch


def test_collect_diff_includes_deleted_paths(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    target = tmp_path / "protected.py"
    target.write_text("SECRET = False\n", encoding="utf-8")
    _git(tmp_path, "add", "--", ".")
    _git(tmp_path, "commit", "-qm", "base")

    target.unlink()
    snapshot = collect_git_diff(tmp_path)

    assert snapshot.paths == ("protected.py",)
    assert snapshot.added_lines == 0
    assert snapshot.deleted_lines == 1


def test_collect_diff_includes_both_sides_of_rename(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    workflow = tmp_path / ".github/workflows/ci.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: CI\n", encoding="utf-8")
    _git(tmp_path, "add", "--", ".")
    _git(tmp_path, "commit", "-qm", "base")

    workflow.rename(tmp_path / "renamed.yml")
    snapshot = collect_git_diff(tmp_path)

    assert snapshot.paths == (".github/workflows/ci.yml", "renamed.yml")
    assert b"rename from .github/workflows/ci.yml" in snapshot.patch
    assert b"rename to renamed.yml" in snapshot.patch


def test_derivation_tree_materializes_the_committed_content(tmp_path: Path) -> None:
    import os

    from agentic_sdlc.git_diff import derivation_tree

    repo = tmp_path / "source"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "a.txt").write_text("committed\n", encoding="utf-8")
    (repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    # A committed .gitattributes selecting a filter whose driver lives only
    # in repository-local config: a checkout IN THAT REPOSITORY would run it,
    # but the derivation clone's pristine configuration defines no driver, so
    # the committed bytes materialize unaltered.
    (repo / ".gitattributes").write_text("a.txt filter=sneaky\n", encoding="utf-8")
    # A committed symlink materializes exactly as a real checkout would.
    os.symlink("a.txt", repo / "alias.txt")
    _git(repo, "add", "--", ".")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "config", "filter.sneaky.smudge", "sed s/committed/smudged/")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()

    # Dirty the caller's checkout in every way a gate could observe: an
    # uncommitted edit, an ignored file, and status output suppressed by
    # repository-local configuration.
    (repo / "a.txt").write_text("uncommitted\n", encoding="utf-8")
    (repo / "ignored.txt").write_text("SECRET=1\n", encoding="utf-8")
    _git(repo, "config", "status.showUntrackedFiles", "no")

    with derivation_tree(repo, head) as tree:
        kept = tree
        # The disposable checkout holds exactly the committed content: no
        # uncommitted edit, no ignored file, and no locally configured
        # smudge filter applied.
        assert (tree / "a.txt").read_text(encoding="utf-8") == "committed\n"
        assert not (tree / "ignored.txt").exists()
        assert (tree / "alias.txt").is_symlink()
        assert os.readlink(tree / "alias.txt") == "a.txt"
        # Full Git metadata rides along for repository-aware gates, with a
        # pristine configuration: no filter driver, no hooks from the source.
        assert (tree / ".git").exists()
        cloned_config = subprocess.run(
            ["git", "config", "--local", "--get", "filter.sneaky.smudge"],
            cwd=tree,
            capture_output=True,
            text=True,
        )
        assert cloned_config.returncode != 0
        # The remote is disconnected: a gate (or candidate code a gate runs)
        # must not be able to push to or edit the caller's real repository.
        url = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=tree,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert url == "/var/empty/agentic-sdlc-derivation-disconnected"
    # The tree is removed afterwards and the caller's own state is untouched.
    assert not kept.exists()
    assert (repo / "a.txt").read_text(encoding="utf-8") == "uncommitted\n"


def test_series_patch_disables_signature_helpers(tmp_path: Path) -> None:
    # The repository's own config is the candidate's to write: with
    # log.showSignature enabled and gpg.program pointing at a
    # candidate-controlled executable, a bare `git log` would EXECUTE that
    # program to verify signed commits. The series patch disables signature
    # rendering outright.
    from agentic_sdlc.git_diff import collect_series_patch

    repo = tmp_path / "signing"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    marker = tmp_path / "verifier-ran"
    fake = tmp_path / "fake-gpg"
    fake.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "*--verify*)\n"
        f'  : > "{marker}"\n'
        '  echo "[GNUPG:] GOODSIG FAKEFAKEFAKEFAKE fake"\n'
        "  exit 0\n"
        "  ;;\n"
        "*)\n"
        '  printf "[GNUPG:] SIG_CREATED D\\n" >&2\n'
        '  printf -- "-----BEGIN PGP SIGNATURE-----\\nfake\\n-----END PGP SIGNATURE-----\\n"\n'
        "  exit 0\n"
        "  ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    # The ambient environment may default gpg.format to ssh; the attack (and
    # the fake) go through the openpgp verifier.
    _git(repo, "config", "gpg.format", "openpgp")
    _git(repo, "config", "gpg.program", str(fake))
    _git(repo, "config", "log.showSignature", "true")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "--", ".")
    _git(repo, "commit", "-qm", "base")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    (repo / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    _git(repo, "commit", "-S", "-qam", "signed head")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    # The fake wiring works: a bare signed log DOES run the helper.
    subprocess.run(
        ["git", "log", "-1", "--show-signature"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    assert marker.exists()
    marker.unlink()
    patch = collect_series_patch(repo, base, head)
    assert b"+two" in patch
    assert not marker.exists()


def test_series_patch_forces_textual_rendering(tmp_path: Path) -> None:
    # The candidate's committed .gitattributes is honored by a default diff:
    # a `*.py -diff` rule renders source changes as an opaque "GIT binary
    # patch" the reviewer cannot read, while the packet still labels the
    # evidence ordinary text. The series patch forces text rendering.
    from agentic_sdlc.git_diff import collect_series_patch

    repo = tmp_path / "attrs"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / ".gitattributes").write_text("*.py -diff\n", encoding="utf-8")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "--", ".")
    _git(repo, "commit", "-qm", "base")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    (repo / "a.py").write_text("x = 1\ny = evil()\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "head")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    patch = collect_series_patch(repo, base, head)
    assert b"GIT binary patch" not in patch
    assert b"+y = evil()" in patch


def test_derivation_tree_pins_caller_supplied_refs(tmp_path: Path) -> None:
    # The source's remote-tracking refs are source-mutable: a caller that
    # authenticated a commit elsewhere pins the tree's ref to that exact
    # value, overriding whatever the mirror imported — and malformed pins
    # fail closed.
    from agentic_sdlc.git_diff import GitDiffError, derivation_tree

    repo = tmp_path / "pinned"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "--", ".")
    _git(repo, "commit", "-qm", "base")
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    (repo / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "head")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    # The producer moved its own remote-tracking ref to the head: a gate
    # diffing origin/main...HEAD would see an empty diff under that value.
    _git(repo, "update-ref", "refs/remotes/origin/main", head)

    with derivation_tree(repo, head, pin_refs={"refs/remotes/origin/main": base}) as tree:
        resolved = subprocess.run(
            ["git", "rev-parse", "origin/main"],
            cwd=tree,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert resolved == base

    for bad in ({"HEAD": base}, {"refs/remotes/origin/main": "not-a-commit"}):
        with (
            pytest.raises(GitDiffError, match="pinned refs"),
            derivation_tree(repo, head, pin_refs=bad),
        ):
            raise AssertionError("the pinned derivation must not yield")


def test_derivation_tree_mirrors_remote_tracking_refs(tmp_path: Path) -> None:
    # CI checkouts often carry the candidate only on remote-tracking refs: a
    # repository-aware gate diffing against origin/<branch> must find the ref
    # inside the derivation clone.
    from agentic_sdlc.git_diff import derivation_tree

    repo = tmp_path / "ci"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "--", ".")
    _git(repo, "commit", "-qm", "base")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    # Only a remote-tracking ref holds the commit, like a detached CI clone.
    _git(repo, "update-ref", "refs/remotes/origin/main", head)
    _git(repo, "checkout", "-q", "--detach", head)
    _git(repo, "branch", "-q", "-D", "main")

    with derivation_tree(repo, head) as tree:
        resolved = subprocess.run(
            ["git", "rev-parse", "origin/main"],
            cwd=tree,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert resolved == head
