"""Label which Claude Code session produced each of a sample of your commits.

    uv run python evals/label.py [--n 30] [--days 30] [--seed 7]

For each sampled commit you see its message and files, then up to 5
candidate sessions that were active shortly before it (with the prompt you
typed nearest to the commit). Type the number of the session that made it,
0 if it wasn't made with AI, s to skip, q to stop. The tool's own guess is
never shown, so it can't bias you.

Everything stays local: labels go to evals/data/labels.jsonl (gitignored).
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from aimpg.attribution import attribute  # noqa: E402
from aimpg.gitkept import DAY, load_commits, user_email  # noqa: E402
from aimpg.logs import DEFAULT_ROOT, iter_log_files, parse_logs, parse_ts  # noqa: E402

LABELS = ROOT / "evals" / "data" / "labels.jsonl"
LOOKBACK = 6 * 3600


def user_prompts(files) -> dict[str, list[tuple[float, str]]]:
    """session -> [(ts, text)] of prompts you typed (not tool results)."""
    out: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for path in files:
        with open(path, "rb") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or row.get("type") != "user" or row.get("isSidechain"):
                    continue
                content = (row.get("message") or {}).get("content")
                text = content if isinstance(content, str) else " ".join(
                    b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"
                )
                ts = parse_ts(row.get("timestamp"))
                auto = text.lstrip().startswith(("<", "Base directory for this skill", "[SYSTEM", "This session is being continued", "[Image", "Another Claude session"))
                if text.strip() and ts and not auto:
                    out[str(row.get("sessionId"))].append((ts, " ".join(text.split())))
    return out


def sample(n: int, days: int, seed: int, done: set[tuple[str, str]]) -> list[dict]:
    """Up to n of your commits, each with the sessions that could have made it.

    Candidates are sessions active in the 6h before the commit that also
    worked in that repo (ran there, committed there, or edited files there),
    nearest first. The tool's own guess is never included.
    """
    now = time.time()
    since = now - days * DAY
    files = list(iter_log_files(DEFAULT_ROOT))
    parsed = parse_logs(files)
    attribution = attribute(parsed, since, now)
    prompts = user_prompts(files)
    by_session = defaultdict(list)
    for r in parsed.requests:
        by_session[r.session_id].append(r)

    commits = []
    for repo in sorted(attribution.repos):
        me = user_email(repo)
        commits += [(repo, c) for c in load_commits(repo, since, reflog=False) if c.author_email == me and (repo, c.sha) not in done]
    random.Random(seed).shuffle(commits)

    out = []
    for repo, commit in commits[:n]:
        candidates = []
        for session, reqs in by_session.items():
            if repo not in attribution.session_repos.get(session, set()):
                continue
            near = [r for r in reqs if commit.ts - LOOKBACK <= r.ts <= commit.ts + 300]
            if not near:
                continue
            before = [p for p in prompts.get(session, []) if p[0] <= commit.ts]
            candidates.append({
                "session": session,
                "minutes_away": round(min(abs(commit.ts - r.ts) for r in near) / 60),
                "requests": len(near),
                "folders": sorted({Path(r.cwd).name for r in near}),
                "last_prompt": before[-1][1][:140] if before else "",
            })
        candidates.sort(key=lambda c: c["minutes_away"])
        out.append({
            "repo": repo,
            "sha": commit.sha,
            "when": datetime.fromtimestamp(commit.ts).strftime("%Y-%m-%d %H:%M"),
            "subject": commit.subject,
            "files": sorted(commit.files)[:6],
            "candidates": candidates[:5],
        })
    return out


def load_done(path: Path) -> set[tuple[str, str]]:
    if not path.exists():
        return set()
    return {(l["repo"], l["sha"]) for l in map(json.loads, path.read_text().splitlines()) if l}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--n", type=int, default=30)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", type=Path, default=LABELS)
    parser.add_argument("--export", type=Path, help="write the questions as JSON instead of asking in the terminal")
    args = parser.parse_args()

    items = sample(args.n, args.days, args.seed, load_done(args.out))
    if args.export:
        args.export.parent.mkdir(parents=True, exist_ok=True)
        args.export.write_text(json.dumps(items, indent=1))
        print(f"Wrote {len(items)} questions to {args.export}")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    labeled = 0
    with open(args.out, "a") as out:
        for k, item in enumerate(items, 1):
            print("\n" + "=" * 78)
            print(f"[{k}/{len(items)}] {Path(item['repo']).name} {item['sha'][:8]}  {item['when']}")
            print(f"  {item['subject']}")
            print(f"  files: {', '.join(item['files'])}")
            cands = item["candidates"]
            if not cands:
                print("  (no Claude Code session worked in this repo in the 6h before this commit)")
            for i, c in enumerate(cands, 1):
                print(f"  {i}) session {c['session'][:8]}  last request {c['minutes_away']} min away  "
                      f"{c['requests']} requests  in {', '.join(c['folders'])}")
                print(f"       you typed: {c['last_prompt'] or '(no typed prompt found)'}")
            while True:
                answer = input("  which session made it? [1-5 / 0 = not AI / s = skip / q = quit] ").strip().lower()
                if answer in {"q", "s", "0"} or (answer.isdigit() and 1 <= int(answer) <= len(cands)):
                    break
            if answer == "q":
                break
            if answer == "s":
                continue
            session = None if answer == "0" else cands[int(answer) - 1]["session"]
            out.write(json.dumps({"repo": item["repo"], "sha": item["sha"], "session": session}) + "\n")
            out.flush()
            labeled += 1
    print(f"\nSaved {labeled} labels to {args.out}. Now run: uv run python evals/attribution_eval.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
