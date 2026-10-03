import io
import json
import time

import pytest

from aimpg import live
from aimpg.live import SessionState, commit_note, status_line, update

from conftest import assistant, bash_call, tool_result


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "LIVE_DIR", tmp_path / "live")
    monkeypatch.setattr(live, "PREVIOUS", tmp_path / "live" / "previous_statusline.json")
    monkeypatch.setattr(live, "_refresh_in_background", lambda: None)


def iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))


def append(path, rows):
    with open(path, "a") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def test_reads_only_new_lines_and_dedupes_requests(tmp_path):
    t = tmp_path / "s.jsonl"
    now = time.time() - 600
    append(t, [assistant("r1", iso(now), fresh=2, write=10_000, read=0, out=100), assistant("r1", iso(now), fresh=2, write=10_000, read=0, out=100)])
    s = update(SessionState(), t)
    assert len(s.task) == 1 and s.base_ctx == 10_002
    first_offset = s.offset
    append(t, [assistant("r2", iso(now + 10), fresh=2, write=500, read=10_000, out=200)])
    s = update(s, t)
    assert len(s.task) == 2 and s.offset > first_offset and s.last_ctx == 10_502


def test_partial_last_line_waits(tmp_path):
    t = tmp_path / "s.jsonl"
    t.write_text(json.dumps(assistant("r1", iso(time.time()))) + "\n" + '{"type": "assistant", "mess')
    s = update(SessionState(), t)
    assert len(s.task) == 1 and s.offset == len(t.read_text().split("\n")[0]) + 1


def test_commit_in_transcript_closes_the_task_and_tracks_carried_context(tmp_path):
    t = tmp_path / "s.jsonl"
    now = time.time() - 600
    append(t, [
        assistant("r1", iso(now), fresh=2, write=10_000, read=0, out=100),
        assistant("r2", iso(now + 5), fresh=2, write=1_000, read=60_000, out=100),
        bash_call("tu1", "git commit -m feat", iso(now + 10)),
        tool_result("tu1", iso(now + 12)),
        assistant("r3", iso(now + 20), fresh=2, write=500, read=72_000, out=100),
    ])
    s = update(SessionState(), t)
    assert len(s.commits) == 1 and s.commits[0]["requests"] == 3  # r1, r2 and the commit call itself
    assert list(s.task) == ["r3"]
    assert 0.8 < s.carried_share() < 0.9  # most of r3's context predates the commit


def test_status_line_colors_and_hints():
    s = SessionState(base_ctx=10_000, last_ctx=100_000, first_ctx_after_commit=90_000, commits=[{"ts": 1, "cost": 1, "requests": 3}])
    s.task = {"r": [0, "claude-sonnet-5-5", 2, 1000, 90_000, 1_000_000, 0, False]}  # ~$10 task
    line = status_line({"rate_limits": {"five_hour": {"used_percentage": 63}}}, s, usual=1.0)
    assert line.startswith("⚡ ") and "\033[31m" in line  # > 3x usual: red
    assert "usual $1.00" in line and "80% from before your last commit" in line and "5h 63%" in line
    assert "/clear ≈ -80% per request" in line
    assert "\033[" not in status_line({}, SessionState(), usual=None)


def test_hook_ignores_other_commands(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})))
    assert live.run_post_commit_hook() == 0
    assert capsys.readouterr().out == ""


def test_hook_announces_a_commit_once(tmp_path, monkeypatch, capsys):
    t = tmp_path / "s.jsonl"
    now = time.time() - 30
    append(t, [assistant("r1", iso(now), fresh=2, write=10_000, read=0, out=100), assistant("r2", iso(now + 5), fresh=2, write=500, read=50_000, out=300)])
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}, "session_id": "S", "transcript_path": str(t)})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    live.run_post_commit_hook()
    msg = json.loads(capsys.readouterr().out)["systemMessage"]
    assert msg.startswith("aimpg: that commit cost $") and "over 2 AI requests" in msg and "/clear" in msg
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    live.run_post_commit_hook()
    assert capsys.readouterr().out == ""  # nothing new: no second note


def test_hook_still_announces_when_the_status_line_saw_the_commit_first(tmp_path, monkeypatch, capsys):
    t = tmp_path / "s.jsonl"
    now = time.time() - 30
    append(t, [assistant("r1", iso(now), out=100), bash_call("tu1", "git commit -m x", iso(now + 5)), tool_result("tu1", iso(now + 6))])
    update(SessionState(), t).save("S")  # status line ran first and closed the task
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}, "session_id": "S", "transcript_path": str(t)})))
    live.run_post_commit_hook()
    assert "that commit cost" in json.loads(capsys.readouterr().out)["systemMessage"]


def test_commit_note_without_old_context_has_no_clear_hint():
    assert "/clear" not in commit_note({"cost": 0.5, "requests": 4, "stale_share": 0.1})


def test_install_keeps_existing_statusline_and_hooks_and_uninstall_restores(tmp_path):
    settings = tmp_path / "settings.json"
    original = {"statusLine": {"type": "command", "command": "~/my-line.sh"}, "theme": "dark",
                "hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": [{"type": "command", "command": "fmt.sh"}]}]}}
    settings.write_text(json.dumps(original))
    assert live.install(settings, "/bin/aimpg", confirm=lambda _: "n") == "Nothing changed."
    assert json.loads(settings.read_text()) == original
    live.install(settings, "/bin/aimpg", confirm=lambda _: "y")
    new = json.loads(settings.read_text())
    assert new["statusLine"]["command"] == "/bin/aimpg statusline" and new["theme"] == "dark"
    assert len(new["hooks"]["PostToolUse"]) == 2
    assert live.previous_statusline() == "~/my-line.sh"
    assert list(tmp_path.glob("settings.json.aimpg-backup-*"))
    live.uninstall(settings)
    assert json.loads(settings.read_text()) == original


def test_status_line_wraps_the_previous_one(tmp_path, monkeypatch, capsys):
    t = tmp_path / "s.jsonl"
    append(t, [assistant("r1", iso(time.time()), out=100)])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "S", "transcript_path": str(t)})))
    live.run_statusline("echo mine")
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "mine" and out[1].startswith("⚡ task $")
