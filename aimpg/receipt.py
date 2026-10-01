"""Turn an Attribution into the printed fuel receipt."""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from aimpg.attribution import NO_COMMIT_YET, Attribution
from aimpg.energy import ZERO, WhRange, load_factors, model_class, request_wh, total_wh, weighted_wh
from aimpg.gitkept import Status
from aimpg.model import ParseResult, Request, Task

STATUS_ORDER = [s.value for s in Status] + ["git error"]
KEPT = {Status.KEPT.value, Status.KEPT_SQUASH.value}


@dataclass
class TaskEnergy:
    task: Task
    wh: WhRange  # direct: the final work burst before the commit
    lead_up: WhRange  # earlier bursts since the previous commit

    @property
    def total(self) -> WhRange:
        return self.wh + self.lead_up


def task_energy(tasks: list[Task]) -> list[TaskEnergy]:
    return [
        TaskEnergy(t, weighted_wh(t.requests, t.weights), weighted_wh(t.lead_up, t.lead_up_weights))
        for t in tasks
    ]


def first_n_share(requests: list[Request], n: int = 10) -> float:
    """Share of energy (geometric-mid) spent in the first n requests of each session."""
    by_session: dict[str, list[Request]] = defaultdict(list)
    for r in requests:
        by_session[r.session_id].append(r)
    first = total = 0.0
    for group in by_session.values():
        group.sort(key=lambda r: r.ts)
        mids = [request_wh(r).mid for r in group]
        first += sum(mids[:n])
        total += sum(mids)
    return 0.0 if total == 0 else first / total


def _fmt_wh(r: WhRange) -> str:
    def one(x: float) -> str:
        return f"{x / 1000:.2f} kWh" if x >= 1000 else f"{x:.1f} Wh" if x >= 1 else f"{x * 1000:.0f} mWh"

    return f"{one(r.low)} – {one(r.high)}"


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def render(parsed: ParseResult, attribution: Attribution, since: float, now: float) -> str:
    in_window = [r for r in parsed.requests if r.ts >= since]
    energies = task_energy(attribution.tasks)
    attributed = sum((e.total for e in energies), ZERO)
    lead_up = sum((e.lead_up for e in energies), ZERO)
    everything = total_wh(in_window)
    lines: list[str] = []
    add = lines.append

    add(f"aimpg receipt · {_day(since)} → {_day(now)}")
    add("")
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
        add(f"    {'of which lead-up (work before a 2h+ break) ':.<42} {_fmt_wh(lead_up)}")
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

    add(f"Energy in the first 10 requests of each session: {first_n_share(in_window):.0%}")
    stale = [Path(repo).name for repo, info in attribution.repos.items() if info.error]
    if stale:
        add(f"Git problems (statuses unknown): {', '.join(sorted(stale))}")
    add("")
    add(
        f"Energy is a low–high range (factors v{load_factors()['version']}): model sizes are not disclosed, "
        "so classes are assumptions. Method: docs/designs/aimpg-design.md"
    )
    return "\n".join(lines) + "\n"
