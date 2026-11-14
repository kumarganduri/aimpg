"""`aimpg replay select | calibrate | run | stats`.

    select     free: find commits whose tests can judge a replay (fail→pass check)
    calibrate  paid: 2 commits × setups × 2 repeats; measures cost + noise, proposes a size
    run        paid: the full comparison, under a total cap you confirm
    stats      free: verdicts from a results file
"""

from __future__ import annotations

import argparse
import hashlib
import json
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
from aimpg.replay import picker
from aimpg.replay import sandbox
from aimpg.replay.setups import SETUPS, with_model
from aimpg.replay.workspace import Layout

ROOT = sandbox.TMP / f"aimpg-replay-{os.getuid()}"  # outside $HOME; every agent profile denies it


def secure_root(root: Path | None = None) -> Path:
    """The replay folder: per user, private (0700), and refused if someone else owns it.

    A fixed shared /tmp path let another user on the machine create it first and
    swap prepared workspaces, or read transcripts (security review 2026-10-11).
    """
    root = root or ROOT
    root.mkdir(mode=0o700, exist_ok=True)
    st = root.lstat()
    if root.is_symlink() or st.st_uid != os.getuid():
        raise SystemExit(f"{root} isn't a private folder you own; remove it and try again.")
    if st.st_mode & 0o077:
        os.chmod(root, 0o700)
    return root
STATE = Path.home() / ".aimpg" / "replay"  # selections + results, unreadable to agents
DEFAULT_MODEL = "claude-sonnet-5-5"
SETUP_GIVE_UP = 5  # the first N candidates all fail to install: the repo can't be set up offline
DEFAULT_SETUPS = "claude-code,claude-code+terse,claude-code+rtk"


def add_parser(sub) -> None:
    p = sub.add_parser("replay", help="rerun past commits through different agent setups")
    rsub = p.add_subparsers(dest="replay_command", required=True)

    s = rsub.add_parser("select", help="find replayable commits (free, no AI calls)")
    s.add_argument("repo", type=Path)
    s.add_argument("--days", type=int, default=60)
    s.add_argument("--cutoff", default="2026-07-01", help="model training cutoff (YYYY-MM-DD); older commits are skipped")
    s.add_argument("--max", type=int, default=30, help="stop after this many qualifying commits")
    s.add_argument("--any-author", action="store_true", help="include everyone's commits (studies on public repos)")

    for name, help_ in (
        ("calibrate", "paid: measure cost and noise on 2 commits"),
        ("run", "paid: the full comparison"),
        ("models", "paid: model picker, the cheapest model that's good enough for your code"),
    ):
        c = rsub.add_parser(name, help=help_)
        c.add_argument("repos", type=Path, nargs="+", help="one or more repos already run through `select`")
        c.add_argument("--model", default=DEFAULT_MODEL)
        c.add_argument("--setups", default=DEFAULT_SETUPS)
        c.add_argument("--per-run-budget", type=float, default=2.0, help="USD cap per agent run (default 2)")
        c.add_argument("--yes", action="store_true", help="skip the cost confirmation prompt")
        c.add_argument("--task-mode", choices=("hint", "tests"), default="hint",
                       help="hint: commit message + new names, tests hidden (default); tests: commit's tests shown")
        if name == "models":
            c.add_argument("--models", default="haiku,sonnet,opus", help="comma list: haiku, sonnet, opus or full model ids")
        if name in ("run", "models"):
            c.add_argument("--commits", type=int, default=10)
            c.add_argument("--repeats", type=int, default=2 if name == "run" else 1)
            c.add_argument("--cap", type=float, required=True, help="total USD cap for the whole run")
            c.add_argument("--resume", type=Path, help="results file of an earlier run: only missing runs are done, appended there")

    t = rsub.add_parser("stats", help="verdicts from a results file (free)")
    t.add_argument("results", type=Path)
    t.add_argument("--baseline", default="claude-code")


def _state(repo: Path) -> Path:
    """This repo's selections and results: named by the folder plus a hash of its full path, so
    ~/work/api and ~/oss/api never share (or run) each other's commits."""
    repo = repo.resolve()
    d = STATE / f"{repo.name}-{hashlib.sha256(str(repo).encode()).hexdigest()[:10]}"
    legacy = STATE / repo.name  # before 0.7.1: the folder name only; kept while its commits are this repo's
    if not d.exists() and _selected_from(legacy, repo):
        return legacy
    d.mkdir(parents=True, exist_ok=True)
    return d


def _selected_from(d: Path, repo: Path) -> bool:
    path = d / "selected.jsonl"
    try:
        commits = select.load(path) if path.is_file() else []
    except (OSError, ValueError, TypeError):
        return False
    return bool(commits) and all(os.path.realpath(c.repo) == str(repo) for c in commits)


def main(args: argparse.Namespace) -> int:
    cmd = args.replay_command
    if cmd == "stats":
        return _stats(args.results, args.baseline)
    if cmd == "select":
        return 1 if _unsandboxable() else _select(args.repo.resolve(), args)
    return _paid([r.resolve() for r in args.repos], args, calibrate=(cmd == "calibrate"), models=(cmd == "models"))


def _unsandboxable() -> bool:
    reason = sandbox.unavailable_reason()
    if reason:
        print(f"Can't sandbox replays on this machine: {reason}")
    return reason is not None


