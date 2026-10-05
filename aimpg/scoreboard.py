"""The public scoreboard: upload view, validation, aggregation and the static page.

The scoreboard is a separate public repo (records + CI + GitHub Pages). All of
its rules live here so they are tested with aimpg and pinned by version.
Decisions: docs/designs/scoreboard-brief.md.

    local record (~/.aimpg/verify/*.record.json, full)
        │ upload_view(): drops texts, per-request tokens and commit ids; rounds the rest
        ▼
    upload (records/<github-login>/<sha256>.json in the scoreboard repo)
        │ validate_upload(): CI on every pull request (data only, nothing executed)
        ▼
    build(): tiers, caps, thresholds → site/index.html + site/data.json

Privacy: a private upload holds model, setup, pass/fail, rounded cost, rounded
token sums, an energy range, coarse time and request count, and the week.
Never code, prompts, CLAUDE.md, commands, hosts, commit ids or the repo name.
A number is shown only when ≥5 repos and ≥3 submitters back it and no
submitter supplies more than half of its runs.

Trust: Reproduced (another account reran a public record and agreed),
Disputed (a rerun disagreed), Self-reported (everything else). Headlines use
reproduced records only; self-reported numbers are shown apart and labeled.
A vendor's records about its own product never count unless reproduced.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import math
import os
import re
import secrets
import statistics
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from aimpg.energy import ZERO, request_wh
from aimpg.model import Request, Usage

UPLOAD_VERSION = 2
SECRET = Path.home() / ".aimpg" / "id"  # never leaves the machine; makes repo ids unguessable
MAX_BYTES = 1_000_000
MIN_REPOS, MIN_SUBMITTERS, MAX_SHARE = 5, 3, 0.5
REPOS_PER_ACCOUNT = 3
OUTCOMES = {"passed", "tests_failed", "timeout", "budget_hit", "agent_error"}
ANSWERS = {"SUPPORTED", "NOT SUPPORTED", "NOT PROVEN"}
# Setups everyone can name; anything else is published as "custom" or "cmd".
_CATALOG = re.compile(r"^(claude-code(\+rtk|\+terse)?(@[\w.\-]+)?|codex(@[\w.\-]+)?)$")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f‎‏‪-‮⁦-⁩]")
FOOTER = ("Self-reported numbers are not audited: `aimpg verify --check` confirms the arithmetic, "
          "not that the runs happened. Only an independent rerun (Reproduced) is evidence.")


class UploadError(ValueError):
    pass


# ---------------------------------------------------------------- identity

def _secret() -> bytes:
    try:
        return bytes.fromhex(SECRET.read_text().strip())
    except (OSError, ValueError):
        SECRET.parent.mkdir(parents=True, exist_ok=True)
        value = secrets.token_bytes(32)
        fd = os.open(SECRET, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(value.hex())
        return value


def repo_id(repo: str) -> str | None:
    """Same repo → same id on this machine, so the scoreboard counts repos, not records.

    HMAC of the repo's first commit with a local secret: unlike a plain hash,
    nobody can test it against public repos.
    """
    proc = subprocess.run(["git", "-C", repo, "rev-list", "--max-parents=0", "HEAD"], capture_output=True, text=True)
    roots = sorted(proc.stdout.split())
    if proc.returncode != 0 or not roots:
        return None
    return hmac.new(_secret(), roots[0].encode(), hashlib.sha256).hexdigest()[:24]


# ---------------------------------------------------------------- upload view

def _sig2(n: float) -> int:
    if n <= 0:
        return 0
    digits = 2 - int(math.floor(math.log10(n))) - 1
    return int(round(n, digits))


def _bucket(n: int) -> str:
    return "1-5" if n <= 5 else "6-20" if n <= 20 else "21-50" if n <= 50 else "51+"


def _week(created: str) -> str:
    year, week, _ = datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).isocalendar()
    return f"{year}-W{week:02d}"


def _major_minor(version: str | None) -> str | None:
    m = re.search(r"(\d+)\.(\d+)", version or "")
    return f"{m.group(1)}.{m.group(2)}" if m else None


def claim_kind(baseline: dict, challenger: dict) -> str:
    if baseline.get("agent", "claude") != challenger.get("agent", "claude"):
        return "agent"
    if (baseline.get("model") or None) != (challenger.get("model") or None):
        return "model"
    return "setup"


def _public_spec(spec: dict, public: bool) -> dict:
    if public:
        return spec
    name = spec["name"] if _CATALOG.match(spec["name"]) else ("cmd" if spec.get("agent") == "cmd" else "custom")
    out = {"name": name, "agent": spec.get("agent", "claude")}
    if spec.get("model"):
        out["model"] = spec["model"]
    return out


def upload_view(record: dict, *, affiliation: str = "") -> dict:
    """What leaves the machine. A --public record keeps what a rerun needs (its owner chose that)."""
    if record.get("aimpg_record") != 1 or record.get("kind") != "verify":
        raise UploadError("not an aimpg verify record")
    public = bool(record.get("public"))
    if not public and not record.get("repo_id"):
        raise UploadError("this record has no repo id (made before aimpg 0.4.3); pass --repo to the repo it was made in")
    commits = {}
    runs = []
    for r in record["runs"]:
        usages = [Usage(*u) for u in r.get("usages", [])]
        wh = ZERO
        for u in usages:
            wh = wh + request_wh(Request("x", "s", r["model"], 0.0, u, "/"))
        sums = [sum(getattr(u, f) for u in usages) for f in ("fresh_in", "cache_write", "cache_read", "output")]
        runs.append({
            "commit": r["commit"] if public else commits.setdefault(r["commit"], len(commits)),
            "setup": _public_spec({"name": r["setup"], "agent": r.get("agent", "claude")}, public)["name"],
            "repeat": r["repeat"], "outcome": r["outcome"], "passed": r["passed"], "model": r["model"],
            "agent": r.get("agent", "claude"), "cost_known": r.get("cost_known", True),
            "cost_usd": round(r["cost_usd"], 2), "wall_s": int(round(r["wall_s"], -1)),
            "tokens": [_sig2(x) for x in sums] if usages else None,
            "wh": [round(wh.low, 2), round(wh.high, 2)] if usages else None,
            "requests": _bucket(len(usages)) if usages else None,
            "tokens_check": r.get("tokens_check", ""),
        })
    baseline, challenger = record["baseline"], record["challenger"]
    v = record["verdict"]
    out = {
        "aimpg_upload": UPLOAD_VERSION,
        "kind": "verify",
        "week": _week(record["created"]),
        "claim_kind": claim_kind(baseline, challenger),
        "task_mode": record["task_mode"],
        "public": public,
        "repo_id": record.get("repo_id"),
        "versions": {k: (val if k in ("aimpg", "factors", "prices") else _major_minor(val))
                     for k, val in record.get("versions", {}).items() if val},
        "baseline": _public_spec(baseline, public),
        "challenger": _public_spec(challenger, public),
        "runs": runs,
        "verdict": {"answer": v["answer"], "cost_ratio": v.get("cost_ratio"),
                    "energy": {k: v["energy"][k] for k in ("verdict", "effect_mid", "interval")} if v.get("energy") else None},
        "superseded_attempts": len(record.get("superseded_attempts", [])),
        "excluded_commits": len(record.get("excluded_commits", [])),
        "rerun_of": record.get("rerun_of"),
        "affiliation": affiliation.strip()[:100] or None,
    }
    if public:
        out["repo"] = record.get("repo")
    return out


def encode(upload: dict) -> bytes:
    return (json.dumps(upload, indent=1, sort_keys=True) + "\n").encode()


def file_name(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest() + ".json"


# ---------------------------------------------------------------- validation (CI)

def _summary(runs: list[dict], setup: str) -> dict:
    mine = [r for r in runs if r["setup"] == setup]
    solved = sum(r["passed"] for r in mine)
    priced = bool(mine) and all(r["cost_known"] for r in mine)
    usd = sum(r["cost_usd"] for r in mine)
    whs = [r["wh"] for r in mine if r.get("wh")]
    return {"runs": len(mine), "solved": solved, "solve_rate": solved / len(mine) if mine else None,
            "usd_per_solved": usd / solved if priced and solved else None,
            "wh_per_solved": [sum(w[0] for w in whs) / solved, sum(w[1] for w in whs) / solved] if whs and solved else None}


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def validate_upload(path: Path, data: bytes, *, author: str | None = None) -> list[str]:
    """Problems with one submitted file (empty = accept). Pure data checks; nothing is executed."""
    problems = []
    if len(data) > MAX_BYTES:
        return [f"{path.name}: larger than {MAX_BYTES} bytes"]
    if path.name != file_name(data):
        problems.append(f"{path.name}: the file name must be the SHA-256 of its content ({file_name(data)})")
    if author is not None and path.parent.name != author:
        problems.append(f"{path}: must be under records/{author}/ (the pull request's author)")
    try:
        u = json.loads(data)
    except ValueError:
        return problems + [f"{path.name}: not JSON"]
    if not isinstance(u, dict) or u.get("aimpg_upload") != UPLOAD_VERSION or u.get("kind") != "verify":
        return problems + [f"{path.name}: not an aimpg upload (version {UPLOAD_VERSION})"]
    text = json.dumps(u)
    if any(_CONTROL.search(t) for t in _strings(u)):
        problems.append("control or direction-changing characters are not allowed")
    if not u.get("public"):
        if re.search(r"https?://|\b[0-9a-f]{40}\b", text):
            problems.append("a private upload must not contain URLs or commit shas")
        for side in ("baseline", "challenger"):
            extra = set(u.get(side, {})) - {"name", "agent", "model"}
            if extra:
                problems.append(f"{side}: private uploads carry only name, agent and model (found {sorted(extra)})")
        if not u.get("repo_id"):
            problems.append("missing repo_id")
    for side in ("baseline", "challenger"):
        if not re.fullmatch(r"[\w.+@\-]{1,60}", str(u.get(side, {}).get("name", ""))):
            problems.append(f"{side}: name must be letters, digits and .+@- only")
    if not re.fullmatch(r"\d{4}-W\d{2}", str(u.get("week", ""))):
        problems.append("week must look like 2026-W40")
    if u.get("affiliation") is not None and (not isinstance(u["affiliation"], str) or len(u["affiliation"]) > 100):
        problems.append("affiliation: text up to 100 characters")
    runs = u.get("runs")
    if not isinstance(runs, list) or not runs:
        return problems + ["no runs"]
    names = {u.get("baseline", {}).get("name"), u.get("challenger", {}).get("name")}
    seen = set()
    for i, r in enumerate(runs):
        if r.get("outcome") not in OUTCOMES:
            problems.append(f"run {i}: unknown outcome")
        if bool(r.get("passed")) != (r.get("outcome") == "passed"):
            problems.append(f"run {i}: passed doesn't match outcome")
        if r.get("setup") not in names:
            problems.append(f"run {i}: setup is neither baseline nor challenger")
        if not isinstance(r.get("cost_usd"), (int, float)) or r["cost_usd"] < 0:
            problems.append(f"run {i}: bad cost")
        if r.get("tokens") is not None and not (len(r["tokens"]) == 4 and all(isinstance(x, int) and x >= 0 for x in r["tokens"])):
            problems.append(f"run {i}: tokens must be four non-negative whole numbers")
        key = (str(r.get("commit")), r.get("setup"), r.get("repeat"))
        if key in seen:
            problems.append(f"run {i}: duplicate")
        seen.add(key)
    commits, repeats = {k[0] for k in seen}, {k[2] for k in seen}
    if any((c, s, n) not in seen for c in commits for s in names for n in repeats):
        problems.append("incomplete: every commit × setup × repeat must be present (submit all runs, including failures)")
    v = u.get("verdict") or {}
    if v.get("answer") not in ANSWERS:
        problems.append("verdict answer must be SUPPORTED, NOT SUPPORTED or NOT PROVEN")
    if not problems:  # re-check what can be re-checked from the upload itself
        b, c = _summary(runs, u["baseline"]["name"]), _summary(runs, u["challenger"]["name"])
        if v["answer"] == "SUPPORTED" and (not c["solved"] or b["solve_rate"] - c["solve_rate"] > 0.10):
            problems.append("verdict says SUPPORTED but the challenger solves fewer tasks")
        if v.get("cost_ratio") and b["usd_per_solved"] and c["usd_per_solved"]:
            ratio = c["usd_per_solved"] / b["usd_per_solved"]
            if abs(ratio - v["cost_ratio"]) > 0.05 * max(1.0, v["cost_ratio"]):
                problems.append(f"cost ratio {v['cost_ratio']} doesn't match the runs ({ratio:.3f})")
    return problems


# ---------------------------------------------------------------- aggregation

def load(records_dir: Path) -> list[dict]:
    """[{submitter, sha, upload}] from records/<login>/<sha256>.json."""
    out = []
    for path in sorted(Path(records_dir).glob("*/*.json")):
        out.append({"submitter": path.parent.name, "sha": path.stem, "upload": json.loads(path.read_text())})
    return out


def _claim(u: dict) -> tuple[str, str]:
    return u["baseline"]["name"], u["challenger"]["name"]


def tiers(entries: list[dict]) -> dict[str, str]:
    """sha → reproduced | disputed | self-reported | rerun."""
    by_sha = {e["sha"]: e for e in entries}
    reruns = defaultdict(list)
    out = {}
    for e in entries:
        target = e["upload"].get("rerun_of")
        if target:
            out[e["sha"]] = "rerun"
            if target in by_sha and by_sha[target]["submitter"] != e["submitter"]:
                reruns[target].append(e["upload"]["verdict"]["answer"])
    for e in entries:
        if e["sha"] in out:
            continue
        answers = reruns.get(e["sha"], [])
        mine = e["upload"]["verdict"]["answer"]
        out[e["sha"]] = "disputed" if any(a != mine for a in answers) else "reproduced" if answers else "self-reported"
    return out


def _metrics(u: dict) -> dict:
    b, c = _summary(u["runs"], u["baseline"]["name"]), _summary(u["runs"], u["challenger"]["name"])
    return {"answer": u["verdict"]["answer"],
            "cost_ratio": c["usd_per_solved"] / b["usd_per_solved"] if b["usd_per_solved"] and c["usd_per_solved"] else None,
            "solve_diff": (c["solve_rate"] or 0) - (b["solve_rate"] or 0),
            "runs": len(u["runs"])}


def aggregate(entries: list[dict], vendors: dict[str, list[str]] | None = None) -> dict:
    vendors = vendors or {}
    tier = tiers(entries)
    groups = {"reproduced": defaultdict(list), "self-reported": defaultdict(list)}
    collecting = set()
    for e in entries:
        u, t = e["upload"], tier[e["sha"]]
        claim = _claim(u)
        if t in ("rerun", "disputed") or "custom" in claim or "cmd" in claim:
            continue
        if e["submitter"] in vendors.get(claim[1], []) and t != "reproduced":
            continue  # a vendor's own claim counts only once someone else reproduces it
        groups[t][claim].append(e)
    cells = {}
    for t, by_claim in groups.items():
        rows = []
        for claim, es in sorted(by_claim.items()):
            # one unit per repo (newest record), at most REPOS_PER_ACCOUNT repos per account
            per_repo = {}
            for e in sorted(es, key=lambda e: e["upload"]["week"]):
                per_repo[e["upload"].get("repo_id") or e["upload"].get("repo")] = e
            kept, per_account = [], defaultdict(int)
            for e in sorted(per_repo.values(), key=lambda e: e["upload"]["week"], reverse=True):
                if per_account[e["submitter"]] < REPOS_PER_ACCOUNT:
                    per_account[e["submitter"]] += 1
                    kept.append(e)
            metrics = [(e["submitter"], _metrics(e["upload"])) for e in kept]
            runs = defaultdict(int)
            for s, m in metrics:
                runs[s] += m["runs"]
            total = sum(runs.values())
            enough = (len(kept) >= MIN_REPOS and len(runs) >= MIN_SUBMITTERS
                      and max(runs.values()) <= MAX_SHARE * total)
            if not enough:
                collecting.add(claim)
                continue
            ratios = [m["cost_ratio"] for _, m in metrics if m["cost_ratio"] is not None]
            rows.append({
                "baseline": claim[0], "challenger": claim[1],
                "repos": len(kept), "submitters": len(runs),
                "answers": {a: sum(m["answer"] == a for _, m in metrics) for a in sorted(ANSWERS)},
                "median_cost_ratio": statistics.median(ratios) if ratios else None,
                "median_solve_diff": statistics.median(m["solve_diff"] for _, m in metrics),
            })
        cells[t] = rows
    shown = {(r["baseline"], r["challenger"]) for rows in cells.values() for r in rows}
    public = [{"sha": e["sha"], "submitter": e["submitter"], "tier": tier[e["sha"]], "week": e["upload"]["week"],
               "baseline": e["upload"]["baseline"]["name"], "challenger": e["upload"]["challenger"]["name"],
               "answer": e["upload"]["verdict"]["answer"], "repo": e["upload"].get("repo")}
              for e in entries if e["upload"].get("public")]
    disputed = [{"sha": e["sha"], "submitter": e["submitter"], "challenger": e["upload"]["challenger"]["name"],
                 "answer": e["upload"]["verdict"]["answer"]} for e in entries if tier[e["sha"]] == "disputed"]
    return {
        "rules": {"min_repos": MIN_REPOS, "min_submitters": MIN_SUBMITTERS, "max_share": MAX_SHARE,
                  "repos_per_account": REPOS_PER_ACCOUNT},
        "records": len(entries),
        "reproduced": cells.get("reproduced", []),
        "self_reported": cells.get("self-reported", []),
        "collecting": sorted(f"{c} vs {b}" for b, c in collecting - shown),
        # trusted first; reruns (evidence for another record) last
        "public_records": sorted(sorted(public, key=lambda p: p["week"], reverse=True),
                                 key=lambda p: ["reproduced", "disputed", "self-reported", "rerun"].index(p["tier"])),
        "disputed": disputed,
        "footer": FOOTER,
    }


# ---------------------------------------------------------------- page

def _pct(x: float | None, signed: bool = True) -> str:
    return "—" if x is None else (f"{x:+.0%}" if signed else f"{x:.0%}")


def _table(rows: list[dict]) -> str:
    if not rows:
        return '<p class="empty">Nothing here yet: no claim has enough repos and people behind it.</p>'
    out = ['<table><thead><tr><th>Claim</th><th>Result across repos</th><th>Cost per solved task</th>'
           '<th>Tasks solved</th><th>Based on</th></tr></thead><tbody>']
    for r in rows:
        a = r["answers"]
        result = (f'<span class="yes">{a["SUPPORTED"]} supported</span> · <span class="no">{a["NOT SUPPORTED"]} not</span>'
                  f' · <span class="maybe">{a["NOT PROVEN"]} not proven</span>')
        cost = "—" if r["median_cost_ratio"] is None else _pct(r["median_cost_ratio"] - 1)
        out.append(f'<tr><td><b>{html.escape(r["challenger"])}</b><br><small>vs {html.escape(r["baseline"])}</small></td>'
                   f'<td>{result}</td><td>{cost} <small>median</small></td><td>{r["median_solve_diff"] * 100:+.0f} pts <small>median</small></td>'
                   f'<td>{r["repos"]} repos · {r["submitters"]} people</td></tr>')
    return "\n".join(out + ["</tbody></table>"])


def render(data: dict, *, demo: bool = False) -> str:
    esc = html.escape
    public_rows = "".join(
        f'<tr><td><span class="tier {esc(p["tier"])}">{esc(p["tier"])}</span></td><td>{esc(p["challenger"])} '
        f'<small>vs {esc(p["baseline"])}</small></td><td>{esc(p["answer"])}</td><td>{esc(p["submitter"])}</td>'
        f'<td>{esc(p["week"])}</td><td><a href="records/{esc(p["submitter"])}/{esc(p["sha"])}.json">record</a></td></tr>'
        for p in data["public_records"]) or '<tr><td colspan="6" class="empty">No public records yet.</td></tr>'
    collecting = ", ".join(esc(c) for c in data["collecting"]) or "—"
    disputed = "".join(f"<li>{esc(d['challenger'])}: {esc(d['answer'])} by {esc(d['submitter'])}, a rerun disagreed "
                       f'(<a href="records/{esc(d["submitter"])}/{esc(d["sha"])}.json">record</a>)</li>'
                       for d in data["disputed"]) or "<li>None.</li>"
    rules = data["rules"]
    footer = re.sub(r"`([^`]+)`", r"<code>\1</code>", esc(data["footer"]))
    banner = '<p class="demo">DEMO DATA: made-up records to preview the page. Not real results.</p>' if demo else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>aimpg scoreboard</title>
<style>
:root{{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#6b6a65;--line:#e4e1d8;--yes:#1f7a4d;--no:#b3261e;--maybe:#8a6d00;--chip:#efece4}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151513;--fg:#ecebe6;--muted:#a3a19a;--line:#33322e;--yes:#5cc18f;--no:#f08a80;--maybe:#e0c063;--chip:#26251f}}}}
body{{background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,-apple-system,sans-serif;margin:0}}
main{{max-width:960px;margin:0 auto;padding:32px 16px 64px}}
h1{{font-size:28px;margin:0 0 4px}} h2{{font-size:19px;margin:36px 0 8px}} p{{margin:6px 0}}
.lede{{color:var(--muted);max-width:680px}} small{{color:var(--muted)}}
.wrap{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;font-size:15px}}
th,td{{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}}
th{{font-size:13px;color:var(--muted);font-weight:600}}
.yes{{color:var(--yes)}} .no{{color:var(--no)}} .maybe{{color:var(--maybe)}}
.tier{{font-size:12px;padding:2px 8px;border-radius:99px;background:var(--chip)}}
.tier.reproduced{{color:var(--yes)}} .tier.disputed{{color:var(--no)}}
.empty{{color:var(--muted)}} .demo{{background:var(--chip);padding:8px 12px;border-radius:8px;font-weight:600}}
footer{{margin-top:40px;color:var(--muted);font-size:14px;border-top:1px solid var(--line);padding-top:12px}}
code{{background:var(--chip);padding:1px 5px;border-radius:4px;font-size:14px}} a{{color:inherit}}
</style></head><body><main>
{banner}
<h1>aimpg scoreboard</h1>
<p class="lede">Do token-savers, prompts, models and agents really save money on real code? Each result reruns a
developer's own past commits both ways, and their own tests judge them. Made with
<a href="https://github.com/kumarganduri/aimpg">aimpg verify</a>; {data["records"]} records so far.</p>

<h2>Reproduced</h2>
<p class="lede">Counted only when someone else reran a public record and got the same answer.</p>
<div class="wrap">{_table(data["reproduced"])}</div>

<h2>Self-reported <small>(not audited)</small></h2>
<div class="wrap">{_table(data["self_reported"])}</div>

<h2>Collecting data</h2>
<p>{collecting}</p>
<p><small>A number appears once at least {rules["min_repos"]} repos and {rules["min_submitters"]} people back it, with no one
supplying more than {rules["max_share"]:.0%} of the runs. Each account counts for at most {rules["repos_per_account"]} repos per claim.
Results use the median repo, never an average.</small></p>

<h2>Disputed</h2><ul>{disputed}</ul>

<h2>Public records <small>(rerunnable)</small></h2>
<div class="wrap"><table><thead><tr><th>Tier</th><th>Claim</th><th>Answer</th><th>By</th><th>Week</th><th></th></tr></thead>
<tbody>{public_rows}</tbody></table></div>

<footer><p>{footer}</p>
<p>Add yours: <code>aimpg verify ...</code> then <code>aimpg submit</code>. Private uploads carry no code, prompts,
commit ids or repo names. Withdraw anytime by pull request.</p></footer>
</main></body></html>
"""


