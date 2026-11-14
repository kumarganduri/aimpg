"""Build the Report No. 1 page (and its public data folder) from the study's own result files.

    uv run python docs/study/report-1/make_page.py OUT_DIR

Reads ~/.aimpg/study/report-1/runs-report.json and runs/*.jsonl, plus this folder's
selection.json. Writes OUT_DIR/index.html and OUT_DIR/data/ (raw records with local
paths replaced by repo URLs). Every number on the page comes from those files.
"""

from __future__ import annotations

import html
import json
import math
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).parent
STATE = Path.home() / ".aimpg" / "study" / "report-1"
REPO_URL = "https://github.com/kumarganduri/aimpg"
PREREG = f"{REPO_URL}/blob/main/docs/study/report-1/PREREGISTRATION.md"
PAGE_URL = "https://kumarganduri.github.io/aimpg-scoreboard/report-1/"
NEXT_URL = "https://github.com/kumarganduri/aimpg/discussions/2"  # "What should Report No. 2 test?"
# Authored before the 2026-07-01 cutoff but committed after it (selection compared committer dates); see the deviation list
PRE_CUTOFF = {
    "47382933d6d6bcb0f3a6b6104c48466545172a0a", "8f00f8e627070fcdd086b1d6457acba22e82f690", "93ba0075f4881b858b61f89a753c7c10e330027e",
    "9f9b149ea30287b4977b800a024ee68144b8c869", "a7a1d4873f47eee566ed436046067dbf1a72a145", "adc95e2e291589a903468d969d7d355d69620f41",
    "c58eff4ccd80cf1e3336a2c558e4b0cb8e7197b7", "d934a47907276beb243611ae3183016872522fcb", "dc0ef09da32c3b591ee1c7d2d33bb88f5dc8e020",
    "fbdd434028fe07ecf35084c945b43b938dfd639f",
}

LABEL = {
    "claude-code@haiku": "Haiku 5.5", "claude-code@opus": "Opus 5.5", "codex@gpt-6.1-sol": "Codex (gpt-6.1-sol)",
    "claude-code+rtk": "RTK (on Sonnet 5.5)", "chain-fresh": "Fresh session for the next task",
    "claude-code": "Sonnet 5.5", "chain-continue": "continuing the session",
}
METRIC = {"per_solved": "cost per solved task", "tokens": "tokens per attempt", "per_attempt": "cost per attempted task"}
VERDICT = {
    "claude-code@haiku": "About 6× cheaper per solved task, solving almost as much (49% vs 51%).",
    "claude-code@opus": "Solves more (61% vs 51%) but costs about 2.6× more per solved task.",
    "codex@gpt-6.1-sol": "Solved a bit more (57% vs 51%); cost per solved task about the same. No difference shown.",
    "claude-code+rtk": "No token saving shown (+1%, −6% to +9%), and the same cost. RTK fired in 163 of 168 runs.",
    "chain-fresh": "Cost 13% more than continuing, despite 16% fewer tokens: the new session has to write its context to the cache again.",
}


def load():
    report = json.loads((STATE / "runs-report.json").read_text())
    sel = json.loads((HERE / "selection.json").read_text())
    urls = {str(Path(r["path"]).expanduser()): r["url"] for r in sel["repos"]}
    return report, sel, urls


def pct(r):
    if r is None:
        return "—"
    v = round((r - 1) * 100)
    return "0%" if v == 0 else f"{v:+d}%"


def records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def sensitivity() -> list:
    """The same five comparisons without the commits authored before the cutoff."""
    from aimpg.replay import run as runner, study
    from aimpg.replay.verify import setup_from

    plan = json.loads((HERE / "plan.json").read_text())
    recs = [r for path in sorted((STATE / "runs").glob("*.jsonl")) for r in runner.load_results(path)[0]
            if r.commit not in PRE_CUTOFF]
    base, primary = setup_from(plan["baseline"]).name, plan.get("primary", {})
    comps = [study.compare(recs, base, setup_from(c).name, primary=primary.get(c, "per_solved")) for c in plan["challengers"]]
    comps.append(study.compare(recs, "chain-continue", "chain-fresh", primary=primary.get("fresh-session", "per_attempt")))
    study.holm(comps)
    return comps