def _select(repo: Path, args) -> int:
    cutoff = datetime.strptime(args.cutoff, "%Y-%m-%d").timestamp()
    any_author = getattr(args, "any_author", False)
    commits, skipped = select.candidates(str(repo), time.time() - args.days * DAY, cutoff, any_author=any_author)
    print(f"{len(commits)} candidates after cheap filters; skipped: {skipped or 'none'}")
    layout = Layout(secure_root())
    good, setup_failures = [], 0
    multi_min = getattr(args, "multi_min", 0)  # a study can ask for this many multi-file commits among them
    not_checked, last_seen = [], None
    with AllowlistProxy() as proxy:
        for c in commits:
            enough_multi = sum(select.is_multi_file(g) for g in good) >= multi_min
            if len(good) >= args.max and enough_multi:
                break
            last_seen = c
            if len(good) >= args.max and not select.is_multi_file(c):
                not_checked.append(c)  # only multi-file commits are still wanted; listed in the manifest
                continue
            check = select.precheck(c, layout, proxy)
            mark = "✓" if check.ok else "✗"
            print(f"  {mark} {c.sha[:8]} {c.subject[:60]:<60} {'' if check.ok else check.reason}")
            if check.ok:
                good.append(c)
            elif check.reason.startswith("setup failed") and not good:
                setup_failures += 1
                if setup_failures >= SETUP_GIVE_UP:
                    print(f"  The first {SETUP_GIVE_UP} commits all fail to install here: giving up on this repo.")
                    break
    out = _state(repo) / "selected.jsonl"
    select.save(good, out)
    if multi_min:  # every candidate looked at, including the ones after the last qualifying commit
        checked = commits[: commits.index(last_seen) + 1] if last_seen is not None else commits
    else:
        checked = commits[: commits.index(good[-1]) + 1] if good else commits
    (out.with_name("selected.manifest.json")).write_text(json.dumps(
        select.manifest(args.cutoff, any_author, checked, good, not_checked), indent=1))
    print(f"\n{len(good)} replayable commits saved to {out}")
    return 0 if good else 1


def _paid(repos: list[Path], args, *, calibrate: bool, models: bool = False) -> int:
    for repo in repos:
        if not (_state(repo) / "selected.jsonl").exists():
            print(f"Run `aimpg replay select {repo}` first.")
            return 1
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("Set ANTHROPIC_API_KEY in your shell first (Anthropic Console → API keys). aimpg never stores it.")
        return 1
    commits = [c for repo in repos for c in select.load(_state(repo) / "selected.jsonl")]
    if models:
        setups = [with_model(m.strip()) for m in args.models.split(",")]
    else:
        setups = [SETUPS[name.strip()] for name in args.setups.split(",")]
    n_commits, repeats = (2, 2) if calibrate else (args.commits, args.repeats)
    if len(commits) < n_commits:
        print(f"Only {len(commits)} replayable commits selected; need {n_commits}.")
        return 1
    if _unsandboxable():  # before the cost question, not after a yes
        return 1
    chosen = random.Random(7).sample(commits, n_commits)
    done = runner.completed(args.resume) if getattr(args, "resume", None) else set()
    todo = [(c.sha, s.name, r) for c in chosen for r in range(repeats) for s in setups]
    runs = sum(1 for job in todo if job not in done)
    if done:
        print(f"Resuming {args.resume.name}: {len(todo) - runs} runs already done, {runs} to go.")
    cap = runs * args.per_run_budget if calibrate else args.cap
    print(f"Plan: {n_commits} commits × {len(setups)} setups × {repeats} repeats = {len(todo)} agent runs on {args.model} (task mode: {args.task_mode})")
    print(f"Per-run cap ${args.per_run_budget:.2f}; total cap ${cap:.2f} (worst case; usually far less).")
    if not args.yes and input("Spend up to that on your API key? [y/N] ").strip().lower() != "y":
        print("Stopped. Nothing was spent.")
        return 1
    out_dir = _state(repos[0]) if len(repos) == 1 else _state(Path("+".join(r.name for r in repos)))
    kind = "calibration" if calibrate else "models" if models else "run"
    results = args.resume if getattr(args, "resume", None) else out_dir / f"{kind}-{args.task_mode}-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    cfg = runner.Config(model=args.model, per_run_budget_usd=args.per_run_budget, total_cap_usd=cap, api_key=api_key, task_mode=args.task_mode)
    batch = runner.Batch(chosen, setups, repeats, Layout(secure_root()), cfg, results, done=done)

    def show(rec: runner.Record) -> None:
        print(f"  {rec.setup:<20} {rec.commit[:8]} r{rec.repeat}  {rec.outcome:<13} ${rec.cost_usd:.2f}  {rec.wall_s:.0f}s  tokens:{rec.tokens_check}")

    batch.run(on_record=show)
    print(f"\nSpent ${batch.spent:.2f}. Results: {results}")
    if batch.stopped:
        print(f"STOPPED EARLY: your Anthropic account can't make calls ({batch.stopped}). "
              "Runs already finished are kept; top up or fix the key, then run again to fill in the rest.")
    return _stats(results, setups[0].name, calibration=calibrate, per_run_budget=args.per_run_budget)


def _stats(path: Path, baseline: str, *, calibration: bool = False, per_run_budget: float = 2.0) -> int:
    records, excluded = runner.load_results(path)
    if records and all(r.setup.startswith("claude-code@") for r in records):
        print(picker.render(records))
        return 0
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
