"""`aimpg study select | run | report PLAN.json`: a pre-registered multi-repo study.

The plan file is the pre-registration: commit it before any paid run, and
nothing in it changes afterwards. Example:

    {
      "name": "report-1",
      "cutoff": "2026-07-01",
      "commits_per_repo": 7,
      "repeats": 3,
      "task_mode": "hint",
      "baseline": "plain",
      "challengers": ["haiku", "opus", "codex:gpt-6.1-sol", "rtk"],
      "chains_per_repo": 2,
      "per_run_budget_usd": 2.0,
      "cap_usd": 250,
      "repos": [{"url": "https://github.com/pallets/click", "path": "~/study/click"}, ...]
    }

Optional, added for Report No. 2:

      "challengers": [..., "planner:opus+haiku"]   an Opus orchestrator that plans and checks, with a Haiku
                                                   worker sub-agent that implements; all tokens are priced
      "multi_file_per_repo": 3    at least this many of each repo's commits change two or more non-test
                                  files (as far as the repo has them); the rest are the earliest others
      "ask_status": true          every task ends by asking for a `STATUS: DONE` / `STATUS: NOT DONE`
                                  line, and each run records what the agent claimed

The report then also shows results by files changed (one, or more than one), an exploratory
table by how hard each task turned out, how often "done" was true, and what a planner really did.

select (free)  the first N qualifying commits per repo after the cutoff, by any
               author, with each repo's full candidate list saved
run (paid)     every setup on every selected commit × repeats, plus the
               fresh-session pairs; --pilot does 1 repo × 2 commits × 1 repeat
report (free)  study.py statistics: Holm correction and the 5-of-6 repo rule
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from aimpg.gitkept import GitError
from aimpg.replay import chain, select, study
from aimpg.replay import run as runner
from aimpg.replay.cli import _select, _state, secure_root
from aimpg.replay.verify import setup_from
from aimpg.replay.workspace import Layout

STATE = Path.home() / ".aimpg" / "study"


def add_parser(sub) -> None:
    p = sub.add_parser("study", help="a pre-registered study across several repos (select free, run paid, report free)")
    ssub = p.add_subparsers(dest="study_command", required=True)
    for name, help_ in (("select", "pick each repo's commits (free)"), ("run", "run everything (paid; asks first)"),
                        ("report", "statistics for the results (free)")):
        c = ssub.add_parser(name, help=help_)
        c.add_argument("plan", type=Path)
        if name == "run":
            c.add_argument("--pilot", action="store_true", help="1 repo × 2 commits × 1 repeat, to measure cost and noise")
            c.add_argument("--yes", action="store_true")
        if name == "report":
            c.add_argument("--runs", type=Path, metavar="DIR",
                           help="read results from DIR (e.g. a downloaded data folder) instead of this machine's study state; prints only")


def load_plan(path: Path) -> dict:
    plan = json.loads(path.read_text())
    for key in ("name", "cutoff", "commits_per_repo", "repeats", "baseline", "challengers", "repos"):
        if key not in plan:
            raise SystemExit(f"plan is missing {key!r}")
    for r in plan["repos"]:
        r["path"] = str(Path(r["path"]).expanduser().resolve())
    return plan


def _out(plan: dict) -> Path:
    d = STATE / plan["name"]
    d.mkdir(parents=True, exist_ok=True)
    return d


def main(args) -> int:
    plan = load_plan(args.plan)
    if args.study_command in ("select", "run"):
        from aimpg.replay import sandbox

        reason = sandbox.unavailable_reason()
        if reason:  # before any work or question: every step runs sandboxed
            print(f"Can't sandbox the study on this machine: {reason}")
            return 1
    if args.study_command == "select":
        return _select_all(plan)
    if args.study_command == "report":
        if args.runs:
            if not args.runs.is_dir():
                raise SystemExit(f"No such folder: {args.runs}")
            return _report(plan, folder=args.runs.resolve(), save=False)
        return _report(plan)
    return _run(plan, args)


MIN_QUALIFYING = 5  # pre-registered: fewer qualifying commits than this and the repo is replaced by the next reserve


def _qualified(repo: str) -> int:
    path = _state(Path(repo)) / "selected.jsonl"
    return len(select.load(path)) if path.exists() and path.read_text().strip() else 0


def _select_all(plan: dict) -> int:
    """Select each repo; apply the pre-registered replacement rule; write selection.json for `run`."""
    reserves = [dict(r, path=str(Path(r["path"]).expanduser().resolve())) for r in plan.get("reserve_repos", [])]
    summary = {"repos": [], "replaced": []}
    queue = list(plan["repos"])
    while queue:
        r = queue.pop(0)
        print(f"\n== {r['path']}")
        if not Path(r["path"]).is_dir():
            print(f"  not cloned: git clone --filter=blob:none --single-branch {r.get('url')} {r['path']}")
            n = 0
        elif (_state(Path(r["path"])) / "selected.manifest.json").exists():
            n = _qualified(r["path"])  # finished in an earlier run (the manifest is written last)
            print(f"  already selected: {n} qualifying (delete {_state(Path(r['path']))} to redo)")
        else:
            for attempt in range(3):  # blobless clones fetch file contents on demand; GitHub sometimes drops one
                try:
                    _select(Path(r["path"]), argparse.Namespace(days=365, cutoff=plan["cutoff"],
                                                                max=plan["commits_per_repo"] * 2, any_author=True,
                                                                multi_min=plan.get("multi_file_per_repo", 0)))
                    break
                except GitError as exc:
                    print(f"  git failed ({str(exc)[:120]}); retrying in 30 s" if attempt < 2 else f"  git failed 3 times: {exc}")
                    time.sleep(30)
            n = _qualified(r["path"])
        if n >= MIN_QUALIFYING:
            summary["repos"].append({"path": r["path"], "url": r.get("url"), "qualifying": n})
        elif reserves:
            nxt = reserves.pop(0)
            summary["replaced"].append({"repo": r.get("url") or r["path"], "qualifying": n, "replaced_by": nxt.get("url")})
            print(f"  {n} qualifying (< {MIN_QUALIFYING}): replaced by the next reserve, {nxt.get('url')}")
            queue.insert(0, nxt)
        else:
            summary["replaced"].append({"repo": r.get("url") or r["path"], "qualifying": n, "replaced_by": None})
            print(f"  {n} qualifying (< {MIN_QUALIFYING}) and no reserves left: dropped")
    (_out(plan) / "selection.json").write_text(json.dumps(summary, indent=1))
    print(f"\nIn the study: {len(summary['repos'])} repos. Replaced: {len(summary['replaced'])}. "
          f"Summary: {_out(plan) / 'selection.json'}")
    return 0


def _selected(plan: dict, repo: str, n: int):
    path = _state(Path(repo)) / "selected.jsonl"
    if not path.exists():
        raise SystemExit(f"run `aimpg study select` first ({repo} has no selection)")
    return select.pick(select.load(path), n, plan.get("multi_file_per_repo", 0))


def _setups(plan: dict):
    base = setup_from(plan["baseline"])
    return [base, *[setup_from(c) for c in plan["challengers"]]]


def _run(plan: dict, args) -> int:
    setups = _setups(plan)
    chosen = _out(plan) / "selection.json"
    if not chosen.exists():
        raise SystemExit("run `aimpg study select` first")
    active = json.loads(chosen.read_text())["repos"]
    repos = active[:1] if args.pilot else active
    n, repeats = (2, 1) if args.pilot else (plan["commits_per_repo"], plan["repeats"])
    chains = 1 if args.pilot else plan.get("chains_per_repo", 0)
    keys = {}
    for s in setups + [chain.FIRST]:
        if s.key_env and s.key_env not in keys:
            value = os.environ.get(s.key_env, "")
            if not value:
                raise SystemExit(f"set {s.key_env} in your shell first (aimpg never stores it)")
            keys[s.key_env] = value
    selected = {r["path"]: _selected(plan, r["path"], n) for r in repos}
    n_runs = sum(len(c) for c in selected.values()) * repeats * len(setups) + len(repos) * chains * repeats * 3
    cap = (min(plan["cap_usd"], 15.0) if args.pilot else plan["cap_usd"])
    print(f"Study {plan['name']}{' (PILOT)' if args.pilot else ''}: {len(repos)} repos, {n_runs} agent runs, "
          f"setups {', '.join(s.name for s in setups)}, {chains} fresh-session pairs per repo, task mode {plan.get('task_mode', 'hint')}")
    out_dir = _out(plan) / ("pilot" if args.pilot else "runs")
    done_runs = sum(len(runner.completed(out_dir / f"{Path(r['path']).name}.jsonl")) for r in repos) if out_dir.exists() else 0
    if done_runs:
        print(f"{done_runs} of those runs are already done (kept); only the rest will run.")
    print(f"Total cap ${cap:.2f}, counted at the published API prices (each run reserves "
          f"${plan.get('per_run_budget_usd', 2.0):.2f} until its real cost is known).")
    if not args.yes and input("Spend up to that on your API keys? [y/N] ").strip().lower() != "y":
        print("Stopped. Nothing was spent.")
        return 1
    out = _out(plan) / ("pilot" if args.pilot else "runs")
    out.mkdir(exist_ok=True)
    cfg = runner.Config(model="claude-sonnet-5-5", per_run_budget_usd=plan.get("per_run_budget_usd", 2.0), total_cap_usd=cap,
                        api_key=keys.get("ANTHROPIC_API_KEY", ""), task_mode=plan.get("task_mode", "hint"), keys=keys,
                        agent_budget_flag_usd=plan.get("agent_budget_flag_usd", 50.0), ask_status=bool(plan.get("ask_status")))
    layout, spent = Layout(secure_root()), 0.0
    for repo, commits in selected.items():
        results = out / f"{Path(repo).name}.jsonl"
        done = runner.completed(results)
        batch = runner.Batch(commits, setups, repeats, layout, cfg, results, done=done, spent=spent)
        batch.run(on_record=lambda r: print(f"  {Path(repo).name:<14} {r.setup:<22} {r.commit[:8]} r{r.repeat} {r.outcome:<13} "
                                            f"{'$%.2f' % r.cost_usd if r.cost_known else '$?':>6}"))
        spent = batch.spent
        if batch.stopped:
            print(f"STOPPED: an account can't make calls ({batch.stopped}). Fix it and run again; finished runs are kept.")
            return 1
        for c1, c2 in chain.pairs(commits)[:chains]:
            for k in range(repeats):
                if (c2.sha, "chain-fresh", k) in done:
                    continue
                if spent + 3 * cfg.per_run_budget_usd > cap:
                    print("Cap reached: stopping before more fresh-session pairs.")
                    break
                for rec in chain.run_chain(c1, c2, k, layout, cfg):
                    spent += rec.cost_usd
                    with open(results, "a") as fh:
                        fh.write(json.dumps(rec.__dict__) + "\n")
                    print(f"  {Path(repo).name:<14} {rec.setup:<22} {rec.commit[:8]} r{k} {rec.outcome:<13} ${rec.cost_usd:.2f}")
    print(f"\nSpent about ${spent:.2f}. Results in {out}")
    return _report(plan, folder=out)


def _report(plan: dict, folder: Path | None = None, *, save: bool = True) -> int:
    folder = folder or (_out(plan) / "runs")
    records = []
    for path in sorted(folder.glob("*.jsonl")):
        recs, _ = runner.load_results(path)
        records += recs
    if not records:
        print(f"No results in {folder} yet.")
        return 1
    base = setup_from(plan["baseline"]).name
    primary = plan.get("primary", {})
    comparisons = [study.compare(records, base, setup_from(c).name, primary=primary.get(c, "per_solved"))
                   for c in plan["challengers"]]
    if any(r.setup == "chain-fresh" for r in records):
        comparisons.append(study.compare(records, "chain-continue", "chain-fresh", primary=primary.get("fresh-session", "per_attempt")))
    study.holm(comparisons)
    text = study.render(comparisons)
    rtk = [r for r in records if r.setup == "claude-code+rtk"]
    if rtk:
        fired = sum(1 for r in rtk if r.extra.get("rtk_fired"))
        text += f"\n\nRTK fired in {fired} of {len(rtk)} RTK runs (RTK's own count); results above include all runs."
    singles = [base, *[setup_from(c).name for c in plan["challengers"]]]
    extras = study.render_extras(records, [s for s in singles if any(r.setup == s for r in records)])
    if extras:
        text += "\n\n" + extras
    print(text)
    if not save:
        return 0
    (folder.parent / f"{folder.name}-report.md").write_text(text + "\n")
    (folder.parent / f"{folder.name}-report.json").write_text(json.dumps(
        [{**{k: v for k, v in c.__dict__.items() if k != "arms"}, "arms": {s: a.__dict__ for s, a in c.arms.items()}}
         for c in comparisons], indent=1, default=str))
    print(f"\nSaved {folder.parent / (folder.name + '-report.md')}")
    return 0
