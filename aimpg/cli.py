"""aimpg: energy receipts for AI coding.

    report    the fuel receipt for your Claude Code usage, with measured tips
    pr        AI energy + cost of the current branch's commits (markdown; --post to the PR)
    export    one CSV row per AI-assisted commit, for teams and sustainability reports
    replay    compare agent setups and models on your own past commits
    live      install/uninstall the live status line + post-commit note in Claude Code
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from aimpg import share
from aimpg.attribution import attribute
from aimpg.gitkept import DAY
from aimpg.logs import DEFAULT_ROOT, iter_log_files, parse_logs
from aimpg.receipt import render
from aimpg.replay import cli as replay_cli


def _common(p: argparse.ArgumentParser, days: int) -> None:
    p.add_argument("--days", type=int, default=days, help=f"window size in days (default {days})")
    p.add_argument("--logs", type=Path, default=DEFAULT_ROOT, help="Claude Code projects dir")
    p.add_argument("--fetch", action="store_true", help="git fetch each repo first (uses the network)")


def _analyze(args):
    now = time.time()
    since = now - args.days * DAY
    parsed = parse_logs(iter_log_files(args.logs))
    return parsed, attribute(parsed, since, now, refresh=args.fetch), since, now


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aimpg", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    _common(sub.add_parser("report", help="print the energy receipt"), 30)

    pr = sub.add_parser("pr", help="AI energy and cost of this branch's commits, as a PR comment")
    _common(pr, 90)
    pr.add_argument("--repo", type=Path, default=Path("."), help="repo to summarize (default: current folder)")
    pr.add_argument("--base", default="main", help="base branch (default main)")
    pr.add_argument("--post", action="store_true", help="add the summary as a comment on the open PR (uses gh)")

    export = sub.add_parser("export", help="one CSV row per AI-assisted commit")
    _common(export, 30)
    export.add_argument("--csv", type=Path, required=True, help="output file")
    export.add_argument("--no-subjects", action="store_true", help="leave commit messages out (privacy)")
    export.add_argument("--no-authors", action="store_true", help="leave author emails out (privacy)")

    live = sub.add_parser("live", help="live status line + post-commit note inside Claude Code")
    live.add_argument("action", choices=("install", "uninstall"))
    sub.add_parser("statusline", help=argparse.SUPPRESS)  # called by Claude Code
    hook = sub.add_parser("hook", help=argparse.SUPPRESS)  # called by Claude Code
    hook.add_argument("event", choices=("post-commit",))

    replay_cli.add_parser(sub)
    args = parser.parse_args(argv)
    if args.command == "replay":
        return replay_cli.main(args)
    if args.command in ("statusline", "hook", "live"):
        from aimpg import live as live_mod

        if args.command == "statusline":
            return live_mod.run_statusline(live_mod.previous_statusline())
        if args.command == "hook":
            return live_mod.run_post_commit_hook()
        if args.action == "install":
            import shutil

            exe = shutil.which("aimpg")
            if exe is None:
                print("Install aimpg permanently first so Claude Code can call it:  uv tool install aimpg")
                return 1
            print(live_mod.install(exe=exe))
        else:
            print(live_mod.uninstall())
        return 0

    if args.days <= 0:
        parser.error("--days must be positive")
    if not args.logs.is_dir():
        print(f"No Claude Code logs found at {args.logs}. Nothing to report.")
        return 0
    parsed, attribution, since, now = _analyze(args)

    if args.command == "export":
        rows = share.csv_rows(attribution, subjects=not args.no_subjects, authors=not args.no_authors)
        share.write_csv(rows, args.csv)
        print(f"Wrote {len(rows)} commits to {args.csv}")
        return 0
    if args.command == "pr":
        try:
            shas = share.branch_commits(args.repo, args.base)
        except RuntimeError as exc:
            print(f"Can't list this branch's commits: {exc}")
            return 1
        markdown = share.pr_markdown(attribution, shas, args.repo.resolve().name)
        sys.stdout.write(markdown)
        if args.post:
            try:
                share.post_comment(markdown, args.repo)
            except RuntimeError as exc:
                print(f"\nNot posted: {exc}")
                return 1
            print("\nPosted to the pull request.")
        return 0

    from aimpg import durable, ledger
    from aimpg.receipt import task_energy

    ledger.record(task_energy(attribution.tasks))
    judged = durable.judge(ledger.load(), now)
    sys.stdout.write(render(parsed, attribution, since, now, judged))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
