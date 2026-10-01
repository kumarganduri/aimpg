"""Speed budget: a 500MB log history + a busy repo must report in under 10s.

Builds ~50MB of synthetic logs and a 2,000-commit repo, then extrapolates
parse time linearly to 500MB. Run with `pytest -m slow`.
"""

import json
import subprocess
import time
from datetime import datetime, timezone

import pytest

from aimpg.attribution import attribute
from aimpg.logs import parse_logs

from conftest import assistant, bash_call, tool_result
from gitrepo import DAY

pytestmark = pytest.mark.slow
BUDGET = 10.0


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def fast_import_repo(path, n: int, start: float) -> list[float]:
    """n commits in one `git fast-import` (seconds, not minutes)."""
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@example.com"], check=True)
    lines, times = [], []
    for i in range(n):
        ts = int(start + i * 600)
        times.append(ts)
        body = f"value_{i} = {i} * compute_something_meaningful()\n"
        lines += [
            "commit refs/heads/main",
            f"committer T <t@example.com> {ts} +0000",
            f"data {len(f'c{i}')}",
            f"c{i}",
            f"M 644 inline f{i % 50}.py",
            f"data {len(body)}",
            body.rstrip("\n"),
        ]
    subprocess.run(["git", "-C", str(path), "fast-import", "--quiet"], input="\n".join(lines) + "\n", text=True, check=True)
    subprocess.run(["git", "-C", str(path), "checkout", "-q", "main"], check=True)
    return times


def test_report_pipeline_fits_budget(tmp_path):
    now = time.time()
    start = now - 20 * DAY
    repo = tmp_path / "repo"
    commit_times = fast_import_repo(repo, 2000, start)
    cwd = str(repo)

    log = tmp_path / "projects" / "-x" / "big.jsonl"
    log.parent.mkdir(parents=True)
    with open(log, "w") as fh:
        for i, ts in enumerate(commit_times):
            for k in range(30):  # ~30 rows per commit, repeated usage rows like real logs
                row = assistant(f"r{i}_{k // 3}", iso(ts - 300 + k), session=f"s{i % 5}", cwd=cwd)
                row["message"]["content"][0]["text"] = "x" * 1200
                fh.write(json.dumps(row) + "\n")
            fh.write(json.dumps(bash_call(f"t{i}", "git commit -m c", iso(ts - 1), session=f"s{i % 5}", cwd=cwd)) + "\n")
            fh.write(json.dumps(tool_result(f"t{i}", iso(ts + 1), session=f"s{i % 5}", cwd=cwd)) + "\n")
    size = log.stat().st_size

    t0 = time.perf_counter()
    parsed = parse_logs([log])
    parse_s = time.perf_counter() - t0
    t1 = time.perf_counter()
    result = attribute(parsed, now - 30 * DAY, now)
    attribute_s = time.perf_counter() - t1

    projected = parse_s * (500e6 / size) + attribute_s
    print(f"\n{size / 1e6:.0f}MB parsed in {parse_s:.2f}s, attribute {attribute_s:.2f}s, projected 500MB report {projected:.1f}s")
    assert len(result.tasks) == 2000
    assert projected < BUDGET, f"parse {parse_s:.2f}s for {size / 1e6:.0f}MB, attribute {attribute_s:.2f}s"
