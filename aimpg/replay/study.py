"""Statistics for a pre-registered multi-repo study (launch-review design, 2026-10-11).

For each challenger against the baseline, across every repo in the results:

    solve rate            with a 95% Wilson interval, overall and per repo
    cost per attempt      intention-to-treat: every run counts, failures included
    cost per solved task  total $ / tasks solved; the challenger/baseline ratio gets
                          a 95% interval from a bootstrap that resamples repos, then
                          commits within each repo (runs of one commit move together)
    tokens per attempt    the unit token-savers advertise; same ratio and interval
    direction by repo     in how many repos the challenger came out cheaper per solved task

Headline rule (pre-registered): a claim is stated only when its interval
excludes "no difference" after Holm correction across the primary comparisons
AND its direction holds in at least 5 of the 6+ repos (or all repos if fewer).
"""

from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import dataclass, field
from statistics import mean

from aimpg.replay.run import Record

BOOTSTRAP = 4000
Z95 = 1.959964


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def _tokens(r: Record) -> int:
    return sum(sum(u) for u in r.usages)


@dataclass
class Arm:
    setup: str
    runs: int = 0
    solved: int = 0
    usd: float = 0.0
    tokens: int = 0

    @property
    def solve_rate(self) -> float:
        return self.solved / self.runs if self.runs else 0.0

    @property
    def usd_per_solved(self) -> float | None:
        return self.usd / self.solved if self.solved else None


@dataclass
class Comparison:
    baseline: str
    challenger: str
    repos: int
    commits: int
    arms: dict[str, Arm]
    solve_ci: dict[str, tuple[float, float]]
    cost_per_attempt: dict[str, float]
    ratio_per_solved: float | None
    ratio_per_solved_ci: tuple[float, float] | None
    ratio_tokens: float | None
    ratio_tokens_ci: tuple[float, float] | None
    p_per_solved: float | None  # p of the PRIMARY metric (name kept for compatibility)
    cheaper_in_repos: int  # repos where the challenger came out lower on the primary metric
    per_repo: dict[str, dict] = field(default_factory=dict)
    p_holm: float | None = None
    headline: bool = False
    primary: str = "per_solved"  # per_solved | tokens | per_attempt | solved
    ratio_per_attempt: float | None = None
    ratio_per_attempt_ci: tuple[float, float] | None = None
    ratio_solved: float | None = None  # challenger's solve rate / baseline's
    ratio_solved_ci: tuple[float, float] | None = None
    higher_in_repos: int = 0  # repos where the challenger came out higher on the primary metric (ties are neither)


def _group(records: list[Record]) -> dict[str, dict[str, list[Record]]]:
    """repo → commit → runs."""
    out: dict[str, dict[str, list[Record]]] = defaultdict(lambda: defaultdict(list))
    for r in records:
        out[r.repo][r.commit].append(r)
    return out


def _ratios(groups, baseline: str, challenger: str) -> tuple[float | None, float | None, float | None, float | None]:
    sums = {s: [0.0, 0, 0] for s in (baseline, challenger)}  # usd, solved, tokens
    for commits in groups:
        for runs in commits:
            for r in runs:
                if r.setup in sums:
                    sums[r.setup][0] += r.cost_usd
                    sums[r.setup][1] += r.passed
                    sums[r.setup][2] += _tokens(r)
    b, c = sums[baseline], sums[challenger]
    per_solved = (c[0] / c[1]) / (b[0] / b[1]) if b[1] and c[1] and b[0] else None
    n_b = sum(1 for g in groups for runs in g for r in runs if r.setup == baseline)
    n_c = sum(1 for g in groups for runs in g for r in runs if r.setup == challenger)
    tokens = (c[2] / n_c) / (b[2] / n_b) if n_b and n_c and b[2] else None
    per_attempt = (c[0] / n_c) / (b[0] / n_b) if n_b and n_c and b[0] else None
    solved = (c[1] / n_c) / (b[1] / n_b) if n_b and n_c and b[1] else None
    return per_solved, tokens, per_attempt, solved


METRICS = ("per_solved", "tokens", "per_attempt", "solved")


