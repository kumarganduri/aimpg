"""Working (durable) changes: AI-assisted commits that shipped and stayed.

A commit becomes judgeable 30 days after it was made. It is:

    reverted   a later commit says "This reverts commit <sha>"
    reworked   within 30 days, later commits on the default branch deleted at
               least 30% of the meaningful lines it added
    not kept   it never reached the default branch (or is unknown)
    durable    none of the above

    cost per working change = AI $ on judged commits / number of durable ones
    waste ratio             = AI $ on judged commits that weren't durable / AI $ on judged commits

Commits younger than 30 days are "too new" and left out of both numbers.
"""

from __future__ import annotations

import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from aimpg.gitkept import DAY, GitError, KeptChecker, load_commits

MATURE_AFTER = 30 * DAY
REWORK_SHARE = 0.30


@dataclass
class Judged:
    durable: list[dict] = field(default_factory=list)
    reverted: list[dict] = field(default_factory=list)
    reworked: list[dict] = field(default_factory=list)
    not_kept: list[dict] = field(default_factory=list)
    too_new: list[dict] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def judged(self) -> list[dict]:
        return self.durable + self.reverted + self.reworked + self.not_kept

    def cost_per_durable(self) -> float | None:
        usd = sum(r["usd"] for r in self.judged)
        return usd / len(self.durable) if self.durable else None

    def waste_ratio(self) -> float | None:
        usd = sum(r["usd"] for r in self.judged)
        wasted = usd - sum(r["usd"] for r in self.durable)
        return wasted / usd if usd else None

    def first_judgeable(self) -> float | None:
        return min((r["ts"] for r in self.too_new), default=None)


def reverted_shas(repo: str, since: float) -> set[str]:
    proc = subprocess.run(
        ["git", "-C", repo, "log", "--all", f"--since={int(since)}", "--grep=This reverts commit", "--format=%B%x1e"],
        capture_output=True, text=True, errors="replace",
    )
    out = set()
    for body in proc.stdout.split("\x1e"):
        for word in body.replace(".", " ").split():
            if len(word) == 40 and all(c in "0123456789abcdef" for c in word):
                out.add(word)
    return out


def judge(ledger: dict[str, dict], now: float | None = None) -> Judged:
    now = now or time.time()
    result = Judged()
    by_repo: dict[str, list[dict]] = defaultdict(list)
    for rec in ledger.values():
        (by_repo[rec["repo"]] if now - rec["ts"] >= MATURE_AFTER else result.too_new).append(rec)
    for repo, recs in by_repo.items():
        oldest = min(r["ts"] for r in recs)
        try:
            checker = KeptChecker(repo, oldest - DAY, now)
            commits = load_commits(repo, oldest - DAY, reflog=False)
            later = load_commits(repo, oldest, refs=[checker.ref])
            statuses = checker.classify(commits)
            reverts = reverted_shas(repo, oldest)
        except GitError as exc:
            result.errors[repo] = str(exc)
            result.not_kept.extend(recs)
            continue
        own = {c.sha: c for c in commits}
        for rec in recs:
            status = statuses.get(rec["sha"])
            if rec["sha"] in reverts:
                result.reverted.append(rec)
            elif status is None or not status.is_kept:
                result.not_kept.append(rec)
            elif _reworked(own.get(rec["sha"]), later):
                result.reworked.append(rec)
            else:
                result.durable.append(rec)
    return result


def _reworked(commit, later) -> bool:
    if commit is None:
        return False
    added = {p: lines for p, lines in commit.added.items() if lines}
    total = sum(len(v) for v in added.values())
    if total == 0:
        return False
    gone = set()
    for c in later:
        if c.sha == commit.sha or not (commit.ts < c.ts <= commit.ts + MATURE_AFTER):
            continue
        for path, lines in added.items():
            gone |= {(path, l) for l in lines & c.removed.get(path, set())}
    return len(gone) / total >= REWORK_SHARE


def day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
