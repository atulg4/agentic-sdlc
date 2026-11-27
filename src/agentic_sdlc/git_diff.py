"""Collect a proposed Git patch without unsafe filename splitting."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .trusted_git import UntrustedGitError, trusted_git


class GitDiffError(RuntimeError):
    """Raised when a diff cannot be collected."""


@dataclass(frozen=True)
class GitDiff:
    paths: tuple[str, ...]
    added_lines: int
    deleted_lines: int
    patch: bytes
    #: The endpoint diff's own paths, when `paths` also holds those of intermediate commits
    #: (`collect_commit_diff`); None when `paths` is the endpoint diff alone.
    net_paths: tuple[str, ...] | None = None
    #: `collect_commit_diff` only (Codex 4242095558): the SUM over every commit in the series of
    #: that commit's own added / deleted lines and `--binary` patch bytes. None when the diff is
    #: the endpoint alone (`collect_git_diff`).
    history_added_lines: int | None = None
    history_deleted_lines: int | None = None
    history_patch_bytes: int | None = None

    @property
    def cap_added_lines(self) -> int:
        """The added lines the size caps are applied to: the larger of the end state's and the
        whole history's (see `collect_commit_diff`)."""
        return max(self.added_lines, self.history_added_lines or 0)

    @property
    def cap_deleted_lines(self) -> int:
        return max(self.deleted_lines, self.history_deleted_lines or 0)

    @property
    def cap_patch_bytes(self) -> int:
        return max(len(self.patch), self.history_patch_bytes or 0)


#: A file that never exists: pointing GIT_GRAFT_FILE at it disables `.git/info/grafts`.
_NO_GRAFTS = os.devnull + ".no-grafts"


#: The fixed search path every trusted program runs with: system directories only, so nothing a
#: caller put on PATH runs as a helper of the trusted git (or gh).
SAFE_PATH = "/usr/bin:/bin"


