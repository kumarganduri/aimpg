"""Turn an Attribution into the printed fuel receipt."""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from aimpg import equivalence
from aimpg.attribution import NO_COMMIT_YET, Attribution
from aimpg.coach import tips as coach_tips
from aimpg.cost import total_cost
from aimpg.energy import ZERO, WhRange, load_factors, model_class, total_wh, weighted_wh
from aimpg.gitkept import Status
from aimpg.model import ParseResult, Task

STATUS_ORDER = [s.value for s in Status] + ["git error"]
KEPT = {Status.KEPT.value, Status.KEPT_SQUASH.value}
METHOD_URL = "https://github.com/kumarganduri/aimpg#how-energy-is-estimated"


@dataclass
class TaskEnergy:
    task: Task
    wh: WhRange  # direct: the final work burst before the commit
    lead_up: WhRange  # earlier bursts since the previous commit
    usd: float = 0.0  # direct, API-equivalent
    lead_up_usd: float = 0.0

    @property
    def total(self) -> WhRange:
        return self.wh + self.lead_up


def task_energy(tasks: list[Task]) -> list[TaskEnergy]:
    return [
        TaskEnergy(
            t,
            weighted_wh(t.requests, t.weights),
            weighted_wh(t.lead_up, t.lead_up_weights),
            total_cost(t.requests, t.weights)[0],
            total_cost(t.lead_up, t.lead_up_weights)[0],
        )
        for t in tasks
    ]


def _usd(x: float) -> str:
    return f"${x:,.0f}" if x >= 100 else f"${x:,.2f}"


def summary(in_window, energies: list[TaskEnergy]) -> list[str]:
    """The plain-language top of the receipt: what it was like, in everyday terms."""
    everything = total_wh(in_window)
    usd, unknown = total_cost(in_window)
    lines = [
        "YOUR AI CODING",
        f"  Energy   {_fmt_wh(everything)}",
        f"           ≈ {equivalence.everyday(everything)}",
        f"  Money    {_usd(usd)} API-equivalent (Anthropic's published prices"
        + (f"; {unknown} requests from unpriced models left out)" if unknown else ")"),
        f"  Output   {len(energies)} commits made with AI help",
    ]
    with_work = [e for e in energies if e.task.requests]
    if with_work:
        kept = [e for e in with_work if e.task.status in KEPT]
        pool = kept if len(kept) >= 5 else with_work
        typical = WhRange(statistics.median(e.wh.low for e in pool), statistics.median(e.wh.high for e in pool))
        label = "A typical kept commit" if pool is kept else "A typical commit"
        lines += [
            "",
            f"  {label + ':':<24}{_fmt_wh(typical)} ≈ {equivalence.phrase(typical, 'phone')}"
            f" · {_usd(statistics.median(e.usd for e in pool))}",
        ]
        top = max(with_work, key=lambda e: e.wh.mid)
        extra = f" (+{_fmt_wh(top.lead_up)} of earlier work)" if top.lead_up.high > 0 else ""
        lines += [
            f"  {'Most expensive commit:':<24}{Path(top.task.repo).name} {top.task.sha[:7]} \"{top.task.subject[:50]}\"",
            f"  {'':<24}{_fmt_wh(top.wh)} ≈ {equivalence.phrase(top.wh, 'phone')} · {_usd(top.usd)}{extra}",
        ]
    return lines


def _fmt_wh(r: WhRange) -> str:
    def one(x: float) -> str:
        return f"{x / 1000:.2f} kWh" if x >= 1000 else f"{x:.1f} Wh" if x >= 1 else f"{x * 1000:.0f} mWh"

    return f"{one(r.low)} – {one(r.high)}"


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def working_changes(judged) -> list[str]:
    from aimpg.durable import MATURE_AFTER, day

    lines = ["WORKING CHANGES (commits that shipped and lasted 30 days)"]
    if not judged.judged:
        first = judged.first_judgeable()
        when = f" on {day(first + MATURE_AFTER)}" if first else " once your AI-assisted commits are 30 days old"
        lines.append(f"  Available{when}: aimpg is keeping your history so it can tell.")
        return lines
    n, d = len(judged.judged), len(judged.durable)
    cpd, waste = judged.cost_per_durable(), judged.waste_ratio()
    lines.append(f"  {d} of {n} judged commits are working changes ({d / n:.0%})"
                 f" · {len(judged.reverted)} reverted · {len(judged.reworked)} reworked · {len(judged.not_kept)} never shipped")
    if cpd is not None:
        lines.append(f"  Cost per working change: {_usd(cpd)}  (AI spend on judged commits ÷ working changes)")
    if waste is not None:
        lines.append(f"  Waste ratio: {waste:.0%} of that AI spend went into work that didn't last")
    if judged.too_new:
        lines.append(f"  ({len(judged.too_new)} newer commits will be judged as they turn 30 days old)")
    return lines


