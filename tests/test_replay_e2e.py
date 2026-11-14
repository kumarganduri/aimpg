"""Full replay pipeline with the free fake agent (macOS + network for PyPI).

Fixture repo:  c0 "base" (add() + its test)  →  c1 adds mul() + test_mul
The replay starts from c0 with c1's tests hidden; Phase C runs them.
"""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from conftest import REAL_HOME, touched

from aimpg.replay import sandbox
from aimpg.replay.run import Batch, Config, load_results, make_solution, run_one
from aimpg.replay.select import candidates, precheck
from aimpg.replay.setups import fake
from aimpg.replay.workspace import Layout
from aimpg.replay.proxy import AllowlistProxy

pytestmark = [pytest.mark.network, pytest.mark.skipif(not sandbox.available(), reason=str(sandbox.unavailable_reason()))]

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
    root = sandbox.TMP / f"aimpg-e2e-{int(time.time())}"
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
    assert commit.task.startswith("feat: add a mul() function that multiplies two numbers")
    assert "calc.py: mul" in commit.task  # interface hint: the new name the hidden test imports
    assert commit.test_files == ["tests/test_mul.py"] and commit.code_files == ["calc.py"]
    assert check.ok, check
    assert check.details == {
        "passed_on_parent": [False, False],
        "passed_on_commit": [True, True],
        "passed_with_stubs": False,  # `mul = None` can't satisfy the test: the hint gives nothing away
        "hint": {"calc.py": ["mul"]},
    }


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


def test_commit_whose_tests_only_check_names_is_dropped(tmp_path_factory):
    # The hidden test passes as soon as the name exists: an interface hint would
    # hand over the answer, so the stub check must reject this commit.
    repo = tmp_path_factory.mktemp("giveaway")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "me@example.com")
    git(repo, "config", "user.name", "Me")
    (repo / "pyproject.toml").write_text(PYPROJECT)
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    subprocess.run(["uv", "lock", "--quiet"], cwd=repo, check=True)
    git(repo, "add", "-A")
    now = time.time()
    git(repo, "commit", "-q", "-m", "base: the add function", when=now - 3 * 86400)
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n\n\nVERSION = '1.0'\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_version.py").write_text("import calc\n\n\ndef test_has_version():\n    calc.VERSION  # only checks the name exists\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "feat: expose a version string for the calc module", when=now - 2 * 86400)
    (commit,), _ = candidates(str(repo), now - 30 * 86400, cutoff=0)
    layout = Layout(sandbox.TMP / f"aimpg-e2e-giveaway-{int(now)}")
    try:
        with AllowlistProxy() as proxy:
            check = precheck(commit, layout, proxy)
    finally:
        shutil.rmtree(layout.root, ignore_errors=True)
    assert not check.ok
    assert "hint would give it away" in check.reason
    assert check.details["passed_with_stubs"] is True


def test_tests_mode_shows_tests_but_cheating_cannot_pass(prepared):
    from aimpg.replay.run import task_text

    layout, commit, _, solution = prepared
    assert "tests/test_mul.py" in task_text(commit, "tests") and "without editing them" in task_text(commit, "tests")
    cheat = run_one(commit, fake("cheat"), 0, layout, cfg(task_mode="tests"))
    assert "tests/test_mul.py" in cheat.note  # it really saw and rewrote the shown test
    assert cheat.outcome == "tests_failed"  # originals restored before judging
    solved = run_one(commit, fake("solve"), 0, layout, cfg(task_mode="tests"), solution=solution)
    assert solved.outcome == "passed"


def test_hint_mode_keeps_tests_hidden(prepared):
    from aimpg.replay.run import task_text

    _, commit, _, _ = prepared
    assert "tests/test_mul.py" not in task_text(commit, "hint")
    assert task_text(commit, "hint") == commit.task


def test_no_credit_stops_the_batch_and_is_never_held_against_the_agent(prepared, tmp_path):
    layout, commit, _, _ = prepared
    rec = run_one(commit, fake("nocredit"), 0, layout, cfg())
    assert rec.outcome == "account_error" and "Credit balance" in rec.note

    results = tmp_path / "r.jsonl"
    batch = Batch([commit], [fake("nocredit")], repeats=3, layout=layout, cfg=Config("m", 0.5, 10, parallel=1), results=results)
    batch.run()
    assert batch.stopped
    assert len(results.read_text().splitlines()) == 1  # stopped after the first, no more launches
    loaded, _ = load_results(results)
    assert loaded == []  # not counted as an agent failure


