"""Pick commits that can fairly judge a replay (Phase 2 review R4). Costs no AI tokens.

A commit qualifies if it is yours, changes both code and tests, has a
descriptive message (≥ 6 words), was authored (and committed) on or after
the model's training cutoff (so it can't be memorized), lives in a supported
repo (pytest, vitest, jest), and its tests flip: they FAIL on the parent code and PASS on the commit, on
two runs each (not flaky). Tests that already passed before can't tell a
real fix from an agent that did nothing.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path

from aimpg.gitkept import load_commits, user_email
from aimpg.replay import hints, workspace
from aimpg.replay.proxy import AllowlistProxy
from aimpg.replay.workspace import TEST_FILE, Commit, Layout, Precheck, WorkspaceError

MIN_WORDS = 6


def candidates(repo: str, since: float, cutoff: float, *, any_author: bool = False) -> tuple[list[Commit], dict[str, int]]:
    """Cheap filters (no installs, no test runs). Returns (commits oldest first, skip counts by reason).

    any_author: include everyone's commits (studies on public repos); by default only yours.
    """
    me = user_email(repo)
    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    out = []
    for c in load_commits(repo, since, reflog=False):
        tests = sorted(f for f in c.files if TEST_FILE.search(f))
        code = sorted(f for f in c.files if not TEST_FILE.search(f))
        if not any_author and c.author_email != me:
            skip("not yours")
        elif c.ts < cutoff or _authored(repo, c.sha) < cutoff:  # c.ts is the committer date; a rebase renews only that
            skip("before model training cutoff")
        elif not tests or not code:
            skip("doesn't change both code and tests")
        else:
            subject, body = _message(repo, c.sha)
            parents = workspace.git(repo, "rev-list", "--parents", "-n", "1", c.sha).decode().split()[1:]
            if len(parents) != 1:
                skip("merge or root commit")
            elif len(subject.split()) + len(body.split()) < MIN_WORDS:
                skip("message too short")
            else:
                out.append(Commit(repo, c.sha, parents[0], subject, body, tests, code, ts=c.ts))
    out.sort(key=lambda c: c.ts)  # oldest first, so "the first N after the cutoff" is well defined
    return out, skipped


def manifest(cutoff: str, any_author: bool, candidates_: list[Commit], qualified: list[Commit],
             not_checked: list[Commit] | None = None) -> dict:
    """What a study commits to: every candidate in order and which qualified, so cherry-picking would show.

    not_checked: candidates passed over without a test run, because enough single-file commits had
    already qualified and only multi-file ones were still wanted."""
    import hashlib

    shas = [c.sha for c in candidates_]
    out = {"cutoff": cutoff, "any_author": any_author, "candidates": shas,
           "candidates_sha256": hashlib.sha256("\n".join(shas).encode()).hexdigest(),
           "qualified": [c.sha for c in qualified]}
    if not_checked:
        out["not_checked_single_file"] = [c.sha for c in not_checked]
    return out


def is_multi_file(commit: Commit) -> bool:
    """The difficulty rule a study fixes before any run: did the original commit change two or more
    non-test files? It uses only the commit, never a model's result."""
    return len(commit.code_files) >= 2


def pick(commits: list[Commit], n: int, multi_min: int = 0) -> list[Commit]:
    """The n commits a study runs, from a repo's qualifying commits (oldest first).

    With multi_min = 0 these are simply the first n. Otherwise the first `multi_min` multi-file
    commits are taken (or as many as there are), and the remaining places go to the earliest other
    commits. The result is in date order."""
    multi = [c for c in commits if is_multi_file(c)][:max(0, min(multi_min, n))]
    taken = {c.sha for c in multi}
    rest = [c for c in commits if c.sha not in taken][:n - len(multi)]
    taken |= {c.sha for c in rest}
    return [c for c in commits if c.sha in taken]