def compare(records: list[Record], baseline: str, challenger: str, *, seed: int = 0, primary: str = "per_solved") -> Comparison:
    """`primary` is the pre-registered metric: its p-value goes into Holm and the headline rule."""
    if primary not in METRICS:
        raise ValueError(f"primary must be one of {METRICS}")
    mine = [r for r in records if r.setup in (baseline, challenger)]
    by_repo = _group(mine)
    # only commits both arms attempted (a commit excluded for one setup is excluded for both)
    for repo, commits in by_repo.items():
        for sha in [s for s, runs in commits.items() if {r.setup for r in runs} != {baseline, challenger}]:
            del commits[sha]
    mine = [r for commits in by_repo.values() for runs in commits.values() for r in runs]
    arms = {s: Arm(s) for s in (baseline, challenger)}
    for r in mine:
        a = arms[r.setup]
        a.runs += 1
        a.solved += r.passed
        a.usd += r.cost_usd
        a.tokens += _tokens(r)
    groups = [list(commits.values()) for commits in by_repo.values()]
    point, tok_point, att_point, sol_point = _ratios(groups, baseline, challenger)

    rng = random.Random(seed)
    boot, boot_tok, boot_att, boot_sol = [], [], [], []
    for _ in range(BOOTSTRAP if groups else 0):
        sample = []
        for _ in groups:  # resample repos, then commits within each
            g = groups[rng.randrange(len(groups))]
            sample.append([g[rng.randrange(len(g))] for _ in g])
        r, t, a, s = _ratios(sample, baseline, challenger)
        if s is not None:
            boot_sol.append(s)
        if r is not None:
            boot.append(r)
        if t is not None:
            boot_tok.append(t)
        if a is not None:
            boot_att.append(a)

    def ci(xs: list[float]) -> tuple[float, float] | None:
        if len(xs) < 100:
            return None
        xs = sorted(xs)
        return (xs[int(0.025 * (len(xs) - 1))], xs[int(0.975 * (len(xs) - 1))])

    idx = METRICS.index(primary)
    p_point, p_boot = (point, tok_point, att_point, sol_point)[idx], (boot, boot_tok, boot_att, boot_sol)[idx]
    p = None
    if p_boot and p_point is not None:
        below = sum(1 for x in p_boot if x < 1) / len(p_boot)
        p = min(1.0, 2 * min(below, 1 - below))

    per_repo, cheaper, higher = {}, 0, 0
    for repo, commits in by_repo.items():
        r, t, a, s = _ratios([list(commits.values())], baseline, challenger)
        primary_ratio = (r, t, a, s)[idx]
        k = {s_: sum(x.passed for runs in commits.values() for x in runs if x.setup == s_) for s_ in arms}
        n = {s_: sum(1 for runs in commits.values() for x in runs if x.setup == s_) for s_ in arms}
        per_repo[repo] = {"ratio_per_solved": r, "ratio_tokens": t, "ratio_per_attempt": a, "ratio_solved": s, "solved": k, "runs": n,
                          "commits": len(commits)}
        if primary == "solved":  # solve rates compare even when one side solved nothing; a tie is neither
            rates = {s_: k[s_] / n[s_] if n[s_] else 0.0 for s_ in arms}
            cheaper += rates[challenger] < rates[baseline]
            higher += rates[challenger] > rates[baseline]
        else:
            cheaper += primary_ratio is not None and primary_ratio < 1
    return Comparison(
        baseline, challenger, len(by_repo), sum(len(c) for c in by_repo.values()), arms,
        {s: wilson(a.solved, a.runs) for s, a in arms.items()},
        {s: a.usd / a.runs if a.runs else 0.0 for s, a in arms.items()},
        point, ci(boot), tok_point, ci(boot_tok), p, cheaper, per_repo,
        primary=primary, ratio_per_attempt=att_point, ratio_per_attempt_ci=ci(boot_att),
        ratio_solved=sol_point, ratio_solved_ci=ci(boot_sol),
        higher_in_repos=higher if primary == "solved" else len(by_repo) - cheaper,
    )


def holm(comparisons: list[Comparison], *, alpha: float = 0.05) -> None:
    """Holm-adjusted p-values and the headline rule, in place."""
    ordered = sorted([c for c in comparisons if c.p_per_solved is not None], key=lambda c: c.p_per_solved)
    m, running = len(ordered), 0.0
    for i, c in enumerate(ordered):
        running = max(running, min(1.0, (m - i) * c.p_per_solved))
        c.p_holm = running
    for c in comparisons:
        need = 5 if c.repos >= 6 else c.repos + 1  # pre-registered: >= 6 repos, direction holding in >= 5 (pilot caught 1 repo passing)
        ratio = {"per_solved": c.ratio_per_solved, "tokens": c.ratio_tokens, "per_attempt": c.ratio_per_attempt,
                 "solved": c.ratio_solved}[c.primary]
        if c.primary == "solved":
            direction = c.cheaper_in_repos if (ratio or 1) < 1 else c.higher_in_repos
        else:
            direction = c.cheaper_in_repos if (ratio or 1) < 1 else c.repos - c.cheaper_in_repos
        c.headline = bool(c.p_holm is not None and c.p_holm < alpha and c.repos and direction >= need)


PRIMARY_NAMES = {"per_solved": "per solved", "tokens": "tokens", "per_attempt": "per attempt", "solved": "solve rate"}


