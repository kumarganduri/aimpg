"""Decide whether a challenger setup really uses less energy than the baseline.

Rules (Phase 2 review R7, R15, R16):

* Energy is compared only on **shared passes**: commits where both setups
  passed at least once. A setup that gives up early can't "win" by being cheap.
* Per commit, a setup's energy is the mean W of its passing runs; the effect
  is the relative difference (challenger - baseline) / baseline, so big and
  small commits weigh the same.
* A paired bootstrap over commits gives a confidence interval. The challenger
  wins only if the interval excludes 0 at **every** corner of the energy-factor
  ranges (W's internal ratios are estimates, D12). Each challenger is tested
  at 97.5% so two claims together stay at 95%.
* Pass rates are reported separately, never folded into the energy verdict.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import dataclass, field
from statistics import NormalDist, mean, stdev

from aimpg.energy import corners, joules, model_class
from aimpg.model import Usage

MIN_SHARED = 6
LEVEL = 0.975
BOOTSTRAP = 2000


@dataclass
class Run:
    commit: str
    setup: str
    repeat: int
    passed: bool
    usages: list[Usage]
    model: str


@dataclass
class Verdict:
    challenger: str
    verdict: str  # "uses less" | "uses more" | "not proven" | "not enough passing commits"
    shared_commits: int
    effect_mid: float | None = None  # mean relative difference at the middle corner
    ci_worst: tuple[float, float] | None = None  # widest interval across corners
    pass_rate: dict[str, float] = field(default_factory=dict)


def run_w(run: Run) -> list[float]:
    """W of one run at every corner of its model class's energy ranges."""
    cls, _ = model_class(run.model)
    return [sum(joules(u, f) for u in run.usages) / f.e_pre for f in corners(cls)]


def _per_commit(runs: list[Run], setup: str) -> dict[str, list[list[float]]]:
    out: dict[str, list[list[float]]] = defaultdict(list)
    for r in runs:
        if r.setup == setup and r.passed:
            out[r.commit].append(run_w(r))
    return out


def _mean_vec(vectors: list[list[float]]) -> list[float]:
    return [mean(col) for col in zip(*vectors)]


def compare(runs: list[Run], baseline: str, challenger: str, *, seed: int = 0) -> Verdict:
    rates = {s: _pass_rate(runs, s) for s in (baseline, challenger)}
    base, chal = _per_commit(runs, baseline), _per_commit(runs, challenger)
    shared = sorted(set(base) & set(chal))
    if len(shared) < MIN_SHARED:
        return Verdict(challenger, "not enough passing commits", len(shared), pass_rate=rates)

    # effects[i][k] = relative difference on shared commit i at corner k
    effects = []
    for c in shared:
        b, ch = _mean_vec(base[c]), _mean_vec(chal[c])
        effects.append([(y - x) / x for x, y in zip(b, ch)])
    n_corners = len(effects[0])

    rng = random.Random(seed)
    lo_q, hi_q = (1 - LEVEL) / 2, 1 - (1 - LEVEL) / 2
    samples: list[list[float]] = [[] for _ in range(n_corners)]
    for _ in range(BOOTSTRAP):
        idx = [rng.randrange(len(shared)) for _ in shared]
        for k in range(n_corners):
            samples[k].append(sum(effects[i][k] for i in idx) / len(idx))
    intervals = []
    for k in range(n_corners):
        s = sorted(samples[k])
        intervals.append((s[int(lo_q * (len(s) - 1))], s[int(hi_q * (len(s) - 1))]))

    if all(hi < 0 for _, hi in intervals):
        verdict = "uses less"
    elif all(lo > 0 for lo, _ in intervals):
        verdict = "uses more"
    else:
        verdict = "not proven"
    worst = (min(lo for lo, _ in intervals), max(hi for _, hi in intervals))
    mid = mean(mean(e) for e in effects)
    return Verdict(challenger, verdict, len(shared), mid, worst, rates)


def _pass_rate(runs: list[Run], setup: str) -> float:
    mine = [r for r in runs if r.setup == setup]
    return sum(r.passed for r in mine) / len(mine) if mine else float("nan")


def commits_needed(runs: list[Run], baseline: str, challenger: str, effect: float = 0.10, power: float = 0.8) -> int | None:
    """Rough sample size to detect a relative energy difference of `effect`.

    Uses the spread of paired per-run relative differences (same commit, same
    repeat index) from calibration. None if there aren't enough pairs.
    """
    by_key: dict[tuple[str, int], dict[str, Run]] = defaultdict(dict)
    for r in runs:
        if r.passed and r.setup in (baseline, challenger):
            by_key[(r.commit, r.repeat)][r.setup] = r
    diffs = []
    for pair in by_key.values():
        if len(pair) == 2:
            b = mean(run_w(pair[baseline]))
            c = mean(run_w(pair[challenger]))
            diffs.append((c - b) / b)
    if len(diffs) < 2:
        return None
    sd = stdev(diffs)
    z = NormalDist().inv_cdf(1 - (1 - LEVEL) / 2) + NormalDist().inv_cdf(power)
    return max(MIN_SHARED, math.ceil((z * sd / effect) ** 2))
