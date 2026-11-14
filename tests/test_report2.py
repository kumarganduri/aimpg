"""What Report No. 2 needs from the harness: the "said done" field, the multi-file selection rule,
results by group, and the planner-with-worker setup. No agent runs here (see test_replay_e2e.py)."""

import argparse
import json

import pytest

from aimpg.replay import cli as replay_cli
from aimpg.replay import sandbox, select, study
from aimpg.replay import setups as S
from aimpg.replay.run import STATUS_ASK, Record, claim_of, codex_last_message, task_text
from aimpg.replay.verify import VerifyError, _spec, setup_from, setup_from_spec
from aimpg.replay.workspace import Commit, Precheck


def commit(i, files=1, repo="/r"):
    return Commit(repo, f"{i:040d}", "p", f"change number {i}", "", ["tests/test_a.py"], [f"a{k}.py" for k in range(files)], ts=float(i))


# ------------------------------------------------------------------ "done" claims

@pytest.mark.parametrize("message, claim", [
    ("All tests pass.\n\nSTATUS: DONE", "done"),
    ("**STATUS: DONE**", "done"),
    ("> status: not done\n", "not_done"),
    ("STATUS: NOT_DONE", "not_done"),
    ("I started with STATUS: DONE in mind.\nSTATUS: NOT DONE", "not_done"),  # the last status line counts
    ("It is done and everything works.", "none"),  # no status line: never guessed from the prose
    ("The STATUS: DONE line was asked for", "none"),  # the line must start with it
    ("", "none"),
])
def test_claim_is_read_only_from_a_status_line(message, claim):
    assert claim_of(message) == claim


def test_status_is_asked_the_same_way_in_both_task_modes():
    c = commit(1)
    assert task_text(c, "hint") == c.task  # unchanged unless a study asks
    for mode in ("hint", "tests"):
        assert task_text(c, mode, True) == task_text(c, mode) + "\n\n" + STATUS_ASK


