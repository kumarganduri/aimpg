"""`aimpg report` prints the fuel receipt for your local Claude Code usage; `aimpg replay` compares agent setups on your own past commits."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from aimpg.attribution import attribute
from aimpg.gitkept import DAY
from aimpg.logs import DEFAULT_ROOT, iter_log_files, parse_logs
from aimpg.receipt import render
from aimpg.replay import cli as replay_cli


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aimpg", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("report", help="print the energy receipt")
    report.add_argument("--days", type=int, default=30, help="window size in days (default 30)")
    report.add_argument("--logs", type=Path, default=DEFAULT_ROOT, help="Claude Code projects dir")
    report.add_argument("--fetch", action="store_true", help="git fetch each repo first (uses the network)")
    replay_cli.add_parser(sub)
    args = parser.parse_args(argv)
    if args.command == "replay":
        return replay_cli.main(args)

    if args.days <= 0:
        parser.error("--days must be positive")
    if not args.logs.is_dir():
        print(f"No Claude Code logs found at {args.logs}. Nothing to report.")
        return 0

    now = time.time()
    since = now - args.days * DAY
    parsed = parse_logs(iter_log_files(args.logs))
    attribution = attribute(parsed, since, now, refresh=args.fetch)
    sys.stdout.write(render(parsed, attribution, since, now))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
