#!/usr/bin/env python3
"""Claude Code PreToolUse hook: block `git commit` on an issue branch without this session's lease.

Installed by `sdlcctl onboard`. Reads the hook payload on stdin; exit 2 blocks the tool call and
shows the message to the agent. Only Bash commands that commit on `*/issue-N*` branches are
examined; everything else passes.

Posture: FAIL CLOSED. Two layers decide whether a command is a commit:

1. The precise parser (`_commit_targets`) PROVES a commit and finds the repository it lands in:
   wrappers, `sh -c`, git global options, aliases, `cd`, and the bodies of `$(...)` and
   backtick substitutions, which are parsed recursively with the same parser.
2. A conservative pre-check over the RAW command: when the text holds a `git` word and a
   `commit` word anywhere -- inside quotes, `$(...)`, backticks, `eval`, here-strings -- the
   command is a commit UNLESS every simple command in it is positively proven not to be one
   (`_segment_proven`): it holds no `$`/backtick expansion or here-string, and it either lacks
   one of the two words, or is git itself running one of its own non-commit subcommands
   (`git log --grep commit`), or is a `sh -c` whose string is proven the same way.

Anything the parser cannot prove is treated as a commit and goes to the lease check. Only an
unleased issue branch is ever blocked, so over-blocking (`echo "git commit"`) costs nothing on a
leased branch or off issue branches; a missed commit costs the lease. Unparseable text (unbalanced
quotes) holding both words is a commit too.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from datetime import UTC, datetime, timedelta

PROJECT = "PROJECT_ID"
ISSUE_BRANCH = re.compile(r"(?:^|/)issue-(\d+)(?:$|[^0-9])")
CLAIM = re.compile(r"<!--\s*forge-claim\s+([^>]*?)\s*-->")
RELEASE = re.compile(r"<!--\s*forge-release\s+([^>]*?)\s*-->")
# `git [global options] commit`: -C/-c/--git-dir/... take the NEXT word as their value, so
# `git -C . commit` and `git -c k=v commit` are commits too. `commit.gpgsign=false` is not.
GIT_VALUE_OPTS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"}
)
# Words that run the rest of the command line as a new command (`env X=1 git commit`, ...), each
# with the options that take a SEPARATE value (`env -u HOME`, `sudo -u bob`, `nice -n 5`): short
# letters (also clustered, `sudo -Eu bob`), long names (also as `--name=value`), how many
# positional words precede the command (`timeout 10 git ...`), and the options that change the
# working directory (`env -C dir`, `sudo -D dir`).
WRAPPER_OPTS: dict[str, tuple[str, frozenset[str], int, frozenset[str]]] = {
    "env": (
        "uCS",
        frozenset({"--unset", "--chdir", "--split-string"}),
        0,
        frozenset({"-C", "--chdir"}),
    ),
    "sudo": (
        "CDgprRtTUu",
        frozenset(
            {
                "--close-from",
                "--chdir",
                "--group",
                "--prompt",
                "--chroot",
                "--role",
                "--type",
                "--command-timeout",
                "--other-user",
                "--user",
                "--host",
            }
        ),
        0,
        frozenset({"-D", "--chdir"}),
    ),
    "doas": ("uC", frozenset(), 0, frozenset()),
    "nice": ("n", frozenset({"--adjustment"}), 0, frozenset()),
    "ionice": ("cn", frozenset({"--class", "--classdata"}), 0, frozenset()),
    "timeout": ("sk", frozenset({"--signal", "--kill-after"}), 1, frozenset()),
    "stdbuf": ("ioe", frozenset({"--input", "--output", "--error"}), 0, frozenset()),
    "xargs": (
        "aEdILnPs",
        frozenset(
            {
                "--arg-file",
                "--delimiter",
                "--max-args",
                "--max-procs",
                "--max-chars",
                "--process-slot-var",
            }
        ),
        0,
        frozenset(),
    ),
    "time": ("fo", frozenset({"--format", "--output"}), 0, frozenset()),
    "exec": ("a", frozenset(), 0, frozenset()),
    "command": ("", frozenset(), 0, frozenset()),
    "builtin": ("", frozenset(), 0, frozenset()),
    "nohup": ("", frozenset(), 0, frozenset()),
    "setsid": ("", frozenset(), 0, frozenset()),
}
WRAPPERS = frozenset(WRAPPER_OPTS)
SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
OPERATORS = frozenset({";", "&", "&&", "|", "||", "(", ")", ";;", "|&"})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Reserved words and grouping that may open a simple command without being its program:
# `if git commit`, `then git commit`, `do git commit`, `! git commit`, `{ git commit; }`, ...
# (`(`/`)` are separators already; `time` is a wrapper above).
SHELL_KEYWORDS = frozenset(
    {"if", "then", "elif", "else", "fi", "while", "until", "do", "done", "!", "{", "}", "esac"}
    | {"coproc", "function"}
)
# The fail-closed pre-check's words (see the module docstring), anywhere in the raw text.
_GIT_WORD = re.compile(r"\bgit\b")
_COMMIT_WORD = re.compile(r"\bcommit\b")
# Text that expands into words the parser cannot see: `$x`, `${x}`, `$(...)`, backticks, and a
# here-string / here-document feeding a command its input.
_EXPANSION = re.compile(r"[$`]|^<<")
MAX_SUBST_DEPTH = 3
MAX_TTL_MINUTES = 7 * 24 * 60  # leases.MAX_TTL_MINUTES
# Who may post lease markers -- the SAME rule as leases.trusted_marker_author (a test holds the
# two to identical decisions): a user with write/maintain/admin on the repository, read from the
# collaborator permission API (author_association is no authority: a read-only member is MEMBER),
# `github-actions[bot]`, or the Forge Publisher App's bot. Unreadable permission = untrusted.
WRITE_PERMISSIONS = frozenset({"admin", "maintain", "write"})  # leases.WRITE_PERMISSIONS
GITHUB_ACTIONS_BOT = "github-actions[bot]"  # leases.GITHUB_ACTIONS_BOT
PUBLISHER_APP_SLUG_HINT = "agentic-sdlc"  # leases.PUBLISHER_APP_SLUG_HINT
LEASE_BOT_LOGINS_ENV = "FORGE_LEASE_BOT_LOGINS"  # leases.LEASE_BOT_LOGINS_ENV
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


def _segments(command: str) -> list[list[str]]:
    """Shell words of each simple command, split on `;`, `&&`, `||`, `|`, `&`, parentheses."""
    lexer = shlex.shlex(command.replace("\n", ";"), posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    lexer.commenters = ""
    segments: list[list[str]] = [[]]
    for token in lexer:
        if token in OPERATORS:
            segments.append([])
        else:
            segments[-1].append(token)
    return [seg for seg in segments if seg]


# A commit invocation: (directories to change into, in order, from the hook's cwd; the git
# options that select the repository: -C/--git-dir/--work-tree; GIT_DIR/GIT_WORK_TREE set on the
# command line).
Target = tuple[tuple[str, ...], tuple[str, ...], tuple[tuple[str, str], ...]]
GIT_REPO_OPTS = frozenset({"-C", "--git-dir", "--work-tree"})
GIT_REPO_ENV = frozenset({"GIT_DIR", "GIT_WORK_TREE"})
# Command-line assignments git reads its configuration (and so its aliases) from.
GIT_CONFIG_ENV = re.compile(r"^GIT_CONFIG(?:_[A-Z0-9_]+)?$")
# Git's own subcommands: git never expands an alias that shadows one, so these are classified by
# name alone. Anything else (`git ci`) may be an alias and is resolved before it is classified.
GIT_BUILTINS = frozenset(
    {
        "add", "am", "annotate", "apply", "archive", "bisect", "blame", "branch", "bundle",
        "cat-file", "check-attr", "check-ignore", "check-ref-format", "checkout", "cherry",
        "cherry-pick", "clean", "clone", "commit-graph", "commit-tree", "config",
        "count-objects", "describe", "diff", "diff-files", "diff-index", "diff-tree",
        "difftool", "fetch", "for-each-ref", "format-patch", "fsck", "gc", "grep",
        "hash-object", "help", "init", "log", "ls-files", "ls-remote", "ls-tree",
        "maintenance", "merge", "merge-base", "mergetool", "mv", "notes", "prune", "pull",
        "push", "range-diff", "rebase", "reflog", "remote", "repack", "replace", "reset",
        "restore", "rev-list", "rev-parse", "revert", "rm", "shortlog", "show", "show-ref",
        "sparse-checkout", "stash", "status", "submodule", "switch", "symbolic-ref", "tag",
        "update-index", "update-ref", "var", "verify-commit", "version", "whatchanged",
        "worktree", "write-tree",
    }
)  # fmt: skip
MAX_ALIAS_DEPTH = 8


def _strip_wrapper(name: str, words: list[str], chdirs: list[str]) -> list[str]:
    """`words` after one wrapper's options and leading positionals (the wrapped command).
    A directory option is appended to `chdirs`; `env -S "..."` splits its string in."""
    short, long, positionals, chdir_opts = WRAPPER_OPTS[name]
    while words:
        word = words[0]
        if word == "--":
            words.pop(0)
            break
        if _ASSIGNMENT.match(word):
            break  # `env -i A=1 git`: the caller reads the assignments, then the command
        if word.startswith("--") and len(word) > 2:
            words.pop(0)
            option, eq, value = word.partition("=")
            if option in long and not eq and words:
                value = words.pop(0)
            if option in chdir_opts and value:
                chdirs.append(value)
            if option == "--split-string" and value:
                words[:0] = shlex.split(value)
            continue
        if word.startswith("-") and len(word) > 1:
            words.pop(0)
            for index, letter in enumerate(word[1:], start=2):
                if letter in short:
                    value = word[index:] or (words.pop(0) if words else "")
                    if f"-{letter}" in chdir_opts and value:
                        chdirs.append(value)
                    if name == "env" and letter == "S" and value:
                        words[:0] = shlex.split(value)
                    break
            continue
        break
    del words[: min(positionals, len(words))]
    return words


def _command_words(argv: list[str], chdirs: list[str], env: list[tuple[str, str]]) -> list[str]:
    """`argv` without leading reserved words, `VAR=value` prefixes and wrappers (their directory
    options go to `chdirs`, GIT_DIR/GIT_WORK_TREE assignments to `env`): the program and its
    arguments."""
    words = list(argv)
    while words:
        if words[0] in SHELL_KEYWORDS:
            words.pop(0)
        elif _ASSIGNMENT.match(words[0]):
            key, _, value = words.pop(0).partition("=")
            if key in GIT_REPO_ENV or GIT_CONFIG_ENV.match(key):
                env.append((key, value))
        elif os.path.basename(words[0]) in WRAPPERS:
            words = _strip_wrapper(os.path.basename(words.pop(0)), words, chdirs)
        else:
            break
    return words


# Shell options that take the NEXT word as their value (`bash -o pipefail -c ...`).
SHELL_VALUE_OPTS = frozenset("oO")
SHELL_LONG_VALUE_OPTS = frozenset({"--rcfile", "--init-file"})


def _shell_command_string(words: list[str]) -> str | None:
    """The command string a shell runs with `-c`, however its options are spelled: `sh -c CMD`,
    `bash -lc CMD`, `sh -ec CMD`, `bash -o pipefail -c CMD`, `bash -c -e CMD`. A shell reads its
    options up to the first operand (or `--`); when any short-option cluster among them holds
    `c`, that first operand is the command. None when `words` is not a shell running `-c`."""
    if not words or os.path.basename(words[0]) not in SHELLS:
        return None
    rest = list(words[1:])
    has_c = False
    while rest:
        word = rest[0]
        if word == "--":
            rest.pop(0)
            break
        if word.startswith("--") and len(word) > 2:
            rest.pop(0)
            if word.partition("=")[0] in SHELL_LONG_VALUE_OPTS and "=" not in word and rest:
                rest.pop(0)
            continue
        if word[:1] in ("-", "+") and len(word) > 1:
            rest.pop(0)
            letters = word[1:]
            if "c" in letters and word[0] == "-":
                has_c = True
            for _ in range(sum(letter in SHELL_VALUE_OPTS for letter in letters)):
                if rest:
                    rest.pop(0)
            continue
        break
    return rest[0] if has_c and rest else None


def _where(cwd: str | None, chdirs) -> str:
    """The directory `chdirs` lead to from `cwd`, in order (`cd -` is unknowable here: stay put)."""
    where = cwd or os.getcwd()
    for directory in chdirs:
        if directory != "-":
            where = os.path.join(where, os.path.expanduser(directory))
    return where


def git_alias(
    name: str, where: str, git_opts: tuple[str, ...] = (), env: dict[str, str] | None = None
) -> str | None:
    """`alias.<name>` as git itself resolves it in `where` (repository, global and system
    configuration, plus GIT_CONFIG_* set on the command line); None when it has none."""
    try:
        proc = subprocess.run(
            ["git", *git_opts, "config", "--get", f"alias.{name}"],
            cwd=where if os.path.isdir(where) else None,
            env={**os.environ, **env} if env else None,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    return proc.stdout.rstrip("\n") if proc.returncode == 0 else None


def _commit_targets(argv: list[str], depth: int = 0, cwd: str | None = None) -> list[Target]:
    """The repository each `git ... commit` in one simple command commits to. A subcommand that
    is not one of git's own is resolved as an alias first -- inline `-c alias.X=...`, then the
    configuration git reads where the command runs -- and classified by its expansion; a shell
    (`!`) alias, or one whose value cannot be read, counts as a commit (fails closed)."""
    chdirs: list[str] = []
    env: list[tuple[str, str]] = []
    words = _command_words(argv, chdirs, env)
    if not words:
        return []
    program = os.path.basename(words[0])
    inner = _shell_command_string(words)
    if inner is not None and depth < 3:
        return [
            ((*chdirs, *inner_dirs), opts, (*env, *inner_env))
            for inner_dirs, opts, inner_env in commit_targets(inner, depth + 1, _where(cwd, chdirs))
        ]
    if program not in ("git", "git.exe"):
        return []
    rest = words[1:]
    repo_opts: list[str] = []
    global_opts: list[str] = []  # every global option, re-applied to an alias's expansion
    # Inline aliases, name -> value; one set from the environment (`--config-env`) cannot be read
    # here and is recorded as a shell alias, which fails closed.
    inline: dict[str, str] = {}
    while rest:
        word = rest.pop(0)
        if word in GIT_VALUE_OPTS:
            if rest:
                value = rest.pop(0)  # the option's value, e.g. the path after -C
                global_opts += [word, value]
                if word in GIT_REPO_OPTS:
                    repo_opts += [word, value]
                key, eq, setting = value.partition("=")
                if key.lower().startswith("alias."):
                    alias = key[len("alias.") :].lower()
                    inline[alias] = "!" if word == "--config-env" else (setting if eq else "")
            continue
        if word.startswith("--config-env="):
            global_opts.append(word)
            key = word[len("--config-env=") :].partition("=")[0]
            if key.lower().startswith("alias."):
                inline[key[len("alias.") :].lower()] = "!"
            continue
        if word.startswith(("--git-dir=", "--work-tree=")):
            repo_opts.append(word)
            global_opts.append(word)
            continue
        if word.startswith("-C") and len(word) > 2:
            repo_opts += ["-C", word[2:]]
            global_opts += ["-C", word[2:]]
            continue
        if word.startswith("-"):
            global_opts.append(word)
            continue  # --no-pager, --exec-path=..., ...
        target = [(tuple(chdirs), tuple(repo_opts), tuple(env))]
        if word == "commit":
            return target
        if word in GIT_BUILTINS:
            return []
        if _EXPANSION.search(word):
            return target  # `git $sub`: the subcommand is unknowable here, fail closed
        expansion: str | None
        if word.lower() in inline:
            expansion = inline[word.lower()]
        else:
            expansion = git_alias(word, _where(cwd, chdirs), tuple(repo_opts), dict(env))
        if expansion is None:
            return []  # not an alias: an external `git-<word>` command
        try:
            expanded = shlex.split(expansion)
        except ValueError:
            return target
        if expansion.lstrip().startswith("!") or not expanded or depth >= MAX_ALIAS_DEPTH:
            return target  # a shell alias (or one we cannot follow): fail closed
        return _commit_targets(
            [*argv[: len(argv) - len(words)], "git", *global_opts, *expanded, *rest],
            depth + 1,
            cwd,
        )
    return []


def _mentions_commit(text: str) -> bool:
    """The pre-check: a `git` word and a `commit` word, in any order, anywhere in `text`."""
    return bool(_GIT_WORD.search(text) and _COMMIT_WORD.search(text))


def _substitutions(word: str) -> list[str]:
    """The bodies of every `$(...)` and backtick substitution in one shell word (nested ones are
    found when the body is parsed). Quoting is gone after shlex, so a single-quoted literal
    counts too -- over-blocking, never under-. An unclosed one runs to the end of the word."""
    bodies: list[str] = []
    index = 0
    while index < len(word):
        if word.startswith("$(", index):
            depth, end = 1, index + 2
            while end < len(word) and depth:
                depth += {"(": 1, ")": -1}.get(word[end], 0)
                end += 1
            bodies.append(word[index + 2 : end - 1 if not depth else end])
            index = end
        elif word[index] == "`":
            close = word.find("`", index + 1)
            end = close if close != -1 else len(word)
            bodies.append(word[index + 1 : end])
            index = end + 1
        else:
            index += 1
    return bodies


def _git_subcommand(words: list[str]) -> str | None:
    """The subcommand of `git [global options] SUB ...` (None when there is none)."""
    rest = list(words[1:])
    while rest:
        word = rest.pop(0)
        if word in GIT_VALUE_OPTS:
            if rest:
                rest.pop(0)
            continue
        if word.startswith("-"):
            continue
        return word
    return None


def _segment_proven(seg: list[str], depth: int, cwd: str | None) -> bool:
    """Is this simple command positively proven NOT to commit (the fail-closed pre-check)? No
    expansion the parser cannot see, and: it lacks a `git` or a `commit` word, or it is git
    itself running one of git's own non-commit subcommands, or a `sh -c` whose string is proven.
    Anything else that mentions both words (`eval`, `echo "git commit"`, `bash <<< ...`, an
    unknown wrapper, a git alias or external `git-foo`) is not proven."""
    if any(_EXPANSION.search(word) for word in seg):
        return False
    if not _mentions_commit(" ".join(seg)):
        return True
    words = _command_words(seg, [], [])
    if not words:
        return True  # assignments only (`MSG="git commit"`): nothing runs
    inner = _shell_command_string(words)
    if inner is not None:
        return depth < MAX_SUBST_DEPTH and not _unproven(inner, depth + 1, cwd)
    if os.path.basename(words[0]) in ("git", "git.exe"):
        sub = _git_subcommand(words)
        return sub is not None and sub != "commit" and sub in GIT_BUILTINS
    return False


def _unproven(command: str, depth: int, cwd: str | None) -> bool:
    """Does any simple command in `command` escape proof (see `_segment_proven`)?"""
    if not _mentions_commit(command):
        return False
    try:
        segments = _segments(command)
    except ValueError:
        return True
    for seg in segments:
        while seg and seg[0] in SHELL_KEYWORDS:
            seg = seg[1:]
        if seg and not _segment_proven(seg, depth, cwd):
            return True
    return False


def commit_targets(command: str, depth: int = 0, cwd: str | None = None) -> list[Target]:
    """Every `git ... commit` in `command` with the repository it commits to. `cd DIR` before it
    (`cd ../wt && git commit`) counts too. Fails closed: a simple command that is not proven
    to be a non-commit while the text mentions `git` and `commit` counts as one."""
    suspicious = _mentions_commit(command)
    found: list[Target] = []
    cds: list[str] = []
    for seg in _segments(command):
        while seg and seg[0] in SHELL_KEYWORDS:
            seg = seg[1:]
        if not seg:
            continue
        where = _where(cwd, cds)
        is_cd = seg[0] == "cd"
        if is_cd:  # changes where later commands run; its own substitutions run here
            operands = [w for w in seg[1:] if not w.startswith("-") or w == "-"]
            cds.append(operands[-1] if operands else "~")
        targets = [] if is_cd else _commit_targets(seg, depth, where)
        for body in (body for word in seg for body in _substitutions(word)):
            if depth >= MAX_SUBST_DEPTH:
                targets += [((), (), ())] if _mentions_commit(body) else []
                continue
            try:  # a substitution runs where the command runs: same `cd`s
                targets += commit_targets(body, depth + 1, where)
            except ValueError:
                targets += [((), (), ())] if _mentions_commit(body) else []
        if not targets and suspicious and not _segment_proven(seg, depth, where):
            targets = [((), (), ())]  # unknown repository: checked from cwd and the `cd`s
        prefix = cds[:-1] if is_cd else cds
        found += [((*prefix, *dirs), opts, env) for dirs, opts, env in targets]
    return found


def is_git_commit(command: str, depth: int = 0, cwd: str | None = None) -> bool:
    """True when any simple command in `command` runs `git ... commit`, however git is spelled
    (`git`, `/usr/bin/git`, `./git`), whatever wrapper (with its options) runs it, whatever
    global options precede the subcommand, and whatever alias (`git ci`) stands for it."""
    try:
        return bool(commit_targets(command, depth, cwd))
    except ValueError:  # unparseable (unbalanced quotes): fail closed on the two words
        return _mentions_commit(command)


def target_branch(target: Target, cwd: str | None) -> str:
    """The branch `git <repo options> commit` would commit to, asked of git itself."""
    chdirs, opts, env = target
    return current_branch(_where(cwd, chdirs), opts, dict(env))


def commit_branches(command: str, cwd: str | None) -> list[str]:
    """The branches the commits in `command` land on. Each commit is checked both where its
    `cd`s lead and from the hook's cwd (a `cd` inside a subshell does not persist), so the guard
    errs towards checking more, never fewer."""
    try:
        targets = commit_targets(command, cwd=cwd)
    except ValueError:
        targets = []
    if not targets:
        return [current_branch(cwd)]
    branches: list[str] = []
    for target in targets:
        for candidate in (target, ((), target[1], target[2])):
            branch = target_branch(candidate, cwd)
            if branch not in branches:
                branches.append(branch)
    return branches


def current_branch(
    cwd: str | None, git_opts: tuple[str, ...] = (), env: dict[str, str] | None = None
) -> str:
    try:
        return subprocess.run(
            ["git", *git_opts, "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd,
            env={**os.environ, **env} if env else None,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _parse_iso(text: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)  # naive means UTC


def trusted_bot(login: str, configured: str | None = None) -> bool:
    """`github-actions[bot]`, or the Publisher App's bot: the logins in FORGE_LEASE_BOT_LOGINS
    when set, else a `<slug>[bot]` whose slug carries PUBLISHER_APP_SLUG_HINT. No other bot."""
    login = login.lower()
    if login == GITHUB_ACTIONS_BOT:
        return True
    if configured is None:
        configured = os.environ.get(LEASE_BOT_LOGINS_ENV, "")
    named = {part.strip().lower() for part in configured.split(",") if part.strip()}
    if named:
        return login in named
    return login.endswith("[bot]") and PUBLISHER_APP_SLUG_HINT in login[: -len("[bot]")]


def trusted_marker_author(comment: dict, permission, configured: str | None = None) -> bool:
    """May this comment's author post lease markers (leases.trusted_marker_author)?"""
    user = comment.get("user") or {}
    login = str(user.get("login") or "")
    if not login:
        return False
    if user.get("type") == "Bot" or login.endswith("[bot]"):
        return user.get("type") == "Bot" and trusted_bot(login, configured)
    if not _LOGIN.fullmatch(login):
        return False
    granted = permission(login)
    return granted is not None and granted in WRITE_PERMISSIONS