def test_codex_last_message_ignores_code_and_command_output():
    out = "\n".join([
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Working on it.\nSTATUS: NOT DONE"}}),
        json.dumps({"type": "item.completed", "item": {"type": "file_change", "diff": "+ print('STATUS: DONE')"}}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Fixed.\nSTATUS: DONE"}}),
        "not json", json.dumps({"type": "turn.completed"}), json.dumps([1, 2]),
    ])
    assert claim_of(codex_last_message(out)) == "done"
    assert codex_last_message("") == ""


# ------------------------------------------------------------------ selection by files changed

def test_pick_without_a_quota_is_the_first_n():
    commits = [commit(i, files=1 + (i % 3 == 0)) for i in range(1, 9)]
    assert select.pick(commits, 4) == commits[:4]


def test_pick_takes_the_first_multi_file_commits_then_fills_in_date_order():
    commits = [commit(1), commit(2), commit(3, 2), commit(4), commit(5), commit(6, 3), commit(7, 2), commit(8)]
    got = select.pick(commits, 5, multi_min=2)
    assert [int(c.sha) for c in got] == [1, 2, 3, 4, 6]  # multi-file 3 and 6, then the earliest others
    assert sum(select.is_multi_file(c) for c in got) == 2
    # more multi-file commits than the quota: later ones still enter by date, like any other commit
    assert [int(c.sha) for c in select.pick([commit(1, 2), commit(2, 2), commit(3, 2), commit(4)], 3, multi_min=1)] == [1, 2, 3]
    # too few multi-file commits: all of them, and the rest single-file
    assert [int(c.sha) for c in select.pick(commits[:5], 4, multi_min=3)] == [1, 2, 3, 4]
    assert [int(c.sha) for c in select.pick(commits, 2, multi_min=5)] == [3, 6]  # a pilot's 2 commits


def test_select_keeps_looking_for_multi_file_commits_and_lists_what_it_passed_over(tmp_path, monkeypatch):
    commits = [commit(1), commit(2), commit(3), commit(4, 2), commit(5), commit(6, 2), commit(7, 2), commit(8)]
    checked = []

    def precheck(c, layout, proxy):
        checked.append(int(c.sha))
        return Precheck(int(c.sha) != 4, "ok" if int(c.sha) != 4 else "flaky tests")  # the first multi-file one doesn't qualify

    class Proxy:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(replay_cli, "STATE", tmp_path)
    monkeypatch.setattr(replay_cli, "secure_root", lambda: tmp_path / "root")
    monkeypatch.setattr(replay_cli, "AllowlistProxy", Proxy)
    monkeypatch.setattr(select, "candidates", lambda *a, **k: (commits, {}))
    monkeypatch.setattr(select, "precheck", precheck)
    args = argparse.Namespace(days=365, cutoff="2026-07-01", max=2, any_author=True, multi_min=2)
    assert replay_cli._select(tmp_path / "repo", args) == 0
    assert checked == [1, 2, 4, 6, 7]  # after 2 qualified, single-file 3 and 5 are not test-run; it stops at the second multi-file
    out = replay_cli._state(tmp_path / "repo")
    assert [int(c.sha) for c in select.load(out / "selected.jsonl")] == [1, 2, 6, 7]
    manifest = json.loads((out / "selected.manifest.json").read_text())
    assert [int(s) for s in manifest["candidates"]] == [1, 2, 3, 4, 5, 6, 7]  # everything looked at, in order
    assert [int(s) for s in manifest["not_checked_single_file"]] == [3, 5]
    assert [int(s) for s in manifest["qualified"]] == [1, 2, 6, 7]


def test_select_without_a_quota_is_unchanged(tmp_path, monkeypatch):
    commits = [commit(i, files=2) for i in range(1, 6)]

    class Proxy:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(replay_cli, "STATE", tmp_path)
    monkeypatch.setattr(replay_cli, "secure_root", lambda: tmp_path / "root")
    monkeypatch.setattr(replay_cli, "AllowlistProxy", Proxy)
    monkeypatch.setattr(select, "candidates", lambda *a, **k: (commits, {}))
    monkeypatch.setattr(select, "precheck", lambda c, layout, proxy: Precheck(True, "ok"))
    assert replay_cli._select(tmp_path / "repo", argparse.Namespace(days=365, cutoff="2026-07-01", max=3, any_author=True)) == 0
    manifest = json.loads((replay_cli._state(tmp_path / "repo") / "selected.manifest.json").read_text())
    assert len(manifest["qualified"]) == 3 and len(manifest["candidates"]) == 3 and "not_checked_single_file" not in manifest


# ------------------------------------------------------------------ results by group

def rec(commit_, setup, passed, cost=0.1, repeat=0, **extra):
    return Record(f"{commit_}{setup}{repeat}", "/r", commit_, setup, repeat, "passed" if passed else "tests_failed",
                  passed, "m", cost, 10.0, [[100, 0, 0, 0]], "ok", "", True, "claude", extra)


def test_results_by_preset_task_groups_and_by_passes():
    records = []
    for k in range(3):
        records += [rec("easy", "cheap", True, 0.01, k, description_words=80, code_lines=30),
                    rec("easy", "strong", True, 0.2, k, description_words=80, code_lines=30),
                    rec("hard", "cheap", False, 0.02, k, description_words=14, code_lines=31),
                    rec("hard", "strong", k == 0, 0.5, k, description_words=14, code_lines=31),
                    rec("old", "cheap", True, 0.01, k)]  # made before aimpg recorded the task's shape
    thin, full = study.by_task(records, ["cheap", "strong"], "description")
    assert (thin[0], thin[1]) == ("under 15 words", 1) and [(a.solved, a.runs) for a in thin[2]] == [(0, 3), (1, 3)]
    assert (full[0], full[1]) == ("15 words or more", 1) and [(a.solved, a.runs) for a in full[2]] == [(3, 3), (3, 3)]
    small, large = study.by_task(records, ["cheap", "strong"], "size")  # 30 lines is still small, 31 is large
    assert (small[1], large[1]) == (1, 1) and [(a.solved, a.runs) for a in large[2]] == [(0, 3), (1, 3)]
    assert {r.commit for r in study.subset(records, "size", "more than 30")} == {"hard"}
    bands = {label: (n, [(a.solved, a.runs) for a in arms]) for label, n, arms in study.by_passes(records, ["cheap", "strong"])}
    assert bands == {"easy": (2, [(6, 6), (3, 3)]), "hard": (1, [(0, 3), (1, 3)])}
    text = study.render_extras(records, ["cheap", "strong"])
    assert "| under 15 words | 1 | 0 of 3 | 1 of 3, $1.50 per solved |" in text
    assert "| more than 30 | 1 | 0 of 3 | 1 of 3, $1.50 per solved |" in text and "Exploratory" in text


def test_how_often_done_is_true():
    records = [rec("a", "cheap", True, claimed="done"), rec("b", "cheap", False, claimed="done"), rec("c", "cheap", False, claimed="done"),
               rec("d", "cheap", False, claimed="not_done"), rec("e", "cheap", True, claimed="not_done"), rec("f", "cheap", True, claimed="none"),
               rec("g", "cheap", False)]  # not asked: not counted
    assert study.claims(records) == {"cheap": {"runs": 6, "said_done": 3, "said_done_but_failed": 2, "said_not_done": 2,
                                               "said_not_done_but_passed": 1, "no_status_line": 1}}
    assert "| cheap | 6 | 3 | 2 (67%) | 2 | 1 | 1 |" in study.render_extras(records, ["cheap"])
    assert study.render_extras([rec("a", "cheap", True)], ["cheap"]).startswith("Exploratory")  # nothing claimed, nothing claimed about it


def test_planner_behaviour_is_reported_as_it_really_was():
    by = {"claude-opus-5-5": {"requests": 4, "usd": 0.3}, "claude-haiku-5-5": {"requests": 9, "usd": 0.1}}
    records = [rec("a", "planner@opus+haiku", True, 0.4, delegations=1, orchestrator_edits=0, worker_edits=3, by_model=by),
               rec("b", "planner@opus+haiku", False, 0.4, delegations=0, orchestrator_edits=2, worker_edits=0, by_model=by)]
    assert ("planner@opus+haiku: the orchestrator delegated in 1 of 2 runs and edited files itself in 1. "
            "Cost by model: claude-haiku-5-5 25%, claude-opus-5-5 75%.") in study.render_extras(records, ["planner@opus+haiku"])


# ------------------------------------------------------------------ planner with a worker

def test_planner_setup_passes_its_prompt_and_worker_to_claude_code(tmp_path):
    setup = setup_from("planner:opus+haiku")
    assert setup.name == "planner@opus+haiku" and setup.model == "claude-opus-5-5"
    argv = setup.argv("the task", "claude-sonnet-5-5", 2.0, tmp_path)
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5"
    assert argv[argv.index("--append-system-prompt") + 1] == S.PLANNER_PROMPT
    agents = json.loads(argv[argv.index("--agents") + 1])
    assert set(agents) == {"worker"} and agents["worker"]["model"] == "claude-haiku-5-5"
    assert agents["worker"]["prompt"] == S.WORKER_PROMPT
    assert setup_from("planner").name == "planner@opus+haiku"
    with pytest.raises(VerifyError):
        setup_from("planner:opus")


def test_planner_record_rebuilds_the_same_setup_and_private_records_hide_its_prompts(tmp_path):
    setup = setup_from("planner:opus+haiku")
    public = _spec(setup, True)
    again = setup_from_spec(public)
    assert again.name == setup.name and again.argv("t", "m", 1.0, tmp_path) == setup.argv("t", "m", 1.0, tmp_path)
    private = _spec(setup, False)
    assert "agents" not in private and "append_prompt" not in private and len(private["agents_sha256"]) == 64
    assert S.WORKER_PROMPT not in json.dumps(private)
    with pytest.raises(VerifyError, match="only a hash of its sub-agents"):
        setup_from_spec(private)


@pytest.mark.parametrize("agents", [
    {}, [], "worker", {"worker": "x"}, {"bad name!": {"description": "d", "prompt": "p"}},
    {"worker": {"description": "d"}}, {"worker": {"description": "d", "prompt": "p", "model": "--dangerously-skip"}},
    {"worker": {"description": "d", "prompt": "p", "model": 5}}, {f"w{i}": {"description": "d", "prompt": "p"} for i in range(9)},
])
def test_sub_agents_from_a_record_have_a_strict_shape(agents):
    with pytest.raises(S.SetupUnavailable):
        S.check_agents(agents)


def test_sub_agents_keep_only_description_prompt_and_model():
    got = S.check_agents({"worker": {"description": "d", "prompt": "p", "model": "haiku", "tools": ["Bash"],
                                     "hooks": {"PreToolUse": "curl evil"}, "mcpServers": {"x": {}}}})
    assert got == {"worker": {"description": "d", "prompt": "p", "model": "claude-haiku-5-5"}}


def test_delegation_is_counted_from_the_transcripts(tmp_path):
    folder = tmp_path / "projects" / "-work"
    folder.mkdir(parents=True)

    def row(side, *tools):
        return {"type": "assistant", "isSidechain": side,
                "message": {"content": [{"type": "tool_use", "id": i, "name": n} for i, n in tools] + [{"type": "text", "text": "x"}]}}

    rows = [row(False, ("t1", "Read"), ("t2", "Agent")), row(False, ("t2", "Agent")),  # repeated row: counted once
            row(True, ("t3", "Edit"), ("t4", "Write")), row(False, ("t5", "Edit")), row(False, ("t6", "Task")),
            {"type": "user", "message": {"content": [{"type": "tool_use", "id": "t7", "name": "Edit"}]}}, "broken"]
    (folder / "s.jsonl").write_text("".join((json.dumps(r) if isinstance(r, dict) else r) + "\n" for r in rows))
    assert S._delegation(tmp_path) == {"delegations": 2, "orchestrator_edits": 1, "worker_edits": 2}
    assert S._delegation(tmp_path / "none") == {"delegations": 0, "orchestrator_edits": 0, "worker_edits": 0}


def test_study_report_prints_the_group_tables_from_result_files(tmp_path, capsys, monkeypatch):
    from aimpg.cli import main

    monkeypatch.setattr(sandbox, "unavailable_reason", lambda: None)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"name": "r2", "cutoff": "2026-07-01", "commits_per_repo": 2, "repeats": 1, "baseline": "plain",
                                "challengers": ["haiku"], "repos": [{"path": str(tmp_path / "repo")}]}))
    runs = tmp_path / "runs"
    runs.mkdir()
    rows = [rec(c, s, p, 0.1, 0, description_words=w, code_lines=5, claimed="done").__dict__
            for c, w in (("a" * 40, 60), ("b" * 40, 8)) for s, p in (("claude-code", True), ("claude-code@haiku", w == 60))]
    (runs / "repo.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert main(["study", "report", str(plan), "--runs", str(runs)]) == 0
    out = capsys.readouterr().out
    assert "| under 15 words | 1 | 1 of 1, $0.10 per solved | 0 of 1 |" in out
    assert "| claude-code@haiku | 2 | 2 | 1 (50%) | 0 | 0 | 0 |" in out
    assert "Also reported" not in out and "Within tasks" not in out  # only when the plan asks for them


def test_a_model_name_can_never_look_like_a_flag():
    for bad in ("--dangerously-skip-permissions", "-m", ".hidden", ""):
        with pytest.raises(S.SetupUnavailable):
            S.check_model(bad)
    assert S.check_model("claude-haiku-5-5") == "claude-haiku-5-5" and S.check_model("gpt-6.1-sol") == "gpt-6.1-sol"


# ------------------------------------------------------------------ solve rate as a primary measure

def grid(rates, repos=6, commits=8, repeats=3, cost=None):
    """rates: setup -> share of commits it solves (the same commits every repeat, the first ones in each repo)."""
    out = []
    for r in range(repos):
        for c in range(commits):
            for s, rate in rates.items():
                for k in range(repeats):
                    out.append(Record(f"{r}-{c}-{s}-{k}", f"repo{r}", f"r{r}c{c}", s, k, "passed" if c < rate * commits else "tests_failed",
                                      c < rate * commits, "m", (cost or {}).get(s, 0.1), 10.0, [[100, 0, 0, 0]], "ok", "", True, "claude",
                                      {"description_words": 5 if c % 2 else 50, "code_lines": 10}))
    return out


def test_a_higher_solve_rate_is_a_headline_only_on_the_solve_rate_measure():
    records = grid({"cheap": 0.25, "planner": 0.75})
    c = study.compare(records, "cheap", "planner", primary="solved")
    study.holm([c])
    assert c.ratio_solved == pytest.approx(3.0) and c.ratio_solved_ci[0] > 1
    assert (c.cheaper_in_repos, c.higher_in_repos) == (0, 6) and c.headline
    assert "| solve rate |" in study.render([c]) and "+200%" in study.render([c]) and "(higher in 6)" in study.render([c])
    same = study.compare(grid({"cheap": 0.5, "planner": 0.5}), "cheap", "planner", primary="solved")
    study.holm([same])
    assert (same.cheaper_in_repos, same.higher_in_repos) == (0, 0) and not same.headline  # ties are neither


def test_solve_rates_compare_even_when_the_baseline_solved_nothing():
    c = study.compare(grid({"cheap": 0.0, "planner": 0.5}), "cheap", "planner", primary="solved")
    study.holm([c])
    assert c.ratio_solved is None and c.higher_in_repos == 6  # no ratio to a zero rate, but the direction is clear
    assert not c.headline  # and without an interval it is never a headline


def test_plan_comparisons_between_any_two_setups_with_secondary_ones_and_groups(tmp_path, capsys, monkeypatch):
    from aimpg.cli import main

    monkeypatch.setattr(sandbox, "unavailable_reason", lambda: None)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({
        "name": "r2", "cutoff": "2026-07-01", "commits_per_repo": 8, "repeats": 3, "baseline": "plain",
        "challengers": ["haiku", "opus", "planner:opus+haiku"], "repos": [{"path": str(tmp_path / "repo")}],
        "comparisons": [{"challenger": "planner:opus+haiku", "baseline": "plain", "primary": "per_solved"},
                        {"challenger": "planner:opus+haiku", "baseline": "haiku", "primary": "solved"}],
        "also": [{"challenger": "haiku", "baseline": "plain"}]}))
    runs = tmp_path / "runs"
    runs.mkdir()
    records = grid({"claude-code": 0.5, "claude-code@haiku": 0.25, "claude-code@opus": 0.75, "planner@opus+haiku": 0.75},
                   cost={"claude-code": 0.1, "claude-code@haiku": 0.02, "claude-code@opus": 0.3, "planner@opus+haiku": 0.06})
    (runs / "all.jsonl").write_text("".join(json.dumps(r.__dict__) + "\n" for r in records))
    assert main(["study", "report", str(plan), "--runs", str(runs)]) == 0
    out = capsys.readouterr().out
    head, _, rest = out.partition("Also reported (not primary comparisons; never a headline):")
    assert "| planner@opus+haiku vs claude-code | per solved |" in head and "| planner@opus+haiku vs claude-code@haiku | solve rate |" in head
    assert "claude-code@opus vs" not in head  # not asked for, so not compared
    assert head.count("| yes |") == 2
    assert "| claude-code@haiku vs claude-code | per solved |" in rest
    assert "Within tasks with under 15 words (24 tasks; never a headline):" in rest
    assert "Within tasks with 30 or fewer changed lines of code (48 tasks; never a headline):" in rest
    assert "| yes |" not in rest  # nothing outside the primary comparisons is ever a headline
    bad = json.loads(plan.read_text())
    bad["comparisons"][0]["primary"] = "vibes"
    plan.write_text(json.dumps(bad))
    with pytest.raises(SystemExit, match="primary must be one of"):
        main(["study", "report", str(plan), "--runs", str(runs)])