def render(comparisons: list[Comparison]) -> str:
    def pct(x):
        return "—" if x is None else f"{x - 1:+.0%}"

    lines = ["| challenger vs baseline | primary metric | repos · commits | solved (95% CI) | $ per attempt (95% CI) | $ per solved task (95% CI) | tokens per attempt (95% CI) | lower in | headline |",
             "|---|---|---|---|---|---|---|---|---|"]
    for c in comparisons:
        b, ch = c.arms[c.baseline], c.arms[c.challenger]
        lo, hi = c.solve_ci[c.challenger]
        rc = c.ratio_per_solved_ci
        tc = c.ratio_tokens_ci
        ac = c.ratio_per_attempt_ci
        lines.append(
            f"| {c.challenger} vs {c.baseline} | {PRIMARY_NAMES[c.primary]} | {c.repos} · {c.commits} | {ch.solve_rate:.0%} ({lo:.0%}–{hi:.0%}) vs {b.solve_rate:.0%}"
            + (f", {pct(c.ratio_solved)} ({pct(c.ratio_solved_ci[0]) if c.ratio_solved_ci else '—'} to {pct(c.ratio_solved_ci[1]) if c.ratio_solved_ci else '—'}) "
               if c.primary == "solved" else " ") +
            f"| ${c.cost_per_attempt[c.challenger]:.2f} vs ${c.cost_per_attempt[c.baseline]:.2f}, {pct(c.ratio_per_attempt)} ({pct(ac[0]) if ac else '—'} to {pct(ac[1]) if ac else '—'}) "
            f"| {pct(c.ratio_per_solved)} ({pct(rc[0]) if rc else '—'} to {pct(rc[1]) if rc else '—'}) "
            f"| {pct(c.ratio_tokens)} ({pct(tc[0]) if tc else '—'} to {pct(tc[1]) if tc else '—'}) "
            f"| {c.cheaper_in_repos} of {c.repos} repos | {'yes' if c.headline else 'no'} |")
        if c.primary == "solved":
            lines[-1] = lines[-1].replace(f"| {c.cheaper_in_repos} of {c.repos} repos |",
                                          f"| {c.cheaper_in_repos} of {c.repos} repos (higher in {c.higher_in_repos}) |")
    lines += ["", "Intervals: 95%, bootstrap over repos then commits. Headline = Holm-adjusted p < 0.05 on the pre-registered primary metric, and its direction holds in ≥5 of 6+ repos."]
    return "\n".join(lines)


# ---------------------------------------------------------------- beyond the primary comparisons
# Report No. 1 showed that one cost-per-solved number can hide which tasks a setup solves at all.
# These tables read the same records by group. `by_task` uses rules fixed before any run; `by_passes`
# groups tasks by the results themselves and is exploratory.

PASS_BANDS = (("easy", 5 / 6), ("in between", 1 / 4), ("hard", 0.0))  # by the share of passing runs across the setups


def _single(records: list[Record], setups: list[str]) -> list[Record]:
    return [r for r in records if r.setup in setups]


def _cells(records: list[Record], setups: list[str], tasks: set) -> list[Arm]:
    arms = {s: Arm(s) for s in setups}
    for r in records:
        if (r.repo, r.commit) in tasks:
            a = arms[r.setup]
            a.runs += 1
            a.solved += r.passed
            a.usd += r.cost_usd
    return [arms[s] for s in setups]


WORDS_SPLIT = 15  # a description shorter than this many words is "thin"
LINES_SPLIT = 30  # an original commit that changed more than this many lines of code is "large"
TASK_GROUPS = {  # fixed before any run, from the original commit alone; Report No. 1 suggested both (see its page)
    "description": ("description_words", (("under 15 words", lambda n: n < WORDS_SPLIT), ("15 words or more", lambda n: n >= WORDS_SPLIT))),
    "size": ("code_lines", (("30 or fewer changed lines of code", lambda n: n <= LINES_SPLIT), ("more than 30", lambda n: n > LINES_SPLIT))),
}


def by_task(records: list[Record], setups: list[str], group: str) -> list[tuple[str, int, list[Arm]]]:
    """Results by a pre-set task group: `description` (how long the task's description is) or `size`
    (how many lines of code the original commit changed). Runs that don't carry the measurement
    (made before aimpg recorded it) are left out."""
    field_, bands = TASK_GROUPS[group]
    mine = [r for r in _single(records, setups) if isinstance(r.extra.get(field_), int)]
    out = []
    for label, test in bands:
        tasks = {(r.repo, r.commit) for r in mine if test(r.extra[field_])}
        if tasks:
            out.append((label, len(tasks), _cells(mine, setups, tasks)))
    return out


def subset(records: list[Record], group: str, label: str) -> list[Record]:
    """The records of one task group (for a comparison within it)."""
    field_, bands = TASK_GROUPS[group]
    test = dict(bands)[label]
    return [r for r in records if isinstance(r.extra.get(field_), int) and test(r.extra[field_])]