def test_parallel_runs_wait_for_reservations_instead_of_skipping(prepared, tmp_path):
    # Cap $1.20 with $0.50 reserved per run and 3 in parallel: only 2 fit at once,
    # but each settles to $0.01, so all 6 runs must happen (they used to be skipped).
    layout, commit, _, _ = prepared
    batch = Batch([commit], [fake("nothing")], repeats=6, layout=layout, cfg=Config("m", 0.5, 1.2, parallel=3), results=tmp_path / "w.jsonl")
    assert len(batch.run()) == 6
    assert batch.spent < 0.1


def test_codex_agent_tokens_from_rollout_log_and_openai_only(prepared):
    layout, commit, _, solution = prepared
    rec = run_one(commit, fake("solve", agent="codex"), 0, layout, cfg(keys={"OPENAI_API_KEY": "sk-test"}), solution=solution)
    assert rec.outcome == "passed", rec.note
    assert rec.agent == "codex" and rec.model == "gpt-fake"
    assert rec.usages[0] == [12000, 0, 0, 400] and len(rec.usages) == 3
    assert rec.cost_known is False and rec.cost_usd == 0  # GPT prices aren't in the table: never guessed
    assert rec.tokens_check.startswith("unverified")


def test_codex_no_quota_is_an_account_error(prepared):
    layout, commit, _, _ = prepared
    rec = run_one(commit, fake("nocredit", agent="codex"), 0, layout, cfg())
    assert rec.outcome == "account_error" and "insufficient_quota" in rec.note


def test_any_command_is_judged_by_tests_without_tokens(prepared):
    layout, commit, _, solution = prepared
    solved = run_one(commit, fake("solve", agent="cmd"), 0, layout, cfg(), solution=solution)
    assert solved.outcome == "passed" and solved.usages == [] and solved.cost_known is False
    assert solved.tokens_check.startswith("not measurable")
    assert run_one(commit, fake("nothing", agent="cmd"), 0, layout, cfg()).outcome == "tests_failed"
    assert run_one(commit, fake("crash", agent="cmd"), 0, layout, cfg()).outcome == "agent_error"


def test_verify_record_from_real_runs_is_private_and_rechecks(prepared, tmp_path):
    from aimpg.replay.verify import build_record, check

    layout, commit, _, solution = prepared
    plain, other = fake("solve"), fake("solve", agent="codex")
    results = tmp_path / "v.jsonl"
    Batch([commit], [plain, other], repeats=1, layout=layout, cfg=cfg(keys={"OPENAI_API_KEY": "sk-test"}),
          results=results, solutions={commit.sha: solution}).run()
    records, _ = load_results(results)
    record = build_record(records, claim="codex is cheaper", baseline=plain, challenger=other, repo=str(commit.repo),
                          task_mode="hint", public=False)
    text = json.dumps(record)
    assert commit.sha not in text and commit.repo not in text and "sk-test" not in text
    assert record["verdict"]["challenger"]["solved"] == 1 and record["verdict"]["challenger"]["usd_per_solved"] is None
    assert check(record)[0]


# The real agent CLIs run only when asked (AIMPG_REAL_AGENT_TESTS=1): they call OpenAI/Anthropic,
# and a plain `pytest` once left codex's arg0 helper folder in the developer's real ~/.codex.
REAL_AGENTS = os.environ.get("AIMPG_REAL_AGENT_TESTS") == "1"


def _real_codex_state() -> set[Path]:
    """What's in the real ~/.codex (top level, and tmp/ where the helper folder appeared)."""
    root = REAL_HOME / ".codex"
    n = len(touched)
    found = set(root.iterdir()) | set((root / "tmp").rglob("*")) if root.is_dir() else set()
    del touched[n:]  # this look is the check itself, not a leak
    return found


@pytest.mark.skipif(not REAL_AGENTS, reason="runs the real codex CLI; set AIMPG_REAL_AGENT_TESTS=1")
@pytest.mark.skipif(shutil.which("codex") is None, reason="codex not installed")
def test_real_codex_reaches_openai_through_the_sandbox_and_key_file_is_deleted(prepared):
    """Free: a fake key gets OpenAI's 401, which proves login, sandbox and proxy all work."""
    from aimpg.replay.setups import codex

    layout, commit, _, _ = prepared
    before = _real_codex_state()
    rec = run_one(commit, codex(), 0, layout, cfg(keys={"OPENAI_API_KEY": "sk-not-a-real-key-000"}, timeout=180))
    print(rec.outcome, rec.note)
    assert rec.outcome == "account_error", (rec.outcome, rec.note)
    assert not list(layout.root.rglob("auth.json"))  # the key never stays on disk
    assert _real_codex_state() <= before  # a run never writes the user's own ~/.codex


