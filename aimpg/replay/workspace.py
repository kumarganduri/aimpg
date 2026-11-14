"""Workspaces for replays: export, prepare (Phase A), per-run copies, judging (Phase C).

    export parent tree ──► Phase A: install deps from the COMMIT's lockfile
    (git archive: no        (proxy: PyPI or npm only), once per commit
     history, no future             │
     commits to peek at)            ▼
                          per run: APFS clone (cp -c) + offline re-sync
                                    │          (a copied uv venv still imports
                                    ▼           the original folder otherwise)
                          Phase B agent works ──► Phase C: copy the commit's
                                                  test files in, run only them,
                                                  no network → pass / fail
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from aimpg.replay import sandbox
from aimpg.replay.proxy import NOTHING, NPM, PYPI, AllowlistProxy
from aimpg.replay.sandbox import Profile

TEST_FILE = re.compile(r"(^|/)(tests?/|test_[^/]+\.py$|[^/]+_test\.py$|[^/]+\.(test|spec)\.[cm]?[jt]sx?$)")
CACHE_DIRS = (".venv", ".uv-cache", ".npm-cache", "node_modules", ".tmp")
PREPARE_TIMEOUT = 15 * 60
JUDGE_TIMEOUT = 10 * 60
UV_PYTHON = Path.home() / ".local/share/uv/python"


class WorkspaceError(RuntimeError):
    pass


@dataclass
class Commit:
    repo: str
    sha: str
    parent: str
    subject: str
    body: str
    test_files: list[str]
    code_files: list[str]
    kind: str = ""  # "python" | "js"
    runner: str = ""  # "pytest" | "vitest" | "jest"
    ts: float = 0.0
    hint: str = ""  # interface hint: new names the hidden tests use (hints.py)
    code_lines: int = -1  # lines added plus removed in non-test files (-1: not measured yet)

    @property
    def words(self) -> int:
        """Length of the description the agent gets: the commit message, without the interface hint."""
        return len((self.subject + " " + self.body).split())

    @property
    def task(self) -> str:
        message = (self.subject + ("\n\n" + self.body if self.body.strip() else "")).strip()
        return message + ("\n\n" + self.hint if self.hint else "")


@dataclass
class Layout:
    """All replay data lives under one root, which every agent profile denies (R18)."""

    root: Path

    def prepared(self, sha: str) -> Path:
        return self.root / "prepared" / sha[:12]

    def run_dir(self, run_id: str) -> Path:
        return self.root / "runs" / run_id

    def profile_path(self, name: str) -> Path:
        (self.root / "profiles").mkdir(parents=True, exist_ok=True)
        return self.root / "profiles" / f"{name}.sb"


def git(repo: str | Path, *args: str, input: bytes | None = None) -> bytes:
    proc = subprocess.run(["git", "-C", str(repo), *args], input=input, capture_output=True)
    if proc.returncode != 0:
        raise WorkspaceError(proc.stderr.decode(errors="replace").strip())
    return proc.stdout


def export_tree(repo: str, sha: str, dest: Path) -> None:
    """The tree at `sha` as a fresh one-commit repo: no history, no refs to peek at."""
    dest.mkdir(parents=True, exist_ok=True)
    tar = git(repo, "archive", "--format=tar", sha)
    subprocess.run(["tar", "-x", "-C", str(dest)], input=tar, check=True)
    ident = ["-c", "user.email=replay@aimpg", "-c", "user.name=aimpg", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(dest)], check=True)
    (dest / ".git" / "info").mkdir(parents=True, exist_ok=True)
    (dest / ".git" / "info" / "exclude").write_text("\n".join(CACHE_DIRS) + "\n")
    subprocess.run(["git", "-C", str(dest), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(dest), *ident, "commit", "-q", "-m", "replay base", "--allow-empty"], check=True)


def code_lines(repo: str | Path, parent: str, sha: str) -> int:
    """Lines added plus removed by a commit in its non-test files (binary files count as 0)."""
    total = 0
    for line in git(repo, "diff", "--numstat", "--no-renames", parent, sha).decode(errors="replace").splitlines():
        added, removed, name = (line.split("\t") + ["", ""])[:3]
        if added.isdigit() and removed.isdigit() and not TEST_FILE.search(name):
            total += int(added) + int(removed)
    return total


def file_at(repo: str, sha: str, path: str) -> bytes | None:
    try:
        return git(repo, "show", f"{sha}:{path}")
    except WorkspaceError:
        return None


def detect(tree: Path) -> tuple[str, str] | None:
    """(kind, runner) for supported repos, else None."""
    pyproject = tree / "pyproject.toml"
    if pyproject.exists() and (tree / "uv.lock").exists() and "pytest" in pyproject.read_text(errors="replace"):
        return "python", "pytest"
    pkg = tree / "package.json"
    if pkg.exists() and (tree / "package-lock.json").exists():
        try:
            data = json.loads(pkg.read_text())
        except ValueError:
            return None
        deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
        script = str(data.get("scripts", {}).get("test", ""))
        for runner in ("vitest", "jest"):
            if runner in deps or runner in script:
                return "js", runner
    return None


def _base_env(home: Path, tmp: Path) -> dict[str, str]:
    return {"PATH": sandbox.tool_path(), "HOME": str(home), "TMPDIR": str(tmp), "LANG": "en_US.UTF-8"}


def _tool_env(work: Path) -> dict[str, str]:
    env = _base_env(work / ".tmp", work / ".tmp")
    env.update(
        UV_CACHE_DIR=str(work / ".uv-cache"),
        UV_PYTHON_INSTALL_DIR=str(UV_PYTHON),
        UV_NO_CONFIG="1",
        UV_PYTHON_DOWNLOADS="never",
        npm_config_cache=str(work / ".npm-cache"),
        npm_config_update_notifier="false",
        npm_config_fund="false",
        npm_config_audit="false",
        # The workspace is a history-free export (no .git), so build backends that read the version
        # from git (setuptools-scm, hatch-vcs: pyinfra) fail; give them a placeholder, the same for every setup
        SETUPTOOLS_SCM_PRETEND_VERSION="0.0.0",
        UV_DYNAMIC_VERSIONING_BYPASS="0.0.0",  # uv-dynamic-versioning (pyinfra)
    )
    return env


def _sandboxed(layout: Layout, name: str, work: Path, argv: list[str], *, proxy: AllowlistProxy | None, timeout: float, extra_env: dict | None = None) -> sandbox.Result:
    (work / ".tmp").mkdir(exist_ok=True)
    env = _tool_env(work)
    if proxy is not None:
        env.update(HTTPS_PROXY=proxy.url, https_proxy=proxy.url)
    env.update(extra_env or {})
    profile = Profile(
        writable=[work],
        readable=sandbox.tool_dirs() + ([UV_PYTHON] if UV_PYTHON.is_dir() else []),
        deny_roots=[layout.root],
        proxy_port=proxy.port if proxy is not None else None,
        proxy_socket=proxy.socket_path if proxy is not None else None,
    )
    return sandbox.run(argv, profile=profile, profile_path=layout.profile_path(name), env=env, cwd=work, timeout=timeout)


def _check(result: sandbox.Result, what: str) -> None:
    if result.timed_out:
        raise WorkspaceError(f"{what} timed out")
    if result.returncode != 0:
        raise WorkspaceError(f"{what} failed: {(result.stderr or result.stdout)[-800:]}")


_GROUP = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9_.-]*)\s*=\s*\[", re.M)
_TEST_GROUP = re.compile(r"(^|[-_])(dev|tests?|testing)($|[-_])")


def test_groups(project: Path) -> list[str]:
    """uv flags for the dependency groups tests need: those named like dev/test/testing.

    Installing every group pulled in docs tooling fetched straight from GitHub
    (pydantic), which the install sandbox rightly can't reach. Falls back to all
    groups when a project has none of those names.
    """
    try:
        text = (project / "pyproject.toml").read_text()
    except OSError:
        return ["--all-groups"]
    section = re.search(r"^\[dependency-groups\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S)
    names = _GROUP.findall(section.group(1)) if section else []
    wanted = [n for n in names if _TEST_GROUP.search(n)]
    if not names:
        return []
    if not wanted:
        return ["--all-groups"]
    return [f for n in wanted for f in ("--group", n)]


def prepare(commit: Commit, layout: Layout, proxy: AllowlistProxy) -> Path:
    """Phase A, once per commit: parent tree + dependencies from the commit's lockfile (R17)."""
    dest = layout.prepared(commit.sha)
    if (dest / ".prepared").exists():
        return dest
    if dest.exists():
        shutil.rmtree(dest)
    export_tree(commit.repo, commit.parent, dest)
    if commit.kind == "python":
        proxy.set_allow(PYPI)
        reqs = _commit_requirements(commit, layout)
        _check(_sandboxed(layout, "prepare", dest, ["uv", "sync", *test_groups(dest), "--quiet"], proxy=proxy, timeout=PREPARE_TIMEOUT), "uv sync")
        if reqs:
            (dest / ".tmp").mkdir(exist_ok=True)
            (dest / ".tmp" / "commit-requirements.txt").write_text(reqs)
            _check(_sandboxed(layout, "prepare", dest, ["uv", "pip", "install", "--quiet", "-r", ".tmp/commit-requirements.txt"], proxy=proxy, timeout=PREPARE_TIMEOUT), "uv pip install (commit deps)")
    else:
        proxy.set_allow(NPM)
        originals = {name: (dest / name).read_bytes() for name in ("package.json", "package-lock.json") if (dest / name).exists()}
        for name in ("package.json", "package-lock.json"):
            data = file_at(commit.repo, commit.sha, name)
            if data is not None:
                (dest / name).write_bytes(data)
        _check(_sandboxed(layout, "prepare", dest, ["npm", "ci", "--no-audit", "--no-fund"], proxy=proxy, timeout=PREPARE_TIMEOUT), "npm ci")
        for name, data in originals.items():  # the agent sees the parent's manifest
            (dest / name).write_bytes(data)
    proxy.set_allow(NOTHING)
    (dest / ".prepared").write_text(commit.sha)
    return dest


