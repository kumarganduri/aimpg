"""Pick commits that can fairly judge a replay (Phase 2 review R4). Costs no AI tokens.

A commit qualifies if it is yours, changes both code and tests, has a
descriptive message (≥ 6 words), is newer than the model's training cutoff
(so it can't be memorized), lives in a supported repo (pytest, vitest, jest),
and its tests flip: they FAIL on the parent code and PASS on the commit, on
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
from aimpg.replay import workspace
from aimpg.replay.proxy import AllowlistProxy
from aimpg.replay.workspace import TEST_FILE, Commit, Layout, Precheck, WorkspaceError

MIN_WORDS = 6


def candidates(repo: str, since: float, cutoff: float) -> tuple[list[Commit], dict[str, int]]:
    """Cheap filters (no installs, no test runs). Returns (commits, skip counts by reason)."""
    me = user_email(repo)
    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    out = []
    for c in load_commits(repo, since, reflog=False):
        tests = sorted(f for f in c.files if TEST_FILE.search(f))
        code = sorted(f for f in c.files if not TEST_FILE.search(f))
        if c.author_email != me:
            skip("not yours")
        elif c.ts <= cutoff:
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
    return out, skipped


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
    try:
        prepared = workspace.prepare(commit, layout, proxy)
        before = []
        work = workspace.clone_for_run(commit, layout, prepared, f"precheck-{commit.sha[:12]}")
        workspace.overlay_commit_tests(commit, work)
        for i in range(2):
            before.append(workspace.judge(commit, layout, work, f"precheck-{commit.sha[:12]}")[0])
        workspace.overlay_commit_tree(commit, work)
        after = [workspace.judge(commit, layout, work, f"precheck-{commit.sha[:12]}")[0] for _ in range(2)]
    except WorkspaceError as exc:
        return Precheck(False, f"setup failed: {str(exc)[:200]}")
    finally:
        shutil.rmtree(layout.run_dir(f"precheck-{commit.sha[:12]}"), ignore_errors=True)
    details = {"passed_on_parent": before, "passed_on_commit": after}
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
