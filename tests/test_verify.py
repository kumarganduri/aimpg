import json

import pytest

from aimpg.cli import main
from aimpg.replay import setups as S
from aimpg.replay.run import Record
from aimpg.replay.verify import VerifyError, build_record, check, setup_from, setup_from_spec, verdict

SONNET = "claude-sonnet-5-5"
BIG = [[5, 3000, 9000, 400]] * 3
SMALL = [[5, 1500, 4000, 200]] * 3


def rec(commit, setup, *, passed=True, usages=BIG, cost=0.10, model=SONNET, repeat=0, agent="claude", cost_known=True):
    return Record(f"{commit}-{setup}-{repeat}", "/private/repo/path", commit, setup, repeat, "passed" if passed else "tests_failed",
                  passed, model, cost, 30.0, usages, "ok", "secret test output", cost_known, agent)


def same_model(chal_usages, chal_pass=True, n=8):
    return [rec(f"c{i}", "claude-code") for i in range(n)] + [
        rec(f"c{i}", "claude-code+rtk", usages=chal_usages, passed=chal_pass or i % 2 == 0) for i in range(n)]


def test_token_saver_that_really_saves_is_supported():
    v = verdict(same_model(SMALL), "claude-code", "claude-code+rtk")
    assert v["answer"] == "SUPPORTED" and v["energy"]["verdict"] == "uses less"


def test_token_saver_that_uses_more_is_not_supported():
    v = verdict(same_model([[5, 6000, 20000, 800]] * 3), "claude-code", "claude-code+rtk")
    assert v["answer"] == "NOT SUPPORTED" and "uses more energy" in " ".join(v["reasons"])


def test_cheaper_but_solving_fewer_is_never_supported():
    v = verdict(same_model(SMALL, chal_pass=False), "claude-code", "claude-code+rtk")
    assert v["answer"] == "NOT SUPPORTED" and "solves fewer" in v["reasons"][0]


def test_too_few_shared_passes_is_not_proven():
    v = verdict(same_model(SMALL, n=3), "claude-code", "claude-code+rtk")
    assert v["answer"] == "NOT PROVEN" and v.get("tentative")


def test_other_model_is_judged_on_dollars_per_solved_not_energy():
    records = [rec(f"c{i}", "claude-code") for i in range(5)] + [
        rec(f"c{i}", "claude-code@haiku", model="claude-haiku-4-5", cost=0.05) for i in range(5)]
    v = verdict(records, "claude-code", "claude-code@haiku")
    assert v["energy"] is None and v["cost_ratio"] == 0.5
    assert v["answer"] == "SUPPORTED" and any("energy not ranked" in r for r in v["reasons"])


def test_unpriced_agent_cost_is_not_measurable():
    records = [rec(f"c{i}", "claude-code") for i in range(5)] + [
        rec(f"c{i}", "codex", model="gpt-6-luna", cost=0, cost_known=False, agent="codex") for i in range(5)]
    v = verdict(records, "claude-code", "codex")
    assert v["answer"] == "NOT PROVEN" and v["challenger"]["usd_per_solved"] is None
    assert v["challenger"]["wh_per_solved"] is not None  # tokens are known, so energy is shown (as a range)


def test_private_record_hides_repo_commits_and_output():
    records = same_model(SMALL)
    record = build_record(records, claim="rtk saves", baseline=S.BASELINE, challenger=S.RTK, repo="/private/repo/path",
                          task_mode="tests", public=False)
    text = json.dumps(record)
    assert "secret test output" not in text and "/private/repo/path" not in text and '"c1"' not in text
    assert len({r["commit"] for r in record["runs"]}) == 8  # still consistent within the record
    ok, _ = check(record)
    assert ok


def test_check_catches_a_tampered_record():
    record = build_record(same_model(SMALL), claim="", baseline=S.BASELINE, challenger=S.RTK, repo=".", task_mode="tests", public=False)
    for r in record["runs"]:
        if r["setup"] == "claude-code+rtk":
            r["usages"] = [[5, 6000, 20000, 800]] * 3
    ok, again = check(record)
    assert not ok and again["answer"] == "NOT SUPPORTED"


