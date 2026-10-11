#!/usr/bin/env python3
"""Claude Code PreToolUse hook: guard git on an issue branch without this session's lease.

Installed by `sdlcctl onboard`. Reads the hook payload on stdin; exit 2 blocks the tool call and
shows the message to the agent. Only git that acts on an issue branch (`*/issue-N*`) this session
does not lease -- the checked-out one, or one the command names as a destination -- is blocked,
plus git whose destinations cannot be known before it runs; everything else passes. A payload
that cannot be read (not JSON, truncated, not an object, no tool name, a Bash call without a
string command) blocks too: only a payload shown to be another tool is let through unread.

Posture: DENY BY DEFAULT for git. On an issue branch, EVERY git invocation needs the lease except
a short allowlist of read-only subcommands proven by plain literal tokens (`_read_only`):
status, log, diff, show, blame, grep (no `-O` pager), ls-files, ls-tree, rev-parse, describe,
shortlog, reflog (show/list), branch (only --list/-l/-a/-r/-v/--show-current/--contains, no
positional arguments), remote (-v/show), config (--get*/--list/-l, no write mode), fetch (no
--update-head-ok/-u, --upload-pack or refspec, and no configured `remote.*.fetch` refspec that
writes a local branch -- see `_configured_fetch_refs`), stash list/show, tag (-l/--list, no
create or delete flag), help, version, cat-file, for-each-ref, name-rev, merge-base, check-ignore,
check-attr. Everything else -- commit, merge, cherry-pick, revert, rebase, am, apply, pull, reset,
checkout, switch, stash push/pop/apply, notes, tag creation, update-ref, commit-tree,
fast-import, filter-branch, replace, worktree, push, `git-<name>` programs, aliases and unknown
subcommands -- goes to the lease check. So does an allowlisted subcommand whose meaning git or
the shell can change out of sight: `-c`/`--config-env`/`--exec-path=` (pager, diff and fetch
commands are configuration), an environment assignment other than GIT_DIR/GIT_WORK_TREE/locale
on it (or a GIT_*/PAGER/EDITOR/SSH_ASKPASS*/LD_* assignment anywhere in the command, or any
mention of PATH there: git runs `ssh` from it), `--output`, or an argument holding `$`, a
backtick or a brace expansion. A relative PATH entry (`.`, an empty entry) guards every
allowlisted subcommand: what it runs depends on the directory. So does a WRITABLE absolute
entry searched before (or at) a program the command may launch -- git itself, the pager, `man`,
and for fetch/remote `ssh` and the helpers git runs by name, with git's exec path searched
first (Codex 4240952107, see `_writable_path_reason`): a command can drop its own `ssh` there.

An allowlisted subcommand is read-only only while git runs nothing configured OUTSIDE the
command (Codex 4239667542): `diff.external`, a textconv / diff / filter driver, a non-trivial
`core.pager`/`pager.*`, a `core.fsmonitor` hook, a signature program for a command that verifies
signatures, an initialized submodule, a hook git runs on its own (reference-transaction,
post-index-change, pre-auto-gc), and for fetch/remote the ssh/askpass/credential/proxy helpers
and remote helpers named by a remote URL -- in any configuration scope git reads (includes
resolved by git itself) or in GIT_EXTERNAL_DIFF/GIT_EXEC_PATH/GIT_PAGER/PAGER/GIT_SSH*/
*ASKPASS/GIT_PROXY_COMMAND -- see `_helper_reason`. Then it goes to the lease check like any
other git, and so does one whose configuration cannot be read, or one passing `--ext-diff`,
`--textconv` or `--filters`.

Every lookup is bounded (GH_TIMEOUT for GitHub, GIT_TIMEOUT for local git): a lookup that times
out is unavailable and refuses, never hangs the session (Codex 4239667544).

Indirect execution is guarded the same way: any other program that receives `git` as a word
(`xargs git`, `xargs -a f git`, `find -exec git`, `parallel git`, `eval git ...`,
`python -c "...'git'..."`), `xargs`/`parallel`/`find` handing work to a shell, and a shell reading
its program from stdin (`... | sh`, `bash <<< ...`, `bash -s`). `sh -c STRING` is parsed
recursively, as are `$(...)`/backtick bodies. A shell script FILE is read and parsed the same
way (Codex 4240526381): `bash x.sh`, `sh -e x.sh`, `bash < x.sh`, `source x.sh`, `. x.sh`, and
`./x.sh` run directly (a `.sh`/`.bash` path, or one whose `#!` line names a shell), and a bare
command name PATH resolves to such a script (Codex 4240628980; one that names git nowhere in
it or in what it runs is not read -- see `_git_free`). Its git is
guarded exactly like inline git (`-C`/`cd` selectors, named refs, the raw-text proof, the
PATH/GIT_* taint), scripts it runs are read in turn up to MAX_SUBST_DEPTH, and a script that is
missing, not a regular file, binary, over MAX_SCRIPT_BYTES, unparseable or nested deeper is
UNRESOLVED_REPO: refused on every branch, whatever lease the session holds.

A raw-text proof (`_raw_unproven`) backs the parser: the shell can ASSEMBLE a program or
subcommand (`g$'it' com$'mit'`, `"g"it`, `\\git`, `co{m,}mit`, `$(echo git)`) the parser cannot
see, so every program and git subcommand must be a plain literal word in the raw text
(`[A-Za-z0-9_./+-]+`), `eval`/`source` may take only plain words, and `$'...'`/`$"..."` quoting
next to the letters of `git` then `commit` is unproven outright. Unparseable text that mentions
git is guarded too. Over-blocking (`echo "git merge"`) costs nothing on a leased branch or off
issue branches; a missed history change costs the lease.

A branch change earlier in the SAME command re-targets the git after it (`git switch X && git
commit` is checked against X, see UNKNOWN_BRANCH); one whose destination cannot be known before
the command runs (`git checkout "$B"`, an alias, bisect, `source`, `cd "$D"`, ...) blocks a later
guarded git outright -- run the two as separate commands. A plain switch to an existing branch
(`git switch main`) rewrites no history and needs no lease.

A guarded command is checked against every ref it NAMES as a destination too, not only the
checked-out branch: `git push origin HEAD:forge/issue-7`, `git push origin :forge/issue-7`,
`git branch -f forge/issue-7 HEAD` or `git update-ref refs/heads/forge/issue-7 X` run from main
need issue 7's lease (see `_named_refs`). One whose destinations cannot be known before it runs
(`git push --all`, a glob refspec, a push driven by `remote.*.push` or `push.default`
upstream/matching, an alias to a shell command, `rebase --update-refs`, ...) is blocked on any
branch: name the destinations explicitly.

The repository a guarded command acts on must be resolvable before it runs (Codex 4239868022):
a selector the shell rewrites (`git -C "$DIR"`, `--git-dir=$X`, `--work-tree`, GIT_DIR=/
GIT_WORK_TREE= set to an expansion, `cd $DIR`, `$(...)`, backticks, a glob, an ambiguous `~`)
is refused outright, and a branch lookup that fails for any reason but "no repository there"
(`git clone`/`git init` from a plain directory) is unknown and refuses, never "no branch". So is
git run through an ASSEMBLED program word (`G=git; "$G" -C DIR commit`, `$(which git)`,
`${GIT:-git}`, `\\git`, `"git"`, `$HOME/bin/git`) or handed its command line by another program
(`xargs`/`find -exec`/`parallel`/`eval`, a shell reading stdin, an interpreter whose code also
selects a repository) (Codex 4240170620): its `-C`/`--git-dir`/GIT_DIR selectors are out of
sight, so it is never mapped to the current checkout. Plain `git`, a literal path to git
(`/usr/bin/git`) and the wrappers parsed above (`env`, `command`, `exec`, `nice`, `time`,
`sudo`, ...) keep their selectors and are checked against the repository they name.

SCOPE -- a guardrail, not a sandbox. The guard sees the git invocations a command line shows
and the git inside the shell script files it runs. A PreToolUse hook cannot see what any other
program does once it runs: a build tool or interpreter that runs git internally (`make`, `npm
run`, `python x.py`, a compiled binary, a non-shell `#!` script) and direct writes into `.git/`
are out of scope. The real enforcement is server side: the issue lease, branch protection and
the review gates; this hook only stops an agent from walking past them by accident.
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
# Codex 4239667544: the hook runs synchronously before every Bash call, so no lookup may hang
# it. A GitHub read past GH_TIMEOUT is UNAVAILABLE (refuses); a local git call past GIT_TIMEOUT
# is unknown (guarded). The SessionStart hook allows 60s for its own reads.
GH_TIMEOUT = 30
GIT_TIMEOUT = 15
ISSUE_BRANCH = re.compile(r"(?:^|/)issue-(\d+)(?:$|[^0-9])")
# A marker is the WHOLE first line of a comment, as leases.py writes and reads it; a comment with
# marker syntax elsewhere, or more than one marker, carries none (same rule as leases._marker).
MARKER_LINE = re.compile(r"^<!--\s*(forge-claim|forge-release|forge-cleanup)\s+([^>]*?)\s*-->$")
ANY_MARKER = re.compile(r"<!--\s*forge-(?:claim|release|cleanup)\b")


def comment_marker(body: str) -> tuple[str, dict] | None:
    """(kind, fields) of the marker a comment carries, or None."""
    body = body or ""
    if len(ANY_MARKER.findall(body)) != 1:
        return None
    lines = body.splitlines()
    m = MARKER_LINE.match(lines[0].strip()) if lines else None
    if m is None:
        return None
    return m.group(1), dict(re.findall(r"(\w+)=(\S+)", m.group(2)))


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
# Programs that only print, search or test their arguments -- they cannot run a `git` word they
# are handed (`grep -rn git src`, `echo "git merge"`, `which git`). Any OTHER program receiving
# `git` (`python -c`, `node -e`, `perl -e`, `gh`, ...) may run it, and when its text also selects
# a repository (`-C`, `--git-dir`, `--work-tree`, GIT_DIR/GIT_WORK_TREE, `cwd=`, `chdir`, `cd`)
# the guard cannot tell which one: UNRESOLVED_REPO (Codex 4240170620), never the current checkout.
INERT_PROGRAMS = frozenset(
    {"echo", "printf", "grep", "egrep", "fgrep", "rg", "cat", "head", "tail", "wc", "ls"}
    | {"which", "type", "whereis", "test", "[", "[[", ":", "true", "false"}
)
_REPO_SELECTOR_TEXT = re.compile(
    r"(?<![\w-])(?:-C|--git-dir|--work-tree|GIT_DIR|GIT_WORK_TREE|cwd|chdir|cd|pushd)(?![\w-])"
)
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
#
# Codex 4240421142: git runs the default SSH transport, `ssh`, from PATH -- and the shell finds
# `git` itself there -- so a command that changes PATH anywhere (`export PATH=/tmp/bin:$PATH;
# git fetch origin`, `PATH+=...`, `read PATH`, `for PATH in ...`, `declare -n p=PATH`) can run a
# program of its own choosing through an allowlisted git command. PATH is always exported, so ANY
# mention of the variable taints (`_PATH_TAINT`), not only an assignment spelled `PATH=`; so do
# the loader variables (LD_*/DYLD_*), ssh's own askpass (SSH_ASKPASS*) and `+=` appends.
_ENV_TAINT = re.compile(
    r"(?<![A-Za-z0-9_])(?:GIT_(?!DIR=|WORK_TREE=)[A-Z0-9_]+|PAGER|EDITOR|VISUAL|LESS[A-Z]*"
    r"|SSH_ASKPASS[A-Z_]*|LD_[A-Z0-9_]+|DYLD_[A-Z0-9_]+)\+?="
)
# zsh ties the array `path` to PATH (`path=(/tmp/bin $path)`, `path+=...`, `path[1]=...`), and
# `hash -p FILE NAME` makes the shell run FILE for NAME (`git`) without touching PATH at all.
_PATH_TAINT = re.compile(
    r"(?<![A-Za-z0-9_])PATH(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_$-])path(?:\[[^\]]*\])?\+?="
    r"|(?<![A-Za-z0-9_-])hash\s+(?:-[A-Za-z]*\s+)*-[A-Za-z]*p"
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
# `github-actions[bot]`, or the Forge Publisher App's bot. A 404 = no access (untrusted); any
# other unreadable permission refuses the lease read (LeaseUnavailable), never drops the marker.
WRITE_PERMISSIONS = frozenset({"admin", "maintain", "write"})  # leases.WRITE_PERMISSIONS
GITHUB_ACTIONS_BOT = "github-actions[bot]"  # leases.GITHUB_ACTIONS_BOT
PUBLISHER_APP_SLUG_HINT = "agentic-sdlc"  # leases.PUBLISHER_APP_SLUG_HINT
LEASE_BOT_LOGINS_ENV = "FORGE_LEASE_BOT_LOGINS"  # leases.LEASE_BOT_LOGINS_ENV
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


def _segments(command: str) -> list[list[str]]:
    """Shell words of each simple command, split on `;`, `&&`, `||`, `|`, `&`, parentheses."""
    return [seg for _, seg in _segments_with_ops(command)]


def _segments_with_ops(command: str) -> list[tuple[tuple[str, ...], list[str]]]:
    """Each simple command with the operators that precede it (`()` for the first one)."""
    lexer = shlex.shlex(command.replace("\n", ";"), posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    lexer.commenters = ""
    segments: list[tuple[list[str], list[str]]] = [([], [])]
    for token in lexer:
        if token in OPERATORS:
            if segments[-1][1]:
                segments.append(([], []))
            segments[-1][0].append(token)
        else:
            segments[-1][1].append(token)
    return [(tuple(ops), seg) for ops, seg in segments if seg]


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
# Options that make an allowlisted subcommand run configured programs (Codex 4239667542).
HELPER_OPTS = frozenset({"--ext-diff", "--textconv", "--filters"})


# ---- Configured helpers (Codex 4239667542). An allowlisted subcommand is read-only for the
# branch's history only while git runs nothing configured outside the command line: a
# `diff.external` or textconv program could commit or push on its own. `_helper_reason` reads the
# configuration git itself would use (`git config --list --includes`, every scope, includes
# resolved by git), the hook's environment and the hooks directory, and names the first thing
# that can run a program; then the command needs the lease like any other git. Unreadable
# configuration is a reason too (fail closed). Plain `less`/`more`/`cat` pagers, boolean
# `pager.<cmd>`/`core.fsmonitor`, git-lfs's canonical filters and the built-in `cache`/`store`
# credential helpers run nothing that writes history and do not count.
HELPER_KEYS = re.compile(
    r"^(?:diff\.external|diff\..+\.(?:textconv|command)|filter\..+\.(?:clean|smudge|process)"
    r"|submodule\..+\..+)$"
)
# `git help` launches a manual viewer or browser, which these name.
HELP_KEYS = re.compile(
    r"^(?:man\..+\.(?:cmd|path)|man\.viewer|browser\..+\.(?:cmd|path)|web\.browser"
    r"|help\.browser|help\.format)$"
)
PAGER_KEYS = re.compile(r"^(?:core\.pager|pager\..+)$")
SIGNATURE_KEYS = re.compile(r"^gpg\.(?:.+\.)?program$")
NETWORK_KEYS = re.compile(
    r"^(?:core\.(?:sshcommand|askpass|gitproxy|alternaterefscommand)|credential\.(?:.+\.)?helper"
    r"|remote\..+\.(?:uploadpack|vcs))$"
)
URL_VALUE_KEYS = re.compile(r"^remote\..+\.(?:url|pushurl)$")
URL_BASE_KEYS = re.compile(r"^url\.(.+)\.(?:insteadof|pushinsteadof)$")
SAFE_PAGER = re.compile(r"^(?:(?:/usr)?/bin/)?(?:less|more|cat)(?:\s+-[A-Za-z0-9=-]*)*$")
GIT_BOOLEANS = frozenset({"", "true", "false", "yes", "no", "on", "off", "1", "0"})
SAFE_CREDENTIAL = re.compile(r"^(?:cache|store)(?:\s+--[a-z-]+(?:=\S+)?)*$")
# What `git lfs install` (also with --skip-smudge) writes: exactly these, nothing appended.
LFS_FILTERS = {
    "filter.lfs.clean": re.compile(r"^git-lfs clean -- %f$"),
    "filter.lfs.smudge": re.compile(r"^git-lfs smudge(?: --skip)? -- %f$"),
    "filter.lfs.process": re.compile(r"^git-lfs filter-process(?: --skip)?$"),
}
SAFE_URL_SCHEMES = frozenset({"http", "https", "ssh", "git", "file", "git+ssh", "ssh+git"})
HELPER_ENV = ("GIT_EXTERNAL_DIFF", "GIT_EXEC_PATH")
PAGER_ENV = ("GIT_PAGER", "PAGER")
NETWORK_ENV = ("GIT_SSH", "GIT_SSH_COMMAND", "GIT_ASKPASS", "SSH_ASKPASS", "GIT_PROXY_COMMAND")
# Hooks git runs by itself during an allowlisted command: fetch updates remote-tracking refs and
# may auto-gc; status/diff may rewrite the index.
READ_ONLY_HOOKS = ("reference-transaction", "post-index-change", "pre-auto-gc")


def _contacts_remote(sub: str, args: list[str]) -> bool:
    """`git fetch`, and `git remote show` without `-n`: the only allowlisted forms that reach a
    remote, and so run the transport's helpers (ssh, askpass, credential, remote helpers)."""
    return sub == "fetch" or (sub == "remote" and "show" in args and "-n" not in args)


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
    if any(a in HELPER_OPTS for a in args):
        return False  # Codex 4239667542: asks git to run configured diff/textconv/filter programs
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
            if arg.startswith(("--update-head-ok", "--upload-pack", "--exec", "--refmap")):
                return False  # --refmap maps fetched refs onto local ones (refs/heads/* too)
            if arg == "--stdin":
                return False  # refspecs from stdin
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


