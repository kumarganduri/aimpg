import json

from aimpg.cli import main
from aimpg.replay import cli as replay_cli


def record(commit, setup, repeat, passed, scale, cost=0.3):
    u = [int(2000 * scale), int(20000 * scale), int(60000 * scale), int(800 * scale)]
    return {"run_id": f"{commit}-{setup}-{repeat}", "repo": "/r", "commit": commit, "setup": setup, "repeat": repeat,
            "outcome": "passed" if passed else "tests_failed", "passed": passed, "model": "claude-sonnet-5-5",
            "cost_usd": cost, "wall_s": 60, "usages": [u], "tokens_check": "ok", "note": ""}


def test_stats_prints_a_verdict_per_challenger(tmp_path, capsys):
    rows = []
    for c in range(8):
        for r in range(2):
            rows.append(record(f"c{c}", "claude-code", r, True, 1 + c * 0.5))
            rows.append(record(f"c{c}", "claude-code+terse", r, True, (1 + c * 0.5) * 0.6))
            rows.append(record(f"c{c}", "claude-code+rtk", r, c < 3, (1 + c * 0.5) * 0.5))
    rows.append({"excluded_commit": "c9", "reason": "harness_error twice"})
    path = tmp_path / "results.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    assert main(["replay", "stats", str(path)]) == 0
    out = capsys.readouterr().out
    assert "claude-code+terse vs claude-code: uses less" in out
    assert "claude-code+rtk vs claude-code: not enough passing commits" in out
    assert "Excluded commits (harness errors twice): 1" in out


def test_paid_commands_refuse_without_api_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(replay_cli, "STATE", tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / "selected.jsonl").write_text("")
    assert main(["replay", "calibrate", str(tmp_path / "repo")]) == 1
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().out


