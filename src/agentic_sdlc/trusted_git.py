"""Resolve a git executable this session cannot have planted (Codex 4241614592).

`check-diff` is the cloud routine's last gate before a push, and it used to run a bare `git`,
i.e. whatever a PATH lookup finds first. The rule a git must pass -- the file and every directory
on the way to it changeable only by root; only system locations when running as root; the
`AGENTIC_SDLC_GIT` override vetted the same way and never honoured as root -- is the generic one
in `trusted_exec`, which the GitHub CLI (`trusted_gh`) shares. No trusted git is an error: callers
fail closed (exit 2), never fall back to PATH."""

from __future__ import annotations

from collections.abc import Mapping

from .trusted_exec import UntrustedExecutableError, candidate_reason, resolve_trusted

#: Where a system git lives, in preference order. Searched before PATH.
SYSTEM_GIT_PATHS = ("/usr/bin/git", "/bin/git", "/usr/local/bin/git", "/opt/homebrew/bin/git")
#: An explicit git to use instead, vetted like any other candidate.
OVERRIDE_ENV = "AGENTIC_SDLC_GIT"


class UntrustedGitError(UntrustedExecutableError):
    """No git executable could be proven to be one this session cannot have replaced."""


def git_candidate_reason(candidate: str) -> str | None:
    """Why the git at absolute path `candidate` is not trusted, or None when it is."""
    return candidate_reason(candidate)


def trusted_git(environ: Mapping[str, str] | None = None) -> str:
    """The resolved path of a trusted git executable; UntrustedGitError when there is none."""
    return resolve_trusted("git", SYSTEM_GIT_PATHS, OVERRIDE_ENV, environ, UntrustedGitError)
