#!/usr/bin/env python3
"""Claude Code PreToolUse hook: guard git on an issue branch without this session's lease.

Installed by `sdlcctl onboard`. Reads the hook payload on stdin; exit 2 blocks the tool call and
shows the message to the agent. Only an issue branch (`*/issue-N*`) this session does not lease
is ever blocked; everything else passes.

Posture: DENY BY DEFAULT for git. On an issue branch, EVERY git invocation needs the lease except
a short allowlist of read-only subcommands proven by plain literal tokens (`_read_only`):
status, log, diff, show, blame, grep (no `-O` pager), ls-files, ls-tree, rev-parse, describe,
shortlog, reflog (show/list), branch (only --list/-l/-a/-r/-v/--show-current/--contains, no
positional arguments), remote (-v/show), config (--get*/--list/-l, no write mode), fetch (no
--update-head-ok/-u, --upload-pack or refspec), stash list/show, tag (-l/--list, no create or
delete flag), help, version, cat-file, for-each-ref, name-rev, merge-base, check-ignore,
check-attr. Everything else -- commit, merge, cherry-pick, revert, rebase, am, apply, pull, reset,
checkout, switch, stash push/pop/apply, notes, tag creation, update-ref, commit-tree,
fast-import, filter-branch, replace, worktree, push, `git-<name>` programs, aliases and unknown
subcommands -- goes to the lease check. So does an allowlisted subcommand whose meaning git or
the shell can change out of sight: `-c`/`--config-env`/`--exec-path=` (pager, diff and fetch
commands are configuration), an environment assignment other than GIT_DIR/GIT_WORK_TREE/locale
on it (or a GIT_*/PAGER/EDITOR assignment anywhere in the command), `--output`, or an argument
holding `$`, a backtick or a brace expansion.

Indirect execution is guarded the same way: any other program that receives `git` as a word
(`xargs git`, `xargs -a f git`, `find -exec git`, `parallel git`, `eval git ...`,
`python -c "...'git'..."`), `xargs`/`parallel`/`find` handing work to a shell, and a shell reading
its program from stdin (`... | sh`, `bash <<< ...`, `bash -s`). `sh -c STRING` is parsed
recursively, as are `$(...)`/backtick bodies; a shell running a script FILE is opaque like any
other program and passes.

A raw-text proof (`_raw_unproven`) backs the parser: the shell can ASSEMBLE a program or
subcommand (`g$'it' com$'mit'`, `"g"it`, `\\git`, `co{m,}mit`, `$(echo git)`) the parser cannot
see, so every program and git subcommand must be a plain literal word in the raw text
(`[A-Za-z0-9_./+-]+`), `eval`/`source` may take only plain words, and `$'...'`/`$"..."` quoting
next to the letters of `git` then `commit` is unproven outright. Unparseable text that mentions
git is guarded too. Over-blocking (`echo "git merge"`) costs nothing on a leased branch or off
issue branches; a missed history change costs the lease.

Out of scope: programs that run git without naming it on the command line (a script file, a
Makefile target) and direct writes into `.git/`.
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
# What lease_for returns when GitHub could not be read. NEVER the same as "no lease" (None): a
# failed read that looked like an empty inventory sent sessions into work owned elsewhere.
UNAVAILABLE: dict = {"unavailable": True, "session": "", "agent": "unknown"}


class LeaseUnavailable(Exception):
    """A lease input (comments, a marker author's permission) could not be read from GitHub."""


def lease_unavailable(lease: object) -> bool:
    return isinstance(lease, dict) and bool(lease.get("unavailable"))


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
    "time": ("fo", frozenset({"--format", "--output"}), 0, frozenset()),
    "exec": ("a", frozenset(), 0, frozenset()),
    "command": ("", frozenset(), 0, frozenset()),
    "builtin": ("", frozenset(), 0, frozenset()),
    "nohup": ("", frozenset(), 0, frozenset()),
    "setsid": ("", frozenset(), 0, frozenset()),
}
WRAPPERS = frozenset(WRAPPER_OPTS)
SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
GIT_PROGRAMS = frozenset({"git", "git.exe"})
# Programs that run a command assembled from their input or arguments (`xargs sh`, `find -exec
# bash`, `parallel sh`): handing work to a shell is unprovable (Codex 4206173667). `xargs` is NOT
# a wrapper any more -- the arguments it appends from stdin or `-a FILE` are invisible here.
FANOUT = frozenset({"xargs", "parallel", "find"})
OPERATORS = frozenset({";", "&", "&&", "|", "||", "(", ")", ";;", "|&"})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Reserved words and grouping that may open a simple command without being its program:
# `if git commit`, `then git commit`, `do git commit`, `! git commit`, `{ git commit; }`, ...
# (`(`/`)` are separators already; `time` is a wrapper above).
SHELL_KEYWORDS = frozenset(
    {"if", "then", "elif", "else", "fi", "while", "until", "do", "done", "!", "{", "}", "esac"}
    | {"coproc", "function"}
)
# A `git` word anywhere in a program's (cooked) arguments: `git`, `/usr/bin/git`, `'git'` inside
# a Python string -- but not `.git/HEAD`, `legit`, `github` or `git-lfs`.
_GIT_WORD = re.compile(r"(?<![\w.-])git(?![\w-])")
# An assignment anywhere in the command to a variable that changes what a read-only git command
# runs (GIT_EXTERNAL_DIFF, GIT_PAGER, GIT_CONFIG_*, GIT_SSH, PAGER, EDITOR, ...), checked
# with quotes and backslashes removed (`export GIT_EXTERNAL"_DIFF=x"`). GIT_DIR and GIT_WORK_TREE
# only select the repository.
_ENV_TAINT = re.compile(
    r"(?<![A-Za-z0-9_])(?:GIT_(?!DIR=|WORK_TREE=)[A-Z0-9_]+|PAGER|EDITOR|VISUAL|LESS[A-Z]*)="
)
# Command-line assignments that leave a read-only git command read-only.
SAFE_GIT_ENV = frozenset({"GIT_DIR", "GIT_WORK_TREE", "LANG", "TZ", "TERM", "NO_COLOR", "COLUMNS"})
# A bash brace expansion (`{a,b}`, `{1..3}`): shlex keeps it one word, the shell does not.
_BRACE_EXPANSION = re.compile(r"\{[^{}]*(?:,|\.\.)[^{}]*\}")
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
# Deny by default (Codex 4206173667 / 4206173686): the read-only subcommands, each a plain
# literal token. Those in READ_ONLY_ANY take any arguments; the rest are read-only only for the
# argument shapes `_read_only` admits. Aliases are not followed: an alias is not a literal
# allowlisted token, so `git st` needs the lease like any unknown subcommand.
READ_ONLY_ANY = frozenset(
    {
        "status", "log", "diff", "show", "blame", "ls-files", "ls-tree", "rev-parse",
        "describe", "shortlog", "help", "version", "cat-file", "for-each-ref", "name-rev",
        "merge-base", "check-ignore", "check-attr",
    }
)  # fmt: skip
READ_ONLY_SHAPED = frozenset({"grep", "reflog", "branch", "remote", "config", "fetch"})
READ_ONLY_SHAPED |= {"stash", "tag"}
BRANCH_LIST_OPTS = frozenset({"--list", "-l", "-a", "--all", "-r", "--remotes", "-v"})
BRANCH_LIST_OPTS |= {"-vv", "--verbose", "--show-current", "--contains"}
CONFIG_READ = frozenset({"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l"})
CONFIG_WRITE = frozenset(
    {"--add", "--unset", "--unset-all", "--replace-all", "--rename-section", "--remove-section"}
    | {"-e", "--edit", "set", "unset", "rename-section", "remove-section", "edit"}
)
TAG_WRITE = frozenset({"--delete", "--annotate", "--sign", "--local-user", "--force"})
TAG_WRITE |= {"--message", "--file", "--create-reflog", "--no-sign", "--edit"}