def test_paid_commands_need_a_selection_first(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(replay_cli, "STATE", tmp_path)
    assert main(["replay", "run", str(tmp_path / "nothing"), "--cap", "5"]) == 1
    assert "aimpg replay select" in capsys.readouterr().out


def test_declining_the_cost_prompt_spends_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(replay_cli, "STATE", tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-used")
    from aimpg.replay.workspace import Commit
    from aimpg.replay import select

    (tmp_path / "repo").mkdir()
    select.save([Commit("/r", f"{i:040d}", "p", "s", "", ["t.py"], ["a.py"]) for i in range(3)], tmp_path / "repo" / "selected.jsonl")
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert main(["replay", "calibrate", str(tmp_path / "repo"), "--setups", "claude-code,claude-code+terse"]) == 1
    out = capsys.readouterr().out
    assert "2 commits × 2 setups × 2 repeats = 8 agent runs" in out
    assert "Nothing was spent" in out


def test_rtk_hook_goes_only_into_the_throwaway_config(tmp_path):
    import shutil

    import pytest

    from aimpg.replay.setups import RTK

    if shutil.which("rtk") is None:
        pytest.skip("rtk not installed")
    RTK.configure(tmp_path)
    settings = json.loads((tmp_path / "settings.json").read_text())
    hooks = settings["hooks"]["PreToolUse"]
    assert any(h["matcher"] == "Bash" and "rtk" in h["hooks"][0]["command"] for h in hooks)


def _req(i, cw, cr, out, sidechain=False, ts=None):
    from aimpg.model import Request, Usage

    return Request(f"r{i}", "s", "claude-sonnet-5-5", ts if ts is not None else float(i), Usage(2, cw, cr, out), "/w", sidechain)


def test_token_check_uses_model_usage_totals_like_real_claude_code():
    # From the paid calibration runs: `usage` was the last request in one run and
    # the total in another, so only modelUsage (running totals) is trusted.
    from aimpg.replay.run import _check_tokens

    reqs = [_req(0, 5766, 10325, 209), _req(1, 295, 16091, 88), _req(2, 220, 16386, 161), _req(3, 282, 16606, 568)]
    mu = {"claude-sonnet-5-5": {"inputTokens": 8, "outputTokens": 1026, "cacheReadInputTokens": 59408, "cacheCreationInputTokens": 6563, "maxOutputTokens": 128000}}
    assert _check_tokens(reqs, {"modelUsage": mu}) == ("ok", False)
    last_only = {"input_tokens": 2, "cache_creation_input_tokens": 282, "cache_read_input_tokens": 16606, "output_tokens": 568}
    assert _check_tokens(reqs, {"usage": last_only, "modelUsage": mu}) == ("ok", False)  # usage ignored

    assert _check_tokens(reqs[:-1], {"modelUsage": mu})[1] is True  # transcript missing a request
    assert _check_tokens([], {"modelUsage": mu}) == ("empty transcript", True)
    assert _check_tokens(reqs, {}) == ("unverified (no modelUsage in result)", False)


def test_paid_run_combines_several_repos(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(replay_cli, "STATE", tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-used")
    from aimpg.replay import select
    from aimpg.replay.workspace import Commit

    for name, n in (("one", 2), ("two", 3)):
        (tmp_path / name).mkdir()
        select.save([Commit(f"/{name}", f"{name}{i:037d}", "p", "s", "", ["t.py"], ["a.py"]) for i in range(n)], tmp_path / name / "selected.jsonl")
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert main(["replay", "run", str(tmp_path / "one"), str(tmp_path / "two"), "--commits", "5", "--cap", "9", "--setups", "claude-code,claude-code+rtk"]) == 1
    out = capsys.readouterr().out
    assert "5 commits × 2 setups × 2 repeats = 20 agent runs" in out
    assert "total cap $9.00" in out


def test_resume_keeps_real_runs_and_redoes_dead_ones(tmp_path):
    from aimpg.replay.run import completed, load_results

    rows = [
        record("c1", "claude-code", 0, True, 1.0),
        record("c1", "claude-code+rtk", 0, True, 0.9),
        {**record("c2", "claude-code", 0, False, 1.0), "outcome": "agent_error", "usages": [], "cost_usd": 0.0},  # never reached the model
        {**record("c2", "claude-code+rtk", 0, False, 1.0), "outcome": "account_error", "usages": [], "cost_usd": 0.0},
        {**record("c3", "claude-code", 0, False, 1.0), "outcome": "agent_error"},  # a real agent failure: kept
    ]
    path = tmp_path / "r.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    assert completed(path) == {("c1", "claude-code", 0), ("c1", "claude-code+rtk", 0), ("c3", "claude-code", 0)}
    loaded, _ = load_results(path)
    assert sorted((r.commit, r.setup) for r in loaded) == [("c1", "claude-code"), ("c1", "claude-code+rtk"), ("c3", "claude-code")]

    # a resumed run appends a fresh record for c2; the latest record wins
    with open(path, "a") as fh:
        fh.write(json.dumps(record("c2", "claude-code", 0, True, 1.1)) + "\n")
    loaded, _ = load_results(path)
    assert ("c2", "claude-code") in {(r.commit, r.setup) for r in loaded}


def test_resume_plan_counts_only_missing_runs(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(replay_cli, "STATE", tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-used")
    import random

    from aimpg.replay import select
    from aimpg.replay.workspace import Commit

    (tmp_path / "repo").mkdir()
    commits = [Commit("/r", f"{i:040d}", "p", "s", "", ["t.py"], ["a.py"]) for i in range(3)]
    select.save(commits, tmp_path / "repo" / "selected.jsonl")
    chosen = random.Random(7).sample(commits, 2)
    prior = tmp_path / "prior.jsonl"
    prior.write_text(json.dumps(record(chosen[0].sha, "claude-code", 0, True, 1.0)) + "\n")
    monkeypatch.setattr("builtins.input", lambda _: "n")
    main(["replay", "run", str(tmp_path / "repo"), "--commits", "2", "--cap", "9", "--setups", "claude-code,claude-code+rtk", "--resume", str(prior)])
    out = capsys.readouterr().out
    assert "1 runs already done, 7 to go" in out


def test_models_command_plans_one_setup_per_model(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(replay_cli, "STATE", tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-used")
    from aimpg.replay import select
    from aimpg.replay.workspace import Commit

    (tmp_path / "repo").mkdir()
    select.save([Commit("/r", f"{i:040d}", "p", "s", "", ["t.py"], ["a.py"]) for i in range(12)], tmp_path / "repo" / "selected.jsonl")
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert main(["replay", "models", str(tmp_path / "repo"), "--commits", "10", "--cap", "15", "--task-mode", "tests"]) == 1
    assert "10 commits × 3 setups × 1 repeats = 30 agent runs" in capsys.readouterr().out


def test_stats_on_a_model_picker_file_prints_the_picker(tmp_path, capsys):
    rows = [record(f"c{c}", f"claude-code@{m}", 0, True, 1.0, cost=cost) | {"model": mid}
            for c in range(3) for m, mid, cost in (("haiku", "claude-haiku-4-5", 0.03), ("sonnet", "claude-sonnet-5-5", 0.14))]
    path = tmp_path / "models.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    assert main(["replay", "stats", str(path)]) == 0
    out = capsys.readouterr().out
    assert "Use haiku" in out and "$ / solved task" in out
