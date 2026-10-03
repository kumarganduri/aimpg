"""Live signals inside Claude Code: a status line and a post-commit note.

The monthly receipt is read once; behavior changes at the moment of decision.

    status line (after each AI message, ~300ms debounce):
      ⚡ task $1.10 · usual $0.90 · ctx 142k (69% from before your last commit) · 5h 63%

    after the agent runs `git commit` (PostToolUse hook, shown to you only):
      aimpg: that commit cost $2.40 over 31 requests (≈ 2 phone charges).
             69% of the context is from before it: /clear before the next task saves ~69% per request.

Speed matters (this runs constantly), so:
  * the session transcript is read incrementally: only bytes added since the
    last call, with the parse state saved in ~/.aimpg/live/<session>.json;
  * "usual" (your median cost per kept commit) comes from a daily cache that is
    refreshed in a detached background process, never inline.
Nothing here talks to the network or changes how the agent works.
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from aimpg.cost import usage_cost
from aimpg.logs import _GIT_COMMIT, _worth_parsing, parse_ts
from aimpg.model import Usage

LIVE_DIR = Path.home() / ".aimpg" / "live"
BASELINE_MAX_AGE = 24 * 3600
CLEAR_HINT_SHARE = 0.4  # suggest /clear when this much of the context predates the last commit


@dataclass
class SessionState:
    offset: int = 0
    base_ctx: int = 0  # context at session start (system prompt + tools)
    task: dict = field(default_factory=dict)  # requestId -> [ts, model, fresh, write, read, out, write_1h, sidechain]
    pending: dict = field(default_factory=dict)  # tool_use id of a `git commit` -> start ts
    commits: list = field(default_factory=list)  # [{ts, cost, requests}]
    first_ctx_after_commit: int = 0
    last_ctx: int = 0

    @classmethod
    def load(cls, session: str) -> "SessionState":
        path = LIVE_DIR / f"{_safe(session)}.json"
        try:
            return cls(**json.loads(path.read_text()))
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self, session: str) -> None:
        LIVE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = LIVE_DIR / f"{_safe(session)}.json.tmp"
        tmp.write_text(json.dumps(asdict(self)))
        tmp.replace(LIVE_DIR / f"{_safe(session)}.json")

    # --- what the views need -------------------------------------------------
    def task_cost(self) -> float:
        return sum(usage_cost(_usage(v), v[1]) or 0.0 for v in self.task.values())

    def carried_share(self) -> float:
        """Share of the current context that was already there before the last commit."""
        if not self.commits or not self.last_ctx:
            return 0.0
        carried = max(0, self.first_ctx_after_commit - self.base_ctx)
        return min(1.0, carried / self.last_ctx)

    def stale_share_now(self) -> float:
        """At commit time: share of the current context that the next task doesn't need."""
        if not self.last_ctx:
            return 0.0
        return max(0.0, min(1.0, (self.last_ctx - self.base_ctx) / self.last_ctx))

    def close_task(self, ts: float) -> dict:
        """A commit happened at `ts`: everything so far was that commit's work."""
        done = {"ts": ts, "cost": round(self.task_cost(), 4), "requests": len(self.task),
                "stale_share": round(self.stale_share_now(), 3), "announced": False}
        self.commits.append(done)
        self.task = {}
        self.first_ctx_after_commit = 0
        return done


def _safe(session: str) -> str:
    return "".join(c for c in session if c.isalnum() or c in "-_")[:80] or "unknown"


def _usage(v: list) -> Usage:
    return Usage(fresh_in=v[2], cache_write=v[3], cache_read=v[4], output=v[5], cache_write_1h=v[6])


def update(state: SessionState, transcript: Path) -> SessionState:
    """Read only what was appended since last time. Partial last lines wait for the next call."""
    try:
        size = transcript.stat().st_size
    except OSError:
        return state
    if size < state.offset:  # transcript replaced: start over
        state = SessionState()
    with open(transcript, "rb") as fh:
        fh.seek(state.offset)
        chunk = fh.read()
    end = chunk.rfind(b"\n")
    if end < 0:
        return state
    for line in chunk[: end + 1].splitlines():
        if line.strip() and _worth_parsing(line, state.pending):
            try:
                _row(state, json.loads(line))
            except (ValueError, TypeError, KeyError):
                continue
    state.offset += end + 1
    return state