# ------------------------------------------------------------------ a study's own tasks

def test_description_length_and_changed_lines_come_from_the_commit_alone(tmp_path):
    from gitrepo import Repo, days_ago

    from aimpg.replay import workspace

    r = Repo(tmp_path / "repo")
    r.commit({"calc.py": "x = 1\ny = 1\n", "tests/test_calc.py": "def test(): ...\n", "logo.bin": "a"}, "base", when=days_ago(9))
    (r.path / "logo.bin").write_bytes(b"\x00\x01\x02")
    r.git("add", "logo.bin")
    sha = r.commit({"calc.py": "x = 2\ny = 1\nz = 3\n", "util.py": "a = 1\n", "tests/test_calc.py": "def test(): 1\ndef test2(): 2\n"},
                   "change x to two and add z and a helper", when=days_ago(5))
    (c,), _ = select.candidates(str(r.path), days_ago(30), days_ago(20), any_author=True)
    assert c.sha == sha and c.code_lines == 4  # calc.py: 1 removed + 2 added; util.py: 1 added; tests and the binary file don't count
    assert c.words == 10 and select.commit_at(str(r.path), sha).code_lines == 4
    assert workspace.code_lines(str(r.path), c.parent, c.sha) == 4
    select.save([c], tmp_path / "selected.jsonl")
    assert select.load(tmp_path / "selected.jsonl")[0].code_lines == 4
    none, skipped = select.candidates(str(r.path), days_ago(30), days_ago(20), any_author=True, exclude={sha})
    assert none == [] and skipped["used in an earlier study"] == 1