def _commit_requirements(commit: Commit, layout: Layout) -> str:
    """The commit's locked third-party packages (names + versions only, no project code)."""
    lock = file_at(commit.repo, commit.sha, "uv.lock")
    pyproject = file_at(commit.repo, commit.sha, "pyproject.toml")
    if lock is None or pyproject is None:
        return ""
    tmp = layout.root / "tmp" / f"reqs-{commit.sha[:12]}"
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "uv.lock").write_bytes(lock)
    (tmp / "pyproject.toml").write_bytes(pyproject)
    proc = subprocess.run(
        ["uv", "export", "--frozen", "--no-hashes", "--no-emit-project", *test_groups(tmp), "--quiet"],
        cwd=tmp,
        capture_output=True,
        text=True,
        env={**_tool_env(tmp), "HOME": str(tmp)},
    )
    shutil.rmtree(tmp, ignore_errors=True)
    if proc.returncode != 0:
        raise WorkspaceError(f"uv export failed: {proc.stderr[-400:]}")
    return "\n".join(l for l in proc.stdout.splitlines() if l and not l.startswith(("#", "-e ", "./", ".")))


def clone_for_run(commit: Commit, layout: Layout, prepared: Path, run_id: str, *, keep_run_dir: bool = False) -> Path:
    """Per-run copy (APFS clone) + offline re-sync so imports point at this copy.

    keep_run_dir: replace only the code, keeping the run's config folder (a resumed
    session in the fresh-session experiment needs the same folder and path)."""
    run_dir = layout.run_dir(run_id)
    if run_dir.exists() and not keep_run_dir:
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    work = run_dir / "work"
    if work.exists():
        shutil.rmtree(work)
    # copy-on-write clone: APFS on macOS, reflink (btrfs/xfs) on Linux, else a plain copy
    clone = ["cp", "-c", "-R"] if sandbox.sys.platform == "darwin" else ["cp", "-a", "--reflink=auto"]
    subprocess.run([*clone, str(prepared), str(work)], check=True)
    (work / ".prepared").unlink(missing_ok=True)
    if commit.kind == "python":
        _check(_sandboxed(layout, run_id, work, ["uv", "sync", "--offline", *test_groups(work), "--quiet"], proxy=None, timeout=300), "offline re-sync")
        reqs = work / ".tmp" / "commit-requirements.txt"
        if reqs.exists():
            _check(_sandboxed(layout, run_id, work, ["uv", "pip", "install", "--offline", "--quiet", "-r", ".tmp/commit-requirements.txt"], proxy=None, timeout=300), "offline commit deps")
    return work


