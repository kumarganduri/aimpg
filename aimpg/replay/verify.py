"""`aimpg verify`: check an efficiency claim on your own commits, and write a
record anyone can check.

    aimpg verify --repo . --challenger rtk --claim "rtk saves tokens"
    aimpg verify --check record.json          free: recompute the verdict from the record
    aimpg verify --rerun record.json --repo . paid: rerun a --public record's commits

The question is always the same: does the challenger solve as much, for less?

* Same agent and model (token-savers, prompts, hooks): the paired energy test
  from replay stats (shared passes, every energy corner) decides; $ per solved
  task and solve rates are shown next to it.
* Different model or agent: model sizes are secret, so energy can't be ranked;
  $ per solved task (when priced) and solve rate decide, like the model picker.
* A challenger that solves more than 10 points fewer tasks is never "supported".

The record never holds code, diffs, commit messages or run output. Repo and
commit ids are salted hashes unless --public (then remote URL + shas, so
others can --rerun it).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import time
from dataclasses import asdict
from importlib import metadata
from pathlib import Path

from aimpg.cost import load_prices
from aimpg.energy import ZERO, WhRange, load_factors, request_wh
from aimpg.model import Request, Usage
from aimpg.replay import setups as S
from aimpg.replay import stats
from aimpg.replay.run import Record

RECORD_VERSION = 1
SOLVE_MARGIN = 0.10  # solving this many points fewer tasks is never "supported"
COST_MARGIN = 0.10  # a $ difference smaller than this is "no real difference"
MIN_RUNS = 20  # fewer runs per setup: verdicts are tentative


class VerifyError(ValueError):
    pass


# ---------------------------------------------------------------- challengers

def setup_from(name: str, *, hosts: list[str] | None = None, key_env: str = "") -> S.Setup:
    """`rtk`, `terse`, `plain`, `haiku`/`sonnet`/`opus`/claude-*, `codex[:model]`, `cmd:<command with {task}>`."""
    name = name.strip()
    if name in ("plain", "claude-code"):
        return S.BASELINE
    if name in ("rtk", "terse"):
        return S.SETUPS[f"claude-code+{name}"]
    if name in S.MODELS or name.startswith("claude-"):
        return S.with_model(name)
    if name == "codex" or name.startswith("codex:"):
        return S.codex(name.partition(":")[2] or None)
    if name.startswith("cmd:"):
        if not hosts:
            raise VerifyError("a cmd: challenger needs --hosts (the API hosts it may reach, e.g. api.openai.com)")
        return S.command(name[4:], hosts, key_env)
    raise VerifyError(f"unknown challenger {name!r}: use rtk, terse, haiku/sonnet/opus, a claude-* model id, codex[:model] or cmd:'...'")


# ---------------------------------------------------------------- verdict

def _summary(records: list[Record]) -> dict:
    runs = len(records)
    solved = sum(r.passed for r in records)
    priced = all(r.cost_known for r in records) and runs > 0
    usd = sum(r.cost_usd for r in records)
    measurable = any(r.usages for r in records)
    wh = ZERO
    for r in records:
        for u in r.usages:
            wh = wh + request_wh(Request("x", "s", r.model, 0.0, Usage(*u), "/"))
    return {
        "runs": runs,
        "solved": solved,
        "solve_rate": round(solved / runs, 4) if runs else None,
        "usd": round(usd, 4) if priced else None,
        "usd_per_solved": round(usd / solved, 4) if priced and solved else None,
        "wh_per_solved": [round(wh.low / solved, 3), round(wh.high / solved, 3)] if measurable and solved else None,
        "models": sorted({r.model for r in records if r.model}),
        "agent": records[0].agent if records else None,
    }


def verdict(records: list[Record], baseline: str, challenger: str) -> dict:
    base = _summary([r for r in records if r.setup == baseline])
    chal = _summary([r for r in records if r.setup == challenger])
    out = {"baseline": base, "challenger": chal, "energy": None, "cost_ratio": None, "answer": "NOT PROVEN", "reasons": []}
    reasons = out["reasons"]
    if not base["runs"] or not chal["runs"]:
        reasons.append("no finished runs for one of the setups")
        return out
    same_model = base["agent"] == chal["agent"] == "claude" and base["models"] == chal["models"]
    if same_model:
        runs = [stats.Run(r.commit, r.setup, r.repeat, r.passed, [Usage(*u) for u in r.usages], r.model) for r in records]
        v = stats.compare(runs, baseline, challenger)
        out["energy"] = {"verdict": v.verdict, "effect_mid": v.effect_mid, "interval": list(v.ci_worst) if v.ci_worst else None,
                         "shared_commits": v.shared_commits}
    if base["usd_per_solved"] and chal["usd_per_solved"]:
        out["cost_ratio"] = round(chal["usd_per_solved"] / base["usd_per_solved"], 4)

    solves_fewer = base["solve_rate"] - chal["solve_rate"] > SOLVE_MARGIN
    if not chal["solved"]:
        out["answer"] = "NOT SUPPORTED"
        reasons.append("the challenger solved nothing")
        return out
    if solves_fewer:
        out["answer"] = "NOT SUPPORTED"
        reasons.append(f"solves fewer tasks ({chal['solve_rate']:.0%} vs {base['solve_rate']:.0%})")
    if same_model:
        e = out["energy"]["verdict"]
        if e == "uses less" and not solves_fewer:
            out["answer"] = "SUPPORTED"
            reasons.append("uses less energy on the same tasks, at every corner of the energy ranges")
        elif e == "uses more":
            out["answer"] = "NOT SUPPORTED"
            reasons.append("uses more energy on the same tasks")
        elif not solves_fewer:
            reasons.append(f"energy difference {e}")
    else:
        ratio = out["cost_ratio"]
        if ratio is None:
            reasons.append("cost per solved task isn't measurable for both (unpriced model or no token source)")
        elif ratio <= 1 - COST_MARGIN and not solves_fewer:
            out["answer"] = "SUPPORTED"
            reasons.append(f"{1 - ratio:.0%} lower cost per solved task")
        elif ratio >= 1 + COST_MARGIN:
            out["answer"] = "NOT SUPPORTED"
            reasons.append(f"{ratio - 1:.0%} higher cost per solved task")
        elif not solves_fewer:
            reasons.append("cost per solved task within 10%")
        reasons.append("energy not ranked: different models' sizes are secret")
    if min(base["runs"], chal["runs"]) < MIN_RUNS:
        out["tentative"] = True
    return out


def render(v: dict, claim: str, baseline: str, challenger: str) -> str:
    def line(name: str, s: dict) -> str:
        usd = f"${s['usd_per_solved']:.2f}" if s["usd_per_solved"] is not None else "not measurable"
        wh = f"{s['wh_per_solved'][0]:.1f}–{s['wh_per_solved'][1]:.1f} Wh" if s["wh_per_solved"] else "not measurable"
        rate = f"{s['solved']}/{s['runs']}"
        return f"  {name:<24}{rate:>8}   {usd:>15}   {wh}"

    out = [f"CLAIM: {claim or challenger + ' is more efficient than ' + baseline}",
           f"VERDICT: {v['answer']}" + (" (tentative: fewer than 20 runs per setup)" if v.get("tentative") else ""),
           *[f"  · {r}" for r in v["reasons"]], "",
           f"  {'setup':<24}{'solved':>8}   {'$ per solved':>15}   energy per solved task",
           line(baseline, v["baseline"]), line(challenger, v["challenger"])]
    e = v.get("energy")
    if e and e["effect_mid"] is not None:
        lo, hi = e["interval"]
        out.append(f"\n  Energy, same tasks: {e['effect_mid']:+.0%} (interval {lo:+.0%}…{hi:+.0%}, {e['shared_commits']} shared solved commits)")
    elif e:
        out.append(f"\n  Energy, same tasks: {e['verdict']} ({e['shared_commits']} shared solved commits; 6 needed)")
    return "\n".join(out)


# ---------------------------------------------------------------- record

def _versions() -> dict:
    out = {"aimpg": _pkg_version(), "factors": load_factors().get("version"), "prices": load_prices().get("version")}
    for tool in ("claude", "codex"):
        try:
            out[tool] = subprocess.run([tool, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()[:80] or None
        except (OSError, subprocess.TimeoutExpired):
            pass
    return out


def _pkg_version() -> str:
    try:
        return metadata.version("aimpg")
    except metadata.PackageNotFoundError:
        return "dev"


def _remote(repo: str) -> str | None:
    proc = subprocess.run(["git", "-C", repo, "remote", "get-url", "origin"], capture_output=True, text=True)
    url = proc.stdout.strip()
    if "@" in url.split("//")[-1].split("/")[0]:  # strip credentials from https://user:token@host
        scheme, _, rest = url.partition("//")
        url = scheme + "//" + rest.split("@", 1)[1]
    return url or None


def _spec(setup: S.Setup, public: bool) -> dict:
    spec = {"name": setup.name, **setup.spec}
    if not public:
        spec.pop("command", None)
        spec.pop("settings", None)
    return spec


def build_record(records: list[Record], *, claim: str, baseline: S.Setup, challenger: S.Setup, repo: str,
                 task_mode: str, public: bool, extra: dict | None = None) -> dict:
    salt = secrets.token_bytes(16)  # never stored: hashes are consistent within a record only

    def hide(text: str) -> str:
        return text if public else hashlib.sha256(salt + text.encode()).hexdigest()[:16]

    runs = []
    for r in records:
        d = asdict(r)
        for k in ("run_id", "note", "repo"):  # notes can hold test output; the repo path is private
            d.pop(k, None)
        d["commit"] = hide(r.commit)
        runs.append(d)
    return {
        "aimpg_record": RECORD_VERSION,
        "kind": "verify",
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "claim": claim,
        "public": public,
        "repo": _remote(repo) if public else hide(os.path.realpath(repo)),
        "task_mode": task_mode,
        "versions": _versions(),
        "baseline": _spec(baseline, public),
        "challenger": _spec(challenger, public),
        "runs": runs,
        "verdict": verdict(records, baseline.name, challenger.name),
        **(extra or {}),
    }


def records_from(record: dict) -> list[Record]:
    return [Record(run_id="", repo="", note="", **r) for r in record["runs"]]


def check(record: dict) -> tuple[bool, dict]:
    """Recompute the verdict from the record's own runs. (matches, recomputed)."""
    if record.get("aimpg_record") != RECORD_VERSION or record.get("kind") != "verify":
        raise VerifyError("not an aimpg verify record (or a newer version: upgrade aimpg)")
    again = verdict(records_from(record), record["baseline"]["name"], record["challenger"]["name"])
    return _same(again, record["verdict"]), again


