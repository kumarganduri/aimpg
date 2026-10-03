"""Share results: a CSV for teams and sustainability reports, and a pull-request summary.

The AI logs live on each developer's machine, so both run locally:

    aimpg export --csv energy.csv        one row per commit; teams concatenate files
    aimpg pr [--base main] [--post]       markdown for the current branch's commits;
                                          --post adds it to the open PR via `gh`
"""

from __future__ import annotations

import csv
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from aimpg import equivalence
from aimpg.attribution import Attribution
from aimpg.energy import ZERO
from aimpg.receipt import TaskEnergy, _fmt_wh, _usd, task_energy

CSV_FIELDS = [
    "date", "repo", "commit", "author", "subject", "status", "matched_by", "ai_requests",
    "energy_wh_low", "energy_wh_high", "lead_up_wh_low", "lead_up_wh_high",
    "co2_g_low", "co2_g_high", "usd_api_equivalent", "lead_up_usd",
]


def csv_rows(attribution: Attribution, *, subjects: bool = True, authors: bool = True) -> list[dict]:
    g = equivalence.load()["co2_g_per_kwh"]["value"]
    rows = []
    for e in task_energy(attribution.tasks):
        t = e.task
        total = e.total
        rows.append({
            "date": datetime.fromtimestamp(t.ts, tz=timezone.utc).strftime("%Y-%m-%d"),
            "repo": Path(t.repo).name,
            "commit": t.sha,
            "author": t.author if authors else "",
            "subject": t.subject if subjects else "",
            "status": t.status,
            "matched_by": t.attribution,
            "ai_requests": len(t.requests) + len(t.lead_up),
            "energy_wh_low": round(e.wh.low, 3),
            "energy_wh_high": round(e.wh.high, 3),
            "lead_up_wh_low": round(e.lead_up.low, 3),
            "lead_up_wh_high": round(e.lead_up.high, 3),
            "co2_g_low": round(total.low / 1000 * g, 2),
            "co2_g_high": round(total.high / 1000 * g, 2),
            "usd_api_equivalent": round(e.usd, 4),
            "lead_up_usd": round(e.lead_up_usd, 4),
        })
    return sorted(rows, key=lambda r: (r["date"], r["repo"]))


def write_csv(rows: list[dict], path: Path) -> None:
    with open(path, "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def branch_commits(repo: Path, base: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(repo), "rev-list", f"{base}..HEAD"], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"git rev-list {base}..HEAD failed")
    return out.stdout.split()


def pr_markdown(attribution: Attribution, shas: list[str], repo_name: str) -> str:
    wanted = set(shas)
    mine: list[TaskEnergy] = [e for e in task_energy(attribution.tasks) if e.task.sha in wanted]
    lines = ["### ⚡ AI energy for this pull request", ""]
    if not mine:
        lines.append(f"No AI-assisted work found for the {len(shas)} commits on this branch (from this machine's Claude Code logs).")
        return "\n".join(lines) + "\n"
    total = sum((e.total for e in mine), ZERO)
    usd = sum(e.usd + e.lead_up_usd for e in mine)
    lines += [
        f"**{_fmt_wh(total)}** ≈ {equivalence.phrase(total, 'phone')} · {equivalence.co2(total)} · **{_usd(usd)}** API-equivalent",
        "",
        f"| commit | energy | ≈ | $ |",
        "|---|---|---|---|",
    ]
    for e in sorted(mine, key=lambda e: e.task.ts):
        name = f"`{e.task.sha[:7]}` {e.task.subject[:60]}"
        if e.total.high == 0:  # committed right after another, no AI work in between
            lines.append(f"| {name} | — | made together with the commit above | — |")
        else:
            lines.append(f"| {name} | {_fmt_wh(e.total)} | {equivalence.phrase(e.total, 'phone')} | {_usd(e.usd + e.lead_up_usd)} |")
    with_ai = sum(1 for e in mine if e.total.high > 0)
    lines += [
        "",
        f"<sub>{with_ai} of {len(shas)} commits had their own AI work · measured locally by [aimpg](https://github.com/kumarganduri/aimpg) "
        "from Claude Code logs; energy is a range because model sizes aren't public.</sub>",
    ]
    return "\n".join(lines) + "\n"


def post_comment(markdown: str, repo: Path) -> None:
    proc = subprocess.run(["gh", "pr", "comment", "--body-file", "-"], cwd=repo, input=markdown, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "gh pr comment failed (is there an open PR for this branch, and is gh logged in?)")
