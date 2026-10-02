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