def _row(state: SessionState, row: dict) -> None:
    message = row.get("message") if isinstance(row.get("message"), dict) else {}
    ts = parse_ts(row.get("timestamp"))
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use" and block.get("name") == "Bash" and ts is not None:
            if _GIT_COMMIT.search(str((block.get("input") or {}).get("command") or "")):
                state.pending[str(block.get("id"))] = ts
        elif block.get("type") == "tool_result" and str(block.get("tool_use_id")) in state.pending and ts is not None:
            del state.pending[str(block.get("tool_use_id"))]
            if not state.commits or state.commits[-1]["ts"] < ts - 5:  # the hook may have closed it already
                state.close_task(ts)
    usage = message.get("usage")
    if row.get("type") != "assistant" or not isinstance(usage, dict) or ts is None:
        return
    if message.get("model") == "<synthetic>":
        return
    key = row.get("requestId") or message.get("id")
    if not key:
        return
    details = usage.get("output_tokens_details") if isinstance(usage.get("output_tokens_details"), dict) else {}
    split = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    values = [
        ts,
        str(message.get("model") or ""),
        int(usage.get("input_tokens") or 0),
        int(usage.get("cache_creation_input_tokens") or 0),
        int(usage.get("cache_read_input_tokens") or 0),
        int(usage.get("output_tokens") or 0) + int(details.get("thinking_tokens") or 0),
        int(split.get("ephemeral_1h_input_tokens") or 0),
        bool(row.get("isSidechain")),
    ]
    current = state.task.get(key)
    if current is None or values[5] > current[5]:
        state.task[key] = values
    if not values[7]:
        ctx = values[2] + values[3] + values[4]
        state.last_ctx = ctx
        if not state.base_ctx:
            state.base_ctx = ctx
        if state.commits and not state.first_ctx_after_commit:
            state.first_ctx_after_commit = ctx


# --- "usual": median cost per kept commit, from a daily cache -------------------

def baseline_path() -> Path:
    return LIVE_DIR / "baseline.json"


def read_baseline() -> float | None:
    try:
        data = json.loads(baseline_path().read_text())
    except (OSError, ValueError):
        data = {}
    if time.time() - data.get("computed_at", 0) > BASELINE_MAX_AGE:
        _refresh_in_background()
    value = data.get("median_usd_per_commit")
    return float(value) if isinstance(value, (int, float)) and value > 0 else None


