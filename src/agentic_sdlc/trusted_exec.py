"""Resolve an executable this session cannot have planted (Codex 4241614592, 4241745403).

`check-diff` (git) and `authorize-labeler` / `check-request` / `claim` / `renew` (the GitHub CLI)
make security decisions from what an external program prints. A bare program name is a PATH
lookup: when a PATH entry ahead of the system copy is writable (a user-owned `~/.local/bin`, a
tool-cache `bin`), the agent being checked can drop its own `git` or `gh` there, have it fabricate
benign output for the check, and then use the real program to publish or claim. The commit guard
(`forge_commit_guard.py`) refuses the same trick for the commands it vets; this module is the
library side of that rule. The guard and the session-start hook are standalone templates copied
into target repositories, so they keep their own copy of the check rather than importing this.

An executable is trusted only when only root can change what runs: the file and every directory on
the way to it -- each prefix of the path as written, its resolved form, and every ancestor of both
-- is owned by root, has no group or other write bit, and (for a non-root user) is not writable by
this user. When this process IS root, ownership proves nothing (everything the agent writes is
root's), so only the known system locations are considered then and PATH is not searched at all;
root can of course replace /usr/bin/git itself, which no check inside the session can prevent.

An override variable (`AGENTIC_SDLC_GIT`, `AGENTIC_SDLC_GH`) may name another copy; it is vetted by
exactly the same rule and never honoured as root. No trusted executable is an error: callers fail
closed (exit 2), never fall back to PATH.

That is the STRICT trust mode, and it is what protects agent contexts. A developer's own machine
rarely has a root-owned Python 3.11+ or GitHub CLI (Homebrew installs them owned by the user), so
the policy may choose `[automation] trust_mode = "local"` (`sdlcctl onboard --trust-mode local`).
In LOCAL mode the program is the first match on PATH (then the system locations), an override
variable is honoured even as root, and the file must be owned by root or by this user and carry
no group or other write bit; its directories are not checked. Everything else stays: the
environment the program runs in is still built from an allowlist (no LD_*/DYLD_*, a fresh gh
config directory, the pinned host), and the hook interpreter is still an absolute path chosen
at onboard time and probed.

The mode in force (`trust_mode()`) is STRICT whenever this runs in an agent context -- under
GitHub Actions (`GITHUB_ACTIONS=true`) or in a Claude Code cloud session
(`CLAUDE_CODE_REMOTE=true`, the variable Claude Code sets in its cloud environments) -- whatever
the policy says. Elsewhere it is the policy's (`use_trust_mode`, which the CLI calls with the
policy it reads), and strict when no policy chose one."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterable, Mapping

STRICT = "strict"
LOCAL = "local"
TRUST_MODES = (STRICT, LOCAL)
#: Environment markers of an agent context, where the mode is always STRICT: (variable, value).
AGENT_CONTEXT_MARKERS = (("GITHUB_ACTIONS", "true"), ("CLAUDE_CODE_REMOTE", "true"))

#: The mode the policy chose (`use_trust_mode`); None = no policy said, i.e. STRICT.
_POLICY_MODE: str | None = None


def trust_mode_problem(value: object) -> str:
    """Why `value` is not a trust mode ('' = it is)."""
    return "" if value in TRUST_MODES else f"must be one of {', '.join(TRUST_MODES)}"


def use_trust_mode(mode: str | None) -> None:
    """Record the policy's trust mode for this process (None: no policy chose one -> strict)."""
    global _POLICY_MODE
    if mode is not None and trust_mode_problem(mode):
        raise ValueError(f"trust mode {mode!r} {trust_mode_problem(mode)}")
    _POLICY_MODE = mode


def agent_context(environ: Mapping[str, str] | None = None) -> str | None:
    """The marker (`NAME=value`) saying this runs in an agent context, or None."""
    env = os.environ if environ is None else environ
    for name, value in AGENT_CONTEXT_MARKERS:
        if env.get(name, "").strip().lower() == value:
            return f"{name}={value}"
    return None


def trust_mode(environ: Mapping[str, str] | None = None) -> str:
    """The trust mode in force: STRICT in an agent context, else the policy's, else STRICT."""
    if agent_context(environ):
        return STRICT
    return _POLICY_MODE or STRICT


class UntrustedExecutableError(RuntimeError):
    """No copy of a program could be proven to be one this session cannot have replaced."""


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


def candidate_reason(candidate: str) -> str | None:
    """Why the executable at absolute path `candidate` is not trusted (see the module
    docstring), or None when it is."""
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


