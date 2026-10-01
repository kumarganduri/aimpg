"""A free stand-in for Claude Code, used only in tests. Speaks the same protocol:

* writes a transcript to $CLAUDE_CONFIG_DIR/projects/<cwd>/<session>.jsonl
  (assistant rows with usage, like the real thing);
* prints one JSON result to stdout (`--output-format json` shape).

Modes: solve (apply the solution the test staged), nothing, crash, timeout,
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


def transcript(cfg: Path, cwd: Path, model: str, n: int) -> dict:
    session = str(uuid.uuid4())
    folder = cfg / "projects" / str(cwd).replace("/", "-")
    folder.mkdir(parents=True, exist_ok=True)
    totals = {"input_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0}
    with open(folder / f"{session}.jsonl", "w") as fh:
        for i in range(n):
            usage = {"input_tokens": 5, "cache_creation_input_tokens": 3000, "cache_read_input_tokens": 9000 * (i + 1), "output_tokens": 400}
            for k in totals:
                totals[k] += usage[k]
            row = {
                "type": "assistant",
                "sessionId": session,
                "cwd": str(cwd),
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                "requestId": f"req_fake_{session[:8]}_{i}",
                "version": "fake",
                "message": {"id": f"msg_{i}", "model": model, "content": [{"type": "text", "text": "..."}], "usage": usage},
            }
            fh.write(json.dumps(row) + "\n")
    return totals


def escape_attempts(cwd: Path, cfg: Path) -> list[str]:
    """Every attempt that SUCCEEDS is a sandbox bug."""
    succeeded = []
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    for target in (real_home, real_home / ".ssh", cwd.parent.parent):  # home, keys, sibling runs
        try:
            os.listdir(target)
            succeeded.append(f"read {target}")
        except OSError:
            pass
    try:
        Path("/private/tmp/aimpg-escape-probe").write_text("x")
        succeeded.append("write /private/tmp")
    except OSError:
        pass
    probe = subprocess.run(["/usr/bin/curl", "-s", "-m", "5", "-o", "/dev/null", "-w", "%{http_code}", "--noproxy", "*", "https://github.com"], capture_output=True, text=True)
    if probe.stdout.strip() not in ("000", ""):
        succeeded.append("direct internet")
    via = subprocess.run(["/usr/bin/curl", "-s", "-m", "5", "-o", "/dev/null", "-w", "%{http_code}", "https://github.com"], capture_output=True, text=True)
    if via.stdout.strip() not in ("000", ""):
        succeeded.append("internet via proxy")
    return succeeded


def main() -> int:
    mode = sys.argv[1]
    args = sys.argv[2:]
    model = args[args.index("--model") + 1] if "--model" in args else "claude-sonnet-fake"
    cfg = Path(os.environ["CLAUDE_CONFIG_DIR"])
    cwd = Path.cwd()
    result = {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.01}

    if mode == "timeout":
        transcript(cfg, cwd, model, 1)
        time.sleep(3600)
    if mode == "crash":
        result["usage"] = transcript(cfg, cwd, model, 1)
        result.update(subtype="error_during_execution", is_error=True)
        print(json.dumps(result))
        return 1
    result["usage"] = transcript(cfg, cwd, model, 3)
    if mode == "budget":
        result.update(subtype="error_max_budget_usd", is_error=True)
    if mode == "escape":
        result["escapes"] = escape_attempts(cwd, cfg)
    if mode == "solve":
        with tarfile.open(cfg / "solution.tar") as tar:
            tar.extractall(cwd)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