def _safe_parent(work: Path, rel: str) -> Path:
    """The folder for `rel` inside `work`, with any symlinks on the way removed.

    The agent can write anywhere in `work`, so it can plant a symlink where a
    test file or folder goes; aimpg then writes on the host, outside the
    sandbox, and would follow it (security review 2026-10-11). Each path
    component is checked; a symlink is deleted and replaced by a real folder.
    """
    parts = Path(rel).parts
    if not parts or Path(rel).is_absolute() or ".." in parts:
        raise WorkspaceError(f"unsafe path in commit: {rel!r}")
    cur = work
    for part in parts[:-1]:
        cur = cur / part
        if cur.is_symlink() or (cur.exists() and not cur.is_dir()):
            cur.unlink()
        cur.mkdir(exist_ok=True)
    return cur


def safe_write(work: Path, rel: str, data: bytes) -> None:
    import os

    target = _safe_parent(work, rel) / Path(rel).name
    if target.is_symlink():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o644)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def overlay_commit_tests(commit: Commit, work: Path) -> None:
    """Phase C setup: put the commit's versions of its test files into the run."""
    for path in commit.test_files:
        data = file_at(commit.repo, commit.sha, path)
        if data is None:
            target = _safe_parent(work, path) / Path(path).name
            if target.is_symlink() or target.is_file():
                target.unlink()
            continue
        safe_write(work, path, data)


