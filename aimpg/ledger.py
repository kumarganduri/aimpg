"""aimpg's own history: what each AI-assisted commit cost, kept beyond Claude Code's logs.

Claude Code deletes transcripts after 30 days by default, but a commit can only
be called durable after 30 days. So every report records a small line per
commit in ~/.aimpg/ledger.json: cost, energy range, request count. Never
prompts, code or diffs. Re-running is safe: a commit's record is replaced
only by one with at least as many requests, so deleted logs never shrink it.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

LEDGER = Path.home() / ".aimpg" / "ledger.json"


def load(path: Path = LEDGER) -> dict[str, dict]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def key(repo: str, sha: str) -> str:
    return f"{repo}|{sha}"


def record(energies, path: Path = LEDGER) -> int:
    """Upsert one line per commit from receipt.task_energy(...). Returns how many changed."""
    data = load(path)
    changed = 0
    now = time.time()
    for e in energies:
        t = e.task
        requests = len(t.requests) + len(t.lead_up)
        if requests == 0:
            continue
        k = key(t.repo, t.sha)
        old = data.get(k)
        if old and old.get("requests", 0) > requests:
            continue  # logs were cleaned up since: keep the fuller record
        data[k] = {
            "repo": t.repo,
            "sha": t.sha,
            "ts": t.ts,
            "subject": t.subject,
            "author": t.author,
            "requests": requests,
            "usd": round(e.usd + e.lead_up_usd, 4),
            "wh_low": round(e.total.low, 3),
            "wh_high": round(e.total.high, 3),
            "first_seen": (old or {}).get("first_seen", now),
        }
        changed += old != data[k]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)
    return changed