def test_a_selection_saved_before_code_lines_still_loads(tmp_path):
    old = {"repo": "/r", "sha": "a" * 40, "parent": "p", "subject": "fix the thing", "body": "", "test_files": ["t.py"], "code_files": ["a.py"]}
    (tmp_path / "selected.jsonl").write_text(json.dumps(old) + "\n")
    (c,) = select.load(tmp_path / "selected.jsonl")
    assert c.code_lines == -1 and c.words == 3


def test_a_study_that_excludes_commits_keeps_its_own_selection(tmp_path, monkeypatch, capsys):
    from aimpg.replay import study_cli

    monkeypatch.setattr(study_cli, "STATE", tmp_path / "study")
    monkeypatch.setattr(replay_cli, "STATE", tmp_path / "replay")
    repo = tmp_path / "repos" / "calc"
    repo.mkdir(parents=True)
    used = [commit(i, repo=str(repo)) for i in (1, 2)]
    fresh = [commit(i, repo=str(repo)) for i in (3, 4, 5)]
    select.save(used + fresh[:1], replay_cli._state(repo) / "selected.jsonl")  # an earlier study's selection of this repo
    plan = {"name": "r2", "repos": [{"path": str(repo)}], "exclude_commits": [c.sha for c in used]}
    assert study_cli._sel_dir(plan, str(repo)) == tmp_path / "study" / "r2" / "selection" / "calc"
    assert study_cli._sel_dir({"name": "r1", "repos": []}, str(repo)) == replay_cli._state(repo)  # without it: as before
    with pytest.raises(SystemExit, match="run `aimpg study select` first"):
        study_cli._selected(plan, str(repo), 2)  # the earlier study's selection is not picked up
    select.save(used[:1] + fresh, study_cli._sel_dir(plan, str(repo)) / "selected.jsonl")
    assert [int(c.sha) for c in study_cli._selected(plan, str(repo), 2)] == [3, 4]  # an excluded commit never runs
    assert study_cli._qualified(plan, str(repo)) == 4
    with pytest.raises(SystemExit, match="full commit ids"):
        study_cli._excluded({"exclude_commits": ["abc123"]})
    twin = {"name": "r3", "exclude_commits": [], "repos": [{"path": str(repo)}, {"path": str(tmp_path / "other" / "calc")}]}
    with pytest.raises(SystemExit, match="different names"):
        study_cli._sel_dir(twin, str(repo))