def _same(a, b) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def setup_from_spec(spec: dict) -> S.Setup:
    """Rebuild a setup from a record (for --rerun). Needs a --public record for settings or commands."""
    agent, name = spec.get("agent"), spec["name"]
    if agent == "codex":
        return S.codex(spec.get("model"))
    if agent == "cmd":
        if not spec.get("command"):
            raise VerifyError(f"{name}: the record keeps only a hash of the command (not --public)")
        return S.command(spec["command"], spec.get("hosts", []), spec.get("key_env", ""), name=name)
    if spec.get("rtk"):
        return S.RTK
    if spec.get("settings_sha256") and not spec.get("settings"):
        raise VerifyError(f"{name}: the record keeps only a hash of settings.json (not --public)")
    if not any(spec.get(k) for k in ("append_prompt", "claude_md", "settings")):
        if not spec.get("model"):
            return S.BASELINE
        setup = S.with_model(spec["model"])
        setup.name = name
        return setup
    return S.custom(name, model=spec.get("model"), append_prompt=spec.get("append_prompt"),
                    claude_md=spec.get("claude_md"), settings=spec.get("settings"))


# ---------------------------------------------------------------- command line

STATE = Path.home() / ".aimpg" / "verify"


def add_parser(sub) -> None:
    v = sub.add_parser("verify", help="check an efficiency claim on your own commits (paid runs; asks first)")
    v.add_argument("--repo", type=Path, default=Path("."), help="repo whose commits judge the claim (default: here)")
    v.add_argument("--challenger", help="rtk, terse, haiku/sonnet/opus, a claude-* model, codex[:model], or cmd:'tool ... {task}'")
    v.add_argument("--baseline", default="plain", help="what it's compared with (default: plain Claude Code)")
    v.add_argument("--claim", default="", help="the claim in words, e.g. \"rtk saves tokens\"")
    g = v.add_argument_group("your own Claude Code change (makes a 'custom' challenger)")
    g.add_argument("--append-prompt", help="extra system prompt text")
    g.add_argument("--claude-md", type=Path, help="a CLAUDE.md file to give the agent")
    g.add_argument("--settings", type=Path, help="a settings.json (hooks etc.) to give the agent")
    g.add_argument("--model", help="model for the custom challenger (haiku/sonnet/opus or an id)")
    c = v.add_argument_group("any agent command (cmd:)")
    c.add_argument("--hosts", default="", help="comma list of API hosts it may reach, e.g. api.openai.com")
    c.add_argument("--key-env", default="", help="env var holding its API key (passed through from your shell)")
    v.add_argument("--commits", type=int, default=10)
    v.add_argument("--repeats", type=int, default=2)
    v.add_argument("--cap", type=float, default=15.0, help="total USD cap (default 15)")
    v.add_argument("--per-run-budget", type=float, default=2.0, help="USD cap per Claude Code run (default 2)")
    v.add_argument("--batch-model", default="claude-sonnet-5-5", help="model for setups that don't name one")
    v.add_argument("--task-mode", choices=("hint", "tests"), default="tests",
                   help="tests: the commit's tests are shown (default, fairest for comparisons); hint: hidden")
    v.add_argument("--public", action="store_true", help="put the repo URL and commit shas in the record so others can rerun it")
    v.add_argument("--yes", action="store_true", help="skip the cost confirmation")
    v.add_argument("--resume", type=Path, help="results file of an interrupted verify")
    v.add_argument("--check", type=Path, metavar="RECORD", help="free: recompute a record's verdict")
    v.add_argument("--rerun", type=Path, metavar="RECORD", help="paid: rerun a --public record's commits and setups")


