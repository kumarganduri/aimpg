"""Personal, measured tips: what would have saved energy and money on YOUR logs.

Each tip is a counterfactual computed with the same energy and price code as
the receipt, so it's never generic advice:

1. Old conversation re-read. Every request re-reads the session's whole
   context. After a commit, most of that history is no longer needed. If the
   session had restarted after each commit, the context carried over from
   before that commit would not be re-read:

       session:  [base] … c1 … c2 …
       request after c2 re-reads:  base + (everything up to c2) + new work
       fresh session after c2:     base + new work

   The carried-over part is removed from that request's cache reads (and from
   the context each output token attends over). It's an upper bound on the
   saving: some of that history is genuinely useful to the next task.

2. Model size. Requests on large-class models (Opus, Fable) re-priced and
   re-estimated as if they had run on a mid-class model (Sonnet). Also an upper
   bound, because quality may differ; `aimpg replay` measures that on your code.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from dataclasses import dataclass, replace

from aimpg.attribution import Attribution
from aimpg.cost import usage_cost
from aimpg.energy import ZERO, WhRange, model_class, request_wh
from aimpg.model import Request

MID_MODEL = "claude-sonnet-5-5"


@dataclass
class Tip:
    title: str
    measured: str  # what the logs show
    action: str  # what to do
    saving_wh: WhRange
    saving_usd: float
    share: float  # of the window's energy (mid estimate)


def _sum(requests: list[Request]) -> tuple[WhRange, float]:
    wh, usd = ZERO, 0.0
    for r in requests:
        wh = wh + request_wh(r)
        usd += usage_cost(r.usage, r.model) or 0.0
    return wh, usd


def fresh_session_tip(requests: list[Request], attribution: Attribution) -> Tip | None:
    commits_by_session: dict[str, list[float]] = defaultdict(list)
    for task in attribution.tasks:
        if task.attribution == "exact":
            for r in {r.session_id for r in task.requests}:
                commits_by_session[r].append(task.ts)
    by_session: dict[str, list[Request]] = defaultdict(list)
    for r in requests:
        by_session[r.session_id].append(r)

    actual, simulated = [], []
    for session, reqs in by_session.items():
        reqs.sort(key=lambda r: r.ts)
        main = [r for r in reqs if not r.is_sidechain]
        if not main:
            continue
        base = min(r.usage.ctx_len for r in main[:3])  # system prompt + tools at the start
        anchors = sorted(commits_by_session.get(session, []))
        # context size right after each commit = first main request after it
        carried_at = []
        for t in anchors:
            after = next((r for r in main if r.ts > t), None)
            carried_at.append(max(0, after.usage.ctx_len - base) if after else 0)
        for r in reqs:
            actual.append(r)
            i = bisect.bisect_right(anchors, r.ts) - 1
            if i < 0 or r.is_sidechain:
                simulated.append(r)
                continue
            cut = min(carried_at[i], r.usage.cache_read)
            simulated.append(replace(r, usage=replace(r.usage, cache_read=r.usage.cache_read - cut)))
    (a_wh, a_usd), (s_wh, s_usd) = _sum(actual), _sum(simulated)
    saved = WhRange(a_wh.low - s_wh.low, a_wh.high - s_wh.high)
    if a_wh.mid == 0 or saved.mid / a_wh.mid < 0.02:
        return None
    share = saved.mid / a_wh.mid
    return Tip(
        title="Start a fresh session after each commit",
        measured=f"{share:.0%} of your AI energy went to re-reading conversation from before your last commit",
        action="After committing, start a new session (or /clear) for the next task, and give it a one-line summary if needed",
        saving_wh=saved,
        saving_usd=a_usd - s_usd,
        share=share,
    )


def model_size_tip(requests: list[Request]) -> Tip | None:
    large = [r for r in requests if model_class(r.model)[0] == "large"]
    if not large:
        return None
    (all_wh, _), (l_wh, l_usd) = _sum(requests), _sum(large)
    as_mid = [replace(r, model=MID_MODEL) for r in large]
    m_wh, m_usd = _sum(as_mid)
    saved = WhRange(l_wh.low - m_wh.low, l_wh.high - m_wh.high)
    share_large = l_wh.mid / all_wh.mid if all_wh.mid else 0.0
    return Tip(
        title="Use a mid-size model for routine work",
        measured=f"{share_large:.0%} of your AI energy ran on the largest models (Opus/Fable class)",
        action="Try Sonnet for routine fixes; check it's good enough on your own commits with `aimpg replay` (model picker)",
        saving_wh=saved,
        saving_usd=l_usd - m_usd,
        share=saved.mid / all_wh.mid if all_wh.mid else 0.0,
    )


def tips(requests: list[Request], attribution: Attribution) -> list[Tip]:
    found = [t for t in (fresh_session_tip(requests, attribution), model_size_tip(requests)) if t]
    return sorted(found, key=lambda t: t.saving_usd, reverse=True)