def render(parsed: ParseResult, attribution: Attribution, since: float, now: float, judged=None) -> str:
    in_window = [r for r in parsed.requests if r.ts >= since]
    energies = task_energy(attribution.tasks)
    attributed = sum((e.total for e in energies), ZERO)
    lead_up = sum((e.lead_up for e in energies), ZERO)
    everything = total_wh(in_window)
    lines: list[str] = []
    add = lines.append

    add(f"aimpg receipt · {_day(since)} → {_day(now)}")
    add("")
    if in_window:
        lines.extend(summary(in_window, energies))
        found = coach_tips(in_window, attribution)
        if found:
            add("")
            add("WHAT WOULD HAVE SAVED THE MOST (measured on your logs; upper bounds that overlap)")
            for i, t in enumerate(found, 1):
                add(f"  {i}. {t.title}: up to {t.share:.0%} less energy, {_usd(t.saving_usd)}")
                add(f"     {t.measured}.")
                add(f"     ≈ {equivalence.everyday(t.saving_wh)}")
                add(f"     → {t.action}.")
        if judged is not None:
            add("")
            lines.extend(working_changes(judged))
        add("")
        add("DETAILS")
    skipped = parsed.stats.get("skipped_requests", 0) + parsed.stats.get("corrupt_rows", 0)
    add(
        f"Read {parsed.stats.get('unique_requests', 0):,} AI requests from {parsed.stats.get('files', 0)} log files "
        f"(coverage {parsed.coverage:.1%}, {skipped} rows skipped, {len(parsed.versions)} Claude Code versions)."
    )
    assumed = sorted({r.model for r in in_window if model_class(r.model)[1]})
    if assumed:
        add(f"Unrecognized models (sized as 'mid', flagged): {', '.join(assumed)}")
    add("")

    if not in_window:
        add("No AI requests in this window.")
        return "\n".join(lines) + "\n"

    add(f"{'AI energy in window ':.<46} {_fmt_wh(everything)}  ({len(in_window):,} requests)")
    tiers = {k: sum(1 for e in energies if e.task.attribution == k) for k in ("exact", "fuzzy", "grace")}
    add(
        f"  {'matched to commits ':.<44} {_fmt_wh(attributed)}  ({len(energies)} commits: "
        f"{tiers['exact']} exact, {tiers['fuzzy']} fuzzy, {tiers['grace']} grace)"
    )
    if lead_up.high > 0:
        add(f"    {'of which lead-up (before a 2h+ break) ':.<42} {_fmt_wh(lead_up)}")
    exact_wh = sum((e.total for e in energies if e.task.attribution == "exact"), ZERO)
    in_repo = attributed + total_wh(attribution.unattributed.get(NO_COMMIT_YET, []))
    if in_repo.mid > 0:
        add(f"  exact-match share of in-repo energy: {exact_wh.mid / in_repo.mid:.0%}, any match: {attributed.mid / in_repo.mid:.0%}")
    for reason, reqs in sorted(attribution.unattributed.items()):
        add(f"  {reason + ' ':.<44} {_fmt_wh(total_wh(reqs))}  ({len(reqs):,} requests)")
    add("")

    if energies:
        add("Commits by status          count   energy")
        by_status: dict[str, list[TaskEnergy]] = defaultdict(list)
        for e in energies:
            by_status[e.task.status].append(e)
        for status in STATUS_ORDER:
            group = by_status.get(status)
            if group:
                add(f"  {status:<25}{len(group):>5}   {_fmt_wh(sum((e.total for e in group), ZERO))}")
        kept = [e for e in energies if e.task.status in KEPT]
        if kept:
            med = WhRange(statistics.median(e.wh.low for e in kept), statistics.median(e.wh.high for e in kept))
            add("")
            add(f"Median direct energy per kept commit: {_fmt_wh(med)}")
        decided = [e for e in energies if e.task.status in KEPT or e.task.status == Status.DISCARDED.value]
        if decided:
            discarded = sum((e.total for e in decided if e.task.status == Status.DISCARDED.value), ZERO)
            share = discarded.mid / max(sum((e.total for e in decided), ZERO).mid, 1e-12)
            add(f"Energy on commits later discarded: {share:.0%} (of commits with a decided status)")
        add("")
        # Commits made back-to-back share one segment; the later ones carry no
        # requests of their own, so they are left out of the rankings.
        ranked = sorted((e for e in energies if e.task.requests), key=lambda e: e.wh.mid, reverse=True)
        shared = len(energies) - len(ranked)
        add("Most energy-hungry commits (direct, + lead-up):")
        for e in ranked[:3]:
            extra = f" +{_fmt_wh(e.lead_up)}" if e.lead_up.high > 0 else ""
            add(f"  {_fmt_wh(e.wh):>24}{extra}  {Path(e.task.repo).name} {e.task.sha[:7]} {e.task.subject[:45]}")
        if len(ranked) > 3:
            add("Leanest commits:")
            for e in ranked[-3:]:
                add(f"  {_fmt_wh(e.wh):>24}  {Path(e.task.repo).name} {e.task.sha[:7]} {e.task.subject[:50]}")
        if shared:
            add(f"  ({shared} commits were made right after another with no AI requests in between)")
        add("")

    stale = [Path(repo).name for repo, info in attribution.repos.items() if info.error]
    if stale:
        add(f"Git problems (statuses unknown): {', '.join(sorted(stale))}")
    add("")
    add(
        f"Energy is a low–high range (factors v{load_factors()['version']}): model sizes are not disclosed, "
        f"so classes are assumptions. Method: {METHOD_URL}"
    )
    return "\n".join(lines) + "\n"