# ---- Shell script files (Codex 4240526381). `bash /tmp/do-commit.sh`, `sh -e x.sh`, `bash < x.sh`,
# `source x.sh`, `. x.sh` and `./x.sh` (a path ending in .sh/.bash, or whose `#!` line names a
# shell) run git the command line does not show. The script FILE is read (a regular file of at
# most MAX_SCRIPT_BYTES, UTF-8, no NUL byte; a relative path from the directory the shell runs
# in, and for a bare name every PATH entry too) and its text is analysed like a command string,
# so the git inside is guarded exactly like inline git, `-C`/`cd` selectors included; a script
# it runs is read the same way, up to MAX_SUBST_DEPTH. A script that cannot be read, is too
# large or binary, cannot be parsed, or is nested deeper has an unresolved repository and is
# refused on every branch. This is a bound, not a sandbox: a build tool or interpreter that runs
# git internally (`make`, `npm run`, `python x.py`) is out of the guard's sight.
MAX_SCRIPT_BYTES = 256 * 1024
SCRIPT_SUFFIXES = (".sh", ".bash")
SOURCE_BUILTINS = frozenset({"source", "."})


def _shell_script_operand(words: list[str]) -> tuple[str, bool] | None:
    """The script FILE a shell (without `-c`) runs, as (word, may be found on PATH): `bash x.sh`,
    `sh -e x.sh`, `bash -o pipefail x.sh`, `bash < x.sh` (a redirection: never on PATH). None
    when the program arrives on stdin (`bash`, `bash -s`, `... | sh`, `bash <<< ...`)."""
    rest = list(words[1:])
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
            if word[0] == "-" and "s" in letters:
                return None
            for _ in range(sum(letter in SHELL_VALUE_OPTS for letter in letters)):
                if rest:
                    rest.pop(0)
            continue
        break
    if not rest:
        return None
    word = rest[0]
    if word == "<":
        return (rest[1], False) if len(rest) > 1 else None
    if word.startswith("<") and not word.startswith("<<"):
        return word[1:], False
    if word.startswith(("<", ">")):
        return None
    return word, True


def _shell_shebang(path: str) -> bool:
    """Does the regular file at `path` start with a `#!` line naming a shell (`#!/bin/sh`,
    `#!/usr/bin/env bash`, `#!/usr/bin/env -S bash -e`)? False when it cannot be read."""
    try:
        if not os.path.isfile(path):
            return False
        with open(path, "rb") as handle:
            head = handle.read(256)
    except OSError:
        return False
    if not head.startswith(b"#!"):
        return False
    words = head[2:].split(b"\n", 1)[0].decode("utf-8", "replace").split()
    if words and os.path.basename(words[0]) == "env":
        words = [w for w in words[1:] if not w.startswith("-") and "=" not in w]
    return bool(words) and os.path.basename(words[0]) in SHELLS


# ---- Bare command names (Codex 4240628980). `deploy` with no `/` runs whatever the shell finds
# first on PATH, and an extensionless file there starting `#!/bin/sh` is a shell script like any
# other. Every bare program word that is a plain literal name, not a shell builtin or keyword,
# not git and not a `git-*` subcommand, is therefore resolved the way the shell resolves it --
# the first regular, executable file named so in a PATH entry (an empty entry is the current
# directory) -- and when that file's `#!` line names a shell it is read and analysed like
# `./deploy`. A binary or a non-shell `#!` interpreter is out of scope (see SCOPE). PATH is the
# hook's own environment, the one the Bash tool's shell inherits, widened by every PATH the
# command (or a script it reads) assigns: `PATH=DIR:$PATH`, `export PATH=...` with a value
# built only from literal text, `$PATH`, `$HOME`, `$PWD` and a leading `~` is resolved in each
# of those PATHs too (all of them: which one is in effect where is not tracked). Any other
# change of PATH -- `read PATH`, `PATH=$(...)`, `for PATH in`, zsh's `path[1]=`, `hash -p` --
# leaves the search path unknown, and then every bare program is UNRESOLVED_REPO: refused on
# every branch (`PATH+=...` is already unproven to the raw-text proof). Resolutions are cached
# for one check.
SHELL_BUILTINS = frozenset(
    {"alias", "bg", "bind", "break", "builtin", "caller", "cd", "command", "compgen"}
    | {"complete", "compopt", "continue", "declare", "dirs", "disown", "echo", "enable", "eval"}
    | {"exec", "exit", "export", "false", "fc", "fg", "getopts", "hash", "help", "history"}
    | {"jobs", "kill", "let", "local", "logout", "mapfile", "popd", "printf", "pushd", "pwd"}
    | {"read", "readarray", "readonly", "return", "set", "shift", "shopt", "source", "suspend"}
    | {"test", "times", "trap", "true", "type", "typeset", "ulimit", "umask", "unalias", "unset"}
    | {"wait", ".", ":", "[", "[[", "]]", "time", "coproc", "select", "case", "for", "in"}
)
_PLAIN_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*")
# A PATH value the guard can compute: literal text and these expansions only.
_PATH_VALUE_VARS = re.compile(r"\$(?:\{(PATH|HOME|PWD)\}|(PATH|HOME|PWD)(?![A-Za-z0-9_]))")
# A mention of PATH that is NOT a read of it (`$PATH`, `${PATH}`, `${PATH:-x}` only read).
_PATH_READ = re.compile(r"\$\{?#?$")
# The only things that can set the shell's PATH besides an assignment word: these builtins and
# loops (`read PATH`, `printf -v PATH`, `for PATH in ...`, `eval PATH=...`, ...) and a `${PATH:=x}`
# expansion. Any other program receiving the word (`echo`, `grep`, `printenv PATH`) cannot.
PATH_WRITERS = frozenset(
    {"read", "readarray", "mapfile", "printf", "getopts", "declare", "typeset", "local"}
    | {"export", "readonly", "unset", "eval", "source", ".", "let", "for", "select", "builtin"}
    | {"command", "exec", "vared", "zle", "typeset", "integer", "float"}
)
_PATH_SEARCH: list = []  # [None | list of PATH lists] while a check runs; [] outside one
_EXEC_CACHE: dict = {}  # (name, where, PATHs) -> the shell scripts it resolves to