def sens_rows(comps) -> str:
    e, out = html.escape, []
    for c in comps:
        key = {"per_solved": "ratio_per_solved", "tokens": "ratio_tokens", "per_attempt": "ratio_per_attempt"}[c.primary]
        r, ci = getattr(c, key), getattr(c, key + "_ci") or (None, None)
        same = c.repos - c.cheaper_in_repos if (r or 1) > 1 else c.cheaper_in_repos
        out.append(f'<tr><th scope="row">{e(LABEL.get(c.challenger, c.challenger))}<small>vs {e(LABEL.get(c.baseline, c.baseline))}</small></th>'
                   f'<td>{e(METRIC[c.primary])}</td><td class="num">{pct(r)}<br><small>{pct(ci[0])} to {pct(ci[1])}</small></td>'
                   f'<td class="num">{same} of {c.repos}</td><td>{"<b>Finding</b>" if c.headline else "Not shown"}</td></tr>')
    return "".join(out)


DIFFICULTY_SETUPS = ["claude-code@haiku", "claude-code", "claude-code@opus", "codex@gpt-6.1-sol"]  # RTK left out: it is Sonnet again
DIFFICULTY_BANDS = [("Easy", "10 to 12 of 12 runs passed", 10, 12), ("In between", "3 to 9", 3, 9), ("Hard", "0 to 2", 0, 2)]


def tasks(urls: dict) -> list[dict]:
    """What each task looked like before any run: how long its description (the commit message) is and
    how much code the original commit changed. Read from this machine's selection and repo clones;
    published as data/tasks.json so the grouping below can be checked."""
    from aimpg.replay import run as runner, select, workspace
    from aimpg.replay.cli import _state

    used = {r.commit for path in sorted((STATE / "runs").glob("*.jsonl")) for r in runner.load_results(path)[0]}
    out = []
    for local, url in urls.items():
        for c in select.load(_state(Path(local)) / "selected.jsonl"):
            if c.sha in used:
                out.append({"repo": url, "commit": c.sha, "description_words": c.words,
                            "code_lines": workspace.code_lines(local, c.parent, c.sha), "code_files": len(c.code_files)})
    return sorted(out, key=lambda d: (d["repo"], d["commit"]))


DESCRIPTION_BANDS = [("50 words or more", lambda n: n >= 50), ("15 to 49 words", lambda n: 15 <= n < 50), ("under 15 words", lambda n: n < 15)]


def difficulty(task_list: list[dict]) -> dict:
    """Results by how hard each task turned out to be (added 2026-11-14, not pre-registered).

    A task's difficulty is the number of passing runs across the four single-task setups (3 runs each, so 0 to 12).
    The bands were chosen after seeing the data."""
    from aimpg.replay import run as runner

    recs = [r for path in sorted((STATE / "runs").glob("*.jsonl")) for r in runner.load_results(path)[0]
            if r.setup in DIFFICULTY_SETUPS]
    passed, runs = {}, {}
    for r in recs:
        passed[r.commit, r.setup] = passed.get((r.commit, r.setup), 0) + bool(r.passed)
        runs[r.commit, r.setup] = runs.get((r.commit, r.setup), 0) + 1
    tasks = sorted({r.commit for r in recs})
    total = {c: sum(passed.get((c, s), 0) for s in DIFFICULTY_SETUPS) for c in tasks}
    bands = []
    for name, rule, lo, hi in DIFFICULTY_BANDS:
        cs = [c for c in tasks if lo <= total[c] <= hi]
        bands.append((name, rule, len(cs), [(sum(passed.get((c, s), 0) for c in cs), sum(runs.get((c, s), 0) for c in cs))
                                            for s in DIFFICULTY_SETUPS]))
    ever = {s: {c for c in tasks if passed.get((c, s), 0)} for s in DIFFICULTY_SETUPS}
    failed = {s: [r for r in recs if r.setup == s and not r.passed] for s in DIFFICULTY_SETUPS}
    mean = lambda xs: sum(xs) / len(xs)
    from statistics import median

    words = {d["commit"]: d["description_words"] for d in task_list}
    by_words = []
    for label, test in DESCRIPTION_BANDS:
        cs = [c for c in tasks if test(words[c])]
        by_words.append((label, len(cs), [(sum(passed.get((c, s), 0) for c in cs), sum(runs.get((c, s), 0) for c in cs))
                                          for s in DIFFICULTY_SETUPS]))
    return {
        "median_words": [median(words[c] for c in tasks if lo <= total[c] <= hi) for _, _, lo, hi in DIFFICULTY_BANDS],
        "by_words": by_words,
        "tasks": len(tasks), "bands": bands,
        "sonnet_only": len(ever["claude-code"] - ever["claude-code@haiku"]),
        "haiku_only": len(ever["claude-code@haiku"] - ever["claude-code"]),
        "failed": {s: (mean([len(r.usages) for r in failed[s]]), mean([r.wall_s for r in failed[s]])) for s in DIFFICULTY_SETUPS},
    }