def main(args) -> int:
    try:
        if args.check:
            return _check(args.check)
        return _run(args)
    except VerifyError as exc:
        print(f"verify: {exc}")
        return 1


def _check(path: Path) -> int:
    record = json.loads(path.read_text())
    ok, again = check(record)
    print(render(again, record.get("claim", ""), record["baseline"]["name"], record["challenger"]["name"]))
    print("\nRecomputed from the record's runs: " + ("MATCHES the recorded verdict." if ok else "DOES NOT MATCH the recorded verdict."))
    return 0 if ok else 2


def _challenger(args) -> S.Setup:
    hosts = [h.strip() for h in args.hosts.split(",") if h.strip()]
    if any((args.append_prompt, args.claude_md, args.settings)) or (args.model and not args.challenger):
        if args.challenger:
            raise VerifyError("use either --challenger or the custom flags (--append-prompt/--claude-md/--settings/--model)")
        try:
            settings = args.settings.read_text() if args.settings else None
            return S.custom(model=args.model, append_prompt=args.append_prompt,
                            claude_md=args.claude_md.read_text() if args.claude_md else None, settings=settings)
        except (OSError, ValueError) as exc:
            raise VerifyError(f"can't use that file: {exc}") from None
    if not args.challenger:
        raise VerifyError("say what to test: --challenger NAME, or --append-prompt/--claude-md/--settings/--model")
    return setup_from(args.challenger, hosts=hosts, key_env=args.key_env)


