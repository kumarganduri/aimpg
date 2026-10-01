import time
from datetime import datetime, timezone

import pytest

from aimpg.attribution import NO_COMMIT_YET, NOT_IN_REPO, attribute
from aimpg.cli import main
from aimpg.logs import parse_logs

from conftest import assistant, bash_call, tool_result
from gitrepo import DAY, Repo


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@pytest.fixture
def world(tmp_path, write_log):
    """A repo with two Claude commits, one slow-hook commit, and known request segments.

    session s1 in repo:   r1 r2 [c1] r3 [c2 (152s hook)] r4      session s2, no repo: r5
    """
    now = time.time()
    t = now - 20 * DAY
    repo = Repo(tmp_path / "repo")
    repo.commit({"base.py": "print('base line')\n"}, "base", t - DAY)
    cwd = str(repo.path)
    c1 = repo.commit({"a.py": "def first_feature():\n    return 1\n"}, "feat: first", t + 100)
    c2 = repo.commit({"b.py": "def second_feature():\n    return 2\n"}, "feat: second", t + 400)
    elsewhere = tmp_path / "not-a-repo"
    elsewhere.mkdir()
    rows = [
        assistant("r1", iso(t + 10), cwd=cwd, out=100),
        assistant("r2", iso(t + 50), cwd=cwd, out=100),
        bash_call("tu1", "git commit -m 'feat: first'", iso(t + 99), cwd=cwd),
        tool_result("tu1", iso(t + 101), cwd=cwd),
        assistant("r3", iso(t + 200), cwd=cwd, out=100),
        bash_call("tu2", "git commit -m 'feat: second'", iso(t + 248), cwd=cwd),  # hooks run 152s
        tool_result("tu2", iso(t + 400), cwd=cwd),
        assistant("r4", iso(t + 500), cwd=cwd, out=100),
        assistant("r5", iso(t + 600), session="s2", cwd=str(elsewhere), out=100),
    ]
    log = write_log(rows)
    return dict(now=now, since=now - 30 * DAY, parsed=parse_logs([log]), c1=c1, c2=c2, log=log, repo=repo)


def test_exact_matches_and_time_segments(world):
    result = attribute(world["parsed"], world["since"], world["now"])
    tasks = {t.sha: t for t in result.tasks}
    assert set(tasks) == {world["c1"], world["c2"]}
    assert [r.id for r in tasks[world["c1"]].requests] == ["r1", "r2", "req_tu1"]
    assert [r.id for r in tasks[world["c2"]].requests] == ["r3", "req_tu2"]
    assert all(t.attribution == "exact" for t in tasks.values())
    assert all(t.status == "kept" for t in tasks.values())


def test_slow_hook_commit_is_matched_by_call_interval(world):
    # c2 landed 152s after its commit call started; a ±30s rule would miss it.
    result = attribute(world["parsed"], world["since"], world["now"])
    assert world["c2"] in {t.sha for t in result.tasks}


def test_unattributed_buckets(world):
    result = attribute(world["parsed"], world["since"], world["now"])
    assert [r.id for r in result.unattributed[NO_COMMIT_YET]] == ["r4"]
    assert [r.id for r in result.unattributed[NOT_IN_REPO]] == ["r5"]


def test_window_excludes_old_requests(world):
    result = attribute(world["parsed"], world["now"] - DAY, world["now"])
    assert result.tasks == []
    assert result.unattributed == {}


def test_commit_call_outside_interval_does_not_match(tmp_path, write_log):
    now = time.time()
    t = now - 20 * DAY
    repo = Repo(tmp_path / "repo")
    repo.commit({"a.py": "def only_feature():\n    return 1\n"}, "feat", t + 100)
    log = write_log([
        assistant("r1", iso(t + 10), cwd=str(repo.path)),
        bash_call("tu1", "git commit -m feat", iso(t + 200), cwd=str(repo.path)),
        tool_result("tu1", iso(t + 201), cwd=str(repo.path)),
    ])
    result = attribute(parse_logs([log]), now - 30 * DAY, now)
    assert result.tasks == []


def test_repo_without_default_branch_reports_git_error(tmp_path, write_log):
    now = time.time()
    t = now - 20 * DAY
    repo = Repo(tmp_path / "repo")
    repo.git("checkout", "-q", "-b", "trunk")  # no main/master/origin
    sha = repo.commit({"a.py": "def only_feature():\n    return 1\n"}, "feat", t + 100)
    repo.git("branch", "-q", "-D", "main") if "main" in repo.git("branch") else None
    log = write_log([
        bash_call("tu1", "git commit -m feat", iso(t + 99), cwd=str(repo.path)),
        tool_result("tu1", iso(t + 101), cwd=str(repo.path)),
    ])
    result = attribute(parse_logs([log]), now - 30 * DAY, now)
    (task,) = result.tasks
    assert task.sha == sha and task.status == "git error"
    assert result.repos[str(repo.path.resolve())].error


