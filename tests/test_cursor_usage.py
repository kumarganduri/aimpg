import csv
import time

import pytest

from aimpg.attribution import NO_COMMIT_YET, NOT_IN_REPO, attribute
from aimpg.cli import main
from aimpg.codex_logs import merge
from aimpg.cost import usage_cost
from aimpg.cursor_usage import CursorExportError, normalize_model, parse_cursor

from gitrepo import DAY, Repo
from test_attribution import iso

HEADER = ["Date", "User", "Kind", "Model", "Max Mode", "Input (w/ Cache Write)",
          "Input (w/o Cache Write)", "Cache Read", "Output Tokens", "Total Tokens", "Cost"]


def export(path, rows, header=HEADER):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        for r in rows:
            w.writerow([r.get(h, "") for h in header])
    return path


def row(ts, model="claude-4.6-sonnet-medium-thinking", user="Jane", write=48048, fresh=27, read=1415894, out=11329):
    return {"Date": iso(ts), "User": user, "Kind": "Included", "Model": model, "Max Mode": "No",
            "Input (w/ Cache Write)": write, "Input (w/o Cache Write)": fresh, "Cache Read": read,
            "Output Tokens": out, "Total Tokens": write + fresh + read + out, "Cost": "1.14"}


def test_columns_map_onto_our_buckets(tmp_path):
    parsed = parse_cursor([export(tmp_path / "u.csv", [row(1_000_000)])])
    (r,) = parsed.requests
    assert (r.usage.fresh_in, r.usage.cache_write, r.usage.cache_read, r.usage.output) == (27, 48048, 1415894, 11329)
    assert r.model == "claude-sonnet-4-6" and r.cwd == "" and r.session_id == "cursor:Jane"
    assert usage_cost(r.usage, r.model) is not None  # priced from Anthropic's table
    assert parsed.versions == {"cursor export": 1}


@pytest.mark.parametrize("cursor, ours", [
    ("claude-4.6-sonnet-medium-thinking", "claude-sonnet-4-6"),
    ("claude-4-opus", "claude-opus-4"),
    ("claude-opus-5.5", "claude-opus-5-5"),
    ("gpt-5.4-medium", "gpt-5.4-medium"),
])
def test_model_names(cursor, ours):
    assert normalize_model(cursor) == ours


def test_overlapping_exports_errors_and_other_users(tmp_path):
    rows = [row(1_000_000), row(1_000_100, user="John"), row(1_000_200, write=0, fresh=0, read=0, out=0)]
    a, b = export(tmp_path / "a.csv", rows), export(tmp_path / "b.csv", rows[:1])
    assert len(parse_cursor([a, b]).requests) == 2  # duplicate and zero-token rows dropped
    assert [r.session_id for r in parse_cursor([a], user="John").requests] == ["cursor:John"]


def test_personal_export_without_user_column(tmp_path):
    header = [h for h in HEADER if h != "User"]
    parsed = parse_cursor([export(tmp_path / "u.csv", [row(1_000_000)], header)], user="anyone")
    assert [r.session_id for r in parsed.requests] == ["cursor:me"]


def test_not_a_cursor_export(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("a,b\n1,2\n")
    with pytest.raises(CursorExportError):
        parse_cursor([bad])


def test_time_tier_matches_your_next_commit_within_2h(tmp_path):
    now = time.time()
    t = now - 20 * DAY
    repo = Repo(tmp_path / "repo")
    repo.commit({"a.py": "x = 0\n"}, "base", t - DAY)
    c1 = repo.commit({"a.py": "x = 1\n"}, "feat: one", t + 300)
    c2 = repo.commit({"a.py": "x = 2\n"}, "feat: two", t + 9 * 3600)  # 3h after the last request: too far
    rows = [row(t + 10), row(t + 200), row(t + 5 * 3600), row(t + 6 * 3600)]
    parsed = parse_cursor([export(tmp_path / "u.csv", rows)], str(repo.path))
    result = attribute(parsed, now - 30 * DAY, now)
    (task,) = result.tasks
    assert task.sha == c1 and task.attribution == "time" and len(task.requests) == 2
    assert c2 not in {t.sha for t in result.tasks}
    assert len(result.unattributed[NO_COMMIT_YET]) == 2


def test_without_a_repo_cursor_counts_in_totals_only(tmp_path):
    parsed = parse_cursor([export(tmp_path / "u.csv", [row(time.time() - DAY)])])
    result = attribute(merge(parsed), time.time() - 30 * DAY, time.time())
    assert result.tasks == [] and len(result.unattributed[NOT_IN_REPO]) == 1


def test_cli_cursor_only(tmp_path, capsys):
    now = time.time()
    repo = Repo(tmp_path / "repo")
    repo.commit({"a.py": "x = 0\n"}, "base", now - 3 * DAY)
    repo.commit({"a.py": "x = 1\n"}, "feat: cursor work", now - DAY + 300)
    csv_path = export(tmp_path / "u.csv", [row(now - DAY), row(now - DAY + 100)])
    code = main(["report", "--logs", str(tmp_path / "none"), "--cursor-usage", str(csv_path),
                 "--cursor-repo", str(repo.path)])
    out = capsys.readouterr().out
    assert code == 0 and "1 by time only" in out


def test_cli_missing_export(tmp_path, capsys):
    assert main(["report", "--cursor-usage", str(tmp_path / "nope.csv")]) == 1
    assert "not found" in capsys.readouterr().out
