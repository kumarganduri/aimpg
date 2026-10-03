import json

from aimpg.codex_logs import merge, parse_codex
from aimpg.logs import parse_logs

from conftest import assistant


def write(tmp_path, rows):
    p = tmp_path / "rollout-x.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return p


def token_count(ts, inp, cached, out, reasoning=0, written=0):
    usage = {"input_tokens": inp, "cached_input_tokens": cached, "cache_write_input_tokens": written,
             "output_tokens": out, "reasoning_output_tokens": reasoning, "total_tokens": inp + out}
    return {"timestamp": ts, "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": usage, "last_token_usage": usage}}}


def test_token_counts_map_to_usage_buckets(tmp_path):
    rows = [
        {"timestamp": "2026-10-03T09:00:00.000Z", "type": "session_meta", "payload": {"session_id": "S1", "cwd": "/repo", "cli_version": "0.159.2"}},
        {"timestamp": "2026-10-03T09:00:01.000Z", "type": "turn_context", "payload": {"model": "gpt-6-luna", "cwd": "/repo"}},
        token_count("2026-10-03T09:00:05.000Z", 12860, 11008, 5, reasoning=100, written=500),
        token_count("2026-10-03T09:00:09.000Z", 13000, 12000, 40),
    ]
    p = parse_codex([write(tmp_path, rows)])
    assert len(p.requests) == 2
    r = p.requests[0]
    assert r.session_id == "codex:S1" and r.model == "gpt-6-luna" and r.cwd == "/repo"
    assert (r.usage.fresh_in, r.usage.cache_write, r.usage.cache_read, r.usage.output) == (1352, 500, 11008, 105)
    assert p.versions == {"codex 0.159.2": 1}


def test_git_commit_tool_calls_become_commit_calls(tmp_path):
    rows = [
        {"timestamp": "2026-10-03T09:00:00.000Z", "type": "session_meta", "payload": {"session_id": "S1", "cwd": "/repo"}},
        {"timestamp": "2026-10-03T09:00:10.000Z", "type": "response_item", "payload": {"type": "function_call", "name": "shell", "call_id": "c1", "arguments": json.dumps({"command": ["bash", "-lc", "git add -A && git commit -m feat"]})}},
        {"timestamp": "2026-10-03T09:00:12.000Z", "type": "response_item", "payload": {"type": "function_call_output", "call_id": "c1", "output": "ok"}},
        {"timestamp": "2026-10-03T09:00:20.000Z", "type": "response_item", "payload": {"type": "function_call", "name": "shell", "call_id": "c2", "arguments": json.dumps({"command": ["ls"]})}},
    ]
    p = parse_codex([write(tmp_path, rows)])
    (call,) = p.commit_calls
    assert call.session_id == "codex:S1" and call.cwd == "/repo" and call.end - call.start == 2.0


def test_merge_combines_agents(tmp_path, write_log):
    claude = parse_logs([write_log([assistant("r1", "2026-10-03T08:00:00.000Z")])])
    codex = parse_codex([write(tmp_path, [token_count("2026-10-03T09:00:05.000Z", 100, 0, 5)])])
    both = merge(claude, codex)
    assert [r.id.split(":")[0] for r in both.requests] == ["r1", "codex"]
    assert both.stats["unique_requests"] == 2 and both.stats["files"] == 2


def test_corrupt_rows_are_counted(tmp_path):
    p = tmp_path / "rollout-y.jsonl"
    p.write_text('{"type": "event_msg", "payl\n')
    assert parse_codex([p]).stats["corrupt_rows"] == 1