def _short_cluster(arg: str) -> str:
    """The letters of a short-option cluster (`-av` -> `av`); empty for anything else."""
    return arg[1:] if arg.startswith("-") and not arg.startswith("--") else ""


def _read_only(sub: str, args: list[str]) -> bool:
    """Is `git SUB ARGS` proven read-only for the branch's history? Deny by default: a
    subcommand outside the allowlist, or an allowlisted one with an argument shape not admitted
    here, is not. Expansions and brace expansions are unprovable everywhere (`git fetch -{u,}`)."""
    if any(_EXPANSION.search(a) or _BRACE_EXPANSION.search(a) for a in args):
        return False
    if any(a == "--output" or a.startswith("--output=") for a in args):
        return False  # log/diff/show write a file of git's choosing (a ref, a hook)
    if sub in READ_ONLY_ANY:
        return True
    if sub not in READ_ONLY_SHAPED:
        return False
    options = args[: args.index("--")] if "--" in args else args
    positionals = [a for a in options if not a.startswith("-")]
    if sub == "grep":  # -O/--open-files-in-pager runs a program
        return not any(
            a.startswith("--open-files-in-pager") or "O" in _short_cluster(a) for a in options
        )
    if sub == "reflog":
        if not args or args[0] in ("show", "list"):
            return not any(a in ("expire", "delete", "drop", "exists") for a in args[1:])
        return args[0].startswith("-") and not any(
            a in ("expire", "delete", "drop", "exists") for a in args
        )
    if sub == "branch":
        rest = list(args)
        while rest:
            arg = rest.pop(0)
            if arg == "--contains":
                if rest and not rest[0].startswith("-"):
                    rest.pop(0)  # its commit
                continue
            if arg.startswith("--contains="):
                continue
            if arg in BRANCH_LIST_OPTS:
                continue
            letters = _short_cluster(arg)
            if letters and set(letters) <= set("larv"):
                continue
            return False  # a positional (create/rename) or any other option
        return True
    if sub == "remote":
        if positionals and positionals[0] != "show":
            return False
        before = options[: options.index("show")] if "show" in options else options
        return all(a in ("-v", "--verbose") for a in before)
    if sub == "config":
        reads = any(a in CONFIG_READ for a in options) or (
            bool(positionals) and positionals[0] in ("get", "list")
        )
        return reads and not any(a in CONFIG_WRITE for a in options)
    if sub == "fetch":
        for arg in options:
            if arg.startswith(("--update-head-ok", "--upload-pack", "--exec")):
                return False
            if "u" in _short_cluster(arg):
                return False
        # `git fetch REMOTE SRC:DST` writes DST; an `ext::` remote runs a command
        return not any(":" in a for a in positionals[1:]) and not any(
            a.startswith("ext::") for a in positionals
        )
    if sub == "stash":
        return bool(args) and args[0] in ("list", "show")
    if sub == "tag":
        if not args:
            return True
        listing = any(a in ("-l", "--list") or "l" in _short_cluster(a) for a in options)
        writes = any(
            a.partition("=")[0] in TAG_WRITE or set(_short_cluster(a)) & set("dasufmFe")
            for a in options
        )
        return listing and not writes
    return False


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


