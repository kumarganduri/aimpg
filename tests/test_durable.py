import time

import pytest

from aimpg import durable, ledger
from aimpg.receipt import working_changes

from gitrepo import Repo, days_ago

LINES = "def compute_total(items):\n    return sum(item.price for item in items)\n\ndef describe_order(order):\n    return f'order {order.id} with {len(order.items)} items'\n"


def rec(repo, sha, ts, usd=1.0):
    return {"repo": str(repo.path), "sha": sha, "ts": ts, "subject": "s", "author": "t@example.com", "requests": 3, "usd": usd, "wh_low": 1, "wh_high": 9, "first_seen": ts}


@pytest.fixture
def repo(tmp_path):
    r = Repo(tmp_path / "repo")
    r.commit({"base.py": "print('base line')\n"}, "base", days_ago(80))
    return r


def test_every_outcome(repo):
    t = days_ago(60)
    good = repo.commit({"good.py": LINES}, "feat: good", t)
    reworked = repo.commit({"redo.py": LINES}, "feat: soon redone", t + 100)
    repo.commit({"redo.py": "def compute_total(items):\n    return 0\n"}, "fix: rewrite", t + 5 * 86400)
    reverted = repo.commit({"oops.py": LINES}, "feat: oops", t + 200)
    repo.git("revert", "--no-edit", reverted, when=t + 300)
    repo.git("checkout", "-q", "-b", "side")
    side = repo.commit({"side.py": LINES}, "feat: never merged", t + 400)
    repo.git("checkout", "-q", "main")
    repo.commit({"later.py": "print('main keeps moving')\n"}, "later", days_ago(1))
    young = repo.commit({"young.py": LINES}, "feat: young", days_ago(3))

    data = {ledger.key(r["repo"], r["sha"]): r for r in (
        rec(repo, good, t), rec(repo, reworked, t + 100), rec(repo, reverted, t + 200),
        rec(repo, side, t + 400), rec(repo, young, days_ago(3)),
    )}
    j = durable.judge(data)
    assert [r["sha"] for r in j.durable] == [good]
    assert [r["sha"] for r in j.reworked] == [reworked]
    assert [r["sha"] for r in j.reverted] == [reverted]
    assert [r["sha"] for r in j.not_kept] == [side]
    assert [r["sha"] for r in j.too_new] == [young]
    assert j.cost_per_durable() == pytest.approx(4.0)  # $4 on 4 judged commits, 1 durable
    assert j.waste_ratio() == pytest.approx(0.75)
    text = "\n".join(working_changes(j))
    assert "1 of 4 judged commits are working changes (25%)" in text and "Cost per working change: $4.00" in text


def test_too_new_says_when(repo):
    sha = repo.commit({"a.py": LINES}, "feat", days_ago(5))
    j = durable.judge({"k": rec(repo, sha, days_ago(5))})
    assert j.judged == [] and j.cost_per_durable() is None
    assert "Available on" in "\n".join(working_changes(j))


def test_ledger_keeps_the_fuller_record(tmp_path):
    from aimpg.energy import WhRange
    from aimpg.model import Request, Task, Usage
    from aimpg.receipt import TaskEnergy

    path = tmp_path / "ledger.json"

    def energy(n):
        t = Task("/r", "abc", 1.0, "s", "exact", "kept", author="a")
        for i in range(n):
            t.add(Request(str(i), "s", "m", 0, Usage(output=1), "/r"))
        return TaskEnergy(t, WhRange(n, 2 * n), WhRange(0, 0), usd=n * 0.1)

    assert ledger.record([energy(5)], path) == 1
    ledger.record([energy(2)], path)  # older logs were deleted: fewer requests now
    assert ledger.load(path)[ledger.key("/r", "abc")]["requests"] == 5
    ledger.record([energy(8)], path)
    assert ledger.load(path)[ledger.key("/r", "abc")]["requests"] == 8
