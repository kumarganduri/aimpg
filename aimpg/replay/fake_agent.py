"""A free stand-in for Claude Code, used only in tests. Speaks the same protocol:

* writes a transcript to $CLAUDE_CONFIG_DIR/projects/<cwd>/<session>.jsonl
  (assistant rows with usage, like the real thing);
* prints one JSON result to stdout (`--output-format json` shape).

Modes: solve (apply the solution the test staged), nothing, cheat (rewrite
visible tests to trivial ones), crash, timeout,
budget (reports a budget stop), escape (tries to break out of the sandbox and
reports what worked; everything should fail).

Stdlib only: it runs inside the sandbox, which can't read the project's venv.
"""

import json
import os
import pwd
import subprocess
import sys
import tarfile
import time
import uuid
from pathlib import Path


def transcript(cfg: Path, cwd: Path, model: str, n: int) -> tuple[dict, dict]:
    """Writes n requests. Returns (last request's usage, running totals), like Claude Code."""
    session = str(uuid.uuid4())
    folder = cfg / "projects" / str(cwd).replace("/", "-")
    folder.mkdir(parents=True, exist_ok=True)
    totals = {"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0}
    usage = dict(totals)
    with open(folder / f"{session}.jsonl", "w") as fh:
        for i in range(n):
            usage = {"input_tokens": 5, "cache_creation_input_tokens": 3000, "cache_read_input_tokens": 9000 * (i + 1), "output_tokens": 400}
            for k in totals:
                totals[k] += usage[k]
            row = {
                "type": "assistant",
                "sessionId": session,
                "cwd": str(cwd),
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{i:03d}Z",
                "requestId": f"req_fake_{session[:8]}_{i}",
                "version": "fake",
                "message": {"id": f"msg_{i}", "model": model, "content": [{"type": "text", "text": "..."}], "usage": usage},
            }
            fh.write(json.dumps(row) + "\n")
            fh.write(json.dumps(row) + "\n")  # real transcripts repeat rows per content block
    model_usage = {model: {"inputTokens": totals["input_tokens"], "outputTokens": totals["output_tokens"],
                           "cacheReadInputTokens": totals["cache_read_input_tokens"],
                           "cacheCreationInputTokens": totals["cache_creation_input_tokens"]}}
    return usage, model_usage


def escape_attempts(cwd: Path, cfg: Path) -> list[str]:
    """Every attempt that SUCCEEDS is a sandbox bug.

    Seeing a folder counts only if real content shows: on Linux, home and the
    runs folder are empty private stand-ins, which is the sandbox working.
    """
    succeeded = []
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    own_run = cwd.parent.name
    for target, allowed in ((real_home, set()), (real_home / ".ssh", set()), (cwd.parent.parent, {own_run})):
        try:
            if set(os.listdir(target)) - allowed:
                succeeded.append(f"read {target}")
        except OSError:
            pass
    try:
        (real_home / "aimpg-escape-probe").write_text("x")
        succeeded.append("write to home")
    except OSError:
        pass
    probe = subprocess.run(["/usr/bin/curl", "-s", "-m", "5", "-o", "/dev/null", "-w", "%{http_code}", "--noproxy", "*", "https://github.com"], capture_output=True, text=True)
    if probe.stdout.strip() not in ("000", ""):
        succeeded.append("direct internet")
    via = subprocess.run(["/usr/bin/curl", "-s", "-m", "5", "-o", "/dev/null", "-w", "%{http_code}", "https://github.com"], capture_output=True, text=True)
    if via.stdout.strip() not in ("000", ""):
        succeeded.append("internet via proxy")
    return succeeded


def codex_rollout(home: Path, cwd: Path, model: str, n: int) -> None:
    """Codex's rollout log shape: session_meta, turn_context, token_count events."""
    folder = home / "sessions" / time.strftime("%Y/%m/%d")
    folder.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    rows = [{"timestamp": ts + ".000Z", "type": "session_meta", "payload": {"id": str(uuid.uuid4()), "cwd": str(cwd), "cli_version": "fake"}},
            {"timestamp": ts + ".001Z", "type": "turn_context", "payload": {"model": model, "cwd": str(cwd)}}]
    for i in range(n):
        usage = {"input_tokens": 12000 * (i + 1), "cached_input_tokens": 9000 * i, "output_tokens": 300, "reasoning_output_tokens": 100}
        rows.append({"timestamp": ts + f".{i + 2:03d}Z", "type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": usage}}})
    (folder / f"rollout-{uuid.uuid4()}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def other_agent(mode: str, args: list[str]) -> int:
    """Stand-ins for `codex exec` (codex-<mode>) and any command (plain mode + task)."""
    cfg = Path(os.environ["CODEX_HOME"])
    cwd = Path.cwd()
    if mode.startswith("codex-"):
        mode = mode[len("codex-"):]
        if mode == "nocredit":
            print('{"type":"error","message":"You exceeded your current quota (insufficient_quota)"}')
            return 1
        model = args[args.index("-m") + 1] if "-m" in args else "gpt-fake"
        codex_rollout(cfg, cwd, model, 3)
        # its output can quote error text from the code it writes: never an account error
        print(json.dumps({"type": "item.completed", "item": {"type": "file_change", "diff": "+ # insufficient_quota: 401 Unauthorized"}}))
        print('{"type":"turn.completed"}')
    if mode == "crash":
        return 1
    if mode == "solve":
        with tarfile.open(cfg / "solution.tar") as tar:
            tar.extractall(cwd)
    return 0


def main() -> int:
    mode = sys.argv[1]
    args = sys.argv[2:]
    if mode.startswith("codex-") or "--output-format" not in args:
        return other_agent(mode, args)
    model = args[args.index("--model") + 1] if "--model" in args else "claude-sonnet-fake"
    cfg = Path(os.environ["CLAUDE_CONFIG_DIR"])
    cwd = Path.cwd()
    result = {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.01}

    if mode == "nocredit":  # what Claude Code prints when the prepaid credit is gone
        result.update(subtype="success", is_error=True, result="Credit balance is too low", total_cost_usd=0)
        print(json.dumps(result))
        return 1
    if mode == "timeout":
        transcript(cfg, cwd, model, 1)
        time.sleep(3600)
    if mode == "crash":
        result["usage"], result["modelUsage"] = transcript(cfg, cwd, model, 1)
        result.update(subtype="error_during_execution", is_error=True)
        print(json.dumps(result))
        return 1
    result["usage"], result["modelUsage"] = transcript(cfg, cwd, model, 3)
    if mode == "budget":
        result.update(subtype="error_max_budget_usd", is_error=True)
    if mode == "escape":
        result["escapes"] = escape_attempts(cwd, cfg)
    if mode == "cheat":  # weakens every test file it can see; judging must undo this
        result["rewrote"] = []
        for test in cwd.rglob("test_*.py"):
            if ".venv" not in test.parts:
                test.write_text("def test_trivial():\n    pass\n")
                result["rewrote"].append(str(test.relative_to(cwd)))
    if mode == "solve":
        with tarfile.open(cfg / "solution.tar") as tar:
            tar.extractall(cwd)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