def by_passes(records: list[Record], setups: list[str]) -> list[tuple[str, int, list[Arm]]]:
    """Exploratory: tasks grouped by the share of passing runs across `setups`. The groups are made
    from the results they describe, so read which setups the passes came from, not the group sizes."""
    mine = _single(records, setups)
    runs, passed = defaultdict(int), defaultdict(int)
    for r in mine:
        runs[r.repo, r.commit] += 1
        passed[r.repo, r.commit] += r.passed
    out, left = [], set(runs)
    for label, floor in PASS_BANDS:
        tasks = {t for t in left if passed[t] / runs[t] >= floor}
        left -= tasks
        if tasks:
            out.append((label, len(tasks), _cells(mine, setups, tasks)))
    return out


def claims(records: list[Record]) -> dict[str, dict[str, int]]:
    """How often "done" is true: per setup, the runs that ended with STATUS: DONE and how many of
    those failed the hidden tests. Only runs that were asked for a status line count."""
    out: dict[str, dict[str, int]] = {}
    for r in records:
        claimed = r.extra.get("claimed")
        if claimed is None:
            continue
        row = out.setdefault(r.setup, {"runs": 0, "said_done": 0, "said_done_but_failed": 0, "said_not_done": 0,
                                       "said_not_done_but_passed": 0, "no_status_line": 0})
        row["runs"] += 1
        if claimed == "done":
            row["said_done"] += 1
            row["said_done_but_failed"] += not r.passed
        elif claimed == "not_done":
            row["said_not_done"] += 1
            row["said_not_done_but_passed"] += bool(r.passed)
        else:
            row["no_status_line"] += 1
    return out


def delegation(records: list[Record]) -> dict[str, dict]:
    """For planner setups: how the runs really behaved (from the transcripts)."""
    out: dict[str, dict] = {}
    for r in records:
        if "delegations" not in r.extra:
            continue
        row = out.setdefault(r.setup, {"runs": 0, "delegated": 0, "orchestrator_edited": 0, "usd": 0.0, "usd_by_model": defaultdict(float)})
        row["runs"] += 1
        row["delegated"] += r.extra["delegations"] > 0
        row["orchestrator_edited"] += r.extra.get("orchestrator_edits", 0) > 0
        row["usd"] += r.cost_usd
        for model, part in (r.extra.get("by_model") or {}).items():
            row["usd_by_model"][model] += part.get("usd", 0.0)
    return out


def render_groups(title: str, groups: list[tuple[str, int, list[Arm]]], setups: list[str]) -> str:
    lines = [title, "", "| tasks | how many | " + " | ".join(setups) + " |", "|---|---|" + "---|" * len(setups)]
    for label, n, arms in groups:
        cells = [f"{a.solved} of {a.runs}" + (f", ${a.usd_per_solved:.2f} per solved" if a.usd_per_solved is not None else "") for a in arms]
        lines.append(f"| {label} | {n} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def render_extras(records: list[Record], setups: list[str]) -> str:
    """The group tables and the claim and delegation counts, for whatever the records hold."""
    parts = []
    for group, title in (("description", "Solved runs by the length of the task's description (pre-set group):"),
                         ("size", "Solved runs by how much code the original commit changed (pre-set group):")):
        rows = by_task(records, setups, group)
        if rows:
            parts.append(render_groups(title, rows, setups))
    passes = by_passes(records, setups)
    if passes:
        parts.append(render_groups("Exploratory: solved runs by how hard each task turned out (share of passing runs across these setups: "
                                   "easy 5/6 or more, hard under 1/4):", passes, setups))
    said = claims(records)
    if said:
        lines = ['How often "done" is true (runs that ended with a status line):', "",
                 "| setup | runs | said done | of those, tests failed | said not done | of those, tests passed | no status line |", "|---|---|---|---|---|---|---|"]
        for s, c in said.items():
            rate = f" ({c['said_done_but_failed'] / c['said_done']:.0%})" if c["said_done"] else ""
            lines.append(f"| {s} | {c['runs']} | {c['said_done']} | {c['said_done_but_failed']}{rate} | {c['said_not_done']} | "
                         f"{c['said_not_done_but_passed']} | {c['no_status_line']} |")
        parts.append("\n".join(lines))
    for s, d in delegation(records).items():
        share = ", ".join(f"{m} {usd / d['usd']:.0%}" for m, usd in sorted(d["usd_by_model"].items())) if d["usd"] else "n/a"
        parts.append(f"{s}: the orchestrator delegated in {d['delegated']} of {d['runs']} runs and edited files itself in "
                     f"{d['orchestrator_edited']}. Cost by model: {share}.")
    return "\n\n".join(parts)