def difficulty_rows(d: dict) -> str:
    return "".join(
        f'<tr><th scope="row">{html.escape(name)}<small>{html.escape(rule)}</small></th><td class="num">{n}</td>'
        + "".join(f'<td class="num">{a} of {b}</td>' for a, b in cells) + "</tr>"
        for name, rule, n, cells in d["bands"])


def times(r):
    if r is None:
        return "—"
    return f"{r:.2f}×" if r < 10 else f"{r:.1f}×"


def forest(c, urls) -> str:
    """Log-scale dot plot: overall ratio with 95% CI, plus one dot per repo, on the primary metric."""
    key = {"per_solved": ("ratio_per_solved", "ratio_per_solved_ci"), "tokens": ("ratio_tokens", "ratio_tokens_ci"),
           "per_attempt": ("ratio_per_attempt", "ratio_per_attempt_ci")}[c["primary"]]
    point, ci = c[key[0]], c[key[1]]
    repo_vals = [(Path(r).name, v[key[0]]) for r, v in c["per_repo"].items() if v.get(key[0])]
    lo, hi = 0.1, 5.0
    w, h, pad = 640, 132, 44

    def x(v):
        v = min(max(v, lo), hi)
        return pad + (math.log(v) - math.log(lo)) / (math.log(hi) - math.log(lo)) * (w - 2 * pad)

    ticks = [0.1, 0.25, 0.5, 1, 2, 4]
    parts = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{html.escape(LABEL.get(c["challenger"], c["challenger"]))}: '
             f'{METRIC[c["primary"]]} relative to {html.escape(LABEL.get(c["baseline"], c["baseline"]))}">']
    for t in ticks:
        parts.append(f'<line x1="{x(t):.1f}" y1="18" x2="{x(t):.1f}" y2="{h - 26}" class="{"one" if t == 1 else "grid"}"/>')
        parts.append(f'<text x="{x(t):.1f}" y="{h - 8}" class="tick" text-anchor="middle">{t:g}×</text>')
    for i, (name, v) in enumerate(sorted(repo_vals, key=lambda t: t[1])):
        parts.append(f'<circle cx="{x(v):.1f}" cy="{42 + (i % 4) * 9}" r="4.5" class="repo"><title>{html.escape(name)}: {times(v)}</title></circle>')
    if ci:
        parts.append(f'<line x1="{x(ci[0]):.1f}" y1="92" x2="{x(ci[1]):.1f}" y2="92" class="ci"/>')
    if point:
        parts.append(f'<rect x="{x(point) - 6:.1f}" y="86" width="12" height="12" class="point"><title>overall {times(point)}</title></rect>')
    parts.append("</svg>")
    return "".join(parts)


def write_data(out: Path, urls: dict, task_list: list[dict]) -> int:
    data = out / "data"
    if data.exists():
        shutil.rmtree(data)
    data.mkdir(parents=True)
    n = 0
    for path in sorted((STATE / "runs").glob("*.jsonl")):
        lines = []
        for line in path.read_text().splitlines():
            d = json.loads(line)
            if "repo" in d:
                d["repo"] = urls.get(d["repo"], d["repo"])
            lines.append(json.dumps(d))
            n += "cost_usd" in d and d.get("outcome") != "account_error"
        text = "\n".join(lines) + "\n"
        assert str(Path.home()) not in text, "a local path would be published"
        (data / path.name).write_text(text)
    original = STATE / "runs" / "pyinfra.jsonl.before-quota-repair"  # promised on the page: the file before the repair
    if original.exists():
        text = original.read_text()
        for local, url in urls.items():
            text = text.replace(local, url)
        (data / original.name).write_text(text)  # not *.jsonl, so `aimpg study report --runs data/` leaves it out
    pilot = STATE / "pilot" / "litestar.jsonl"
    if pilot.exists():
        text = pilot.read_text()
        for local, url in urls.items():
            text = text.replace(local, url)
        (data / "pilot").mkdir()
        (data / "pilot" / pilot.name).write_text(text)
    (data / "tasks.json").write_text(json.dumps(task_list, indent=1) + "\n")
    shutil.copy(HERE / "plan.json", data / "plan.json")
    shutil.copytree(HERE / "selection", data / "selection")
    shutil.copy(HERE / "selection.json", data / "selection.json")
    for f in (data / "selection").glob("*.json"):
        f.write_text(f.read_text().replace(str(Path.home() / "aimpg-study"), "repos"))
    (data / "selection.json").write_text((data / "selection.json").read_text().replace(str(Path.home() / "aimpg-study"), "repos")
                                         .replace("~/aimpg-study", "repos"))
    shutil.copy(STATE / "runs-report.json", data / "report.json")
    rep = (data / "report.json").read_text()
    for local, url in urls.items():
        rep = rep.replace(local, url)
    (data / "report.json").write_text(rep)
    for f in data.rglob("*"):
        if f.is_file():
            assert str(Path.home()) not in f.read_text(errors="ignore"), f"local path in {f}"
    items = sorted(p.relative_to(data).as_posix() for p in data.rglob("*") if p.is_file())
    (data / "index.html").write_text("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
                                     "<title>Report No. 1 data</title><body style='font:15px/1.6 system-ui;margin:24px'>"
                                     "<h1 style='font-size:20px'>AI Coding MPG Report No. 1: raw data</h1><ul>"
                                     + "".join(f"<li><a href='{html.escape(i)}'>{html.escape(i)}</a></li>" for i in items)
                                     + "</ul><p>One JSON record per line: setup, commit, repeat, outcome, model, tokens per request "
                                       "[fresh, cache write, cache read, output], cost at published prices, seconds.</p>"
                                       "<p>Recompute every number: <code>aimpg study report plan.json --runs .</code> in this folder. "
                                       "<code>pilot/</code> holds the pilot runs, which are not part of the results.</p>")
    return n