@pytest.mark.skipif(not REAL_AGENTS, reason="runs the real claude CLI; set AIMPG_REAL_AGENT_TESTS=1")
@pytest.mark.skipif(shutil.which("claude") is None, reason="claude not installed")
def test_real_claude_code_starts_in_the_sandbox_and_reaches_anthropic(prepared):
    """Free: a fake key gets Anthropic's 401, which proves Claude Code starts and talks through the proxy."""
    from aimpg.replay.setups import BASELINE

    layout, commit, _, _ = prepared
    rec = run_one(commit, BASELINE, 0, layout, Config(model="claude-sonnet-5-5", per_run_budget_usd=0.5, total_cap_usd=1,
                                                       api_key="sk-ant-not-a-real-key-000", timeout=240))
    assert rec.outcome == "account_error", (rec.outcome, rec.note)


def test_fresh_session_chain_pairs_continue_and_fresh_on_task_two(prepared, monkeypatch):
    from aimpg.replay import chain

    layout, commit, _, solution = prepared
    solve = fake("solve")
    for name, attr in (("chain-first", "FIRST"), ("chain-continue", "CONTINUE"), ("chain-fresh", "FRESH")):
        monkeypatch.setattr(chain, attr, chain.Setup(name, program=solve.program, configure=solve.configure, requires=()))
    # the fixture has one qualifying commit: it plays both steps (the mechanics are what's tested)
    first, cont, fresh = chain.run_chain(commit, commit, 0, layout, cfg(), solutions={commit.sha: solution})
    assert (first.outcome, cont.outcome, fresh.outcome) == ("passed",) * 3, (cont.note, fresh.note)
    assert cont.commit == fresh.commit == commit.sha and (cont.setup, fresh.setup) == ("chain-continue", "chain-fresh")
    assert cont.extra["prev_outcome"] == "passed" and len(cont.usages) == 3  # task 2's requests only
    assert cont.cost_usd > 0 and fresh.cost_usd > 0  # priced from tokens at list prices


def test_codex_quota_recorded_only_in_its_session_log_is_an_account_error(prepared):
    layout, commit, _, _ = prepared
    rec = run_one(commit, fake("quota-in-log", agent="codex"), 0, layout, cfg())
    assert rec.outcome == "account_error" and "Quota exceeded" in rec.note


def test_said_done_is_recorded_whether_or_not_it_was_true(prepared):
    layout, commit, _, solution = prepared
    asked = cfg(ask_status=True)
    solved = run_one(commit, fake("solve"), 0, layout, asked, solution=solution, run_id="claim-solve")
    assert solved.outcome == "passed" and solved.extra["claimed"] == "done"
    idle = run_one(commit, fake("nothing"), 0, layout, asked, run_id="claim-idle")
    assert idle.outcome == "tests_failed" and idle.extra["claimed"] == "done"  # said done, tests failed
    honest = run_one(commit, fake("honest"), 0, layout, asked, run_id="claim-honest")
    assert honest.outcome == "tests_failed" and honest.extra["claimed"] == "not_done"
    codex = run_one(commit, fake("solve", agent="codex"), 0, layout, cfg(ask_status=True, keys={"OPENAI_API_KEY": "sk-test"}),
                    solution=solution, run_id="claim-codex")
    assert codex.outcome == "passed" and codex.extra["claimed"] == "done"
    not_asked = run_one(commit, fake("solve"), 0, layout, cfg(), solution=solution, run_id="claim-not-asked")
    assert "claimed" not in not_asked.extra and not_asked.extra["files_changed"] == 1


def test_a_worker_on_another_model_is_priced_and_counted_separately(prepared):
    from aimpg.replay import setups as S

    layout, commit, _, solution = prepared
    setup = fake("delegate")
    setup.measure = S._delegation  # what the planner setup measures after each run
    rec = run_one(commit, setup, 0, layout, cfg(), solution=solution, run_id="delegate")
    assert rec.outcome == "passed", rec.note
    assert rec.tokens_check == "ok"  # the worker's tokens are in the transcript total and in modelUsage
    assert len(rec.usages) == 5 and rec.extra["subagent_requests"] == 2
    assert rec.extra["by_model"]["claude-haiku-5-5"]["requests"] == 2 and rec.extra["by_model"]["claude-haiku-5-5"]["usd"] > 0
    assert rec.extra["by_model"]["claude-sonnet-5"]["requests"] == 3
    assert (rec.extra["delegations"], rec.extra["orchestrator_edits"], rec.extra["worker_edits"]) == (1, 0, 2)

