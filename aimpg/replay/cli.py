"""`aimpg replay select | calibrate | run | stats`.

    select     free: find commits whose tests can judge a replay (fail→pass check)
    calibrate  paid: 2 commits × setups × 2 repeats; measures cost + noise, proposes a size
    run        paid: the full comparison, under a total cap you confirm
    stats      free: verdicts from a results file
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

from aimpg.gitkept import DAY
from aimpg.replay import run as runner
from aimpg.replay import select, stats
from aimpg.replay.proxy import AllowlistProxy
from aimpg.replay.setups import SETUPS
from aimpg.replay.workspace import Layout

ROOT = Path("/private/tmp/aimpg-replay")  # outside $HOME; every agent profile denies it
STATE = Path.home() / ".aimpg" / "replay"  # selections + results, unreadable to agents
DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_SETUPS = "claude-code,claude-code+terse,claude-code+rtk"


def add_parser(sub) -> None:
    p = sub.add_parser("replay", help="rerun past commits through different agent setups")
    rsub = p.add_subparsers(dest="replay_command", required=True)

    s = rsub.add_parser("select", help="find replayable commits (free, no AI calls)")
    s.add_argument("repo", type=Path)
    s.add_argument("--days", type=int, default=60)
    s.add_argument("--cutoff", default="2026-07-01", help="model training cutoff (YYYY-MM-DD); older commits are skipped")
    s.add_argument("--max", type=int, default=30, help="stop after this many qualifying commits")

    for name, help_ in (("calibrate", "paid: measure cost and noise on 2 commits"), ("run", "paid: the full comparison")):
        c = rsub.add_parser(name, help=help_)
        c.add_argument("repos", type=Path, nargs="+", help="one or more repos already run through `select`")
        c.add_argument("--model", default=DEFAULT_MODEL)
        c.add_argument("--setups", default=DEFAULT_SETUPS)
        c.add_argument("--per-run-budget", type=float, default=2.0, help="USD cap per agent run (default 2)")
        c.add_argument("--yes", action="store_true", help="skip the cost confirmation prompt")
        if name == "run":
            c.add_argument("--commits", type=int, default=10)
            c.add_argument("--repeats", type=int, default=2)
            c.add_argument("--cap", type=float, required=True, help="total USD cap for the whole run")

    t = rsub.add_parser("stats", help="verdicts from a results file (free)")
    t.add_argument("results", type=Path)
    t.add_argument("--baseline", default="claude-code")


def _state(repo: Path) -> Path:
    d = STATE / repo.resolve().name
    d.mkdir(parents=True, exist_ok=True)
    return d


def main(args: argparse.Namespace) -> int:
    cmd = args.replay_command
    if cmd == "stats":
        return _stats(args.results, args.baseline)
    if cmd == "select":
        return _select(args.repo.resolve(), args)
    return _paid([r.resolve() for r in args.repos], args, calibrate=(cmd == "calibrate"))


def _select(repo: Path, args) -> int:
    cutoff = datetime.strptime(args.cutoff, "%Y-%m-%d").timestamp()
    commits, skipped = select.candidates(str(repo), time.time() - args.days * DAY, cutoff)
    print(f"{len(commits)} candidates after cheap filters; skipped: {skipped or 'none'}")
    layout = Layout(ROOT)
    good = []
    with AllowlistProxy() as proxy:
        for c in commits:
            if len(good) >= args.max:
                break
            check = select.precheck(c, layout, proxy)
            mark = "✓" if check.ok else "✗"
            print(f"  {mark} {c.sha[:8]} {c.subject[:60]:<60} {'' if check.ok else check.reason}")
            if check.ok:
                good.append(c)
    out = _state(repo) / "selected.jsonl"
    select.save(good, out)
    print(f"\n{len(good)} replayable commits saved to {out}")
    return 0 if good else 1


def _paid(repos: list[Path], args, *, calibrate: bool) -> int:
    for repo in repos:
        if not (_state(repo) / "selected.jsonl").exists():
            print(f"Run `aimpg replay select {repo}` first.")
            return 1
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("Set ANTHROPIC_API_KEY in your shell first (Anthropic Console → API keys). aimpg never stores it.")
        return 1
    commits = [c for repo in repos for c in select.load(_state(repo) / "selected.jsonl")]
    setups = [SETUPS[name.strip()] for name in args.setups.split(",")]
    n_commits, repeats = (2, 2) if calibrate else (args.commits, args.repeats)
    if len(commits) < n_commits:
        print(f"Only {len(commits)} replayable commits selected; need {n_commits}.")
        return 1
    chosen = random.Random(7).sample(commits, n_commits)
    runs = n_commits * len(setups) * repeats
    cap = runs * args.per_run_budget if calibrate else args.cap
    print(f"Plan: {n_commits} commits × {len(setups)} setups × {repeats} repeats = {runs} agent runs on {args.model}")
    print(f"Per-run cap ${args.per_run_budget:.2f}; total cap ${cap:.2f} (worst case; usually far less).")
    if not args.yes and input("Spend up to that on your API key? [y/N] ").strip().lower() != "y":
        print("Stopped. Nothing was spent.")
        return 1
    out_dir = _state(repos[0]) if len(repos) == 1 else _state(Path("+".join(r.name for r in repos)))
    results = out_dir / f"{'calibration' if calibrate else 'run'}-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    cfg = runner.Config(model=args.model, per_run_budget_usd=args.per_run_budget, total_cap_usd=cap, api_key=api_key)
    batch = runner.Batch(chosen, setups, repeats, Layout(ROOT), cfg, results)

    def show(rec: runner.Record) -> None:
        print(f"  {rec.setup:<20} {rec.commit[:8]} r{rec.repeat}  {rec.outcome:<13} ${rec.cost_usd:.2f}  {rec.wall_s:.0f}s  tokens:{rec.tokens_check}")

    batch.run(on_record=show)
    print(f"\nSpent ${batch.spent:.2f}. Results: {results}")
    return _stats(results, setups[0].name, calibration=calibrate, per_run_budget=args.per_run_budget)


def _stats(path: Path, baseline: str, *, calibration: bool = False, per_run_budget: float = 2.0) -> int:
    records, excluded = runner.load_results(path)
    runs = [stats.Run(r.commit, r.setup, r.repeat, r.passed, [_usage(u) for u in r.usages], r.model) for r in records]
    setups = sorted({r.setup for r in records} - {baseline})
    if excluded:
        print(f"Excluded commits (harness errors twice): {len(excluded)}")
    for ch in setups:
        if calibration:
            n = stats.commits_needed(runs, baseline, ch)
            mean_cost = sum(r.cost_usd for r in records) / max(len(records), 1)
            if n is None:
                print(f"{ch}: not enough passing pairs to estimate noise yet.")
            else:
                total = n * (len(setups) + 1) * 2 * mean_cost
                print(f"{ch}: ~{n} commits needed to detect a 10% energy difference (≈ ${total:.0f} at ${mean_cost:.2f}/run).")
            continue
        v = stats.compare(runs, baseline, ch)
        rates = ", ".join(f"{k} {v_:.0%}" for k, v_ in v.pass_rate.items())
        detail = "" if v.effect_mid is None else f" (mid {v.effect_mid:+.0%}, interval {v.ci_worst[0]:+.0%}…{v.ci_worst[1]:+.0%})"
        print(f"{ch} vs {baseline}: {v.verdict}{detail}; shared passing commits {v.shared_commits}; pass rates: {rates}")
    return 0


def _usage(u: list[int]):
    from aimpg.model import Usage

    return Usage(*u)