def overlay_commit_tree(commit: Commit, work: Path) -> None:
    """The commit's full tree over a parent copy (used by the free pre-check).

    Written file by file through safe_write: install scripts ran in this folder
    and a foreign repo's tree can itself contain symlinks, so nothing is
    extracted through an existing link. Symlinks in the tree are recreated only
    when they point inside the folder.
    """
    import io
    import os
    import tarfile

    tar = git(commit.repo, "archive", "--format=tar", commit.sha)
    with tarfile.open(fileobj=io.BytesIO(tar)) as archive:
        for m in archive.getmembers():
            if m.isdir():
                _safe_parent(work, m.name.rstrip("/") + "/x")
            elif m.isfile():
                safe_write(work, m.name, archive.extractfile(m).read())
            elif m.issym():
                link = _safe_parent(work, m.name) / Path(m.name).name
                dest = (link.parent / m.linkname).resolve()
                if not os.path.isabs(m.linkname) and str(dest).startswith(str(work.resolve()) + os.sep):
                    if link.is_symlink() or link.is_file():
                        link.unlink()
                    if not link.exists():
                        os.symlink(m.linkname, link)


def test_argv(commit: Commit, work: Path | None = None) -> list[str]:
    """Run only the commit's test files, with coverage thresholds off: a repo that demands 100%
    coverage (lint-staged) fails any subset run even when every test passes."""
    files = [f for f in commit.test_files if f.endswith((".py", ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs", ".mts", ".cts"))]
    if commit.runner == "pytest":
        has_cov = work is not None and any((work / ".venv").glob("lib/python*/site-packages/pytest_cov"))
        return [".venv/bin/python", "-m", "pytest", "-q", "-p", "no:cacheprovider", *(["--no-cov"] if has_cov else []), *files]
    if commit.runner == "vitest":
        return ["node_modules/.bin/vitest", "run", "--coverage.enabled=false", *files]
    return ["node_modules/.bin/jest", "--ci", "--coverage=false", *files]


def judge(commit: Commit, layout: Layout, work: Path, name: str) -> tuple[bool, str]:
    """Phase C: run only the commit's test files, no network. (passed, tail of output)."""
    result = _sandboxed(layout, name, work, test_argv(commit, work), proxy=None, timeout=JUDGE_TIMEOUT, extra_env={"CI": "1"})
    tail = (result.stdout + result.stderr)[-600:]
    return (not result.timed_out and result.returncode == 0), tail


@dataclass
class Precheck:
    ok: bool
    reason: str
    details: dict = field(default_factory=dict)