def local_candidate_reason(candidate: str) -> str | None:
    """Why the executable at absolute path `candidate` is not acceptable in LOCAL mode, or None:
    the resolved file must be owned by root or by this user and carry no group or other write
    bit (its directories are not checked)."""
    euid = _euid()
    if euid is None:
        return "file ownership cannot be checked on this platform"
    if not os.path.isabs(candidate):
        return f"{candidate} is not an absolute path"
    real = os.path.realpath(candidate)
    if not (os.path.isfile(real) and os.access(real, os.X_OK)):
        return f"{candidate} is not an executable file"
    try:
        info = os.stat(real)
    except OSError as exc:
        return f"{candidate}: {real} cannot be inspected ({exc.strerror or exc})"
    if info.st_uid not in (0, euid):
        return f"{candidate}: {real} is owned by neither root nor this user"
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return f"{candidate}: {real} is writable by its group or by others"
    return None


def mode_candidate_reason(candidate: str, mode: str) -> str | None:
    """`candidate_reason` in STRICT mode, `local_candidate_reason` in LOCAL mode."""
    return local_candidate_reason(candidate) if mode == LOCAL else candidate_reason(candidate)


def _resolve_local(
    name: str,
    system_paths: tuple[str, ...],
    override_env: str,
    env: Mapping[str, str],
    error: type[UntrustedExecutableError],
) -> str:
    """LOCAL mode: the override when set, else the FIRST `name` on the absolute PATH entries,
    then the system locations -- vetted by `local_candidate_reason`. The first match is the one
    a shell would run: one that fails the vetting is refused, never skipped for a later one."""
    override = env.get(override_env, "")
    if override:
        reason = local_candidate_reason(override)
        if reason:
            raise error(f"{override_env} names an unsafe {name}: {reason}")
        return os.path.realpath(override)
    candidates = [
        os.path.join(entry, name)
        for entry in env.get("PATH", "").split(os.pathsep)
        if entry and os.path.isabs(entry)
    ]
    for candidate in dict.fromkeys([*candidates, *system_paths]):
        real = os.path.realpath(candidate)
        if not (os.path.isfile(real) and os.access(real, os.X_OK)):
            continue
        reason = local_candidate_reason(candidate)
        if reason:
            raise error(f"the {name} found first (trust mode local) is unsafe: {reason}")
        return real
    raise error(f"no {name} executable was found on PATH or in {', '.join(system_paths)}")


def resolve_trusted(
    name: str,
    system_paths: Iterable[str],
    override_env: str,
    environ: Mapping[str, str] | None = None,
    error: type[UntrustedExecutableError] = UntrustedExecutableError,
) -> str:
    """The resolved path of a trusted `name` executable. STRICT mode (`trust_mode`):
    `override_env` when set (vetted; refused as root), else the first trusted one of
    `system_paths` and then -- for a non-root user only -- the absolute PATH entries. LOCAL mode:
    `_resolve_local`. `error` (an UntrustedExecutableError) when there is none."""
    env = os.environ if environ is None else environ
    if trust_mode(env) == LOCAL:
        return _resolve_local(name, tuple(system_paths), override_env, env, error)
    euid = _euid()
    override = env.get(override_env, "")
    if override:
        if euid == 0:
            raise error(
                f"{override_env} is not honoured when running as root: ownership cannot tell "
                f"a system {name} from one this session wrote; unset it"
            )
        reason = candidate_reason(override)
        if reason:
            raise error(f"{override_env} names an untrusted {name}: {reason}")
        return os.path.realpath(override)
    candidates = list(system_paths)
    if euid != 0:
        for entry in env.get("PATH", "").split(os.pathsep):
            if entry and os.path.isabs(entry):  # a relative entry depends on the cwd: never
                candidates.append(os.path.join(entry, name))
    rejected: list[str] = []
    for candidate in dict.fromkeys(candidates):
        if not os.path.lexists(candidate):
            continue
        reason = candidate_reason(candidate)
        if reason is None:
            return os.path.realpath(candidate)
        rejected.append(reason)
    detail = "; ".join(rejected) or f"no {name} executable was found"
    raise error(
        f"no trusted {name} executable (one only root can replace, outside any writable PATH "
        f"entry): {detail}"
    )


# ------------------------------------------------------------------ the GitHub CLI

#: Where a system GitHub CLI lives, in preference order. Searched before PATH.
SYSTEM_GH_PATHS = ("/usr/bin/gh", "/bin/gh", "/usr/local/bin/gh", "/opt/homebrew/bin/gh")
#: An explicit GitHub CLI to use instead, vetted like any other candidate.
GH_OVERRIDE_ENV = "AGENTIC_SDLC_GH"


class UntrustedGhError(UntrustedExecutableError):
    """No GitHub CLI could be proven to be one this session cannot have replaced."""


def trusted_gh(environ: Mapping[str, str] | None = None) -> str:
    """The resolved path of a trusted `gh` (Codex 4241745403); UntrustedGhError when none."""
    return resolve_trusted("gh", SYSTEM_GH_PATHS, GH_OVERRIDE_ENV, environ, UntrustedGhError)
