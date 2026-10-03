import csv

from aimpg import share
from aimpg.attribution import Attribution
from aimpg.model import Request, Task, Usage

from gitrepo import Repo, days_ago


def task(sha, ts, n_requests, subject="feat: thing"):
    t = Task("/repo/app", sha, ts, subject, "exact", "kept", author="dev@example.com")
    for i in range(n_requests):
        t.add(Request(f"{sha}-{i}", "s", "claude-sonnet-5-5", ts - 10 + i, Usage(fresh_in=2, cache_write=1000, cache_read=20000, output=300), "/repo/app"))
    return t


def test_csv_rows_with_privacy_switches(tmp_path):
    a = Attribution(tasks=[task("a" * 40, 1_790_000_000, 3)])
    (row,) = share.csv_rows(a)
    assert row["repo"] == "app" and row["author"] == "dev@example.com" and row["subject"] == "feat: thing"
    assert 0 < row["energy_wh_low"] < row["energy_wh_high"] and row["usd_api_equivalent"] > 0
    assert row["co2_g_low"] < row["co2_g_high"]
    (private,) = share.csv_rows(a, subjects=False, authors=False)
    assert private["subject"] == "" and private["author"] == ""
    path = tmp_path / "out.csv"
    share.write_csv([row], path)
    assert list(csv.DictReader(open(path)))[0]["commit"] == "a" * 40


def test_pr_markdown_summarizes_only_branch_commits():
    a = Attribution(tasks=[task("a" * 40, 100.0, 3, "feat: one"), task("b" * 40, 200.0, 0, "fix: two"), task("c" * 40, 300.0, 2, "not on branch")])
    md = share.pr_markdown(a, ["a" * 40, "b" * 40, "d" * 40], "app")
    assert "AI energy for this pull request" in md and "phone charge" in md and "API-equivalent" in md
    assert "feat: one" in md and "not on branch" not in md
    assert "made together with the commit above" in md  # b had no AI work of its own
    assert "1 of 3 commits had their own AI work" in md


def test_pr_markdown_without_ai_work():
    md = share.pr_markdown(Attribution(), ["x" * 40], "app")
    assert "No AI-assisted work found for the 1 commits" in md


def test_branch_commits_lists_commits_since_base(tmp_path):
    repo = Repo(tmp_path / "r")
    repo.commit({"a.py": "x = 1\n"}, "base", days_ago(3))
    repo.git("checkout", "-q", "-b", "feature")
    sha = repo.commit({"a.py": "x = 2\n"}, "change", days_ago(2))
    assert share.branch_commits(repo.path, "main") == [sha]