def build(records_dir: Path, out_dir: Path, vendors_file: Path | None = None, *, demo: bool = False) -> dict:
    vendors = json.loads(vendors_file.read_text()) if vendors_file and vendors_file.exists() else {}
    data = aggregate(load(records_dir), vendors)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "data.json").write_text(json.dumps(data, indent=1))
    (out_dir / "index.html").write_text(render(data, demo=demo))
    return data


# ---------------------------------------------------------------- command line

SCOREBOARD_REPO = "kumarganduri/aimpg-scoreboard"


def add_parsers(sub) -> None:
    s = sub.add_parser("submit", help="share a verify record on the public scoreboard (shows everything first, asks)")
    s.add_argument("record", type=Path)
    s.add_argument("--affiliation", default="", help="who you work for, if it relates to the claim (shown publicly)")
    s.add_argument("--repo", type=Path, help="the repo the record was made in (only for records from before 0.4.3)")
    s.add_argument("--to-dir", type=Path, help="write into a local scoreboard checkout instead of opening a pull request")
    s.add_argument("--login", help="your GitHub username (with --to-dir; otherwise taken from gh)")
    s.add_argument("--scoreboard", default=SCOREBOARD_REPO, help=f"scoreboard repo (default {SCOREBOARD_REPO})")
    s.add_argument("--yes", action="store_true", help="skip the confirmation")

    b = sub.add_parser("scoreboard", help="scoreboard maintenance (used by its CI)")
    bsub = b.add_subparsers(dest="scoreboard_command", required=True)
    v = bsub.add_parser("validate", help="check submitted files")
    v.add_argument("files", type=Path, nargs="+")
    v.add_argument("--author", help="pull request author: files must be under records/<author>/")
    g = bsub.add_parser("build", help="aggregate records into the static page")
    g.add_argument("records", type=Path)
    g.add_argument("out", type=Path)
    g.add_argument("--vendors", type=Path, help="vendors.json: {challenger: [github logins]}")
    g.add_argument("--demo", action="store_true", help="label the page as demo data")