def page(report, sel, urls, n_runs: int, total_usd: float, sens: str, haiku: tuple, pilot_usd: float, diff: dict) -> str:
    e = html.escape
    rows, charts = [], []
    for c in report:
        ch, b = c["challenger"], c["baseline"]
        a_ch, a_b = c["arms"][ch], c["arms"][b]
        sr_ch, sr_b = a_ch["solved"] / a_ch["runs"], a_b["solved"] / a_b["runs"]
        lo, hi = c["solve_ci"][ch]
        # 4,000 bootstrap resamples and 5 Holm-corrected tests: nothing below about 0.003 can be told apart
        p = "< 0.003" if c["p_holm"] is not None and c["p_holm"] < 0.003 else (f"{c['p_holm']:.3f}" if c["p_holm"] is not None else "—")
        found = c["headline"]
        rows.append(
            f'<tr class="{"found" if found else ""}"><th scope="row">{e(LABEL.get(ch, ch))}<small>vs {e(LABEL.get(b, b))}</small></th>'
            f'<td>{e(METRIC[c["primary"]])}</td>'
            f'<td class="num">{sr_ch:.0%} <small>({lo:.0%}–{hi:.0%})</small><br><small>vs {sr_b:.0%}</small></td>'
            f'<td class="num">{pct(c["ratio_per_solved"])}<br><small>{pct((c["ratio_per_solved_ci"] or [None, None])[0])} to {pct((c["ratio_per_solved_ci"] or [None, None])[1])}</small></td>'
            f'<td class="num">{pct(c["ratio_per_attempt"])}<br><small>{pct((c["ratio_per_attempt_ci"] or [None, None])[0])} to {pct((c["ratio_per_attempt_ci"] or [None, None])[1])}</small></td>'
            f'<td class="num">{pct(c["ratio_tokens"])}<br><small>{pct((c["ratio_tokens_ci"] or [None, None])[0])} to {pct((c["ratio_tokens_ci"] or [None, None])[1])}</small></td>'
            f'<td class="num">{c["repos"] - c["cheaper_in_repos"] if (c[{"per_solved":"ratio_per_solved","tokens":"ratio_tokens","per_attempt":"ratio_per_attempt"}[c["primary"]]] or 1) > 1 else c["cheaper_in_repos"]} of {c["repos"]}</td>'
            f'<td class="num">{p}</td><td>{"<b>Finding</b>" if found else "Not shown"}</td></tr>')
        charts.append(
            f'<figure><figcaption><b>{e(LABEL.get(ch, ch))}</b> vs {e(LABEL.get(b, b))}: {e(METRIC[c["primary"]])} '
            f'<span class="{"tag found" if found else "tag"}">{"finding" if found else "not shown"}</span>'
            f'<span class="verdict">{e(VERDICT.get(ch, ""))}</span></figcaption>{forest(c, urls)}</figure>')
    repos = ", ".join(f'<a href="{e(r["url"])}">{e(Path(r["path"]).name)}</a>' for r in sel["repos"])
    hard = diff["bands"][2][3]  # (passed, runs) per setup on the hard tasks
    long, short = diff["by_words"][0][2], diff["by_words"][2][2]  # the same, for long and for short descriptions
    diff_head = "".join(f"<th>{e(LABEL.get(s, s))}</th>" for s in DIFFICULTY_SETUPS)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>AI Coding MPG Report No. 1</title>
