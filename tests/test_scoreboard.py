import json
import subprocess

import pytest

from aimpg import scoreboard as sb
from aimpg.cli import main
from aimpg.replay import setups as S
from aimpg.replay.run import Record
from aimpg.replay.verify import build_record

SONNET = "claude-sonnet-5-5"


def run(commit, setup, *, passed=True, usages=None, cost=0.10, model=SONNET, repeat=0):
    usages = usages or [[5, 3000, 9000, 400]] * 3
    return Record(f"{commit}-{setup}-{repeat}", "/repo", commit, setup, repeat, "passed" if passed else "tests_failed",
                  passed, model, cost, 33.0, usages, "ok", "", True, "claude")


def record(tmp_path, *, challenger=S.TERSE, chal_cost=0.10, chal_pass=True, public=False, n=3):
    repo = tmp_path / f"repo{len(list(tmp_path.iterdir()))}"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", f"root {repo.name}"], check=True)
    runs = [run(f"{i:040x}", "claude-code") for i in range(n)] + [
        run(f"{i:040x}", challenger.name, cost=chal_cost, passed=chal_pass) for i in range(n)]
    return build_record(runs, claim="", baseline=S.BASELINE, challenger=challenger, repo=str(repo), task_mode="tests", public=public)


def put(records_dir, login, upload):
    data = sb.encode(upload)
    path = records_dir / login / sb.file_name(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path.stem


# ---------------------------------------------------------------- upload view

def test_private_upload_carries_no_texts_ids_or_exact_numbers(tmp_path):
    mine = S.custom(append_prompt="internal: ask #team-secrets", claude_md="db.internal.corp")
    rec = record(tmp_path, challenger=mine)
    up = sb.upload_view(rec)
    text = json.dumps(up)
    assert "internal" not in text and "sha256" not in text and f"{0:040x}" not in text
    assert up["challenger"] == {"name": "custom", "agent": "claude"}
    assert {r["commit"] for r in up["runs"]} == {0, 1, 2}  # run-local indexes, not commit ids
    assert up["runs"][0]["tokens"] == [15, 9000, 27000, 1200]  # sums, 2 significant figures
    assert up["runs"][0]["requests"] == "1-5" and up["runs"][0]["wall_s"] == 30
    assert up["week"].startswith("20") and "-W" in up["week"] and "created" not in up
    assert up["repo_id"] and len(up["repo_id"]) == 24
    assert sb.validate_upload(tmp_path / "x" / sb.file_name(sb.encode(up)), sb.encode(up)) == []


def test_repo_id_is_stable_per_repo_and_keyed_by_a_local_secret(tmp_path):
    a = record(tmp_path)
    repo = tmp_path / "repo0"
    assert sb.repo_id(str(repo)) == a["repo_id"]
    sb.SECRET.write_text("00" * 32)  # another machine's secret → a different id for the same repo
    assert sb.repo_id(str(repo)) != a["repo_id"]


def test_catalog_setups_keep_their_names():
    for name in ("claude-code+rtk", "claude-code@haiku", "codex@gpt-6.1-sol"):
        assert sb._public_spec({"name": name, "agent": "claude"}, False)["name"] == name


# ---------------------------------------------------------------- validation

def valid(tmp_path, **kw):
    up = sb.upload_view(record(tmp_path, **kw))
    return up


def problems(tmp_path, up, author=None, folder="me"):
    data = sb.encode(up)
    return sb.validate_upload(tmp_path / folder / sb.file_name(data), data, author=author)


def test_validation_rejects_tampering_and_leaks(tmp_path):
    up = valid(tmp_path)
    assert problems(tmp_path, up, author="me") == []
    assert problems(tmp_path, up, author="someone-else")  # wrong folder for the PR author
    data = sb.encode(up)
    assert sb.validate_upload(tmp_path / "me" / "wrong.json", data)  # name must be the content hash

    bad = json.loads(json.dumps(up))
    bad["runs"][0]["passed"] = not bad["runs"][0]["passed"]
    assert any("passed" in p for p in problems(tmp_path, bad))

    leak = json.loads(json.dumps(up))
    leak["challenger"]["claude_md"] = "secret"
    leak["affiliation"] = "see https://corp.example"
    found = problems(tmp_path, leak)
    assert any("only name, agent and model" in p for p in found) and any("URLs" in p for p in found)

    partial = json.loads(json.dumps(up))
    partial["runs"] = partial["runs"][:-1]
    assert any("incomplete" in p for p in problems(tmp_path, partial))

    bidi = json.loads(json.dumps(up))
    bidi["affiliation"] = "acme‮"
    assert any("control" in p for p in problems(tmp_path, bidi))

    html_name = json.loads(json.dumps(up))
    html_name["challenger"]["name"] = "<script>"
    assert any("letters, digits" in p for p in problems(tmp_path, html_name))


def test_validation_rechecks_the_verdict_against_the_runs(tmp_path):
    up = valid(tmp_path, chal_pass=False)
    up["verdict"]["answer"] = "SUPPORTED"
    assert any("solves fewer" in p for p in problems(tmp_path, up))


# ---------------------------------------------------------------- aggregation

def many(tmp_path, records_dir, logins, **kw):
    shas = []
    for login in logins:
        shas.append(put(records_dir, login, sb.upload_view(record(tmp_path, **kw))))
    return shas


def test_numbers_appear_only_with_5_repos_and_3_people(tmp_path):
    d = tmp_path / "records"
    many(tmp_path, d, ["ann", "bob", "cat", "dan"], chal_cost=0.05)
    data = sb.aggregate(sb.load(d))
    assert data["self_reported"] == [] and data["collecting"] == ["claude-code+terse vs claude-code"]
    many(tmp_path, d, ["eve"], chal_cost=0.05)
    (row,) = sb.aggregate(sb.load(d))["self_reported"]
    assert row["repos"] == 5 and row["submitters"] == 5
    assert row["answers"]["NOT PROVEN"] == 5 and row["median_cost_ratio"] == pytest.approx(0.5)  # same model: energy decides
    assert sb.aggregate(sb.load(d))["reproduced"] == []  # self-reported never reaches the headline


def test_one_account_cannot_carry_a_claim(tmp_path):
    d = tmp_path / "records"
    many(tmp_path, d, ["ann"] * 6 + ["bob", "cat"])  # ann's 6 repos are capped at 3, and 3 of 5 runs > 50%
    data = sb.aggregate(sb.load(d))
    assert data["self_reported"] == []


def test_vendor_claims_dont_count_unless_reproduced(tmp_path):
    d = tmp_path / "records"
    many(tmp_path, d, ["rtk-inc", "ann", "bob", "cat", "dan"], challenger=S.RTK)
    assert sb.aggregate(sb.load(d), {"claude-code+rtk": ["rtk-inc"]})["self_reported"] == []
    assert sb.aggregate(sb.load(d))["self_reported"]  # without the vendor list it would count


def test_tiers_from_independent_reruns(tmp_path):
    d = tmp_path / "records"
    original = put(d, "ann", sb.upload_view(record(tmp_path, public=True)))
    same_person = sb.upload_view(record(tmp_path, public=True))
    same_person["rerun_of"] = original
    put(d, "ann", same_person)
    assert sb.tiers(sb.load(d))[original] == "self-reported"  # rerunning your own record proves nothing
    agree = sb.upload_view(record(tmp_path, public=True))
    agree["rerun_of"] = original
    put(d, "bob", agree)
    assert sb.tiers(sb.load(d))[original] == "reproduced"
    disagree = sb.upload_view(record(tmp_path, public=True, chal_pass=False))
    disagree["rerun_of"] = original
    put(d, "cat", disagree)
    assert sb.tiers(sb.load(d))[original] == "disputed"


def test_page_escapes_and_labels(tmp_path):
    d = tmp_path / "records"
    up = sb.upload_view(record(tmp_path, public=True))
    up["affiliation"] = "<b>acme</b>"
    put(d, "ann", up)
    data = sb.build(d, tmp_path / "site", demo=True)
    page = (tmp_path / "site" / "index.html").read_text()
    assert "DEMO DATA" in page and "not audited" in page and data["records"] == 1
    assert "<b>acme" not in page


# ---------------------------------------------------------------- submit

def test_submit_to_a_local_scoreboard_checkout(tmp_path, monkeypatch, capsys):
    rec = record(tmp_path, challenger=S.custom(append_prompt="secret prompt"))
    path = tmp_path / "r.record.json"
    path.write_text(json.dumps(rec))
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    board = tmp_path / "board"
    assert main(["submit", str(path), "--to-dir", str(board), "--login", "ann"]) == 0
    out = capsys.readouterr().out
    assert "This exact file will be published" in out and "secret prompt" not in out
    (written,) = list((board / "records" / "ann").glob("*.json"))
    assert main(["scoreboard", "validate", str(written), "--author", "ann"]) == 0


def test_submit_refuses_an_edited_record(tmp_path, monkeypatch, capsys):
    rec = record(tmp_path)
    rec["runs"][0]["passed"] = not rec["runs"][0]["passed"]
    path = tmp_path / "r.record.json"
    path.write_text(json.dumps(rec))
    assert main(["submit", str(path), "--to-dir", str(tmp_path), "--login", "ann", "--yes"]) == 1
    assert "fails `aimpg verify --check`" in capsys.readouterr().out
