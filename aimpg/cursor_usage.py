"""Read Cursor's usage export (cursor.com/dashboard → Usage → Export CSV).

Cursor keeps token counts on its servers, not on your machine, so the export is
the only per-request source. Columns (checked against a public sample export):

    Date, [User], Kind, Model, Max Mode, Input (w/ Cache Write),
    Input (w/o Cache Write), Cache Read, Output Tokens, Total Tokens, Cost

The four token columns add up to Total Tokens, so "Input (w/ Cache Write)" is
the cache-write count and "Input (w/o Cache Write)" is fresh input.

The export doesn't say which folder a request worked in. Its requests always
count in the totals; with a repo given, they are matched to your own commits
there by time alone (the "time" tier: no file evidence, lowest confidence).
"""

from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Iterable

from aimpg.logs import parse_ts
from aimpg.model import ParseResult, Request, Usage

SESSION_PREFIX = "cursor:"
REQUIRED = ("Date", "Model", "Input (w/ Cache Write)", "Input (w/o Cache Write)", "Cache Read", "Output Tokens")
_CURSOR_CLAUDE = re.compile(r"^claude-(\d+)(?:\.(\d+))?-(opus|sonnet|haiku|fable)\b")


class CursorExportError(ValueError):
    pass


def normalize_model(name: str) -> str:
    """Cursor's model ids onto Anthropic's: claude-4.6-sonnet-medium-thinking → claude-sonnet-4-6."""
    m = _CURSOR_CLAUDE.match(name)
    if m:
        major, minor, family = m.groups()
        return f"claude-{family}-{major}" + (f"-{minor}" if minor else "")
    if name.startswith("claude-"):
        return name.replace(".", "-")
    return name


def _int(value: str | None) -> int:
    try:
        return int(float((value or "0").replace(",", "")))
    except ValueError:
        return 0


def parse_cursor(paths: Iterable[Path], repo: str = "", user: str | None = None) -> ParseResult:
    result = ParseResult()
    seen: set[tuple] = set()
    stats = {"files": 0, "rows": 0, "unique_requests": 0, "corrupt_rows": 0}
    for path in paths:
        stats["files"] += 1
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            missing = [c for c in REQUIRED if c not in (reader.fieldnames or [])]
            if missing:
                raise CursorExportError(f"{path}: not a Cursor usage export (missing columns: {', '.join(missing)})")
            for row in reader:
                stats["rows"] += 1
                if user is not None and row.get("User", user) != user:
                    continue
                ts = parse_ts(row.get("Date"))
                if ts is None:
                    stats["corrupt_rows"] += 1
                    continue
                usage = Usage(
                    fresh_in=_int(row["Input (w/o Cache Write)"]),
                    cache_write=_int(row["Input (w/ Cache Write)"]),
                    cache_read=_int(row["Cache Read"]),
                    output=_int(row["Output Tokens"]),
                )
                if not (usage.ctx_len or usage.output):
                    continue  # errored / not-charged events carry no tokens
                key = (row["Date"], row["Model"], row.get("User", ""), usage)
                if key in seen:  # overlapping exports
                    continue
                seen.add(key)
                result.requests.append(Request(
                    id=f"cursor:{len(seen)}",
                    session_id=SESSION_PREFIX + (row.get("User") or "me"),
                    model=normalize_model(row["Model"].strip()),
                    ts=ts,
                    usage=usage,
                    cwd=repo,
                ))
    stats["unique_requests"] = len(seen)
    result.requests.sort(key=lambda r: r.ts)
    result.stats = stats
    result.versions = {"cursor export": len(seen)} if seen else {}
    return result
