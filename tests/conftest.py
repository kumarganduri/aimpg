"""Builders for fixture log rows that mirror the real Claude Code JSONL shape."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def assistant(
    request_id: str | None,
    ts: str,
    *,
    session: str = "s1",
    cwd: str = "/repo",
    model: str = "claude-sonnet-5",
    fresh: int = 10,
    write: int = 100,
    read: int = 1000,
    out: int = 50,
    content: list | None = None,
    sidechain: bool = False,
    message_id: str = "msg_1",
    version: str = "2.1.280",
) -> dict:
    row = {
        "type": "assistant",
        "sessionId": session,
        "cwd": cwd,
        "timestamp": ts,
        "isSidechain": sidechain,
        "version": version,
        "message": {
            "id": message_id,
            "model": model,
            "content": content or [{"type": "text", "text": "ok"}],
            "usage": {
                "input_tokens": fresh,
                "cache_creation_input_tokens": write,
                "cache_read_input_tokens": read,
                "output_tokens": out,
            },
        },
    }
    if request_id is not None:
        row["requestId"] = request_id
    return row


def bash_call(tool_id: str, command: str, ts: str, *, session: str = "s1", cwd: str = "/repo") -> dict:
    return assistant(
        f"req_{tool_id}",
        ts,
        session=session,
        cwd=cwd,
        content=[{"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": command}}],
    )


def tool_result(tool_id: str, ts: str, *, session: str = "s1", cwd: str = "/repo") -> dict:
    return {
        "type": "user",
        "sessionId": session,
        "cwd": cwd,
        "timestamp": ts,
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": "done"}],
        },
    }


@pytest.fixture
def write_log(tmp_path: Path):
    def _write(rows: list, name: str = "session.jsonl", raw_lines: list[str] | None = None) -> Path:
        path = tmp_path / "projects" / "-folder" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(r) for r in rows] + list(raw_lines or [])
        path.write_text("\n".join(lines) + "\n")
        return path

    return _write


@pytest.fixture(autouse=True)
def _never_read_the_real_agent_logs(tmp_path_factory, monkeypatch):
    """Tests must not depend on (or read) the developer's real ~/.codex or ~/.aimpg."""
    from aimpg import codex_logs, ledger
    from aimpg.replay import cli as replay_cli
    from aimpg.replay import verify

    monkeypatch.setattr(codex_logs, "DEFAULT_ROOT", tmp_path_factory.mktemp("no-codex") / "sessions")
    monkeypatch.setattr(ledger, "LEDGER", tmp_path_factory.mktemp("ledger") / "ledger.json")
    monkeypatch.setattr(replay_cli, "STATE", tmp_path_factory.mktemp("replay-state"))
    monkeypatch.setattr(verify, "STATE", tmp_path_factory.mktemp("verify-state"))
    from aimpg import scoreboard

    monkeypatch.setattr(scoreboard, "SECRET", tmp_path_factory.mktemp("aimpg-id") / "id")
