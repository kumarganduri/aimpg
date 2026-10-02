"""Full replay pipeline with the free fake agent (macOS + network for PyPI).

Fixture repo:  c0 "base" (add() + its test)  →  c1 adds mul() + test_mul
The replay starts from c0 with c1's tests hidden; Phase C runs them.
"""

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from aimpg.replay import sandbox
from aimpg.replay.run import Batch, Config, load_results, make_solution, run_one
from aimpg.replay.select import candidates, precheck
from aimpg.replay.setups import fake
from aimpg.replay.workspace import Layout
from aimpg.replay.proxy import AllowlistProxy

pytestmark = [pytest.mark.network, pytest.mark.skipif(not sandbox.available(), reason="macOS only")]

PYPROJECT = """[project]
name = "calc"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = []

[dependency-groups]
dev = ["pytest>=8"]
"""


def git(repo, *args, when=None):
    env = None
    if when:
        import os

        env = {**os.environ, "GIT_AUTHOR_DATE": f"@{int(when)} +0000", "GIT_COMMITTER_DATE": f"@{int(when)} +0000"}
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env).stdout.strip()


@pytest.fixture(scope="module")
def fixture_repo(tmp_path_factory):
    repo = tmp_path_factory.mktemp("calc")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "me@example.com")
    git(repo, "config", "user.name", "Me")
    (repo / "pyproject.toml").write_text(PYPROJECT)
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_add.py").write_text("from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    subprocess.run(["uv", "lock", "--quiet"], cwd=repo, check=True)
    git(repo, "add", "-A")
    now = time.time()
    git(repo, "commit", "-q", "-m", "base: add() with a test", when=now - 3 * 86400)
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n")
    (repo / "tests" / "test_mul.py").write_text("from calc import mul\n\n\ndef test_mul():\n    assert mul(4, 5) == 20\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "feat: add a mul() function that multiplies two numbers", when=now - 2 * 86400)
    return repo


@pytest.fixture(scope="module")
def prepared(fixture_repo, tmp_path_factory):
    root = Path("/private/tmp") / f"aimpg-e2e-{int(time.time())}"
    layout = Layout(root)
    (commit,), _ = candidates(str(fixture_repo), time.time() - 30 * 86400, cutoff=0)
    with AllowlistProxy() as proxy:
        check = precheck(commit, layout, proxy)
    solution = make_solution(commit, tmp_path_factory.mktemp("sol") / "solution.tar")
    yield layout, commit, check, solution
    shutil.rmtree(root, ignore_errors=True)


def cfg(**kw):
    return Config(model="claude-sonnet-5", per_run_budget_usd=0.5, total_cap_usd=10, parallel=2, **kw)


def test_candidate_and_precheck(prepared):
    _, commit, check, _ = prepared
    assert commit.task == "feat: add a mul() function that multiplies two numbers"
    assert commit.test_files == ["tests/test_mul.py"] and commit.code_files == ["calc.py"]
    assert check.ok, check
    assert check.details == {"passed_on_parent": [False, False], "passed_on_commit": [True, True]}


def test_solving_agent_passes_with_tokens_from_transcript(prepared):
    layout, commit, _, solution = prepared
    rec = run_one(commit, fake("solve"), 0, layout, cfg(), solution=solution)
    assert rec.outcome == "passed", rec.note
    assert rec.tokens_check == "ok"
    assert len(rec.usages) == 3 and rec.usages[0] == [5, 3000, 9000, 400]


def test_idle_agent_fails_hidden_tests(prepared):
    layout, commit, _, _ = prepared
    rec = run_one(commit, fake("nothing"), 0, layout, cfg())
    assert rec.outcome == "tests_failed"
    assert rec.usages  # its tokens still count


def test_hidden_tests_are_not_in_the_agent_workspace(prepared):
    layout, commit, _, _ = prepared
    from aimpg.replay import workspace

    work = workspace.clone_for_run(commit, layout, layout.prepared(commit.sha), "peek")
    assert not (work / "tests" / "test_mul.py").exists()
    assert subprocess.run(["git", "-C", str(work), "log", "--oneline"], capture_output=True, text=True).stdout.count("\n") == 1


def test_crash_timeout_and_budget_outcomes(prepared):
    layout, commit, _, _ = prepared
    assert run_one(commit, fake("crash"), 0, layout, cfg()).outcome == "agent_error"
    assert run_one(commit, fake("budget"), 0, layout, cfg()).outcome == "budget_hit"
    assert run_one(commit, fake("timeout"), 0, layout, cfg(timeout=3)).outcome == "timeout"


def test_escape_attempts_all_fail(prepared):
    layout, commit, _, _ = prepared
    rec = run_one(commit, fake("escape"), 0, layout, cfg())
    assert "escapes:" not in rec.note, rec.note
    assert "github.com:443" in rec.note  # the proxy saw and blocked it


def test_batch_writes_every_run_and_respects_the_cap(prepared, tmp_path):
    layout, commit, _, solution = prepared
    results = tmp_path / "results.jsonl"
    batch = Batch([commit], [fake("solve"), fake("nothing")], repeats=2, layout=layout, cfg=cfg(), results=results, solutions={commit.sha: solution})
    records = batch.run()
    assert sorted(r.outcome for r in records) == ["passed", "passed", "tests_failed", "tests_failed"]
    assert len(results.read_text().splitlines()) == 4
    loaded, excluded = load_results(results)
    assert len(loaded) == 4 and not excluded

    # A run starts only if its worst case (the per-run budget) still fits under the cap.
    too_small = Batch([commit], [fake("nothing")], repeats=3, layout=layout, cfg=Config("m", 0.5, 0.4, parallel=1), results=tmp_path / "a.jsonl")
    assert too_small.run() == []
    nearly_spent = Batch([commit], [fake("nothing")], repeats=3, layout=layout, cfg=Config("m", 0.5, 1.0, parallel=1), results=tmp_path / "b.jsonl", spent=0.6)
    assert nearly_spent.run() == []
    room_for_one = Batch([commit], [fake("nothing")], repeats=3, layout=layout, cfg=Config("m", 0.5, 1.0, parallel=1), results=tmp_path / "c.jsonl", spent=0.49)
    # 0.49 → run 1 (reserve to 0.99, settles at 0.50) → run 2 (reserve 1.00, settles 0.51)
    # → run 3 would reserve 1.01 > 1.00: not started.
    assert len(room_for_one.run()) == 2