_PERMISSIONS: dict[str, str | None] = {}  # one read per login per hook run


def repo_permission(login: str) -> str | None:
    """The login's repository permission (role_name, else permission); None when unreadable."""
    if login in _PERMISSIONS:
        return _PERMISSIONS[login]
    granted: str | None = None
    try:
        out = subprocess.run(
            ["gh", "api", f"repos/{PROJECT}/collaborators/{login}/permission"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        data = json.loads(out or "{}")
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
        data = None
    if isinstance(data, dict):
        for key in ("role_name", "permission"):
            value = str(data.get(key) or "").lower()
            if value in WRITE_PERMISSIONS:
                granted = value
                break
        else:
            granted = str(data.get("permission") or "") or None
    _PERMISSIONS[login] = granted
    return granted


def lease_from_comments(
    comments: list, permission=None, now: datetime | None = None
) -> dict | None:
    """The live lease in an issue's comments: the earliest-claiming live session (None if none).
    Same ordering as leases.current_lease(): a session's newest claim supersedes its older ones,
    a release voids that session, and the live session that claimed FIRST holds the lease, so a
    losing racer's retraction cannot clear the winner."""
    permission = permission or repo_permission
    first_seen: dict[str, int] = {}
    latest: dict[str, dict] = {}
    for position, c in enumerate(comments):
        if not isinstance(c, dict) or not trusted_marker_author(c, permission):
            continue  # markers count only from writers and the trusted bots
        body = c.get("body") or ""
        found = list(CLAIM.finditer(body))
        m = found[-1] if found else None
        if m:
            fields = dict(re.findall(r"(\w+)=(\S+)", m.group(1)))
            exp = _parse_iso(fields.get("expires", ""))
            if exp is None or not all(k in fields for k in ("agent", "session", "branch")):
                continue
            posted = _parse_iso(c.get("created_at") or "")
            if posted is not None:  # expiry is capped at post time + MAX_TTL, as in leases.py
                exp = min(exp, posted + timedelta(minutes=MAX_TTL_MINUTES))
            previous = latest.get(fields["session"])
            if previous is not None and posted is not None and previous["expires"] <= posted:
                first_seen[fields["session"]] = position  # lapsed: a fresh claim, as in leases.py
            first_seen.setdefault(fields["session"], position)
            latest[fields["session"]] = {**fields, "expires": exp}
            continue
        r = RELEASE.search(body)
        released = dict(re.findall(r"(\w+)=(\S+)", r.group(1))).get("session") if r else None
        if released in latest:
            del latest[released]
            del first_seen[released]
    now = now or datetime.now(UTC)
    for session in sorted(latest, key=first_seen.__getitem__):
        if latest[session]["expires"] > now:
            return latest[session]
    return None


def lease_for(issue: int) -> dict | None:
    """The live lease of `issue` (None if none/unreachable)."""
    try:
        out = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{PROJECT}/issues/{issue}/comments?per_page=100",
                "--paginate",
                "--slurp",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        pages = json.loads(out or "[]")
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
        return None
    comments = [c for page in pages if isinstance(page, list) for c in page if isinstance(c, dict)]
    return lease_from_comments(comments, repo_permission)


def open_pr_for(branch: str) -> bool:
    """An open PR from this branch is the durable claim once the lease is released."""
    try:
        out = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                PROJECT,
                "--head",
                branch,
                "--state",
                "open",
                "--json",
                "number",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return bool(json.loads(out or "[]"))
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
        return False


def decide(
    payload: dict, branch: str | list[str], lease_lookup=lease_for, pr_lookup=open_pr_for
) -> tuple[int, str]:
    """Block (2, why) a commit onto an issue branch this session does not lease. `branch` is the
    branch the commit lands on, or every candidate (`commit_branches`); any unleased one blocks."""
    if payload.get("tool_name") != "Bash":
        return 0, ""
    command = str((payload.get("tool_input") or {}).get("command", ""))
    if not is_git_commit(command, cwd=payload.get("cwd")):
        return 0, ""
    for candidate in [branch] if isinstance(branch, str) else branch:
        verdict = _decide_branch(payload, candidate, lease_lookup, pr_lookup)
        if verdict[0]:
            return verdict
    return 0, ""


def _decide_branch(payload: dict, branch: str, lease_lookup, pr_lookup) -> tuple[int, str]:
    m = ISSUE_BRANCH.search(branch or "")
    if not m:
        return 0, ""
    issue = int(m.group(1))
    session = str(payload.get("session_id") or "")
    lease = lease_lookup(issue)
    if lease is None and pr_lookup(branch):
        return 0, ""  # follow-up commits to an open PR need no lease
    if lease is None:
        return 2, (
            f"Forge guard: branch '{branch}' targets issue #{issue} but no live lease exists. "
            f"Claim it first: sdlcctl claim --project {PROJECT} --issue {issue} "
            f"--agent claude-code --session {session or '<session_id>'} --branch {branch}"
        )
    if session and lease.get("session") != session:
        return 2, (
            f"Forge guard: issue #{issue} is leased by agent={lease.get('agent')} "
            f"session={lease.get('session')} until {lease['expires'].isoformat()}. "
            "Do not commit to it; pick another issue or wait for the lease to expire."
        )
    return 0, ""


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0
    command = str((payload.get("tool_input") or {}).get("command", ""))
    branches = (
        commit_branches(command, payload.get("cwd"))
        if is_git_commit(command, cwd=payload.get("cwd"))
        else []
    )
    code, message = decide(payload, branches)
    if message:
        print(message, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