def main(args) -> int:
    if args.command == "submit":
        try:
            return _submit(args)
        except UploadError as exc:
            print(f"submit: {exc}")
            return 1
    if args.scoreboard_command == "validate":
        bad = 0
        for f in args.files:
            problems = validate_upload(f, f.read_bytes(), author=args.author)
            print(f"{'✗' if problems else '✓'} {f}")
            for p in problems:
                print(f"    {p}")
            bad += bool(problems)
        return 1 if bad else 0
    data = build(args.records, args.out, args.vendors, demo=args.demo)
    print(f"Built {args.out / 'index.html'} from {data['records']} records.")
    return 0


def _submit(args) -> int:
    from aimpg.replay.verify import check, integrity

    record = json.loads(args.record.read_text())
    if not record.get("repo_id") and args.repo:
        record["repo_id"] = repo_id(str(args.repo))
    ok, _ = check(record)
    problems, _ = integrity(record)
    if not ok or problems:
        raise UploadError("the record fails `aimpg verify --check`; only unmodified records can be submitted")
    upload = upload_view(record, affiliation=args.affiliation)
    data = encode(upload)
    name = file_name(data)
    issues = validate_upload(Path("records") / "x" / name, data)
    if issues:
        raise UploadError("; ".join(issues))

    print("This exact file will be published on the public scoreboard:\n")
    print(data.decode())
    if upload["public"]:
        print("It's a --public record: it includes the repo URL, commit ids and setup texts, so anyone can rerun it.")
    else:
        print("Not included: your code, prompts, CLAUDE.md, commands, commit ids, repo name, exact token counts or dates.")
    print("Your GitHub username will be shown with it. You can withdraw it anytime with a pull request that deletes it.")
    if not args.yes and input("Publish it? [y/N] ").strip().lower() != "y":
        print("Stopped. Nothing was sent.")
        return 1

    if args.to_dir:
        if not args.login:
            raise UploadError("--to-dir needs --login (your GitHub username)")
        dest = args.to_dir / "records" / args.login / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        print(f"Written to {dest}")
        return 0
    url = _open_pull_request(args.scoreboard, name, data, upload)
    print(f"Pull request: {url}\nThe scoreboard's checks run on it; once merged, the page updates.")
    return 0