def _path_value(value: str, where: str) -> list[str] | None:
    """The PATH entries `value` (a cooked `PATH=` value) expands to, or None when it holds any
    expansion other than `$PATH`/`$HOME`/`$PWD` (or a `~` that is not leading an entry)."""
    session = os.environ.get("PATH", "")
    home = os.environ.get("HOME", "")

    def var(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        return {"PATH": session, "HOME": home, "PWD": where}[name]

    entries = []
    for entry in _PATH_VALUE_VARS.sub(var, value).split(os.pathsep):
        if entry.startswith("~") and (entry == "~" or entry.startswith("~/")):
            entry = home + entry[1:]
        if re.search(r"[$`~*?\[{]", entry):
            return None
        entries.append(entry)
    return entries


def _path_changes(text: str) -> int:
    """How many mentions of PATH in `text` (quotes removed) may CHANGE it: every `_PATH_TAINT`
    match except a read (`$PATH`, `${PATH}`, `${PATH:-x}`, `${#PATH}`)."""
    text = re.sub(r"[\"'\\]", "", text)
    return sum(1 for m in _PATH_TAINT.finditer(text) if not _PATH_READ.search(text[: m.start()]))


def _path_searches(command: str, where: str) -> list[list[str]] | None:
    """Every PATH `command` may run its bare programs under: the hook's own, plus each PATH the
    command assigns (see the block comment above). None when it may change PATH any other way:
    a mention in a segment that runs a PATH_WRITERS builtin, in a `${PATH:=x}` expansion or next
    to a command substitution. A mention another program merely receives (`echo "PATH is"`,
    `grep PATH f`, `printenv PATH`) changes nothing."""
    searches = [os.environ.get("PATH", "").split(os.pathsep)]
    if not _path_changes(command):
        return searches
    try:
        segments = _segments(command)
    except ValueError:
        return None
    for seg in segments:
        words = list(seg)
        while words and words[0] in SHELL_KEYWORDS:
            words.pop(0)
        set_builtin = bool(words) and words[0] in SET_BUILTINS and words[0] != "unset"
        if set_builtin:
            words.pop(0)
            while words and words[0] in ("-x", "-g", "--"):
                words.pop(0)
        while words and _ASSIGNMENT.match(words[0].replace("+=", "=", 1)):
            name, _, value = words.pop(0).partition("=")
            if name.rstrip("+") != "PATH":
                if _path_changes(name + "="):
                    return None  # zsh `path=(...)`, `path[1]=...`
                continue
            entries = _path_value(("$PATH" if name == "PATH+" else "") + value, where)
            if entries is None:
                return None
            searches.append(entries)
        if set_builtin:
            if _path_changes(" ".join(words)):
                return None  # `export X PATH`, `declare -n p=PATH`
            continue
        if words[:1] == ["hash"] and any(w[:1] == "-" and "p" in w for w in words[1:]):
            return None  # `hash -p FILE NAME` runs FILE for NAME
        mentioned = [word for word in words if _path_changes(word)]
        if mentioned and (
            words[0] in PATH_WRITERS
            or (words[0] in WRAPPERS and any(word in PATH_WRITERS for word in words))
            or any(re.search(r"\$\(|`|\$\{PATH:?[=?+-]", word) for word in mentioned)
        ):
            return None
    return searches


def _exec_scripts(name: str, where: str) -> list[str] | None:
    """The shell script FILES a bare program `name` resolves to, one per PATH it may run under
    (empty: none of them is a shell script); None when the search path is unknown."""
    searches = _PATH_SEARCH[0] if _PATH_SEARCH else [os.environ.get("PATH", "").split(os.pathsep)]
    if searches is None:
        return None
    key = (name, where, tuple(tuple(search) for search in searches))
    if key not in _EXEC_CACHE:
        scripts: list[str] = []
        for search in searches:
            for entry in search:
                candidate = os.path.join(where, entry or ".", name)
                if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                    if _shell_shebang(candidate) and candidate not in scripts:
                        scripts.append(candidate)
                    break  # the shell runs the first one
        _EXEC_CACHE[key] = scripts
    return _EXEC_CACHE[key]


# A PATH-resolved script that names git nowhere -- not in its own text, nor in any script it may
# run by a literal path or bare name, nor in any file it sources by a literal name, followed
# transitively (at most MAX_SCAN_FILES files) -- is not read further (Codex 4240628980): system
# tools are shell scripts (`which`, `zgrep`, `ldd`, `mvn`, `gradle`, `gcloud`) whose text this
# parser was never meant to prove, and refusing them on every branch would make the guard
# unusable. One that does name git anywhere in that closure is analysed like `./deploy`, and
# refused when it cannot be. Every word of a file that names a script counts (comments too):
# over-reading, never under-.
_GIT_IN_SCRIPT = re.compile(rb"(?<![\w.])git(?!\w)")
_SCRIPT_WORD = re.compile(rb"[A-Za-z0-9_./+-]+")
_SOURCED = re.compile(rb"(?:^|[\s;&|(])(?:\.|source)\s+[\"']?([A-Za-z0-9_./+-]+)")
MAX_SCAN_BYTES = 4 * 1024 * 1024
MAX_SCAN_FILES = 256
_FILE_SCAN: dict = {}  # (path, where) -> (names git, files it may run) | None; one check


def _scan_bytes(path: str) -> bytes | None:
    """The first MAX_SCAN_BYTES of the regular file `path`; None when it is not one, cannot be
    read or is larger."""
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as handle:
            data = handle.read(MAX_SCAN_BYTES + 1)
    except OSError:
        return None
    return None if len(data) > MAX_SCAN_BYTES else data


def _file_scan(path: str, where: str) -> tuple[bool, list[str] | None] | None:
    """(does the file name git, every file it may run or source -- None when the search path is
    unknown) for the regular file `path`; None when it cannot be read."""
    if (path, where) in _FILE_SCAN:
        return _FILE_SCAN[path, where]
    data = _scan_bytes(path)
    result: tuple[bool, list[str] | None] | None = None
    if data is not None:
        here = os.path.dirname(path)
        sourced = {m.decode("ascii") for m in _SOURCED.findall(data)}
        runs: list[str] | None = []
        for token in {m.decode("ascii") for m in _SCRIPT_WORD.findall(data)}:
            if "/" in token:
                found = [token] if os.path.isabs(token) else [os.path.join(where, token)]
                found += [] if os.path.isabs(token) else [os.path.join(here, token)]
                found = [
                    f
                    for f in found
                    if os.path.isfile(f)
                    and (token in sourced or f.endswith(SCRIPT_SUFFIXES) or _shell_shebang(f))
                ]
            else:
                scripts = _exec_scripts(token, where)
                if scripts is None:
                    runs = None
                    break
                found = list(scripts)
                if token in sourced:  # `. NAME`: PATH, then the current directory; any file
                    found += [
                        f
                        for f in (os.path.join(d, token) for d in (*_search_dirs(where), where))
                        if os.path.isfile(f)
                    ]
            runs += [os.path.realpath(f) for f in found]
        result = (bool(_GIT_IN_SCRIPT.search(data)), runs)
    _FILE_SCAN[path, where] = result
    return result


def _git_free(path: str, where: str) -> bool:
    """Does the shell script FILE `path`, run from `where`, name git nowhere (see above)?"""
    start = os.path.realpath(path)
    seen, queue = {start}, [start]
    while queue:
        scan = _file_scan(queue.pop(), where)
        if scan is None or scan[0] or scan[1] is None:
            return False
        for run in scan[1]:
            if run not in seen:
                seen.add(run)
                queue.append(run)
        if len(seen) > MAX_SCAN_FILES:
            return False
    return True


def _search_dirs(where: str) -> list[str]:
    """Every directory of every PATH the check may run under (unknown: none)."""
    searches = _PATH_SEARCH[0] if _PATH_SEARCH else [os.environ.get("PATH", "").split(os.pathsep)]
    return [os.path.join(where, entry or ".") for search in searches or () for entry in search]


def _widen_path_search(code: str, where: str) -> None:
    """Add the PATHs a script's text assigns to the check's search (unknown stays unknown)."""
    if not _PATH_SEARCH or _PATH_SEARCH[0] is None:
        return
    searches = _path_searches(code, where)
    if searches is None:
        _PATH_SEARCH[0] = None
    else:
        _PATH_SEARCH[0] += [s for s in searches if s not in _PATH_SEARCH[0]]


def _script_invocation(words: list[str], where: str) -> tuple[str, bool | None] | None:
    """(script word, may be found on PATH) when `words` runs a shell script FILE: a shell with a
    script operand or `<` redirection, `source`/`.`, or a path (or `.sh`/`.bash` name) executed
    directly that is a shell script. A bare program name that PATH resolves to a shell script
    (or that cannot be resolved: the search path is unknown) is (name, None). None for anything
    else (stdin shells included)."""
    program = words[0]
    name = os.path.basename(program)
    if name in SHELLS:
        return _shell_script_operand(words)
    if name in SOURCE_BUILTINS and program == name:
        return (words[1], True) if len(words) > 1 else None
    if name in GIT_PROGRAMS or name in WRAPPERS:
        return None
    if program.endswith(SCRIPT_SUFFIXES):
        return program, "/" not in program
    if "/" in program:
        return (program, False) if _shell_shebang(os.path.join(where, program)) else None
    if (
        _PLAIN_NAME.fullmatch(program)
        and program not in SHELL_BUILTINS
        and program not in SHELL_KEYWORDS
        and not program.startswith("git-")
    ):
        scripts = _exec_scripts(program, where)  # Codex 4240628980
        if scripts is None or not all(_git_free(script, where) for script in scripts):
            return program, None
    return None


def _script_paths(word: str, where: str, search_path: bool | None) -> list[str] | None:
    """Every file the shell may run for script `word` from `where` (a bare name may come from
    any absolute PATH entry too: all are read; `search_path` None: a bare program, resolved as
    `_exec_scripts` does); None when the shell rewrites the word or the directory first (an
    expansion, a glob, a brace or `~`), or the search path is unknown."""
    if search_path is None:
        return None if re.search(r"[$`]", where) else _exec_scripts(word, where)
    if (
        not word
        or _SELECTOR_EXPANSION.search(word)
        or _BRACE_EXPANSION.search(word)
        or "~" in word
        or re.search(r"[$`]", where)
    ):
        return None
    local = os.path.join(where, word)
    if "/" in word or not search_path:
        return [local]
    paths = [local] if os.path.lexists(local) else []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(entry, word)
        if os.path.isabs(entry) and os.path.isfile(candidate) and candidate not in paths:
            paths.append(candidate)
    return paths or [local]


def _read_script(path: str) -> str | None:
    """The text of the regular file `path`; None when it is not one, cannot be read, is larger
    than MAX_SCRIPT_BYTES, holds a NUL byte or is not UTF-8. (Never a FIFO or device: reading
    one could hang the hook.)"""
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as handle:
            data = handle.read(MAX_SCRIPT_BYTES + 1)
    except OSError:
        return None
    if len(data) > MAX_SCRIPT_BYTES or b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _script_code(text: str) -> str:
    """A script's text as one command string: comments (a `#` opening a word, through the end
    of its line) dropped and line continuations joined, quoting understood as `_raw_span` reads
    it. ValueError when a quote or substitution is never closed."""
    out: list[str] = []
    index, in_word = 0, False
    while index < len(text):
        char = text[index]
        if char in " \t\r\n;&|()":
            out.append(char)
            in_word = False
            index += 1
        elif char == "#" and not in_word:
            newline = text.find("\n", index)
            index = len(text) if newline == -1 else newline
        elif text.startswith("\\\n", index):
            index += 2  # a line continuation: the shell joins the two lines
        else:
            end = _raw_span(text, index)
            out.append(text[index:end])
            in_word = True
            index = end
    return "".join(out)


def _script_texts(word: str, search_path: bool | None, where: str, depth: int) -> list[str] | None:
    """The analysable text (`_script_code`) of every file script `word` may be; None when any
    one cannot be read or parsed, or the script is nested deeper than MAX_SUBST_DEPTH."""
    paths = _script_paths(word, where, search_path)
    if paths is None or depth >= MAX_SUBST_DEPTH:
        return None
    texts: list[str] = []
    for path in paths:
        text = _read_script(path)
        if text is None:
            return None
        try:
            texts.append(_script_code(text))
        except ValueError:
            return None
    return texts


def _program_targets(code: str, depth: int, where: str) -> list[Target]:
    """`commit_targets` of a script's text, with the top-level proofs a command line gets: the
    raw-text proof (`_raw_unproven`) and the `_ENV_TAINT`/`_PATH_TAINT` rule."""
    unquoted = re.sub(r"[\"'\\]", "", code)
    tainted = _mentions_git(code) and bool(
        _ENV_TAINT.search(unquoted) or _PATH_TAINT.search(unquoted)
    )
    if tainted:
        _TAINTED[:] = [True]  # kept for the rest of the check: over-guarding, never under-
    _widen_path_search(code, where)  # Codex 4240628980: the PATHs the script assigns
    found = commit_targets(code, depth, where)
    if _raw_unproven(code, depth):
        found.append(_unresolved())
        found += _mentioned_refs(((), (), ()), code)
    if tainted:
        cds = _cd_words(code)
        found += [(tuple(cds[:count]), (), ()) for count in range(len(cds) + 1)]
        found += _mentioned_refs(((), (), ()), code)
    return found


def _script_targets(
    script: tuple[str, bool | None], depth: int, where: str, chdirs: tuple[str, ...]
) -> list[Target]:
    """The guarded git invocations of the shell script FILE `script` runs from `where` (see the
    block comment above), relative to the command's own directory changes `chdirs`."""
    texts = _script_texts(script[0], script[1], where, depth)
    if texts is None:
        return [_unresolved(chdirs)]
    found: list[Target] = []
    try:
        for code in texts:
            found += [
                ((*chdirs, *dirs), opts, env)
                for dirs, opts, env in _program_targets(code, depth + 1, where)
            ]
    except ValueError:
        return [_unresolved(chdirs)]
    return found


def _script_changes_branch(script: tuple[str, bool | None], depth: int, where: str) -> bool:
    """Could the shell script FILE `script` move HEAD (an unreadable one could)?"""
    texts = _script_texts(script[0], script[1], where, depth)
    return texts is None or any(_changes_branch(code, depth + 1, where) for code in texts)


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
    # Codex 4240170620: git run from text this parser does not read as git's own command line
    # (a shell's stdin, xargs/find/parallel/eval, an interpreter's code) keeps its `-C`/
    # `--git-dir`/GIT_DIR selectors out of sight: its repository is unresolved, never the
    # current checkout with those selectors dropped.
    unresolved = [_unresolved(tuple(chdirs))]
    program = os.path.basename(words[0])
    inner = _shell_command_string(words)
    if inner is not None:
        if depth >= MAX_SUBST_DEPTH:
            return unresolved
        return [
            ((*chdirs, *inner_dirs), opts, (*env, *inner_env))
            for inner_dirs, opts, inner_env in commit_targets(inner, depth + 1, _where(cwd, chdirs))
        ]
    args = [_strip_substitutions(word) for word in words[1:]]
    named = _mentioned_refs(unknown[0], " ".join(words[1:]))
    where = _where(cwd, chdirs)
    script = _script_invocation(words, where)
    if script is not None:  # Codex 4240526381: the git inside a script file is guarded too
        found = _script_targets(script, depth, where, tuple(chdirs))
        if any(_mentions_git(a) for a in args):
            found += unresolved  # `bash x.sh git commit`: "$@" runs it
        return found + named
    if program in SHELLS:  # `| sh`, `bash <<< ...`: the program arrives on stdin
        reads_stdin = _shell_reads_stdin(words)
        return unresolved + named if reads_stdin or any(_mentions_git(a) for a in args) else []
    if program.startswith("git-"):  # `git-commit`, `git-push`: a git subcommand run directly
        refs = _named_refs(program[4:], words[1:], _where(cwd, chdirs), unknown[0], False)
        return unknown + [_with_ref(unknown[0], ref) for ref in refs]
    if program not in GIT_PROGRAMS:
        if any(_mentions_git(a) for a in args):
            # `xargs git`, `find -exec git`, `eval git ...`: git's arguments (and so the refs
            # it updates) arrive from stdin or are assembled -- no destination can be known.
            # `python -c '...git'`: every issue ref the text names is a destination.
            if program in FANOUT or program in _EVAL_LIKE:
                fanned = _fanout_git_target(words, chdirs, env) if program in FANOUT else None
                base = [fanned] if fanned is not None else unresolved
                if not _ref_free_fanout(words):
                    return [_with_ref(unknown[0], UNKNOWN_REF), *base, *named]
                return base + named
            if program not in INERT_PROGRAMS and _REPO_SELECTOR_TEXT.search(" ".join(args)):
                return unresolved + named  # `python -c "...['git', '-C', p, 'commit']"`
            return unknown + named
        if program in FANOUT and any(
            os.path.basename(a) in SHELLS or a.endswith(SCRIPT_SUFFIXES) for a in args
        ):
            return unresolved + named  # `xargs sh`, `find -exec bash -c ...`, `-exec ./x.sh`
        return []
    unsafe = any(
        key not in SAFE_GIT_ENV and not key.startswith("LC_") for key in keys
    )  # GIT_EXTERNAL_DIFF=..., GIT_CONFIG_*=..., PAGER=... git log
    # An exported GIT_*/PAGER/EDITOR setting elsewhere in the command (`_ENV_TAINT`): even a
    # read-only subcommand is guarded, in the repository ITS selectors name (Codex 4240170620).
    tainted = bool(_TAINTED)
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
        if unsafe or tainted or not _read_only(word, rest):
            # Codex 4239397967: the refs the command names are destinations too, not only the
            # checked-out branch (`git push origin HEAD:forge/issue-7` from main).
            refs = _named_refs(word, rest, _where(cwd, chdirs), target[0], unsafe)
            return target + [_with_ref(target[0], ref) for ref in refs]
        if word == "fetch":  # Codex 4239868024: configured refspecs may write local branches
            refs = _configured_fetch_refs(_where(cwd, chdirs), target[0], unsafe)
            if refs:
                return target + [_with_ref(target[0], ref) for ref in refs]
        reason = _helper_reason(
            word, rest, _where(cwd, chdirs), target[0], bare_git="/" not in words[0]
        )
        if reason:  # Codex 4239667542: configuration can make it run a program
            _HELPER_NOTES[reason] = None
            return target
        return []
    return []  # `git`, `git --version`: no subcommand


def _fanout_git_target(
    words: list[str], chdirs: list[str], env: list[tuple[str, str]]
) -> Target | None:
    """`xargs git -C DIR log`, `find . -exec git log -- {} ;`: a fan-out running a LITERAL
    `git [global options] SUB`, whose own selectors name its repository -- the words the fan-out
    supplies land after SUB, where git reads no repository option. None (unresolved, Codex
    4240170620) when they may land anywhere else: no literal subcommand (`xargs git`, `parallel
    git ::: commit`), a placeholder or replace string before it (`xargs -I{} git -C {} commit`),
    a per-file directory (`find -execdir`, `parallel --wd`), or a wrapper, shell or assignment
    between the fan-out and git."""
    program = os.path.basename(words[0])
    index = next((i for i, w in enumerate(words) if os.path.basename(w) in GIT_PROGRAMS), None)
    if index is None:
        return None
    for word in words[1:index]:
        name = os.path.basename(word)
        if _ASSIGNMENT.match(word) or name in WRAPPERS or name in SHELLS or name == "eval":
            return None
        if word in ("-execdir", "-okdir") or word.startswith(("--wd", "--workdir")):
            return None
        if program == "xargs" and (
            word.startswith("--replace")
            or (word.startswith("-") and not word.startswith("--") and set(word[1:]) & set("iI"))
        ):
            return None
    rest = list(words[index + 1 :])
    repo_opts: list[str] = []
    while rest:
        word = rest.pop(0)
        if "{" in word or word in (":::", "::::", ":::+", "::::+", ";", "+"):
            return None
        if word in GIT_VALUE_OPTS:
            if not rest or "{" in rest[0]:
                return None
            value = rest.pop(0)
            if word in GIT_REPO_OPTS:
                repo_opts += [word, value]
            continue
        if word.startswith(("--git-dir=", "--work-tree=")):
            repo_opts.append(word)
            continue
        if word.startswith("-C") and len(word) > 2:
            repo_opts += ["-C", word[2:]]
            continue
        if word.startswith("-"):
            continue
        if not _plain(word):
            return None  # `xargs git < args`: a redirection, not a subcommand
        return (tuple(chdirs), tuple(repo_opts), tuple(env))
    return None


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


# ---- Branch changes inside one command (Codex 4238832970). Every target's branch is asked of git
# BEFORE anything runs, so `git switch forge/issue-7 && git commit` would be judged on the branch
# the hook started on. A segment that can move HEAD to another branch therefore re-targets every
# guarded git invocation after it: to the branch it names when that is statically known
# (`git switch X`, `git checkout X`, `git checkout -b X`, `git switch -c X`), and to
# UNKNOWN_BRANCH -- blocked outright -- when it is not (`git checkout "$B"`, `git checkout -`,
# bisect, symbolic-ref, an alias or unknown subcommand, git run indirectly, `source`/`eval`, a
# script, `pushd`/`popd`, a `cd` to an expanded directory, an exported GIT_DIR/GIT_WORK_TREE).
# The new branch REPLACES the old one only for an invocation in the same repository joined to the
# switch by `&&` alone (the switch then certainly succeeded); otherwise both are checked.
UNKNOWN_BRANCH = "\0unknown-branch"
BRANCH_TIMEOUT = "\0branch-timeout"  # git did not report the branch within GIT_TIMEOUT
_BRANCH_KEY = "\0branch"  # a Target env entry carrying the branch HEAD was moved to
# Git subcommands that never move HEAD to another branch (the read-only ones never do either).
SWITCH_FREE = frozenset(
    {
        "commit", "merge", "cherry-pick", "revert", "am", "apply", "pull", "reset", "push",
        "stash", "notes", "tag", "update-ref", "commit-tree", "fetch", "add", "rm", "mv",
        "restore", "worktree", "clean", "gc", "fsck", "prune", "repack", "hash-object",
        "update-index", "write-tree", "read-tree", "format-patch", "mktree", "mktag", "rerere",
        "init", "clone", "submodule", "sparse-checkout", "lfs", "maintenance", "range-diff",
        "rev-list", "show-ref", "ls-remote", "count-objects", "var", "diff-tree", "diff-index",
        "diff-files", "whatchanged", "cherry", "archive", "bundle", "verify-commit",
        "verify-tag", "check-ref-format", "interpret-trailers", "request-pull", "annotate",
        "show-branch", "difftool", "mergetool", "pack-refs", "credential", "send-email",
    }
)  # fmt: skip
QUIET_OPTS = frozenset({"-q", "--quiet"})
CHECKOUT_CREATE_OPTS = frozenset({"-b", "-B", "--orphan"})
SWITCH_CREATE_OPTS = frozenset({"-c", "-C", "--create", "--force-create", "--orphan"})
# `git rebase` options (Codex 4239965192): which ones take a value -- attached (`--onto=X`,
# `-sours`) or as the next word -- and which take none. `-S`/`--gpg-sign` and `-r`/
# `--rebase-merges` take an OPTIONAL value, attached only: `-S main forge/issue-7` is two
# operands. Anything else -- an abbreviated `--ont`, an option this list does not know -- makes
# the operands unknowable and the command is blocked (`_rebase_branch` returns UNKNOWN_REF).
REBASE_LONG_VALUE = frozenset(
    {"--onto", "--strategy", "--strategy-option", "--exec", "--whitespace", "--empty", "--trailer"}
)
REBASE_LONG_OPTIONAL = frozenset({"--gpg-sign", "--rebase-merges"})
REBASE_LONG_FLAGS = frozenset(
    {
        "--interactive", "--keep-base", "--root", "--fork-point", "--no-fork-point",
        "--autosquash", "--no-autosquash", "--autostash", "--no-autostash", "--quiet",
        "--verbose", "--stat", "--no-stat", "--verify", "--no-verify", "--signoff",
        "--committer-date-is-author-date", "--reset-author-date", "--ignore-date",
        "--ignore-whitespace", "--force-rebase", "--no-ff", "--merge", "--apply",
        "--keep-empty", "--no-keep-empty", "--reapply-cherry-picks",
        "--no-reapply-cherry-picks", "--allow-empty-message", "--rerere-autoupdate",
        "--no-rerere-autoupdate", "--no-rebase-merges", "--no-gpg-sign", "--update-refs",
        "--no-update-refs", "--reschedule-failed-exec", "--no-reschedule-failed-exec",
    }
)  # fmt: skip
# The action forms (`git rebase --continue`) take no operand at all.
REBASE_ACTIONS = frozenset(
    {"--continue", "--skip", "--abort", "--quit", "--edit-todo", "--show-current-patch"}
)
REBASE_SHORT_FLAGS = frozenset("iqvnfmk")
REBASE_SHORT_OPTIONAL = frozenset("rS")
REBASE_SHORT_VALUE = frozenset("sXxC")
# A program that mentions git AND one of these words may move HEAD through it.
_MOVES_HEAD = re.compile(
    r"(?<![\w-])(?:checkout|switch|rebase|bisect|symbolic-ref|branch)(?![\w-])"
)
SET_BUILTINS = frozenset({"export", "declare", "typeset", "local", "readonly", "unset"})
_GIT_REPO_VAR = re.compile(r"^GIT_(?:DIR|WORK_TREE)(?:=|$)")


# ---- Named destination refs (Codex 4239397967). A guarded git command updates the checked-out
# branch AND every ref it names: `git push origin HEAD:forge/issue-7`, `git push origin
# :forge/issue-7`, `git branch -f forge/issue-7 HEAD`, `git update-ref refs/heads/forge/issue-7 X`
# run from main touch issue 7 all the same. Each named ref rides its Target as `_REF_KEY` and is
# checked against ITS issue's lease. When the destinations cannot be known before the command
# runs -- `--all`/`--mirror`/`--prune`, a glob or `:` refspec, a push whose refspecs come from
# configuration that may map elsewhere (`remote.*.push`, `push.default` upstream/matching), an
# alias to a shell command or an external `git-<name>` program, `rebase --exec/--update-refs`,
# `update-ref --stdin`, a computed name -- the target carries UNKNOWN_REF and the command is
# blocked outright, issue branch or not.
UNKNOWN_REF = "\0unknown-ref"
_REF_KEY = "\0ref"  # a Target env entry carrying a ref the command names as a destination
_ISSUE_REF_TEXT = re.compile(r"[A-Za-z0-9_./-]*issue-\d+[A-Za-z0-9_./-]*")
# Subcommands that update no ref but the checked-out branch (or none at all). A branch CHANGE
# (`checkout -b X`, `switch -c X`, `rebase U X`) is `_segment_switch`'s and re-targets already.
NO_NAMED_REFS = (
    READ_ONLY_ANY
    | {"grep", "reflog", "remote", "config", "tag", "checkout", "switch"}
    | (SWITCH_FREE - {"push", "update-ref", "fetch", "pull", "worktree", "stash", "submodule"})
    | {"merge-tree", "merge-file", "patch-id", "column", "stripspace", "mailsplit", "mailinfo"}
    | {"commit-graph", "multi-pack-index", "pack-objects", "index-pack", "verify-pack"}
    | {"unpack-objects", "prune-packed", "update-server-info", "show-index", "check-mailmap"}
    | {"fmt-merge-msg", "get-tar-commit-id", "bugreport", "diagnose", "quiltimport", "replace"}
)
UNKNOWN_REF_SUBCOMMANDS = frozenset({"filter-branch", "fast-import", "replay", "filter-repo"})
PUSH_VALUE_OPTS = frozenset({"-o", "--push-option", "--repo", "--receive-pack", "--exec"})
PUSH_ALL_OPTS = frozenset({"--all", "--branches", "--mirror", "--prune"})
PUSH_LONG_FLAGS = frozenset(
    {
        "--tags", "--follow-tags", "--no-follow-tags", "--atomic", "--no-atomic", "--dry-run",
        "--porcelain", "--force", "--set-upstream", "--thin", "--no-thin", "--progress",
        "--no-progress", "--no-verify", "--verify", "--ipv4", "--ipv6", "--quiet", "--verbose",
        "--signed", "--no-signed", "--force-with-lease", "--no-force-with-lease",
        "--force-if-includes", "--no-force-if-includes", "--recurse-submodules",
        "--no-recurse-submodules",
    }
)  # fmt: skip
PUSH_SHORT_FLAGS = frozenset("fnqvu46")
BRANCH_VALUE_OPTS = frozenset(
    {"-u", "--set-upstream-to", "--contains", "--no-contains", "--merged", "--no-merged"}
    | {"--points-at", "--sort", "--format"}
)
# Long `git branch` options -> the short letter of their mode ("" = no bearing on the mode).
BRANCH_LONG_MODES = {
    "--delete": "d", "--move": "m", "--copy": "c", "--list": "l", "--all": "a",
    "--remotes": "r", "--show-current": "l", "--unset-upstream": "U",
    "--edit-description": "U", "--force": "f", "--verbose": "", "--quiet": "", "--track": "",
    "--no-track": "", "--ignore-case": "", "--create-reflog": "", "--no-create-reflog": "",
    "--recurse-submodules": "", "--no-recurse-submodules": "", "--omit-empty": "",
    "--color": "", "--no-color": "", "--column": "", "--no-column": "", "--abbrev": "",
    "--no-abbrev": "",
}  # fmt: skip
BRANCH_SHORT_FLAGS = frozenset("dDmMcCflarvqti")


def _with_ref(target: Target, ref: str) -> Target:
    dirs, opts, env = target
    return (dirs, opts, (*env, (_REF_KEY, ref)))


def _branch_name(ref: str) -> str:
    """`refs/heads/X` -> `X`; any other spelling as given."""
    return ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref


def _mentioned_refs(target: Target, text: str) -> list[Target]:
    """A destination Target for every issue ref `text` names (`HEAD:forge/issue-7` inside a
    `python -c` string): what an indirect git run can update is at least what it spells out."""
    names = dict.fromkeys(_branch_name(m) for m in _ISSUE_REF_TEXT.findall(text))
    return [_with_ref(target, name) for name in names]


def _ref_free_fanout(words: list[str]) -> bool:
    """`xargs git log`, `find -exec git status {} ;`: the git subcommand is a literal on the
    command line and updates no named ref, so only its trailing arguments come from elsewhere."""
    for index, word in enumerate(words):
        if os.path.basename(word) in GIT_PROGRAMS:
            sub, _ = _git_sub_args(words[index:])
            return sub is not None and sub in NO_NAMED_REFS
    return False


def _git_config(where: str, target: Target, *args: str) -> tuple[int, str]:
    """`git config ARGS` in the repository `target` acts on: (exit code, stdout); -1 when git
    could not be run at all or did not answer within GIT_TIMEOUT. Exit 1 is git's "no such
    key"."""
    return _git_in(where, target, "config", *args)


def _git_in(where: str, target: Target, *args: str) -> tuple[int, str]:
    """`git ARGS` in the repository `target` acts on: (exit code, stdout); -1 when git could
    not be run at all or did not answer within GIT_TIMEOUT (Codex 4239667544)."""
    _, opts, env = target
    clean = {key: value for key, value in env if not key.startswith("\0")}
    try:
        proc = subprocess.run(
            ["git", *opts, *args],
            cwd=where,
            env={**os.environ, **clean} if clean else None,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return -1, ""
    return proc.returncode, proc.stdout


def _safe_url(url: str) -> bool:
    """A URL git fetches from without a remote helper program: http(s)/ssh/git/file, an
    scp-like `host:path` or a local path. `<transport>::...` and any other `<scheme>://` run
    `git-remote-<name>` (`ext::` runs an arbitrary command)."""
    url = url.strip()
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*::", url):
        return False
    scheme = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*)://", url)
    return scheme is None or scheme.group(1).lower() in SAFE_URL_SCHEMES


def _signature_wanted(args: list[str], config: dict[str, list[str]]) -> bool:
    """Does the command verify signatures (and so run the gpg/ssh signature program)?"""
    if any(a == "--show-signature" or "%G" in a or "%(signature" in a for a in args):
        return True
    return any(
        value.strip().lower() in ("true", "yes", "on", "1")
        for value in config.get("log.showsignature", [])
    )


def _config_reason(sub: str, args: list[str], config: dict[str, list[str]]) -> str | None:
    """The first configuration entry that makes `git SUB ARGS` run a program, or None."""
    network = _contacts_remote(sub, args)
    signatures = _signature_wanted(args, config)
    for key, values in config.items():
        for value in values:
            stripped = value.strip()
            if HELPER_KEYS.match(key):
                lfs = LFS_FILTERS.get(key)
                if lfs is not None and lfs.match(stripped):
                    continue
                return f"{key}={stripped}"
            if PAGER_KEYS.match(key):
                if key != "core.pager" and stripped.lower() in GIT_BOOLEANS:
                    continue
                if not stripped or SAFE_PAGER.match(stripped):
                    continue
                return f"{key}={stripped}"
            if key == "core.fsmonitor" and stripped.lower() not in GIT_BOOLEANS:
                return f"{key}={stripped}"
            if sub == "help" and HELP_KEYS.match(key) and stripped.lower() not in ("man", ""):
                return f"{key}={stripped}"
            if signatures and SIGNATURE_KEYS.match(key) and stripped:
                return f"{key}={stripped}"
            if "ext::" in key.lower() or stripped.lower().startswith("ext::"):
                return f"{key}={stripped}"
            if not network:
                continue
            if NETWORK_KEYS.match(key) and stripped:
                if key.endswith(".helper") and SAFE_CREDENTIAL.match(stripped):
                    continue
                return f"{key}={stripped}"
            if URL_VALUE_KEYS.match(key) and not _safe_url(stripped):
                return f"{key}={stripped}"
            base = URL_BASE_KEYS.match(key)
            if base and not _safe_url(base.group(1)):
                return f"{key}={stripped}"
    return None


def _read_config(where: str, target: Target) -> dict[str, list[str]] | None:
    """Every configuration entry git would use for `target` (all scopes, includes resolved by
    git, GIT_CONFIG_* honoured), by key with section and variable names lowercased (they are
    case-insensitive; subsections are not); None when it cannot be read."""
    code, out = _git_config(where, target, "--list", "--includes", "-z")
    if code != 0:
        return None
    config: dict[str, list[str]] = {}
    for entry in out.split("\0"):
        if not entry:
            continue
        key, _, value = entry.partition("\n")
        section, dot, rest = key.partition(".")
        name, _, var = rest.rpartition(".")
        # section and variable names are case-insensitive, subsections are not
        lowered = f"{section.lower()}{dot}{name}{'.' if name else ''}{var.lower()}"
        config.setdefault(lowered, []).append(value)
    return config


def _helper_reason(
    sub: str, args: list[str], where: str, target: Target, *, bare_git: bool = True
) -> str | None:
    """Why the allowlisted `git SUB ARGS` may still run a program configured outside the
    command (Codex 4239667542), or None when it provably does not. Fail closed: configuration
    or a hooks directory that cannot be read is a reason. `bare_git`: the command runs `git` by
    its bare name, so the shell finds git itself on PATH (`_writable_path_reason`)."""
    names = HELPER_ENV + PAGER_ENV + (NETWORK_ENV if _contacts_remote(sub, args) else ())
    for name in names:
        value = os.environ.get(name, "").strip()
        if value and not (name in PAGER_ENV and SAFE_PAGER.match(value)):
            return f"{name}={value} in the environment"
    relative = _relative_path_entry()
    if relative is not None:  # Codex 4240421142: `ssh` (and git) found relative to the cwd
        return f"PATH entry {relative!r} is relative: what it runs depends on the directory"
    config = _read_config(where, target)
    if config is None:
        return "git configuration could not be read"
    found = _config_reason(sub, args, config)
    if found:
        return found
    found = _writable_path_reason(sub, args, where, target, config, bare_git)
    if found:  # Codex 4240952107: a program dropped where PATH finds it first
        return found
    code, out = _git_in(where, target, "rev-parse", "--git-path", "hooks")
    lines = out.splitlines()
    if code == -1 or (code == 0 and (len(lines) != 1 or not lines[0].strip())):
        return "the hooks directory could not be read"
    if code != 0:
        return None  # no repository there: the command itself fails before any hook could run
    base = where  # a relative answer is relative to where `-C` options lead
    opts, index = list(target[1]), 0
    while index < len(opts):
        if opts[index] in GIT_REPO_OPTS and index + 1 < len(opts):
            if opts[index] == "-C":
                base = os.path.join(base, os.path.expanduser(opts[index + 1]))
            index += 2
        else:
            index += 1  # `--git-dir=...`, `--work-tree=...`
    hooks = os.path.join(base, lines[0].strip())
    for hook in READ_ONLY_HOOKS:
        if os.path.lexists(os.path.join(hooks, hook)):
            return f"hook {os.path.join(hooks, hook)}"
    code, out = _git_in(where, target, "rev-parse", "--show-toplevel")
    if code == 0 and out.strip() and os.path.lexists(os.path.join(out.strip(), ".gitmodules")):
        return "submodules (.gitmodules): git recurses into their own configuration"
    return None


def _relative_path_entry() -> str | None:
    """The first PATH entry (in the hook's environment, which the command inherits) that is not
    an absolute directory -- `.`, `bin`, or an empty entry, all resolved against the directory
    the command runs in, which a `cd` or a file the agent wrote can make anything -- or None."""
    path = os.environ.get("PATH")
    if path is None:
        return None
    for entry in path.split(os.pathsep):
        if not os.path.isabs(entry):
            return entry
    return None


# ---- Writable PATH entries (Codex 4240952107). An absolute PATH entry is not proof that the
# program found through it is the one installed there: when a directory the search reaches
# BEFORE (or at) the real program is writable by this user, a command can drop an executable
# named `ssh` there -- `cp x ~/.local/bin/ssh && git fetch origin` never mentions PATH -- and
# git runs it. So every PATH-resolved program an allowlisted command may launch is resolved the
# way it would be, and the command needs the lease when a directory searched up to and including
# the one it resolves in (every entry when it resolves nowhere: a new file there would be found)
# is writable -- `os.access(W_OK)`, or for a missing entry its nearest existing ancestor -- or
# the resolved file itself is. The programs: `git` itself when the command names it bare (the
# shell resolves it), the pager (`less` by default, plus the first word of every
# GIT_PAGER/PAGER/`core.pager`/`pager.*` value; any allowlisted command may page under `git -p`),
# `man` for `git help`, and for a command that contacts a remote `ssh` and the helpers git runs
# by name (`git-remote-http(s)`/`ftp(s)`, `git-upload-pack`, the `cache`/`store` credential
# helpers). Git searches its exec path (`git --exec-path`) before PATH for every program it runs,
# so that directory is checked first for those; an exec path that cannot be read is a reason.
# How the file got there does not matter, which is why this is sound without tracking writes.
# The cost: on a machine where such a directory IS writable (running as root; a user-owned
# `~/.local/bin` or Homebrew prefix ahead of the system git) every allowlisted git command on an
# unleased issue branch needs the lease.
NETWORK_PROGRAMS = (
    "ssh",
    "git-remote-http",
    "git-remote-https",
    "git-remote-ftp",
    "git-remote-ftps",
    "git-upload-pack",
    "git-credential-cache",
    "git-credential-cache--daemon",
    "git-credential-store",
)
DEFAULT_PAGER = "less"


def _dir_writable(directory: str) -> bool:
    """Can this user create a file in `directory` -- or, when it does not exist, create it (its
    nearest existing ancestor is writable)? A path that is not a directory cannot hold one."""
    current = os.path.abspath(directory)
    while not os.path.lexists(current):
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent
    return os.path.isdir(current) and os.access(current, os.W_OK)


def _first_writable(name: str, entries: list[tuple[str, str]]) -> str | None:
    """Walk `entries` ((label, directory) in search order) as an exec search for `name` does:
    the first directory reached that is writable, or the resolved file when it is writable, as
    a reason; None once `name` resolves to a file nobody can replace or nothing is writable."""
    for label, directory in entries:
        if _dir_writable(directory):
            return f"writable {label} {directory} precedes {name}"
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            if os.access(candidate, os.W_OK):
                return f"{name} resolves to the writable file {candidate}"
            return None
    return None


def _pager_programs(config: dict[str, list[str]]) -> list[str]:
    """The bare program names git may start as a pager: the default and the first word of every
    configured or exported pager value (a boolean `pager.<cmd>` names no program)."""
    values = [os.environ.get(name, "") for name in PAGER_ENV]
    for key, entries in config.items():
        if PAGER_KEYS.match(key):
            values += [
                v for v in entries if key == "core.pager" or v.strip().lower() not in GIT_BOOLEANS
            ]
    names = [DEFAULT_PAGER]
    for value in values:
        words = value.split()
        if words and "/" not in words[0] and words[0] not in names:
            names.append(words[0])
    return names


def _writable_path_reason(
    sub: str,
    args: list[str],
    where: str,
    target: Target,
    config: dict[str, list[str]],
    bare_git: bool,
) -> str | None:
    """Why a PATH-resolved program the allowlisted `git SUB ARGS` may launch could be one this
    session placed (see the block comment above), or None."""
    path = [("PATH entry", entry) for entry in os.environ.get("PATH", "").split(os.pathsep)]
    if bare_git:
        found = _first_writable("git", path)
        if found:
            return found
    code, out = _git_in(where, target, "--exec-path")
    exec_path = out.strip()
    if code != 0 or not exec_path or "\n" in exec_path or not os.path.isabs(exec_path):
        return "git's exec path could not be read"
    searched = [("git exec path", exec_path), *path]
    names = _pager_programs(config) + (["man"] if sub == "help" else [])
    if _contacts_remote(sub, args):
        names += list(NETWORK_PROGRAMS)
    for name in names:
        found = _first_writable(name, searched)
        if found:
            return found
    return None


# Reasons `_helper_reason` gave in the current check, for the refusal message.
_HELPER_NOTES: dict[str, None] = {}


def _helper_note() -> str:
    if not _HELPER_NOTES:
        return ""
    return (
        " This git command is otherwise read-only, but git can run a program configured "
        f"outside the command ({'; '.join(_HELPER_NOTES)}), so it needs the lease too: remove "
        "that configuration, or claim the issue."
    )


def _named_refs(
    sub: str, args: list[str], where: str, target: Target, unsafe: bool, depth: int = 0
) -> list[str]:
    """The refs `git SUB ARGS` updates besides the checked-out branch, as branch names, with
    UNKNOWN_REF when they cannot be known (see the block comment above). `unsafe`: git runs
    under configuration given on the command line (`-c`, GIT_CONFIG_*), which the repository's
    configuration read here does not show."""
    if sub in NO_NAMED_REFS:
        return []
    if sub in UNKNOWN_REF_SUBCOMMANDS or any(_dynamic_ref(a) for a in args):
        return [UNKNOWN_REF]
    if sub == "push":
        return _push_refs(args, where, target, unsafe)
    if sub == "branch":
        return _branch_refs(args)
    if sub == "update-ref":
        return _update_ref_refs(args)
    if sub == "symbolic-ref":
        return _symbolic_ref_refs(args)
    if sub in ("fetch", "pull"):
        refs = _fetch_refs(args)
        if UNKNOWN_REF not in refs:
            refs += [r for r in _configured_fetch_refs(where, target, unsafe) if r not in refs]
        if sub == "pull" and UNKNOWN_REF not in refs and "--no-rebase" not in args:
            refs += _rebase_update_refs(where, target, unsafe)
        return refs
    if sub == "rebase":
        return _rebase_refs(args, where, target, unsafe)
    if sub == "worktree":
        return _worktree_refs(args)
    if sub == "stash":
        rest = [a for a in args if not a.startswith("-")]
        return [_branch_name(rest[1])] if len(rest) > 1 and rest[0] == "branch" else []
    if sub == "bisect":
        return [UNKNOWN_REF] if args[:1] == ["run"] else []
    if sub == "submodule":
        return [UNKNOWN_REF] if "foreach" in args else []
    return _alias_refs(sub, args, where, target, unsafe, depth)


def _alias_refs(
    sub: str, args: list[str], where: str, target: Target, unsafe: bool, depth: int
) -> list[str]:
    """What an alias updates: a git alias is followed (`push-wip = push origin HEAD:x`); a
    shell alias (`!...`), an external `git-<name>` program or an unreadable one is unknown."""
    if unsafe or depth >= MAX_SUBST_DEPTH:
        return [UNKNOWN_REF]
    code, out = _git_config(where, target, "--get", f"alias.{sub}")
    value = out.strip()
    if code != 0 or not value or value.startswith("!"):
        return [UNKNOWN_REF]
    try:
        words = shlex.split(value)
    except ValueError:
        return [UNKNOWN_REF]
    if not words or words[0].startswith("-"):
        return [UNKNOWN_REF]  # global options inside the alias (`-c`, `-C`)
    return _named_refs(words[0], [*words[1:], *args], where, target, unsafe, depth + 1)


def _push_refs(args: list[str], where: str, target: Target, unsafe: bool) -> list[str]:
    """`git push [options] [REPOSITORY [REFSPEC...]]`: each refspec's destination; an option
    this parser does not know is unknown (it may take a value that shifts the refspecs)."""
    positionals: list[str] = []
    delete = False
    rest = list(args)
    while rest:
        arg = rest.pop(0)
        if arg == "--":
            positionals += rest
            break
        if not arg.startswith("-") or arg == "-":
            positionals.append(arg)
            continue
        option, eq, _ = arg.partition("=")
        if option in PUSH_ALL_OPTS:
            return [UNKNOWN_REF]
        if option in PUSH_VALUE_OPTS:
            if not eq and rest:
                rest.pop(0)
            continue
        if option in ("-d", "--delete"):
            delete = True
            continue
        if arg.startswith("--"):
            if option in PUSH_LONG_FLAGS:
                continue
            return [UNKNOWN_REF]
        letters = arg[1:]
        for index, letter in enumerate(letters):
            if letter == "o":  # `-o VALUE` / `-oVALUE`
                if not letters[index + 1 :] and rest:
                    rest.pop(0)
                break
            if letter == "d":
                delete = True
            elif letter not in PUSH_SHORT_FLAGS:
                return [UNKNOWN_REF]
    if len(positionals) < 2:
        return _configured_push_refs(where, target, unsafe)
    refs: list[str] = []
    for spec in positionals[1:]:
        ref = _push_destination(spec, delete)
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def _push_destination(spec: str, delete: bool) -> str | None:
    """The branch one push refspec updates; None for the checked-out branch (its own target)."""
    spec = spec[1:] if spec.startswith("+") else spec
    if "*" in spec or spec == ":":
        return UNKNOWN_REF  # a glob, or `:` (every matching branch)
    if delete:
        return _branch_name(spec)
    src, colon, dst = spec.partition(":")
    if not colon or not dst:
        return None if src in ("HEAD", "@") else _branch_name(src)
    if dst in ("HEAD", "@"):
        return UNKNOWN_REF
    return _branch_name(dst)


def _configured_push_refs(where: str, target: Target, unsafe: bool) -> list[str]:
    """A push without refspecs pushes what configuration says. Only `push.default` simple,
    current or nothing with no `remote.*.push` provably lands on the checked-out branch's own
    name (which its own target checks); anything else, or unreadable configuration, is unknown."""
    if unsafe:
        return [UNKNOWN_REF]
    code, out = _git_config(where, target, "--get-regexp", r"^remote\..*\.push$")
    if code not in (0, 1) or (code == 0 and out.strip()):
        return [UNKNOWN_REF]
    code, out = _git_config(where, target, "--get", "push.default")
    if code not in (0, 1):
        return [UNKNOWN_REF]
    mode = out.strip().lower() if code == 0 else "simple"
    return [] if mode in ("simple", "current", "nothing") else [UNKNOWN_REF]


def _branch_refs(args: list[str]) -> list[str]:
    """`git branch`: the names it deletes, renames, copies, configures or (re)creates."""
    positionals: list[str] = []
    modes: set[str] = set()
    rest = list(args)
    while rest:
        arg = rest.pop(0)
        if arg == "--":
            positionals += rest
            break
        if not arg.startswith("-") or arg == "-":
            positionals.append(arg)
            continue
        option, eq, _ = arg.partition("=")
        if option in BRANCH_VALUE_OPTS:
            if not eq and rest:
                rest.pop(0)
            modes.add("U" if option in ("-u", "--set-upstream-to") else "l")
            continue
        if arg.startswith("--"):
            if option not in BRANCH_LONG_MODES:
                return [UNKNOWN_REF]
            modes.update(BRANCH_LONG_MODES[option])
            continue
        letters = arg[1:]
        for index, letter in enumerate(letters):
            if letter == "u":  # `-u UPSTREAM` / `-uUPSTREAM`
                if not letters[index + 1 :] and rest:
                    rest.pop(0)
                modes.add("U")
                break
            if letter not in BRANCH_SHORT_FLAGS:
                return [UNKNOWN_REF]
            modes.add(letter)
    if modes & set("dDmMcCU"):
        chosen = positionals  # every name is deleted, renamed/copied or configured
    elif modes & set("lar") or not positionals:
        return []  # a listing: the positionals are patterns
    else:
        chosen = positionals[:1]  # `git branch [-f] NAME [START]`
    if any("*" in name or "?" in name or "[" in name for name in chosen):
        return [UNKNOWN_REF]
    return [_branch_name(name) for name in chosen]


def _update_ref_refs(args: list[str]) -> list[str]:
    """`git update-ref [-m MSG] [-d] REF ...`: REF (`--stdin` reads its refs from stdin)."""
    rest, positionals = list(args), []
    while rest:
        arg = rest.pop(0)
        if arg == "--stdin":
            return [UNKNOWN_REF]
        if arg == "-m":
            if rest:
                rest.pop(0)
            continue
        if arg.startswith("-") and arg != "-":
            continue
        positionals.append(arg)
    if not positionals or positionals[0] in ("HEAD", "@"):
        return []  # HEAD: the checked-out branch, its own target
    return [_branch_name(positionals[0])]


def _symbolic_ref_refs(args: list[str]) -> list[str]:
    """`git symbolic-ref NAME REF` / `-d NAME` writes NAME; one operand only reads it."""
    rest, positionals, delete = list(args), [], False
    while rest:
        arg = rest.pop(0)
        if arg == "-m":
            if rest:
                rest.pop(0)
        elif arg in ("-d", "--delete"):
            delete = True
        elif not arg.startswith("-"):
            positionals.append(arg)
    if not positionals or (len(positionals) < 2 and not delete) or positionals[0] == "HEAD":
        return []
    return [_branch_name(positionals[0])]


def _fetch_refs(args: list[str]) -> list[str]:
    """`git fetch|pull REMOTE SRC:DST`: each DST. Any word with a `:` counts (an scp-style
    remote only adds a harmless non-issue name); `--stdin`/`--refmap` refspecs are unknown."""
    refs: list[str] = []
    for arg in args:
        if arg.partition("=")[0] in ("--stdin", "--refmap"):
            return [UNKNOWN_REF]
        if arg.startswith("-") or ":" not in arg or "://" in arg:
            continue
        dst = (arg[1:] if arg.startswith("+") else arg).partition(":")[2]
        if "*" in dst:
            return [UNKNOWN_REF]
        if dst and _branch_name(dst) not in refs:
            refs.append(_branch_name(dst))
    return refs


# ---- Configured fetch refspecs (Codex 4239868024). `git fetch origin` with no refspec on the
# command line fetches what `remote.origin.fetch` says, and even with one it updates the refs
# those configured refspecs map it onto; `+refs/heads/main:refs/heads/forge/issue-7` there makes
# a "read-only" fetch (or a pull) write an issue branch. Every remote's configured refspecs are
# read -- a superset of the remotes the fetch can reach (the default remote, `--all`,
# `--multiple`, `remotes.<group>`) -- and each destination that is a local branch (`refs/heads/X`
# or an unqualified name git may resolve to one) is a named destination; `--prune` deletes only
# under those same destinations. A glob destination outside refs/remotes, refs/tags and the
# other non-branch namespaces, configuration given on the command line, or configuration that
# cannot be read is unknown. The standard `+refs/heads/*:refs/remotes/origin/*` stays read-only.
_NON_BRANCH_REF = re.compile(r"^refs/(?!heads/)[^/]+/")


def _fetch_destination(spec: str) -> str | None:
    """The local branch one configured fetch refspec writes; None when it writes none."""
    spec = spec.strip()
    if spec.startswith("^"):
        return None  # a negative refspec excludes, never writes
    src, colon, dst = (spec[1:] if spec.startswith("+") else spec).partition(":")
    if not colon or not dst:
        return None  # `refs/heads/main`: FETCH_HEAD only
    if _NON_BRANCH_REF.match(dst):
        return None  # refs/remotes/..., refs/tags/..., refs/notes/...
    name = _branch_name(dst)
    if not name or "*" in name or name in ("HEAD", "@") or name.startswith("refs/"):
        return UNKNOWN_REF
    return name


def _configured_fetch_refs(where: str, target: Target, unsafe: bool) -> list[str]:
    """The local branches every configured `remote.*.fetch` refspec writes (see above)."""
    if unsafe:
        return [UNKNOWN_REF]  # `-c remote.origin.fetch=...`: not in the configuration read here
    config = _read_config(where, target)
    if config is None:
        return [UNKNOWN_REF]
    refs: list[str] = []
    for key, values in config.items():
        if not (key.startswith("remote.") and key.endswith(".fetch")):
            continue
        for spec in values:
            ref = _fetch_destination(spec)
            if ref == UNKNOWN_REF:
                return [UNKNOWN_REF]
            if ref and ref not in refs:
                refs.append(ref)
    return refs


def _rebase_refs(args: list[str], where: str, target: Target, unsafe: bool) -> list[str]:
    """A rebase rewrites the branch it is on, plus every branch stacked on the rebased commits
    when it updates refs (`--update-refs`, `rebase.updateRefs`); `--exec` runs commands."""
    for arg in args:
        if arg.startswith(("--exec", "--update-refs")) or "x" in _short_cluster(arg):
            return [UNKNOWN_REF]
    # The branch operand is a destination like any named ref (`git rebase --onto X --root
    # forge/issue-7` from main rewrites issue 7's branch); unknowable operands block.
    branch = _rebase_branch(args)
    if branch == UNKNOWN_REF:
        return [UNKNOWN_REF]
    named = [_branch_name(branch)] if branch else []
    if "--no-update-refs" in args:
        return named
    return named + [r for r in _rebase_update_refs(where, target, unsafe) if r not in named]


def _rebase_update_refs(where: str, target: Target, unsafe: bool) -> list[str]:
    if unsafe:
        return [UNKNOWN_REF]
    code, out = _git_config(where, target, "--type=bool", "--get", "rebase.updateRefs")
    if code == 1 or (code == 0 and out.strip() == "false"):
        return []
    return [UNKNOWN_REF]


def _worktree_refs(args: list[str]) -> list[str]:
    """`git worktree add [-b|-B NEW] PATH [COMMIT]`: NEW, else the branch it checks out or
    DWIM-creates (PATH's basename, or COMMIT)."""
    if not args or args[0] != "add":
        return []
    rest, positionals, refs, detach = list(args[1:]), [], [], False
    while rest:
        arg = rest.pop(0)
        if arg == "--":
            positionals += rest
            break
        option, eq, value = arg.partition("=")
        if option in ("-b", "-B"):
            refs.append(value if eq else (rest.pop(0) if rest else ""))
        elif arg[:2] in ("-b", "-B") and len(arg) > 2:
            refs.append(arg[2:])
        elif option == "--reason":
            if not eq and rest:
                rest.pop(0)
        elif arg in ("--detach", "-d"):
            detach = True
        elif not arg.startswith("-"):
            positionals.append(arg)
    if not refs and not detach:
        if len(positionals) == 1:
            refs.append(os.path.basename(positionals[0].rstrip("/")))
        elif len(positionals) > 1:
            refs.append(positionals[1])
    return [_branch_name(ref) for ref in refs if ref]


def _git_sub_args(words: list[str]) -> tuple[str | None, list[str]]:
    """`git [global options] SUB ARGS...` -> (SUB, ARGS)."""
    rest = list(words[1:])
    while rest:
        word = rest.pop(0)
        if word in GIT_VALUE_OPTS:
            if rest:
                rest.pop(0)
            continue
        if word.startswith("-"):
            continue
        return word, rest
    return None, []


def _branch_exists(branch: str, where: str, target: Target) -> bool:
    """Is `branch` a local branch of the repository `target` acts on from `where`?"""
    _, opts, env = target
    try:
        subprocess.run(
            ["git", *opts, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=where,
            env={**os.environ, **_real_env(dict(env))} if env else None,
            capture_output=True,
            check=True,
            timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False  # not provably a plain switch: the command stays guarded
    return True


def _switch_target(sub: str, args: list[str]) -> tuple[str, str | None]:
    """How `git checkout|switch ARGS` moves HEAD: ("none", None) when it does not (a path
    checkout), ("create", X) / ("detach", "HEAD") / ("branch", X) when the destination is a
    literal, ("unknown", None) otherwise. ("branch", X) may still be a path (`git checkout
    file`) or a remote branch to create; the caller tells a pure switch by X existing locally."""
    if any(_dynamic_ref(a) for a in args):
        return "unknown", None
    create = CHECKOUT_CREATE_OPTS if sub == "checkout" else SWITCH_CREATE_OPTS
    rest, positionals, detach = list(args), [], False
    while rest:
        arg = rest.pop(0)
        if arg == "--":
            if sub == "checkout" and rest:
                return "none", None  # `git checkout [tree] -- paths`
            break
        option, eq, value = arg.partition("=")
        if option in create:
            value = value if eq else (rest.pop(0) if rest else "")
            return ("create", value) if value else ("unknown", None)
        if arg[:2] in create and not arg.startswith("--"):
            return "create", arg[2:]  # `-bX`
        if arg == "--detach" or (sub == "switch" and arg == "-d"):
            detach = True
        elif arg in ("-p", "--patch") or arg.startswith("--pathspec-from-file"):
            return "none", None
        elif not arg.startswith("-"):
            positionals.append(arg)
    if detach:
        return "detach", "HEAD"
    if sub == "checkout" and len(positionals) != 1:
        return "none", None  # `git checkout` (no-op) or `git checkout TREE PATH...`
    if not positionals:
        return "none", None
    return "branch", positionals[0]


def _dynamic_ref(arg: str) -> bool:
    """An argument whose branch only the shell or git knows: `$B`, `{a,b}`, `-`, `@{-1}`."""
    return (
        bool(_EXPANSION.search(arg) or _BRACE_EXPANSION.search(arg)) or arg == "-" or ("@{" in arg)
    )


def _rebase_operands(args: list[str]) -> tuple[bool, bool, list[str]] | None:
    """(--root given, an action form, the positional operands) of `git rebase ARGS`, or None
    when they cannot be told apart from option values (see REBASE_LONG_VALUE)."""
    rest, positionals, root, action = list(args), [], False, False
    while rest:
        arg = rest.pop(0)
        if arg == "--":
            positionals += rest
            break
        if arg.startswith("--"):
            option, eq, _ = arg.partition("=")
            if option in REBASE_LONG_VALUE:
                if not eq:
                    if not rest:
                        return None
                    rest.pop(0)
            elif option in REBASE_LONG_OPTIONAL:
                pass  # its value is attached or absent
            elif option in REBASE_LONG_FLAGS and not eq:
                root = root or option == "--root"
            elif option in REBASE_ACTIONS and not eq:
                action = True
            else:
                return None
        elif arg.startswith("-") and len(arg) > 1:
            letters = arg[1:]
            for index, letter in enumerate(letters):
                if letter in REBASE_SHORT_FLAGS:
                    continue
                if letter in REBASE_SHORT_OPTIONAL:
                    break  # the rest of the cluster is its value
                if letter in REBASE_SHORT_VALUE:
                    if index == len(letters) - 1:
                        if not rest:
                            return None
                        rest.pop(0)
                    break
                return None
        else:
            positionals.append(arg)
    return root, action, positionals


def _rebase_branch(args: list[str]) -> str | None:
    """The branch `git rebase` checks out and rewrites when it names one, None when it names
    none (it rebases the current branch), UNKNOWN_REF when that cannot be known. The forms
    (Codex 4239965192): `[UPSTREAM [BRANCH]]` -- with or without `--onto X`, `--keep-base`,
    `--fork-point`, `-i` -- and `--root [BRANCH]`, whose ONLY operand is the branch."""
    parsed = _rebase_operands(args)
    if parsed is None:
        return UNKNOWN_REF
    root, action, positionals = parsed
    if action:
        return UNKNOWN_REF if positionals else None
    limit = 1 if root else 2
    if len(positionals) > limit:
        return UNKNOWN_REF  # git refuses too many operands; never guess which one is the branch
    return positionals[limit - 1] if len(positionals) == limit else None


def _segment_switch(seg: list[str], depth: int, cwd: str | None = None) -> tuple[str, str | None]:
    """How one simple command can move HEAD (see the block comment above): ("none", None),
    ("unknown", None), or a checkout/switch/rebase/branch-rename shape for `_switch_target`'s
    kinds plus ("maybe", X) for a destination X that may or may not be taken."""
    if seg[0] in ("pushd", "popd"):
        return "unknown", None
    if seg[0] == "cd":
        operands = [w for w in seg[1:] if not w.startswith("-") or w == "-"]
        dynamic = any(w == "-" or _EXPANSION.search(w) for w in operands)
        return ("unknown", None) if dynamic else ("none", None)
    if seg[0] in SET_BUILTINS or all(_ASSIGNMENT.match(w) for w in seg):
        if any(_GIT_REPO_VAR.match(w) for w in seg[1 if seg[0] in SET_BUILTINS else 0 :]):
            return "unknown", None
        return "none", None
    chdirs: list[str] = []
    words = _command_words(seg, chdirs, [])
    if not words:
        return "none", None
    program = os.path.basename(words[0])
    if program in _EVAL_LIKE:
        return "unknown", None  # `source x.sh` runs in THIS shell: its `cd`s and exports stay
    where = _where(cwd, chdirs)
    inner = _shell_command_string(words)
    if inner is not None:
        return "unknown" if depth >= MAX_SUBST_DEPTH or _changes_branch(
            inner, depth + 1, where
        ) else ("none"), None
    script = _script_invocation(words, where)
    if script is not None:  # a child shell running a script file: what its git does
        return ("unknown" if _script_changes_branch(script, depth, where) else "none"), None
    if program in SHELLS:
        return "unknown", None  # stdin: anything
    if program not in GIT_PROGRAMS:
        if program.startswith("git-"):
            return "unknown", None  # `git-checkout`, `git-switch`
        args = " ".join(_strip_substitutions(w) for w in words[1:])
        if program in FANOUT and (
            _mentions_git(args)
            or any(w in SHELLS or w.endswith(SCRIPT_SUFFIXES) for w in words[1:])
        ):
            return "unknown", None  # `xargs git` / `xargs sh`: the subcommand comes from stdin
        if _mentions_git(args) and _MOVES_HEAD.search(args):
            return "unknown", None  # `python -c "...['git', 'checkout', b]..."`
        return "none", None
    sub, args = _git_sub_args(words)
    if sub is None or sub in READ_ONLY_ANY or sub in SWITCH_FREE:
        return "none", None
    if sub in READ_ONLY_SHAPED and sub != "branch":
        return "none", None
    if sub in ("checkout", "switch"):
        return _switch_target(sub, args)
    if sub == "rebase":
        if any(_EXPANSION.search(a) for a in args):
            return "unknown", None
        branch = _rebase_branch(args)
        if branch == UNKNOWN_REF:
            return "unknown", None
        return ("maybe", branch) if branch else ("none", None)
    if sub == "branch":
        if not any(a in ("-m", "-M", "--move") for a in args):
            return "none", None
        positionals = [a for a in args if not a.startswith("-")]
        if not positionals or any(_EXPANSION.search(a) for a in positionals):
            return "unknown", None
        return "maybe", positionals[-1]  # `git branch -m NEW` renames the current branch
    return "unknown", None  # bisect, symbolic-ref, an alias, an unknown subcommand


def _changes_branch(command: str, depth: int, cwd: str | None = None) -> bool:
    """Could any simple command (or substitution body) in `command` move HEAD?"""
    try:
        for seg in _segments(command):
            while seg and seg[0] in SHELL_KEYWORDS:
                seg = seg[1:]
            if not seg:
                continue
            if _segment_switch(seg, depth, cwd)[0] != "none":
                return True
            for body in _substitutions(" ".join(seg)):
                if depth >= MAX_SUBST_DEPTH or _changes_branch(body, depth + 1, cwd):
                    return True
    except ValueError:
        return _mentions_git(command)
    return False


def _with_branch(target: Target, branch: str) -> Target:
    dirs, opts, env = target
    env = tuple(item for item in env if item[0] != _BRANCH_KEY)
    return (dirs, opts, (*env, (_BRANCH_KEY, branch)))


def _retarget(targets: list[Target], state) -> list[Target]:
    """`targets` after a branch change `state` = (location, branch, replace) or UNKNOWN_BRANCH."""
    if state is None:
        return targets
    if state == UNKNOWN_BRANCH:
        return [_with_branch(t, UNKNOWN_BRANCH) for t in targets]
    location, branch, replace = state
    out: list[Target] = []
    for target in targets:
        if not (replace and target == location):
            out.append(target)
        out.append(_with_branch(target, branch))
    return out


def commit_targets(command: str, depth: int = 0, cwd: str | None = None) -> list[Target]:
    """Every guarded git invocation in `command` (see the module docstring) with the repository
    it acts on. `cd DIR` before it (`cd ../wt && git merge x`) counts too, and so does a branch
    change before it (`git switch X && git commit`, see UNKNOWN_BRANCH). Deny by default: any
    git invocation not proven read-only, and anything that runs git or a shell indirectly, is
    one; an assembled program or subcommand is one with an unknown repository."""
    if depth == 0:
        _HELPER_NOTES.clear()
        unquoted = re.sub(r"[\"'\\]", "", command)
        tainted = _mentions_git(command) and bool(
            _ENV_TAINT.search(unquoted) or _PATH_TAINT.search(unquoted)
        )
        _TAINTED[:] = [True] if tainted else []
        _PATH_SEARCH[:] = [_path_searches(command, cwd or os.getcwd())]
        _EXEC_CACHE.clear()
        _FILE_SCAN.clear()
        try:
            return _commit_targets_top(command, cwd, tainted)
        finally:
            _TAINTED.clear()
            _PATH_SEARCH.clear()
    return _commit_targets_in(command, depth, cwd)


def _commit_targets_top(command: str, cwd: str | None, tainted: bool) -> list[Target]:
    found = _commit_targets_in(command, 0, cwd)
    if _raw_unproven(command):
        # Codex 4240170620: an ASSEMBLED program or subcommand (`G=git; "$G" -C DIR commit`,
        # `$(which git)`, `${GIT:-git}`, `\git`, `"git"`, `$HOME/bin/git`): what the shell runs,
        # and with which `-C`/`--git-dir`/GIT_DIR selectors, cannot be known -- unresolved, never
        # the current checkout with the selectors dropped.
        found.append(_unresolved())
        found += _mentioned_refs(((), (), ()), command)  # every issue ref it names, too
    if tainted:
        # git run under an exported GIT_*/PAGER/EDITOR setting: every git invocation was
        # already guarded in the repository its own selectors name (`tainted` above); every
        # `cd` prefix is checked too.
        cds = _cd_words(command)
        found += [(tuple(cds[:count]), (), ()) for count in range(len(cds) + 1)]
        found += _mentioned_refs(((), (), ()), command)
    return found


def _cd_words(command: str) -> list[str]:
    """The directory of every `cd` in `command`, in order (as `_commit_targets_in` reads them)."""
    cds: list[str] = []
    for _, seg in _segments_with_ops(command):
        while seg and seg[0] in SHELL_KEYWORDS:
            seg = seg[1:]
        if seg and seg[0] == "cd":
            operands = [w for w in seg[1:] if not w.startswith("-") or w == "-"]
            cds.append(operands[-1] if operands else "~")
    return cds


def _commit_targets_in(command: str, depth: int, cwd: str | None) -> list[Target]:
    found: list[Target] = []
    cds: list[str] = []
    state = None  # the branch change in effect: None, UNKNOWN_BRANCH or (location, X, replace)
    for ops, seg in _segments_with_ops(command):
        while seg and seg[0] in SHELL_KEYWORDS:
            seg = seg[1:]
        if not seg:
            continue
        if isinstance(state, tuple) and any(op != "&&" for op in ops):
            state = (state[0], state[1], False)  # the switch may not have happened
        where = _where(cwd, cds)
        is_cd = seg[0] == "cd"
        if is_cd:  # changes where later commands run; its own substitutions run here
            operands = [w for w in seg[1:] if not w.startswith("-") or w == "-"]
            cds.append(operands[-1] if operands else "~")
        prefix = cds[:-1] if is_cd else cds
        own = [] if is_cd else _commit_targets(seg, depth, where)
        own = [((*prefix, *dirs), opts, env) for dirs, opts, env in own]
        subst: list[Target] = []
        # over the joined words: shlex splits an unquoted `cd \`git commit\`` across words
        for body in _substitutions(" ".join(seg)):
            if depth >= MAX_SUBST_DEPTH:
                subst += [_unresolved()] if _mentions_git(body) else []
                if _mentions_git(body):
                    state = UNKNOWN_BRANCH
                continue
            try:  # a substitution runs where the command runs: same `cd`s
                subst += commit_targets(body, depth + 1, where)
            except ValueError:
                subst += [_unresolved()] if _mentions_git(body) else []
            if _changes_branch(body, depth + 1, where):
                state = UNKNOWN_BRANCH  # it ran before this command did
        found += _retarget([((*prefix, *dirs), o, e) for dirs, o, e in subst], state)
        kind, branch = _segment_switch(seg, depth, where)
        location = own[0] if own else (tuple(prefix), (), ())
        if kind in ("branch", "create", "detach") and own and branch:
            # `pure`: certainly lands on `branch` -- a creation, a detach, or a plain switch to a
            # branch that exists (`git checkout NAME` may also be a path, or a DWIM create)
            pure = kind != "branch" or (
                {a for a in seg if a.startswith("-")} <= QUIET_OPTS
                and _branch_exists(branch, _where(cwd, location[0]), location)
            )
            if kind == "branch" and pure:
                own = []  # a pure switch to an existing branch rewrites no history
            elif kind == "create":
                own = [_with_branch(t, branch) for t in own]  # it creates `branch`
            elif kind == "branch":
                own = own + [_with_branch(t, branch) for t in own]  # path, or DWIM create
            found += _retarget(own, state)
            if state != UNKNOWN_BRANCH:
                state = (location, branch, pure)
            continue
        if kind == "maybe" and branch:
            found += _retarget(own + [_with_branch(t, branch) for t in own], state)
            if state != UNKNOWN_BRANCH:
                state = (location, branch, False)
            continue
        found += _retarget(own, state)
        if kind != "none":
            state = UNKNOWN_BRANCH
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


# ---- Repository selectors (Codex 4239868022). The branch a guarded command acts on is asked of
# git in the repository the command selects: `cd DIR`, `env -C DIR`, `git -C DIR`, `--git-dir`,
# `--work-tree`, GIT_DIR=/GIT_WORK_TREE= prefixes. A selector the shell rewrites before git sees it
# (`"$DIR"`, `$(pwd)`, backticks, a glob, a brace expansion, a `~` the shell may or may not
# expand) cannot be resolved here: the probe would ask git about the literal text, fail, and a
# failure that read as "no branch" let `DIR=wt; git -C "$DIR" commit` into an unleased issue
# checkout. Such a target is UNRESOLVED_REPO and blocks; a probe that fails for any reason other
# than "there is no repository there" is BRANCH_UNKNOWN and blocks too.
UNRESOLVED_REPO = "\0unresolved-repo"
_UNRESOLVED_KEY = "\0unresolved"  # a Target env entry: its repository cannot be known at all
_TAINTED: list[bool] = []  # set while a top-level command carries an `_ENV_TAINT` assignment
BRANCH_UNKNOWN = "\0branch-unknown"  # git could not report the branch: unknown, never "none"
_SELECTOR_EXPANSION = re.compile(r"[$`*?\[]")
_QUOTED_TILDE = re.compile(r"[\"'\\]~")


def _unresolved(chdirs: tuple[str, ...] = ()) -> Target:
    """A guarded git invocation whose repository cannot be known before the shell runs."""
    return (chdirs, (), ((_UNRESOLVED_KEY, "1"),))


def _unresolvable(value: str, word_start: bool, quoted_tilde: bool) -> bool:
    """Can the shell turn this selector value into something else before git sees it?
    `word_start`: the value is a whole shell word (or an assignment's value), where the shell
    expands a leading `~` (resolved here with expanduser) -- unless the command also quotes or
    escapes a `~`, when which one is literal cannot be told from the cooked words."""
    if _SELECTOR_EXPANSION.search(value) or _BRACE_EXPANSION.search(value):
        return True
    if "~" not in value:
        return False
    return not word_start or not value.startswith("~") or "~" in value[1:] or quoted_tilde


def _selector_values(target: Target) -> list[tuple[str, bool]]:
    """(value, whole word) for every repository selector of `target`."""
    chdirs, opts, env = target
    values = [(d, True) for d in chdirs if d != "-"]
    index = 0
    while index < len(opts):
        opt = opts[index]
        if opt in GIT_REPO_OPTS and index + 1 < len(opts):
            values.append((opts[index + 1], True))
            index += 2
            continue
        values.append((opt.partition("=")[2], False))  # `--git-dir=X`: no tilde expansion
        index += 1
    values += [(value, True) for key, value in env if key in GIT_REPO_ENV]
    return values


def _resolve_opts(opts: tuple[str, ...]) -> tuple[str, ...]:
    """`-C ~/wt` as the shell passes it to git (git itself never expands `~`)."""
    out = list(opts)
    for index in range(1, len(out)):
        if out[index - 1] in GIT_REPO_OPTS and out[index].startswith("~"):
            out[index] = os.path.expanduser(out[index])
    return tuple(out)


def _resolve_env(env: dict[str, str]) -> dict[str, str]:
    """GIT_DIR=~/x as the shell assigns it (a leading `~` of an assignment is expanded)."""
    return {
        key: os.path.expanduser(value) if key in GIT_REPO_ENV else value
        for key, value in env.items()
    }


def target_branch(target: Target, cwd: str | None, quoted_tilde: bool = False) -> str:
    """The branch `git <repo options> ...` acts on, asked of git itself. UNRESOLVED_REPO when a
    repository selector cannot be resolved before the shell runs (Codex 4239868022)."""
    chdirs, opts, env = target
    if _UNRESOLVED_KEY in dict(env):
        return UNRESOLVED_REPO  # an assembled or indirect git (Codex 4240170620)
    named = dict(env).get(_REF_KEY)
    if named is not None:
        return named  # a ref the command names as a destination (Codex 4239397967)
    moved = dict(env).pop(_BRANCH_KEY, None)
    if moved is not None:
        return moved  # a branch change earlier in the command put HEAD there
    if any(_unresolvable(v, whole, quoted_tilde) for v, whole in _selector_values(target)):
        return UNRESOLVED_REPO
    return current_branch(_where(cwd, chdirs), _resolve_opts(opts), _resolve_env(dict(env)))


def commit_branches(command: str, cwd: str | None) -> list[str]:
    """The branches the commits in `command` land on. Each commit is checked both where its
    `cd`s lead and from the hook's cwd (a `cd` inside a subshell does not persist), so the guard
    errs towards checking more, never fewer."""
    try:
        targets = commit_targets(command, cwd=cwd)
    except ValueError:
        # unparseable here but maybe not to the shell (`$'it\\''`): nothing it selects is known
        return [UNRESOLVED_REPO]
    if not targets:
        return [current_branch(cwd)]
    branches: list[str] = []
    quoted_tilde = bool(_QUOTED_TILDE.search(command))
    for target in targets:
        for candidate in (target, ((), target[1], target[2])):
            branch = target_branch(candidate, cwd, quoted_tilde)
            if branch not in branches:
                branches.append(branch)
    return branches


def _real_env(env: dict[str, str]) -> dict[str, str]:
    """`env` without the guard's own bookkeeping entries (`\0branch`, `\0ref`)."""
    return {key: value for key, value in env.items() if not key.startswith("\0")}


def current_branch(
    cwd: str | None, git_opts: tuple[str, ...] = (), env: dict[str, str] | None = None
) -> str:
    """The checked-out branch ("HEAD" when detached). "" ONLY when git positively answers that
    there is no repository there (`git clone`/`git init` from a plain directory: no history to
    protect). Any other failure -- a directory that does not exist, git missing, an unreadable or
    untrusted repository -- is BRANCH_UNKNOWN (Codex 4239868022): a failure that read as "no
    branch" let a command into an issue checkout the probe never saw."""
    run_env = {**os.environ, **_real_env(env or {}), "LC_ALL": "C", "LANGUAGE": ""}

    def probe(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *git_opts, *args],
            cwd=cwd,
            env=run_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_TIMEOUT,
        )

    try:
        proc = probe("rev-parse", "--abbrev-ref", "HEAD")
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
        if "not a git repository" in (proc.stderr or ""):
            return ""
        unborn = probe("symbolic-ref", "--short", "HEAD")  # a repository with no commits yet
        if unborn.returncode == 0 and unborn.stdout.strip():
            return unborn.stdout.strip()
    except subprocess.TimeoutExpired:
        return BRANCH_TIMEOUT  # unknown, not "no branch": refuses (Codex 4239667544)
    except OSError:
        return BRANCH_UNKNOWN
    return BRANCH_UNKNOWN


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
            timeout=GH_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise LeaseUnavailable(f"permission read timed out after {GH_TIMEOUT}s") from exc
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
        if not isinstance(data, dict) or not (data.get("permission") or data.get("role_name")):
            raise LeaseUnavailable("permission response carries no permission")
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
        marker = comment_marker(c.get("body") or "")
        if marker is None or marker[0] == "forge-cleanup":
            continue  # chatter: no permission read needed
        if not trusted_marker_author(c, permission):
            continue  # markers count only from writers and the trusted bots
        kind, fields = marker
        if kind == "forge-claim":
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
        released = fields.get("session")
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
            timeout=GH_TIMEOUT,
        ).stdout
        pages = json.loads(out or "null")
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ):
        return UNAVAILABLE
    # --slurp wraps the pages in one outer array. Any other shape -- a page that is not a list,
    # an item that is not an object -- is an unreadable lease history, never an empty one.
    if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
        return UNAVAILABLE
    comments = [c for page in pages for c in page]
    if not all(isinstance(c, dict) for c in comments):
        return UNAVAILABLE
    try:
        return lease_from_comments(comments, repo_permission)
    except LeaseUnavailable:
        return UNAVAILABLE


