"""Run replays: one run = clone → agent (Phase B) → integrity check → judge (Phase C).

Every run ends as exactly one outcome and is appended to results.jsonl at once,
so a crash loses nothing (Phase 2 review R10):

    passed · tests_failed · timeout · budget_hit · agent_error · harness_error

All but harness_error count as "not passed", and their tokens still count.
A harness_error (our bug, e.g. an empty transcript) is retried once; if it
happens again the commit is excluded for every setup, so no setup is judged
on a different set of commits.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tarfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

from aimpg.logs import iter_log_files, parse_logs
from aimpg.replay import sandbox, workspace
from aimpg.replay.proxy import ANTHROPIC, AllowlistProxy
from aimpg.replay.sandbox import Profile
from aimpg.replay.setups import Setup, parse_result
from aimpg.replay.workspace import Commit, Layout, WorkspaceError

AGENT_TIMEOUT = 30 * 60
TOKEN_TOLERANCE = 0.02
NOT_PASSED = ("tests_failed", "timeout", "budget_hit", "agent_error")
# Found in the first tests-mode run: after the prepaid credit ran out, 47 runs
# "failed" in 2s with "Credit balance is too low" and were counted against the agent.
_ACCOUNT_ERROR = re.compile(r"credit balance|invalid x-api-key|authentication_error|permission_error|billing", re.I)


@dataclass
class Config:
    model: str
    per_run_budget_usd: float
    total_cap_usd: float
    api_key: str = ""  # from the user's shell env only; never written anywhere
    parallel: int = 3
    timeout: float = AGENT_TIMEOUT
    task_mode: str = "hint"  # "hint": message + interface hint, tests hidden | "tests": tests shown


def task_text(commit: Commit, mode: str) -> str:
    """What the agent is told. Identical for every setup in a batch."""
    if mode != "tests":
        return commit.task
    message = (commit.subject + ("\n\n" + commit.body if commit.body.strip() else "")).strip()
    files = "\n".join(f"- {f}" for f in commit.test_files)
    return (
        f"{message}\n\nThese test files describe the change and are already in the repo:\n{files}\n"
        "Make them pass without editing them."
    )


@dataclass
class Record:
    run_id: str
    repo: str
    commit: str
    setup: str
    repeat: int
    outcome: str
    passed: bool
    model: str
    cost_usd: float
    wall_s: float
    usages: list[list[int]] = field(default_factory=list)  # [fresh_in, cache_write, cache_read, output] per request
    tokens_check: str = ""  # "ok" | "unverified" | reason for harness_error
    note: str = ""


class BudgetExhausted(RuntimeError):
    pass


def preflight_key(api_key: str) -> None:
    """Fail fast on a bad key instead of letting Claude Code retry for minutes.

    GET /v1/models costs no tokens.
    """
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/models",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status != 200:
                raise RuntimeError(f"API key check failed: HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"API key rejected by Anthropic (HTTP {exc.code}). Check ANTHROPIC_API_KEY.") from None


def _agent_env(work: Path, cfg: Path, proxy: AllowlistProxy, api_key: str) -> dict[str, str]:
    env = workspace._tool_env(work)
    env.update(
        HOME=str(cfg),
        CLAUDE_CONFIG_DIR=str(cfg),
        HTTPS_PROXY=proxy.url,
        https_proxy=proxy.url,
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
        DISABLE_AUTOUPDATER="1",
        UV_OFFLINE="1",
        npm_config_offline="true",
    )
    if api_key:
        env["ANTHROPIC_API_KEY"] = api_key
    return env


def _check_tokens(requests, result: dict) -> tuple[str, bool]:
    """(tokens_check, is_harness_error). Is the transcript this run's, and complete?

    Learned from the first paid calibration runs: Claude Code's `usage` field
    is unreliable as a check (in one run it was the LAST request, in another
    the TOTAL of all requests). `modelUsage` (running totals per model,
    sub-agents included) matched the transcript exactly in both, so it is the
    gate: transcript total must match it within 2%.
    """
    if not requests:
        return "empty transcript", True
    ours = sum(r.usage.fresh_in + r.usage.cache_write + r.usage.cache_read + r.usage.output for r in requests)
    totals = result.get("modelUsage")
    if isinstance(totals, dict) and totals:
        keys = ("inputTokens", "cacheCreationInputTokens", "cacheReadInputTokens", "outputTokens", "thinkingTokens")
        theirs = sum(int(m.get(k) or 0) for m in totals.values() if isinstance(m, dict) for k in keys)
        if theirs == 0:
            return "unverified (modelUsage is zero)", False
        if abs(ours - theirs) / theirs > TOKEN_TOLERANCE:
            return f"transcript total {ours} vs modelUsage {theirs}", True
        return "ok", False
    return "unverified (no modelUsage in result)", False


def run_one(commit: Commit, setup: Setup, repeat: int, layout: Layout, cfg: Config, *, attempt: int = 0, solution: Path | None = None) -> Record:
    run_id = f"{commit.sha[:10]}-{setup.name}-r{repeat}-a{attempt}"
    started = time.time()

    def record(outcome: str, *, usages=(), cost=0.0, check="", note="") -> Record:
        return Record(run_id, commit.repo, commit.sha, setup.name, repeat, outcome, outcome == "passed", setup.model or cfg.model, cost, round(time.time() - started, 1), [list(u) for u in usages], check, note[:500])

    try:
        work = workspace.clone_for_run(commit, layout, layout.prepared(commit.sha), run_id)
    except WorkspaceError as exc:
        return record("harness_error", note=f"clone: {exc}")
    if cfg.task_mode == "tests":
        workspace.overlay_commit_tests(commit, work)  # shown to the agent; restored before judging
    cfgdir = layout.run_dir(run_id) / "cfg"
    cfgdir.mkdir(parents=True)
    if setup.configure:
        try:
            setup.configure(cfgdir)
        except Exception as exc:  # never let one setup's tooling crash the batch
            return record("harness_error", note=f"setup configure failed: {exc}"[:500])
    if solution is not None:  # fake agent tests only
        shutil.copy(solution, cfgdir / "solution.tar")

    with AllowlistProxy(ANTHROPIC) as proxy:
        profile = Profile(
            writable=[work, cfgdir],
            readable=sandbox.tool_dirs() + ([workspace.UV_PYTHON] if workspace.UV_PYTHON.is_dir() else []),
            # the source repo too: it holds the answer, and may live outside $HOME
            deny_roots=[layout.root, Path(commit.repo)],
            proxy_port=proxy.port,
        )
        res = sandbox.run(
            setup.argv(task_text(commit, cfg.task_mode), cfg.model, cfg.per_run_budget_usd, cfgdir),
            profile=profile,
            profile_path=layout.profile_path(run_id),
            env=_agent_env(work, cfgdir, proxy, cfg.api_key),
            cwd=work,
            timeout=cfg.timeout,
        )
        blocked = proxy.blocked()

    result = parse_result(res.stdout)
    (cfgdir / "result.json").write_text(json.dumps(result, indent=1))  # audit trail (no key in it)
    parsed = parse_logs(iter_log_files(cfgdir / "projects"))
    usages = [(r.usage.fresh_in, r.usage.cache_write, r.usage.cache_read, r.usage.output) for r in parsed.requests]
    cost = float(result.get("total_cost_usd") or 0.0)
    note = f"blocked: {blocked[:5]}" if blocked else ""
    if result.get("escapes"):
        note += f" escapes: {result['escapes']}"
    if result.get("rewrote"):  # fake agent tests only
        note += f" rewrote: {result['rewrote']}"

    if res.timed_out:
        return record("timeout", usages=usages, cost=cost, check="n/a (timed out)", note=note)
    check, harness_error = _check_tokens(parsed.requests, result)
    subtype = str(result.get("subtype", ""))
    if "budget" in subtype:
        return record("budget_hit", usages=usages, cost=cost, check=check, note=note)
    message = str(result.get("result") or "")
    if result.get("is_error") and _ACCOUNT_ERROR.search(message):
        # The account can't make calls (no credit, bad key): not the agent's fault,
        # and every later run would fail the same way. The batch stops on this.
        return record("account_error", usages=usages, cost=cost, check=check, note=message[:300])
    if res.returncode != 0 or result.get("is_error"):
        if harness_error and not parsed.requests and not result:
            return record("harness_error", note=f"agent produced nothing: {(res.stderr or '')[-300:]}")
        return record("agent_error", usages=usages, cost=cost, check=check, note=(note + " " + (res.stderr or subtype))[-500:])
    if harness_error:
        return record("harness_error", usages=usages, cost=cost, check=check, note=note)

    workspace.overlay_commit_tests(commit, work)  # always the originals: edits to tests can't help
    passed, tail = workspace.judge(commit, layout, work, run_id + "-judge")
    rec = record("passed" if passed else "tests_failed", usages=usages, cost=cost, check=check, note=note or ("" if passed else tail[-300:]))
    shutil.rmtree(work, ignore_errors=True)  # keep cfg (transcript) for audits, drop the code copy
    return rec


@dataclass
class Batch:
    commits: list[Commit]
    setups: list[Setup]
    repeats: int
    layout: Layout
    cfg: Config
    results: Path
    solutions: dict[str, Path] = field(default_factory=dict)  # fake agent tests only
    spent: float = 0.0
    excluded: set[str] = field(default_factory=set)
    stopped: str = ""  # set when the account can't make calls; no new runs start
    done: set[tuple[str, str, int]] = field(default_factory=set)  # (commit, setup, repeat) already recorded (--resume)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def run(self, on_record=None) -> list[Record]:
        for s in self.setups:
            s.check()
            if s.configure:  # dry run before any spending: a broken setup fails here, for free
                probe = self.layout.root / "setup-check" / s.name
                shutil.rmtree(probe, ignore_errors=True)
                probe.mkdir(parents=True)
                s.configure(probe)
                shutil.rmtree(probe, ignore_errors=True)
        if self.cfg.api_key:
            preflight_key(self.cfg.api_key)
        with AllowlistProxy() as proxy:  # Phase A, one commit at a time
            for c in self.commits:
                workspace.prepare(c, self.layout, proxy)
        jobs = [(c, s, r) for c in self.commits for r in range(self.repeats) for s in self.setups if (c.sha, s.name, r) not in self.done]
        records: list[Record] = []
        with ThreadPoolExecutor(max_workers=self.cfg.parallel) as pool:
            for rec in pool.map(lambda job: self._job(*job), jobs):
                if rec is not None:
                    records.append(rec)
                    if on_record:
                        on_record(rec)
        return records

    def _job(self, commit: Commit, setup: Setup, repeat: int) -> Record | None:
        for attempt in range(2):
            with self._lock:
                if self.stopped or commit.sha in self.excluded:
                    return None
                if self.spent + self.cfg.per_run_budget_usd > self.cfg.total_cap_usd:
                    return None  # would risk crossing the cap: stop launching
                self.spent += self.cfg.per_run_budget_usd  # reserve the worst case
            rec = run_one(commit, setup, repeat, self.layout, self.cfg, attempt=attempt, solution=self.solutions.get(commit.sha))
            with self._lock:
                self.spent += rec.cost_usd - self.cfg.per_run_budget_usd  # settle to actual
                self._append(rec)
                if rec.outcome == "account_error":
                    self.stopped = rec.note
            if rec.outcome != "harness_error":
                return rec
        with self._lock:
            self.excluded.add(commit.sha)
            self._append_exclusion(commit.sha)
        return rec

    def _append(self, rec: Record) -> None:
        with open(self.results, "a") as fh:
            fh.write(json.dumps(asdict(rec)) + "\n")

    def _append_exclusion(self, sha: str) -> None:
        with open(self.results, "a") as fh:
            fh.write(json.dumps({"excluded_commit": sha, "reason": "harness_error twice"}) + "\n")


def completed(path: Path) -> set[tuple[str, str, int]]:
    """Runs in a results file that really happened, for --resume.

    An agent_error with no requests and no cost never reached the model (e.g.
    the account had no credit before account_error existed), so it is redone.
    """
    if not path.exists():
        return set()
    done = set()
    for line in path.read_text().splitlines():
        d = json.loads(line) if line.strip() else {}
        if "outcome" not in d or d["outcome"] in ("harness_error", "account_error"):
            continue
        if d["outcome"] == "agent_error" and not d["usages"] and not d["cost_usd"]:
            continue
        done.add((d["commit"], d["setup"], d["repeat"]))
    return done


def load_results(path: Path) -> tuple[list[Record], set[str]]:
    records, excluded = [], set()
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        if "excluded_commit" in data:
            excluded.add(data["excluded_commit"])
        else:
            records.append(Record(**data))
    # The last record per (commit, setup, repeat) wins, so --resume can redo dead runs.
    latest: dict[tuple[str, str, int], Record] = {}
    for r in records:
        latest[(r.commit, r.setup, r.repeat)] = r
    final = [
        r for r in latest.values()
        if r.outcome not in ("harness_error", "account_error")
        and not (r.outcome == "agent_error" and not r.usages and not r.cost_usd)
        and r.commit not in excluded
    ]
    return final, excluded


def make_solution(commit: Commit, dest: Path) -> Path:
    """Tar of the commit's non-test files (fake 'solve' agent only)."""
    tmp = dest.with_suffix(".tree")
    workspace.export_tree(commit.repo, commit.sha, tmp)
    with tarfile.open(dest, "w") as tar:
        for path in commit.code_files:
            if (tmp / path).exists():
                tar.add(tmp / path, arcname=path)
    shutil.rmtree(tmp, ignore_errors=True)
    return dest