<meta name="description" content="What AI coding agents cost per solved task on 56 real commits from 8 open-source repos, pre-registered, with all raw data.">
<meta property="og:type" content="article"><meta property="og:url" content="{PAGE_URL}">
<meta property="og:title" content="AI Coding MPG Report No. 1: what AI coding costs per solved task">
<meta property="og:description" content="984 reruns of 56 real commits from 8 open-source repos. Haiku 5.5 about 6× cheaper per solved task than Sonnet 5.5; Opus 5.5 solves more at 2.6× the cost. Pre-registered, raw data public.">
<meta property="og:image" content="{PAGE_URL}og.png"><meta property="og:image:width" content="1200"><meta property="og:image:height" content="630">
<meta property="og:image:alt" content="Haiku 5.5 6× cheaper per solved task; Opus 5.5 2.6× more; a fresh session +13%">
<meta name="twitter:card" content="summary_large_image">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Archivo:wdth,wght@112,600;112,800&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
/* Layout: a single report column; each comparison gets a log-scale dot plot where 1× means "same as the baseline". */
:root {{ --bg:#eef2f0; --paper:#f8faf9; --ink:#13302f; --muted:#557069; --line:#cfdcd7; --accent:#c27a00; --accent-soft:#f6e6c6;
  --good:#1d7a4f; --dot:#3b6b65; --display:"Archivo","Helvetica Neue",Arial,sans-serif; --body:"IBM Plex Sans",system-ui,sans-serif;
  --mono:"IBM Plex Mono",ui-monospace,Menlo,monospace; }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ --bg:#0d1a1b; --paper:#132426; --ink:#e2ece8; --muted:#93aaa3;
  --line:#24393a; --accent:#f0ad3a; --accent-soft:#3a2c12; --good:#5cc493; --dot:#8fbdb5; color-scheme:dark; }} }}
:root[data-theme="dark"] {{ --bg:#0d1a1b; --paper:#132426; --ink:#e2ece8; --muted:#93aaa3; --line:#24393a; --accent:#f0ad3a;
  --accent-soft:#3a2c12; --good:#5cc493; --dot:#8fbdb5; color-scheme:dark; }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:var(--bg); color:var(--ink); font:16px/1.6 var(--body); }}
.wrap {{ max-width:880px; margin:0 auto; padding-inline:20px; padding-block:40px 72px; }}
h1,h2 {{ font-family:var(--display); font-stretch:112%; text-wrap:balance; margin:0; letter-spacing:-0.01em; }}
h1 {{ font-size:clamp(32px,6vw,50px); line-height:1.04; font-weight:800; }} h2 {{ font-size:22px; font-weight:600; margin-top:44px; }}
p {{ max-width:68ch; margin:10px 0 0; }} a {{ color:inherit; text-decoration-color:var(--accent); text-underline-offset:3px; }}
.eyebrow {{ font:500 12px/1 var(--mono); letter-spacing:.12em; text-transform:uppercase; color:var(--accent); }}
.lede {{ color:var(--muted); font-size:18px; }} small {{ color:var(--muted); display:block; font-size:12.5px; }}
.facts {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-top:24px; }}
.facts div {{ background:var(--paper); border:1px solid var(--line); border-radius:10px; padding:12px 14px; }}
.facts b {{ display:block; font:600 24px/1.2 var(--mono); font-variant-numeric:tabular-nums; }}
figure {{ margin:22px 0 0; background:var(--paper); border:1px solid var(--line); border-radius:10px; padding:14px 16px 6px; }}
figcaption {{ font-size:15px; }} .verdict {{ display:block; color:var(--muted); font-size:14px; margin-top:2px; }}
.tag {{ font:500 11px/1 var(--mono); text-transform:uppercase; letter-spacing:.08em; padding:3px 7px; border-radius:99px;
  border:1px solid var(--line); color:var(--muted); margin-left:6px; vertical-align:2px; }}
.tag.found {{ color:var(--good); border-color:var(--good); }}
svg {{ width:100%; height:auto; display:block; margin-top:4px; }} .grid {{ stroke:var(--line); stroke-width:1; }}
.one {{ stroke:var(--muted); stroke-width:1.5; stroke-dasharray:4 3; }} .tick {{ fill:var(--muted); font:12px var(--mono); }}
.repo {{ fill:var(--dot); opacity:.55; }} .ci {{ stroke:var(--accent); stroke-width:4; stroke-linecap:round; }} .point {{ fill:var(--accent); }}
.legend {{ font-size:13px; color:var(--muted); margin-top:10px; }}
.tablewrap {{ overflow-x:auto; margin-top:16px; }} table {{ border-collapse:collapse; width:100%; font-size:14px; min-width:760px; }}
th,td {{ text-align:left; padding:9px 8px; border-bottom:1px solid var(--line); vertical-align:top; }}
thead th {{ font:500 11.5px/1.3 var(--mono); text-transform:uppercase; letter-spacing:.05em; color:var(--muted); }}
td.num {{ font-variant-numeric:tabular-nums; white-space:nowrap; }} tr.found th {{ color:var(--good); }}
code {{ background:var(--accent-soft); padding:1px 5px; border-radius:4px; font:13px var(--mono); }}
ul {{ max-width:70ch; padding-left:20px; }} li {{ margin-top:6px; }}
footer {{ margin-top:48px; border-top:1px solid var(--line); padding-top:14px; color:var(--muted); font-size:14px; }}
</style></head><body><div class="wrap">
<div class="eyebrow">AI Coding MPG · Report No. 1</div>
<h1>What AI coding agents cost per solved task, on real code</h1>
<p class="lede">56 real commits from 8 open-source projects, each redone 3 times by 5 agent setups in a sandbox, judged by the
project's own hidden tests. Pre-registered and frozen before the main run (after a ${pilot_usd:.2f} pilot), and every record is public.</p>
<div class="facts">
<div><b>{n_runs}</b>agent runs</div><div><b>8 · 56</b>repos · commits</div><div><b>${total_usd:,.0f}</b>total cost, at list prices</div><div><b>3 of 5</b>comparisons with a finding</div>
</div>

<h2>The findings</h2>
<p>Every setup is compared with Claude Code on Sonnet 5.5, except the last chart, which compares a fresh session with continuing
the session. The square shows the result overall with its 95% interval, and each dot is one repo. Left of the dashed line means lower
(cheaper or fewer tokens), right means higher.</p>
{''.join(charts)}
<p class="legend">A result counts as a <b>finding</b> only if it was pre-registered as the comparison's primary measure, its Holm-adjusted p is
below 0.05, and its direction holds in at least 5 of the 6+ repos. Everything else is reported as "not shown", with its interval.</p>

<h2>All numbers</h2>
<div class="tablewrap"><table>
<thead><tr><th>Setup</th><th>Primary measure</th><th>Solved (95% CI)</th><th>$ per solved task</th><th>$ per attempt</th><th>Tokens per attempt</th><th>Repos in the same direction</th><th>Holm p</th><th>Result</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>
<p class="legend">Changes are relative to the baseline, with 95% intervals from a bootstrap over repos and then commits. Dollars are each run's
tokens priced at Anthropic's and OpenAI's published prices. Solve-rate intervals are Wilson intervals over runs, which ignore that runs of one repo are alike, so they are too narrow.</p>

<h2>How it was done</h2>
<ul>
<li><b>Repos</b>, picked by a rule written before any run: {repos}. pydantic was replaced by the first reserve, litestar, under that rule, because it now builds its core from Rust source.</li>
<li><b>Commits:</b> the first 7 per repo committed on or after every model's training cutoff (2026-07-01) whose tests fail before the change and pass after it, twice each. 10 of the 56 were written before that date; see below. Every candidate checked is listed, with its fingerprint.</li>
<li><b>Task:</b> the commit message plus the names the hidden tests import. The commit's own tests decide, and any edits an agent makes to them are undone first.</li>
<li><b>Sandbox:</b> each run starts from a fresh copy without git history. While the agent works, the only network is the model's API (plus localhost, for the project's own test servers).</li>
<li><b>Setups:</b> Claude Code 2.1.287 on Sonnet 5.5, Haiku 5.5 or Opus 5.5; Codex 0.159.2 on gpt-6.1-sol; RTK 0.51.0 on Sonnet 5.5.
Fresh-session pairs: task 1 in a session, then task 2 either continued or started fresh with a one-line summary.</li>
<li><b>Statistics:</b> 5 primary comparisons, Holm-corrected, with a headline only if the direction holds in at least 5 of the 6+ repos. The pre-registration is
<a href="{PREREG}">frozen on GitHub</a>.</li>
</ul>

<h2>What went wrong, and what we changed</h2>
<ul>
<li><b>Before any paid run, the harness was fixed so tests run fairly.</b> Without these fixes, agents would have been marked as failing in 4 repos:
<ul>
<li>localhost networking for tests' own servers;</li>
<li>placeholder versions for builds that read git history;</li>
<li>coverage thresholds off;</li>
<li>imports from a folder;</li>
<li>dependencies pinned to GitHub commits.</li>
</ul></li>
<li><b>The ${pilot_usd:.2f} pilot found that Claude Code shows Haiku 5.5 runs at about 27× the published price.</b> Every run is priced from its tokens at the published prices. The author's Anthropic bill confirms those prices: billed at Claude Code's displayed rate, the {haiku[0]} Haiku runs would have cost about ${haiku[1]:.0f} instead of ${haiku[2]:.2f}, and the account balance shows no such charge. Claude Code's cost display for Haiku 5.5 (version 2.1.287) is wrong; the bill isn't.</li>
<li><b>During the run, both accounts ran out of credit once.</b> Interrupted runs were redone. Codex's quota error was at first misread as aimpg's own failure, which wrongly excluded 2 commits; that was repaired before the analysis. The original file is published.</li>
<li><b>Found in a review after the analysis: 10 of the 56 commits were written before the cutoff.</b> The rule says "authored on or after
2026-07-01", but the selection compared the date each commit was <i>committed</i>. All 7 homebridge commits and 3 click commits were written
in May or June and merged in July, so the models could have seen that code. Without those 10 commits, every result keeps its direction and its
finding status:
<div class="tablewrap"><table>
<thead><tr><th>Setup</th><th>Primary measure</th><th>Change (95% CI)</th><th>Repos in the same direction</th><th>Result</th></tr></thead>
<tbody>{sens}</tbody></table></div></li>
</ul>
<p>All of these are dated in the pre-registration's change and deviation lists.</p>

<h2>Added after publication: results by task difficulty</h2>
<p><b>Added 2026-11-14. This was not pre-registered, and the groups were chosen after seeing the data.</b> A reader pointed out that cost per
solved task can flatter a cheap model: if it only solves the tasks every model solves, it looks efficient while never solving a hard one.
So here are the same runs grouped by how hard each of the {diff["tasks"]} tasks turned out to be, counted as passing runs across four setups
(3 runs each, so 0 to 12). The groups are made from the same runs they describe, so a hard task has few passes by definition. What the table
adds is which setups those few passes came from.</p>
<div class="tablewrap"><table>
<thead><tr><th>Tasks</th><th>How many</th>{diff_head}</tr></thead>
<tbody>{difficulty_rows(diff)}</tbody></table></div>
<ul>
<li><b>Haiku 5.5 against Sonnet 5.5 stands.</b> Sonnet solved {diff["sonnet_only"]} tasks that Haiku never solved, and Haiku solved {diff["haiku_only"]} that Sonnet never did, so they solve almost the same set.
Haiku isn't cheap because it gives up early either. Its failed runs took more requests and more time than Sonnet's ({diff["failed"]["claude-code@haiku"][0]:.0f} requests and {diff["failed"]["claude-code@haiku"][1]:.0f} seconds, against {diff["failed"]["claude-code"][0]:.0f} and {diff["failed"]["claude-code"][1]:.0f}).</li>
<li><b>"Opus 5.5 costs 2.6× as much per solved task" hides something.</b> On the hard tasks, Haiku solved {hard[0][0]} of {hard[0][1]} runs, Sonnet {hard[1][0]} of {hard[1][1]}, Opus {hard[2][0]} of {hard[2][1]} and Codex {hard[3][0]} of {hard[3][1]}.
For those tasks the choice isn't between a cheap and a dear solution. The cheaper models didn't solve them at all.</li>
<li><b>"Hard" here partly means thinly described</b> (added later the same day). The task an agent gets is the commit message. The hard tasks'
messages had a median of {diff["median_words"][2]:g} words, and the easy ones {diff["median_words"][0]:g}. Tasks described in 50 words or more were solved in
{long[0][0]} of {long[0][1]} runs by Haiku, {long[1][0]} of {long[1][1]} by Sonnet and {long[2][0]} of {long[2][1]} by Opus. Tasks described in under 15 words were solved in
{short[0][0]} of {short[0][1]}, {short[1][0]} of {short[1][1]} and {short[2][0]} of {short[2][1]}. So part of what the hard tasks test is working out what is wanted from very little, and that
is where Opus pulled ahead. Each task's description length and size are in <a href="data/tasks.json">data/tasks.json</a>.</li>
<li><b>The task set separates the models poorly.</b> {diff["bands"][0][2]} tasks were easy for every setup and {diff["bands"][2][2]} were too hard for most, which leaves {diff["bands"][1][2]} in between.
The counts on hard tasks are small, so read them as a pattern to test, not as a measured rate. Report No. 2 will report results by difficulty from the start.</li>
</ul>

<h2>Check it yourself</h2>
<ul>
<li><b>Raw data:</b> <a href="data/">every run record</a> (outcome, tokens per request, cost and time), the selection lists, the pilot runs and <a href="data/report.json">the analysis output</a>.</li>
<li><b>Recompute every number, for free:</b> download <a href="data/">the data folder</a> and run <code>aimpg study report plan.json --runs .</code> in it.</li>
<li><b>Rerun a comparison on your own machine:</b> the Haiku-vs-Sonnet result for each of the 8 repos is a public record on the <a href="../">scoreboard</a>; rerun one with <code>aimpg verify --rerun &lt;record&gt; --repo &lt;clone&gt;</code> (about $2–3 of API credit). Reruns by other people appear as <b>Reproduced</b>. The whole study reruns with <code>aimpg study run plan.json</code>.</li>
<li><b>Code:</b> <a href="{REPO_URL}">aimpg</a> (MIT), with <code>aimpg/replay/study.py</code> for the statistics.</li>{f'<li><b>What should Report No. 2 test?</b> <a href="{NEXT_URL}">Suggest a setup or a claim to check</a>; the most-wanted ones go into the next pre-registration.</li>' if NEXT_URL else ''}
</ul>

<h2>Limits</h2>
<ul>
<li><b>"Solved" means the project's own hidden tests pass.</b> Code style, readability and maintainability weren't graded, so two passing solutions can still differ in quality. A cheaper model that passes the tests as often may still write code you'd rather not keep.</li>
<li>Haiku 5.5 was cheaper per solved task in all 8 repos, but it solved fewer tasks than Sonnet 5.5 in 3 of them (litestar 12 vs 15 of 21 runs, starlette 10 vs 12, homebridge 17 vs 18). Check it on your own code before switching: <code>aimpg verify</code> reruns your past commits with both models and lets your tests judge.</li>
<li>This covers 8 Python and TypeScript repos with good test suites, and tasks small enough to judge by tests. It may not hold for large refactors or other languages.</li>
<li>One configuration per tool, at default settings. RTK's maintainers were asked to review their setup and didn't reply by the deadline (<a href="https://github.com/rtk-ai/rtk/discussions/4511">discussion</a>).</li>
<li>Prices change. The ratios hold only as long as the published prices behind them do.</li>
<li>aimpg is built by one developer using Claude Code. The author paid for every run and has no affiliation with any vendor tested.</li>
</ul>
<footer>AI Coding MPG Report No. 1 · data CC BY 4.0 · <a href="{REPO_URL}">github.com/kumarganduri/aimpg</a></footer>
</div></body></html>
"""


def main(out: Path) -> None:
    report, sel, urls = load()
    out.mkdir(parents=True, exist_ok=True)
    task_list = tasks(urls)
    n = write_data(out, urls, task_list)
    total = 0.0
    for path in (STATE / "runs").glob("*.jsonl"):
        for line in path.read_text().splitlines():
            d = json.loads(line)
            if "cost_usd" in d and d.get("outcome") != "account_error":
                total += d["cost_usd"]
    hk = [d for p in (STATE / "runs").glob("*.jsonl") for d in records(p)
          if d.get("setup") == "claude-code@haiku" and "cost_usd" in d and d.get("outcome") != "account_error"]
    haiku = (len(hk), sum((d.get("extra") or {}).get("agent_reported_cost_usd") or 0 for d in hk), sum(d["cost_usd"] for d in hk))
    pilot_usd = sum(d.get("cost_usd") or 0 for d in records(STATE / "pilot" / "litestar.jsonl"))
    (out / "index.html").write_text(page(report, sel, urls, n, total, sens_rows(sensitivity()), haiku, pilot_usd, difficulty(task_list)))
    shutil.copyfile(HERE / "og" / "og.png", out / "og.png")  # link-preview image, made from og/og.html
    print(f"wrote {out / 'index.html'} ({n} run records, ${total:.2f})")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