def test_cli_report_end_to_end(world, capsys):
    assert main(["report", "--logs", str(world["log"].parent.parent)]) == 0
    out = capsys.readouterr().out
    assert "2 commits: 2 exact, 0 fuzzy, 0 grace" in out
    assert "kept" in out
    assert "no commit from this session yet" in out
    assert "not in a git repo" in out
    assert "coverage 100.0%" in out


def test_cli_without_logs_dir(tmp_path, capsys):
    assert main(["report", "--logs", str(tmp_path / "missing")]) == 0
    assert "No Claude Code logs found" in capsys.readouterr().out


def test_cli_with_logs_but_nothing_in_window(write_log, tmp_path, capsys):
    log = write_log([assistant("old", "2020-01-01T00:00:00.000Z")])
    assert main(["report", "--logs", str(log.parent.parent)]) == 0
    assert "No AI requests in this window." in capsys.readouterr().out


@pytest.fixture
def hand_world(tmp_path, write_log):
    """Session edits a.py via Edit and b.py via sed, then you commit by hand.

    requests r1..r3 (no commit calls) → hand commits: h1 touches a.py (during),
    h2 touches b.py (+30 min, grace), h3 touches unrelated.py (ignored),
    late touches a.py but 3h after the session (outside grace).
    """
    now = time.time()
    t = now - 20 * DAY
    repo = Repo(tmp_path / "repo")
    repo.commit({"a.py": "x = 0\n", "b.py": "y = 0\n", "unrelated.py": "z = 0\n"}, "base", t - DAY)
    cwd = str(repo.path)
    h1 = repo.commit({"a.py": "x = 1\nx_more = 2\nx_even_more = 3\n"}, "hand: a", t + 150)
    h2 = repo.commit({"b.py": "y = 1\n"}, "hand: b", t + 300 + 1800)
    repo.commit({"unrelated.py": "z = 1\n"}, "hand: unrelated", t + 200)
    late = repo.commit({"a.py": "x = 9\n"}, "hand: late", t + 300 + 3 * 3600)
    rows = [
        assistant("r1", iso(t + 10), cwd=cwd, out=100, content=[
            {"type": "tool_use", "id": "e1", "name": "Edit", "input": {"file_path": f"{cwd}/a.py"}}]),
        bash_call("tu_sed", "sed -i '' 's/0/1/' b.py", iso(t + 100), cwd=cwd),
        assistant("r3", iso(t + 300), cwd=cwd, out=100),
    ]
    log = write_log(rows)
    return dict(now=now, since=now - 30 * DAY, parsed=parse_logs([log]), h1=h1, h2=h2, late=late, repo=repo)


def test_fuzzy_and_grace_matches_split_by_lines(hand_world):
    result = attribute(hand_world["parsed"], hand_world["since"], hand_world["now"])
    tasks = {t.sha: t for t in result.tasks}
    assert set(tasks) == {hand_world["h1"], hand_world["h2"]}
    assert tasks[hand_world["h1"]].attribution == "fuzzy"
    assert tasks[hand_world["h2"]].attribution == "grace"
    # h1 changed 4 lines (1 deleted + 3 added), h2 changed 2 → 4/6 and 2/6 of every request
    assert tasks[hand_world["h1"]].weights == pytest.approx([4 / 6] * 3)
    assert tasks[hand_world["h2"]].weights == pytest.approx([2 / 6] * 3)
    assert NO_COMMIT_YET not in result.unattributed


def test_fuzzy_energy_is_conserved(hand_world):
    from aimpg.energy import total_wh, weighted_wh

    result = attribute(hand_world["parsed"], hand_world["since"], hand_world["now"])
    split = sum(
        weighted_wh(t.requests, t.weights).high + weighted_wh(t.lead_up, t.lead_up_weights).high
        for t in result.tasks
    )
    assert split == pytest.approx(total_wh(hand_world["parsed"].requests).high)


def test_no_touched_files_means_no_fuzzy_match(world):
    # In `world`, r4 comes after the last exact commit and the session edited nothing.
    result = attribute(world["parsed"], world["since"], world["now"])
    assert {t.attribution for t in result.tasks} == {"exact"}


def test_exact_commits_are_not_reclaimed_by_fuzzy(tmp_path, write_log):
    now = time.time()
    t = now - 20 * DAY
    repo = Repo(tmp_path / "repo")
    repo.commit({"a.py": "x = 0\n"}, "base", t - DAY)
    cwd = str(repo.path)
    claude = repo.commit({"a.py": "x = 1\n"}, "claude commit", t + 100)
    edit = [{"type": "tool_use", "id": "e1", "name": "Edit", "input": {"file_path": f"{cwd}/a.py"}}]
    log = write_log([
        bash_call("tu1", "git commit -m c", iso(t + 99), session="A", cwd=cwd),
        tool_result("tu1", iso(t + 101), session="A", cwd=cwd),
        assistant("b1", iso(t + 50), session="B", cwd=cwd, content=edit),
    ])
    result = attribute(parse_logs([log]), now - 30 * DAY, now)
    (task,) = result.tasks
    assert task.sha == claude and task.attribution == "exact"
    assert [r.id for r in result.unattributed[NO_COMMIT_YET]] == ["b1"]