def _command_words(
    argv: list[str],
    chdirs: list[str],
    env: list[tuple[str, str]],
    keys: list[str] | None = None,
) -> list[str]:
    """`argv` without leading reserved words, `VAR=value` prefixes and wrappers (their directory
    options go to `chdirs`, GIT_DIR/GIT_WORK_TREE assignments to `env`, every assigned name to
    `keys`): the program and its arguments."""
    words = list(argv)
    while words:
        if words[0] in SHELL_KEYWORDS:
            words.pop(0)
        elif _ASSIGNMENT.match(words[0]):
            key, _, value = words.pop(0).partition("=")
            if keys is not None:
                keys.append(key)
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


def _shell_reads_stdin(words: list[str]) -> bool:
    """A shell (without `-c`) taking its program from stdin -- `... | sh`, `bash -s`, `bash`,
    `sh < file`, `bash <<< "..."` -- rather than from a script file operand."""
    for word in words[1:]:
        if word == "--":
            continue
        if word[:1] in ("-", "+") and len(word) > 1:
            if word[0] == "-" and not word.startswith("--") and "s" in word[1:]:
                return True
            continue
        return word.startswith(("<", ">"))  # a redirection, not a script operand
    return True


def _where(cwd: str | None, chdirs) -> str:
    """The directory `chdirs` lead to from `cwd`, in order (`cd -` is unknowable here: stay put)."""
    where = cwd or os.getcwd()
    for directory in chdirs:
        if directory != "-":
            where = os.path.join(where, os.path.expanduser(directory))
    return where