def _same_repository_pr(pr: object, branch: str) -> bool:
    """True only for an open PR (REST `pulls` shape) whose head is `branch` IN PROJECT itself: a
    fork's PR from an identically named branch is not this branch's claim. A missing or malformed
    field proves nothing and is no claim."""
    if not isinstance(pr, dict) or pr.get("state") != "open":
        return False
    head = pr.get("head")
    repo = head.get("repo") if isinstance(head, dict) else None
    name = repo.get("full_name") if isinstance(repo, dict) else None
    if not isinstance(name, str) or not name or head.get("ref") != branch:
        return False
    return name.lower() == PROJECT.lower()


def open_pr_for(branch: str) -> bool | None:
    """An open PR from this branch OF THIS REPOSITORY is the durable claim once the lease is
    released (a fork PR with the same head branch name is not). None when the PR list could
    not be read.

    Codex 4240421140: `gh pr list --head` fetched 30 PRs and filtered by branch NAME only, so
    more than 30 fork PRs from an identically named branch could push the same-repository one
    out of the answer. The REST `pulls` list takes `head=OWNER:BRANCH`, which names the head
    repository's owner -- and so this repository's own branch -- directly; every page is read
    (`--paginate --slurp`) and each PR is still checked to be this repository's."""
    from urllib.parse import quote

    owner = PROJECT.split("/", 1)[0]
    head = quote(f"{owner}:{branch}", safe="")
    try:
        out = subprocess.run(
            [
                "gh",
                "api",
                f"repos/{PROJECT}/pulls?state=open&head={head}&per_page=100",
                "--paginate",
                "--slurp",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=GH_TIMEOUT,
        ).stdout
        pages = json.loads(out or "null")
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ):
        return None
    # --slurp wraps the pages in one outer array; any other shape is unreadable, never "no PR".
    if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
        return None
    return any(_same_repository_pr(pr, branch) for page in pages for pr in page)