def test_teammates_commits_are_never_fuzzy_matched(tmp_path, write_log):
    # After a pull, a teammate's merged PR touching your file lands in the window.
    now = time.time()
    t = now - 20 * DAY
    repo = Repo(tmp_path / "repo")
    repo.commit({"a.py": "x = 0\n"}, "base", t - DAY)
    cwd = str(repo.path)
    repo.git("-c", "user.email=teammate@example.com", "commit", "-q", "--allow-empty", "-m", "noop", when=t)
    (repo.path / "a.py").write_text("x = 'teammate'\n")
    repo.git("add", "a.py")
    theirs = repo.git("-c", "user.email=teammate@example.com", "commit", "-q", "-m", "feat: theirs (#42)", when=t + 100)
    edit = [{"type": "tool_use", "id": "e1", "name": "Edit", "input": {"file_path": f"{cwd}/a.py"}}]
    log = write_log([assistant("r1", iso(t + 10), cwd=cwd, content=edit), assistant("r2", iso(t + 200), cwd=cwd)])
    result = attribute(parse_logs([log]), now - 30 * DAY, now)
    assert result.tasks == []
    assert len(result.unattributed[NO_COMMIT_YET]) == 2


def test_no_user_email_means_no_fuzzy_guessing(hand_world, monkeypatch):
    hand_world["repo"].git("config", "--unset", "user.email")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")  # ignore the developer's ~/.gitconfig
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    result = attribute(hand_world["parsed"], hand_world["since"], hand_world["now"])
    assert result.tasks == []
    assert len(result.unattributed[NO_COMMIT_YET]) == 3


def test_session_cwd_elsewhere_still_attributed(tmp_path, write_log):
    # Common real case: the session's cwd is another project, but it commits
    # into this repo with `cd repo && git commit`. Requests follow the commit.
    now = time.time()
    t = now - 20 * DAY
    repo = Repo(tmp_path / "repo")
    repo.commit({"a.py": "x = 0\n"}, "base", t - DAY)
    other = tmp_path / "other-project"
    other.mkdir()
    sha = repo.commit({"a.py": "x = 1\n"}, "feat", t + 100)
    log = write_log([
        assistant("r1", iso(t + 10), cwd=str(other)),
        bash_call("tu1", f"cd {repo.path} && git commit -m feat", iso(t + 99), cwd=str(other)),
        tool_result("tu1", iso(t + 101), cwd=str(other)),
    ])
    result = attribute(parse_logs([log]), now - 30 * DAY, now)
    (task,) = result.tasks
    assert task.sha == sha
    assert [r.id for r in task.requests] == ["r1", "req_tu1"]


def test_work_before_a_long_break_is_lead_up_not_direct(tmp_path, write_log):
    #   r1 r2 ··· 5h break ··· r3 [c1]   → direct: r3 + commit call, lead-up: r1 r2
    now = time.time()
    t = now - 20 * DAY
    repo = Repo(tmp_path / "repo")
    repo.commit({"a.py": "x = 0\n"}, "base", t - DAY)
    cwd = str(repo.path)
    late = t + 5 * 3600
    sha = repo.commit({"a.py": "x = 1\n"}, "feat", late + 100)
    log = write_log([
        assistant("r1", iso(t + 10), cwd=cwd),
        assistant("r2", iso(t + 60), cwd=cwd),
        assistant("r3", iso(late + 10), cwd=cwd),
        bash_call("tu1", "git commit -m feat", iso(late + 99), cwd=cwd),
        tool_result("tu1", iso(late + 101), cwd=cwd),
    ])
    (task,) = attribute(parse_logs([log]), now - 30 * DAY, now).tasks
    assert task.sha == sha
    assert [r.id for r in task.requests] == ["r3", "req_tu1"]
    assert [r.id for r in task.lead_up] == ["r1", "r2"]


def test_short_pauses_stay_direct(world):
    result = attribute(world["parsed"], world["since"], world["now"])
    assert all(t.lead_up == [] for t in result.tasks)


def test_receipt_shows_lead_up(tmp_path, write_log, capsys):
    now = time.time()
    t = now - 20 * DAY
    repo = Repo(tmp_path / "repo")
    repo.commit({"a.py": "x = 0\n"}, "base", t - DAY)
    cwd = str(repo.path)
    late = t + 5 * 3600
    repo.commit({"a.py": "x = 1\n"}, "feat", late + 100)
    log = write_log([
        assistant("r1", iso(t + 10), cwd=cwd),
        bash_call("tu1", "git commit -m feat", iso(late + 99), cwd=cwd),
        tool_result("tu1", iso(late + 101), cwd=cwd),
    ])
    assert main(["report", "--logs", str(log.parent.parent)]) == 0
    out = capsys.readouterr().out
    assert "of which lead-up" in out
    assert "Median direct energy per kept commit" in out