def _strip_substitutions(word: str) -> str:
    """One word without its `$(...)`/backtick bodies (those are parsed as commands of their own)."""
    for body in _substitutions(word):
        word = word.replace(body, "", 1)
    return word


def _commit_targets(argv: list[str], depth: int = 0, cwd: str | None = None) -> list[Target]:
    """The repository each guarded git invocation in one simple command acts on (see the module
    docstring): empty only when the command is proven not to touch history through git."""
    chdirs: list[str] = []
    env: list[tuple[str, str]] = []
    keys: list[str] = []
    words = _command_words(argv, chdirs, env, keys)
    if not words:
        return []
    unknown = [(tuple(chdirs), (), tuple(env))]
    program = os.path.basename(words[0])
    inner = _shell_command_string(words)
    if inner is not None:
        if depth >= MAX_SUBST_DEPTH:
            return unknown
        return [
            ((*chdirs, *inner_dirs), opts, (*env, *inner_env))
            for inner_dirs, opts, inner_env in commit_targets(inner, depth + 1, _where(cwd, chdirs))
        ]
    args = [_strip_substitutions(word) for word in words[1:]]
    if program in SHELLS:  # `| sh`, `bash <<< ...`: the program arrives on stdin
        reads_stdin = _shell_reads_stdin(words)
        return unknown if reads_stdin or any(_mentions_git(a) for a in args) else []
    if program.startswith("git-"):
        return unknown  # `git-commit`, `git-merge`: a git subcommand run directly
    if program not in GIT_PROGRAMS:
        if any(_mentions_git(a) for a in args):
            return unknown  # `xargs git`, `find -exec git`, `eval git ...`, `python -c '...git'`
        if program in FANOUT and any(os.path.basename(a) in SHELLS for a in args):
            return unknown  # `xargs sh`, `find -exec bash -c ...`
        return []
    unsafe = any(
        key not in SAFE_GIT_ENV and not key.startswith("LC_") for key in keys
    )  # GIT_EXTERNAL_DIFF=..., GIT_CONFIG_*=..., PAGER=... git log
    rest = words[1:]
    repo_opts: list[str] = []
    while rest:
        word = rest.pop(0)
        if word in GIT_VALUE_OPTS:
            value = rest.pop(0) if rest else ""
            if word in GIT_REPO_OPTS:
                repo_opts += [word, value]
            if word in ("-c", "--config-env"):
                unsafe = True  # core.pager, diff.external, alias.*: configuration runs commands
            continue
        if word.startswith(("--config-env=", "--exec-path=")) or (
            word.startswith("-c") and len(word) > 2
        ):
            unsafe = True
            continue
        if word.startswith(("--git-dir=", "--work-tree=")):
            repo_opts.append(word)
            continue
        if word.startswith("-C") and len(word) > 2:
            repo_opts += ["-C", word[2:]]
            continue
        if word.startswith("-"):
            continue  # --no-pager, --bare, ...
        target = [(tuple(chdirs), tuple(repo_opts), tuple(env))]
        if unsafe or not _read_only(word, rest):
            return target
        return []
    return []  # `git`, `git --version`: no subcommand


def _mentions_git(text: str) -> bool:
    """A `git` word anywhere in `text` (see `_GIT_WORD`)."""
    return bool(_GIT_WORD.search(text))


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


# ---- The raw-text proof (Codex 4205654928). shlex cooks quotes away and does not know ANSI-C
# quoting (`g$'it'` -> `g$it`), so a program or git subcommand ASSEMBLED by the shell from quote
# fragments, escapes, braces or expansions looks like some other word to the parser. A simple
# command is proven only when the words in those two positions are plain literals in the RAW text.
_PLAIN_WORD = re.compile(r"^[A-Za-z0-9_./+-]+$")
_PLAIN_PROGRAMS = frozenset({"[", "[[", ":"})  # test builtins and `:`: run nothing
_EVAL_LIKE = frozenset({"eval", "source", "."})  # run their (assembled) arguments as code
_ANSI_C_QUOTE = re.compile(r"\$['\"]")
_SPELLED_GIT_COMMIT = re.compile(r"git.*commit")