def test_cli_check_command(tmp_path, capsys):
    record = build_record(same_model(SMALL), claim="rtk saves tokens", baseline=S.BASELINE, challenger=S.RTK, repo=".", task_mode="tests", public=False)
    path = tmp_path / "r.record.json"
    path.write_text(json.dumps(record))
    assert main(["verify", "--check", str(path)]) == 0
    out = capsys.readouterr().out
    assert "CLAIM: rtk saves tokens" in out and "VERDICT: SUPPORTED" in out and "MATCHES" in out


@pytest.mark.parametrize("name, agent, setup_name", [
    ("rtk", "claude", "claude-code+rtk"), ("terse", "claude", "claude-code+terse"), ("haiku", "claude", "claude-code@haiku"),
    ("codex", "codex", "codex"), ("codex:gpt-6-luna", "codex", "codex@gpt-6-luna"),
])
def test_challenger_names(name, agent, setup_name):
    s = setup_from(name)
    assert s.agent == agent and s.name == setup_name


def test_command_challenger_needs_hosts_and_a_task_slot():
    with pytest.raises(VerifyError):
        setup_from("cmd:aider --message {task}")
    with pytest.raises(S.SetupUnavailable):
        setup_from("cmd:aider --yes", hosts=["api.openai.com"])
    s = setup_from("cmd:aider --message {task}", hosts=["api.openai.com"], key_env="OPENAI_API_KEY")
    assert s.argv("do it", "m", 1, tmp := __import__("pathlib").Path("/x"))[-1] == "do it" and s.hosts == {"api.openai.com"}


def test_specs_round_trip_for_reruns():
    custom = S.custom(model="haiku", append_prompt="be brief", claude_md="# rules", settings='{"hooks": {}}')
    for setup in (S.BASELINE, S.RTK, S.with_model("opus"), S.codex("gpt-6-luna"), custom,
                  S.command("tool {task}", ["api.example.com"], "TOOL_KEY")):
        again = setup_from_spec({"name": setup.name, **setup.spec})
        assert (again.name, again.agent, again.model, again.extra_args) == (setup.name, setup.agent, setup.model, setup.extra_args)


def test_private_specs_cannot_be_rerun():
    custom = S.custom(settings='{"hooks": {}}')
    spec = {"name": custom.name, **custom.spec}
    spec.pop("settings")
    with pytest.raises(VerifyError):
        setup_from_spec(spec)


def test_custom_settings_must_be_json():
    with pytest.raises(ValueError):
        S.custom(settings="not json")


def test_missing_key_stops_before_spending(tmp_path, monkeypatch, capsys):
    from aimpg.replay import cli as replay_cli

    monkeypatch.setattr(replay_cli, "STATE", tmp_path / "state")
    (replay_cli._state(tmp_path)).joinpath("selected.jsonl").write_text(
        "\n".join(json.dumps({"repo": str(tmp_path), "sha": f"{i:040x}", "parent": "p", "subject": "s", "body": "",
                              "test_files": [], "code_files": []}) for i in range(10)) + "\n")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert main(["verify", "--repo", str(tmp_path), "--challenger", "rtk"]) == 1
    assert "set ANTHROPIC_API_KEY" in capsys.readouterr().out


def test_verdict_does_not_depend_on_commit_ids():
    varied = [[[5, 3000, 9000, 400 + 50 * i]] * 3 for i in range(8)]
    records = [rec(f"c{i}", "claude-code") for i in range(8)] + [
        rec(f"c{i}", "claude-code+rtk", usages=varied[i]) for i in range(8)]
    renamed = [Record(**{**r.__dict__, "commit": "z" + str(7 - int(r.commit[1:]))}) for r in records]
    assert verdict(records, "claude-code", "claude-code+rtk") == verdict(renamed, "claude-code", "claude-code+rtk")


def test_missing_repo_is_a_plain_message(tmp_path, capsys):
    assert main(["verify", "--repo", str(tmp_path / "nope"), "--challenger", "terse"]) == 1
    assert "not a git repo" in capsys.readouterr().out