def payload_session(payload: dict) -> str:
    """The hook payload's session id, or "" when it is missing, null, empty, blank or not a
    string. Never stringified: a non-string id (a number, a list, ...) is no identity at all and
    must not be able to equal a lease's session text (Codex 4240101138)."""
    session = payload.get("session_id") if isinstance(payload, dict) else None
    if not isinstance(session, str) or not session.strip():
        return ""
    return session


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
    candidates = [branch] if isinstance(branch, str) else list(branch)
    # the "run them separately" advice first: it is the one that unblocks a mixed command
    for candidate in sorted(candidates, key=lambda b: b != UNKNOWN_BRANCH):
        verdict = _decide_branch(payload, candidate, lease_lookup, pr_lookup)
        if verdict[0]:
            return verdict
    return 0, ""


def _decide_branch(payload: dict, branch: str, lease_lookup, pr_lookup) -> tuple[int, str]:
    if branch == UNKNOWN_REF:
        return 2, (
            "Forge guard: this git command can update refs that cannot be known before it runs "
            "(--all/--mirror/--prune, a glob or `:` refspec, a push whose destinations come from "
            "configuration -- remote.*.push, push.default upstream/matching --, an alias to a "
            "shell command or an external git-<name> program, rebase --exec/--update-refs, "
            "update-ref --stdin, xargs/eval feeding git its arguments, a computed ref name, ...). "
            "Name every destination explicitly (git push origin HEAD:<branch>) so the guard can "
            "check the lease of each issue branch it touches."
        )
    if branch == UNKNOWN_BRANCH:
        return 2, (
            "Forge guard: this command can move HEAD to a branch that cannot be known before it "
            "runs (a branch change with a computed name, `-`, an alias, bisect, a sourced or "
            "piped script, a `cd` to an expanded path, ...) and then runs git that needs the "
            "issue lease. Run the branch change and the git command as SEPARATE commands so the "
            "guard can check the branch the second one acts on."
        )
    if branch == UNRESOLVED_REPO:
        return 2, (
            "Forge guard: this git command selects its repository through text the shell "
            'expands before git runs (`git -C "$DIR"`, `--git-dir=$X`, `--work-tree`, '
            "GIT_DIR=/GIT_WORK_TREE= set to an expansion, `cd $DIR`, `$(...)`, backticks, a glob "
            "or an ambiguous `~`), or runs git through a name or program the guard cannot read "
            '(`"$G"`, `$(which git)`, `${GIT:-git}`, `\\git`, a quoted `"git"`, xargs/find/eval, '
            "a script piped to a shell, an interpreter's code that selects a repository), or "
            "runs a shell script file the guard could not read and analyse (missing, not a "
            f"regular file, binary, over {MAX_SCRIPT_BYTES // 1024} KiB, unparseable, or scripts "
            f"nested more than {MAX_SUBST_DEPTH} deep), so the branch it acts on -- and the lease "
            "it needs -- cannot be checked. Spell the program as a plain `git` and the repository "
            "as a literal path (git -C /path/to/checkout commit), or cd there in a separate "
            "command first; run git inline rather than from an unreadable script."
        )
    if branch == BRANCH_UNKNOWN:
        return 2, (
            "Forge guard: git could not report the branch of the repository this command acts on "
            "(a directory that does not exist yet, an unreadable or untrusted repository, git "
            "unavailable), so the lease it needs cannot be checked. Create or enter the "
            "directory in a separate command first, then run the git command on its own."
        )
    if branch == BRANCH_TIMEOUT:
        return 2, (
            f"Forge guard: git did not report the checked-out branch within {GIT_TIMEOUT}s, so "
            "the lease this command needs cannot be checked. Retry once git responds."
        )
    m = ISSUE_BRANCH.search(branch or "")
    if not m:
        return 0, ""
    issue = int(m.group(1))
    session = payload_session(payload)
    lease = lease_lookup(issue)
    if lease_unavailable(lease):
        return 2, (
            f"Forge guard: the lease of issue #{issue} could not be read from GitHub "
            "(authentication, network or API failure). Lease inventory unavailable -- do not "
            "commit issue work until it can be checked; retry once `gh` works."
        ) + _helper_note()
    has_pr = pr_lookup(branch) if lease is None else False
    if lease is None and has_pr:
        return 0, ""  # follow-up commits to an open PR need no lease
    if lease is None and has_pr is None:
        return 2, (
            f"Forge guard: branch '{branch}' targets issue #{issue}, it has no live lease and "
            "its open PRs could not be read from GitHub (no PR could be confirmed). Claim it: "
            f"sdlcctl claim --project {PROJECT} --issue {issue} --agent claude-code "
            f"--session {session or '<session_id>'} --branch {branch}"
        ) + _helper_note()
    if lease is None:
        return 2, (
            f"Forge guard: branch '{branch}' targets issue #{issue} but no live lease exists. "
            f"Claim it first: sdlcctl claim --project {PROJECT} --issue {issue} "
            f"--agent claude-code --session {session or '<session_id>'} --branch {branch}"
        ) + _helper_note()
    if not session:
        # Codex 4240101138: without a usable session id no lease can be shown to be this
        # session's, so a live lease is never taken as ours (fail closed).
        return 2, (
            f"Forge guard: issue #{issue} is leased by agent={lease.get('agent')} "
            f"session={lease.get('session')} until {lease['expires'].isoformat()}, and this "
            "hook payload carries no usable session_id (missing, null, empty or not a string), "
            "so the lease cannot be confirmed as this session's. Do not commit to it; restart "
            "the session so Claude Code supplies its session id, then retry."
        ) + _helper_note()
    if lease.get("session") != session:
        return 2, (
            f"Forge guard: issue #{issue} is leased by agent={lease.get('agent')} "
            f"session={lease.get('session')} until {lease['expires'].isoformat()}. "
            "Do not commit to it; pick another issue or wait for the lease to expire."
        ) + _helper_note()
    return 0, ""