def _plain(word: str | None) -> bool:
    return bool(word and _PLAIN_WORD.match(word))


def _raw_span(text: str, index: int) -> int:
    """The index just past the shell construct that starts at `index` (a quoted string, an
    escape, `$(...)`, backticks, `${...}`, or a single character). ValueError when unclosed."""
    end = len(text)
    char = text[index]
    if char == "\\":
        return min(index + 2, end)
    if char == "'":
        close = text.find("'", index + 1)
        if close == -1:
            raise ValueError("unclosed '")
        return close + 1
    if text.startswith("$'", index) or char == "`":
        quote, pos = text[index + 1] if char == "$" else "`", index + (2 if char == "$" else 1)
        while pos < end:
            if text[pos] == "\\":
                pos += 2
            elif text[pos] == quote:
                return pos + 1
            else:
                pos += 1
        raise ValueError(f"unclosed {quote}")
    if char == '"' or text.startswith('$"', index):
        pos = index + (2 if char == "$" else 1)
        while pos < end:
            if text[pos] == "\\":
                pos += 2
            elif text[pos] == '"':
                return pos + 1
            elif text.startswith("$(", pos) or text[pos] == "`":
                pos = _raw_span(text, pos)
            else:
                pos += 1
        raise ValueError('unclosed "')
    if text.startswith("$(", index):
        depth, pos = 1, index + 2
        while pos < end:
            if text[pos] in "\\'\"`" or text.startswith(("$(", "$'", '$"'), pos):
                pos = _raw_span(text, pos)
                continue
            depth += {"(": 1, ")": -1}.get(text[pos], 0)
            pos += 1
            if not depth:
                return pos
        raise ValueError("unclosed $(")
    if text.startswith("${", index):
        close = text.find("}", index)
        if close == -1:
            raise ValueError("unclosed ${")
        return close + 1
    return index + 1


def _raw_segments(command: str) -> list[list[str]]:
    """Each simple command's words exactly as written (quotes, escapes and expansions kept),
    split like `_segments`; a `#` opening a word comments out the rest of the line."""
    segments: list[list[str]] = [[]]
    word: list[str] = []
    index = 0
    while index < len(command):
        char = command[index]
        if char in " \t\r\n;&|()":
            if word:
                segments[-1].append("".join(word))
                word = []
            if char not in " \t":
                segments.append([])
            index += 1
        elif char == "#" and not word:
            newline = command.find("\n", index)
            index = len(command) if newline == -1 else newline
        else:
            end = _raw_span(command, index)
            word.append(command[index:end])
            index = end
    if word:
        segments[-1].append("".join(word))
    return [seg for seg in segments if seg]


def _raw_substitutions(word: str) -> list[str]:
    """The bodies of the `$(...)` and backtick substitutions in one raw word (live inside double
    quotes too, dead inside single quotes)."""
    bodies: list[str] = []
    index, in_double = 0, False
    while index < len(word):
        char = word[index]
        if char == '"':
            in_double = not in_double
            index += 1
        elif char == "\\":
            index += 2
        elif not in_double and (char == "'" or word.startswith("$'", index)):
            index = _raw_span(word, index)
        elif word.startswith("$(", index) or char == "`":
            end = _raw_span(word, index)
            bodies.append(word[index + (2 if char == "$" else 1) : end - 1])
            index = end
        else:
            index += 1
    return bodies


def _cook(word: str) -> str:
    """One raw word with its quoting removed, as the shell would pass it (best effort)."""
    try:
        return " ".join(shlex.split(word))
    except ValueError:
        return word


def _raw_segment_proven(seg: list[str], depth: int) -> bool:
    """Are the program and (for git) the subcommand of this raw simple command plain literals?"""
    try:
        words = _command_words(seg, [], [])
    except ValueError:
        return False
    if not words:
        return True
    program = words[0]
    if not (_plain(program) or program in _PLAIN_PROGRAMS):
        return False
    name = os.path.basename(program)
    if name in ("git", "git.exe"):
        sub = _git_subcommand(words)
        return sub is None or _plain(sub)
    if name in _EVAL_LIKE:
        return all(_plain(word) for word in words[1:])
    inner = _shell_command_string([_cook(word) for word in words])
    if inner is not None:
        return depth < MAX_SUBST_DEPTH and not _raw_unproven(inner, depth + 1)
    return True