def _run(args) -> int:
    from aimpg.replay import cli as replay_cli
    from aimpg.replay import run as runner
    from aimpg.replay import select
    from aimpg.replay.proxy import AllowlistProxy
    from aimpg.replay.workspace import Layout, WorkspaceError

    repo = args.repo.expanduser().resolve()
    if not (repo / ".git").exists():
        raise VerifyError(f"{repo} is not a git repo (or doesn't exist). Pass --repo with your project's folder.")
    prior = None
    if args.rerun:
        prior = json.loads(args.rerun.read_text())
        if not prior.get("public"):
            raise VerifyError("only --public records can be rerun (private ones hide which commits were used)")
        baseline, challenger = setup_from_spec(prior["baseline"]), setup_from_spec(prior["challenger"])
        shas = sorted({r["commit"] for r in prior["runs"]})
        repeats = max(r["repeat"] for r in prior["runs"]) + 1
        task_mode, claim = prior["task_mode"], prior.get("claim", "")
        layout = Layout(replay_cli.ROOT)
        chosen = []
        with AllowlistProxy() as proxy:
            for sha in shas:
                try:
                    commit = select.commit_at(str(repo), sha)
                except WorkspaceError as exc:
                    raise VerifyError(f"{sha[:10]}: {exc}") from None
                except Exception:
                    raise VerifyError(f"commit {sha[:10]} isn't in {repo}: clone {prior.get('repo')} and pass --repo") from None
                if not select.precheck(commit, layout, proxy).ok:
                    raise VerifyError(f"commit {sha[:10]} doesn't fail→pass on this machine; can't rerun it fairly")
                chosen.append(commit)
    else:
        baseline, challenger = setup_from(args.baseline), _challenger(args)
        if baseline.name == challenger.name:
            raise VerifyError("the challenger is the same as the baseline")
        task_mode, claim, repeats = args.task_mode, args.claim, args.repeats
        selected = replay_cli._state(repo) / "selected.jsonl"
        if not selected.exists():
            print("Finding commits whose tests can judge a rerun (free, no AI calls)…")
            import argparse

            replay_cli._select(repo, argparse.Namespace(days=60, cutoff="2026-07-01", max=30))
        commits = select.load(selected) if selected.exists() else []
        if len(commits) < args.commits:
            raise VerifyError(f"only {len(commits)} replayable commits in {repo}; need {args.commits} (try --commits {len(commits)})"
                              if commits else f"no replayable commits in {repo} (see `aimpg replay select`)")
        import random

        chosen = random.Random(7).sample(commits, args.commits)

    setups = [baseline, challenger]
    keys = {}
    for s in setups:
        if s.key_env:
            value = os.environ.get(s.key_env, "")
            if not value:
                raise VerifyError(f"set {s.key_env} in your shell first ({s.name} needs it). aimpg never stores it.")
            keys[s.key_env] = value

    n = len(chosen) * repeats * len(setups)
    print(f"Claim: {claim or challenger.name + ' is more efficient than ' + baseline.name}")
    print(f"Plan: {len(chosen)} commits × {repeats} repeats × 2 setups ({baseline.name} vs {challenger.name}) = {n} agent runs, task mode {task_mode}")
    print(f"Total cap ${args.cap:.2f}; Claude Code runs stop at ${args.per_run_budget:.2f} each.")
    unbounded = [s.name for s in setups if s.agent != "claude"]
    if unbounded:
        print(f"Note: {', '.join(unbounded)} can't be capped per run by aimpg (only the 30-minute timeout); "
              "their spend counts toward the cap only when its model is priced. Set a spend limit with that provider.")
    if not args.yes and input("Spend up to that on your API key(s)? [y/N] ").strip().lower() != "y":
        print("Stopped. Nothing was spent.")
        return 1

    STATE.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    results = args.resume or STATE / f"{challenger.name.replace('/', '_')}-{stamp}.jsonl"
    done = runner.completed(results) if args.resume else set()
    cfg = runner.Config(model=args.batch_model, per_run_budget_usd=args.per_run_budget, total_cap_usd=args.cap,
                        api_key=keys.get("ANTHROPIC_API_KEY", ""), task_mode=task_mode, keys=keys)
    batch = runner.Batch(chosen, setups, repeats, Layout(replay_cli.ROOT), cfg, results, done=done)
    batch.run(on_record=lambda r: print(f"  {r.setup:<22} {r.commit[:8]} r{r.repeat}  {r.outcome:<13} "
                                        f"{'$%.2f' % r.cost_usd if r.cost_known else '$?':>6}  {r.wall_s:.0f}s"))
    if batch.stopped:
        print(f"STOPPED EARLY: an account can't make calls ({batch.stopped}). Fix it, then rerun with --resume {results}")
    records, _ = runner.load_results(results)
    record = build_record(records, claim=claim, baseline=baseline, challenger=challenger, repo=str(repo),
                          task_mode=task_mode, public=args.public if not prior else True)
    out = results.with_suffix(".record.json")
    out.write_text(json.dumps(record, indent=1))
    print()
    print(render(record["verdict"], claim, baseline.name, challenger.name))
    if prior:
        same = prior["verdict"]["answer"] == record["verdict"]["answer"]
        print(f"\nOriginal record said {prior['verdict']['answer']}; this rerun says {record['verdict']['answer']}"
              + (" (agrees)." if same else " (DISAGREES)."))
    print(f"\nRecord: {out}" + ("" if record["public"] else " (private: repo and commits are hashed; add --public to let others rerun it)"))
    print(f"Anyone can recheck the math for free:  aimpg verify --check {out}")
    return 0