PAYLOAD_BLOCK = (
    "Forge guard: the PreToolUse hook payload {why}, so whether this tool call runs a git "
    "command on an issue branch -- and the lease it would need -- cannot be checked. Blocked "
    "(fail closed). Retry the command; if this persists, the installed Claude Code sends a hook "
    "payload this guard does not understand -- update Claude Code or re-run `sdlcctl onboard`."
)


def read_payload(text: str) -> tuple[dict | None, str]:
    """The hook payload as (payload, "") when it is a JSON object that is either a Bash call
    with a string `tool_input.command` (and a string `cwd`, if any) or clearly another tool;
    otherwise (None, why). Only a payload that parses AND has that shape may be let through:
    anything else (empty, truncated, not JSON, not an object, no tool name, a Bash call
    without a command) blocks (Codex 4240306495)."""
    try:
        payload = json.loads(text)
    except ValueError:
        return None, "is not valid JSON (empty, truncated or corrupted)"
    if not isinstance(payload, dict):
        return None, "is not a JSON object"
    tool = payload.get("tool_name")
    if not isinstance(tool, str) or not tool:
        return None, "names no tool (tool_name missing or not a string)"
    if tool != "Bash":
        return payload, ""  # clearly not the guarded tool
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict) or not isinstance(tool_input.get("command"), str):
        return None, "is a Bash call without a string tool_input.command"
    if payload.get("cwd") is not None and not isinstance(payload.get("cwd"), str):
        return None, "carries a cwd that is not a string"
    return payload, ""


def main() -> int:
    try:
        text = sys.stdin.read()
    except (OSError, ValueError):  # unreadable or not text
        text = ""
    payload, why = read_payload(text)
    if payload is None:
        print(PAYLOAD_BLOCK.format(why=why), file=sys.stderr)
        return 2
    if payload.get("tool_name") != "Bash":
        return 0
    command = payload["tool_input"]["command"]
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