def _raw_unproven(command: str, depth: int = 0) -> bool:
    """Could the shell assemble a program or git subcommand in `command` that the parser cannot
    see? True when any simple command (or substitution body, or `sh -c` string) has a program or
    git subcommand that is not a plain literal word, when `eval`/`source` gets anything but
    plain words, or when `$'...'`/`$"..."` quoting meets the letters of `git` then `commit`."""
    if _ANSI_C_QUOTE.search(command) and _SPELLED_GIT_COMMIT.search(
        re.sub(r"[^a-z]", "", command.lower())
    ):
        return True
    try:
        segments = _raw_segments(command)
        for seg in segments:
            for body in (body for word in seg for body in _raw_substitutions(word)):
                if depth >= MAX_SUBST_DEPTH or _raw_unproven(body, depth + 1):
                    return True
            while seg and seg[0] in SHELL_KEYWORDS:
                seg = seg[1:]
            if seg and not _raw_segment_proven(seg, depth):
                return True
    except ValueError:
        return True
    return False


def commit_targets(command: str, depth: int = 0, cwd: str | None = None) -> list[Target]:
    """Every guarded git invocation in `command` (see the module docstring) with the repository
    it acts on. `cd DIR` before it (`cd ../wt && git merge x`) counts too. Deny by default: any
    git invocation not proven read-only, and anything that runs git or a shell indirectly, is
    one; an assembled program or subcommand is one with an unknown repository."""
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
        # over the joined words: shlex splits an unquoted `cd \`git commit\`` across words
        for body in _substitutions(" ".join(seg)):
            if depth >= MAX_SUBST_DEPTH:
                targets += [((), (), ())] if _mentions_git(body) else []
                continue
            try:  # a substitution runs where the command runs: same `cd`s
                targets += commit_targets(body, depth + 1, where)
            except ValueError:
                targets += [((), (), ())] if _mentions_git(body) else []
        prefix = cds[:-1] if is_cd else cds
        found += [((*prefix, *dirs), opts, env) for dirs, opts, env in targets]
    if depth == 0 and (
        _raw_unproven(command)
        or (_mentions_git(command) and _ENV_TAINT.search(re.sub(r"[\"'\\]", "", command)))
    ):
        # An assembled program/subcommand, or git run under an exported GIT_*/PAGER/EDITOR
        # setting: unknown repository, so every `cd` prefix.
        found += [(tuple(cds[:count]), (), ()) for count in range(len(cds) + 1)]
    return found


def is_git_commit(command: str, depth: int = 0, cwd: str | None = None) -> bool:
    """True when `command` holds a guarded git invocation (the name predates deny-by-default:
    any history-changing, unknown or unprovable git use counts, not only `git commit`), however
    git is spelled (`git`, `/usr/bin/git`, `./git`), whatever wrapper (with its options) runs it,
    and whatever runs it indirectly (`xargs git`, `... | sh`)."""
    try:
        return bool(commit_targets(command, depth, cwd))
    except ValueError:  # unparseable to shlex: fail closed on a git word or the raw proof
        return _mentions_git(command) or _raw_unproven(command)


def target_branch(target: Target, cwd: str | None) -> str:
    """The branch `git <repo options> ...` acts on, asked of git itself."""
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


def login_key(login: object) -> str:
    """A GitHub login as compared (leases.login_key): logins are case-insensitive, so every
    comparison and cache key goes through here; the original spelling is kept for API calls."""
    return str(login or "").strip().casefold()


def trusted_bot(login: str, configured: str | None = None) -> bool:
    """`github-actions[bot]`, or the Publisher App's bot: the logins in FORGE_LEASE_BOT_LOGINS
    when set, else a `<slug>[bot]` whose slug carries PUBLISHER_APP_SLUG_HINT. No other bot."""
    login = login_key(login)
    if login == login_key(GITHUB_ACTIONS_BOT):
        return True
    if configured is None:
        configured = os.environ.get(LEASE_BOT_LOGINS_ENV, "")
    named = {login_key(part) for part in configured.split(",") if part.strip()}
    if named:
        return login in named
    return login.endswith("[bot]") and PUBLISHER_APP_SLUG_HINT in login[: -len("[bot]")]