def _refresh_in_background() -> None:
    lock = LIVE_DIR / "baseline.lock"
    try:
        if lock.exists() and time.time() - lock.stat().st_mtime < 600:
            return  # a refresh is already running
        LIVE_DIR.mkdir(parents=True, exist_ok=True)
        lock.write_text(str(os.getpid()))
        subprocess.Popen(
            [sys.executable, "-m", "aimpg.live", "refresh-baseline"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass


def compute_baseline() -> dict:
    from aimpg.attribution import attribute
    from aimpg.logs import iter_log_files, parse_logs
    from aimpg.receipt import KEPT, task_energy

    now = time.time()
    parsed = parse_logs(iter_log_files())
    attribution = attribute(parsed, now - 30 * 86400, now)
    all_energies = task_energy(attribution.tasks)
    from aimpg import ledger

    ledger.record(all_energies)  # the daily refresh also keeps the history growing
    energies = [e for e in all_energies if e.task.requests]
    kept = [e for e in energies if e.task.status in KEPT]
    pool = kept if len(kept) >= 5 else energies
    data = {
        "computed_at": now,
        "median_usd_per_commit": round(statistics.median(e.usd for e in pool), 4) if pool else None,
        "commits": len(pool),
    }
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    baseline_path().write_text(json.dumps(data))
    (LIVE_DIR / "baseline.lock").unlink(missing_ok=True)
    return data


# --- views ------------------------------------------------------------------------

AMBER, RED, DIM, RESET = "\033[33m", "\033[31m", "\033[2m", "\033[0m"


def _usd(x: float) -> str:
    return f"${x:,.0f}" if x >= 100 else f"${x:.2f}"


def _k(n: int) -> str:
    return f"{n / 1000:.0f}k" if n >= 1000 else str(n)


def status_line(data: dict, state: SessionState, usual: float | None) -> str:
    task = state.task_cost()
    parts = []
    color = ""
    if usual:
        if task > 3 * usual:
            color = RED
        elif task > 1.5 * usual:
            color = AMBER
    parts.append(f"{color}task {_usd(task)}{RESET if color else ''}")
    if usual:
        parts.append(f"usual {_usd(usual)}")
    ctx = (data.get("context_window") or {}).get("total_input_tokens") or state.last_ctx
    if ctx:
        share = state.carried_share()
        if share >= 0.05:
            parts.append(f"ctx {_k(ctx)} ({share:.0%} from before your last commit)")
        else:
            parts.append(f"ctx {_k(ctx)}")
        if share >= CLEAR_HINT_SHARE and len(state.task) <= 3:
            parts.append(f"/clear ≈ -{share:.0%} per request")
    five = ((data.get("rate_limits") or {}).get("five_hour") or {}).get("used_percentage")
    if isinstance(five, (int, float)):
        parts.append(f"5h {five:.0f}%")
    return "⚡ " + " · ".join(parts)


def commit_note(done: dict) -> str:
    msg = f"aimpg: that commit cost {_usd(done['cost'])} over {done['requests']} AI requests."
    share = done.get("stale_share", 0.0)
    if share >= CLEAR_HINT_SHARE:
        msg += f" {share:.0%} of the context is from earlier work: /clear before the next task saves ~{share:.0%} per request."
    return msg


def _read_stdin_json() -> dict:
    try:
        return json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return {}


def run_statusline(previous: str | None = None) -> int:
    raw = sys.stdin.read()
    try:
        data = json.loads(raw or "{}")
    except ValueError:
        data = {}
    out = []
    if previous:  # keep the user's own status line, then add ours
        try:
            prev = subprocess.run(previous, shell=True, input=raw, capture_output=True, text=True, timeout=2)
            if prev.stdout.strip():
                out.append(prev.stdout.rstrip("\n"))
        except (subprocess.TimeoutExpired, OSError):
            pass
    session, transcript = data.get("session_id"), data.get("transcript_path")
    if session and transcript:
        state = update(SessionState.load(session), Path(transcript))
        state.save(session)
        out.append(status_line(data, state, read_baseline()))
    print("\n".join(out))
    return 0


def run_post_commit_hook() -> int:
    data = _read_stdin_json()
    command = str((data.get("tool_input") or {}).get("command") or "")
    if data.get("tool_name") != "Bash" or not _GIT_COMMIT.search(command):
        return 0  # most Bash calls: nothing to do, exit fast
    session, transcript = data.get("session_id"), data.get("transcript_path")
    if not session or not transcript:
        return 0
    state = update(SessionState.load(session), Path(transcript))
    if state.task:
        done = state.close_task(time.time())
    elif state.commits and not state.commits[-1].get("announced") and time.time() - state.commits[-1]["ts"] < 120:
        done = state.commits[-1]  # the status line already saw this commit in the transcript
    else:
        state.save(session)
        return 0
    done["announced"] = True
    state.save(session)
    print(json.dumps({"systemMessage": commit_note(done)}))
    return 0


if __name__ == "__main__":  # python -m aimpg.live refresh-baseline
    if sys.argv[1:] == ["refresh-baseline"]:
        compute_baseline()


# --- install / uninstall into Claude Code settings --------------------------------------

SETTINGS = Path.home() / ".claude" / "settings.json"
PREVIOUS = LIVE_DIR / "previous_statusline.json"
OURS = ("aimpg statusline", "aimpg hook post-commit")


def planned_settings(current: dict, exe: str) -> dict:
    """The settings after install: our status line (wrapping any existing one) + a commit hook."""
    new = json.loads(json.dumps(current))
    new["statusLine"] = {"type": "command", "command": f"{exe} statusline"}
    hooks = new.setdefault("hooks", {}).setdefault("PostToolUse", [])
    if not any(h.get("command", "").endswith("hook post-commit") for e in hooks for h in e.get("hooks", [])):
        hooks.append({"matcher": "Bash", "hooks": [{"type": "command", "command": f"{exe} hook post-commit", "timeout": 10}]})
    return new


def install(settings: Path = SETTINGS, exe: str = "aimpg", *, confirm=input) -> str:
    current = json.loads(settings.read_text()) if settings.exists() else {}
    existing = (current.get("statusLine") or {}).get("command", "")
    if existing.endswith("statusline") and "aimpg" in existing:
        return "aimpg live is already installed."
    new = planned_settings(current, exe)
    print(f"This will change {settings}:")
    print(f"  statusLine → {exe} statusline" + (f"  (your current one, `{existing}`, keeps showing above it)" if existing else ""))
    print(f"  PostToolUse hook (Bash) → {exe} hook post-commit  (shows you one line after the agent commits)")
    if confirm("Apply? [y/N] ").strip().lower() != "y":
        return "Nothing changed."
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    if existing:
        PREVIOUS.write_text(json.dumps({"command": existing}))
    if settings.exists():
        backup = settings.with_suffix(f".json.aimpg-backup-{int(time.time())}")
        backup.write_text(settings.read_text())
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(new, indent=2) + "\n")
    return "Installed. It shows up after the next AI message. Undo any time with `aimpg live uninstall`."


def uninstall(settings: Path = SETTINGS) -> str:
    if not settings.exists():
        return "Nothing to remove."
    current = json.loads(settings.read_text())
    if "aimpg" in (current.get("statusLine") or {}).get("command", ""):
        try:
            current["statusLine"] = {"type": "command", "command": json.loads(PREVIOUS.read_text())["command"]}
        except (OSError, ValueError, KeyError):
            current.pop("statusLine", None)
    post = (current.get("hooks") or {}).get("PostToolUse") or []
    kept = [e for e in post if not any("aimpg" in h.get("command", "") and "post-commit" in h.get("command", "") for h in e.get("hooks", []))]
    if post:
        if kept:
            current["hooks"]["PostToolUse"] = kept
        else:
            current["hooks"].pop("PostToolUse")
            if not current["hooks"]:
                current.pop("hooks")
    settings.write_text(json.dumps(current, indent=2) + "\n")
    PREVIOUS.unlink(missing_ok=True)
    return "Removed. Your previous status line (if any) is back."


def previous_statusline() -> str | None:
    try:
        return json.loads(PREVIOUS.read_text())["command"]
    except (OSError, ValueError, KeyError):
        return None