def test_study_select_passes_the_exclusions_and_its_own_folder_on(tmp_path, monkeypatch, capsys):
    from aimpg.replay import study_cli

    monkeypatch.setattr(study_cli, "STATE", tmp_path / "study")
    repo = tmp_path / "calc"
    repo.mkdir()
    seen = {}

    def fake_select(path, args):
        seen.update(exclude=set(args.exclude), state_dir=args.state_dir, max=args.max)
        select.save([commit(i, repo=str(repo)) for i in range(3, 9)], args.state_dir / "selected.jsonl")
        (args.state_dir / "selected.manifest.json").write_text("{}")
        return 0

    monkeypatch.setattr(study_cli, "_select", fake_select)
    plan = {"name": "r2", "cutoff": "2026-07-01", "commits_per_repo": 3, "repos": [{"path": str(repo), "url": "u"}],
            "exclude_commits": ["a" * 40]}
    assert study_cli._select_all(plan) == 0
    assert seen == {"exclude": {"a" * 40}, "state_dir": tmp_path / "study" / "r2" / "selection" / "calc", "max": 6}
    summary = json.loads((tmp_path / "study" / "r2" / "selection.json").read_text())
    assert summary["repos"] == [{"path": str(repo), "url": "u", "qualifying": 6}]
    assert study_cli._select_all(plan) == 0 and "already selected: 6 qualifying" in capsys.readouterr().out

