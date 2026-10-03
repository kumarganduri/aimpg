"""Git layer: load commits and decide whether each one was kept.

Everything is batched per repo (a few subprocess calls, never one per
commit) and offline. `refresh=True` is the only thing that touches the
network (`git fetch`).

Kept-status decision for a commit c, checked in order:

    age < 7 days ................................ PENDING
    c reachable from the default ref ............ KEPT
    same patch-id as a default-ref commit ....... KEPT          (rebased / cherry-picked)
    added lines ⊆ a later default-ref commit .... KEPT_SQUASH   (squash merge, offline)
    default ref not updated since c + 7 days .... UNKNOWN_STALE (pull to update)
    c adds no meaningful lines to compare ....... UNKNOWN_SQUASH
    otherwise ................................... DISCARDED
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from pathlib import Path

DAY = 86400.0
KEEP_AFTER = 7 * DAY
SQUASH_WINDOW = 30 * DAY
_MIN_LINE = 4  # ignore trivial added lines like "}" or "" when matching squashes

_REC = "\x1e"
_FLD = "\x1f"


class Status(str, Enum):
    PENDING = "pending"
    KEPT = "kept"
    KEPT_SQUASH = "kept (squash)"
    DISCARDED = "discarded"
    UNKNOWN_STALE = "unknown (pull to update)"
    UNKNOWN_SQUASH = "unknown (possible squash)"

    @property
    def is_kept(self) -> bool:
        return self in (Status.KEPT, Status.KEPT_SQUASH)


@dataclass
class Commit:
    sha: str
    ts: float  # committer time, epoch seconds
    subject: str
    author_email: str = ""
    files: dict[str, tuple[int, int]] = field(default_factory=dict)  # path -> (added, deleted)
    added: dict[str, set[str]] = field(default_factory=dict)  # path -> meaningful added lines
    removed: dict[str, set[str]] = field(default_factory=dict)  # path -> meaningful removed lines (rework detection)
    message: str = ""  # full message body (revert detection); filled by load_commits

    @property
    def lines_changed(self) -> int:
        return sum(a + d for a, d in self.files.values())


class GitError(RuntimeError):
    pass


def _git(repo: str | Path, *args: str, stdin: str | None = None) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            input=stdin,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
    except FileNotFoundError as exc:  # git not installed
        raise GitError("git is not installed") from exc
    if proc.returncode != 0:
        raise GitError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout


def user_email(repo: str) -> str:
    """The identity this machine commits as in `repo` ("" if unset)."""
    try:
        return _git(repo, "config", "user.email").strip().lower()
    except GitError:
        return ""


def repo_root(path: str) -> str | None:
    """Toplevel of the repo containing `path`, or None (missing dir / not a repo).

    Walks up looking for `.git` (a dir, or a file for worktrees/submodules)
    instead of spawning `git rev-parse`: receipts resolve hundreds of paths.
    """
    if not path or not os.path.isdir(path):
        return None
    return _find_git_dir(os.path.realpath(path))


@lru_cache(maxsize=4096)
def _find_git_dir(directory: str) -> str | None:
    if os.path.exists(os.path.join(directory, ".git")):
        return directory
    parent = os.path.dirname(directory)
    return None if parent == directory else _find_git_dir(parent)


def default_ref(repo: str) -> tuple[str, float]:
    """The freshest of origin/<default> and local main/master, with when it was last updated."""
    candidates: list[str] = []
    try:
        head = _git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD").strip()
        candidates.append(head.removeprefix("refs/remotes/"))
    except GitError:
        pass
    candidates += ["origin/main", "origin/master", "main", "master"]

    best: tuple[str, float] | None = None
    for ref in dict.fromkeys(candidates):
        try:
            tip = float(_git(repo, "log", "-1", "--format=%ct", ref).strip())
        except (GitError, ValueError):
            continue
        if best is None or tip > best[1]:
            best = (ref, tip)
    if best is None:
        raise GitError("no default branch found (looked for origin/HEAD, main, master)")
    ref, tip = best
    return ref, max(tip, _ref_updated(repo, ref, tip))


def _ref_updated(repo: str, ref: str, tip: float) -> float:
    """Last time the ref moved (reflog), falling back to its tip commit time."""
    try:
        out = _git(repo, "log", "-g", "-1", "--format=%gd", "--date=unix", ref).strip()
        # %gd with --date=unix looks like "origin/main@{1759339200}"
        return float(out.rsplit("{", 1)[1].rstrip("}"))
    except (GitError, ValueError, IndexError):
        return tip


def load_commits(repo: str, since: float, until: float | None = None, *, refs: list[str] | None = None, reflog: bool = True) -> list[Commit]:
    """All commits in the window, with numstat and meaningful added lines.

    Defaults to `--all --reflog` so amended/rebased-away commits are still found.
    """
    args = ["log", "-p", "--no-color", "--no-renames", "--unified=0",
            f"--format={_REC}%H{_FLD}%ct{_FLD}%ae{_FLD}%s", f"--since={int(since)}"]
    if until is not None:
        args.append(f"--until={int(until)}")
    if refs is None:
        args.append("--all")
        if reflog:
            args.append("--reflog")
    else:
        args += refs
    return _parse_patch_log(_git(repo, *args))


def _parse_patch_log(text: str) -> list[Commit]:
    commits: list[Commit] = []
    for record in text.split(_REC)[1:]:
        header, _, body = record.partition("\n")
        sha, ts, email, subject = (header.split(_FLD) + ["", "", "", ""])[:4]
        commit = Commit(sha=sha, ts=float(ts or 0), subject=subject, author_email=email.lower())
        current: str | None = None
        for line in body.split("\n"):
            if line.startswith("diff --git "):
                current = line.split(" b/", 1)[-1]
                commit.files.setdefault(current, (0, 0))
            elif current is None or line.startswith(("+++", "---")):
                continue
            elif line.startswith("+"):
                a, d = commit.files[current]
                commit.files[current] = (a + 1, d)
                stripped = line[1:].strip()
                if len(stripped) >= _MIN_LINE:
                    commit.added.setdefault(current, set()).add(stripped)
            elif line.startswith("-"):
                a, d = commit.files[current]
                commit.files[current] = (a, d + 1)
                stripped = line[1:].strip()
                if len(stripped) >= _MIN_LINE:
                    commit.removed.setdefault(current, set()).add(stripped)
        commits.append(commit)
    return commits


def patch_ids(repo: str, shas: list[str]) -> dict[str, str]:
    """sha -> stable patch-id, in one `git show | git patch-id` pipeline."""
    result: dict[str, str] = {}
    for i in range(0, len(shas), 500):  # stay well under ARG_MAX
        shown = _git(repo, "show", "--no-color", "--format=commit %H", *shas[i : i + 500])
        for line in _git(repo, "patch-id", "--stable", stdin=shown).splitlines():
            pid, sha = line.split()
            result[sha] = pid
    return result


def reachable(repo: str, ref: str, since: float) -> set[str]:
    return set(_git(repo, "rev-list", f"--since={int(since)}", ref).split())


@dataclass
class KeptChecker:
    """Classifies commits of one repo against its default branch. Loads git data once."""

    repo: str
    since: float
    now: float
    refresh: bool = False
    ref: str = ""
    ref_updated: float = 0.0
    commits: list[Commit] | None = None  # pass load_commits(--all) output to avoid a second git log
    _on_ref: set[str] = field(default_factory=set)
    _upstream: list[Commit] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.refresh:
            _git(self.repo, "fetch", "--quiet", "origin")
        self.ref, self.ref_updated = default_ref(self.repo)
        self._on_ref = reachable(self.repo, self.ref, self.since)
        if self.commits is None:
            self._upstream = load_commits(self.repo, self.since, refs=[self.ref])
        else:  # `--all` already includes everything reachable from the ref
            self._upstream = [c for c in self.commits if c.sha in self._on_ref]

    def classify(self, commits: list[Commit]) -> dict[str, Status]:
        candidates = [c for c in commits if c.sha not in self._on_ref and self.now - c.ts >= KEEP_AFTER]
        # one `git show | git patch-id` pipeline for both sides
        pids = patch_ids(self.repo, [c.sha for c in self._upstream] + [c.sha for c in candidates]) if candidates else {}
        upstream_pids = {pids[c.sha] for c in self._upstream if c.sha in pids}
        return {c.sha: self._status(c, pids.get(c.sha), upstream_pids) for c in commits}

    def _status(self, c: Commit, pid: str | None, upstream_pids: set[str]) -> Status:
        if self.now - c.ts < KEEP_AFTER:
            return Status.PENDING
        if c.sha in self._on_ref or (pid is not None and pid in upstream_pids):
            return Status.KEPT
        meaningful = {p: lines for p, lines in c.added.items() if lines}
        if meaningful and self._squash_match(c, meaningful):
            return Status.KEPT_SQUASH
        if self.ref_updated < c.ts + KEEP_AFTER:
            return Status.UNKNOWN_STALE
        if not meaningful:
            return Status.UNKNOWN_SQUASH
        return Status.DISCARDED

    def _squash_match(self, c: Commit, meaningful: dict[str, set[str]]) -> bool:
        for up in self._upstream:
            if not (c.ts <= up.ts <= c.ts + SQUASH_WINDOW):
                continue
            if all(lines <= up.added.get(path, set()) for path, lines in meaningful.items()):
                return True
        return False
