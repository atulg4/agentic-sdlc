"""Resolve a git executable this session cannot have planted (Codex 4241614592).

`check-diff` is the cloud routine's last gate before a push, and it used to run a bare `git`,
i.e. whatever a PATH lookup finds first. When a PATH entry ahead of the system git is writable
(a user-owned `~/.local/bin`, a tool-cache `bin`), the agent being checked can drop its own `git`
there, have it fabricate benign `merge-base`/`rev-parse`/`diff` output for the check, and let the
real git publish the forbidden commit. The commit guard (`forge_commit_guard.py`) refuses the
same trick for the commands it vets (`_writable_path_reason`, `_path_git_reason`); this module is
the library side of that rule. The guard is a standalone template copied into target
repositories, so the small helper is duplicated rather than imported.

A git is trusted only when only root can change what runs: the file and every directory on the
way to it -- each prefix of the path as written, its resolved form, and every ancestor of both --
is owned by root, has no group or other write bit, and (for a non-root user) is not writable by
this user. When this process IS root, ownership proves nothing (everything the agent writes is
root's), so only the known system locations are considered then and PATH is not searched at all;
root can of course replace /usr/bin/git itself, which no check inside the session can prevent.

`AGENTIC_SDLC_GIT` may name another git; it is vetted by exactly the same rule (never honoured as
root). No trusted git is an error: callers fail closed (exit 2), never fall back to PATH."""

from __future__ import annotations

import os
import stat

#: Where a system git lives, in preference order. Searched before PATH.
SYSTEM_GIT_PATHS = ("/usr/bin/git", "/bin/git", "/usr/local/bin/git", "/opt/homebrew/bin/git")
#: An explicit git to use instead, vetted like any other candidate.
OVERRIDE_ENV = "AGENTIC_SDLC_GIT"


class UntrustedGitError(RuntimeError):
    """No git executable could be proven to be one this session cannot have replaced."""


def _euid() -> int | None:
    geteuid = getattr(os, "geteuid", None)
    return geteuid() if geteuid is not None else None


def _ancestors(path: str) -> list[str]:
    out = [path]
    while os.path.dirname(out[-1]) != out[-1]:
        out.append(os.path.dirname(out[-1]))
    return out


def _node_reason(path: str, euid: int) -> str | None:
    """Why `path` (a file or directory) could be changed by someone other than root."""
    try:
        info = os.stat(path)
    except OSError as exc:
        return f"{path} cannot be inspected ({exc.strerror or exc})"
    if info.st_uid != 0:
        return f"{path} is not owned by root"
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return f"{path} is writable by its group or by others"
    if euid != 0 and os.access(path, os.W_OK):
        return f"{path} is writable by this user"
    return None


def git_candidate_reason(candidate: str) -> str | None:
    """Why the git at absolute path `candidate` is not trusted (see the module docstring), or
    None when it is."""
    euid = _euid()
    if euid is None:
        return "file ownership cannot be checked on this platform"
    if not os.path.isabs(candidate):
        return f"{candidate} is not an absolute path"
    real = os.path.realpath(candidate)
    if not (os.path.isfile(real) and os.access(real, os.X_OK)):
        return f"{candidate} is not an executable file"
    nodes: list[str] = [real]
    prefix = os.sep
    for part in [p for p in candidate.split(os.sep) if p][:-1]:
        prefix = os.path.join(prefix, part)
        nodes += _ancestors(prefix) + _ancestors(os.path.realpath(prefix))
    nodes += _ancestors(os.path.dirname(real))
    for node in dict.fromkeys(nodes):
        reason = _node_reason(node, euid)
        if reason:
            return f"{candidate}: {reason}"
    return None


def trusted_git(environ: dict[str, str] | None = None) -> str:
    """The resolved path of a trusted git executable; UntrustedGitError when there is none."""
    env = os.environ if environ is None else environ
    euid = _euid()
    override = env.get(OVERRIDE_ENV, "")
    if override:
        if euid == 0:
            raise UntrustedGitError(
                f"{OVERRIDE_ENV} is not honoured when running as root: ownership cannot tell "
                "a system git from one this session wrote; unset it"
            )
        reason = git_candidate_reason(override)
        if reason:
            raise UntrustedGitError(f"{OVERRIDE_ENV} names an untrusted git: {reason}")
        return os.path.realpath(override)
    candidates = list(SYSTEM_GIT_PATHS)
    if euid != 0:
        for entry in env.get("PATH", "").split(os.pathsep):
            if entry and os.path.isabs(entry):  # a relative entry depends on the cwd: never
                candidates.append(os.path.join(entry, "git"))
    rejected: list[str] = []
    for candidate in dict.fromkeys(candidates):
        if not os.path.lexists(candidate):
            continue
        reason = git_candidate_reason(candidate)
        if reason is None:
            return os.path.realpath(candidate)
        rejected.append(reason)
    detail = "; ".join(rejected) or "no git executable was found"
    raise UntrustedGitError(
        "no trusted git executable (one only root can replace, outside any writable PATH "
        f"entry): {detail}"
    )