def _object_env() -> dict[str, str]:
    """The environment every git run here gets: BUILT from an allowlist, never inherited
    (Codex 4241839908). Inheriting all but `GIT_*` (Codex 4241745395) still handed git the
    dynamic loader's variables -- `LD_PRELOAD`, `LD_LIBRARY_PATH`, `LD_AUDIT`, `DYLD_*` -- which
    load code of the caller's choosing into the trusted git, and a caller PATH for the helpers
    git spawns. No variable of the caller's survives: `GIT_DIR`, `GIT_WORK_TREE`,
    `GIT_COMMON_DIR`, `GIT_INDEX_FILE`, `GIT_OBJECT_DIRECTORY`,
    `GIT_ALTERNATE_OBJECT_DIRECTORIES`, `GIT_NAMESPACE`, `GIT_CONFIG*`, `GIT_EXEC_PATH`, ... each
    re-points what git reads, so a guard could inspect a benign repository while a later plain
    push publishes the real one. The set below is the whole of git's environment:

    - a fixed system PATH, the C locale, and no HOME (no per-user configuration or attributes);
    - history as the object store holds it: git otherwise substitutes `refs/replace/*` objects
      and grafts when it reads a commit or tree, while a push publishes the ORIGINAL objects
      (Codex 4241532610);
    - no system or global configuration: only the repository's own (and the `-c` overrides
      `_run` passes) applies;
    - never a prompt."""
    return {
        "PATH": SAFE_PATH,
        "LC_ALL": "C",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_GRAFT_FILE": _NO_GRAFTS,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }


@dataclass(frozen=True)
class _Repository:
    """A repository resolved once, from its real path, with a clean environment; every later
    git run is bound to it explicitly (`--git-dir`, `--work-tree`)."""

    git: str
    git_dir: str
    top: str


def _git_program() -> str:
    # Never a bare `git`: a PATH lookup could find one this session planted to fabricate the
    # output a policy check reads (Codex 4241614592). No trusted git: fail closed.
    try:
        return trusted_git()
    except UntrustedGitError as exc:
        raise GitDiffError(str(exc)) from exc


def _exec(
    arguments: list[str], cwd: str, label: str, stdin: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        arguments,
        cwd=cwd,
        env=_object_env(),
        capture_output=True,
        check=False,
        timeout=120,
        input=stdin,
    )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace")[-1000:]
        raise GitDiffError(f"git {label} failed: {message}")
    return result


def _bind(repository: str | Path) -> _Repository:
    """The repository at `repository` -- its top level, which `repository` must BE -- found by
    git's own discovery from that directory with every `GIT_*` variable removed, so nothing in
    the caller's environment can substitute another repository (Codex 4241745395)."""
    program = _git_program()
    where = os.path.realpath(repository)
    if not os.path.isdir(where):
        raise GitDiffError(f"{repository} is not a directory")
    out = _exec(
        [program, "-C", where, "rev-parse", "--absolute-git-dir", "--show-toplevel"],
        where,
        "rev-parse",
    ).stdout.decode("utf-8", errors="surrogateescape")
    lines = out.split("\n")
    if len(lines) != 3 or lines[2] or not lines[0] or not lines[1]:
        raise GitDiffError(f"{repository} is not the top level of a git work tree")
    git_dir, top = lines[0], lines[1]
    if os.path.realpath(top) != where:
        raise GitDiffError(
            f"{repository} is not the top level of its git work tree ({top}); pass the top "
            "level as the repository"
        )
    return _Repository(git=program, git_dir=os.path.realpath(git_dir), top=where)


def _run(
    arguments: list[str],
    repository: str | Path | _Repository,
    stdin: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    if arguments[:1] != ["git"]:
        raise GitDiffError(f"not a git command: {arguments[:1]!r}")
    repo = repository if isinstance(repository, _Repository) else _bind(repository)
    # `--no-replace-objects` on the command line as well as in the environment: either alone
    # disables replacement; both together survive a caller that rewrites one of them. The
    # repository is named explicitly on every run, never discovered from the environment, and
    # no fsmonitor program from the repository's config runs.
    subcommand = arguments[1:]
    if subcommand[:1] == ["diff"]:
        # The repository's own config is the agent's to write: `diff.ignoreSubmodules=all` (or a
        # `submodule.<name>.ignore`) would hide a gitlink change from the listing while the
        # push publishes it. The command line outranks every config file.
        subcommand = ["diff", "--ignore-submodules=none", *subcommand[1:]]
    command = [
        repo.git,
        "--no-replace-objects",
        f"--git-dir={repo.git_dir}",
        f"--work-tree={repo.top}",
        "-c",
        "core.fsmonitor=false",
        *subcommand,
    ]
    return _exec(command, repo.top, arguments[1] if len(arguments) > 1 else "", stdin=stdin)


def _decode_path(value: bytes) -> str:
    return value.decode("utf-8", errors="surrogateescape")


#: Every `--name-status` letter a tree, index or working-tree diff can report for a changed path
#: (Codex 4241745397): Added, Copied, Deleted, Modified, Renamed, Type changed (file <->
#: symlink <-> submodule). Unmerged (U), unknown (X) or anything else is an error, never a path
#: that is silently left out of the policy check.
_KNOWN_STATUS = re.compile(rb"[ACDMRT][0-9]*")


def _paths_from_name_status(raw: bytes) -> tuple[str, ...]:
    """Every path the diff touches, BOTH sides of a rename or copy. No `--diff-filter` limits
    the listing: a filter that leaves out one status (it once dropped `T`) lets that change ride
    the patch past the forbidden-path check."""
    fields = raw.split(b"\0")
    paths: set[str] = set()
    index = 0
    while index < len(fields) and fields[index]:
        status = fields[index]
        index += 1
        if not _KNOWN_STATUS.fullmatch(status):
            raise GitDiffError(
                f"git reported a change of unsupported status {_decode_path(status)!r}; "
                "not concluding"
            )
        path_count = 2 if status[:1] in {b"R", b"C"} else 1
        if index + path_count > len(fields) or any(
            not value for value in fields[index : index + path_count]
        ):
            raise GitDiffError("git name-status output is malformed")
        paths.update(_decode_path(value) for value in fields[index : index + path_count])
        index += path_count
    if index != len(fields) - 1 or fields[index]:
        raise GitDiffError("git name-status output is malformed")
    return tuple(sorted(paths))


def collect_git_diff(repository: str | Path, base: str = "HEAD") -> GitDiff:
    root = _bind(repository)
    _run(["git", "add", "-N", "--", "."], root)

    names = _run(
        [
            "git",
            "diff",
            "--name-status",
            "-z",
            "--find-renames",
            base,
            "--",
        ],
        root,
    ).stdout
    paths = _paths_from_name_status(names)

    raw_stats = _run(["git", "diff", "--numstat", "-z", base, "--"], root).stdout
    added = 0
    deleted = 0
    for match in re.finditer(rb"(?:^|\x00)(\d+|-)\t(\d+|-)\t", raw_stats):
        added += int(match.group(1)) if match.group(1).isdigit() else 0
        deleted += int(match.group(2)) if match.group(2).isdigit() else 0

    patch = _run(["git", "diff", "--binary", "--full-index", base, "--"], root).stdout
    return GitDiff(paths=paths, added_lines=added, deleted_lines=deleted, patch=patch)


#: A full object name, as `rev-list` prints it.
_OBJECT_NAME = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


@dataclass(frozen=True)
class _History:
    """What the commits of a series publish, commit by commit (`_every_commit`)."""

    paths: set[str]
    added_lines: int
    deleted_lines: int
    patch_bytes: int


#: Options every per-commit diff here runs with: both sides of a rename as their own deletion
#: and addition, no external diff or textconv program, every submodule change shown.
_PLAIN_TREE_DIFF = (
    "-r",
    "-z",
    "--no-commit-id",
    "--no-renames",
    "--no-ext-diff",
    "--no-textconv",
    "--ignore-submodules=none",
)


def _blob_sizes(root: _Repository, rev: str) -> dict[str, int]:
    """The size in bytes of every blob in the tree of commit `rev`, by path."""
    listing = _run(["git", "ls-tree", "-r", "-l", "-z", "--full-tree", rev], root).stdout
    sizes: dict[str, int] = {}
    for entry in listing.split(b"\0"):
        if not entry:
            continue
        meta, tab, name = entry.partition(b"\t")
        fields = meta.split()
        if not tab or len(fields) != 4:
            raise GitDiffError("git ls-tree output is malformed")
        if fields[1] == b"blob":
            if not fields[3].isdigit():
                raise GitDiffError("git ls-tree output is malformed")
            sizes[_decode_path(name)] = int(fields[3])
    return sizes


def _line_counts(root: _Repository, raw: bytes, old: str, new: str) -> tuple[int, int]:
    """(added, deleted) lines of a `--numstat -z --no-renames` listing of `old`..`new`. A binary
    change (`-\t-`) has no line count, and counting it as zero would let any blob through the
    line cap, so it counts FAIL-CLOSED as its size in bytes -- the new blob's as added, the old
    one's as deleted: no text has more lines than bytes, so that is never less than the lines it
    could hold."""
    added = 0
    deleted = 0
    sizes: dict[str, dict[str, int]] = {}
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        fields = entry.split(b"\t", 2)
        if len(fields) != 3 or not fields[2]:
            raise GitDiffError("git numstat output is malformed")
        plus, minus, name = fields
        if plus.isdigit() and minus.isdigit():
            added += int(plus)
            deleted += int(minus)
            continue
        if plus != b"-" or minus != b"-":
            raise GitDiffError("git numstat output is malformed")
        path = _decode_path(name)
        for rev in (old, new):
            if rev not in sizes:
                sizes[rev] = _blob_sizes(root, rev)
        added += sizes[new].get(path, 0)
        deleted += sizes[old].get(path, 0)
    return added, deleted


def _every_commit(root: _Repository, merge_base: str, tip: str) -> _History:
    """What ANY commit in `merge_base..tip` publishes (Codex 4241839922, 4242095558). A push
    publishes every one of those commits, not only the endpoint tree:

    - every path any commit touches: a forbidden or secret file added in one commit and deleted
      in the next is absent from the endpoint diff yet published in the history;
    - the SUM over the commits of each one's added and deleted lines (binary changes counted by
      `_line_counts`) and of the bytes of its `--binary --full-index` patch against its parent:
      10,000 lines added in one commit and shrunk to one in the next is a one-line endpoint diff,
      while the push still publishes the 10,000-line commit and its blob. The sum, not the
      largest single commit, because a cap split across several commits publishes all of them.

    Each commit is diffed against its single parent, both sides of every rename (`--no-renames`:
    a rename is its deletion plus its addition). A merge commit in the range is refused (exit 2)
    rather than judged: which parent its change is relative to is ambiguous, and the routine
    never needs one -- its branch is cut from the base and committed onto."""
    listing = _run(["git", "rev-list", "--parents", f"{merge_base}..{tip}", "--"], root)
    paths: set[str] = set()
    added = deleted = patch_bytes = 0
    for line in listing.stdout.decode("ascii", errors="replace").splitlines():
        names = line.split(" ")
        if not all(_OBJECT_NAME.fullmatch(name) for name in names):
            raise GitDiffError("git rev-list output is malformed")
        if len(names) != 2:
            kind = "a merge commit" if len(names) > 2 else "a commit without a parent"
            raise GitDiffError(
                f"{names[0][:12]} is {kind}; check-diff judges a linear series of commits on "
                "the base only -- not concluding"
            )
        commit, parent = names
        span = [parent, commit, "--"]
        listed = _run(["git", "diff-tree", *_PLAIN_TREE_DIFF, "--name-status", *span], root)
        paths.update(_paths_from_name_status(listed.stdout))
        stats = _run(["git", "diff-tree", *_PLAIN_TREE_DIFF, "--numstat", *span], root).stdout
        plus, minus = _line_counts(root, stats, parent, commit)
        added += plus
        deleted += minus
        patch = _run(
            ["git", "diff-tree", *_PLAIN_TREE_DIFF, "-p", "--binary", "--full-index", *span],
            root,
        ).stdout
        patch_bytes += len(patch)
    return _History(paths=paths, added_lines=added, deleted_lines=deleted, patch_bytes=patch_bytes)


def collect_commit_diff(repository: str | Path, base: str, head: str = "HEAD") -> GitDiff:
    """The COMMITTED change `head` brings onto `base`: the diff from their merge base to `head`,
    tree to tree -- what a pull request from `head` into `base` shows, and what a push of `head`
    publishes (Codex 4241434509). `paths` is that diff's paths PLUS every path any single commit
    of the series touches (Codex 4241839922: a push publishes the history, not only the end
    state); `added_lines`, `deleted_lines` and `patch` are the endpoint diff's, and the
    `history_*` fields the per-commit sums (Codex 4242095558, `_every_commit`). The size caps
    apply to the larger of the two (`GitDiff.cap_*`): the end state alone hides a large commit
    that a later one shrank; the history alone can, with a binary or an odd textconv-free
    rendering, come out smaller than the net patch. Binary changes count by size in both
    (`_line_counts`). The working tree and index are never read, so an uncommitted edit can
    neither hide a committed change nor add one that is not pushed. No external diff or textconv
    program runs."""
    for rev in (base, head):
        if not rev or rev.startswith("-"):
            raise GitDiffError(f"not a revision: {rev!r}")
    root = _bind(repository)
    merge_base = _run(["git", "merge-base", base, head], root).stdout.decode().strip()
    tip = _run(["git", "rev-parse", "--verify", f"{head}^{{commit}}"], root).stdout.decode()
    if not merge_base or not tip.strip():
        raise GitDiffError(f"no merge base between {base} and {head}")
    span = [merge_base, tip.strip()]
    plain = ["--no-ext-diff", "--no-textconv"]
    names = _run(
        [
            "git",
            "diff",
            *plain,
            "--name-status",
            "-z",
            "--find-renames",
            *span,
            "--",
        ],
        root,
    ).stdout
    net_paths = _paths_from_name_status(names)
    history = _every_commit(root, *span)
    paths = tuple(sorted(set(net_paths) | history.paths))
    raw_stats = _run(
        ["git", "diff", *plain, "--no-renames", "--numstat", "-z", *span, "--"], root
    ).stdout
    added, deleted = _line_counts(root, raw_stats, *span)
    patch = _run(["git", "diff", *plain, "--binary", "--full-index", *span, "--"], root).stdout
    return GitDiff(
        paths=paths,
        added_lines=added,
        deleted_lines=deleted,
        patch=patch,
        net_paths=net_paths,
        history_added_lines=history.added_lines,
        history_deleted_lines=history.deleted_lines,
        history_patch_bytes=history.patch_bytes,
    )


def default_branch_name(repository: str | Path) -> str:
    """The branch the repository's ``origin/HEAD`` symref names, or ``""``.

    Set by ``git clone`` (and refreshed with ``git remote set-head origin
    --auto``), the symref is TARGET-side state: it names the remote's
    default branch without consulting any committed policy, so base
    authentication can anchor on it instead of a branch name the candidate
    — or a stale branch tip's own policy — wrote.
    """
    try:
        out = (
            _run(["git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], repository)
            .stdout.decode("utf-8", errors="surrogateescape")
            .strip()
        )
    except GitDiffError:
        return ""
    prefix = "refs/remotes/origin/"
    return out[len(prefix) :] if out.startswith(prefix) else ""


def collect_series_patch(repository: str | Path, base: str, head: str) -> bytes:
    """Every published change of ``base..head`` as per-commit patches.

    A push of ``head`` publishes the whole series, not only its end state: a
    commit that adds content a later commit removes leaves the endpoint tree
    diff empty while the pushed history still carries the blob. The reviewer
    therefore reads the per-commit patches (``git log --patch``), which show
    every intermediate change the endpoint diff can hide. No external diff
    or textconv program runs."""
    for rev in (base, head):
        if not rev or rev.startswith("-"):
            raise GitDiffError(f"not a revision: {rev!r}")
    root = _bind(repository)
    return _run(
        [
            "git",
            "log",
            # The repository's own config is the candidate's to write:
            # `log.showSignature` would make this log VERIFY signed commits
            # by executing the configured `gpg.program` — a
            # candidate-controlled executable — before any gate sandbox is
            # entered, so signature rendering is disabled outright.
            "--no-show-signature",
            # --no-show-signature does not cover a signature REQUESTED BY THE
            # FORMAT: a repository-local `format.pretty = %G?` would make
            # this log verify signatures anyway, executing the candidate's
            # `gpg.program` on the host, so the format is pinned explicitly
            # to git's default instead of inherited from candidate config.
            "--pretty=medium",
            "--no-ext-diff",
            "--no-textconv",
            # The candidate's committed .gitattributes is honored by default:
            # a `*.py -diff` rule would render source changes as an opaque
            # "GIT binary patch" the reviewer cannot read, while the packet
            # still labels the evidence ordinary text. Every file is forced
            # to render AS TEXT; genuinely non-UTF-8 bytes still travel
            # losslessly through the evidence's base64 encoding.
            "--text",
            "--binary",
            "--full-index",
            # A repository-local `diff.ignoreSubmodules = all` would omit a
            # gitlink change from the patch entirely, concealing the
            # dependency revision the series introduces while the diff
            # summary still names the path — the same override `_run` gives
            # every `git diff`, which `git log --patch` does not inherit.
            "--ignore-submodules=none",
            "--patch",
            "--reverse",
            f"{base}..{head}",
            "--",
        ],
        root,
    ).stdout


def committed_path(repository: str | Path, path: str | Path) -> str:
    """`path` as a path inside the repository at `repository` (POSIX, relative to its top
    level), for reading it from a commit. A relative `path` is taken relative to the top level;
    an absolute one must lie inside it."""
    top = _bind(repository).top
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            candidate = Path(os.path.realpath(candidate)).relative_to(os.path.realpath(top))
        except ValueError as exc:
            raise GitDiffError(f"{path} is not inside the repository {top}") from exc
    relative = PurePosixPath(*candidate.parts)
    if not relative.parts or any(part in ("", ".", "..") for part in relative.parts):
        raise GitDiffError(f"not a path inside the repository: {str(path)!r}")
    return str(relative)


def resolve_commit(repository: str | Path, rev: str) -> str:
    """The full object name of the commit `rev` names in the repository at `repository`."""
    if not rev or rev.startswith("-"):
        raise GitDiffError(f"not a revision: {rev!r}")
    root = _bind(repository)
    commit = _run(["git", "rev-parse", "--verify", f"{rev}^{{commit}}"], root).stdout.decode()
    commit = commit.strip()
    if not _OBJECT_NAME.fullmatch(commit):
        raise GitDiffError(f"{rev} does not name a commit")
    return commit


def repository_identity(repository: str | Path) -> str:
    """The canonical identity of the repository's object store.

    The realpath of its COMMON git directory: one repository, every worktree
    linked to it, and every symlink alias of either all share this identity,
    so two paths with equal identities are the same mutable store however
    they are spelled."""
    root = _bind(repository)
    out = (
        _run(["git", "rev-parse", "--git-common-dir"], root)
        .stdout.decode("utf-8", errors="surrogateescape")
        .strip()
    )
    common = out if os.path.isabs(out) else os.path.join(root.top, out)
    return os.path.realpath(common)


@contextmanager
def derivation_tree(
    repository: str | Path, rev: str, *, pin_refs: Mapping[str, str] | None = None
) -> Iterator[Path]:
    """A disposable checkout of commit `rev` under pristine configuration.

    For callers about to RUN programs against a commit (gate commands): the
    caller's own checkout is not evidence — it can carry uncommitted edits,
    ignored files a gate would read, or repository-local configuration that
    suppresses status output or defines smudge filters for a committed
    ``.gitattributes`` — and a gate run leaves its own caches behind, so two
    derivations in one checkout would observe each other. The tree is a
    fresh CLONE (``--no-local``, then a detached checkout): its brand-new
    configuration defines no filter drivers and no hooks, so the committed
    bytes materialize unaltered, while the clone still carries full Git
    metadata and the origin refs a repository-aware gate (one diffing
    against ``origin/<branch>``, say) needs. Every committed entry —
    symlinks included — materializes exactly as a real checkout would, so
    gates test the candidate tree itself, and the whole directory is removed
    afterwards. The commit must be reachable from the source repository's
    refs, as any published candidate is."""
    commit = resolve_commit(repository, rev)
    root = _bind(repository)
    program = _git_program()
    base = tempfile.mkdtemp(prefix="agentic-sdlc-derivation-")
    tree = Path(base) / "tree"
    try:
        _exec(
            [
                program,
                "clone",
                "--quiet",
                "--no-local",
                "--no-checkout",
                "--",
                root.git_dir,
                str(tree),
            ],
            base,
            "clone",
        )
        # An ordinary clone maps only the source's HEADS to origin/*: a CI
        # checkout often carries the candidate solely on remote-tracking
        # refs, so the source's refs/remotes/* and refs/tags/* are mirrored
        # in verbatim — gates diffing against origin/<branch> find the ref,
        # and a head reachable only through those refs transfers too.
        _exec(
            [
                program,
                "-C",
                str(tree),
                "--no-replace-objects",
                "-c",
                "core.fsmonitor=false",
                "fetch",
                "--quiet",
                "origin",
                "+refs/remotes/*:refs/remotes/*",
                "+refs/tags/*:refs/tags/*",
            ],
            str(tree),
            "fetch",
        )
        # Caller-pinned refs OVERRIDE what the mirror carried: the source's
        # own remote-tracking refs are source-mutable, so a caller that has
        # authenticated a commit elsewhere (the review base) pins the tree's
        # ref to that exact value rather than trusting the imported one.
        for ref_name, value in sorted((pin_refs or {}).items()):
            if not ref_name.startswith("refs/") or not _OBJECT_NAME.fullmatch(value):
                raise GitDiffError("pinned refs must map refs/* names to full commit object names")
            _exec(
                [
                    program,
                    "-C",
                    str(tree),
                    "--no-replace-objects",
                    "-c",
                    "core.fsmonitor=false",
                    "update-ref",
                    ref_name,
                    value,
                ],
                str(tree),
                "update-ref",
            )
        # The clone's origin URL names the caller's real Git directory: a
        # gate — or candidate code a gate runs — could push refs or edit that
        # repository through it. The refs are mirrored already, so the remote
        # is repointed at an inert, non-writable location.
        _exec(
            [
                program,
                "-C",
                str(tree),
                "remote",
                "set-url",
                "origin",
                "/var/empty/agentic-sdlc-derivation-disconnected",
            ],
            str(tree),
            "remote",
        )
        _exec(
            [
                program,
                "-C",
                str(tree),
                "--no-replace-objects",
                "-c",
                "core.fsmonitor=false",
                "checkout",
                "--quiet",
                "--detach",
                commit,
            ],
            str(tree),
            "checkout",
        )
        yield tree
    finally:
        shutil.rmtree(base, ignore_errors=True)


def read_committed_file(
    repository: str | Path, rev: str, path: str, label: str | None = None
) -> bytes:
    """The bytes of `path` (relative to the top level) as commit `rev` holds them -- never the
    working tree or the index, which the agent being checked can change without committing
    (Codex 4241614590). Replace refs and grafts are ignored as for every read here, and no
    filter, textconv or external program runs (`cat-file blob`). A missing file is an error."""
    if not rev or rev.startswith("-"):
        raise GitDiffError(f"not a revision: {rev!r}")
    root = _bind(repository)
    commit = _run(["git", "rev-parse", "--verify", f"{rev}^{{commit}}"], root).stdout.decode()
    commit = commit.strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise GitDiffError(f"{rev} does not name a commit")
    try:
        return _run(["git", "cat-file", "blob", f"{commit}:{path}"], root).stdout
    except GitDiffError as exc:
        shown = label or rev
        raise GitDiffError(f"{path} could not be read from {shown} ({commit[:12]}): {exc}") from exc
