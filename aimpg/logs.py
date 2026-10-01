"""Tolerant parser for Claude Code session logs (~/.claude/projects/**/*.jsonl).

The log format is not a public API, so nothing here is fatal: every row we
can't use is counted in `ParseResult.stats` and surfaced by the receipt's
coverage line instead of being dropped silently.

Two facts about the format drive the design (measured on real logs):

* Claude Code writes one row per content block and repeats the request's
  `usage` on each, so summing rows overcounts ~2.5x. We dedupe by
  `requestId` and keep the row with the largest `output_tokens` (partial
  streaming rows can carry smaller counts).
* Logs are filed under the folder a session *started* in, which is often
  not the repo it worked on. Every row carries its own `cwd`; we use that
  and never the log folder name.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator

from aimpg.model import CommitCall, ParseResult, Request, Usage

DEFAULT_ROOT = Path.home() / ".claude" / "projects"

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SYNTHETIC_MODEL = "<synthetic>"

# `git commit` as a command, not text inside a pipe like `git log | grep commit`.
_GIT_COMMIT = re.compile(r"\bgit\b[^|;&\n]*?\bcommit\b")


def parse_ts(value: str | None) -> float | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def iter_log_files(root: Path = DEFAULT_ROOT) -> Iterator[Path]:
    if not root.is_dir():
        return
    yield from sorted(root.rglob("*.jsonl"))


def commit_cwd(command: str, row_cwd: str) -> str | None:
    """Repo dir a `git commit` command runs in, or None if it isn't one.

    Handles a leading `cd <dir> &&` and `git -C <dir> commit`.
    """
    for segment in re.split(r"&&|;|\|\|", command):
        if not _GIT_COMMIT.search(segment):
            continue
        cwd = row_cwd
        for prev in re.split(r"&&|;|\|\|", command[: command.find(segment)]):
            cd = re.match(r"\s*cd\s+(.+?)\s*$", prev)
            if cd:
                cwd = _resolve(cd.group(1), cwd)
        try:
            tokens = shlex.split(segment)
        except ValueError:
            tokens = segment.split()
        if "-C" in tokens:
            i = tokens.index("-C")
            if i + 1 < len(tokens):
                cwd = _resolve(tokens[i + 1], cwd)
        return cwd
    return None


_REDIRECT = re.compile(r"(?<![0-9&>])>>?\s*([^\s|;&<>()]+)")
_FILE_COMMANDS = {"mv", "cp", "rm", "touch", "tee"}
_PATH_COMMANDS = _FILE_COMMANDS | {"sed", "perl"}


def bash_paths(command: str) -> set[str]:
    """Files a shell command writes or moves, as written (not resolved).

    Covers `sed -i`, `perl -pi/-i`, `>`/`>>` redirects, and mv/cp/rm/touch/tee.
    Best effort: anything missed falls through to "unattributed", never to a
    wrong commit.
    """
    found: set[str] = set()
    for target in _REDIRECT.findall(command):
        if not target.startswith(("/dev/", "&")):
            found.add(target.strip("'\""))
    for segment in re.split(r"&&|\|\||;|\|", command):
        first = segment.split(None, 1)[0] if segment.strip() else ""
        if os.path.basename(first.strip("'\"")) not in _PATH_COMMANDS:
            continue  # full shell tokenizing is slow; only do it for commands we read
        try:
            tokens = shlex.split(segment)
        except ValueError:
            continue
        # drop redirect tokens so their targets aren't read as arguments
        tokens = [t for t in tokens if not re.match(r"^\d*>>?", t)]
        if not tokens:
            continue
        cmd, args = os.path.basename(tokens[0]), tokens[1:]
        if cmd == "sed" and any(a == "-i" or a.startswith("-i") for a in args):
            found.update(_sed_files(args))
        elif cmd == "perl" and any(a.startswith("-") and "i" in a for a in args):
            found.update(_after_script(args))
        elif cmd in _FILE_COMMANDS:
            found.update(a for a in args if not a.startswith("-"))
    return {p for p in found if p and not p.startswith("$")}


def _sed_files(args: list[str]) -> list[str]:
    script_given = any(a in ("-e", "--expression") or a.startswith("-e") for a in args)
    files, skip_next, seen_script = [], False, script_given
    for k, a in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        if a in ("-e", "--expression", "-f"):
            skip_next = True
            continue
        if a == "-i" and k + 1 < len(args) and (args[k + 1] == "" or args[k + 1].startswith(".")):
            skip_next = True  # BSD/macOS: `-i ''` or `-i .bak` takes a backup suffix
            continue
        if a.startswith("-"):
            continue
        if not seen_script:
            seen_script = True  # first positional is the sed script
            continue
        files.append(a)
    return files


def _after_script(args: list[str]) -> list[str]:
    files, skip_next = [], False
    for a in args:
        if skip_next:
            skip_next = False
            continue
        if a.startswith("-"):
            skip_next = a.endswith("e")  # -e / -pe / -pie take the script next
            continue
        files.append(a)
    return files


def _resolve(path: str, base: str) -> str:
    path = path.strip().strip("'\"")
    path = os.path.expanduser(path)
    return os.path.normpath(path if os.path.isabs(path) else os.path.join(base, path))


def _usage(raw: object) -> Usage | None:
    if not isinstance(raw, dict):
        return None
    try:
        values = [
            int(raw.get(k) or 0)
            for k in (
                "input_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
                "output_tokens",
            )
        ]
    except (TypeError, ValueError):
        return None
    if any(v < 0 for v in values):
        return None
    return Usage(*values)


_TOOL_USE_ID = re.compile(rb'"tool_use_id":\s*"([^"]+)"')


def _worth_parsing(line: bytes, pending_commits: dict[str, CommitCall]) -> bool:
    """Cheap byte check so we only JSON-decode rows that can matter.

    Most bytes in a log are tool results (file contents, command output) we
    never use. A row can only carry usage if it contains `"usage"`, and only
    issue tool calls if it contains `"tool_use"`; a tool result matters only
    when it closes a `git commit` call we're timing.
    """
    if b'"usage"' in line or b'"tool_use"' in line:
        return True
    if b'"tool_result"' in line:
        return any(m.decode() in pending_commits for m in _TOOL_USE_ID.findall(line))
    return False


def parse_logs(files: Iterable[Path]) -> ParseResult:
    stats: Counter[str] = Counter()
    versions: Counter[str] = Counter()
    best: dict[str, Request] = {}
    pending_commits: dict[str, CommitCall] = {}
    files_touched: dict[str, set[tuple[str, str]]] = {}

    for path in files:
        stats["files"] += 1
        with open(path, "rb") as fh:
            for line in fh:
                if not line.strip():
                    continue
                stats["rows"] += 1
                if not _worth_parsing(line, pending_commits):
                    continue
                try:
                    row = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    stats["corrupt_rows"] += 1
                    continue
                if not isinstance(row, dict):
                    stats["corrupt_rows"] += 1
                    continue
                if row.get("version"):
                    versions[str(row["version"])] += 1
                _handle_row(row, stats, best, pending_commits, files_touched)

    result = ParseResult(
        requests=sorted(best.values(), key=lambda r: r.ts),
        commit_calls=sorted(pending_commits.values(), key=lambda c: c.start),
        files_touched=files_touched,
        versions=dict(versions),
    )
    stats["unique_requests"] = len(best)
    result.stats = dict(stats)
    return result


def _handle_row(
    row: dict,
    stats: Counter[str],
    best: dict[str, Request],
    pending_commits: dict[str, CommitCall],
    files_touched: dict[str, set[tuple[str, str]]],
) -> None:
    message = row.get("message")
    if not isinstance(message, dict):
        return
    session = str(row.get("sessionId") or "")
    cwd = str(row.get("cwd") or "")
    ts = parse_ts(row.get("timestamp"))

    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "tool_use":
                _tool_use(block, session, cwd, ts, pending_commits, files_touched)
            elif kind == "tool_result":
                call = pending_commits.get(str(block.get("tool_use_id")))
                if call is not None and ts is not None and call.end is None:
                    pending_commits[str(block.get("tool_use_id"))] = CommitCall(
                        call.session_id, call.cwd, call.start, ts
                    )

    if row.get("type") != "assistant" or "usage" not in message:
        return
    stats["usage_rows"] += 1
    model = str(message.get("model") or "")
    if model == SYNTHETIC_MODEL:
        stats["skipped_synthetic"] += 1
        return
    usage = _usage(message.get("usage"))
    key = row.get("requestId") or (
        f"{session}:{message['id']}" if session and message.get("id") else None
    )
    if usage is None or ts is None or not key:
        stats["skipped_requests"] += 1
        stats["skipped_bad_shape"] += 1
        return
    request = Request(
        id=str(key),
        session_id=session,
        model=model,
        ts=ts,
        usage=usage,
        cwd=cwd,
        is_sidechain=bool(row.get("isSidechain")),
    )
    current = best.get(request.id)
    if current is None:
        best[request.id] = request
    else:
        stats["duplicate_rows"] += 1
        if usage.output > current.usage.output:
            best[request.id] = request


def _tool_use(
    block: dict,
    session: str,
    cwd: str,
    ts: float | None,
    pending_commits: dict[str, CommitCall],
    files_touched: dict[str, set[tuple[str, str]]],
) -> None:
    name = block.get("name")
    args = block.get("input") if isinstance(block.get("input"), dict) else {}
    if name == "Bash":
        command = str(args.get("command") or "")
        repo_dir = commit_cwd(command, cwd)
        if repo_dir and ts is not None and block.get("id"):
            pending_commits[str(block["id"])] = CommitCall(session, repo_dir, ts, None)
        for target in bash_paths(command):
            files_touched.setdefault(session, set()).add((cwd, target))
    elif name in EDIT_TOOLS:
        target = args.get("file_path") or args.get("notebook_path")
        if target:
            files_touched.setdefault(session, set()).add((cwd, str(target)))