def _gh(*argv: str, input_text: str | None = None) -> str:
    proc = subprocess.run(["gh", *argv], capture_output=True, text=True, input=input_text)
    if proc.returncode != 0:
        raise UploadError(f"gh {argv[0]} failed: {(proc.stderr or proc.stdout).strip()[-300:]}")
    return proc.stdout.strip()


def _open_pull_request(upstream: str, name: str, data: bytes, upload: dict) -> str:
    """Fork (if needed), add the file on a new branch through the API (no clone), open the PR."""
    import base64
    import time

    try:
        login = _gh("api", "user", "-q", ".login")
    except (UploadError, OSError):
        raise UploadError("needs the GitHub CLI, logged in: brew install gh && gh auth login") from None
    owner_repo = upstream
    if login.lower() != upstream.split("/")[0].lower():
        _gh("repo", "fork", upstream, "--clone=false")
        owner_repo = f"{login}/{upstream.split('/')[1]}"
        for _ in range(10):  # forks are created asynchronously
            try:
                _gh("repo", "sync", owner_repo)
                break
            except UploadError:
                time.sleep(3)
    base = _gh("api", f"repos/{upstream}", "-q", ".default_branch")
    sha = _gh("api", f"repos/{owner_repo}/git/ref/heads/{base}", "-q", ".object.sha")
    branch = f"aimpg-{name[:12]}"
    _gh("api", "-X", "POST", f"repos/{owner_repo}/git/refs", "-f", f"ref=refs/heads/{branch}", "-f", f"sha={sha}")
    _gh("api", "-X", "PUT", f"repos/{owner_repo}/contents/records/{login}/{name}",
        "-f", f"message=verify: {upload['challenger']['name']} vs {upload['baseline']['name']}",
        "-f", f"content={base64.b64encode(data).decode()}", "-f", f"branch={branch}")
    body = (f"**Claim:** {upload['challenger']['name']} vs {upload['baseline']['name']} → "
            f"{upload['verdict']['answer']}\n\n**Affiliation:** {upload.get('affiliation') or 'none stated'}\n\n"
            f"Submitted with `aimpg submit`. {FOOTER}")
    return _gh("pr", "create", "--repo", upstream, "--head", f"{login}:{branch}" if owner_repo != upstream else branch,
               "--base", base, "--title", f"verify: {upload['challenger']['name']} vs {upload['baseline']['name']}", "--body", body)