def trusted_marker_author(comment: dict, permission, configured: str | None = None) -> bool:
    """May this comment's author post lease markers (leases.trusted_marker_author)?"""
    user = comment.get("user") or {}
    login = str(user.get("login") or "")
    if not login:
        return False
    if user.get("type") == "Bot" or login_key(login).endswith("[bot]"):
        return user.get("type") == "Bot" and trusted_bot(login, configured)
    if not _LOGIN.fullmatch(login):
        return False
    granted = permission(login)
    return granted is not None and login_key(granted) in WRITE_PERMISSIONS


_PERMISSIONS: dict[str, str | None] = {}  # one read per login (login_key) per hook run


def repo_permission(login: str) -> str | None:
    """The login's repository permission (role_name, else permission); None when GitHub says the
    account has none (404). Any other failure raises LeaseUnavailable: an unreadable permission
    must not silently drop that author's markers and read as "no lease"."""
    if login_key(login) in _PERMISSIONS:
        return _PERMISSIONS[login_key(login)]
    granted: str | None = None
    try:
        proc = subprocess.run(
            ["gh", "api", f"repos/{PROJECT}/collaborators/{login}/permission"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise LeaseUnavailable(f"gh unavailable: {exc}") from exc
    if proc.returncode != 0:
        if "HTTP 404" not in (proc.stderr or ""):
            raise LeaseUnavailable((proc.stderr or "").strip() or "permission read failed")
        data = None
    else:
        try:
            data = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise LeaseUnavailable("unparseable permission response") from exc
    if isinstance(data, dict):
        for key in ("role_name", "permission"):
            value = login_key(data.get(key))
            if value in WRITE_PERMISSIONS:
                granted = value
                break
        else:
            granted = str(data.get("permission") or "") or None
    _PERMISSIONS[login_key(login)] = granted
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
        if not isinstance(c, dict):
            continue
        body = c.get("body") or ""
        if not (CLAIM.search(body) or RELEASE.search(body)):
            continue  # chatter: no permission read needed
        if not trusted_marker_author(c, permission):
            continue  # markers count only from writers and the trusted bots
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
    """The live lease of `issue`: None when there is none, UNAVAILABLE when GitHub could not be
    read (callers must treat that as held, never as free)."""
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
        return UNAVAILABLE
    if not isinstance(pages, list):
        return UNAVAILABLE
    comments = [c for page in pages if isinstance(page, list) for c in page if isinstance(c, dict)]
    try:
        return lease_from_comments(comments, repo_permission)
    except LeaseUnavailable:
        return UNAVAILABLE


def open_pr_for(branch: str) -> bool | None:
    """An open PR from this branch is the durable claim once the lease is released. None when
    the PR list could not be read."""
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
        return None


def decide(
    payload: dict, branch: str | list[str], lease_lookup=lease_for, pr_lookup=open_pr_for
) -> tuple[int, str]:
    """Block (2, why) a guarded git command (see the module docstring) on an issue branch this
    session does not lease. `branch` is the branch it acts on, or every candidate
    (`commit_branches`); any unleased one blocks."""
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
    if lease_unavailable(lease):
        return 2, (
            f"Forge guard: the lease of issue #{issue} could not be read from GitHub "
            "(authentication, network or API failure). Lease inventory unavailable -- do not "
            "commit issue work until it can be checked; retry once `gh` works."
        )
    has_pr = pr_lookup(branch) if lease is None else False
    if lease is None and has_pr:
        return 0, ""  # follow-up commits to an open PR need no lease
    if lease is None and has_pr is None:
        return 2, (
            f"Forge guard: branch '{branch}' targets issue #{issue}, it has no live lease and "
            "its open PRs could not be read from GitHub (no PR could be confirmed). Claim it: "
            f"sdlcctl claim --project {PROJECT} --issue {issue} --agent claude-code "
            f"--session {session or '<session_id>'} --branch {branch}"
        )
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