def commit_at(repo: str, sha: str) -> Commit:
    """One named commit, without the authorship/cutoff filters (rerunning someone's public record)."""
    import re

    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise WorkspaceError(f"not a commit id: {sha[:60]!r}")
    files = workspace.git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "--end-of-options", sha).decode().split()
    parents = workspace.git(repo, "rev-list", "--parents", "-n", "1", "--end-of-options", sha).decode().split()[1:]
    if len(parents) != 1:
        raise WorkspaceError(f"{sha[:10]} is a merge or root commit")
    subject, body = _message(repo, sha)
    ts = float(workspace.git(repo, "log", "-1", "--format=%ct", "--end-of-options", sha).decode().strip())
    tests = sorted(f for f in files if TEST_FILE.search(f))
    return Commit(repo, sha, parents[0], subject, body, tests, sorted(set(files) - set(tests)), ts=ts)


def _authored(repo: str, sha: str) -> float:
    return float(workspace.git(repo, "log", "-1", "--format=%at", "--end-of-options", sha).decode().strip() or 0)


def _message(repo: str, sha: str) -> tuple[str, str]:
    text = workspace.git(repo, "log", "-1", "--format=%B", sha).decode(errors="replace").strip()
    subject, _, body = text.partition("\n")
    # trailers like Co-Authored-By add nothing to the task
    body = "\n".join(l for l in body.splitlines() if not l.lower().startswith(("co-authored-by:", "signed-off-by:")))
    return subject.strip(), body.strip()


def precheck(commit: Commit, layout: Layout, proxy: AllowlistProxy) -> Precheck:
    """Fail→pass check, twice each. Prepares (and caches) the commit's Phase A workspace."""
    with tempfile.TemporaryDirectory() as tmp:
        kind = workspace.detect(_tree_at(commit, Path(tmp)))
    if kind is None:
        return Precheck(False, "unsupported repo (needs pytest + uv.lock, or vitest/jest + package-lock.json)")
    commit.kind, commit.runner = kind
    hint = hints.for_commit(commit)
    commit.hint = hint.text()
    name = f"precheck-{commit.sha[:12]}"
    try:
        prepared = workspace.prepare(commit, layout, proxy)
        before = []
        work = workspace.clone_for_run(commit, layout, prepared, name)
        workspace.overlay_commit_tests(commit, work)
        for i in range(2):
            before.append(workspace.judge(commit, layout, work, name)[0])
        stub_passed = None
        if hint.names:  # the hint alone must never be enough: names with empty values must FAIL
            stub_work = workspace.clone_for_run(commit, layout, prepared, name + "-stub")
            hints.write_stubs(hint, stub_work, commit.kind)
            workspace.overlay_commit_tests(commit, stub_work)
            stub_passed = workspace.judge(commit, layout, stub_work, name + "-stub")[0]
        workspace.overlay_commit_tree(commit, work)
        after = [workspace.judge(commit, layout, work, name)[0] for _ in range(2)]
    except WorkspaceError as exc:
        return Precheck(False, f"setup failed: {str(exc)[:200]}")
    finally:
        shutil.rmtree(layout.run_dir(name), ignore_errors=True)
        shutil.rmtree(layout.run_dir(name + "-stub"), ignore_errors=True)
    details = {"passed_on_parent": before, "passed_on_commit": after, "passed_with_stubs": stub_passed, "hint": dict(hint.names)}
    if stub_passed:
        return Precheck(False, "tests pass with empty stubs of the hinted names (hint would give it away)", details)
    if before != [False, False] or after != [True, True]:
        if len(set(before)) > 1 or len(set(after)) > 1:
            return Precheck(False, "flaky tests", details)
        if any(before):
            return Precheck(False, "tests already pass on the parent (can't judge)", details)
        return Precheck(False, "tests don't pass on the commit itself", details)
    return Precheck(True, "ok", details)


def _tree_at(commit: Commit, dest: Path) -> Path:
    for name in ("pyproject.toml", "uv.lock", "package.json", "package-lock.json"):
        data = workspace.file_at(commit.repo, commit.sha, name)
        if data is not None:
            (dest / name).write_bytes(data)
    return dest


def save(commits: list[Commit], path: Path) -> None:
    path.write_text("\n".join(json.dumps(asdict(c)) for c in commits) + "\n")


def load(path: Path) -> list[Commit]:
    return [Commit(**json.loads(l)) for l in path.read_text().splitlines() if l.strip()]
